"""
Unit tests for the circuit breaker.

Tests verify state transitions:
  CLOSED → OPEN (after failure_threshold failures)
  OPEN   → HALF_OPEN (after timeout)
  HALF_OPEN → CLOSED (after success_threshold successes)
  HALF_OPEN → OPEN (if probe fails)
"""
import sys
sys.path.insert(0, "services")

import time
import pytest
from moto import mock_aws


def _make_cb_table(boto3_client):
    boto3_client.create_table(
        TableName="test-circuit-breakers",
        AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
def test_circuit_stays_closed_on_success(aws_env):
    """Successful calls don't change the circuit state."""
    import boto3
    from shared.circuit_breaker import CircuitBreaker, CircuitState

    _make_cb_table(boto3.client("dynamodb", region_name="us-east-1"))

    cb = CircuitBreaker("test-success", failure_threshold=3, timeout_seconds=60)
    cb.reset()

    for _ in range(5):
        result = cb.call(lambda: "ok")
        assert result == "ok"

    state = cb._get_state()
    assert state["circuit_state"] == CircuitState.CLOSED


@mock_aws
def test_circuit_opens_after_threshold_failures(aws_env):
    """Circuit opens after hitting the failure threshold."""
    import boto3
    from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

    _make_cb_table(boto3.client("dynamodb", region_name="us-east-1"))

    cb = CircuitBreaker("test-open", failure_threshold=3, timeout_seconds=60)
    cb.reset()

    def always_fails():
        raise ConnectionError("Provider down")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            cb.call(always_fails)

    # Next call should fast-fail with CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        cb.call(always_fails)

    assert "OPEN" in str(exc_info.value)


@mock_aws
def test_circuit_allows_probe_after_timeout(aws_env):
    """After timeout, circuit transitions to HALF_OPEN and allows one probe."""
    import boto3
    from shared.circuit_breaker import CircuitBreaker

    _make_cb_table(boto3.client("dynamodb", region_name="us-east-1"))

    cb = CircuitBreaker("test-halfopen", failure_threshold=2, timeout_seconds=1)
    cb.reset()

    def always_fails():
        raise ConnectionError("down")

    # Open the circuit
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(always_fails)

    # Wait for timeout
    time.sleep(1.1)

    # Probe should be allowed through (returns success)
    call_count = [0]

    def probe():
        call_count[0] += 1
        return "probe_ok"

    result = cb.call(probe)
    assert result == "probe_ok"
    assert call_count[0] == 1


@mock_aws
def test_concurrent_failures_from_stale_state_still_trip(aws_env):
    """Two failures reported against the same stale snapshot must both count.

    Simulates concurrent Lambda invocations that each read state before either
    writes. With a read-modify-write counter both would write failure_count=1
    and the breaker would never trip; the atomic ADD guarantees the second
    increment lands and opens the circuit.
    """
    import boto3
    from shared.circuit_breaker import CircuitBreaker, CircuitState

    _make_cb_table(boto3.client("dynamodb", region_name="us-east-1"))

    cb = CircuitBreaker("test-race", failure_threshold=2, timeout_seconds=60)
    cb.reset()

    stale = cb._get_state()  # both "invocations" observe the same CLOSED snapshot
    cb._record_failure(stale)
    cb._record_failure(stale)

    state = cb._get_state()
    assert int(state.get("failure_count", 0)) == 2
    assert state["circuit_state"] == CircuitState.OPEN


@mock_aws
def test_circuit_closes_after_probe_successes(aws_env):
    """Circuit closes after success_threshold successful probes in HALF_OPEN."""
    import boto3
    from shared.circuit_breaker import CircuitBreaker, CircuitState

    _make_cb_table(boto3.client("dynamodb", region_name="us-east-1"))

    cb = CircuitBreaker("test-close", failure_threshold=2, success_threshold=2, timeout_seconds=1)
    cb.reset()

    def always_fails():
        raise ConnectionError("down")

    # Open the circuit
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(always_fails)

    time.sleep(1.1)

    # Two successful probes should close the circuit
    cb.call(lambda: "ok1")
    cb.call(lambda: "ok2")

    state = cb._get_state()
    assert state["circuit_state"] == CircuitState.CLOSED
    assert int(state.get("failure_count", 0)) == 0

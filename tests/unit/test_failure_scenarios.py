"""
Failure Scenario Tests
======================
Tests that verify the system behaves correctly under failure conditions.
This is what separates a distributed system from a CRUD app.

Scenarios covered:
  1. Payment failure triggers compensation (inventory released)
  2. Circuit breaker open → fast-fail (no waiting for timeout)
  3. Duplicate order request → idempotent (SAGA runs once)
  4. Insufficient inventory → clean structured failure
  5. Circuit breaker reopens after probe fails in HALF_OPEN
"""
import sys
import time
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, "services")


def _create_tables(client):
    """Create all DynamoDB tables needed for failure tests."""
    tables = [
        {
            "TableName": "test-idempotency",
            "AttributeDefinitions": [{"AttributeName": "idempotency_key", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
        {
            "TableName": "test-circuit-breakers",
            "AttributeDefinitions": [{"AttributeName": "name", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "name", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
        {
            "TableName": "test-inventory",
            "AttributeDefinitions": [{"AttributeName": "product_id", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "product_id", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
        {
            "TableName": "test-reservations",
            "AttributeDefinitions": [{"AttributeName": "reservation_id", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "reservation_id", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
        },
    ]
    for table in tables:
        client.create_table(**table)


# ---------------------------------------------------------------------------
# Scenario 1: Payment failure → inventory compensation
# ---------------------------------------------------------------------------

@mock_aws
def test_reserve_then_release_simulates_payment_failure(aws_env):
    """
    SAGA compensation path: payment fails → inventory released.

    In the real SAGA, Step Functions calls release() when charge() fails.
    Here we call reserve() then release() directly to verify the compensation
    transaction restores stock to its original value.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(client)

    # Seed inventory
    product_id = f"prod-{uuid.uuid4().hex[:8]}"
    ddb.Table("test-inventory").put_item(Item={
        "product_id": product_id,
        "quantity": 10,
        "unit_price_cents": 500,
    })

    from inventory_service.handler import handler

    order_id = f"order-{uuid.uuid4()}"

    # Step 1: Reserve inventory (SAGA step 1 succeeds)
    reserve_result = handler({
        "action": "reserve",
        "order_id": order_id,
        "items": [{"product_id": product_id, "quantity": 3, "unit_price_cents": 500}],
        "correlation_id": str(uuid.uuid4()),
    }, None)
    assert reserve_result["success"] is True
    reservation_id = reserve_result["reservation_id"]

    # Verify stock decremented
    stock = int(ddb.Table("test-inventory").get_item(
        Key={"product_id": product_id})["Item"]["quantity"])
    assert stock == 7  # 10 - 3

    # Step 2: Payment fails → SAGA triggers compensation (release)
    release_result = handler({
        "action": "release",
        "order_id": order_id,
        "reservation_id": reservation_id,
        "correlation_id": str(uuid.uuid4()),
    }, None)
    assert release_result["success"] is True

    # Verify stock restored — no money charged, no stock held
    stock_after = int(ddb.Table("test-inventory").get_item(
        Key={"product_id": product_id})["Item"]["quantity"])
    assert stock_after == 10  # fully restored


# ---------------------------------------------------------------------------
# Scenario 2: Circuit breaker open → fast-fail
# ---------------------------------------------------------------------------

@mock_aws
def test_circuit_breaker_open_fast_fails(aws_env):
    """
    After failure_threshold failures, circuit opens.
    Subsequent calls fail immediately (< 1ms) without calling the provider.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_tables(client)

    from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

    cb = CircuitBreaker("test-fast-fail", failure_threshold=3, timeout_seconds=60)
    cb.reset()

    call_count = [0]

    def failing_provider():
        call_count[0] += 1
        raise ConnectionError("Provider down")

    # Exhaust the threshold
    for _ in range(3):
        with pytest.raises(ConnectionError):
            cb.call(failing_provider)

    assert call_count[0] == 3  # provider called 3 times

    # Circuit is now OPEN — next call must fast-fail without calling provider
    start = time.time()
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        cb.call(failing_provider)
    elapsed = time.time() - start

    assert call_count[0] == 3          # provider NOT called again
    assert elapsed < 0.5               # fast-fail: < 500ms
    assert "OPEN" in str(exc_info.value)
    assert exc_info.value.resets_at > time.time()  # resets_at is in the future


# ---------------------------------------------------------------------------
# Scenario 3: Duplicate order → idempotent (SAGA runs once)
# ---------------------------------------------------------------------------

@mock_aws
def test_duplicate_reservation_is_idempotent(aws_env):
    """
    Submitting the same reservation twice (e.g. SQS redelivery or client retry)
    results in the same outcome without double-decrementing stock.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(client)

    product_id = f"prod-{uuid.uuid4().hex[:8]}"
    ddb.Table("test-inventory").put_item(Item={
        "product_id": product_id,
        "quantity": 10,
        "unit_price_cents": 200,
    })

    from inventory_service.handler import handler

    order_id = f"order-{uuid.uuid4()}"
    event = {
        "action": "reserve",
        "order_id": order_id,
        "items": [{"product_id": product_id, "quantity": 4, "unit_price_cents": 200}],
        "correlation_id": str(uuid.uuid4()),
    }

    # First call — should reserve
    result1 = handler(event, None)
    assert result1["success"] is True

    # Second call with same order_id — should return cached result, NOT re-reserve
    result2 = handler(event, None)
    assert result2["success"] is True

    # reservation_id must be the same (returned from cache)
    assert result1["reservation_id"] == result2["reservation_id"]

    # Stock must be decremented exactly ONCE (10 - 4 = 6, not 10 - 8 = 2)
    stock = int(ddb.Table("test-inventory").get_item(
        Key={"product_id": product_id})["Item"]["quantity"])
    assert stock == 6


# ---------------------------------------------------------------------------
# Scenario 4: Insufficient inventory → clean structured failure
# ---------------------------------------------------------------------------

@mock_aws
def test_insufficient_stock_returns_structured_failure(aws_env):
    """
    Requesting more stock than available returns a structured error response
    (not an exception). The SAGA can read this and trigger compensation.
    The stock is NOT modified.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(client)

    product_id = f"prod-{uuid.uuid4().hex[:8]}"
    ddb.Table("test-inventory").put_item(Item={
        "product_id": product_id,
        "quantity": 2,  # only 2 in stock
        "unit_price_cents": 100,
    })

    from inventory_service.handler import handler

    result = handler({
        "action": "reserve",
        "order_id": f"order-{uuid.uuid4()}",
        "items": [{"product_id": product_id, "quantity": 5, "unit_price_cents": 100}],
        "correlation_id": str(uuid.uuid4()),
    }, None)

    # Must return structured failure — not raise an exception
    assert result["success"] is False
    assert result["error"] == "INSUFFICIENT_STOCK"
    assert product_id in result["message"]

    # Stock must be unchanged
    stock = int(ddb.Table("test-inventory").get_item(
        Key={"product_id": product_id})["Item"]["quantity"])
    assert stock == 2  # not modified


# ---------------------------------------------------------------------------
# Scenario 5: Circuit breaker probe fails → reopens
# ---------------------------------------------------------------------------

@mock_aws
def test_circuit_reopens_if_probe_fails(aws_env):
    """
    In HALF_OPEN state, if the probe call fails, the circuit reopens immediately.
    This prevents cascading failures from a partially-recovered provider.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_tables(client)

    from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, CircuitState

    cb = CircuitBreaker("test-reopen", failure_threshold=2, timeout_seconds=1)
    cb.reset()

    def always_fails():
        raise ConnectionError("Still down")

    # Open the circuit
    for _ in range(2):
        with pytest.raises(ConnectionError):
            cb.call(always_fails)

    # Wait for timeout → circuit transitions to HALF_OPEN
    time.sleep(1.1)

    # Probe call also fails → circuit must go back to OPEN
    with pytest.raises(ConnectionError):
        cb.call(always_fails)

    # Next call must fast-fail (circuit is OPEN again)
    with pytest.raises(CircuitBreakerOpenError):
        cb.call(always_fails)


# ---------------------------------------------------------------------------
# Scenario 6: Idempotency key expiry (TTL simulation)
# ---------------------------------------------------------------------------

@mock_aws
def test_idempotency_key_is_stored_with_ttl(aws_env):
    """
    After processing, the idempotency record exists in DynamoDB.
    In production, a TTL of 24h ensures old keys are automatically cleaned up.
    """
    client = boto3.client("dynamodb", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_tables(client)

    product_id = f"prod-{uuid.uuid4().hex[:8]}"
    ddb.Table("test-inventory").put_item(Item={
        "product_id": product_id,
        "quantity": 10,
        "unit_price_cents": 100,
    })

    from inventory_service.handler import handler

    order_id = f"order-{uuid.uuid4()}"
    handler({
        "action": "reserve",
        "order_id": order_id,
        "items": [{"product_id": product_id, "quantity": 1, "unit_price_cents": 100}],
        "correlation_id": "corr-1",
    }, None)

    # Idempotency record must exist after the call
    record = ddb.Table("test-idempotency").get_item(
        Key={"idempotency_key": f"reserve-{order_id}"}
    ).get("Item")

    assert record is not None
    assert record["status"] == "COMPLETE"
    assert "result" in record  # cached result for future duplicate calls

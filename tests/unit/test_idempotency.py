"""
Unit tests for the idempotency decorator.

These tests verify the most critical invariant in the system:
a function wrapped with @idempotent returns the same result for
the same key, regardless of how many times it's called.
"""
import sys
sys.path.insert(0, "services")

import pytest
from moto import mock_aws


@mock_aws
def test_idempotent_first_call_executes_function(aws_env):
    """Function runs normally on first call."""
    import boto3
    from shared.idempotency import idempotent

    # Create the table inside the mock context
    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )

    call_count = [0]

    @idempotent(key_fn=lambda key: key)
    def my_fn(key):
        call_count[0] += 1
        return {"result": "hello", "count": call_count[0]}

    result = my_fn("test-key-1")
    assert result == {"result": "hello", "count": 1}
    assert call_count[0] == 1


@mock_aws
def test_idempotent_second_call_returns_cached(aws_env):
    """Second call with same key returns cached result without executing function."""
    import boto3
    from shared.idempotency import idempotent

    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )

    call_count = [0]

    @idempotent(key_fn=lambda key: key)
    def my_fn(key):
        call_count[0] += 1
        return {"result": "hello", "count": call_count[0]}

    result1 = my_fn("test-key-2")
    result2 = my_fn("test-key-2")  # same key

    assert result1 == result2
    assert call_count[0] == 1, "Function should only execute once"


@mock_aws
def test_idempotent_different_keys_both_execute(aws_env):
    """Different keys each execute the function independently."""
    import boto3
    from shared.idempotency import idempotent

    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )

    call_count = [0]

    @idempotent(key_fn=lambda key: key)
    def my_fn(key):
        call_count[0] += 1
        return {"count": call_count[0]}

    r1 = my_fn("key-a")
    r2 = my_fn("key-b")

    assert r1 == {"count": 1}
    assert r2 == {"count": 2}
    assert call_count[0] == 2


@mock_aws
def test_idempotent_cleans_up_on_exception(aws_env):
    """If the function raises, the idempotency key is removed so caller can retry."""
    import boto3
    from shared.idempotency import idempotent

    boto3.client("dynamodb", region_name="us-east-1").create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )

    call_count = [0]

    @idempotent(key_fn=lambda key: key)
    def flaky_fn(key):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("Transient failure")
        return {"success": True}

    with pytest.raises(RuntimeError):
        flaky_fn("retry-key")

    # Second call should succeed (key was cleaned up)
    result = flaky_fn("retry-key")
    assert result == {"success": True}
    assert call_count[0] == 2

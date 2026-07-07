"""
Unit tests for the payment service Lambda handler.

Covers the charge/refund actions plus the two failure modes the handler
translates into structured responses:
  - PaymentDeclinedError  -> {"error": "PAYMENT_DECLINED"}
  - CircuitBreakerOpenError -> {"error": "PAYMENT_PROVIDER_UNAVAILABLE"}

The external provider call (random failure + sleep) is monkeypatched out so the
tests are deterministic; the circuit breaker itself runs against a moto table.
"""
import sys
import time

sys.path.insert(0, "services")

import boto3
import pytest
from moto import mock_aws


def _make_tables(client):
    client.create_table(
        TableName="test-payments",
        AttributeDefinitions=[{"AttributeName": "payment_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "payment_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.create_table(
        TableName="test-circuit-breakers",
        AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
def test_charge_success(aws_env, monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_tables(client)

    from payment_service import handler as h

    h.payment_circuit_breaker.reset()
    monkeypatch.setattr(
        h, "_call_payment_provider",
        lambda action, **kw: {"success": True, "charge_id": "ch_test123"},
    )

    result = h.handler(
        {"action": "charge", "order_id": "o1", "customer_id": "c1", "total_cents": 5000},
        None,
    )
    assert result["success"] is True
    assert "payment_id" in result

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("test-payments")
    stored = table.get_item(Key={"payment_id": result["payment_id"]}).get("Item")
    assert stored["status"] == "CHARGED"
    assert stored["provider_charge_id"] == "ch_test123"


@mock_aws
def test_charge_declined(aws_env, monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_tables(client)

    from payment_service import handler as h

    h.payment_circuit_breaker.reset()
    monkeypatch.setattr(
        h, "_call_payment_provider",
        lambda action, **kw: {"success": False, "decline_reason": "Insufficient funds"},
    )

    result = h.handler(
        {"action": "charge", "order_id": "o2", "customer_id": "c1", "total_cents": 100},
        None,
    )
    assert result == {
        "success": False,
        "error": "PAYMENT_DECLINED",
        "message": "Insufficient funds",
    }


@mock_aws
def test_charge_circuit_open_returns_unavailable(aws_env, monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_tables(client)

    from shared.circuit_breaker import CircuitBreakerOpenError
    from payment_service import handler as h

    def _raise_open(fn, *args, **kwargs):
        raise CircuitBreakerOpenError("external-payment-provider", time.time() + 30)

    monkeypatch.setattr(h.payment_circuit_breaker, "call", _raise_open)

    result = h.handler(
        {"action": "charge", "order_id": "o3", "customer_id": "c1", "total_cents": 100},
        None,
    )
    assert result["success"] is False
    assert result["error"] == "PAYMENT_PROVIDER_UNAVAILABLE"
    assert result["retry_after_seconds"] >= 0


@mock_aws
def test_refund_success(aws_env, monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_tables(client)

    from payment_service import handler as h

    h.payment_circuit_breaker.reset()
    monkeypatch.setattr(
        h, "_call_payment_provider",
        lambda action, **kw: {"success": True, "charge_id": "ch_refundme"},
    )

    charge = h.handler(
        {"action": "charge", "order_id": "o4", "customer_id": "c1", "total_cents": 250},
        None,
    )
    payment_id = charge["payment_id"]

    refund = h.handler({"action": "refund", "order_id": "o4", "payment_id": payment_id}, None)
    assert refund["success"] is True

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("test-payments")
    stored = table.get_item(Key={"payment_id": payment_id}).get("Item")
    assert stored["status"] == "REFUNDED"


@mock_aws
def test_refund_without_payment_id_is_noop(aws_env):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_tables(client)

    from payment_service import handler as h

    result = h.handler({"action": "refund", "order_id": "o5"}, None)
    assert result == {"success": True, "message": "Nothing to refund"}


@mock_aws
def test_unknown_action_raises(aws_env):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_tables(client)

    from payment_service import handler as h

    with pytest.raises(ValueError):
        h.handler({"action": "teleport"}, None)


def test_provider_url_falls_back_to_env(aws_env, monkeypatch):
    from payment_service import handler as h

    monkeypatch.delenv("PAYMENT_PROVIDER_SECRET_NAME", raising=False)
    monkeypatch.setenv("PAYMENT_PROVIDER_URL", "https://provider.example")
    h._payment_provider_url = None

    assert h._get_payment_provider_url() == "https://provider.example"

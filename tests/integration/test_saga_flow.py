"""
Integration tests — handler functions called directly against LocalStack DynamoDB.

No Lambda deployment needed. We import the handler functions as Python modules
and call them with real DynamoDB (LocalStack) instead of moto mocks.

This tests the actual I/O paths: DynamoDB conditional writes, idempotency
records, reservation records — all hitting a real (emulated) AWS service.

Run: USE_LOCALSTACK=true pytest tests/integration/ -m integration
"""
import json
import os
import sys
import uuid

import boto3
import pytest

sys.path.insert(0, "services")

LOCALSTACK = os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")
REGION = "us-east-1"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def localstack_env(aws_env, monkeypatch):
    """
    Override the unit-test aws_env settings with real LocalStack table names.
    Depends on aws_env so it runs AFTER it — monkeypatch overrides win.
    Also disables X-Ray (no daemon running locally).
    """
    monkeypatch.setenv("AWS_ENDPOINT_URL", LOCALSTACK)
    monkeypatch.setenv("ORDERS_TABLE", "cloudflow-orders")
    monkeypatch.setenv("INVENTORY_TABLE", "cloudflow-inventory")
    monkeypatch.setenv("RESERVATIONS_TABLE", "cloudflow-reservations")
    monkeypatch.setenv("PAYMENTS_TABLE", "cloudflow-payments")
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "cloudflow-idempotency")
    monkeypatch.setenv("CIRCUIT_BREAKER_TABLE", "cloudflow-circuit-breakers")
    monkeypatch.setenv("EVENT_BUS_NAME", "cloudflow-events")
    monkeypatch.setenv("SAGA_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:000000000000:stateMachine:test")
    monkeypatch.setenv("AWS_XRAY_SDK_ENABLED", "false")  # no X-Ray daemon in tests


@pytest.fixture(scope="module")
def ddb():
    return boto3.resource(
        "dynamodb",
        endpoint_url=LOCALSTACK,
        region_name=REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture
def sample_product(ddb):
    """Seed a fresh product with known stock for test isolation."""
    product_id = f"test-prod-{uuid.uuid4().hex[:8]}"
    ddb.Table("cloudflow-inventory").put_item(Item={
        "product_id": product_id,
        "quantity": 10,
        "unit_price_cents": 999,
        "name": "Test Product",
    })
    yield product_id
    ddb.Table("cloudflow-inventory").delete_item(Key={"product_id": product_id})


# ---------------------------------------------------------------------------
# Inventory Service — reserve
# ---------------------------------------------------------------------------

class TestInventoryReservation:

    def test_reserve_decrements_stock(self, sample_product, ddb):
        """Reserving items reduces available stock in DynamoDB."""
        from inventory_service.handler import handler

        event = {
            "action": "reserve",
            "order_id": f"order-{uuid.uuid4()}",
            "items": [{"product_id": sample_product, "quantity": 3, "unit_price_cents": 999}],
            "correlation_id": str(uuid.uuid4()),
        }

        result = handler(event, None)

        assert result["success"] is True
        assert "reservation_id" in result

        item = ddb.Table("cloudflow-inventory").get_item(
            Key={"product_id": sample_product}
        )["Item"]
        assert int(item["quantity"]) == 7  # 10 - 3

    def test_reserve_fails_on_insufficient_stock(self, ddb):
        """Reserving more than available returns a structured failure, not an exception."""
        from inventory_service.handler import handler

        product_id = f"scarce-{uuid.uuid4().hex[:8]}"
        ddb.Table("cloudflow-inventory").put_item(Item={
            "product_id": product_id,
            "quantity": 1,
            "name": "Scarce Item",
        })

        event = {
            "action": "reserve",
            "order_id": f"order-{uuid.uuid4()}",
            "items": [{"product_id": product_id, "quantity": 5, "unit_price_cents": 100}],
            "correlation_id": str(uuid.uuid4()),
        }

        result = handler(event, None)

        assert result["success"] is False
        assert result["error"] == "INSUFFICIENT_STOCK"

    def test_reserve_is_idempotent(self, sample_product, ddb):
        """Calling reserve twice with the same order_id only decrements stock once."""
        from inventory_service.handler import handler

        order_id = f"order-{uuid.uuid4()}"
        event = {
            "action": "reserve",
            "order_id": order_id,
            "items": [{"product_id": sample_product, "quantity": 2, "unit_price_cents": 999}],
            "correlation_id": str(uuid.uuid4()),
        }

        result1 = handler(event, None)
        result2 = handler(event, None)  # duplicate

        assert result1["success"] is True
        assert result2["success"] is True
        assert result1["reservation_id"] == result2["reservation_id"], \
            "Idempotent calls must return the same reservation_id"

        # Stock decremented exactly once (10 - 2 = 8, NOT 10 - 4 = 6)
        item = ddb.Table("cloudflow-inventory").get_item(
            Key={"product_id": sample_product}
        )["Item"]
        assert int(item["quantity"]) == 8

    def test_release_restores_stock(self, sample_product, ddb):
        """Releasing a reservation adds stock back (compensating transaction)."""
        from inventory_service.handler import handler

        order_id = f"order-{uuid.uuid4()}"

        reserve_result = handler({
            "action": "reserve",
            "order_id": order_id,
            "items": [{"product_id": sample_product, "quantity": 4, "unit_price_cents": 999}],
            "correlation_id": str(uuid.uuid4()),
        }, None)
        assert reserve_result["success"] is True
        reservation_id = reserve_result["reservation_id"]

        release_result = handler({
            "action": "release",
            "order_id": order_id,
            "reservation_id": reservation_id,
            "correlation_id": str(uuid.uuid4()),
        }, None)
        assert release_result["success"] is True

        item = ddb.Table("cloudflow-inventory").get_item(
            Key={"product_id": sample_product}
        )["Item"]
        assert int(item["quantity"]) == 10  # restored

    def test_release_is_idempotent(self, sample_product, ddb):
        """Releasing the same reservation twice doesn't double-add stock."""
        from inventory_service.handler import handler

        order_id = f"order-{uuid.uuid4()}"

        reserve_result = handler({
            "action": "reserve",
            "order_id": order_id,
            "items": [{"product_id": sample_product, "quantity": 3, "unit_price_cents": 999}],
            "correlation_id": str(uuid.uuid4()),
        }, None)
        reservation_id = reserve_result["reservation_id"]

        # Release twice with same key
        r1 = handler({"action": "release", "order_id": order_id, "reservation_id": reservation_id, "correlation_id": "x"}, None)
        r2 = handler({"action": "release", "order_id": order_id, "reservation_id": reservation_id, "correlation_id": "x"}, None)

        assert r1["success"] is True
        assert r2["success"] is True

        # Stock = 10, not 13 (not double-added)
        item = ddb.Table("cloudflow-inventory").get_item(
            Key={"product_id": sample_product}
        )["Item"]
        assert int(item["quantity"]) == 10


# ---------------------------------------------------------------------------
# Order Repository
# ---------------------------------------------------------------------------

class TestOrderRepository:

    def test_create_and_retrieve_order(self, ddb):
        """Created order can be fetched back with correct fields."""
        from order_service.repository import OrderRepository
        from shared.events import OrderItem

        repo = OrderRepository()
        order_id = str(uuid.uuid4())

        order = repo.create(
            order_id=order_id,
            customer_id="cust-test",
            items=[OrderItem(product_id="p1", quantity=2, unit_price_cents=500)],
            total_cents=1000,
            correlation_id=str(uuid.uuid4()),
        )

        assert order["order_id"] == order_id
        assert order["status"] == "PENDING"
        assert order["total_cents"] == 1000

        fetched = repo.get(order_id)
        assert fetched["order_id"] == order_id

    def test_event_history_appended_on_status_update(self, ddb):
        """Every status update appends an immutable event — full audit trail."""
        from order_service.repository import OrderRepository
        from shared.events import OrderItem, OrderStatus

        repo = OrderRepository()
        order_id = str(uuid.uuid4())

        repo.create(
            order_id=order_id,
            customer_id="cust-test",
            items=[OrderItem(product_id="p1", quantity=1, unit_price_cents=100)],
            total_cents=100,
            correlation_id=str(uuid.uuid4()),
        )
        repo.update_status(order_id, OrderStatus.CONFIRMED)
        repo.update_status(order_id, OrderStatus.PAYMENT_CHARGED)

        history = repo.get_event_history(order_id)
        statuses = [e["status"] for e in history]

        assert "PENDING" in statuses
        assert "CONFIRMED" in statuses
        assert "PAYMENT_CHARGED" in statuses
        assert len(history) == 3

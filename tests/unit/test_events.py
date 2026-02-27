"""
Unit tests for event schemas and domain models.
"""
import pytest
from pydantic import ValidationError

import sys
sys.path.insert(0, "services")

from shared.events import CreateOrderRequest, OrderItem, SagaContext


def test_order_item_total_cents():
    """Total price calculation uses integer arithmetic (no float rounding errors)."""
    item = OrderItem(product_id="prod-1", quantity=3, unit_price_cents=999)
    assert item.total_cents == 2997


def test_create_order_total_cents():
    """Order total is sum of all items."""
    order = CreateOrderRequest(
        customer_id="cust-1",
        items=[
            OrderItem(product_id="prod-1", quantity=2, unit_price_cents=1000),
            OrderItem(product_id="prod-2", quantity=1, unit_price_cents=500),
        ],
    )
    assert order.total_cents == 2500


def test_create_order_auto_generates_idempotency_key():
    """Idempotency key is auto-generated if not provided."""
    order = CreateOrderRequest(customer_id="cust-1", items=[
        OrderItem(product_id="prod-1", quantity=1, unit_price_cents=100),
    ])
    assert order.idempotency_key
    assert len(order.idempotency_key) > 0


def test_create_order_requires_at_least_one_item():
    """Orders must have items — empty order list should fail validation."""
    with pytest.raises(ValidationError):
        CreateOrderRequest(customer_id="cust-1", items=[])


def test_saga_context_serializes_to_json():
    """SagaContext must be JSON-serializable for Step Functions."""
    import json
    ctx = SagaContext(
        order_id="order-123",
        customer_id="cust-456",
        total_cents=5000,
        items=[OrderItem(product_id="p1", quantity=1, unit_price_cents=5000)],
        correlation_id="corr-789",
    )
    serialized = ctx.model_dump_json()
    parsed = json.loads(serialized)
    assert parsed["order_id"] == "order-123"
    assert parsed["total_cents"] == 5000


def test_order_item_rejects_zero_quantity():
    """Quantity must be positive."""
    with pytest.raises(ValidationError):
        OrderItem(product_id="p1", quantity=0, unit_price_cents=100)


def test_order_item_rejects_negative_price():
    """Price cannot be negative."""
    with pytest.raises(ValidationError):
        OrderItem(product_id="p1", quantity=1, unit_price_cents=-1)

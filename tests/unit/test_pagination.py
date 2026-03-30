"""
Unit tests for cursor-based pagination on order event history.

Verifies that get_event_history returns paginated results with a
next_cursor token that can be used to fetch subsequent pages.
"""
import sys
import time
import uuid

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, "services")


def _create_orders_table(client):
    client.create_table(
        TableName="test-orders",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@mock_aws
def test_event_history_returns_paginated_dict(aws_env):
    """get_event_history returns {events: [...], next_cursor: ...} not a flat list."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_orders_table(client)

    from order_service.repository import OrderRepository
    from shared.events import OrderItem, OrderStatus

    repo = OrderRepository()
    order_id = str(uuid.uuid4())

    repo.create(
        order_id=order_id,
        customer_id="cust-1",
        items=[OrderItem(product_id="p1", quantity=1, unit_price_cents=100)],
        total_cents=100,
        correlation_id=str(uuid.uuid4()),
    )

    result = repo.get_event_history(order_id)

    assert isinstance(result, dict)
    assert "events" in result
    assert "next_cursor" in result
    assert isinstance(result["events"], list)
    assert len(result["events"]) == 1  # PENDING event from create


@mock_aws
def test_event_history_respects_limit(aws_env):
    """When limit is smaller than total events, only limit events are returned."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_orders_table(client)

    from order_service.repository import OrderRepository
    from shared.events import OrderItem, OrderStatus

    repo = OrderRepository()
    order_id = str(uuid.uuid4())

    repo.create(
        order_id=order_id,
        customer_id="cust-1",
        items=[OrderItem(product_id="p1", quantity=1, unit_price_cents=100)],
        total_cents=100,
        correlation_id=str(uuid.uuid4()),
    )
    # Add more events
    for status in [OrderStatus.INVENTORY_RESERVED, OrderStatus.PAYMENT_CHARGED, OrderStatus.CONFIRMED]:
        time.sleep(0.001)  # ensure unique sort keys
        repo.update_status(order_id, status)

    # 4 total events: PENDING + 3 updates
    full = repo.get_event_history(order_id, limit=50)
    assert len(full["events"]) == 4

    # Fetch only 2
    page1 = repo.get_event_history(order_id, limit=2)
    assert len(page1["events"]) == 2
    assert page1["next_cursor"] is not None


@mock_aws
def test_cursor_based_pagination_walks_all_events(aws_env):
    """Cursor from page 1 can be used to fetch page 2, covering all events."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_orders_table(client)

    from order_service.repository import OrderRepository
    from shared.events import OrderItem, OrderStatus

    repo = OrderRepository()
    order_id = str(uuid.uuid4())

    repo.create(
        order_id=order_id,
        customer_id="cust-1",
        items=[OrderItem(product_id="p1", quantity=1, unit_price_cents=100)],
        total_cents=100,
        correlation_id=str(uuid.uuid4()),
    )
    for status in [OrderStatus.INVENTORY_RESERVED, OrderStatus.PAYMENT_CHARGED, OrderStatus.CONFIRMED]:
        time.sleep(0.001)
        repo.update_status(order_id, status)

    # Page through 2 at a time
    page1 = repo.get_event_history(order_id, limit=2)
    assert len(page1["events"]) == 2
    assert page1["next_cursor"] is not None

    page2 = repo.get_event_history(order_id, limit=2, cursor=page1["next_cursor"])
    assert len(page2["events"]) == 2
    assert page2["next_cursor"] is None  # no more pages

    # All 4 statuses covered
    all_statuses = [e["status"] for e in page1["events"] + page2["events"]]
    assert "PENDING" in all_statuses
    assert "CONFIRMED" in all_statuses


@mock_aws
def test_no_cursor_when_all_events_fit(aws_env):
    """When all events fit in one page, next_cursor is None."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    _create_orders_table(client)

    from order_service.repository import OrderRepository
    from shared.events import OrderItem

    repo = OrderRepository()
    order_id = str(uuid.uuid4())

    repo.create(
        order_id=order_id,
        customer_id="cust-1",
        items=[OrderItem(product_id="p1", quantity=1, unit_price_cents=100)],
        total_cents=100,
        correlation_id=str(uuid.uuid4()),
    )

    result = repo.get_event_history(order_id, limit=50)
    assert result["next_cursor"] is None

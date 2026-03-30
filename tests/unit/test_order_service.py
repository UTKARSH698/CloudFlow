"""
Unit tests for the Order Service Lambda handler.

Tests cover:
  - Health check endpoint
  - POST /orders validation and response format
  - GET /orders/{id} success and 404 paths
  - Error handling (missing idempotency key, invalid body, unknown route)
"""
import json
import sys
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, "services")


def _create_tables(client):
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
    client.create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@mock_aws
def test_health_check_returns_200(aws_env):
    """GET /health returns 200 with service name — no auth required."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client"), \
         patch("order_service.handler.sfn_client"):
        from order_service.handler import handler

        result = handler({"httpMethod": "GET", "path": "/health"}, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "healthy"
    assert body["service"] == "order-service"


# ---------------------------------------------------------------------------
# POST /orders
# ---------------------------------------------------------------------------

@mock_aws
def test_create_order_returns_202(aws_env):
    """Valid POST /orders returns 202 with order_id, status, and correlation_id."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    mock_events = MagicMock()
    mock_sfn = MagicMock()

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client", mock_events), \
         patch("order_service.handler.sfn_client", mock_sfn):
        from order_service.handler import handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {"Idempotency-Key": str(uuid.uuid4())},
            "body": json.dumps({
                "customer_id": "cust-001",
                "items": [{"product_id": "p1", "quantity": 2, "unit_price_cents": 500}],
            }),
        }
        result = handler(event, None)

    assert result["statusCode"] == 202
    body = json.loads(result["body"])
    assert "order_id" in body
    assert body["status"] == "PENDING"
    assert "correlation_id" in body

    # Verify SAGA was started
    mock_sfn.start_execution.assert_called_once()

    # Verify EventBridge event was emitted
    mock_events.put_events.assert_called_once()


@mock_aws
def test_create_order_missing_idempotency_key_returns_error(aws_env):
    """POST /orders without Idempotency-Key header returns an error."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client"), \
         patch("order_service.handler.sfn_client"):
        from order_service.handler import handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {},  # no Idempotency-Key
            "body": json.dumps({
                "customer_id": "cust-001",
                "items": [{"product_id": "p1", "quantity": 1, "unit_price_cents": 100}],
            }),
        }
        result = handler(event, None)

    assert result["statusCode"] == 500  # ValueError bubbles up as 500


@mock_aws
def test_create_order_invalid_body_returns_400(aws_env):
    """POST /orders with invalid body returns 400 with validation details."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client"), \
         patch("order_service.handler.sfn_client"):
        from order_service.handler import handler

        event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {"Idempotency-Key": str(uuid.uuid4())},
            "body": json.dumps({
                "customer_id": "cust-001",
                "items": [],  # min_length=1 violated
            }),
        }
        result = handler(event, None)

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert "Validation failed" in body["error"]


# ---------------------------------------------------------------------------
# GET /orders/{id}
# ---------------------------------------------------------------------------

@mock_aws
def test_get_order_returns_200(aws_env):
    """GET /orders/{id} returns order data with paginated event history."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    mock_events = MagicMock()
    mock_sfn = MagicMock()

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client", mock_events), \
         patch("order_service.handler.sfn_client", mock_sfn), \
         patch("order_service.handler.xray_recorder") as mock_xray:
        mock_xray.in_subsegment.return_value.__enter__ = MagicMock()
        mock_xray.in_subsegment.return_value.__exit__ = MagicMock()

        from order_service.handler import handler

        # First create an order
        idem_key = str(uuid.uuid4())
        create_event = {
            "httpMethod": "POST",
            "path": "/orders",
            "headers": {"Idempotency-Key": idem_key},
            "body": json.dumps({
                "customer_id": "cust-001",
                "items": [{"product_id": "p1", "quantity": 1, "unit_price_cents": 999}],
            }),
        }
        create_result = handler(create_event, None)
        order_id = json.loads(create_result["body"])["order_id"]

        # Now fetch it
        get_event = {
            "httpMethod": "GET",
            "path": f"/orders/{order_id}",
            "queryStringParameters": None,
        }
        result = handler(get_event, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["order_id"] == order_id
    assert body["status"] == "PENDING"
    assert "events" in body
    assert "next_cursor" in body


@mock_aws
def test_get_order_not_found_returns_404(aws_env):
    """GET /orders/{id} for nonexistent order returns 404."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client"), \
         patch("order_service.handler.sfn_client"), \
         patch("order_service.handler.xray_recorder") as mock_xray:
        mock_xray.in_subsegment.return_value.__enter__ = MagicMock()
        mock_xray.in_subsegment.return_value.__exit__ = MagicMock()

        from order_service.handler import handler

        result = handler({
            "httpMethod": "GET",
            "path": "/orders/nonexistent-id",
            "queryStringParameters": None,
        }, None)

    assert result["statusCode"] == 404
    body = json.loads(result["body"])
    assert "not found" in body["error"]


# ---------------------------------------------------------------------------
# Unknown route
# ---------------------------------------------------------------------------

@mock_aws
def test_unknown_route_returns_404(aws_env):
    """Unknown HTTP method/path combination returns 404."""
    _create_tables(boto3.client("dynamodb", region_name="us-east-1"))

    with patch("order_service.handler.patch_all"), \
         patch("order_service.handler.events_client"), \
         patch("order_service.handler.sfn_client"):
        from order_service.handler import handler

        result = handler({"httpMethod": "DELETE", "path": "/orders/123"}, None)

    assert result["statusCode"] == 404

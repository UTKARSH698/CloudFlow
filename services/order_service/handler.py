"""
Order Service Lambda Handler
=============================
Handles two routes:
  POST /orders     → Create a new order and start the SAGA
  GET  /orders/:id → Query order status + event history

The handler is thin: validation → idempotency check → repository → SAGA kickoff.
All business logic lives in the repository and shared utilities.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder
from pydantic import ValidationError

from shared.events import (
    CloudFlowEvent, CreateOrderRequest, EventType, OrderCreatedPayload, SagaContext
)
from shared.idempotency import IdempotencyAlreadyInProgressError, IdempotencyKey, idempotent
from .repository import OrderRepository

# Patch boto3 clients for X-Ray distributed tracing
patch_all()

from shared.logger import get_logger
logger = get_logger(__name__)

EVENT_BUS_NAME = os.environ["EVENT_BUS_NAME"]
SAGA_STATE_MACHINE_ARN = os.environ["SAGA_STATE_MACHINE_ARN"]

events_client = boto3.client("events")
sfn_client = boto3.client("stepfunctions")
repo = OrderRepository()


# ---------------------------------------------------------------------------
# Main handler — dispatches to sub-handlers by HTTP method + path
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")

    try:
        if http_method == "POST" and path == "/orders":
            return _create_order(event)
        if http_method == "GET" and path.startswith("/orders/"):
            order_id = path.split("/")[-1]
            return _get_order(order_id)
        return _response(404, {"error": "Not Found"})
    except ValidationError as e:
        return _response(400, {"error": "Validation failed", "details": e.errors()})
    except IdempotencyAlreadyInProgressError as e:
        return _response(409, {"error": str(e)})
    except Exception:
        logger.exception(
            "Unhandled exception in order_service handler",
            extra={"http_method": http_method, "path": path},
        )
        return _response(500, {"error": "Internal server error"})


# ---------------------------------------------------------------------------
# Create order
# ---------------------------------------------------------------------------

def _create_order(api_event: dict) -> dict:
    idempotency_key = IdempotencyKey.from_api_gateway(api_event)

    @idempotent(key_fn=lambda: idempotency_key)
    def _idempotent_create():
        body = json.loads(api_event.get("body") or "{}")
        request = CreateOrderRequest(**body, idempotency_key=idempotency_key)

        order_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())

        # 1. Persist order in PENDING state
        order = repo.create(
            order_id=order_id,
            customer_id=request.customer_id,
            items=request.items,
            total_cents=request.total_cents,
            correlation_id=correlation_id,
        )

        # 2. Emit domain event to EventBridge (for audit / downstream consumers)
        _emit_order_created_event(order, correlation_id)

        # 3. Start the SAGA state machine
        saga_context = SagaContext(
            order_id=order_id,
            customer_id=request.customer_id,
            total_cents=request.total_cents,
            items=request.items,
            correlation_id=correlation_id,
        )
        sfn_client.start_execution(
            stateMachineArn=SAGA_STATE_MACHINE_ARN,
            name=f"order-saga-{order_id}",          # must be unique per execution
            input=saga_context.model_dump_json(),
        )

        logger.info(
            "Order created",
            extra={"order_id": order_id, "correlation_id": correlation_id},
        )
        return {"order_id": order_id, "status": order["status"]}

    result = _idempotent_create()
    return _response(202, result)   # 202 Accepted: processing is asynchronous


# ---------------------------------------------------------------------------
# Get order
# ---------------------------------------------------------------------------

def _get_order(order_id: str) -> dict:
    with xray_recorder.in_subsegment("get_order"):
        order = repo.get(order_id)
        if not order:
            return _response(404, {"error": f"Order {order_id!r} not found"})

        history = repo.get_event_history(order_id)
        return _response(200, {**order, "event_history": history})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit_order_created_event(order: dict, correlation_id: str) -> None:
    evt = CloudFlowEvent(
        event_type=EventType.ORDER_CREATED,
        source_service="order-service",
        correlation_id=correlation_id,
        payload=OrderCreatedPayload(
            order_id=order["order_id"],
            customer_id=order["customer_id"],
            items=order["items"],
            total_cents=order["total_cents"],
            idempotency_key=order.get("idempotency_key", ""),
        ).model_dump(),
    )
    events_client.put_events(Entries=[evt.to_eventbridge_entry(EVENT_BUS_NAME)])


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "X-Correlation-Id": body.get("correlation_id", ""),
        },
        "body": json.dumps(body),
    }

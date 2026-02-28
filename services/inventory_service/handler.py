"""
Inventory Service Lambda Handler
=================================
Called by the SAGA Step Functions orchestrator (not directly by users).

Two operations:
  reserve   → atomically decrement stock, create reservation record
  release   → compensating transaction: undo a reservation (rollback)

Why atomic decrements matter:
  If two orders both check stock (quantity=1) and both see "available",
  both will try to reserve — causing overselling. DynamoDB's UpdateItem
  with a ConditionExpression is the atomic check-and-decrement:
    SET quantity = quantity - :n WHERE quantity >= :n
  This is equivalent to SQL's SELECT FOR UPDATE but without a lock table.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder
from botocore.exceptions import ClientError

from shared.dynamodb import decimal_to_python, get_table
from shared.events import OrderItem, SagaContext
from shared.idempotency import idempotent

patch_all()

from shared.logger import get_logger
logger = get_logger(__name__)

def _inv_table():
    return get_table(os.environ.get("INVENTORY_TABLE", "cloudflow-inventory"))

def _res_table():
    return get_table(os.environ.get("RESERVATIONS_TABLE", "cloudflow-reservations"))


# ---------------------------------------------------------------------------
# Entry point — Step Functions calls this directly
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    """
    event shape from Step Functions:
      {
        "action": "reserve" | "release",
        "order_id": "...",
        "reservation_id": "...",  # only for release
        "items": [...],
        "correlation_id": "..."
      }
    """
    action = event.get("action")
    try:
        if action == "reserve":
            return _reserve(event)
        if action == "release":
            return _release(event)
        raise ValueError(f"Unknown action: {action!r}")
    except InsufficientStockError as e:
        logger.warning("Insufficient stock: %s", e)
        return {
            "success": False,
            "error": "INSUFFICIENT_STOCK",
            "message": str(e),
        }
    except Exception:
        logger.exception("Inventory service error")
        raise  # Let Step Functions handle the retry/catch


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------

def _reserve(event: dict) -> dict:
    order_id = event["order_id"]
    items = [OrderItem(**i) for i in event["items"]]
    reservation_id = str(uuid.uuid4())
    correlation_id = event.get("correlation_id", "")

    @idempotent(key_fn=lambda: f"reserve-{order_id}")
    def _do_reserve():
        with xray_recorder.in_subsegment("inventory_reserve"):
            # Atomically decrement each product's stock
            for item in items:
                _decrement_stock(item.product_id, item.quantity, order_id)

            # Record the reservation for potential rollback
            _res_table().put_item(Item={
                "reservation_id": reservation_id,
                "order_id": order_id,
                "items": [i.model_dump() for i in items],
                "status": "ACTIVE",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

            logger.info(
                "Inventory reserved",
                extra={"order_id": order_id, "reservation_id": reservation_id},
            )
            return {"success": True, "reservation_id": reservation_id}

    return _do_reserve()


# ---------------------------------------------------------------------------
# Release (compensating transaction)
# ---------------------------------------------------------------------------

def _release(event: dict) -> dict:
    reservation_id = event["reservation_id"]
    order_id = event["order_id"]

    @idempotent(key_fn=lambda: f"release-{reservation_id}")
    def _do_release():
        with xray_recorder.in_subsegment("inventory_release"):
            # Look up the reservation to know what to return to stock
            resp = _res_table().get_item(Key={"reservation_id": reservation_id})
            reservation = resp.get("Item")
            if not reservation:
                logger.warning("Reservation %s not found — nothing to release", reservation_id)
                return {"success": True, "message": "Nothing to release"}

            items = [OrderItem(**i) for i in reservation["items"]]
            for item in items:
                # Increment back — this never fails (adding stock is always safe)
                _inv_table().update_item(
                    Key={"product_id": item.product_id},
                    UpdateExpression="ADD quantity :n",
                    ExpressionAttributeValues={":n": item.quantity},
                )

            _res_table().update_item(
                Key={"reservation_id": reservation_id},
                UpdateExpression="SET #s = :s",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "RELEASED"},
            )

            logger.info(
                "Inventory released",
                extra={"order_id": order_id, "reservation_id": reservation_id},
            )
            return {"success": True}

    return _do_release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decrement_stock(product_id: str, quantity: int, order_id: str) -> None:
    """
    Atomically decrement stock. Raises InsufficientStockError if not enough.

    The ConditionExpression (quantity >= :n) is evaluated atomically by DynamoDB.
    No two concurrent requests can both pass this check with the same item.
    """
    try:
        _inv_table().update_item(
            Key={"product_id": product_id},
            UpdateExpression="SET quantity = quantity - :n, updated_at = :ts",
            ConditionExpression="quantity >= :n",
            ExpressionAttributeValues={
                ":n": quantity,
                ":ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise InsufficientStockError(
                f"Product {product_id!r}: requested {quantity}, insufficient stock"
            ) from e
        raise


class InsufficientStockError(Exception):
    pass

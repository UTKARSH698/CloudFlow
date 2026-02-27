"""
Order Repository
================
All DynamoDB access for the order service lives here.
The Lambda handler stays clean; data access logic is testable independently.

DynamoDB table design (single-table):
  PK: ORDER#{order_id}
  SK: META            → current order state
  SK: EVENT#{ts}      → event sourcing log entries

Why single-table design?
  DynamoDB charges per read capacity unit. Fetching an order + its full
  event history in one query (via PK) costs 1 RCU instead of N RCUs for
  N separate table reads.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3

from shared.dynamodb import decimal_to_python, get_table, put_item_with_optimistic_lock
from shared.events import OrderItem, OrderStatus

class OrderRepository:
    def __init__(self):
        self._table = get_table(os.environ.get("ORDERS_TABLE", "cloudflow-orders"))

    def create(
        self,
        order_id: str,
        customer_id: str,
        items: list[OrderItem],
        total_cents: int,
        correlation_id: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "pk": f"ORDER#{order_id}",
            "sk": "META",
            "order_id": order_id,
            "customer_id": customer_id,
            "items": [i.model_dump() for i in items],
            "total_cents": total_cents,
            "status": OrderStatus.PENDING,
            "correlation_id": correlation_id,
            "created_at": now,
            "updated_at": now,
            "version": 0,  # optimistic lock starts at 0 → written as 1
        }
        put_item_with_optimistic_lock(self._table, item)
        self._append_event(order_id, OrderStatus.PENDING, {})
        return decimal_to_python(item)

    def update_status(self, order_id: str, status: OrderStatus, metadata: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._table.update_item(
            Key={"pk": f"ORDER#{order_id}", "sk": "META"},
            UpdateExpression="SET #st = :s, updated_at = :u ADD version :one",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": status, ":u": now, ":one": 1},
        )
        self._append_event(order_id, status, metadata or {})

    def get(self, order_id: str) -> dict | None:
        resp = self._table.get_item(Key={"pk": f"ORDER#{order_id}", "sk": "META"})
        item = resp.get("Item")
        return decimal_to_python(item) if item else None

    def get_event_history(self, order_id: str) -> list[dict]:
        """Return all events for this order in chronological order."""
        resp = self._table.query(
            KeyConditionExpression=(
                "pk = :pk AND begins_with(sk, :prefix)"
            ),
            ExpressionAttributeValues={
                ":pk": f"ORDER#{order_id}",
                ":prefix": "EVENT#",
            },
        )
        return [decimal_to_python(i) for i in resp.get("Items", [])]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _append_event(self, order_id: str, status: OrderStatus, metadata: dict) -> None:
        ts = f"{int(time.time() * 1_000_000)}"  # microsecond precision for sort order
        self._table.put_item(Item={
            "pk": f"ORDER#{order_id}",
            "sk": f"EVENT#{ts}",
            "status": status,
            "metadata": metadata,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        })

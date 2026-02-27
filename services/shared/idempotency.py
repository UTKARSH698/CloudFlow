"""
Idempotency — The Silent Killer of Distributed Systems
=======================================================
SQS delivers messages at-least-once. Lambda retries on failure. Networks partition.
Without idempotency, a customer gets charged twice or inventory goes negative.

Implementation: DynamoDB conditional write with TTL.
  - On first receipt: atomically write idempotency_key → "IN_FLIGHT"
  - If write fails (key exists): return cached response immediately
  - On success: update record to "COMPLETE" with the result
  - On failure: delete record so the caller can retry

Why DynamoDB vs Redis?
  DynamoDB's conditional writes (`attribute_not_exists`) are atomic by default.
  No need to implement distributed locking (SETNX + EXPIRE + Lua script).
  DynamoDB also survives Lambda cold starts and AZ failures — Redis requires
  replication config for the same durability guarantees.

Why not SQS exactly-once (FIFO)?
  FIFO queues have lower throughput (3000 msg/s vs 300,000 msg/s standard).
  For a high-volume order system, idempotent consumers on standard queues scale better.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import time
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL_SECONDS = int(os.environ.get("IDEMPOTENCY_TTL_SECONDS", "86400"))  # 24h


def _get_table():
    # Read env each call so monkeypatch overrides work correctly in tests
    table_name = os.environ.get("IDEMPOTENCY_TABLE", "cloudflow-idempotency")
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(table_name)


class IdempotencyError(Exception):
    """Raised when an idempotency record is in an unexpected state."""


class IdempotencyAlreadyInProgressError(IdempotencyError):
    """Another invocation with the same key is currently running."""


def idempotent(key_fn: Callable[..., str]):
    """
    Decorator that makes a function idempotent using DynamoDB.

    Usage:
        @idempotent(key_fn=lambda event, ctx: event["idempotency_key"])
        def handler(event, context):
            ...

    The decorated function will:
    1. Check if a result is already cached for this key.
    2. If cached and COMPLETE: return the cached result.
    3. If IN_FLIGHT: raise IdempotencyAlreadyInProgressError.
    4. If not present: atomically claim the key, run the function, cache the result.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = key_fn(*args, **kwargs)
            table = _get_table()
            ttl = int(time.time()) + IDEMPOTENCY_TTL_SECONDS

            # --- Attempt to claim the key (atomic conditional write) ---
            try:
                table.put_item(
                    Item={
                        "idempotency_key": key,
                        "status": "IN_FLIGHT",
                        "created_at": int(time.time()),
                        "ttl": ttl,
                    },
                    ConditionExpression="attribute_not_exists(idempotency_key)",
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise

                # Key already exists — check its status
                existing = table.get_item(Key={"idempotency_key": key}).get("Item", {})
                status = existing.get("status")

                if status == "COMPLETE":
                    logger.info("Idempotency cache hit for key=%s, returning cached result", key)
                    return json.loads(existing["result"])

                if status == "IN_FLIGHT":
                    raise IdempotencyAlreadyInProgressError(
                        f"Request {key!r} is already being processed. "
                        "Retry after a short delay."
                    )

                # Unknown status — treat as unrecoverable, delete and allow retry
                logger.warning("Unknown idempotency status %r for key=%s, deleting", status, key)
                table.delete_item(Key={"idempotency_key": key})
                raise IdempotencyError(f"Unexpected idempotency state: {status}")

            # --- Run the actual function ---
            try:
                result = fn(*args, **kwargs)
            except Exception:
                # Clean up so caller can retry with the same key
                table.delete_item(Key={"idempotency_key": key})
                raise

            # --- Cache the successful result ---
            table.update_item(
                Key={"idempotency_key": key},
                UpdateExpression="SET #s = :s, #r = :r",
                ExpressionAttributeNames={"#s": "status", "#r": "result"},
                ExpressionAttributeValues={":s": "COMPLETE", ":r": json.dumps(result)},
            )
            return result

        return wrapper
    return decorator


class IdempotencyKey:
    """Helpers for constructing consistent idempotency keys."""

    @staticmethod
    def from_sqs_message(record: dict) -> str:
        """
        Use the SQS MessageDeduplicationId if present (FIFO queues),
        otherwise fall back to MessageId. Never use the message body hash —
        two different requests can have the same payload.
        """
        attrs = record.get("attributes", {})
        return attrs.get("MessageDeduplicationId") or record["messageId"]

    @staticmethod
    def from_api_gateway(event: dict) -> str:
        """
        Use the client-supplied idempotency key from the request header.
        API Gateway clients MUST supply 'Idempotency-Key: <uuid>' header.
        """
        headers = event.get("headers") or {}
        key = headers.get("Idempotency-Key") or headers.get("idempotency-key")
        if not key:
            raise ValueError("Missing required 'Idempotency-Key' header")
        return key

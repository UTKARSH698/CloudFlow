"""
DynamoDB Helpers
================
Thin wrappers around boto3 that handle common patterns:
- Optimistic locking with version counters (prevent lost updates)
- Structured logging of all DB operations
- Consistent serialization of Pydantic models
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def get_table(table_name: str):
    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(table_name)


def put_item_with_optimistic_lock(
    table,
    item: dict,
    version_key: str = "version",
) -> None:
    """
    Write item with optimistic locking.

    Increments `version_key` on every write. If another process updated
    the item between our read and write, DynamoDB rejects the write.
    Caller should retry on OptimisticLockError.

    This is the poor man's transaction — no 2PC, no distributed lock,
    just compare-and-swap at the item level.
    """
    current_version = item.get(version_key, 0)
    new_item = {**item, version_key: current_version + 1}

    try:
        if current_version == 0:
            # New item — ensure it doesn't exist yet
            table.put_item(
                Item=new_item,
                ConditionExpression=Attr("pk").not_exists(),
            )
        else:
            # Existing item — ensure version hasn't changed
            table.put_item(
                Item=new_item,
                ConditionExpression=Attr(version_key).eq(current_version),
            )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise OptimisticLockError(
                f"Item was modified by another process (version mismatch at v{current_version})"
            ) from e
        raise


class OptimisticLockError(Exception):
    """Raised when a concurrent update was detected. Caller should retry."""


def decimal_to_python(obj: Any) -> Any:
    """
    DynamoDB returns Decimals for all numbers.
    Recursively convert to int or float for JSON serialization.
    """
    if isinstance(obj, Decimal):
        if obj % 1 == 0:
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: decimal_to_python(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decimal_to_python(v) for v in obj]
    return obj

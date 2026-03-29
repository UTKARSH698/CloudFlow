"""
DLQ Processor Lambda Handler
==============================
Processes failed messages from all Dead Letter Queues.

Why a DLQ processor?
  When a message fails delivery after maxReceiveCount retries it lands in the DLQ.
  Without a processor, these messages silently accumulate — the CloudWatch alarm tells
  us SOMETHING failed but not WHAT or WHY. This Lambda reads the DLQ, emits a
  structured error log with full context (order_id, queue, receive count, body), and
  lets CloudWatch Logs Insights surface actionable failure details.

Design: this is intentionally NOT a retry mechanism. Messages arrive here because they
  failed 3 consecutive times — retrying automatically risks repeating the same failure.
  The structured log is the artifact; human investigation and selective replay come next.
"""
from __future__ import annotations

import json
import os

from shared.logger import get_logger

logger = get_logger(__name__)


def handler(event: dict, context) -> dict:
    """
    SQS batch handler for DLQ messages.

    Always returns an empty batchItemFailures list — we never want a DLQ message
    to bounce back into the DLQ (that would create an infinite loop).
    The log record IS the output; operators query it in CloudWatch Logs Insights.
    """
    for record in event.get("Records", []):
        try:
            _log_failed_message(record)
        except Exception:
            logger.exception(
                "Error while logging DLQ record — original message details may be incomplete",
                extra={"message_id": record.get("messageId", "unknown")},
            )

    return {"batchItemFailures": []}


def _log_failed_message(record: dict) -> None:
    message_id = record.get("messageId", "unknown")
    queue_arn = record.get("eventSourceARN", "unknown")
    queue_name = queue_arn.split(":")[-1] if ":" in queue_arn else queue_arn
    receive_count = record.get("attributes", {}).get("ApproximateReceiveCount", "unknown")

    try:
        body = json.loads(record.get("body", "{}"))
    except (json.JSONDecodeError, TypeError):
        body = {"raw": record.get("body", "")}

    logger.error(
        "DLQ message requires investigation",
        extra={
            "message_id": message_id,
            "queue": queue_name,
            "receive_count": receive_count,
            "order_id": body.get("order_id", "unknown"),
            "notification_type": body.get("notification_type"),
            "body": body,
        },
    )

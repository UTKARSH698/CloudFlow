"""
Notification Service Lambda Handler
=====================================
Triggered by SQS messages (not Step Functions directly).
This service is fire-and-forget from the SAGA's perspective.

Why SQS instead of Step Functions direct invocation?
  Notifications are not in the critical path. If SNS is slow, we don't want
  to delay the SAGA result. Decoupling via SQS means:
  - The SAGA completes immediately once order is confirmed
  - Notification delivery is retried independently (up to 3 times + DLQ)
  - Notification failures don't cause order rollback

SQS visibility timeout vs Lambda timeout:
  If the Lambda crashes mid-processing, the message becomes visible again
  after `visibilityTimeout` seconds. Idempotency ensures we don't
  double-notify even if the message is re-delivered.
"""
from __future__ import annotations

import json
import logging
import os

import boto3
from aws_xray_sdk.core import patch_all

from shared.idempotency import IdempotencyKey, idempotent

patch_all()

from shared.logger import get_logger
logger = get_logger(__name__)

SNS_TOPIC_ARN = os.environ.get("NOTIFICATION_TOPIC_ARN", "")
sns_client = boto3.client("sns")


# ---------------------------------------------------------------------------
# Entry point — triggered by SQS event source mapping
# ---------------------------------------------------------------------------

def handler(event: dict, context) -> dict:
    """
    SQS batch handler. Each record is one notification request.

    Returns {"batchItemFailures": [...]} to enable partial batch failure —
    only failed messages are retried, not the entire batch.
    See: https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html
    """
    failures = []

    for record in event.get("Records", []):
        try:
            _process_record(record)
        except Exception:
            logger.exception("Failed to process SQS record %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------

def _process_record(record: dict) -> None:
    idempotency_key = IdempotencyKey.from_sqs_message(record)
    body = json.loads(record["body"])

    @idempotent(key_fn=lambda: f"notify-{idempotency_key}")
    def _do_notify():
        notification_type = body.get("notification_type", "ORDER_CONFIRMED")
        order_id = body["order_id"]
        customer_id = body["customer_id"]

        message = _build_message(notification_type, order_id, body)

        if SNS_TOPIC_ARN:
            sns_client.publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject=_build_subject(notification_type, order_id),
                Message=message,
                MessageAttributes={
                    "notification_type": {
                        "DataType": "String",
                        "StringValue": notification_type,
                    },
                    "customer_id": {
                        "DataType": "String",
                        "StringValue": customer_id,
                    },
                },
            )

        logger.info(
            "Notification sent",
            extra={
                "order_id": order_id,
                "customer_id": customer_id,
                "notification_type": notification_type,
            },
        )
        return {"sent": True}

    _do_notify()


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

def _build_subject(notification_type: str, order_id: str) -> str:
    subjects = {
        "ORDER_CONFIRMED": f"Your order {order_id[:8].upper()} is confirmed!",
        "ORDER_FAILED": f"We couldn't process your order {order_id[:8].upper()}",
    }
    return subjects.get(notification_type, f"Order update: {order_id[:8].upper()}")


def _build_message(notification_type: str, order_id: str, body: dict) -> str:
    if notification_type == "ORDER_CONFIRMED":
        total = body.get("total_cents", 0) / 100
        return (
            f"Your order has been confirmed!\n\n"
            f"Order ID: {order_id}\n"
            f"Total: ${total:.2f}\n\n"
            f"Your items will be shipped within 2-3 business days."
        )
    if notification_type == "ORDER_FAILED":
        return (
            f"We were unable to process your order.\n\n"
            f"Order ID: {order_id}\n"
            f"Reason: {body.get('error_reason', 'Payment or inventory issue')}\n\n"
            f"No charges have been made. Please try again."
        )
    return f"Order {order_id} status update."

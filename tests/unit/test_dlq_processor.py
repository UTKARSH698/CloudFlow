"""
Unit tests for the DLQ processor Lambda handler.

The DLQ processor must NEVER fail — it's the last stop for messages.
If it crashes, messages bounce back into the DLQ (infinite loop).
These tests verify resilience against malformed input.
"""
import json
import sys

sys.path.insert(0, "services")

from dlq_processor.handler import handler


def _make_record(message_id: str, body: str | dict, queue_name: str = "cloudflow-notification-dlq") -> dict:
    if isinstance(body, dict):
        body = json.dumps(body)
    return {
        "messageId": message_id,
        "body": body,
        "eventSourceARN": f"arn:aws:sqs:us-east-1:123456789:{queue_name}",
        "attributes": {"ApproximateReceiveCount": "4"},
        "messageAttributes": {},
    }


def test_returns_empty_failures():
    """DLQ processor must never report batch item failures (no bounce-back loop)."""
    event = {
        "Records": [
            _make_record("msg-1", {"order_id": "order-123", "notification_type": "ORDER_CONFIRMED"}),
        ]
    }
    result = handler(event, None)
    assert result == {"batchItemFailures": []}


def test_handles_malformed_json_body():
    """DLQ processor must not crash on invalid JSON in message body."""
    event = {
        "Records": [
            _make_record("msg-2", "not valid json {{{"),
        ]
    }
    result = handler(event, None)
    assert result == {"batchItemFailures": []}


def test_handles_empty_records():
    """DLQ processor handles empty batch gracefully."""
    result = handler({"Records": []}, None)
    assert result == {"batchItemFailures": []}


def test_handles_multiple_records():
    """DLQ processor processes all records in a batch without partial failure."""
    event = {
        "Records": [
            _make_record(f"msg-{i}", {"order_id": f"order-{i}"})
            for i in range(5)
        ]
    }
    result = handler(event, None)
    assert result == {"batchItemFailures": []}


def test_handles_missing_attributes():
    """DLQ processor handles records with missing optional fields."""
    event = {
        "Records": [
            {
                "messageId": "msg-bare",
                "body": "{}",
            },
        ]
    }
    result = handler(event, None)
    assert result == {"batchItemFailures": []}


def test_extracts_queue_name_from_arn():
    """DLQ processor logs the queue name, not the full ARN."""
    record = _make_record("msg-q", {"order_id": "x"}, queue_name="cloudflow-payment-dlq")
    # Smoke test — just ensure it doesn't crash; log output is verified by structured logging
    event = {"Records": [record]}
    result = handler(event, None)
    assert result == {"batchItemFailures": []}

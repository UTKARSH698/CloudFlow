"""
Unit tests for the notification service Lambda handler.

The notification service is SQS-triggered and fire-and-forget from the SAGA's
perspective. These tests verify:
  - message/subject templates for each notification type
  - the SQS batch handler publishes to SNS and records idempotency
  - a bad record is reported as a batch item failure (retried in isolation)
"""
import json
import sys

sys.path.insert(0, "services")

import boto3
from moto import mock_aws


def _make_idempotency_table(client):
    client.create_table(
        TableName="test-idempotency",
        AttributeDefinitions=[{"AttributeName": "idempotency_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _sqs_record(message_id: str, body: dict) -> dict:
    return {"messageId": message_id, "body": json.dumps(body), "attributes": {}}


# ---------------------------------------------------------------------------
# Message templates (pure functions — no AWS)
# ---------------------------------------------------------------------------

def test_confirmed_subject_and_message(aws_env):
    from notification_service import handler as h

    subject = h._build_subject("ORDER_CONFIRMED", "abcdef12-9999")
    assert "ABCDEF12" in subject

    message = h._build_message("ORDER_CONFIRMED", "order-1", {"total_cents": 2599})
    assert "$25.99" in message
    assert "confirmed" in message.lower()


def test_failed_subject_and_message(aws_env):
    from notification_service import handler as h

    subject = h._build_subject("ORDER_FAILED", "abcdef12-9999")
    assert "couldn't" in subject.lower()

    message = h._build_message("ORDER_FAILED", "order-2", {"error_reason": "Card declined"})
    assert "Card declined" in message
    assert "No charges" in message


def test_unknown_type_falls_back_to_generic(aws_env):
    from notification_service import handler as h

    assert "update" in h._build_subject("SOMETHING_ELSE", "order-xyz12345").lower()
    assert h._build_message("SOMETHING_ELSE", "order-9", {}) == "Order order-9 status update."


# ---------------------------------------------------------------------------
# SQS batch handler
# ---------------------------------------------------------------------------

@mock_aws
def test_handler_publishes_and_records_idempotency(aws_env, monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_idempotency_table(client)
    sns = boto3.client("sns", region_name="us-east-1")
    topic_arn = sns.create_topic(Name="cloudflow-notifications")["TopicArn"]

    from notification_service import handler as h

    monkeypatch.setattr(h, "SNS_TOPIC_ARN", topic_arn)
    monkeypatch.setattr(h, "sns_client", boto3.client("sns", region_name="us-east-1"))

    event = {
        "Records": [
            _sqs_record(
                "m1",
                {
                    "order_id": "order-123",
                    "customer_id": "cust-1",
                    "notification_type": "ORDER_CONFIRMED",
                    "total_cents": 1000,
                },
            )
        ]
    }
    result = h.handler(event, None)
    assert result == {"batchItemFailures": []}

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("test-idempotency")
    item = table.get_item(Key={"idempotency_key": "notify-m1"}).get("Item")
    assert item is not None
    assert item["status"] == "COMPLETE"


@mock_aws
def test_handler_reports_failure_for_bad_record(aws_env):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_idempotency_table(client)

    from notification_service import handler as h

    # Missing "order_id" makes _do_notify raise -> reported as a batch item failure.
    event = {"Records": [_sqs_record("bad1", {"notification_type": "ORDER_CONFIRMED"})]}
    result = h.handler(event, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad1"}]}


@mock_aws
def test_handler_handles_empty_batch(aws_env):
    client = boto3.client("dynamodb", region_name="us-east-1")
    _make_idempotency_table(client)

    from notification_service import handler as h

    assert h.handler({"Records": []}, None) == {"batchItemFailures": []}

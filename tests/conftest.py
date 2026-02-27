"""
Pytest configuration and shared fixtures.
Unit tests use moto (AWS mocks in-process).
Integration tests use LocalStack (real service emulation via Docker).
"""
import os

import boto3
import pytest
from moto import mock_aws

# Point all boto3 calls at LocalStack when running integration tests
LOCALSTACK_ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "http://localhost:4566")
USE_LOCALSTACK = os.environ.get("USE_LOCALSTACK", "false").lower() == "true"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    """Set fake AWS credentials so boto3 doesn't error in tests."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "test-idempotency")
    monkeypatch.setenv("CIRCUIT_BREAKER_TABLE", "test-circuit-breakers")
    monkeypatch.setenv("ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("INVENTORY_TABLE", "test-inventory")
    monkeypatch.setenv("RESERVATIONS_TABLE", "test-reservations")
    monkeypatch.setenv("PAYMENTS_TABLE", "test-payments")
    monkeypatch.setenv("EVENT_BUS_NAME", "test-bus")
    monkeypatch.setenv("SAGA_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")


@pytest.fixture
def dynamodb_tables(aws_env):
    """
    Create all required DynamoDB tables using moto (in-process mock).
    Faster than LocalStack for unit tests — no Docker required.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")

        tables = [
            ("test-idempotency", "idempotency_key", None),
            ("test-circuit-breakers", "name", None),
            ("test-orders", "pk", "sk"),
            ("test-inventory", "product_id", None),
            ("test-reservations", "reservation_id", None),
            ("test-payments", "payment_id", None),
        ]

        for table_name, pk, sk in tables:
            attr_defs = [{"AttributeName": pk, "AttributeType": "S"}]
            key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
            if sk:
                attr_defs.append({"AttributeName": sk, "AttributeType": "S"})
                key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})

            client.create_table(
                TableName=table_name,
                AttributeDefinitions=attr_defs,
                KeySchema=key_schema,
                BillingMode="PAY_PER_REQUEST",
            )

        yield boto3.resource("dynamodb", region_name="us-east-1")

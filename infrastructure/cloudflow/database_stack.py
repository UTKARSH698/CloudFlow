"""
Database Stack
==============
All DynamoDB tables.

Design principles applied:
1. Single-table design for Orders (reduces RCUs for relational queries)
2. Separate tables per service (enforce microservice ownership)
3. TTL on idempotency table (prevents unbounded growth)
4. Pay-per-request billing (no capacity planning for unpredictable workloads)
5. Point-in-time recovery enabled (compliance + disaster recovery)
"""
import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct


class DatabaseStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.tables = {}

        # ----------------------------------------------------------------
        # Orders table (single-table design)
        # PK: ORDER#{order_id}
        # SK: META | EVENT#{timestamp}
        # ----------------------------------------------------------------
        self.tables["orders"] = dynamodb.Table(
            self, "OrdersTable",
            table_name="cloudflow-orders",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,  # use RETAIN in production
        )

        # GSI: query orders by customer (for "my orders" API)
        self.tables["orders"].add_global_secondary_index(
            index_name="customer-index",
            partition_key=dynamodb.Attribute(name="customer_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
        )

        # ----------------------------------------------------------------
        # Inventory table
        # PK: product_id
        # ----------------------------------------------------------------
        self.tables["inventory"] = dynamodb.Table(
            self, "InventoryTable",
            table_name="cloudflow-inventory",
            partition_key=dynamodb.Attribute(name="product_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ----------------------------------------------------------------
        # Reservations table
        # PK: reservation_id
        # ----------------------------------------------------------------
        self.tables["reservations"] = dynamodb.Table(
            self, "ReservationsTable",
            table_name="cloudflow-reservations",
            partition_key=dynamodb.Attribute(name="reservation_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ----------------------------------------------------------------
        # Payments table
        # PK: payment_id
        # ----------------------------------------------------------------
        self.tables["payments"] = dynamodb.Table(
            self, "PaymentsTable",
            table_name="cloudflow-payments",
            partition_key=dynamodb.Attribute(name="payment_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ----------------------------------------------------------------
        # Idempotency table (shared across all services)
        # PK: idempotency_key
        # TTL: auto-expire after 24h
        # ----------------------------------------------------------------
        self.tables["idempotency"] = dynamodb.Table(
            self, "IdempotencyTable",
            table_name="cloudflow-idempotency",
            partition_key=dynamodb.Attribute(name="idempotency_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",  # DynamoDB auto-deletes expired records
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ----------------------------------------------------------------
        # Circuit breaker state table (shared)
        # PK: name (breaker name)
        # ----------------------------------------------------------------
        self.tables["circuit_breakers"] = dynamodb.Table(
            self, "CircuitBreakerTable",
            table_name="cloudflow-circuit-breakers",
            partition_key=dynamodb.Attribute(name="name", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # Output table names for cross-stack references
        for name, table in self.tables.items():
            cdk.CfnOutput(self, f"{name.title()}TableName", value=table.table_name)

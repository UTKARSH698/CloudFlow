"""
API Stack
=========
API Gateway + all four Lambda functions.

Lambda configuration highlights:
- X-Ray active tracing enabled on all functions
- Reserved concurrency on payment service (prevents runaway spend)
- Lambda Powertools layer for structured logging + metrics
- Shared Lambda layer for the /services/shared/ code
"""
import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from constructs import Construct

LAMBDA_RUNTIME = _lambda.Runtime.PYTHON_3_11


class ApiStack(cdk.Stack):
    def __init__(self, scope, id: str, *, tables, event_bus, queues, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.lambdas: dict[str, _lambda.Function] = {}

        # ----------------------------------------------------------------
        # Shared Lambda layer: /services/shared/
        # ----------------------------------------------------------------
        shared_layer = _lambda.LayerVersion(
            self, "SharedLayer",
            code=_lambda.Code.from_asset("../services/shared"),
            compatible_runtimes=[LAMBDA_RUNTIME],
            description="CloudFlow shared utilities (events, idempotency, circuit_breaker)",
        )

        # ----------------------------------------------------------------
        # Common environment variables
        # ----------------------------------------------------------------
        common_env = {
            "ORDERS_TABLE": tables["orders"].table_name,
            "INVENTORY_TABLE": tables["inventory"].table_name,
            "RESERVATIONS_TABLE": tables["reservations"].table_name,
            "PAYMENTS_TABLE": tables["payments"].table_name,
            "IDEMPOTENCY_TABLE": tables["idempotency"].table_name,
            "CIRCUIT_BREAKER_TABLE": tables["circuit_breakers"].table_name,
            "EVENT_BUS_NAME": event_bus.event_bus_name,
            "POWERTOOLS_SERVICE_NAME": "cloudflow",
            "LOG_LEVEL": "INFO",
        }

        # ----------------------------------------------------------------
        # Order Service
        # ----------------------------------------------------------------
        self.order_fn = _lambda.Function(
            self, "OrderFunction",
            function_name="cloudflow-order-service",
            runtime=LAMBDA_RUNTIME,
            handler="handler.handler",
            code=_lambda.Code.from_asset("../services/order_service"),
            layers=[shared_layer],
            # SAGA_STATE_MACHINE_ARN is intentionally "PLACEHOLDER" here.
            # SagaStack depends on this Lambda, so it must be created after ApiStack.
            # app.py injects the real ARN via add_environment() once both stacks exist.
            environment={**common_env, "SAGA_STATE_MACHINE_ARN": "PLACEHOLDER"},
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_WEEK,
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
        )
        self.lambdas["order"] = self.order_fn

        # Grant DynamoDB permissions
        tables["orders"].grant_read_write_data(self.order_fn)
        tables["idempotency"].grant_read_write_data(self.order_fn)
        event_bus.grant_put_events_to(self.order_fn)

        # ----------------------------------------------------------------
        # Inventory Service
        # ----------------------------------------------------------------
        inventory_fn = _lambda.Function(
            self, "InventoryFunction",
            function_name="cloudflow-inventory-service",
            runtime=LAMBDA_RUNTIME,
            handler="handler.handler",
            code=_lambda.Code.from_asset("../services/inventory_service"),
            layers=[shared_layer],
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_WEEK,
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
        )
        self.lambdas["inventory"] = inventory_fn
        tables["inventory"].grant_read_write_data(inventory_fn)
        tables["reservations"].grant_read_write_data(inventory_fn)
        tables["idempotency"].grant_read_write_data(inventory_fn)

        # ----------------------------------------------------------------
        # Payment Service (reserved concurrency — cost control)
        # ----------------------------------------------------------------
        payment_fn = _lambda.Function(
            self, "PaymentFunction",
            function_name="cloudflow-payment-service",
            runtime=LAMBDA_RUNTIME,
            handler="handler.handler",
            code=_lambda.Code.from_asset("../services/payment_service"),
            layers=[shared_layer],
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_WEEK,
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            reserved_concurrent_executions=50,  # Hard cap: prevent runaway payment calls
        )
        self.lambdas["payment"] = payment_fn
        tables["payments"].grant_read_write_data(payment_fn)
        tables["idempotency"].grant_read_write_data(payment_fn)
        tables["circuit_breakers"].grant_read_write_data(payment_fn)

        # ----------------------------------------------------------------
        # Notification Service (SQS-triggered)
        # ----------------------------------------------------------------
        notification_fn = _lambda.Function(
            self, "NotificationFunction",
            function_name="cloudflow-notification-service",
            runtime=LAMBDA_RUNTIME,
            handler="handler.handler",
            code=_lambda.Code.from_asset("../services/notification_service"),
            layers=[shared_layer],
            environment=common_env,
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_WEEK,
            timeout=cdk.Duration.seconds(30),
            memory_size=128,
        )
        self.lambdas["notification"] = notification_fn
        tables["idempotency"].grant_read_write_data(notification_fn)

        # SQS event source mapping with partial batch failure reporting
        notification_fn.add_event_source(
            event_sources.SqsEventSource(
                queues["notification"],
                batch_size=10,
                report_batch_item_failures=True,  # Only retry failed messages
            )
        )

        # ----------------------------------------------------------------
        # API Gateway
        # ----------------------------------------------------------------
        log_group = logs.LogGroup(self, "ApiGwLogs", retention=logs.RetentionDays.ONE_WEEK)

        api = apigw.RestApi(
            self, "CloudFlowApi",
            rest_api_name="cloudflow-api",
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                access_log_destination=apigw.LogGroupLogDestination(log_group),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    calling_user=True,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=True,
                ),
                tracing_enabled=True,
            ),
        )

        order_integration = apigw.LambdaIntegration(self.order_fn)
        orders_resource = api.root.add_resource("orders")
        orders_resource.add_method("POST", order_integration)

        order_id_resource = orders_resource.add_resource("{orderId}")
        order_id_resource.add_method("GET", order_integration)

        cdk.CfnOutput(self, "ApiUrl", value=api.url)

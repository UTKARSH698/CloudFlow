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
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

LAMBDA_RUNTIME = _lambda.Runtime.PYTHON_3_11


class ApiStack(cdk.Stack):
    def __init__(self, scope, id: str, *, tables, event_bus, queues, notification_topic, **kwargs):
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
        # Secrets Manager — payment provider credentials
        # Rotating this secret is safe: Lambda fetches it once per container
        # lifetime, so a rotation triggers a cold start on next invocation.
        # ----------------------------------------------------------------
        payment_provider_secret = sm.Secret(
            self, "PaymentProviderSecret",
            secret_name="cloudflow/payment-provider-url",
            description="External payment provider base URL for the CloudFlow payment service",
        )

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
            environment={**common_env, "PAYMENT_PROVIDER_SECRET_NAME": payment_provider_secret.secret_name},
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
        payment_provider_secret.grant_read(payment_fn)

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
            environment={**common_env, "NOTIFICATION_TOPIC_ARN": notification_topic.topic_arn},
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_WEEK,
            timeout=cdk.Duration.seconds(30),
            memory_size=128,
        )
        self.lambdas["notification"] = notification_fn
        tables["idempotency"].grant_read_write_data(notification_fn)
        notification_topic.grant_publish(notification_fn)

        # SQS event source mapping with partial batch failure reporting
        notification_fn.add_event_source(
            event_sources.SqsEventSource(
                queues["notification"],
                batch_size=10,
                report_batch_item_failures=True,  # Only retry failed messages
            )
        )

        # ----------------------------------------------------------------
        # DLQ Processor — structured logging for failed messages
        # Triggered by all DLQs; never retries, only logs for investigation.
        # ----------------------------------------------------------------
        dlq_fn = _lambda.Function(
            self, "DlqProcessorFunction",
            function_name="cloudflow-dlq-processor",
            runtime=LAMBDA_RUNTIME,
            handler="handler.handler",
            code=_lambda.Code.from_asset("../services/dlq_processor"),
            layers=[shared_layer],
            environment={"LOG_LEVEL": "ERROR", "POWERTOOLS_SERVICE_NAME": "cloudflow-dlq"},
            tracing=_lambda.Tracing.ACTIVE,
            log_retention=logs.RetentionDays.ONE_MONTH,  # Longer retention for forensics
            timeout=cdk.Duration.seconds(60),
            memory_size=128,
        )
        self.lambdas["dlq_processor"] = dlq_fn

        for dlq_name in ["inventory-dlq", "payment-dlq", "notification-dlq"]:
            dlq_fn.add_event_source(
                event_sources.SqsEventSource(
                    queues[dlq_name],
                    batch_size=10,
                    report_batch_item_failures=True,
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

        # API key + usage plan — require x-api-key header on all requests.
        # Callers retrieve their key via AWS Console / CLI; rotate quarterly.
        api_key = api.add_api_key(
            "CloudFlowApiKey",
            api_key_name="cloudflow-api-key",
            description="Primary API key for CloudFlow — rotate quarterly",
        )
        usage_plan = api.add_usage_plan(
            "CloudFlowUsagePlan",
            name="CloudFlowPlan",
            throttle=apigw.ThrottleSettings(rate_limit=100, burst_limit=200),
            quota=apigw.QuotaSettings(limit=100_000, period=apigw.Period.MONTH),
        )
        usage_plan.add_api_key(api_key)
        usage_plan.add_api_stage(stage=api.deployment_stage)

        order_integration = apigw.LambdaIntegration(self.order_fn)
        orders_resource = api.root.add_resource("orders")
        orders_resource.add_method("POST", order_integration, api_key_required=True)

        order_id_resource = orders_resource.add_resource("{orderId}")
        order_id_resource.add_method("GET", order_integration, api_key_required=True)

        cdk.CfnOutput(self, "ApiUrl", value=api.url)
        cdk.CfnOutput(self, "ApiKeyId", value=api_key.key_id)

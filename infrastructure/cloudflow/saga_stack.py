"""
SAGA State Machine Stack
========================
AWS Step Functions state machine implementing the SAGA orchestration pattern.

The state machine definition is the most important artifact in this project —
it makes the distributed transaction protocol EXPLICIT and VISIBLE instead of
hiding it in message handler code.

States:
  ReserveInventory ──────── success ──▶ ChargePayment ── success ──▶ ConfirmOrder
       │                                      │
       │ fail                                 │ fail
       ▼                                      ▼
  FailOrder ◀──────────────────── ReleaseInventory ──▶ FailOrder

Retry strategy:
  Each state retries on transient errors (Lambda.ServiceException,
  Lambda.TooManyRequestsException) with exponential backoff.
  Business errors (INSUFFICIENT_STOCK, PAYMENT_DECLINED) are NOT retried —
  they trigger the compensation path immediately.
"""
import json

import aws_cdk as cdk
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct


class SagaStack(cdk.Stack):
    def __init__(self, scope, id: str, *, tables, queues, lambdas, **kwargs):
        super().__init__(scope, id, **kwargs)

        # ----------------------------------------------------------------
        # Lambda task definitions
        # ----------------------------------------------------------------

        reserve_inventory = tasks.LambdaInvoke(
            self, "ReserveInventory",
            lambda_function=lambdas["inventory"],
            payload=sfn.TaskInput.from_object({
                "action": "reserve",
                "order_id.$": "$.order_id",
                "items.$": "$.items",
                "correlation_id.$": "$.correlation_id",
            }),
            result_selector={"body.$": "$.Payload"},
            result_path="$.inventory_result",
            retry_on_service_exceptions=True,
        )

        release_inventory = tasks.LambdaInvoke(
            self, "ReleaseInventory",
            lambda_function=lambdas["inventory"],
            payload=sfn.TaskInput.from_object({
                "action": "release",
                "order_id.$": "$.order_id",
                "reservation_id.$": "$.inventory_result.body.reservation_id",
                "correlation_id.$": "$.correlation_id",
            }),
            result_path="$.release_result",
            retry_on_service_exceptions=True,
        )

        charge_payment = tasks.LambdaInvoke(
            self, "ChargePayment",
            lambda_function=lambdas["payment"],
            payload=sfn.TaskInput.from_object({
                "action": "charge",
                "order_id.$": "$.order_id",
                "customer_id.$": "$.customer_id",
                "total_cents.$": "$.total_cents",
                "correlation_id.$": "$.correlation_id",
            }),
            result_selector={"body.$": "$.Payload"},
            result_path="$.payment_result",
            retry_on_service_exceptions=True,
        )

        # ----------------------------------------------------------------
        # SQS send tasks (fire-and-forget notifications)
        # ----------------------------------------------------------------

        notify_confirmed = tasks.SqsSendMessage(
            self, "NotifyOrderConfirmed",
            queue=queues["notification"],
            message_body=sfn.TaskInput.from_object({
                "notification_type": "ORDER_CONFIRMED",
                "order_id.$": "$.order_id",
                "customer_id.$": "$.customer_id",
                "total_cents.$": "$.total_cents",
            }),
            result_path=sfn.JsonPath.DISCARD,
        )

        notify_failed = tasks.SqsSendMessage(
            self, "NotifyOrderFailed",
            queue=queues["notification"],
            message_body=sfn.TaskInput.from_object({
                "notification_type": "ORDER_FAILED",
                "order_id.$": "$.order_id",
                "customer_id.$": "$.customer_id",
                "error_reason.$": "$.error",
            }),
            result_path=sfn.JsonPath.DISCARD,
        )

        # ----------------------------------------------------------------
        # Terminal states
        # ----------------------------------------------------------------

        order_confirmed = sfn.Succeed(self, "OrderConfirmed")
        order_failed = sfn.Fail(self, "OrderFailed", cause="SAGA compensation complete")

        # ----------------------------------------------------------------
        # Compensation path: release inventory → notify → fail
        # ----------------------------------------------------------------
        compensation_chain = (
            release_inventory
            .next(notify_failed)
            .next(order_failed)
        )

        # ----------------------------------------------------------------
        # Choice: did charge succeed?
        # ----------------------------------------------------------------
        payment_succeeded = sfn.Choice(self, "PaymentSucceeded?")
        payment_succeeded.when(
            sfn.Condition.boolean_equals("$.payment_result.body.success", True),
            notify_confirmed.next(order_confirmed),
        )
        payment_succeeded.otherwise(
            sfn.Pass(
                self, "SetPaymentFailedError",
                parameters={"error": "Payment declined or provider unavailable"},
                result_path="$",
            ).next(compensation_chain)
        )

        charge_payment.next(payment_succeeded)

        # ----------------------------------------------------------------
        # Choice: did reserve succeed?
        # ----------------------------------------------------------------
        inventory_succeeded = sfn.Choice(self, "InventoryReserved?")
        inventory_succeeded.when(
            sfn.Condition.boolean_equals("$.inventory_result.body.success", True),
            charge_payment,
        )
        inventory_succeeded.otherwise(
            sfn.Pass(
                self, "SetInventoryFailedError",
                parameters={"error": "Insufficient stock"},
                result_path="$",
            ).next(
                sfn.Pass(self, "NoInventoryToRelease")
                .next(notify_failed)
                .next(order_failed)
            )
        )

        reserve_inventory.next(inventory_succeeded)

        # ----------------------------------------------------------------
        # State machine
        # ----------------------------------------------------------------
        sm_log_group = logs.LogGroup(
            self, "SagaLogGroup",
            log_group_name="/aws/states/cloudflow-order-saga",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        self.state_machine = sfn.StateMachine(
            self, "OrderSagaStateMachine",
            state_machine_name="cloudflow-order-saga",
            definition_body=sfn.DefinitionBody.from_chainable(reserve_inventory),
            timeout=cdk.Duration.minutes(5),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=sm_log_group,
                level=sfn.LogLevel.ALL,  # log every state transition
                include_execution_data=True,
            ),
        )

        cdk.CfnOutput(
            self, "StateMachineArn",
            value=self.state_machine.state_machine_arn,
        )

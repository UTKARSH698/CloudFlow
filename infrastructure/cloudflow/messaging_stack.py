"""
Messaging Stack
===============
SQS queues and EventBridge event bus.

Each queue has a paired Dead Letter Queue (DLQ).
DLQs are where messages go after maxReceiveCount delivery attempts fail.
CloudWatch alarms on DLQ depth page on-call when messages start accumulating.

SQS visibility timeout > Lambda timeout:
  If Lambda takes up to 30s and crashes, the message must stay invisible
  longer than 30s so it isn't delivered to another consumer while the first
  is still processing. Setting timeout to 6x Lambda timeout is AWS best practice.
"""
import aws_cdk as cdk
from aws_cdk import aws_events as events
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class MessagingStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        self.queues: dict[str, sqs.Queue] = {}

        # ----------------------------------------------------------------
        # EventBridge custom bus — all domain events flow through here
        # ----------------------------------------------------------------
        self.event_bus = events.EventBus(
            self, "CloudFlowEventBus",
            event_bus_name="cloudflow-events",
        )
        cdk.CfnOutput(self, "EventBusName", value=self.event_bus.event_bus_name)

        # ----------------------------------------------------------------
        # Helper: create queue + DLQ pair
        # ----------------------------------------------------------------
        def make_queue(name: str, lambda_timeout_seconds: int = 30) -> sqs.Queue:
            dlq = sqs.Queue(
                self, f"{name}Dlq",
                queue_name=f"cloudflow-{name}-dlq",
                retention_period=cdk.Duration.days(14),
            )
            queue = sqs.Queue(
                self, f"{name}Queue",
                queue_name=f"cloudflow-{name}",
                visibility_timeout=cdk.Duration.seconds(lambda_timeout_seconds * 6),
                dead_letter_queue=sqs.DeadLetterQueue(
                    max_receive_count=3,   # retry 3 times before DLQ
                    queue=dlq,
                ),
            )
            self.queues[f"{name}-dlq"] = dlq
            return queue

        # ----------------------------------------------------------------
        # Per-service queues
        # ----------------------------------------------------------------
        self.queues["inventory"] = make_queue("inventory")
        self.queues["payment"] = make_queue("payment")
        self.queues["notification"] = make_queue("notification")

        # Output queue URLs
        for name, queue in self.queues.items():
            if not name.endswith("-dlq"):
                cdk.CfnOutput(self, f"{name.title()}QueueUrl", value=queue.queue_url)

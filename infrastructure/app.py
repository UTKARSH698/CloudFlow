#!/usr/bin/env python3
"""
CloudFlow CDK App
=================
Infrastructure as Code using AWS CDK (Python).

Why CDK over Terraform or CloudFormation?
  CDK generates CloudFormation but adds:
  - Real programming language (loops, conditionals, functions)
  - Type-safe constructs with IDE autocompletion
  - Higher-level abstractions (e.g., aws_lambda_event_sources)
  - Native Python tooling (pytest, mypy)

Stack dependency order:
  DatabaseStack → MessagingStack → ApiStack → SagaStack → MonitoringStack

Run: cdk deploy --all
"""
import aws_cdk as cdk

from cloudflow.database_stack import DatabaseStack
from cloudflow.messaging_stack import MessagingStack
from cloudflow.api_stack import ApiStack
from cloudflow.saga_stack import SagaStack
from cloudflow.monitoring_stack import MonitoringStack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account"),
    region=app.node.try_get_context("region") or "us-east-1",
)

db_stack = DatabaseStack(app, "CloudFlowDatabase", env=env)
messaging_stack = MessagingStack(app, "CloudFlowMessaging", env=env)
api_stack = ApiStack(
    app, "CloudFlowApi",
    tables=db_stack.tables,
    event_bus=messaging_stack.event_bus,
    queues=messaging_stack.queues,
    env=env,
)
saga_stack = SagaStack(
    app, "CloudFlowSaga",
    tables=db_stack.tables,
    queues=messaging_stack.queues,
    lambdas=api_stack.lambdas,
    env=env,
)
MonitoringStack(
    app, "CloudFlowMonitoring",
    lambdas=api_stack.lambdas,
    queues=messaging_stack.queues,
    env=env,
)

# Update the Order Service lambda with the SAGA state machine ARN (circular dep avoided)
api_stack.order_fn.add_environment(
    "SAGA_STATE_MACHINE_ARN", saga_stack.state_machine.state_machine_arn
)
saga_stack.state_machine.grant_start_execution(api_stack.order_fn)

app.synth()

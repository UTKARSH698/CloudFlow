"""
Monitoring Stack
================
CloudWatch dashboard + alarms for production observability.

The four golden signals (Brendan Gregg / Google SRE book):
  1. Latency     — P99 Lambda duration per service
  2. Traffic     — Invocations per minute
  3. Errors      — Error rate per service
  4. Saturation  — Concurrent executions, DLQ depth

Alarm philosophy: alarm on symptoms (high error rate) not causes (CPU usage).
A user doesn't care if CPU is high; they care if orders are failing.
"""
import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from constructs import Construct


class MonitoringStack(cdk.Stack):
    def __init__(self, scope, id: str, *, lambdas, queues, **kwargs):
        super().__init__(scope, id, **kwargs)

        # SNS topic for alarm notifications
        alarm_topic = sns.Topic(self, "AlarmTopic", topic_name="cloudflow-alarms")

        # ----------------------------------------------------------------
        # Per-service Lambda metrics
        # ----------------------------------------------------------------
        service_widgets = []
        for name, fn in lambdas.items():
            error_metric = fn.metric_errors(
                period=cdk.Duration.minutes(1),
                statistic="Sum",
            )
            duration_p99 = fn.metric_duration(
                period=cdk.Duration.minutes(5),
                statistic="p99",
            )

            # Alarm: >5 errors in 5 min
            error_alarm = cw.Alarm(
                self, f"{name.title()}ErrorAlarm",
                alarm_name=f"cloudflow-{name}-errors",
                metric=error_metric,
                threshold=5,
                evaluation_periods=5,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            error_alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))

            service_widgets.append(
                cw.GraphWidget(
                    title=f"{name.title()} Service",
                    left=[error_metric],
                    right=[duration_p99],
                    width=12,
                )
            )

        # ----------------------------------------------------------------
        # DLQ depth alarms (poison pill detection)
        # ----------------------------------------------------------------
        dlq_widgets = []
        for name, queue in queues.items():
            if "dlq" not in name:
                continue
            dlq_metric = queue.metric_approximate_number_of_messages_visible(
                period=cdk.Duration.minutes(1),
                statistic="Maximum",
            )
            cw.Alarm(
                self, f"{name.replace('-', '').title()}DepthAlarm",
                alarm_name=f"cloudflow-{name}-depth",
                metric=dlq_metric,
                threshold=1,  # Any message in DLQ = alert
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

            dlq_widgets.append(
                cw.GraphWidget(
                    title=f"{name} Depth",
                    left=[dlq_metric],
                    width=8,
                )
            )

        # ----------------------------------------------------------------
        # CloudWatch Dashboard
        # ----------------------------------------------------------------
        dashboard = cw.Dashboard(self, "CloudFlowDashboard", dashboard_name="CloudFlow")
        dashboard.add_widgets(
            cw.TextWidget(
                markdown="# CloudFlow — Order Processing Platform\n"
                         "Real-time metrics for all services. [Runbook](https://github.com/your-org/cloudflow/wiki/runbook)",
                width=24,
            )
        )
        for row_widgets in [service_widgets[i:i+2] for i in range(0, len(service_widgets), 2)]:
            dashboard.add_widgets(*row_widgets)
        if dlq_widgets:
            dashboard.add_widgets(*dlq_widgets)

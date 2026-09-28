"""Alarms, the operations dashboard, and scheduled checks (SPEC §21.3.3-§21.3.5).

Every alarm in SPEC §21.3.4 goes to one SNS topic whose email subscribers come
from `alert_emails` (config or `-c alert_emails=...`). SNS email subscriptions
must be confirmed from the inbox after the first deploy.

App metrics are EMF in the `ListUploader` namespace with dimensions env and
service (Powertools adds service); no user or lead values are ever dimensions.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from infra.config import EnvConfig
from infra.lambda_code import ARCH, RUNTIME, backend_code
from infra.stacks.api import ApiStack
from infra.stacks.storage import StorageStack, grant_audit_append
from infra.stacks.workflows import WorkflowsStack

NAMESPACE = "ListUploader"
GTE = cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
SERVICE = "list-uploader"


class MonitoringStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: EnvConfig,
        storage: StorageStack,
        workflows: WorkflowsStack,
        api: ApiStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]
        self.cfg = cfg
        self.dims = {"env": cfg.name, "service": SERVICE}

        # CloudWatch can only publish to an encrypted topic through a customer key
        # whose policy lets it.
        key = kms.Key(
            self,
            "AlertsKey",
            alias=f"alias/list-uploader-{cfg.name}-alerts",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        key.grant(
            iam.ServicePrincipal("cloudwatch.amazonaws.com"), "kms:Decrypt", "kms:GenerateDataKey*"
        )
        topic_name = (
            "list-uploader-alerts" if cfg.name == "prod" else f"list-uploader-alerts-{cfg.name}"
        )
        self.topic = sns.Topic(self, "Alerts", topic_name=topic_name, master_key=key)
        self.topic.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                principals=[iam.AnyPrincipal()],
                actions=["sns:Publish"],
                resources=[self.topic.topic_arn],
                conditions={"Bool": {"aws:SecureTransport": "false"}},
            )
        )
        for email in cfg.alert_emails:
            self.topic.add_subscription(subs.EmailSubscription(email))

        # --- scheduled checks ---------------------------------------------------
        self.monitor = self._function(
            "Monitor", "tasks.monitor.handler", storage, Duration.minutes(2)
        )
        storage.jobs.grant_read_data(self.monitor)
        storage.rows.grant_read_data(self.monitor)
        events.Rule(
            self,
            "EveryFiveMinutes",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.monitor, retry_attempts=0)],
        )
        functions: dict[str, lambda_.IFunction] = {
            "Bff": api.bff,
            "ParseTask": api.parse_task,
            "AnalyzeTask": workflows.analyze_task,
            "EnrichTask": workflows.enrich_task,
            "SendTask": workflows.send_task,
            "AuditArchiver": storage.archiver,
            "Monitor": self.monitor,
        }
        self.canary: lambda_.Function | None = None
        if cfg.canary:
            self.canary = self._canary(storage)
            functions["Canary"] = self.canary

        self.alarms = self._alarms(workflows, functions)
        self._dashboard(api, workflows, functions, storage)

    # --- helpers --------------------------------------------------------------

    def _function(
        self, cid: str, handler: str, storage: StorageStack, timeout: Duration
    ) -> lambda_.Function:
        return lambda_.Function(
            self,
            cid,
            runtime=RUNTIME,
            architecture=ARCH,
            handler=handler,
            code=backend_code(),
            timeout=timeout,
            memory_size=512,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "ENV": self.cfg.name,
                "INTEGRATIONS": self.cfg.integrations,
                "SEND_TO_PROD": "true" if self.cfg.send_to_prod else "false",
                "BEDROCK_MODEL_ID": self.cfg.bedrock_model_id,
                "JOBS_TABLE": storage.jobs.table_name,
                "ROWS_TABLE": storage.rows.table_name,
                "CONFIG_TABLE": storage.config_table.table_name,
                "AUDIT_TABLE": storage.audit_events.table_name,
                "AUDIT_RETENTION_DAYS": str(self.cfg.audit_retention_days),
                "UPLOADS_BUCKET": storage.uploads.bucket_name,
                "PROCESSED_BUCKET": storage.processed.bucket_name,
                "ROW_RETENTION_DAYS": str(self.cfg.row_retention_days),
                "RAW_FILE_RETENTION_DAYS": str(self.cfg.raw_file_retention_days),
                "POWERTOOLS_SERVICE_NAME": SERVICE,
                "POWERTOOLS_METRICS_NAMESPACE": NAMESPACE,
            },
            log_group=logs.LogGroup(
                self,
                f"{cid}Logs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )

    def _canary(self, storage: StorageStack) -> lambda_.Function:
        fn = self._function("Canary", "tasks.canary.handler", storage, Duration.minutes(10))
        for table in (storage.jobs, storage.rows, storage.config_table):
            table.grant_read_write_data(fn)
        storage.uploads.grant_put(fn)
        storage.uploads.grant_read(fn)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObjectRetention"], resources=[storage.uploads.arn_for_objects("*")]
            )
        )
        grant_audit_append(fn.role, storage.audit_events)  # type: ignore[arg-type]
        storage.key.grant_encrypt_decrypt(fn)
        events.Rule(
            self,
            "Daily",
            # 13:07 UTC: a quiet time, off the top of the hour.
            schedule=events.Schedule.cron(minute="7", hour="13"),
            targets=[targets.LambdaFunction(fn, retry_attempts=0)],
        )
        return fn

    def _app_metric(
        self, name: str, stat: str = "Sum", period: Duration | None = None
    ) -> cw.Metric:
        return cw.Metric(
            namespace=NAMESPACE,
            metric_name=name,
            dimensions_map=self.dims,
            statistic=stat,
            period=period or Duration.minutes(5),
        )

    def _alarm(
        self,
        cid: str,
        metric: cw.IMetric,
        *,
        threshold: float,
        severity: str,
        what: str,
        comparison: cw.ComparisonOperator = GTE,
        missing: cw.TreatMissingData = cw.TreatMissingData.NOT_BREACHING,
    ) -> cw.Alarm:
        alarm = cw.Alarm(
            self,
            cid,
            alarm_name=f"list-uploader-{self.cfg.name}-{cid}",
            alarm_description=f"[{severity}] {what} See docs/DEPLOY.md, 'Alarms'.",
            metric=metric,
            threshold=threshold,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=comparison,
            treat_missing_data=missing,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(self.topic))
        return alarm

    def _sfn_failures(self, machine: sfn.IStateMachine, period: Duration) -> cw.IMetric:
        return cw.MathExpression(
            expression="failed + timedout + aborted",
            using_metrics={
                "failed": machine.metric_failed(period=period, statistic="Sum"),
                "timedout": machine.metric_timed_out(period=period, statistic="Sum"),
                "aborted": machine.metric_aborted(period=period, statistic="Sum"),
            },
            period=period,
            label="Failed, timed out or aborted executions",
        )

    def _alarms(
        self, workflows: WorkflowsStack, functions: dict[str, lambda_.IFunction]
    ) -> dict[str, cw.Alarm]:
        five = Duration.minutes(5)
        one = Duration.minutes(1)
        a: dict[str, cw.Alarm] = {}
        a["audit-write-failure"] = self._alarm(
            "audit-write-failure",
            self._app_metric("AuditWriteFailures", period=one),
            threshold=1,
            severity="critical",
            what="An audit event couldn't be written, so the action was refused. Sends are "
            "blocked by design until this is fixed.",
        )
        a["send-workflow-failed"] = self._alarm(
            "send-workflow-failed",
            self._sfn_failures(workflows.state_machines["Send"], five),
            threshold=1,
            severity="high",
            what="A SendWorkflow execution failed.",
        )
        a["rows-stuck-sending"] = self._alarm(
            "rows-stuck-sending",
            self._app_metric("StuckSendingRows", "Maximum"),
            threshold=1,
            severity="high",
            what="A row has been posted to Eloqua for over 15 minutes with no answer.",
        )
        a["workato-error-rate"] = self._alarm(
            "workato-error-rate",
            cw.MathExpression(
                expression="IF(calls > 0, 100 * errors / calls, 0)",
                using_metrics={
                    "errors": self._app_metric("WorkatoErrors", period=Duration.minutes(15)),
                    "calls": self._app_metric("WorkatoCalls", period=Duration.minutes(15)),
                },
                period=Duration.minutes(15),
                label="Workato error rate (%)",
            ),
            threshold=5,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            severity="high",
            what="More than 5% of Workato calls failed over 15 minutes.",
        )
        a["gate-rejected-after-ui"] = self._alarm(
            "gate-rejected-after-ui",
            cw.MathExpression(
                expression="ui + workflow",
                using_metrics={
                    "ui": self._app_metric("GateRejectedAfterUiPassed"),
                    "workflow": self._app_metric("GateRejectedInWorkflow"),
                },
                period=five,
                label="Server gate rejections after the UI passed",
            ),
            threshold=1,
            severity="high",
            what="The server-side gate refused a send the browser showed as passing: a bug "
            "or tampering.",
        )
        a["enrichment-batch-errors"] = self._alarm(
            "enrichment-batch-errors",
            self._app_metric("EnrichmentErrorPct", "Maximum"),
            threshold=20,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            severity="medium",
            what="More than 20% of a job's enrichment batches failed.",
        )
        a["bedrock-parse-failures"] = self._alarm(
            "bedrock-parse-failures",
            cw.MathExpression(
                expression="IF(calls > 0, 100 * failed / calls, 0)",
                using_metrics={
                    "failed": self._app_metric("AIParseFailures", period=Duration.hours(1)),
                    "calls": self._app_metric("AIInvocations", period=Duration.hours(1)),
                },
                period=Duration.hours(1),
                label="AI parse failure rate (%)",
            ),
            threshold=20,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            severity="medium",
            what="More than 20% of AI calls returned unusable output over an hour.",
        )
        ids = {name: f"f{i}" for i, name in enumerate(functions)}
        a["lambda-errors"] = self._alarm(
            "lambda-errors",
            cw.MathExpression(
                expression=" + ".join(ids.values()),
                using_metrics={
                    ids[n]: f.metric_errors(period=five, statistic="Sum")
                    for n, f in functions.items()
                },
                period=five,
                label="Lambda errors",
            ),
            threshold=5,
            severity="medium",
            what="Lambda functions raised 5 or more errors in 5 minutes.",
        )
        a["lambda-throttles"] = self._alarm(
            "lambda-throttles",
            cw.MathExpression(
                expression=" + ".join(ids.values()),
                using_metrics={
                    ids[n]: f.metric_throttles(period=five, statistic="Sum")
                    for n, f in functions.items()
                },
                period=five,
                label="Lambda throttles",
            ),
            threshold=1,
            severity="medium",
            what="Lambda invocations were throttled.",
        )
        a["job-stuck-running"] = self._alarm(
            "job-stuck-running",
            self._app_metric("JobsStuckRunning", "Maximum"),
            threshold=1,
            severity="medium",
            what="A job has been parsing, analyzing, enriching or sending for over 60 minutes.",
        )
        if self.cfg.canary:
            a["canary-failed"] = self._alarm(
                "canary-failed",
                self._app_metric("CanarySucceeded", "Minimum", period=Duration.days(1)),
                threshold=1,
                comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
                # No result in a day means it didn't run: that's a failure too.
                missing=cw.TreatMissingData.BREACHING,
                severity="high",
                what="The daily end-to-end canary failed or didn't run.",
            )
        return a

    def _dashboard(
        self,
        api: ApiStack,
        workflows: WorkflowsStack,
        functions: dict[str, lambda_.IFunction],
        storage: StorageStack,
    ) -> None:
        five = Duration.minutes(5)
        http = api.http_api
        dash = cw.Dashboard(
            self, "Operations", dashboard_name=f"list-uploader-{self.cfg.name}-operations"
        )
        dash.add_widgets(
            cw.AlarmStatusWidget(title="Alarms", alarms=list(self.alarms.values()), width=24)
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="API requests and errors",
                left=[
                    http.metric_count(period=five),
                    http.metric_client_error(period=five),
                    http.metric_server_error(period=five),
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="API latency p95 (ms)",
                left=[http.metric_latency(period=five, statistic="p95")],
                width=12,
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Lambda errors",
                left=[f.metric_errors(period=five, label=n) for n, f in functions.items()],
                width=8,
            ),
            cw.GraphWidget(
                title="Lambda throttles",
                left=[f.metric_throttles(period=five, label=n) for n, f in functions.items()],
                width=8,
            ),
            cw.GraphWidget(
                title="Lambda duration p95 (ms)",
                left=[
                    f.metric_duration(period=five, statistic="p95", label=n)
                    for n, f in functions.items()
                ],
                width=8,
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Workflow executions failed, timed out or aborted",
                left=[
                    cw.MathExpression(
                        expression=f"{n}f + {n}t + {n}a",
                        using_metrics={
                            f"{n}f": m.metric_failed(period=five),
                            f"{n}t": m.metric_timed_out(period=five),
                            f"{n}a": m.metric_aborted(period=five),
                        },
                        label=name,
                        period=five,
                    )
                    for name, m in workflows.state_machines.items()
                    for n in [name.lower()]
                ],
                width=12,
            ),
            cw.GraphWidget(
                title="Workato calls, errors and latency",
                left=[self._app_metric("WorkatoCalls"), self._app_metric("WorkatoErrors")],
                right=[self._app_metric("WorkatoLatencyMs", "p95")],
                width=12,
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Send: submitted, failed, stuck",
                left=[
                    self._app_metric("RowsSubmitted"),
                    self._app_metric("RowsSendFailed"),
                    self._app_metric("StuckSendingRows", "Maximum"),
                ],
                width=8,
            ),
            cw.GraphWidget(
                title="AI: calls, parse failures, tokens",
                left=[self._app_metric("AIInvocations"), self._app_metric("AIParseFailures")],
                right=[self._app_metric("AIInputTokens"), self._app_metric("AIOutputTokens")],
                width=8,
            ),
            cw.GraphWidget(
                title="DynamoDB throttled reads and writes",
                left=[
                    cw.Metric(
                        namespace="AWS/DynamoDB",
                        metric_name=metric,
                        dimensions_map={"TableName": table.table_name},
                        statistic="Sum",
                        period=five,
                        label=f"{label} {metric[:-14].lower()}",
                    )
                    for label, table in (
                        ("Jobs", storage.jobs),
                        ("Rows", storage.rows),
                        ("Audit", storage.audit_events),
                    )
                    for metric in ("ReadThrottleEvents", "WriteThrottleEvents")
                ],
                width=8,
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Audit write failures and funnel",
                left=[self._app_metric("AuditWriteFailures")],
                right=[
                    self._app_metric("JobsCreated"),
                    self._app_metric("JobsReachedSend"),
                    self._app_metric("JobsCompleted"),
                ],
                width=8,
            ),
        )

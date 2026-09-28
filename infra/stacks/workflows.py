"""Step Functions for analyze / enrich / send (SPEC §5.2).

AnalyzeWorkflow (P3) and EnrichWorkflow (P4) are real; Send is a placeholder until P5.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from infra.config import EnvConfig
from infra.lambda_code import ARCH, RUNTIME, backend_code
from infra.stacks.storage import StorageStack, grant_audit_append

JUNK_MAX_CONCURRENCY = 4
ENRICH_MAX_CONCURRENCY = 2  # SPEC §5.2


class WorkflowsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: EnvConfig,
        storage: StorageStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]

        self.analyze_task = self._task_function(
            cfg, storage, "AnalyzeTask", "tasks.analyze.handler"
        )
        storage.config_table.grant_read_data(self.analyze_task)
        self.enrich_task = self._task_function(cfg, storage, "EnrichTask", "tasks.enrich.handler")

        self.state_machines: dict[str, sfn.StateMachine] = {
            "Analyze": self._analyze_machine(cfg),
            "Enrich": self._enrich_machine(cfg),
        }
        for name in ("Send",):
            self.state_machines[name] = sfn.StateMachine(
                self,
                f"{name}Workflow",
                state_machine_name=f"list-uploader-{cfg.name}-{name.lower()}",
                definition_body=sfn.DefinitionBody.from_chainable(
                    sfn.Pass(self, f"{name}NotImplemented", comment="Implemented in a later phase")
                ),
                tracing_enabled=True,
                logs=self._log_options(name),
            )

    def _task_function(
        self, cfg: EnvConfig, storage: StorageStack, cid: str, handler: str
    ) -> lambda_.Function:
        fn = lambda_.Function(
            self,
            cid,
            runtime=RUNTIME,
            architecture=ARCH,
            handler=handler,
            code=backend_code(),
            timeout=Duration.minutes(10),
            memory_size=2048,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "ENV": cfg.name,
                "INTEGRATIONS": cfg.integrations,
                "BEDROCK_MODEL_ID": cfg.bedrock_model_id,
                "JOBS_TABLE": storage.jobs.table_name,
                "ROWS_TABLE": storage.rows.table_name,
                "CONFIG_TABLE": storage.config_table.table_name,
                "AUDIT_TABLE": storage.audit_events.table_name,
                "POWERTOOLS_SERVICE_NAME": "list-uploader",
                "POWERTOOLS_METRICS_NAMESPACE": "ListUploader",
            },
            log_group=logs.LogGroup(
                self,
                f"{cid}Logs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        storage.jobs.grant_read_write_data(fn)
        storage.rows.grant_read_write_data(fn)
        grant_audit_append(fn.role, storage.audit_events)  # type: ignore[arg-type]
        storage.key.grant_encrypt_decrypt(fn)
        return fn

    def _log_options(self, name: str) -> sfn.LogOptions:
        return sfn.LogOptions(
            destination=logs.LogGroup(
                self,
                f"{name}Logs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            level=sfn.LogLevel.ERROR,
        )

    def _step(
        self, cid: str, step: str, fn: lambda_.IFunction | None = None, **extra: object
    ) -> tasks.LambdaInvoke:
        task = tasks.LambdaInvoke(
            self,
            cid,
            lambda_function=fn or self.analyze_task,
            payload=sfn.TaskInput.from_object(
                {"step": step, "job_id": sfn.JsonPath.string_at("$.job_id"), **extra}
            ),
            payload_response_only=True,
            retry_on_service_exceptions=True,
        )
        # Transient errors (throttling, 5xx, timeouts): 3 tries, backoff with jitter (SPEC §5.2).
        task.add_retry(
            errors=["States.ALL"],
            max_attempts=3,
            interval=Duration.seconds(2),
            backoff_rate=2,
            jitter_strategy=sfn.JitterType.FULL,
        )
        return task

    def _analyze_machine(self, cfg: EnvConfig) -> sfn.StateMachine:
        fail_step = self._step("MarkFailed", "fail", error=sfn.JsonPath.object_at("$.error"))
        fail_step.next(sfn.Fail(self, "AnalysisFailed"))

        prepare = self._step("Prepare", "prepare")
        prepare.add_catch(fail_step, result_path="$.error")
        prepare_out = sfn.Pass(
            self,
            "KeepBatches",
            parameters={
                "job_id": sfn.JsonPath.string_at("$.job_id"),
                "batches": sfn.JsonPath.list_at("$.batches"),
            },
        )
        junk = sfn.Map(
            self,
            "JunkChecks",
            items_path="$.batches",
            max_concurrency=JUNK_MAX_CONCURRENCY,
            item_selector={
                "job_id": sfn.JsonPath.string_at("$.job_id"),
                "row_ids": sfn.JsonPath.list_at("$$.Map.Item.Value"),
            },
            result_path=sfn.JsonPath.DISCARD,
        )
        junk.item_processor(
            self._step("JunkBatch", "junk_batch", row_ids=sfn.JsonPath.list_at("$.row_ids"))
        )
        junk.add_catch(fail_step, result_path="$.error")
        finalize = self._step("Finalize", "finalize")
        finalize.add_catch(fail_step, result_path="$.error")

        return sfn.StateMachine(
            self,
            "AnalyzeWorkflow",
            state_machine_name=f"list-uploader-{cfg.name}-analyze",
            definition_body=sfn.DefinitionBody.from_chainable(
                prepare.next(prepare_out).next(junk).next(finalize)
            ),
            timeout=Duration.hours(1),
            tracing_enabled=True,
            logs=self._log_options("Analyze"),
        )

    def _enrich_machine(self, cfg: EnvConfig) -> sfn.StateMachine:
        fn = self.enrich_task
        fail_step = self._step(
            "EnrichMarkFailed", "fail", fn, error=sfn.JsonPath.object_at("$.error")
        )
        fail_step.next(sfn.Fail(self, "EnrichmentFailed"))
        prepare = self._step("EnrichPrepare", "prepare", fn)
        prepare.add_catch(fail_step, result_path="$.error")
        batches = sfn.Map(
            self,
            "EnrichBatches",
            items_path="$.batches",
            max_concurrency=ENRICH_MAX_CONCURRENCY,
            item_selector={
                "job_id": sfn.JsonPath.string_at("$.job_id"),
                "row_ids": sfn.JsonPath.list_at("$$.Map.Item.Value"),
            },
            result_path=sfn.JsonPath.DISCARD,
        )
        batches.item_processor(
            self._step("EnrichBatch", "enrich_batch", fn, row_ids=sfn.JsonPath.list_at("$.row_ids"))
        )
        batches.add_catch(fail_step, result_path="$.error")
        finalize = self._step("EnrichFinalize", "finalize", fn)
        finalize.add_catch(fail_step, result_path="$.error")
        return sfn.StateMachine(
            self,
            "EnrichWorkflow",
            state_machine_name=f"list-uploader-{cfg.name}-enrich",
            definition_body=sfn.DefinitionBody.from_chainable(prepare.next(batches).next(finalize)),
            timeout=Duration.hours(1),
            tracing_enabled=True,
            logs=self._log_options("Enrich"),
        )

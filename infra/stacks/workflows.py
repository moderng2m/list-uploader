"""Step Functions for analyze / enrich / send (SPEC §5.2). Placeholders until P3-P5."""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from infra.config import EnvConfig

WORKFLOWS = ("Analyze", "Enrich", "Send")


class WorkflowsStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: EnvConfig, **kwargs: object) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]

        self.state_machines: dict[str, sfn.StateMachine] = {}
        for name in WORKFLOWS:
            log_group = logs.LogGroup(
                self,
                f"{name}Logs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            )
            self.state_machines[name] = sfn.StateMachine(
                self,
                f"{name}Workflow",
                state_machine_name=f"list-uploader-{cfg.name}-{name.lower()}",
                definition_body=sfn.DefinitionBody.from_chainable(
                    sfn.Pass(self, f"{name}NotImplemented", comment="Implemented in a later phase")
                ),
                tracing_enabled=True,
                logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ERROR),
            )

"""Fire every alarm once in dev and check it reaches the alert email (P7 acceptance).

    uv run python -m ops.fire_alarms --env dev            # plan only (default)
    uv run python -m ops.fire_alarms --env dev --execute  # do it

Where it's safe, a real fault goes through the real path:
- app-metric alarms: breaching datapoints are published to the same metric,
  namespace and dimensions the app uses (metric -> alarm -> SNS -> email);
- send-workflow-failed: a SendWorkflow execution with no job ID, which fails in
  its first step;
- lambda-errors: the send task invoked with an unknown step, which raises.
Throttling can't be caused safely, so lambda-throttles is set to ALARM with
SetAlarmState, which still proves the alarm -> SNS -> email path.

Only dev. Nothing here touches jobs, rows or the audit trail.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3

NAMESPACE = "ListUploader"
SERVICE = "list-uploader"


@dataclass(frozen=True)
class Step:
    alarm: str
    how: str  # "metric" | "workflow" | "invoke" | "set_state"
    data: tuple[tuple[str, float], ...] = ()


# Datapoints that breach each app-metric alarm (see infra/stacks/monitoring.py).
PLAN: tuple[Step, ...] = (
    Step("audit-write-failure", "metric", (("AuditWriteFailures", 1),)),
    Step("rows-stuck-sending", "metric", (("StuckSendingRows", 1),)),
    Step("workato-error-rate", "metric", (("WorkatoCalls", 10), ("WorkatoErrors", 5))),
    Step("gate-rejected-after-ui", "metric", (("GateRejectedAfterUiPassed", 1),)),
    Step("enrichment-batch-errors", "metric", (("EnrichmentErrorPct", 50),)),
    Step("bedrock-parse-failures", "metric", (("AIInvocations", 10), ("AIParseFailures", 5))),
    Step("job-stuck-running", "metric", (("JobsStuckRunning", 1),)),
    Step("canary-failed", "metric", (("CanarySucceeded", 0),)),
    Step("send-workflow-failed", "workflow"),
    Step("lambda-errors", "invoke"),
    Step("lambda-throttles", "set_state"),
)


def plan(env: str, existing: set[str]) -> list[tuple[str, Step]]:
    """(alarm name, step) for each alarm that exists in this env."""
    return [
        (f"list-uploader-{env}-{s.alarm}", s)
        for s in PLAN
        if f"list-uploader-{env}-{s.alarm}" in existing
    ]


def execute(
    env: str, steps: list[tuple[str, Step]], session: Any, log: Callable[[str], None]
) -> None:
    cw = session.client("cloudwatch")
    for name, step in steps:
        if step.how == "metric":
            cw.put_metric_data(
                Namespace=NAMESPACE,
                MetricData=[
                    {
                        "MetricName": metric,
                        "Dimensions": [
                            {"Name": "env", "Value": env},
                            {"Name": "service", "Value": SERVICE},
                        ],
                        "Value": value,
                        "Unit": "Percent" if metric.endswith("Pct") else "Count",
                    }
                    for metric, value in step.data
                ],
            )
            log(f"{name}: published {dict(step.data)}")
        elif step.how == "workflow":
            sfn = session.client("stepfunctions")
            arn = next(
                m["stateMachineArn"]
                for m in sfn.list_state_machines()["stateMachines"]
                if m["name"] == f"list-uploader-{env}-send"
            )
            sfn.start_execution(
                stateMachineArn=arn, name=f"fault-injection-{int(time.time())}", input="{}"
            )
            log(f"{name}: started a SendWorkflow execution with no job ID")
        elif step.how == "invoke":
            lam = session.client("lambda")
            fn = next(
                f["FunctionName"]
                for page in lam.get_paginator("list_functions").paginate()
                for f in page["Functions"]
                if f"-{env}-Workflows" in f["FunctionName"] and "SendTask" in f["FunctionName"]
            )
            for _ in range(5):
                lam.invoke(
                    FunctionName=fn,
                    Payload=json.dumps({"step": "fault-injection", "job_id": "none"}).encode(),
                )
            log(f"{name}: invoked {fn} 5 times with an unknown step")
        else:
            cw.set_alarm_state(
                AlarmName=name,
                StateValue="ALARM",
                StateReason="Fault injection test (ops/fire_alarms.py)",
            )
            log(f"{name}: set to ALARM")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", default="dev")
    parser.add_argument("--execute", action="store_true", help="actually fire the alarms")
    args = parser.parse_args()
    if args.env != "dev":
        raise SystemExit("Fault injection only runs in dev.")
    session = boto3.Session()
    names = {
        a["AlarmName"]
        for page in session.client("cloudwatch")
        .get_paginator("describe_alarms")
        .paginate(AlarmNamePrefix=f"list-uploader-{args.env}-")
        for a in page["MetricAlarms"]
    }
    steps = plan(args.env, names)
    for name, step in steps:
        print(f"{'FIRE' if args.execute else 'plan'}  {name}  ({step.how})")
    missing = {f"list-uploader-{args.env}-{s.alarm}" for s in PLAN} - names
    if missing:
        print(f"not deployed: {sorted(missing)}")
    if args.execute:
        execute(args.env, steps, session, print)
        print("Done. Each alarm should email within a few minutes (the hourly and daily ones")
        print("evaluate at the end of their period). Alarms return to OK on their own.")


if __name__ == "__main__":
    main()

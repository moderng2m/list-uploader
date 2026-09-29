from __future__ import annotations

from typing import Any

from infra.tests.test_hardening import SPEC_ALARMS
from ops.fire_alarms import PLAN, execute, plan


class FakeClient:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def __getattr__(self, op: str) -> Any:
        def call(**kwargs: Any) -> Any:
            self.calls.append((op, kwargs))
            if op == "list_state_machines":
                return {
                    "stateMachines": [
                        {"name": "list-uploader-dev-send", "stateMachineArn": "arn:sm"}
                    ]
                }
            return {}

        return call

    def get_paginator(self, op: str) -> Any:
        class P:
            def paginate(self, **_: Any) -> list[dict[str, Any]]:
                return [
                    {"Functions": [{"FunctionName": "ListUploader-dev-Workflows-SendTask12AB"}]}
                ]

        return P()


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def client(self, _: str) -> FakeClient:
        return FakeClient(self.calls)


def test_plan_covers_every_spec_alarm_and_the_canary() -> None:
    assert {s.alarm for s in PLAN} == SPEC_ALARMS | {"canary-failed"}


def test_plan_skips_alarms_that_are_not_deployed() -> None:
    steps = plan("dev", {"list-uploader-dev-audit-write-failure"})
    assert [name for name, _ in steps] == ["list-uploader-dev-audit-write-failure"]


def test_execute_uses_real_paths_where_safe() -> None:
    session = FakeSession()
    names = {f"list-uploader-dev-{s.alarm}" for s in PLAN}
    execute("dev", plan("dev", names), session, lambda _: None)
    ops = [op for op, _ in session.calls]
    assert ops.count("put_metric_data") == 8
    assert ops.count("invoke") == 5
    assert ops.count("start_execution") == 1
    assert [kw["AlarmName"] for op, kw in session.calls if op == "set_alarm_state"] == [
        "list-uploader-dev-lambda-throttles"
    ]
    audit = next(kw for op, kw in session.calls if op == "put_metric_data")
    [datum] = audit["MetricData"]
    assert datum["MetricName"] == "AuditWriteFailures"
    assert {"Name": "env", "Value": "dev"} in datum["Dimensions"]

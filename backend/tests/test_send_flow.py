"""P5 end to end: gate -> confirm -> SendWorkflow (in-process) -> result, retry, download."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shared.audit import AuditWriter
from shared.jobs import JobRepo
from shared.processed_file import safe_cell
from shared.sending import build_payload
from shared.workato_client import FakeWorkatoClient, SendToProdViolation
from tasks.analyze import run_all as run_analysis
from tasks.parse_file import run as run_parse
from tasks.send import prepare, send_batch
from tasks.send import run_all as run_send
from tests.conftest import JOBS_TABLE, PROCESSED_BUCKET, UPLOADS_BUCKET, Env
from tests.helpers import call, http_event

OWNER = "uploader@example.com"
CAMPAIGN = "701000000000001AAA"


def _csv(n: int, *, extra: str = "") -> bytes:
    lines = ["Company,First name,Last Name,Email Address,SFDC Last Campaign ID"]
    lines += [
        f"Co {i},First{'abcdefghij'[i % 10]},Sample,p{i}@co.example,{CAMPAIGN}" for i in range(n)
    ]
    if extra:
        lines.append(extra)
    return ("\n".join(lines) + "\n").encode()


def _analyzed(env: Env, data: bytes) -> str:
    status, body = call(
        http_event("POST", "/jobs", email=OWNER, body={"filename": "leads.csv", "enrich": False})
    )
    assert status == 201
    job_id = str(body["job"]["job_id"])
    env.s3.put_object(Bucket=UPLOADS_BUCKET, Key=body["upload"]["fields"]["key"], Body=data)
    call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
    run_parse(job_id, env.parse_deps())
    _, view = call(http_event("GET", f"/jobs/{job_id}/mapping", email=OWNER))
    columns = [
        {"source_header": c["source_header"], "field_key": c["field_key"]} for c in view["columns"]
    ]
    body = {"columns": columns}
    assert call(http_event("PUT", f"/jobs/{job_id}/mapping", email=OWNER, body=body))[0] == 200
    call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))
    assert run_analysis(job_id, env.analyze_deps()) == "ANALYSIS_REVIEW"
    return job_id


def _gate(job_id: str, email: str = OWNER) -> tuple[int, Any]:
    return call(http_event("GET", f"/jobs/{job_id}/gate", email=email))


def _confirmation(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "rows_to_send": gate["rows_to_send"],
        "by_campaign": [
            {k: c[k] for k in ("campaign_id", "status", "rows")} for c in gate["by_campaign"]
        ],
    }


def _send(job_id: str, confirmation: dict[str, Any] | None = None, **extra: Any) -> tuple[int, Any]:
    if confirmation is None:
        confirmation = _confirmation(_gate(job_id)[1])
    body = {"confirmation": confirmation, "ui_gate_passed": True, **extra}
    return call(http_event("POST", f"/jobs/{job_id}/send", email=OWNER, body=body))


def _job(job_id: str) -> dict[str, Any]:
    return call(http_event("GET", f"/jobs/{job_id}", email=OWNER))[1]  # type: ignore[no-any-return]


def _rows(job_id: str) -> dict[int, dict[str, Any]]:
    _, body = call(http_event("GET", f"/jobs/{job_id}/rows", email=OWNER, query={"limit": "500"}))
    return {r["row_id"]: r for r in body["rows"]}


def _events(env: Env, job_id: str, event_type: str) -> list[dict[str, Any]]:
    items = env.audit_table.scan()["Items"]
    out = [
        {**i, "details": json.loads(i["details"])}
        for i in items
        if i["job_id"] == job_id and i["event_type"] == event_type
    ]
    return sorted(out, key=lambda i: i["sk"])


def _posts(env: Env) -> list[str]:
    return [c[1] for c in env.workato.calls if c[0] == "post_to_eloqua"]


def _ready(env: Env, n: int = 3) -> str:
    job_id = _analyzed(env, _csv(n))
    status, gate = _gate(job_id)
    assert status == 200
    assert gate["passed"], gate["reasons"]
    return job_id


class TestGate:
    def test_clean_file_passes_with_summary(self, app_env: Env) -> None:
        job_id = _ready(app_env, 3)
        _, gate = _gate(job_id)
        assert gate["rows_to_send"] == 3
        assert gate["campaign_count"] == 1
        assert gate["can_send"] is True
        assert gate["send_to_prod"] is False
        [group] = gate["by_campaign"]
        assert group["campaign_id"] == CAMPAIGN
        assert group["rows"] == 3
        assert group["status"]
        event = _events(app_env, job_id, "GATE_EVALUATED")[-1]
        assert event["details"]["where"] == "ui"
        assert event["details"]["passed"] is True

    def test_oq1_interim_rule_company_and_last_name_not_sent(self, app_env: Env) -> None:
        _, gate = _gate(_ready(app_env))
        assert "Company" in gate["not_sent_fields"]
        assert "Last Name" in gate["not_sent_fields"]
        assert "Company" not in gate["sent_fields"]
        assert "Email Address" in gate["sent_fields"]

    def test_blocking_issues_fail_the_gate(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(2, extra=f"Co X,Bo,Sample,not-an-email,{CAMPAIGN}"))
        _, gate = _gate(job_id)
        assert gate["passed"] is False
        assert "BLOCKING_ISSUES" in gate["reason_codes"]
        assert any("still" in r for r in gate["reasons"])

    def test_excluding_the_bad_row_passes(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(2, extra=f"Co X,Bo,Sample,not-an-email,{CAMPAIGN}"))
        body = {"excluded": True, "reason": "bad email"}
        assert call(http_event("PATCH", f"/jobs/{job_id}/rows/4", email=OWNER, body=body))[0] == 200
        _, gate = _gate(job_id)
        assert gate["passed"], gate["reasons"]
        assert gate["rows_to_send"] == 2
        assert gate["excluded"] == 1

    def test_stale_campaigns_then_revalidate(self, app_env: Env) -> None:
        job_id = _ready(app_env)
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        app_env.jobs_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET analysis_context.campaigns_validated_at = :t",
            ExpressionAttributeValues={":t": old},
        )
        _, gate = _gate(job_id)
        assert gate["reason_codes"] == ["CAMPAIGNS_STALE"]
        assert gate["campaigns_stale"] is True

        status, body = call(http_event("POST", f"/jobs/{job_id}/revalidate-campaigns", email=OWNER))
        assert status == 200, body
        assert body["campaigns"] == 1
        assert _gate(job_id)[1]["passed"] is True
        [event] = [
            e
            for e in _events(app_env, job_id, "CAMPAIGN_VALIDATED")
            if e["details"].get("reason") == "pre_send_revalidation"
        ]
        assert event["details"]["found"] == [CAMPAIGN]

    def test_other_user_is_denied(self, app_env: Env) -> None:
        job_id = _ready(app_env)
        assert _gate(job_id, email="someone@example.com")[0] == 403
        body = {"confirmation": {}}
        status, _ = call(
            http_event("POST", f"/jobs/{job_id}/send", email="someone@example.com", body=body)
        )
        assert status == 403


class TestSend:
    def test_happy_path(self, app_env: Env) -> None:
        job_id = _ready(app_env, 3)
        status, body = _send(job_id)
        assert status == 202, body
        assert body["state"] == "SENDING"
        assert app_env.send_requests == [(job_id, False)]
        [confirmed] = _events(app_env, job_id, "SEND_CONFIRMED")
        assert confirmed["details"]["rows_to_send"] == 3
        assert confirmed["details"]["send_to_prod"] is False
        assert "Company" in confirmed["details"]["not_sent_fields"]

        assert run_send(job_id, app_env.send_deps()) == "COMPLETED"
        assert sorted(_posts(app_env)) == [f"{job_id}:{i}" for i in (2, 3, 4)]
        rows = _rows(job_id)
        assert {r["send"]["status"] for r in rows.values()} == {"submitted"}
        submitted = _events(app_env, job_id, "ROW_SUBMITTED")
        assert len(submitted) == 3
        payload = submitted[0]["details"]["payload"]
        assert payload["send_to_prod"] is False
        assert payload["source_system"] == "list-uploader"
        assert payload["campaign_id"] == CAMPAIGN
        assert "company" not in payload and "last_name" not in payload
        assert submitted[0]["details"]["payload_sha256"]
        where = [e["details"]["where"] for e in _events(app_env, job_id, "GATE_EVALUATED")]
        assert where == ["ui", "ui", "server", "workflow"]

        _, result = call(http_event("GET", f"/jobs/{job_id}/result", email=OWNER))
        assert result["state"] == "COMPLETED"
        assert result["submitted"] == 3
        assert result["failed"] == [] and result["unconfirmed"] == []
        assert result["campaigns"][0]["rows"] == 3
        assert result["submitted_by"] == OWNER

    def test_double_click_starts_one_send(self, app_env: Env) -> None:
        job_id = _ready(app_env)
        confirmation = _confirmation(_gate(job_id)[1])
        assert _send(job_id, confirmation)[0] == 202
        status, _ = _send(job_id, confirmation)
        assert status == 409
        assert len(app_env.send_requests) == 1

    def test_two_executions_submit_each_row_once(self, app_env: Env) -> None:
        job_id = _ready(app_env, 30)
        assert _send(job_id)[0] == 202
        deps = app_env.send_deps()
        first = prepare(job_id, deps)["batches"]
        second = prepare(job_id, deps)["batches"]
        assert first == second and [len(b) for b in first] == [25, 5]
        for batch in first + second:
            send_batch(job_id, batch, deps)
        assert run_send(job_id, deps) == "COMPLETED"
        posts = _posts(app_env)
        assert len(posts) == 30 and len(set(posts)) == 30
        assert len(_events(app_env, job_id, "ROW_SUBMITTED")) == 30

    def test_forced_500_on_two_rows_then_retry_only_those(self, app_env: Env) -> None:
        job_id = _ready(app_env, 5)
        app_env.workato.post_status = {f"{job_id}:3": 500, f"{job_id}:5": 500}
        assert _send(job_id)[0] == 202
        assert run_send(job_id, app_env.send_deps()) == "COMPLETED_WITH_ERRORS"
        _, result = call(http_event("GET", f"/jobs/{job_id}/result", email=OWNER))
        assert result["submitted"] == 3
        assert [f["row_id"] for f in result["failed"]] == [3, 5]
        assert len(_events(app_env, job_id, "ROW_SEND_FAILED")) == 2

        app_env.workato.post_status = {}
        app_env.workato.calls.clear()
        status, body = call(http_event("POST", f"/jobs/{job_id}/retry-failed", email=OWNER))
        assert status == 202, body
        assert body["row_ids"] == [3, 5]
        assert app_env.send_requests[-1] == (job_id, True)
        assert run_send(job_id, app_env.send_deps(), only_failed=True) == "COMPLETED"
        assert sorted(_posts(app_env)) == [f"{job_id}:3", f"{job_id}:5"]
        rows = _rows(job_id)
        assert rows[3]["send"]["attempts"] == 2
        assert rows[2]["send"]["attempts"] == 1
        # Nothing left to retry.
        assert call(http_event("POST", f"/jobs/{job_id}/retry-failed", email=OWNER))[0] == 409

    def test_send_to_prod_in_dev_raises_before_any_call(self, app_env: Env) -> None:
        job_id = _ready(app_env)
        assert _send(job_id)[0] == 202
        assert run_send(job_id, app_env.send_deps(send_to_prod=True)) == "FAILED"
        assert _posts(app_env) == []
        assert {r["send"]["status"] for r in _rows(job_id).values()} == {"not_sent"}
        assert _job(job_id)["last_error"]["stage"] == "send"

    def test_client_guard_refuses_prod_payload_in_dev(self) -> None:
        workato = FakeWorkatoClient(env="dev")
        row = {"row_id": 1, "processed": {"email": "ada@acme.example"}}
        with pytest.raises(SendToProdViolation):
            workato.post_to_eloqua(build_payload("j", row, send_to_prod=True))
        assert workato.calls == []

    def test_no_response_leaves_row_unconfirmed(self, app_env: Env) -> None:
        class Flaky(FakeWorkatoClient):
            def _post_to_eloqua(self, payload: dict[str, Any]) -> Any:
                if payload["source_record_id"].endswith(":3"):
                    raise TimeoutError("read timed out")
                return super()._post_to_eloqua(payload)

        job_id = _ready(app_env)
        assert _send(job_id)[0] == 202
        deps = app_env.send_deps(workato=Flaky(env="dev"))
        assert run_send(job_id, deps) == "COMPLETED_WITH_ERRORS"
        row = _rows(job_id)[3]
        assert row["send"]["status"] == "sending"
        _, result = call(http_event("GET", f"/jobs/{job_id}/result", email=OWNER))
        assert [u["row_id"] for u in result["unconfirmed"]] == [3]
        [event] = _events(app_env, job_id, "ROW_SEND_FAILED")
        assert event["details"]["outcome"] == "no_response"
        # Never auto-retried: it isn't a failed row.
        assert call(http_event("POST", f"/jobs/{job_id}/retry-failed", email=OWNER))[0] == 409

    def test_confirmation_must_match(self, app_env: Env) -> None:
        job_id = _ready(app_env, 3)
        stale = _confirmation(_gate(job_id)[1])
        stale["rows_to_send"] = 2
        status, body = _send(job_id, stale)
        assert status == 409
        assert "changed" in body["message"].lower()
        assert _job(job_id)["state"] == "ANALYSIS_REVIEW"

    def test_gate_failure_blocks_send(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(1, extra=f"Co X,Bo,Sample,not-an-email,{CAMPAIGN}"))
        status, body = _send(job_id, {"rows_to_send": 2, "by_campaign": []})
        assert status == 409
        assert body["reasons"]
        assert app_env.send_requests == []
        gates = _events(app_env, job_id, "GATE_EVALUATED")
        [event] = [e for e in gates if e["details"]["where"] == "server"]
        assert event["details"]["passed"] is False

    def test_audit_failure_blocks_send(self, app_env: Env) -> None:
        job_id = _ready(app_env)
        confirmation = _confirmation(_gate(job_id)[1])
        broken = AuditWriter("missing-table", env="dev", app_version="t")
        from bff.app import set_deps

        set_deps(app_env.bff_deps(audit=broken, jobs=JobRepo(broken, JOBS_TABLE)))
        assert _send(job_id, confirmation)[0] == 503
        set_deps(app_env.bff_deps())
        assert _job(job_id)["state"] == "ANALYSIS_REVIEW"
        assert app_env.send_requests == []


class TestDownload:
    def test_processed_csv(self, app_env: Env) -> None:
        job_id = _ready(app_env, 2)
        assert _send(job_id)[0] == 202
        run_send(job_id, app_env.send_deps())
        status, body = call(http_event("GET", f"/jobs/{job_id}/download", email=OWNER))
        assert status == 200, body
        assert body["filename"].startswith("leads-processed-")
        [event] = _events(app_env, job_id, "PROCESSED_FILE_DOWNLOADED")
        key = event["details"]["s3_key"]
        data = app_env.s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)["Body"].read()
        assert data.startswith(b"\xef\xbb\xbf")
        reader = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        assert len(reader) == 2
        assert reader[0]["Email Address"] == "p0@co.example"
        assert reader[0]["processed_email"] == "p0@co.example"
        assert reader[0]["_send_status"] == "submitted"
        assert reader[0]["_row_status"] == "ready"

    def test_not_before_analysis(self, app_env: Env) -> None:
        _, body = call(
            http_event("POST", "/jobs", email=OWNER, body={"filename": "a.csv", "enrich": False})
        )
        job_id = body["job"]["job_id"]
        assert call(http_event("GET", f"/jobs/{job_id}/download", email=OWNER))[0] == 409


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=HYPERLINK(1)", "'=HYPERLINK(1)"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("+cmd", "'+cmd"),
        ("-2+3", "'-2+3"),
        ("-5", "-5"),
        ("+1 555 010 0100", "+1 555 010 0100"),
        ("Ada", "Ada"),
        ("", ""),
    ],
)
def test_formula_injection_guard(value: str, expected: str) -> None:
    assert safe_cell(value) == expected

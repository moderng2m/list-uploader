"""P4 end to end: analyze -> enrich (workflow in-process) -> review decisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shared.workato_client import FakeWorkatoClient
from tasks.analyze import run_all as run_analysis
from tasks.enrich import run_all as run_enrich
from tasks.parse_file import run as run_parse
from tests.conftest import UPLOADS_BUCKET, Env
from tests.helpers import call, http_event

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"
OWNER = "uploader@example.com"


def _analyzed(env: Env, data: bytes, *, filename: str = "leads.csv", enrich: bool = True) -> str:
    status, body = call(
        http_event("POST", "/jobs", email=OWNER, body={"filename": filename, "enrich": enrich})
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
    assert (
        call(http_event("PUT", f"/jobs/{job_id}/mapping", email=OWNER, body={"columns": columns}))[
            0
        ]
        == 200
    )
    call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))
    assert run_analysis(job_id, env.analyze_deps()) == "ANALYSIS_REVIEW"
    return job_id


def _enriched(env: Env, data: bytes | None = None, **deps: Any) -> str:
    job_id = _analyzed(env, data or (FIXTURES / "enrichment_demo.csv").read_bytes())
    status, _ = call(http_event("POST", f"/jobs/{job_id}/enrich", email=OWNER))
    assert status == 202
    assert env.enrichment_requests[-1] == job_id
    assert run_enrich(job_id, env.enrich_deps(**deps)) == "ENRICHMENT_REVIEW"
    return job_id


def _rows(job_id: str) -> dict[int, dict[str, Any]]:
    _, body = call(http_event("GET", f"/jobs/{job_id}/rows", email=OWNER, query={"limit": "500"}))
    return {r["row_id"]: r for r in body["rows"]}


def _codes(row: dict[str, Any]) -> set[str]:
    return {i["code"] for i in row["issues"]}


def _events(env: Env, job_id: str, event_type: str) -> list[dict[str, Any]]:
    items = env.audit_table.scan()["Items"]
    out = [i for i in items if i["job_id"] == job_id and i["event_type"] == event_type]
    return sorted(out, key=lambda i: i["sk"])


def _enrich_calls(env: Env) -> list[Any]:
    return [c for c in env.workato.calls if c[0] == "enrich_contacts"]


def _csv(n: int, *, extra: str = "") -> bytes:
    lines = ["Company,First name,Last Name,Email Address,SFDC Last Campaign ID"]
    lines += [
        f"Co {i},First{'abcdefghij'[i % 10]},Sample,p{i}@co.example,701000000000001AAA"
        for i in range(n)
    ]
    if extra:
        lines.append(extra)
    return ("\n".join(lines) + "\n").encode()


class TestAcceptance:
    def test_60_rows_make_exactly_3_calls(self, app_env: Env) -> None:
        _enriched(app_env, _csv(60))
        calls = _enrich_calls(app_env)
        assert len(calls) == 3
        assert [len(c[1]) for c in calls] == [25, 25, 10]

    def test_pending_row_becomes_ready_when_company_is_filled(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, (FIXTURES / "enrichment_demo.csv").read_bytes())
        assert _rows(job_id)[3]["status"] == "pending_enrichment"
        call(http_event("POST", f"/jobs/{job_id}/enrich", email=OWNER))
        run_enrich(job_id, app_env.enrich_deps())
        kay = _rows(job_id)[3]
        assert kay["processed"]["company"] == "Pied Piper Demo"
        assert kay["provenance"]["company"] == "enrichment:zoominfo"
        assert kay["status"] == "ready", kay["issues"]

    def test_non_blank_title_is_never_overwritten(self, app_env: Env) -> None:
        def everyone_is_a_ceo(contacts: list[dict[str, str]]) -> dict[str, Any]:
            return {
                "best_choices": [
                    {
                        "source_record_id": c["source_record_id"],
                        "match_status": "high_confidence",
                        "accept_enrichment": True,
                        "zi_best_job_title": "Chief Everything Officer",
                        "zi_best_company_name": "Other Co",
                    }
                    for c in contacts
                ]
            }

        workato = FakeWorkatoClient(enrich_handler=everyone_is_a_ceo)
        from shared.enrichment import ZoomInfoProvider

        job_id = _enriched(app_env, provider=ZoomInfoProvider(workato, sleep=lambda _: None))
        rows = _rows(job_id)
        assert rows[4]["processed"]["title"] == "Engineer"  # had a title
        assert rows[6]["processed"]["title"] == "Buyer"
        assert rows[2]["processed"]["title"] == "Chief Everything Officer"  # was blank
        assert rows[2]["processed"]["company"] == "Acme Demo Co"  # never overwritten

    def test_whole_batch_error(self, app_env: Env) -> None:
        job_id = _enriched(
            app_env, _csv(3, extra="Down Co,Otto,Sample,outage@down.example,701000000000001AAA")
        )
        rows = _rows(job_id)
        assert len(_enrich_calls(app_env)) == 3  # one batch, 3 tries
        job = app_env.jobs.get(job_id)
        assert job is not None
        assert job["enrichment_summary"]["errors"] == 4
        assert any("couldn't be enriched (service error)" in n for n in job["analysis_notes"])
        assert all(r["status"] == "ready" for r in rows.values())  # they carry on unenriched


class TestResults:
    def test_every_outcome(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        rows = _rows(job_id)
        ada, linus, nia, art, rex = rows[2], rows[4], rows[5], rows[6], rows[7]
        assert ada["processed"]["title"] == "VP Marketing"
        assert "phone" not in ada["processed"]  # do-not-call
        assert ada["processed"]["mobile_phone"] == "+15550100151"  # normalized
        assert "LINKEDIN_MULTIPLE_PROFILES" in _codes(ada)
        assert ada["status"] == "warning"
        assert "ENRICHMENT_REVIEW" in _codes(linus)
        assert linus["processed"]["company"] == "Hooli Example"  # nothing applied yet
        assert nia["status"] == "ready"
        assert "EMAIL_ROLE_BASED" in _codes(art)
        missing = next(i for i in rex["issues"] if i["code"] == "REQUIRED_MISSING")
        assert missing["field"] == "company" and not missing.get("pending")
        assert rex["status"] == "blocked"

    def test_summary_and_review_view(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        status, view = call(http_event("GET", f"/jobs/{job_id}/enrichment", email=OWNER))
        assert status == 200
        assert (
            view["sent"],
            view["accepted"],
            view["needs_review"],
            view["no_match"],
            view["errors"],
        ) == (6, 2, 1, 3, 0)
        assert view["fields_filled"]["Company"] == 1
        assert view["fields_filled"]["Title"] == 2
        assert view["linkedin_found"] == 2
        (item,) = view["review"]
        assert item["row_id"] == 4
        assert item["candidate"]["company"] == "Hooli XYZ Demo"
        assert "ZoomInfo shows a different current employer." in item["conflicts"]
        assert item["decision"] is None
        assert any(
            f["field"] == "Company" and f["after"] == "Pied Piper Demo" for f in view["filled"]
        )

    def test_audit_trail(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        requested = _events(app_env, job_id, "ENRICHMENT_REQUESTED")
        assert len(requested) == 1
        assert json.loads(requested[0]["details"])["row_ids"] == [2, 3, 4, 5, 6, 7]
        results = {int(e["row_id"]): e for e in _events(app_env, job_id, "ENRICHMENT_RESULT")}
        assert set(results) == {2, 3, 4, 5, 6, 7}
        kay = results[3]
        assert json.loads(kay["after"])["company"] == "Pied Piper Demo"
        details = json.loads(kay["details"])
        assert details["status"] == "accepted" and "company" in details["fields_filled"]
        assert json.loads(results[5]["details"])["status"] == "no_match"
        for e in results.values():
            assert "email" not in json.loads(e["details"])["fields_filled"]


class TestDecisions:
    def _decide(self, job_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        return call(
            http_event("POST", f"/jobs/{job_id}/enrichment-decisions", email=OWNER, body=body)
        )

    def test_apply_fills_blanks_only(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        status, body = self._decide(job_id, {"decisions": [{"row_id": 4, "decision": "apply"}]})
        assert status == 200 and body["decided_row_ids"] == [4]
        linus = _rows(job_id)[4]
        assert linus["processed"]["linkedin_url"] == "linkedin.com/in/linus-sample-demo"
        assert linus["processed"]["company"] == "Hooli Example"  # not overwritten
        assert linus["processed"]["title"] == "Engineer"
        assert "ENRICHMENT_REVIEW" not in _codes(linus)
        decision = _events(app_env, job_id, "ENRICHMENT_DECISION")
        assert json.loads(decision[0]["details"])["decision"] == "apply"

    def test_skip_all(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        status, body = self._decide(job_id, {"skip_all": True})
        assert status == 200 and body["decided_row_ids"] == [4]
        linus = _rows(job_id)[4]
        assert "linkedin_url" not in linus["processed"]
        assert "ENRICHMENT_REVIEW" not in _codes(linus)
        _, view = call(http_event("GET", f"/jobs/{job_id}/enrichment", email=OWNER))
        assert view["review"][0]["decision"] == "skip"

    @pytest.mark.parametrize(
        "body",
        [
            {"decisions": [{"row_id": 2, "decision": "apply"}]},  # not a review row
            {"decisions": [{"row_id": 4, "decision": "maybe"}]},
            {"decisions": []},
        ],
    )
    def test_bad_decisions(self, app_env: Env, body: dict[str, Any]) -> None:
        assert self._decide(_enriched(app_env), body)[0] == 400

    def test_rows_can_still_be_fixed_after_enrichment(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        status, body = call(
            http_event(
                "PATCH",
                f"/jobs/{job_id}/rows/7",
                email=OWNER,
                body={"processed": {"company": "Rex Demo Co"}},
            )
        )
        assert status == 200
        assert body["row"]["status"] == "ready"


class TestLifecycle:
    def test_enrich_needs_the_flag(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, (FIXTURES / "enrichment_demo.csv").read_bytes(), enrich=False)
        status, body = call(http_event("POST", f"/jobs/{job_id}/enrich", email=OWNER))
        assert status == 409 and "wasn't turned on" in body["message"]

    def test_enrich_only_from_analysis_review(self, app_env: Env) -> None:
        job_id = _enriched(app_env)
        assert call(http_event("POST", f"/jobs/{job_id}/enrich", email=OWNER))[0] == 409

    def test_failure_then_retry(self, app_env: Env) -> None:
        class Crashing:
            name = "zoominfo"
            max_batch = 25

            def enrich(self, rows: Any, *, job_id: str) -> Any:
                raise RuntimeError("bug")

        job_id = _analyzed(app_env, (FIXTURES / "enrichment_demo.csv").read_bytes())
        call(http_event("POST", f"/jobs/{job_id}/enrich", email=OWNER))
        assert run_enrich(job_id, app_env.enrich_deps(provider=Crashing())) == "FAILED"
        job = app_env.jobs.get(job_id)
        assert job is not None and job["last_error"]["stage"] == "enrichment"
        assert call(http_event("POST", f"/jobs/{job_id}/enrich", email=OWNER))[0] == 202
        assert run_enrich(job_id, app_env.enrich_deps()) == "ENRICHMENT_REVIEW"

    def test_other_user_is_denied(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, (FIXTURES / "enrichment_demo.csv").read_bytes())
        status, _ = call(http_event("POST", f"/jobs/{job_id}/enrich", email="x@example.com"))
        assert status == 403

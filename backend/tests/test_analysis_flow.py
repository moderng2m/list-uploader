"""P3 end to end: confirm mapping -> analyze (workflow in-process) -> review, edit, bulk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shared.audit import AuditWriter
from shared.jobs import JobRepo
from tasks.analyze import run_all
from tasks.parse_file import run as run_parse
from tests.conftest import JOBS_TABLE, UPLOADS_BUCKET, Env
from tests.helpers import call, http_event

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"
OWNER = "uploader@example.com"
EVENTS_ID = "701000000000001AAA"
WEBINAR_ID = "701000000000002AAA"

LEAD_SOURCE_AI = json.dumps(
    {"items": [{"input": "Webcast", "match": "Marketing: Webinar", "confidence": 0.8}]}
)
JUNK_AI = json.dumps(
    {
        "items": [
            {"row_id": 6, "field": "company", "reason_code": "keyboard_mash",
             "confidence": 0.95, "explanation": "Keyboard mash."},
        ]
    }
)  # fmt: skip


def _analyzed(env: Env, *, ai: tuple[str, ...] = (LEAD_SOURCE_AI, JUNK_AI), enrich: bool = True,
              fixture: str = "analysis_demo.csv") -> str:  # fmt: skip
    status, body = call(
        http_event("POST", "/jobs", email=OWNER, body={"filename": fixture, "enrich": enrich})
    )
    assert status == 201
    job_id = str(body["job"]["job_id"])
    env.s3.put_object(Bucket=UPLOADS_BUCKET, Key=body["upload"]["fields"]["key"],
                      Body=(FIXTURES / fixture).read_bytes())  # fmt: skip
    call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
    run_parse(job_id, env.parse_deps())
    _, view = call(http_event("GET", f"/jobs/{job_id}/mapping", email=OWNER))
    choices = [{"source_header": c["source_header"], "field_key": c["field_key"]}
               for c in view["columns"]]  # fmt: skip
    status, _ = call(http_event("PUT", f"/jobs/{job_id}/mapping", email=OWNER,
                                body={"columns": choices}))  # fmt: skip
    assert status == 200
    status, _ = call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))
    assert status == 202
    assert env.analysis_requests[-1] == job_id
    env.bedrock.queue(*ai)
    run_all(job_id, env.analyze_deps())
    return job_id


def _rows(job_id: str, **query: str) -> dict[int, dict[str, Any]]:
    status, body = call(http_event("GET", f"/jobs/{job_id}/rows", email=OWNER,
                                   query={"limit": "500", **query}))  # fmt: skip
    assert status == 200, body
    return {r["row_id"]: r for r in body["rows"]}


def _codes(row: dict[str, Any]) -> set[str]:
    return {i["code"] for i in row["issues"]}


def _issue(row: dict[str, Any], code: str) -> dict[str, Any]:
    return next(i for i in row["issues"] if i["code"] == code)


def _events(env: Env, job_id: str, event_type: str) -> list[dict[str, Any]]:
    items = env.audit_table.scan()["Items"]
    out = [i for i in items if i["job_id"] == job_id and i["event_type"] == event_type]
    return sorted(out, key=lambda i: i["sk"])


def _patch(job_id: str, row_id: int, body: dict[str, Any], email: str = OWNER) -> tuple[int, Any]:
    return call(http_event("PATCH", f"/jobs/{job_id}/rows/{row_id}", email=email, body=body))


def _bulk(job_id: str, action: str, **params: Any) -> tuple[int, Any]:
    return call(http_event("POST", f"/jobs/{job_id}/bulk-actions", email=OWNER,
                           body={"action": action, "params": params}))  # fmt: skip


class TestAcceptance:
    def test_campaign_id_variants(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        rows = _rows(job_id)
        # 15-char -> 18-char, valid.
        assert rows[2]["processed"]["campaign_id"] == EVENTS_ID
        assert "CAMPAIGN_ID_FORMAT" not in _codes(rows[2])
        # Bad checksum.
        bad = _issue(rows[3], "CAMPAIGN_ID_FORMAT")
        assert "701000000000001AAB looks mistyped" in bad["message"]
        # Valid format, not in Salesforce.
        missing = _issue(rows[4], "CAMPAIGN_NOT_FOUND")
        assert missing["message"].startswith(
            "Campaign ID 701000000000009AAA wasn't found in Salesforce."
        )
        assert rows[3]["status"] == rows[4]["status"] == "blocked"

    def test_blank_status_uses_campaign_default(self, app_env: Env) -> None:
        row = _rows(_analyzed(app_env))[2]
        assert row["processed"]["campaign_status"] == "Registered"
        assert row["provenance"]["campaign_status"] == "derived:sfdc_default_status"
        assert "STATUS_DEFAULTED" in _codes(row)

    def test_events_auto_corrected(self, app_env: Env) -> None:
        row = _rows(_analyzed(app_env))[2]
        assert row["source"]["lead_source"] == "Events"
        assert row["processed"]["lead_source"] == "Marketing: Events"
        assert _issue(row, "LEAD_SOURCE_AUTO_CORRECTED")["severity"] == "info"
        assert row["status"] == "ready"

    def test_ai_failure_never_blocks(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, ai=("junk", "junk", "junk", "junk"))
        job = app_env.jobs.get(job_id)
        assert job is not None and job["state"] == "ANALYSIS_REVIEW"
        rows = _rows(job_id)
        assert not any(i["source"] == "ai" for r in rows.values() for i in r["issues"])
        _, analysis = call(http_event("GET", f"/jobs/{job_id}/analysis", email=OWNER))
        assert any("junk checks weren't available" in n for n in analysis["notes"])
        # Without AI, 'Webcast' can't be resolved: blocked by the rule, not by the AI failure.
        assert _issue(rows[7], "LEAD_SOURCE_INVALID")["source"] == "rule"


class TestResults:
    def test_every_row(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        rows = _rows(job_id)
        status = {rid: r["status"] for rid, r in rows.items()}
        assert status == {
            2: "ready", 3: "blocked", 4: "blocked", 5: "blocked",
            6: "blocked", 7: "blocked", 8: "pending_enrichment", 9: "warning",
        }  # fmt: skip
        assert _issue(rows[5], "DUPLICATE_IN_FILE")["suggestion"] == {"first_row_id": 2}
        junk = _issue(rows[6], "VALUE_JUNK")
        assert junk["field"] == "company" and junk["source"] == "ai"
        suggested = _issue(rows[7], "LEAD_SOURCE_SUGGESTED")
        assert suggested["suggestion"] == {"value": "Marketing: Webinar", "confidence": 0.8}
        assert _issue(rows[7], "STATUS_INVALID")["suggestion"]["options"][0] == "Attended"
        assert {"CAMPAIGN_INACTIVE", "EMAIL_ROLE_BASED", "PHONE_INVALID"} <= _codes(rows[9])
        assert rows[2]["processed"]["postal_code"] == "02134"
        assert rows[2]["processed"]["phone"] == "+15550100100"

    def test_job_summary_and_analysis_view(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        job = app_env.jobs.get(job_id)
        assert job is not None
        assert job["summary"] == {
            "rows_total": 8, "rows_ready": 1, "rows_warning": 1, "rows_blocked": 5,
            "rows_excluded": 0, "rows_pending_enrichment": 1,
        }  # fmt: skip
        assert job["analysis_context"]["normalizer_version"].startswith("stand-in")
        _, view = call(http_event("GET", f"/jobs/{job_id}/analysis", email=OWNER))
        groups = {g["code"]: g for g in view["issue_groups"]}
        assert groups["DUPLICATE_IN_FILE"]["bulk_action"] == "exclude_duplicates"
        assert groups["VALUE_JUNK"]["bulk_action"] == "exclude_junk"
        assert groups["STATUS_INVALID"]["bulk_action"] == "set_status"
        assert view["issue_groups"][0]["severity"] == "blocking"
        campaigns = {c["id"]: c for c in view["campaigns"]}
        assert campaigns[EVENTS_ID]["row_count"] == 4  # rows 2, 5, 6, 8 (row 3 has a bad ID)
        assert campaigns["701000000000009AAA"]["found"] is False
        assert campaigns["701000000000003AAA"]["is_active"] is False
        # Rows eligible for enrichment: ready, warning, pending (not blocked for other reasons).
        assert view["enrichment_lookup_count"] == 3

    def test_row_filters(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        assert set(_rows(job_id, issue_code="CAMPAIGN_NOT_FOUND")) == {4}
        assert set(_rows(job_id, status="blocked")) == {3, 4, 5, 6, 7}
        assert set(_rows(job_id, campaign_id=WEBINAR_ID)) == {7}

    def test_audit_trail(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        started = _events(app_env, job_id, "ANALYSIS_STARTED")[0]
        snapshot = json.loads(started["details"])["snapshot"]
        assert snapshot["normalizer_version"].startswith("stand-in")
        assert snapshot["thresholds"]["junk_block_threshold"] == 0.9
        assert "Marketing: Events" in snapshot["lead_sources"]
        validated = json.loads(_events(app_env, job_id, "CAMPAIGN_VALIDATED")[0]["details"])
        assert validated["not_found"] == ["701000000000009AAA"]
        assert validated["inactive"] == ["701000000000003AAA"]
        purposes = [json.loads(e["details"])["purpose"]
                    for e in _events(app_env, job_id, "AI_INVOCATION")]  # fmt: skip
        assert purposes.count("lead_source_matching") == 1
        assert purposes.count("junk_detection") == 1
        corrected = [e for e in _events(app_env, job_id, "VALUE_AUTO_CORRECTED")
                     if int(e["row_id"]) == 2]  # fmt: skip
        assert json.loads(corrected[0]["after"]) == {"lead_source": "Marketing: Events"}
        derived = [e for e in _events(app_env, job_id, "VALUE_DERIVED") if int(e["row_id"]) == 2]
        assert {"campaign_status", "campaign_name", "list_name"} <= set(
            json.loads(derived[0]["after"])
        )
        raised = [e for e in _events(app_env, job_id, "ISSUE_RAISED") if int(e["row_id"]) == 4]
        raised_codes = {i["code"] for i in json.loads(raised[0]["details"])["issues"]}
        assert "CAMPAIGN_NOT_FOUND" in raised_codes
        assert all("email_sha256" in e for e in raised)
        # Values never leak into the AI audit records.
        assert "asdf" not in json.dumps(_events(app_env, job_id, "AI_INVOCATION"), default=str)


class TestEdits:
    def test_fixing_a_campaign_id_revalidates(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        status, body = _patch(job_id, 3, {"processed": {"campaign_id": EVENTS_ID}})
        assert status == 200, body
        row = body["row"]
        assert row["status"] == "ready"
        assert row["provenance"]["campaign_id"] == "user_edit"
        assert row["user_edits"][-1]["to"] == EVENTS_ID
        edit = _events(app_env, job_id, "USER_EDIT")[-1]
        assert json.loads(edit["after"]) == {"campaign_id": EVENTS_ID}
        cleared = _events(app_env, job_id, "ISSUE_CLEARED")[-1]
        assert "CAMPAIGN_ID_FORMAT" in {i["code"] for i in json.loads(cleared["details"])["issues"]}
        job = app_env.jobs.get(job_id)
        assert job is not None and job["summary"]["rows_ready"] == 2

    def test_new_campaign_id_is_looked_up(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        before = len(app_env.workato.calls)
        _, body = _patch(job_id, 4, {"processed": {"campaign_id": "701000000000005AAA"}})
        assert len(app_env.workato.calls) == before + 1
        assert "CAMPAIGN_NOT_FOUND" in _codes(body["row"])
        assert len(_events(app_env, job_id, "CAMPAIGN_VALIDATED")) == 2

    def test_excluding_the_first_row_clears_the_later_duplicate(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        status, body = _patch(job_id, 2, {"excluded": True})
        assert status == 200
        assert body["row"]["status"] == "excluded"
        assert body["also_changed"] == [5]
        assert "DUPLICATE_IN_FILE" not in _codes(_rows(job_id)[5])
        assert len(_events(app_env, job_id, "ROW_EXCLUDED")) == 1
        system_clear = [e for e in _events(app_env, job_id, "ISSUE_CLEARED")
                        if e.get("reason") == "duplicate_recheck"]  # fmt: skip
        assert system_clear and system_clear[0]["actor"]["type"] == "system"

    def test_clearing_a_flag(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        _, body = _patch(job_id, 6, {"dismiss": "VALUE_JUNK:company"})
        assert "VALUE_JUNK" not in _codes(body["row"])
        assert body["row"]["status"] == "warning"  # rule-based suspects remain
        _, body = _patch(job_id, 6, {"restore": "VALUE_JUNK:company"})
        assert body["row"]["status"] == "blocked"

    @pytest.mark.parametrize(
        ("body", "status"),
        [
            ({"processed": {"campaign_name": "x"}}, 400),
            ({"processed": {"not_a_field": "x"}}, 400),
            ({"excluded": "yes"}, 400),
            ({}, 400),
        ],
    )
    def test_bad_edits(self, app_env: Env, body: dict[str, Any], status: int) -> None:
        job_id = _analyzed(app_env)
        assert _patch(job_id, 2, body)[0] == status

    def test_unknown_row(self, app_env: Env) -> None:
        assert _patch(_analyzed(app_env), 999, {"excluded": True})[0] == 404

    def test_other_user_is_denied(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        assert _patch(job_id, 2, {"excluded": True}, email="someone@example.com")[0] == 403

    def test_audit_failure_changes_nothing(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        broken = AuditWriter("missing-table", env="dev", app_version="t")
        from bff.app import set_deps

        set_deps(app_env.bff_deps(audit=broken, jobs=JobRepo(broken, JOBS_TABLE)))
        assert _patch(job_id, 2, {"excluded": True})[0] == 503
        set_deps(app_env.bff_deps())
        assert _rows(job_id)[2]["excluded"] is False


class TestBulk:
    def test_exclude_duplicates(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        status, body = _bulk(job_id, "exclude_duplicates")
        assert status == 200
        assert body["affected_row_ids"] == [5]
        assert body["summary"]["rows_excluded"] == 1
        bulk = json.loads(_events(app_env, job_id, "BULK_ACTION")[0]["details"])
        assert bulk == {"action": "exclude_duplicates", "params": {}, "row_ids": [5]}

    def test_accept_suggestions_then_fix_status(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        _, body = _bulk(job_id, "accept_lead_source_suggestions", min_confidence=0.9)
        assert body["affected_row_ids"] == []  # 0.8 is below 0.9
        _, body = _bulk(job_id, "accept_lead_source_suggestions", min_confidence=0.75)
        assert body["affected_row_ids"] == [7]
        assert len(_events(app_env, job_id, "SUGGESTION_ACCEPTED")) == 1
        _, body = _bulk(job_id, "set_status", campaign_id=WEBINAR_ID, value="atended",
                        status="Attended")  # fmt: skip
        assert body["affected_row_ids"] == [7]
        row = _rows(job_id)[7]
        assert row["processed"]["lead_source"] == "Marketing: Webinar"
        assert row["processed"]["campaign_status"] == "Attended"
        assert row["status"] == "ready"

    def test_set_status_must_be_a_campaign_status(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        status, _ = _bulk(job_id, "set_status", campaign_id=WEBINAR_ID, value="atended",
                          status="Made Up")  # fmt: skip
        assert status == 400

    def test_exclude_junk(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        _, body = _bulk(job_id, "exclude_junk")
        assert body["affected_row_ids"] == [6]

    def test_unknown_action(self, app_env: Env) -> None:
        assert _bulk(_analyzed(app_env), "delete_everything")[0] == 400


class TestLifecycle:
    def test_analyze_needs_confirmed_mapping(self, app_env: Env) -> None:
        _, body = call(http_event("POST", "/jobs", email=OWNER, body={"filename": "a.csv"}))
        job_id = body["job"]["job_id"]
        assert call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))[0] == 409

    def test_rerun_keeps_user_edits(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        _patch(job_id, 3, {"processed": {"campaign_id": EVENTS_ID}})
        assert call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))[0] == 202
        assert _patch(job_id, 2, {"excluded": True})[0] == 409  # locked while analyzing
        app_env.bedrock.queue(LEAD_SOURCE_AI, JUNK_AI)
        run_all(job_id, app_env.analyze_deps())
        row = _rows(job_id)[3]
        assert row["processed"]["campaign_id"] == EVENTS_ID and row["status"] == "ready"
        assert len(_events(app_env, job_id, "ANALYSIS_STARTED")) == 2

    def test_workflow_failure_then_retry(self, app_env: Env) -> None:
        class Broken:
            def lookup_campaigns(self, ids: list[str], *, caller_job_id: str) -> list[Any]:
                raise RuntimeError("Workato down")

        _, body = call(http_event("POST", "/jobs", email=OWNER,
                                  body={"filename": "analysis_demo.csv"}))  # fmt: skip
        job_id = body["job"]["job_id"]
        app_env.s3.put_object(Bucket=UPLOADS_BUCKET, Key=body["upload"]["fields"]["key"],
                              Body=(FIXTURES / "analysis_demo.csv").read_bytes())  # fmt: skip
        call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))
        run_parse(job_id, app_env.parse_deps())
        _, view = call(http_event("GET", f"/jobs/{job_id}/mapping", email=OWNER))
        call(http_event("PUT", f"/jobs/{job_id}/mapping", email=OWNER, body={"columns": [
            {"source_header": c["source_header"], "field_key": c["field_key"]}
            for c in view["columns"]]}))  # fmt: skip
        call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))
        assert run_all(job_id, app_env.analyze_deps(workato=Broken())) == "FAILED"
        job = app_env.jobs.get(job_id)
        assert job is not None and "try running the analysis again" in job["last_error"]["message"]
        assert call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))[0] == 202
        assert run_all(job_id, app_env.analyze_deps()) == "ANALYSIS_REVIEW"
        job = app_env.jobs.get(job_id)
        assert job is not None and "last_error" not in job


class TestRemapAfterAnalysis:
    """The mapping stays editable until enrichment starts; a change re-runs analysis."""

    def _mapping(self, job_id: str) -> dict[str, Any]:
        return call(http_event("GET", f"/jobs/{job_id}/mapping", email=OWNER))[1]  # type: ignore[no-any-return]

    def _put(self, job_id: str, columns: list[dict[str, Any]]) -> tuple[int, Any]:
        body = {"columns": columns}
        return call(http_event("PUT", f"/jobs/{job_id}/mapping", email=OWNER, body=body))

    def test_change_saves_and_rerun_uses_it_keeping_edits(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        _patch(job_id, 3, {"processed": {"campaign_id": EVENTS_ID}})
        assert _rows(job_id)[2]["processed"].get("phone")
        view = self._mapping(job_id)
        assert view["editable"] is True and view["confirmed"] is True
        columns = [
            {"source_header": c["source_header"],
             "field_key": None if c["source_header"] == "Business Phone" else c["field_key"]}
            for c in view["columns"]
        ]  # fmt: skip
        suggestions_before = len(_events(app_env, job_id, "SUGGESTION_ACCEPTED")) + len(
            _events(app_env, job_id, "SUGGESTION_REJECTED")
        )
        status, saved = self._put(job_id, columns)
        assert status == 200, saved
        assert saved["analysis_needed"] is True
        confirmed = _events(app_env, job_id, "MAPPING_CONFIRMED")
        assert len(confirmed) == 2
        assert json.loads(confirmed[-1]["details"])["changed_vs_previous"] == ["Business Phone"]
        # AI suggestions were decided at the first confirmation, not again.
        assert suggestions_before == len(_events(app_env, job_id, "SUGGESTION_ACCEPTED")) + len(
            _events(app_env, job_id, "SUGGESTION_REJECTED")
        )

        assert call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))[0] == 202
        app_env.bedrock.queue(LEAD_SOURCE_AI, JUNK_AI)
        run_all(job_id, app_env.analyze_deps())
        rows = _rows(job_id)
        assert "phone" not in rows[2]["processed"]
        assert rows[3]["processed"]["campaign_id"] == EVENTS_ID  # the user's edit survives

    def test_saving_the_same_mapping_changes_nothing(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        view = self._mapping(job_id)
        columns = [{"source_header": c["source_header"], "field_key": c["field_key"]}
                   for c in view["columns"]]  # fmt: skip
        status, saved = self._put(job_id, columns)
        assert status == 200
        assert saved["analysis_needed"] is False
        assert len(_events(app_env, job_id, "MAPPING_CONFIRMED")) == 1

    def test_locked_during_analysis(self, app_env: Env) -> None:
        job_id = _analyzed(app_env)
        columns = [{"source_header": c["source_header"], "field_key": c["field_key"]}
                   for c in self._mapping(job_id)["columns"]]  # fmt: skip
        assert call(http_event("POST", f"/jobs/{job_id}/analyze", email=OWNER))[0] == 202
        assert self._put(job_id, columns)[0] == 409

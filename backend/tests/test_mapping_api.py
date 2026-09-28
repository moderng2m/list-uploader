"""P2 through the API: parse -> GET mapping -> PUT (confirm)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tasks.parse_file import run
from tests.conftest import UPLOADS_BUCKET, Env
from tests.helpers import call, http_event
from tests.test_mapping import VENDOR_AI

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"
OWNER = "uploader@example.com"


def _uploaded(env: Env, fixture: str) -> str:
    status, body = call(http_event("POST", "/jobs", email=OWNER, body={"filename": fixture}))
    assert status == 201
    job_id = body["job"]["job_id"]
    env.s3.put_object(
        Bucket=UPLOADS_BUCKET,
        Key=body["upload"]["fields"]["key"],
        Body=(FIXTURES / fixture).read_bytes(),
    )
    assert call(http_event("POST", f"/jobs/{job_id}/uploaded", email=OWNER))[0] == 202
    run(job_id, env.parse_deps())
    return str(job_id)


def _get(job_id: str, email: str = OWNER) -> tuple[int, Any]:
    return call(http_event("GET", f"/jobs/{job_id}/mapping", email=email))


def _put(job_id: str, columns: list[dict[str, Any]], email: str = OWNER) -> tuple[int, Any]:
    return call(
        http_event("PUT", f"/jobs/{job_id}/mapping", email=email, body={"columns": columns})
    )


def _as_choices(view: dict[str, Any], **overrides: str | None) -> list[dict[str, Any]]:
    choices = {c["source_header"]: c["field_key"] for c in view["columns"]}
    choices.update(overrides)
    return [{"source_header": h, "field_key": k} for h, k in choices.items()]


def _events(env: Env, job_id: str, event_type: str) -> list[dict[str, Any]]:
    items = env.audit_table.scan()["Items"]
    return [
        {**i, "details": json.loads(i.get("details", "null") or "null")}
        for i in items
        if i["job_id"] == job_id and i["event_type"] == event_type
    ]


class TestGet:
    def test_template_suggestion_with_samples_and_catalog(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        status, view = _get(job_id)
        assert status == 200
        assert len(view["columns"]) == 25
        assert all(c["method"] == "exact" for c in view["columns"])
        company = view["columns"][0]
        assert company["source_header"] == "Company"
        assert company["samples"] == ["Acme Demo Co", "Globex Test Inc", "Initech Sample"]
        assert view["editable"] is True and view["confirmed"] is False
        assert view["ai_note"] is None
        must_map = {f["key"] for f in view["catalog"] if f["must_map"]}
        assert must_map == {"company", "first_name", "last_name", "email", "campaign_id"}
        assert app_env.bedrock.prompts == []
        assert _events(app_env, job_id, "AI_INVOCATION") == []
        suggested = _events(app_env, job_id, "MAPPING_SUGGESTED")[0]["details"]
        assert suggested["method_counts"] == {"exact": 25, "alias": 0, "ai": 0, "none": 0}

    def test_vendor_file_ai_suggestions_are_audited(self, app_env: Env) -> None:
        app_env.bedrock.queue(VENDOR_AI)
        job_id = _uploaded(app_env, "vendor_export.xlsx")
        _, view = _get(job_id)
        org = next(c for c in view["columns"] if c["source_header"] == "Org")
        assert (org["field_key"], org["method"], org["confidence"]) == ("company", "ai", 0.91)
        invocation = _events(app_env, job_id, "AI_INVOCATION")[0]["details"]
        assert invocation["purpose"] == "column_mapping"
        assert invocation["prompt_version"] == "mapping-v1"
        assert invocation["outcome"] == "ok"
        # No prompt or response content, and no sample values, in the audit trail.
        assert "Acme" not in json.dumps(invocation)
        suggested = _events(app_env, job_id, "MAPPING_SUGGESTED")[0]["details"]
        assert "ada@acme.example" not in json.dumps(suggested)

    def test_degraded_ai_shows_a_note(self, app_env: Env) -> None:
        app_env.bedrock.queue("junk", "junk")
        job_id = _uploaded(app_env, "vendor_export.xlsx")
        _, view = _get(job_id)
        assert "Automatic suggestions weren't available" in view["ai_note"]
        assert _events(app_env, job_id, "AI_INVOCATION")[0]["details"]["outcome"] == (
            "parse_failed"
        )

    def test_before_parse_is_conflict(self, app_env: Env) -> None:
        _, body = call(http_event("POST", "/jobs", email=OWNER, body={"filename": "a.csv"}))
        assert _get(body["job"]["job_id"])[0] == 409

    def test_other_user_is_denied(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "semicolon.csv")
        assert _get(job_id, email="someone@example.com")[0] == 403


class TestConfirm:
    def test_accepting_the_template_suggestion(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        _, view = _get(job_id)
        status, confirmed = _put(job_id, _as_choices(view))
        assert status == 200
        assert confirmed["confirmed"] is True
        job = app_env.jobs.get(job_id)
        assert job is not None
        assert job["state"] == "MAPPING_REVIEW"  # analysis starts separately (P3)
        assert job["mapping_confirmed"]["changed_vs_suggestion"] == []
        assert job["mapping_confirmed"]["confirmed_by"] == OWNER
        assert len(_events(app_env, job_id, "MAPPING_CONFIRMED")) == 1

    def test_ai_decisions_and_manual_changes(self, app_env: Env) -> None:
        app_env.bedrock.queue(VENDOR_AI)
        job_id = _uploaded(app_env, "vendor_export.xlsx")
        _, view = _get(job_id)
        # Keep Org->company, reject Job Position, map Column H by hand, add a campaign ID.
        choices = _as_choices(
            view, **{"Job Position": None, "Column H": "notes", "Score": "campaign_id"}
        )
        status, confirmed = _put(job_id, choices)
        assert status == 200, confirmed
        methods = {c["source_header"]: c["method"] for c in confirmed["columns"]}
        assert methods["Org"] == "ai"
        assert methods["Column H"] == "manual"
        assert methods["Job Position"] == "none"
        assert methods["E-mail"] == "alias"

        accepted = _events(app_env, job_id, "SUGGESTION_ACCEPTED")
        rejected = _events(app_env, job_id, "SUGGESTION_REJECTED")
        assert {json.loads(e["subject"])["source_header"] for e in accepted} == {
            "Org",
            "Phone (2)",
        }
        assert [json.loads(e["subject"])["source_header"] for e in rejected] == ["Job Position"]
        details = _events(app_env, job_id, "MAPPING_CONFIRMED")[0]["details"]
        assert set(details["changed_vs_suggestion"]) == {"Job Position", "Column H", "Score"}

    def test_reconfirming_replaces_the_mapping(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        _, view = _get(job_id)
        _put(job_id, _as_choices(view))
        status, second = _put(job_id, _as_choices(view, **{"Title": None}))
        assert status == 200
        title = next(c for c in second["columns"] if c["source_header"] == "Title")
        assert title["field_key"] is None
        assert len(_events(app_env, job_id, "MAPPING_CONFIRMED")) == 2

    def test_missing_required_is_rejected_with_the_list(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "vendor_export.xlsx")  # has no campaign ID column
        _, view = _get(job_id)
        status, body = _put(job_id, _as_choices(view))
        assert status == 400
        assert "SFDC Last Campaign ID" in body["message"]
        assert _events(app_env, job_id, "MAPPING_CONFIRMED") == []

    def test_duplicate_field_is_rejected(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        _, view = _get(job_id)
        status, body = _put(job_id, _as_choices(view, **{"Title": "email"}))
        assert status == 400
        assert "both mapped to Email Address" in body["message"]

    def test_bad_body(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        status, _ = call(
            http_event("PUT", f"/jobs/{job_id}/mapping", email=OWNER, body={"columns": "x"})
        )
        assert status == 400

    def test_other_user_cannot_confirm(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        _, view = _get(job_id)
        assert _put(job_id, _as_choices(view), email="someone@example.com")[0] == 403

    def test_locked_after_mapping_review(self, app_env: Env) -> None:
        job_id = _uploaded(app_env, "template_filled.xlsx")
        _, view = _get(job_id)
        from shared.audit import Actor
        from shared.jobs import JobState

        app_env.jobs.transition(
            job_id, JobState.MAPPING_REVIEW, JobState.ANALYZING, actor=Actor.system()
        )
        status, body = _put(job_id, _as_choices(view))
        assert status == 409
        assert "already confirmed" in body["message"]
        assert _get(job_id)[1]["editable"] is False

"""P6: job timeline, row history, admin settings, audit search and export."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest

from shared.audit import GLOBAL_PARTITION, email_sha256
from tasks.analyze import run_all as run_analysis
from tasks.parse_file import run as run_parse
from tasks.send import run_all as run_send
from tests.conftest import PROCESSED_BUCKET, UPLOADS_BUCKET, Env
from tests.helpers import ADMIN, call, http_event
from tests.test_mapping import _ai, _item

OWNER = "uploader@example.com"
OTHER = "someone@example.com"
BOSS = "admin@example.com"
CAMPAIGN = "701000000000001AAA"
WEBINAR = "701000000000002AAA"
HEADER = "Company,First name,Last Name,Email Address,SFDC Last Campaign ID"


def _admin(method: str, path: str, **kw: Any) -> tuple[int, Any]:
    return call(http_event(method, path, email=BOSS, groups=ADMIN, **kw))


def _as(email: str, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    return call(http_event(method, path, email=email, **kw))


def _uploaded(env: Env, data: bytes, *, ai: str | None = None, owner: str = OWNER) -> str:
    status, body = _as(owner, "POST", "/jobs", body={"filename": "leads.csv", "enrich": False})
    assert status == 201
    job_id = str(body["job"]["job_id"])
    env.s3.put_object(Bucket=UPLOADS_BUCKET, Key=body["upload"]["fields"]["key"], Body=data)
    _as(owner, "POST", f"/jobs/{job_id}/uploaded")
    if ai:
        env.bedrock.queue(ai)
    run_parse(job_id, env.parse_deps())
    return job_id


def _analyzed(env: Env, data: bytes, *, ai: str | None = None, owner: str = OWNER) -> str:
    job_id = _uploaded(env, data, ai=ai, owner=owner)
    _, view = _as(owner, "GET", f"/jobs/{job_id}/mapping")
    columns = [
        {"source_header": c["source_header"], "field_key": c["field_key"]} for c in view["columns"]
    ]
    assert _as(owner, "PUT", f"/jobs/{job_id}/mapping", body={"columns": columns})[0] == 200
    assert _as(owner, "POST", f"/jobs/{job_id}/analyze")[0] == 202
    assert run_analysis(job_id, env.analyze_deps()) == "ANALYSIS_REVIEW"
    return job_id


def _sent(env: Env, data: bytes) -> str:
    job_id = _analyzed(env, data)
    _, gate = _as(OWNER, "GET", f"/jobs/{job_id}/gate")
    assert gate["passed"], gate["reasons"]
    confirmation = {
        "rows_to_send": gate["rows_to_send"],
        "by_campaign": [
            {k: c[k] for k in ("campaign_id", "status", "rows")} for c in gate["by_campaign"]
        ],
    }
    body = {"confirmation": confirmation, "ui_gate_passed": True}
    assert _as(OWNER, "POST", f"/jobs/{job_id}/send", body=body)[0] == 202
    assert run_send(job_id, env.send_deps()) == "COMPLETED"
    return job_id


def _csv(*lines: str, header: str = HEADER) -> bytes:
    return ("\n".join([header, *lines]) + "\n").encode()


def _events(env: Env, partition: str, event_type: str) -> list[dict[str, Any]]:
    items = env.audit_table.scan()["Items"]
    out = [
        {**i, **{k: json.loads(i[k]) for k in ("details", "before", "after") if k in i}}
        for i in items
        if i["job_id"] == partition and i["event_type"] == event_type
    ]
    return sorted(out, key=lambda i: i["sk"])


def _rows(job_id: str) -> dict[int, dict[str, Any]]:
    _, body = _as(OWNER, "GET", f"/jobs/{job_id}/rows", query={"limit": "500"})
    return {r["row_id"]: r for r in body["rows"]}


class TestAccessControl:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/admin/config", None),
            ("PUT", "/admin/thresholds", {"version": "seed", "values": {}}),
            ("GET", "/admin/lead-sources", None),
            ("POST", "/admin/lead-sources", {"version": "seed", "value": "X"}),
            ("PATCH", "/admin/lead-sources/ls_seed_0", {"version": "seed", "active": False}),
            ("PUT", "/admin/lead-sources/order", {"version": "seed", "ids": []}),
            ("GET", "/admin/aliases", None),
            ("PUT", "/admin/aliases/title", {"version": "seed", "aliases": []}),
            ("GET", "/admin/ai-mappings", None),
            ("POST", "/admin/aliases/promote", {"version": "seed"}),
            ("GET", "/admin/audit", None),
            ("POST", "/admin/audit/export", {"email": "ada@acme.example"}),
            ("GET", "/jobs", None),  # with ?all=true below
        ],
    )
    def test_non_admin_gets_403_and_is_audited(
        self, app_env: Env, method: str, path: str, body: Any
    ) -> None:
        query = {"all": "true"} if path == "/jobs" else {"email": "ada@acme.example"}
        status, resp = _as(OWNER, method, path, body=body, query=query)
        assert status == 403, resp
        [denied] = _events(app_env, GLOBAL_PARTITION, "ACCESS_DENIED")
        assert denied["actor"]["email"] == OWNER

    def test_other_users_timeline_and_row_history(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        for path in (f"/jobs/{job_id}/timeline", f"/jobs/{job_id}/rows/2/history"):
            assert _as(OTHER, "GET", path)[0] == 403
            assert _admin("GET", path)[0] == 200
            assert _as(OWNER, "GET", path)[0] == 200
        denied = _events(app_env, job_id, "ACCESS_DENIED")
        assert [d["details"]["route"] for d in denied] == [
            "/jobs/{id}/timeline",
            "/jobs/{id}/rows/{row_id}/history",
        ]


class TestLeadSources:
    def test_crud_is_versioned_and_audited(self, app_env: Env) -> None:
        _, start = _admin("GET", "/admin/lead-sources")
        assert start["version"] == "seed"
        values = [i["value"] for i in start["items"]]
        assert "Marketing: Events" in values

        status, added = _admin(
            "POST",
            "/admin/lead-sources",
            body={"version": "seed", "value": "  Partner:  Referral "},
        )
        assert status == 200, added
        assert added["items"][-1]["value"] == "Partner: Referral"
        [event] = _events(app_env, GLOBAL_PARTITION, "ADMIN_CONFIG_CHANGED")
        assert event["actor"]["email"] == BOSS
        assert event["details"]["change"] == "added 'Partner: Referral'"
        assert len(event["after"]["lead_sources"]) == len(event["before"]["lead_sources"]) + 1

        # Someone else's stale version is refused; nothing is written.
        stale = {"version": "seed", "value": "Other"}
        assert _admin("POST", "/admin/lead-sources", body=stale)[0] == 409
        assert len(_events(app_env, GLOBAL_PARTITION, "ADMIN_CONFIG_CHANGED")) == 1

        dup = {"version": added["version"], "value": "marketing: EVENTS"}
        assert _admin("POST", "/admin/lead-sources", body=dup)[0] == 400

        new_id = added["items"][-1]["id"]
        body = {"version": added["version"], "value": "Partner: Referral Program"}
        status, renamed = _admin("PATCH", f"/admin/lead-sources/{new_id}", body=body)
        assert status == 200
        assert renamed["items"][-1]["value"] == "Partner: Referral Program"

        ids = [i["id"] for i in renamed["items"]]
        body = {"version": renamed["version"], "ids": [ids[-1], *ids[:-1]]}
        status, moved = _admin("PUT", "/admin/lead-sources/order", body=body)
        assert status == 200
        assert moved["items"][0]["value"] == "Partner: Referral Program"
        assert [i["order"] for i in moved["items"]] == list(range(len(ids)))

    def test_there_is_no_delete(self, app_env: Env) -> None:
        assert _admin("DELETE", "/admin/lead-sources/ls_seed_0")[0] in (404, 405)

    def test_deactivating_affects_new_jobs_not_historical_ones(self, app_env: Env) -> None:
        header = HEADER + ",Lead Source - Most Recent"
        data = _csv(f"Co,Ada,Example,ada@acme.example,{WEBINAR},Marketing: Webinar", header=header)
        before = _analyzed(app_env, data)
        assert _rows(before)[2]["processed"]["lead_source"] == "Marketing: Webinar"

        _, sources = _admin("GET", "/admin/lead-sources")
        webinar = next(i for i in sources["items"] if i["value"] == "Marketing: Webinar")
        body = {"version": sources["version"], "active": False}
        status, _ = _admin("PATCH", f"/admin/lead-sources/{webinar['id']}", body=body)
        assert status == 200
        [event] = _events(app_env, GLOBAL_PARTITION, "ADMIN_CONFIG_CHANGED")
        assert event["details"]["change"] == "deactivated 'Marketing: Webinar'"

        # A new job no longer accepts it.
        after = _analyzed(app_env, data)
        row = _rows(after)[2]
        assert any(i["field"] == "lead_source" for i in row["issues"]), row["issues"]
        assert row["status"] == "blocked"

        # The earlier job keeps its own snapshot, even when a row is re-evaluated.
        patch = {"processed": {"first_name": "Adah"}}
        assert _as(OWNER, "PATCH", f"/jobs/{before}/rows/2", body=patch)[0] == 200
        row = _rows(before)[2]
        assert row["processed"]["lead_source"] == "Marketing: Webinar"
        assert not any(
            i["field"] == "lead_source" and i["severity"] == "blocking" for i in row["issues"]
        )


class TestThresholds:
    def test_update_validate_and_snapshot(self, app_env: Env) -> None:
        _, config = _admin("GET", "/admin/config")
        assert config["version"] == "seed"
        assert config["thresholds"]["junk_flag_threshold"] == 0.7
        bad = [
            {"junk_flag_threshold": 1.5},
            {"junk_block_threshold": 0.5},  # below the flag threshold
            {"unknown": 0.5},
            {"junk_flag_threshold": True},
        ]
        for values in bad:
            body = {"version": "seed", "values": values}
            assert _admin("PUT", "/admin/thresholds", body=body)[0] == 400, values
        body = {"version": "seed", "values": {"junk_flag_threshold": 0.8}}
        status, updated = _admin("PUT", "/admin/thresholds", body=body)
        assert status == 200
        assert updated["thresholds"]["junk_flag_threshold"] == 0.8
        [event] = _events(app_env, GLOBAL_PARTITION, "ADMIN_CONFIG_CHANGED")
        assert event["before"] == {"thresholds": {"junk_flag_threshold": 0.7}}
        assert event["after"] == {"thresholds": {"junk_flag_threshold": 0.8}}

        job_id = _analyzed(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        [started] = _events(app_env, job_id, "ANALYSIS_STARTED")
        assert started["details"]["snapshot"]["thresholds"]["junk_flag_threshold"] == 0.8


class TestAliases:
    def test_edit_and_validate(self, app_env: Env) -> None:
        _, view = _admin("GET", "/admin/aliases")
        title = next(f for f in view["fields"] if f["key"] == "title")
        body = {"version": view["version"], "aliases": [*title["aliases"], "Role"]}
        status, updated = _admin("PUT", "/admin/aliases/title", body=body)
        assert status == 200
        assert "Role" in next(f for f in updated["fields"] if f["key"] == "title")["aliases"]
        v = updated["version"]
        for aliases in (["work email"], ["Email Address"], [""]):
            body = {"version": v, "aliases": aliases}
            assert _admin("PUT", "/admin/aliases/title", body=body)[0] == 400, aliases
        assert _admin("PUT", "/admin/aliases/nope", body={"version": v, "aliases": []})[0] == 404

        # New uploads map the alias without AI.
        job_id = _uploaded(
            app_env, _csv("Co,Ada,Ex,ada@acme.example,x,VP", header=HEADER + ",Role")
        )
        _, mapping = _as(OWNER, "GET", f"/jobs/{job_id}/mapping")
        role = next(c for c in mapping["columns"] if c["source_header"] == "Role")
        assert (role["field_key"], role["method"]) == ("title", "alias")

    def test_promote_an_ai_match_users_kept(self, app_env: Env) -> None:
        header = "Org,First name,Last Name,Email Address,SFDC Last Campaign ID"
        data = _csv(f"Acme Demo Co,Ada,Example,ada@acme.example,{CAMPAIGN}", header=header)
        _analyzed(app_env, data, ai=_ai(_item("Org", "company", 0.91)))

        _, mappings = _admin("GET", "/admin/ai-mappings")
        [org] = mappings["items"]
        assert (org["source_header"], org["field_key"], org["times"]) == ("Org", "company", 1)

        _, aliases = _admin("GET", "/admin/aliases")
        body = {"version": aliases["version"], "source_header": "Org", "field_key": "title"}
        assert _admin("POST", "/admin/aliases/promote", body=body)[0] == 400  # not what AI did
        body["field_key"] = "company"
        status, promoted = _admin("POST", "/admin/aliases/promote", body=body)
        assert status == 200, promoted
        assert "Org" in next(f for f in promoted["fields"] if f["key"] == "company")["aliases"]
        assert _admin("GET", "/admin/ai-mappings")[1]["items"] == []
        [event] = _events(app_env, GLOBAL_PARTITION, "ADMIN_CONFIG_CHANGED")
        assert event["details"]["change"] == "promoted AI match 'Org' -> company"

        job_id = _uploaded(app_env, data)  # no AI queued
        _, mapping = _as(OWNER, "GET", f"/jobs/{job_id}/mapping")
        org_col = next(c for c in mapping["columns"] if c["source_header"] == "Org")
        assert (org_col["field_key"], org_col["method"]) == ("company", "alias")


class TestTimeline:
    def test_job_events_in_plain_english(self, app_env: Env) -> None:
        job_id = _sent(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        status, body = _as(OWNER, "GET", f"/jobs/{job_id}/timeline")
        assert status == 200
        events = body["events"]
        assert all(e["row_id"] is None for e in events)
        stamps = [e["occurred_at"] for e in events]
        assert stamps == sorted(stamps)
        summaries = [e["summary"] for e in events]
        assert summaries[0] == "Upload started"
        assert "Moved from analysis review to ready to send" in summaries
        assert "Send of 1 lead confirmed" in summaries
        assert "Moved from sending to completed" in summaries
        created = events[0]
        assert created["actor"] == {"type": "user", "email": OWNER}

        _, with_rows = _as(OWNER, "GET", f"/jobs/{job_id}/timeline", query={"rows": "true"})
        submitted = [e for e in with_rows["events"] if e["event_type"] == "ROW_SUBMITTED"]
        assert submitted[0]["summary"] == "Row 2: submitted to Eloqua (attempt 1)"

    def test_pages(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        _, everything = _as(OWNER, "GET", f"/jobs/{job_id}/timeline")
        seen: list[str] = []
        cursor = None
        while True:
            query = {"limit": "3", **({"cursor": cursor} if cursor else {})}
            _, page = _as(OWNER, "GET", f"/jobs/{job_id}/timeline", query=query)
            seen += [e["event_id"] for e in page["events"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert seen == [e["event_id"] for e in everything["events"]]


class TestRowHistory:
    def test_every_value_of_a_sent_row_traces_back(self, app_env: Env) -> None:
        header = HEADER + ",Title,Lead Source - Most Recent"
        data = _csv(
            "Acme Demo Co,Ada,Example,ada@acme.example,701000000000001,vp mktg,Events",
            f"Globex Test Inc,Grace,Sample,grace@globex.example,{CAMPAIGN},,",
            header=header,
        )
        job_id = _analyzed(app_env, data)
        patch = {"processed": {"title": "vp marketing"}, "reason": "typo"}
        assert _as(OWNER, "PATCH", f"/jobs/{job_id}/rows/2", body=patch)[0] == 200
        _, gate = _as(OWNER, "GET", f"/jobs/{job_id}/gate")
        confirmation = {
            "rows_to_send": gate["rows_to_send"],
            "by_campaign": [
                {k: c[k] for k in ("campaign_id", "status", "rows")} for c in gate["by_campaign"]
            ],
        }
        body = {"confirmation": confirmation}
        assert _as(OWNER, "POST", f"/jobs/{job_id}/send", body=body)[0] == 202
        assert run_send(job_id, app_env.send_deps()) == "COMPLETED"

        for row_id, row in _rows(job_id).items():
            assert row["send"]["status"] == "submitted"
            status, history = _as(OWNER, "GET", f"/jobs/{job_id}/rows/{row_id}/history")
            assert status == 200
            by_field = {f["field"]: f for f in history["fields"]}
            # Every processed value is explained, ending at the value that was sent.
            assert set(row["processed"]) <= set(by_field)
            for key, value in row["processed"].items():
                lineage = by_field[key]
                assert lineage["explained"], lineage
                assert lineage["steps"][-1]["value"] == value
            assert history["send"]["status"] == "submitted"
            assert any(e["event_type"] == "ROW_SUBMITTED" for e in history["events"])

        _, ada = _as(OWNER, "GET", f"/jobs/{job_id}/rows/2/history")
        fields = {f["field"]: f for f in ada["fields"]}
        title = [(s["kind"], s["value"]) for s in fields["title"]["steps"]]
        assert title[0] == ("source", "vp mktg")
        assert ("user_edit", "vp marketing") in title
        assert title[-1][1] == _rows(job_id)[2]["processed"]["title"]
        cid = fields["campaign_id"]["steps"]
        assert cid[0]["value"] == "701000000000001" and cid[0]["label"].startswith("column ")
        assert cid[-1]["value"] == CAMPAIGN
        lead_source = fields["lead_source"]["steps"]
        assert [s["kind"] for s in lead_source] == ["source", "auto_corrected"]
        assert "Marketing: " in lead_source[-1]["label"]
        name = fields["campaign_name"]["steps"]
        assert name == [
            {
                **name[0],
                "kind": "derived",
                "value": "Demo Conference 2026",
                "label": "the campaign's name in Salesforce",
            }
        ]

    def test_unknown_row(self, app_env: Env) -> None:
        job_id = _analyzed(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        assert _as(OWNER, "GET", f"/jobs/{job_id}/rows/99/history")[0] == 404
        assert _as(OWNER, "GET", f"/jobs/{job_id}/rows/x/history")[0] == 400


class TestAuditSearch:
    def test_email_finds_every_job_that_included_the_person(self, app_env: Env) -> None:
        sent = _sent(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        analyzed = _analyzed(
            app_env,
            _csv(
                f"Globex,Grace,Sample,grace@globex.example,{CAMPAIGN}",
                f"Acme,Ada,Example,Ada@Acme.example,{WEBINAR}",
            ),
            owner=OTHER,
        )
        unrelated = _analyzed(app_env, _csv(f"Initech,Alan,Sample,alan@initech.example,{CAMPAIGN}"))

        status, body = _admin("GET", "/admin/audit", query={"email": " ADA@acme.example "})
        assert status == 200
        assert {j["job_id"] for j in body["jobs"]} == {sent, analyzed}
        assert unrelated not in {e["job_id"] for e in body["events"]}
        assert {j["owner_email"] for j in body["jobs"]} == {OWNER, OTHER}
        # Only Ada's rows, never Grace's.
        assert {(e["job_id"], e["row_id"]) for e in body["events"]} >= {(sent, 2), (analyzed, 3)}
        assert (analyzed, 2) not in {(e["job_id"], e["row_id"]) for e in body["events"]}

    def test_other_filters(self, app_env: Env) -> None:
        job_id = _sent(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))

        def search(**query: str) -> list[dict[str, Any]]:
            status, body = _admin("GET", "/admin/audit", query=query)
            assert status == 200, body
            return body["events"]  # type: ignore[no-any-return]

        assert {e["event_type"] for e in search(event_type="SEND_CONFIRMED")} == {"SEND_CONFIRMED"}
        mine = search(user=OWNER)
        assert mine and all(e["actor"]["email"] == OWNER for e in mine)
        assert {e["job_id"] for e in search(campaign_id=CAMPAIGN)} == {job_id}
        assert search(campaign_id="701000000000009AAA") == []
        assert len(search(job_id=job_id)) == len(
            [i for i in app_env.audit_table.scan()["Items"] if i["job_id"] == job_id]
        )
        today = _events(app_env, job_id, "JOB_CREATED")[0]["occurred_at"][:10]
        assert search(job_id=job_id, **{"from": today, "to": today})
        assert search(job_id=job_id, **{"from": "2999-01-01"}) == []
        assert _admin("GET", "/admin/audit", query={"from": "yesterday"})[0] == 400
        assert _admin("GET", "/admin/audit")[0] == 400  # no criteria

    def test_export_is_audited_without_plaintext_email(self, app_env: Env) -> None:
        job_id = _sent(app_env, _csv(f"=Evil Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        status, body = _admin("POST", "/admin/audit/export", body={"email": "ada@acme.example"})
        assert status == 200, body
        assert body["events"] > 0
        [event] = _events(app_env, GLOBAL_PARTITION, "AUDIT_EXPORTED")
        assert event["actor"]["email"] == BOSS
        assert event["details"]["filters"] == {"email_sha256": email_sha256("ada@acme.example")}
        assert "ada@acme.example" not in json.dumps(event["details"])
        data = app_env.s3.get_object(Bucket=PROCESSED_BUCKET, Key=event["details"]["s3_key"])
        rows = list(csv.DictReader(io.StringIO(data["Body"].read().decode("utf-8-sig"))))
        assert len(rows) == body["events"]
        assert {r["job_id"] for r in rows} == {job_id}
        # Values that a spreadsheet would run are neutralized.
        assert not any(v.startswith("=") for r in rows for v in r.values())


class TestHistory:
    def test_list_shows_campaigns_and_admin_sees_all(self, app_env: Env) -> None:
        mine = _analyzed(app_env, _csv(f"Co,Ada,Example,ada@acme.example,{CAMPAIGN}"))
        theirs = _analyzed(app_env, _csv(f"Co,Bo,Sample,bo@co.example,{WEBINAR}"), owner=OTHER)
        _, jobs = _as(OWNER, "GET", "/jobs")
        assert [j["job_id"] for j in jobs] == [mine]
        assert jobs[0]["campaigns"] == ["Demo Conference 2026"]
        _, everyone = _admin("GET", "/jobs", query={"all": "true"})
        assert {j["job_id"] for j in everyone} >= {mine, theirs}

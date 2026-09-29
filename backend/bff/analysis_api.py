"""Analysis routes (SPEC §6.3, §19): start, read, rows, edits, bulk actions."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError

from bff.app import app, correlation_id, current_user, deps, json_body, load_job_for
from bff.auth import User
from shared import messages
from shared.analysis import (
    Issue,
    RowEvaluation,
    apply_duplicates,
    derive_status,
    evaluate_row,
    summarize,
)
from shared.analysis_store import (
    build_context,
    campaign_info,
    mapping_from_job,
    row_events,
    row_item,
)
from shared.audit import Actor, AuditEvent, EventType
from shared.catalog import BY_KEY, CATALOG_VERSION, FIELDS
from shared.enrichment import eligible
from shared.issue_catalog import BULK_ACTION_LABELS, EXPLANATIONS, bulk_action_for
from shared.jobs import JobState, now_iso
from shared.mapping import PROMPT_VERSION as MAPPING_PROMPT_VERSION
from shared.observability import metrics
from shared.sfdc_ids import check_campaign_id

# Derived from Salesforce; the file can't override it (SPEC §8 footnote **).
NOT_EDITABLE = frozenset({"campaign_name"})
ROWS_PAGE_MAX = 500
# Rows can be fixed during analysis review and enrichment review (rows that were
# pending enrichment may need a fix once it has run).
REVIEW_STATES = frozenset({JobState.ANALYSIS_REVIEW, JobState.ENRICHMENT_REVIEW})


def _json(status: int, body: Any) -> Response[Any]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


# --- start ------------------------------------------------------------------------------


@app.post("/jobs/<job_id>/analyze")
def start_analysis(job_id: str) -> Response[Any]:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/analyze")
    state = JobState(job["state"])
    if state not in (JobState.MAPPING_REVIEW, JobState.ANALYSIS_REVIEW, JobState.FAILED):
        return _json(409, {"message": messages.JOB_STATE_CONFLICT})
    if not job.get("mapping_confirmed"):
        return _json(409, {"message": messages.ANALYSIS_NEEDS_MAPPING})
    d = deps()
    lead_sources = d.config.lead_sources()
    thresholds = d.config.thresholds()
    snapshot = {
        "normalizer_version": d.normalizer.version,
        "file_sha256": (job.get("file") or {}).get("sha256"),
        "catalog_version": CATALOG_VERSION,
        "alias_version": (job.get("mapping_suggestion") or {}).get("alias_version"),
        "lead_sources": list(lead_sources.active),
        "lead_source_version": lead_sources.version,
        "thresholds": thresholds,
        "bedrock_model_id": d.bedrock_model_id,
        "prompt_versions": {
            "column_mapping": MAPPING_PROMPT_VERSION,
            "junk_detection": "junk-v1",
            "lead_source_matching": "lead-source-v1",
        },
        "app_version": d.audit.app_version,
    }
    d.jobs.transition(
        job_id,
        state,
        JobState.ANALYZING,
        actor=user.actor(),
        set_fields={"analysis_snapshot": snapshot},
        events=[
            AuditEvent(
                event_type=EventType.ANALYSIS_STARTED,
                actor=user.actor(),
                job_id=job_id,
                details={"snapshot": snapshot, "rerun": state != JobState.MAPPING_REVIEW},
                correlation_id=correlation_id(),
            )
        ],
        correlation_id=correlation_id(),
    )
    d.start_analysis(job_id)
    metrics.add_metric(name="JobsReachedAnalysis", unit="Count", value=1)
    return _json(202, {"job_id": job_id, "state": JobState.ANALYZING})


# --- read -------------------------------------------------------------------------------


def _source_by_field(row: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, str]:
    source = row.get("source", {})
    return {key: str(source.get(header, "")) for header, key in mapping.items()}


def row_view(row: Mapping[str, Any], mapping: Mapping[str, str]) -> dict[str, Any]:
    return {
        "row_id": row["row_id"],
        "status": row.get("status"),
        "excluded": bool(row.get("excluded")),
        "source": _source_by_field(row, mapping),
        "processed": row.get("processed", {}),
        "provenance": row.get("provenance", {}),
        "issues": row.get("issues", []),
        "user_edits": row.get("user_edits", []),
        "dismissed": row.get("dismissed", []),
        "send": row.get("send") or {"status": "not_sent"},
        # Columns the user chose to ignore, by header, so the grid can show every
        # column of the file.
        "unmapped": {h: str(v) for h, v in (row.get("source") or {}).items() if h not in mapping},
    }


def grid_columns(job: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The Rows grid's columns: the file's columns in file order (ignored ones
    included, read only), then fields the app filled in that no column maps to."""
    confirmed = (job.get("mapping_confirmed") or {}).get("columns", [])
    columns: list[dict[str, Any]] = []
    mapped: set[str] = set()
    for c in confirmed:
        key = c.get("field_key")
        header = c["source_header"]
        if key:
            mapped.add(key)
            columns.append(
                {
                    "kind": "mapped",
                    "key": key,
                    "label": BY_KEY[key].label if key in BY_KEY else key,
                    "source_header": header,
                    "editable": key in BY_KEY and key not in NOT_EDITABLE,
                }
            )
        else:
            columns.append(
                {
                    "kind": "ignored",
                    "key": None,
                    "label": header,
                    "source_header": header,
                    "editable": False,
                }
            )
    present = {k for r in rows for k, v in (r.get("processed") or {}).items() if v}
    for f in FIELDS:
        if f.key in mapped:
            continue
        if f.fill_when_unmapped is not None or f.key in present:
            columns.append(
                {
                    "kind": "filled",
                    "key": f.key,
                    "label": f.label,
                    "source_header": None,
                    "editable": f.key not in NOT_EDITABLE,
                }
            )
    return columns


def _reviewable(job: Mapping[str, Any]) -> bool:
    return "analyzed_at" in job and job["state"] not in (
        JobState.ANALYZING,
        JobState.MAPPING_REVIEW,
        JobState.UPLOADED,
    )


@app.get("/jobs/<job_id>/analysis")
def get_analysis(job_id: str) -> Any:
    job = load_job_for(current_user(), job_id, "/jobs/{id}/analysis")
    if not _reviewable(job):
        return _json(409, {"message": messages.ANALYSIS_NOT_READY, "state": job["state"]})
    rows = deps().rows.list(job_id)
    counts: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("excluded"):
            continue
        # `count` is rows (what "Show rows" lists); `values` is flags, as one row can
        # have several (e.g. junk in four fields).
        seen: set[str] = set()
        for i in r.get("issues", []):
            entry = counts.setdefault(
                i["code"], {"code": i["code"], "severity": i["severity"], "count": 0, "values": 0}
            )
            entry["values"] += 1
            if i["code"] not in seen:
                seen.add(i["code"])
                entry["count"] += 1
    severity_order = {"blocking": 0, "warning": 1, "info": 2}
    groups = sorted(counts.values(), key=lambda g: (severity_order[g["severity"]], -g["count"]))
    for g in groups:
        g["explanation"] = EXPLANATIONS.get(g["code"], "")
        action = bulk_action_for(g["code"])
        g["bulk_action"] = action
        g["bulk_action_label"] = BULK_ACTION_LABELS.get(action) if action else None

    context = job.get("analysis_context") or {}
    rows_per_campaign: dict[str, int] = {}
    for r in rows:
        cid = (r.get("processed") or {}).get("campaign_id")
        if cid and not r.get("excluded"):
            rows_per_campaign[cid] = rows_per_campaign.get(cid, 0) + 1
    campaigns = [
        {**c, "row_count": rows_per_campaign.get(cid, 0)}
        for cid, c in sorted(context.get("campaigns", {}).items())
    ]
    return {
        "state": job["state"],
        "editable": job["state"] in REVIEW_STATES,
        "enrich": bool(job.get("enrich")),
        "summary": summarize(r.get("status", "ready") for r in rows),
        "issue_groups": groups,
        "campaigns": campaigns,
        "lead_sources": context.get("lead_sources", []),
        "enrichment_lookup_count": sum(1 for r in rows if eligible(r)) if job.get("enrich") else 0,
        "notes": job.get("analysis_notes", []),
        "normalizer_version": context.get("normalizer_version"),
        "columns": grid_columns(job, rows),
    }


@app.get("/jobs/<job_id>/rows")
def list_rows(job_id: str) -> Any:
    job = load_job_for(current_user(), job_id, "/jobs/{id}/rows")
    if not _reviewable(job):
        return _json(409, {"message": messages.ANALYSIS_NOT_READY, "state": job["state"]})
    q = app.current_event.query_string_parameters or {}
    try:
        offset = max(0, int(q.get("offset", 0)))
        limit = min(ROWS_PAGE_MAX, max(1, int(q.get("limit", 100))))
    except ValueError as exc:
        raise BadRequestError("offset and limit must be numbers") from exc
    rows = deps().rows.list(job_id)
    if q.get("status"):
        rows = [r for r in rows if r.get("status") == q["status"]]
    if q.get("issue_code"):
        rows = [r for r in rows if any(i["code"] == q["issue_code"] for i in r.get("issues", []))]
    if q.get("campaign_id"):
        rows = [
            r for r in rows if (r.get("processed") or {}).get("campaign_id") == q["campaign_id"]
        ]
    mapping = _mapping(job)
    return {
        "total": len(rows),
        "offset": offset,
        "rows": [row_view(r, mapping) for r in rows[offset : offset + limit]],
    }


def _mapping(job: Mapping[str, Any]) -> dict[str, str]:
    return mapping_from_job(job)


# --- changes ----------------------------------------------------------------------------


@dataclass
class RowChange:
    edits: dict[str, str] = field(default_factory=dict)
    excluded: bool | None = None
    dismiss: list[str] = field(default_factory=list)
    restore: list[str] = field(default_factory=list)
    reason: str = "user_edit"
    accepted_suggestion: dict[str, Any] | None = None
    enrichment_decision: str | None = None  # apply | skip (SPEC §15.4)


def _ensure_campaigns(job: dict[str, Any], values: Sequence[str], user: User) -> None:
    """Look up campaign IDs typed during review that analysis hasn't seen."""
    context = job.setdefault("analysis_context", {})
    known = context.setdefault("campaigns", {})
    new_ids = sorted(
        {c.value for v in values if (c := check_campaign_id(v)).value and c.value not in known}
    )
    if not new_ids:
        return
    d = deps()
    found = {
        c.id: campaign_info(c)
        for c in d.workato.lookup_campaigns(new_ids, caller_job_id=job["job_id"])
    }
    for cid, info in found.items():
        known[cid] = info.as_dict()
    d.jobs.update_in_state(
        job["job_id"],
        JobState(job["state"]),
        set_fields={"analysis_context": context},
        events=[
            AuditEvent(
                event_type=EventType.CAMPAIGN_VALIDATED,
                actor=user.actor(),
                job_id=job["job_id"],
                details={
                    "requested": new_ids,
                    "found": [i for i in new_ids if found[i].found],
                    "not_found": [i for i in new_ids if not found[i].found],
                    "inactive": [i for i in new_ids if found[i].is_active is False],
                },
                correlation_id=correlation_id(),
            )
        ],
    )


def _without_duplicates(issues: Sequence[Mapping[str, Any]]) -> list[Issue]:
    return [
        Issue(
            i["code"],
            i["severity"],
            i["message"],
            i.get("field"),
            i.get("source", "rule"),
            i.get("suggestion"),
            bool(i.get("pending")),
        )
        for i in issues
        if i["code"] != "DUPLICATE_IN_FILE"
    ]


def apply_changes(
    job: dict[str, Any],
    changes: Mapping[int, RowChange],
    user: User,
    *,
    reevaluate_all: bool = False,
    system_reason: str = "duplicate_recheck",
) -> dict[int, dict[str, Any]]:
    """Apply edits to rows, re-validate, re-check duplicates, and write each changed row
    together with its audit events. Returns the updated rows by ID."""
    d = deps()
    job_id = job["job_id"]
    state = JobState(job["state"])
    for change in changes.values():
        if "campaign_id" in change.edits:
            _ensure_campaigns(job, [change.edits["campaign_id"]], user)
    ctx = build_context(job, d.normalizer)
    rows = {r["row_id"]: r for r in d.rows.list(job_id)}
    missing = [rid for rid in changes if rid not in rows]
    if missing:
        raise NotFoundError(f"Row {missing[0]} isn't in this upload.")

    at = now_iso()
    updated: dict[int, dict[str, Any]] = {}
    evaluations: dict[int, RowEvaluation] = {}
    for rid, row in rows.items():
        if rid in changes:
            change = changes[rid]
            new = copy.deepcopy(row)
            for key, value in change.edits.items():
                new.setdefault("user_edits", []).append(
                    {
                        "field": key,
                        "from": (row.get("processed") or {}).get(key, ""),
                        "to": value,
                        "by": user.email,
                        "at": at,
                    }
                )
            if change.excluded is not None:
                new["excluded"] = change.excluded
            if change.enrichment_decision:
                new["enrichment"] = {
                    **(new.get("enrichment") or {}),
                    "decision": change.enrichment_decision,
                    "decided_by": user.email,
                    "decided_at": at,
                }
            dismissed = set(new.get("dismissed", [])) | set(change.dismiss)
            new["dismissed"] = sorted(dismissed - set(change.restore))
            updated[rid] = new
            evaluations[rid] = evaluate_row(new, ctx)
        elif reevaluate_all:
            evaluations[rid] = evaluate_row(row, ctx)
        else:
            # Unchanged rows keep their stored evaluation; only duplicates are rechecked.
            issues = _without_duplicates(row.get("issues", []))
            evaluations[rid] = RowEvaluation(
                processed=dict(row.get("processed", {})),
                provenance=dict(row.get("provenance", {})),
                issues=issues,
                status=derive_status(issues, bool(row.get("excluded"))),
            )
    excluded = {rid for rid in rows if (updated.get(rid) or rows[rid]).get("excluded")}
    apply_duplicates(evaluations, excluded)

    written: dict[int, dict[str, Any]] = {}
    for rid, ev in evaluations.items():
        before = rows[rid]
        after_row = updated.get(rid, before)
        item = row_item(after_row, ev, before.get("analyzed_at", at))
        changed = rid in changes or [i["code"] for i in before.get("issues", [])] != [
            i.code for i in ev.issues
        ]
        if reevaluate_all and not changed:
            changed = (before.get("processed") or {}) != ev.processed or before.get(
                "status"
            ) != ev.status
        if not changed:
            continue
        events: list[AuditEvent] = []
        if rid in changes:
            change = changes[rid]
            for key, value in change.edits.items():
                events.append(
                    AuditEvent(
                        event_type=EventType.USER_EDIT,
                        actor=user.actor(),
                        job_id=job_id,
                        row_id=rid,
                        subject={"field": key},
                        before={key: (before.get("processed") or {}).get(key)},
                        after={key: value},
                        reason=change.reason,
                        lead_email=ev.processed.get("email"),
                        correlation_id=correlation_id(),
                    )
                )
            if change.accepted_suggestion:
                events.append(
                    AuditEvent(
                        event_type=EventType.SUGGESTION_ACCEPTED,
                        actor=user.actor(),
                        job_id=job_id,
                        row_id=rid,
                        subject={"field": "lead_source"},
                        after={"lead_source": change.accepted_suggestion["value"]},
                        details={"confidence": change.accepted_suggestion.get("confidence")},
                        reason=change.reason,
                        correlation_id=correlation_id(),
                    )
                )
            if change.enrichment_decision:
                events.append(
                    AuditEvent(
                        event_type=EventType.ENRICHMENT_DECISION,
                        actor=user.actor(),
                        job_id=job_id,
                        row_id=rid,
                        details={
                            "decision": change.enrichment_decision,
                            "match_status": (before.get("enrichment") or {}).get("match_status"),
                            "score": (before.get("enrichment") or {}).get("score"),
                        },
                        reason=change.reason,
                        lead_email=ev.processed.get("email"),
                        correlation_id=correlation_id(),
                    )
                )
            if change.excluded is not None and change.excluded != bool(before.get("excluded")):
                events.append(
                    AuditEvent(
                        event_type=(
                            EventType.ROW_EXCLUDED if change.excluded else EventType.ROW_INCLUDED
                        ),
                        actor=user.actor(),
                        job_id=job_id,
                        row_id=rid,
                        reason=change.reason,
                        lead_email=ev.processed.get("email"),
                        correlation_id=correlation_id(),
                    )
                )
        events.extend(
            row_events(
                job_id,
                before,
                ev,
                user.actor() if rid in changes else Actor.system(),
                reason=change.reason if rid in changes else system_reason,
                correlation_id=correlation_id(),
            )
        )
        writes = [
            d.jobs.condition_in_state(job_id, state),
            {"Put": {"TableName": d.rows.table_name, "Item": item}},
        ]
        if not events:
            continue  # nothing actually changed (e.g. excluding an excluded row)
        d.audit.transact(events, writes)
        written[rid] = item

    all_rows = {**rows, **written}
    d.jobs.update_cached(
        job_id,
        state,
        {"summary": summarize(r.get("status", "ready") for r in all_rows.values())},
    )
    return written


def _review_job(job_id: str, route: str) -> tuple[User, dict[str, Any]]:
    user = current_user()
    job = load_job_for(user, job_id, route)
    if job["state"] not in REVIEW_STATES:
        raise _Conflict(messages.ROW_NOT_EDITABLE)
    return user, job


class _Conflict(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


@app.exception_handler(_Conflict)  # type: ignore[untyped-decorator]
def _conflict(exc: _Conflict) -> Response[Any]:
    return _json(409, {"message": exc.message})


def _parse_change(body: Mapping[str, Any]) -> RowChange:
    change = RowChange()
    processed = body.get("processed") or {}
    if not isinstance(processed, dict):
        raise BadRequestError("processed must be an object of field: value.")
    for key, value in processed.items():
        if key not in BY_KEY or key in NOT_EDITABLE:
            raise BadRequestError(f"'{key}' can't be edited here.")
        if not isinstance(value, str) or len(value) > 1000:
            raise BadRequestError(f"{BY_KEY[key].label} must be text of 1,000 characters or fewer.")
        change.edits[key] = value
    if "excluded" in body:
        if not isinstance(body["excluded"], bool):
            raise BadRequestError("excluded must be true or false.")
        change.excluded = body["excluded"]
    for key in ("dismiss", "restore"):
        value = body.get(key)
        if value is not None:
            if not isinstance(value, str) or ":" not in value:
                raise BadRequestError(f"{key} must look like 'CODE:field'.")
            getattr(change, key).append(value)
    if not (change.edits or change.excluded is not None or change.dismiss or change.restore):
        raise BadRequestError("Nothing to change.")
    return change


@app.patch("/jobs/<job_id>/rows/<row_id>")
def edit_row(job_id: str, row_id: str) -> Any:
    user, job = _review_job(job_id, "/jobs/{id}/rows/{row_id}")
    try:
        rid = int(row_id)
    except ValueError as exc:
        raise NotFoundError("Row not found.") from exc
    written = apply_changes(job, {rid: _parse_change(json_body())}, user)
    row = written.get(rid) or deps().rows.get(job_id, rid)
    return {"row": row_view(row or {}, _mapping(job)), "also_changed": sorted(set(written) - {rid})}


@app.post("/jobs/<job_id>/bulk-actions")
def bulk_action(job_id: str) -> Any:
    user, job = _review_job(job_id, "/jobs/{id}/bulk-actions")
    body = json_body()
    action = body.get("action")
    params = body.get("params") or {}
    rows = deps().rows.list(job_id)

    def with_issue(code: str) -> list[dict[str, Any]]:
        return [
            r
            for r in rows
            if not r.get("excluded") and any(i["code"] == code for i in r.get("issues", []))
        ]

    changes: dict[int, RowChange] = {}
    reason = f"bulk:{action}"
    if action == "exclude_duplicates":
        for r in with_issue("DUPLICATE_IN_FILE"):
            changes[r["row_id"]] = RowChange(excluded=True, reason=reason)
    elif action == "exclude_junk":
        for code in ("VALUE_JUNK", "VALUE_SUSPECT"):
            for r in with_issue(code):
                changes[r["row_id"]] = RowChange(excluded=True, reason=reason)
    elif action == "accept_lead_source_suggestions":
        minimum = float(params.get("min_confidence", 0.9))
        for r in with_issue("LEAD_SOURCE_SUGGESTED"):
            s = next(i for i in r["issues"] if i["code"] == "LEAD_SOURCE_SUGGESTED")["suggestion"]
            if float(s.get("confidence") or 0) >= minimum:
                changes[r["row_id"]] = RowChange(
                    edits={"lead_source": s["value"]}, reason=reason, accepted_suggestion=s
                )
    elif action == "set_status":
        campaign_id, value, status = (params.get(k) for k in ("campaign_id", "value", "status"))
        campaign = ((job.get("analysis_context") or {}).get("campaigns") or {}).get(campaign_id)
        if not campaign or status not in campaign.get("statuses", []):
            raise BadRequestError(f"'{status}' isn't a status on that campaign.")
        for r in with_issue("STATUS_INVALID"):
            p = r.get("processed") or {}
            if (
                p.get("campaign_id") == campaign_id
                and str(p.get("campaign_status", "")).casefold() == str(value).casefold()
            ):
                changes[r["row_id"]] = RowChange(
                    edits={"campaign_status": str(status)}, reason=reason
                )
    else:
        raise BadRequestError(f"Unknown bulk action '{action}'.")

    if not changes:
        return {"action": action, "affected_row_ids": [], "summary": job.get("summary")}
    deps().audit.write(
        AuditEvent(
            event_type=EventType.BULK_ACTION,
            actor=user.actor(),
            job_id=job_id,
            details={"action": action, "params": params, "row_ids": sorted(changes)},
            correlation_id=correlation_id(),
        )
    )
    apply_changes(job, changes, user)
    metrics.add_metric(name="BulkActionRows", unit="Count", value=len(changes))
    refreshed = deps().jobs.get(job_id) or job
    return {
        "action": action,
        "affected_row_ids": sorted(changes),
        "summary": refreshed.get("summary"),
    }

"""Enrichment routes (SPEC §6.4, §15, §19): start, review, apply/skip decisions."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError

from bff.analysis_api import RowChange, apply_changes
from bff.app import app, correlation_id, current_user, deps, json_body, load_job_for
from shared import messages
from shared.catalog import BY_KEY
from shared.jobs import JobState
from shared.observability import metrics

REVIEW_LIST_MAX = 500
FILLED_LIST_MAX = 1000
DECISIONS = ("apply", "skip")


def _json(status: int, body: Any) -> Response[Any]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


@app.post("/jobs/<job_id>/enrich")
def start_enrichment(job_id: str) -> Response[Any]:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/enrich")
    if not job.get("enrich"):
        return _json(409, {"message": messages.ENRICHMENT_NOT_ENABLED})
    state = JobState(job["state"])
    retry = state == JobState.FAILED and (job.get("last_error") or {}).get("stage") == "enrichment"
    if state != JobState.ANALYSIS_REVIEW and not retry:
        return _json(409, {"message": messages.JOB_STATE_CONFLICT})
    deps().jobs.transition(
        job_id,
        state,
        JobState.ENRICHING,
        actor=user.actor(),
        details={"retry": retry},
        correlation_id=correlation_id(),
    )
    deps().start_enrichment(job_id)
    metrics.add_metric(name="JobsReachedEnrichment", unit="Count", value=1)
    return _json(202, {"job_id": job_id, "state": JobState.ENRICHING})


def _label(key: str) -> str:
    return BY_KEY[key].label if key in BY_KEY else key


@app.get("/jobs/<job_id>/enrichment")
def get_enrichment(job_id: str) -> Any:
    job = load_job_for(current_user(), job_id, "/jobs/{id}/enrichment")
    if "enrichment_completed_at" not in job:
        return _json(409, {"message": messages.ENRICHMENT_NOT_READY, "state": job["state"]})
    rows = deps().rows.list(job_id)
    review: list[dict[str, Any]] = []
    filled: list[dict[str, Any]] = []
    for r in rows:
        enr = r.get("enrichment") or {}
        processed = r.get("processed") or {}
        if enr.get("status") == "review" and len(review) < REVIEW_LIST_MAX:
            review.append(
                {
                    "row_id": r["row_id"],
                    "source": {
                        "name": " ".join(
                            filter(None, (processed.get("first_name"), processed.get("last_name")))
                        ),
                        "company": processed.get("company", ""),
                        "title": processed.get("title", ""),
                        "email": processed.get("email", ""),
                    },
                    "candidate": enr.get("candidate_display", {}),
                    "match_score": enr.get("score"),
                    "conflicts": enr.get("conflicts", []),
                    "would_fill": sorted(
                        _label(k) for k in (enr.get("fields") or {}) if not processed.get(k)
                    ),
                    "decision": enr.get("decision"),
                }
            )
        for key, how in (r.get("provenance") or {}).items():
            if how == "enrichment:zoominfo" and len(filled) < FILLED_LIST_MAX:
                filled.append(
                    {
                        "row_id": r["row_id"],
                        "field": _label(key),
                        "before": "",
                        "after": processed.get(key, ""),
                    }
                )
    summary = job.get("enrichment_summary") or {}
    return {
        "state": job["state"],
        "editable": job["state"] == JobState.ENRICHMENT_REVIEW,
        **summary,
        "fields_filled": {_label(k): n for k, n in (summary.get("fields_filled") or {}).items()},
        "review": review,
        "filled": filled,
        "notes": job.get("analysis_notes", []),
        "row_summary": job.get("summary"),
    }


@app.post("/jobs/<job_id>/enrichment-decisions")
def decide(job_id: str) -> Any:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/enrichment-decisions")
    if job["state"] != JobState.ENRICHMENT_REVIEW:
        return _json(409, {"message": messages.JOB_STATE_CONFLICT})
    body = json_body()
    rows = {r["row_id"]: r for r in deps().rows.list(job_id)}
    reviewable = {
        rid for rid, r in rows.items() if (r.get("enrichment") or {}).get("status") == "review"
    }
    decisions: dict[int, str] = {}
    if body.get("skip_all"):
        decisions = {
            rid: "skip" for rid in reviewable if not rows[rid]["enrichment"].get("decision")
        }
    else:
        items = body.get("decisions")
        if not isinstance(items, list) or not items:
            raise BadRequestError("Send decisions as a list of {row_id, decision}.")
        for item in items:
            rid = item.get("row_id") if isinstance(item, dict) else None
            decision = item.get("decision") if isinstance(item, dict) else None
            if not isinstance(rid, int) or rid not in reviewable:
                raise BadRequestError(f"Row {rid} doesn't have a match waiting for review.")
            if decision not in DECISIONS:
                raise BadRequestError("Each decision must be 'apply' or 'skip'.")
            decisions[rid] = str(decision)
    if not decisions:
        return {"decided_row_ids": [], "summary": job.get("summary")}
    changes = {
        rid: RowChange(enrichment_decision=d, reason=f"enrichment_decision:{d}")
        for rid, d in decisions.items()
    }
    apply_changes(job, changes, user)
    applied = sum(1 for d in decisions.values() if d == "apply")
    metrics.add_metric(name="EnrichmentReviewApplied", unit="Count", value=applied)
    metrics.add_metric(name="EnrichmentReviewSkipped", unit="Count", value=len(decisions) - applied)
    refreshed = deps().jobs.get(job_id) or job
    return {"decided_row_ids": sorted(decisions), "summary": refreshed.get("summary")}

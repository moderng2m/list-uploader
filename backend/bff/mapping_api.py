"""Column mapping routes (SPEC §6.2, §10, §19)."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError

from bff.app import app, correlation_id, current_user, deps, json_body, load_job_for
from shared import messages
from shared.audit import AuditEvent, EventType
from shared.catalog import CATALOG_VERSION, FIELDS
from shared.jobs import JobState, now_iso
from shared.mapping import MappingInvalid, Method, validate_confirmation
from shared.observability import metrics

SAMPLE_ROWS = 50
SAMPLES_SHOWN = 3


def _conflict(message: str) -> Response[Any]:
    return Response(
        status_code=409, content_type=content_types.APPLICATION_JSON, body={"message": message}
    )


def _samples(job_id: str, headers: list[str]) -> dict[str, list[str]]:
    """A few non-empty values per column, read from the first rows (not stored on the job)."""
    samples: dict[str, list[str]] = {h: [] for h in headers}
    for row in deps().rows.first(job_id, SAMPLE_ROWS):
        for header, value in row.get("source", {}).items():
            bucket = samples.get(header)
            if bucket is not None and len(bucket) < SAMPLES_SHOWN and str(value).strip():
                bucket.append(str(value))
    return samples


def mapping_editable(job: dict[str, Any]) -> bool:
    """The mapping can change until enrichment (or sending) starts: while the mapping is
    being reviewed, while the analysis is being reviewed, or after an analysis failed.
    A change after analysis means the analysis runs again (the page starts it)."""
    state = job["state"]
    return state in (JobState.MAPPING_REVIEW, JobState.ANALYSIS_REVIEW) or (
        state == JobState.FAILED and (job.get("last_error") or {}).get("stage") == "analysis"
    )


def mapping_view(job: dict[str, Any]) -> dict[str, Any]:
    suggestion = job["mapping_suggestion"]
    confirmed = job.get("mapping_confirmed")
    columns = confirmed["columns"] if confirmed else suggestion["columns"]
    samples = _samples(job["job_id"], [c["source_header"] for c in columns])
    ai = suggestion.get("ai", {})
    return {
        "columns": [
            {
                "source_header": c["source_header"],
                "samples": samples.get(c["source_header"], []),
                "field_key": c.get("field_key"),
                "method": c.get("method", "none"),
                "confidence": c.get("confidence"),
                "reason": c.get("reason"),
            }
            for c in columns
        ],
        "catalog": [
            {
                "key": f.key,
                "label": f.label,
                "description": f.description,
                "required": f.required,
                "must_map": f.must_map,
                "fill_when_unmapped": f.fill_when_unmapped,
            }
            for f in FIELDS
        ],
        "confirmed": confirmed is not None,
        "confirmed_at": confirmed.get("confirmed_at") if confirmed else None,
        "editable": mapping_editable(job),
        "ai_note": messages.MAPPING_AI_UNAVAILABLE if ai.get("degraded") else None,
    }


@app.get("/jobs/<job_id>/mapping")
def get_mapping(job_id: str) -> Any:
    job = load_job_for(current_user(), job_id, "/jobs/{id}/mapping")
    if "mapping_suggestion" not in job:
        return _conflict(messages.MAPPING_NOT_READY)
    return mapping_view(job)


@app.put("/jobs/<job_id>/mapping")
def confirm_mapping(job_id: str) -> Any:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/mapping")
    if "mapping_suggestion" not in job:
        return _conflict(messages.MAPPING_NOT_READY)
    if not mapping_editable(job):
        return _conflict(messages.MAPPING_LOCKED)
    state = JobState(job["state"])
    previous = job.get("mapping_confirmed")
    choices = json_body().get("columns")
    if not isinstance(choices, list) or not all(isinstance(c, dict) for c in choices):
        raise BadRequestError("Send the mapping as a list of columns.")
    try:
        chosen = validate_confirmation(job["parse"]["headers"], choices)
    except MappingInvalid as err:
        raise BadRequestError(err.message) from err

    suggested = {c["source_header"]: c for c in job["mapping_suggestion"]["columns"]}
    final: list[dict[str, Any]] = []
    changed: list[str] = []
    decisions: list[AuditEvent] = []
    for header in job["parse"]["headers"]:
        key = chosen[header]
        was = suggested.get(header, {})
        kept = key == was.get("field_key") and key is not None
        method = was.get("method", Method.NONE) if kept else (Method.MANUAL if key else Method.NONE)
        final.append(
            {
                "source_header": header,
                "field_key": key,
                "method": str(method),
                "confidence": was.get("confidence") if kept else None,
                "reason": was.get("reason") if kept else None,
            }
        )
        if key != was.get("field_key"):
            changed.append(header)
        # AI suggestions are accepted or rejected once, at the first confirmation.
        if was.get("method") == Method.AI and previous is None:
            decisions.append(
                AuditEvent(
                    event_type=(
                        EventType.SUGGESTION_ACCEPTED if kept else EventType.SUGGESTION_REJECTED
                    ),
                    actor=user.actor(),
                    job_id=job_id,
                    subject={"field": "column_mapping", "source_header": header},
                    before={"field_key": was.get("field_key")},
                    after={"field_key": key},
                    reason="ai:column_mapping",
                    details={"confidence": was.get("confidence")},
                    correlation_id=correlation_id(),
                )
            )

    before = {c["source_header"]: c.get("field_key") for c in (previous or {}).get("columns", [])}
    changed_vs_previous = [
        c["source_header"] for c in final if before.get(c["source_header"]) != c["field_key"]
    ]
    # Analysis has to (re)run after the first confirmation, after any change, and to
    # recover from a failed analysis.
    analysis_needed = previous is None or bool(changed_vs_previous) or state == JobState.FAILED
    if previous is not None and not changed_vs_previous:
        return {**mapping_view(job), "analysis_needed": analysis_needed}

    confirmed = {
        "columns": final,
        "confirmed_at": now_iso(),
        "confirmed_by": user.email,
        "catalog_version": CATALOG_VERSION,
        "changed_vs_suggestion": changed,
    }
    deps().jobs.update_in_state(
        job_id,
        state,
        set_fields={"mapping_confirmed": confirmed},
        events=[
            *decisions,
            AuditEvent(
                event_type=EventType.MAPPING_CONFIRMED,
                actor=user.actor(),
                job_id=job_id,
                details={
                    "columns": [
                        {k: c[k] for k in ("source_header", "field_key", "method")} for c in final
                    ],
                    "changed_vs_suggestion": changed,
                    **(
                        {"reconfirmed": True, "changed_vs_previous": changed_vs_previous}
                        if previous is not None
                        else {}
                    ),
                },
                correlation_id=correlation_id(),
            ),
        ],
    )
    accepted = sum(1 for e in decisions if e.event_type == EventType.SUGGESTION_ACCEPTED)
    metrics.add_metric(name="AISuggestionsAccepted", unit="Count", value=accepted)
    metrics.add_metric(name="AISuggestionsRejected", unit="Count", value=len(decisions) - accepted)
    metrics.add_metric(name="MappingColumnsChanged", unit="Count", value=len(changed))
    return {
        **mapping_view({**job, "mapping_confirmed": confirmed}),
        "analysis_needed": analysis_needed,
    }

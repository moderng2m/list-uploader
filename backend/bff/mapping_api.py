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
        "editable": job["state"] == JobState.MAPPING_REVIEW,
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
    if job["state"] != JobState.MAPPING_REVIEW or "mapping_suggestion" not in job:
        return _conflict(
            messages.MAPPING_NOT_READY
            if "mapping_suggestion" not in job
            else messages.MAPPING_LOCKED
        )
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
        if was.get("method") == Method.AI:
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

    confirmed = {
        "columns": final,
        "confirmed_at": now_iso(),
        "confirmed_by": user.email,
        "catalog_version": CATALOG_VERSION,
        "changed_vs_suggestion": changed,
    }
    deps().jobs.update_in_state(
        job_id,
        JobState.MAPPING_REVIEW,
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
                },
                correlation_id=correlation_id(),
            ),
        ],
    )
    accepted = sum(1 for e in decisions if e.event_type == EventType.SUGGESTION_ACCEPTED)
    metrics.add_metric(name="AISuggestionsAccepted", unit="Count", value=accepted)
    metrics.add_metric(name="AISuggestionsRejected", unit="Count", value=len(decisions) - accepted)
    metrics.add_metric(name="MappingColumnsChanged", unit="Count", value=len(changed))
    return mapping_view({**job, "mapping_confirmed": confirmed})

"""Job timeline and row history (SPEC §6.7, §21.2.6). Owner or admin only."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError

from bff.app import app, current_user, deps, load_job_for
from shared import messages
from shared.analysis_store import mapping_from_job
from shared.audit_read import AuditReader
from shared.audit_text import event_view
from shared.lineage import field_lineage

TIMELINE_PAGE = 200


def reader() -> AuditReader:
    d = deps()
    return d.audit_reader or AuditReader(d.audit.table_name)


def _int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise BadRequestError(f"{what} must be a number.") from None


@app.get("/jobs/<job_id>/timeline")
def timeline(job_id: str) -> dict[str, Any]:
    load_job_for(current_user(), job_id, "/jobs/{id}/timeline")
    q = app.current_event.query_string_parameters or {}
    limit = min(TIMELINE_PAGE, max(1, _int(q.get("limit", str(TIMELINE_PAGE)), "limit")))
    page = reader().job_page(
        job_id,
        include_rows=q.get("rows") == "true",
        cursor=q.get("cursor") or None,
        limit=limit,
    )
    return {"events": [event_view(e) for e in page.events], "next_cursor": page.next_cursor}


@app.get("/jobs/<job_id>/rows/<row_id>/history")
def row_history(job_id: str, row_id: str) -> dict[str, Any]:
    job = load_job_for(current_user(), job_id, "/jobs/{id}/rows/{row_id}/history")
    rid = _int(row_id, "row")
    events = reader().row_events(job_id, rid)
    row = deps().rows.get(job_id, rid)
    if row is None and not events:
        raise NotFoundError(messages.ROW_NOT_FOUND)
    snapshot = job.get("analysis_snapshot") or {}
    normalizer = str(snapshot.get("normalizer_version") or "normalizer")
    return {
        "row_id": rid,
        # Rows expire before the audit trail does (SPEC §21.2.5): events only then.
        "row_available": row is not None,
        "status": (row or {}).get("status"),
        "excluded": bool((row or {}).get("excluded")),
        "normalizer_version": normalizer,
        "fields": field_lineage(row, mapping_from_job(job), events, normalizer=normalizer)
        if row
        else [],
        "enrichment": (row or {}).get("enrichment"),
        "send": (row or {}).get("send"),
        "events": [event_view(e) for e in events],
    }

"""Plain-English one-liners for audit events and provenance (SPEC §21.2.6).

Shown to the job's owner and to admins. Values stay out of the summary; the
expandable details carry before/after.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from shared.catalog import BY_KEY

PROVENANCE: dict[str, str] = {
    "source": "from your file",
    "normalized": "cleaned up by the normalizer",
    "user_edit": "edited by hand",
    "enrichment:zoominfo": "filled by ZoomInfo",
    "derived:sfdc_campaign_name": "the campaign's name in Salesforce",
    "derived:sfdc_campaign_type": "the campaign's type in Salesforce",
    "derived:sfdc_default_status": "the campaign's default member status",
    "derived:auto_list_name": "built from the campaign name and today's date",
    "auto_corrected:rule:marketing_prefix": "corrected by rule (added 'Marketing: ')",
    "auto_corrected:ai": "corrected by AI (high confidence)",
}


def provenance_text(how: str | None) -> str:
    if not how:
        return ""
    return PROVENANCE.get(how, how.replace("_", " ").replace(":", ": "))


def _label(key: str) -> str:
    field = BY_KEY.get(key)
    return field.label if field else key.replace("_", " ")


def _fields(event: Mapping[str, Any]) -> str:
    subject = event.get("subject") or {}
    keys = subject.get("fields") or ([subject["field"]] if subject.get("field") else [])
    if not keys:
        keys = sorted((event.get("after") or {}).keys())
    return ", ".join(_label(k) for k in keys)


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _state(value: Any) -> str:
    return str(value or "?").replace("_", " ").lower()


def _details(event: Mapping[str, Any]) -> dict[str, Any]:
    d = event.get("details")
    return d if isinstance(d, dict) else {}


def _row(event: Mapping[str, Any]) -> str:
    return f"Row {event['row_id']}: " if event.get("row_id") is not None else ""


_SUMMARIES: dict[str, Callable[[Mapping[str, Any], dict[str, Any]], str]] = {
    "JOB_CREATED": lambda e, d: (
        "Upload started" + (" with enrichment on" if d.get("enrich") else "")
    ),
    "FILE_UPLOADED": lambda e, d: (
        "File received and locked"
        + (f" ({d['size']:,} bytes)" if isinstance(d.get("size"), int) else "")
    ),
    "FILE_PARSED": lambda e, d: (
        "File read: "
        + _n(int(d.get("row_count") or 0), "row", "rows")
        + ", "
        + _n(int(d.get("column_count") or 0), "column", "columns")
    ),
    "PARSE_FAILED": lambda e, d: "The file couldn't be read",
    "MAPPING_SUGGESTED": lambda e, d: "Column mapping suggested",
    "MAPPING_CONFIRMED": lambda e, d: "Column mapping confirmed",
    "ANALYSIS_STARTED": lambda e, d: "Analysis re-run" if d.get("rerun") else "Analysis started",
    "VALUE_NORMALIZED": lambda e, d: f"{_row(e)}{_fields(e)} cleaned up by the normalizer",
    "VALUE_AUTO_CORRECTED": lambda e, d: f"{_row(e)}{_fields(e)} corrected automatically",
    "VALUE_DERIVED": lambda e, d: f"{_row(e)}{_fields(e)} filled from Salesforce",
    "ISSUE_RAISED": lambda e, d: (
        f"{_row(e)}issue found: " + ", ".join(str(i.get("code")) for i in d.get("issues", []))
    ),
    "ISSUE_CLEARED": lambda e, d: (
        f"{_row(e)}issue resolved: " + ", ".join(str(i.get("code")) for i in d.get("issues", []))
    ),
    "AI_INVOCATION": lambda e, d: (
        f"AI call ({d.get('purpose', 'unknown')}): " + str(d.get("outcome", "?"))
    ),
    "CAMPAIGN_VALIDATED": lambda e, d: (
        "Campaigns checked in Salesforce: "
        + f"{len(d.get('found', []))} found, {len(d.get('not_found', []))} not found"
    ),
    "USER_EDIT": lambda e, d: f"{_row(e)}{_fields(e)} edited",
    "ROW_EXCLUDED": lambda e, d: f"{_row(e)}excluded",
    "ROW_INCLUDED": lambda e, d: f"{_row(e)}included again",
    "BULK_ACTION": lambda e, d: (
        f"Bulk action '{d.get('action')}' on " + _n(len(d.get("row_ids", [])), "row", "rows")
    ),
    "SUGGESTION_ACCEPTED": lambda e, d: (
        f"{_row(e)}AI suggestion accepted for {_fields(e)}"
        if (e.get("subject") or {}).get("field") != "column_mapping"
        else f"AI column match kept for '{(e.get('subject') or {}).get('source_header')}'"
    ),
    "SUGGESTION_REJECTED": lambda e, d: (
        f"{_row(e)}AI suggestion changed for {_fields(e)}"
        if (e.get("subject") or {}).get("field") != "column_mapping"
        else f"AI column match changed for '{(e.get('subject') or {}).get('source_header')}'"
    ),
    "ENRICHMENT_REQUESTED": lambda e, d: (
        "ZoomInfo lookup for " + _n(len(d.get("row_ids", [])), "contact", "contacts")
    ),
    "ENRICHMENT_RESULT": lambda e, d: (
        f"{_row(e)}ZoomInfo "
        + (f"filled {_fields(e)}" if e.get("after") else f"result: {d.get('status', 'recorded')}")
    ),
    "ENRICHMENT_DECISION": lambda e, d: f"{_row(e)}ZoomInfo match: {d.get('decision')}",
    "GATE_EVALUATED": lambda e, d: (
        f"Send checks {'passed' if d.get('passed') else 'failed'} " + f"({d.get('where', '?')})"
    ),
    "SEND_CONFIRMED": lambda e, d: (
        f"Retry of {_n(len(d.get('row_ids', [])), 'failed row', 'failed rows')} confirmed"
        if d.get("retry")
        else f"Send of {_n(int(d.get('rows_to_send') or 0), 'lead', 'leads')} confirmed"
    ),
    "ROW_SUBMITTED": lambda e, d: f"{_row(e)}submitted to Eloqua (attempt {d.get('attempt')})",
    "ROW_SEND_FAILED": lambda e, d: (
        f"{_row(e)}"
        + (
            "no answer from Eloqua"
            if d.get("outcome") == "no_response"
            else f"send failed (HTTP {d.get('http_status')})"
        )
    ),
    "JOB_STATE_CHANGED": lambda e, d: (
        f"Moved from {_state((e.get('before') or {}).get('state'))}"
        + f" to {_state((e.get('after') or {}).get('state'))}"
    ),
    "PROCESSED_FILE_DOWNLOADED": lambda e, d: "Processed file downloaded",
    "ADMIN_CONFIG_CHANGED": lambda e, d: (
        f"Admin changed {d.get('setting', 'settings')}"
        + (f": {d['change']}" if d.get("change") else "")
    ),
    "ACCESS_DENIED": lambda e, d: f"Access denied to {d.get('route', 'a page')}",
    "AUDIT_EXPORTED": lambda e, d: (
        "Audit search exported (" + _n(int(d.get("events") or 0), "event", "events") + ")"
    ),
}


def summarize(event: Mapping[str, Any]) -> str:
    fn = _SUMMARIES.get(str(event.get("event_type")))
    if fn is None:
        return str(event.get("event_type", "Event")).replace("_", " ").capitalize()
    try:
        return fn(event, _details(event))
    except (KeyError, TypeError, ValueError):
        return str(event.get("event_type")).replace("_", " ").capitalize()


def event_view(event: Mapping[str, Any]) -> dict[str, Any]:
    actor = event.get("actor") or {}
    return {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "occurred_at": event.get("occurred_at"),
        "job_id": event.get("job_id"),
        "row_id": event.get("row_id"),
        "actor": {"type": actor.get("type"), "email": actor.get("email")},
        "summary": summarize(event),
        "reason": event.get("reason"),
        "subject": event.get("subject"),
        "before": event.get("before"),
        "after": event.get("after"),
        "details": event.get("details"),
    }

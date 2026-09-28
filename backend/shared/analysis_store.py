"""Glue between stored jobs/rows and the pure evaluation in `analysis.py`.

Used by both the analyze workflow and the BFF's row edits, so a row edited in
review is re-validated with exactly the context the analysis used.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from shared.analysis import AnalysisContext, CampaignInfo, LeadSourceResolution, RowEvaluation
from shared.audit import Actor, AuditEvent, EventType
from shared.normalizer import Normalizer
from shared.workato_client import Campaign

# Row attributes owned by analysis (rewritten on every evaluation).
EVALUATED_KEYS = ("processed", "provenance", "issues", "status")


def campaign_info(c: Campaign) -> CampaignInfo:
    statuses = sorted(c.member_statuses, key=lambda s: s.sort_order)
    default = next((s.label for s in statuses if s.is_default), None)
    return CampaignInfo(
        id=c.id,
        found=c.found,
        name=c.name,
        type=c.type,
        is_active=c.is_active,
        statuses=tuple(s.label for s in statuses),
        default_status=default,
    )


def mapping_from_job(job: Mapping[str, Any]) -> dict[str, str]:
    confirmed = job.get("mapping_confirmed") or {}
    return {
        c["source_header"]: c["field_key"]
        for c in confirmed.get("columns", [])
        if c.get("field_key")
    }


def build_context(job: Mapping[str, Any], normalizer: Normalizer) -> AnalysisContext:
    stored = job.get("analysis_context") or {}
    return AnalysisContext(
        mapping=mapping_from_job(job),
        campaigns={k: CampaignInfo.from_dict(v) for k, v in stored.get("campaigns", {}).items()},
        lead_sources=tuple(stored.get("lead_sources", ())),
        lead_source_resolutions={
            k: LeadSourceResolution.from_dict(v)
            for k, v in stored.get("lead_source_resolutions", {}).items()
        },
        thresholds={k: float(v) for k, v in stored.get("thresholds", {}).items()},
        enrich=bool(job.get("enrich")),
        normalizer=normalizer,
        owner_email=job["owner_email"],
        list_date=stored.get("list_date", str(job.get("created_at", ""))[:10]),
        enrichment_done="enrichment_completed_at" in job,
    )


def row_item(row: Mapping[str, Any], ev: RowEvaluation, analyzed_at: str) -> dict[str, Any]:
    return {
        **row,
        "processed": ev.processed,
        "provenance": ev.provenance,
        "issues": [i.as_dict() for i in ev.issues],
        "status": ev.status,
        "analyzed_at": analyzed_at,
    }


def _issue_keys(issues: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {f"{i['code']}:{i.get('field')}": i for i in issues}


def row_events(
    job_id: str,
    previous: Mapping[str, Any],
    ev: RowEvaluation,
    actor: Actor,
    *,
    reason: str | None = None,
    correlation_id: str | None = None,
    details_for: Mapping[EventType, dict[str, Any]] | None = None,
) -> list[AuditEvent]:
    """Audit events explaining how this evaluation differs from the row's last one.

    One event per kind per row (fields grouped), not per field, to keep a
    5,000-row analysis to a few events per row. Every event carries the lead's
    email for the hashed person lookup.
    """
    row_id = int(previous["row_id"])
    email = ev.processed.get("email") or None
    old_processed: Mapping[str, str] = previous.get("processed") or {}
    changed = {k: v for k, v in ev.processed.items() if old_processed.get(k) != v}
    groups: dict[EventType, dict[str, str]] = {}
    for key, value in changed.items():
        how = ev.provenance.get(key, "")
        if how == "normalized":
            groups.setdefault(EventType.VALUE_NORMALIZED, {})[key] = value
        elif how.startswith("auto_corrected"):
            groups.setdefault(EventType.VALUE_AUTO_CORRECTED, {})[key] = value
        elif how.startswith("derived"):
            groups.setdefault(EventType.VALUE_DERIVED, {})[key] = value
        elif how.startswith("enrichment"):
            groups.setdefault(EventType.ENRICHMENT_RESULT, {})[key] = value
    extra = dict(details_for or {})
    # A details-only event (e.g. an enrichment with no match) still gets recorded.
    for event_type in extra:
        groups.setdefault(event_type, {})
    events: list[AuditEvent] = []
    for event_type, after in groups.items():
        events.append(
            AuditEvent(
                event_type=event_type,
                actor=actor,
                job_id=job_id,
                row_id=row_id,
                subject={"fields": sorted(after)},
                before={k: old_processed.get(k) for k in after} if after else None,
                after=after or None,
                reason=reason or ",".join(sorted({ev.provenance[k] for k in after})) or None,
                details=extra.get(event_type),
                lead_email=email,
                correlation_id=correlation_id,
            )
        )

    old_issues = _issue_keys(previous.get("issues") or [])
    new_issues = _issue_keys([i.as_dict() for i in ev.issues])
    raised = [new_issues[k] for k in new_issues if k not in old_issues]
    cleared = [old_issues[k] for k in old_issues if k not in new_issues]
    for event_type, items in ((EventType.ISSUE_RAISED, raised), (EventType.ISSUE_CLEARED, cleared)):
        if items:
            events.append(
                AuditEvent(
                    event_type=event_type,
                    actor=actor,
                    job_id=job_id,
                    row_id=row_id,
                    details={
                        "issues": [
                            {
                                "code": i["code"],
                                "field": i.get("field"),
                                "severity": i["severity"],
                                "source": i.get("source"),
                            }
                            for i in items
                        ]
                    },
                    reason=reason,
                    lead_email=email,
                    correlation_id=correlation_id,
                )
            )
    return events

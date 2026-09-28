"""AnalyzeWorkflow steps (SPEC §5.2). One Lambda, dispatched on `event["step"]`.

    prepare      validate campaign IDs against Salesforce (via Workato), resolve
                 lead source values (rules, then AI), store the context on the job,
                 and return batches of row IDs for the junk check
    junk_batch   AI junk check for up to 100 rows (Map state, several in parallel)
    finalize     evaluate every row, flag duplicates, write rows and their audit
                 events, then ANALYZING -> ANALYSIS_REVIEW
    fail         ANALYZING -> FAILED with a user-facing message

Every step is safe to retry: each one starts from what's stored.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shared import messages
from shared.ai_checks import JUNK_BATCH_SIZE, junk_check, match_lead_sources
from shared.analysis import (
    AnalysisContext,
    LeadSourceResolution,
    apply_duplicates,
    evaluate_row,
    lead_source_key,
    resolve_lead_source_rules,
    summarize,
)
from shared.analysis_store import build_context, campaign_info, row_events, row_item
from shared.audit import Actor, AuditEvent, AuditWriter, EventType
from shared.bedrock_client import BedrockClient
from shared.config_store import ConfigStore
from shared.fake_ai import HeuristicFakeBedrock
from shared.fake_sfdc import CAMPAIGNS
from shared.jobs import JobRepo, JobState, now_iso
from shared.normalizer import Normalizer, load_normalizer
from shared.observability import logger, metrics, tracer
from shared.rows import RowRepo
from shared.sfdc_ids import check_campaign_id
from shared.workato_client import MAX_CAMPAIGN_IDS, FakeWorkatoClient, WorkatoClient

LEAD_SOURCE_AI_BATCH = 100
AUDIT_WRITERS = 16


@dataclass
class AnalyzeDeps:
    jobs: JobRepo
    rows: RowRepo
    workato: WorkatoClient
    bedrock: BedrockClient
    config: ConfigStore
    normalizer: Normalizer = field(default_factory=load_normalizer)
    today: Callable[[], str] = field(default=lambda: datetime.now(UTC).date().isoformat())

    @property
    def audit(self) -> AuditWriter:
        return self.jobs.audit

    @classmethod
    def from_env(cls) -> AnalyzeDeps:
        fake = os.environ.get("INTEGRATIONS", "fake") == "fake"
        if not fake:
            raise RuntimeError("only INTEGRATIONS=fake is wired up in this build")
        audit = AuditWriter()
        return cls(
            jobs=JobRepo(audit),
            rows=RowRepo(),
            workato=FakeWorkatoClient(env=os.environ.get("ENV", "dev"), campaigns=dict(CAMPAIGNS)),
            bedrock=HeuristicFakeBedrock(),
            config=ConfigStore(),
        )


_deps: AnalyzeDeps | None = None
SYSTEM = Actor.system()


def _analyzing_job(deps: AnalyzeDeps, job_id: str) -> dict[str, Any] | None:
    job = deps.jobs.get(job_id)
    if job is None or job["state"] != JobState.ANALYZING:
        logger.warning("analyze step skipped", extra={"job_id": job_id})
        return None
    return job


# --- prepare ---------------------------------------------------------------------------


def prepare(job_id: str, deps: AnalyzeDeps) -> dict[str, Any]:
    job = _analyzing_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "batches": []}
    rows = deps.rows.list(job_id)
    # Settings come from the snapshot taken when analysis started (ANALYSIS_STARTED),
    # so a config edit mid-run can't make one analysis use two rule sets.
    snapshot = job.get("analysis_snapshot") or {}
    active = tuple(snapshot.get("lead_sources") or deps.config.lead_sources().active)
    thresholds = snapshot.get("thresholds") or deps.config.thresholds()

    # A first pass with no campaigns yet gives each row's processed campaign ID
    # and lead source (normalized, user edits applied).
    base = build_context(job, deps.normalizer)
    base.lead_sources = active
    first_pass = [evaluate_row(r, base) for r in rows]

    ids = sorted(
        {
            check.value
            for ev in first_pass
            if (check := check_campaign_id(ev.processed.get("campaign_id", ""))).value
        }
    )
    campaigns = {}
    for start in range(0, len(ids), MAX_CAMPAIGN_IDS):
        for c in deps.workato.lookup_campaigns(
            ids[start : start + MAX_CAMPAIGN_IDS], caller_job_id=job_id
        ):
            campaigns[c.id] = campaign_info(c)
    events: list[AuditEvent] = [
        AuditEvent(
            event_type=EventType.CAMPAIGN_VALIDATED,
            actor=SYSTEM,
            job_id=job_id,
            details={
                "requested": ids,
                "found": [i for i in ids if campaigns[i].found],
                "not_found": [i for i in ids if not campaigns[i].found],
                "inactive": [i for i in ids if campaigns[i].is_active is False],
            },
        )
    ]

    # Lead sources: rules first; distinct leftovers go to the AI in batches.
    unresolved: dict[str, str] = {}
    for ev in first_pass:
        value = ev.processed.get("lead_source", "")
        if value and resolve_lead_source_rules(value, active) is None:
            unresolved.setdefault(lead_source_key(value), value)
    resolutions: dict[str, LeadSourceResolution] = {}
    values = list(unresolved.values())
    for start in range(0, len(values), LEAD_SOURCE_AI_BATCH):
        batch = values[start : start + LEAD_SOURCE_AI_BATCH]
        found, llm = match_lead_sources(batch, active, deps.bedrock)
        resolutions.update(found)
        events.append(
            AuditEvent(
                event_type=EventType.AI_INVOCATION,
                actor=SYSTEM,
                job_id=job_id,
                details=llm.audit_details(row_count=len(batch)),
            )
        )

    context = {
        "campaigns": {k: v.as_dict() for k, v in campaigns.items()},
        "campaigns_validated_at": now_iso(),
        "lead_sources": list(active),
        "lead_source_resolutions": {k: v.as_dict() for k, v in resolutions.items()},
        "thresholds": thresholds,
        "normalizer_version": deps.normalizer.version,
        "list_date": deps.today(),
    }
    deps.jobs.update_in_state(
        job_id, JobState.ANALYZING, set_fields={"analysis_context": context}, events=events
    )
    candidates = [r["row_id"] for r in rows if not r.get("excluded")]
    batches = [
        candidates[i : i + JUNK_BATCH_SIZE] for i in range(0, len(candidates), JUNK_BATCH_SIZE)
    ]
    logger.info(
        "analysis prepared",
        extra={"job_id": job_id, "rows": len(rows), "campaigns": len(ids), "batches": len(batches)},
    )
    return {"job_id": job_id, "batches": batches}


# --- junk batch -------------------------------------------------------------------------


def junk_batch(job_id: str, row_ids: Sequence[int], deps: AnalyzeDeps) -> dict[str, Any]:
    job = _analyzing_job(deps, job_id)
    if job is None or not row_ids:
        return {"flagged": 0, "degraded": False}
    ctx = build_context(job, deps.normalizer)
    rows = deps.rows.get_many(job_id, row_ids)
    evaluated = [(r["row_id"], evaluate_row(r, ctx).processed) for r in rows]
    result = junk_check(evaluated, deps.bedrock)
    deps.audit.write(
        AuditEvent(
            event_type=EventType.AI_INVOCATION,
            actor=SYSTEM,
            job_id=job_id,
            details=result.llm.audit_details(row_count=len(evaluated)),
        )
    )
    deps.rows.set_ai_flags(job_id, result.flags_by_row, "degraded" if result.degraded else "ok")
    flagged = sum(1 for flags in result.flags_by_row.values() if flags)
    return {"flagged": flagged, "degraded": result.degraded}


# --- finalize ---------------------------------------------------------------------------


def _write_events(deps: AnalyzeDeps, events: Sequence[AuditEvent]) -> None:
    """Parallel PutItem (batch writes are denied on AuditEvents). Fails on the first error."""
    with ThreadPoolExecutor(max_workers=AUDIT_WRITERS) as pool:
        for _ in pool.map(deps.audit.write, events):
            pass


def finalize(job_id: str, deps: AnalyzeDeps) -> dict[str, Any]:
    job = _analyzing_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "state": "skipped"}
    ctx: AnalysisContext = build_context(job, deps.normalizer)
    rows = {r["row_id"]: r for r in deps.rows.list(job_id)}
    evaluations = {rid: evaluate_row(r, ctx) for rid, r in rows.items()}
    apply_duplicates(evaluations, {rid for rid, r in rows.items() if r.get("excluded")})

    analyzed_at = now_iso()
    events: list[AuditEvent] = []
    for rid, ev in evaluations.items():
        events.extend(row_events(job_id, rows[rid], ev, SYSTEM))
    # Record first, then show: rows only change once their events are stored.
    _write_events(deps, events)
    deps.rows.put_all(row_item(rows[rid], ev, analyzed_at) for rid, ev in evaluations.items())

    summary = summarize(ev.status for ev in evaluations.values())
    issue_counts = Counter(i.code for ev in evaluations.values() for i in ev.issues)
    degraded = sum(1 for r in rows.values() if r.get("ai_status") == "degraded")
    notes = [messages.ANALYSIS_AI_UNAVAILABLE] if degraded else []
    deps.jobs.transition(
        job_id,
        JobState.ANALYZING,
        JobState.ANALYSIS_REVIEW,
        actor=SYSTEM,
        set_fields={
            "summary": summary,
            "issue_counts": dict(issue_counts),
            "analysis_notes": notes,
            "analyzed_at": analyzed_at,
            "campaign_ids": sorted(ctx.campaigns),
        },
        remove_fields=("last_error",),
        details={"summary": summary},
    )

    total = max(1, summary["rows_total"])
    metrics.add_metric(name="RowsPerJob", unit="Count", value=summary["rows_total"])
    metrics.add_metric(
        name="PctRowsBlockedFirstAnalysis",
        unit="Percent",
        value=100.0 * summary["rows_blocked"] / total,
    )
    for code, n in issue_counts.items():
        metrics.add_metric(name=f"Issues_{code}", unit="Count", value=n)
    logger.info("analysis finished", extra={"job_id": job_id, **summary})
    return {"job_id": job_id, "state": JobState.ANALYSIS_REVIEW, "summary": summary}


def fail(job_id: str, deps: AnalyzeDeps, error: str | None = None) -> dict[str, Any]:
    job = _analyzing_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "state": "skipped"}
    logger.error("analysis failed", extra={"job_id": job_id, "error_type": error})
    deps.jobs.transition(
        job_id,
        JobState.ANALYZING,
        JobState.FAILED,
        actor=SYSTEM,
        set_fields={"last_error": {"stage": "analysis", "message": messages.ANALYSIS_FAILED}},
        details={"error_type": error},
    )
    metrics.add_metric(name="AnalysisFailures", unit="Count", value=1)
    return {"job_id": job_id, "state": JobState.FAILED}


def run_all(job_id: str, deps: AnalyzeDeps) -> str:
    """The whole workflow in-process (tests, local runs). Mirrors the state machine."""
    try:
        prepared = prepare(job_id, deps)
        for batch in prepared["batches"]:
            junk_batch(job_id, batch, deps)
        return str(finalize(job_id, deps)["state"])
    except Exception as exc:
        fail(job_id, deps, type(exc).__name__)
        return JobState.FAILED


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _deps
    if _deps is None:
        _deps = AnalyzeDeps.from_env()
    job_id = event["job_id"]
    logger.append_keys(job_id=job_id)
    step = event["step"]
    if step == "prepare":
        return prepare(job_id, _deps)
    if step == "junk_batch":
        return junk_batch(job_id, event["row_ids"], _deps)
    if step == "finalize":
        return finalize(job_id, _deps)
    if step == "fail":
        cause = event.get("error", {})
        return fail(job_id, _deps, cause.get("Error") if isinstance(cause, dict) else None)
    raise ValueError(f"unknown step {step!r}")

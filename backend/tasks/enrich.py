"""EnrichWorkflow steps (SPEC §5.2, §15). One Lambda, dispatched on `event["step"]`.

prepare        pick eligible rows (§15.2) and split them into batches of 25
enrich_batch   one provider call per batch (Map state, 2 in parallel); a batch
               that still fails after 3 tries marks its rows `error` and the
               job carries on (§15.5)
finalize       re-evaluate every row with enrichment applied, write rows and
               audit events, then ENRICHING -> ENRICHMENT_REVIEW
fail           ENRICHING -> FAILED with a user-facing message
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from shared import messages
from shared.analysis import apply_duplicates, evaluate_row, summarize
from shared.analysis_store import build_context, row_events, row_item
from shared.audit import Actor, AuditEvent, AuditWriter, EventType
from shared.enrichment import EnrichInput, EnrichmentProvider, ZoomInfoProvider, eligible
from shared.fake_sfdc import CAMPAIGNS
from shared.fake_zoominfo import enrich_handler
from shared.jobs import JobRepo, JobState, now_iso
from shared.normalizer import Normalizer, load_normalizer
from shared.observability import logger, metrics, tracer
from shared.rows import RowRepo
from shared.workato_client import FakeWorkatoClient

AUDIT_WRITERS = 16
SYSTEM = Actor.system()


@dataclass
class EnrichDeps:
    jobs: JobRepo
    rows: RowRepo
    provider: EnrichmentProvider
    normalizer: Normalizer = field(default_factory=load_normalizer)

    @property
    def audit(self) -> AuditWriter:
        return self.jobs.audit

    @classmethod
    def from_env(cls) -> EnrichDeps:
        if os.environ.get("INTEGRATIONS", "fake") != "fake":
            raise RuntimeError("only INTEGRATIONS=fake is wired up in this build")
        audit = AuditWriter()
        workato = FakeWorkatoClient(
            env=os.environ.get("ENV", "dev"),
            campaigns=dict(CAMPAIGNS),
            enrich_handler=enrich_handler,
        )
        return cls(jobs=JobRepo(audit), rows=RowRepo(), provider=ZoomInfoProvider(workato))


_deps: EnrichDeps | None = None


def _enriching_job(deps: EnrichDeps, job_id: str) -> dict[str, Any] | None:
    job = deps.jobs.get(job_id)
    if job is None or job["state"] != JobState.ENRICHING:
        logger.warning("enrich step skipped", extra={"job_id": job_id})
        return None
    return job


def prepare(job_id: str, deps: EnrichDeps) -> dict[str, Any]:
    job = _enriching_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "batches": []}
    ids = [r["row_id"] for r in deps.rows.list(job_id) if eligible(r)]
    size = deps.provider.max_batch
    batches = [ids[i : i + size] for i in range(0, len(ids), size)]
    logger.info(
        "enrichment prepared", extra={"job_id": job_id, "rows": len(ids), "batches": len(batches)}
    )
    return {"job_id": job_id, "batches": batches}


def enrich_batch(job_id: str, row_ids: Sequence[int], deps: EnrichDeps) -> dict[str, Any]:
    job = _enriching_job(deps, job_id)
    if job is None or not row_ids:
        return {"rows": 0}
    ctx = build_context(job, deps.normalizer)
    rows = deps.rows.get_many(job_id, row_ids)
    inputs = [EnrichInput(r["row_id"], evaluate_row(r, ctx).processed) for r in rows]
    results = deps.provider.enrich(inputs, job_id=job_id)
    outcome = Counter(r.status for r in results)
    # Record the request before storing its results (fail closed).
    deps.audit.write(
        AuditEvent(
            event_type=EventType.ENRICHMENT_REQUESTED,
            actor=SYSTEM,
            job_id=job_id,
            details={
                "provider": deps.provider.name,
                "row_ids": [r.row_id for r in inputs],
                "outcome": dict(outcome),
            },
        )
    )
    at = now_iso()
    deps.rows.set_enrichment(
        job_id,
        {
            r.row_id: {**r.as_dict(), "provider": deps.provider.name, "attempted_at": at}
            for r in results
        },
    )
    metrics.add_metric(name="EnrichmentContactsSent", unit="Count", value=len(inputs))
    if outcome.get("error"):
        metrics.add_metric(name="EnrichmentBatchErrors", unit="Count", value=1)
    return {"rows": len(inputs), "outcome": dict(outcome)}


def enrichment_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter((r.get("enrichment") or {}).get("status") for r in rows)
    filled: Counter[str] = Counter()
    linkedin = 0
    for r in rows:
        for key, how in (r.get("provenance") or {}).items():
            if how == "enrichment:zoominfo":
                filled[key] += 1
                if key == "linkedin_url":
                    linkedin += 1
    attempted = sum(statuses[s] for s in ("accepted", "review", "no_match", "error"))
    return {
        "sent": attempted,
        "accepted": statuses["accepted"],
        "needs_review": statuses["review"],
        "no_match": statuses["no_match"],
        "errors": statuses["error"],
        "fields_filled": dict(filled),
        "linkedin_found": linkedin,
    }


def _write_events(deps: EnrichDeps, events: Sequence[AuditEvent]) -> None:
    with ThreadPoolExecutor(max_workers=AUDIT_WRITERS) as pool:
        for _ in pool.map(deps.audit.write, events):
            pass


def finalize(job_id: str, deps: EnrichDeps) -> dict[str, Any]:
    job = _enriching_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "state": "skipped"}
    ctx = build_context(job, deps.normalizer)
    ctx.enrichment_done = True
    rows = {r["row_id"]: r for r in deps.rows.list(job_id)}
    for r in rows.values():
        r.setdefault("enrichment", {"status": "not_attempted"})
    evaluations = {rid: evaluate_row(r, ctx) for rid, r in rows.items()}
    apply_duplicates(evaluations, {rid for rid, r in rows.items() if r.get("excluded")})

    events: list[AuditEvent] = []
    for rid, ev in evaluations.items():
        enr = rows[rid]["enrichment"]
        details = None
        if enr.get("status") != "not_attempted":
            details = {
                EventType.ENRICHMENT_RESULT: {
                    "provider": enr.get("provider"),
                    "status": enr.get("status"),
                    "match_status": enr.get("match_status"),
                    "score": enr.get("score"),
                    "conflicts": enr.get("conflicts", []),
                    "fields_filled": sorted(
                        k for k, how in ev.provenance.items() if how == "enrichment:zoominfo"
                    ),
                    "notes": enr.get("notes", []),
                }
            }
        events.extend(row_events(job_id, rows[rid], ev, SYSTEM, details_for=details))
    _write_events(deps, events)
    analyzed_at = now_iso()
    items = [row_item(rows[rid], ev, analyzed_at) for rid, ev in evaluations.items()]
    deps.rows.put_all(items)

    summary = summarize(ev.status for ev in evaluations.values())
    enrichment = enrichment_summary(items)
    notes = list(job.get("analysis_notes") or [])
    if enrichment["errors"]:
        notes.append(messages.ENRICHMENT_BATCH_ERRORS.format(n=enrichment["errors"]))
    deps.jobs.transition(
        job_id,
        JobState.ENRICHING,
        JobState.ENRICHMENT_REVIEW,
        actor=SYSTEM,
        set_fields={
            "summary": summary,
            "enrichment_summary": enrichment,
            "enrichment_completed_at": analyzed_at,
            "analysis_notes": notes,
        },
        remove_fields=("last_error",),
        details={"summary": summary, "enrichment": enrichment},
    )
    sent = max(1, enrichment["sent"])
    for key in ("accepted", "needs_review", "no_match"):
        metrics.add_metric(
            name=f"EnrichmentPct_{key}", unit="Percent", value=100.0 * enrichment[key] / sent
        )
    for key, n in enrichment["fields_filled"].items():
        metrics.add_metric(name=f"EnrichmentFilled_{key}", unit="Count", value=n)
    metrics.add_metric(name="JobsReachedEnrichmentReview", unit="Count", value=1)
    return {"job_id": job_id, "state": JobState.ENRICHMENT_REVIEW, "enrichment": enrichment}


def fail(job_id: str, deps: EnrichDeps, error: str | None = None) -> dict[str, Any]:
    job = _enriching_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "state": "skipped"}
    logger.error("enrichment failed", extra={"job_id": job_id, "error_type": error})
    deps.jobs.transition(
        job_id,
        JobState.ENRICHING,
        JobState.FAILED,
        actor=SYSTEM,
        set_fields={"last_error": {"stage": "enrichment", "message": messages.ENRICHMENT_FAILED}},
        details={"error_type": error},
    )
    return {"job_id": job_id, "state": JobState.FAILED}


def run_all(job_id: str, deps: EnrichDeps) -> str:
    """The whole workflow in-process (tests, local runs). Mirrors the state machine."""
    try:
        for batch in prepare(job_id, deps)["batches"]:
            enrich_batch(job_id, batch, deps)
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
        _deps = EnrichDeps.from_env()
    job_id = event["job_id"]
    logger.append_keys(job_id=job_id)
    step = event["step"]
    if step == "prepare":
        return prepare(job_id, _deps)
    if step == "enrich_batch":
        return enrich_batch(job_id, event["row_ids"], _deps)
    if step == "finalize":
        return finalize(job_id, _deps)
    if step == "fail":
        cause = event.get("error", {})
        return fail(job_id, _deps, cause.get("Error") if isinstance(cause, dict) else None)
    raise ValueError(f"unknown step {step!r}")

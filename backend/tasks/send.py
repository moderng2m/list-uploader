"""SendWorkflow steps (SPEC §5.2, §16). One Lambda, dispatched on `event["step"]`.

    prepare      re-check the gate on the server (SPEC §17) and split the rows to send
                 into batches of 25 (only failed rows when retrying)
    send_batch   post each row (Map state, MaxConcurrency 5): claim -> post -> record
    finalize     count results -> COMPLETED or COMPLETED_WITH_ERRORS
    fail         SENDING -> FAILED with a user-facing message

Each Map item carries 25 rows instead of one, which keeps a 5,000-row job under
the Step Functions execution-history limit; with 5 items in flight, at most 5
posts run at once, as the spec intends.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from shared import messages
from shared.audit import Actor, AuditEvent, AuditWriter, EventType
from shared.fake_sfdc import CAMPAIGNS
from shared.jobs import JobRepo, JobState, now_iso
from shared.observability import logger, metrics, tracer
from shared.rows import RowRepo
from shared.sending import build_payload, evaluate_gate, payload_sha256, sendable_row_ids
from shared.workato_client import (
    FakeWorkatoClient,
    SendToProdViolation,
    WorkatoClient,
    assert_send_to_prod_allowed,
)

SEND_BATCH = 25
SYSTEM = Actor.system()


class GateRejected(RuntimeError):
    """The server-side gate failed inside the workflow. Should never happen after the
    BFF's own check; alarmed on (SPEC §21.3.4)."""


@dataclass
class SendDeps:
    jobs: JobRepo
    rows: RowRepo
    workato: WorkatoClient
    env: str
    send_to_prod: bool
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def audit(self) -> AuditWriter:
        return self.jobs.audit

    @classmethod
    def from_env(cls) -> SendDeps:
        if os.environ.get("INTEGRATIONS", "fake") != "fake":
            raise RuntimeError("only INTEGRATIONS=fake is wired up in this build")
        env = os.environ.get("ENV", "dev")
        audit = AuditWriter()
        return cls(
            jobs=JobRepo(audit),
            rows=RowRepo(),
            workato=FakeWorkatoClient(env=env, campaigns=dict(CAMPAIGNS)),
            env=env,
            send_to_prod=os.environ.get("SEND_TO_PROD", "false") == "true",
        )


_deps: SendDeps | None = None


def _sending_job(deps: SendDeps, job_id: str) -> dict[str, Any] | None:
    job = deps.jobs.get(job_id)
    if job is None or job["state"] != JobState.SENDING:
        logger.warning("send step skipped", extra={"job_id": job_id})
        return None
    return job


def prepare(job_id: str, deps: SendDeps, *, only_failed: bool = False) -> dict[str, Any]:
    job = _sending_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "batches": []}
    rows = deps.rows.list(job_id)
    if not only_failed:
        gate = evaluate_gate(job, rows, deps.now())
        deps.audit.write(
            AuditEvent(
                event_type=EventType.GATE_EVALUATED,
                actor=SYSTEM,
                job_id=job_id,
                details={
                    "where": "workflow",
                    "passed": gate.passed,
                    "reason_codes": gate.reason_codes,
                    **gate.confirmation(),
                },
            )
        )
        if not gate.passed:
            metrics.add_metric(name="GateRejectedInWorkflow", unit="Count", value=1)
            raise GateRejected(",".join(gate.reason_codes))
    ids = sendable_row_ids(rows, only_failed=only_failed)
    batches = [ids[i : i + SEND_BATCH] for i in range(0, len(ids), SEND_BATCH)]
    logger.info(
        "send prepared", extra={"job_id": job_id, "rows": len(ids), "only_failed": only_failed}
    )
    return {"job_id": job_id, "batches": batches}


def send_one(job_id: str, row: dict[str, Any], deps: SendDeps) -> str:
    """Returns submitted | failed | unconfirmed | skipped."""
    payload = build_payload(job_id, row, send_to_prod=deps.send_to_prod)
    # Refuse before any state changes if a non-prod env would send to prod.
    assert_send_to_prod_allowed(payload, deps.env)
    digest = payload_sha256(payload)
    attempt = int((row.get("send") or {}).get("attempts") or 0) + 1
    started = now_iso()
    if not deps.rows.claim_send(
        job_id, row["row_id"], attempt=attempt, payload_sha256=digest, at=started
    ):
        return "skipped"  # another execution has it, or it was already submitted

    email = (row.get("processed") or {}).get("email")
    t0 = time.monotonic()
    try:
        result = deps.workato.post_to_eloqua(payload)
    except SendToProdViolation:
        raise
    except Exception as exc:
        # No answer: the post may have landed. Leave the row `sending` for an admin
        # (SPEC §16.3); never auto-retry it.
        send = {
            "status": "sending",
            "attempts": attempt,
            "payload_sha256": digest,
            "sending_at": started,
            "last_error": f"no response ({type(exc).__name__})",
        }
        deps.audit.transact(
            [
                AuditEvent(
                    event_type=EventType.ROW_SEND_FAILED,
                    actor=SYSTEM,
                    job_id=job_id,
                    row_id=row["row_id"],
                    lead_email=email,
                    details={
                        "outcome": "no_response",
                        "attempt": attempt,
                        "payload_sha256": digest,
                        "payload": payload,
                        "error_type": type(exc).__name__,
                    },
                )
            ],
            [deps.rows.send_result_write(job_id, row["row_id"], attempt=attempt, send=send)],
        )
        metrics.add_metric(name="RowsSendUnconfirmed", unit="Count", value=1)
        return "unconfirmed"

    latency_ms = int((time.monotonic() - t0) * 1000)
    done = now_iso()
    send = {
        "status": "submitted" if result.ok else "failed",
        "attempts": attempt,
        "http_status": result.status_code,
        "workato_job_id": result.workato_job_id,
        "payload_sha256": digest,
        "sending_at": started,
        **({"submitted_at": done} if result.ok else {"error": result.error, "failed_at": done}),
    }
    deps.audit.transact(
        [
            AuditEvent(
                event_type=EventType.ROW_SUBMITTED if result.ok else EventType.ROW_SEND_FAILED,
                actor=SYSTEM,
                job_id=job_id,
                row_id=row["row_id"],
                lead_email=email,
                details={
                    "http_status": result.status_code,
                    "workato_job_id": result.workato_job_id,
                    "attempt": attempt,
                    "payload_sha256": digest,
                    "payload": payload,
                    "error": result.error,
                    "latency_ms": latency_ms,
                },
            )
        ],
        [deps.rows.send_result_write(job_id, row["row_id"], attempt=attempt, send=send)],
    )
    metrics.add_metric(
        name="RowsSubmitted" if result.ok else "RowsSendFailed", unit="Count", value=1
    )
    metrics.add_metric(name=f"WorkatoStatus_{result.status_code}", unit="Count", value=1)
    metrics.add_metric(name="SendLatencyMs", unit="Milliseconds", value=latency_ms)
    return str(send["status"])


def send_batch(job_id: str, row_ids: Sequence[int], deps: SendDeps) -> dict[str, Any]:
    job = _sending_job(deps, job_id)
    if job is None or not row_ids:
        return {"rows": 0}
    outcomes: Counter[str] = Counter()
    for row in deps.rows.get_many(job_id, row_ids):
        if row.get("excluded"):
            continue
        outcomes[send_one(job_id, row, deps)] += 1
    return {"rows": len(row_ids), "outcomes": dict(outcomes)}


def send_counts(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    sendable = [r for r in rows if not r.get("excluded")]
    statuses = Counter((r.get("send") or {}).get("status", "not_sent") for r in sendable)
    by_campaign: Counter[str] = Counter()
    for r in sendable:
        if (r.get("send") or {}).get("status") == "submitted":
            by_campaign[(r.get("processed") or {}).get("campaign_id", "")] += 1
    return {
        "submitted": statuses["submitted"],
        "failed": statuses["failed"],
        "unconfirmed": statuses["sending"],
        "not_sent": statuses["not_sent"],
        "submitted_by_campaign": dict(by_campaign),
    }


def finalize(job_id: str, deps: SendDeps) -> dict[str, Any]:
    job = _sending_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "state": "skipped"}
    counts = send_counts(deps.rows.list(job_id))
    clean = counts["failed"] == counts["unconfirmed"] == counts["not_sent"] == 0
    to_state = JobState.COMPLETED if clean else JobState.COMPLETED_WITH_ERRORS
    summary = {**counts, "completed_at": now_iso()}
    deps.jobs.transition(
        job_id,
        JobState.SENDING,
        to_state,
        actor=SYSTEM,
        set_fields={"send_summary": summary},
        remove_fields=("last_error",),
        details={"send": summary},
    )
    if counts["unconfirmed"]:
        metrics.add_metric(name="RowsStuckSending", unit="Count", value=counts["unconfirmed"])
    metrics.add_metric(name="JobsCompleted", unit="Count", value=1)
    logger.info(
        "send finished",
        extra={
            "job_id": job_id,
            "state": str(to_state),
            "submitted": counts["submitted"],
            "failed": counts["failed"],
        },
    )
    return {"job_id": job_id, "state": to_state, "send": summary}


def fail(job_id: str, deps: SendDeps, error: str | None = None) -> dict[str, Any]:
    job = _sending_job(deps, job_id)
    if job is None:
        return {"job_id": job_id, "state": "skipped"}
    logger.error("send failed", extra={"job_id": job_id, "error_type": error})
    counts = send_counts(deps.rows.list(job_id))
    deps.jobs.transition(
        job_id,
        JobState.SENDING,
        JobState.FAILED,
        actor=SYSTEM,
        set_fields={
            "last_error": {"stage": "send", "message": messages.SEND_FAILED},
            "send_summary": {**counts, "completed_at": now_iso()},
        },
        details={"error_type": error, "send": counts},
    )
    metrics.add_metric(name="SendWorkflowFailed", unit="Count", value=1)
    return {"job_id": job_id, "state": JobState.FAILED}


def run_all(job_id: str, deps: SendDeps, *, only_failed: bool = False) -> str:
    """The whole workflow in-process (tests, local runs). Mirrors the state machine."""
    try:
        for batch in prepare(job_id, deps, only_failed=only_failed)["batches"]:
            send_batch(job_id, batch, deps)
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
        _deps = SendDeps.from_env()
    job_id = event["job_id"]
    logger.append_keys(job_id=job_id)
    step = event["step"]
    if step == "prepare":
        return prepare(job_id, _deps, only_failed=bool(event.get("only_failed")))
    if step == "send_batch":
        return send_batch(job_id, event["row_ids"], _deps)
    if step == "finalize":
        return finalize(job_id, _deps)
    if step == "fail":
        cause = event.get("error", {})
        return fail(job_id, _deps, cause.get("Error") if isinstance(cause, dict) else None)
    raise ValueError(f"unknown step {step!r}")

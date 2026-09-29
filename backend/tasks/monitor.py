"""Scheduled health check (every 5 minutes) for two alarms that no single request
can see (SPEC §21.3.4):

- StuckSendingRows: rows posted to Eloqua with no answer for more than 15 minutes,
  in jobs that changed in the last day (older ones have been reported already).
- JobsStuckRunning: jobs in a running state (parse, analysis, enrichment, send)
  for more than 60 minutes.

Both are published every run, zero included, so the alarms always have data.
Only job IDs are logged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from shared.audit import AuditWriter
from shared.jobs import RUNNING, JobRepo, JobState
from shared.observability import logger, metrics, tracer
from shared.rows import RowRepo

STUCK_SENDING = timedelta(minutes=15)
STUCK_RUNNING = timedelta(minutes=60)
RECENT = timedelta(days=1)
SEND_STATES = {JobState.SENDING, JobState.COMPLETED_WITH_ERRORS, JobState.FAILED}


def _at(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def check(jobs: JobRepo, rows: RowRepo, now: datetime) -> dict[str, Any]:
    stuck_jobs: list[str] = []
    stuck_rows = 0
    for job in jobs.list_all(limit=1_000_000):
        updated = _at(job.get("updated_at") or job.get("created_at"))
        if updated is None:
            continue
        if job["state"] in RUNNING and now - updated > STUCK_RUNNING:
            stuck_jobs.append(job["job_id"])
        if job["state"] in SEND_STATES and now - updated < RECENT:
            for row in rows.list(job["job_id"]):
                send = row.get("send") or {}
                since = _at(send.get("sending_at"))
                if send.get("status") == "sending" and since and now - since > STUCK_SENDING:
                    stuck_rows += 1
    return {"stuck_jobs": stuck_jobs, "stuck_sending_rows": stuck_rows}


_deps: tuple[JobRepo, RowRepo] | None = None


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _deps
    if _deps is None:
        _deps = (JobRepo(AuditWriter()), RowRepo())
    result = check(*_deps, datetime.now(UTC))
    metrics.add_metric(name="JobsStuckRunning", unit="Count", value=len(result["stuck_jobs"]))
    metrics.add_metric(name="StuckSendingRows", unit="Count", value=result["stuck_sending_rows"])
    if result["stuck_jobs"] or result["stuck_sending_rows"]:
        logger.warning(
            "stuck work",
            extra={"job_ids": result["stuck_jobs"], "rows": result["stuck_sending_rows"]},
        )
    return result

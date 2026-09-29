"""Gate, send, retry, result and download routes (SPEC §6.5, §6.6, §16, §17, §19)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError

from bff.analysis_api import REVIEW_STATES, apply_changes
from bff.app import app, correlation_id, current_user, deps, json_body, load_job_for
from shared import messages
from shared.analysis_store import campaign_info
from shared.audit import AuditEvent, EventType
from shared.jobs import JobState, now_iso
from shared.observability import metrics
from shared.processed_file import build_csv
from shared.sending import evaluate_gate
from shared.workato_client import lookup_distinct

SEND_FROM = REVIEW_STATES | {JobState.READY_TO_SEND}
DOWNLOAD_URL_TTL = 300


def _json(status: int, body: Any) -> Response[Any]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


def _gate_event(job_id: str, user: Any, where: str, gate: Any) -> AuditEvent:
    return AuditEvent(
        event_type=EventType.GATE_EVALUATED,
        actor=user.actor(),
        job_id=job_id,
        details={
            "where": where,
            "passed": gate.passed,
            "reason_codes": gate.reason_codes,
            **gate.confirmation(),
        },
        correlation_id=correlation_id(),
    )


@app.get("/jobs/<job_id>/gate")
def get_gate(job_id: str) -> Any:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/gate")
    d = deps()
    gate = evaluate_gate(job, d.rows.list(job_id), datetime.now(UTC))
    d.audit.write(_gate_event(job_id, user, "ui", gate))
    return {
        **gate.as_dict(),
        "state": job["state"],
        "can_send": job["state"] in SEND_FROM,
        "send_to_prod": d.send_to_prod,
    }


@app.post("/jobs/<job_id>/revalidate-campaigns")
def revalidate_campaigns(job_id: str) -> Any:
    """Re-check every campaign against Salesforce and re-evaluate the rows (SPEC §17 item 3)."""
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/revalidate-campaigns")
    if job["state"] not in REVIEW_STATES:
        return _json(409, {"message": messages.JOB_STATE_CONFLICT})
    d = deps()
    context = dict(job.get("analysis_context") or {})
    ids = sorted((context.get("campaigns") or {}).keys())
    found = {
        cid: campaign_info(c).as_dict()
        for cid, c in lookup_distinct(d.workato, ids, caller_job_id=job_id).items()
    }
    context["campaigns"] = found
    context["campaigns_validated_at"] = now_iso()
    d.jobs.update_in_state(
        job_id,
        JobState(job["state"]),
        set_fields={"analysis_context": context},
        events=[
            AuditEvent(
                event_type=EventType.CAMPAIGN_VALIDATED,
                actor=user.actor(),
                job_id=job_id,
                details={
                    "requested": ids,
                    "found": [i for i in ids if found[i]["found"]],
                    "not_found": [i for i in ids if not found[i]["found"]],
                    "inactive": [i for i in ids if found[i]["is_active"] is False],
                    "reason": "pre_send_revalidation",
                },
                correlation_id=correlation_id(),
            )
        ],
    )
    job["analysis_context"] = context
    changed = apply_changes(
        job, {}, user, reevaluate_all=True, system_reason="campaign_revalidation"
    )
    return {"campaigns": len(ids), "rows_changed": sorted(changed)}


@app.post("/jobs/<job_id>/send")
def send(job_id: str) -> Response[Any]:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/send")
    state = JobState(job["state"])
    if state not in SEND_FROM:
        return _json(409, {"message": messages.JOB_STATE_CONFLICT})
    body = json_body()
    confirmed = body.get("confirmation")
    if not isinstance(confirmed, dict):
        raise BadRequestError("Confirm the summary before sending.")
    d = deps()
    gate = evaluate_gate(job, d.rows.list(job_id), datetime.now(UTC))
    gate_event = _gate_event(job_id, user, "server", gate)
    if not gate.passed:
        d.audit.write(gate_event)
        if body.get("ui_gate_passed"):
            # The browser showed the gate as passed: a bug or tampering (SPEC §21.3.4).
            metrics.add_metric(name="GateRejectedAfterUiPassed", unit="Count", value=1)
        return _json(409, {"message": messages.SEND_GATE_FAILED, "reasons": gate.reasons})
    if confirmed != gate.confirmation():
        d.audit.write(gate_event)
        return _json(409, {"message": messages.SEND_SUMMARY_CHANGED})

    if state != JobState.READY_TO_SEND:
        d.jobs.transition(
            job_id,
            state,
            JobState.READY_TO_SEND,
            actor=user.actor(),
            events=[gate_event],
            correlation_id=correlation_id(),
        )
    # A second click (or a second tab) fails here with 409: only one send starts.
    d.jobs.transition(
        job_id,
        JobState.READY_TO_SEND,
        JobState.SENDING,
        actor=user.actor(),
        set_fields={"send_confirmed_by": user.email, "send_confirmed_at": now_iso()},
        events=[
            AuditEvent(
                event_type=EventType.SEND_CONFIRMED,
                actor=user.actor(),
                job_id=job_id,
                details={
                    **gate.confirmation(),
                    "send_to_prod": d.send_to_prod,
                    "sent_fields": gate.sent_fields,
                    "not_sent_fields": gate.not_sent_fields,
                    "snapshot": job.get("analysis_snapshot"),
                },
                correlation_id=correlation_id(),
            )
        ],
        correlation_id=correlation_id(),
    )
    d.start_send(job_id, False)
    metrics.add_metric(name="JobsReachedSend", unit="Count", value=1)
    return _json(202, {"job_id": job_id, "state": JobState.SENDING})


@app.post("/jobs/<job_id>/retry-failed")
def retry_failed(job_id: str) -> Response[Any]:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/retry-failed")
    state = JobState(job["state"])
    after_failure = (
        state == JobState.FAILED and (job.get("last_error") or {}).get("stage") == "send"
    )
    if state != JobState.COMPLETED_WITH_ERRORS and not after_failure:
        return _json(409, {"message": messages.JOB_STATE_CONFLICT})
    d = deps()
    failed = [
        r["row_id"]
        for r in d.rows.list(job_id)
        if not r.get("excluded") and (r.get("send") or {}).get("status") == "failed"
    ]
    if not failed:
        return _json(409, {"message": messages.SEND_NO_FAILED_ROWS})
    d.jobs.transition(
        job_id,
        state,
        JobState.SENDING,
        actor=user.actor(),
        remove_fields=("last_error",),
        events=[
            AuditEvent(
                event_type=EventType.SEND_CONFIRMED,
                actor=user.actor(),
                job_id=job_id,
                details={"retry": True, "row_ids": failed, "send_to_prod": d.send_to_prod},
                correlation_id=correlation_id(),
            )
        ],
        correlation_id=correlation_id(),
    )
    d.start_send(job_id, True)
    metrics.add_metric(name="SendRetries", unit="Count", value=1)
    return _json(202, {"job_id": job_id, "state": JobState.SENDING, "row_ids": failed})


@app.get("/jobs/<job_id>/result")
def get_result(job_id: str) -> Any:
    job = load_job_for(current_user(), job_id, "/jobs/{id}/result")
    rows = deps().rows.list(job_id)
    campaigns = (job.get("analysis_context") or {}).get("campaigns") or {}
    summary = job.get("send_summary") or {}
    failed, unconfirmed = [], []
    for r in rows:
        send = r.get("send") or {}
        email = (r.get("processed") or {}).get("email", "")
        if r.get("excluded"):
            continue
        if send.get("status") == "failed":
            failed.append(
                {
                    "row_id": r["row_id"],
                    "email": email,
                    "reason": send.get("error") or f"HTTP {send.get('http_status')}",
                }
            )
        elif send.get("status") == "sending":
            unconfirmed.append(
                {"row_id": r["row_id"], "email": email, "reason": messages.SEND_UNCONFIRMED}
            )
    return {
        "state": job["state"],
        "submitted": summary.get("submitted", 0),
        "failed": failed,
        "unconfirmed": unconfirmed,
        "campaigns": [
            {"id": cid, "name": (campaigns.get(cid) or {}).get("name") or cid, "rows": n}
            for cid, n in sorted((summary.get("submitted_by_campaign") or {}).items())
        ],
        "submitted_at": summary.get("completed_at"),
        "submitted_by": job.get("send_confirmed_by"),
        "send_to_prod": deps().send_to_prod,
        "last_error": job.get("last_error"),
    }


@app.get("/jobs/<job_id>/download")
def download(job_id: str) -> Any:
    user = current_user()
    job = load_job_for(user, job_id, "/jobs/{id}/download")
    if "analyzed_at" not in job:
        return _json(409, {"message": messages.ANALYSIS_NOT_READY})
    d = deps()
    rows = d.rows.list(job_id)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"{job_id}/processed-{stamp}.csv"
    base = job["filename"].rsplit(".", 1)[0]
    filename = f"{base}-processed-{stamp[:8]}.csv"
    # A PII export: record it before handing out the link (SPEC §21.2.3).
    d.audit.write(
        AuditEvent(
            event_type=EventType.PROCESSED_FILE_DOWNLOADED,
            actor=user.actor(),
            job_id=job_id,
            details={"rows": len(rows), "s3_key": key},
            correlation_id=correlation_id(),
        )
    )
    d.s3.put_object(
        Bucket=d.processed_bucket,
        Key=key,
        Body=build_csv((job.get("parse") or {}).get("headers", []), rows),
        ContentType="text/csv; charset=utf-8",
        ContentDisposition=f'attachment; filename="{filename}"',
    )
    url = d.s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": d.processed_bucket, "Key": key},
        ExpiresIn=DOWNLOAD_URL_TTL,
    )
    return {"url": url, "filename": filename, "expires_in": DOWNLOAD_URL_TTL}

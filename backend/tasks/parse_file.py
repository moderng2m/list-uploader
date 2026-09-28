"""Parse an uploaded file into Rows (SPEC §6.1, §7.1). Invoked async by the BFF.

UPLOADED -> MAPPING_REVIEW on success, UPLOADED -> PARSE_FAILED otherwise.
Safe to re-run: it does nothing unless the job is still UPLOADED, and row
writes overwrite the same keys.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass
from typing import Any

import boto3

from shared import config_defaults, messages
from shared.audit import Actor, AuditEvent, AuditWriter, EventType
from shared.jobs import JobRepo, JobState
from shared.observability import logger, metrics, tracer
from shared.parsing import ParseError, ParseResult, parse_file
from shared.rows import RowRepo


@dataclass
class ParseDeps:
    jobs: JobRepo
    rows: RowRepo
    s3: Any
    uploads_bucket: str
    max_bytes: int = config_defaults.LIMITS["max_file_bytes"]
    max_rows: int = config_defaults.LIMITS["max_rows"]

    @classmethod
    def from_env(cls) -> ParseDeps:
        audit = AuditWriter()
        return cls(
            jobs=JobRepo(audit),
            rows=RowRepo(),
            s3=boto3.client("s3"),
            uploads_bucket=os.environ["UPLOADS_BUCKET"],
        )


_deps: ParseDeps | None = None


def _parse_summary(result: ParseResult) -> dict[str, Any]:
    return {
        "file_type": result.file_type,
        "sheet_name": result.sheet_name,
        "encoding": result.encoding,
        "delimiter": result.delimiter,
        "headers": result.headers,
        "row_count": len(result.rows),
        "column_count": len(result.headers),
        "warnings": [w.as_dict() for w in result.warnings],
        "row_issue_count": result.row_issue_count(),
    }


def run(job_id: str, deps: ParseDeps) -> str:
    """Returns the resulting state (or 'skipped')."""
    job = deps.jobs.get(job_id)
    if job is None or job["state"] != JobState.UPLOADED:
        logger.warning("parse skipped", extra={"job_id": job_id, "state": job and job["state"]})
        return "skipped"

    system = Actor.system()
    file_info: dict[str, Any] = dict(job.get("file", {}))
    try:
        get_kwargs: dict[str, Any] = {"Bucket": deps.uploads_bucket, "Key": job["upload_key"]}
        if file_info.get("version_id"):
            get_kwargs["VersionId"] = file_info["version_id"]
        data: bytes = deps.s3.get_object(**get_kwargs)["Body"].read()
        file_info.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        uploaded = AuditEvent(
            event_type=EventType.FILE_UPLOADED,
            actor=system,
            job_id=job_id,
            details={**file_info, "s3_key": job["upload_key"]},
        )
        result = parse_file(data, job["filename"], max_bytes=deps.max_bytes, max_rows=deps.max_rows)
    except ParseError as err:
        _fail(deps, job_id, err.code, err.message, file_info, system)
        return JobState.PARSE_FAILED
    except Exception:
        logger.exception("parse crashed", extra={"job_id": job_id})
        _fail(deps, job_id, "SYSTEM_ERROR", messages.PARSE_SYSTEM_ERROR, file_info, system)
        return JobState.PARSE_FAILED

    deps.rows.put_parsed(job_id, result.rows)
    summary = _parse_summary(result)
    warning_codes = Counter(w.code for w in result.warnings)
    warning_codes.update(i.code for r in result.rows for i in r.issues)
    deps.jobs.transition(
        job_id,
        JobState.UPLOADED,
        JobState.MAPPING_REVIEW,
        actor=system,
        set_fields={"file": file_info, "parse": summary},
        events=[
            uploaded,
            AuditEvent(
                event_type=EventType.FILE_PARSED,
                actor=system,
                job_id=job_id,
                details={
                    "file_type": result.file_type,
                    "sheet_name": result.sheet_name,
                    "encoding": result.encoding,
                    "delimiter": result.delimiter,
                    "row_count": len(result.rows),
                    "column_count": len(result.headers),
                    "warnings": dict(warning_codes),
                },
            ),
        ],
    )
    metrics.add_metric(name="JobsReachedMapping", unit="Count", value=1)
    metrics.add_metric(name="RowsPerJob", unit="Count", value=len(result.rows))
    logger.info(
        "parsed", extra={"job_id": job_id, "row_count": len(result.rows), "type": result.file_type}
    )
    return JobState.MAPPING_REVIEW


def _fail(
    deps: ParseDeps,
    job_id: str,
    code: str,
    message: str,
    file_info: dict[str, Any],
    actor: Actor,
) -> None:
    events = []
    if file_info.get("sha256"):
        events.append(
            AuditEvent(
                event_type=EventType.FILE_UPLOADED, actor=actor, job_id=job_id, details=file_info
            )
        )
    events.append(
        AuditEvent(
            event_type=EventType.PARSE_FAILED,
            actor=actor,
            job_id=job_id,
            details={"code": code},
        )
    )
    deps.jobs.transition(
        job_id,
        JobState.UPLOADED,
        JobState.PARSE_FAILED,
        actor=actor,
        set_fields={"file": file_info, "parse_error": {"code": code, "message": message}},
        events=events,
    )
    metrics.add_metric(name="ParseFailures", unit="Count", value=1)
    logger.info("parse failed", extra={"job_id": job_id, "code": code})


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: Any) -> dict[str, str]:
    global _deps
    if _deps is None:
        _deps = ParseDeps.from_env()
    job_id = event["job_id"]
    logger.append_keys(job_id=job_id)
    return {"job_id": job_id, "state": run(job_id, _deps)}

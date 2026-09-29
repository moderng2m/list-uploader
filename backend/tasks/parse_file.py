"""Parse an uploaded file into Rows and suggest a column mapping (SPEC §6.1, §7.1, §10).

Invoked async by the BFF. UPLOADED -> MAPPING_REVIEW on success,
UPLOADED -> PARSE_FAILED otherwise. The mapping suggestion (exact -> alias ->
AI) runs here rather than in the BFF because the AI step can take seconds.
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
from shared.bedrock_client import BedrockClient, BedrockMantleClient
from shared.config_store import ConfigStore
from shared.fake_ai import HeuristicFakeBedrock
from shared.jobs import JobRepo, JobState
from shared.mapping import MappingSuggestion, suggest
from shared.observability import logger, metrics, tracer
from shared.parsing import ParseError, ParseResult, parse_file
from shared.rows import RowRepo

SAMPLES_PER_COLUMN = 5


@dataclass
class ParseDeps:
    jobs: JobRepo
    rows: RowRepo
    s3: Any
    uploads_bucket: str
    bedrock: BedrockClient
    config: ConfigStore
    max_bytes: int = config_defaults.LIMITS["max_file_bytes"]
    max_rows: int = config_defaults.LIMITS["max_rows"]

    @classmethod
    def from_env(cls) -> ParseDeps:
        audit = AuditWriter()
        fake = os.environ.get("INTEGRATIONS", "fake") == "fake"
        return cls(
            jobs=JobRepo(audit),
            rows=RowRepo(),
            s3=boto3.client("s3"),
            uploads_bucket=os.environ["UPLOADS_BUCKET"],
            bedrock=HeuristicFakeBedrock() if fake else BedrockMantleClient(),
            config=ConfigStore(),
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


def column_samples(result: ParseResult, limit: int = SAMPLES_PER_COLUMN) -> dict[str, list[str]]:
    """First few non-empty values per column. Used for the AI prompt only; never stored."""
    samples: dict[str, list[str]] = {h: [] for h in result.headers}
    for row in result.rows:
        for header, value in row.values.items():
            if value.strip() and len(samples[header]) < limit:
                samples[header].append(value)
    return samples


def _suggest_mapping(result: ParseResult, deps: ParseDeps) -> MappingSuggestion:
    thresholds = deps.config.thresholds()
    return suggest(
        result.headers,
        column_samples(result),
        aliases=deps.config.aliases(),
        threshold=thresholds["mapping_suggest_threshold"],
        bedrock=deps.bedrock,
    )


def _mapping_events(job_id: str, mapping: MappingSuggestion, actor: Actor) -> list[AuditEvent]:
    events = []
    if mapping.ai.result is not None:
        events.append(
            AuditEvent(
                event_type=EventType.AI_INVOCATION,
                actor=actor,
                job_id=job_id,
                details=mapping.ai.result.audit_details(row_count=mapping.ai.columns_sent),
            )
        )
    events.append(
        AuditEvent(
            event_type=EventType.MAPPING_SUGGESTED,
            actor=actor,
            job_id=job_id,
            details={
                "columns": [
                    {
                        "source_header": c.source_header,
                        "field_key": c.field_key,
                        "method": str(c.method),
                        "confidence": c.confidence,
                    }
                    for c in mapping.columns
                ],
                "method_counts": mapping.method_counts(),
                "alias_version": mapping.alias_version,
                "catalog_version": mapping.catalog_version,
                "threshold": mapping.threshold,
                "ai_outcome": mapping.ai.outcome,
            },
        )
    )
    return events


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
        mapping = _suggest_mapping(result, deps)
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
        set_fields={
            "file": file_info,
            "parse": summary,
            "mapping_suggestion": mapping.as_dict(),
        },
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
            *_mapping_events(job_id, mapping, system),
        ],
    )
    metrics.add_metric(name="JobsReachedMapping", unit="Count", value=1)
    for method, count in mapping.method_counts().items():
        metrics.add_metric(name=f"MappingColumns_{method}", unit="Count", value=count)
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

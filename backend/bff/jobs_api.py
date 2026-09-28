"""Job routes for upload and parse (SPEC §6.1, §19)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError
from botocore.exceptions import ClientError

from bff.app import app, correlation_id, current_user, deps, json_body, load_job_for, require_admin
from shared import messages
from shared.jobs import JobState
from shared.observability import logger, metrics
from shared.parsing import file_extension

UPLOAD_URL_TTL_SECONDS = 900
MAX_FILENAME = 255
# Internal fields not returned to the browser.
_HIDDEN = ("owner_sub", "upload_key")


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if k not in _HIDDEN}


@app.post("/jobs")
def create_job() -> Response[Any]:
    user = current_user()
    body = json_body()
    filename = str(body.get("filename") or "").strip()
    enrich = body.get("enrich", True)
    if not filename or len(filename) > MAX_FILENAME or "/" in filename or "\\" in filename:
        raise BadRequestError("Choose a file to upload.")
    if not isinstance(enrich, bool):
        raise BadRequestError("enrich must be true or false.")
    ext = file_extension(filename)
    if ext not in (".csv", ".xlsx"):
        raise BadRequestError(messages.WRONG_FILE_TYPE.format(ext=ext.lstrip(".") or "file"))

    d = deps()
    job = d.jobs.create(
        actor=user.actor(),
        owner_email=user.email,
        owner_sub=user.sub,
        filename=filename,
        enrich=enrich,
        # The key never contains the user's filename.
        upload_key_for=lambda job_id: f"{job_id}/source{ext}",
        correlation_id=correlation_id(),
    )
    # Presigned POST lets S3 enforce the size limit itself.
    upload = d.s3.generate_presigned_post(
        Bucket=d.uploads_bucket,
        Key=job["upload_key"],
        Conditions=[["content-length-range", 1, d.max_bytes]],
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )
    metrics.add_metric(name="JobsCreated", unit="Count", value=1)
    return Response(
        status_code=201,
        content_type=content_types.APPLICATION_JSON,
        body={
            "job": job_view(job),
            "upload": {"url": upload["url"], "fields": upload["fields"]},
            "max_bytes": d.max_bytes,
        },
    )


@app.post("/jobs/<job_id>/uploaded")
def mark_uploaded(job_id: str) -> Response[Any]:
    user = current_user()
    d = deps()
    job = load_job_for(user, job_id, "/jobs/{id}/uploaded")
    if job["state"] != JobState.AWAITING_UPLOAD:
        return Response(
            status_code=409,
            content_type=content_types.APPLICATION_JSON,
            body={"message": messages.JOB_STATE_CONFLICT},
        )
    try:
        head = d.s3.head_object(Bucket=d.uploads_bucket, Key=job["upload_key"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            raise BadRequestError(messages.UPLOAD_NOT_RECEIVED) from exc
        raise
    version_id = head.get("VersionId")
    size = int(head["ContentLength"])

    # Write-once: lock the exact version we'll parse for the raw-file retention period.
    if version_id:
        d.s3.put_object_retention(
            Bucket=d.uploads_bucket,
            Key=job["upload_key"],
            VersionId=version_id,
            Retention={
                "Mode": "GOVERNANCE",
                "RetainUntilDate": datetime.now(UTC) + timedelta(days=d.raw_file_retention_days),
            },
        )

    d.jobs.transition(
        job_id,
        JobState.AWAITING_UPLOAD,
        JobState.UPLOADED,
        actor=user.actor(),
        set_fields={"file": {"version_id": version_id, "size": size}},
        details={"size": size, "version_id": version_id},
        correlation_id=correlation_id(),
    )
    d.start_parse(job_id)
    logger.info("upload confirmed", extra={"job_id": job_id, "size": size})
    updated = d.jobs.get(job_id) or job
    return Response(
        status_code=202, content_type=content_types.APPLICATION_JSON, body=job_view(updated)
    )


@app.get("/jobs")
def list_jobs() -> list[dict[str, Any]]:
    params = app.current_event.query_string_parameters or {}
    if params.get("all") == "true":
        require_admin("/jobs?all=true")
        return [job_view(j) for j in deps().jobs.list_all()]
    user = current_user()
    return [job_view(j) for j in deps().jobs.list_for_owner(user.email)]


@app.get("/jobs/<job_id>")
def get_job(job_id: str) -> dict[str, Any]:
    return job_view(load_job_for(current_user(), job_id, "/jobs/{id}"))

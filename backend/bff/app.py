"""BFF resolver, dependencies, and authorization helpers shared by route modules."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import boto3
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import (
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)
from botocore.config import Config as BotoConfig

from bff.auth import User, user_from_event
from shared import config_defaults, messages
from shared.audit import AuditEvent, AuditWriteError, AuditWriter, EventType, StateConflict
from shared.audit_read import AuditReader
from shared.config_store import ConfigStore
from shared.fake_sfdc import CAMPAIGNS
from shared.ids import new_ulid
from shared.jobs import InvalidTransition, JobRepo
from shared.normalizer import Normalizer, load_normalizer
from shared.rows import RowRepo
from shared.workato_client import FakeWorkatoClient, WorkatoClient

app = APIGatewayHttpResolver()


def signing_s3_client() -> Any:
    """S3 client for the presigned upload POST and download GET.

    The buckets use SSE-KMS, and S3 refuses KMS uploads signed with the legacy
    SigV2 (`AWSAccessKeyId`/`signature` fields), which boto3 can still pick for
    presigned POSTs. Pin SigV4 and the regional virtual-host endpoint.
    """
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


@dataclass
class BffDeps:
    audit: AuditWriter
    jobs: JobRepo
    s3: Any
    uploads_bucket: str
    start_parse: Callable[[str], None]
    rows: RowRepo
    start_analysis: Callable[[str], None]
    start_enrichment: Callable[[str], None]
    start_send: Callable[[str, bool], None]
    config: ConfigStore
    workato: WorkatoClient
    normalizer: Normalizer = field(default_factory=load_normalizer)
    bedrock_model_id: str = "fake-heuristic"
    processed_bucket: str = ""
    send_to_prod: bool = False
    raw_file_retention_days: int = 90
    max_bytes: int = config_defaults.LIMITS["max_file_bytes"]
    # None: read the table the audit writer writes to.
    audit_reader: AuditReader | None = None

    @classmethod
    def from_env(cls) -> BffDeps:
        lambda_client = boto3.client("lambda")
        parse_fn = os.environ["PARSE_FUNCTION"]

        def start_parse(job_id: str) -> None:
            lambda_client.invoke(
                FunctionName=parse_fn,
                InvocationType="Event",
                Payload=json.dumps({"job_id": job_id}).encode(),
            )

        sfn = boto3.client("stepfunctions")
        analyze_arn = os.environ["ANALYZE_STATE_MACHINE"]

        def starter(arn: str) -> Callable[[str], None]:
            def start(job_id: str) -> None:
                sfn.start_execution(
                    stateMachineArn=arn,
                    name=f"{job_id}-{new_ulid()}",
                    input=json.dumps({"job_id": job_id}),
                )

            return start

        start_analysis = starter(analyze_arn)
        start_enrichment = starter(os.environ["ENRICH_STATE_MACHINE"])
        send_arn = os.environ["SEND_STATE_MACHINE"]

        def start_send(job_id: str, only_failed: bool) -> None:
            sfn.start_execution(
                stateMachineArn=send_arn,
                name=f"{job_id}-{new_ulid()}",
                input=json.dumps({"job_id": job_id, "only_failed": only_failed}),
            )

        return cls.with_starters(
            start_parse=start_parse,
            start_analysis=start_analysis,
            start_enrichment=start_enrichment,
            start_send=start_send,
        )

    @classmethod
    def with_starters(
        cls,
        *,
        start_parse: Callable[[str], None],
        start_analysis: Callable[[str], None],
        start_enrichment: Callable[[str], None],
        start_send: Callable[[str, bool], None],
    ) -> BffDeps:
        """Everything from the environment except how async work is started (the
        canary runs it in-process instead)."""
        if os.environ.get("INTEGRATIONS", "fake") != "fake":
            raise RuntimeError("only INTEGRATIONS=fake is wired up in this build")
        audit = AuditWriter()
        return cls(
            start_analysis=start_analysis,
            start_enrichment=start_enrichment,
            start_send=start_send,
            processed_bucket=os.environ["PROCESSED_BUCKET"],
            send_to_prod=os.environ.get("SEND_TO_PROD", "false") == "true",
            config=ConfigStore(),
            workato=FakeWorkatoClient(env=os.environ.get("ENV", "dev"), campaigns=dict(CAMPAIGNS)),
            bedrock_model_id=os.environ.get("BEDROCK_MODEL_ID", "fake-heuristic"),
            audit=audit,
            jobs=JobRepo(audit),
            s3=signing_s3_client(),
            uploads_bucket=os.environ["UPLOADS_BUCKET"],
            start_parse=start_parse,
            rows=RowRepo(),
            raw_file_retention_days=int(os.environ.get("RAW_FILE_RETENTION_DAYS", "90")),
        )


_deps: BffDeps | None = None


def deps() -> BffDeps:
    global _deps
    if _deps is None:
        _deps = BffDeps.from_env()
    return _deps


def set_deps(value: BffDeps | None) -> None:
    """Test hook."""
    global _deps
    _deps = value


def correlation_id() -> str:
    return str(app.current_event.request_context.request_id)


def current_user() -> User:
    user = user_from_event(app.current_event.raw_event)
    if user is None:
        raise UnauthorizedError("missing identity claims")
    return user


def _deny(user: User, route: str, job_id: str | None = None) -> None:
    """Record ACCESS_DENIED, then refuse. Fails closed if the audit write fails."""
    deps().audit.write(
        AuditEvent(
            event_type=EventType.ACCESS_DENIED,
            actor=user.actor(),
            job_id=job_id,
            details={"route": route, "method": app.current_event.http_method},
            correlation_id=correlation_id(),
        )
    )
    raise ForbiddenError(messages.ACCESS_DENIED)


def require_admin(route: str) -> User:
    user = current_user()
    if not user.is_admin:
        _deny(user, route)
    return user


def load_job_for(user: User, job_id: str, route: str) -> dict[str, Any]:
    """Owner-or-admin check on every /jobs/{id} route (SPEC §19)."""
    job = deps().jobs.get(job_id)
    if job is None:
        raise NotFoundError(messages.JOB_NOT_FOUND)
    if job["owner_email"] != user.email and not user.is_admin:
        _deny(user, route, job_id)
    return job


def json_body() -> dict[str, Any]:
    try:
        body = app.current_event.json_body
    except (ValueError, TypeError):
        body = None
    return body if isinstance(body, dict) else {}


def _error(status: int, message: str) -> Response[Any]:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body={"message": message},
    )


@app.exception_handler(AuditWriteError)  # type: ignore[untyped-decorator]
def _audit_failed(exc: AuditWriteError) -> Response[Any]:
    return _error(503, messages.SERVICE_UNAVAILABLE)


@app.exception_handler(StateConflict)  # type: ignore[untyped-decorator]
def _state_conflict(exc: StateConflict) -> Response[Any]:
    return _error(409, messages.JOB_STATE_CONFLICT)


@app.exception_handler(InvalidTransition)  # type: ignore[untyped-decorator]
def _invalid_transition(exc: InvalidTransition) -> Response[Any]:
    return _error(409, messages.JOB_STATE_CONFLICT)

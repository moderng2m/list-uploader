"""BFF resolver, dependencies, and authorization helpers shared by route modules."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import (
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)

from bff.auth import User, user_from_event
from shared import config_defaults, messages
from shared.audit import AuditEvent, AuditWriteError, AuditWriter, EventType, StateConflict
from shared.jobs import InvalidTransition, JobRepo

app = APIGatewayHttpResolver()


@dataclass
class BffDeps:
    audit: AuditWriter
    jobs: JobRepo
    s3: Any
    uploads_bucket: str
    start_parse: Callable[[str], None]
    raw_file_retention_days: int = 90
    max_bytes: int = config_defaults.LIMITS["max_file_bytes"]

    @classmethod
    def from_env(cls) -> BffDeps:
        audit = AuditWriter()
        lambda_client = boto3.client("lambda")
        parse_fn = os.environ["PARSE_FUNCTION"]

        def start_parse(job_id: str) -> None:
            lambda_client.invoke(
                FunctionName=parse_fn,
                InvocationType="Event",
                Payload=json.dumps({"job_id": job_id}).encode(),
            )

        return cls(
            audit=audit,
            jobs=JobRepo(audit),
            s3=boto3.client("s3"),
            uploads_bucket=os.environ["UPLOADS_BUCKET"],
            start_parse=start_parse,
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

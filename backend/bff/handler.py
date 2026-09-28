"""BFF Lambda: single router for the REST API (SPEC §19).

P0 ships the skeleton: identity, admin authorization with ACCESS_DENIED
auditing, and fail-closed handling of audit write failures. Job routes arrive
in P1 onward.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import ForbiddenError, UnauthorizedError
from aws_lambda_powertools.utilities.typing import LambdaContext

from bff.auth import User, user_from_event
from shared import config_defaults, messages
from shared.audit import AuditEvent, AuditWriteError, AuditWriter, EventType
from shared.observability import logger, metrics, tracer

app = APIGatewayHttpResolver()
_audit: AuditWriter | None = None


def audit_writer() -> AuditWriter:
    global _audit
    if _audit is None:
        _audit = AuditWriter()
    return _audit


def set_audit_writer(writer: AuditWriter | None) -> None:
    """Test hook."""
    global _audit
    _audit = writer


def current_user() -> User:
    user = user_from_event(app.current_event.raw_event)
    if user is None:
        raise UnauthorizedError("missing identity claims")
    return user


def require_admin(route: str, job_id: str | None = None) -> User:
    """Deny non-admins, recording ACCESS_DENIED first. Fails closed if the audit write fails."""
    user = current_user()
    if not user.is_admin:
        audit_writer().write(
            AuditEvent(
                event_type=EventType.ACCESS_DENIED,
                actor=user.actor(),
                job_id=job_id,
                details={"route": route, "method": app.current_event.http_method},
                correlation_id=app.current_event.request_context.request_id,
            )
        )
        raise ForbiddenError(messages.ACCESS_DENIED)
    return user


@app.exception_handler(AuditWriteError)  # type: ignore[untyped-decorator]
def _audit_failed(exc: AuditWriteError) -> Response[Any]:
    return Response(
        status_code=503,
        content_type=content_types.APPLICATION_JSON,
        body={"message": messages.SERVICE_UNAVAILABLE},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/me")
def me() -> dict[str, Any]:
    user = current_user()
    return {"email": user.email, "is_admin": user.is_admin}


@app.get("/admin/config")
def admin_config() -> dict[str, Any]:
    require_admin("/admin/config")
    return {"thresholds": config_defaults.THRESHOLDS, "limits": config_defaults.LIMITS}


@logger.inject_lambda_context(correlation_id_path="requestContext.requestId")
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)

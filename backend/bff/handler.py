"""BFF Lambda entry point: single router for the REST API (SPEC §19)."""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from bff import (  # noqa: F401  (registers routes)
    analysis_api,
    enrichment_api,
    jobs_api,
    mapping_api,
    send_api,
)
from bff.app import app, current_user, require_admin
from shared import config_defaults
from shared.observability import logger, metrics, tracer


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

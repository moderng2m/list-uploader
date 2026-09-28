"""API Gateway HTTP API (v2) events and a context for BFF tests."""

from __future__ import annotations

import json
from typing import Any

ADMIN = "[admin]"
UPLOADER = "[uploaders]"


class Ctx:
    function_name = "bff"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:bff"
    aws_request_id = "req-1"


def http_event(
    method: str,
    path: str,
    *,
    email: str = "uploader@example.com",
    groups: str | None = UPLOADER,
    body: Any = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    claims: dict[str, Any] = {"email": email, "sub": f"sub-{email}"}
    if groups is not None:
        claims["cognito:groups"] = groups
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "queryStringParameters": query,
        "headers": {"content-type": "application/json"},
        "body": None if body is None else json.dumps(body),
        "requestContext": {
            "requestId": "req-1",
            "stage": "$default",
            "http": {"method": method, "path": path, "sourceIp": "10.0.0.1"},
            "authorizer": {"jwt": {"claims": claims, "scopes": None}},
        },
        "isBase64Encoded": False,
    }


def call(event: dict[str, Any]) -> tuple[int, Any]:
    from bff import handler as bff

    resp = bff.handler(event, Ctx())  # type: ignore[arg-type]
    body = json.loads(resp["body"]) if resp.get("body") else None
    return resp["statusCode"], body

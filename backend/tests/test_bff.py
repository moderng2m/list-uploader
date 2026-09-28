from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from bff import handler as bff
from shared.audit import AuditWriter
from tests.conftest import AUDIT_TABLE


class _Ctx:
    function_name = "bff"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:bff"
    aws_request_id = "req-1"


def _event(path: str, groups: str | None = None) -> dict[str, Any]:
    claims: dict[str, Any] = {"email": "Uploader@Example.com", "sub": "sub-1"}
    if groups is not None:
        claims["cognito:groups"] = groups
    return {
        "version": "2.0",
        "routeKey": f"GET {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {},
        "requestContext": {
            "requestId": "req-1",
            "stage": "$default",
            "http": {"method": "GET", "path": path, "sourceIp": "10.0.0.1"},
            "authorizer": {"jwt": {"claims": claims, "scopes": None}},
        },
        "isBase64Encoded": False,
    }


@pytest.fixture
def writer(audit_table: Any) -> Iterator[AuditWriter]:
    w = AuditWriter(AUDIT_TABLE, env="dev", app_version="t")
    bff.set_audit_writer(w)
    yield w
    bff.set_audit_writer(None)


def _call(path: str, groups: str | None = None) -> dict[str, Any]:
    return bff.handler(_event(path, groups), _Ctx())  # type: ignore[arg-type]


def test_me(writer: AuditWriter) -> None:
    resp = _call("/me", "[admin]")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"email": "uploader@example.com", "is_admin": True}


def test_admin_route_allows_admin(writer: AuditWriter, audit_table: Any) -> None:
    resp = _call("/admin/config", "[admin uploaders]")
    assert resp["statusCode"] == 200
    assert audit_table.scan()["Count"] == 0


def test_non_admin_is_denied_and_audited(writer: AuditWriter, audit_table: Any) -> None:
    resp = _call("/admin/config", "[uploaders]")
    assert resp["statusCode"] == 403
    items = audit_table.scan()["Items"]
    assert [i["event_type"] for i in items] == ["ACCESS_DENIED"]
    assert json.loads(items[0]["details"])["route"] == "/admin/config"


def test_audit_failure_fails_the_request(aws: None) -> None:
    bff.set_audit_writer(AuditWriter("missing-table", env="dev", app_version="t"))
    try:
        resp = _call("/admin/config", "[uploaders]")
    finally:
        bff.set_audit_writer(None)
    assert resp["statusCode"] == 503


def test_missing_claims_is_unauthorized(writer: AuditWriter) -> None:
    event = _event("/me")
    event["requestContext"]["authorizer"]["jwt"]["claims"] = {}
    assert bff.handler(event, _Ctx())["statusCode"] == 401  # type: ignore[arg-type]

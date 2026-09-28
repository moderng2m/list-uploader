from __future__ import annotations

import json
from typing import Any

import pytest

from bff.app import set_deps
from shared.audit import AuditWriter
from tests.conftest import Env
from tests.helpers import ADMIN, UPLOADER, call, http_event


@pytest.fixture
def bff(app_env: Env) -> Env:
    return app_env


def test_me(bff: Env) -> None:
    status, body = call(http_event("GET", "/me", email="Uploader@Example.com", groups=ADMIN))
    assert status == 200
    assert body == {"email": "uploader@example.com", "is_admin": True}


def test_admin_route_allows_admin(bff: Env) -> None:
    status, _ = call(http_event("GET", "/admin/config", groups="[admin uploaders]"))
    assert status == 200
    assert bff.audit_table.scan()["Count"] == 0


def test_non_admin_is_denied_and_audited(bff: Env) -> None:
    status, _ = call(http_event("GET", "/admin/config", groups=UPLOADER))
    assert status == 403
    items = bff.audit_table.scan()["Items"]
    assert [i["event_type"] for i in items] == ["ACCESS_DENIED"]
    assert json.loads(items[0]["details"])["route"] == "/admin/config"


def test_audit_failure_fails_the_request(bff: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = AuditWriter("missing-table", env="dev", app_version="t")
    monkeypatch.setattr(bff.jobs, "audit", broken)
    set_deps(bff.bff_deps(audit=broken))
    assert call(http_event("GET", "/admin/config", groups=UPLOADER))[0] == 503


def test_missing_claims_is_unauthorized(bff: Env) -> None:
    event: dict[str, Any] = http_event("GET", "/me")
    event["requestContext"]["authorizer"]["jwt"]["claims"] = {}
    assert call(event)[0] == 401

from __future__ import annotations

import json
from typing import Any

import pytest

from shared.audit import (
    GLOBAL_PARTITION,
    Actor,
    ActorType,
    AuditEvent,
    AuditWriteError,
    AuditWriter,
    EventType,
    email_sha256,
    run_audited,
)
from tests.conftest import AUDIT_TABLE

USER = Actor(type=ActorType.USER, email="uploader@example.com", sub="sub-1")


def _writer() -> AuditWriter:
    return AuditWriter(AUDIT_TABLE, env="dev", app_version="0.1.0+test")


def test_write_persists_event(audit_table: Any) -> None:
    event = AuditEvent(
        event_type=EventType.USER_EDIT,
        actor=USER,
        job_id="j_1",
        row_id=12,
        subject={"field": "title"},
        before={"title": "VP Mktg"},
        after={"title": "VP Marketing"},
        reason="user_edit",
        lead_email="Jane.Doe@Example.com ",
    )
    _writer().write(event)

    items = audit_table.scan()["Items"]
    assert len(items) == 1
    item = items[0]
    assert item["job_id"] == "j_1"
    assert item["sk"] == f"{event.occurred_at}#{event.event_id}"
    assert item["event_type"] == "USER_EDIT"
    assert json.loads(item["after"]) == {"title": "VP Marketing"}
    assert item["email_sha256"] == email_sha256("jane.doe@example.com")
    assert "Jane.Doe@Example.com" not in json.dumps(item, default=str)
    assert item["actor"] == {"type": "user", "email": "uploader@example.com", "sub": "sub-1"}


def test_events_without_job_use_global_partition(audit_table: Any) -> None:
    _writer().write(AuditEvent(event_type=EventType.ADMIN_CONFIG_CHANGED, actor=USER))
    assert audit_table.scan()["Items"][0]["job_id"] == GLOBAL_PARTITION


def test_write_is_append_only(audit_table: Any) -> None:
    event = AuditEvent(event_type=EventType.JOB_CREATED, actor=USER, job_id="j_1")
    writer = _writer()
    writer.write(event)
    with pytest.raises(AuditWriteError):
        writer.write(event)  # same event_id + timestamp: conditional put rejects overwrite


def test_write_failure_raises(aws: None) -> None:
    writer = AuditWriter("missing-table", env="dev", app_version="t")
    with pytest.raises(AuditWriteError):
        writer.write(AuditEvent(event_type=EventType.JOB_CREATED, actor=USER, job_id="j_1"))


def test_run_audited_does_not_run_action_when_audit_fails(aws: None) -> None:
    writer = AuditWriter("missing-table", env="dev", app_version="t")
    ran: list[bool] = []
    with pytest.raises(AuditWriteError):
        run_audited(
            writer,
            AuditEvent(event_type=EventType.SEND_CONFIRMED, actor=USER, job_id="j_1"),
            lambda: ran.append(True),
        )
    assert ran == []


def test_run_audited_runs_action_after_recording(audit_table: Any) -> None:
    result = run_audited(
        _writer(),
        AuditEvent(event_type=EventType.SEND_CONFIRMED, actor=USER, job_id="j_1"),
        lambda: "sent",
    )
    assert result == "sent"
    assert audit_table.scan()["Count"] == 1


def test_event_ids_sort_by_time() -> None:
    from shared.ids import new_ulid

    assert new_ulid(1_000) < new_ulid(2_000)
    assert len(new_ulid()) == 26

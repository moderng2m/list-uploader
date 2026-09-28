"""Append-only audit trail (SPEC §21.2).

Principles enforced here:
- Fail closed: `AuditWriter.write` raises `AuditWriteError` on any failure, and
  `run_audited` never runs an action whose audit event wasn't recorded.
- Append-only: writes are conditional puts; app roles hold PutItem only (IaC).
- Values in `before`/`after` are PII, so they are never logged.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar

import boto3

from shared.ids import new_ulid
from shared.observability import env_name, logger, metrics

T = TypeVar("T")

# Partition for events not tied to a job (admin config changes, denied admin routes).
GLOBAL_PARTITION = "_global"


class EventType(StrEnum):
    """SPEC §21.2.3 event catalog."""

    JOB_CREATED = "JOB_CREATED"
    FILE_UPLOADED = "FILE_UPLOADED"
    FILE_PARSED = "FILE_PARSED"
    PARSE_FAILED = "PARSE_FAILED"
    MAPPING_SUGGESTED = "MAPPING_SUGGESTED"
    MAPPING_CONFIRMED = "MAPPING_CONFIRMED"
    ANALYSIS_STARTED = "ANALYSIS_STARTED"
    VALUE_NORMALIZED = "VALUE_NORMALIZED"
    VALUE_AUTO_CORRECTED = "VALUE_AUTO_CORRECTED"
    VALUE_DERIVED = "VALUE_DERIVED"
    ISSUE_RAISED = "ISSUE_RAISED"
    ISSUE_CLEARED = "ISSUE_CLEARED"
    AI_INVOCATION = "AI_INVOCATION"
    CAMPAIGN_VALIDATED = "CAMPAIGN_VALIDATED"
    USER_EDIT = "USER_EDIT"
    ROW_EXCLUDED = "ROW_EXCLUDED"
    ROW_INCLUDED = "ROW_INCLUDED"
    BULK_ACTION = "BULK_ACTION"
    SUGGESTION_ACCEPTED = "SUGGESTION_ACCEPTED"
    SUGGESTION_REJECTED = "SUGGESTION_REJECTED"
    ENRICHMENT_REQUESTED = "ENRICHMENT_REQUESTED"
    ENRICHMENT_RESULT = "ENRICHMENT_RESULT"
    ENRICHMENT_DECISION = "ENRICHMENT_DECISION"
    GATE_EVALUATED = "GATE_EVALUATED"
    SEND_CONFIRMED = "SEND_CONFIRMED"
    ROW_SUBMITTED = "ROW_SUBMITTED"
    ROW_SEND_FAILED = "ROW_SEND_FAILED"
    JOB_STATE_CHANGED = "JOB_STATE_CHANGED"
    PROCESSED_FILE_DOWNLOADED = "PROCESSED_FILE_DOWNLOADED"
    ADMIN_CONFIG_CHANGED = "ADMIN_CONFIG_CHANGED"
    ACCESS_DENIED = "ACCESS_DENIED"


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    ADMIN = "admin"


@dataclass(frozen=True)
class Actor:
    type: ActorType
    email: str | None = None
    sub: str | None = None
    ip: str | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls(type=ActorType.SYSTEM)


def email_sha256(email: str) -> str:
    """Hash used for the person-lookup GSI (SPEC §21.2.4)."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class AuditEvent:
    event_type: EventType
    actor: Actor
    job_id: str | None = None
    row_id: int | None = None
    subject: dict[str, Any] | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None
    details: dict[str, Any] | None = None
    correlation_id: str | None = None
    # The lead's email, if the event concerns one person. Stored only as a hash.
    lead_email: str | None = None
    event_id: str = field(default_factory=new_ulid)
    occurred_at: str = field(default_factory=_now_iso)

    def to_item(self, *, env: str, app_version: str) -> dict[str, Any]:
        """DynamoDB item. Nested payloads are JSON strings so floats and nulls survive."""
        item: dict[str, Any] = {
            "job_id": self.job_id or GLOBAL_PARTITION,
            "sk": f"{self.occurred_at}#{self.event_id}",
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "occurred_at": self.occurred_at,
            "env": env,
            "app_version": app_version,
            "actor": {k: v for k, v in _actor_dict(self.actor).items() if v is not None},
        }
        optional: dict[str, Any] = {
            "row_id": self.row_id,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "subject": _json_or_none(self.subject),
            "before": _json_or_none(self.before),
            "after": _json_or_none(self.after),
            "details": _json_or_none(self.details),
            "email_sha256": email_sha256(self.lead_email) if self.lead_email else None,
        }
        item.update({k: v for k, v in optional.items() if v is not None})
        return item


def _actor_dict(actor: Actor) -> dict[str, str | None]:
    return {"type": str(actor.type), "email": actor.email, "sub": actor.sub, "ip": actor.ip}


def _json_or_none(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


class AuditWriteError(RuntimeError):
    """The audit event could not be recorded; the calling action must fail."""


class AuditWriter:
    def __init__(
        self,
        table_name: str | None = None,
        *,
        env: str | None = None,
        app_version: str | None = None,
        dynamodb_client: Any | None = None,
    ) -> None:
        self._table_name = table_name or os.environ["AUDIT_TABLE"]
        self._env = env or env_name()
        self._app_version = app_version or os.environ.get("APP_VERSION", "0.0.0+local")
        self._table = (dynamodb_client or boto3.resource("dynamodb")).Table(self._table_name)

    def write(self, event: AuditEvent) -> AuditEvent:
        item = event.to_item(env=self._env, app_version=self._app_version)
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(job_id) AND attribute_not_exists(sk)",
            )
        except Exception as exc:
            metrics.add_metric(name="AuditWriteFailures", unit="Count", value=1)
            logger.error(
                "audit write failed",
                extra={
                    "event": str(event.event_type),
                    "job_id": event.job_id,
                    "row_id": event.row_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise AuditWriteError(f"audit write failed for {event.event_type}") from exc
        return event


def run_audited(writer: AuditWriter, event: AuditEvent, action: Callable[[], T]) -> T:
    """Record `event`, then perform `action`. If recording fails, the action never runs."""
    writer.write(event)
    return action()

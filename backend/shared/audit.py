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
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, TypeVar

import boto3
from botocore.exceptions import ClientError

from shared.ids import new_ulid
from shared.observability import env_name, logger, metrics

T = TypeVar("T")

# Matches the audit archive's retention (SPEC §21.2.5, interim 2 years).
DEFAULT_RETENTION_DAYS = 730

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
    # Not in the SPEC §21.2.3 table: the audit export is itself audited (§21.2.6).
    AUDIT_EXPORTED = "AUDIT_EXPORTED"


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

    def to_item(
        self, *, env: str, app_version: str, retention_days: int = DEFAULT_RETENTION_DAYS
    ) -> dict[str, Any]:
        """DynamoDB item. Nested payloads are JSON strings so floats and nulls survive."""
        occurred = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        item: dict[str, Any] = {
            "job_id": self.job_id or GLOBAL_PARTITION,
            "sk": f"{self.occurred_at}#{self.event_id}",
            "event_id": self.event_id,
            "event_type": str(self.event_type),
            "occurred_at": self.occurred_at,
            "env": env,
            "app_version": app_version,
            "actor": {k: v for k, v in _actor_dict(self.actor).items() if v is not None},
            # DynamoDB TTL (epoch seconds): the query copy expires; the archive stays.
            "expires_at": int((occurred + timedelta(days=retention_days)).timestamp()),
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


class StateConflict(RuntimeError):
    """A conditional write alongside the audit events failed (e.g. the job moved on)."""


_AUDIT_CONDITION = "attribute_not_exists(job_id) AND attribute_not_exists(sk)"


def to_dynamo(value: Any) -> Any:
    """Floats -> Decimal, recursively (DynamoDB rejects floats)."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_dynamo(v) for v in value]
    return value


def _cancellation_codes(exc: ClientError) -> list[str]:
    reasons = exc.response.get("CancellationReasons")
    if reasons:
        return [str(r.get("Code", "None")) for r in reasons]
    match = re.search(r"\[([^\]]*)\]", str(exc))
    return [c.strip() for c in match.group(1).split(",")] if match else []


class AuditWriter:
    def __init__(
        self,
        table_name: str | None = None,
        *,
        env: str | None = None,
        app_version: str | None = None,
        dynamodb_resource: Any | None = None,
    ) -> None:
        self._table_name = table_name or os.environ["AUDIT_TABLE"]
        self._env = env or env_name()
        self._app_version = app_version or os.environ.get("APP_VERSION", "0.0.0+local")
        self._retention_days = int(
            os.environ.get("AUDIT_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
        )
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(self._table_name)
        self._client = self._table.meta.client

    @property
    def app_version(self) -> str:
        return self._app_version

    @property
    def table_name(self) -> str:
        return self._table_name

    def _item(self, event: AuditEvent) -> dict[str, Any]:
        return event.to_item(
            env=self._env, app_version=self._app_version, retention_days=self._retention_days
        )

    def write(self, event: AuditEvent) -> AuditEvent:
        item = self._item(event)
        try:
            self._table.put_item(Item=item, ConditionExpression=_AUDIT_CONDITION)
        except Exception as exc:
            raise self._failed([event], exc) from exc
        return event

    def transact(self, events: Sequence[AuditEvent], writes: Sequence[dict[str, Any]] = ()) -> None:
        """Write audit events and other changes atomically: all happen or none do.

        `writes` are TransactWriteItems operations with plain Python values, e.g.
        `{"Update": {"TableName": ..., "Key": {...}, "UpdateExpression": ...}}`.
        Raises `StateConflict` if one of `writes` fails its condition, otherwise
        `AuditWriteError` on any failure.
        """
        audit_ops = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": self._item(e),
                    "ConditionExpression": _AUDIT_CONDITION,
                }
            }
            for e in events
        ]
        # The resource-backed client serializes plain Python values itself.
        ops = to_dynamo([*audit_ops, *writes])
        try:
            self._client.transact_write_items(TransactItems=ops)
        except ClientError as exc:
            codes = _cancellation_codes(exc)
            if any(c == "ConditionalCheckFailed" for c in codes[len(events) :]):
                raise StateConflict("a conditional write failed") from exc
            raise self._failed(events, exc) from exc
        except Exception as exc:
            raise self._failed(events, exc) from exc

    def _failed(self, events: Sequence[AuditEvent], exc: Exception) -> AuditWriteError:
        metrics.add_metric(name="AuditWriteFailures", unit="Count", value=1)
        logger.error(
            "audit write failed",
            extra={
                "event": ",".join(str(e.event_type) for e in events),
                "job_id": events[0].job_id if events else None,
                "error_type": type(exc).__name__,
            },
        )
        return AuditWriteError(
            f"audit write failed for {','.join(str(e.event_type) for e in events)}"
        )


def run_audited(writer: AuditWriter, event: AuditEvent, action: Callable[[], T]) -> T:
    """Record `event`, then perform `action`. If recording fails, the action never runs."""
    writer.write(event)
    return action()

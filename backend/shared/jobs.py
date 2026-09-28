"""Jobs table access and the job state machine (SPEC §5.1).

Every state change goes through `JobRepo.transition`, which writes the job
update and its JOB_STATE_CHANGED audit event in one DynamoDB transaction.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from shared.audit import Actor, AuditEvent, AuditWriter, EventType, to_dynamo
from shared.ids import new_job_id


class JobState(StrEnum):
    # Created, waiting for the browser to upload the file. Not in the SPEC §5.1
    # diagram, which starts at UPLOADED; added so a job exists before its file does.
    AWAITING_UPLOAD = "AWAITING_UPLOAD"
    UPLOADED = "UPLOADED"
    PARSE_FAILED = "PARSE_FAILED"
    MAPPING_REVIEW = "MAPPING_REVIEW"
    ANALYZING = "ANALYZING"
    ANALYSIS_REVIEW = "ANALYSIS_REVIEW"
    ENRICHING = "ENRICHING"
    ENRICHMENT_REVIEW = "ENRICHMENT_REVIEW"
    READY_TO_SEND = "READY_TO_SEND"
    SENDING = "SENDING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


S = JobState
RUNNING = frozenset({S.UPLOADED, S.ANALYZING, S.ENRICHING, S.SENDING})
REVIEW = frozenset({S.MAPPING_REVIEW, S.ANALYSIS_REVIEW, S.ENRICHMENT_REVIEW})

ALLOWED: dict[JobState, frozenset[JobState]] = {
    S.AWAITING_UPLOAD: frozenset({S.UPLOADED, S.CANCELLED, S.EXPIRED}),
    S.UPLOADED: frozenset({S.MAPPING_REVIEW, S.PARSE_FAILED}),
    S.MAPPING_REVIEW: frozenset({S.ANALYZING, S.CANCELLED}),
    S.ANALYZING: frozenset({S.ANALYSIS_REVIEW}),
    S.ANALYSIS_REVIEW: frozenset({S.ANALYZING, S.ENRICHING, S.READY_TO_SEND, S.CANCELLED}),
    S.ENRICHING: frozenset({S.ENRICHMENT_REVIEW}),
    S.ENRICHMENT_REVIEW: frozenset({S.READY_TO_SEND, S.CANCELLED}),
    S.READY_TO_SEND: frozenset({S.SENDING}),
    S.SENDING: frozenset({S.COMPLETED, S.COMPLETED_WITH_ERRORS}),
    # SPEC §5.1: after an unrecoverable error the user can retry.
    S.FAILED: frozenset({S.ANALYZING}),
}
# FAILED can follow any running state; review states can expire (SPEC §5.1).
for _state in RUNNING:
    ALLOWED[_state] = ALLOWED.get(_state, frozenset()) | {S.FAILED}
for _state in REVIEW:
    ALLOWED[_state] = ALLOWED[_state] | {S.EXPIRED}


class InvalidTransition(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plain(value: Any) -> Any:
    """DynamoDB values -> JSON-safe Python (Decimal -> int/float)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | set | tuple):
        return [plain(v) for v in value]
    return value


class JobRepo:
    def __init__(
        self,
        audit: AuditWriter,
        table_name: str | None = None,
        *,
        dynamodb_resource: Any | None = None,
    ) -> None:
        self.table_name = table_name or os.environ["JOBS_TABLE"]
        self.audit = audit
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(self.table_name)

    def create(
        self,
        *,
        actor: Actor,
        owner_email: str,
        owner_sub: str,
        filename: str,
        enrich: bool,
        upload_key_for: Any,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a job in AWAITING_UPLOAD. `upload_key_for(job_id)` names the S3 key."""
        job_id = new_job_id()
        ts = now_iso()
        job: dict[str, Any] = {
            "job_id": job_id,
            "owner_email": owner_email,
            "owner_sub": owner_sub,
            "filename": filename,
            "enrich": enrich,
            "state": str(S.AWAITING_UPLOAD),
            "created_at": ts,
            "updated_at": ts,
            "upload_key": upload_key_for(job_id),
        }
        events = [
            AuditEvent(
                event_type=EventType.JOB_CREATED,
                actor=actor,
                job_id=job_id,
                details={"enrich": enrich, "filename": filename},
                correlation_id=correlation_id,
            ),
            AuditEvent(
                event_type=EventType.JOB_STATE_CHANGED,
                actor=actor,
                job_id=job_id,
                before={"state": None},
                after={"state": str(S.AWAITING_UPLOAD)},
                correlation_id=correlation_id,
            ),
        ]
        self.audit.transact(
            events,
            [
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": job,
                        "ConditionExpression": "attribute_not_exists(job_id)",
                    }
                }
            ],
        )
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        item = self._table.get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")
        return plain(item) if item else None

    def list_for_owner(self, owner_email: str, limit: int = 50) -> list[dict[str, Any]]:
        resp = self._table.query(
            IndexName="by_owner",
            KeyConditionExpression=Key("owner_email").eq(owner_email),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [plain(i) for i in resp.get("Items", [])]

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        # Admin view. A scan is fine at this app's volume; revisit past ~10k jobs.
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            resp = self._table.scan(**kwargs)
            items.extend(plain(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        items.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return items[:limit]

    def transition(
        self,
        job_id: str,
        from_state: JobState,
        to_state: JobState,
        *,
        actor: Actor,
        set_fields: dict[str, Any] | None = None,
        remove_fields: Sequence[str] = (),
        events: Sequence[AuditEvent] = (),
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Move `job_id` from `from_state` to `to_state` if it is still in `from_state`.

        Raises `StateConflict` if the job has moved on, `AuditWriteError` if the
        audit write fails; in both cases nothing is written.
        """
        if to_state not in ALLOWED.get(from_state, frozenset()):
            raise InvalidTransition(f"{from_state} -> {to_state}")
        change = AuditEvent(
            event_type=EventType.JOB_STATE_CHANGED,
            actor=actor,
            job_id=job_id,
            before={"state": str(from_state)},
            after={"state": str(to_state)},
            details=details,
            correlation_id=correlation_id,
        )
        self.update_in_state(
            job_id,
            from_state,
            set_fields={"state": str(to_state), **(set_fields or {})},
            remove_fields=remove_fields,
            events=[*events, change],
        )

    def update_cached(self, job_id: str, state: JobState, set_fields: dict[str, Any]) -> None:
        """Refresh derived, recomputable data (e.g. summary counts) with no audit event.

        Never use this for anything a user or the system decided; use
        `update_in_state` for that.
        """
        names = {f"#f{i}": name for i, name in enumerate(set_fields)}
        values = {f":v{i}": to_dynamo(value) for i, value in enumerate(set_fields.values())}
        names["#state"] = "state"
        values[":expected"] = str(state)
        self._table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(set_fields))),
            ConditionExpression="#state = :expected",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def condition_in_state(self, job_id: str, state: JobState) -> dict[str, Any]:
        """A TransactWriteItems ConditionCheck: the job is still in `state`."""
        return {
            "ConditionCheck": {
                "TableName": self.table_name,
                "Key": {"job_id": job_id},
                "ConditionExpression": "#state = :expected",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":expected": str(state)},
            }
        }

    def update_in_state(
        self,
        job_id: str,
        state: JobState,
        *,
        set_fields: dict[str, Any],
        events: Sequence[AuditEvent],
        remove_fields: Sequence[str] = (),
    ) -> None:
        """Update a job only if it is in `state`, atomically with its audit events.

        Raises `StateConflict` if the job isn't in `state`, `AuditWriteError` if
        the audit write fails; in both cases nothing is written.
        """
        if not events:
            raise ValueError("every job update must carry at least one audit event")
        fields = {"updated_at": now_iso(), **set_fields}
        names = {f"#f{i}": name for i, name in enumerate(fields)}
        values = {f":v{i}": value for i, value in enumerate(fields.values())}
        expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        if remove_fields:
            remove_names = {f"#r{i}": name for i, name in enumerate(remove_fields)}
            names.update(remove_names)
            expr += " REMOVE " + ", ".join(remove_names)
        names["#state"] = "state"
        values[":expected"] = str(state)
        self.audit.transact(
            events,
            [
                {
                    "Update": {
                        "TableName": self.table_name,
                        "Key": {"job_id": job_id},
                        "UpdateExpression": expr,
                        "ConditionExpression": "#state = :expected",
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": values,
                    }
                }
            ],
        )

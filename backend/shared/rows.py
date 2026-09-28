"""Rows table access (SPEC §7.2)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from shared.audit import to_dynamo
from shared.jobs import plain
from shared.parsing import ParsedRow

DAY_SECONDS = 86_400


class RowRepo:
    def __init__(
        self,
        table_name: str | None = None,
        *,
        retention_days: int | None = None,
        dynamodb_resource: Any | None = None,
    ) -> None:
        self.table_name = table_name or os.environ["ROWS_TABLE"]
        self.retention_days = retention_days or int(os.environ.get("ROW_RETENTION_DAYS", "90"))
        self._resource = dynamodb_resource or boto3.resource("dynamodb")
        self._table = self._resource.Table(self.table_name)

    def put_parsed(self, job_id: str, rows: Iterable[ParsedRow]) -> int:
        """Store source rows verbatim. Idempotent: re-running overwrites the same keys."""
        expires_at = int(time.time()) + self.retention_days * DAY_SECONDS
        count = 0
        with self._table.batch_writer() as batch:
            for row in rows:
                batch.put_item(
                    Item={
                        "job_id": job_id,
                        "row_id": row.row_id,
                        "source": row.values,
                        # Kept apart from analysis issues, which are recomputed on every run.
                        "issues_parse": [i.as_dict() for i in row.issues],
                        "expires_at": expires_at,
                    }
                )
                count += 1
        return count

    def get(self, job_id: str, row_id: int) -> dict[str, Any] | None:
        item = self._table.get_item(
            Key={"job_id": job_id, "row_id": row_id}, ConsistentRead=True
        ).get("Item")
        return plain(item) if item else None

    def get_many(self, job_id: str, row_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = list(row_ids)
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), 100):
            keys = [{"job_id": job_id, "row_id": rid} for rid in ids[start : start + 100]]
            request: dict[str, Any] = {self.table_name: {"Keys": keys, "ConsistentRead": True}}
            while request:
                resp = self._resource.batch_get_item(RequestItems=request)
                out.extend(plain(i) for i in resp["Responses"].get(self.table_name, []))
                request = resp.get("UnprocessedKeys") or {}
        out.sort(key=lambda r: r["row_id"])
        return out

    def set_ai_flags(
        self, job_id: str, flags_by_row: dict[int, list[dict[str, Any]]], status: str
    ) -> None:
        for row_id, flags in flags_by_row.items():
            self._table.update_item(
                Key={"job_id": job_id, "row_id": row_id},
                UpdateExpression="SET ai_flags = :f, ai_status = :s",
                ExpressionAttributeValues={":f": to_dynamo(flags), ":s": status},
            )

    def put_all(self, items: Iterable[dict[str, Any]]) -> None:
        with self._table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=to_dynamo(item))

    def first(self, job_id: str, limit: int) -> list[dict[str, Any]]:
        resp = self._table.query(KeyConditionExpression=Key("job_id").eq(job_id), Limit=limit)
        return [plain(i) for i in resp.get("Items", [])]

    def list(self, job_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("job_id").eq(job_id)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(plain(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

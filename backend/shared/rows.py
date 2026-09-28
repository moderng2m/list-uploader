"""Rows table access (SPEC §7.2)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

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
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(self.table_name)

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
                        "issues": [
                            {**i.as_dict(), "source": "parse", "field": None} for i in row.issues
                        ],
                        "expires_at": expires_at,
                    }
                )
                count += 1
        return count

    def list(self, job_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("job_id").eq(job_id)}
        while True:
            resp = self._table.query(**kwargs)
            items.extend(plain(i) for i in resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

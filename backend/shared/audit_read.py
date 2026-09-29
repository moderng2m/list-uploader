"""Read side of the audit trail (SPEC §21.2.6): job timeline, row history, search.

Reads never change anything. The BFF role holds Query/Scan/GetItem on AuditEvents
and still no Update/Delete (infra/tests/test_synth.py).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

from shared.audit import GLOBAL_PARTITION, email_sha256
from shared.jobs import plain

_JSON_FIELDS = ("subject", "before", "after", "details")
EMAIL_INDEX = "by_email"


def parse_item(item: dict[str, Any]) -> dict[str, Any]:
    """A stored event with its JSON payloads decoded and Decimals made plain."""
    out: dict[str, Any] = dict(plain(item))
    for key in _JSON_FIELDS:
        if isinstance(out.get(key), str):
            with contextlib.suppress(ValueError):
                out[key] = json.loads(out[key])
    if out.get("job_id") == GLOBAL_PARTITION:
        out["job_id"] = None
    return out


@dataclass(frozen=True)
class Page:
    events: list[dict[str, Any]]
    next_cursor: str | None


@dataclass(frozen=True)
class SearchCriteria:
    email: str | None = None
    job_id: str | None = None
    user: str | None = None
    campaign_id: str | None = None
    event_type: str | None = None
    date_from: str | None = None  # ISO date or timestamp, inclusive
    date_to: str | None = None  # ISO date or timestamp, inclusive

    def is_empty(self) -> bool:
        return not any(vars(self).values())

    def matches(self, event: dict[str, Any]) -> bool:
        if self.event_type and event.get("event_type") != self.event_type:
            return False
        if self.user and (event.get("actor") or {}).get("email", "").lower() != self.user.lower():
            return False
        at = str(event.get("occurred_at", ""))
        if self.date_from and at < self.date_from:
            return False
        # A bare date includes the whole day.
        return not (self.date_to and at > self.date_to + ("~" if len(self.date_to) == 10 else ""))


class AuditReader:
    def __init__(self, table_name: str | None = None, *, dynamodb_resource: Any = None) -> None:
        self._table = (dynamodb_resource or boto3.resource("dynamodb")).Table(
            table_name or os.environ["AUDIT_TABLE"]
        )

    def _query(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        while True:
            resp = self._table.query(**kwargs)
            yield from resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                return
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def job_page(self, job_id: str, *, include_rows: bool, cursor: str | None, limit: int) -> Page:
        """Events for one job, oldest first. Without rows, per-row events are left out
        (they're in the row history drawer)."""
        kwargs: dict[str, Any] = {"KeyConditionExpression": Key("job_id").eq(job_id)}
        if cursor:
            kwargs["KeyConditionExpression"] &= Key("sk").gt(cursor)
        if not include_rows:
            kwargs["FilterExpression"] = Attr("row_id").not_exists()
        events: list[dict[str, Any]] = []
        for item in self._query(**kwargs):
            events.append(parse_item(item))
            if len(events) == limit:
                return Page(events, str(item["sk"]))
        return Page(events, None)

    def row_events(self, job_id: str, row_id: int) -> list[dict[str, Any]]:
        return [
            parse_item(i)
            for i in self._query(
                KeyConditionExpression=Key("job_id").eq(job_id),
                FilterExpression=Attr("row_id").eq(row_id),
            )
        ]

    def by_email(self, email: str) -> list[dict[str, Any]]:
        return [
            parse_item(i)
            for i in self._query(
                IndexName=EMAIL_INDEX,
                KeyConditionExpression=Key("email_sha256").eq(email_sha256(email)),
            )
        ]

    def scan(self, keep: Callable[[dict[str, Any]], bool]) -> Iterator[dict[str, Any]]:
        """Every event. Fine at this app's volume; Athena over the archive is the
        answer past that (SPEC §21.2.4)."""
        kwargs: dict[str, Any] = {}
        while True:
            resp = self._table.scan(**kwargs)
            for item in resp.get("Items", []):
                event = parse_item(item)
                if keep(event):
                    yield event
            if "LastEvaluatedKey" not in resp:
                return
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def search(
        self,
        criteria: SearchCriteria,
        *,
        jobs_with_campaign: Callable[[str], list[str]],
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Events matching every given criterion, newest first; (events, truncated).

        The narrowest source is read first: the email index, one job, the jobs using a
        campaign, else a scan. The remaining criteria filter the result.
        """
        source: Iterator[dict[str, Any]] | list[dict[str, Any]]
        if criteria.email:
            source = self.by_email(criteria.email)
            if criteria.job_id:
                source = [e for e in source if e.get("job_id") == criteria.job_id]
        elif criteria.job_id:
            key = Key("job_id").eq(criteria.job_id)
            source = [parse_item(i) for i in self._query(KeyConditionExpression=key)]
        elif criteria.campaign_id:
            source = [
                parse_item(i)
                for job_id in jobs_with_campaign(criteria.campaign_id)
                for i in self._query(KeyConditionExpression=Key("job_id").eq(job_id))
            ]
        else:
            source = self.scan(criteria.matches)
        if criteria.campaign_id and (criteria.email or criteria.job_id):
            allowed = set(jobs_with_campaign(criteria.campaign_id))
            source = [e for e in source if e.get("job_id") in allowed]
        matched = [e for e in source if criteria.matches(e)]
        matched.sort(key=lambda e: str(e.get("sk", "")), reverse=True)
        return matched[:limit], len(matched) > limit

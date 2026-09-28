"""AuditEvents stream -> Firehose -> S3 audit archive (SPEC §21.2.4).

Only INSERTs are forwarded; the table is append-only, so any MODIFY/REMOVE is
unexpected and is counted as a metric rather than archived.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer

from shared.observability import logger, metrics

FIREHOSE_BATCH = 500
_deserializer = TypeDeserializer()
_firehose: Any = None


def _client() -> Any:
    global _firehose
    if _firehose is None:
        _firehose = boto3.client("firehose")
    return _firehose


def to_archive_record(new_image: dict[str, Any]) -> dict[str, Any]:
    item = {k: _deserializer.deserialize(v) for k, v in new_image.items()}
    record: dict[str, Any] = {}
    for key, value in item.items():
        if key in ("subject", "before", "after", "details") and isinstance(value, str):
            record[key] = json.loads(value)
        elif isinstance(value, Decimal):
            record[key] = int(value) if value == int(value) else float(value)
        else:
            record[key] = value
    return record


def handler(event: dict[str, Any], context: Any, firehose: Any = None) -> dict[str, int]:
    client = firehose or _client()
    stream = os.environ["AUDIT_FIREHOSE_STREAM"]
    records: list[dict[str, bytes]] = []
    unexpected = 0
    for rec in event.get("Records", []):
        if rec.get("eventName") != "INSERT":
            unexpected += 1
            continue
        archived = to_archive_record(rec["dynamodb"]["NewImage"])
        records.append({"Data": (json.dumps(archived, default=str) + "\n").encode("utf-8")})

    for start in range(0, len(records), FIREHOSE_BATCH):
        batch = records[start : start + FIREHOSE_BATCH]
        resp = client.put_record_batch(DeliveryStreamName=stream, Records=batch)
        if resp.get("FailedPutCount", 0):
            # Raise so the stream retries the whole batch; archive is at-least-once.
            raise RuntimeError(f"firehose rejected {resp['FailedPutCount']} audit records")

    if unexpected:
        metrics.add_metric(name="AuditUnexpectedStreamEvents", unit="Count", value=unexpected)
        logger.error("non-insert events on append-only audit table", extra={"count": unexpected})
    return {"archived": len(records), "unexpected": unexpected}

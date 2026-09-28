from __future__ import annotations

import json
from typing import Any

import pytest

from audit_archiver.handler import handler


class FakeFirehose:
    def __init__(self, failed: int = 0) -> None:
        self.batches: list[list[dict[str, bytes]]] = []
        self.failed = failed

    def put_record_batch(self, DeliveryStreamName: str, Records: list[Any]) -> dict[str, int]:
        self.batches.append(Records)
        return {"FailedPutCount": self.failed}


def _insert(event_id: str) -> dict[str, Any]:
    return {
        "eventName": "INSERT",
        "dynamodb": {
            "NewImage": {
                "job_id": {"S": "j_1"},
                "event_id": {"S": event_id},
                "row_id": {"N": "12"},
                "after": {"S": '{"title": "VP Marketing"}'},
            }
        },
    }


@pytest.fixture(autouse=True)
def _stream_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_FIREHOSE_STREAM", "audit-stream")


def test_forwards_inserts_as_json_lines() -> None:
    fh = FakeFirehose()
    result = handler({"Records": [_insert("e1"), _insert("e2")]}, None, firehose=fh)
    assert result == {"archived": 2, "unexpected": 0}
    first = json.loads(fh.batches[0][0]["Data"])
    assert first == {
        "job_id": "j_1",
        "event_id": "e1",
        "row_id": 12,
        # Payloads stay JSON text, as in the table (stable Glue schema).
        "after": '{"title": "VP Marketing"}',
    }


def test_counts_non_inserts() -> None:
    result = handler(
        {"Records": [{"eventName": "REMOVE", "dynamodb": {}}]}, None, firehose=FakeFirehose()
    )
    assert result == {"archived": 0, "unexpected": 1}


def test_ttl_expiry_is_expected() -> None:
    expiry = {
        "eventName": "REMOVE",
        "userIdentity": {"type": "Service", "principalId": "dynamodb.amazonaws.com"},
        "dynamodb": {},
    }
    result = handler({"Records": [expiry]}, None, firehose=FakeFirehose())
    assert result == {"archived": 0, "unexpected": 0}


def test_firehose_rejection_raises_for_retry() -> None:
    with pytest.raises(RuntimeError):
        handler({"Records": [_insert("e1")]}, None, firehose=FakeFirehose(failed=1))

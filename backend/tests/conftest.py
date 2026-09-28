from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "ListUploader")
os.environ.setdefault("ENV", "dev")

AUDIT_TABLE = "AuditEvents-test"
JOBS_TABLE = "Jobs-test"
ROWS_TABLE = "Rows-test"
UPLOADS_BUCKET = "uploads-test"


@pytest.fixture
def aws() -> Iterator[None]:
    with mock_aws():
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        yield


def _table(name: str, pk: tuple[str, str], sk: tuple[str, str] | None = None, **extra: Any) -> Any:
    keys = [{"AttributeName": pk[0], "KeyType": "HASH"}]
    attrs = {pk[0]: pk[1]}
    if sk:
        keys.append({"AttributeName": sk[0], "KeyType": "RANGE"})
        attrs[sk[0]] = sk[1]
    for gsi in extra.get("GlobalSecondaryIndexes", []):
        for key in gsi["KeySchema"]:
            attrs.setdefault(key["AttributeName"], "S")
    return boto3.resource("dynamodb").create_table(
        TableName=name,
        KeySchema=keys,
        AttributeDefinitions=[{"AttributeName": k, "AttributeType": t} for k, t in attrs.items()],
        BillingMode="PAY_PER_REQUEST",
        **extra,
    )


@pytest.fixture
def audit_table(aws: None) -> Any:
    return _table(AUDIT_TABLE, ("job_id", "S"), ("sk", "S"))


@pytest.fixture
def jobs_table(aws: None) -> Any:
    return _table(
        JOBS_TABLE,
        ("job_id", "S"),
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by_owner",
                "KeySchema": [
                    {"AttributeName": "owner_email", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


@pytest.fixture
def rows_table(aws: None) -> Any:
    return _table(ROWS_TABLE, ("job_id", "S"), ("row_id", "N"))


@pytest.fixture
def uploads_bucket(aws: None) -> str:
    boto3.client("s3").create_bucket(Bucket=UPLOADS_BUCKET, ObjectLockEnabledForBucket=True)
    return UPLOADS_BUCKET


@dataclass
class Env:
    """Wired-up app dependencies against moto."""

    audit: Any
    jobs: Any
    rows: Any
    s3: Any
    audit_table: Any
    jobs_table: Any
    rows_table: Any
    parse_requests: list[str] = field(default_factory=list)

    def audit_types(self, job_id: str) -> list[str]:
        items = self.audit_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("job_id").eq(job_id)
        )["Items"]
        return [i["event_type"] for i in items]


@pytest.fixture
def env(audit_table: Any, jobs_table: Any, rows_table: Any, uploads_bucket: str) -> Env:
    from shared.audit import AuditWriter
    from shared.jobs import JobRepo
    from shared.rows import RowRepo

    audit = AuditWriter(AUDIT_TABLE, env="dev", app_version="t")
    return Env(
        audit=audit,
        jobs=JobRepo(audit, JOBS_TABLE),
        rows=RowRepo(ROWS_TABLE, retention_days=90),
        s3=boto3.client("s3"),
        audit_table=audit_table,
        jobs_table=jobs_table,
        rows_table=rows_table,
    )

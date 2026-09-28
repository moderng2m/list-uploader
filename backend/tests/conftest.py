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
CONFIG_TABLE = "Config-test"
JOBS_TABLE = "Jobs-test"
ROWS_TABLE = "Rows-test"
UPLOADS_BUCKET = "uploads-test"
PROCESSED_BUCKET = "processed-test"


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
def config_table(aws: None) -> Any:
    return _table(CONFIG_TABLE, ("pk", "S"), ("sk", "S"))


@pytest.fixture
def uploads_bucket(aws: None) -> str:
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket=UPLOADS_BUCKET, ObjectLockEnabledForBucket=True)
    s3.create_bucket(Bucket=PROCESSED_BUCKET)
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
    config: Any
    bedrock: Any
    workato: Any = None
    parse_requests: list[str] = field(default_factory=list)
    analysis_requests: list[str] = field(default_factory=list)
    enrichment_requests: list[str] = field(default_factory=list)
    send_requests: list[tuple[str, bool]] = field(default_factory=list)

    def bff_deps(self, **overrides: Any) -> Any:
        from bff.app import BffDeps

        kwargs: dict[str, Any] = {
            "audit": self.audit,
            "jobs": self.jobs,
            "rows": self.rows,
            "s3": self.s3,
            "uploads_bucket": UPLOADS_BUCKET,
            "start_parse": self.parse_requests.append,
            "start_analysis": self.analysis_requests.append,
            "start_enrichment": self.enrichment_requests.append,
            "start_send": lambda job_id, only_failed: self.send_requests.append(
                (job_id, only_failed)
            ),
            "processed_bucket": PROCESSED_BUCKET,
            "config": self.config,
            "workato": self.workato,
            **overrides,
        }
        return BffDeps(**kwargs)

    def analyze_deps(self, **overrides: Any) -> Any:
        from tasks.analyze import AnalyzeDeps

        kwargs: dict[str, Any] = {
            "jobs": self.jobs,
            "rows": self.rows,
            "workato": self.workato,
            "bedrock": self.bedrock,
            "config": self.config,
            "today": lambda: "2026-09-28",
            **overrides,
        }
        return AnalyzeDeps(**kwargs)

    def enrich_deps(self, **overrides: Any) -> Any:
        from shared.enrichment import ZoomInfoProvider
        from tasks.enrich import EnrichDeps

        kwargs: dict[str, Any] = {
            "jobs": self.jobs,
            "rows": self.rows,
            "provider": ZoomInfoProvider(self.workato, sleep=lambda _: None),
            **overrides,
        }
        return EnrichDeps(**kwargs)

    def send_deps(self, **overrides: Any) -> Any:
        from tasks.send import SendDeps

        kwargs: dict[str, Any] = {
            "jobs": self.jobs,
            "rows": self.rows,
            "workato": self.workato,
            "env": "dev",
            "send_to_prod": False,
            **overrides,
        }
        return SendDeps(**kwargs)

    def parse_deps(self, **overrides: Any) -> Any:
        from tasks.parse_file import ParseDeps

        kwargs: dict[str, Any] = {
            "jobs": self.jobs,
            "rows": self.rows,
            "s3": self.s3,
            "uploads_bucket": UPLOADS_BUCKET,
            "bedrock": self.bedrock,
            "config": self.config,
            **overrides,
        }
        return ParseDeps(**kwargs)

    def audit_types(self, job_id: str) -> list[str]:
        items = self.audit_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("job_id").eq(job_id)
        )["Items"]
        return [i["event_type"] for i in items]


@pytest.fixture
def env(
    audit_table: Any, jobs_table: Any, rows_table: Any, config_table: Any, uploads_bucket: str
) -> Env:
    from shared.audit import AuditWriter
    from shared.bedrock_client import FakeBedrockClient
    from shared.config_store import ConfigStore
    from shared.fake_sfdc import CAMPAIGNS
    from shared.fake_zoominfo import enrich_handler
    from shared.jobs import JobRepo
    from shared.rows import RowRepo
    from shared.workato_client import FakeWorkatoClient

    audit = AuditWriter(AUDIT_TABLE, env="dev", app_version="t")
    return Env(
        audit=audit,
        jobs=JobRepo(audit, JOBS_TABLE),
        rows=RowRepo(ROWS_TABLE, retention_days=90),
        s3=boto3.client("s3"),
        audit_table=audit_table,
        jobs_table=jobs_table,
        rows_table=rows_table,
        config=ConfigStore(CONFIG_TABLE),
        # No AI suggestions unless a test queues some.
        bedrock=FakeBedrockClient(default='{"items": []}'),
        workato=FakeWorkatoClient(
            env="dev", campaigns=dict(CAMPAIGNS), enrich_handler=enrich_handler
        ),
    )


@pytest.fixture
def app_env(env: Env) -> Iterator[Env]:
    """`env` with the BFF wired to it."""
    from bff.app import set_deps

    set_deps(env.bff_deps())
    yield env
    set_deps(None)

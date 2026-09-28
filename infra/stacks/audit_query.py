"""SQL access to the audit archive for MOps (SPEC §21.2.4, §21.2.5, §21.3.3).

- Glue table `events` over s3://<archive>/audit/dt=YYYY-MM-DD/ (JSON lines from
  Firehose), with partition projection so new days need no crawler.
- Athena workgroup `list-uploader-audit` with enforced settings; results go to a
  KMS-encrypted bucket that expires them after 30 days.
- Role `mops-audit-reader`: read-only on the archive, the catalog and that
  workgroup. Who may assume it is decided outside the app (the SSO permission set
  that identifies MOps, D14); here the trust is the account, and a principal still
  needs its own permission to assume the role. App admins get nothing from being
  app admins: Cognito groups carry no AWS access.
- CloudTrail S3 data events on the archive, because querying it reads PII.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cloudtrail as cloudtrail
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.storage import StorageStack

ARCHIVE_PREFIX = "audit/"

# Matches the records audit_archiver writes (shared.audit.AuditEvent.to_item).
COLUMNS = [
    ("event_id", "string"),
    ("event_type", "string"),
    ("occurred_at", "string"),
    ("env", "string"),
    ("app_version", "string"),
    ("actor", "struct<type:string,email:string,sub:string,ip:string>"),
    ("job_id", "string"),
    ("sk", "string"),
    ("row_id", "bigint"),
    ("correlation_id", "string"),
    ("reason", "string"),
    ("email_sha256", "string"),
    ("subject", "string"),
    ("before", "string"),
    ("after", "string"),
    ("details", "string"),
    ("expires_at", "bigint"),
]


def business_queries(table: str) -> dict[str, tuple[str, str]]:
    """SPEC §21.3.3 business dashboard, as saved queries (name: (description, SQL))."""
    return {
        "job-history": (
            "Every event for one upload, oldest first. Replace the job ID.",
            f"SELECT occurred_at, event_type, row_id, actor.email AS actor, reason, before, "
            f"after, details FROM {table} WHERE job_id = 'j_REPLACE_ME' ORDER BY sk",
        ),
        "person-lookup": (
            "Every upload that included a person. Replace the email (lowercase).",
            f"SELECT DISTINCT job_id, min(occurred_at) AS first_seen FROM {table} "
            f"WHERE email_sha256 = lower(to_hex(sha256(to_utf8(lower(trim('ada@acme.example')))))) "
            f"GROUP BY job_id ORDER BY first_seen",
        ),
        "uploads-per-week-by-campaign": (
            "Leads submitted per week and campaign.",
            f"SELECT date_trunc('week', from_iso8601_timestamp(occurred_at)) AS week, "
            f"json_extract_scalar(details, '$.payload.campaign_id') AS campaign_id, "
            f"count(DISTINCT job_id) AS uploads, count(*) AS rows_sent FROM {table} "
            f"WHERE event_type = 'ROW_SUBMITTED' GROUP BY 1, 2 ORDER BY 1 DESC, 4 DESC",
        ),
        "time-from-upload-to-send": (
            "Hours from upload to the user confirming the send, per job.",
            f"SELECT job_id, date_diff('minute', min(CASE WHEN event_type = 'JOB_CREATED' "
            f"THEN from_iso8601_timestamp(occurred_at) END), min(CASE WHEN event_type = "
            f"'SEND_CONFIRMED' THEN from_iso8601_timestamp(occurred_at) END)) / 60.0 AS hours "
            f"FROM {table} GROUP BY job_id HAVING count_if(event_type = 'SEND_CONFIRMED') > 0 "
            f"ORDER BY hours DESC",
        ),
        "top-issue-codes": (
            "Issues raised by code, across all uploads.",
            f"SELECT json_extract_scalar(issue, '$.code') AS code, count(*) AS raised FROM "
            f"{table} CROSS JOIN UNNEST(CAST(json_extract(details, '$.issues') AS "
            f"array(json))) AS t(issue) WHERE event_type = 'ISSUE_RAISED' GROUP BY 1 "
            f"ORDER BY 2 DESC",
        ),
        "enrichment-yield": (
            "ZoomInfo results by match status.",
            f"SELECT json_extract_scalar(details, '$.status') AS status, count(*) AS rows "
            f"FROM {table} WHERE event_type = 'ENRICHMENT_RESULT' GROUP BY 1 ORDER BY 2 DESC",
        ),
        "ai-accept-rate": (
            "AI suggestions accepted vs changed, by kind.",
            f"SELECT json_extract_scalar(subject, '$.field') AS kind, "
            f"count_if(event_type = 'SUGGESTION_ACCEPTED') AS accepted, "
            f"count_if(event_type = 'SUGGESTION_REJECTED') AS changed FROM {table} "
            f"WHERE event_type IN ('SUGGESTION_ACCEPTED', 'SUGGESTION_REJECTED') GROUP BY 1",
        ),
        "template-adoption": (
            "Column mapping method mix per upload (exact means the template was used).",
            f"SELECT job_id, json_extract(details, '$.method_counts') AS method_counts FROM "
            f"{table} WHERE event_type = 'MAPPING_SUGGESTED' ORDER BY occurred_at DESC",
        ),
    }


class AuditQueryStack(Stack):
    def __init__(
        self, scope: Construct, cid: str, *, cfg: EnvConfig, storage: StorageStack, **kwargs: object
    ) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]
        suffix = "" if cfg.name == "prod" else f"-{cfg.name}"
        archive = storage.audit_archive

        self.database_name = f"list_uploader_{cfg.name}_audit"
        database = glue.CfnDatabase(
            self,
            "Database",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=self.database_name,
                description="List Uploader audit archive (PII: restricted to MOps)",
            ),
        )
        location = f"s3://{archive.bucket_name}/{ARCHIVE_PREFIX}"
        table = glue.CfnTable(
            self,
            "Events",
            catalog_id=self.account,
            database_name=self.database_name,
            table_input=glue.CfnTable.TableInputProperty(
                name="events",
                table_type="EXTERNAL_TABLE",
                partition_keys=[glue.CfnTable.ColumnProperty(name="dt", type="string")],
                parameters={
                    "classification": "json",
                    "projection.enabled": "true",
                    "projection.dt.type": "date",
                    "projection.dt.format": "yyyy-MM-dd",
                    "projection.dt.range": "2026-01-01,NOW",
                    "projection.dt.interval": "1",
                    "projection.dt.interval.unit": "DAYS",
                    "storage.location.template": f"{location}dt=${{dt}}/",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=location,
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    compressed=True,
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe",
                        parameters={"ignore.malformed.json": "false"},
                    ),
                    columns=[glue.CfnTable.ColumnProperty(name=n, type=t) for n, t in COLUMNS],
                ),
            ),
        )
        table.add_dependency(database)
        self.table_name = f"{self.database_name}.events"

        self.results = s3.Bucket(
            self,
            "AthenaResults",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=storage.key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(cfg.athena_results_retention_days),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.workgroup_name = f"list-uploader-audit{suffix}"
        workgroup = athena.CfnWorkGroup(
            self,
            "Workgroup",
            name=self.workgroup_name,
            description="Queries over the List Uploader audit archive (logged)",
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
                bytes_scanned_cutoff_per_query=10 * 1024**3,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{self.results.bucket_name}/",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_KMS", kms_key=storage.key.key_arn
                    ),
                ),
            ),
        )
        for name, (description, sql) in business_queries(self.table_name).items():
            query = athena.CfnNamedQuery(
                self,
                f"Query-{name}",
                name=name,
                description=description,
                database=self.database_name,
                query_string=sql,
                work_group=self.workgroup_name,
            )
            query.add_dependency(workgroup)

        self.reader = self._reader_role(cfg, suffix, storage, workgroup)

        # Reading the archive reads PII, so every object access is logged.
        trail_bucket = s3.Bucket(
            self,
            "AccessTrail",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(cfg.audit_retention_days))],
            removal_policy=RemovalPolicy.RETAIN,
        )
        trail = cloudtrail.Trail(
            self,
            "ArchiveAccess",
            trail_name=f"list-uploader{suffix}-audit-archive-access",
            bucket=trail_bucket,
            enable_file_validation=True,
            include_global_service_events=False,
            is_multi_region_trail=False,
            management_events=cloudtrail.ReadWriteType.NONE,
        )
        trail.add_s3_event_selector(
            [cloudtrail.S3EventSelector(bucket=archive, object_prefix=ARCHIVE_PREFIX)],
            read_write_type=cloudtrail.ReadWriteType.ALL,
            include_management_events=False,
        )

    def _reader_role(
        self, cfg: EnvConfig, suffix: str, storage: StorageStack, workgroup: athena.CfnWorkGroup
    ) -> iam.Role:
        archive = storage.audit_archive
        role = iam.Role(
            self,
            "MopsAuditReader",
            role_name=f"mops-audit-reader{suffix}",
            description="MOps: read-only SQL over the audit archive (SPEC §21.2.5)",
            assumed_by=iam.AccountPrincipal(self.account),
            max_session_duration=Duration.hours(4),
        )
        workgroup_arn = (
            f"arn:{self.partition}:athena:{self.region}:{self.account}:workgroup/{workgroup.name}"
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AuditWorkgroupOnly",
                actions=[
                    "athena:StartQueryExecution",
                    "athena:StopQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:GetQueryResultsStream",
                    "athena:ListQueryExecutions",
                    "athena:BatchGetQueryExecution",
                    "athena:GetWorkGroup",
                    "athena:ListNamedQueries",
                    "athena:GetNamedQuery",
                    "athena:BatchGetNamedQuery",
                ],
                resources=[workgroup_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="AuditCatalog",
                actions=[
                    "glue:GetDatabase",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartitions",
                ],
                resources=[
                    f"arn:{self.partition}:glue:{self.region}:{self.account}:catalog",
                    f"arn:{self.partition}:glue:{self.region}:{self.account}:database/"
                    f"{self.database_name}",
                    f"arn:{self.partition}:glue:{self.region}:{self.account}:table/"
                    f"{self.database_name}/*",
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadArchive",
                actions=["s3:GetObject"],
                resources=[archive.arn_for_objects(f"{ARCHIVE_PREFIX}*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListArchive",
                actions=["s3:ListBucket", "s3:GetBucketLocation"],
                resources=[archive.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{ARCHIVE_PREFIX}*", ""]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="QueryResults",
                actions=[
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                resources=[self.results.arn_for_objects("*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListResults",
                actions=["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
                resources=[self.results.bucket_arn],
            )
        )
        storage.key.grant(role, "kms:Decrypt", "kms:GenerateDataKey")
        return role

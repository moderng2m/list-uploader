"""S3 buckets, DynamoDB tables, and the audit archive pipeline (SPEC §4.1, §21.2.4)."""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kinesisfirehose as firehose
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import EnvConfig
from infra.lambda_code import ARCH, RUNTIME, backend_code

# Actions an app role must never hold on AuditEvents (SPEC §21.2.4).
AUDIT_FORBIDDEN_ACTIONS = [
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:PartiQLUpdate",
    "dynamodb:PartiQLDelete",
    "dynamodb:DeleteTable",
    "dynamodb:UpdateTable",
]


class StorageStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: EnvConfig, **kwargs: object) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]

        self.key = kms.Key(
            self,
            "DataKey",
            alias=f"alias/list-uploader-{cfg.name}",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- S3 ---------------------------------------------------------------
        common: dict[str, object] = {
            "encryption": s3.BucketEncryption.KMS,
            "encryption_key": self.key,
            "bucket_key_enabled": True,
            "block_public_access": s3.BlockPublicAccess.BLOCK_ALL,
            "enforce_ssl": True,
            "minimum_tls_version": 1.2,
            "versioned": True,
            "removal_policy": RemovalPolicy.RETAIN,
        }

        # Raw uploads are write-once. Object Lock is on, but with no default retention:
        # S3 would then demand a Content-MD5 on every browser upload. Instead the BFF
        # locks the exact version it will parse (put_object_retention) at confirm time.
        self.uploads = s3.Bucket(
            self,
            "Uploads",
            object_lock_enabled=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(cfg.raw_file_retention_days),
                    noncurrent_version_expiration=Duration.days(1),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.POST],  # presigned POST
                    allowed_origins=["*"],  # narrowed to the CloudFront origin in P7
                    allowed_headers=["*"],
                    max_age=3000,
                )
            ],
            **common,  # type: ignore[arg-type]
        )
        self.processed = s3.Bucket(
            self,
            "Processed",
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(cfg.raw_file_retention_days),
                    noncurrent_version_expiration=Duration.days(1),
                )
            ],
            **common,  # type: ignore[arg-type]
        )
        # System of record for audit. Governance mode (D12): a break-glass role can purge.
        self.audit_archive = s3.Bucket(
            self,
            "AuditArchive",
            object_lock_enabled=True,
            object_lock_default_retention=s3.ObjectLockRetention.governance(
                Duration.days(cfg.audit_retention_days)
            ),
            **common,  # type: ignore[arg-type]
        )

        # --- DynamoDB ---------------------------------------------------------
        table_common: dict[str, object] = {
            "billing": ddb.Billing.on_demand(),
            "encryption": ddb.TableEncryptionV2.customer_managed_key(self.key),
            "point_in_time_recovery_specification": ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            "removal_policy": RemovalPolicy.RETAIN,
        }
        self.jobs = ddb.TableV2(
            self,
            "Jobs",
            partition_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING),
            global_secondary_indexes=[
                ddb.GlobalSecondaryIndexPropsV2(
                    index_name="by_owner",
                    partition_key=ddb.Attribute(name="owner_email", type=ddb.AttributeType.STRING),
                    sort_key=ddb.Attribute(name="created_at", type=ddb.AttributeType.STRING),
                )
            ],
            **table_common,  # type: ignore[arg-type]
        )
        self.rows = ddb.TableV2(
            self,
            "Rows",
            partition_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="row_id", type=ddb.AttributeType.NUMBER),
            time_to_live_attribute="expires_at",
            **table_common,  # type: ignore[arg-type]
        )
        self.config_table = ddb.TableV2(
            self,
            "Config",
            partition_key=ddb.Attribute(name="pk", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            **table_common,  # type: ignore[arg-type]
        )
        self.audit_events = ddb.TableV2(
            self,
            "AuditEvents",
            partition_key=ddb.Attribute(name="job_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
            dynamo_stream=ddb.StreamViewType.NEW_IMAGE,
            global_secondary_indexes=[
                ddb.GlobalSecondaryIndexPropsV2(
                    index_name="by_email",
                    partition_key=ddb.Attribute(name="email_sha256", type=ddb.AttributeType.STRING),
                    sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
                )
            ],
            deletion_protection=True,
            **table_common,  # type: ignore[arg-type]
        )

        # --- Audit archive: Streams -> Lambda -> Firehose -> S3 ---------------
        firehose_role = iam.Role(
            self, "AuditFirehoseRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com")
        )
        self.audit_archive.grant_put(firehose_role)
        self.audit_archive.grant_read(firehose_role)
        self.key.grant_encrypt_decrypt(firehose_role)
        # JSON lines, GZIP, partitioned by dt=. Parquet + Glue table land in P7.
        self.audit_stream = firehose.CfnDeliveryStream(
            self,
            "AuditDeliveryStream",
            delivery_stream_type="DirectPut",
            delivery_stream_encryption_configuration_input=(
                firehose.CfnDeliveryStream.DeliveryStreamEncryptionConfigurationInputProperty(
                    key_type="AWS_OWNED_CMK"
                )
            ),
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=self.audit_archive.bucket_arn,
                role_arn=firehose_role.role_arn,
                prefix="audit/dt=!{timestamp:yyyy-MM-dd}/",
                error_output_prefix="audit-errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/",
                compression_format="GZIP",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    interval_in_seconds=60, size_in_m_bs=5
                ),
                encryption_configuration=firehose.CfnDeliveryStream.EncryptionConfigurationProperty(
                    kms_encryption_config=firehose.CfnDeliveryStream.KMSEncryptionConfigProperty(
                        awskms_key_arn=self.key.key_arn
                    )
                ),
            ),
        )
        self.audit_stream.node.add_dependency(firehose_role)

        archiver = lambda_.Function(
            self,
            "AuditArchiver",
            runtime=RUNTIME,
            architecture=ARCH,
            handler="audit_archiver.handler.handler",
            code=backend_code(),
            timeout=Duration.seconds(60),
            memory_size=256,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
                "ENV": cfg.name,
                "AUDIT_FIREHOSE_STREAM": self.audit_stream.ref,
                "POWERTOOLS_SERVICE_NAME": "list-uploader",
                "POWERTOOLS_METRICS_NAMESPACE": "ListUploader",
            },
            log_group=logs.LogGroup(
                self,
                "AuditArchiverLogs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        archiver.add_event_source(
            sources.DynamoEventSource(
                self.audit_events,
                starting_position=lambda_.StartingPosition.TRIM_HORIZON,
                batch_size=500,
                retry_attempts=10,
                bisect_batch_on_error=True,
            )
        )
        archiver.add_to_role_policy(
            iam.PolicyStatement(
                actions=["firehose:PutRecordBatch"],
                resources=[self.audit_stream.attr_arn],
            )
        )
        deny_audit_mutation(archiver.role, self.audit_events)  # type: ignore[arg-type]


def grant_audit_append(grantee: iam.IRole, table: ddb.ITableV2) -> None:
    """PutItem only on AuditEvents, plus explicit denies (SPEC §21.2.4)."""
    grantee.add_to_principal_policy(
        iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[table.table_arn])
    )
    deny_audit_mutation(grantee, table)


AUDIT_READ_ACTIONS = ["dynamodb:Query", "dynamodb:Scan"]


def grant_audit_read(grantee: iam.IRole, table: ddb.ITableV2) -> None:
    """Timeline, row history and admin search (SPEC §21.2.6). Read-only; the explicit
    denies from `grant_audit_append` still apply."""
    grantee.add_to_principal_policy(
        iam.PolicyStatement(
            actions=AUDIT_READ_ACTIONS,
            resources=[table.table_arn, f"{table.table_arn}/index/*"],
        )
    )


def deny_audit_mutation(grantee: iam.IRole, table: ddb.ITableV2) -> None:
    grantee.add_to_principal_policy(
        iam.PolicyStatement(
            effect=iam.Effect.DENY,
            actions=AUDIT_FORBIDDEN_ACTIONS,
            resources=[table.table_arn, f"{table.table_arn}/*"],
        )
    )

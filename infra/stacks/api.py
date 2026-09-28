"""HTTP API, BFF Lambda, and the parse task (SPEC §4.1, §6.1, §19)."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from infra.config import EnvConfig
from infra.lambda_code import ARCH, RUNTIME, backend_code
from infra.stacks.auth import AuthStack
from infra.stacks.storage import StorageStack, grant_audit_append


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: EnvConfig,
        storage: StorageStack,
        auth: AuthStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]

        common_env = {
            "ENV": cfg.name,
            "INTEGRATIONS": cfg.integrations,
            "SEND_TO_PROD": "true" if cfg.send_to_prod else "false",
            "BEDROCK_MODEL_ID": cfg.bedrock_model_id,
            "JOBS_TABLE": storage.jobs.table_name,
            "ROWS_TABLE": storage.rows.table_name,
            "CONFIG_TABLE": storage.config_table.table_name,
            "AUDIT_TABLE": storage.audit_events.table_name,
            "UPLOADS_BUCKET": storage.uploads.bucket_name,
            "PROCESSED_BUCKET": storage.processed.bucket_name,
            "ROW_RETENTION_DAYS": str(cfg.row_retention_days),
            "RAW_FILE_RETENTION_DAYS": str(cfg.raw_file_retention_days),
            "POWERTOOLS_SERVICE_NAME": "list-uploader",
            "POWERTOOLS_METRICS_NAMESPACE": "ListUploader",
        }

        def function(name: str, handler: str, **overrides: object) -> lambda_.Function:
            props: dict[str, object] = {
                "runtime": RUNTIME,
                "architecture": ARCH,
                "handler": handler,
                "code": backend_code(),
                "timeout": Duration.seconds(29),
                "memory_size": 512,
                "tracing": lambda_.Tracing.ACTIVE,
                "environment": dict(common_env),
                "log_group": logs.LogGroup(
                    self,
                    f"{name}Logs",
                    retention=logs.RetentionDays.THREE_MONTHS,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
                **overrides,
            }
            return lambda_.Function(self, name, **props)  # type: ignore[arg-type]

        # --- Parse task: async, one invocation per uploaded file ----------------
        self.parse_task = function(
            "ParseTask",
            "tasks.parse_file.handler",
            timeout=Duration.minutes(5),
            memory_size=2048,  # openpyxl on a 10 MB workbook
            retry_attempts=0,  # failures are recorded as PARSE_FAILED, not retried
        )
        storage.jobs.grant_read_write_data(self.parse_task)
        storage.rows.grant_write_data(self.parse_task)
        storage.config_table.grant_read_data(self.parse_task)  # aliases, thresholds
        storage.uploads.grant_read(self.parse_task)
        if cfg.integrations != "fake":
            # Column-mapping suggestions. Dev uses the in-process fake and gets no
            # Bedrock access at all. TODO(P7): confirm the exact actions the Mantle
            # endpoint checks, and scope to the configured model's ARN.
            self.parse_task.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}::foundation-model/*",
                        f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                    ],
                )
            )
        grant_audit_append(self.parse_task.role, storage.audit_events)  # type: ignore[arg-type]
        storage.key.grant_encrypt_decrypt(self.parse_task)

        # --- BFF ----------------------------------------------------------------
        self.bff = function(
            "Bff",
            "bff.handler.handler",
            environment={**common_env, "PARSE_FUNCTION": self.parse_task.function_name},
        )
        for table in (storage.jobs, storage.rows, storage.config_table):
            table.grant_read_write_data(self.bff)
        # Presigned POST is signed with the BFF's role, so it needs PutObject.
        storage.uploads.grant_put(self.bff)
        storage.uploads.grant_read(self.bff)
        self.bff.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObjectRetention"],
                resources=[storage.uploads.arn_for_objects("*")],
            )
        )
        storage.processed.grant_read_write(self.bff)
        grant_audit_append(self.bff.role, storage.audit_events)  # type: ignore[arg-type]
        storage.key.grant_encrypt_decrypt(self.bff)
        self.parse_task.grant_invoke(self.bff)

        authorizer = authorizers.HttpUserPoolAuthorizer(
            "Jwt", auth.user_pool, user_pool_clients=[auth.client]
        )
        self.http_api = apigw.HttpApi(
            self,
            "HttpApi",
            api_name=f"list-uploader-{cfg.name}",
            default_authorizer=authorizer,
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],  # narrowed to the CloudFront origin in P7
                allow_methods=[apigw.CorsHttpMethod.ANY],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.hours(1),
            ),
        )
        self.http_api.add_routes(
            path="/{proxy+}",
            methods=[
                apigw.HttpMethod.GET,
                apigw.HttpMethod.POST,
                apigw.HttpMethod.PUT,
                apigw.HttpMethod.PATCH,
            ],
            integration=integrations.HttpLambdaIntegration("BffIntegration", self.bff),
        )
        CfnOutput(self, "ApiUrl", value=self.http_api.api_endpoint)

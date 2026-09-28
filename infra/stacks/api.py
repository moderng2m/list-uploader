"""HTTP API + BFF Lambda (SPEC §4.1, §19)."""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
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

        self.bff = lambda_.Function(
            self,
            "Bff",
            runtime=RUNTIME,
            architecture=ARCH,
            handler="bff.handler.handler",
            code=backend_code(),
            timeout=Duration.seconds(29),
            memory_size=512,
            tracing=lambda_.Tracing.ACTIVE,
            environment={
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
                "POWERTOOLS_SERVICE_NAME": "list-uploader",
                "POWERTOOLS_METRICS_NAMESPACE": "ListUploader",
            },
            log_group=logs.LogGroup(
                self,
                "BffLogs",
                retention=logs.RetentionDays.THREE_MONTHS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        for table in (storage.jobs, storage.rows, storage.config_table):
            table.grant_read_write_data(self.bff)
        storage.uploads.grant_put(self.bff)
        storage.uploads.grant_read(self.bff)
        storage.processed.grant_read_write(self.bff)
        grant_audit_append(self.bff.role, storage.audit_events)  # type: ignore[arg-type]
        storage.key.grant_encrypt_decrypt(self.bff)

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

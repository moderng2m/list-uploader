"""P7 synth assertions: sign-in, origins, headers, alarms, scheduled checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk.assertions import Match, Template

from infra.app import build
from infra.config import get_config


def _build(env: str = "dev", **context: str) -> dict[str, Stack]:
    app = App(context={"aws:cdk:bundling-stacks": []})
    built = build(app, get_config(env, context), deploy_web_assets=False)
    return {k: v for k, v in built.items() if isinstance(v, Stack)}


@pytest.fixture(scope="module")
def dev() -> dict[str, Stack]:
    return _build()


def _t(stacks: dict[str, Stack], name: str) -> Template:
    return Template.from_stack(stacks[name])


def _only(template: Template, kind: str) -> dict[str, Any]:
    [resource] = template.find_resources(kind).values()
    return resource["Properties"]  # type: ignore[no-any-return]


class TestOrigins:
    def test_uploads_cors_is_the_site_only(self, dev: dict[str, Stack]) -> None:
        buckets = _t(dev, "storage").find_resources("AWS::S3::Bucket")
        rules = [
            rule
            for b in buckets.values()
            for rule in b["Properties"].get("CorsConfiguration", {}).get("CorsRules", [])
        ]
        assert len(rules) == 1
        assert rules[0]["AllowedMethods"] == ["POST"]
        origins = json.dumps(rules[0]["AllowedOrigins"])
        assert rules[0]["AllowedOrigins"] != ["*"]
        assert "Distribution" in origins and "DomainName" in origins

    def test_api_cors_is_the_site_only(self, dev: dict[str, Stack]) -> None:
        cors = _only(_t(dev, "api"), "AWS::ApiGatewayV2::Api")["CorsConfiguration"]
        assert "*" not in json.dumps(cors["AllowOrigins"])
        assert sorted(cors["AllowMethods"]) == ["GET", "PATCH", "POST", "PUT"]

    def test_custom_origin_from_context(self) -> None:
        stacks = _build(web_origin="https://uploads.example.org/")
        cors = _only(_t(stacks, "api"), "AWS::ApiGatewayV2::Api")["CorsConfiguration"]
        assert cors["AllowOrigins"] == ["https://uploads.example.org"]


class TestSignIn:
    def test_code_flow_with_pkce_and_no_secret(self, dev: dict[str, Stack]) -> None:
        client = _only(_t(dev, "auth"), "AWS::Cognito::UserPoolClient")
        assert client["AllowedOAuthFlows"] == ["code"]
        assert client["AllowedOAuthFlowsUserPoolClient"] is True
        assert client.get("GenerateSecret") is False
        assert client["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
        assert client["SupportedIdentityProviders"] == ["COGNITO"]
        assert "/auth/callback" in json.dumps(client["CallbackURLs"])
        assert client["EnableTokenRevocation"] is True

    def test_hosted_sign_in_domain_and_no_self_sign_up(self, dev: dict[str, Stack]) -> None:
        auth = _t(dev, "auth")
        auth.resource_count_is("AWS::Cognito::UserPoolDomain", 1)
        pool = _only(auth, "AWS::Cognito::UserPool")
        assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True

    def test_saml_federation_from_context(self) -> None:
        stacks = _build(saml_metadata_url="https://idp.example.org/metadata", saml_idp_name="Okta")
        auth = _t(stacks, "auth")
        auth.has_resource_properties(
            "AWS::Cognito::UserPoolIdentityProvider",
            {"ProviderName": "Okta", "ProviderType": "SAML"},
        )
        client = _only(auth, "AWS::Cognito::UserPoolClient")
        assert client["SupportedIdentityProviders"] == ["COGNITO", "Okta"]

    def test_runtime_config_for_the_spa(self, dev: dict[str, Stack]) -> None:
        config = dev["site"].config  # type: ignore[attr-defined]
        assert set(config) == {
            "env",
            "region",
            "apiUrl",
            "userPoolId",
            "clientId",
            "cognitoDomain",
            "redirectUri",
            "logoutUri",
        }


class TestHeaders:
    def test_csp_and_hsts(self, dev: dict[str, Stack]) -> None:
        policy = _only(_t(dev, "web"), "AWS::CloudFront::ResponseHeadersPolicy")
        security = policy["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
        csp = security["ContentSecurityPolicy"]["ContentSecurityPolicy"]
        for directive in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'"):
            assert directive in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert security["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31536000
        assert security["FrameOptions"]["FrameOption"] == "DENY"

    def test_https_only(self, dev: dict[str, Stack]) -> None:
        dist = _only(_t(dev, "web"), "AWS::CloudFront::Distribution")["DistributionConfig"]
        assert dist["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"


SPEC_ALARMS = {
    "audit-write-failure",
    "send-workflow-failed",
    "rows-stuck-sending",
    "workato-error-rate",
    "gate-rejected-after-ui",
    "enrichment-batch-errors",
    "bedrock-parse-failures",
    "lambda-errors",
    "lambda-throttles",
    "job-stuck-running",
}


def _alarm_names(stacks: dict[str, Stack]) -> set[str]:
    alarms = _t(stacks, "monitoring").find_resources("AWS::CloudWatch::Alarm")
    return {
        a["Properties"]["AlarmName"].removeprefix("list-uploader-dev-") for a in alarms.values()
    }


class TestAlarms:
    def test_every_spec_alarm_notifies_the_topic(self, dev: dict[str, Stack]) -> None:
        mon = _t(dev, "monitoring")
        assert _alarm_names(dev) == SPEC_ALARMS | {"canary-failed"}
        topic_id = next(iter(mon.find_resources("AWS::SNS::Topic")))
        for alarm in mon.find_resources("AWS::CloudWatch::Alarm").values():
            assert alarm["Properties"]["AlarmActions"] == [{"Ref": topic_id}]
            assert alarm["Properties"]["AlarmDescription"].startswith("[")

    def test_audit_failure_alarms_within_a_minute(self, dev: dict[str, Stack]) -> None:
        _t(dev, "monitoring").has_resource_properties(
            "AWS::CloudWatch::Alarm",
            {
                "AlarmName": "list-uploader-dev-audit-write-failure",
                "MetricName": "AuditWriteFailures",
                "Namespace": "ListUploader",
                "Period": 60,
                "Threshold": 1,
                "Dimensions": Match.array_with([{"Name": "env", "Value": "dev"}]),
            },
        )

    def test_topic_is_encrypted_and_emails_come_from_context(self) -> None:
        stacks = _build(alert_emails="ops@example.org, oncall@example.org")
        mon = _t(stacks, "monitoring")
        assert "KmsMasterKeyId" in _only(mon, "AWS::SNS::Topic")
        subs = mon.find_resources("AWS::SNS::Subscription")
        assert sorted(s["Properties"]["Endpoint"] for s in subs.values()) == [
            "oncall@example.org",
            "ops@example.org",
        ]

    def test_no_subscribers_unless_configured(self, dev: dict[str, Stack]) -> None:
        _t(dev, "monitoring").resource_count_is("AWS::SNS::Subscription", 0)

    def test_prod_has_no_canary(self) -> None:
        stacks = _build("prod")
        alarms = _t(stacks, "monitoring").find_resources("AWS::CloudWatch::Alarm")
        names = {a["Properties"]["AlarmName"] for a in alarms.values()}
        assert names == {f"list-uploader-prod-{n}" for n in SPEC_ALARMS}
        mon = _t(stacks, "monitoring")
        handlers = [
            f["Properties"]["Handler"]
            for f in mon.find_resources("AWS::Lambda::Function").values()
            if "Handler" in f["Properties"]
        ]
        assert "tasks.canary.handler" not in handlers


class TestScheduledChecks:
    def test_monitor_every_five_minutes_and_daily_canary(self, dev: dict[str, Stack]) -> None:
        mon = _t(dev, "monitoring")
        schedules = sorted(
            r["Properties"]["ScheduleExpression"]
            for r in mon.find_resources("AWS::Events::Rule").values()
        )
        assert schedules == ["cron(7 13 * * ? *)", "rate(5 minutes)"]

    def test_dashboard_exists(self, dev: dict[str, Stack]) -> None:
        _t(dev, "monitoring").has_resource_properties(
            "AWS::CloudWatch::Dashboard", {"DashboardName": "list-uploader-dev-operations"}
        )


def _statements(template: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for lid, res in template.get("Resources", {}).items():
        if res["Type"] == "AWS::IAM::Policy":
            roles = json.dumps(res["Properties"].get("Roles", []))
            out += [(roles, s) for s in res["Properties"]["PolicyDocument"]["Statement"]]
        if res["Type"] == "AWS::IAM::Role":
            for pol in res["Properties"].get("Policies", []):
                out += [(lid, s) for s in pol["PolicyDocument"]["Statement"]]
    return out


class TestAuditArchiveAccess:
    def test_only_the_mops_reader_can_query(self, dev: dict[str, Stack]) -> None:
        for name, stack in dev.items():
            for roles, stmt in _statements(Template.from_stack(stack).to_json()):
                actions = json.dumps(stmt.get("Action"))
                if "athena:" in actions or "glue:Get" in actions:
                    assert name == "audit_query" and "MopsAuditReader" in roles, (name, roles)

    def test_reader_is_read_only_and_scoped(self, dev: dict[str, Stack]) -> None:
        tpl = _t(dev, "audit_query")
        tpl.has_resource_properties("AWS::IAM::Role", {"RoleName": "mops-audit-reader-dev"})
        stmts = {s.get("Sid"): s for _, s in _statements(tpl.to_json()) if s.get("Sid")}
        assert "workgroup/list-uploader-audit-dev" in json.dumps(stmts["AuditWorkgroupOnly"])
        assert stmts["ReadArchive"]["Action"] == "s3:GetObject"
        assert "/audit/*" in json.dumps(stmts["ReadArchive"]["Resource"])
        every = json.dumps([s for _, s in _statements(tpl.to_json())])
        for write in ("s3:DeleteObject", "s3:PutObjectRetention", "s3:BypassGovernanceRetention"):
            assert write not in every

    def test_workgroup_enforces_encrypted_results(self, dev: dict[str, Stack]) -> None:
        tpl = _t(dev, "audit_query")
        wg = _only(tpl, "AWS::Athena::WorkGroup")
        conf = wg["WorkGroupConfiguration"]
        assert wg["Name"] == "list-uploader-audit-dev"
        assert conf["EnforceWorkGroupConfiguration"] is True
        assert (
            conf["ResultConfiguration"]["EncryptionConfiguration"]["EncryptionOption"] == "SSE_KMS"
        )
        tpl.has_resource_properties(
            "AWS::S3::Bucket",
            {
                "LifecycleConfiguration": {
                    "Rules": Match.array_with([Match.object_like({"ExpirationInDays": 30})])
                }
            },
        )

    def test_table_projects_daily_partitions(self, dev: dict[str, Stack]) -> None:
        table = _only(_t(dev, "audit_query"), "AWS::Glue::Table")["TableInput"]
        params = table["Parameters"]
        assert params["projection.dt.format"] == "yyyy-MM-dd"
        assert "/audit/dt=${dt}/" in json.dumps(params["storage.location.template"])
        cols = {c["Name"] for c in table["StorageDescriptor"]["Columns"]}
        assert {"event_type", "job_id", "email_sha256", "details"} <= cols

    def test_archive_reads_are_logged(self, dev: dict[str, Stack]) -> None:
        trail = _only(_t(dev, "audit_query"), "AWS::CloudTrail::Trail")
        [selector] = trail["EventSelectors"]
        assert selector["IncludeManagementEvents"] is False
        assert selector["ReadWriteType"] == "All"
        assert "/audit/" in json.dumps(selector["DataResources"])
        assert trail["EnableLogFileValidation"] is True

    def test_business_queries(self, dev: dict[str, Stack]) -> None:
        names = {
            q["Properties"]["Name"]
            for q in _t(dev, "audit_query").find_resources("AWS::Athena::NamedQuery").values()
        }
        assert {"job-history", "person-lookup", "top-issue-codes", "ai-accept-rate"} <= names


# Actions AWS only allows on "*": X-Ray tracing, Step Functions log delivery setup,
# and CloudFront invalidations from the CDK site deployment.
WILDCARD_OK = {
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
    "logs:CreateLogDelivery",
    "logs:DeleteLogDelivery",
    "logs:DescribeLogGroups",
    "logs:DescribeResourcePolicies",
    "logs:GetLogDelivery",
    "logs:ListLogDeliveries",
    "logs:PutResourcePolicy",
    "logs:UpdateLogDelivery",
    "cloudfront:CreateInvalidation",
    "cloudfront:GetInvalidation",
}


class TestLeastPrivilege:
    @pytest.mark.parametrize("env", ["dev", "prod"])
    def test_no_wildcard_resources_beyond_aws_required(self, env: str) -> None:
        stacks = _build(env)
        for name, stack in stacks.items():
            for _, stmt in _statements(Template.from_stack(stack).to_json()):
                if stmt["Effect"] != "Allow":
                    continue
                resources = (
                    stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
                )
                actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
                assert "*" not in actions and not any(a.endswith(":*") for a in actions), (
                    name,
                    actions,
                )
                if "*" in resources:
                    assert set(actions) <= WILDCARD_OK, (name, actions)


class TestRetention:
    def test_processed_files_expire_after_a_day(self, dev: dict[str, Stack]) -> None:
        buckets = _t(dev, "storage").find_resources("AWS::S3::Bucket")
        processed = next(b for lid, b in buckets.items() if lid.startswith("Processed"))
        [rule] = processed["Properties"]["LifecycleConfiguration"]["Rules"]
        assert rule["ExpirationInDays"] == 1

    def test_audit_query_copy_has_ttl(self, dev: dict[str, Stack]) -> None:
        tables = _t(dev, "storage").find_resources("AWS::DynamoDB::GlobalTable")
        audit = next(t for lid, t in tables.items() if lid.startswith("AuditEvents"))
        assert audit["Properties"]["TimeToLiveSpecification"] == {
            "AttributeName": "expires_at",
            "Enabled": True,
        }
        _t(dev, "api").has_resource_properties(
            "AWS::Lambda::Function",
            {"Environment": {"Variables": Match.object_like({"AUDIT_RETENTION_DAYS": "730"})}},
        )


class TestApi:
    def test_throttled_and_access_logged_without_pii(self, dev: dict[str, Stack]) -> None:
        stage = _only(_t(dev, "api"), "AWS::ApiGatewayV2::Stage")
        assert stage["DefaultRouteSettings"] == {
            "ThrottlingBurstLimit": 100,
            "ThrottlingRateLimit": 50,
        }
        fmt = stage["AccessLogSettings"]["Format"]
        assert "$context.requestId" in fmt
        for pii in ("sourceIp", "identity", "claims", "email"):
            assert pii not in fmt


class TestSecrets:
    def test_dev_has_no_workato_secret(self, dev: dict[str, Stack]) -> None:
        for stack in dev.values():
            Template.from_stack(stack).resource_count_is("AWS::SecretsManager::Secret", 0)

    def test_prod_token_is_readable_by_the_callers_only(self) -> None:
        stacks = _build("prod")
        wf = _t(stacks, "workflows")
        wf.has_resource_properties(
            "AWS::SecretsManager::Secret", {"Name": "list-uploader/prod/workato-api-token"}
        )
        readers = set()
        for name, stack in stacks.items():
            for roles, stmt in _statements(Template.from_stack(stack).to_json()):
                if "secretsmanager:GetSecretValue" in json.dumps(stmt["Action"]):
                    readers.add((name, roles.split("ServiceRole")[0].split('"')[-1]))
        assert {r for _, r in readers} == {"AnalyzeTask", "EnrichTask", "SendTask", "Bff"}

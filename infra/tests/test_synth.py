from __future__ import annotations

import json
from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk.assertions import Match, Template

from infra.app import build
from infra.config import get_config
from infra.stacks.storage import AUDIT_FORBIDDEN_ACTIONS

# Actions that could modify or remove an audit event.
MUTATING = {a.lower() for a in AUDIT_FORBIDDEN_ACTIONS} | {"dynamodb:*", "*"}


@pytest.fixture(scope="module")
def stacks() -> dict[str, Stack]:
    # Skip asset bundling (pip installs) in unit tests.
    app = App(context={"aws:cdk:bundling-stacks": []})
    built = build(app, get_config("dev"), deploy_web_assets=False)
    return {k: v for k, v in built.items() if isinstance(v, Stack)}


def _templates(stacks: dict[str, Stack]) -> dict[str, dict[str, Any]]:
    return {k: Template.from_stack(s).to_json() for k, s in stacks.items()}


def _statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for res in template.get("Resources", {}).values():
        if res["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            out.extend(res["Properties"]["PolicyDocument"]["Statement"])
        if res["Type"] == "AWS::IAM::Role":
            for pol in res["Properties"].get("Policies", []):
                out.extend(pol["PolicyDocument"]["Statement"])
    return out


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _audit_logical_id(stacks: dict[str, Stack]) -> str:
    template = Template.from_stack(stacks["storage"]).to_json()
    ids = [
        lid
        for lid, res in template["Resources"].items()
        if res["Type"] == "AWS::DynamoDB::GlobalTable" and lid.startswith("AuditEvents")
    ]
    assert len(ids) == 1
    return ids[0]


def test_no_role_can_update_or_delete_audit_events(stacks: dict[str, Stack]) -> None:
    audit_id = _audit_logical_id(stacks)
    checked = 0
    for name, template in _templates(stacks).items():
        for stmt in _statements(template):
            if stmt.get("Effect") != "Allow":
                continue
            resources = json.dumps(stmt.get("Resource"))
            if audit_id not in resources and resources != '"*"':
                continue
            actions = {a.lower() for a in _as_list(stmt["Action"])}
            bad = actions & MUTATING
            assert not bad, f"{name}: {bad} allowed on AuditEvents"
            checked += 1
    assert checked > 0, "expected at least one grant on AuditEvents"


def test_bff_has_put_only_and_explicit_deny(stacks: dict[str, Stack]) -> None:
    audit_id = _audit_logical_id(stacks)
    stmts = _statements(_templates(stacks)["api"])
    on_audit = [s for s in stmts if audit_id in json.dumps(s.get("Resource"))]
    allowed = {a for s in on_audit if s["Effect"] == "Allow" for a in _as_list(s["Action"])}
    denied = {a for s in on_audit if s["Effect"] == "Deny" for a in _as_list(s["Action"])}
    assert allowed == {"dynamodb:PutItem"}
    assert set(AUDIT_FORBIDDEN_ACTIONS) <= denied


def test_audit_table_is_protected(stacks: dict[str, Stack]) -> None:
    Template.from_stack(stacks["storage"]).has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {
            "KeySchema": [
                {"AttributeName": "job_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            "StreamSpecification": {"StreamViewType": "NEW_IMAGE"},
            "Replicas": [
                Match.object_like(
                    {
                        "DeletionProtectionEnabled": True,
                        "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                    }
                )
            ],
        },
    )


def test_audit_archive_uses_governance_object_lock(stacks: dict[str, Stack]) -> None:
    Template.from_stack(stacks["storage"]).has_resource_properties(
        "AWS::S3::Bucket",
        {
            "ObjectLockEnabled": True,
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 730}},
            },
        },
    )


def test_data_buckets_are_private_encrypted_tls_only(stacks: dict[str, Stack]) -> None:
    template = Template.from_stack(stacks["storage"])
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 3
    for bucket in buckets.values():
        props = bucket["Properties"]
        assert props["PublicAccessBlockConfiguration"]["BlockPublicPolicy"] is True
        sse = props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
        assert sse["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
    policies = template.find_resources("AWS::S3::BucketPolicy")
    assert len(policies) == 3
    for policy in policies.values():
        conditions = json.dumps(policy["Properties"]["PolicyDocument"])
        assert "aws:SecureTransport" in conditions


def test_api_requires_jwt(stacks: dict[str, Stack]) -> None:
    template = Template.from_stack(stacks["api"])
    template.resource_count_is("AWS::ApiGatewayV2::Authorizer", 1)
    for route in template.find_resources("AWS::ApiGatewayV2::Route").values():
        assert route["Properties"]["AuthorizationType"] == "JWT"


def test_dev_never_sends_to_prod(stacks: dict[str, Stack]) -> None:
    Template.from_stack(stacks["api"]).has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like({"SEND_TO_PROD": "false", "INTEGRATIONS": "fake"})
            }
        },
    )


def test_three_state_machines(stacks: dict[str, Stack]) -> None:
    Template.from_stack(stacks["workflows"]).resource_count_is(
        "AWS::StepFunctions::StateMachine", 3
    )


def test_uploads_bucket_is_object_locked_without_default_retention(
    stacks: dict[str, Stack],
) -> None:
    buckets = Template.from_stack(stacks["storage"]).find_resources("AWS::S3::Bucket")
    uploads = [b for lid, b in buckets.items() if lid.startswith("Uploads")]
    assert len(uploads) == 1
    props = uploads[0]["Properties"]
    assert props["ObjectLockEnabled"] is True
    assert "Rule" not in props.get("ObjectLockConfiguration", {})
    assert props["CorsConfiguration"]["CorsRules"][0]["AllowedMethods"] == ["POST"]


def test_parse_task_wiring(stacks: dict[str, Stack]) -> None:
    template = Template.from_stack(stacks["api"])
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "tasks.parse_file.handler", "Timeout": 300, "MemorySize": 2048},
    )
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 0})
    bff = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Handler": "bff.handler.handler"}}
    )
    (bff_props,) = [r["Properties"] for r in bff.values()]
    assert "PARSE_FUNCTION" in bff_props["Environment"]["Variables"]
    actions = {
        a
        for stmt in _statements(template.to_json())
        if stmt["Effect"] == "Allow"
        for a in _as_list(stmt["Action"])
    }
    assert {"lambda:InvokeFunction", "s3:PutObjectRetention"} <= actions


def test_dev_has_no_bedrock_access(stacks: dict[str, Stack]) -> None:
    for name, template in _templates(stacks).items():
        for stmt in _statements(template):
            actions = " ".join(_as_list(stmt["Action"]))
            assert "bedrock:" not in actions, f"{name} grants Bedrock in dev"


def test_parse_task_reads_config() -> None:
    app = App(context={"aws:cdk:bundling-stacks": []})
    built = build(app, get_config("dev"), deploy_web_assets=False)
    api_json = Template.from_stack(built["api"]).to_json()  # type: ignore[arg-type]
    storage_json = Template.from_stack(built["storage"]).to_json()  # type: ignore[arg-type]
    config_id = next(lid for lid in storage_json["Resources"] if lid.startswith("Config"))
    grants = [
        s for s in _statements(api_json)
        if config_id in json.dumps(s.get("Resource")) and s["Effect"] == "Allow"
    ]  # fmt: skip
    assert any("dynamodb:GetItem" in _as_list(s["Action"]) for s in grants)


def test_prod_parse_task_can_call_bedrock() -> None:
    app = App(context={"aws:cdk:bundling-stacks": []})
    built = build(app, get_config("prod"), deploy_web_assets=False)
    api_json = Template.from_stack(built["api"]).to_json()  # type: ignore[arg-type]
    actions = {a for s in _statements(api_json) for a in _as_list(s["Action"])}
    assert "bedrock:InvokeModel" in actions


def test_analyze_workflow_shape(stacks: dict[str, Stack]) -> None:
    template = Template.from_stack(stacks["workflows"])
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    analyze = next(m for lid, m in machines.items() if lid.startswith("AnalyzeWorkflow"))
    definition = json.dumps(analyze["Properties"]["DefinitionString"])
    for state in ("Prepare", "JunkChecks", "Finalize", "MarkFailed", "AnalysisFailed"):
        assert state in definition, state
    assert '\\"MaxConcurrency\\":4' in definition
    template.has_resource_properties(
        "AWS::Lambda::Function", {"Handler": "tasks.analyze.handler", "Timeout": 600}
    )


def test_bff_can_start_analysis_and_use_transactions(stacks: dict[str, Stack]) -> None:
    api_json = Template.from_stack(stacks["api"]).to_json()
    actions = {a for s in _statements(api_json) if s["Effect"] == "Allow"
               for a in _as_list(s["Action"])}  # fmt: skip
    assert "states:StartExecution" in actions
    assert "dynamodb:ConditionCheckItem" in actions


def test_enrich_workflow_shape(stacks: dict[str, Stack]) -> None:
    template = Template.from_stack(stacks["workflows"])
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    enrich = next(m for lid, m in machines.items() if lid.startswith("EnrichWorkflow"))
    definition = json.dumps(enrich["Properties"]["DefinitionString"])
    for state in ("EnrichPrepare", "EnrichBatches", "EnrichFinalize", "EnrichMarkFailed"):
        assert state in definition, state
    assert '\\"MaxConcurrency\\":2' in definition
    template.has_resource_properties("AWS::Lambda::Function", {"Handler": "tasks.enrich.handler"})
    api = Template.from_stack(stacks["api"])
    api.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like({"ENRICH_STATE_MACHINE": Match.any_value()})
            }
        },
    )

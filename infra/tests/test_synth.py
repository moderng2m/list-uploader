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

"""Check the audit archive end to end after a deploy (P7 acceptance).

    uv run python -m ops.verify_archive --env dev --job-id j_...
    uv run python -m ops.verify_archive --env dev --job-id j_... \
        --denied-principal arn:aws:iam::<account>:role/<some-non-mops-role>

1. Firehose delivered today's events under audit/dt=YYYY-MM-DD/ and nothing
   landed under audit-errors/ (which is where Object Lock or KMS problems show).
2. As mops-audit-reader, Athena (workgroup list-uploader-audit-<env>) returns the
   job's full event history, matching the DynamoDB copy.
3. Optionally, IAM's policy simulator confirms another principal can't query.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key


def _find(session: Any, env: str) -> dict[str, str]:
    cfn = session.client("cloudformation")
    out: dict[str, str] = {}
    for stack in (f"ListUploader-{env}-Storage", f"ListUploader-{env}-AuditQuery"):
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
            for r in page["StackResourceSummaries"]:
                out[r["LogicalResourceId"]] = r["PhysicalResourceId"]
    return out


def _physical(resources: dict[str, str], prefix: str) -> str:
    return next(v for k, v in resources.items() if k.startswith(prefix))


def check_delivery(session: Any, bucket: str) -> None:
    s3 = session.client("s3")
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    delivered = s3.list_objects_v2(Bucket=bucket, Prefix=f"audit/dt={today}/").get("KeyCount", 0)
    errors = s3.list_objects_v2(Bucket=bucket, Prefix="audit-errors/").get("KeyCount", 0)
    print(f"archive: {delivered} object(s) for {today}; {errors} under audit-errors/")
    if not delivered or errors:
        raise SystemExit("FAIL: archive delivery (see audit-errors/ and the Firehose metrics)")


def query_as_reader(session: Any, env: str, job_id: str, expected: int) -> None:
    account = session.client("sts").get_caller_identity()["Account"]
    creds = session.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account}:role/mops-audit-reader-{env}",
        RoleSessionName="verify-archive",
    )["Credentials"]
    athena = boto3.client(
        "athena",
        region_name=session.region_name,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    qid = athena.start_query_execution(
        QueryString=f"SELECT count(DISTINCT event_id) FROM events WHERE job_id = '{job_id}'",
        QueryExecutionContext={"Database": f"list_uploader_{env}_audit"},
        WorkGroup=f"list-uploader-audit-{env}",
    )["QueryExecutionId"]
    while True:
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if state["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if state["State"] != "SUCCEEDED":
        raise SystemExit(f"FAIL: Athena query {state['State']}: {state.get('StateChangeReason')}")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    found = int(rows[1]["Data"][0]["VarCharValue"])
    print(f"athena (as mops-audit-reader): {found} events for {job_id}; DynamoDB has {expected}")
    if found != expected:
        raise SystemExit(
            "FAIL: the archive doesn't hold the job's full history yet (Firehose "
            "buffers for about a minute; try again shortly)"
        )


def check_denied(session: Any, env: str, principal: str) -> None:
    account = session.client("sts").get_caller_identity()["Account"]
    workgroup = (
        f"arn:aws:athena:{session.region_name}:{account}:workgroup/list-uploader-audit-{env}"
    )
    result = session.client("iam").simulate_principal_policy(
        PolicySourceArn=principal,
        ActionNames=["athena:StartQueryExecution", "sts:AssumeRole"],
        ResourceArns=[workgroup],
    )
    decisions = {r["EvalActionName"]: r["EvalDecision"] for r in result["EvaluationResults"]}
    print(f"simulated for {principal}: {decisions}")
    if decisions.get("athena:StartQueryExecution") == "allowed":
        raise SystemExit("FAIL: that principal can query the audit workgroup")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", default="dev")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--denied-principal")
    args = parser.parse_args()
    session = boto3.Session()
    resources = _find(session, args.env)
    check_delivery(session, _physical(resources, "AuditArchive"))
    table = session.resource("dynamodb").Table(_physical(resources, "AuditEvents"))
    expected = 0
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("job_id").eq(args.job_id),
        "Select": "COUNT",
    }
    while True:
        page = table.query(**kwargs)
        expected += int(page["Count"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    query_as_reader(session, args.env, args.job_id, expected)
    if args.denied_principal:
        check_denied(session, args.env, args.denied_principal)
    print("OK")


if __name__ == "__main__":
    main()

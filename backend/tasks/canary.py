"""Daily end-to-end synthetic check (SPEC §21.3.5), dev only.

Runs a tiny synthetic file through the real BFF routes and task code, in process:
create job -> upload -> parse -> confirm mapping -> analyze -> gate -> send (with
send_to_prod=false) -> result. It uses the deployed tables, buckets and Workato
client, so a broken endpoint, credential or permission shows up here first. The
Step Functions wiring is covered by the workflow-failure alarms instead.

Publishes CanarySucceeded (1 or 0) and raises on failure, so both the canary
alarm and the Lambda error alarm fire. The job is owned by a clearly synthetic
user, so it's easy to tell apart in history and the audit trail.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from bff import handler as bff
from bff.app import BffDeps, set_deps
from shared.observability import logger, metrics, tracer
from tasks import analyze, parse_file, send

CANARY_USER = "canary@list-uploader.invalid"
CAMPAIGN = "701000000000001AAA"  # a synthetic campaign known to the fake Salesforce
CANARY_FILE = (
    "Company,First name,Last Name,Email Address,SFDC Last Campaign ID\n"
    f"Canary Demo Co,Ada,Example,ada.canary@acme.example,{CAMPAIGN}\n"
    f"Canary Demo Co,Grace,Sample,grace.canary@acme.example,{CAMPAIGN}\n"
).encode()


class CanaryFailed(RuntimeError):
    pass


class _Ctx:
    function_name = "canary"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:canary"
    aws_request_id = "canary"


def _event(method: str, path: str, body: Any = None) -> dict[str, Any]:
    claims = {"email": CANARY_USER, "sub": "canary"}
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "body": None if body is None else json.dumps(body),
        "requestContext": {
            "requestId": "canary",
            "stage": "$default",
            "http": {"method": method, "path": path, "sourceIp": "127.0.0.1"},
            "authorizer": {"jwt": {"claims": claims, "scopes": None}},
        },
        "isBase64Encoded": False,
    }


Call = Callable[..., Any]


def _api(method: str, path: str, body: Any = None, *, expect: int = 200) -> Any:
    resp = bff.handler(_event(method, path, body), _Ctx())
    status = resp["statusCode"]
    if status != expect:
        raise CanaryFailed(f"{method} {path} returned {status}, expected {expect}")
    return json.loads(resp["body"]) if resp.get("body") else None


def run(
    deps: BffDeps,
    *,
    parse: Call,
    analyze_all: Call,
    send_all: Call,
) -> dict[str, Any]:
    set_deps(deps)
    created = _api("POST", "/jobs", {"filename": "canary.csv", "enrich": False}, expect=201)
    job_id = created["job"]["job_id"]
    logger.append_keys(job_id=job_id)
    deps.s3.put_object(
        Bucket=deps.uploads_bucket, Key=created["upload"]["fields"]["key"], Body=CANARY_FILE
    )
    _api("POST", f"/jobs/{job_id}/uploaded", expect=202)
    parse(job_id)

    mapping = _api("GET", f"/jobs/{job_id}/mapping")
    columns = [
        {"source_header": c["source_header"], "field_key": c["field_key"]}
        for c in mapping["columns"]
    ]
    _api("PUT", f"/jobs/{job_id}/mapping", {"columns": columns})
    _api("POST", f"/jobs/{job_id}/analyze", expect=202)
    if analyze_all(job_id) != "ANALYSIS_REVIEW":
        raise CanaryFailed("analysis didn't finish")

    gate = _api("GET", f"/jobs/{job_id}/gate")
    if not gate["passed"]:
        raise CanaryFailed(f"gate failed: {gate['reason_codes']}")
    confirmation = {
        "rows_to_send": gate["rows_to_send"],
        "by_campaign": [
            {k: c[k] for k in ("campaign_id", "status", "rows")} for c in gate["by_campaign"]
        ],
    }
    _api(
        "POST",
        f"/jobs/{job_id}/send",
        {"confirmation": confirmation, "ui_gate_passed": True},
        expect=202,
    )
    if send_all(job_id) != "COMPLETED":
        raise CanaryFailed("send didn't complete")
    result = _api("GET", f"/jobs/{job_id}/result")
    if result["submitted"] != 2:
        raise CanaryFailed(f"expected 2 rows submitted, got {result['submitted']}")
    return {"job_id": job_id, "submitted": result["submitted"]}


def _noop(*_: Any) -> None:
    """Async starts are replaced by running the step in-process."""


@logger.inject_lambda_context
@tracer.capture_lambda_handler
@metrics.log_metrics
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    deps = BffDeps.with_starters(
        start_parse=_noop, start_analysis=_noop, start_enrichment=_noop, start_send=_noop
    )
    parse_deps = parse_file.ParseDeps.from_env()
    analyze_deps = analyze.AnalyzeDeps.from_env()
    send_deps = send.SendDeps.from_env()
    try:
        outcome = run(
            deps,
            parse=lambda job_id: parse_file.run(job_id, parse_deps),
            analyze_all=lambda job_id: analyze.run_all(job_id, analyze_deps),
            send_all=lambda job_id: send.run_all(job_id, send_deps),
        )
    except Exception:
        metrics.add_metric(name="CanarySucceeded", unit="Count", value=0)
        logger.exception("canary failed")
        raise
    metrics.add_metric(name="CanarySucceeded", unit="Count", value=1)
    logger.info("canary passed", extra=outcome)
    return outcome

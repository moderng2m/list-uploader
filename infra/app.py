"""CDK entry point: `cdk synth -c env=dev`."""

from __future__ import annotations

import os

from aws_cdk import App, Environment, Tags

from infra.config import EnvConfig, get_config
from infra.stacks.api import ApiStack
from infra.stacks.audit_query import AuditQueryStack
from infra.stacks.auth import AuthStack
from infra.stacks.monitoring import MonitoringStack
from infra.stacks.site import SiteStack
from infra.stacks.storage import StorageStack
from infra.stacks.web import WebStack
from infra.stacks.workflows import WorkflowsStack


def build(app: App, cfg: EnvConfig, *, deploy_web_assets: bool = True) -> dict[str, object]:
    env = Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=cfg.region)
    prefix = f"ListUploader-{cfg.name}"
    # Web first: the site's origin pins CORS and the sign-in callback everywhere else.
    web = WebStack(app, f"{prefix}-Web", cfg=cfg, env=env)
    storage = StorageStack(app, f"{prefix}-Storage", cfg=cfg, web_origin=web.origin, env=env)
    audit_query = AuditQueryStack(app, f"{prefix}-AuditQuery", cfg=cfg, storage=storage, env=env)
    auth = AuthStack(app, f"{prefix}-Auth", cfg=cfg, web_origin=web.origin, env=env)
    workflows = WorkflowsStack(app, f"{prefix}-Workflows", cfg=cfg, storage=storage, env=env)
    api = ApiStack(
        app,
        f"{prefix}-Api",
        cfg=cfg,
        storage=storage,
        auth=auth,
        workflows=workflows,
        web_origin=web.origin,
        env=env,
    )
    monitoring = MonitoringStack(
        app,
        f"{prefix}-Monitoring",
        cfg=cfg,
        storage=storage,
        workflows=workflows,
        api=api,
        env=env,
    )
    site = SiteStack(
        app,
        f"{prefix}-Site",
        cfg=cfg,
        web=web,
        auth=auth,
        api=api,
        deploy_assets=deploy_web_assets,
        env=env,
    )
    Tags.of(app).add("app", "list-uploader")
    Tags.of(app).add("env", cfg.name)
    return {
        "web": web,
        "storage": storage,
        "auth": auth,
        "workflows": workflows,
        "api": api,
        "site": site,
        "monitoring": monitoring,
        "audit_query": audit_query,
    }


def main() -> None:
    app = App()
    context = {
        k: app.node.try_get_context(k)
        for k in ("alert_emails", "saml_metadata_url", "saml_idp_name", "web_origin")
    }
    build(app, get_config(app.node.try_get_context("env") or "dev", context))
    app.synth()


if __name__ == "__main__":
    main()

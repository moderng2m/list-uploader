"""CDK entry point: `cdk synth -c env=dev`."""

from __future__ import annotations

import os

from aws_cdk import App, Environment, Tags

from infra.config import EnvConfig, get_config
from infra.stacks.api import ApiStack
from infra.stacks.auth import AuthStack
from infra.stacks.storage import StorageStack
from infra.stacks.web import WebStack
from infra.stacks.workflows import WorkflowsStack


def build(app: App, cfg: EnvConfig, *, deploy_web_assets: bool = True) -> dict[str, object]:
    env = Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=cfg.region)
    prefix = f"ListUploader-{cfg.name}"
    storage = StorageStack(app, f"{prefix}-Storage", cfg=cfg, env=env)
    auth = AuthStack(app, f"{prefix}-Auth", cfg=cfg, env=env)
    api = ApiStack(app, f"{prefix}-Api", cfg=cfg, storage=storage, auth=auth, env=env)
    workflows = WorkflowsStack(app, f"{prefix}-Workflows", cfg=cfg, env=env)
    web = WebStack(app, f"{prefix}-Web", cfg=cfg, deploy_assets=deploy_web_assets, env=env)
    Tags.of(app).add("app", "list-uploader")
    Tags.of(app).add("env", cfg.name)
    return {"storage": storage, "auth": auth, "api": api, "workflows": workflows, "web": web}


def main() -> None:
    app = App()
    build(app, get_config(app.node.try_get_context("env") or "dev"))
    app.synth()


if __name__ == "__main__":
    main()

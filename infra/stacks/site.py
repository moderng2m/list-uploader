"""Copies the built SPA and its runtime config into the Web stack's bucket (last).

`config.json` tells the live SPA where its API and sign-in are; the build itself
has no environment baked in. index.html and config.json are never cached, so a
deploy takes effect at once; the hashed assets are immutable.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from infra.config import EnvConfig
from infra.stacks.api import ApiStack
from infra.stacks.auth import AuthStack
from infra.stacks.web import WebStack

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
NO_CACHE = [s3deploy.CacheControl.from_string("no-cache, no-store, must-revalidate")]
IMMUTABLE = [s3deploy.CacheControl.from_string("public, max-age=31536000, immutable")]


def runtime_config(cfg: EnvConfig, web: WebStack, auth: AuthStack, api: ApiStack) -> dict[str, str]:
    return {
        "env": cfg.name,
        "region": cfg.region,
        "apiUrl": api.http_api.api_endpoint,
        "userPoolId": auth.user_pool.user_pool_id,
        "clientId": auth.client.user_pool_client_id,
        "cognitoDomain": auth.domain.base_url(),
        "redirectUri": f"{web.origin}/auth/callback",
        "logoutUri": f"{web.origin}/",
    }


class SiteStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        cfg: EnvConfig,
        web: WebStack,
        auth: AuthStack,
        api: ApiStack,
        deploy_assets: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, cid, **kwargs)  # type: ignore[arg-type]
        self.config = runtime_config(cfg, web, auth, api)
        if deploy_assets and FRONTEND_DIST.is_dir():
            s3deploy.BucketDeployment(
                self,
                "Assets",
                sources=[s3deploy.Source.asset(str(FRONTEND_DIST), exclude=["index.html"])],
                destination_bucket=web.site_bucket,
                cache_control=IMMUTABLE,
                prune=False,
            )
            s3deploy.BucketDeployment(
                self,
                "Entry",
                sources=[
                    s3deploy.Source.asset(str(FRONTEND_DIST), exclude=["*", "!index.html"]),
                    s3deploy.Source.json_data("config.json", self.config),
                ],
                destination_bucket=web.site_bucket,
                cache_control=NO_CACHE,
                prune=False,
                distribution=web.distribution,
                distribution_paths=["/index.html", "/config.json"],
            )
        CfnOutput(self, "SiteUrl", value=web.origin)

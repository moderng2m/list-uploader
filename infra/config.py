"""Per-environment settings (SPEC §22).

Values that differ per deployer (alert emails, an IdP's metadata URL) can also come
from CDK context, e.g. `cdk deploy -c env=dev -c alert_emails=me@example.com`, so
they never need to be committed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class SamlIdp:
    """Corporate SSO through Cognito (OQ-7). Users sign in with their IdP account."""

    name: str
    metadata_url: str
    # SAML attribute holding the user's email.
    email_attribute: str = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"


@dataclass(frozen=True)
class EnvConfig:
    name: str
    region: str
    send_to_prod: bool
    # "fake" = in-memory Workato/Bedrock fakes; no external calls, no real data.
    integrations: str
    bedrock_model_id: str
    alert_emails: tuple[str, ...] = ()
    row_retention_days: int = 90
    raw_file_retention_days: int = 90
    audit_retention_days: int = 730
    log_retention_days: int = 90
    # Parallel Post to Eloqua calls (SPEC §5.2): Map items in flight, 25 rows each.
    send_max_concurrency: int = 5
    # https origin the SPA is served from when it has a custom domain; None means
    # the CloudFront distribution's own domain.
    web_origin: str | None = None
    saml_idp: SamlIdp | None = None
    # Daily end-to-end synthetic check (SPEC §21.3.5); dev only.
    canary: bool = False
    api_rate_limit: int = 50  # requests per second, steady state
    api_burst_limit: int = 100
    # Downloaded processed files and audit exports are only needed for a few minutes.
    processed_file_retention_days: int = 1
    athena_results_retention_days: int = 30

    def __post_init__(self) -> None:
        if self.send_to_prod and self.name != "prod":
            raise ValueError(f"send_to_prod must be false outside prod (env={self.name})")


ENVS: dict[str, EnvConfig] = {
    # Personal build account: mocks and synthetic data only (see CLAUDE.md).
    "dev": EnvConfig(
        name="dev",
        region="us-east-1",
        send_to_prod=False,
        integrations="fake",
        bedrock_model_id="anthropic.claude-opus-5",
        canary=True,
    ),
    # Placeholder for the eventual TriNet deployment; not deployed from this account.
    "prod": EnvConfig(
        name="prod",
        region="us-east-1",
        send_to_prod=True,
        integrations="workato",
        bedrock_model_id="anthropic.claude-opus-5",
    ),
}


def get_config(name: str, context: dict[str, Any] | None = None) -> EnvConfig:
    """The named environment, with deployer-specific values from CDK context."""
    try:
        cfg = ENVS[name]
    except KeyError:
        raise ValueError(f"unknown env {name!r}; expected one of {sorted(ENVS)}") from None
    context = context or {}
    emails = context.get("alert_emails")
    if emails:
        cfg = replace(
            cfg, alert_emails=tuple(e.strip() for e in str(emails).split(",") if e.strip())
        )
    if context.get("saml_metadata_url"):
        cfg = replace(
            cfg,
            saml_idp=SamlIdp(
                name=str(context.get("saml_idp_name") or "CorporateSSO"),
                metadata_url=str(context["saml_metadata_url"]),
            ),
        )
    if context.get("web_origin"):
        cfg = replace(cfg, web_origin=str(context["web_origin"]).rstrip("/"))
    return cfg

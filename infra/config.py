"""Per-environment settings (SPEC §22). Runtime values go to SSM in later phases."""

from __future__ import annotations

from dataclasses import dataclass


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


ENVS: dict[str, EnvConfig] = {
    # Personal build account: mocks and synthetic data only (see CLAUDE.md).
    "dev": EnvConfig(
        name="dev",
        region="us-east-1",
        send_to_prod=False,
        integrations="fake",
        bedrock_model_id="anthropic.claude-opus-5",
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


def get_config(name: str) -> EnvConfig:
    try:
        return ENVS[name]
    except KeyError:
        raise ValueError(f"unknown env {name!r}; expected one of {sorted(ENVS)}") from None

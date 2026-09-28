"""Caller identity from the API Gateway JWT authorizer (SPEC §2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.audit import Actor, ActorType

ADMIN_GROUP = "admin"


@dataclass(frozen=True)
class User:
    email: str
    sub: str
    groups: frozenset[str]
    ip: str | None = None

    @property
    def is_admin(self) -> bool:
        return ADMIN_GROUP in self.groups

    def actor(self) -> Actor:
        return Actor(
            type=ActorType.ADMIN if self.is_admin else ActorType.USER,
            email=self.email,
            sub=self.sub,
            ip=self.ip,
        )


def _parse_groups(raw: Any) -> frozenset[str]:
    # HTTP API flattens array claims to strings like "[admin uploaders]".
    if isinstance(raw, list):
        return frozenset(str(g) for g in raw)
    if not raw:
        return frozenset()
    text = str(raw).strip().strip("[]")
    return frozenset(g for g in text.replace(",", " ").split() if g)


def user_from_event(event: dict[str, Any]) -> User | None:
    ctx = event.get("requestContext", {})
    claims = ctx.get("authorizer", {}).get("jwt", {}).get("claims", {})
    email = claims.get("email")
    sub = claims.get("sub")
    if not email or not sub:
        return None
    return User(
        email=str(email).lower(),
        sub=str(sub),
        groups=_parse_groups(claims.get("cognito:groups")),
        ip=ctx.get("http", {}).get("sourceIp"),
    )

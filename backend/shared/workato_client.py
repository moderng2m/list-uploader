"""Workato API Platform callables (SPEC §15, §16, §18).

`WorkatoClient` is the only way the app reaches SFDC, ZoomInfo, or Eloqua.
Environment guards (batch limits, `send_to_prod`) live in `BaseWorkatoClient`
so every implementation, fakes included, enforces them before any call.

This build uses `FakeWorkatoClient` only: no real Workato endpoints are
configured, and no real lead data is used.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from shared.observability import metrics

MAX_CAMPAIGN_IDS = 50
MAX_ENRICH_BATCH = 25


@dataclass(frozen=True)
class MemberStatus:
    label: str
    is_default: bool
    has_responded: bool
    sort_order: int


@dataclass(frozen=True)
class Campaign:
    id: str
    found: bool
    name: str | None = None
    type: str | None = None
    is_active: bool | None = None
    status: str | None = None
    member_statuses: tuple[MemberStatus, ...] = ()


@dataclass(frozen=True)
class PostResult:
    status_code: int
    error: str | None = None
    body: str | None = None
    workato_job_id: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class SendToProdViolation(RuntimeError):
    """A non-prod environment tried to post with `send_to_prod=true`."""


class WorkatoError(RuntimeError):
    """A callable failed as a whole (non-2xx from Workato, timeout, etc.)."""


class WorkatoClient(Protocol):
    def lookup_campaigns(
        self, campaign_ids: list[str], *, caller_job_id: str
    ) -> list[Campaign]: ...

    def enrich_contacts(
        self, contacts: list[dict[str, str]], *, caller_job_id: str
    ) -> dict[str, Any]: ...

    def post_to_eloqua(self, payload: dict[str, Any]) -> PostResult: ...


def assert_send_to_prod_allowed(payload: dict[str, Any], env: str) -> None:
    if env != "prod" and payload.get("send_to_prod") is not False:
        raise SendToProdViolation(
            f"send_to_prod must be false outside prod (env={env}, "
            f"got {payload.get('send_to_prod')!r})"
        )


T = TypeVar("T")


def _measured(call: Callable[[], T], failed: Callable[[T], bool] = lambda _: False) -> T:
    """Every callable call counts toward the Workato error-rate alarm (SPEC §21.3.4)."""
    t0 = time.monotonic()
    error = True
    try:
        result = call()
        error = failed(result)
        return result
    finally:
        metrics.add_metric(name="WorkatoCalls", unit="Count", value=1)
        metrics.add_metric(name="WorkatoErrors", unit="Count", value=1 if error else 0)
        metrics.add_metric(
            name="WorkatoLatencyMs", unit="Milliseconds", value=int((time.monotonic() - t0) * 1000)
        )


class BaseWorkatoClient(ABC):
    def __init__(self, env: str) -> None:
        self.env = env

    def lookup_campaigns(self, campaign_ids: list[str], *, caller_job_id: str) -> list[Campaign]:
        if len(campaign_ids) > MAX_CAMPAIGN_IDS:
            raise ValueError(f"at most {MAX_CAMPAIGN_IDS} campaign IDs per lookup")
        return _measured(lambda: self._lookup_campaigns(campaign_ids, caller_job_id))

    def enrich_contacts(
        self, contacts: list[dict[str, str]], *, caller_job_id: str
    ) -> dict[str, Any]:
        if len(contacts) > MAX_ENRICH_BATCH:
            raise ValueError(f"at most {MAX_ENRICH_BATCH} contacts per enrichment batch")
        return _measured(lambda: self._enrich_contacts(contacts, caller_job_id))

    def post_to_eloqua(self, payload: dict[str, Any]) -> PostResult:
        # The guard runs before the call and isn't counted as a call.
        assert_send_to_prod_allowed(payload, self.env)
        return _measured(lambda: self._post_to_eloqua(payload), failed=lambda result: not result.ok)

    @abstractmethod
    def _lookup_campaigns(self, campaign_ids: list[str], caller_job_id: str) -> list[Campaign]: ...

    @abstractmethod
    def _enrich_contacts(
        self, contacts: list[dict[str, str]], caller_job_id: str
    ) -> dict[str, Any]: ...

    @abstractmethod
    def _post_to_eloqua(self, payload: dict[str, Any]) -> PostResult: ...


@dataclass
class FakeWorkatoClient(BaseWorkatoClient):
    """In-memory Workato for tests and the mock-only deployment.

    - `campaigns`: known campaigns by 18-char ID; unknown IDs return `found=False`.
    - `enrich_handler`: builds the callable's response for a batch; default is all no_match.
    - `post_status`: HTTP status per `source_record_id`; default 200.
    - `fail_enrich`: raise `WorkatoError` for every enrichment batch.
    """

    env: str = "dev"
    campaigns: dict[str, Campaign] = field(default_factory=dict)
    post_status: dict[str, int] = field(default_factory=dict)
    fail_enrich: bool = False
    enrich_handler: Any = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.env)

    def _lookup_campaigns(self, campaign_ids: list[str], caller_job_id: str) -> list[Campaign]:
        self.calls.append(("lookup_campaigns", list(campaign_ids)))
        return [self.campaigns.get(cid, Campaign(id=cid, found=False)) for cid in campaign_ids]

    def _enrich_contacts(
        self, contacts: list[dict[str, str]], caller_job_id: str
    ) -> dict[str, Any]:
        self.calls.append(("enrich_contacts", [c.get("source_record_id") for c in contacts]))
        if self.fail_enrich:
            raise WorkatoError("ZoomInfo returned 500 (fake)")
        if self.enrich_handler is not None:
            result: dict[str, Any] = self.enrich_handler(contacts)
            return result
        return {
            "best_choices": [
                {
                    "source_record_id": c.get("source_record_id"),
                    "match_status": "no_match",
                    "accept_enrichment": False,
                    "needs_human_review": False,
                }
                for c in contacts
            ]
        }

    def _post_to_eloqua(self, payload: dict[str, Any]) -> PostResult:
        record_id = str(payload.get("source_record_id"))
        self.calls.append(("post_to_eloqua", record_id))
        status = self.post_status.get(record_id, 200)
        return PostResult(
            status_code=status,
            error=None if status < 300 else f"HTTP {status} (fake)",
            workato_job_id=f"fake-{len(self.calls)}",
        )

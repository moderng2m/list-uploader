"""Pre-send gate (SPEC §17) and the Post to Eloqua payload (SPEC §16.2).

The gate replaces MOps review, so it is evaluated on the server every time: when
the Review & Send screen loads, when the user confirms, and again in the first
step of SendWorkflow. The UI is never trusted.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from shared import messages
from shared.catalog import BY_KEY, CALLABLE_PARAMS, NOT_SENT_KEYS
from shared.issue_catalog import EXPLANATIONS

REQUIRED_FOR_SEND = (
    "email",
    "first_name",
    "last_name",
    "company",
    "campaign_id",
    "lead_source",
    "campaign_status",
    "campaign_name",
    "list_name",
)
CAMPAIGN_MAX_AGE = timedelta(hours=24)
SOURCE_SYSTEM = "list-uploader"
SOURCE_RECIPE_ID = "list-uploader-app"


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    by_campaign: list[dict[str, Any]] = field(default_factory=list)
    rows_to_send: int = 0
    campaign_count: int = 0
    excluded: int = 0
    not_sent_fields: list[str] = field(default_factory=list)
    sent_fields: list[str] = field(default_factory=list)
    campaigns_stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "reason_codes": self.reason_codes,
            "by_campaign": self.by_campaign,
            "rows_to_send": self.rows_to_send,
            "campaign_count": self.campaign_count,
            "excluded": self.excluded,
            "not_sent_fields": self.not_sent_fields,
            "sent_fields": self.sent_fields,
            "campaigns_stale": self.campaigns_stale,
        }

    def confirmation(self) -> dict[str, Any]:
        """What the user confirms: rows per campaign and status (SPEC §17 item 5)."""
        return {
            "rows_to_send": self.rows_to_send,
            "by_campaign": [
                {k: c[k] for k in ("campaign_id", "status", "rows")} for c in self.by_campaign
            ],
        }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate_gate(
    job: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    now: datetime | None = None,
) -> GateResult:
    now = now or datetime.now(UTC)
    rows = list(rows)
    result = GateResult(passed=False)

    def fail(code: str, reason: str) -> None:
        result.reason_codes.append(code)
        result.reasons.append(reason)

    if "analyzed_at" not in job:
        fail("NOT_ANALYZED", messages.GATE_NOT_ANALYZED)
        return result
    if job.get("enrich") and "enrichment_completed_at" not in job:
        fail("ENRICHMENT_NOT_RUN", messages.GATE_ENRICHMENT_NOT_RUN)

    sendable = [r for r in rows if not r.get("excluded")]
    result.excluded = len(rows) - len(sendable)
    result.rows_to_send = len(sendable)

    # 1. No blocking issues (pending ones count now; ENRICHMENT_REVIEW until decided).
    blocking: Counter[str] = Counter()
    undecided = 0
    for r in sendable:
        codes = {i["code"] for i in r.get("issues", []) if i["severity"] == "blocking"}
        blocking.update(codes)
        if any(i["code"] == "ENRICHMENT_REVIEW" for i in r.get("issues", [])):
            undecided += 1
    if blocking:
        n = sum(
            1 for r in sendable if any(i["severity"] == "blocking" for i in r.get("issues", []))
        )
        what = "; ".join(
            f"{EXPLANATIONS.get(code, code).rstrip('.').lower()} ({count})"
            for code, count in blocking.most_common()
        )
        fail(
            "BLOCKING_ISSUES",
            messages.GATE_BLOCKING.format(
                n=n, rows=_plural(n, "row", "rows"), have=_plural(n, "has", "have"), what=what
            ),
        )
    if undecided:
        fail(
            "ENRICHMENT_UNDECIDED",
            messages.GATE_ENRICHMENT_UNDECIDED.format(
                n=undecided,
                matches=_plural(undecided, "match", "matches"),
                are=_plural(undecided, "is", "are"),
            ),
        )

    # 2. Required values present.
    for key in REQUIRED_FOR_SEND:
        n = sum(1 for r in sendable if not (r.get("processed") or {}).get(key))
        if n:
            fail(
                f"MISSING:{key}",
                messages.GATE_MISSING_FIELD.format(
                    n=n,
                    rows=_plural(n, "row", "rows"),
                    are=_plural(n, "is", "are"),
                    field=BY_KEY[key].label,
                ),
            )

    # 3. Campaigns validated against Salesforce in this job within 24 hours.
    context = job.get("analysis_context") or {}
    validated = _parse_time(context.get("campaigns_validated_at"))
    if validated is None or now - validated > CAMPAIGN_MAX_AGE:
        result.campaigns_stale = True
        fail("CAMPAIGNS_STALE", messages.GATE_STALE_CAMPAIGNS)

    # 4. At least one row.
    if not sendable:
        fail("NO_ROWS", messages.GATE_NO_ROWS)

    # What will be sent, per campaign and member status (shown for confirmation, item 5).
    campaigns = context.get("campaigns") or {}
    groups: Counter[tuple[str, str]] = Counter()
    for r in sendable:
        p = r.get("processed") or {}
        groups[(p.get("campaign_id", ""), p.get("campaign_status", ""))] += 1
    result.by_campaign = [
        {
            "campaign_id": cid,
            "campaign_name": (campaigns.get(cid) or {}).get("name") or cid,
            "status": status,
            "rows": n,
        }
        for (cid, status), n in sorted(groups.items())
    ]
    result.campaign_count = len({cid for cid, _ in groups})
    with_values = {k for r in sendable for k, v in (r.get("processed") or {}).items() if v}
    result.not_sent_fields = [
        BY_KEY[k].label for k in BY_KEY if k in NOT_SENT_KEYS and k in with_values
    ]
    result.sent_fields = [BY_KEY[k].label for k in CALLABLE_PARAMS]
    result.passed = not result.reasons
    return result


def build_payload(job_id: str, row: Mapping[str, Any], *, send_to_prod: bool) -> dict[str, Any]:
    """SPEC §16.2. Only fields with a confirmed callable parameter are sent (OQ-1)."""
    processed = row.get("processed") or {}
    payload: dict[str, Any] = {
        "source_system": SOURCE_SYSTEM,
        "source_record_id": f"{job_id}:{row['row_id']}",
        "source_recipe_id": SOURCE_RECIPE_ID,
        "source_job_id": job_id,
        "send_to_prod": send_to_prod,
    }
    for key, param in CALLABLE_PARAMS.items():
        value = processed.get(key)
        if value:
            payload[param] = value
    return payload


def payload_sha256(payload: Mapping[str, Any]) -> str:
    """Hash of the exact JSON sent (SPEC §21.2.3)."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def sendable_row_ids(rows: Sequence[Mapping[str, Any]], *, only_failed: bool) -> list[int]:
    wanted = ("failed",) if only_failed else ("not_sent", "failed")
    return [
        r["row_id"]
        for r in rows
        if not r.get("excluded") and (r.get("send") or {}).get("status", "not_sent") in wanted
    ]

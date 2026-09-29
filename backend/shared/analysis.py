"""Row evaluation: processed values, provenance, issues, status (SPEC §7.3, §9, §11-§14).

`evaluate_row` is pure: given a stored row and the job's `AnalysisContext` it
returns the full evaluation. The analyze workflow and single-row re-validation
after an edit both use it, so they can't disagree. Duplicates need the whole
file and are applied afterwards by `apply_duplicates`.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from shared import messages
from shared.catalog import BY_KEY, FIELDS, NOT_SENT_KEYS
from shared.normalizer import (
    NORMALIZED_FIELDS,
    Normalizer,
    employee_count,
    free_text,
    naics_code,
    restore_zip_leading_zeros,
    zoominfo_id,
)
from shared.sfdc_ids import check_campaign_id

BLOCKING, WARNING, INFO = "blocking", "warning", "info"
JUNK_FIELDS = ("first_name", "last_name", "email", "company", "title")
ENRICHABLE_REQUIRED = frozenset({"company", "first_name", "last_name"})
MARKETING_PREFIX = "Marketing: "
LIST_NAME_SEP = " \u2013 "  # en dash, per OQ-2: campaign, date, uploader


# --- data -------------------------------------------------------------------------------


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    field: str | None = None
    source: str = "rule"  # rule | ai | sfdc | enrichment | parse
    suggestion: dict[str, Any] | None = None
    pending: bool = False  # blocking only until enrichment has had its chance

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "field": self.field,
            "source": self.source,
        }
        if self.suggestion is not None:
            out["suggestion"] = self.suggestion
        if self.pending:
            out["pending"] = True
        return out


@dataclass(frozen=True)
class CampaignInfo:
    id: str
    found: bool
    name: str | None = None
    type: str | None = None
    is_active: bool | None = None
    statuses: tuple[str, ...] = ()
    default_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "found": self.found,
            "name": self.name,
            "type": self.type,
            "is_active": self.is_active,
            "statuses": list(self.statuses),
            "default_status": self.default_status,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CampaignInfo:
        return cls(
            id=d["id"],
            found=bool(d["found"]),
            name=d.get("name"),
            type=d.get("type"),
            is_active=d.get("is_active"),
            statuses=tuple(d.get("statuses", [])),
            default_status=d.get("default_status"),
        )


@dataclass(frozen=True)
class LeadSourceResolution:
    """How one distinct lead source value resolves (SPEC §12)."""

    resolved: str | None  # value to use; None if not resolvable
    method: str  # exact | prefix | ai | suggested | none
    confidence: float | None = None
    suggestion: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "method": self.method,
            "confidence": self.confidence,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> LeadSourceResolution:
        return cls(d.get("resolved"), d["method"], d.get("confidence"), d.get("suggestion"))


@dataclass
class AnalysisContext:
    mapping: dict[str, str]  # source header -> field key (mapped columns only)
    campaigns: dict[str, CampaignInfo]
    lead_sources: tuple[str, ...]
    lead_source_resolutions: dict[str, LeadSourceResolution]
    thresholds: dict[str, float]
    enrich: bool
    normalizer: Normalizer
    owner_email: str
    list_date: str  # YYYY-MM-DD for generated list names
    enrichment_done: bool = False  # enrichment has run for this job (P4)


@dataclass
class RowEvaluation:
    processed: dict[str, str]
    provenance: dict[str, str]
    issues: list[Issue] = field(default_factory=list)
    status: str = "ready"
    junk_rule_hits: dict[str, str] = field(default_factory=dict)

    def issue_codes(self) -> set[str]:
        return {i.code for i in self.issues}


# --- helpers ------------------------------------------------------------------------------


def lead_source_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def resolve_lead_source_rules(value: str, active: Sequence[str]) -> LeadSourceResolution | None:
    """Deterministic steps of SPEC §12: exact (case/space-insensitive), then the
    'Marketing: ' prefix fix. None means the AI step is needed."""
    key = lead_source_key(value)
    by_key = {lead_source_key(v): v for v in active}
    if key in by_key:
        return LeadSourceResolution(by_key[key], "exact")
    for candidate in active:
        if candidate.startswith(MARKETING_PREFIX) and (
            lead_source_key(candidate[len(MARKETING_PREFIX) :]) == key
        ):
            return LeadSourceResolution(candidate, "prefix")
    return None


def label(key: str) -> str:
    return BY_KEY[key].label if key in BY_KEY else key


_PLACEHOLDERS = frozenset(
    {"test", "asdf", "n/a", "na", "none", "xxx", "-", ".", "null", "unknown", "tbd", "?"}
)
_KEYBOARD_RUNS = ("qwerty", "asdfgh", "zxcvbn", "asdf", "qwer")
_JUNK_LOCALS = frozenset({"test", "asdf", "example"})
_JUNK_DOMAINS = frozenset({"example.com", "test.com"})


def junk_rule_hits(processed: Mapping[str, str]) -> dict[str, str]:
    """SPEC §14.2 deterministic pre-checks -> {field: reason_code}."""
    hits: dict[str, str] = {}
    for f in JUNK_FIELDS:
        value = processed.get(f, "").strip()
        low = value.casefold()
        if not value:
            continue
        if f == "email":
            local, _, domain = low.partition("@")
            if local in _JUNK_LOCALS or domain in _JUNK_DOMAINS:
                hits[f] = "test_record"
            continue
        if low in _PLACEHOLDERS:
            hits[f] = "placeholder"
        elif (len(low) >= 3 and len(set(low.replace(" ", ""))) == 1) or any(
            run in low.replace(" ", "") for run in _KEYBOARD_RUNS
        ):
            hits[f] = "keyboard_mash"
        elif f in ("first_name", "last_name") and any(ch.isdigit() for ch in value):
            hits[f] = "not_a_person_name"
    first, last, company = (
        processed.get(k, "").strip().casefold() for k in ("first_name", "last_name", "company")
    )
    if first and first == last == company:
        hits.setdefault("company", "company_looks_like_person")
    return hits


_NAME_ALLOWED = re.compile(r"[^\w\s'.-]|\d", re.UNICODE)


def _name_changed(before: str, after: str) -> bool:
    return bool(_NAME_ALLOWED.search(before)) and not _NAME_ALLOWED.search(after)


def derive_status(issues: Iterable[Issue], excluded: bool) -> str:
    """SPEC §9 row status."""
    if excluded:
        return "excluded"
    blocking = [i for i in issues if i.severity == BLOCKING]
    if any(not i.pending for i in blocking):
        return "blocked"
    if blocking:
        return "pending_enrichment"
    if any(i.severity == WARNING for i in issues):
        return "warning"
    return "ready"


# --- evaluation --------------------------------------------------------------------------


def current_edits(row: Mapping[str, Any]) -> dict[str, str]:
    """Latest user edit per field."""
    edits: dict[str, str] = {}
    for edit in row.get("user_edits", []):
        edits[edit["field"]] = edit["to"]
    return edits


def evaluate_row(row: Mapping[str, Any], ctx: AnalysisContext) -> RowEvaluation:
    source: Mapping[str, str] = row.get("source", {})
    edits = current_edits(row)
    mapped = {key: str(source.get(header, "")) for header, key in ctx.mapping.items()}
    # A user edit replaces the source value and is normalized like one (SPEC §11.1).
    inputs = {**mapped, **edits}
    # Enrichment fills blanks only, never email, and is normalized like source
    # values (SPEC §7.3 step 3, §15.4). Review matches apply only if the user said so.
    enrichment = row.get("enrichment") or {}
    enriched: set[str] = set()
    if enrichment.get("status") == "accepted" or (
        enrichment.get("status") == "review" and enrichment.get("decision") == "apply"
    ):
        for key, value in (enrichment.get("fields") or {}).items():
            if key != "email" and value and not str(inputs.get(key, "")).strip():
                inputs[key] = str(value)
                enriched.add(key)
    normalized = ctx.normalizer.normalize(inputs)
    flags = normalized.flags

    processed: dict[str, str] = {}
    provenance: dict[str, str] = {}
    issues: list[Issue] = []

    def put(key: str, value: str, how: str) -> None:
        if value:
            processed[key] = value
            provenance[key] = how
        else:
            processed.pop(key, None)
            provenance.pop(key, None)

    def origin(key: str, value: str) -> str:
        if key in edits:
            return "user_edit"
        if key in enriched:
            return "enrichment:zoominfo"
        return "source" if value == inputs.get(key, "") else "normalized"

    # 1-2. User edit / normalized source.
    for f in FIELDS:
        raw = inputs.get(f.key, "")
        if f.key in NORMALIZED_FIELDS:
            value = normalized.values.get(f.key, "")
        elif f.key == "employee_count":
            value, ok = employee_count(raw)
            if not ok:
                issues.append(_format_issue(f.key, raw))
        elif f.key == "naics_code":
            value, ok = naics_code(raw)
            if not ok:
                issues.append(_format_issue(f.key, raw))
        elif f.key in ("zi_contact_id", "zi_company_id"):
            value, ok = zoominfo_id(raw)
            if not ok:
                issues.append(_format_issue(f.key, raw))
        elif f.key == "notes":
            value = free_text(raw, 1000)
        else:
            value = free_text(raw)
        put(f.key, value, origin(f.key, value))

    # Enrichment outcomes that need the user (SPEC §15.4).
    if enrichment.get("status") == "review" and not enrichment.get("decision"):
        issues.append(
            Issue("ENRICHMENT_REVIEW", WARNING, messages.ENRICHMENT_REVIEW, None, "enrichment")
        )
    if (
        provenance.get("linkedin_url") == "enrichment:zoominfo"
        and int(enrichment.get("linkedin_profile_count") or 0) > 1
    ):
        issues.append(
            Issue(
                "LINKEDIN_MULTIPLE_PROFILES",
                WARNING,
                messages.LINKEDIN_MULTIPLE_PROFILES,
                "linkedin_url",
                "enrichment",
            )
        )

    # Parse-time Excel errors on mapped columns.
    for pi in row.get("issues_parse", []):
        key = ctx.mapping.get(pi.get("column", ""))
        if key:
            issues.append(Issue(pi["code"], WARNING, pi["message"], key, "parse"))

    # ZIP leading zeros (SPEC §7.1).
    restored = restore_zip_leading_zeros(
        processed.get("postal_code", ""), processed.get("country", "")
    )
    if restored:
        before = processed["postal_code"]
        put("postal_code", restored, "normalized")
        issues.append(
            Issue(
                "ZIP_LEADING_ZERO_RESTORED",
                INFO,
                messages.ZIP_LEADING_ZERO_RESTORED.format(before=before, after=restored),
                "postal_code",
            )
        )

    # Email.
    email = processed.get("email", "")
    if email:
        if not flags.get("email_is_valid"):
            issues.append(
                Issue(
                    "EMAIL_INVALID", BLOCKING, messages.EMAIL_INVALID.format(value=email), "email"
                )
            )
        else:
            if flags.get("email_is_role_based"):
                issues.append(
                    Issue(
                        "EMAIL_ROLE_BASED",
                        WARNING,
                        messages.EMAIL_ROLE_BASED.format(value=email),
                        "email",
                    )
                )
            if flags.get("email_public_domain_address"):
                issues.append(
                    Issue(
                        "EMAIL_PUBLIC_DOMAIN",
                        WARNING,
                        messages.EMAIL_PUBLIC_DOMAIN.format(value=email),
                        "email",
                    )
                )

    # Names changed by the normalizer.
    for key in ("first_name", "last_name"):
        before, after = inputs.get(key, "").strip(), processed.get(key, "")
        if before and after and before != after and _name_changed(before, after):
            issues.append(
                Issue(
                    "NAME_CHANGED_BY_NORMALIZER",
                    INFO,
                    messages.NAME_CHANGED.format(field=label(key), before=before, after=after),
                    key,
                )
            )

    # Phone and country (warn only when the source had something).
    phone_raw = inputs.get("phone", "").strip()
    if phone_raw and not flags.get("phone_is_valid"):
        issues.append(
            Issue("PHONE_INVALID", WARNING, messages.PHONE_INVALID.format(value=phone_raw), "phone")
        )
    country_raw = inputs.get("country", "").strip()
    if country_raw and not flags.get("country_is_valid"):
        issues.append(
            Issue(
                "COUNTRY_UNRECOGNIZED",
                WARNING,
                messages.COUNTRY_UNRECOGNIZED.format(value=country_raw),
                "country",
            )
        )

    # Campaign and everything derived from it.
    campaign = _campaign_step(processed, provenance, issues, ctx, put)
    _lead_source_step(processed, issues, ctx, campaign, put, edits)
    _status_step(processed, issues, campaign, put)
    if campaign and not processed.get("list_name"):
        name = LIST_NAME_SEP.join((campaign.name or "", ctx.list_date, ctx.owner_email))
        put("list_name", name, "derived:auto_list_name")

    # Required fields (SPEC §9). Fields derived from the campaign are only
    # required once the campaign itself is valid; until then its issue covers them.
    for key in ("email", "campaign_id", "company", "first_name", "last_name"):
        if not processed.get(key):
            # Pending until enrichment has had its chance (SPEC §9 footnote *): before it
            # runs, or while this row's match awaits the user's apply/skip.
            awaiting = not ctx.enrichment_done or (
                enrichment.get("status") == "review" and not enrichment.get("decision")
            )
            pending = ctx.enrich and key in ENRICHABLE_REQUIRED and awaiting
            msg = messages.REQUIRED_PENDING if pending else messages.REQUIRED_MISSING
            issues.append(
                Issue(
                    "REQUIRED_MISSING", BLOCKING, msg.format(field=label(key)), key, pending=pending
                )
            )
    if campaign:
        for key in ("lead_source", "campaign_status", "list_name", "campaign_name"):
            if not processed.get(key) and not any(
                i.field == key and i.severity == BLOCKING for i in issues
            ):
                issues.append(
                    Issue(
                        "REQUIRED_MISSING",
                        BLOCKING,
                        messages.REQUIRED_MISSING.format(field=label(key)),
                        key,
                    )
                )

    # Junk: deterministic rules + stored AI flags (SPEC §14.2).
    rule_hits = junk_rule_hits(processed)
    issues.extend(_junk_issues(processed, rule_hits, row.get("ai_flags") or [], ctx))

    # Fields with no Eloqua destination yet (OQ-1).
    not_sent = [label(k) for k in sorted(NOT_SENT_KEYS) if processed.get(k)]
    if not_sent:
        issues.append(
            Issue(
                "NOT_SENT_FIELD", INFO, messages.NOT_SENT_FIELD.format(fields=", ".join(not_sent))
            )
        )

    dismissed = set(row.get("dismissed", []))
    issues = [i for i in issues if f"{i.code}:{i.field}" not in dismissed]
    status = derive_status(issues, bool(row.get("excluded")))
    return RowEvaluation(processed, provenance, issues, status, rule_hits)


def _format_issue(key: str, raw: str) -> Issue:
    return Issue(
        "FIELD_FORMAT_INVALID",
        WARNING,
        messages.FIELD_FORMAT_INVALID.format(field=label(key), value=raw.strip()),
        key,
    )


def _campaign_step(
    processed: dict[str, str],
    provenance: dict[str, str],
    issues: list[Issue],
    ctx: AnalysisContext,
    put: Any,
) -> CampaignInfo | None:
    raw = processed.get("campaign_id", "")
    if not raw:
        return None
    check = check_campaign_id(raw)
    if check.value is None:
        if check.problem == "checksum":
            msg = messages.CAMPAIGN_BAD_CHECKSUM.format(id=raw)
        else:
            msg = messages.CAMPAIGN_ID_FORMAT.format(id=raw)
        issues.append(Issue("CAMPAIGN_ID_FORMAT", BLOCKING, msg, "campaign_id"))
        return None
    if check.converted_from_15:
        put("campaign_id", check.value, "normalized")
    campaign = ctx.campaigns.get(check.value)
    if campaign is None or not campaign.found:
        issues.append(
            Issue(
                "CAMPAIGN_NOT_FOUND",
                BLOCKING,
                messages.CAMPAIGN_NOT_FOUND.format(id=check.value),
                "campaign_id",
                "sfdc",
            )
        )
        return None
    if campaign.is_active is False:
        issues.append(
            Issue(
                "CAMPAIGN_INACTIVE",
                WARNING,
                messages.CAMPAIGN_INACTIVE.format(campaign=campaign.name),
                "campaign_id",
                "sfdc",
            )
        )
    supplied = processed.get("campaign_name", "")
    if supplied and supplied.casefold() != (campaign.name or "").casefold():
        issues.append(
            Issue(
                "CAMPAIGN_NAME_MISMATCH",
                WARNING,
                messages.CAMPAIGN_NAME_MISMATCH.format(supplied=supplied, actual=campaign.name),
                "campaign_name",
                "sfdc",
            )
        )
    put("campaign_name", campaign.name or "", "derived:sfdc_campaign_name")
    return campaign


def _lead_source_step(
    processed: dict[str, str],
    issues: list[Issue],
    ctx: AnalysisContext,
    campaign: CampaignInfo | None,
    put: Any,
    edits: Mapping[str, str],
) -> None:
    value = processed.get("lead_source", "")
    how_prefix = "user_edit" if "lead_source" in edits else None
    if not value:
        if campaign and campaign.type and campaign.type in ctx.lead_sources:
            put("lead_source", campaign.type, "derived:sfdc_campaign_type")
        return
    res = resolve_lead_source_rules(value, ctx.lead_sources) or ctx.lead_source_resolutions.get(
        lead_source_key(value), LeadSourceResolution(None, "none")
    )
    auto = ctx.thresholds.get("lead_source_auto_threshold", 0.9)
    if res.method == "exact" and res.resolved:
        put(
            "lead_source",
            res.resolved,
            how_prefix or ("source" if res.resolved == value else "normalized"),
        )
    elif (
        res.method in ("prefix", "ai")
        and res.resolved
        and (res.method == "prefix" or (res.confidence or 0) >= auto)
    ):
        put(
            "lead_source",
            res.resolved,
            how_prefix
            or f"auto_corrected:{'rule:marketing_prefix' if res.method == 'prefix' else 'ai'}",
        )
        issues.append(
            Issue(
                "LEAD_SOURCE_AUTO_CORRECTED",
                INFO,
                messages.LEAD_SOURCE_AUTO_CORRECTED_ROW.format(from_=value, to=res.resolved),
                "lead_source",
                "rule" if res.method == "prefix" else "ai",
                {"value": res.resolved, "confidence": res.confidence},
            )
        )
    elif res.suggestion:
        pct = round((res.confidence or 0) * 100)
        issues.append(
            Issue(
                "LEAD_SOURCE_SUGGESTED",
                BLOCKING,
                messages.LEAD_SOURCE_SUGGESTED.format(from_=value, to=res.suggestion, pct=pct),
                "lead_source",
                "ai",
                {"value": res.suggestion, "confidence": res.confidence},
            )
        )
        return
    else:
        issues.append(
            Issue(
                "LEAD_SOURCE_INVALID",
                BLOCKING,
                messages.LEAD_SOURCE_INVALID.format(value=value),
                "lead_source",
            )
        )
        return
    resolved = processed.get("lead_source", "")
    if (
        campaign
        and campaign.type
        and campaign.type in ctx.lead_sources
        and resolved != campaign.type
    ):
        issues.append(
            Issue(
                "LEAD_SOURCE_CAMPAIGN_MISMATCH",
                WARNING,
                messages.LEAD_SOURCE_CAMPAIGN_MISMATCH.format(
                    value=resolved, campaign_type=campaign.type
                ),
                "lead_source",
                "sfdc",
            )
        )


def _status_step(
    processed: dict[str, str],
    issues: list[Issue],
    campaign: CampaignInfo | None,
    put: Any,
) -> None:
    if campaign is None:
        return
    value = processed.get("campaign_status", "")
    if not value:
        if campaign.default_status:
            put("campaign_status", campaign.default_status, "derived:sfdc_default_status")
            issues.append(
                Issue(
                    "STATUS_DEFAULTED",
                    INFO,
                    messages.STATUS_DEFAULTED_ROW.format(default=campaign.default_status),
                    "campaign_status",
                    "sfdc",
                )
            )
        return
    by_key = {s.casefold(): s for s in campaign.statuses}
    if value.casefold() in by_key:
        canonical = by_key[value.casefold()]
        if canonical != value:
            put("campaign_status", canonical, "normalized")
        return
    close = difflib.get_close_matches(value, list(campaign.statuses), n=3, cutoff=0.0)
    issues.append(
        Issue(
            "STATUS_INVALID",
            BLOCKING,
            messages.STATUS_INVALID.format(
                value=value, campaign=campaign.name, list=", ".join(campaign.statuses)
            ),
            "campaign_status",
            "sfdc",
            {"options": close},
        )
    )


def _junk_issues(
    processed: Mapping[str, str],
    rule_hits: Mapping[str, str],
    ai_flags: Sequence[Mapping[str, Any]],
    ctx: AnalysisContext,
) -> list[Issue]:
    flag_at = ctx.thresholds.get("junk_flag_threshold", 0.7)
    block_at = ctx.thresholds.get("junk_block_threshold", 0.9)
    best_ai: dict[str, Mapping[str, Any]] = {}
    for f in ai_flags:
        key = f.get("field")
        if key in JUNK_FIELDS and float(f.get("confidence", 0)) > float(
            best_ai.get(key, {}).get("confidence", -1)
        ):
            best_ai[key] = f
    out: list[Issue] = []
    for key in JUNK_FIELDS:
        value = processed.get(key, "")
        ai = best_ai.get(key)
        conf = float(ai["confidence"]) if ai else 0.0
        if ai and conf >= block_at and key in rule_hits:
            out.append(
                Issue(
                    "VALUE_JUNK",
                    BLOCKING,
                    messages.VALUE_JUNK.format(field=label(key), value=value),
                    key,
                    "ai",
                    {"reason": ai.get("reason_code"), "confidence": conf},
                )
            )
        elif ai and conf >= flag_at:
            out.append(
                Issue(
                    "VALUE_SUSPECT",
                    WARNING,
                    messages.VALUE_SUSPECT.format(
                        field=label(key), value=value, explanation=ai.get("explanation", "")
                    ),
                    key,
                    "ai",
                    {"reason": ai.get("reason_code"), "confidence": conf},
                )
            )
        elif key in rule_hits:
            out.append(
                Issue(
                    "VALUE_SUSPECT",
                    WARNING,
                    messages.VALUE_SUSPECT.format(field=label(key), value=value, explanation=""),
                    key,
                    "rule",
                    {"reason": rule_hits[key]},
                )
            )
    return out


# --- whole-file steps --------------------------------------------------------------------


def apply_duplicates(evaluations: dict[int, RowEvaluation], excluded: set[int]) -> None:
    """DUPLICATE_IN_FILE on later rows with the same processed email and campaign ID."""
    groups: dict[tuple[str, str], list[int]] = {}
    for row_id in sorted(evaluations):
        ev = evaluations[row_id]
        email = ev.processed.get("email", "").casefold()
        campaign_id = ev.processed.get("campaign_id", "")
        if email and campaign_id and row_id not in excluded:
            groups.setdefault((email, campaign_id), []).append(row_id)
    for (email, _), ids in groups.items():
        for later in ids[1:]:
            ev = evaluations[later]
            ev.issues.append(
                Issue(
                    "DUPLICATE_IN_FILE",
                    BLOCKING,
                    messages.DUPLICATE_ROW.format(email=email, n=len(ids), first=ids[0]),
                    "email",
                    "rule",
                    {"first_row_id": ids[0]},
                )
            )
            ev.status = derive_status(ev.issues, False)


def summarize(statuses: Iterable[str]) -> dict[str, int]:
    counts = {s: 0 for s in ("ready", "warning", "blocked", "excluded", "pending_enrichment")}
    total = 0
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1
        total += 1
    return {
        "rows_total": total,
        "rows_ready": counts["ready"],
        "rows_warning": counts["warning"],
        "rows_blocked": counts["blocked"],
        "rows_excluded": counts["excluded"],
        "rows_pending_enrichment": counts["pending_enrichment"],
    }

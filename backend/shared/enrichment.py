"""Enrichment providers (SPEC §15). ZoomInfo via the Workato callable is the only v1
provider; the protocol keeps Clay a drop-in later (§15.6).

Results are provider-neutral `EnrichResult`s. The merge into a row (fill blanks
only, never email) happens in `analysis.evaluate_row`, so enriched values go
through the same normalization and precedence as everything else.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.analysis import ENRICHABLE_REQUIRED
from shared.workato_client import MAX_ENRICH_BATCH, WorkatoClient

# ZoomInfo "zi_best_*" suffix -> canonical field (SPEC §8). Email is never filled.
ZI_FIELDS: dict[str, str] = {
    "company_name": "company",
    "first_name": "first_name",
    "last_name": "last_name",
    "job_title": "title",
    "phone": "phone",
    "mobile_phone": "mobile_phone",
    "street": "address_line_1",
    "city": "city",
    "state": "state_province",
    "zip_code": "postal_code",
    "country": "country",
    "contact_id": "zi_contact_id",
    "company_id": "zi_company_id",
    "company_naics_codes": "naics_code",
    "company_primary_industry": "industry",
    "company_website": "website",
    "company_employee_count": "employee_count",
    "linkedin_url": "linkedin_url",
}
# Spellings tried when reading selected_candidate_json. Its exact keys aren't in
# the recipe docs yet (need a sample response); these cover snake, camel and
# zi_best_ forms.
_CANDIDATE_ALIASES: dict[str, tuple[str, ...]] = {
    suffix: (
        f"zi_best_{suffix}",
        suffix,
        "".join(w if i == 0 else w.capitalize() for i, w in enumerate(suffix.split("_"))),
    )
    for suffix in ZI_FIELDS
}
_CANDIDATE_ALIASES["email"] = ("zi_best_email", "email", "emailAddress")

ACCEPTED = frozenset({"high_confidence", "likely_match"})
REVIEW = frozenset(
    {
        "possible_match",
        "ambiguous",
        "conflicting_profile",
        "possible_match_low_accuracy",
        "low_confidence",
    }
)

# Conflict codes -> plain English (SPEC §15.4). Unknown codes get a generic line.
CONFLICT_TEXT: dict[str, str] = {
    "current_company_mismatch": "ZoomInfo shows a different current employer.",
    "company_mismatch": "ZoomInfo shows a different company.",
    "name_mismatch": "The name in ZoomInfo is different.",
    "first_name_mismatch": "ZoomInfo has a different first name.",
    "last_name_mismatch": "ZoomInfo has a different last name.",
    "title_mismatch": "ZoomInfo shows a different job title.",
    "email_domain_mismatch": "The email's domain doesn't match the company in ZoomInfo.",
    "location_mismatch": "ZoomInfo shows a different location.",
    "low_contact_accuracy": "ZoomInfo isn't confident this contact's details are current.",
    "multiple_candidates": "ZoomInfo found more than one person who could match.",
    "phone_mismatch": "ZoomInfo has a different phone number.",
}


def translate_conflict(code: str) -> str:
    code = code.strip()
    return CONFLICT_TEXT.get(code, f"ZoomInfo flagged: {code.replace('_', ' ')}.")


@dataclass(frozen=True)
class EnrichInput:
    row_id: int
    values: Mapping[str, str]  # processed values (normalized)


@dataclass
class EnrichResult:
    row_id: int
    status: str  # accepted | review | no_match | error
    match_status: str | None = None
    fields: dict[str, str] = field(default_factory=dict)  # canonical -> value (fill candidates)
    score: float | None = None
    conflicts: list[str] = field(default_factory=list)  # plain English
    candidate_display: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    linkedin_profile_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "match_status": self.match_status,
            "fields": self.fields,
            "score": self.score,
            "conflicts": self.conflicts,
            "candidate_display": self.candidate_display,
            "notes": self.notes,
            "linkedin_profile_count": self.linkedin_profile_count,
        }


class EnrichmentProvider(Protocol):
    name: str
    max_batch: int

    def enrich(self, rows: Sequence[EnrichInput], *, job_id: str) -> list[EnrichResult]: ...


# --- eligibility (SPEC §15.2) -------------------------------------------------------------


def eligible(row: Mapping[str, Any]) -> bool:
    """ready/warning/pending rows, and blocked rows only when every blocker is a blank
    field enrichment can fill. Never excluded rows."""
    if row.get("excluded"):
        return False
    status = row.get("status")
    if status in ("ready", "warning", "pending_enrichment"):
        return True
    if status != "blocked":
        return False
    blockers = [i for i in row.get("issues", []) if i["severity"] == "blocking"]
    return all(
        i["code"] == "REQUIRED_MISSING" and i.get("field") in ENRICHABLE_REQUIRED for i in blockers
    )


# --- ZoomInfo via Workato ---------------------------------------------------------------------


def _first(value: Any) -> str:
    """ZI list-valued fields (NAICS codes, industries): the first entry, as text."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("id") or value.get("name") or ""
    return "" if value is None else str(value).strip()


def _dnc(choice: Mapping[str, Any], key: str) -> bool:
    return str(choice.get(key, "")).strip().lower() in ("true", "1", "yes")


class ZoomInfoProvider:
    name = "zoominfo"
    max_batch = MAX_ENRICH_BATCH

    def __init__(
        self,
        workato: WorkatoClient,
        *,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._workato = workato
        self._attempts = attempts
        self._sleep = sleep

    @staticmethod
    def contact(row: EnrichInput) -> dict[str, str]:
        """SPEC §15.3 request: processed values; exact ZI IDs make matching unambiguous."""
        v = row.values
        contact = {
            "source_record_id": str(row.row_id),
            "email": v.get("email", ""),
            "first_name": v.get("first_name", ""),
            "last_name": v.get("last_name", ""),
            "company_name": v.get("company", ""),
            "title": v.get("title", ""),
            "phone": v.get("phone", ""),
            "company_website": v.get("website", ""),
            "zi_contact_id": v.get("zi_contact_id", ""),
            "zi_company_id": v.get("zi_company_id", ""),
            "social_url": v.get("linkedin_url", ""),
        }
        return {k: val for k, val in contact.items() if val}

    def enrich(self, rows: Sequence[EnrichInput], *, job_id: str) -> list[EnrichResult]:
        if len(rows) > self.max_batch:
            raise ValueError(f"at most {self.max_batch} rows per call")
        contacts = [self.contact(r) for r in rows]
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                response = self._workato.enrich_contacts(contacts, caller_job_id=job_id)
                return self._parse(rows, response)
            except Exception as exc:  # the recipe fails the whole batch (SPEC §15.5)
                last_error = exc
                if attempt + 1 < self._attempts:
                    self._sleep(min(8.0, 1.0 * 2**attempt))
        note = f"service error ({type(last_error).__name__})"
        return [EnrichResult(r.row_id, "error", notes=[note]) for r in rows]

    def _parse(
        self, rows: Sequence[EnrichInput], response: Mapping[str, Any]
    ) -> list[EnrichResult]:
        by_id = {str(c.get("source_record_id")): c for c in response.get("best_choices") or []}
        return [self._one(r.row_id, by_id.get(str(r.row_id))) for r in rows]

    def _one(self, row_id: int, choice: Mapping[str, Any] | None) -> EnrichResult:
        if choice is None:
            return EnrichResult(row_id, "no_match", "missing_result")
        match_status = str(choice.get("match_status") or "")
        score = choice.get("match_score")
        codes = str(choice.get("conflicts_csv") or "").split(",")
        conflicts = [translate_conflict(c) for c in codes if c.strip()]
        profile_count = int(choice.get("zi_best_linkedin_profile_count") or 0)
        accepted = str(choice.get("accept_enrichment", "")).lower() == "true" or (
            choice.get("accept_enrichment") is True
        )
        review = str(choice.get("needs_human_review", "")).lower() == "true" or (
            choice.get("needs_human_review") is True
        )
        result = EnrichResult(
            row_id=row_id,
            status="no_match",
            match_status=match_status,
            score=float(score) if score not in (None, "") else None,
            conflicts=conflicts,
            linkedin_profile_count=profile_count,
        )
        if accepted:
            result.status = "accepted"
            result.fields, result.notes = self._fields(choice, lambda s: choice.get(f"zi_best_{s}"))
        elif review:
            candidate = _candidate(choice.get("selected_candidate_json"))

            def lookup(suffix: str) -> Any:
                for key in _CANDIDATE_ALIASES[suffix]:
                    if candidate.get(key) not in (None, ""):
                        return candidate[key]
                return None

            result.status = "review"
            result.fields, result.notes = self._fields(choice, lookup)
            name = " ".join(
                filter(None, (_first(lookup("first_name")), _first(lookup("last_name"))))
            )
            display = {
                "name": name,
                "company": _first(lookup("company_name")),
                "title": _first(lookup("job_title")),
                "email": _first(lookup("email")),
            }
            result.candidate_display = {k: v for k, v in display.items() if v}
        return result

    @staticmethod
    def _fields(
        choice: Mapping[str, Any], lookup: Callable[[str], Any]
    ) -> tuple[dict[str, str], list[str]]:
        fields: dict[str, str] = {}
        notes: list[str] = []
        for suffix, key in ZI_FIELDS.items():
            value = _first(lookup(suffix))
            if not value:
                continue
            # Do-not-call: never fill the number (SPEC §15.4).
            if key == "phone" and _dnc(choice, "zi_best_direct_phone_do_not_call"):
                notes.append("ZoomInfo marks the direct phone do-not-call, so it wasn't added.")
                continue
            if key == "mobile_phone" and _dnc(choice, "zi_best_mobile_phone_do_not_call"):
                notes.append("ZoomInfo marks the mobile phone do-not-call, so it wasn't added.")
                continue
            fields[key] = value
        return fields, notes


def _candidate(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

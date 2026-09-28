"""Normalizer integration (SPEC §11.1) and the fields it doesn't cover (§11.2).

The real normalizer is lead normalizer v5, added unmodified under
`shared/lead_normalizer/` with a `main(inputs) -> outputs` entry point. Until
it arrives, `StandInNormalizer` runs instead. It is deliberately minimal (trim,
lowercase emails, basic E.164 for US numbers) and is NOT a model of v5's
behavior; its version string makes that visible in every audit snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

# Processed field <- normalizer output key (SPEC §11.1 table).
OUTPUT_TO_FIELD: dict[str, str] = {
    "clean_first_name": "first_name",
    "clean_last_name": "last_name",
    "clean_title": "title",
    "clean_company_name": "company",
    "clean_email": "email",
    "clean_phone_e164": "phone",
    "clean_mobile_phone_e164": "mobile_phone",
    "clean_address_line_1": "address_line_1",
    "clean_city": "city",
    "clean_state_province": "state_province",
    "clean_postal_code": "postal_code",
    "country_display_name": "country",
    "clean_website_url": "website",
    "clean_linkedin_url": "linkedin_url",
}
# Normalizer input key <- processed field.
FIELD_TO_INPUT: dict[str, str] = {
    "first_name": "first_name",
    "last_name": "last_name",
    "title": "title",
    "email": "email",
    "company": "company_name",
    "address_line_1": "address_line_1",
    "city": "city",
    "state_province": "state_province",
    "country": "country",
    "phone": "phone",
    "mobile_phone": "mobile_phone",
    "postal_code": "postal_code",
    "linkedin_url": "linkedin_url",
    "website": "website_url",
}
FLAG_KEYS = (
    "email_is_valid",
    "email_is_role_based",
    "email_public_domain_address",
    "phone_is_valid",
    "country_is_valid",
)
NORMALIZED_FIELDS = frozenset(OUTPUT_TO_FIELD.values())


@dataclass(frozen=True)
class Normalized:
    values: dict[str, str]  # processed field -> value (only NORMALIZED_FIELDS)
    flags: dict[str, bool]


class Normalizer(Protocol):
    version: str

    def normalize(self, fields: Mapping[str, str]) -> Normalized: ...


class MainFunctionNormalizer:
    """Adapter for v5's `main(inputs)` contract."""

    def __init__(self, main: Callable[[dict[str, Any]], dict[str, Any]], version: str) -> None:
        self._main = main
        self.version = version

    def normalize(self, fields: Mapping[str, str]) -> Normalized:
        inputs = {inp: fields.get(field, "") or "" for field, inp in FIELD_TO_INPUT.items()}
        inputs["address_line_2"] = ""  # not in the template
        out = self._main(inputs)
        values = {field: str(out.get(key) or "") for key, field in OUTPUT_TO_FIELD.items()}
        flags = {k: bool(out.get(k)) for k in FLAG_KEYS}
        return Normalized(values, flags)


# --- stand-in -----------------------------------------------------------------------

_WS = re.compile(r"\s+")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_ROLE_LOCALS = frozenset(
    {"info", "sales", "admin", "support", "contact", "marketing", "hello", "office", "hr"}
)
_PUBLIC_DOMAINS = frozenset(
    {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com"}
)
_COUNTRIES = {
    "us": "United States", "usa": "United States", "united states": "United States",
    "united states of america": "United States", "canada": "Canada", "ca": "Canada",
    "uk": "United Kingdom", "united kingdom": "United Kingdom", "gb": "United Kingdom",
    "germany": "Germany", "de": "Germany", "mexico": "Mexico", "mx": "Mexico",
}  # fmt: skip


def _clean(value: str) -> str:
    return _WS.sub(" ", value).strip()


def _e164(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if value.strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


class StandInNormalizer:
    version = "stand-in-0.1 (NOT lead normalizer v5)"

    def normalize(self, fields: Mapping[str, str]) -> Normalized:
        values = {f: _clean(fields.get(f, "") or "") for f in NORMALIZED_FIELDS}
        values["email"] = values["email"].lower()
        country_key = values["country"].lower()
        if country_key in _COUNTRIES:
            values["country"] = _COUNTRIES[country_key]
        for phone_field in ("phone", "mobile_phone"):
            # Invalid numbers come back blank (SPEC §11.1); PHONE_INVALID shows the source.
            values[phone_field] = _e164(values[phone_field]) if values[phone_field] else ""
        email = values["email"]
        local, _, domain = email.partition("@")
        flags = {
            "email_is_valid": bool(_EMAIL.match(email)),
            "email_is_role_based": local in _ROLE_LOCALS,
            "email_public_domain_address": domain in _PUBLIC_DOMAINS,
            "phone_is_valid": bool(values["phone"]),
            "country_is_valid": values["country"] in set(_COUNTRIES.values()),
        }
        return Normalized(values, flags)


def load_normalizer() -> Normalizer:
    """v5 if it has been added to the repo, otherwise the stand-in."""
    try:
        from shared.lead_normalizer import main as v5_main  # type: ignore[attr-defined]
    except ImportError:
        return StandInNormalizer()
    version = getattr(v5_main, "__version__", "v5")
    return MainFunctionNormalizer(v5_main, f"lead-normalizer {version}")


# --- fields the normalizer doesn't cover (SPEC §11.2) -------------------------------------

_RANGE = re.compile(r"^\s*(\d[\d,]*)\s*(?:-|to|\u2013)\s*\d[\d,]*\s*$", re.IGNORECASE)


def employee_count(raw: str) -> tuple[str, bool]:
    """-> (value, ok). Range takes the lower bound; non-numeric -> ("", False)."""
    text = raw.strip()
    if not text:
        return "", True
    match = _RANGE.match(text)
    if match:
        text = match.group(1)
    digits = re.sub(r"[,\s+]", "", text)
    return (digits, True) if digits.isdigit() else ("", False)


def naics_code(raw: str) -> tuple[str, bool]:
    text = raw.strip()
    if not text:
        return "", True
    digits = re.sub(r"\.0$", "", text)
    ok = digits.isdigit() and 2 <= len(digits) <= 6
    return (digits, True) if ok else ("", False)


def zoominfo_id(raw: str) -> tuple[str, bool]:
    text = re.sub(r"\.0$", "", raw.strip())
    if not text:
        return "", True
    return (text, True) if text.isdigit() else ("", False)


def free_text(raw: str, max_len: int | None = None) -> str:
    text = _clean(raw)
    return text[:max_len] if max_len else text


US_NAMES = frozenset({"United States", "US", "USA", "United States of America"})


def restore_zip_leading_zeros(postal: str, country: str) -> str | None:
    """SPEC §7.1: a 3-4 digit ZIP in the US lost its leading zeros in Excel."""
    if country.strip() in US_NAMES and postal.isdigit() and 3 <= len(postal) <= 4:
        return postal.zfill(5)
    return None

"""Field catalog (SPEC §8) and seed aliases (SPEC §10.2).

`key` is the canonical snake_case name used everywhere after mapping.
`label` is the exact header in the 2026-09-02 template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATALOG_VERSION = "2026-09-23.1"


@dataclass(frozen=True)
class CatalogField:
    key: str
    label: str
    description: str  # one line, shown to the AI mapper
    required: bool = False
    # Why a required field may stay unmapped (filled later), or None if it must be mapped.
    fill_when_unmapped: str | None = None
    in_template: bool = True

    @property
    def must_map(self) -> bool:
        return self.required and self.fill_when_unmapped is None


FIELDS: tuple[CatalogField, ...] = (
    CatalogField("company", "Company", "Employer / organization name", required=True),
    CatalogField("first_name", "First name", "Person's given name", required=True),
    CatalogField("last_name", "Last Name", "Person's family name / surname", required=True),
    CatalogField("email", "Email Address", "Person's business email address", required=True),
    CatalogField(
        "lead_source",
        "Lead Source - Most Recent",
        "Marketing lead source picklist value, e.g. 'Marketing: Events'",
        required=True,
        fill_when_unmapped="derived from the Salesforce campaign type",
    ),
    CatalogField(
        "campaign_id",
        "SFDC Last Campaign ID",
        "Salesforce campaign ID, 15 or 18 characters starting with 701",
        required=True,
    ),
    CatalogField("title", "Title", "Job title"),
    CatalogField("phone", "Business Phone", "Business / work / direct phone number"),
    CatalogField("mobile_phone", "Mobile Phone", "Mobile / cell phone number"),
    CatalogField("address_line_1", "Address 1", "Street address"),
    CatalogField("city", "City", "City"),
    CatalogField("state_province", "State or Province", "State, province, or region"),
    CatalogField("postal_code", "Zip or Postal Code", "ZIP or postal code"),
    CatalogField("country", "Country", "Country name or code"),
    CatalogField("notes", "Notes additional Information", "Free-text notes about the lead"),
    CatalogField("zi_contact_id", "Zoom Individual ID", "ZoomInfo person/contact ID (digits)"),
    CatalogField("zi_company_id", "Zoom Company ID", "ZoomInfo company ID (digits)"),
    CatalogField(
        "campaign_status",
        "SFDC Last Campaign Status",
        "Campaign member status, e.g. Registered, Attended, No Show",
        required=True,
        fill_when_unmapped="blank statuses use the campaign's default status",
    ),
    CatalogField("naics_code", "NAICS Code", "Industry classification code, 2-6 digits"),
    CatalogField("industry", "Industry", "Industry name"),
    CatalogField("website", "Website", "Company website URL or domain"),
    CatalogField("employee_count", "Number of Employees", "Company employee count or range"),
    CatalogField(
        "list_name",
        "SFDC List Name",
        "Name of the Salesforce list this upload belongs to",
        required=True,
        # OQ-2 interim: auto-generated when blank.
        fill_when_unmapped="generated from campaign name, date, and uploader",
    ),
    CatalogField("last_response_class", "Last Response Class", "Marketing response classification"),
    CatalogField(
        "campaign_name",
        "SFDC Last Campaign Name",
        "Salesforce campaign name",
        required=True,
        fill_when_unmapped="taken from Salesforce",
    ),
    CatalogField("linkedin_url", "LinkedIn", "Person's LinkedIn profile URL", in_template=False),
)

BY_KEY: dict[str, CatalogField] = {f.key: f for f in FIELDS}
TEMPLATE_FIELDS: tuple[CatalogField, ...] = tuple(f for f in FIELDS if f.in_template)

SEED_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "e-mail", "email address", "work email", "business email"),
    "first_name": ("first", "firstname", "first name", "given name"),
    "last_name": ("last", "lastname", "surname", "family name"),
    "company": ("company name", "organization", "organisation", "account", "account name"),
    "title": ("job title", "position"),
    "phone": ("phone", "work phone", "business phone", "direct phone", "phone number"),
    "mobile_phone": ("mobile", "cell", "cell phone"),
    "postal_code": ("zip", "zip code", "postal code", "postcode"),
    "state_province": ("state", "province", "region"),
    "campaign_id": ("campaign id", "sfdc campaign id", "salesforce campaign id"),
    "campaign_status": ("status", "campaign member status", "member status"),
    "lead_source": ("lead source", "source"),
    "zi_contact_id": ("zoominfo contact id", "zoominfo individual id"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin profile"),
}

_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize_header(text: str) -> str:
    """SPEC §10.1: lowercase, trim, collapse whitespace, strip punctuation except '-'."""
    text = _PUNCT.sub(" ", text.lower()).replace("_", " ")
    return _SPACE.sub(" ", text).strip()


# Fields with no confirmed Post to Eloqua destination (SPEC §8 "confirm (OQ-1)").
# Until OQ-1 is resolved they stay in the processed file but are not sent, and
# rows with a value get a NOT_SENT_FIELD info issue.
NOT_SENT_KEYS: frozenset[str] = frozenset(
    {
        "mobile_phone",
        "notes",
        "zi_contact_id",
        "zi_company_id",
        "naics_code",
        "industry",
        "website",
        "employee_count",
        "list_name",
        "last_response_class",
    }
)

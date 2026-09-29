"""Plain-English explanation and safe bulk action per issue code (SPEC §6.3, §9)."""

from __future__ import annotations

EXPLANATIONS: dict[str, str] = {
    "REQUIRED_MISSING": "A required field is blank.",
    "EMAIL_INVALID": "The email address isn't valid.",
    "EMAIL_ROLE_BASED": "The email looks like a shared inbox, not a person.",
    "EMAIL_PUBLIC_DOMAIN": "The email is a personal address (Gmail, Yahoo, etc.).",
    "DUPLICATE_IN_FILE": "Same email and campaign as an earlier row.",
    "VALUE_SUSPECT": "A value looks like a placeholder or test data.",
    "VALUE_JUNK": "A value looks like junk, not real lead data.",
    "NAME_CHANGED_BY_NORMALIZER": "Numbers or symbols were removed from a name.",
    "PHONE_INVALID": "The phone number isn't valid, so it was left blank.",
    "COUNTRY_UNRECOGNIZED": "The country wasn't recognized.",
    "CAMPAIGN_ID_FORMAT": "The campaign ID is mistyped or isn't a campaign ID.",
    "CAMPAIGN_NOT_FOUND": "The campaign ID wasn't found in Salesforce.",
    "CAMPAIGN_INACTIVE": "The campaign is inactive in Salesforce.",
    "LEAD_SOURCE_INVALID": "The lead source isn't on the list.",
    "LEAD_SOURCE_SUGGESTED": "We suggested a lead source; accept it or pick another.",
    "LEAD_SOURCE_AUTO_CORRECTED": "A lead source was corrected automatically.",
    "LEAD_SOURCE_CAMPAIGN_MISMATCH": "The lead source doesn't match the campaign's type.",
    "STATUS_INVALID": "The status isn't one of the campaign's member statuses.",
    "STATUS_DEFAULTED": "Blank statuses will use the campaign's default.",
    "CAMPAIGN_NAME_MISMATCH": "The campaign name differs from Salesforce; Salesforce wins.",
    "EXCEL_ERROR_VALUE": "A cell had an Excel error and was left blank.",
    "ZIP_LEADING_ZERO_RESTORED": "A US ZIP code had its leading zero restored.",
    "FIELD_FORMAT_INVALID": "A value wasn't in a usable format and was left blank.",
    "NOT_SENT_FIELD": (
        "For your information: when you send in step 4, some fields won't go to Eloqua "
        "(Post to Eloqua doesn't accept them yet). They stay in your processed file. "
        "No action needed."
    ),
    "ENRICHMENT_REVIEW": "An enrichment match needs your decision.",
    "LINKEDIN_MULTIPLE_PROFILES": "ZoomInfo has more than one LinkedIn profile for the person.",
}

# Bulk action id -> the issue codes it applies to. Only actions that are safe to apply
# to every affected row at once (SPEC §6.3).
BULK_ACTIONS: dict[str, tuple[str, ...]] = {
    "accept_lead_source_suggestions": ("LEAD_SOURCE_SUGGESTED",),
    "exclude_duplicates": ("DUPLICATE_IN_FILE",),
    "exclude_junk": ("VALUE_JUNK", "VALUE_SUSPECT"),
}
BULK_ACTION_LABELS: dict[str, str] = {
    "accept_lead_source_suggestions": "Accept all suggested lead sources at 90% or higher",
    "exclude_duplicates": "Exclude duplicate rows",
    "exclude_junk": "Exclude all rows flagged as junk",
    "set_status": "Apply a status to all rows with this value",
}


def bulk_action_for(code: str) -> str | None:
    if code == "STATUS_INVALID":
        return "set_status"
    return next((a for a, codes in BULK_ACTIONS.items() if code in codes), None)

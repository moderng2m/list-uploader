"""All user-facing text (SPEC §20). Keep wording plain and say what to do next."""

WRONG_FILE_TYPE = (
    "This file is a .{ext}. Upload a .csv or .xlsx file — you can download the template below."
)
CAMPAIGN_NOT_FOUND = (
    "Campaign ID {id} wasn't found in Salesforce. Open the campaign in Salesforce, copy the "
    "18-character ID from the URL, and paste it here."
)
CAMPAIGN_BAD_CHECKSUM = (
    "Campaign ID {id} looks mistyped — its last three characters don't match. IDs are "
    "case-sensitive; copy it directly from Salesforce."
)
STATUS_INVALID = "'{value}' isn't a member status on {campaign}. Valid statuses: {list}."
STATUS_DEFAULTED = "{n} rows had no status and will use this campaign's default, '{default}'."
LEAD_SOURCE_AUTO_CORRECTED = "'{from_}' was changed to '{to}' on {n} rows."
LEAD_SOURCE_SUGGESTED = "We think '{from_}' means '{to}' ({pct}% sure). Accept or pick another."
DUPLICATE = (
    "{email} appears {n} times for the same campaign. Only the first row will be sent unless "
    "you choose otherwise."
)
RESULT = "{n} leads submitted to Eloqua for {campaigns} on {datetime}."

ACCESS_DENIED = "You don't have access to this. Ask a Marketing Tech Ops admin if you need it."
SERVICE_UNAVAILABLE = (
    "We couldn't record this action, so it wasn't saved. Try again in a minute; if it keeps "
    "happening, contact Marketing Tech Ops."
)

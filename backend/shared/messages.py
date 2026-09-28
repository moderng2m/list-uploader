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

# --- Upload and parse (SPEC §6.1, §7.1) ---
FILE_TOO_LARGE = "This file is over {limit_mb} MB. Split it into smaller files and upload each one."
EMPTY_FILE = "This file is empty. Check that you saved your data in it, then upload it again."
NO_DATA_ROWS = (
    "This file has a header row but no data rows. Add your leads under the headers, then "
    "upload it again."
)
TOO_MANY_ROWS = (
    "This file has more than {limit:,} rows. Split it into files of {limit:,} rows or fewer "
    "and upload each one."
)
TOO_MANY_COLUMNS = (
    "This file has more than {limit} columns. Delete the columns you don't need, then upload "
    "it again."
)
ROW_TOO_LARGE = (
    "Row {row} holds far more text than a lead record should. Check it for pasted content, "
    "then upload again."
)
XLSX_PROTECTED = (
    "This file is password-protected or is an old .xls file. Remove the password, or open it "
    "in Excel and save it as .xlsx, then upload it again."
)
XLSX_UNREADABLE = (
    "We couldn't open this .xlsx file. Open it in Excel, save it again as .xlsx, then upload it."
)
CSV_IS_EXCEL = (
    "This file is named .csv but is really an Excel file. Rename it to .xlsx, or save it from "
    "Excel as CSV, then upload it again."
)
EXCEL_ERROR_VALUE = "Cell {cell} contained the Excel error {value}; it was left blank."
BLANK_HEADER = "Column {letter} has data but no header; it was named 'Column {letter}'."
DUPLICATE_HEADER = "The header '{header}' appears more than once; the repeat is named '{renamed}'."
UPLOAD_NOT_RECEIVED = "We didn't receive your file. Try uploading it again."
JOB_STATE_CONFLICT = "This upload has already moved on. Refresh the page to see where it is."
JOB_NOT_FOUND = "We couldn't find that upload."
PARSE_SYSTEM_ERROR = (
    "Something went wrong reading this file. Try uploading it again; if it keeps happening, "
    "contact Marketing Tech Ops."
)

# --- Column mapping (SPEC §6.2, §10) ---
MAPPING_AI_UNAVAILABLE = (
    "Automatic suggestions weren't available for this file, so some columns aren't mapped "
    "yet. Choose a field for each column you want to keep."
)
MAPPING_NOT_READY = "This file is still being read. Wait a moment and refresh."
MAPPING_LOCKED = (
    "The mapping can't be changed once enrichment or sending has started. To use a different "
    "mapping, upload the file again."
)

# --- Analysis issues (SPEC §9) ---
REQUIRED_MISSING = "{field} is blank. Fill it in, or exclude this row."
REQUIRED_PENDING = (
    "{field} is blank. Enrichment may fill it; if not, fill it in or exclude the row."
)
EMAIL_INVALID = "'{value}' isn't a valid email address. Fix it, or exclude this row."
EMAIL_ROLE_BASED = (
    "'{value}' looks like a shared inbox, not a person. Check it's the right contact."
)
EMAIL_PUBLIC_DOMAIN = (
    "'{value}' is a personal email address. A work address is better if you have it."
)
NAME_CHANGED = "{field} had numbers or symbols removed: '{before}' became '{after}'."
PHONE_INVALID = "'{value}' isn't a valid phone number, so it was left blank. Fix it if you can."
COUNTRY_UNRECOGNIZED = "'{value}' isn't a country we recognize. Check the spelling."
CAMPAIGN_ID_FORMAT = (
    "Campaign ID '{id}' isn't a Salesforce campaign ID. Campaign IDs are 15 or 18 characters "
    "and start with 701; copy it from the campaign's URL in Salesforce."
)
CAMPAIGN_INACTIVE = "{campaign} is marked inactive in Salesforce. Check it's the right campaign."
CAMPAIGN_NAME_MISMATCH = (
    "The file says '{supplied}', but Salesforce calls this campaign '{actual}'. "
    "The Salesforce name will be used."
)
LEAD_SOURCE_INVALID = "'{value}' isn't a lead source we recognize. Choose one from the list."
LEAD_SOURCE_CAMPAIGN_MISMATCH = (
    "Lead source '{value}' doesn't match this campaign's type, '{campaign_type}'. Check it's right."
)
FIELD_FORMAT_INVALID = "{field} value '{value}' isn't in a format we can use, so it was left blank."
VALUE_SUSPECT = "{field} '{value}' looks like a placeholder or test value. {explanation}"
VALUE_JUNK = (
    "{field} '{value}' looks like junk, not real lead data. Fix it, exclude the row, or clear "
    "this flag if it's genuine."
)
ZIP_LEADING_ZERO_RESTORED = (
    "ZIP code '{before}' was padded to '{after}' (Excel drops leading zeros)."
)
NOT_SENT_FIELD = (
    "When you send in step 4, these fields stay in your processed file but aren't sent to "
    "Eloqua (Post to Eloqua doesn't accept them yet): {fields}. No action needed."
)
AUTO_LIST_NAME = "SFDC List Name was blank, so it was set to '{value}'."
ANALYSIS_AI_UNAVAILABLE = (
    "Automatic junk checks weren't available for some rows. Rule-based checks still ran; "
    "look over names and companies before you send."
)
ANALYSIS_NOT_READY = "This upload hasn't been analyzed yet."
ANALYSIS_NEEDS_MAPPING = "Confirm the column mapping before starting analysis."
ROW_NOT_EDITABLE = "Rows can only be changed while you're reviewing the analysis."
STATUS_DEFAULTED_ROW = "Status was blank, so this campaign's default, '{default}', will be used."
LEAD_SOURCE_AUTO_CORRECTED_ROW = "Lead source '{from_}' was changed to '{to}'."
DUPLICATE_ROW = (
    "{email} appears {n} times for the same campaign (first on row {first}). Only the first "
    "row will be sent unless you choose otherwise."
)
ANALYSIS_FAILED = (
    "Something went wrong while analyzing this file. Your mapping is saved; try running the "
    "analysis again. If it keeps happening, contact Marketing Tech Ops."
)

# --- Enrichment (SPEC §15) ---
ENRICHMENT_REVIEW = (
    "ZoomInfo found a possible match that needs your decision. Apply it or skip it on the "
    "Enrichment screen."
)
LINKEDIN_MULTIPLE_PROFILES = (
    "ZoomInfo has more than one LinkedIn profile for this person. Check the one added is right."
)
ENRICHMENT_BATCH_ERRORS = (
    "{n} contacts couldn't be enriched (service error). They'll go ahead with the details "
    "you uploaded if they pass the checks."
)
ENRICHMENT_NOT_ENABLED = "Enrichment wasn't turned on for this upload."
ENRICHMENT_FAILED = (
    "Something went wrong while enriching this file. Your fixes are saved; try enriching "
    "again. If it keeps happening, contact Marketing Tech Ops."
)
ENRICHMENT_NOT_READY = "This upload hasn't been enriched yet."

# --- Gate and send (SPEC §16, §17) ---
GATE_BLOCKING = "{n} {rows} still {have} an issue to fix: {what}."
GATE_ENRICHMENT_UNDECIDED = (
    "{n} enrichment {matches} {are} waiting for Apply or Skip on the Enrichment screen."
)
GATE_MISSING_FIELD = "{n} {rows} {are} missing {field}."
GATE_STALE_CAMPAIGNS = (
    "Campaigns were last checked in Salesforce more than 24 hours ago. Re-check them before "
    "you send."
)
GATE_NOT_ANALYZED = "This file hasn't been analyzed yet."
GATE_ENRICHMENT_NOT_RUN = "Enrichment is turned on for this upload but hasn't run yet."
GATE_NO_ROWS = "There are no rows to send. Every row is excluded."
SEND_SUMMARY_CHANGED = (
    "The rows changed since you reviewed them. Check the summary again, then send."
)
SEND_GATE_FAILED = "This upload can't be sent yet. Fix the items listed, then try again."
SEND_NO_FAILED_ROWS = "There are no failed rows to retry."
SEND_UNCONFIRMED = (
    "Workato didn't answer for this row, so it may or may not have reached Eloqua. It won't be "
    "retried automatically; ask Marketing Tech Ops to check."
)
SEND_FAILED = (
    "Something went wrong while sending. Rows already submitted stay submitted; ask Marketing "
    "Tech Ops before retrying."
)
RESULT = "{n} leads submitted to Eloqua for {campaigns} on {datetime}."

# --- History, admin, audit (SPEC §6.7, §6.8, §21.2.6) ---
ROW_NOT_FOUND = "We couldn't find that row."
CONFIG_CHANGED = (
    "Someone else changed this setting while you were editing. Reload the page to see the "
    "latest version, then make your change again."
)
LEAD_SOURCE_BLANK = "Enter a lead source value."
LEAD_SOURCE_TOO_LONG = "Lead source values can be at most 255 characters."
LEAD_SOURCE_DUPLICATE = "'{value}' is already in the list."
LEAD_SOURCE_NOT_FOUND = "We couldn't find that lead source. Reload the page and try again."
LEAD_SOURCE_ORDER_MISMATCH = "The new order must list every lead source exactly once."
THRESHOLD_UNKNOWN = "'{key}' isn't a setting you can change here."
THRESHOLD_RANGE = "{key} must be a number between 0 and 1."
THRESHOLD_JUNK_ORDER = (
    "The junk block threshold must be at least the junk flag threshold, or flagged rows "
    "could be blocked without ever being flagged."
)
ALIAS_FIELD_UNKNOWN = "'{field}' isn't a field in the catalog."
ALIAS_BLANK = "Aliases can't be blank."
ALIAS_TAKEN = "'{alias}' already maps to {field}. Remove it there first."
ALIAS_IS_LABEL = "'{alias}' is already the name of the field {field}; it doesn't need an alias."
PROMOTE_NOT_AI = "Only a column the AI matched, and the user kept, can be saved as an alias."
AUDIT_SEARCH_EMPTY = "Enter at least one search term: email, job, user, campaign, event, or date."
AUDIT_DATE_INVALID = "Dates must look like 2026-09-28."

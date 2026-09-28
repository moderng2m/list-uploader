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

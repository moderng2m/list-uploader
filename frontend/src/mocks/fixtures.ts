// Synthetic data only. Never put real lead data here (see CLAUDE.md).
import type {
  AdminConfig,
  CatalogField,
  Analysis,
  Enrichment,
  Job,
  Issue,
  Mapping,
  Me,
  ParseSummary,
  Row,
} from "../api/types";

export const DEMO_JOB_ID = "j_01JDEMO0000000000000000000";
const CAMPAIGN_A = "701000000000001AAA";
const CAMPAIGN_B = "701000000000002AAA";

export const me: Me = { email: "demo.uploader@example.com", is_admin: true };

const summary = {
  rows_total: 8,
  rows_ready: 1,
  rows_warning: 1,
  rows_blocked: 5,
  rows_excluded: 0,
  rows_pending_enrichment: 1,
};

export const demoParse: ParseSummary = {
  file_type: "xlsx",
  sheet_name: "Sheet1",
  encoding: null,
  delimiter: null,
  headers: ["Company", "First name", "Last Name", "E-mail", "Job Position", "SFDC Last Campaign ID", "Badge Color"],
  row_count: 6,
  column_count: 7,
  warnings: [
    {
      code: "EXCEL_ERROR_VALUE",
      severity: "warning",
      message: "Cell E2 contained the Excel error #VALUE!; it was left blank.",
      row_id: 2,
      column: "Job Position",
    },
  ],
  row_issue_count: 0,
};

export const jobs: Job[] = [
  {
    job_id: DEMO_JOB_ID,
    filename: "demo_event_list.xlsx",
    owner_email: me.email,
    state: "ANALYSIS_REVIEW",
    enrich: true,
    created_at: "2026-09-28T15:04:00Z",
    campaigns: ["Demo Conference 2026", "Demo Webinar"],
    summary,
    parse: demoParse,
  },
  {
    job_id: "j_01JDEMO0000000000000000001",
    filename: "fake_booth_scans.csv",
    owner_email: "other.user@example.com",
    state: "COMPLETED",
    enrich: false,
    created_at: "2026-09-21T18:30:00Z",
    campaigns: ["Demo Conference 2026"],
    summary: { ...summary, rows_total: 40, rows_ready: 40, rows_blocked: 0, rows_warning: 0 },
  },
  {
    job_id: "j_01JDEMO0000000000000000002",
    filename: "empty_export.csv",
    owner_email: me.email,
    state: "PARSE_FAILED",
    enrich: true,
    created_at: "2026-09-20T13:10:00Z",
    parse_error: {
      code: "NO_DATA_ROWS",
      message:
        "This file has a header row but no data rows. Add your leads under the headers, then upload it again.",
    },
  },
];

// Mirrors backend/shared/catalog.py.
export const catalog: CatalogField[] = [
  { key: "company", label: "Company", description: "Employer / organization name", required: true, must_map: true, fill_when_unmapped: null },
  { key: "first_name", label: "First name", description: "Person's given name", required: true, must_map: true, fill_when_unmapped: null },
  { key: "last_name", label: "Last Name", description: "Person's family name / surname", required: true, must_map: true, fill_when_unmapped: null },
  { key: "email", label: "Email Address", description: "Person's business email address", required: true, must_map: true, fill_when_unmapped: null },
  { key: "lead_source", label: "Lead Source - Most Recent", description: "Marketing lead source picklist value", required: true, must_map: false, fill_when_unmapped: "derived from the Salesforce campaign type" },
  { key: "campaign_id", label: "SFDC Last Campaign ID", description: "Salesforce campaign ID", required: true, must_map: true, fill_when_unmapped: null },
  { key: "title", label: "Title", description: "Job title", required: false, must_map: false, fill_when_unmapped: null },
  { key: "phone", label: "Business Phone", description: "Business / work / direct phone number", required: false, must_map: false, fill_when_unmapped: null },
  { key: "mobile_phone", label: "Mobile Phone", description: "Mobile / cell phone number", required: false, must_map: false, fill_when_unmapped: null },
  { key: "address_line_1", label: "Address 1", description: "Street address", required: false, must_map: false, fill_when_unmapped: null },
  { key: "city", label: "City", description: "City", required: false, must_map: false, fill_when_unmapped: null },
  { key: "state_province", label: "State or Province", description: "State, province, or region", required: false, must_map: false, fill_when_unmapped: null },
  { key: "postal_code", label: "Zip or Postal Code", description: "ZIP or postal code", required: false, must_map: false, fill_when_unmapped: null },
  { key: "country", label: "Country", description: "Country name or code", required: false, must_map: false, fill_when_unmapped: null },
  { key: "notes", label: "Notes additional Information", description: "Free-text notes about the lead", required: false, must_map: false, fill_when_unmapped: null },
  { key: "zi_contact_id", label: "Zoom Individual ID", description: "ZoomInfo person/contact ID (digits)", required: false, must_map: false, fill_when_unmapped: null },
  { key: "zi_company_id", label: "Zoom Company ID", description: "ZoomInfo company ID (digits)", required: false, must_map: false, fill_when_unmapped: null },
  { key: "campaign_status", label: "SFDC Last Campaign Status", description: "Campaign member status", required: true, must_map: false, fill_when_unmapped: "blank statuses use the campaign's default status" },
  { key: "naics_code", label: "NAICS Code", description: "Industry classification code, 2-6 digits", required: false, must_map: false, fill_when_unmapped: null },
  { key: "industry", label: "Industry", description: "Industry name", required: false, must_map: false, fill_when_unmapped: null },
  { key: "website", label: "Website", description: "Company website URL or domain", required: false, must_map: false, fill_when_unmapped: null },
  { key: "employee_count", label: "Number of Employees", description: "Company employee count or range", required: false, must_map: false, fill_when_unmapped: null },
  { key: "list_name", label: "SFDC List Name", description: "Name of the Salesforce list", required: true, must_map: false, fill_when_unmapped: "generated from campaign name, date, and uploader" },
  { key: "last_response_class", label: "Last Response Class", description: "Marketing response classification", required: false, must_map: false, fill_when_unmapped: null },
  { key: "campaign_name", label: "SFDC Last Campaign Name", description: "Salesforce campaign name", required: true, must_map: false, fill_when_unmapped: "taken from Salesforce" },
  { key: "linkedin_url", label: "LinkedIn", description: "Person's LinkedIn profile URL", required: false, must_map: false, fill_when_unmapped: null },
];

export const mapping: Mapping = {
  columns: [
    { source_header: "Company", samples: ["Acme Demo Co", "Globex Test Inc", "Initech Sample"], field_key: "company", method: "exact", confidence: null },
    { source_header: "First name", samples: ["Ada", "Grace", "Alan"], field_key: "first_name", method: "exact", confidence: null },
    { source_header: "Last Name", samples: ["Example", "Sample", "Placeholder"], field_key: "last_name", method: "exact", confidence: null },
    { source_header: "E-mail", samples: ["ada@example.com", "grace@example.org", "alan@example.net"], field_key: "email", method: "alias", confidence: null },
    { source_header: "Job Position", samples: ["VP Marketing", "Director, Ops", "CFO"], field_key: "title", method: "ai", confidence: 0.87, reason: "Header and values look like job titles." },
    { source_header: "SFDC Last Campaign ID", samples: [CAMPAIGN_A, CAMPAIGN_B, CAMPAIGN_A], field_key: "campaign_id", method: "exact", confidence: null },
    { source_header: "Badge Color", samples: ["blue", "red", "blue"], field_key: null, method: "none", confidence: null },
  ],
  catalog,
  confirmed: false,
  confirmed_at: null,
  editable: true,
  ai_note: null,
};

const EVENT_STATUSES = ["Registered", "Attended", "No Show"];
const LIST = (c: string) => `${c} \u2013 2026-09-28 \u2013 ${me.email}`;

function issue(code: string, severity: Issue["severity"], field: string | null, message: string, extra: Partial<Issue> = {}): Issue {
  return { code, severity, field, message, source: "rule", ...extra };
}

// Mirrors what the backend returns for backend/tests/fixtures/synthetic/analysis_demo.csv.
export const analysisRows: Row[] = [
  {
    row_id: 2, status: "ready", excluded: false,
    source: { company: "Acme Demo Co", first_name: "Ada", last_name: "Example", email: "ada@acme.example", campaign_id: "701000000000001", campaign_status: "", lead_source: "Events" },
    processed: { company: "Acme Demo Co", first_name: "Ada", last_name: "Example", email: "ada@acme.example", campaign_id: CAMPAIGN_A, campaign_status: "Registered", lead_source: "Marketing: Events", campaign_name: "Demo Conference 2026", list_name: LIST("Demo Conference 2026") },
    provenance: { campaign_id: "normalized", campaign_status: "derived:sfdc_default_status", lead_source: "auto_corrected:rule:marketing_prefix" },
    issues: [
      issue("LEAD_SOURCE_AUTO_CORRECTED", "info", "lead_source", "Lead source 'Events' was changed to 'Marketing: Events'."),
      issue("STATUS_DEFAULTED", "info", "campaign_status", "Status was blank, so this campaign's default, 'Registered', will be used.", { source: "sfdc" }),
    ],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 3, status: "blocked", excluded: false,
    source: { company: "Globex Test Inc", first_name: "Grace", last_name: "Sample", email: "grace@globex.example", campaign_id: "701000000000001AAB", campaign_status: "Attended", lead_source: "Marketing: Events" },
    processed: { company: "Globex Test Inc", first_name: "Grace", last_name: "Sample", email: "grace@globex.example", campaign_id: "701000000000001AAB", campaign_status: "Attended", lead_source: "Marketing: Events" },
    provenance: {},
    issues: [issue("CAMPAIGN_ID_FORMAT", "blocking", "campaign_id", "Campaign ID 701000000000001AAB looks mistyped — its last three characters don't match. IDs are case-sensitive; copy it directly from Salesforce.")],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 4, status: "blocked", excluded: false,
    source: { company: "Initech Sample", first_name: "Alan", last_name: "Placeholder", email: "alan@initech.example", campaign_id: "701000000000009AAA", campaign_status: "Registered", lead_source: "Marketing: Events" },
    processed: { company: "Initech Sample", first_name: "Alan", last_name: "Placeholder", email: "alan@initech.example", campaign_id: "701000000000009AAA", campaign_status: "Registered", lead_source: "Marketing: Events" },
    provenance: {},
    issues: [issue("CAMPAIGN_NOT_FOUND", "blocking", "campaign_id", "Campaign ID 701000000000009AAA wasn't found in Salesforce. Open the campaign in Salesforce, copy the 18-character ID from the URL, and paste it here.", { source: "sfdc" })],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 5, status: "blocked", excluded: false,
    source: { company: "Acme Demo Co", first_name: "Ada", last_name: "Example", email: "ADA@acme.example", campaign_id: CAMPAIGN_A, campaign_status: "Attended", lead_source: "Marketing: Events" },
    processed: { company: "Acme Demo Co", first_name: "Ada", last_name: "Example", email: "ada@acme.example", campaign_id: CAMPAIGN_A, campaign_status: "Attended", lead_source: "Marketing: Events", campaign_name: "Demo Conference 2026" },
    provenance: { email: "normalized" },
    issues: [issue("DUPLICATE_IN_FILE", "blocking", "email", "ada@acme.example appears 2 times for the same campaign (first on row 2). Only the first row will be sent unless you choose otherwise.", { suggestion: { first_row_id: 2 } })],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 6, status: "blocked", excluded: false,
    source: { company: "asdf", first_name: "Test", last_name: "Test", email: "test@umbrella.example", campaign_id: CAMPAIGN_A, campaign_status: "Attended", lead_source: "Marketing: Events" },
    processed: { company: "asdf", first_name: "Test", last_name: "Test", email: "test@umbrella.example", campaign_id: CAMPAIGN_A, campaign_status: "Attended", lead_source: "Marketing: Events", campaign_name: "Demo Conference 2026" },
    provenance: {},
    issues: [
      issue("VALUE_JUNK", "blocking", "company", "Company 'asdf' looks like junk, not real lead data. Fix it, exclude the row, or clear this flag if it's genuine.", { source: "ai", suggestion: { reason: "keyboard_mash", confidence: 0.95 } }),
      issue("VALUE_SUSPECT", "warning", "first_name", "First name 'Test' looks like a placeholder or test value. "),
    ],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 7, status: "blocked", excluded: false,
    source: { company: "Hooli Example", first_name: "Linus", last_name: "Sample", email: "linus@hooli.example", campaign_id: CAMPAIGN_B, campaign_status: "Atended", lead_source: "Webcast" },
    processed: { company: "Hooli Example", first_name: "Linus", last_name: "Sample", email: "linus@hooli.example", campaign_id: CAMPAIGN_B, campaign_status: "Atended", lead_source: "Webcast", campaign_name: "Demo Webinar Series" },
    provenance: {},
    issues: [
      issue("LEAD_SOURCE_SUGGESTED", "blocking", "lead_source", "We think 'Webcast' means 'Marketing: Webinar' (80% sure). Accept or pick another.", { source: "ai", suggestion: { value: "Marketing: Webinar", confidence: 0.8 } }),
      issue("STATUS_INVALID", "blocking", "campaign_status", "'Atended' isn't a member status on Demo Webinar Series. Valid statuses: Registered, Attended, Watched On Demand.", { source: "sfdc", suggestion: { options: ["Attended", "Registered", "Watched On Demand"] } }),
    ],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 8, status: "pending_enrichment", excluded: false,
    source: { company: "", first_name: "Kay", last_name: "Sample", email: "kay@pied.example", campaign_id: CAMPAIGN_A, campaign_status: "Registered", lead_source: "Marketing: Events" },
    processed: { first_name: "Kay", last_name: "Sample", email: "kay@pied.example", campaign_id: CAMPAIGN_A, campaign_status: "Registered", lead_source: "Marketing: Events", campaign_name: "Demo Conference 2026" },
    provenance: {},
    issues: [issue("REQUIRED_MISSING", "blocking", "company", "Company is blank. Enrichment may fill it; if not, fill it in or exclude the row.", { pending: true })],
    user_edits: [], dismissed: [],
  },
  {
    row_id: 9, status: "warning", excluded: false,
    source: { company: "Vandelay Demo", first_name: "Art", last_name: "Sample", email: "info@vandelay.example", campaign_id: "701000000000003AAA", campaign_status: "Attended", lead_source: "Marketing: Events", phone: "12345" },
    processed: { company: "Vandelay Demo", first_name: "Art", last_name: "Sample", email: "info@vandelay.example", campaign_id: "701000000000003AAA", campaign_status: "Attended", lead_source: "Marketing: Events", campaign_name: "Demo Roadshow 2025" },
    provenance: {},
    issues: [
      issue("CAMPAIGN_INACTIVE", "warning", "campaign_id", "Demo Roadshow 2025 is marked inactive in Salesforce. Check it's the right campaign.", { source: "sfdc" }),
      issue("EMAIL_ROLE_BASED", "warning", "email", "'info@vandelay.example' looks like a shared inbox, not a person. Check it's the right contact."),
      issue("PHONE_INVALID", "warning", "phone", "'12345' isn't a valid phone number, so it was left blank. Fix it if you can."),
    ],
    user_edits: [], dismissed: [],
  },
];

export const analysis: Analysis = {
  state: "ANALYSIS_REVIEW",
  editable: true,
  enrich: true,
  summary,
  enrichment_lookup_count: 3,
  notes: [],
  normalizer_version: "stand-in-0.1 (NOT lead normalizer v5)",
  lead_sources: ["Marketing: Events", "Marketing: Webinar", "Marketing: Content Syndication", "Marketing: Paid Social", "Marketing: Website", "Sales: Outbound"],
  issue_groups: [
    { code: "CAMPAIGN_ID_FORMAT", severity: "blocking", count: 1, explanation: "The campaign ID is mistyped or isn't a campaign ID.", bulk_action: null, bulk_action_label: null },
    { code: "CAMPAIGN_NOT_FOUND", severity: "blocking", count: 1, explanation: "The campaign ID wasn't found in Salesforce.", bulk_action: null, bulk_action_label: null },
    { code: "DUPLICATE_IN_FILE", severity: "blocking", count: 1, explanation: "Same email and campaign as an earlier row.", bulk_action: "exclude_duplicates", bulk_action_label: "Exclude duplicate rows" },
    { code: "VALUE_JUNK", severity: "blocking", count: 1, explanation: "A value looks like junk, not real lead data.", bulk_action: "exclude_junk", bulk_action_label: "Exclude all rows flagged as junk" },
    { code: "LEAD_SOURCE_SUGGESTED", severity: "blocking", count: 1, explanation: "We suggested a lead source; accept it or pick another.", bulk_action: "accept_lead_source_suggestions", bulk_action_label: "Accept all suggested lead sources at 90% or higher" },
    { code: "STATUS_INVALID", severity: "blocking", count: 1, explanation: "The status isn't one of the campaign's member statuses.", bulk_action: "set_status", bulk_action_label: "Apply a status to all rows with this value" },
    { code: "REQUIRED_MISSING", severity: "blocking", count: 1, explanation: "A required field is blank.", bulk_action: null, bulk_action_label: null },
    { code: "CAMPAIGN_INACTIVE", severity: "warning", count: 1, explanation: "The campaign is inactive in Salesforce.", bulk_action: null, bulk_action_label: null },
    { code: "STATUS_DEFAULTED", severity: "info", count: 1, explanation: "Blank statuses will use the campaign's default.", bulk_action: null, bulk_action_label: null },
  ],
  campaigns: [
    { id: CAMPAIGN_A, found: true, name: "Demo Conference 2026", type: "Marketing: Events", is_active: true, statuses: EVENT_STATUSES, default_status: "Registered", row_count: 4 },
    { id: CAMPAIGN_B, found: true, name: "Demo Webinar Series", type: "Marketing: Webinar", is_active: true, statuses: ["Registered", "Attended", "Watched On Demand"], default_status: "Registered", row_count: 1 },
    { id: "701000000000003AAA", found: true, name: "Demo Roadshow 2025", type: "Marketing: Events", is_active: false, statuses: EVENT_STATUSES, default_status: "Registered", row_count: 1 },
    { id: "701000000000009AAA", found: false, name: null, type: null, is_active: null, statuses: [], default_status: null, row_count: 1 },
  ],
};

// Mirrors the backend result for backend/tests/fixtures/synthetic/enrichment_demo.csv.
export const enrichment: Enrichment = {
  state: "ENRICHMENT_REVIEW",
  editable: true,
  sent: 6,
  accepted: 2,
  needs_review: 1,
  no_match: 3,
  errors: 0,
  linkedin_found: 2,
  fields_filled: { Company: 1, Title: 2, LinkedIn: 2, "Mobile Phone": 1, City: 1 },
  review: [
    {
      row_id: 4,
      source: { name: "Linus Sample", company: "Hooli Example", title: "Engineer", email: "linus@hooli.example" },
      candidate: { name: "Linus Sample", company: "Hooli XYZ Demo", title: "Head of Platform", email: "linus.sample@hooli.example" },
      match_score: 71,
      conflicts: ["ZoomInfo shows a different current employer.", "ZoomInfo shows a different job title."],
      would_fill: ["LinkedIn"],
      decision: null,
    },
  ],
  filled: [
    { row_id: 2, field: "Title", before: "", after: "VP Marketing" },
    { row_id: 2, field: "LinkedIn", before: "", after: "https://www.linkedin.com/in/ada-example-demo" },
    { row_id: 2, field: "Mobile Phone", before: "", after: "+15550100151" },
    { row_id: 3, field: "Company", before: "", after: "Pied Piper Demo" },
    { row_id: 3, field: "Title", before: "", after: "CTO" },
    { row_id: 3, field: "LinkedIn", before: "", after: "linkedin.com/in/kay-sample-demo" },
    { row_id: 3, field: "City", before: "", after: "Palo Alto" },
  ],
  notes: [],
};

export const adminConfig: AdminConfig = {
  version: "seed",
  thresholds: {
    mapping_suggest_threshold: 0.75,
    lead_source_auto_threshold: 0.9,
    junk_flag_threshold: 0.7,
    junk_block_threshold: 0.9,
  },
  limits: { max_file_bytes: 10485760, max_rows: 5000, send_max_concurrency: 5 },
};

// Synthetic data only. Never put real lead data here (see CLAUDE.md).
import type {
  AdminConfig,
  Analysis,
  Enrichment,
  GateResult,
  Job,
  LeadSource,
  Mapping,
  Me,
  SendResult,
} from "../api/types";

export const DEMO_JOB_ID = "j_01JDEMO0000000000000000000";
const CAMPAIGN_A = "701000000000001AAA";
const CAMPAIGN_B = "701000000000002AAA";

export const me: Me = { email: "demo.uploader@example.com", is_admin: true };

const summary = {
  rows_total: 6,
  rows_ready: 2,
  rows_warning: 1,
  rows_blocked: 2,
  rows_excluded: 0,
  rows_pending_enrichment: 1,
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
];

export const mapping: Mapping = {
  columns: [
    { source_header: "Company", samples: ["Acme Demo Co", "Globex Test Inc", "Initech Sample"], field_key: "company", method: "exact", confidence: null },
    { source_header: "First name", samples: ["Ada", "Grace", "Alan"], field_key: "first_name", method: "exact", confidence: null },
    { source_header: "Last Name", samples: ["Example", "Sample", "Placeholder"], field_key: "last_name", method: "exact", confidence: null },
    { source_header: "E-mail", samples: ["ada@example.com", "grace@example.org", "alan@example.net"], field_key: "email", method: "alias", confidence: null },
    { source_header: "Job Position", samples: ["VP Marketing", "Director, Ops", "CFO"], field_key: "title", method: "ai", confidence: 0.87 },
    { source_header: "SFDC Last Campaign ID", samples: [CAMPAIGN_A, CAMPAIGN_B, CAMPAIGN_A], field_key: "campaign_id", method: "exact", confidence: null },
    { source_header: "Badge Color", samples: ["blue", "red", "blue"], field_key: null, method: "none", confidence: null },
  ],
  catalog: [
    { key: "company", label: "Company", required: true },
    { key: "first_name", label: "First name", required: true },
    { key: "last_name", label: "Last Name", required: true },
    { key: "email", label: "Email Address", required: true },
    { key: "lead_source", label: "Lead Source - Most Recent", required: false },
    { key: "campaign_id", label: "SFDC Last Campaign ID", required: true },
    { key: "campaign_status", label: "SFDC Last Campaign Status", required: false },
    { key: "list_name", label: "SFDC List Name", required: true },
    { key: "title", label: "Title", required: false },
    { key: "phone", label: "Business Phone", required: false },
    { key: "campaign_name", label: "SFDC Last Campaign Name", required: false },
  ],
};

export const analysis: Analysis = {
  summary,
  enrichment_lookup_count: 5,
  issue_groups: [
    { code: "CAMPAIGN_ID_FORMAT", severity: "blocking", count: 1, explanation: "A campaign ID looks mistyped.", bulk_action: null },
    { code: "DUPLICATE_IN_FILE", severity: "blocking", count: 1, explanation: "Same email and campaign as an earlier row.", bulk_action: "Exclude duplicate rows" },
    { code: "STATUS_DEFAULTED", severity: "info", count: 3, explanation: "Blank statuses will use the campaign default.", bulk_action: null },
    { code: "VALUE_SUSPECT", severity: "warning", count: 1, explanation: "A value looks like a placeholder.", bulk_action: "Exclude all rows flagged as junk" },
  ],
  rows: [
    { row_id: 2, status: "ready", source: { Company: "acme demo co", "E-mail": "ADA@EXAMPLE.COM" }, processed: { company: "Acme Demo Co", email: "ada@example.com" }, issues: [] },
    { row_id: 3, status: "blocked", source: { Company: "Globex Test", "E-mail": "grace@example.org" }, processed: { company: "Globex Test", email: "grace@example.org" }, issues: [{ field: "campaign_id", severity: "blocking", code: "CAMPAIGN_ID_FORMAT", message: "Campaign ID 701000000000002AAB looks mistyped." }] },
    { row_id: 4, status: "warning", source: { Company: "asdf", "E-mail": "alan@example.net" }, processed: { company: "asdf", email: "alan@example.net" }, issues: [{ field: "company", severity: "warning", code: "VALUE_SUSPECT", message: "'asdf' looks like a placeholder." }] },
    { row_id: 5, status: "blocked", source: { Company: "Acme Demo Co", "E-mail": "ada@example.com" }, processed: { company: "Acme Demo Co", email: "ada@example.com" }, issues: [{ field: "email", severity: "blocking", code: "DUPLICATE_IN_FILE", message: "ada@example.com appears 2 times for the same campaign." }] },
    { row_id: 6, status: "pending_enrichment", source: { Company: "", "E-mail": "kay@example.com" }, processed: { company: "", email: "kay@example.com" }, issues: [{ field: "company", severity: "blocking", code: "REQUIRED_MISSING", message: "Company is blank; enrichment may fill it." }] },
    { row_id: 7, status: "ready", source: { Company: "Initech Sample", "E-mail": "linus@example.com" }, processed: { company: "Initech Sample", email: "linus@example.com" }, issues: [] },
  ],
  campaigns: [
    { id: CAMPAIGN_A, found: true, name: "Demo Conference 2026", type: "Marketing: Events", is_active: true, member_statuses: ["Registered", "Attended", "No Show"], row_count: 4 },
    { id: "701000000000002AAB", found: false, name: null, type: null, is_active: null, member_statuses: [], row_count: 1 },
  ],
};

export const enrichment: Enrichment = {
  sent: 5,
  accepted: 3,
  needs_review: 1,
  no_match: 1,
  errors: 0,
  linkedin_found: 3,
  fields_filled: { company: 1, title: 2, linkedin_url: 3 },
  review: [
    {
      row_id: 4,
      source: { name: "Alan Placeholder", company: "asdf", title: "CFO" },
      candidate: { name: "Alan Placeholder", company: "Initech Sample", title: "Chief Financial Officer" },
      match_score: 74,
      conflicts: ["ZoomInfo shows a different current employer"],
    },
  ],
};

export const gate: GateResult = {
  passed: false,
  reasons: ["1 row has a campaign ID that wasn't found in Salesforce.", "1 enrichment match is waiting for Apply or Skip."],
  by_campaign: [
    { campaign_id: CAMPAIGN_A, campaign_name: "Demo Conference 2026", status: "Attended", rows: 3 },
    { campaign_id: CAMPAIGN_A, campaign_name: "Demo Conference 2026", status: "Registered", rows: 1 },
  ],
  not_sent_fields: ["Mobile Phone", "NAICS Code", "Notes additional Information"],
};

export const result: SendResult = {
  submitted: 3,
  failed: [{ row_id: 7, email: "linus@example.com", reason: "HTTP 500 from Workato (mock)" }],
  campaigns: [{ name: "Demo Conference 2026", rows: 3 }],
  submitted_at: "2026-09-28T19:05:00Z",
  submitted_by: me.email,
};

export const leadSources: LeadSource[] = [
  { value: "Marketing: Events", active: true },
  { value: "Marketing: Webinar", active: true },
  { value: "Marketing: Content Syndication", active: true },
  { value: "Marketing: Trade Show (retired)", active: false },
];

export const adminConfig: AdminConfig = {
  thresholds: {
    mapping_suggest_threshold: 0.75,
    lead_source_auto_threshold: 0.9,
    junk_flag_threshold: 0.7,
    junk_block_threshold: 0.9,
  },
  limits: { max_file_bytes: 10485760, max_rows: 5000, send_max_concurrency: 5 },
};

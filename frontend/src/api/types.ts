// Shapes returned by the BFF (SPEC §19). Only what the screens need so far.

export type JobState =
  | "AWAITING_UPLOAD"
  | "UPLOADED"
  | "PARSE_FAILED"
  | "MAPPING_REVIEW"
  | "ANALYZING"
  | "ANALYSIS_REVIEW"
  | "ENRICHING"
  | "ENRICHMENT_REVIEW"
  | "READY_TO_SEND"
  | "SENDING"
  | "COMPLETED"
  | "COMPLETED_WITH_ERRORS"
  | "CANCELLED"
  | "EXPIRED"
  | "FAILED";

export type RowStatus = "ready" | "warning" | "blocked" | "excluded" | "pending_enrichment";
export type Severity = "blocking" | "warning" | "info";
export type MatchMethod = "exact" | "alias" | "ai" | "manual" | "none";

export interface Me {
  email: string;
  is_admin: boolean;
}

export interface JobSummary {
  rows_total: number;
  rows_ready: number;
  rows_warning: number;
  rows_blocked: number;
  rows_excluded: number;
  rows_pending_enrichment: number;
}

export interface ParseWarning {
  code: string;
  severity: Severity;
  message: string;
  row_id?: number;
  column?: string;
}

export interface ParseSummary {
  file_type: "csv" | "xlsx";
  sheet_name: string | null;
  encoding: string | null;
  delimiter: string | null;
  headers: string[];
  row_count: number;
  column_count: number;
  warnings: ParseWarning[];
  row_issue_count: number;
}

export interface Job {
  job_id: string;
  filename: string;
  owner_email: string;
  state: JobState;
  enrich: boolean;
  created_at: string;
  updated_at?: string;
  file?: { size?: number; sha256?: string; version_id?: string };
  parse?: ParseSummary;
  parse_error?: { code: string; message: string };
  last_error?: { stage: string; message: string };
  // Filled in by later phases.
  campaigns?: string[];
  summary?: JobSummary;
}

export interface CreatedJob {
  job: Job;
  upload: { url: string; fields: Record<string, string> };
  max_bytes: number;
}

export interface CatalogField {
  key: string;
  label: string;
  description: string;
  required: boolean;
  /** Required and has no automatic fill: the file must have a column for it. */
  must_map: boolean;
  /** For required fields that may stay unmapped: how they get filled. */
  fill_when_unmapped: string | null;
}

export interface MappingColumn {
  source_header: string;
  samples: string[];
  field_key: string | null;
  method: MatchMethod;
  confidence: number | null;
  reason?: string | null;
}

export interface Mapping {
  columns: MappingColumn[];
  catalog: CatalogField[];
  confirmed: boolean;
  confirmed_at: string | null;
  editable: boolean;
  ai_note: string | null;
}

export interface Issue {
  code: string;
  severity: Severity;
  message: string;
  field: string | null;
  source: "rule" | "ai" | "sfdc" | "enrichment" | "parse";
  suggestion?: {
    value?: string;
    confidence?: number | null;
    options?: string[];
    first_row_id?: number;
    reason?: string;
  };
  pending?: boolean;
}

export type BulkAction = "accept_lead_source_suggestions" | "exclude_duplicates" | "exclude_junk" | "set_status";

export interface IssueGroup {
  code: string;
  severity: Severity;
  count: number;
  explanation: string;
  bulk_action: BulkAction | null;
  bulk_action_label: string | null;
}

export interface Row {
  row_id: number;
  status: RowStatus;
  excluded: boolean;
  /** Raw values from the file, keyed by catalog field. */
  source: Record<string, string>;
  processed: Record<string, string>;
  provenance: Record<string, string>;
  issues: Issue[];
  user_edits: { field: string; from: string; to: string; by: string; at: string }[];
  dismissed: string[];
  send?: { status: SendStatus; attempts?: number; error?: string };
}

export interface RowsPage {
  total: number;
  offset: number;
  rows: Row[];
}

export interface CampaignCard {
  id: string;
  found: boolean;
  name: string | null;
  type: string | null;
  is_active: boolean | null;
  statuses: string[];
  default_status: string | null;
  row_count: number;
}

export interface Analysis {
  state: JobState;
  editable: boolean;
  enrich: boolean;
  summary: JobSummary;
  issue_groups: IssueGroup[];
  campaigns: CampaignCard[];
  lead_sources: string[];
  enrichment_lookup_count: number;
  notes: string[];
  normalizer_version: string | null;
}

export interface RowChange {
  processed?: Record<string, string>;
  excluded?: boolean;
  dismiss?: string;
  restore?: string;
}

export type EnrichmentDecision = "apply" | "skip";

export interface EnrichmentReviewItem {
  row_id: number;
  source: { name: string; company: string; title: string; email: string };
  candidate: { name?: string; company?: string; title?: string; email?: string };
  match_score: number | null;
  conflicts: string[];
  would_fill: string[];
  decision: EnrichmentDecision | null;
}

export interface Enrichment {
  state: JobState;
  editable: boolean;
  sent: number;
  accepted: number;
  needs_review: number;
  no_match: number;
  errors: number;
  linkedin_found: number;
  /** Field label -> rows filled. */
  fields_filled: Record<string, number>;
  review: EnrichmentReviewItem[];
  filled: { row_id: number; field: string; before: string; after: string }[];
  notes: string[];
}

export interface CampaignGroup {
  campaign_id: string;
  campaign_name: string;
  status: string;
  rows: number;
}

/** What the user confirms; the server rejects the send if it no longer matches. */
export interface SendConfirmation {
  rows_to_send: number;
  by_campaign: { campaign_id: string; status: string; rows: number }[];
}

export interface GateResult {
  passed: boolean;
  reasons: string[];
  reason_codes: string[];
  by_campaign: CampaignGroup[];
  rows_to_send: number;
  campaign_count: number;
  excluded: number;
  /** Field labels with values that won't go to Eloqua (OQ-1). */
  not_sent_fields: string[];
  sent_fields: string[];
  campaigns_stale: boolean;
  state: JobState;
  can_send: boolean;
  send_to_prod: boolean;
}

export type SendStatus = "not_sent" | "sending" | "submitted" | "failed";

export interface RowProblem {
  row_id: number;
  email: string;
  reason: string;
}

export interface SendResult {
  state: JobState;
  submitted: number;
  failed: RowProblem[];
  /** Posted but no answer came back: may or may not have reached Eloqua. */
  unconfirmed: RowProblem[];
  campaigns: { id: string; name: string; rows: number }[];
  submitted_at: string | null;
  submitted_by: string | null;
  send_to_prod: boolean;
  last_error: { stage: string; message: string } | null;
}

export interface LeadSourceItem {
  id: string;
  value: string;
  active: boolean;
  order: number;
}

/** Admin settings carry the version the admin saw; a stale one gets 409. */
export interface LeadSources {
  version: string;
  items: LeadSourceItem[];
}

export interface AdminConfig {
  version: string;
  thresholds: Record<string, number>;
  limits: Record<string, number>;
}

export interface FieldAliases {
  key: string;
  label: string;
  aliases: string[];
}

export interface Aliases {
  version: string;
  fields: FieldAliases[];
}

export interface AiMapping {
  source_header: string;
  field_key: string;
  field_label: string;
  times: number;
  job_ids: string[];
  last_at: string;
}

export interface AuditEventView {
  event_id: string;
  event_type: string;
  occurred_at: string;
  job_id: string | null;
  row_id: number | null;
  actor: { type: "user" | "system" | "admin"; email: string | null };
  /** Plain English, no values. */
  summary: string;
  reason: string | null;
  subject: Record<string, unknown> | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  details: Record<string, unknown> | null;
}

export interface Timeline {
  events: AuditEventView[];
  next_cursor: string | null;
}

export interface LineageStep {
  kind: string;
  value: string;
  label: string;
  at?: string;
  event_id?: string;
  column?: string;
}

export interface FieldLineage {
  field: string;
  label: string;
  current: string;
  provenance: string | null;
  provenance_text: string;
  steps: LineageStep[];
  explained: boolean;
}

export interface RowHistory {
  row_id: number;
  /** False once the row has expired; the audit events remain. */
  row_available: boolean;
  status: RowStatus | null;
  excluded: boolean;
  normalizer_version: string;
  fields: FieldLineage[];
  enrichment: Record<string, unknown> | null;
  send: { status: SendStatus; attempts?: number; http_status?: number; error?: string; submitted_at?: string } | null;
  events: AuditEventView[];
}

export interface AuditFilters {
  email?: string;
  job_id?: string;
  user?: string;
  campaign_id?: string;
  event_type?: string;
  from?: string;
  to?: string;
}

export interface AuditSearchResult {
  events: AuditEventView[];
  truncated: boolean;
  /** For an email search: every upload that included the person. */
  jobs: { job_id: string; filename: string | null; owner_email: string | null; state: JobState | null; created_at: string | null }[];
}


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

export interface EnrichmentReviewItem {
  row_id: number;
  source: Record<string, string>;
  candidate: Record<string, string>;
  match_score: number;
  conflicts: string[];
}

export interface Enrichment {
  sent: number;
  accepted: number;
  needs_review: number;
  no_match: number;
  errors: number;
  linkedin_found: number;
  fields_filled: Record<string, number>;
  review: EnrichmentReviewItem[];
}

export interface GateResult {
  passed: boolean;
  reasons: string[];
  by_campaign: { campaign_id: string; campaign_name: string; status: string; rows: number }[];
  not_sent_fields: string[];
}

export interface SendResult {
  submitted: number;
  failed: { row_id: number; email: string; reason: string }[];
  campaigns: { name: string; rows: number }[];
  submitted_at: string;
  submitted_by: string;
}

export interface LeadSource {
  value: string;
  active: boolean;
}

export interface AdminConfig {
  thresholds: Record<string, number>;
  limits: Record<string, number>;
}

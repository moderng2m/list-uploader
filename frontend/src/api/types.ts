// Shapes returned by the BFF (SPEC §19). Only what the screens need so far.

export type JobState =
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
export type MatchMethod = "exact" | "alias" | "ai" | "none";

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

export interface Job {
  job_id: string;
  filename: string;
  owner_email: string;
  state: JobState;
  enrich: boolean;
  created_at: string;
  campaigns: string[];
  summary: JobSummary;
}

export interface CatalogField {
  key: string;
  label: string;
  required: boolean;
}

export interface MappingColumn {
  source_header: string;
  samples: string[];
  field_key: string | null;
  method: MatchMethod;
  confidence: number | null;
}

export interface Mapping {
  columns: MappingColumn[];
  catalog: CatalogField[];
}

export interface Issue {
  field: string;
  severity: Severity;
  code: string;
  message: string;
}

export interface IssueGroup {
  code: string;
  severity: Severity;
  count: number;
  explanation: string;
  bulk_action: string | null;
}

export interface Row {
  row_id: number;
  status: RowStatus;
  source: Record<string, string>;
  processed: Record<string, string>;
  issues: Issue[];
}

export interface CampaignCard {
  id: string;
  found: boolean;
  name: string | null;
  type: string | null;
  is_active: boolean | null;
  member_statuses: string[];
  row_count: number;
}

export interface Analysis {
  summary: JobSummary;
  issue_groups: IssueGroup[];
  rows: Row[];
  campaigns: CampaignCard[];
  enrichment_lookup_count: number;
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

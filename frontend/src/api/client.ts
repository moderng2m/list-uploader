import type {
  AdminConfig,
  AiMapping,
  Aliases,
  Analysis,
  AuditFilters,
  AuditSearchResult,
  BulkAction,
  EnrichmentDecision,
  Row,
  RowChange,
  RowsPage,
  CreatedJob,
  Enrichment,
  GateResult,
  Job,
  LeadSources,
  Mapping,
  Me,
  RowHistory,
  SendConfirmation,
  SendResult,
  Timeline,
} from "./types";

// In mock mode (default) MSW answers these requests in the browser.
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { message?: string };
    throw new ApiError(res.status, body.message ?? `Request failed (${res.status})`);
  }
  return (await res.json()) as T;
}

export const api = {
  me: () => request<Me>("/me"),
  listJobs: (all = false) => request<Job[]>(all ? "/jobs?all=true" : "/jobs"),
  getJob: (id: string) => request<Job>(`/jobs/${id}`),
  createJob: (filename: string, enrich: boolean) =>
    request<CreatedJob>("/jobs", {
      method: "POST",
      body: JSON.stringify({ filename, enrich }),
    }),
  markUploaded: (id: string) => request<Job>(`/jobs/${id}/uploaded`, { method: "POST" }),
  getMapping: (id: string) => request<Mapping>(`/jobs/${id}/mapping`),
  confirmMapping: (id: string, columns: { source_header: string; field_key: string | null }[]) =>
    request<Mapping>(`/jobs/${id}/mapping`, { method: "PUT", body: JSON.stringify({ columns }) }),
  startAnalysis: (id: string) => request<{ job_id: string; state: string }>(`/jobs/${id}/analyze`, { method: "POST" }),
  getAnalysis: (id: string) => request<Analysis>(`/jobs/${id}/analysis`),
  getRows: (id: string, filters: Record<string, string | number | undefined> = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) if (v !== undefined && v !== "") q.set(k, String(v));
    return request<RowsPage>(`/jobs/${id}/rows?${q.toString()}`);
  },
  editRow: (id: string, rowId: number, change: RowChange) =>
    request<{ row: Row; also_changed: number[] }>(`/jobs/${id}/rows/${rowId}`, {
      method: "PATCH",
      body: JSON.stringify(change),
    }),
  bulkAction: (id: string, action: BulkAction, params: Record<string, unknown> = {}) =>
    request<{ action: string; affected_row_ids: number[] }>(`/jobs/${id}/bulk-actions`, {
      method: "POST",
      body: JSON.stringify({ action, params }),
    }),
  startEnrichment: (id: string) => request<{ job_id: string; state: string }>(`/jobs/${id}/enrich`, { method: "POST" }),
  getEnrichment: (id: string) => request<Enrichment>(`/jobs/${id}/enrichment`),
  decideEnrichment: (
    id: string,
    body: { decisions: { row_id: number; decision: EnrichmentDecision }[] } | { skip_all: true },
  ) =>
    request<{ decided_row_ids: number[] }>(`/jobs/${id}/enrichment-decisions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getGate: (id: string) => request<GateResult>(`/jobs/${id}/gate`),
  revalidateCampaigns: (id: string) =>
    request<{ campaigns: number; rows_changed: number[] }>(`/jobs/${id}/revalidate-campaigns`, { method: "POST" }),
  send: (id: string, confirmation: SendConfirmation, uiGatePassed: boolean) =>
    request<{ job_id: string; state: string }>(`/jobs/${id}/send`, {
      method: "POST",
      body: JSON.stringify({ confirmation, ui_gate_passed: uiGatePassed }),
    }),
  retryFailed: (id: string) =>
    request<{ job_id: string; state: string; row_ids: number[] }>(`/jobs/${id}/retry-failed`, { method: "POST" }),
  getResult: (id: string) => request<SendResult>(`/jobs/${id}/result`),
  download: (id: string) => request<{ url: string; filename: string; expires_in: number }>(`/jobs/${id}/download`),
  getTimeline: (id: string, opts: { rows?: boolean; cursor?: string | null } = {}) => {
    const q = new URLSearchParams();
    if (opts.rows) q.set("rows", "true");
    if (opts.cursor) q.set("cursor", opts.cursor);
    return request<Timeline>(`/jobs/${id}/timeline?${q.toString()}`);
  },
  getRowHistory: (id: string, rowId: number) => request<RowHistory>(`/jobs/${id}/rows/${rowId}/history`),

  // Admin (SPEC §6.8). Every change sends the version it was based on.
  adminConfig: () => request<AdminConfig>("/admin/config"),
  updateThresholds: (version: string, values: Record<string, number>) =>
    request<AdminConfig>("/admin/thresholds", { method: "PUT", body: JSON.stringify({ version, values }) }),
  leadSources: () => request<LeadSources>("/admin/lead-sources"),
  addLeadSource: (version: string, value: string) =>
    request<LeadSources>("/admin/lead-sources", { method: "POST", body: JSON.stringify({ version, value }) }),
  changeLeadSource: (version: string, id: string, change: { value?: string; active?: boolean }) =>
    request<LeadSources>(`/admin/lead-sources/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ version, ...change }),
    }),
  reorderLeadSources: (version: string, ids: string[]) =>
    request<LeadSources>("/admin/lead-sources/order", { method: "PUT", body: JSON.stringify({ version, ids }) }),
  aliases: () => request<Aliases>("/admin/aliases"),
  replaceAliases: (version: string, fieldKey: string, aliases: string[]) =>
    request<Aliases>(`/admin/aliases/${fieldKey}`, { method: "PUT", body: JSON.stringify({ version, aliases }) }),
  aiMappings: () => request<{ items: AiMapping[] }>("/admin/ai-mappings"),
  promoteAlias: (version: string, sourceHeader: string, fieldKey: string) =>
    request<Aliases>("/admin/aliases/promote", {
      method: "POST",
      body: JSON.stringify({ version, source_header: sourceHeader, field_key: fieldKey }),
    }),
  auditSearch: (filters: AuditFilters) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) if (v) q.set(k, v);
    return request<AuditSearchResult>(`/admin/audit?${q.toString()}`);
  },
  auditExport: (filters: AuditFilters) =>
    request<{ url: string; filename: string; events: number; truncated: boolean }>("/admin/audit/export", {
      method: "POST",
      body: JSON.stringify(filters),
    }),
};

/** Upload straight to S3 with the presigned POST from createJob. */
export async function uploadFile(upload: CreatedJob["upload"], file: File): Promise<void> {
  const form = new FormData();
  for (const [k, v] of Object.entries(upload.fields)) form.append(k, v);
  form.append("file", file); // must be the last field
  const res = await fetch(upload.url, { method: "POST", body: form });
  if (!res.ok) {
    throw new ApiError(
      res.status,
      res.status === 400
        ? "The file was rejected by storage. Check it's under 10 MB and try again."
        : "The upload didn't finish. Check your connection and try again.",
    );
  }
}

const DONE_STATES = new Set(["MAPPING_REVIEW", "PARSE_FAILED", "FAILED", "CANCELLED"]);

/** Poll until parsing finishes. */
export async function waitForParse(
  id: string,
  { intervalMs = 1000, timeoutMs = 120_000 } = {},
): Promise<Job> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const job = await api.getJob(id);
    if (DONE_STATES.has(job.state)) return job;
    if (Date.now() > deadline)
      throw new Error("Reading your file is taking longer than expected. Check History in a minute.");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

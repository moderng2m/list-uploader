import type {
  AdminConfig,
  Analysis,
  BulkAction,
  EnrichmentDecision,
  Row,
  RowChange,
  RowsPage,
  CreatedJob,
  Enrichment,
  GateResult,
  Job,
  LeadSource,
  Mapping,
  Me,
  SendResult,
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
  listJobs: () => request<Job[]>("/jobs"),
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
  getResult: (id: string) => request<SendResult>(`/jobs/${id}/result`),
  leadSources: () => request<LeadSource[]>("/admin/lead-sources"),
  adminConfig: () => request<AdminConfig>("/admin/config"),
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

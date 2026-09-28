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
    request<{ job_id: string; upload_url: string }>("/jobs", {
      method: "POST",
      body: JSON.stringify({ filename, enrich }),
    }),
  getMapping: (id: string) => request<Mapping>(`/jobs/${id}/mapping`),
  getAnalysis: (id: string) => request<Analysis>(`/jobs/${id}/analysis`),
  getEnrichment: (id: string) => request<Enrichment>(`/jobs/${id}/enrichment`),
  getGate: (id: string) => request<GateResult>(`/jobs/${id}/gate`),
  getResult: (id: string) => request<SendResult>(`/jobs/${id}/result`),
  leadSources: () => request<LeadSource[]>("/admin/lead-sources"),
  adminConfig: () => request<AdminConfig>("/admin/config"),
};

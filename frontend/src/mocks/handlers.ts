import { http, HttpResponse } from "msw";
import { API_BASE } from "../api/client";
import type { BulkAction, Job, RowChange } from "../api/types";
import { mockAnalysis, mockBulk, mockEdit, mockRows, resetAnalysisMock } from "./analysisMock";
import * as f from "./fixtures";

const u = (path: string) => `${API_BASE}${path}`;
export const MOCK_UPLOAD_URL = "https://uploads.mock.invalid/";

// Jobs created during this session. A filename containing "broken" fails parsing,
// so the error path can be demoed.
const created = new Map<string, Job>();
let counter = 0;

// Jobs whose analysis has been started in this session (done on the next poll).
const analyzing = new Set<string>();

export function resetMockJobs() {
  created.clear();
  analyzing.clear();
  counter = 0;
  resetAnalysisMock();
}

function findJob(id: string): Job | undefined {
  return created.get(id) ?? f.jobs.find((j) => j.job_id === id);
}

/** Simulate async work (parse, analysis) finishing the first time the job is polled. */
function advance(job: Job): Job {
  if (job.state === "ANALYZING") {
    const done: Job = { ...job, state: "ANALYSIS_REVIEW", summary: mockAnalysis().summary };
    if (created.has(job.job_id)) created.set(job.job_id, done);
    analyzing.delete(job.job_id);
    return done;
  }
  if (job.state !== "UPLOADED") return job;
  const next: Job = job.filename.toLowerCase().includes("broken")
    ? {
        ...job,
        state: "PARSE_FAILED",
        parse_error: {
          code: "NO_DATA_ROWS",
          message:
            "This file has a header row but no data rows. Add your leads under the headers, then upload it again.",
        },
      }
    : { ...job, state: "MAPPING_REVIEW", parse: f.demoParse };
  created.set(job.job_id, next);
  return next;
}

export const handlers = [
  http.get(u("/me"), () => HttpResponse.json(f.me)),
  http.get(u("/jobs"), () => HttpResponse.json([...created.values()].reverse().concat(f.jobs))),
  http.post(u("/jobs"), async ({ request }) => {
    const body = (await request.json()) as { filename: string; enrich: boolean };
    counter += 1;
    const job: Job = {
      job_id: `j_MOCK${String(counter).padStart(22, "0")}`,
      filename: body.filename,
      owner_email: f.me.email,
      state: "AWAITING_UPLOAD",
      enrich: body.enrich,
      created_at: new Date().toISOString(),
    };
    created.set(job.job_id, job);
    return HttpResponse.json(
      {
        job,
        upload: { url: MOCK_UPLOAD_URL, fields: { key: `${job.job_id}/source` } },
        max_bytes: 10 * 1024 * 1024,
      },
      { status: 201 },
    );
  }),
  http.post(MOCK_UPLOAD_URL, () => new HttpResponse(null, { status: 204 })),
  http.post(u("/jobs/:id/uploaded"), ({ params }) => {
    const job = created.get(String(params.id));
    if (!job) return HttpResponse.json({ message: "We couldn't find that upload." }, { status: 404 });
    const next: Job = { ...job, state: "UPLOADED" };
    created.set(job.job_id, next);
    return HttpResponse.json(next, { status: 202 });
  }),
  http.get(u("/jobs/:id"), ({ params }) => {
    const id = String(params.id);
    const found = findJob(id);
    const job = found && analyzing.has(id) ? { ...found, state: "ANALYZING" as const } : found;
    return job
      ? HttpResponse.json(advance(job))
      : HttpResponse.json({ message: "We couldn't find that upload." }, { status: 404 });
  }),
  http.get(u("/jobs/:id/mapping"), () => HttpResponse.json(f.mapping)),
  http.put(u("/jobs/:id/mapping"), async ({ request }) => {
    const body = (await request.json()) as { columns: { source_header: string; field_key: string | null }[] };
    const used = new Set(body.columns.map((c) => c.field_key).filter(Boolean));
    const missing = f.catalog.filter((c) => c.must_map && !used.has(c.key)).map((c) => c.label);
    if (missing.length)
      return HttpResponse.json(
        { message: `Map these required fields before you continue: ${missing.join(", ")}.` },
        { status: 400 },
      );
    return HttpResponse.json({ ...f.mapping, confirmed: true, confirmed_at: new Date().toISOString() });
  }),
  http.post(u("/jobs/:id/analyze"), ({ params }) => {
    const id = String(params.id);
    analyzing.add(id);
    const job = created.get(id);
    if (job) created.set(id, { ...job, state: "ANALYZING" });
    return HttpResponse.json({ job_id: id, state: "ANALYZING" }, { status: 202 });
  }),
  http.get(u("/jobs/:id/analysis"), () => HttpResponse.json(mockAnalysis())),
  http.get(u("/jobs/:id/rows"), ({ request }) => HttpResponse.json(mockRows(new URL(request.url).searchParams))),
  http.patch(u("/jobs/:id/rows/:rowId"), async ({ params, request }) => {
    const result = mockEdit(Number(params.rowId), (await request.json()) as RowChange);
    return result ? HttpResponse.json(result) : HttpResponse.json({ message: "Row not found." }, { status: 404 });
  }),
  http.post(u("/jobs/:id/bulk-actions"), async ({ request }) => {
    const body = (await request.json()) as { action: BulkAction; params: Record<string, unknown> };
    return HttpResponse.json(mockBulk(body.action, body.params ?? {}));
  }),
  http.get(u("/jobs/:id/enrichment"), () => HttpResponse.json(f.enrichment)),
  http.get(u("/jobs/:id/gate"), () => HttpResponse.json(f.gate)),
  http.get(u("/jobs/:id/result"), () => HttpResponse.json(f.result)),
  http.get(u("/admin/lead-sources"), () => HttpResponse.json(f.leadSources)),
  http.get(u("/admin/config"), () => HttpResponse.json(f.adminConfig)),
];

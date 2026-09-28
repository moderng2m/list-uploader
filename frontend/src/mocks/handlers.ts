import { http, HttpResponse } from "msw";
import { API_BASE } from "../api/client";
import type { BulkAction, EnrichmentDecision, Job, JobState, RowChange, SendConfirmation } from "../api/types";
import { mockAnalysis, mockBulk, mockEdit, mockRows, resetAnalysisMock } from "./analysisMock";
import { mockDecide, mockEnrichment, resetEnrichmentMock } from "./enrichmentMock";
import * as f from "./fixtures";
import { finishSend, mockDownload, mockGate, mockResult, mockRetry, mockSend, resetSendMock } from "./sendMock";

const u = (path: string) => `${API_BASE}${path}`;
export const MOCK_UPLOAD_URL = "https://uploads.mock.invalid/";

// Jobs created during this session. A filename containing "broken" fails parsing,
// so the error path can be demoed.
const created = new Map<string, Job>();
let counter = 0;

// Jobs with a workflow running in this session (it finishes on the next poll).
const running = new Map<string, JobState>();
const DONE: Partial<Record<JobState, () => JobState>> = {
  ANALYZING: () => "ANALYSIS_REVIEW",
  ENRICHING: () => "ENRICHMENT_REVIEW",
  SENDING: finishSend,
};

// State changes to the fixture jobs (sent, retried) made during this session.
const moved = new Map<string, JobState>();

export function resetMockJobs() {
  created.clear();
  running.clear();
  counter = 0;
  resetAnalysisMock();
  resetEnrichmentMock();
  resetSendMock();
  moved.clear();
}

function findJob(id: string): Job | undefined {
  const job = created.get(id) ?? f.jobs.find((j) => j.job_id === id);
  if (!job) return undefined;
  const state = running.get(id) ?? moved.get(id);
  return state ? { ...job, state } : job;
}

/** Simulate async work (parse, analysis) finishing the first time the job is polled. */
function advance(job: Job): Job {
  const finish = DONE[job.state];
  if (finish && running.has(job.job_id)) {
    const done: Job = { ...job, state: finish(), summary: mockAnalysis().summary };
    if (created.has(job.job_id)) created.set(job.job_id, done);
    else moved.set(job.job_id, done.state);
    running.delete(job.job_id);
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

function withJob(id: unknown, respond: (job: Job) => Response) {
  const job = findJob(String(id));
  return job ? respond(job) : HttpResponse.json({ message: "We couldn't find that upload." }, { status: 404 });
}

function start(id: string, state: JobState) {
  running.set(id, state);
  const job = created.get(id);
  if (job) created.set(id, { ...job, state });
  return HttpResponse.json({ job_id: id, state }, { status: 202 });
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
    const job = findJob(id);
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
  http.post(u("/jobs/:id/analyze"), ({ params }) => start(String(params.id), "ANALYZING")),
  http.post(u("/jobs/:id/enrich"), ({ params }) => start(String(params.id), "ENRICHING")),
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
  http.get(u("/jobs/:id/enrichment"), () => HttpResponse.json(mockEnrichment())),
  http.post(u("/jobs/:id/enrichment-decisions"), async ({ request }) => {
    const body = (await request.json()) as {
      decisions?: { row_id: number; decision: EnrichmentDecision }[];
      skip_all?: boolean;
    };
    return HttpResponse.json(mockDecide(body));
  }),
  http.get(u("/jobs/:id/gate"), ({ params }) => withJob(params.id, (job) => HttpResponse.json(mockGate(job)))),
  http.post(u("/jobs/:id/revalidate-campaigns"), () =>
    HttpResponse.json({ campaigns: mockAnalysis().campaigns.length, rows_changed: [] }),
  ),
  http.post(u("/jobs/:id/send"), async ({ params, request }) => {
    const body = (await request.json()) as { confirmation: SendConfirmation };
    return withJob(params.id, (job) => {
      const error = mockSend(job, body.confirmation);
      return error ? HttpResponse.json({ message: error }, { status: 409 }) : start(job.job_id, "SENDING");
    });
  }),
  http.post(u("/jobs/:id/retry-failed"), ({ params }) =>
    withJob(params.id, (job) => {
      if (job.state !== "COMPLETED_WITH_ERRORS")
        return HttpResponse.json({ message: "There are no failed rows to retry." }, { status: 409 });
      const rowIds = mockRetry();
      if (!rowIds.length) return HttpResponse.json({ message: "There are no failed rows to retry." }, { status: 409 });
      start(job.job_id, "SENDING");
      return HttpResponse.json({ job_id: job.job_id, state: "SENDING", row_ids: rowIds }, { status: 202 });
    }),
  ),
  http.get(u("/jobs/:id/result"), ({ params }) => withJob(params.id, (job) => HttpResponse.json(mockResult(job)))),
  http.get(u("/jobs/:id/download"), ({ params }) => withJob(params.id, (job) => HttpResponse.json(mockDownload(job)))),
  http.get(u("/admin/lead-sources"), () => HttpResponse.json(f.leadSources)),
  http.get(u("/admin/config"), () => HttpResponse.json(f.adminConfig)),
];

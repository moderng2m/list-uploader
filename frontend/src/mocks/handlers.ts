import { http, HttpResponse } from "msw";
import { API_BASE } from "../api/client";
import type { BulkAction, EnrichmentDecision, Job, JobState, RowChange, SendConfirmation } from "../api/types";
import { mockAnalysis, mockBulk, mockEdit, mockRows, resetAnalysisMock } from "./analysisMock";
import { mockDecide, mockEnrichment, resetEnrichmentMock } from "./enrichmentMock";
import { Conflict, Invalid, mockAdmin, resetAdminMock } from "./adminMock";
import { mockAuditSearch, mockRowHistory, mockTimeline } from "./auditMock";
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
  resetAdminMock();
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

/** Run an admin change; its validation and version errors become 400/409. */
async function adminChange(request: Request, apply: (body: Record<string, any>) => unknown) {
  const body = (await request.json()) as Record<string, any>;
  try {
    return HttpResponse.json(apply(body) as object);
  } catch (e) {
    if (e instanceof Conflict || e instanceof Invalid)
      return HttpResponse.json({ message: e.message }, { status: e.status });
    throw e;
  }
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
  http.get(u("/jobs/:id/timeline"), ({ params, request }) =>
    withJob(params.id, (job) =>
      HttpResponse.json(mockTimeline(job.job_id, new URL(request.url).searchParams.get("rows") === "true")),
    ),
  ),
  http.get(u("/jobs/:id/rows/:rowId/history"), ({ params }) => {
    const history = mockRowHistory(Number(params.rowId));
    return history ? HttpResponse.json(history) : HttpResponse.json({ message: "We couldn't find that row." }, { status: 404 });
  }),
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
  http.get(u("/admin/config"), () => HttpResponse.json(mockAdmin.config())),
  http.put(u("/admin/thresholds"), ({ request }) => adminChange(request, (b) => mockAdmin.thresholds(b.version, b.values))),
  http.get(u("/admin/lead-sources"), () => HttpResponse.json(mockAdmin.sources())),
  http.post(u("/admin/lead-sources"), ({ request }) => adminChange(request, (b) => mockAdmin.addSource(b.version, b.value))),
  http.put(u("/admin/lead-sources/order"), ({ request }) =>
    adminChange(request, (b) => mockAdmin.reorderSources(b.version, b.ids)),
  ),
  http.patch(u("/admin/lead-sources/:id"), ({ params, request }) =>
    adminChange(request, (b) => mockAdmin.changeSource(b.version, String(params.id), b)),
  ),
  http.get(u("/admin/aliases"), () => HttpResponse.json(mockAdmin.aliases())),
  http.put(u("/admin/aliases/:field"), ({ params, request }) =>
    adminChange(request, (b) => mockAdmin.replaceAliases(b.version, String(params.field), b.aliases)),
  ),
  http.get(u("/admin/ai-mappings"), () => HttpResponse.json(mockAdmin.aiMappings())),
  http.post(u("/admin/aliases/promote"), ({ request }) =>
    adminChange(request, (b) => mockAdmin.promote(b.version, b.source_header, b.field_key)),
  ),
  http.get(u("/admin/audit"), ({ request }) => {
    const filters = Object.fromEntries(new URL(request.url).searchParams);
    if (!Object.values(filters).some(Boolean))
      return HttpResponse.json(
        { message: "Enter at least one search term: email, job, user, campaign, event, or date." },
        { status: 400 },
      );
    return HttpResponse.json(mockAuditSearch(filters));
  }),
  http.post(u("/admin/audit/export"), async ({ request }) => {
    const result = mockAuditSearch((await request.json()) as Record<string, string>);
    const lines = ["occurred_at,event_type,job_id,row_id,summary", ...result.events.map((e) =>
      [e.occurred_at, e.event_type, e.job_id ?? "", e.row_id ?? "", `"${e.summary.replace(/"/g, '""')}"`].join(","))];
    return HttpResponse.json({
      url: `data:text/csv;charset=utf-8,${encodeURIComponent(lines.join("\r\n"))}`,
      filename: "audit-demo.csv",
      events: result.events.length,
      truncated: false,
    });
  }),
];

import { http, HttpResponse } from "msw";
import { API_BASE } from "../api/client";
import * as f from "./fixtures";

const u = (path: string) => `${API_BASE}${path}`;

export const handlers = [
  http.get(u("/me"), () => HttpResponse.json(f.me)),
  http.get(u("/jobs"), () => HttpResponse.json(f.jobs)),
  http.post(u("/jobs"), () =>
    HttpResponse.json({ job_id: f.DEMO_JOB_ID, upload_url: "https://example.invalid/upload" }),
  ),
  http.get(u("/jobs/:id"), ({ params }) => {
    const job = f.jobs.find((j) => j.job_id === params.id);
    return job ? HttpResponse.json(job) : HttpResponse.json({ message: "Not found" }, { status: 404 });
  }),
  http.get(u("/jobs/:id/mapping"), () => HttpResponse.json(f.mapping)),
  http.get(u("/jobs/:id/analysis"), () => HttpResponse.json(f.analysis)),
  http.get(u("/jobs/:id/enrichment"), () => HttpResponse.json(f.enrichment)),
  http.get(u("/jobs/:id/gate"), () => HttpResponse.json(f.gate)),
  http.get(u("/jobs/:id/result"), () => HttpResponse.json(f.result)),
  http.get(u("/admin/lead-sources"), () => HttpResponse.json(f.leadSources)),
  http.get(u("/admin/config"), () => HttpResponse.json(f.adminConfig)),
];

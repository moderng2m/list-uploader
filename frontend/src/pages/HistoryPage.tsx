import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Job, JobState } from "../api/types";
import { Async, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

// Where "Open" lands for each state.
const RESUME: Partial<Record<JobState, string>> = {
  MAPPING_REVIEW: "mapping",
  ANALYSIS_REVIEW: "analysis",
  ENRICHMENT_REVIEW: "enrichment",
  READY_TO_SEND: "send",
  COMPLETED: "result",
  COMPLETED_WITH_ERRORS: "result",
};

function rowCount(j: Job): string {
  const n = j.summary?.rows_total ?? j.parse?.row_count;
  return n == null ? "—" : n.toLocaleString();
}

export function HistoryPage() {
  const state = useApi(api.listJobs, "jobs");
  return (
    <>
      <PageHeader title="Upload history" />
      <Async state={state}>
        {(jobs) => (
          <section className="card">
            <table className="grid">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th>Uploaded</th>
                  <th>Rows</th>
                  <th>Campaigns</th>
                  <th>Owner</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => {
                  const step = RESUME[j.state];
                  return (
                    <tr key={j.job_id}>
                      <td>{j.filename}</td>
                      <td>
                        {j.state.replaceAll("_", " ").toLowerCase()}
                        {j.parse_error && <div className="error small">{j.parse_error.message}</div>}
                      </td>
                      <td>{new Date(j.created_at).toLocaleString()}</td>
                      <td>{rowCount(j)}</td>
                      <td>{(j.campaigns ?? []).join(", ")}</td>
                      <td>{j.owner_email}</td>
                      <td>{step && <Link to={`/jobs/${j.job_id}/${step}`}>Open</Link>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        )}
      </Async>
    </>
  );
}

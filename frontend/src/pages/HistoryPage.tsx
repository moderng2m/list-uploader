import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { JobState } from "../api/types";
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
              {jobs.map((j) => (
                <tr key={j.job_id}>
                  <td>{j.filename}</td>
                  <td>{j.state.replaceAll("_", " ").toLowerCase()}</td>
                  <td>{new Date(j.created_at).toLocaleString()}</td>
                  <td>{j.summary.rows_total}</td>
                  <td>{j.campaigns.join(", ")}</td>
                  <td>{j.owner_email}</td>
                  <td>
                    <Link to={`/jobs/${j.job_id}/${RESUME[j.state] ?? "analysis"}`}>Open</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </section>
        )}
      </Async>
    </>
  );
}

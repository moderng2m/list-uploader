import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Job, JobState } from "../api/types";
import { Async, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

// Where "Open" lands for each state.
const RESUME: Partial<Record<JobState, string>> = {
  MAPPING_REVIEW: "mapping",
  ANALYZING: "analysis",
  ANALYSIS_REVIEW: "analysis",
  ENRICHING: "enrichment",
  ENRICHMENT_REVIEW: "enrichment",
  READY_TO_SEND: "send",
  SENDING: "result",
  COMPLETED: "result",
  COMPLETED_WITH_ERRORS: "result",
};

function rowCount(j: Job): string {
  const n = j.summary?.rows_total ?? j.parse?.row_count;
  return n == null ? "—" : n.toLocaleString();
}

export function HistoryPage() {
  const me = useApi(api.me, "me");
  const isAdmin = me.status === "ready" && me.data.is_admin;
  const [all, setAll] = useState(false);
  const state = useApi(() => api.listJobs(all), `jobs:${all}`);
  return (
    <>
      <PageHeader title="Upload history">{all ? "Every upload." : "Your uploads."}</PageHeader>
      {isAdmin && (
        <div className="tabs" role="tablist" aria-label="Whose uploads">
          <button type="button" role="tab" aria-selected={!all} onClick={() => setAll(false)}>
            Mine
          </button>
          <button type="button" role="tab" aria-selected={all} onClick={() => setAll(true)}>
            Everyone's
          </button>
        </div>
      )}
      <Async state={state}>
        {(jobs) => (
          <section className="card">
            {jobs.length === 0 ? (
              <p className="muted">No uploads yet.</p>
            ) : (
              <div className="table-wrap">
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
                          <td className="row-actions">
                            {step && <Link to={`/jobs/${j.job_id}/${step}`}>Open</Link>}{" "}
                            <Link to={`/jobs/${j.job_id}/timeline`} aria-label={`Timeline for ${j.filename}`}>
                              Timeline
                            </Link>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}
      </Async>
    </>
  );
}

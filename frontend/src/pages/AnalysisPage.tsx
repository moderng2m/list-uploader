import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Analysis } from "../api/types";
import { Async, PageHeader, SeverityBadge, StatusBadge, SummaryCards } from "../components/ui";
import { useApi } from "../components/useApi";

function AnalysisView({ jobId, a, enrich }: { jobId: string; a: Analysis; enrich: boolean }) {
  const [filter, setFilter] = useState<string | null>(null);
  const rows = filter ? a.rows.filter((r) => r.issues.some((i) => i.code === filter)) : a.rows;
  return (
    <>
      <SummaryCards summary={a.summary} />

      <section className="card">
        <h2>Issues</h2>
        <ul className="issue-groups">
          {a.issue_groups.map((g) => (
            <li key={g.code}>
              <SeverityBadge severity={g.severity} /> <code>{g.code}</code> · {g.count} rows —{" "}
              {g.explanation}
              <span className="issue-actions">
                <button type="button" onClick={() => setFilter(filter === g.code ? null : g.code)}>
                  {filter === g.code ? "Show all rows" : "Show rows"}
                </button>
                {g.bulk_action && (
                  <button type="button" disabled title="Available in P3">
                    {g.bulk_action}
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h2>Campaigns</h2>
        <div className="campaigns">
          {a.campaigns.map((c) => (
            <article key={c.id} className={`campaign ${c.found ? "" : "invalid"}`}>
              <h3>{c.found ? c.name : "Campaign not found"}</h3>
              <code>{c.id}</code>
              {c.found ? (
                <p>
                  {c.type} · {c.is_active ? "Active" : "Inactive"} · {c.row_count} rows
                  <br />
                  Statuses: {c.member_statuses.join(", ")}
                </p>
              ) : (
                <p className="error">
                  This ID wasn't found in Salesforce. Copy the 18-character ID from the campaign's URL.
                </p>
              )}
            </article>
          ))}
        </div>
      </section>

      <section className="card">
        <h2>Rows {filter && <small>(filtered: {filter})</small>}</h2>
        <table className="grid">
          <thead>
            <tr>
              <th>Row</th>
              <th>Status</th>
              <th>Company (source → processed)</th>
              <th>Email</th>
              <th>Issues</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.row_id}>
                <td>{r.row_id}</td>
                <td>
                  <StatusBadge status={r.status} />
                </td>
                <td>
                  <span className="muted">{r.source.Company || "(blank)"}</span> →{" "}
                  {r.processed.company || "(blank)"}
                </td>
                <td>{r.processed.email}</td>
                <td>{r.issues.map((i) => i.message).join("; ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="actions">
        {enrich && <p className="muted">Enrichment will look up {a.enrichment_lookup_count} contacts.</p>}
        <Link className="button primary" to={`/jobs/${jobId}/${enrich ? "enrichment" : "send"}`}>
          {enrich ? "Next: Enrich" : "Next: Review & Send"}
        </Link>
      </div>
    </>
  );
}

export function AnalysisPage() {
  const { jobId = "" } = useParams();
  const analysis = useApi(() => api.getAnalysis(jobId), `analysis:${jobId}`);
  const job = useApi(() => api.getJob(jobId), `job:${jobId}`);
  const enrich = job.status === "ready" ? job.data.enrich : true;
  return (
    <>
      <PageHeader title="Analyze and fix">Fix blocking issues before you can send.</PageHeader>
      <Async state={analysis}>{(a) => <AnalysisView jobId={jobId} a={a} enrich={enrich} />}</Async>
    </>
  );
}

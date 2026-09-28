import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Enrichment } from "../api/types";
import { Async, PageHeader, Stat } from "../components/ui";
import { useApi } from "../components/useApi";

type Decision = "apply" | "skip";

function EnrichmentView({ jobId, e }: { jobId: string; e: Enrichment }) {
  // Default is Skip (SPEC §6.4).
  const [decisions, setDecisions] = useState<Record<number, Decision>>({});
  const decide = (rowId: number, d: Decision) => setDecisions((prev) => ({ ...prev, [rowId]: d }));
  return (
    <>
      <section className="stats" aria-label="Enrichment summary">
        <Stat label="Sent to ZoomInfo" value={e.sent} />
        <Stat label="Accepted" value={e.accepted} tone="ok" />
        <Stat label="Needs review" value={e.needs_review} tone="warn" />
        <Stat label="No match" value={e.no_match} />
        <Stat label="LinkedIn URLs found" value={e.linkedin_found} tone="info" />
      </section>
      {e.errors > 0 && (
        <p className="error">{e.errors} contacts couldn't be enriched (service error).</p>
      )}

      <section className="card">
        <h2>Needs review</h2>
        {e.review.length === 0 && <p className="muted">Nothing to review.</p>}
        {e.review.map((item) => (
          <article key={item.row_id} className="review-item">
            <h3>
              Row {item.row_id} · match score {item.match_score}
            </h3>
            <table className="grid compact">
              <thead>
                <tr>
                  <th />
                  <th>Your file</th>
                  <th>ZoomInfo</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(item.source).map((k) => (
                  <tr key={k}>
                    <th>{k}</th>
                    <td>{item.source[k]}</td>
                    <td>{item.candidate[k]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <ul>
              {item.conflicts.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
            <div className="segmented" role="radiogroup" aria-label={`Decision for row ${item.row_id}`}>
              {(["apply", "skip"] as const).map((d) => (
                <label key={d}>
                  <input
                    type="radio"
                    name={`decision-${item.row_id}`}
                    checked={(decisions[item.row_id] ?? "skip") === d}
                    onChange={() => decide(item.row_id, d)}
                  />
                  {d === "apply" ? "Apply" : "Skip"}
                </label>
              ))}
            </div>
          </article>
        ))}
      </section>

      <section className="card">
        <h2>Fields filled</h2>
        <ul>
          {Object.entries(e.fields_filled).map(([field, n]) => (
            <li key={field}>
              {field}: {n}
            </li>
          ))}
        </ul>
      </section>

      <div className="actions">
        <Link className="button primary" to={`/jobs/${jobId}/send`}>
          Next: Review & Send
        </Link>
      </div>
    </>
  );
}

export function EnrichmentPage() {
  const { jobId = "" } = useParams();
  const state = useApi(() => api.getEnrichment(jobId), `enrichment:${jobId}`);
  return (
    <>
      <PageHeader title="Enrichment results">
        ZoomInfo only fills blank fields. It never changes an email address.
      </PageHeader>
      <Async state={state}>{(e) => <EnrichmentView jobId={jobId} e={e} />}</Async>
    </>
  );
}

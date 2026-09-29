import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Enrichment, EnrichmentDecision, EnrichmentReviewItem } from "../api/types";
import { Async, PageHeader, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { useJobPoll } from "../components/useJobPoll";

const COMPARE: { key: "name" | "company" | "title" | "email"; label: string }[] = [
  { key: "name", label: "Name" },
  { key: "company", label: "Company" },
  { key: "title", label: "Title" },
  { key: "email", label: "Email" },
];

export function EnrichmentPage({ pollIntervalMs = 1500 }: { pollIntervalMs?: number }) {
  const { jobId = "" } = useParams();
  const { job, error, reload } = useJobPoll(jobId, pollIntervalMs);
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    try {
      await api.startEnrichment(jobId);
      reload();
    } finally {
      setBusy(false);
    }
  }

  let body;
  if (error) body = <p role="alert" className="error">{error}</p>;
  else if (!job) body = <p className="muted">Loading…</p>;
  else if (job.state === "ENRICHING")
    body = (
      <section className="card" role="status">
        <p className="big">Looking up contacts in ZoomInfo…</p>
        <p className="muted">This takes a few seconds per 25 contacts.</p>
      </section>
    );
  else if (job.state === "FAILED" && job.last_error?.stage === "enrichment")
    body = (
      <section className="card blocked">
        <p role="alert" className="error">{job.last_error.message}</p>
        <button type="button" className="primary" onClick={start} disabled={busy}>
          Try enriching again
        </button>
      </section>
    );
  else if (job.state === "ANALYSIS_REVIEW")
    body = (
      <section className="card">
        <p>{job.enrich ? "Enrichment hasn't run yet." : "Enrichment wasn't turned on for this upload."}</p>
        {job.enrich ? (
          <button type="button" className="primary" onClick={start} disabled={busy}>
            Enrich now
          </button>
        ) : (
          <Link className="button primary" to={`/jobs/${jobId}/send`}>
            Next: Review & Send
          </Link>
        )}
      </section>
    );
  else body = <EnrichmentReview jobId={jobId} />;

  return (
    <>
      <PageHeader title="Enrichment results">
        ZoomInfo only fills blank fields. It never changes an email address.
      </PageHeader>
      {body}
    </>
  );
}

function EnrichmentReview({ jobId }: { jobId: string }) {
  const [version, setVersion] = useState(0);
  const state = useApi(() => api.getEnrichment(jobId), `enrichment:${jobId}:${version}`);
  return <Async state={state}>{(e) => <EnrichmentView jobId={jobId} e={e} onSaved={() => setVersion((v) => v + 1)} />}</Async>;
}

function EnrichmentView({ jobId, e, onSaved }: { jobId: string; e: Enrichment; onSaved: () => void }) {
  // Default is Skip (SPEC §6.4).
  const initial = () => Object.fromEntries(e.review.map((r) => [r.row_id, r.decision ?? "skip"])) as Record<number, EnrichmentDecision>;
  const [choices, setChoices] = useState<Record<number, EnrichmentDecision>>(initial);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => setChoices(initial()), [e]);

  const undecided = e.review.filter((r) => !r.decision).length;

  async function save(body: Parameters<typeof api.decideEnrichment>[1]) {
    setSaving(true);
    setError(null);
    try {
      await api.decideEnrichment(jobId, body);
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

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
        <p className="card note" role="note">
          {e.errors} contacts couldn't be enriched (service error). They'll go ahead with the details you uploaded if
          they pass the checks.
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      <section className="card" aria-label="Needs review">
        <h2>Needs review {undecided > 0 && <small className="muted">({undecided} waiting)</small>}</h2>
        {e.review.length === 0 ? (
          <p className="muted">Nothing to review.</p>
        ) : (
          <>
            {e.review.map((item) => (
              <ReviewItem
                key={item.row_id}
                item={item}
                choice={choices[item.row_id] ?? "skip"}
                disabled={!e.editable || saving}
                onChoose={(d) => setChoices({ ...choices, [item.row_id]: d })}
              />
            ))}
            {e.editable && (
              <div className="actions">
                <button type="button" onClick={() => save({ skip_all: true })} disabled={saving || undecided === 0}>
                  Skip all
                </button>
                <button
                  type="button"
                  className="primary"
                  disabled={saving}
                  onClick={() =>
                    save({ decisions: e.review.map((r) => ({ row_id: r.row_id, decision: choices[r.row_id] ?? "skip" })) })
                  }
                >
                  {saving ? "Saving…" : "Save decisions"}
                </button>
              </div>
            )}
          </>
        )}
      </section>

      <section className="card">
        <h2>Fields filled</h2>
        {Object.keys(e.fields_filled).length === 0 ? (
          <p className="muted">ZoomInfo didn't fill any fields.</p>
        ) : (
          <p>
            {Object.entries(e.fields_filled)
              .map(([field, n]) => `${field}: ${n}`)
              .join(" · ")}
          </p>
        )}
        {e.filled.length > 0 && (
          <div className="table-wrap">
            <table className="grid compact">
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Field</th>
                  <th>Before</th>
                  <th>After</th>
                </tr>
              </thead>
              <tbody>
                {e.filled.map((f) => (
                  <tr key={`${f.row_id}:${f.field}`}>
                    <td>{f.row_id}</td>
                    <td>{f.field}</td>
                    <td className="muted">{f.before || "(blank)"}</td>
                    <td>{f.after}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="actions">
        <Link to={`/jobs/${jobId}/analysis`}>Back to rows</Link>
        <Link className="button primary" to={`/jobs/${jobId}/send`}>
          Next: Review & Send
        </Link>
      </div>
    </>
  );
}

function ReviewItem({
  item,
  choice,
  disabled,
  onChoose,
}: {
  item: EnrichmentReviewItem;
  choice: EnrichmentDecision;
  disabled: boolean;
  onChoose: (d: EnrichmentDecision) => void;
}) {
  return (
    <article className="review-item" aria-label={`Row ${item.row_id}`}>
      <h3>
        Row {item.row_id} · match score {item.match_score ?? "—"}
        {item.decision && <span className="badge"> decided: {item.decision}</span>}
      </h3>
      <div className="table-wrap">
      <table className="grid compact">
        <thead>
          <tr>
            <th />
            <th>Your file</th>
            <th>ZoomInfo</th>
          </tr>
        </thead>
        <tbody>
          {COMPARE.map(({ key, label }) => {
            const mine = item.source[key];
            const theirs = item.candidate[key] ?? "";
            return (
              <tr key={key} className={theirs && mine && theirs !== mine ? "differs" : ""}>
                <th>{label}</th>
                <td>{mine || <span className="muted">(blank)</span>}</td>
                <td>{theirs || <span className="muted">—</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
      <ul>
        {item.conflicts.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <p className="muted small">
        {item.would_fill.length
          ? `Apply would fill: ${item.would_fill.join(", ")}. Nothing you uploaded is changed.`
          : "Apply wouldn't fill anything: every field it has is already filled."}
      </p>
      <div className="segmented" role="radiogroup" aria-label={`Decision for row ${item.row_id}`}>
        {(["apply", "skip"] as const).map((d) => (
          <label key={d}>
            <input
              type="radio"
              name={`decision-${item.row_id}`}
              checked={choice === d}
              disabled={disabled}
              onChange={() => onChoose(d)}
            />
            {d === "apply" ? "Apply" : "Skip"}
          </label>
        ))}
      </div>
    </article>
  );
}

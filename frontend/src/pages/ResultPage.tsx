import { useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { RowProblem, SendResult } from "../api/types";
import { Async, PageHeader, Stat } from "../components/ui";
import { useApi } from "../components/useApi";
import { useJobPoll } from "../components/useJobPoll";

const fmt = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "America/Chicago",
});

export function ResultPage({ pollIntervalMs = 1500 }: { pollIntervalMs?: number }) {
  const { jobId = "" } = useParams();
  const { job, error, reload } = useJobPoll(jobId, pollIntervalMs);

  let body;
  if (error) body = <p role="alert" className="error">{error}</p>;
  else if (!job) body = <p className="muted">Loading…</p>;
  else if (job.state === "SENDING")
    body = (
      <section className="card" role="status">
        <p className="big">Sending leads to Eloqua…</p>
        <p className="muted">Up to five leads are sent at a time. You can leave this page; sending carries on.</p>
      </section>
    );
  else body = <ResultLoader jobId={jobId} state={job.state} onRetried={reload} />;

  return (
    <>
      <PageHeader title="Result" />
      {body}
    </>
  );
}

function ResultLoader({ jobId, state, onRetried }: { jobId: string; state: string; onRetried: () => void }) {
  const result = useApi(() => api.getResult(jobId), `result:${jobId}:${state}`);
  return <Async state={result}>{(r) => <ResultView jobId={jobId} r={r} onRetried={onRetried} />}</Async>;
}

function ProblemList({ items }: { items: RowProblem[] }) {
  return (
    <ul>
      {items.map((f) => (
        <li key={f.row_id}>
          Row {f.row_id} ({f.email}): {f.reason}
        </li>
      ))}
    </ul>
  );
}

function ResultView({ jobId, r, onRetried }: { jobId: string; r: SendResult; onRetried: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const retry = () =>
    run(async () => {
      await api.retryFailed(jobId);
      onRetried();
    });

  const download = () =>
    run(async () => {
      const { url, filename } = await api.download(jobId);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.rel = "noopener";
      document.body.append(a);
      a.click();
      a.remove();
    });

  const campaigns = r.campaigns.map((c) => `${c.name} (${c.rows})`).join(", ");
  return (
    <>
      {r.last_error && (
        <p role="alert" className="card blocked">
          {r.last_error.message}
        </p>
      )}
      <section className="stats" aria-label="Send summary">
        <Stat label="Submitted" value={r.submitted} tone="ok" />
        <Stat label="Failed" value={r.failed.length} tone={r.failed.length ? "bad" : undefined} />
        <Stat label="Not confirmed" value={r.unconfirmed.length} tone={r.unconfirmed.length ? "warn" : undefined} />
      </section>
      <section className="card">
        {r.submitted > 0 ? (
          <p className="big">
            {r.submitted} {r.submitted === 1 ? "lead" : "leads"} submitted to Eloqua for {campaigns}
            {r.submitted_at && <> on {fmt.format(new Date(r.submitted_at))} CT</>}
            {r.submitted_by && <> by {r.submitted_by}</>}.
          </p>
        ) : (
          <p className="big">No leads were submitted.</p>
        )}
        <p className="muted">
          "Submitted" means Eloqua accepted the form post. It doesn't yet confirm the contact was created.
          {!r.send_to_prod && " Test environment: nothing reached Eloqua."}
        </p>
        <button type="button" onClick={download} disabled={busy}>
          Download processed file
        </button>
      </section>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {r.failed.length > 0 && (
        <section className="card blocked" aria-label="Failed rows">
          <h2>
            {r.failed.length} {r.failed.length === 1 ? "row" : "rows"} failed
          </h2>
          <ProblemList items={r.failed} />
          <button type="button" className="primary" onClick={retry} disabled={busy}>
            Retry failed rows
          </button>
        </section>
      )}
      {r.unconfirmed.length > 0 && (
        <section className="card note" aria-label="Unconfirmed rows">
          <h2>
            {r.unconfirmed.length} {r.unconfirmed.length === 1 ? "row wasn't" : "rows weren't"} confirmed
          </h2>
          <p>
            No answer came back for these, so they may or may not have reached Eloqua. They aren't retried
            automatically, so a lead isn't sent twice. Ask Marketing Ops to check them in Eloqua.
          </p>
          <ProblemList items={r.unconfirmed} />
        </section>
      )}
    </>
  );
}

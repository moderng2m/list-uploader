import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { GateResult } from "../api/types";
import { Async, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

function SendView({ jobId, gate }: { jobId: string; gate: GateResult }) {
  const total = gate.by_campaign.reduce((n, c) => n + c.rows, 0);
  const campaigns = new Set(gate.by_campaign.map((c) => c.campaign_id)).size;
  return (
    <>
      <section className="card">
        <h2>What will be sent</h2>
        <table className="grid">
          <thead>
            <tr>
              <th>Campaign</th>
              <th>ID</th>
              <th>Member status</th>
              <th>Rows</th>
            </tr>
          </thead>
          <tbody>
            {gate.by_campaign.map((c) => (
              <tr key={`${c.campaign_id}:${c.status}`}>
                <td>{c.campaign_name}</td>
                <td>
                  <code>{c.campaign_id}</code>
                </td>
                <td>{c.status}</td>
                <td>{c.rows}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {gate.not_sent_fields.length > 0 && (
          <p className="muted">
            Not sent to Eloqua (kept in the processed file): {gate.not_sent_fields.join(", ")}.
          </p>
        )}
      </section>

      {gate.passed ? (
        <section className="card">
          <p>
            Send {total} leads to Eloqua for {campaigns} campaign{campaigns === 1 ? "" : "s"}.
          </p>
          <Link className="button primary" to={`/jobs/${jobId}/result`}>
            Send
          </Link>
        </section>
      ) : (
        <section className="card blocked" aria-label="Blocking issues">
          <h2>You can't send yet</h2>
          <ul>
            {gate.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <Link to={`/jobs/${jobId}/analysis`}>Go back and fix these</Link>
        </section>
      )}
    </>
  );
}

export function SendPage() {
  const { jobId = "" } = useParams();
  const state = useApi(() => api.getGate(jobId), `gate:${jobId}`);
  return (
    <>
      <PageHeader title="Review and send" />
      <Async state={state}>{(g) => <SendView jobId={jobId} gate={g} />}</Async>
    </>
  );
}

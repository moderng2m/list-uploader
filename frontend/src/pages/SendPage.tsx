import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { GateResult, SendConfirmation } from "../api/types";
import { Async, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

function confirmationOf(gate: GateResult): SendConfirmation {
  return {
    rows_to_send: gate.rows_to_send,
    by_campaign: gate.by_campaign.map(({ campaign_id, status, rows }) => ({ campaign_id, status, rows })),
  };
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function SendView({ jobId, gate, reload }: { jobId: string; gate: GateResult; reload: () => void }) {
  const navigate = useNavigate();
  // A ref, not just state: a fast double click must never start two sends.
  const sent = useRef(false);
  const [sending, setSending] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send() {
    if (sent.current) return;
    sent.current = true;
    setSending(true);
    setError(null);
    try {
      await api.send(jobId, confirmationOf(gate), gate.passed);
      navigate(`/jobs/${jobId}/result`);
    } catch (err) {
      sent.current = false;
      setSending(false);
      setError(err instanceof Error ? err.message : String(err));
      reload();
    }
  }

  async function recheck() {
    setChecking(true);
    setError(null);
    try {
      await api.revalidateCampaigns(jobId);
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  }

  if (!gate.can_send)
    return (
      <section className="card">
        <p>This upload has already been sent or is being sent.</p>
        <Link className="button primary" to={`/jobs/${jobId}/result`}>
          See the result
        </Link>
      </section>
    );

  return (
    <>
      {!gate.send_to_prod && (
        <p className="card note" role="note">
          Test environment: leads go to the fake Post to Eloqua. Nothing reaches Eloqua or Salesforce.
        </p>
      )}
      <section className="card">
        <h2>What will be sent</h2>
        <p>
          {plural(gate.rows_to_send, "lead", "leads")}
          {gate.excluded > 0 && <span className="muted"> ({plural(gate.excluded, "row", "rows")} excluded)</span>}
        </p>
        <div className="table-wrap">
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
                  <td>{c.status || <span className="muted">(blank)</span>}</td>
                  <td>{c.rows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <h3>Fields sent to Eloqua</h3>
        <p>{gate.sent_fields.join(", ")}</p>
        {gate.not_sent_fields.length > 0 && (
          <p className="muted">
            Not sent (kept in the processed file): {gate.not_sent_fields.join(", ")}. Post to Eloqua doesn't accept
            these fields yet.
          </p>
        )}
      </section>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      {gate.passed ? (
        <section className="card" aria-label="Confirm send">
          <p className="big">
            Send {plural(gate.rows_to_send, "lead", "leads")} to Eloqua for{" "}
            {plural(gate.campaign_count, "campaign", "campaigns")}.
          </p>
          <div className="actions">
            <Link to={`/jobs/${jobId}/analysis`}>Back to rows</Link>
            <button type="button" className="primary" onClick={send} disabled={sending}>
              {sending ? "Sending…" : "Send"}
            </button>
          </div>
        </section>
      ) : (
        <section className="card blocked" aria-label="Blocking issues">
          <h2>You can't send yet</h2>
          <ul>
            {gate.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <div className="actions">
            <Link to={`/jobs/${jobId}/analysis`}>Go back and fix these</Link>
            {gate.campaigns_stale && (
              <button type="button" className="primary" onClick={recheck} disabled={checking}>
                {checking ? "Checking…" : "Re-check campaigns"}
              </button>
            )}
          </div>
        </section>
      )}
    </>
  );
}

export function SendPage() {
  const { jobId = "" } = useParams();
  const [version, setVersion] = useState(0);
  const state = useApi(() => api.getGate(jobId), `gate:${jobId}:${version}`);
  return (
    <>
      <PageHeader title="Review and send">
        Check what will be sent. Every lead goes to Eloqua through Post to Eloqua, one at a time.
      </PageHeader>
      <Async state={state}>{(g) => <SendView jobId={jobId} gate={g} reload={() => setVersion((v) => v + 1)} />}</Async>
    </>
  );
}

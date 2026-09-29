import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { ErrorLine } from "./ErrorLine";
import { useVersioned } from "./useVersioned";

const LABELS: Record<string, string> = {
  mapping_suggest_threshold: "Suggest a column match at or above",
  lead_source_auto_threshold: "Auto-correct a lead source at or above",
  junk_flag_threshold: "Flag a value as suspect at or above",
  junk_block_threshold: "Block a value as junk at or above",
};

export function ThresholdsPanel() {
  const { data, error, busy, change, reload } = useVersioned(api.adminConfig);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (data) setDraft(Object.fromEntries(Object.entries(data.thresholds).map(([k, v]) => [k, String(v)])));
  }, [data]);
  if (!data) return error ? <ErrorLine error={error} /> : <p className="muted">Loading…</p>;

  const changed = Object.fromEntries(
    Object.entries(draft)
      .filter(([k, v]) => Number(v) !== data.thresholds[k])
      .map(([k, v]) => [k, Number(v)]),
  );

  return (
    <section className="card" aria-label="Thresholds">
      <h2>AI confidence thresholds</h2>
      <p className="muted small">Between 0 and 1. New analyses use the new values; past uploads keep theirs.</p>
      <ErrorLine error={error} onReload={reload} />
      <form
        className="form"
        onSubmit={async (e) => {
          e.preventDefault();
          setSaved(await change(() => api.updateThresholds(data.version, changed)));
        }}
      >
        {Object.keys(data.thresholds).map((k) => (
          <label key={k} className="kv">
            <span>{LABELS[k] ?? k}</span>
            <input
              type="number"
              min={0}
              max={1}
              step="any"
              value={draft[k] ?? ""}
              onChange={(e) => (setSaved(false), setDraft({ ...draft, [k]: e.target.value }))}
              aria-label={LABELS[k] ?? k}
            />
          </label>
        ))}
        <div className="actions">
          {saved && <span className="muted" role="status">Saved.</span>}
          <button type="submit" className="primary" disabled={busy || Object.keys(changed).length === 0}>
            Save thresholds
          </button>
        </div>
      </form>
      <h3>Limits</h3>
      <dl>
        {Object.entries(data.limits).map(([k, v]) => (
          <div key={k} className="kv">
            <dt>{k.replaceAll("_", " ")}</dt>
            <dd>{v.toLocaleString()}</dd>
          </div>
        ))}
      </dl>
      <p className="muted small">Limits are set in the deployment config.</p>
    </section>
  );
}

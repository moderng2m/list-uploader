import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type { AuditFilters, AuditSearchResult } from "../../api/types";
import { EventList, formatTime } from "../../components/EventList";
import { ErrorLine } from "./ErrorLine";

// SPEC §21.2.3 event catalog, plus the audited export.
const EVENT_TYPES = [
  "JOB_CREATED", "FILE_UPLOADED", "FILE_PARSED", "PARSE_FAILED", "MAPPING_SUGGESTED", "MAPPING_CONFIRMED",
  "ANALYSIS_STARTED", "VALUE_NORMALIZED", "VALUE_AUTO_CORRECTED", "VALUE_DERIVED", "ISSUE_RAISED",
  "ISSUE_CLEARED", "AI_INVOCATION", "CAMPAIGN_VALIDATED", "USER_EDIT", "ROW_EXCLUDED", "ROW_INCLUDED",
  "BULK_ACTION", "SUGGESTION_ACCEPTED", "SUGGESTION_REJECTED", "ENRICHMENT_REQUESTED", "ENRICHMENT_RESULT",
  "ENRICHMENT_DECISION", "GATE_EVALUATED", "SEND_CONFIRMED", "ROW_SUBMITTED", "ROW_SEND_FAILED",
  "JOB_STATE_CHANGED", "PROCESSED_FILE_DOWNLOADED", "ADMIN_CONFIG_CHANGED", "ACCESS_DENIED", "AUDIT_EXPORTED",
];

const FIELDS: { key: keyof AuditFilters; label: string; type?: string; placeholder?: string }[] = [
  { key: "email", label: "Lead email", type: "email", placeholder: "ada@acme.example" },
  { key: "job_id", label: "Upload (job ID)" },
  { key: "user", label: "Done by (user email)", type: "email" },
  { key: "campaign_id", label: "Campaign ID" },
  { key: "from", label: "From", type: "date" },
  { key: "to", label: "To", type: "date" },
];

export function AuditSearchPanel() {
  const [filters, setFilters] = useState<AuditFilters>({});
  const [searched, setSearched] = useState<AuditFilters | null>(null);
  const [result, setResult] = useState<AuditSearchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [exported, setExported] = useState<string | null>(null);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const search = () =>
    run(async () => {
      setExported(null);
      setResult(await api.auditSearch(filters));
      setSearched(filters);
    });

  const exportCsv = () =>
    run(async () => {
      const out = await api.auditExport(searched ?? filters);
      const a = document.createElement("a");
      a.href = out.url;
      a.download = out.filename;
      a.rel = "noopener";
      document.body.append(a);
      a.click();
      a.remove();
      setExported(`Exported ${out.events.toLocaleString()} events${out.truncated ? " (first 10,000)" : ""}.`);
    });

  return (
    <section className="card" aria-label="Audit search">
      <h2>Audit search</h2>
      <p className="muted small">
        Emails are looked up by hash; the audit trail never stores them in a searchable form. Exports are
        themselves recorded.
      </p>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        {FIELDS.map((f) => (
          <label key={f.key}>
            {f.label}
            <input
              type={f.type ?? "text"}
              placeholder={f.placeholder}
              value={filters[f.key] ?? ""}
              onChange={(e) => setFilters({ ...filters, [f.key]: e.target.value })}
            />
          </label>
        ))}
        <label>
          Event
          <select value={filters.event_type ?? ""} onChange={(e) => setFilters({ ...filters, event_type: e.target.value })}>
            <option value="">Any</option>
            {EVENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <div className="actions">
          <button type="submit" className="primary" disabled={busy}>
            Search
          </button>
        </div>
      </form>
      <ErrorLine error={error} />
      {result && (
        <>
          {searched?.email && (
            <section aria-label="Uploads with this person">
              <h3>
                {result.jobs.length === 0
                  ? "No uploads included this person."
                  : `${result.jobs.length} ${result.jobs.length === 1 ? "upload" : "uploads"} included this person`}
              </h3>
              {result.jobs.length > 0 && (
                <div className="table-wrap">
                  <table className="grid compact">
                    <thead>
                      <tr>
                        <th>File</th>
                        <th>Owner</th>
                        <th>Uploaded</th>
                        <th>Status</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {result.jobs.map((j) => (
                        <tr key={j.job_id}>
                          <td>{j.filename ?? j.job_id}</td>
                          <td>{j.owner_email}</td>
                          <td>{formatTime(j.created_at)}</td>
                          <td>{j.state?.replaceAll("_", " ").toLowerCase()}</td>
                          <td>
                            <Link to={`/jobs/${j.job_id}/timeline`}>Timeline</Link>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
          <div className="actions">
            {exported && (
              <span className="muted" role="status">
                {exported}
              </span>
            )}
            <button type="button" onClick={() => void exportCsv()} disabled={busy || result.events.length === 0}>
              Export CSV
            </button>
          </div>
          <h3>
            {result.events.length.toLocaleString()} events{result.truncated && " (newest 500; narrow the search)"}
          </h3>
          <EventList events={result.events} showJob />
        </>
      )}
    </section>
  );
}

import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Job, Mapping, MappingColumn } from "../api/types";
import { Async, MethodBadge, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

function MappingEditor({ jobId, mapping }: { jobId: string; mapping: Mapping }) {
  const navigate = useNavigate();
  const [columns, setColumns] = useState<MappingColumn[]>(mapping.columns);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => setColumns(mapping.columns), [mapping]);

  const original = new Map(mapping.columns.map((c) => [c.source_header, c]));
  const assigned = new Set(columns.map((c) => c.field_key).filter(Boolean));
  const missing = mapping.catalog.filter((f) => f.must_map && !assigned.has(f.key));
  const autoFilled = mapping.catalog.filter((f) => f.fill_when_unmapped && !assigned.has(f.key));

  function choose(index: number, fieldKey: string | null) {
    setError(null);
    setColumns((prev) =>
      prev.map((c, i) => {
        if (i === index) {
          const was = original.get(c.source_header);
          // Back to what we suggested: restore its badge. Otherwise it's the user's choice.
          if (was && was.field_key === fieldKey && fieldKey) return { ...was, samples: c.samples };
          return { ...c, field_key: fieldKey, method: fieldKey ? "manual" : "none", confidence: null };
        }
        // One-to-one: picking a field another column holds takes it off that column.
        if (fieldKey && c.field_key === fieldKey) return { ...c, field_key: null, method: "none", confidence: null };
        return c;
      }),
    );
  }

  async function onContinue() {
    setSaving(true);
    setError(null);
    try {
      await api.confirmMapping(
        jobId,
        columns.map((c) => ({ source_header: c.source_header, field_key: c.field_key })),
      );
      navigate(`/jobs/${jobId}/analysis`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  }

  return (
    <div className="two-col">
      <div className="card table-wrap">
        <table className="grid mapping">
          <thead>
            <tr>
              <th>Your column</th>
              <th aria-hidden>→</th>
              <th>Maps to</th>
              <th>Match</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((c, i) => (
              <tr key={c.source_header} className={c.method === "ai" ? "ai-row" : ""}>
                <td>
                  <strong>{c.source_header}</strong>
                  <div className="samples">{c.samples.join(" · ") || "(no values)"}</div>
                </td>
                <td aria-hidden>→</td>
                <td>
                  <select
                    aria-label={`Target for ${c.source_header}`}
                    value={c.field_key ?? ""}
                    disabled={!mapping.editable || saving}
                    onChange={(e) => choose(i, e.target.value || null)}
                  >
                    <option value="">Ignore this column</option>
                    {mapping.catalog.map((f) => (
                      <option key={f.key} value={f.key} title={f.description}>
                        {f.label}
                        {f.must_map ? " *" : ""}
                      </option>
                    ))}
                  </select>
                </td>
                <td title={c.reason ?? undefined}>
                  <MethodBadge method={c.field_key ? c.method : "none"} confidence={c.confidence} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <aside className="card">
        <h2>Required fields not yet mapped</h2>
        {missing.length === 0 ? (
          <p className="ok">All required fields are mapped.</p>
        ) : (
          <ul aria-label="Required fields not yet mapped">
            {missing.map((f) => (
              <li key={f.key}>{f.label}</li>
            ))}
          </ul>
        )}
        {autoFilled.length > 0 && (
          <>
            <h3>Filled in automatically</h3>
            <ul className="muted small">
              {autoFilled.map((f) => (
                <li key={f.key}>
                  {f.label}: {f.fill_when_unmapped}
                </li>
              ))}
            </ul>
          </>
        )}
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {mapping.editable ? (
          <button
            type="button"
            className="primary"
            disabled={missing.length > 0 || saving}
            onClick={onContinue}
          >
            {saving ? "Saving…" : "Confirm mapping and continue"}
          </button>
        ) : (
          <p className="muted">This mapping is confirmed and can no longer be changed.</p>
        )}
      </aside>
    </div>
  );
}

function FileDetails({ job }: { job: Job }) {
  const p = job.parse;
  if (!p) return null;
  const where = p.file_type === "xlsx" ? `sheet “${p.sheet_name}”` : `${p.encoding} CSV`;
  return (
    <section className="card file-details" aria-label="File details">
      <p>
        <strong>{job.filename}</strong>: {p.row_count.toLocaleString()} rows and {p.column_count} columns read
        from {where}.
      </p>
      {p.warnings.length > 0 && (
        <ul className="warnings">
          {p.warnings.map((w, i) => (
            <li key={i}>{w.message}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function MappingPage() {
  const { jobId = "" } = useParams();
  const state = useApi(() => api.getMapping(jobId), `mapping:${jobId}`);
  const job = useApi(() => api.getJob(jobId), `job:${jobId}`);
  return (
    <>
      <PageHeader title="Map your columns">
        Check each column goes to the right field. AI suggestions are highlighted — confirm them before you continue.
      </PageHeader>
      {job.status === "ready" && <FileDetails job={job.data} />}
      {state.status === "ready" && state.data.ai_note && (
        <p className="card note" role="note">
          {state.data.ai_note}
        </p>
      )}
      <Async state={state}>{(m) => <MappingEditor jobId={jobId} mapping={m} />}</Async>
    </>
  );
}

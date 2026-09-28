import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Job, Mapping, MappingColumn } from "../api/types";
import { Async, MethodBadge, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

// Derived from Salesforce, so they may stay unmapped (SPEC §8).
const DERIVABLE = new Set(["campaign_name", "lead_source", "campaign_status"]);

function MappingEditor({ jobId, mapping }: { jobId: string; mapping: Mapping }) {
  const [columns, setColumns] = useState<MappingColumn[]>(mapping.columns);
  useEffect(() => setColumns(mapping.columns), [mapping]);

  const assigned = new Set(columns.map((c) => c.field_key).filter(Boolean));
  const missing = mapping.catalog.filter(
    (f) => f.required && !DERIVABLE.has(f.key) && !assigned.has(f.key),
  );

  function choose(index: number, fieldKey: string | null) {
    setColumns((prev) =>
      prev.map((c, i) => {
        if (i === index) return { ...c, field_key: fieldKey, method: fieldKey ? c.method : "none" };
        // One-to-one: picking a field another column holds swaps it off that column.
        if (fieldKey && c.field_key === fieldKey) return { ...c, field_key: null, method: "none" };
        return c;
      }),
    );
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
                <div className="samples">{c.samples.join(" · ")}</div>
              </td>
              <td aria-hidden>→</td>
              <td>
                <select
                  aria-label={`Target for ${c.source_header}`}
                  value={c.field_key ?? ""}
                  onChange={(e) => choose(i, e.target.value || null)}
                >
                  <option value="">Ignore this column</option>
                  {mapping.catalog.map((f) => (
                    <option key={f.key} value={f.key}>
                      {f.label}
                      {f.required ? " *" : ""}
                    </option>
                  ))}
                </select>
              </td>
              <td>
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
          <ul>
            {missing.map((f) => (
              <li key={f.key}>{f.label}</li>
            ))}
          </ul>
        )}
        <p className="muted">Campaign Name, Lead Source, and Status can come from Salesforce.</p>
        <Link
          className={`button primary ${missing.length ? "disabled" : ""}`}
          aria-disabled={missing.length > 0}
          to={missing.length ? "#" : `/jobs/${jobId}/analysis`}
        >
          Continue
        </Link>
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
      <Async state={state}>{(m) => <MappingEditor jobId={jobId} mapping={m} />}</Async>
    </>
  );
}

import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Analysis, BulkAction, CampaignCard, GridColumn, Issue, Row, RowChange } from "../api/types";
import { Async, PageHeader, SeverityBadge, StatusBadge, SummaryCards } from "../components/ui";
import { RowHistoryDrawer } from "../components/RowHistoryDrawer";
import { useApi } from "../components/useApi";
import { useJobPoll } from "../components/useJobPoll";

// Used only if the server doesn't describe the columns.
const FALLBACK_COLUMNS: GridColumn[] = [
  ["company", "Company"],
  ["first_name", "First name"],
  ["last_name", "Last Name"],
  ["email", "Email Address"],
  ["campaign_id", "SFDC Last Campaign ID"],
  ["campaign_status", "SFDC Last Campaign Status"],
  ["lead_source", "Lead Source - Most Recent"],
].map(([key, label]) => ({ kind: "mapped", key: key!, label: label!, source_header: null, editable: true }));

const gridColumns = (analysis: Analysis) => analysis.columns ?? FALLBACK_COLUMNS;
const columnId = (c: GridColumn) => c.key ?? `ignored:${c.source_header}`;
const PAGE = 100;

type Act = (fn: () => Promise<unknown>) => Promise<void>;

// --- job state wrapper ------------------------------------------------------------------

export function AnalysisPage({ pollIntervalMs = 1500 }: { pollIntervalMs?: number }) {
  const { jobId = "" } = useParams();
  const { job, error, reload } = useJobPoll(jobId, pollIntervalMs);
  const [starting, setStarting] = useState(false);

  async function rerun() {
    setStarting(true);
    try {
      await api.startAnalysis(jobId);
      reload();
    } finally {
      setStarting(false);
    }
  }

  let body;
  if (error) body = <p role="alert" className="error">{error}</p>;
  else if (!job) body = <p className="muted">Loading…</p>;
  else if (job.state === "ANALYZING")
    body = (
      <section className="card" role="status">
        <p className="big">Analyzing your file…</p>
        <p className="muted">
          Checking campaigns in Salesforce, lead sources, statuses, duplicates, and junk values. This usually takes
          under a minute.
        </p>
      </section>
    );
  else if (job.state === "FAILED")
    body = (
      <section className="card blocked">
        <p role="alert" className="error">{job.last_error?.message ?? "Analysis didn't finish."}</p>
        <button type="button" className="primary" onClick={rerun} disabled={starting}>
          Run analysis again
        </button>
      </section>
    );
  else if (["AWAITING_UPLOAD", "UPLOADED", "MAPPING_REVIEW", "PARSE_FAILED"].includes(job.state))
    body = (
      <section className="card">
        <p>This file hasn't been analyzed yet.</p>
        <Link to={`/jobs/${jobId}/mapping`}>Confirm the column mapping first</Link>
      </section>
    );
  else body = <AnalysisReview jobId={jobId} />;

  return (
    <>
      <PageHeader title="Analyze and fix">Fix blocking issues before you can send.</PageHeader>
      {body}
    </>
  );
}

// --- review -----------------------------------------------------------------------------

function AnalysisReview({ jobId }: { jobId: string }) {
  const [version, setVersion] = useState(0);
  const [filter, setFilter] = useState<{ issue_code?: string; status?: string }>({});
  const [offset, setOffset] = useState(0);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const analysis = useApi(() => api.getAnalysis(jobId), `analysis:${jobId}:${version}`);
  const rows = useApi(
    () => api.getRows(jobId, { ...filter, offset, limit: PAGE }),
    `rows:${jobId}:${version}:${JSON.stringify(filter)}:${offset}`,
  );

  const act: Act = async (fn) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      setVersion((v) => v + 1);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const rowsPanel = useRef<HTMLElement>(null);
  const setIssueFilter = (code?: string) => {
    setOffset(0);
    setFilter((f) => ({ ...f, issue_code: code }));
    // Side by side the rows are already in view; stacked (narrow screens) they're below.
    if (code) rowsPanel.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  };

  return (
    <Async state={analysis}>
      {(a) => (
        <div aria-busy={busy}>
          {a.notes.map((n) => (
            <p key={n} className="card note" role="note">
              {n}
            </p>
          ))}
          <SummaryCards summary={a.summary} />
          {actionError && (
            <p role="alert" className="error">
              {actionError}
            </p>
          )}
          <div className="review-layout">
            <div className="review-side">
              <IssueGroups
                analysis={a}
                active={filter.issue_code}
                onFilter={setIssueFilter}
                onBulk={(action, params) => act(() => api.bulkAction(jobId, action, params))}
              />
              <CampaignPanel campaigns={a.campaigns} />
            </div>
          <section className="card review-rows" ref={rowsPanel} aria-label="Rows">
            <h2>Rows</h2>
            {filter.issue_code && (
              <p className="filter-banner" role="status">
                Showing rows with <code>{filter.issue_code}</code>{" "}
                <button type="button" className="link" onClick={() => setIssueFilter(undefined)}>
                  Show all rows
                </button>
              </p>
            )}
            <label className="inline">
              Show{" "}
              <select
                aria-label="Filter rows by status"
                value={filter.status ?? ""}
                onChange={(e) => {
                  setOffset(0);
                  setFilter((f) => ({ ...f, status: e.target.value || undefined }));
                }}
              >
                <option value="">all rows</option>
                <option value="blocked">blocked</option>
                <option value="warning">warnings</option>
                <option value="pending_enrichment">pending enrichment</option>
                <option value="ready">ready</option>
                <option value="excluded">excluded</option>
              </select>
            </label>
            <Async state={rows}>
              {(page) => (
                <>
                  <RowsGrid
                    rows={page.rows}
                    analysis={a}
                    editable={a.editable && !busy}
                    onChange={(rowId, change) => act(() => api.editRow(jobId, rowId, change))}
                    onBulk={(action, params) => act(() => api.bulkAction(jobId, action, params))}
                  />
                  {page.total > PAGE && (
                    <div className="actions">
                      <button type="button" disabled={offset === 0} onClick={() => setOffset(offset - PAGE)}>
                        Previous
                      </button>
                      <span className="muted">
                        {offset + 1}–{Math.min(offset + PAGE, page.total)} of {page.total}
                      </span>
                      <button
                        type="button"
                        disabled={offset + PAGE >= page.total}
                        onClick={() => setOffset(offset + PAGE)}
                      >
                        Next
                      </button>
                    </div>
                  )}
                </>
              )}
            </Async>
          </section>
          </div>
          <NextStep jobId={jobId} analysis={a} />
        </div>
      )}
    </Async>
  );
}

function IssueGroups({
  analysis,
  active,
  onFilter,
  onBulk,
}: {
  analysis: Analysis;
  active?: string;
  onFilter: (code?: string) => void;
  onBulk: (action: BulkAction, params?: Record<string, unknown>) => void;
}) {
  if (analysis.issue_groups.length === 0)
    return <p className="card ok">No issues found. Every row is ready.</p>;
  return (
    <section className="card">
      <h2>Issues</h2>
      <ul className="issue-groups">
        {analysis.issue_groups.map((g) => (
          <li key={g.code} className={active === g.code ? "active" : undefined}>
            <SeverityBadge severity={g.severity} /> <code>{g.code}</code> · {g.count} {g.count === 1 ? "row" : "rows"}
            {g.values && g.values > g.count ? ` · ${g.values} values` : ""} —{" "}
            {g.explanation}
            <span className="issue-actions">
              <button type="button" onClick={() => onFilter(active === g.code ? undefined : g.code)}>
                {active === g.code ? "Show all rows" : "Show rows"}
              </button>
              {g.bulk_action && g.bulk_action !== "set_status" && analysis.editable && (
                <button
                  type="button"
                  onClick={() =>
                    onBulk(g.bulk_action!, g.bulk_action === "accept_lead_source_suggestions" ? { min_confidence: 0.9 } : {})
                  }
                >
                  {g.bulk_action_label}
                </button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function CampaignPanel({ campaigns }: { campaigns: CampaignCard[] }) {
  return (
    <section className="card" aria-label="Campaigns">
      <h2>Campaigns</h2>
      <div className="campaigns">
        {campaigns.map((c) => (
          <article key={c.id} className={`campaign ${c.found ? "" : "invalid"}`}>
            <h3>{c.found ? c.name : "Campaign not found"}</h3>
            <code>{c.id}</code>
            {c.found ? (
              <p>
                {c.type} · {c.is_active ? "Active" : <strong>Inactive</strong>} · {c.row_count}{" "}
                {c.row_count === 1 ? "row" : "rows"}
                <br />
                Statuses: {c.statuses.join(", ")} (default {c.default_status})
              </p>
            ) : (
              <p className="error">
                Salesforce doesn't have this ID. Copy the 18-character ID from the campaign's URL in Salesforce.
              </p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}

// --- grid -------------------------------------------------------------------------------

function RowsGrid({
  rows,
  analysis,
  editable,
  onChange,
  onBulk,
}: {
  rows: Row[];
  analysis: Analysis;
  editable: boolean;
  onChange: (rowId: number, change: RowChange) => void;
  onBulk: (action: BulkAction, params: Record<string, unknown>) => void;
}) {
  const { jobId = "" } = useParams();
  const [historyOf, setHistoryOf] = useState<number | null>(null);
  if (rows.length === 0) return <p className="muted">No rows match.</p>;
  return (
    <>
    <p className="muted grid-hint">
      {gridColumns(analysis).length} columns: your file's columns in order, then fields filled in for you. Scroll
      sideways to see them all.
    </p>
    <div className="table-wrap">
      {historyOf != null && <RowHistoryDrawer jobId={jobId} rowId={historyOf} onClose={() => setHistoryOf(null)} />}
      <table className="grid rows-grid">
        <thead>
          <tr>
            <th>Row</th>
            <th>Status</th>
            <th />
            {gridColumns(analysis).map((c) => (
              <ColumnHeader key={columnId(c)} column={c} />
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <RowLine
              key={r.row_id}
              row={r}
              analysis={analysis}
              editable={editable}
              onChange={onChange}
              onBulk={onBulk}
              onHistory={() => setHistoryOf(r.row_id)}
            />
          ))}
        </tbody>
      </table>
    </div>
    </>
  );
}

function ColumnHeader({ column: c }: { column: GridColumn }) {
  if (c.kind === "ignored")
    return (
      <th className="ignored-cell" title="You chose to ignore this column in step 1. It isn't used or sent.">
        {c.label}<span className="col-note">not used</span>
      </th>
    );
  if (c.kind === "filled")
    return (
      <th title="Not in your file: the app filled this in.">
        {c.label}<span className="col-note">filled in</span>
      </th>
    );
  const renamed = c.source_header && c.source_header !== c.label;
  return (
    <th title={renamed ? `Your column “${c.source_header}”, mapped to ${c.label}` : undefined}>
      {c.source_header ?? c.label}
      {renamed && <span className="col-note">→ {c.label}</span>}
    </th>
  );
}

function RowLine({
  row,
  analysis,
  editable,
  onChange,
  onBulk,
  onHistory,
}: {
  row: Row;
  analysis: Analysis;
  editable: boolean;
  onChange: (rowId: number, change: RowChange) => void;
  onBulk: (action: BulkAction, params: Record<string, unknown>) => void;
  onHistory: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});

  function startEdit() {
    setDraft(
      Object.fromEntries(
        gridColumns(analysis)
          .filter((c) => c.editable && c.key)
          .map((c) => [c.key!, row.processed[c.key!] ?? ""]),
      ),
    );
    setEditing(true);
  }
  function save() {
    const changed = Object.fromEntries(
      Object.entries(draft).filter(([k, v]) => v !== (row.processed[k] ?? "")),
    );
    setEditing(false);
    if (Object.keys(changed).length) onChange(row.row_id, { processed: changed });
  }

  const visible = row.issues.filter((i) => i.code !== "NOT_SENT_FIELD");
  return (
    <>
    <tr className={`row-main ${row.excluded ? "excluded" : ""} ${visible.length ? "has-issues" : ""}`} data-row-id={row.row_id}>
      <td>{row.row_id}</td>
      <td>
        <StatusBadge status={row.status} />
      </td>
      <td className="row-actions">
        {editable &&
          (editing ? (
            <>
              <button type="button" className="primary" onClick={save}>
                Save
              </button>
              <button type="button" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              <button type="button" onClick={startEdit} aria-label={`Edit row ${row.row_id}`}>
                Edit
              </button>
              <label className="check">
                <input
                  type="checkbox"
                  checked={row.excluded}
                  onChange={(e) => onChange(row.row_id, { excluded: e.target.checked })}
                  aria-label={`Exclude row ${row.row_id}`}
                />
                Exclude
              </label>
            </>
          ))}
        {!editing && (
          <button type="button" onClick={onHistory} aria-label={`History of row ${row.row_id}`}>
            History
          </button>
        )}
      </td>
      {gridColumns(analysis).map((c) => {
        if (!c.key) {
          const raw = row.unmapped?.[c.source_header ?? ""] ?? "";
          return (
            <td key={columnId(c)} className="ignored-cell">
              {raw}
            </td>
          );
        }
        const f = { key: c.key, label: c.source_header ?? c.label };
        const value = row.processed[f.key] ?? "";
        const source = row.source[f.key] ?? "";
        return (
          <td key={f.key}>
            {editing && c.editable ? (
              f.key === "lead_source" ? (
                <select
                  aria-label={`${f.label} for row ${row.row_id}`}
                  value={draft[f.key]}
                  onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                >
                  {!analysis.lead_sources.includes(draft[f.key] ?? "") && <option value={draft[f.key]}>{draft[f.key]}</option>}
                  {analysis.lead_sources.map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </select>
              ) : (
                <input
                  aria-label={`${f.label} for row ${row.row_id}`}
                  value={draft[f.key] ?? ""}
                  onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                />
              )
            ) : (
              <>
                {value || <span className="muted">(blank)</span>}
                {source !== value && source && <div className="was">was: {source}</div>}
              </>
            )}
          </td>
        );
      })}
    </tr>
    {visible.length > 0 && (
      <tr className={`row-issues ${row.excluded ? "excluded" : ""}`} data-issues-for={row.row_id}>
        <td colSpan={gridColumns(analysis).length + 3}>
          {visible.map((i) => (
            <IssueLine key={`${i.code}:${i.field}`} issue={i} row={row} analysis={analysis} editable={editable && !editing}
              onChange={onChange} onBulk={onBulk} />
          ))}
        </td>
      </tr>
    )}
    </>
  );
}

function IssueLine({
  issue,
  row,
  analysis,
  editable,
  onChange,
  onBulk,
}: {
  issue: Issue;
  row: Row;
  analysis: Analysis;
  editable: boolean;
  onChange: (rowId: number, change: RowChange) => void;
  onBulk: (action: BulkAction, params: Record<string, unknown>) => void;
}) {
  const [status, setStatus] = useState(issue.suggestion?.options?.[0] ?? "");
  const campaign = analysis.campaigns.find((c) => c.id === row.processed.campaign_id);
  return (
    <div className={`row-issue sev-text-${issue.severity}`}>
      <span>{issue.message}</span>
      {editable && issue.code === "LEAD_SOURCE_SUGGESTED" && issue.suggestion?.value && (
        <button type="button" onClick={() => onChange(row.row_id, { processed: { lead_source: issue.suggestion!.value! } })}>
          Accept “{issue.suggestion.value}”
        </button>
      )}
      {editable && issue.code === "STATUS_INVALID" && campaign && (
        <span className="fix">
          <select aria-label={`Status for row ${row.row_id}`} value={status} onChange={(e) => setStatus(e.target.value)}>
            {campaign.statuses.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <button type="button" onClick={() => onChange(row.row_id, { processed: { campaign_status: status } })}>
            Use for this row
          </button>
          <button
            type="button"
            onClick={() =>
              onBulk("set_status", { campaign_id: campaign.id, value: row.processed.campaign_status, status })
            }
          >
            Use for all rows with “{row.processed.campaign_status}”
          </button>
        </span>
      )}
      {editable && issue.code === "DUPLICATE_IN_FILE" && (
        <button type="button" onClick={() => onChange(row.row_id, { excluded: true })}>
          Exclude this row
        </button>
      )}
      {editable && (issue.code === "VALUE_SUSPECT" || issue.code === "VALUE_JUNK") && (
        <button type="button" onClick={() => onChange(row.row_id, { dismiss: `${issue.code}:${issue.field}` })}>
          Clear flag
        </button>
      )}
    </div>
  );
}

function NextStep({ jobId, analysis }: { jobId: string; analysis: Analysis }) {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  if (!analysis.enrich || analysis.state !== "ANALYSIS_REVIEW")
    return (
      <div className="actions">
        {analysis.state === "ENRICHMENT_REVIEW" && (
          <Link to={`/jobs/${jobId}/enrichment`}>Back to enrichment results</Link>
        )}
        <Link className="button primary" to={`/jobs/${jobId}/send`}>
          Next: Review & Send
        </Link>
      </div>
    );
  async function enrich() {
    setStarting(true);
    setError(null);
    try {
      await api.startEnrichment(jobId);
      navigate(`/jobs/${jobId}/enrichment`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStarting(false);
    }
  }
  return (
    <div className="actions">
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <p className="muted">
        Enrichment will look up {analysis.enrichment_lookup_count} contacts in ZoomInfo. Each lookup may use a
        ZoomInfo credit.
      </p>
      <button type="button" className="primary" onClick={enrich} disabled={starting}>
        {starting ? "Starting…" : "Next: Enrich"}
      </button>
    </div>
  );
}

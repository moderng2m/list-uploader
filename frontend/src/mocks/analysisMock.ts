// In-browser stand-in for the analysis endpoints (demo mode). It applies edits and bulk
// actions to a copy of the fixture rows with simplified rules; the real re-validation
// runs in the backend.
import type { BulkAction, Issue, Row, RowChange, RowStatus } from "../api/types";
import { analysis, analysisRows } from "./fixtures";

let rows: Row[] = structuredClone(analysisRows);

export function resetAnalysisMock() {
  rows = structuredClone(analysisRows);
}

function deriveStatus(r: Row): RowStatus {
  if (r.excluded) return "excluded";
  const blocking = r.issues.filter((i) => i.severity === "blocking");
  if (blocking.some((i) => !i.pending)) return "blocked";
  if (blocking.length) return "pending_enrichment";
  if (r.issues.some((i) => i.severity === "warning")) return "warning";
  return "ready";
}

function summary() {
  const count = (s: RowStatus) => rows.filter((r) => r.status === s).length;
  return {
    rows_total: rows.length,
    rows_ready: count("ready"),
    rows_warning: count("warning"),
    rows_blocked: count("blocked"),
    rows_excluded: count("excluded"),
    rows_pending_enrichment: count("pending_enrichment"),
  };
}

export function mockAnalysis() {
  // Rows per issue (what "Show rows" lists) and flags per issue (a row can have several).
  const rowCounts = new Map<string, number>();
  const valueCounts = new Map<string, number>();
  for (const r of rows) {
    if (r.excluded) continue;
    for (const code of new Set(r.issues.map((i) => i.code))) rowCounts.set(code, (rowCounts.get(code) ?? 0) + 1);
    for (const i of r.issues) valueCounts.set(i.code, (valueCounts.get(i.code) ?? 0) + 1);
  }
  return {
    ...analysis,
    summary: summary(),
    issue_groups: analysis.issue_groups
      .filter((g) => rowCounts.has(g.code))
      .map((g) => ({ ...g, count: rowCounts.get(g.code) ?? 0, values: valueCounts.get(g.code) ?? 0 })),
  };
}

// The demo file's ignored column (see `analysis.columns`).
const BADGES = ["blue", "red", "green"];
const withIgnored = (r: Row): Row => ({ ...r, unmapped: r.unmapped ?? { "Badge Color": BADGES[r.row_id % BADGES.length]! } });

export function mockRows(params: URLSearchParams) {
  let out = rows;
  const status = params.get("status");
  const code = params.get("issue_code");
  const campaign = params.get("campaign_id");
  if (status) out = out.filter((r) => r.status === status);
  if (code) out = out.filter((r) => r.issues.some((i) => i.code === code));
  if (campaign) out = out.filter((r) => r.processed.campaign_id === campaign);
  const offset = Number(params.get("offset") ?? 0);
  const limit = Number(params.get("limit") ?? 100);
  return {
    total: out.length,
    offset,
    rows: out
      .slice(offset, offset + limit)
      .map(withIgnored),
  };
}

function apply(r: Row, change: RowChange): Row {
  const next: Row = structuredClone(r);
  for (const [field, value] of Object.entries(change.processed ?? {})) {
    next.user_edits.push({ field, from: r.processed[field] ?? "", to: value, by: "demo", at: new Date().toISOString() });
    next.processed[field] = value;
    next.provenance[field] = "user_edit";
    next.issues = next.issues.filter((i: Issue) => i.field !== field);
  }
  if (change.excluded !== undefined) next.excluded = change.excluded;
  if (change.dismiss) {
    next.dismissed.push(change.dismiss);
    next.issues = next.issues.filter((i) => `${i.code}:${i.field}` !== change.dismiss);
  }
  next.status = deriveStatus(next);
  return next;
}

export function mockEdit(rowId: number, change: RowChange) {
  const index = rows.findIndex((r) => r.row_id === rowId);
  if (index < 0) return null;
  rows[index] = apply(rows[index]!, change);
  return { row: withIgnored(rows[index]!), also_changed: [] as number[] };
}

export function mockBulk(action: BulkAction, params: Record<string, unknown>) {
  const has = (r: Row, ...codes: string[]) => !r.excluded && r.issues.some((i) => codes.includes(i.code));
  const affected: number[] = [];
  rows = rows.map((r) => {
    let change: RowChange | null = null;
    if (action === "exclude_duplicates" && has(r, "DUPLICATE_IN_FILE")) change = { excluded: true };
    if (action === "exclude_junk" && has(r, "VALUE_JUNK", "VALUE_SUSPECT")) change = { excluded: true };
    if (action === "accept_lead_source_suggestions") {
      const s = r.issues.find((i) => i.code === "LEAD_SOURCE_SUGGESTED")?.suggestion;
      if (s?.value && (s.confidence ?? 0) >= Number(params.min_confidence ?? 0.9))
        change = { processed: { lead_source: s.value } };
    }
    if (
      action === "set_status" &&
      has(r, "STATUS_INVALID") &&
      r.processed.campaign_id === params.campaign_id &&
      (r.processed.campaign_status ?? "").toLowerCase() === String(params.value).toLowerCase()
    )
      change = { processed: { campaign_status: String(params.status) } };
    if (!change) return r;
    affected.push(r.row_id);
    return apply(r, change);
  });
  return { action, affected_row_ids: affected, summary: summary() };
}

/** Every row as it stands now (the send mock reads and updates send status). */
export function allRows(): Row[] {
  return rows;
}

// In-browser stand-in for the timeline, row history and audit search (demo mode).
// Summaries mirror backend/shared/audit_text.py; lineage mirrors shared/lineage.py
// in a simplified form (source cell -> processed value).
import type { AuditEventView, AuditFilters, AuditSearchResult, FieldLineage, RowHistory, Timeline } from "../api/types";
import { allRows } from "./analysisMock";
import { catalog, DEMO_JOB_ID, jobs, me } from "./fixtures";

const PROVENANCE: Record<string, string> = {
  source: "from your file",
  normalized: "cleaned up by the normalizer",
  user_edit: "edited by hand",
  "enrichment:zoominfo": "filled by ZoomInfo",
  "derived:sfdc_campaign_name": "the campaign's name in Salesforce",
  "derived:sfdc_campaign_type": "the campaign's type in Salesforce",
  "derived:sfdc_default_status": "the campaign's default member status",
  "derived:auto_list_name": "built from the campaign name and today's date",
  "auto_corrected:rule:marketing_prefix": "corrected by rule (added 'Marketing: ')",
  "auto_corrected:ai": "corrected by AI (high confidence)",
};
const NORMALIZER = "stand-in (NOT v5)";

let seq = 0;
function ev(
  at: string,
  type: string,
  summary: string,
  extra: Partial<AuditEventView> = {},
): AuditEventView {
  seq += 1;
  return {
    event_id: `01JDEMOEVENT${String(seq).padStart(14, "0")}`,
    event_type: type,
    occurred_at: at,
    job_id: DEMO_JOB_ID,
    row_id: null,
    actor: { type: "user", email: me.email },
    summary,
    reason: null,
    subject: null,
    before: null,
    after: null,
    details: null,
    ...extra,
  };
}
const system = { actor: { type: "system" as const, email: null } };

const JOB_EVENTS: AuditEventView[] = [
  ev("2026-09-28T15:04:00.000Z", "JOB_CREATED", "Upload started with enrichment on", { details: { enrich: true, filename: "demo_event_list.xlsx" } }),
  ev("2026-09-28T15:04:05.000Z", "FILE_UPLOADED", "File received and locked (48,211 bytes)", { ...system, details: { size: 48211, sha256: "0f3c…demo" } }),
  ev("2026-09-28T15:04:06.000Z", "FILE_PARSED", "File read: 8 rows, 10 columns", { ...system, details: { row_count: 8, column_count: 10 } }),
  ev("2026-09-28T15:04:06.500Z", "MAPPING_SUGGESTED", "Column mapping suggested", { ...system, details: { method_counts: { exact: 8, alias: 1, ai: 1, none: 0 } } }),
  ev("2026-09-28T15:05:40.000Z", "SUGGESTION_ACCEPTED", "AI column match kept for 'Job Position'", {
    subject: { field: "column_mapping", source_header: "Job Position" },
    after: { field_key: "title" },
    details: { confidence: 0.88 },
    reason: "ai:column_mapping",
  }),
  ev("2026-09-28T15:05:41.000Z", "MAPPING_CONFIRMED", "Column mapping confirmed"),
  ev("2026-09-28T15:05:42.000Z", "ANALYSIS_STARTED", "Analysis started", { details: { snapshot: { normalizer_version: NORMALIZER, thresholds: { junk_flag_threshold: 0.7 } } } }),
  ev("2026-09-28T15:05:43.000Z", "CAMPAIGN_VALIDATED", "Campaigns checked in Salesforce: 3 found, 1 not found", { ...system }),
  ev("2026-09-28T15:05:44.000Z", "AI_INVOCATION", "AI call (junk_detection): ok", { ...system, details: { purpose: "junk_detection", outcome: "ok", model_id: "fake-heuristic" } }),
  ev("2026-09-28T15:05:50.000Z", "JOB_STATE_CHANGED", "Moved from analyzing to analysis review", { ...system, before: { state: "ANALYZING" }, after: { state: "ANALYSIS_REVIEW" } }),
];

function rowEvents(): AuditEventView[] {
  return allRows().flatMap((r) => {
    const out: AuditEventView[] = [];
    const changed = Object.entries(r.provenance).filter(([, how]) => how !== "source");
    if (changed.length)
      out.push(
        ev("2026-09-28T15:05:49.000Z", "VALUE_NORMALIZED", `Row ${r.row_id}: ${changed.map(([k]) => label(k)).join(", ")} cleaned up`, {
          ...system,
          row_id: r.row_id,
          after: Object.fromEntries(changed.map(([k]) => [k, r.processed[k] ?? ""])),
          details: { provenance: Object.fromEntries(changed) },
        }),
      );
    if (r.issues.length)
      out.push(
        ev("2026-09-28T15:05:49.500Z", "ISSUE_RAISED", `Row ${r.row_id}: issue found: ${r.issues.map((i) => i.code).join(", ")}`, {
          ...system,
          row_id: r.row_id,
          details: { issues: r.issues.map((i) => ({ code: i.code, field: i.field, severity: i.severity })) },
        }),
      );
    if (r.send && r.send.status !== "not_sent")
      out.push(
        ev(new Date().toISOString(), r.send.status === "submitted" ? "ROW_SUBMITTED" : "ROW_SEND_FAILED",
          r.send.status === "submitted" ? `Row ${r.row_id}: submitted to Eloqua (attempt ${r.send.attempts ?? 1})` : `Row ${r.row_id}: send failed (HTTP 500)`,
          { ...system, row_id: r.row_id }),
      );
    return out;
  });
}

const label = (key: string) => catalog.find((c) => c.key === key)?.label ?? key;

export function mockTimeline(jobId: string, rows: boolean): Timeline {
  const base = jobId === DEMO_JOB_ID ? JOB_EVENTS : [ev(jobs.find((j) => j.job_id === jobId)?.created_at ?? new Date().toISOString(), "JOB_CREATED", "Upload started", { job_id: jobId })];
  const events = rows && jobId === DEMO_JOB_ID ? [...base, ...rowEvents()] : base;
  return { events: [...events].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at)), next_cursor: null };
}

export function mockRowHistory(rowId: number): RowHistory | null {
  const r = allRows().find((x) => x.row_id === rowId);
  if (!r) return null;
  const fields: FieldLineage[] = catalog
    .filter((c) => r.source[c.key] || r.processed[c.key])
    .map((c) => {
      const source = r.source[c.key] ?? "";
      const current = r.processed[c.key] ?? "";
      // The fixture rows don't carry provenance for campaign-derived fields.
      const derived: Record<string, string> = { campaign_name: "derived:sfdc_campaign_name", list_name: "derived:auto_list_name" };
      const how = r.provenance[c.key] ?? derived[c.key] ?? (source ? "source" : "");
      const steps = [
        ...(source ? [{ kind: "source", value: source, label: `column '${c.label}', row ${r.row_id}` }] : []),
        ...(current !== source
          ? [{ kind: how.split(":")[0] || "normalized", value: current, label: PROVENANCE[how] ?? `normalizer (${NORMALIZER})`, at: "2026-09-28T15:05:49.000Z", event_id: "01JDEMOLINEAGE" }]
          : []),
      ];
      return { field: c.key, label: c.label, current, provenance: how || null, provenance_text: PROVENANCE[how] ?? "", steps, explained: true };
    });
  return {
    row_id: r.row_id,
    row_available: true,
    status: r.status,
    excluded: r.excluded,
    normalizer_version: NORMALIZER,
    fields,
    enrichment: null,
    send: r.send ?? null,
    events: rowEvents().filter((e) => e.row_id === rowId),
  };
}

export function mockAuditSearch(f: AuditFilters): AuditSearchResult {
  let events = [...JOB_EVENTS, ...rowEvents()];
  let jobList: AuditSearchResult["jobs"] = [];
  if (f.email) {
    const ids = new Set(
      allRows()
        .filter((r) => (r.processed.email ?? "").toLowerCase() === f.email!.trim().toLowerCase())
        .map((r) => r.row_id),
    );
    events = events.filter((e) => e.row_id != null && ids.has(e.row_id));
    const demo = jobs[0]!;
    if (ids.size) jobList = [{ job_id: demo.job_id, filename: demo.filename, owner_email: demo.owner_email, state: demo.state, created_at: demo.created_at }];
  }
  if (f.job_id) events = events.filter((e) => e.job_id === f.job_id);
  if (f.user) events = events.filter((e) => e.actor.email?.toLowerCase() === f.user!.toLowerCase());
  if (f.event_type) events = events.filter((e) => e.event_type === f.event_type);
  if (f.from) events = events.filter((e) => e.occurred_at >= f.from!);
  if (f.to) events = events.filter((e) => e.occurred_at.slice(0, 10) <= f.to!);
  events.sort((a, b) => b.occurred_at.localeCompare(a.occurred_at));
  return { events, truncated: false, jobs: jobList };
}

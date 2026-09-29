// In-browser stand-in for the gate, send, result and download endpoints (demo mode).
// The first send fails the last row with a fake HTTP 500 so the retry path can be
// demoed; the retry succeeds. The real gate runs on the server (backend/shared/sending.py).
import type { GateResult, Job, JobState, RowProblem, SendConfirmation, SendResult } from "../api/types";
import { allRows } from "./analysisMock";
import { mockEnrichment } from "./enrichmentMock";
import { analysis, catalog, me } from "./fixtures";

// Mirrors CALLABLE_PARAMS in backend/shared/catalog.py (OQ-1 interim rule).
const SENT_KEYS = new Set([
  "first_name",
  "email",
  "lead_source",
  "campaign_id",
  "postal_code",
  "campaign_status",
  "campaign_name",
  "linkedin_url",
]);

let sentAt: string | null = null;
let attempts = 0;

export function resetSendMock() {
  sentAt = null;
  attempts = 0;
}

const sendable = () => allRows().filter((r) => !r.excluded);

export function mockGate(job: Job): GateResult {
  const rows = sendable();
  const reasons: string[] = [];
  const codes: string[] = [];
  const blocked = rows.filter((r) => r.issues.some((i) => i.severity === "blocking"));
  if (blocked.length) {
    codes.push("BLOCKING_ISSUES");
    reasons.push(
      `${blocked.length} ${blocked.length === 1 ? "row still has" : "rows still have"} an issue to fix. Fix or exclude ${blocked.length === 1 ? "it" : "them"} on the analysis page.`,
    );
  }
  const undecided = job.enrich ? mockEnrichment().review.filter((r) => !r.decision).length : 0;
  if (undecided) {
    codes.push("ENRICHMENT_UNDECIDED");
    reasons.push(`${undecided} enrichment ${undecided === 1 ? "match is" : "matches are"} waiting for Apply or Skip.`);
  }
  if (!rows.length) {
    codes.push("NO_ROWS");
    reasons.push("There are no rows to send. Every row is excluded.");
  }
  const groups = new Map<string, number>();
  for (const r of rows) {
    const key = `${r.processed.campaign_id ?? ""}|${r.processed.campaign_status ?? ""}`;
    groups.set(key, (groups.get(key) ?? 0) + 1);
  }
  const by_campaign = [...groups.entries()].sort().map(([key, n]) => {
    const [campaign_id = "", status = ""] = key.split("|");
    const name = analysis.campaigns.find((c) => c.id === campaign_id)?.name;
    return { campaign_id, campaign_name: name ?? campaign_id, status, rows: n };
  });
  const withValues = new Set(rows.flatMap((r) => Object.entries(r.processed).filter(([, v]) => v).map(([k]) => k)));
  return {
    passed: reasons.length === 0,
    reasons,
    reason_codes: codes,
    by_campaign,
    rows_to_send: rows.length,
    campaign_count: new Set(by_campaign.map((c) => c.campaign_id)).size,
    excluded: allRows().length - rows.length,
    not_sent_fields: catalog.filter((c) => !SENT_KEYS.has(c.key) && withValues.has(c.key)).map((c) => c.label),
    sent_fields: catalog.filter((c) => SENT_KEYS.has(c.key)).map((c) => c.label),
    campaigns_stale: false,
    state: job.state,
    can_send: ["ANALYSIS_REVIEW", "ENRICHMENT_REVIEW", "READY_TO_SEND"].includes(job.state),
    send_to_prod: false,
  };
}

/** Returns an error message, or null when the send may start. */
export function mockSend(job: Job, confirmation: SendConfirmation): string | null {
  const gate = mockGate(job);
  if (!gate.can_send) return "This upload has moved on. Refresh the page to see where it is.";
  if (!gate.passed) return "This upload can't be sent yet. Fix the items listed, then try again.";
  const expected: SendConfirmation = {
    rows_to_send: gate.rows_to_send,
    by_campaign: gate.by_campaign.map(({ campaign_id, status, rows }) => ({ campaign_id, status, rows })),
  };
  if (JSON.stringify(expected) !== JSON.stringify(confirmation))
    return "The rows to send changed since you loaded this page. Check the summary again, then send.";
  for (const r of sendable()) r.send = { status: "sending", attempts: 0 };
  return null;
}

/** The workflow finishing: the first attempt fails the last row. */
export function finishSend(): JobState {
  attempts += 1;
  const rows = sendable();
  const last = rows[rows.length - 1];
  for (const r of rows) {
    if (r.send?.status !== "sending") continue;
    const fail = attempts === 1 && r === last;
    r.send = fail
      ? { status: "failed", attempts, error: "HTTP 500 (mock)" }
      : { status: "submitted", attempts };
  }
  sentAt = new Date().toISOString();
  return rows.some((r) => r.send?.status !== "submitted") ? "COMPLETED_WITH_ERRORS" : "COMPLETED";
}

export function mockRetry(): number[] {
  const failed = sendable().filter((r) => r.send?.status === "failed");
  for (const r of failed) r.send = { ...r.send, status: "sending" };
  return failed.map((r) => r.row_id);
}

export function mockResult(job: Job): SendResult {
  if (!sentAt && job.state === "COMPLETED") {
    // A fixture job sent before this session.
    return {
      state: job.state,
      submitted: job.summary?.rows_ready ?? 0,
      failed: [],
      unconfirmed: [],
      campaigns: [{ id: "701000000000001AAA", name: "Demo Conference 2026", rows: job.summary?.rows_ready ?? 0 }],
      submitted_at: job.created_at,
      submitted_by: job.owner_email,
      send_to_prod: false,
      last_error: null,
    };
  }
  const rows = sendable();
  const problems = (status: string): RowProblem[] =>
    rows
      .filter((r) => r.send?.status === status)
      .map((r) => ({ row_id: r.row_id, email: r.processed.email ?? "", reason: r.send?.error ?? "" }));
  const submitted = rows.filter((r) => r.send?.status === "submitted");
  const byCampaign = new Map<string, number>();
  for (const r of submitted) byCampaign.set(r.processed.campaign_id ?? "", (byCampaign.get(r.processed.campaign_id ?? "") ?? 0) + 1);
  return {
    state: job.state,
    submitted: submitted.length,
    failed: problems("failed"),
    unconfirmed: job.state === "SENDING" ? [] : problems("sending"),
    campaigns: [...byCampaign.entries()].map(([id, n]) => ({
      id,
      name: analysis.campaigns.find((c) => c.id === id)?.name ?? id,
      rows: n,
    })),
    submitted_at: sentAt,
    submitted_by: sentAt ? me.email : null,
    send_to_prod: false,
    last_error: null,
  };
}

export function mockDownload(job: Job) {
  const esc = (v: string) => (/[",\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
  const lines = [
    ["_row_id", "processed_email", "processed_company", "_row_status", "_send_status"].join(","),
    ...allRows().map((r) =>
      [String(r.row_id), r.processed.email ?? "", r.processed.company ?? "", r.status, r.send?.status ?? "not_sent"]
        .map(esc)
        .join(","),
    ),
  ];
  const base = job.filename.replace(/\.[^.]+$/, "");
  return {
    url: `data:text/csv;charset=utf-8,${encodeURIComponent(lines.join("\r\n"))}`,
    filename: `${base}-processed.csv`,
    expires_in: 300,
  };
}

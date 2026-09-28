import type { ReactNode } from "react";
import type { Loadable } from "./useApi";
import type { JobSummary, MatchMethod, RowStatus, Severity } from "../api/types";

export function Async<T>({ state, children }: { state: Loadable<T>; children: (data: T) => ReactNode }) {
  if (state.status === "loading") return <p className="muted">Loading…</p>;
  if (state.status === "error")
    return (
      <p role="alert" className="error">
        {state.message}
      </p>
    );
  return <>{children(state.data)}</>;
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <header className="page-header">
      <h1>{title}</h1>
      {children && <div className="page-lede">{children}</div>}
    </header>
  );
}

const methodLabel: Record<MatchMethod, string> = {
  exact: "Exact",
  alias: "Alias",
  ai: "AI suggested",
  none: "Not mapped",
};

export function MethodBadge({ method, confidence }: { method: MatchMethod; confidence: number | null }) {
  const pct = method === "ai" && confidence != null ? ` (${Math.round(confidence * 100)}%)` : "";
  return <span className={`badge badge-${method}`}>{methodLabel[method] + pct}</span>;
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`badge sev-${severity}`}>{severity}</span>;
}

const statusLabel: Record<RowStatus, string> = {
  ready: "Ready",
  warning: "Warning",
  blocked: "Blocked",
  excluded: "Excluded",
  pending_enrichment: "Pending enrichment",
};

export function StatusBadge({ status }: { status: RowStatus }) {
  return <span className={`badge status-${status}`}>{statusLabel[status]}</span>;
}

export function Stat({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className={`stat ${tone ? `stat-${tone}` : ""}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function SummaryCards({ summary }: { summary: JobSummary }) {
  return (
    <section className="stats" aria-label="Row summary">
      <Stat label="Rows total" value={summary.rows_total} />
      <Stat label="Ready" value={summary.rows_ready} tone="ok" />
      <Stat label="Warnings" value={summary.rows_warning} tone="warn" />
      <Stat label="Blocked" value={summary.rows_blocked} tone="bad" />
      <Stat label="Excluded" value={summary.rows_excluded} />
      <Stat label="Pending enrichment" value={summary.rows_pending_enrichment} tone="info" />
    </section>
  );
}

import type { AuditEventView } from "../api/types";

const fmt = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "medium",
  timeZone: "America/Chicago",
});

export function formatTime(iso: string | null | undefined): string {
  return iso ? `${fmt.format(new Date(iso))} CT` : "";
}

function who(e: AuditEventView): string {
  if (e.actor.type === "system") return "System";
  return e.actor.email ?? e.actor.type;
}

function Payload({ e }: { e: AuditEventView }) {
  const parts: [string, unknown][] = [
    ["Before", e.before],
    ["After", e.after],
    ["Details", e.details],
    ["Subject", e.subject],
  ];
  const shown = parts.filter(([, v]) => v != null && Object.keys(v as object).length > 0);
  return (
    <details className="event-details">
      <summary>Details</summary>
      <dl>
        <div>
          <dt>Event</dt>
          <dd>
            <code>{e.event_type}</code>
            {e.reason && (
              <>
                {" "}
                · reason <code>{e.reason}</code>
              </>
            )}
          </dd>
        </div>
        {shown.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              <pre>{JSON.stringify(value, null, 2)}</pre>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/** Chronological audit events: time, who, a plain-English line, expandable details. */
export function EventList({ events, showJob = false }: { events: AuditEventView[]; showJob?: boolean }) {
  if (!events.length) return <p className="muted">No events.</p>;
  return (
    <ol className="events">
      {events.map((e) => (
        <li key={e.event_id}>
          <div className="event-head">
            <time dateTime={e.occurred_at}>{formatTime(e.occurred_at)}</time>
            <span className="muted"> · {who(e)}</span>
            {showJob && e.job_id && (
              <span className="muted">
                {" "}
                · <code>{e.job_id}</code>
                {e.row_id != null && ` row ${e.row_id}`}
              </span>
            )}
          </div>
          <div className="event-summary">{e.summary}</div>
          <Payload e={e} />
        </li>
      ))}
    </ol>
  );
}

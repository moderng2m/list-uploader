import { useEffect, useRef } from "react";
import { api } from "../api/client";
import type { FieldLineage, RowHistory } from "../api/types";
import { EventList, formatTime } from "./EventList";
import { Async } from "./ui";
import { useApi } from "./useApi";

function Lineage({ f }: { f: FieldLineage }) {
  return (
    <li className={f.explained ? "" : "unexplained"}>
      <strong>{f.label}</strong>
      <ol className="lineage" aria-label={`${f.label} lineage`}>
        {f.steps.map((s, i) => (
          <li key={i}>
            <span className="lineage-value">{s.value ? `'${s.value}'` : <em>blank</em>}</span>{" "}
            <span className="muted small">
              ({s.label}
              {s.at && `, ${formatTime(s.at)}`})
            </span>
          </li>
        ))}
      </ol>
      {!f.explained && <p className="error small">This value can't be traced back fully. Check the events below.</p>}
    </li>
  );
}

function HistoryBody({ h }: { h: RowHistory }) {
  const send = h.send;
  return (
    <>
      {!h.row_available && (
        <p className="card note" role="note">
          This row's working data has expired. Its audit events are still here.
        </p>
      )}
      {h.row_available && (
        <section aria-label="Value lineage">
          <h3>Where each value came from</h3>
          <p className="muted small">Normalizer: {h.normalizer_version}</p>
          <ul className="plain lineage-list">
            {h.fields.map((f) => (
              <Lineage key={f.field} f={f} />
            ))}
          </ul>
        </section>
      )}
      {send && send.status !== "not_sent" && (
        <section aria-label="Send result">
          <h3>Send</h3>
          <p>
            {send.status === "submitted"
              ? `Submitted to Eloqua${send.submitted_at ? ` ${formatTime(send.submitted_at)}` : ""}`
              : send.status === "failed"
                ? `Failed: ${send.error ?? `HTTP ${send.http_status}`}`
                : send.status === "sending"
                  ? "Posted but not confirmed"
                  : send.status}
            {send.attempts ? ` (attempt ${send.attempts})` : ""}
          </p>
        </section>
      )}
      <section aria-label="Row events">
        <h3>Events</h3>
        <EventList events={h.events} />
      </section>
    </>
  );
}

/** Side panel with one row's value lineage, send result and audit events (SPEC §21.2.6). */
export function RowHistoryDrawer({ jobId, rowId, onClose }: { jobId: string; rowId: number; onClose: () => void }) {
  const state = useApi(() => api.getRowHistory(jobId, rowId), `history:${jobId}:${rowId}`);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`History of row ${rowId}`}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="drawer-head">
          <h2>Row {rowId} history</h2>
          <button type="button" ref={closeRef} onClick={onClose}>
            Close
          </button>
        </header>
        <Async state={state}>{(h) => <HistoryBody h={h} />}</Async>
      </aside>
    </div>
  );
}

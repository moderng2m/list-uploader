import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { AuditEventView } from "../api/types";
import { EventList } from "../components/EventList";
import { PageHeader } from "../components/ui";

/** Every recorded event for one upload (SPEC §6.7, §21.2.6). */
export function TimelinePage() {
  const { jobId = "" } = useParams();
  const [rows, setRows] = useState(false);
  const [events, setEvents] = useState<AuditEventView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load(from: string | null, replace: boolean) {
    setLoading(true);
    setError(null);
    try {
      const page = await api.getTimeline(jobId, { rows, cursor: from });
      setEvents((prev) => (replace ? page.events : [...prev, ...page.events]));
      setCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(null, true);
  }, [jobId, rows]);

  return (
    <>
      <PageHeader title="Timeline">Everything that happened to this upload, oldest first.</PageHeader>
      <section className="card">
        <label className="check">
          <input type="checkbox" checked={rows} onChange={(e) => setRows(e.target.checked)} /> Include per-row
          events (values cleaned up, issues, sends)
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {loading && !events.length ? <p className="muted">Loading…</p> : <EventList events={events} />}
        {cursor && (
          <div className="actions">
            <button type="button" onClick={() => void load(cursor, false)} disabled={loading}>
              {loading ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </section>
    </>
  );
}

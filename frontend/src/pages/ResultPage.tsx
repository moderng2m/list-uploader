import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { Async, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

const fmt = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "America/Chicago",
});

export function ResultPage() {
  const { jobId = "" } = useParams();
  const state = useApi(() => api.getResult(jobId), `result:${jobId}`);
  return (
    <>
      <PageHeader title="Result" />
      <Async state={state}>
        {(r) => (
          <>
            <section className="card">
              <p className="big">
                {r.submitted} leads submitted to Eloqua for{" "}
                {r.campaigns.map((c) => `${c.name} (${c.rows})`).join(", ")} on{" "}
                {fmt.format(new Date(r.submitted_at))} CT by {r.submitted_by}.
              </p>
              <p className="muted">
                "Submitted" means Eloqua accepted the form post. It doesn't yet confirm the contact was
                created.
              </p>
              <button type="button" disabled title="Available in P5">
                Download processed file
              </button>
            </section>
            {r.failed.length > 0 && (
              <section className="card blocked">
                <h2>{r.failed.length} rows failed</h2>
                <ul>
                  {r.failed.map((f) => (
                    <li key={f.row_id}>
                      Row {f.row_id} ({f.email}): {f.reason}
                    </li>
                  ))}
                </ul>
                <button type="button" disabled title="Available in P5">
                  Retry failed rows
                </button>
              </section>
            )}
          </>
        )}
      </Async>
    </>
  );
}

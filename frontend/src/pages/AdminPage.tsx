import { api } from "../api/client";
import { Async, PageHeader } from "../components/ui";
import { useApi } from "../components/useApi";

export function AdminPage() {
  const sources = useApi(api.leadSources, "lead-sources");
  const config = useApi(api.adminConfig, "admin-config");
  return (
    <>
      <PageHeader title="Admin">Changes here are audited.</PageHeader>
      <div className="two-col">
        <section className="card">
          <h2>Lead sources</h2>
          <Async state={sources}>
            {(list) => (
              <ul className="plain">
                {list.map((s) => (
                  <li key={s.value} className={s.active ? "" : "muted"}>
                    {s.value} {!s.active && <em>(deactivated)</em>}
                  </li>
                ))}
              </ul>
            )}
          </Async>
        </section>
        <section className="card">
          <h2>Thresholds</h2>
          <Async state={config}>
            {(c) => (
              <dl>
                {Object.entries(c.thresholds).map(([k, v]) => (
                  <div key={k} className="kv">
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
            )}
          </Async>
        </section>
      </div>
    </>
  );
}

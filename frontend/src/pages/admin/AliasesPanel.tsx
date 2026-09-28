import { useState } from "react";
import { api } from "../../api/client";
import type { AiMapping, FieldAliases } from "../../api/types";
import { useApi } from "../../components/useApi";
import { ErrorLine } from "./ErrorLine";
import { useVersioned } from "./useVersioned";

function FieldRow({
  f,
  busy,
  onSave,
}: {
  f: FieldAliases;
  busy: boolean;
  onSave: (aliases: string[]) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState("");
  return (
    <tr>
      <th scope="row">{f.label}</th>
      <td>
        <ul className="chips" aria-label={`Aliases for ${f.label}`}>
          {f.aliases.map((a) => (
            <li key={a}>
              {a}
              <button
                type="button"
                aria-label={`Remove ${a} from ${f.label}`}
                disabled={busy}
                onClick={() => void onSave(f.aliases.filter((x) => x !== a))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
        <form
          className="inline-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await onSave([...f.aliases, draft])) setDraft("");
          }}
        >
          <input aria-label={`New alias for ${f.label}`} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button type="submit" disabled={busy || !draft.trim()}>
            Add
          </button>
        </form>
      </td>
    </tr>
  );
}

export function AliasesPanel() {
  const { data, error, busy, change, reload } = useVersioned(api.aliases);
  const [tick, setTick] = useState(0);
  const suggestions = useApi(api.aiMappings, `ai-mappings:${tick}`);
  if (!data) return error ? <ErrorLine error={error} /> : <p className="muted">Loading…</p>;

  async function promote(m: AiMapping) {
    if (await change(() => api.promoteAlias(data!.version, m.source_header, m.field_key))) setTick((t) => t + 1);
  }

  return (
    <>
      <section className="card" aria-label="AI matches to keep">
        <h2>AI column matches users kept</h2>
        <p className="muted small">Save one as an alias and future files with that header map without AI.</p>
        {suggestions.status === "ready" &&
          (suggestions.data.items.length === 0 ? (
            <p className="muted">Nothing to review.</p>
          ) : (
            <div className="table-wrap">
              <table className="grid compact">
                <thead>
                  <tr>
                    <th>Header in the file</th>
                    <th>Mapped to</th>
                    <th>Uploads</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {suggestions.data.items.map((m) => (
                    <tr key={`${m.source_header}:${m.field_key}`}>
                      <td>{m.source_header}</td>
                      <td>{m.field_label}</td>
                      <td>{m.times}</td>
                      <td>
                        <button type="button" onClick={() => void promote(m)} disabled={busy}>
                          Save as alias
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
      </section>
      <section className="card" aria-label="Field aliases">
        <h2>Field aliases</h2>
        <p className="muted small">
          A header matches a field when it equals the field's name or one of these aliases (case, spaces and
          punctuation ignored).
        </p>
        <ErrorLine error={error} onReload={reload} />
        <div className="table-wrap">
          <table className="grid compact">
            <tbody>
              {data.fields.map((f) => (
                <FieldRow
                  key={f.key}
                  f={f}
                  busy={busy}
                  onSave={(aliases) => change(() => api.replaceAliases(data.version, f.key, aliases))}
                />
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

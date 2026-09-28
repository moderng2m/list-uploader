import { useState } from "react";
import { api } from "../../api/client";
import type { LeadSourceItem } from "../../api/types";
import { ErrorLine } from "./ErrorLine";
import { useVersioned } from "./useVersioned";

function Item({
  item,
  first,
  last,
  busy,
  onRename,
  onToggle,
  onMove,
}: {
  item: LeadSourceItem;
  first: boolean;
  last: boolean;
  busy: boolean;
  onRename: (value: string) => Promise<boolean>;
  onToggle: () => void;
  onMove: (by: -1 | 1) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.value);
  return (
    <li className={item.active ? "" : "muted"} aria-label={item.value}>
      {editing ? (
        <form
          className="inline-form"
          onSubmit={async (e) => {
            e.preventDefault();
            if (await onRename(draft)) setEditing(false);
          }}
        >
          <input aria-label={`New name for ${item.value}`} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button type="submit" className="primary" disabled={busy}>
            Save
          </button>
          <button type="button" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <div className="ls-row">
          <span className="ls-value">
            {item.value} {!item.active && <em>(deactivated)</em>}
          </span>
          <span className="ls-actions">
            <button type="button" onClick={() => onMove(-1)} disabled={busy || first} aria-label={`Move ${item.value} up`}>
              ↑
            </button>
            <button type="button" onClick={() => onMove(1)} disabled={busy || last} aria-label={`Move ${item.value} down`}>
              ↓
            </button>
            <button type="button" onClick={() => (setDraft(item.value), setEditing(true))} disabled={busy}>
              Rename
            </button>
            <button type="button" onClick={onToggle} disabled={busy}>
              {item.active ? "Deactivate" : "Reactivate"}
            </button>
          </span>
        </div>
      )}
    </li>
  );
}

export function LeadSourcesPanel() {
  const { data, error, busy, change, reload } = useVersioned(api.leadSources);
  const [value, setValue] = useState("");
  if (!data) return error ? <ErrorLine error={error} /> : <p className="muted">Loading…</p>;
  const { version, items } = data;

  function move(index: number, by: -1 | 1) {
    const ids = items.map((i) => i.id);
    [ids[index], ids[index + by]] = [ids[index + by]!, ids[index]!];
    void change(() => api.reorderLeadSources(version, ids));
  }

  return (
    <section className="card" aria-label="Lead sources">
      <h2>Lead sources</h2>
      <p className="muted small">
        Uploads can only use active values. Deactivating never deletes: past uploads keep the list they were
        checked against.
      </p>
      <ErrorLine error={error} onReload={reload} />
      <ul className="plain ls-list">
        {items.map((item, i) => (
          <Item
            key={item.id}
            item={item}
            first={i === 0}
            last={i === items.length - 1}
            busy={busy}
            onRename={(v) => change(() => api.changeLeadSource(version, item.id, { value: v }))}
            onToggle={() => void change(() => api.changeLeadSource(version, item.id, { active: !item.active }))}
            onMove={(by) => move(i, by)}
          />
        ))}
      </ul>
      <form
        className="inline-form"
        onSubmit={async (e) => {
          e.preventDefault();
          if (await change(() => api.addLeadSource(version, value))) setValue("");
        }}
      >
        <input aria-label="New lead source" placeholder="e.g. Marketing: Podcast" value={value} onChange={(e) => setValue(e.target.value)} />
        <button type="submit" className="primary" disabled={busy || !value.trim()}>
          Add
        </button>
      </form>
    </section>
  );
}

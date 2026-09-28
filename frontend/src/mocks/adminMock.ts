// In-browser stand-in for the admin settings endpoints (demo mode). Mirrors the
// backend's optimistic versioning: a change based on an old version gets 409.
import type { AdminConfig, AiMapping, Aliases, LeadSourceItem, LeadSources } from "../api/types";
import { adminConfig, catalog } from "./fixtures";

// A few illustrative aliases; the real seed set is in backend/shared/catalog.py.
const SEED_ALIASES: Record<string, string[]> = {
  email: ["email", "e-mail", "work email"],
  first_name: ["first", "given name"],
  last_name: ["last", "surname"],
  company: ["company name", "organization", "account"],
  title: ["job title", "position"],
};

const SEED_SOURCES = ["Marketing: Events", "Marketing: Webinar", "Marketing: Content Syndication", "Sales: Outbound"];

let counter = 0;
let sources: LeadSources;
let config: AdminConfig;
let aliases: Aliases;
let aiMappings: AiMapping[];

export function resetAdminMock() {
  counter = 0;
  sources = {
    version: "seed",
    items: SEED_SOURCES.map((value, order) => ({ id: `ls_seed_${order}`, value, active: true, order })),
  };
  config = structuredClone(adminConfig);
  aliases = {
    version: "seed",
    fields: catalog.map((c) => ({ key: c.key, label: c.label, aliases: SEED_ALIASES[c.key] ?? [] })),
  };
  aiMappings = [
    {
      source_header: "Job Position",
      field_key: "title",
      field_label: "Title",
      times: 3,
      job_ids: ["j_01JDEMO0000000000000000000"],
      last_at: "2026-09-28T15:06:00Z",
    },
  ];
}
resetAdminMock();

export class Conflict extends Error {
  status = 409;
}
export class Invalid extends Error {
  status = 400;
}

const nextVersion = () => `v${++counter}`;
const key = (v: string) => v.replace(/\s+/g, " ").trim().toLowerCase();

function check(version: string, current: string) {
  if (version !== current)
    throw new Conflict(
      "Someone else changed this setting while you were editing. Reload the page to see the latest version, then make your change again.",
    );
}

export const mockAdmin = {
  config: () => config,
  thresholds(version: string, values: Record<string, number>) {
    check(version, config.version);
    for (const [k, v] of Object.entries(values)) {
      if (!(k in config.thresholds)) throw new Invalid(`'${k}' isn't a setting you can change here.`);
      if (typeof v !== "number" || Number.isNaN(v) || v < 0 || v > 1)
        throw new Invalid(`${k} must be a number between 0 and 1.`);
    }
    const next = { ...config.thresholds, ...values };
    if ((next.junk_block_threshold ?? 0) < (next.junk_flag_threshold ?? 0))
      throw new Invalid(
        "The junk block threshold must be at least the junk flag threshold, or flagged rows could be blocked without ever being flagged.",
      );
    config = { ...config, thresholds: next, version: nextVersion() };
    return config;
  },
  sources: () => sources,
  addSource(version: string, value: string) {
    check(version, sources.version);
    const clean = value.replace(/\s+/g, " ").trim();
    if (!clean) throw new Invalid("Enter a lead source value.");
    const dup = sources.items.find((i) => key(i.value) === key(clean));
    if (dup) throw new Invalid(`'${dup.value}' is already in the list.`);
    const item: LeadSourceItem = { id: `ls_${Date.now()}_${counter}`, value: clean, active: true, order: sources.items.length };
    sources = { version: nextVersion(), items: [...sources.items, item] };
    return sources;
  },
  changeSource(version: string, id: string, change: { value?: string; active?: boolean }) {
    check(version, sources.version);
    const value = change.value?.replace(/\s+/g, " ").trim();
    if (value !== undefined) {
      if (!value) throw new Invalid("Enter a lead source value.");
      const dup = sources.items.find((i) => i.id !== id && key(i.value) === key(value));
      if (dup) throw new Invalid(`'${dup.value}' is already in the list.`);
    }
    sources = {
      version: nextVersion(),
      items: sources.items.map((i) =>
        i.id === id ? { ...i, value: value ?? i.value, active: change.active ?? i.active } : i,
      ),
    };
    return sources;
  },
  reorderSources(version: string, ids: string[]) {
    check(version, sources.version);
    const byId = new Map(sources.items.map((i) => [i.id, i]));
    sources = { version: nextVersion(), items: ids.map((id, order) => ({ ...byId.get(id)!, order })) };
    return sources;
  },
  aliases: () => aliases,
  replaceAliases(version: string, fieldKey: string, list: string[]) {
    check(version, aliases.version);
    const norm = (a: string) => a.toLowerCase().replace(/[^\w\s-]/g, " ").replace(/_/g, " ").replace(/\s+/g, " ").trim();
    for (const a of list) {
      if (!norm(a)) throw new Invalid("Aliases can't be blank.");
      const other = aliases.fields.find((f) => f.key !== fieldKey && f.aliases.some((x) => norm(x) === norm(a)));
      if (other) throw new Invalid(`'${a.trim()}' already maps to ${other.label}. Remove it there first.`);
    }
    aliases = {
      version: nextVersion(),
      fields: aliases.fields.map((f) => (f.key === fieldKey ? { ...f, aliases: list.map((a) => a.trim()) } : f)),
    };
    aiMappings = aiMappings.filter(
      (m) => !aliases.fields.some((f) => f.aliases.some((a) => norm(a) === norm(m.source_header))),
    );
    return aliases;
  },
  aiMappings: () => ({ items: aiMappings }),
  promote(version: string, header: string, fieldKey: string) {
    if (!aiMappings.some((m) => m.source_header === header && m.field_key === fieldKey))
      throw new Invalid("Only a column the AI matched, and the user kept, can be saved as an alias.");
    const field = aliases.fields.find((f) => f.key === fieldKey)!;
    return this.replaceAliases(version, fieldKey, [...field.aliases, header]);
  },
};

import { useState } from "react";
import { PageHeader } from "../components/ui";
import { AliasesPanel } from "./admin/AliasesPanel";
import { AuditSearchPanel } from "./admin/AuditSearchPanel";
import { LeadSourcesPanel } from "./admin/LeadSourcesPanel";
import { ThresholdsPanel } from "./admin/ThresholdsPanel";

const TABS = [
  { key: "lead-sources", label: "Lead sources", panel: LeadSourcesPanel },
  { key: "thresholds", label: "Thresholds", panel: ThresholdsPanel },
  { key: "aliases", label: "Field aliases", panel: AliasesPanel },
  { key: "audit", label: "Audit search", panel: AuditSearchPanel },
] as const;

export function AdminPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("lead-sources");
  const Panel = TABS.find((t) => t.key === tab)!.panel;
  return (
    <>
      <PageHeader title="Admin">Every change here is recorded with who made it and what it was before.</PageHeader>
      <div className="tabs" role="tablist" aria-label="Admin sections">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>
      <Panel />
    </>
  );
}

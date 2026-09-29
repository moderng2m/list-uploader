// In-browser stand-in for enrichment decisions (demo mode).
import type { Enrichment, EnrichmentDecision } from "../api/types";
import { enrichment } from "./fixtures";

let state: Enrichment = structuredClone(enrichment);

export function resetEnrichmentMock() {
  state = structuredClone(enrichment);
}

export function mockEnrichment(): Enrichment {
  return state;
}

export function mockDecide(body: { decisions?: { row_id: number; decision: EnrichmentDecision }[]; skip_all?: boolean }) {
  const decided: number[] = [];
  state = {
    ...state,
    review: state.review.map((item) => {
      const choice = body.skip_all
        ? item.decision ? null : "skip"
        : body.decisions?.find((d) => d.row_id === item.row_id)?.decision ?? null;
      if (!choice) return item;
      decided.push(item.row_id);
      return { ...item, decision: choice };
    }),
  };
  return { decided_row_ids: decided };
}

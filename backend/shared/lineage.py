"""Per-field value lineage for one row (SPEC §21.2.6 row history drawer).

Built from the row as stored plus its audit events, oldest first:

    source cell -> every event that set the field (normalizer, derivation,
    auto-correction, enrichment, user edit) -> the current processed value

Row events are written only when a processed value changes, and a user edit is
recorded as the typed value (USER_EDIT), which is then normalized like a source
value. So when the last recorded step differs from the current value, the
normalizer made that last change, and a final "normalized" step says so.

A field counts as explained when its lineage ends at the current value and
starts at a source cell or at a recorded event (derived fields have no cell).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from shared.audit_text import provenance_text
from shared.catalog import FIELDS

# Events whose `after` holds processed (or typed) values for the fields they list.
VALUE_EVENTS = {
    "VALUE_NORMALIZED": "normalized",
    "VALUE_AUTO_CORRECTED": "auto_corrected",
    "VALUE_DERIVED": "derived",
    "ENRICHMENT_RESULT": "enrichment",
    "USER_EDIT": "user_edit",
    "SUGGESTION_ACCEPTED": "suggestion_accepted",
}


def _step(kind: str, value: Any, label: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "value": "" if value is None else str(value), "label": label, **extra}


def _event_label(event: Mapping[str, Any], field: str, kind: str, normalizer: str) -> str:
    actor = (event.get("actor") or {}).get("email")
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    reason = ((details or {}).get("provenance") or {}).get(field) or event.get("reason") or ""
    if kind == "user_edit":
        return f"edited by {actor}" if actor else "edited"
    if kind == "suggestion_accepted":
        return f"AI suggestion accepted by {actor}" if actor else "AI suggestion accepted"
    if kind == "normalized":
        return f"normalizer ({normalizer})"
    # Grouped events list every field's reason; keep the readable ones.
    texts = [provenance_text(r) for r in reason.split(",") if r]
    return "; ".join(dict.fromkeys(t for t in texts if t)) or kind.replace("_", " ")


def field_lineage(
    row: Mapping[str, Any],
    mapping: Mapping[str, str],
    events: Sequence[Mapping[str, Any]],
    *,
    normalizer: str,
) -> list[dict[str, Any]]:
    header_for = {key: header for header, key in mapping.items()}
    source: Mapping[str, Any] = row.get("source") or {}
    processed: Mapping[str, Any] = row.get("processed") or {}
    provenance: Mapping[str, Any] = row.get("provenance") or {}
    ordered = sorted(events, key=lambda e: str(e.get("sk") or e.get("occurred_at") or ""))

    out: list[dict[str, Any]] = []
    for f in FIELDS:
        header = header_for.get(f.key)
        cell = str(source.get(header, "")) if header else ""
        current = str(processed.get(f.key, "") or "")
        steps: list[dict[str, Any]] = []
        if header is not None:
            steps.append(
                _step("source", cell, f"column '{header}', row {row['row_id']}", column=header)
            )
        for e in ordered:
            kind = VALUE_EVENTS.get(str(e.get("event_type")))
            after = e.get("after") or {}
            if kind is None or f.key not in after:
                continue
            value = after.get(f.key)
            if steps and steps[-1]["value"] == str(value or "") and kind != "user_edit":
                continue
            steps.append(
                _step(
                    kind,
                    value,
                    _event_label(e, f.key, kind, normalizer),
                    at=e.get("occurred_at"),
                    event_id=e.get("event_id"),
                )
            )
        if not current and not any(s["value"] for s in steps):
            continue  # nothing in the file and nothing processed
        if not steps or steps[-1]["value"] != current:
            how = str(provenance.get(f.key, ""))
            kind = "cleared" if not current else "normalized"
            label = "cleared" if not current else f"normalizer ({normalizer})"
            if how and how not in ("normalized", "user_edit", "source") and current:
                label = provenance_text(how)
            steps.append(_step(kind, current, label))
        out.append(
            {
                "field": f.key,
                "label": f.label,
                "current": current,
                "provenance": provenance.get(f.key),
                "provenance_text": provenance_text(provenance.get(f.key)),
                "steps": steps,
                "explained": bool(steps)
                and steps[-1]["value"] == current
                and (steps[0]["kind"] == "source" or "event_id" in steps[0]),
            }
        )
    return out

"""A deterministic stand-in for Bedrock used when INTEGRATIONS=fake.

It reads the JSON payload the real prompt carries and answers with simple
string-similarity guesses, so the dev deployment shows realistic
"AI suggested" results without calling a model. It is not a quality bar for
the real prompt.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from shared.bedrock_client import BedrockClient, RawResponse
from shared.catalog import SEED_ALIASES, normalize_header

_INPUT = re.compile(r"<input>\s*(.*?)\s*</input>", re.DOTALL)


def _score(header: str, field: dict[str, Any]) -> float:
    h = normalize_header(header)
    candidates = [
        normalize_header(field["label"]),
        field["field_key"].replace("_", " "),
        *(normalize_header(a) for a in SEED_ALIASES.get(field["field_key"], ())),
    ]
    best = 0.0
    for cand in candidates:
        ratio = SequenceMatcher(None, h, cand).ratio()
        # A shared whole word ("job position" vs "position") or a prefix ("org" of
        # "organization") is a strong hint.
        if set(h.split()) & set(cand.split()) or cand.startswith(h) or h.startswith(cand):
            ratio = max(ratio, 0.8)
        best = max(best, ratio)
    return best


class HeuristicFakeBedrock(BedrockClient):
    def __init__(self) -> None:
        super().__init__("fake-heuristic")

    def _invoke_raw(self, system: str, prompt: str, schema: dict[str, Any]) -> RawResponse:
        match = _INPUT.search(prompt)
        payload = json.loads(match.group(1)) if match else {}
        if "rows" in payload:
            items = _junk(payload["rows"])
        elif "values" in payload:
            items = _lead_sources(payload["values"], payload.get("allowed", []))
        else:
            items = _mapping(payload)
        return RawResponse(text=json.dumps({"items": items}), input_tokens=0, output_tokens=0)


_JUNK_WORDS = {"test", "asdf", "n/a", "none", "xxx", "qwerty", "fake", "sample", "-", "."}


def _junk(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        for field in ("first_name", "last_name", "company", "title"):
            value = str(row.get(field, "")).strip().casefold()
            if value in _JUNK_WORDS or (len(value) >= 3 and len(set(value)) == 1):
                items.append({
                    "row_id": row["row_id"], "field": field, "reason_code": "placeholder",
                    "confidence": 0.95, "explanation": "Looks like a placeholder (demo heuristic).",
                })  # fmt: skip
        local = str(row.get("email", "")).partition("@")[0].casefold()
        if local in {"test", "asdf", "noreply"}:
            items.append({
                "row_id": row["row_id"], "field": "email", "reason_code": "test_record",
                "confidence": 0.9, "explanation": "Looks like a test address (demo heuristic).",
            })  # fmt: skip
    return items


def _lead_sources(values: list[str], allowed: list[str]) -> list[dict[str, Any]]:
    items = []
    for value in values:
        scored = sorted(
            ((SequenceMatcher(None, value.casefold(), a.casefold()).ratio(), a) for a in allowed),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.5:
            items.append({"input": value, "match": scored[0][1],
                          "confidence": round(min(0.97, scored[0][0] + 0.2), 2)})  # fmt: skip
        else:
            items.append({"input": value, "match": None, "confidence": 0.1})
    return items


def _mapping(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    fields = payload.get("allowed_fields", [])
    for col in payload.get("columns", []):
        header = col["source_header"]
        scored = sorted(((_score(header, f), f) for f in fields), key=lambda p: p[0], reverse=True)
        if scored and scored[0][0] >= 0.6:
            score, best = scored[0]
            items.append(
                {
                    "source_header": header,
                    "field_key": best["field_key"],
                    "confidence": round(min(0.95, score), 2),
                    "reason": "Header resembles the field name (demo heuristic).",
                }
            )
        else:
            items.append(
                {
                    "source_header": header,
                    "field_key": None,
                    "confidence": 0.2,
                    "reason": "No field looks like a match (demo heuristic).",
                }
            )
    return items

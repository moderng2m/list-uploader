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
        items = []
        for col in payload.get("columns", []):
            fields = payload.get("allowed_fields", [])
            scored = sorted(((_score(col["source_header"], f), f) for f in fields),
                            key=lambda pair: pair[0], reverse=True)  # fmt: skip
            if scored and scored[0][0] >= 0.6:
                score, best = scored[0]
                items.append({
                    "source_header": col["source_header"],
                    "field_key": best["field_key"],
                    "confidence": round(min(0.95, score), 2),
                    "reason": "Header resembles the field name (demo heuristic).",
                })  # fmt: skip
            else:
                items.append({
                    "source_header": col["source_header"],
                    "field_key": None,
                    "confidence": 0.2,
                    "reason": "No field looks like a match (demo heuristic).",
                })  # fmt: skip
        return RawResponse(text=json.dumps({"items": items}), input_tokens=0, output_tokens=0)

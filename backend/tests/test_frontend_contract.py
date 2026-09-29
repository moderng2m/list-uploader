"""The frontend's mock catalog must match the real one, or demo mode drifts from reality."""

from __future__ import annotations

import re
from pathlib import Path

from shared.catalog import FIELDS

FIXTURES_TS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "mocks" / "fixtures.ts"
_ENTRY = re.compile(
    r'\{ key: "(?P<key>\w+)", label: "(?P<label>[^"]+)",.*?required: (?P<required>true|false), '
    r"must_map: (?P<must_map>true|false)"
)


def test_mock_catalog_matches_backend() -> None:
    text = FIXTURES_TS.read_text()
    block = text[text.index("export const catalog") : text.index("export const mapping")]
    mock = [
        (m["key"], m["label"], m["required"] == "true", m["must_map"] == "true")
        for m in _ENTRY.finditer(block)
    ]
    real = [(f.key, f.label, f.required, f.must_map) for f in FIELDS]
    assert sorted(mock) == sorted(real)

"""The downloadable processed file (SPEC §7.4).

Columns: the original source columns in file order, then processed_<field> for every
catalog field, then _row_id, _row_status, _issues, _enrichment_status, _send_status.

Values that a spreadsheet would run as a formula are prefixed with an apostrophe
(CSV formula injection). Phone numbers such as "+15550100100" are left alone.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from shared.catalog import FIELDS

_NUMBERISH = re.compile(r"^[+-]?[\d\s().-]+$")


def safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text
    first = text[0]
    if first in ("=", "@", "\t", "\r") or (first in ("+", "-") and not _NUMBERISH.match(text)):
        return "'" + text
    return text


def header(source_headers: Sequence[str]) -> list[str]:
    return [
        *source_headers,
        *(f"processed_{f.key}" for f in FIELDS),
        "_row_id",
        "_row_status",
        "_issues",
        "_enrichment_status",
        "_send_status",
    ]


def build_csv(source_headers: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([safe_cell(h) for h in header(source_headers)])
    for row in sorted(rows, key=lambda r: r["row_id"]):
        source = row.get("source") or {}
        processed = row.get("processed") or {}
        issues = "; ".join(
            f"{i['code']}{':' + i['field'] if i.get('field') else ''}"
            for i in row.get("issues", [])
        )
        writer.writerow(
            [
                *(safe_cell(source.get(h, "")) for h in source_headers),
                *(safe_cell(processed.get(f.key, "")) for f in FIELDS),
                row["row_id"],
                row.get("status", ""),
                issues,
                (row.get("enrichment") or {}).get("status", "not_attempted"),
                (row.get("send") or {}).get("status", "not_sent"),
            ]
        )
    # UTF-8 with BOM so Excel opens accented names correctly.
    return buf.getvalue().encode("utf-8-sig")

"""Admin-editable settings in the Config table (SPEC §10.2, §14.4).

Reads fall back to the seed values in code, so a fresh environment works
before any admin edits. Admin editing lands in P6.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import boto3

from shared import config_defaults
from shared.catalog import SEED_ALIASES, normalize_header
from shared.jobs import plain

ALIASES_PK = "field_aliases"
THRESHOLDS_PK = "thresholds"
CURRENT = "current"


@dataclass(frozen=True)
class AliasSet:
    version: str
    by_field: dict[str, tuple[str, ...]]

    def lookup(self) -> dict[str, str]:
        """normalized alias -> field key."""
        out: dict[str, str] = {}
        for key, aliases in self.by_field.items():
            for alias in aliases:
                out.setdefault(normalize_header(alias), key)
        return out


SEED_ALIAS_SET = AliasSet(version="seed", by_field=dict(SEED_ALIASES))


class ConfigStore:
    def __init__(self, table_name: str | None = None, *, dynamodb_resource: Any = None) -> None:
        name = table_name or os.environ.get("CONFIG_TABLE")
        self._table = (
            (dynamodb_resource or boto3.resource("dynamodb")).Table(name) if name else None
        )

    def _get(self, pk: str) -> dict[str, Any] | None:
        if self._table is None:
            return None
        item = self._table.get_item(Key={"pk": pk, "sk": CURRENT}).get("Item")
        return plain(item) if item else None

    def aliases(self) -> AliasSet:
        item = self._get(ALIASES_PK)
        if not item:
            return SEED_ALIAS_SET
        return AliasSet(
            version=str(item.get("version", "unknown")),
            by_field={k: tuple(v) for k, v in item.get("aliases", {}).items()},
        )

    def thresholds(self) -> dict[str, float]:
        item = self._get(THRESHOLDS_PK)
        values = dict(config_defaults.THRESHOLDS)
        if item:
            values.update({k: float(v) for k, v in item.get("values", {}).items()})
        return values

"""Admin-editable settings in the Config table (SPEC §6.8, §10.2, §14.4).

Reads fall back to the seed values in code, so a fresh environment works
before any admin edits. Each setting is one item (`sk = current`) with a
`version`. An admin change writes the whole new item, conditioned on the version
the admin saw, in the same transaction as its ADMIN_CONFIG_CHANGED event
(`put_op`), so two admins can't silently overwrite each other and no change
goes unrecorded.
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
LEAD_SOURCES_PK = "lead_sources"
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

# Placeholder list until an admin maintains the real one (P6). Illustrative
# values only; TriNet's actual picklist is not in this repo.
SEED_LEAD_SOURCES: tuple[str, ...] = (
    "Marketing: Events",
    "Marketing: Webinar",
    "Marketing: Content Syndication",
    "Marketing: Paid Social",
    "Marketing: Website",
    "Sales: Outbound",
)


@dataclass(frozen=True)
class LeadSourceList:
    version: str
    active: tuple[str, ...]


SEED_VERSION = "seed"


@dataclass(frozen=True)
class LeadSourceItem:
    id: str
    value: str
    active: bool
    order: int

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "value": self.value, "active": self.active, "order": self.order}


class ConfigStore:
    def __init__(self, table_name: str | None = None, *, dynamodb_resource: Any = None) -> None:
        name = table_name or os.environ.get("CONFIG_TABLE")
        self.table_name = name
        self._table = (
            (dynamodb_resource or boto3.resource("dynamodb")).Table(name) if name else None
        )

    # --- admin writes -------------------------------------------------------------

    def put_op(self, pk: str, body: dict[str, Any], *, expected: str, new: str) -> dict[str, Any]:
        """TransactWriteItems Put replacing setting `pk`, only if it's still at `expected`."""
        if not self.table_name:
            raise RuntimeError("CONFIG_TABLE isn't set")
        op: dict[str, Any] = {
            "TableName": self.table_name,
            "Item": {**body, "pk": pk, "sk": CURRENT, "version": new},
        }
        if expected == SEED_VERSION:
            op["ConditionExpression"] = "attribute_not_exists(pk)"
        else:
            op["ConditionExpression"] = "version = :v"
            op["ExpressionAttributeValues"] = {":v": expected}
        return {"Put": op}

    def lead_source_items(self) -> tuple[str, list[LeadSourceItem]]:
        """Every lead source, active or not, in display order."""
        item = self._get(LEAD_SOURCES_PK)
        if not item:
            return SEED_VERSION, [
                LeadSourceItem(f"ls_seed_{i}", v, True, i) for i, v in enumerate(SEED_LEAD_SOURCES)
            ]
        values = [
            LeadSourceItem(
                str(v.get("id") or f"ls_{i}"),
                str(v["value"]),
                bool(v.get("active", True)),
                int(v.get("order", i)),
            )
            for i, v in enumerate(item.get("values", []))
        ]
        values.sort(key=lambda v: v.order)
        return str(item.get("version", "unknown")), values

    def alias_state(self) -> tuple[str, dict[str, list[str]]]:
        aliases = self.aliases()
        version = aliases.version
        return version, {k: list(v) for k, v in aliases.by_field.items()}

    def threshold_state(self) -> tuple[str, dict[str, float]]:
        item = self._get(THRESHOLDS_PK)
        return (str(item.get("version", "unknown")) if item else SEED_VERSION), self.thresholds()

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

    def lead_sources(self) -> LeadSourceList:
        item = self._get(LEAD_SOURCES_PK)
        if not item:
            return LeadSourceList(version="seed", active=SEED_LEAD_SOURCES)
        values = [v for v in item.get("values", []) if v.get("active", True)]
        values.sort(key=lambda v: v.get("order", 0))
        return LeadSourceList(
            version=str(item.get("version", "unknown")),
            active=tuple(str(v["value"]) for v in values),
        )

    def thresholds(self) -> dict[str, float]:
        item = self._get(THRESHOLDS_PK)
        values = dict(config_defaults.THRESHOLDS)
        if item:
            values.update({k: float(v) for k, v in item.get("values", {}).items()})
        return values

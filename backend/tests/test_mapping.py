from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from shared.bedrock_client import FakeBedrockClient
from shared.catalog import FIELDS, TEMPLATE_FIELDS, normalize_header
from shared.config_store import SEED_ALIAS_SET, AliasSet, ConfigStore
from shared.fake_ai import HeuristicFakeBedrock
from shared.mapping import (
    MAX_SAMPLES,
    MappingInvalid,
    MappingSuggestion,
    Method,
    suggest,
    validate_confirmation,
)
from shared.parsing import parse_file
from tasks.parse_file import column_samples
from tests.conftest import CONFIG_TABLE

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def _file(name: str) -> tuple[list[str], dict[str, list[str]]]:
    result = parse_file((FIXTURES / name).read_bytes(), name)
    return result.headers, column_samples(result)


def _suggest(
    name: str, bedrock: Any, *, threshold: float = 0.75, aliases: AliasSet = SEED_ALIAS_SET
) -> MappingSuggestion:
    headers, samples = _file(name)
    return suggest(headers, samples, aliases=aliases, threshold=threshold, bedrock=bedrock)


def _by_header(s: MappingSuggestion) -> dict[str, tuple[str | None, str]]:
    return {c.source_header: (c.field_key, str(c.method)) for c in s.columns}


def _ai(*items: dict[str, Any]) -> str:
    return json.dumps({"items": list(items)})


def _item(header: str, key: str | None, confidence: float) -> dict[str, Any]:
    return {"source_header": header, "field_key": key, "confidence": confidence, "reason": "r"}


VENDOR_AI = _ai(
    _item("Org", "company", 0.91),
    _item("Job Position", "title", 0.88),
    _item("Phone (2)", "mobile_phone", 0.8),
    _item("Scan Date", None, 0.1),
    _item("Column H", "notes", 0.6),  # below threshold
    _item("Score", "badge_score", 0.99),  # not a real field
    _item("Opt In", "email", 0.95),  # already taken by alias
)


class TestAcceptance:
    def test_template_maps_25_of_25_with_zero_ai_calls(self) -> None:
        bedrock = FakeBedrockClient()
        s = _suggest("template_filled.xlsx", bedrock)
        assert len(s.columns) == 25
        assert all(c.method == Method.EXACT for c in s.columns)
        assert {c.field_key for c in s.columns} == {f.key for f in TEMPLATE_FIELDS}
        assert bedrock.prompts == []
        assert not s.ai.invoked

    def test_vendor_file_maps_via_alias_and_ai(self) -> None:
        s = _suggest("vendor_export.xlsx", FakeBedrockClient().queue(VENDOR_AI))
        got = _by_header(s)
        assert got["E-mail"] == ("email", "alias")
        assert got["First"] == ("first_name", "alias")
        assert got["Zip"] == ("postal_code", "alias")
        assert got["Org"] == ("company", "ai")
        assert got["Job Position"] == ("title", "ai")
        assert got["Phone (2)"] == ("mobile_phone", "ai")
        for header in ("Scan Date", "Column H", "Score", "Opt In"):
            assert got[header] == (None, "none"), header
        org = next(c for c in s.columns if c.source_header == "Org")
        assert org.confidence == 0.91

    def test_low_confidence_leaves_column_unmapped(self) -> None:
        bedrock = FakeBedrockClient().queue(_ai(_item("Org", "company", 0.74)))
        s = _suggest("vendor_export.xlsx", bedrock)
        assert _by_header(s)["Org"] == (None, "none")
        assert s.ai.outcome == "ok" and not s.ai.degraded

    def test_invalid_json_still_completes_with_no_suggestions(self) -> None:
        bedrock = FakeBedrockClient().queue("not json", '{"items": [{"oops": 1}]}')
        s = _suggest("vendor_export.xlsx", bedrock)
        assert s.ai.degraded and s.ai.outcome == "parse_failed"
        assert s.method_counts()["ai"] == 0
        assert _by_header(s)["E-mail"] == ("email", "alias")  # deterministic steps unaffected
        assert len(bedrock.prompts) == 2  # one retry

    def test_bedrock_error_degrades(self) -> None:
        s = _suggest("vendor_export.xlsx", FakeBedrockClient().queue(RuntimeError("throttled")))
        assert s.ai.degraded and s.ai.outcome == "error"


class TestRules:
    def test_normalize_header(self) -> None:
        assert normalize_header("  E-Mail  Address ") == "e-mail address"
        assert normalize_header("Lead Source - Most Recent") == "lead source - most recent"
        assert normalize_header("Zip/Postal_Code") == "zip postal code"
        assert normalize_header("Phone (2)") == "phone 2"

    def test_exact_beats_alias_and_loser_goes_to_ai(self) -> None:
        bedrock = FakeBedrockClient().queue(_ai(_item("Phone", "mobile_phone", 0.8)))
        s = suggest(
            ["Phone", "Business Phone"],
            {},
            aliases=SEED_ALIAS_SET,
            threshold=0.75,
            bedrock=bedrock,
        )
        assert _by_header(s) == {
            "Phone": ("mobile_phone", "ai"),
            "Business Phone": ("phone", "exact"),
        }

    def test_ai_one_to_one_keeps_highest_confidence(self) -> None:
        bedrock = FakeBedrockClient().queue(
            _ai(_item("Org", "company", 0.8), _item("Employer", "company", 0.9))
        )
        s = suggest(
            ["Org", "Employer"], {}, aliases=SEED_ALIAS_SET, threshold=0.75, bedrock=bedrock
        )
        assert _by_header(s) == {"Org": (None, "none"), "Employer": ("company", "ai")}

    def test_out_of_range_confidence_is_ignored(self) -> None:
        bedrock = FakeBedrockClient().queue(_ai(_item("Org", "company", 1.7)))
        s = suggest(["Org"], {}, aliases=SEED_ALIAS_SET, threshold=0.75, bedrock=bedrock)
        assert _by_header(s) == {"Org": (None, "none")}

    def test_prompt_has_only_unmatched_columns_and_free_fields(self) -> None:
        bedrock = FakeBedrockClient()
        _suggest("vendor_export.xlsx", bedrock)
        payload = json.loads(bedrock.prompts[0].split("<input>")[1].split("</input>")[0])
        sent = {c["source_header"] for c in payload["columns"]}
        assert "E-mail" not in sent and "Org" in sent
        fields = {f["field_key"] for f in payload["allowed_fields"]}
        assert "email" not in fields and "company" in fields
        for col in payload["columns"]:
            assert len(col["samples"]) <= MAX_SAMPLES
            assert all(v.strip() for v in col["samples"])

    def test_aliases_come_from_config(self, config_table: Any) -> None:
        config_table.put_item(
            Item={
                "pk": "field_aliases",
                "sk": "current",
                "version": "7",
                "aliases": {"company": ["org", "organization"]},
            }
        )
        store = ConfigStore(CONFIG_TABLE)
        s = suggest(["Org"], {}, aliases=store.aliases(), threshold=0.75,
                    bedrock=FakeBedrockClient())  # fmt: skip
        assert _by_header(s) == {"Org": ("company", "alias")}
        assert s.alias_version == "7"

    def test_threshold_comes_from_config(self, config_table: Any) -> None:
        config_table.put_item(
            Item={
                "pk": "thresholds",
                "sk": "current",
                "values": {"mapping_suggest_threshold": "0.95"},
            }
        )
        thresholds = ConfigStore(CONFIG_TABLE).thresholds()
        assert thresholds["mapping_suggest_threshold"] == 0.95
        assert thresholds["junk_flag_threshold"] == 0.70

    def test_config_falls_back_to_seed_without_a_table(self) -> None:
        store = ConfigStore(None)
        assert store.aliases() is SEED_ALIAS_SET
        assert store.thresholds()["mapping_suggest_threshold"] == 0.75

    def test_heuristic_fake_finds_obvious_matches(self) -> None:
        got = _by_header(_suggest("vendor_export.xlsx", HeuristicFakeBedrock()))
        assert got["Org"] == ("company", "ai")
        assert got["Job Position"] == ("title", "ai")
        assert got["Opt In"] == (None, "none")


class TestConfirmation:
    HEADERS = ("Company", "First", "Last", "Email", "Campaign", "Badge")

    def _choices(self, **overrides: str | None) -> list[dict[str, Any]]:
        mapping: dict[str, str | None] = {
            "Company": "company", "First": "first_name", "Last": "last_name",
            "Email": "email", "Campaign": "campaign_id", "Badge": None,
        }  # fmt: skip
        mapping.update(overrides)
        return [{"source_header": h, "field_key": k} for h, k in mapping.items()]

    def test_valid_mapping_without_derivable_fields(self) -> None:
        chosen = validate_confirmation(self.HEADERS, self._choices())
        assert chosen["Badge"] is None and chosen["Email"] == "email"

    def test_exempt_fields_are_exactly_the_fillable_ones(self) -> None:
        exempt = {f.key for f in FIELDS if f.required and not f.must_map}
        assert exempt == {"lead_source", "campaign_status", "list_name", "campaign_name"}

    def test_missing_required(self) -> None:
        with pytest.raises(MappingInvalid) as err:
            validate_confirmation(self.HEADERS, self._choices(Campaign=None))
        assert "SFDC Last Campaign ID" in err.value.message

    def test_duplicate_field(self) -> None:
        with pytest.raises(MappingInvalid) as err:
            validate_confirmation(self.HEADERS, self._choices(Badge="email"))
        assert "'Email' and 'Badge' are both mapped to Email Address" in err.value.message

    @pytest.mark.parametrize(
        "choices",
        [
            [{"source_header": "Nope", "field_key": None}],
            [{"source_header": "Company", "field_key": "not_a_field"}],
            [{"source_header": "Company", "field_key": "company"}],  # other headers missing
        ],
    )
    def test_malformed(self, choices: list[dict[str, Any]]) -> None:
        with pytest.raises(MappingInvalid):
            validate_confirmation(self.HEADERS, choices)

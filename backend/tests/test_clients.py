from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from shared.analysis_store import campaign_info
from shared.bedrock_client import FakeBedrockClient, Outcome
from shared.fake_sfdc import CAMPAIGNS
from shared.workato_client import (
    Campaign,
    FakeWorkatoClient,
    SendToProdViolation,
    WorkatoError,
    lookup_distinct,
    parse_campaign_lookup,
)


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_header: str
    field_key: str | None
    confidence: float


class Suggestions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Suggestion]


def _invoke(client: FakeBedrockClient):  # type: ignore[no-untyped-def]
    return client.invoke_json(
        purpose="column_mapping",
        prompt_version="v1",
        system="map columns",
        prompt="headers...",
        output_model=Suggestions,
    )


VALID = {"items": [{"source_header": "E-mail", "field_key": "email", "confidence": 0.93}]}


class TestBedrock:
    def test_valid_json(self) -> None:
        result = _invoke(FakeBedrockClient().queue(VALID))
        assert result.outcome is Outcome.OK
        assert result.value is not None and result.value.items[0].field_key == "email"

    def test_fenced_json_is_accepted(self) -> None:
        text = '```json\n{"items": []}\n```'
        assert _invoke(FakeBedrockClient().queue(text)).outcome is Outcome.OK

    def test_retries_once_with_reminder(self) -> None:
        client = FakeBedrockClient().queue("not json", VALID)
        result = _invoke(client)
        assert result.outcome is Outcome.OK_AFTER_RETRY
        assert "not valid JSON" in client.prompts[1]

    def test_invalid_twice_degrades_without_raising(self) -> None:
        result = _invoke(FakeBedrockClient().queue("nope", {"wrong": 1}))
        assert result.outcome is Outcome.PARSE_FAILED
        assert result.value is None

    def test_service_error_degrades_without_raising(self) -> None:
        result = _invoke(FakeBedrockClient().queue(RuntimeError("throttled")))
        assert result.outcome is Outcome.ERROR
        assert result.value is None

    def test_audit_details_have_no_content(self) -> None:
        details = _invoke(FakeBedrockClient().queue(VALID)).audit_details(row_count=3)
        assert "E-mail" not in str(details)
        assert details["row_count"] == 3 and details["purpose"] == "column_mapping"


class TestWorkato:
    def test_dev_rejects_send_to_prod_before_calling(self) -> None:
        client = FakeWorkatoClient(env="dev")
        with pytest.raises(SendToProdViolation):
            client.post_to_eloqua({"source_record_id": "j_1:1", "send_to_prod": True})
        assert client.calls == []

    def test_dev_rejects_missing_send_to_prod(self) -> None:
        with pytest.raises(SendToProdViolation):
            FakeWorkatoClient(env="dev").post_to_eloqua({"source_record_id": "j_1:1"})

    def test_prod_allows_send_to_prod(self) -> None:
        result = FakeWorkatoClient(env="prod").post_to_eloqua(
            {"source_record_id": "j_1:1", "send_to_prod": True}
        )
        assert result.ok

    def test_forced_post_failure(self) -> None:
        client = FakeWorkatoClient(post_status={"j_1:2": 500})
        assert not client.post_to_eloqua({"source_record_id": "j_1:2", "send_to_prod": False}).ok

    def test_lookup_found_and_not_found(self) -> None:
        known = Campaign(id="701000000000001AAA", found=True, name="Test Event")
        client = FakeWorkatoClient(campaigns={known.id: known})
        assert client.lookup_campaign(known.id, caller_job_id="j_1").found is True
        assert client.lookup_campaign("701000000000002AAA", caller_job_id="j_1").found is False
        assert client.calls == [
            ("lookup_campaign", "701000000000001AAA"),
            ("lookup_campaign", "701000000000002AAA"),
        ]

    def test_lookup_distinct_calls_once_per_campaign(self) -> None:
        client = FakeWorkatoClient(campaigns=dict(CAMPAIGNS))
        ids = ["701000000000001AAA"] * 100 + ["701000000000002AAA"] * 3
        found = lookup_distinct(client, ids, caller_job_id="j_1")
        assert set(found) == {"701000000000001AAA", "701000000000002AAA"}
        assert len(client.calls) == 2

    def test_blank_campaign_id_is_not_sent(self) -> None:
        client = FakeWorkatoClient()
        with pytest.raises(ValueError):
            client.lookup_campaign(" ", caller_job_id="j_1")
        assert client.calls == []

    def test_batch_limits(self) -> None:
        client = FakeWorkatoClient()
        with pytest.raises(ValueError):
            client.enrich_contacts([{}] * 26, caller_job_id="j_1")

    def test_whole_batch_enrichment_error(self) -> None:
        with pytest.raises(WorkatoError):
            FakeWorkatoClient(fail_enrich=True).enrich_contacts(
                [{"source_record_id": "1"}], caller_job_id="j_1"
            )


# The campaign lookup recipe's response shape, from a sample the recipe owner
# shared. Values are synthetic; the extra Salesforce fields are ignored.
RECIPE_RESPONSE: dict[str, Any] = {
    "input_id": "701000000000001AAA",
    "campaign": {
        "id": "701000000000001AAA",
        "name": "Demo Conference 2026",
        "type": "Marketing: Events",
        "status": "In Progress",
        "is_active": False,
        "record_type_id": "012000000000001AAA",
        "created_date": "2026-09-23T19:10:35.000000+00:00",
        "budgeted_cost": 100.0,
        "expected_revenue": "",
        "number_of_leads": 1.0,
        "member_statuses": [
            {"id": "01Y000000000001AAA", "label": "Sent", "is_default": True,
             "has_responded": False},
            {"id": "01Y000000000002AAA", "label": "Responded", "is_default": False,
             "has_responded": True},
        ],
    },
    "campaign_exists": True,
    "valid_id": True,
}  # fmt: skip


class TestCampaignLookupResponse:
    def test_found(self) -> None:
        c = parse_campaign_lookup(RECIPE_RESPONSE, "701000000000001AAA")
        assert (c.found, c.name, c.type, c.is_active, c.status) == (
            True, "Demo Conference 2026", "Marketing: Events", False, "In Progress",
        )  # fmt: skip
        # Salesforce's order is kept; the default is the one flagged.
        assert [s.label for s in c.member_statuses] == ["Sent", "Responded"]
        info = campaign_info(c)
        assert info.statuses == ("Sent", "Responded") and info.default_status == "Sent"

    def test_invalid_id_and_not_found(self) -> None:
        cid = "701000000000009AAA"
        for response in (
            {"input_id": cid, "valid_id": False, "campaign_exists": False},
            {"input_id": cid, "valid_id": True, "campaign_exists": False},
        ):
            c = parse_campaign_lookup(response, cid)
            assert c.found is False and c.id == cid

    def test_answer_for_another_id_is_an_error(self) -> None:
        with pytest.raises(WorkatoError):
            parse_campaign_lookup(RECIPE_RESPONSE, "701000000000002AAA")

    def test_exists_without_campaign_is_an_error(self) -> None:
        response = {"input_id": "701000000000001AAA", "valid_id": True, "campaign_exists": True}
        with pytest.raises(WorkatoError):
            parse_campaign_lookup(response, "701000000000001AAA")

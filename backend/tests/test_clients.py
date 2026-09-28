from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from shared.bedrock_client import FakeBedrockClient, Outcome
from shared.workato_client import (
    Campaign,
    FakeWorkatoClient,
    SendToProdViolation,
    WorkatoError,
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

    def test_lookup_returns_entry_for_every_id(self) -> None:
        known = Campaign(id="701000000000001AAA", found=True, name="Test Event")
        client = FakeWorkatoClient(campaigns={known.id: known})
        result = client.lookup_campaigns([known.id, "701000000000002AAA"], caller_job_id="j_1")
        assert [c.found for c in result] == [True, False]

    def test_batch_limits(self) -> None:
        client = FakeWorkatoClient()
        with pytest.raises(ValueError):
            client.enrich_contacts([{}] * 26, caller_job_id="j_1")
        with pytest.raises(ValueError):
            client.lookup_campaigns(["x"] * 51, caller_job_id="j_1")

    def test_whole_batch_enrichment_error(self) -> None:
        with pytest.raises(WorkatoError):
            FakeWorkatoClient(fail_enrich=True).enrich_contacts(
                [{"source_record_id": "1"}], caller_job_id="j_1"
            )

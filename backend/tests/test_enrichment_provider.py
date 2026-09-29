"""ZoomInfo provider: request shape, result parsing, and batch failure (SPEC §15)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from shared.enrichment import EnrichInput, ZoomInfoProvider, eligible, translate_conflict
from shared.fake_zoominfo import enrich_handler
from shared.workato_client import FakeWorkatoClient


def _provider(
    handler: Any = enrich_handler, **kw: Any
) -> tuple[ZoomInfoProvider, FakeWorkatoClient]:
    workato = FakeWorkatoClient(enrich_handler=handler, **kw)
    return ZoomInfoProvider(workato, sleep=lambda _: None), workato


def _row(row_id: int, email: str, **values: str) -> EnrichInput:
    return EnrichInput(row_id, {"email": email, **values})


def _one(email: str, handler: Any = enrich_handler, **values: str) -> Any:
    provider, _ = _provider(handler)
    return provider.enrich([_row(2, email, **values)], job_id="j_1")[0]


class TestOutcomes:
    def test_accepted(self) -> None:
        r = _one("ada@acme.example")
        assert (r.status, r.match_status, r.score) == ("accepted", "high_confidence", 96.0)
        assert r.fields["title"] == "VP Marketing"
        assert r.fields["naics_code"] == "541511"  # first NAICS id
        assert r.fields["industry"] == "Software"  # first industry
        assert r.fields["employee_count"] == "250"
        assert r.linkedin_profile_count == 2

    def test_do_not_call_phone_is_not_filled(self) -> None:
        r = _one("ada@acme.example")
        assert "phone" not in r.fields
        assert r.fields["mobile_phone"] == "+1 555 010 0151"
        assert any("do-not-call" in n for n in r.notes)

    def test_email_is_never_a_fill_candidate(self) -> None:
        def handler(contacts: list[dict[str, str]]) -> dict[str, Any]:
            return {
                "best_choices": [
                    {
                        "source_record_id": "2",
                        "accept_enrichment": True,
                        "zi_best_email": "alias@acme.example",
                        "zi_best_job_title": "CFO",
                    }
                ]
            }

        r = _one("ada@acme.example", handler)
        assert "email" not in r.fields and r.fields["title"] == "CFO"

    def test_needs_review(self) -> None:
        r = _one("linus@hooli.example")
        assert (r.status, r.match_status, r.score) == ("review", "possible_match", 71.0)
        assert r.conflicts == [
            "ZoomInfo shows a different current employer.",
            "ZoomInfo shows a different job title.",
        ]
        assert r.candidate_display == {
            "name": "Linus Sample",
            "company": "Hooli XYZ Demo",
            "title": "Head of Platform",
            "email": "linus.sample@hooli.example",
        }
        # Candidate values are ready to apply if the user says so.
        assert r.fields["company"] == "Hooli XYZ Demo"
        assert r.fields["linkedin_url"] == "linkedin.com/in/linus-sample-demo"

    def test_no_match(self) -> None:
        r = _one("nobody@nowhere.example")
        assert (r.status, r.match_status, r.fields) == ("no_match", "no_match", {})

    def test_invalid_input(self) -> None:
        r = _one("info@vandelay.example")
        assert (r.status, r.match_status) == ("no_match", "invalid_input")

    def test_missing_result_for_a_row(self) -> None:
        r = _one("x@y.example", lambda contacts: {"best_choices": []})
        assert (r.status, r.match_status) == ("no_match", "missing_result")

    def test_whole_batch_error_retries_then_marks_rows(self) -> None:
        provider, workato = _provider()
        rows = [_row(2, "ada@acme.example"), _row(3, "outage@acme.example")]
        results = provider.enrich(rows, job_id="j_1")
        assert [r.status for r in results] == ["error", "error"]
        assert "service error" in results[0].notes[0]
        assert len([c for c in workato.calls if c[0] == "enrich_contacts"]) == 3

    def test_recovers_on_retry(self) -> None:
        calls = {"n": 0}

        def flaky(contacts: list[dict[str, str]]) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("timeout")
            return enrich_handler(contacts)

        assert _one("ada@acme.example", flaky).status == "accepted"
        assert calls["n"] == 2


class TestRequest:
    def test_contact_uses_processed_values_and_ids(self) -> None:
        contact = ZoomInfoProvider.contact(
            _row(
                7,
                "ada@acme.example",
                first_name="Ada",
                company="Acme Demo Co",
                website="acme.example",
                zi_contact_id="1000000001",
                linkedin_url="linkedin.com/in/ada",
            )
        )
        assert contact == {
            "source_record_id": "7",
            "email": "ada@acme.example",
            "first_name": "Ada",
            "company_name": "Acme Demo Co",
            "company_website": "acme.example",
            "zi_contact_id": "1000000001",
            "social_url": "linkedin.com/in/ada",
        }

    def test_batch_limit(self) -> None:
        provider, _ = _provider()
        with pytest.raises(ValueError):
            provider.enrich([_row(i, f"p{i}@x.example") for i in range(26)], job_id="j_1")


class TestHelpers:
    def test_conflict_translation(self) -> None:
        assert translate_conflict(" name_mismatch ") == "The name in ZoomInfo is different."
        assert translate_conflict("brand_new_code") == "ZoomInfo flagged: brand new code."

    def test_candidate_json_may_be_malformed(self) -> None:
        def handler(contacts: list[dict[str, str]]) -> dict[str, Any]:
            return {
                "best_choices": [
                    {
                        "source_record_id": "2",
                        "needs_human_review": "true",
                        "selected_candidate_json": "{not json",
                    }
                ]
            }

        r = _one("x@y.example", handler)
        assert r.status == "review" and r.fields == {} and r.candidate_display == {}

    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            ({"status": "ready"}, True),
            ({"status": "warning"}, True),
            ({"status": "pending_enrichment"}, True),
            ({"status": "ready", "excluded": True}, False),
            (
                {
                    "status": "blocked",
                    "issues": [
                        {"severity": "blocking", "code": "REQUIRED_MISSING", "field": "company"}
                    ],
                },
                True,
            ),
            (
                {
                    "status": "blocked",
                    "issues": [{"severity": "blocking", "code": "CAMPAIGN_NOT_FOUND"}],
                },
                False,
            ),
        ],
    )
    def test_eligibility(self, row: dict[str, Any], expected: bool) -> None:
        assert eligible(row) is expected

    def test_fake_response_is_json_serializable(self) -> None:
        json.dumps(enrich_handler([{"source_record_id": "1", "email": "ada@acme.example"}]))

"""Row evaluation rules (SPEC §7.3, §9, §11-§14). Every §9 issue code is exercised here,
except ENRICHMENT_REVIEW, which arrives with enrichment in P4."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from shared.analysis import (
    AnalysisContext,
    CampaignInfo,
    LeadSourceResolution,
    RowEvaluation,
    apply_duplicates,
    evaluate_row,
    junk_rule_hits,
    lead_source_key,
    summarize,
)
from shared.config_store import SEED_LEAD_SOURCES
from shared.normalizer import Normalized, StandInNormalizer

EVENTS = CampaignInfo(
    id="701000000000001AAA",
    found=True,
    name="Demo Conference 2026",
    type="Marketing: Events",
    is_active=True,
    statuses=("Registered", "Attended", "No Show"),
    default_status="Registered",
)
INACTIVE = CampaignInfo(
    id="701000000000003AAA",
    found=True,
    name="Demo Roadshow 2025",
    type="Marketing: Events",
    is_active=False,
    statuses=("Registered", "Attended"),
    default_status="Registered",
)
MISSING = CampaignInfo(id="701000000000009AAA", found=False)

HEADERS = {
    "Company": "company",
    "First": "first_name",
    "Last": "last_name",
    "Email": "email",
    "Campaign": "campaign_id",
    "Status": "campaign_status",
    "Source": "lead_source",
    "Title": "title",
    "Phone": "phone",
    "Country": "country",
    "Zip": "postal_code",
    "Employees": "employee_count",
    "Mobile": "mobile_phone",
    "Campaign Name": "campaign_name",
}


class ScriptedNormalizer:
    """Stand-in output with flags forced, to test the issue rules in isolation."""

    version = "scripted"

    def __init__(self, **flags: bool) -> None:
        self.flags = flags

    def normalize(self, fields: Mapping[str, str]) -> Normalized:
        base = StandInNormalizer().normalize(fields)
        return Normalized(base.values, {**base.flags, **self.flags})


def ctx(**overrides: Any) -> AnalysisContext:
    base: dict[str, Any] = {
        "mapping": dict(HEADERS),
        "campaigns": {c.id: c for c in (EVENTS, INACTIVE, MISSING)},
        "lead_sources": SEED_LEAD_SOURCES,
        "lead_source_resolutions": {},
        "thresholds": {
            "lead_source_auto_threshold": 0.9,
            "junk_flag_threshold": 0.7,
            "junk_block_threshold": 0.9,
        },
        "enrich": False,
        "normalizer": StandInNormalizer(),
        "owner_email": "uploader@acme.example",
        "list_date": "2026-09-28",
    }
    base.update(overrides)
    return AnalysisContext(**base)


def row(**source: str) -> dict[str, Any]:
    values = {
        "Company": "Acme Demo Co",
        "First": "Ada",
        "Last": "Example",
        "Email": "ada@acme.example",
        "Campaign": EVENTS.id,
        "Status": "Attended",
        "Source": "Marketing: Events",
    }
    values.update(source)
    return {"row_id": 2, "source": values}


def ev(r: Mapping[str, Any] | None = None, **kw: Any) -> RowEvaluation:
    return evaluate_row(r or row(), ctx(**kw))


def codes(e: RowEvaluation) -> set[str]:
    return e.issue_codes()


def issue(e: RowEvaluation, code: str) -> Any:
    return next(i for i in e.issues if i.code == code)


class TestCleanRow:
    def test_ready(self) -> None:
        e = ev()
        assert e.status == "ready", [i.as_dict() for i in e.issues]
        assert e.processed["campaign_name"] == "Demo Conference 2026"
        assert e.provenance["campaign_name"] == "derived:sfdc_campaign_name"
        assert e.processed["list_name"] == (
            "Demo Conference 2026 – 2026-09-28 – uploader@acme.example"
        )
        assert e.provenance["list_name"] == "derived:auto_list_name"


class TestRequired:
    def test_required_missing_blocks(self) -> None:
        e = ev(row(Email=""))
        i = issue(e, "REQUIRED_MISSING")
        assert (i.field, i.severity, i.pending) == ("email", "blocking", False)
        assert e.status == "blocked"

    def test_enrichable_fields_are_pending_when_enrichment_is_on(self) -> None:
        e = ev(row(Company=""), enrich=True)
        assert issue(e, "REQUIRED_MISSING").pending
        assert e.status == "pending_enrichment"

    def test_email_and_campaign_are_never_pending(self) -> None:
        e = ev(row(Company="", Campaign=""), enrich=True)
        pending = {i.field: i.pending for i in e.issues if i.code == "REQUIRED_MISSING"}
        assert pending == {"company": True, "campaign_id": False}
        assert e.status == "blocked"

    def test_enrichable_fields_block_without_enrichment(self) -> None:
        assert ev(row(First="")).status == "blocked"


class TestEmail:
    def test_invalid(self) -> None:
        e = ev(row(Email="not-an-email"))
        assert issue(e, "EMAIL_INVALID").severity == "blocking"

    def test_role_based(self) -> None:
        assert issue(ev(row(Email="info@acme.example")), "EMAIL_ROLE_BASED").severity == "warning"

    def test_public_domain(self) -> None:
        e = ev(row(Email="ada@gmail.com"))
        assert issue(e, "EMAIL_PUBLIC_DOMAIN").severity == "warning"
        assert e.status == "warning"

    def test_email_is_lowercased(self) -> None:
        e = ev(row(Email="  Ada@ACME.example "))
        assert e.processed["email"] == "ada@acme.example"
        assert e.provenance["email"] == "normalized"


class TestNormalizerDrivenIssues:
    def test_name_changed(self) -> None:
        class StripDigits(ScriptedNormalizer):
            def normalize(self, fields: Mapping[str, str]) -> Normalized:
                out = super().normalize(fields)
                out.values["first_name"] = "".join(
                    c for c in out.values["first_name"] if not c.isdigit()
                )
                return out

        e = ev(row(First="Ada2"), normalizer=StripDigits())
        assert issue(e, "NAME_CHANGED_BY_NORMALIZER").severity == "info"
        assert e.processed["first_name"] == "Ada"

    def test_phone_invalid_leaves_processed_blank(self) -> None:
        e = ev(row(Phone="12345"))
        assert issue(e, "PHONE_INVALID").severity == "warning"
        assert "phone" not in e.processed
        assert "12345" in issue(e, "PHONE_INVALID").message

    def test_phone_ok(self) -> None:
        e = ev(row(Phone="(555) 010-0100"))
        assert e.processed["phone"] == "+15550100100"
        assert "PHONE_INVALID" not in codes(e)

    def test_blank_phone_is_not_an_issue(self) -> None:
        assert "PHONE_INVALID" not in codes(ev(row(Phone="")))

    def test_country_unrecognized(self) -> None:
        assert issue(ev(row(Country="Atlantis")), "COUNTRY_UNRECOGNIZED").severity == "warning"


class TestCampaign:
    def test_15_char_is_converted(self) -> None:
        e = ev(row(Campaign="701000000000001"))
        assert e.processed["campaign_id"] == EVENTS.id
        assert e.status == "ready"

    def test_bad_checksum(self) -> None:
        e = ev(row(Campaign="701000000000001AAB"))
        i = issue(e, "CAMPAIGN_ID_FORMAT")
        assert "last three characters don't match" in i.message
        assert e.status == "blocked"

    def test_not_a_campaign_id(self) -> None:
        assert "start with 701" in issue(ev(row(Campaign="12345")), "CAMPAIGN_ID_FORMAT").message

    def test_not_found(self) -> None:
        e = ev(row(Campaign=MISSING.id))
        i = issue(e, "CAMPAIGN_NOT_FOUND")
        assert i.source == "sfdc" and "wasn't found in Salesforce" in i.message
        # Derived fields aren't also reported missing while the campaign is invalid.
        assert [x.field for x in e.issues if x.code == "REQUIRED_MISSING"] == []

    def test_inactive_warns(self) -> None:
        e = ev(row(Campaign=INACTIVE.id))
        assert issue(e, "CAMPAIGN_INACTIVE").severity == "warning"
        assert e.status == "warning"

    def test_name_mismatch_sfdc_wins(self) -> None:
        e = ev(row(**{"Campaign Name": "Old Name"}))
        assert issue(e, "CAMPAIGN_NAME_MISMATCH").severity == "warning"
        assert e.processed["campaign_name"] == "Demo Conference 2026"

    def test_name_match_ignores_case(self) -> None:
        assert "CAMPAIGN_NAME_MISMATCH" not in codes(
            ev(row(**{"Campaign Name": "demo conference 2026"}))
        )


class TestLeadSource:
    def test_exact_gets_canonical_casing(self) -> None:
        e = ev(row(Source="  marketing:   events "))
        assert e.processed["lead_source"] == "Marketing: Events"
        assert "LEAD_SOURCE_AUTO_CORRECTED" not in codes(e)

    def test_events_becomes_marketing_events(self) -> None:
        e = ev(row(Source="Events"))
        i = issue(e, "LEAD_SOURCE_AUTO_CORRECTED")
        assert i.severity == "info" and i.source == "rule"
        assert e.processed["lead_source"] == "Marketing: Events"
        assert e.provenance["lead_source"] == "auto_corrected:rule:marketing_prefix"

    def test_blank_derives_from_campaign_type(self) -> None:
        e = ev(row(Source=""))
        assert e.processed["lead_source"] == "Marketing: Events"
        assert e.provenance["lead_source"] == "derived:sfdc_campaign_type"
        assert e.status == "ready"

    def test_blank_without_usable_type_is_missing(self) -> None:
        odd = CampaignInfo(**{**EVENTS.as_dict(), "type": "Partner", "statuses": ("Registered",)})
        e = ev(row(Source=""), campaigns={odd.id: odd})
        assert issue(e, "REQUIRED_MISSING").field == "lead_source"

    def test_ai_high_confidence_auto_applies(self) -> None:
        res = {
            lead_source_key("Tradeshow"): LeadSourceResolution(
                "Marketing: Events", "ai", 0.93, "Marketing: Events"
            )
        }
        e = ev(row(Source="Tradeshow"), lead_source_resolutions=res)
        assert issue(e, "LEAD_SOURCE_AUTO_CORRECTED").source == "ai"
        assert e.processed["lead_source"] == "Marketing: Events"

    def test_ai_low_confidence_is_a_blocking_suggestion(self) -> None:
        res = {
            lead_source_key("Webcast"): LeadSourceResolution(
                "Marketing: Webinar", "ai", 0.8, "Marketing: Webinar"
            )
        }
        e = ev(row(Source="Webcast"), lead_source_resolutions=res)
        i = issue(e, "LEAD_SOURCE_SUGGESTED")
        assert i.severity == "blocking"
        assert i.suggestion == {"value": "Marketing: Webinar", "confidence": 0.8}
        assert "80% sure" in i.message
        assert e.processed["lead_source"] == "Webcast"

    def test_unresolvable_is_invalid(self) -> None:
        assert issue(ev(row(Source="Carrier pigeon")), "LEAD_SOURCE_INVALID").severity == "blocking"

    def test_mismatch_with_campaign_type(self) -> None:
        e = ev(row(Source="Marketing: Webinar"))
        assert issue(e, "LEAD_SOURCE_CAMPAIGN_MISMATCH").severity == "warning"


class TestStatus:
    def test_blank_uses_campaign_default(self) -> None:
        e = ev(row(Status=""))
        assert e.processed["campaign_status"] == "Registered"
        assert e.provenance["campaign_status"] == "derived:sfdc_default_status"
        assert issue(e, "STATUS_DEFAULTED").severity == "info"
        assert e.status == "ready"

    def test_canonical_casing(self) -> None:
        assert ev(row(Status="attended")).processed["campaign_status"] == "Attended"

    def test_invalid_offers_closest(self) -> None:
        e = ev(row(Status="Atended"))
        i = issue(e, "STATUS_INVALID")
        assert i.severity == "blocking"
        assert i.suggestion["options"][0] == "Attended"
        assert "Valid statuses: Registered, Attended, No Show" in i.message


class TestJunk:
    def test_rules(self) -> None:
        hits = junk_rule_hits(
            {
                "first_name": "Test",
                "last_name": "Smith3",
                "company": "asdfgh",
                "title": "xxxx",
                "email": "a@example.com",
            }
        )
        assert hits == {
            "first_name": "placeholder",
            "last_name": "not_a_person_name",
            "company": "keyboard_mash",
            "title": "keyboard_mash",
            "email": "test_record",
        }

    def test_same_name_everywhere(self) -> None:
        hits = junk_rule_hits({"first_name": "Bob", "last_name": "bob", "company": "BOB"})
        assert hits == {"company": "company_looks_like_person"}

    def test_rule_alone_is_suspect(self) -> None:
        e = ev(row(Company="asdf"))
        i = issue(e, "VALUE_SUSPECT")
        assert (i.field, i.source, e.status) == ("company", "rule", "warning")

    def test_ai_alone_never_blocks(self) -> None:
        r = row(Company="Globex Test Inc")
        r["ai_flags"] = [
            {
                "field": "company",
                "reason_code": "test_record",
                "confidence": 0.99,
                "explanation": "Has 'Test'.",
            }
        ]
        e = ev(r)
        assert issue(e, "VALUE_SUSPECT").source == "ai"
        assert e.status == "warning"

    def test_ai_plus_rule_blocks(self) -> None:
        r = row(Company="asdf")
        r["ai_flags"] = [
            {
                "field": "company",
                "reason_code": "keyboard_mash",
                "confidence": 0.95,
                "explanation": "",
            }
        ]
        e = ev(r)
        assert issue(e, "VALUE_JUNK").severity == "blocking"
        assert "VALUE_SUSPECT" not in codes(e)

    def test_low_ai_confidence_is_ignored(self) -> None:
        r = row()
        r["ai_flags"] = [
            {"field": "company", "reason_code": "other", "confidence": 0.5, "explanation": ""}
        ]
        assert ev(r).status == "ready"

    def test_user_can_clear_a_flag(self) -> None:
        r = row(Company="asdf")
        r["dismissed"] = ["VALUE_SUSPECT:company"]
        assert ev(r).status == "ready"


class TestOtherFields:
    def test_zip_leading_zero(self) -> None:
        e = ev(row(Zip="2134", Country="USA"))
        assert e.processed["postal_code"] == "02134"
        assert issue(e, "ZIP_LEADING_ZERO_RESTORED").severity == "info"

    def test_zip_not_padded_outside_us(self) -> None:
        assert ev(row(Zip="2134", Country="Germany")).processed["postal_code"] == "2134"

    def test_employee_range(self) -> None:
        assert ev(row(Employees="50-100")).processed["employee_count"] == "50"
        assert ev(row(Employees="1,200+")).processed["employee_count"] == "1200"

    def test_employee_non_numeric(self) -> None:
        e = ev(row(Employees="lots"))
        assert issue(e, "FIELD_FORMAT_INVALID").field == "employee_count"
        assert "employee_count" not in e.processed

    def test_excel_error_value_carries_to_mapped_field(self) -> None:
        r = row()
        r["issues_parse"] = [
            {
                "code": "EXCEL_ERROR_VALUE",
                "severity": "warning",
                "message": "Cell E2 ...",
                "column": "Title",
            }
        ]
        assert issue(ev(r), "EXCEL_ERROR_VALUE").field == "title"

    def test_not_sent_field(self) -> None:
        e = ev(row(Mobile="+1 555 010 0199"))
        i = issue(e, "NOT_SENT_FIELD")
        assert i.severity == "info"
        assert "Mobile Phone" in i.message and "SFDC List Name" in i.message


class TestPrecedence:
    def test_user_edit_wins_and_is_normalized(self) -> None:
        r = row(Email="broken")
        r["user_edits"] = [
            {"field": "email", "from": "broken", "to": " Ada@Acme.example ", "by": "u", "at": "t"}
        ]
        e = ev(r)
        assert e.processed["email"] == "ada@acme.example"
        assert e.provenance["email"] == "user_edit"
        assert "EMAIL_INVALID" not in codes(e)

    def test_enrichment_only_fills_blanks_and_never_email(self) -> None:
        r = row(Title="")
        r["enrichment_values"] = {"title": "CFO", "company": "Other Co", "email": "x@y.example"}
        e = ev(r)
        assert e.processed["title"] == "CFO"
        assert e.provenance["title"] == "enrichment:zoominfo"
        assert e.processed["company"] == "Acme Demo Co"
        assert e.processed["email"] == "ada@acme.example"


class TestFileLevel:
    def test_duplicates_block_later_rows(self) -> None:
        c = ctx()
        rows = {i: {**row(), "row_id": i} for i in (2, 3, 4)}
        rows[4]["source"] = {**rows[4]["source"], "Email": "other@acme.example"}
        evals = {i: evaluate_row(r, c) for i, r in rows.items()}
        apply_duplicates(evals, excluded=set())
        assert "DUPLICATE_IN_FILE" not in codes(evals[2])
        dup = issue(evals[3], "DUPLICATE_IN_FILE")
        assert dup.suggestion == {"first_row_id": 2}
        assert "appears 2 times" in dup.message
        assert evals[3].status == "blocked"
        assert evals[4].status == "ready"

    def test_same_email_different_campaign_is_fine(self) -> None:
        c = ctx()
        a = evaluate_row(row(), c)
        b = evaluate_row(row(Campaign=INACTIVE.id), c)
        evals = {2: a, 3: b}
        apply_duplicates(evals, excluded=set())
        assert "DUPLICATE_IN_FILE" not in codes(b)

    def test_excluded_rows_dont_count(self) -> None:
        c = ctx()
        evals = {2: evaluate_row(row(), c), 3: evaluate_row(row(), c)}
        apply_duplicates(evals, excluded={2})
        assert "DUPLICATE_IN_FILE" not in codes(evals[3])

    def test_excluded_status(self) -> None:
        r = row(Email="")
        r["excluded"] = True
        assert ev(r).status == "excluded"

    def test_summarize(self) -> None:
        assert summarize(["ready", "ready", "blocked", "excluded"]) == {
            "rows_total": 4,
            "rows_ready": 2,
            "rows_warning": 0,
            "rows_blocked": 1,
            "rows_excluded": 1,
            "rows_pending_enrichment": 0,
        }


class TestNormalizer:
    @pytest.mark.parametrize(
        "fields",
        [
            {"email": " Ada@ACME.example ", "phone": "(555) 010-0100", "country": "usa"},
            {"first_name": "  José  ", "company": "Société  Générale", "phone": "+44 20 7946 0958"},
            {"phone": "12345", "country": "Atlantis", "website": "acme.example"},
        ],
    )
    def test_stand_in_is_idempotent(self, fields: dict[str, str]) -> None:
        n = StandInNormalizer()
        once = n.normalize(fields).values
        assert n.normalize(once).values == once

    @pytest.mark.skip(reason="Needs lead normalizer v5 and its synthetic golden fixtures")
    def test_golden_output_matches_v5(self) -> None:
        raise AssertionError("unreachable")

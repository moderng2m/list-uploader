"""Synthetic ZoomInfo answers for the fake Workato client (INTEGRATIONS=fake).

Shapes follow the `[MOPS] ZI Contact Enrich | Callable` output documented in
SPEC §15.1. People and companies are invented. An email whose local part is
"outage" makes the whole batch fail, like a ZoomInfo 5xx.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from shared.workato_client import WorkatoError

_DIRECTORY: dict[str, dict[str, Any]] = {
    "ada@acme.example": {
        "match_status": "high_confidence",
        "match_score": 96,
        "accept_enrichment": True,
        "zi_best_first_name": "Ada",
        "zi_best_last_name": "Example",
        "zi_best_company_name": "Acme Demo Co",
        "zi_best_job_title": "VP Marketing",
        "zi_best_phone": "+1 555 010 0150",
        "zi_best_direct_phone_do_not_call": True,
        "zi_best_mobile_phone": "+1 555 010 0151",
        "zi_best_linkedin_url": "https://www.linkedin.com/in/ada-example-demo",
        "zi_best_linkedin_profile_count": 2,
        "zi_best_company_website": "acme.example",
        "zi_best_company_employee_count": 250,
        "zi_best_company_primary_industry": ["Software"],
        "zi_best_company_naics_codes": [{"id": "541511", "name": "Custom Computer Programming"}],
        "zi_best_contact_id": "1000000001",
        "zi_best_company_id": "2000000001",
    },
    "kay@pied.example": {
        "match_status": "likely_match",
        "match_score": 88,
        "accept_enrichment": True,
        "zi_best_company_name": "Pied Piper Demo",
        "zi_best_job_title": "CTO",
        "zi_best_linkedin_url": "linkedin.com/in/kay-sample-demo",
        "zi_best_linkedin_profile_count": 1,
        "zi_best_city": "Palo Alto",
        "zi_best_state": "CA",
        "zi_best_country": "United States",
    },
    "linus@hooli.example": {
        "match_status": "possible_match",
        "match_score": 71,
        "accept_enrichment": False,
        "needs_human_review": True,
        "conflicts_csv": "current_company_mismatch,title_mismatch",
        "selected_candidate_json": json.dumps(
            {
                "firstName": "Linus",
                "lastName": "Sample",
                "companyName": "Hooli XYZ Demo",
                "jobTitle": "Head of Platform",
                "email": "linus.sample@hooli.example",
                "linkedinUrl": "linkedin.com/in/linus-sample-demo",
            }
        ),
    },
    "info@vandelay.example": {"match_status": "invalid_input", "accept_enrichment": False},
}


def enrich_handler(contacts: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    if any(str(c.get("email", "")).startswith("outage@") for c in contacts):
        raise WorkatoError("ZoomInfo returned 503 (synthetic outage)")
    choices = []
    for c in contacts:
        email = str(c.get("email", "")).casefold()
        found = _DIRECTORY.get(email, {"match_status": "no_match", "accept_enrichment": False})
        choices.append({"source_record_id": c.get("source_record_id"), **found})
    return {"best_choices": choices, "batch_count": len(contacts)}

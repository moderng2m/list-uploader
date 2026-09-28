from __future__ import annotations

import io
import json
import logging

from shared.observability import REDACTED, get_logger, scrub

EMAIL = "jane.doe@example.com"
PHONE = "+1 (555) 123-4567"
FIRST = "Jane"
LAST = "Doerington"


def _capture() -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    log = get_logger()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(log.registered_formatter)
    log._logger.handlers = [handler]
    return log, buf


def test_log_call_with_email_phone_and_name_emits_none_of_them() -> None:
    log, buf = _capture()
    log.info(
        f"processing {EMAIL} at {PHONE}",
        extra={
            "first_name": FIRST,
            "last_name": LAST,
            "email": EMAIL,
            "row": {"First name": FIRST, "Business Phone": "5551234567", "note": f"call {PHONE}"},
            "job_id": "j_01J9ABC",
            "row_count": 250,
        },
    )
    out = buf.getvalue()
    for secret in (EMAIL, PHONE, FIRST, LAST, "5551234567"):
        assert secret not in out, secret

    record = json.loads(out)
    assert record["job_id"] == "j_01J9ABC"
    assert record["row_count"] == 250
    assert record["first_name"] == REDACTED
    assert "timestamp" in record


def test_scrub_keeps_dates_and_counts() -> None:
    assert scrub("parsed 4999 rows on 2026-09-23") == "parsed 4999 rows on 2026-09-23"


def test_scrub_redacts_nested_values() -> None:
    assert scrub({"a": [{"email": "x"}, "reach me at a@b.co"]}) == {
        "a": [{"email": REDACTED}, f"reach me at {REDACTED}"]
    }

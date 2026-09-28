"""Logging, tracing, and metrics for every Lambda (SPEC §21.3.2).

Logs never contain PII. Every record passes through `scrub()` before it is
serialized: values under PII-named keys are replaced, and email addresses and
phone numbers are redacted from all other string values, including the message.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.logging.formatter import LambdaPowertoolsFormatter
from aws_lambda_powertools.logging.types import PowertoolsLogRecord

SERVICE = "list-uploader"
METRICS_NAMESPACE = "ListUploader"
REDACTED = "[REDACTED]"

# Normalized key names (lowercase, non-alphanumerics removed) whose values are
# always redacted. `before`/`after` carry audit values and never belong in logs.
_PII_KEYS = frozenset(
    {
        "email",
        "emailaddress",
        "workemail",
        "cleanemail",
        "phone",
        "businessphone",
        "mobilephone",
        "mobile",
        "phonenumber",
        "cleanphonee164",
        "cleanmobilephonee164",
        "name",
        "fullname",
        "firstname",
        "lastname",
        "cleanfirstname",
        "cleanlastname",
        "before",
        "after",
        "source",
        "processed",
    }
)

# Powertools' own fields are structural and must stay readable.
_STRUCTURAL_KEYS = frozenset(
    {
        "level",
        "location",
        "timestamp",
        "service",
        "sampling_rate",
        "xray_trace_id",
        "cold_start",
        "function_name",
        "function_memory_size",
        "function_arn",
        "function_request_id",
    }
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A run of digits and phone punctuation; redacted only if it holds 9+ digits,
# so counts and dates survive.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w-])\+?\(?\d[\d\s().-]{6,}\d(?![\w-])")
_MIN_PHONE_DIGITS = 9


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _scrub_string(value: str) -> str:
    value = _EMAIL_RE.sub(REDACTED, value)

    def _phone(match: re.Match[str]) -> str:
        digits = sum(ch.isdigit() for ch in match.group(0))
        return REDACTED if digits >= _MIN_PHONE_DIGITS else match.group(0)

    return _PHONE_CANDIDATE_RE.sub(_phone, value)


def scrub(value: Any) -> Any:
    """Return a copy of `value` with PII redacted."""
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if _normalize_key(str(k)) in _PII_KEYS else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _scrub_string(value)
    return value


class ScrubbingFormatter(LambdaPowertoolsFormatter):
    """Powertools JSON formatter that scrubs PII from every record."""

    def serialize(self, log: dict[str, Any] | PowertoolsLogRecord) -> str:
        structural = {k: v for k, v in log.items() if k in _STRUCTURAL_KEYS}
        rest = scrub({k: v for k, v in log.items() if k not in _STRUCTURAL_KEYS})
        return super().serialize({**structural, **rest})


def env_name() -> str:
    return os.environ.get("ENV", "dev")


def get_logger(child: bool = False) -> Logger:
    return Logger(service=SERVICE, logger_formatter=ScrubbingFormatter(), child=child)


logger = get_logger()
tracer = Tracer(service=SERVICE)
metrics = Metrics(namespace=METRICS_NAMESPACE, service=SERVICE)
metrics.set_default_dimensions(env=env_name())

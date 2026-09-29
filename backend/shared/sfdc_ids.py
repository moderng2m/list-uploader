"""Salesforce ID checks for campaign IDs (SPEC §11.3).

The 18-character form appends a 3-character checksum to the case-sensitive
15-character ID: for each 5-character chunk, bit i is set when character i is
an uppercase A-Z, and the 5-bit value indexes `_SUFFIX_ALPHABET`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SUFFIX_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
_ALNUM = re.compile(r"^[A-Za-z0-9]+$")
CAMPAIGN_PREFIX = "701"


def checksum_suffix(id15: str) -> str:
    if len(id15) != 15:
        raise ValueError("expected a 15-character ID")
    suffix = ""
    for start in range(0, 15, 5):
        bits = 0
        for i, ch in enumerate(id15[start : start + 5]):
            if "A" <= ch <= "Z":
                bits |= 1 << i
        suffix += _SUFFIX_ALPHABET[bits]
    return suffix


def to_18(id15: str) -> str:
    return id15 + checksum_suffix(id15)


@dataclass(frozen=True)
class CampaignIdCheck:
    value: str | None  # the 18-character ID when valid
    problem: str | None  # None, "format", "prefix", "checksum"
    converted_from_15: bool = False


def check_campaign_id(raw: str) -> CampaignIdCheck:
    text = raw.strip()
    if len(text) not in (15, 18) or not _ALNUM.match(text):
        return CampaignIdCheck(None, "format")
    if not text.startswith(CAMPAIGN_PREFIX):
        return CampaignIdCheck(None, "prefix")
    if len(text) == 15:
        return CampaignIdCheck(to_18(text), None, converted_from_15=True)
    if checksum_suffix(text[:15]) != text[15:]:
        return CampaignIdCheck(None, "checksum")
    return CampaignIdCheck(text, None)

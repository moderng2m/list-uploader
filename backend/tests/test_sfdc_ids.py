from __future__ import annotations

import pytest

from shared.sfdc_ids import check_campaign_id, checksum_suffix, to_18

# Known 15 -> 18 pairs. The first is the widely published Salesforce example;
# the others are worked by hand in the comments so the test doesn't only check
# the implementation against itself.
KNOWN_PAIRS = [
    ("001A0000006Vm9r", "001A0000006Vm9rIAC"),
    # 70100|00000|00001: no uppercase anywhere -> A A A
    ("701000000000001", "701000000000001AAA"),
    # "701Ab": A at index 3 -> 8 -> I;  "C0000": C at 0 -> 1 -> B;  "0000Z": Z at 4 -> 16 -> Q
    ("701AbC00000000Z", "701AbC00000000ZIBQ"),
    # "ABCDE": all five -> 31 -> 5;  "FGHIJ" -> 5;  "KLMNO" -> 5
    ("ABCDEFGHIJKLMNO", "ABCDEFGHIJKLMNO555"),
]


@pytest.mark.parametrize(("id15", "id18"), KNOWN_PAIRS)
def test_known_pairs(id15: str, id18: str) -> None:
    assert to_18(id15) == id18


def test_checksum_rejects_wrong_length() -> None:
    with pytest.raises(ValueError):
        checksum_suffix("701")


class TestCheckCampaignId:
    def test_15_char_is_converted(self) -> None:
        check = check_campaign_id(" 701000000000001 ")
        assert check.value == "701000000000001AAA"
        assert check.converted_from_15

    def test_valid_18(self) -> None:
        assert check_campaign_id("701AbC00000000ZIBQ").value == "701AbC00000000ZIBQ"

    def test_case_change_breaks_checksum(self) -> None:
        # Same ID retyped in lowercase: the suffix no longer matches.
        assert check_campaign_id("701abc00000000zibq").problem == "checksum"

    def test_bad_suffix(self) -> None:
        assert check_campaign_id("701000000000001AAB").problem == "checksum"

    @pytest.mark.parametrize("raw", ["", "701", "7010000000000011", "701-00000000001"])
    def test_format(self, raw: str) -> None:
        assert check_campaign_id(raw).problem == "format"

    def test_not_a_campaign(self) -> None:
        assert check_campaign_id("001A0000006Vm9rIAC").problem == "prefix"

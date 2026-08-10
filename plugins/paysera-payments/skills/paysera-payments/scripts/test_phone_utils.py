"""Tests for phone_utils.format_e164."""

import pytest
from phone_utils import format_e164, PhoneFormatError


# ---------------------------------------------------------------------------
# Happy-path: already-international inputs
# ---------------------------------------------------------------------------

class TestInternationalPrefix:
    def test_plus_no_spaces(self):
        assert format_e164("+37061234567") == "+37061234567"

    def test_plus_with_spaces(self):
        assert format_e164("+370 612 34567") == "+37061234567"

    def test_plus_with_dashes(self):
        assert format_e164("+370-612-34567") == "+37061234567"

    def test_plus_with_mixed_separators(self):
        assert format_e164("+1 (202) 555-0173") == "+12025550173"

    def test_idd_00_prefix(self):
        assert format_e164("0037061234567") == "+37061234567"

    def test_idd_00_with_separators(self):
        assert format_e164("00 370 612-34 567") == "+37061234567"

    def test_uk_number(self):
        assert format_e164("+447911123456") == "+447911123456"

    def test_us_number(self):
        assert format_e164("+12025550173") == "+12025550173"

    def test_germany(self):
        assert format_e164("+4915123456789") == "+4915123456789"

    def test_latvia(self):
        assert format_e164("+37129123456") == "+37129123456"


# ---------------------------------------------------------------------------
# Happy-path: local/national format with default_country_code
# ---------------------------------------------------------------------------

class TestLocalFormat:
    def test_local_number_no_trunk(self):
        # Lithuanian national number without trunk prefix: 61234567
        assert format_e164("61234567", default_country_code="370") == "+37061234567"

    def test_local_number_with_dashes(self):
        assert format_e164("612 34-567", default_country_code="370") == "+37061234567"

    def test_default_cc_with_plus(self):
        # default_country_code may have a leading '+' — should be stripped
        assert format_e164("61234567", default_country_code="+370") == "+37061234567"

    def test_local_us_number(self):
        assert format_e164("2025550173", default_country_code="1") == "+12025550173"

    def test_local_de_number(self):
        assert format_e164("15123456789", default_country_code="49") == "+4915123456789"


# ---------------------------------------------------------------------------
# Country-code validation
# ---------------------------------------------------------------------------

class TestCountryCodeValidation:
    def test_invalid_cc_in_plus_number(self):
        # Country code "999" is not assigned
        with pytest.raises(PhoneFormatError, match="No valid ITU country code"):
            format_e164("+99912345678")

    def test_invalid_default_cc(self):
        with pytest.raises(PhoneFormatError, match="not a recognised ITU country code"):
            format_e164("61234567", default_country_code="999")

    def test_default_cc_non_digits(self):
        with pytest.raises(PhoneFormatError, match="digits only"):
            format_e164("61234567", default_country_code="LT")


# ---------------------------------------------------------------------------
# Subscriber-length guards
# ---------------------------------------------------------------------------

class TestSubscriberLength:
    def test_too_short(self):
        with pytest.raises(PhoneFormatError, match="too short"):
            format_e164("+370123")   # subscriber "123" = 3 digits

    def test_too_long_exceeds_15_total(self):
        with pytest.raises(PhoneFormatError, match="E.164 maximum"):
            format_e164("+370" + "1" * 13)   # 3 + 13 = 16 digits total

    def test_exactly_15_digits_ok(self):
        # CC=370 (3) + 12 subscriber digits = 15 total
        num = format_e164("+370" + "1" * 12)
        assert num == "+370" + "1" * 12


# ---------------------------------------------------------------------------
# Edge cases and error handling
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_string(self):
        with pytest.raises(PhoneFormatError, match="empty"):
            format_e164("")

    def test_whitespace_only(self):
        with pytest.raises(PhoneFormatError, match="empty"):
            format_e164("   ")

    def test_non_string_input(self):
        with pytest.raises(PhoneFormatError, match="Expected str"):
            format_e164(37061234567)  # type: ignore[arg-type]

    def test_no_prefix_no_default(self):
        with pytest.raises(PhoneFormatError, match="default_country_code"):
            format_e164("861234567")

    def test_letters_after_plus(self):
        with pytest.raises(PhoneFormatError, match="Non-digit"):
            format_e164("+370abc1234")

    def test_letters_after_idd(self):
        with pytest.raises(PhoneFormatError, match="Non-digit"):
            format_e164("0037xyz1234")


# ---------------------------------------------------------------------------
# Separator coverage — every separator type the regex claims to handle
# ---------------------------------------------------------------------------

class TestSeparators:
    def test_dot_separators(self):
        assert format_e164("+370.612.34567") == "+37061234567"

    def test_slash_separators(self):
        assert format_e164("+370/612/34567") == "+37061234567"

    def test_plus_with_space_before_digits(self):
        # "+ 370 61234567" — space right after '+' gets stripped by _strip_formatting
        assert format_e164("+ 370 61234567") == "+37061234567"


# ---------------------------------------------------------------------------
# Subscriber boundary — minimum exactly 4 digits succeeds
# ---------------------------------------------------------------------------

class TestSubscriberBoundary:
    def test_exactly_4_subscriber_digits_ok(self):
        # CC=370 (3) + subscriber=1234 (4) = 7 total, well under 15
        assert format_e164("+3701234") == "+3701234"

    def test_3_subscriber_digits_rejected(self):
        # Existing test covers this but make the boundary explicit from below too
        with pytest.raises(PhoneFormatError, match="too short"):
            format_e164("+370123")


# ---------------------------------------------------------------------------
# 1-digit country codes
# ---------------------------------------------------------------------------

class TestOneDigitCountryCode:
    def test_russia_1digit_cc(self):
        assert format_e164("+79161234567") == "+79161234567"

    def test_nanp_via_idd(self):
        # IDD form: 001 + NANP number
        assert format_e164("0012025550173") == "+12025550173"


# ---------------------------------------------------------------------------
# default_country_code whitespace / '+' normalisation
# ---------------------------------------------------------------------------

class TestDefaultCountryCodeNormalisation:
    def test_leading_space_in_default_cc(self):
        # Users sometimes pass " 370" — should still work after .strip()
        # NOTE: code does .lstrip("+").strip() — leading space survives lstrip("+")
        # then .strip() removes it, so this should pass
        assert format_e164("61234567", default_country_code=" 370") == "+37061234567"

    def test_trailing_space_in_default_cc(self):
        assert format_e164("61234567", default_country_code="370 ") == "+37061234567"

    def test_empty_default_cc_raises(self):
        with pytest.raises(PhoneFormatError, match="digits only"):
            format_e164("61234567", default_country_code="")


# ---------------------------------------------------------------------------
# IDD prefix edge cases
# ---------------------------------------------------------------------------

class TestIDDEdgeCases:
    def test_idd_immediately_non_digit(self):
        # "00xyz" — after stripping formatting chars: "00xyz" still; digit_part="xyz"
        with pytest.raises(PhoneFormatError, match="Non-digit"):
            format_e164("00xyz")

    def test_idd_with_unrecognised_cc(self):
        # "00999…" — digit_part starts with "999", no valid CC
        with pytest.raises(PhoneFormatError, match="No valid ITU country code"):
            format_e164("009991234567")

    def test_bare_idd_prefix_raises(self):
        # "00" alone — digit_part="" → isdigit() is False → Non-digit error
        with pytest.raises(PhoneFormatError):
            format_e164("00")


# ---------------------------------------------------------------------------
# Non-string input types
# ---------------------------------------------------------------------------

class TestNonStringInputTypes:
    def test_none_input(self):
        with pytest.raises(PhoneFormatError, match="Expected str"):
            format_e164(None)  # type: ignore[arg-type]

    def test_list_input(self):
        with pytest.raises(PhoneFormatError, match="Expected str"):
            format_e164(["+37061234567"])  # type: ignore[arg-type]

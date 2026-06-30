"""
test_normalizers.py — unit tests for transformer/normalizers.py.

Includes a regression test for a real bug found and fixed during
implementation: normalize_phone was prepending a duplicate '+' to any
input that already started with '+' (e.g. "+14155550192" -> "++14155550192"),
which corrupted already-E.164 phones and broke idempotency. See the
BUGFIX comment in normalizers.py for details.
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transformer.normalizers import (
    normalize_phone, normalize_date, normalize_skill, normalize_skills,
    normalize_country, normalize_email, normalize_name,
)


# ─── normalize_phone ─────────────────────────────────────────────────────────

def test_phone_explicit_us_country_code():
    assert normalize_phone("(415) 555-0192", default_country_code="1") == "+14155550192"


def test_phone_bare_ten_digit_uses_india_default():
    # Default country code is now India (+91) — see normalizers.py CHANGED
    # comment; the sample dataset is India-focused.
    assert normalize_phone("9811023456") == "+919811023456"


def test_phone_already_e164_unchanged():
    # Regression test: this used to return "++14155550192" before the fix.
    assert normalize_phone("+14155550192") == "+14155550192"


def test_phone_is_idempotent():
    once = normalize_phone("+14155550192")
    twice = normalize_phone(once)
    assert once == twice == "+14155550192"


def test_phone_international_with_plus():
    assert normalize_phone("+886912345678") == "+886912345678"


def test_phone_ten_digit_gets_default_country_code():
    # Default country code is India (+91) — see normalizers.py BUGFIX/CHANGED
    # comment; the sample dataset is India-focused.
    assert normalize_phone("9876543210") == "+919876543210"


def test_phone_too_short_returns_none():
    assert normalize_phone("123") is None


def test_phone_empty_or_none_returns_none():
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_phone_non_phone_garbage_returns_none():
    assert normalize_phone("call me maybe") is None


# ─── normalize_date ──────────────────────────────────────────────────────────

def test_date_yyyy_mm():
    assert normalize_date("2020-03") == "2020-03"


def test_date_mm_yyyy():
    assert normalize_date("03/2020") == "2020-03"


def test_date_month_name_year():
    assert normalize_date("March 2020") == "2020-03"
    assert normalize_date("Mar 2020") == "2020-03"


def test_date_year_month_name():
    assert normalize_date("2020 March") == "2020-03"


def test_date_year_only():
    assert normalize_date("2020") == "2020"


def test_date_present_returns_none():
    assert normalize_date("Present") is None
    assert normalize_date("present") is None
    assert normalize_date("Current") is None


def test_date_unparseable_returns_none():
    assert normalize_date("sometime last year") is None


def test_date_empty_or_none_returns_none():
    assert normalize_date("") is None
    assert normalize_date(None) is None


# ─── normalize_skill / normalize_skills ─────────────────────────────────────

def test_skill_known_alias():
    assert normalize_skill("python3") == "Python"
    assert normalize_skill("JS") == "JavaScript"
    assert normalize_skill("reactjs") == "React"


def test_skill_unknown_falls_back_to_title_case():
    assert normalize_skill("rust") == "Rust"


def test_skill_empty_returns_empty():
    assert normalize_skill("") == ""
    assert normalize_skill(None) == ""


def test_skills_dedup_by_canonical_name():
    result = normalize_skills(["python3", "Python", "PYTHON", "py"])
    assert result == ["Python"]


def test_skills_preserves_order_of_first_occurrence():
    result = normalize_skills(["aws", "python3", "AWS"])
    assert result == ["AWS", "Python"]


# ─── normalize_country ───────────────────────────────────────────────────────

def test_country_known_alias():
    assert normalize_country("United States") == "US"
    assert normalize_country("usa") == "US"
    assert normalize_country("india") == "IN"


def test_country_already_alpha2():
    assert normalize_country("us") == "US"
    assert normalize_country("GB") == "GB"


def test_country_remote_returns_none():
    assert normalize_country("Remote") is None


def test_country_unknown_returns_none():
    assert normalize_country("Atlantis") is None


def test_country_empty_returns_none():
    assert normalize_country("") is None
    assert normalize_country(None) is None


# ─── normalize_email ──────────────────────────────────────────────────────────

def test_email_lowercases_and_strips():
    assert normalize_email("  Jane.Doe@Example.COM  ") == "jane.doe@example.com"


def test_email_invalid_returns_none():
    assert normalize_email("not-an-email") is None
    assert normalize_email("missing@nodot") is None


def test_email_empty_returns_none():
    assert normalize_email("") is None
    assert normalize_email(None) is None


# ─── normalize_name ───────────────────────────────────────────────────────────

def test_name_collapses_whitespace_and_title_cases():
    assert normalize_name("  jane   doe  ") == "Jane Doe"


def test_name_too_short_returns_none():
    assert normalize_name("J") is None


def test_name_empty_returns_none():
    assert normalize_name("") is None
    assert normalize_name(None) is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
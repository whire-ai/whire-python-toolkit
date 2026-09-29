"""Local validation matrices shared by the client and the toolkit: amounts, datetimes, mandate references (SPEC §3, §9)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from whire import MANDATE_REFERENCE_MAX_LENGTH, InvalidInputError
from whire import _validation as V

UTC = timezone.utc

VALID_AMOUNTS: list[tuple[Any, Decimal]] = [
    (50, Decimal("50")), (0.01, Decimal("0.01")), (1e2, Decimal("100")), ("50.00", Decimal("50.00")), ("  12.5 ", Decimal("12.5")),
    (20.1, Decimal("20.1")),  # floats go through their shortest repr, never their binary expansion
    (Decimal("9999999999999.99"), Decimal("9999999999999.99")),  # just below the 1e13 cap
]

INVALID_AMOUNTS: list[tuple[Any, str]] = [
    (True, "must be a number, not bool"), (None, "must be a number"), ([50], "must be a number, not list"),
    (float("nan"), "finite"), (Decimal("Infinity"), "finite"),
    (0, "greater than zero"), ("0.00", "greater than zero"), (-0.5, "greater than zero"),
    (1.005, "two decimal places"), ("50.000", "two decimal places"), (Decimal("1.000"), "two decimal places"),
    (0.1 + 0.2, "two decimal places"),  # repr is 0.30000000000000004
    ("abc", "not a valid decimal"), ("12,50", "not a valid decimal"),
    ("1e400", "too large"), (Decimal("99999999999999.99"), "too large"),  # would lose the last cent as an IEEE double
]


def test_amount_matrix() -> None:
    for value, expected in VALID_AMOUNTS:
        amount = V.to_amount(value)
        assert isinstance(amount, Decimal) and amount == expected, value
    assert V.to_amount(20.1).as_tuple().exponent == -1 and str(V.to_amount("50.00")) == "50.00"  # scale as given
    for value, fragment in INVALID_AMOUNTS:
        with pytest.raises(InvalidInputError) as info:
            V.to_amount(value)
        assert fragment in str(info.value) and str(info.value).startswith("amount"), value
        assert info.value.error_code == "invalid_input" and info.value.is_input_error
    with pytest.raises(InvalidInputError, match="^max_amount must be greater than zero"):
        V.to_amount(0, field="max_amount")


def test_amount_to_json_matches_server_expectations() -> None:
    for amount, expected in ((Decimal("50"), 50), (Decimal("50.00"), 50), (Decimal("20.10"), 20.1), (Decimal("20.01"), 20.01)):
        value = V.amount_to_json(amount)
        assert value == expected and type(value) is type(expected), amount
    assert V.amount_to_json(V.to_amount("20.10")) == 20.1 and V.amount_to_json(V.to_amount(20.01)) == 20.01


def test_datetime_rules() -> None:
    with pytest.raises(InvalidInputError, match="valid_from.*timezone-aware"):
        V.to_iso_datetime(datetime(2026, 9, 23, 18, 42, 40), field="valid_from")  # naive
    assert V.to_iso_datetime(datetime(2026, 9, 23, 18, 42, 40, 612345, tzinfo=UTC), field="x") == "2026-09-23T18:42:40.612Z"
    offset = datetime(2026, 9, 23, 20, 42, 40, 5000, tzinfo=timezone(timedelta(hours=2)))
    assert V.to_iso_datetime(offset, field="x") == "2026-09-23T18:42:40.005Z"  # converted to UTC, milliseconds
    assert V.to_iso_datetime(datetime(2026, 1, 1, tzinfo=UTC), field="x") == "2026-01-01T00:00:00.000Z"
    assert V.to_iso_datetime(date(2026, 9, 23), field="x") == "2026-09-23"
    for text in ("2026-09-23", "2026-09-23T18:42:40.612Z", "2026-09-23T18:42:40+00:00", "2026-09-23T18:42:40"):
        assert V.to_iso_datetime(f"  {text}  ", field="x") == text  # ISO strings are sent as given, stripped
    for value in ("", "tomorrow", "23/09/2026", "2026-13-01", "2026-09-23T25:00:00Z", 1_700_000_000, None):
        with pytest.raises(InvalidInputError, match="valid_until"):
            V.to_iso_datetime(value, field="valid_until")  # type: ignore[arg-type]


def test_mandate_reference_rules() -> None:
    for reference in ("SHOP-001", "A", "x" * MANDATE_REFERENCE_MAX_LENGTH, "INV 2026/09 (Q3), ok:+?.'", "a/b/c", " spaces ok "):
        assert V.validate_mandate_reference(reference) == reference
    for reference, fragment in (
        ("", "1-35 characters"),
        ("x" * (MANDATE_REFERENCE_MAX_LENGTH + 1), "1-35 characters"),
        ("/lead", "start or end with '/'"),
        ("trail/", "start or end with '/'"),
        ("a//b", "contain '//'"),
        ("bad_underscore", "may only contain"),
        ("ümlaut", "may only contain"),
        ("tab\there", "may only contain"),
    ):
        with pytest.raises(InvalidInputError) as info:
            V.validate_mandate_reference(reference)
        assert fragment in str(info.value) and str(info.value).startswith("mandate_reference"), reference
    for value in (None, 123):
        with pytest.raises(InvalidInputError, match="must be a string"):
            V.validate_mandate_reference(value)  # type: ignore[arg-type]

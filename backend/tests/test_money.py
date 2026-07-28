"""Tests for the single money-parsing chokepoint.

Everything that reads a price, a count or a fee off the wire goes through
``_to_decimal``, so this is the one place a bad number can be stopped before
it reaches arithmetic that will not complain about it.

The property that matters here is **non-finite refusal**. ``Decimal("NaN")``
is a perfectly valid Decimal — it constructs without complaint, compares
without raising, and only detonates later inside the fee engine as an
``InvalidOperation``, which is an ``ArithmeticError`` and therefore slips past
every ``ValueError`` handler wrapped around money parsing. Observed: a 500
from ``/api/fees/quote?price_dollars=NaN``, and proposal 267 stored and served
with ``net_edge_cents: "NaN"`` after executing a real order against the demo
exchange.

The subtle half is the ``Decimal`` passthrough: a NaN built upstream and
handed in as a ``Decimal`` skipped the string parse entirely, so the check has
to live *after* coercion rather than inside the string branch.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.money import (
    MoneyParseError,
    cents_to_dollars,
    format_count,
    format_dollars,
    parse_count,
    parse_dollars,
    quantize_count,
    quantize_price,
)

# ---------------------------------------------------------------------------
# Non-finite refusal
# ---------------------------------------------------------------------------

#: Every spelling of a non-finite value the Decimal constructor accepts.
#: Case is deliberately mixed: ``Decimal`` is case-insensitive about these,
#: so a guard that only matched "NaN" exactly would be trivially bypassed.
NON_FINITE_STRINGS = [
    "NaN",
    "nan",
    "-NaN",
    "sNaN",
    "-sNaN",
    "Infinity",
    "-Infinity",
    "inf",
    "-inf",
    "INF",
]


class TestNonFiniteStrings:
    @pytest.mark.parametrize("value", NON_FINITE_STRINGS)
    def test_a_non_finite_price_is_refused(self, value: str) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(value)

    @pytest.mark.parametrize("value", NON_FINITE_STRINGS)
    def test_a_non_finite_count_is_refused(self, value: str) -> None:
        with pytest.raises(MoneyParseError):
            parse_count(value)

    @pytest.mark.parametrize("value", NON_FINITE_STRINGS)
    def test_a_non_finite_cents_value_is_refused(self, value: str) -> None:
        """``cents_to_dollars`` parses too, so it needs the same guard."""
        with pytest.raises(MoneyParseError):
            cents_to_dollars(value)

    def test_surrounding_whitespace_does_not_smuggle_one_through(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars("  NaN  ")

    def test_the_message_names_the_field_and_the_value(self) -> None:
        """A refusal an operator can act on names both halves."""
        with pytest.raises(MoneyParseError) as exc:
            parse_dollars("NaN", "fair_price")
        assert "fair_price" in str(exc.value)
        assert "NaN" in str(exc.value)

    def test_the_error_is_a_valueerror(self) -> None:
        """The API layer catches ``ValueError`` around money parsing.

        ``InvalidOperation`` — what a NaN raised further downstream — is an
        ``ArithmeticError``, which those handlers do not catch, which is how
        this became a bare HTTP 500 rather than a 400.
        """
        assert issubclass(MoneyParseError, ValueError)
        with pytest.raises(ValueError):
            parse_dollars("Infinity")


class TestNonFiniteFloats:
    """The API never sends floats, but internal callers can."""

    def test_float_nan_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(float("nan"))

    def test_float_infinity_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(float("inf"))
        with pytest.raises(MoneyParseError):
            parse_dollars(float("-inf"))

    def test_float_nan_is_refused_as_a_count_too(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_count(float("nan"))


class TestDecimalPassthrough:
    """The subtle hole: a Decimal is returned as-is by the coercion step.

    ``_coerce_decimal`` short-circuits on ``isinstance(value, Decimal)``, so a
    NaN constructed anywhere upstream never touches the string parser. The
    finite check has to sit *outside* that branch or this path stays open.
    """

    def test_a_decimal_nan_passed_directly_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(Decimal("NaN"))

    def test_a_decimal_signaling_nan_passed_directly_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(Decimal("sNaN"))

    def test_a_decimal_infinity_passed_directly_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(Decimal("Infinity"))
        with pytest.raises(MoneyParseError):
            parse_dollars(Decimal("-Infinity"))

    def test_a_decimal_nan_is_refused_as_a_count_too(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_count(Decimal("NaN"))

    def test_an_ordinary_decimal_still_passes_through_unchanged(self) -> None:
        value = Decimal("0.5600")
        parsed = parse_dollars(value)
        assert parsed == value
        # Exponent preserved: the guard must not quantize on its way past.
        assert str(parsed) == "0.5600"


# ---------------------------------------------------------------------------
# Ordinary values still parse — the guard must not over-refuse
# ---------------------------------------------------------------------------


class TestOrdinaryValuesStillParse:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.5600", Decimal("0.5600")),
            ("0.005", Decimal("0.005")),
            ("10.50", Decimal("10.50")),
            ("0.000001", Decimal("0.000001")),
            ("1", Decimal(1)),
            ("0", Decimal(0)),
        ],
    )
    def test_wire_shaped_strings_parse(self, raw: str, expected: Decimal) -> None:
        assert parse_dollars(raw) == expected

    def test_the_string_form_is_preserved_exactly(self) -> None:
        """Never round a quote on ingest: tick size varies per market, so
        sub-cent prices are real."""
        assert str(parse_dollars("0.5600")) == "0.5600"
        assert str(parse_count("10.00")) == "10.00"

    @pytest.mark.parametrize("raw", [0, 1, 56, -3])
    def test_ints_parse(self, raw: int) -> None:
        """Range checking is the pricing layer's job, not this one's.

        ``parse_dollars(56)`` is a valid *parse* of $56; it is
        ``price_ticket`` that refuses it as a cents-style price.
        """
        assert parse_dollars(raw) == Decimal(raw)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("-1.50", Decimal("-1.50")),
            ("-0.0175", Decimal("-0.0175")),
        ],
    )
    def test_negative_amounts_parse(self, raw: str, expected: Decimal) -> None:
        """Legal here: a realised P&L or an edge may be negative, and this
        function is shared by all of them."""
        assert parse_dollars(raw) == expected

    def test_cents_convert_to_dollars(self) -> None:
        assert cents_to_dollars("175") == Decimal("1.75")
        assert cents_to_dollars(Decimal("1.75")) == Decimal("0.0175")

    def test_a_float_that_is_finite_still_parses_via_str(self) -> None:
        """Converting via ``str`` limits the damage already done upstream."""
        assert parse_dollars(0.56) == Decimal("0.56")


class TestStillRefusesTheOtherBadInputs:
    """The non-finite guard is additive; nothing it was already catching may
    have started passing."""

    def test_an_empty_string_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars("")
        with pytest.raises(MoneyParseError):
            parse_dollars("   ")

    def test_nonsense_raises_rather_than_becoming_zero(self) -> None:
        """A price that quietly reads as 0 looks like free money."""
        with pytest.raises(MoneyParseError):
            parse_dollars("banana")

    def test_a_bool_is_refused(self) -> None:
        """``True`` is an ``int`` in Python and would otherwise price at $1."""
        with pytest.raises(MoneyParseError):
            parse_dollars(True)

    def test_an_unsupported_type_is_refused(self) -> None:
        with pytest.raises(MoneyParseError):
            parse_dollars(None)
        with pytest.raises(MoneyParseError):
            parse_dollars(["0.56"])


class TestFormatting:
    """Unchanged by the guard, asserted so a regression here is visible."""

    def test_price_quantization_keeps_six_places(self) -> None:
        assert quantize_price(Decimal("0.5600004")) == Decimal("0.56")

    def test_counts_quantize_to_a_hundredth(self) -> None:
        assert quantize_count(Decimal("10.004")) == Decimal("10.00")

    def test_format_dollars_pads_to_four_places_by_default(self) -> None:
        assert format_dollars(Decimal("0.56")) == "0.5600"

    def test_format_count_renders_two_places(self) -> None:
        assert format_count(Decimal(10)) == "10.00"

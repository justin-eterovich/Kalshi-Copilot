"""Money and quantity handling for Kalshi's fixed-point API.

The API exchanges money and size as **decimal strings**, not integers:

- ``FixedPointDollars`` — a dollar amount with up to 6 decimal places
  (``"0.5600"``). Tick size varies by market via ``price_level_structure``,
  so sub-cent prices are real and must not be rounded away on ingest.
- ``FixedPointCount`` — a contract count with 2 decimals (``"10.00"``).
  Fractional contracts are supported down to 0.01.

Everything internal therefore uses :class:`~decimal.Decimal`, never float.
Prices are carried in **dollars**; edges and fees are reported in **cents**
because that is the unit a trader reads.

Parsing is strict: a malformed number from the wire raises rather than
silently becoming zero, because a price that quietly reads as 0 would look
like an enormous edge.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, DecimalException
from typing import Any, Final

__all__ = [
    "DOLLAR",
    "CENT",
    "COUNT_QUANTUM",
    "PRICE_QUANTUM",
    "MoneyParseError",
    "parse_dollars",
    "parse_count",
    "dollars_to_cents",
    "cents_to_dollars",
    "format_dollars",
    "format_count",
    "quantize_price",
    "quantize_count",
]

DOLLAR: Final = Decimal("1")
CENT: Final = Decimal("0.01")
#: Maximum precision the API documents for prices.
PRICE_QUANTUM: Final = Decimal("0.000001")
#: Minimum contract granularity.
COUNT_QUANTUM: Final = Decimal("0.01")


class MoneyParseError(ValueError):
    """A price or count from the wire could not be parsed."""


def _to_decimal(value: Any, field: str) -> Decimal:
    return _reject_non_finite(_coerce_decimal(value, field), value, field)


def _coerce_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise MoneyParseError(f"{field}: refusing to read bool {value!r} as a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # The API never sends floats; if one appears, something upstream has
        # already lost precision. Convert via str to limit the damage.
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyParseError(f"{field}: empty string is not a number")
        try:
            return Decimal(text)
        except DecimalException as exc:
            raise MoneyParseError(f"{field}: cannot parse {value!r}") from exc
    raise MoneyParseError(f"{field}: unsupported type {type(value).__name__}")


def _reject_non_finite(parsed: Decimal, original: Any, field: str) -> Decimal:
    """Refuse NaN and the infinities.

    ``Decimal("NaN")`` is a perfectly valid Decimal, so the parse above
    accepts it and every downstream comparison then raises
    ``InvalidOperation`` — an ``ArithmeticError``, which the ``ValueError``
    handlers around money parsing do not catch. Observed: a 500 from
    ``/api/fees/quote?price_dollars=NaN``, and a proposal stored and served
    with ``net_edge_cents: "NaN"`` after executing a real order.

    A non-finite price is not a near-miss to be clamped; it is a value that
    poisons every average computed over the column it lands in. Refuse it at
    the one chokepoint every price, count and fee flows through — including
    the ``Decimal`` passthrough, which is how a NaN constructed upstream
    would otherwise slip in untouched.
    """
    if not parsed.is_finite():
        raise MoneyParseError(f"{field}: refusing non-finite value {original!r}")
    return parsed


def parse_dollars(value: Any, field: str = "price") -> Decimal:
    """Parse a ``FixedPointDollars`` string into a Decimal dollar amount.

    >>> parse_dollars("0.5600")
    Decimal('0.5600')
    """
    return _to_decimal(value, field)


def parse_count(value: Any, field: str = "count") -> Decimal:
    """Parse a ``FixedPointCount`` string into a Decimal contract count.

    >>> parse_count("136.00")
    Decimal('136.00')
    """
    return _to_decimal(value, field)


def dollars_to_cents(dollars: Decimal) -> Decimal:
    """Convert a dollar amount to cents, preserving sub-cent precision.

    Takes a ``Decimal`` directly rather than going through :func:`parse_dollars`,
    which made it the one way into the money path that skipped the non-finite
    check. Its callers all parse first, so this was never reachable — but "not
    reachable today" is how the other holes in this file started.
    """
    return _reject_non_finite(dollars, dollars, "dollars") * Decimal(100)


def cents_to_dollars(cents: Decimal | int | str) -> Decimal:
    """Convert cents to dollars."""
    return _to_decimal(cents, "cents") / Decimal(100)


def quantize_price(dollars: Decimal) -> Decimal:
    """Clamp a price to the API's documented maximum precision."""
    return dollars.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN).normalize()


def quantize_count(count: Decimal) -> Decimal:
    """Clamp a contract count to 0.01 granularity."""
    return count.quantize(COUNT_QUANTUM, rounding=ROUND_HALF_EVEN)


def format_dollars(dollars: Decimal, places: int = 4) -> str:
    """Render a dollar amount the way the API expects it in requests."""
    quantum = Decimal(1).scaleb(-places)
    return str(dollars.quantize(quantum, rounding=ROUND_HALF_EVEN))


def format_count(count: Decimal) -> str:
    """Render a contract count as a ``FixedPointCount`` string."""
    return str(quantize_count(count))

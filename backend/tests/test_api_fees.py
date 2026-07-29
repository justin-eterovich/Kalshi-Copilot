"""Tests for the fee-quote endpoint.

Regression coverage for two real breaks at this boundary:

1. When fee inputs migrated from integer cents to dollar strings, this
   endpoint kept passing cents and every call 500'd. The engine's validation
   caught it, but only at runtime — nothing tested the API layer.
2. When the schedule turned out to be keyed by series rather than category,
   this endpoint's ``category`` parameter became meaningless.

Money leaves as strings here, because fees are fractional cents.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.routes.health import fee_quote


class TestFeeQuote:
    async def test_prices_a_normal_market(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="100")
        assert Decimal(result["fee_cents"]) == Decimal(175)
        assert Decimal(result["fee_per_contract_cents"]) == Decimal("1.75")

    async def test_single_contract_is_1_75_not_2(self) -> None:
        """Rounding is to a centicent, so 0.07 * 0.5 * 0.5 stands as-is."""
        result = await fee_quote(price_dollars="0.5000", contracts="1")
        assert Decimal(result["fee_cents"]) == Decimal("1.75")

    async def test_accepts_fractional_contracts(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="2.50")
        # $0.04375 is 437.5 centicents -> rounds up to 438 -> 4.38c
        assert Decimal(result["fee_cents"]) == Decimal("4.38")

    async def test_maker_on_an_unlisted_series_fails_closed(self) -> None:
        """The maker default is 0, but only while defaulting is safe.

        This asserted a free maker quote, which was right while every listed
        maker multiplier was 0. The real schedule now lists series above that
        default, so assuming it for a series we did not find can understate the
        fee — and this endpoint's whole job at that point is to refuse rather
        than to render a number the ticket UI would show as fact.

        Unlike the engine tests this one reads the deployed schedule, so it
        pins the endpoint's *handling* rather than a particular table: whatever
        the file says, an unpriceable maker leg comes back as a 409 and not a
        zero.
        """
        with pytest.raises(HTTPException) as exc:
            await fee_quote(
                price_dollars="0.5000", contracts="1000", is_taker=False
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["error"] == "unknown_series"
        # The refusal is per side: `test_prices_a_normal_market` above quotes
        # a taker on the same unlisted series and still gets a number. A
        # maker-side refusal leaking across would empty the proposal queue
        # rather than tighten it, since every ticket this system builds is a
        # taker order.

    async def test_maker_is_charged_on_a_listed_series(self) -> None:
        maker = await fee_quote(
            price_dollars="0.5000",
            contracts="1000",
            ticker="KXCPI-26JUL",
            is_taker=False,
        )
        taker = await fee_quote(
            price_dollars="0.5000", contracts="1000", ticker="KXCPI-26JUL"
        )
        assert 0 < Decimal(maker["fee_cents"]) < Decimal(taker["fee_cents"])

    async def test_a_fee_free_series_costs_nothing(self) -> None:
        """KXBTCY is listed at 0/0 in the real schedule."""
        result = await fee_quote(
            price_dollars="0.5000", contracts="100", ticker="KXBTCY-26DEC31-B1"
        )
        assert Decimal(result["fee_cents"]) == 0

    async def test_the_series_is_derived_from_a_market_ticker(self) -> None:
        result = await fee_quote(
            price_dollars="0.5000", contracts="100", ticker="KXFEDDECISION-26JUL-H25"
        )
        assert result["series"] == "KXFEDDECISION"

    async def test_omitting_the_ticker_prices_at_the_default(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="100")
        assert result["series"] is None
        assert Decimal(result["fee_cents"]) == Decimal(175)

    @pytest.mark.parametrize("bad_price", ["50", "0", "1", "-0.5", "nonsense"])
    async def test_bad_price_returns_400(self, bad_price: str) -> None:
        """A cents-style '50' must be rejected, not silently mispriced."""
        with pytest.raises(HTTPException) as exc:
            await fee_quote(price_dollars=bad_price, contracts="100")
        assert exc.value.status_code == 400

    async def test_zero_contracts_is_free(self) -> None:
        result = await fee_quote(price_dollars="0.5000", contracts="0")
        assert Decimal(result["fee_cents"]) == 0
        assert result["fee_per_contract_cents"] == "0"

    async def test_echoes_inputs_for_ui_display(self) -> None:
        result = await fee_quote(price_dollars="0.5600", contracts="100")
        assert result["price_dollars"] == "0.5600"
        assert result["contracts"] == "100"
        assert result["is_taker"] is True

    async def test_money_leaves_as_a_string(self) -> None:
        """Fees are fractional cents; a JSON number would invite rounding."""
        result = await fee_quote(price_dollars="0.5000", contracts="1")
        assert isinstance(result["fee_cents"], str)


class TestNonFiniteInputs:
    """``NaN`` produced a bare HTTP 500 on a public endpoint.

    ``Decimal("NaN")`` is a valid Decimal, so the money parser accepted it and
    the fee engine then raised ``InvalidOperation`` — an ``ArithmeticError``,
    which the ``ValueError`` handler here does not catch. The response was
    ``500 Internal Server Error`` with an empty body, and the same value
    reached Postgres by the other route: proposal 267 is stored ``executed``
    with ``net_edge_cents: "NaN"`` against a real demo fill.

    A refusal at the parse step turns all of that into a 400 that says which
    field was wrong.
    """

    @pytest.mark.parametrize(
        "bad", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "inf"]
    )
    async def test_a_non_finite_price_returns_400(self, bad: str) -> None:
        with pytest.raises(HTTPException) as exc:
            await fee_quote(price_dollars=bad, contracts="1")
        assert exc.value.status_code == 400

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    async def test_a_non_finite_contract_count_returns_400(self, bad: str) -> None:
        with pytest.raises(HTTPException) as exc:
            await fee_quote(price_dollars="0.5000", contracts=bad)
        assert exc.value.status_code == 400

    async def test_the_400_names_the_field(self) -> None:
        """A bare 500 tells an operator nothing about which input was bad."""
        with pytest.raises(HTTPException) as exc:
            await fee_quote(price_dollars="NaN", contracts="1")
        assert "price" in str(exc.value.detail).lower()

    async def test_nothing_non_finite_is_ever_echoed_back(self) -> None:
        """The endpoint echoes its inputs for display. A refused request must
        produce no body at all rather than a payload carrying ``"NaN"`` into
        the UI."""
        with pytest.raises(HTTPException):
            await fee_quote(price_dollars="NaN", contracts="1")

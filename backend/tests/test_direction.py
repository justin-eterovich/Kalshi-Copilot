"""Tests for the (side, action) -> Kalshi bid/ask translation.

This mapping has no natural error signal. The fee formula is symmetric in
``P`` and ``1 - P``, so an inverted direction produces the *same fee*, the
same notional, and a plausible-looking confirmation — it just buys the
opposite contract. Nothing else in the system catches it. Hence this file.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.models import Side
from app.trading.direction import (
    ASK,
    BID,
    book_side,
    from_yes_price,
    opposite_side,
    signed_contracts,
    to_yes_price,
)


class TestBookSide:
    """bid = buy YES; ask = sell YES. Buying NO is an ask."""

    @pytest.mark.parametrize(
        ("side", "action", "expected"),
        [
            (Side.YES, "buy", BID),
            (Side.YES, "sell", ASK),
            (Side.NO, "buy", ASK),
            (Side.NO, "sell", BID),
        ],
    )
    def test_truth_table(self, side: Side, action: str, expected: str) -> None:
        assert book_side(side, action) == expected

    def test_buying_no_is_an_ask_not_a_bid(self) -> None:
        """The one everybody gets wrong: NO is bought by *selling* YES."""
        assert book_side(Side.NO, "buy") == ASK

    def test_accepts_string_sides(self) -> None:
        assert book_side("yes", "buy") == BID
        assert book_side("no", "buy") == ASK

    def test_rejects_unknown_action(self) -> None:
        with pytest.raises(ValueError, match="action"):
            book_side(Side.YES, "hold")


class TestPriceConversion:
    def test_yes_price_passes_through(self) -> None:
        assert to_yes_price(Side.YES, "0.5600") == Decimal("0.5600")

    def test_no_price_is_complemented(self) -> None:
        """Buy NO at 30c goes on the wire as a YES price of 70c."""
        assert to_yes_price(Side.NO, "0.30") == Decimal("0.70")

    def test_sub_cent_precision_survives(self) -> None:
        """Tick size varies per market, so sub-cent prices are real."""
        assert to_yes_price(Side.NO, "0.123456") == Decimal("0.876544")

    def test_round_trip_is_lossless(self) -> None:
        for price in ("0.01", "0.5600", "0.999999", "0.123456"):
            for side in (Side.YES, Side.NO):
                assert from_yes_price(side, to_yes_price(side, price)) == Decimal(price)

    def test_a_cents_style_price_is_not_silently_accepted(self) -> None:
        """56 would become a YES price of -55, which is not a price."""
        assert to_yes_price(Side.NO, 56) == Decimal(-55)  # nonsense in, nonsense out
        # ...which is why the pricing layer validates the range before this
        # is ever called. See test_ticket_pricing.


class TestSignedContracts:
    """Positions are one signed number: positive YES, negative NO."""

    @pytest.mark.parametrize(
        ("side", "action", "expected"),
        [
            (Side.YES, "buy", Decimal(10)),
            (Side.YES, "sell", Decimal(-10)),
            (Side.NO, "buy", Decimal(-10)),
            (Side.NO, "sell", Decimal(10)),
        ],
    )
    def test_signs(self, side: Side, action: str, expected: Decimal) -> None:
        assert signed_contracts(side, action, Decimal(10)) == expected

    def test_buying_no_offsets_buying_yes(self) -> None:
        """The property the whole representation exists for."""
        long_yes = signed_contracts(Side.YES, "buy", Decimal(10))
        long_no = signed_contracts(Side.NO, "buy", Decimal(10))
        assert long_yes + long_no == 0

    def test_selling_yes_equals_buying_no(self) -> None:
        assert signed_contracts(Side.YES, "sell", Decimal(7)) == signed_contracts(
            Side.NO, "buy", Decimal(7)
        )

    def test_fractional_contracts_keep_their_decimals(self) -> None:
        assert signed_contracts(Side.NO, "buy", Decimal("0.50")) == Decimal("-0.50")


def test_opposite_side() -> None:
    assert opposite_side(Side.YES) is Side.NO
    assert opposite_side("no") is Side.YES

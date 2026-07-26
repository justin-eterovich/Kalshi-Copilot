"""Tests for the pessimistic paper fill simulator.

A simulator that flatters itself is worse than none at all: it produces a
report card saying a detector works when it does not, and that report card is
what decides whether real money gets deployed. So the tests here are mostly
about the simulator being *unfavourable* in the right places.

The book fixture, in Kalshi's own convention where both sides are bids:

    yes: 0.40 x 100, 0.39 x 250      -> bids to buy YES
    no:  0.55 x 80,  0.54 x 300      -> bids to buy NO

which implies a YES ask of 0.45 (80 available) then 0.46 (300), and a NO ask
of 0.60 (100) then 0.61 (250).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.fees import taker_fee_cents
from app.db.models import Side
from app.trading.paper import levels_for, simulate_fills


@pytest.fixture
def book() -> dict:
    return {
        "yes": [["0.4000", "100.00"], ["0.3900", "250.00"]],
        "no": [["0.5500", "80.00"], ["0.5400", "300.00"]],
    }


def fill(book: dict, **overrides: object):
    kwargs: dict = {
        "book": book,
        "side": Side.YES,
        "action": "buy",
        "limit_price": Decimal("0.99"),
        "contracts": Decimal(10),
        "category": "Sports",
    }
    kwargs.update(overrides)
    return simulate_fills(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Which side of the book gets hit
# ---------------------------------------------------------------------------


class TestLevelSelection:
    """Get this backwards and every order fills at a wonderful price."""

    def test_buying_yes_lifts_the_offers_derived_from_no_bids(
        self, book: dict
    ) -> None:
        levels = levels_for(book, Side.YES, "buy")
        assert levels[0] == (Decimal("0.45"), Decimal("80.00"))
        assert levels[1] == (Decimal("0.46"), Decimal("300.00"))

    def test_selling_yes_hits_the_yes_bids_directly(self, book: dict) -> None:
        levels = levels_for(book, Side.YES, "sell")
        assert levels[0] == (Decimal("0.4000"), Decimal("100.00"))

    def test_buying_no_lifts_offers_derived_from_yes_bids(self, book: dict) -> None:
        levels = levels_for(book, Side.NO, "buy")
        assert levels[0] == (Decimal("0.60"), Decimal("100.00"))
        assert levels[1] == (Decimal("0.61"), Decimal("250.00"))

    def test_selling_no_hits_the_no_bids_directly(self, book: dict) -> None:
        levels = levels_for(book, Side.NO, "sell")
        assert levels[0] == (Decimal("0.5500"), Decimal("80.00"))

    def test_buys_are_ordered_cheapest_first(self, book: dict) -> None:
        prices = [p for p, _ in levels_for(book, Side.YES, "buy")]
        assert prices == sorted(prices)

    def test_sells_are_ordered_richest_first(self, book: dict) -> None:
        prices = [p for p, _ in levels_for(book, Side.YES, "sell")]
        assert prices == sorted(prices, reverse=True)

    def test_buying_never_fills_at_the_bid(self, book: dict) -> None:
        """We cross the spread. Filling at the bid would be free money."""
        best_bid = Decimal("0.40")
        assert levels_for(book, Side.YES, "buy")[0][0] > best_bid

    def test_empty_side_yields_nothing(self) -> None:
        assert levels_for({"yes": [], "no": []}, Side.YES, "buy") == []


# ---------------------------------------------------------------------------
# Filling
# ---------------------------------------------------------------------------


class TestFilling:
    def test_small_order_fills_at_the_top_level(self, book: dict) -> None:
        fills = fill(book, contracts=Decimal(10))
        assert len(fills) == 1
        assert fills[0].price == Decimal("0.45")
        assert fills[0].contracts == Decimal(10)

    def test_large_order_walks_the_book(self, book: dict) -> None:
        """Size beyond the top level costs more. That is what slippage is."""
        fills = fill(book, contracts=Decimal(200))
        assert [(f.price, f.contracts) for f in fills] == [
            (Decimal("0.45"), Decimal("80.00")),
            (Decimal("0.46"), Decimal(120)),
        ]

    def test_each_level_is_a_separate_fill(self, book: dict) -> None:
        """Fees round up per fill, so levels must not be merged."""
        fills = fill(book, contracts=Decimal(200))
        assert len(fills) == 2

    def test_per_level_fees_cost_more_than_one_blended_fill(
        self, book: dict
    ) -> None:
        """The reason the previous test matters, asserted in money.

        Two fills round up twice. Pricing the same 200 contracts as a single
        blended fill would understate the cost — in exactly the thin markets
        this system is built to trade.
        """
        fills = fill(book, contracts=Decimal(200))
        per_level = sum(f.fee_cents for f in fills)
        blended = taker_fee_cents(Decimal("0.456"), Decimal(200), "Sports")
        assert per_level >= blended

    def test_fee_per_level_matches_the_fee_engine(self, book: dict) -> None:
        fills = fill(book, contracts=Decimal(10))
        assert fills[0].fee_cents == taker_fee_cents(
            Decimal("0.45"), Decimal(10), "Sports"
        )

    def test_partial_fill_when_the_book_is_too_thin(self, book: dict) -> None:
        fills = fill(book, contracts=Decimal(10_000))
        assert sum(f.contracts for f in fills) == Decimal("380.00")

    def test_fractional_contracts_fill(self, book: dict) -> None:
        fills = fill(book, contracts=Decimal("0.50"))
        assert fills[0].contracts == Decimal("0.50")

    def test_empty_book_fills_nothing(self) -> None:
        assert fill({"yes": [], "no": []}) == []

    def test_missing_side_fills_nothing(self) -> None:
        assert fill({"yes": [["0.40", "10"]]}, side=Side.YES, action="buy") == []


class TestLimitPrice:
    def test_never_fills_above_the_limit_on_a_buy(self, book: dict) -> None:
        """The limit is what makes an approved price binding."""
        fills = fill(book, contracts=Decimal(200), limit_price=Decimal("0.45"))
        assert len(fills) == 1
        assert fills[0].price == Decimal("0.45")
        assert fills[0].contracts == Decimal("80.00")

    def test_unmarketable_buy_fills_nothing(self, book: dict) -> None:
        assert fill(book, limit_price=Decimal("0.10")) == []

    def test_never_fills_below_the_limit_on_a_sell(self, book: dict) -> None:
        fills = fill(
            book,
            action="sell",
            contracts=Decimal(200),
            limit_price=Decimal("0.40"),
        )
        assert len(fills) == 1
        assert fills[0].price == Decimal("0.4000")

    def test_unmarketable_sell_fills_nothing(self, book: dict) -> None:
        assert fill(book, action="sell", limit_price=Decimal("0.95")) == []


class TestSlippage:
    def test_buys_pay_more(self, book: dict) -> None:
        fills = fill(book, slippage_cents=Decimal(1))
        assert fills[0].price == Decimal("0.46")

    def test_sells_receive_less(self, book: dict) -> None:
        # The default limit in this helper is a buy limit; a sell needs one
        # low enough to be marketable.
        fills = fill(
            book,
            action="sell",
            limit_price=Decimal("0.01"),
            slippage_cents=Decimal(1),
        )
        # Best YES bid is 0.40; 1c of adverse slippage receives 0.39.
        assert fills[0].price == Decimal("0.3900")

    def test_slippage_can_push_a_marginal_order_out_of_the_money(
        self, book: dict
    ) -> None:
        """Configured pessimism: a trade that only works at the touch does
        not fill at a fiction."""
        assert fill(book, limit_price=Decimal("0.45")) != []
        assert fill(book, limit_price=Decimal("0.45"), slippage_cents=Decimal(1)) == []

    def test_slippage_never_produces_an_impossible_price(self) -> None:
        """A price of 1.00 or above is not a contract price; skip, do not
        hand it to the fee engine."""
        book = {"yes": [], "no": [["0.005", "10.00"]]}  # implies a YES ask of 0.995
        assert fill(book, slippage_cents=Decimal(1)) == []

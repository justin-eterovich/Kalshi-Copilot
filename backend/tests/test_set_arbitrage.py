"""Tests for set-arbitrage pricing.

The property this file exists to protect is the asymmetry between the two
directions:

- **Selling** every leg of a mutually-exclusive set is riskless on
  exclusivity alone. At most one leg pays out $1, so collecting more than
  that is free money.
- **Buying** every leg is only riskless if the set is **exhaustive**. Kalshi's
  ``mutually_exclusive`` flag does not say that — it says "only one market in
  this event can resolve to 'yes'", which is *at most* one. ``KXNEWPOPE-70``
  is exclusive and carries seven candidates; there are more than seven
  possible popes.

A detector that treats the two symmetrically will happily propose buying an
incomplete set for 99c, which is not an arb but an uncovered short of every
outcome nobody listed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.fees import FeeSchedule
from app.detectors.set_arbitrage import (
    LegBook,
    max_executable_sets,
    price_set,
    walk,
)


@pytest.fixture
def schedule() -> FeeSchedule:
    return FeeSchedule.from_dict(
        {
            "meta": {"verified_on": "2026-07-27"},
            "formula": {"base_taker_rate": "0.07", "base_maker_rate": "0.0175"},
            "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
            "series": {"KXFREE": {"maker": 0, "taker": 0}},
        }
    )


def book(ticker: str, yes: list, no: list) -> LegBook:
    return LegBook.from_payload(ticker, {"yes": yes, "no": no})


def two_leg(bid_a: str, bid_b: str, size: str = "1000") -> list[LegBook]:
    """A two-leg set where each leg's YES bid is given.

    The NO bids are set so the YES *offer* on each leg is 1c above its bid,
    which is what a tight two-sided market looks like.
    """
    legs = []
    for i, bid in enumerate((bid_a, bid_b)):
        offer = str(Decimal(bid) + Decimal("0.01"))
        legs.append(
            book(
                f"KXGAME-26-{'AB'[i]}",
                yes=[[bid, size]],
                no=[[str(ONE_ - Decimal(offer)), size]],
            )
        )
    return legs


ONE_ = Decimal(1)


# ---------------------------------------------------------------------------
# Book walking
# ---------------------------------------------------------------------------


class TestWalk:
    def test_fills_from_the_best_level_first(self) -> None:
        result = walk([(Decimal("0.60"), Decimal(50)), (Decimal("0.59"), Decimal(100))],
                      Decimal(30), direction="sell")
        assert result is not None
        avg, filled, consumed = result
        assert avg == Decimal("0.60")
        assert filled == Decimal(30)
        assert consumed == [(Decimal("0.60"), Decimal(30))]

    def test_walks_into_worse_levels_for_size(self) -> None:
        result = walk([(Decimal("0.60"), Decimal(50)), (Decimal("0.59"), Decimal(100))],
                      Decimal(100), direction="sell")
        assert result is not None
        avg, _, consumed = result
        assert len(consumed) == 2
        expected = (50 * Decimal("0.60") + 50 * Decimal("0.59")) / 100
        assert avg == expected

    def test_refuses_a_partial_fill(self) -> None:
        """An arb that fills on some legs is not an arb — it is a position."""
        assert walk([(Decimal("0.60"), Decimal(10))], Decimal(50)) is None

    def test_selling_receives_less_with_slippage(self) -> None:
        result = walk([(Decimal("0.60"), Decimal(50))], Decimal(10),
                      slippage_cents=Decimal(1), direction="sell")
        assert result is not None
        assert result[0] == Decimal("0.59")

    def test_buying_pays_more_with_slippage(self) -> None:
        result = walk([(Decimal("0.60"), Decimal(50))], Decimal(10),
                      slippage_cents=Decimal(1), direction="buy")
        assert result is not None
        assert result[0] == Decimal("0.61")

    def test_an_empty_book_fills_nothing(self) -> None:
        assert walk([], Decimal(1)) is None


class TestExecutableSides:
    def test_selling_hits_the_yes_bids(self) -> None:
        leg = book("KX-A", yes=[["0.60", "10"]], no=[["0.38", "10"]])
        assert leg.executable("sell") == [(Decimal("0.60"), Decimal(10))]

    def test_buying_lifts_offers_derived_from_no_bids(self) -> None:
        """A NO bid at 0.38 is an offer to sell YES at 0.62."""
        leg = book("KX-A", yes=[["0.60", "10"]], no=[["0.38", "10"]])
        assert leg.executable("buy") == [(Decimal("0.62"), Decimal(10))]

    def test_sell_levels_are_best_first_descending(self) -> None:
        leg = book("KX-A", yes=[["0.58", "10"], ["0.60", "10"]], no=[])
        assert [p for p, _ in leg.executable("sell")] == [
            Decimal("0.60"), Decimal("0.58")
        ]

    def test_buy_levels_are_best_first_ascending(self) -> None:
        leg = book("KX-A", yes=[], no=[["0.30", "10"], ["0.38", "10"]])
        assert [p for p, _ in leg.executable("buy")] == [
            Decimal("0.62"), Decimal("0.70")
        ]


# ---------------------------------------------------------------------------
# The edge
# ---------------------------------------------------------------------------


class TestSellSide:
    def test_bids_summing_over_one_is_an_edge(self, schedule: FeeSchedule) -> None:
        """0.55 + 0.52 = 1.07, so 7c per set gross before fees."""
        opp = price_set(
            event_ticker="KXGAME-26", books=two_leg("0.55", "0.52"),
            contracts=Decimal(100), direction="sell", schedule=schedule,
        )
        assert opp is not None
        assert opp.gross_sum == Decimal("1.07")
        assert opp.net_edge_cents > 0
        # 7c/set * 100 sets = 700c gross, less both legs' fees.
        assert opp.net_edge_cents == Decimal(700) - opp.total_fee_cents

    def test_bids_summing_under_one_is_not_an_edge(
        self, schedule: FeeSchedule
    ) -> None:
        opp = price_set(
            event_ticker="KXGAME-26", books=two_leg("0.49", "0.49"),
            contracts=Decimal(100), direction="sell", schedule=schedule,
        )
        assert opp is not None
        assert opp.net_edge_cents < 0

    def test_a_thin_gross_edge_is_eaten_by_fees(
        self, schedule: FeeSchedule
    ) -> None:
        """The entire reason edges are reported net.

        1.005 is half a cent per set gross; two legs of taker fee near the
        money cost far more than that.
        """
        opp = price_set(
            event_ticker="KXGAME-26", books=two_leg("0.50", "0.505"),
            contracts=Decimal(100), direction="sell", schedule=schedule,
        )
        assert opp is not None
        assert opp.gross_sum > ONE_
        assert opp.net_edge_cents < 0

    def test_fees_are_charged_on_every_leg(self, schedule: FeeSchedule) -> None:
        opp = price_set(
            event_ticker="KXGAME-26", books=two_leg("0.55", "0.52"),
            contracts=Decimal(100), direction="sell", schedule=schedule,
        )
        assert opp is not None
        assert len(opp.legs) == 2
        assert all(leg.fee_cents > 0 for leg in opp.legs)
        assert opp.total_fee_cents == sum(leg.fee_cents for leg in opp.legs)

    def test_a_fee_free_series_keeps_the_gross_edge(
        self, schedule: FeeSchedule
    ) -> None:
        legs = [
            book("KXFREE-26-A", yes=[["0.55", "1000"]], no=[["0.44", "1000"]]),
            book("KXFREE-26-B", yes=[["0.52", "1000"]], no=[["0.47", "1000"]]),
        ]
        opp = price_set(
            event_ticker="KXFREE-26", books=legs, contracts=Decimal(100),
            direction="sell", schedule=schedule,
        )
        assert opp is not None
        assert opp.total_fee_cents == 0
        assert opp.net_edge_cents == Decimal(700)


class TestBuySide:
    def test_asks_summing_under_one_is_an_edge(self, schedule: FeeSchedule) -> None:
        """Only valid if the set is exhaustive — enforced by the caller, not here."""
        legs = [
            book("KXGAME-26-A", yes=[], no=[["0.55", "1000"]]),  # YES offer 0.45
            book("KXGAME-26-B", yes=[], no=[["0.53", "1000"]]),  # YES offer 0.47
        ]
        opp = price_set(
            event_ticker="KXGAME-26", books=legs, contracts=Decimal(100),
            direction="buy", schedule=schedule,
        )
        assert opp is not None
        assert opp.gross_sum == Decimal("0.92")
        assert opp.net_edge_cents == Decimal(800) - opp.total_fee_cents

    def test_asks_summing_over_one_is_not_an_edge(
        self, schedule: FeeSchedule
    ) -> None:
        legs = [
            book("KXGAME-26-A", yes=[], no=[["0.44", "1000"]]),  # YES offer 0.56
            book("KXGAME-26-B", yes=[], no=[["0.42", "1000"]]),  # YES offer 0.58
        ]
        opp = price_set(
            event_ticker="KXGAME-26", books=legs, contracts=Decimal(100),
            direction="buy", schedule=schedule,
        )
        assert opp is not None
        assert opp.net_edge_cents < 0

    def test_the_non_exhaustive_trap(self, schedule: FeeSchedule) -> None:
        """Seven pope candidates priced at 12c each sum to 0.84.

        Buying the set looks like a 16c arb and is not one: there are more
        than seven possible popes, so every leg can lose. This function will
        happily price it — refusing is the detector's job, and that is exactly
        why the detector requires exhaustiveness to be declared rather than
        inferred from `mutually_exclusive`.
        """
        legs = [
            book(f"KXNEWPOPE-70-{i}", yes=[], no=[["0.88", "1000"]])
            for i in range(7)
        ]
        opp = price_set(
            event_ticker="KXNEWPOPE-70", books=legs, contracts=Decimal(10),
            direction="buy", schedule=schedule,
        )
        assert opp is not None
        assert opp.gross_sum == Decimal("0.84")
        assert opp.net_edge_cents > 0  # looks like an arb...
        # ...and it is not, which is why nothing here may act on it alone.


class TestSizing:
    def test_the_thinnest_leg_caps_the_set(self, schedule: FeeSchedule) -> None:
        legs = [
            book("KXGAME-26-A", yes=[["0.55", "1000"]], no=[]),
            book("KXGAME-26-B", yes=[["0.52", "7"]], no=[]),
        ]
        assert max_executable_sets(legs, "sell") == Decimal(7)

    def test_a_set_larger_than_the_thinnest_leg_does_not_price(
        self, schedule: FeeSchedule
    ) -> None:
        legs = [
            book("KXGAME-26-A", yes=[["0.55", "1000"]], no=[]),
            book("KXGAME-26-B", yes=[["0.52", "7"]], no=[]),
        ]
        assert price_set(
            event_ticker="KXGAME-26", books=legs, contracts=Decimal(100),
            direction="sell", schedule=schedule,
        ) is None

    def test_walking_deeper_erodes_the_edge(self, schedule: FeeSchedule) -> None:
        """Size is not free: the second level is a worse price."""
        legs = [
            book("KXGAME-26-A", yes=[["0.55", "50"], ["0.50", "500"]], no=[]),
            book("KXGAME-26-B", yes=[["0.52", "50"], ["0.47", "500"]], no=[]),
        ]
        small = price_set(event_ticker="E", books=legs, contracts=Decimal(50),
                          direction="sell", schedule=schedule)
        large = price_set(event_ticker="E", books=legs, contracts=Decimal(500),
                          direction="sell", schedule=schedule)
        assert small is not None and large is not None
        assert large.gross_sum < small.gross_sum

    def test_no_books_prices_nothing(self, schedule: FeeSchedule) -> None:
        assert price_set(event_ticker="E", books=[], contracts=Decimal(1),
                         direction="sell", schedule=schedule) is None

    def test_zero_size_prices_nothing(self, schedule: FeeSchedule) -> None:
        assert price_set(event_ticker="E", books=two_leg("0.55", "0.52"),
                         contracts=Decimal(0), direction="sell",
                         schedule=schedule) is None


class TestSerialisation:
    def test_money_serialises_as_strings(self, schedule: FeeSchedule) -> None:
        opp = price_set(
            event_ticker="KXGAME-26", books=two_leg("0.55", "0.52"),
            contracts=Decimal(100), direction="sell", schedule=schedule,
        )
        assert opp is not None
        payload = opp.as_dict()
        for key in ("contracts", "gross_sum", "total_fee_cents", "net_edge_cents"):
            assert isinstance(payload[key], str), key
        assert all(isinstance(leg["avg_price"], str) for leg in payload["legs"])

    def test_every_leg_is_recorded(self, schedule: FeeSchedule) -> None:
        """The proposal has to name what it would trade, leg by leg."""
        opp = price_set(
            event_ticker="KXGAME-26", books=two_leg("0.55", "0.52"),
            contracts=Decimal(100), direction="sell", schedule=schedule,
        )
        assert opp is not None
        assert [leg["ticker"] for leg in opp.as_dict()["legs"]] == [
            "KXGAME-26-A", "KXGAME-26-B"
        ]


# ---------------------------------------------------------------------------
# The exhaustiveness gate — the detector's central safety rule
# ---------------------------------------------------------------------------


class TestExhaustivenessGate:
    """`mutually_exclusive` licenses the sell side only.

    Selling every leg is riskless because at most one pays out. Buying every
    leg needs at least one to pay out, which the flag does not promise.
    """

    @staticmethod
    def _config(exhaustive: list[str]):
        from app.config import Config

        return Config.model_validate(
            {
                "detectors": {
                    "set_arbitrage": {
                        "enabled": True,
                        "min_net_edge_cents": 1.0,
                        "exhaustive_series": exhaustive,
                    }
                }
            }
        )

    def test_buy_side_is_off_by_default(self) -> None:
        from app.detectors.runner import SetArbitrageDetector

        assert SetArbitrageDetector._exhaustive_series(self._config([])) == set()

    def test_a_declared_series_is_recognised(self) -> None:
        from app.detectors.runner import SetArbitrageDetector

        series = SetArbitrageDetector._exhaustive_series(
            self._config(["kxnflgame", "KXTESTMATCH"])
        )
        assert series == {"KXNFLGAME", "KXTESTMATCH"}

    def test_the_shipped_config_declares_nothing_exhaustive(self) -> None:
        """Ships empty on purpose: each entry is a claim about the world that
        the exchange never made, and a wrong one turns 'buy the set' into an
        uncovered short of every unlisted outcome."""
        import pathlib

        import yaml

        root = next(
            p
            for p in (pathlib.Path(__file__).resolve().parents[2], pathlib.Path("/app"))
            if (p / "config.yaml").is_file()
        )
        cfg = yaml.safe_load((root / "config.yaml").read_text())
        assert cfg["detectors"]["set_arbitrage"]["exhaustive_series"] == []

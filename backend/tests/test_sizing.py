"""Tests for Kelly position sizing.

The two properties worth protecting here are that the cost used is the
after-fee cost (Kelly is very sensitive near breakeven, so a gross price
oversizes badly) and that every cap can only reduce the size.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Config
from app.trading.sizing import full_kelly_fraction, recommend_size


def cfg(**risk: object) -> Config:
    base: dict[str, object] = {
        "bankroll_usd": 1000.0,
        "kelly_fraction": 0.25,
        "max_pct_per_market": 0.05,
        "max_total_exposure_pct": 0.40,
    }
    base.update(risk)
    return Config.model_validate({"risk": base})


class TestFullKelly:
    def test_a_fair_coin_at_a_fair_price_stakes_nothing(self) -> None:
        assert full_kelly_fraction(
            fair_price=Decimal("0.50"), cost_per_contract=Decimal("0.50")
        ) == 0

    def test_an_edge_produces_a_positive_fraction(self) -> None:
        # p=0.60, c=0.50 -> (0.60-0.50)/(1-0.50) = 0.20
        assert full_kelly_fraction(
            fair_price=Decimal("0.60"), cost_per_contract=Decimal("0.50")
        ) == Decimal("0.20")

    def test_a_negative_edge_stakes_nothing(self) -> None:
        assert full_kelly_fraction(
            fair_price=Decimal("0.40"), cost_per_contract=Decimal("0.50")
        ) == 0

    def test_the_same_edge_is_sized_larger_on_an_expensive_contract(self) -> None:
        """5c of edge at 10c and at 90c are not the same bet, and the
        direction is the counterintuitive one.

        At 90c you lose the stake only 5% of the time, so Kelly stakes half
        the bankroll; at 10c you lose it 85% of the time and Kelly stakes 5%.
        The fraction tracks how often the stake survives, not how much is
        won when it does.
        """
        cheap = full_kelly_fraction(
            fair_price=Decimal("0.15"), cost_per_contract=Decimal("0.10")
        )
        dear = full_kelly_fraction(
            fair_price=Decimal("0.95"), cost_per_contract=Decimal("0.90")
        )
        assert dear == Decimal("0.5")
        assert dear > cheap

    def test_a_near_certainty_stakes_an_alarming_fraction(self) -> None:
        """Which is exactly why the caps exist.

        Full Kelly on a 98c-fair contract bought at 90c is 80% of bankroll —
        from a *heuristic* fair value with no volatility model behind it.
        Nothing in this system may act on that number undiscounted; the
        fair price is capped below certainty upstream, quarter-Kelly cuts it
        again, and `max_pct_per_market` cuts it to 5% after that.
        """
        assert full_kelly_fraction(
            fair_price=Decimal("0.98"), cost_per_contract=Decimal("0.90")
        ) == Decimal("0.8")

    def test_the_fraction_rises_with_the_win_probability(self) -> None:
        at_cost = Decimal("0.50")
        fractions = [
            full_kelly_fraction(fair_price=Decimal(p), cost_per_contract=at_cost)
            for p in ("0.55", "0.65", "0.75", "0.85")
        ]
        assert fractions == sorted(fractions)

    def test_the_fraction_falls_as_the_price_rises_against_a_fixed_fair(
        self,
    ) -> None:
        fair = Decimal("0.80")
        fractions = [
            full_kelly_fraction(fair_price=fair, cost_per_contract=Decimal(c))
            for c in ("0.50", "0.60", "0.70", "0.79")
        ]
        assert fractions == sorted(fractions, reverse=True)

    def test_a_contract_costing_a_dollar_is_refused(self) -> None:
        """It pays at most a dollar, so it cannot profit — and the formula's
        denominator would be zero."""
        assert full_kelly_fraction(
            fair_price=Decimal("1.00"), cost_per_contract=Decimal("1.00")
        ) == 0

    def test_a_contract_costing_over_a_dollar_is_refused(self) -> None:
        assert full_kelly_fraction(
            fair_price=Decimal("1.00"), cost_per_contract=Decimal("1.05")
        ) == 0

    def test_a_free_contract_is_refused_rather_than_infinite(self) -> None:
        assert full_kelly_fraction(
            fair_price=Decimal("0.50"), cost_per_contract=Decimal(0)
        ) == 0

    def test_certainty_stakes_everything(self) -> None:
        """The formula's own answer at p=1 is the whole bankroll, which is
        why nothing in this system is allowed to produce a fair price of 1 —
        see decisive_fair_price's cap."""
        assert full_kelly_fraction(
            fair_price=Decimal(1), cost_per_contract=Decimal("0.50")
        ) == Decimal(1)


class TestFeesChangeTheSize:
    def test_fees_shrink_the_stake_sharply_near_breakeven(self) -> None:
        """A 2c gross edge on a market charging 1.7c is a 0.3c edge.

        Sizing off the gross price stakes many times too much — this is the
        most expensive form of the "a gross edge is a lie" rule.
        """
        gross = full_kelly_fraction(
            fair_price=Decimal("0.52"), cost_per_contract=Decimal("0.50")
        )
        net = full_kelly_fraction(
            fair_price=Decimal("0.52"), cost_per_contract=Decimal("0.517")
        )
        assert net < gross / 6

    def test_fees_can_erase_the_bet_entirely(self) -> None:
        assert full_kelly_fraction(
            fair_price=Decimal("0.52"), cost_per_contract=Decimal("0.525")
        ) == 0


class TestRecommendSize:
    def test_no_edge_means_no_contracts(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.50"),
            cost_per_contract=Decimal("0.55"),
            config=cfg(),
        )
        assert rec.contracts == 0
        assert not rec.is_tradeable
        assert rec.binding_constraint == "kelly"

    def test_the_market_cap_binds_before_kelly_on_a_big_edge(self) -> None:
        """f* = 0.5 here; quarter-Kelly is still 12.5% of bankroll, well over
        the 5% per-market limit."""
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
        )
        assert rec.binding_constraint == "market_cap"
        # 5% of $1000 = $50 at 50c = 100 contracts.
        assert rec.contracts == Decimal(100)

    def test_kelly_binds_on_a_thin_edge(self) -> None:
        # f* = (0.52-0.50)/0.50 = 0.04; quarter of that is 1% of bankroll.
        rec = recommend_size(
            fair_price=Decimal("0.52"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
        )
        assert rec.binding_constraint == "kelly"
        assert rec.kelly_fraction == Decimal("0.04")
        assert rec.scaled_fraction == Decimal("0.01")
        # 1% of $1000 = $10 at 50c = 20 contracts.
        assert rec.contracts == Decimal(20)

    def test_depth_binds_when_the_book_is_thin(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
            available_contracts=Decimal(7),
        )
        assert rec.binding_constraint == "depth"
        assert rec.contracts == Decimal(7)

    def test_exposure_headroom_binds_when_the_book_is_committed(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
            exposure_headroom_cents=Decimal(600),
        )
        assert rec.binding_constraint == "exposure"
        assert rec.contracts == Decimal(12)

    def test_no_headroom_means_no_trade(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
            exposure_headroom_cents=Decimal(0),
        )
        assert rec.contracts == 0

    def test_negative_headroom_is_not_read_as_room(self) -> None:
        """An over-committed book must clamp to zero, not wrap to a large
        stake through a sign error."""
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
            exposure_headroom_cents=Decimal(-5000),
        )
        assert rec.contracts == 0

    def test_the_size_is_rounded_down_not_up(self) -> None:
        """Rounding up would step past whichever cap just bound the size,
        which makes a limit advisory."""
        rec = recommend_size(
            fair_price=Decimal("0.90"),
            cost_per_contract=Decimal("0.30"),
            config=cfg(),
            available_contracts=Decimal("7.999"),
        )
        assert rec.contracts == Decimal("7.99")

    def test_a_size_below_the_tick_is_zero_not_a_minimum_trade(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
            available_contracts=Decimal("0.001"),
        )
        assert rec.contracts == 0
        assert not rec.is_tradeable

    def test_the_stake_never_exceeds_the_binding_cap(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.80"),
            cost_per_contract=Decimal("0.37"),
            config=cfg(),
        )
        assert rec.stake_cents <= Decimal(1000) * Decimal("0.05") * Decimal(100)

    @pytest.mark.parametrize("fraction", [1.0, 0.5, 0.25, 0.1])
    def test_the_kelly_discount_scales_the_stake_linearly(
        self, fraction: float
    ) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.52"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(kelly_fraction=fraction, max_pct_per_market=1.0),
        )
        assert rec.scaled_fraction == Decimal("0.04") * Decimal(str(fraction))

    def test_full_kelly_is_reported_alongside_the_discounted_stake(self) -> None:
        """So the card can show that fractional Kelly is doing work."""
        rec = recommend_size(
            fair_price=Decimal("0.52"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(),
        )
        assert rec.kelly_fraction > rec.scaled_fraction

    def test_a_zero_bankroll_sizes_nothing(self) -> None:
        rec = recommend_size(
            fair_price=Decimal("0.75"),
            cost_per_contract=Decimal("0.50"),
            config=cfg(bankroll_usd=0.01),
        )
        assert rec.contracts == 0

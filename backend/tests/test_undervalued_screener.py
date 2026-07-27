"""Tests for the undervalued screener.

The screener's job is to be *less* useful than it could be: it selects on a
wide spread and then refuses to turn that spread into a number of cents. Most
of what follows tests the refusals — the missing side of a book, the crossed
book, the market past its close, the percentile handed in as a percent — plus
the standing guarantee that no edge or fair value is exposed anywhere.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from app.detectors.undervalued_screener import (
    MarketSnapshot,
    ScreenResult,
    percentile_rank,
    screen,
    spread_cents,
)


def snap(
    ticker: str = "KX-A",
    *,
    volume: str = "0",
    bid: str | None = "0.20",
    ask: str | None = "0.45",
    hours: float = 6.0,
    open_interest: str | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        ticker=ticker,
        volume_24h=Decimal(volume),
        yes_bid=None if bid is None else Decimal(bid),
        yes_ask=None if ask is None else Decimal(ask),
        hours_to_close=hours,
        open_interest=None if open_interest is None else Decimal(open_interest),
    )


def run(markets: list[MarketSnapshot], **overrides: object) -> list[ScreenResult]:
    kwargs: dict = {
        "max_volume_percentile": 0.5,
        "min_spread_cents": Decimal(5),
        "max_hours_to_close": 24.0,
    }
    kwargs.update(overrides)
    return screen(markets, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------


class TestSpreadCents:
    def test_a_two_sided_book_has_a_spread_in_cents(self) -> None:
        assert spread_cents(Decimal("0.20"), Decimal("0.45")) == Decimal(25)

    def test_sub_cent_ticks_survive(self) -> None:
        """Tick size varies per market, so sub-cent spreads are real."""
        assert spread_cents(Decimal("0.5600"), Decimal("0.5625")) == Decimal("0.25")

    def test_a_touching_book_has_zero_spread(self) -> None:
        assert spread_cents(Decimal("0.40"), Decimal("0.40")) == 0

    def test_a_missing_ask_has_no_spread(self) -> None:
        """Not 'maximally wide'. There is no second side to subtract."""
        assert spread_cents(Decimal("0.20"), None) is None

    def test_a_missing_bid_has_no_spread(self) -> None:
        assert spread_cents(None, Decimal("0.45")) is None

    def test_an_empty_book_has_no_spread(self) -> None:
        assert spread_cents(None, None) is None

    def test_a_crossed_book_is_refused(self) -> None:
        """bid > ask is corrupt data, not free money."""
        assert spread_cents(Decimal("0.60"), Decimal("0.40")) is None

    @pytest.mark.parametrize("bad", ["0", "1", "1.40", "-0.10"])
    def test_a_price_outside_the_open_interval_is_refused(self, bad: str) -> None:
        assert spread_cents(Decimal(bad), Decimal("0.99")) is None
        assert spread_cents(Decimal("0.01"), Decimal(bad)) is None

    def test_a_nan_price_is_refused_rather_than_raising(self) -> None:
        assert spread_cents(Decimal("NaN"), Decimal("0.45")) is None


# ---------------------------------------------------------------------------
# Percentile
# ---------------------------------------------------------------------------


class TestPercentileRank:
    def test_the_lowest_value_ranks_at_zero(self) -> None:
        values = [Decimal(v) for v in ("1", "2", "3", "4")]
        assert percentile_rank(values, Decimal(1)) == 0.0

    def test_the_highest_value_ranks_below_one(self) -> None:
        values = [Decimal(v) for v in ("1", "2", "3", "4")]
        assert percentile_rank(values, Decimal(4)) == 0.75

    def test_a_value_above_everything_ranks_at_one(self) -> None:
        values = [Decimal(v) for v in ("1", "2", "3", "4")]
        assert percentile_rank(values, Decimal(99)) == 1.0

    def test_ties_do_not_inflate_the_rank(self) -> None:
        """Fifty zero-volume markets all rank at the bottom, not the middle.

        Zero-volume ties are the common case in exactly the corner of the
        exchange this screener looks at, so a non-strict comparison would put
        half of them above the filter for no reason.
        """
        values = [Decimal(0)] * 50
        assert percentile_rank(values, Decimal(0)) == 0.0

    def test_an_empty_reference_set_ranks_at_the_bottom(self) -> None:
        """Documented, not accidental: nothing is below it."""
        assert percentile_rank([], Decimal("10.00")) == 0.0

    def test_non_finite_entries_are_dropped_from_the_reference_set(self) -> None:
        values = [Decimal(1), Decimal("NaN"), Decimal(3)]
        assert percentile_rank(values, Decimal(2)) == 0.5

    def test_a_non_finite_value_ranks_at_the_bottom_instead_of_raising(self) -> None:
        assert percentile_rank([Decimal(1), Decimal(2)], Decimal("NaN")) == 0.0

    def test_fractional_volumes_rank_correctly(self) -> None:
        values = [Decimal("0.50"), Decimal("1.25"), Decimal("2.00")]
        assert percentile_rank(values, Decimal("1.25")) == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# The screen itself
# ---------------------------------------------------------------------------


class TestScreenRefusals:
    def test_empty_input_returns_an_empty_list(self) -> None:
        assert run([]) == []

    def test_a_one_sided_book_is_excluded(self) -> None:
        """You cannot screen what you cannot price.

        The tempting bug is to score this at the top: no ask means "infinitely
        wide", which is the most interesting thing on the list right up until
        someone tries to trade it.
        """
        assert run([snap("KX-A", ask=None)]) == []
        assert run([snap("KX-A", bid=None)]) == []

    def test_a_crossed_book_is_excluded(self) -> None:
        assert run([snap("KX-A", bid="0.60", ask="0.40")]) == []

    def test_a_market_at_its_close_is_excluded(self) -> None:
        """Zero hours left is the resolution sniper's problem, not this one."""
        assert run([snap("KX-A", hours=0.0)]) == []

    def test_a_market_past_its_close_is_excluded(self) -> None:
        assert run([snap("KX-A", hours=-3.0)]) == []

    def test_a_market_closing_beyond_the_window_is_excluded(self) -> None:
        assert run([snap("KX-A", hours=200.0)], max_hours_to_close=24.0) == []

    def test_a_tight_book_is_excluded(self) -> None:
        """The whole premise is an untraded book. A 1c spread is a market."""
        assert run([snap("KX-A", bid="0.44", ask="0.45")]) == []

    def test_the_spread_floor_is_inclusive(self) -> None:
        got = run([snap("KX-A", bid="0.40", ask="0.45")], min_spread_cents=Decimal(5))
        assert [r.ticker for r in got] == ["KX-A"]

    def test_a_heavily_traded_market_is_excluded(self) -> None:
        markets = [snap(f"KX-{i}", volume=str(i)) for i in range(10)]
        got = run(markets, max_volume_percentile=0.3)
        # 0.3 of ten markets: ranks 0.0, 0.1, 0.2, 0.3 survive.
        assert [r.ticker for r in got] == ["KX-0", "KX-1", "KX-2", "KX-3"]

    def test_a_negative_volume_is_excluded(self) -> None:
        assert run([snap("KX-A", volume="-5")]) == []

    def test_a_non_finite_hours_to_close_is_excluded(self) -> None:
        assert run([snap("KX-A", hours=float("nan"))]) == []
        assert run([snap("KX-A", hours=float("inf"))]) == []

    def test_a_percentile_given_as_a_percent_is_rejected(self) -> None:
        """A caller meaning 'the quietest 5%' who passes 5 would otherwise get
        every market back with no complaint."""
        with pytest.raises(ValueError, match="fraction in"):
            run([snap()], max_volume_percentile=5.0)

    def test_a_negative_percentile_bound_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="fraction in"):
            run([snap()], max_volume_percentile=-0.1)

    def test_a_negative_spread_floor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_spread_cents"):
            run([snap()], min_spread_cents=Decimal(-1))

    def test_a_non_positive_window_returns_nothing_rather_than_raising(self) -> None:
        """'Nothing closing in the next zero hours' is a coherent question."""
        assert run([snap()], max_hours_to_close=0.0) == []
        assert run([snap()], max_hours_to_close=-5.0) == []


class TestScreenRanking:
    def test_a_thin_wide_soon_closing_market_surfaces(self) -> None:
        got = run([snap("KX-A", volume="0", bid="0.20", ask="0.45", hours=2.0)])
        assert len(got) == 1
        assert got[0].ticker == "KX-A"
        assert got[0].spread_cents == Decimal(25)
        assert got[0].volume_percentile == 0.0

    def test_thinner_outranks_busier_all_else_equal(self) -> None:
        markets = [
            snap("KX-BUSY", volume="500"),
            snap("KX-QUIET", volume="0"),
        ]
        got = run(markets, max_volume_percentile=1.0)
        assert [r.ticker for r in got] == ["KX-QUIET", "KX-BUSY"]

    def test_wider_outranks_tighter_all_else_equal(self) -> None:
        markets = [
            snap("KX-TIGHT", bid="0.40", ask="0.48"),
            snap("KX-WIDE", bid="0.20", ask="0.45"),
        ]
        got = run(markets)
        assert [r.ticker for r in got] == ["KX-WIDE", "KX-TIGHT"]

    def test_sooner_outranks_later_all_else_equal(self) -> None:
        markets = [
            snap("KX-LATER", hours=20.0),
            snap("KX-SOON", hours=1.0),
        ]
        got = run(markets)
        assert [r.ticker for r in got] == ["KX-SOON", "KX-LATER"]

    def test_the_spread_term_saturates(self) -> None:
        """Past ~25c the book is absent rather than wide, and the extra cents
        say nothing more. Without saturation one absurd quote owns the list."""
        markets = [
            snap("KX-25", bid="0.20", ask="0.45"),
            snap("KX-80", bid="0.10", ask="0.90"),
        ]
        got = run(markets)
        by_ticker = {r.ticker: r for r in got}
        assert by_ticker["KX-25"].score == pytest.approx(by_ticker["KX-80"].score)

    def test_ties_are_broken_by_ticker_not_by_input_order(self) -> None:
        markets = [snap("KX-Z"), snap("KX-A")]
        assert [r.ticker for r in run(markets)] == ["KX-A", "KX-Z"]

    def test_scores_stay_inside_the_documented_range(self) -> None:
        markets = [
            snap("KX-MAX", volume="0", bid="0.05", ask="0.95", hours=0.01),
            snap("KX-MIN", volume="0", bid="0.40", ask="0.45", hours=24.0),
        ]
        got = run(markets, max_volume_percentile=1.0)
        assert all(0.0 <= r.score <= 100.0 for r in got)

    def test_results_are_sorted_by_score_descending(self) -> None:
        markets = [
            snap("KX-A", volume="10", bid="0.40", ask="0.46", hours=20.0),
            snap("KX-B", volume="0", bid="0.10", ask="0.50", hours=1.0),
            snap("KX-C", volume="5", bid="0.30", ask="0.45", hours=10.0),
        ]
        got = run(markets, max_volume_percentile=1.0)
        assert [r.score for r in got] == sorted((r.score for r in got), reverse=True)


class TestPercentileIsRelativeToTheSuppliedSet:
    def test_a_lone_market_ranks_at_the_bottom_of_its_own_set(self) -> None:
        """Screening a set of one measures nothing, and says so by ranking the
        market at 0.0 — the thinnest thing in a set containing only itself."""
        got = run([snap("KX-A", volume="100000")])
        assert len(got) == 1
        assert got[0].volume_percentile == 0.0

    def test_the_same_market_ranks_differently_in_a_busier_set(self) -> None:
        """The documented limitation, made concrete: screening a watchlist
        measures the watchlist, not Kalshi."""
        alone = run([snap("KX-A", volume="100")])
        crowded = run(
            [snap("KX-A", volume="100")]
            + [snap(f"KX-{i}", volume="1") for i in range(9)],
            max_volume_percentile=1.0,
        )
        rank_crowded = next(r for r in crowded if r.ticker == "KX-A")
        assert alone[0].volume_percentile == 0.0
        assert rank_crowded.volume_percentile == 0.9

    def test_unpriceable_markets_still_shape_the_reference_distribution(self) -> None:
        """They are excluded from the output but not from the denominator.

        Markets with no two-sided book are usually the quietest names in the
        set; dropping them from the reference distribution would push every
        survivor's rank down and flatter it as thinner than it is.
        """
        markets = [
            snap("KX-A", volume="10"),
            *[snap(f"KX-DEAD-{i}", volume="1", ask=None) for i in range(9)],
        ]
        got = run(markets, max_volume_percentile=1.0)
        assert [r.ticker for r in got] == ["KX-A"]
        assert got[0].volume_percentile == 0.9


class TestNoEdgeIsExposed:
    def test_the_result_carries_no_price_opinion(self) -> None:
        """The central refusal.

        Any 'edge' here would be computed from the spread the screener selects
        on, and would have to be paid to the spread to realise it. The field
        does not exist so nothing downstream can start reading it.
        """
        names = {f.name for f in fields(ScreenResult)}
        forbidden = {
            "edge",
            "edge_cents",
            "net_edge_cents",
            "fair_value",
            "fair_price",
            "expected_value",
            "ev",
            "ev_cents",
            "mid",
            "midpoint",
        }
        assert names & forbidden == set()

    def test_the_module_exports_nothing_that_prices_a_market(self) -> None:
        import app.detectors.undervalued_screener as mod

        assert set(mod.__all__) == {
            "MarketSnapshot",
            "ScreenResult",
            "percentile_rank",
            "screen",
            "spread_cents",
        }

    def test_the_reason_describes_the_market_not_its_worth(self) -> None:
        got = run([snap("KX-A", volume="3", bid="0.20", ask="0.45", hours=2.0)])
        reason = got[0].reason
        assert "traded" in reason
        assert "wide" in reason
        assert "closes in" in reason
        for word in ("edge", "fair", "cheap", "undervalued", "EV"):
            assert word not in reason

    def test_open_interest_reaches_the_human_without_touching_the_score(self) -> None:
        with_oi = run([snap("KX-A", open_interest="250.00")])
        without = run([snap("KX-A")])
        assert with_oi[0].score == without[0].score
        assert "OI 250" in with_oi[0].reason
        assert "OI" not in without[0].reason

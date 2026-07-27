"""Tests for whale-flow detection.

The signals here are easy; the refusals are the point. A flow detector that
never says "I don't know" will happily report a buy sweep on a tape whose
taker sides were all null, and by the time a human sees that observation it
is indistinguishable from a real one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.detectors.whale_flow import FlowEvent, Trade, analyse, detect_sweep, size_zscore

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def tr(
    offset_sec: float, price: str, count: str, side: str | None = "yes"
) -> Trade:
    return Trade(
        ts=T0 + timedelta(seconds=offset_sec),
        yes_price=Decimal(price),
        count=Decimal(count),
        taker_side=side,
    )


def varied_tape(n: int = 40) -> list[Trade]:
    """A dull but not degenerate tape: one price, ordinary size variation.

    Sizes must actually vary or every z-score refuses, which is correct
    behaviour and would silently make the outlier tests vacuous.
    """
    return [tr(i, "0.5000", str(10 + i % 5)) for i in range(n)]


def look(trades: list[Trade], **overrides: float) -> FlowEvent | None:
    kwargs: dict = {
        "zscore_threshold": 3.0,
        "window_sec": 5.0,
        "min_levels": 3,
        "base_confidence": 0.30,
    }
    kwargs.update(overrides)
    return analyse(trades, **kwargs)


# ---------------------------------------------------------------------------
# Size z-score
# ---------------------------------------------------------------------------


class TestSizeZscore:
    def test_a_large_value_scores_above_the_sample(self) -> None:
        counts = [Decimal("10.00")] * 20 + [Decimal("12.00")] * 20
        z = size_zscore(counts, Decimal("500.00"))
        assert z is not None and z > 5

    def test_a_typical_value_scores_near_zero(self) -> None:
        counts = [Decimal("10.00")] * 20 + [Decimal("12.00")] * 20
        z = size_zscore(counts, Decimal("11.00"))
        assert z is not None and abs(z) < 0.1

    def test_a_short_sample_is_refused(self) -> None:
        """The first big print on a quiet market would otherwise score
        enormously against the three small ones before it."""
        assert size_zscore([Decimal("10.00")] * 19, Decimal("500.00")) is None

    def test_the_sample_floor_is_configurable(self) -> None:
        counts = [Decimal("10.00")] * 5 + [Decimal("11.00")] * 5
        assert size_zscore(counts, Decimal("500.00"), min_samples=10) is not None
        assert size_zscore(counts, Decimal("500.00"), min_samples=11) is None

    def test_an_empty_sample_is_refused(self) -> None:
        assert size_zscore([], Decimal("500.00")) is None

    def test_a_uniform_tape_has_no_outliers(self) -> None:
        """The central refusal of this function.

        Every trade the same size means zero spread. Dividing by it would
        manufacture infinite conviction out of the flattest evidence there
        is, and the number it produced would look exactly like a real one.
        """
        assert size_zscore([Decimal("10.00")] * 40, Decimal("5000.00")) is None

    def test_a_uniform_tape_is_refused_even_for_a_matching_value(self) -> None:
        assert size_zscore([Decimal("10.00")] * 40, Decimal("10.00")) is None

    def test_fractional_counts_are_handled(self) -> None:
        """Contracts are fractional to 0.01; nothing here assumes integers."""
        counts = [Decimal("0.01")] * 20 + [Decimal("0.02")] * 20
        z = size_zscore(counts, Decimal("0.50"))
        assert z is not None and z > 5


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------


class TestDetectSweep:
    def test_a_buy_sweep_lifts_through_increasing_prices(self) -> None:
        trades = [
            tr(0, "0.5000", "100"),
            tr(1, "0.5100", "100"),
            tr(2, "0.5200", "150"),
            tr(3, "0.5300", "50"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None
        assert s.direction == "buy"
        assert s.levels == 4
        assert s.contracts == Decimal("400")
        assert s.first_price == Decimal("0.5000")
        assert s.last_price == Decimal("0.5300")
        assert s.seconds == pytest.approx(3.0)

    def test_a_sell_sweep_hits_down_through_decreasing_prices(self) -> None:
        trades = [
            tr(0, "0.5300", "100", "no"),
            tr(1, "0.5200", "100", "no"),
            tr(2, "0.5100", "100", "no"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None and s.direction == "sell"
        assert s.levels == 3

    def test_a_buy_sweep_is_not_read_as_a_sell_sweep(self) -> None:
        """Increasing prices with a sell taker side is not a run at all."""
        trades = [tr(i, f"0.5{i}00", "100", "no") for i in range(4)]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_buy_and_sell_spellings_are_accepted(self) -> None:
        """Tapes say yes/no, traders say buy/sell. Both mean the same push."""
        trades = [tr(i, f"0.5{i}00", "100", "buy") for i in range(3)]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None and s.direction == "buy"

    def test_a_block_at_one_price_is_not_a_sweep(self) -> None:
        """The distinction the whole module rests on.

        Twenty prints at 0.52 is one order meeting the queue, not a walk
        through the book. Counting them as levels would make every block
        trade look like aggression.
        """
        trades = [tr(i * 0.1, "0.5200", "500") for i in range(20)]
        assert detect_sweep(trades, window_sec=5, min_levels=2) is None

    def test_repeated_prices_inside_a_sweep_do_not_add_levels(self) -> None:
        trades = [
            tr(0, "0.5000", "100"),
            tr(1, "0.5000", "100"),
            tr(2, "0.5100", "100"),
            tr(3, "0.5100", "100"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=2)
        assert s is not None and s.levels == 2
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_equal_prices_written_differently_are_one_level(self) -> None:
        """0.50 and 0.5000 are the same price; Decimal knows that."""
        trades = [
            tr(0, "0.50", "100"),
            tr(1, "0.5000", "100"),
            tr(2, "0.500000", "100"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=2) is None

    def test_too_few_levels_is_not_a_sweep(self) -> None:
        trades = [tr(0, "0.5000", "100"), tr(1, "0.5100", "100")]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_a_time_gap_breaks_the_run(self) -> None:
        """Three levels over ten minutes is a market drifting, not a sweep."""
        trades = [
            tr(0, "0.5000", "100"),
            tr(200, "0.5100", "100"),
            tr(400, "0.5200", "100"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_a_side_change_breaks_the_run(self) -> None:
        trades = [
            tr(0, "0.5000", "100", "yes"),
            tr(1, "0.5100", "100", "no"),
            tr(2, "0.5200", "100", "yes"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_a_price_reversal_breaks_the_run(self) -> None:
        """A buy run that steps back down is two things, not one."""
        trades = [
            tr(0, "0.5000", "100"),
            tr(1, "0.5100", "100"),
            tr(2, "0.4900", "100"),
            tr(3, "0.5000", "100"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_the_most_recent_qualifying_run_wins(self) -> None:
        trades = [
            tr(0, "0.3000", "100"),
            tr(1, "0.3100", "100"),
            tr(2, "0.3200", "100"),
            # Long gap, then a second, separate sweep.
            tr(500, "0.6000", "100"),
            tr(501, "0.6100", "100"),
            tr(502, "0.6200", "100"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None and s.first_price == Decimal("0.6000")

    def test_an_earlier_run_is_reported_when_the_latest_does_not_qualify(self) -> None:
        trades = [
            tr(0, "0.3000", "100"),
            tr(1, "0.3100", "100"),
            tr(2, "0.3200", "100"),
            tr(500, "0.6000", "100"),
            tr(501, "0.6100", "100"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None and s.first_price == Decimal("0.3000")

    def test_trades_are_sorted_before_analysis(self) -> None:
        """The caller's ordering is not assumed to be chronological."""
        trades = [
            tr(2, "0.5200", "100"),
            tr(0, "0.5000", "100"),
            tr(1, "0.5100", "100"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None and s.first_price == Decimal("0.5000")

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        trades = [
            Trade(
                ts=(T0 + timedelta(seconds=i)).replace(tzinfo=None),
                yes_price=Decimal(f"0.5{i}00"),
                count=Decimal("100"),
                taker_side="yes",
            )
            for i in range(3)
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is not None

    def test_an_empty_tape_has_no_sweep(self) -> None:
        assert detect_sweep([], window_sec=5, min_levels=3) is None

    def test_a_single_trade_is_not_a_sweep(self) -> None:
        one = [tr(0, "0.5000", "999")]
        assert detect_sweep(one, window_sec=5, min_levels=2) is None


class TestSweepRefusesUnknownSides:
    def test_a_null_taker_side_never_becomes_a_buy(self) -> None:
        """Nulls defaulted to "buy" would produce a permanent, entirely
        fictional buy-side bias that reads exactly like real flow."""
        trades = [tr(i, f"0.5{i}00", "100", None) for i in range(4)]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_an_unrecognised_side_is_treated_as_unknown(self) -> None:
        trades = [tr(i, f"0.5{i}00", "100", "aggressor") for i in range(4)]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_side_spelling_is_case_and_space_insensitive(self) -> None:
        trades = [tr(i, f"0.5{i}00", "100", " YES ") for i in range(3)]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is not None

    def test_a_null_in_the_middle_breaks_the_run(self) -> None:
        """Skipping it would splice two runs into a sweep nobody executed.

        Breaking can only hide a sweep that happened; splicing invents one.
        Only one of those puts a fabricated observation in front of a human.
        """
        trades = [
            tr(0, "0.5000", "100", "yes"),
            tr(1, "0.5100", "100", "yes"),
            tr(2, "0.5200", "100", None),
            tr(3, "0.5300", "100", "yes"),
            tr(4, "0.5400", "100", "yes"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_a_readable_run_after_a_null_still_qualifies(self) -> None:
        trades = [
            tr(0, "0.5000", "100", None),
            tr(1, "0.5100", "100", "yes"),
            tr(2, "0.5200", "100", "yes"),
            tr(3, "0.5300", "100", "yes"),
        ]
        s = detect_sweep(trades, window_sec=5, min_levels=3)
        assert s is not None and s.first_price == Decimal("0.5100")


class TestSweepRefusesBadInput:
    @pytest.mark.parametrize("levels", [0, 1, -3])
    def test_fewer_than_two_levels_is_refused(self, levels: int) -> None:
        """A one-level "sweep" is just a trade."""
        trades = [tr(i, f"0.5{i}00", "100") for i in range(4)]
        assert detect_sweep(trades, window_sec=5, min_levels=levels) is None

    @pytest.mark.parametrize("window", [0.0, -1.0])
    def test_a_nonpositive_window_is_refused(self, window: float) -> None:
        trades = [tr(i, f"0.5{i}00", "100") for i in range(4)]
        assert detect_sweep(trades, window_sec=window, min_levels=3) is None

    def test_a_price_outside_the_unit_interval_breaks_the_run(self) -> None:
        """A price of 1.00 on the tape is a malformed row, not a certain one."""
        trades = [
            tr(0, "0.5000", "100"),
            tr(1, "0.5100", "100"),
            tr(2, "1.0000", "100"),
            tr(3, "0.5300", "100"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None

    def test_a_nonpositive_count_breaks_the_run(self) -> None:
        trades = [
            tr(0, "0.5000", "100"),
            tr(1, "0.5100", "0"),
            tr(2, "0.5200", "100"),
            tr(3, "0.5300", "100"),
        ]
        assert detect_sweep(trades, window_sec=5, min_levels=3) is None


# ---------------------------------------------------------------------------
# analyse
# ---------------------------------------------------------------------------


class TestAnalyseRefusals:
    def test_an_empty_tape_reports_nothing(self) -> None:
        assert look([]) is None

    def test_a_single_trade_reports_nothing(self) -> None:
        """One print is not unusual relative to anything, and the only
        baseline available for judging it would be itself."""
        assert look([tr(0, "0.5000", "99999")]) is None

    def test_a_uniform_tape_reports_nothing(self) -> None:
        assert look([tr(i, "0.5000", "10") for i in range(40)]) is None

    def test_a_tape_with_ordinary_variation_reports_nothing(self) -> None:
        assert look(varied_tape()) is None

    def test_a_big_print_on_a_short_tape_reports_nothing(self) -> None:
        """Below the z-score's sample floor there is no baseline, and there
        is no sweep either — so there is nothing honest to say."""
        trades = [tr(i, "0.5000", str(10 + i % 3)) for i in range(5)]
        trades.append(tr(6, "0.5000", "5000"))
        assert look(trades) is None

    def test_a_big_print_below_the_threshold_reports_nothing(self) -> None:
        trades = varied_tape()
        trades.append(tr(41, "0.5000", "16"))
        assert look(trades) is None

    def test_a_tape_of_only_unreadable_sides_reports_no_sweep(self) -> None:
        trades = [tr(i, "0.5000", str(10 + i % 5), None) for i in range(40)]
        trades += [tr(100 + j, f"0.5{j}00", "10", None) for j in range(4)]
        e = look(trades)
        assert e is None or e.sweep is None


class TestAnalyseSizeOutliers:
    def build(self, side: str | None = "yes") -> list[Trade]:
        trades = varied_tape()
        trades.append(tr(41, "0.5000", "5000", side))
        return trades

    def test_a_lone_block_is_reported_as_size_not_aggression(self) -> None:
        """The `sweep is None` split: 5000 contracts at one price is somebody
        with a lot to do, not somebody paying up to get it done."""
        e = look(self.build())
        assert e is not None
        assert e.sweep is None
        assert e.contracts == Decimal("5000")
        assert e.zscore is not None and e.zscore > 3

    def test_the_direction_of_a_block_follows_its_taker_side(self) -> None:
        sell = look(self.build("no"))
        buy = look(self.build("yes"))
        assert sell is not None and sell.direction == "sell"
        assert buy is not None and buy.direction == "buy"

    def test_a_block_with_an_unreadable_side_reports_unknown(self) -> None:
        """The volume printed — that is real. Which way it pushed is not
        known, and is reported as not known rather than assumed."""
        e = look(self.build(None))
        assert e is not None and e.direction == "unknown"

    def test_the_outlier_is_excluded_from_its_own_baseline(self) -> None:
        """Leaving it in drags the mean towards itself and shrinks the very
        deviation being measured."""
        counts = [Decimal("10")] * 20 + [Decimal("12")] * 19
        with_self = size_zscore([*counts, Decimal("5000")], Decimal("5000"))
        without_self = size_zscore(counts, Decimal("5000"))
        assert with_self is not None and without_self is not None
        assert without_self > with_self

    def test_the_largest_print_is_scored_not_the_newest(self) -> None:
        """Otherwise the answer depends on when the caller happened to ask."""
        trades = self.build()
        trades.append(tr(42, "0.5000", "11"))
        e = look(trades)
        assert e is not None and e.contracts == Decimal("5000")

    def test_a_malformed_print_is_not_the_outlier(self) -> None:
        """A 9,000-contract row priced at 0.00 is a bad row, and reporting it
        would put the loudest number on the tape in front of a human."""
        trades = self.build()
        trades.append(tr(43, "0.0000", "9000"))
        e = look(trades)
        assert e is not None and e.contracts == Decimal("5000")


class TestAnalyseSweeps:
    def build(self) -> list[Trade]:
        trades = [tr(i, "0.5000", "10") for i in range(40)]
        trades += [tr(100 + j, f"0.5{j}00", "10") for j in range(4)]
        return trades

    def test_a_sweep_is_reported_without_a_size_outlier(self) -> None:
        """Nobody traded big. Somebody traded through four prices, fast."""
        e = look(self.build())
        assert e is not None
        assert e.sweep is not None and e.sweep.levels == 4
        assert e.zscore is None
        assert e.direction == "buy"
        assert e.contracts == Decimal("40")

    def test_the_sweep_supplies_the_direction_when_both_fire(self) -> None:
        trades = varied_tape()
        trades += [
            tr(100, "0.5000", "10"),
            tr(101, "0.5100", "10"),
            tr(102, "0.5200", "5000"),
        ]
        e = look(trades)
        assert e is not None
        assert e.sweep is not None
        assert e.zscore is not None and e.zscore > 3
        assert e.direction == "buy"
        # The sweep, not the single print, describes the reported flow.
        assert e.contracts == Decimal("5020")


class TestConfidenceIsCapped:
    def monster(self) -> list[Trade]:
        """The most emphatic tape this module can be shown: a 9,000-contract
        print at the end of a nine-level sweep."""
        trades = varied_tape()
        trades += [
            tr(100 + j, f"0.5{j}00", "9000" if j == 8 else "10") for j in range(9)
        ]
        return trades

    @pytest.mark.parametrize("base", [0.05, 0.10, 0.30, 0.60])
    def test_no_amount_of_size_beats_the_ceiling(self, base: float) -> None:
        """The central honesty requirement, enforced rather than documented.

        `base_confidence` encodes how much this *kind* of evidence is worth.
        No individual instance of it gets to argue with that.
        """
        e = look(self.monster(), base_confidence=base)
        assert e is not None and e.confidence <= base

    def test_confidence_is_positive_when_something_fired(self) -> None:
        e = look(self.monster(), base_confidence=0.30)
        assert e is not None and e.confidence > 0

    def test_a_zero_base_confidence_still_reports_the_observation(self) -> None:
        """The cap governs confidence, not whether a human gets to see it."""
        e = look(self.monster(), base_confidence=0.0)
        assert e is not None and e.confidence == 0.0

    def test_a_negative_base_confidence_clamps_to_zero(self) -> None:
        e = look(self.monster(), base_confidence=-1.0)
        assert e is not None and e.confidence == 0.0

    def test_a_base_above_one_is_still_bounded_by_one(self) -> None:
        e = look(self.monster(), base_confidence=5.0)
        assert e is not None and e.confidence <= 1.0

    def test_corroborated_flow_scores_above_a_bare_sweep(self) -> None:
        bare = varied_tape()
        bare += [tr(100 + j, f"0.5{j}00", "10") for j in range(3)]
        both = varied_tape()
        both += [tr(100 + j, f"0.5{j}00", "9000" if j == 2 else "10") for j in range(3)]
        a, b = look(bare), look(both)
        assert a is not None and b is not None
        assert b.confidence > a.confidence


class TestRationale:
    def test_the_rationale_always_carries_the_caveat(self) -> None:
        """Every event says out loud that the other side may be informed."""
        trades = varied_tape()
        trades.append(tr(41, "0.5000", "5000"))
        e = look(trades)
        assert e is not None and "informed" in e.rationale

    def test_a_sweep_rationale_names_the_levels_walked(self) -> None:
        trades = [tr(i, "0.5000", "10") for i in range(40)]
        trades += [tr(100 + j, f"0.5{j}00", "10") for j in range(4)]
        e = look(trades)
        assert e is not None
        assert "sweep" in e.rationale and "4 levels" in e.rationale

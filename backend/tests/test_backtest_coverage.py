"""Tests for the backtest coverage gate.

The gate's failure mode is not a wrong number, it is a permissive one: a
backtest that runs over ten hours of data and prints a Sharpe ratio nobody
can distinguish from a real one. So most of what follows is about what the
module refuses to allow — and, just as importantly, that a genuinely healthy
dataset sails through, because a gate that always says no is a gate the
operator switches off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.backtest.coverage import (
    MAX_GAP_FLOOR,
    MIN_MARKETS,
    MIN_OBSERVED_SPAN,
    MIN_SETTLED_MARKETS,
    CoverageRefused,
    CoverageReport,
    MarketCoverage,
    RefusalCode,
    Severity,
    assert_usable,
    audit,
    gap_tolerance,
)

WINDOW_START = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW_END = datetime(2026, 5, 1, tzinfo=UTC)  # 120 days
WINDOW_SPAN = WINDOW_END - WINDOW_START
INTERVAL = timedelta(minutes=1)


def healthy(
    *,
    markets: int = 400,
    series: int = 20,
    settled: int = 240,
    span: timedelta = WINDOW_SPAN,
    observations: int = 20_000,
    max_gap: timedelta | None = timedelta(minutes=10),
    start: datetime = WINDOW_START,
) -> list[MarketCoverage]:
    """A dataset that clears every threshold, with one knob per refusal.

    Defaults: 400 markets over 20 series, 240 settled and 160 still open,
    spanning the full 120-day window with a measured 10-minute worst gap
    against a 20-minute tolerance.
    """
    rows: list[MarketCoverage] = []
    for i in range(markets):
        if i < settled:
            outcome: bool | None = i % 3 == 0
        else:
            outcome = None
        rows.append(
            MarketCoverage(
                ticker=f"KXSER{i % series}-26JAN-{i}",
                series_ticker=f"KXSER{i % series}",
                first_ts=start,
                last_ts=start + span,
                observations=observations,
                resolved_outcome=outcome,
                close_time=start + span,
                max_gap=max_gap,
            )
        )
    return rows


def run(
    rows: list[MarketCoverage],
    *,
    window_start: datetime = WINDOW_START,
    window_end: datetime = WINDOW_END,
    interval: timedelta = INTERVAL,
    **kwargs: object,
) -> CoverageReport:
    return audit(
        rows,
        window_start=window_start,
        window_end=window_end,
        expected_interval=interval,
        **kwargs,  # type: ignore[arg-type]
    )


def codes(report: CoverageReport) -> set[RefusalCode]:
    return set(report.refusal_codes)


def warns(report: CoverageReport) -> set[RefusalCode]:
    return set(report.warning_codes)


# ---------------------------------------------------------------------------
# The healthy case — the gate has to be passable
# ---------------------------------------------------------------------------


class TestHealthyDataset:
    def test_a_generous_dataset_produces_no_refusals(self) -> None:
        """If the gate cannot be cleared it is just 'always no' and gets
        disabled. 400 markets, 20 series, 240 settled, 120 days."""
        report = run(healthy())
        assert report.refusal_codes == ()
        assert report.usable is True

    def test_a_fully_instrumented_healthy_dataset_produces_no_warnings(self) -> None:
        """With measured gaps and a full window there is nothing left to say."""
        report = run(healthy())
        assert report.warning_codes == ()

    def test_assert_usable_passes_silently(self) -> None:
        assert assert_usable(run(healthy())) is None

    def test_the_report_states_both_windows(self) -> None:
        report = run(healthy())
        assert report.requested_span == WINDOW_SPAN
        assert report.observed_span == WINDOW_SPAN
        assert report.window_fill == pytest.approx(1.0)

    def test_it_counts_settled_and_open_separately(self) -> None:
        report = run(healthy(markets=400, settled=240))
        assert report.market_count == 400
        assert report.settled_count == 240
        assert report.unsettled_count == 160

    def test_a_no_resolution_still_counts_as_settled(self) -> None:
        """The trap this module renamed the field to avoid: `resolved_outcome`
        is False for a market that settled NO, and that market is ground truth
        exactly like a YES. Counting it as unsettled would halve the outcome
        sample and bias what remained toward YES."""
        rows = [
            MarketCoverage(
                ticker=f"KXA-{i}",
                series_ticker="KXA" if i % 2 else "KXB",
                first_ts=WINDOW_START,
                last_ts=WINDOW_END,
                observations=20_000,
                resolved_outcome=False,
                max_gap=timedelta(minutes=1),
            )
            for i in range(300)
        ]
        rows[0] = MarketCoverage(
            ticker="KXA-open",
            series_ticker="KXA",
            first_ts=WINDOW_START,
            last_ts=WINDOW_END,
            observations=20_000,
            resolved_outcome=None,
            max_gap=timedelta(minutes=1),
        )
        report = run(rows)
        assert report.settled_count == 299
        assert RefusalCode.TOO_FEW_SETTLED not in codes(report)

    def test_is_settled_distinguishes_open_from_resolved_no(self) -> None:
        resolved_no = MarketCoverage(
            ticker="KXA-1",
            series_ticker="KXA",
            first_ts=WINDOW_START,
            last_ts=WINDOW_END,
            observations=2,
            resolved_outcome=False,
        )
        still_open = MarketCoverage(
            ticker="KXA-2",
            series_ticker="KXA",
            first_ts=WINDOW_START,
            last_ts=WINDOW_END,
            observations=2,
            resolved_outcome=None,
        )
        assert resolved_no.is_settled is True
        assert still_open.is_settled is False


# ---------------------------------------------------------------------------
# Input validation — a naive timestamp is an instant nobody can place
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_a_naive_first_ts_raises(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=datetime(2026, 1, 1),  # naive on purpose
                last_ts=WINDOW_END,
                observations=10,
                resolved_outcome=None,
            )

    def test_a_naive_last_ts_raises(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=WINDOW_START,
                last_ts=datetime(2026, 5, 1),  # naive on purpose
                observations=10,
                resolved_outcome=None,
            )

    def test_a_naive_close_time_raises(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=WINDOW_START,
                last_ts=WINDOW_END,
                observations=10,
                resolved_outcome=None,
                close_time=datetime(2026, 5, 1),  # naive on purpose
            )

    def test_a_naive_window_bound_raises(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            audit(
                healthy(),
                window_start=datetime(2026, 1, 1),  # naive on purpose
                window_end=WINDOW_END,
                expected_interval=INTERVAL,
            )

    def test_a_naive_timestamp_is_not_coerced_to_utc(self) -> None:
        """Coercion would silently shift the whole sample by an unknown offset
        — for a span measured in hours, the difference between pass and fail."""
        with pytest.raises(ValueError, match="hard error"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=datetime(2026, 1, 1),  # naive on purpose
                last_ts=WINDOW_END,
                observations=10,
                resolved_outcome=None,
            )

    def test_a_non_utc_aware_timestamp_is_accepted(self) -> None:
        """Aware is the requirement; the offset is a rendering detail. Instants
        compare correctly across zones, so a +05:00 timestamp is not an error
        the way a naive one is."""
        start = datetime(2026, 1, 1, 5, tzinfo=timezone(timedelta(hours=5)))
        row = MarketCoverage(
            ticker="KXA-1",
            series_ticker="KXA",
            first_ts=start,
            last_ts=start + timedelta(days=1),
            observations=10,
            resolved_outcome=None,
        )
        assert row.span == timedelta(days=1)
        assert row.first_ts.astimezone(UTC) == datetime(2026, 1, 1, tzinfo=UTC)

    def test_an_inverted_market_span_raises(self) -> None:
        with pytest.raises(ValueError, match="precedes"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=WINDOW_END,
                last_ts=WINDOW_START,
                observations=10,
                resolved_outcome=None,
            )

    def test_a_row_with_no_observations_raises(self) -> None:
        """An empty row is not coverage. Emitting one would inflate the market
        count with markets we hold nothing for."""
        with pytest.raises(ValueError, match="not coverage"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=WINDOW_START,
                last_ts=WINDOW_END,
                observations=0,
                resolved_outcome=None,
            )

    def test_a_negative_max_gap_raises(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            MarketCoverage(
                ticker="KXA-1",
                series_ticker="KXA",
                first_ts=WINDOW_START,
                last_ts=WINDOW_END,
                observations=10,
                resolved_outcome=None,
                max_gap=timedelta(seconds=-1),
            )

    def test_an_inverted_window_raises(self) -> None:
        with pytest.raises(ValueError, match="must be after"):
            audit(
                healthy(),
                window_start=WINDOW_END,
                window_end=WINDOW_START,
                expected_interval=INTERVAL,
            )

    @pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1)])
    def test_a_non_positive_expected_interval_raises(self, bad: timedelta) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            run(healthy(), interval=bad)


# ---------------------------------------------------------------------------
# NO_DATA
# ---------------------------------------------------------------------------


class TestNoData:
    def test_an_empty_sample_refuses(self) -> None:
        report = run([])
        assert codes(report) == {RefusalCode.NO_DATA}

    def test_no_data_is_reported_alone(self) -> None:
        """Every other check fails trivially on an empty window, and seven
        refusals bury the one that matters."""
        report = run([])
        assert len(report.refusals) == 1
        assert report.warning_codes == ()

    def test_an_empty_report_still_carries_the_window(self) -> None:
        report = run([])
        assert report.observed_start is None
        assert report.observed_span == timedelta(0)
        assert report.window_fill == 0.0
        assert report.observations_per_market == 0.0


# ---------------------------------------------------------------------------
# TOO_FEW_MARKETS
# ---------------------------------------------------------------------------


class TestTooFewMarkets:
    def test_it_fires_below_the_floor(self) -> None:
        report = run(healthy(markets=150, settled=120))
        assert codes(report) == {RefusalCode.TOO_FEW_MARKETS}

    def test_it_does_not_fire_at_the_floor(self) -> None:
        report = run(healthy(markets=MIN_MARKETS, settled=120))
        assert RefusalCode.TOO_FEW_MARKETS not in codes(report)

    def test_the_message_names_the_measurement_and_the_floor(self) -> None:
        """'insufficient data' tells the operator nothing; '79 against 200'
        tells them whether to wait a week or change the config."""
        report = run(healthy(markets=150, settled=120))
        finding = report.refusals[0]
        assert finding.measured == "150 markets"
        assert finding.threshold == f"{MIN_MARKETS} markets"
        assert "150" in finding.message and str(MIN_MARKETS) in finding.message

    def test_a_custom_floor_is_honoured(self) -> None:
        report = run(healthy(markets=150, settled=120), min_markets=100)
        assert RefusalCode.TOO_FEW_MARKETS not in codes(report)


# ---------------------------------------------------------------------------
# WINDOW_TOO_SHORT — measured on the observed span, never the requested one
# ---------------------------------------------------------------------------


class TestWindowTooShort:
    def test_it_fires_on_a_short_observed_span(self) -> None:
        report = run(healthy(span=timedelta(days=10)))
        assert RefusalCode.WINDOW_TOO_SHORT in codes(report)

    def test_it_does_not_fire_at_the_floor(self) -> None:
        report = run(healthy(span=MIN_OBSERVED_SPAN))
        assert RefusalCode.WINDOW_TOO_SHORT not in codes(report)

    def test_a_generous_requested_window_does_not_rescue_a_short_span(self) -> None:
        """Asking for three years does not produce three years. The refusal
        reads the data, not the request."""
        report = run(
            healthy(span=timedelta(hours=6), observations=100),
            window_end=WINDOW_START + timedelta(days=1095),
        )
        assert RefusalCode.WINDOW_TOO_SHORT in codes(report)
        assert report.requested_span == timedelta(days=1095)
        assert report.observed_span == timedelta(hours=6)

    def test_the_message_distinguishes_observed_from_requested(self) -> None:
        report = run(healthy(span=timedelta(days=10)))
        finding = next(
            f for f in report.refusals if f.code is RefusalCode.WINDOW_TOO_SHORT
        )
        assert "observed span" in finding.message
        assert "10 days" in finding.measured


# ---------------------------------------------------------------------------
# TOO_FEW_SETTLED — the binding constraint in practice
# ---------------------------------------------------------------------------


class TestTooFewSettled:
    def test_it_fires_below_the_floor(self) -> None:
        report = run(healthy(settled=50))
        assert codes(report) == {RefusalCode.TOO_FEW_SETTLED}

    def test_it_does_not_fire_at_the_floor(self) -> None:
        report = run(healthy(settled=MIN_SETTLED_MARKETS))
        assert RefusalCode.TOO_FEW_SETTLED not in codes(report)

    def test_open_markets_do_not_substitute_for_outcomes(self) -> None:
        """Ten thousand markets with no result is still no ground truth: they
        contribute a cost basis and can never be shown to have made money."""
        report = run(healthy(markets=10_000, settled=0))
        assert RefusalCode.TOO_FEW_MARKETS not in codes(report)
        assert RefusalCode.TOO_FEW_SETTLED in codes(report)

    def test_the_message_names_both_counts(self) -> None:
        report = run(healthy(settled=50))
        finding = report.refusals[0]
        assert finding.measured == "50 settled markets"
        assert "400" in finding.message  # the sample it came from


# ---------------------------------------------------------------------------
# SURVIVORSHIP — the least obvious refusal here
# ---------------------------------------------------------------------------


class TestSurvivorship:
    def test_it_fires_when_every_market_has_settled(self) -> None:
        """Full ground truth sounds like the good case. It means the sample was
        drawn from markets that resolved inside the window — the short-dated
        ones — and every long-dated market tradeable throughout is missing."""
        report = run(healthy(markets=400, settled=400))
        assert codes(report) == {RefusalCode.SURVIVORSHIP}

    def test_it_does_not_fire_on_a_healthy_mix(self) -> None:
        report = run(healthy(markets=400, settled=240))
        assert RefusalCode.SURVIVORSHIP not in codes(report)

    def test_a_single_open_market_clears_it(self) -> None:
        """The check is 'nothing is open', not a fraction: any threshold below
        1.0 would refuse a genuinely matured historical window."""
        report = run(healthy(markets=400, settled=399))
        assert RefusalCode.SURVIVORSHIP not in codes(report)

    def test_it_reports_closes_inside_the_window_as_evidence(self) -> None:
        report = run(healthy(markets=400, settled=400))
        finding = report.refusals[0]
        assert report.closed_in_window == 400
        assert "closed inside the window" in finding.message

    def test_it_fires_alongside_a_thin_outcome_count(self) -> None:
        """A tiny all-settled sample is both too few outcomes and biased."""
        report = run(healthy(markets=50, settled=50))
        assert RefusalCode.SURVIVORSHIP in codes(report)
        assert RefusalCode.TOO_FEW_SETTLED in codes(report)

    def test_the_two_outcome_floors_do_not_contradict_each_other(self) -> None:
        """MIN_SETTLED_MARKETS sits below MIN_MARKETS deliberately. Were they
        equal, a sample could only clear the outcome floor by approaching
        all-settled, which is what survivorship refuses."""
        assert MIN_SETTLED_MARKETS < MIN_MARKETS


# ---------------------------------------------------------------------------
# SINGLE_SERIES and concentration
# ---------------------------------------------------------------------------


class TestSeriesDiversity:
    def test_a_single_series_sample_refuses(self) -> None:
        report = run(healthy(series=1))
        assert RefusalCode.SINGLE_SERIES in codes(report)

    def test_a_diverse_sample_does_not_refuse(self) -> None:
        report = run(healthy(series=20))
        assert RefusalCode.SINGLE_SERIES not in codes(report)

    def test_two_series_clears_the_hard_gate(self) -> None:
        """The gate catches only the degenerate case; it certifies nothing."""
        report = run(healthy(series=2))
        assert RefusalCode.SINGLE_SERIES not in codes(report)

    def test_markets_with_no_series_group_together(self) -> None:
        """Unknown series can only make a sample look less diverse, never
        more — the conservative direction."""
        rows = [
            MarketCoverage(
                ticker=f"UNKNOWN-{i}",
                series_ticker=None,
                first_ts=WINDOW_START,
                last_ts=WINDOW_END,
                observations=20_000,
                resolved_outcome=True,
                max_gap=timedelta(minutes=1),
            )
            for i in range(400)
        ]
        report = run(rows)
        assert report.series_count == 1
        assert RefusalCode.SINGLE_SERIES in codes(report)

    def test_a_dominant_series_warns_rather_than_refusing(self) -> None:
        """A hard gate at two series is satisfied by adding one market, so the
        concentration warning is what actually informs the operator."""
        rows = healthy(markets=400, series=2, settled=240)
        skewed = [
            MarketCoverage(
                ticker=r.ticker,
                series_ticker="KXSER0" if i % 20 else "KXSER1",
                first_ts=r.first_ts,
                last_ts=r.last_ts,
                observations=r.observations,
                resolved_outcome=r.resolved_outcome,
                close_time=r.close_time,
                max_gap=r.max_gap,
            )
            for i, r in enumerate(rows)
        ]
        report = run(skewed)
        assert report.refusal_codes == ()
        assert RefusalCode.SERIES_CONCENTRATION in warns(report)
        assert report.largest_series_share == pytest.approx(0.95)

    def test_a_balanced_sample_does_not_warn(self) -> None:
        report = run(healthy(series=20))
        assert RefusalCode.SERIES_CONCENTRATION not in warns(report)


# ---------------------------------------------------------------------------
# GAPS_TOO_LARGE
# ---------------------------------------------------------------------------


class TestGaps:
    def test_the_tolerance_is_a_multiple_floored_at_five_minutes(self) -> None:
        assert gap_tolerance(timedelta(seconds=1)) == MAX_GAP_FLOOR
        assert gap_tolerance(timedelta(minutes=1)) == timedelta(minutes=20)
        assert gap_tolerance(timedelta(minutes=1), multiple=5) == timedelta(minutes=5)

    def test_a_typical_market_beyond_tolerance_refuses(self) -> None:
        report = run(healthy(max_gap=timedelta(minutes=45)))
        assert RefusalCode.GAPS_TOO_LARGE in codes(report)

    def test_a_typical_market_inside_tolerance_does_not_refuse(self) -> None:
        report = run(healthy(max_gap=timedelta(minutes=19)))
        assert RefusalCode.GAPS_TOO_LARGE not in codes(report)

    def test_the_gap_at_exactly_the_tolerance_is_allowed(self) -> None:
        report = run(healthy(max_gap=timedelta(minutes=20)))
        assert RefusalCode.GAPS_TOO_LARGE not in codes(report)

    def test_one_bad_market_warns_but_does_not_void_the_run(self) -> None:
        """Keyed to the median: one ingest hiccup is not a broken dataset, but
        the operator is told to go look at it."""
        rows = healthy(max_gap=timedelta(minutes=5))
        rows[0] = MarketCoverage(
            ticker=rows[0].ticker,
            series_ticker=rows[0].series_ticker,
            first_ts=rows[0].first_ts,
            last_ts=rows[0].last_ts,
            observations=rows[0].observations,
            resolved_outcome=rows[0].resolved_outcome,
            max_gap=timedelta(hours=9),
        )
        report = run(rows)
        assert report.refusal_codes == ()
        assert RefusalCode.ISOLATED_GAPS in warns(report)
        assert report.worst_gap == timedelta(hours=9)

    def test_gaps_are_implied_from_counts_when_unmeasured(self) -> None:
        """span / (observations - 1). 120 days over 200 rows is ~14.5 hours a
        row, far past a 20-minute tolerance."""
        report = run(healthy(max_gap=None, observations=200))
        assert report.gaps_measured is False
        assert RefusalCode.GAPS_TOO_LARGE in codes(report)

    def test_an_implied_estimate_warns_that_it_is_a_lower_bound(self) -> None:
        """A market with one long hole and otherwise dense sampling shows a
        small mean, so the reported figures are optimistic."""
        report = run(healthy(max_gap=None))
        assert RefusalCode.GAP_ESTIMATE_IMPRECISE in warns(report)
        assert report.refusal_codes == ()

    def test_a_measured_gap_suppresses_the_imprecision_warning(self) -> None:
        report = run(healthy(max_gap=timedelta(minutes=10)))
        assert RefusalCode.GAP_ESTIMATE_IMPRECISE not in warns(report)
        assert report.gaps_measured is True

    def test_a_measured_gap_overrides_the_implied_one(self) -> None:
        """The caller's lag() is authoritative: dense sampling with one real
        hole must not be laundered into a small mean."""
        report = run(healthy(observations=1_000_000, max_gap=timedelta(hours=3)))
        assert report.median_gap == timedelta(hours=3)
        assert RefusalCode.GAPS_TOO_LARGE in codes(report)

    def test_a_single_observation_market_counts_as_a_full_window_gap(self) -> None:
        """One snapshot is one frozen book. Replaying it across the window
        assumes the book never moved, which is the whole point of the check —
        treating it as 'no gap' would let such a sample sail through."""
        rows = [
            MarketCoverage(
                ticker=f"KXSER{i % 20}-26JAN-{i}",
                series_ticker=f"KXSER{i % 20}",
                first_ts=WINDOW_START,
                last_ts=WINDOW_START,
                observations=1,
                resolved_outcome=i % 2 == 0,
                max_gap=None,
            )
            for i in range(400)
        ]
        report = run(rows)
        assert report.single_observation_markets == 400
        assert report.median_gap == WINDOW_SPAN
        assert RefusalCode.GAPS_TOO_LARGE in codes(report)

    def test_a_few_single_observation_markets_do_not_void_a_healthy_run(self) -> None:
        rows = healthy(max_gap=None, markets=400)
        rows[0] = MarketCoverage(
            ticker="KXSER0-26JAN-lonely",
            series_ticker="KXSER0",
            first_ts=WINDOW_START,
            last_ts=WINDOW_START,
            observations=1,
            resolved_outcome=True,
        )
        report = run(rows)
        assert report.single_observation_markets == 1
        assert report.refusal_codes == ()
        assert RefusalCode.ISOLATED_GAPS in warns(report)


# ---------------------------------------------------------------------------
# PARTIAL_WINDOW
# ---------------------------------------------------------------------------


class TestPartialWindow:
    def test_a_short_slice_of_a_long_request_warns(self) -> None:
        report = run(healthy(span=timedelta(days=40)))
        assert RefusalCode.PARTIAL_WINDOW in warns(report)
        assert report.window_fill == pytest.approx(40 / 120)

    def test_a_full_window_does_not_warn(self) -> None:
        report = run(healthy(span=WINDOW_SPAN))
        assert RefusalCode.PARTIAL_WINDOW not in warns(report)

    def test_it_warns_rather_than_refusing(self) -> None:
        """The absolute floors are the real gate. What this catches is the
        label on the result, not the result."""
        report = run(healthy(span=timedelta(days=40)))
        assert report.refusal_codes == ()
        assert report.usable is True


# ---------------------------------------------------------------------------
# This deployment, today
# ---------------------------------------------------------------------------


class TestThisDeploymentToday:
    """Why the backtester says no on 2026-07-27.

    Reconstructed from the live database: 1,873 orderbook snapshots across 79
    distinct tickers spanning 9.8 hours, nothing settled, no fills, no
    settlements. The audit is run at a 1-second expected interval, which is
    the book-snapshot cadence.
    """

    OBSERVED_END = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
    OBSERVED_SPAN = timedelta(hours=9, minutes=48)  # 9.8 hours
    OBSERVED_START = OBSERVED_END - OBSERVED_SPAN
    REQUESTED_START = OBSERVED_END - timedelta(days=90)

    def rows(self) -> list[MarketCoverage]:
        # 1,873 rows over 79 tickers: 56 tickers hold 24 rows, 23 hold 23.
        rows: list[MarketCoverage] = []
        for i in range(79):
            rows.append(
                MarketCoverage(
                    ticker=f"KXSER{i % 20}-26JUL-{i}",
                    series_ticker=f"KXSER{i % 20}",
                    first_ts=self.OBSERVED_START,
                    last_ts=self.OBSERVED_END,
                    observations=24 if i < 56 else 23,
                    resolved_outcome=None,  # zero settlements recorded
                    close_time=None,
                    max_gap=None,  # no lag() measurement available
                )
            )
        assert sum(r.observations for r in rows) == 1873
        return rows

    def report(self) -> CoverageReport:
        return audit(
            self.rows(),
            window_start=self.REQUESTED_START,
            window_end=self.OBSERVED_END,
            expected_interval=timedelta(seconds=1),
        )

    def test_it_refuses(self) -> None:
        report = self.report()
        assert report.usable is False
        with pytest.raises(CoverageRefused):
            assert_usable(report)

    def test_it_trips_exactly_four_refusals(self) -> None:
        """Named precisely, because 'insufficient data' would not tell the
        operator that three months of ingest fixes three of them and the
        fourth needs markets to actually resolve."""
        assert codes(self.report()) == {
            RefusalCode.TOO_FEW_MARKETS,
            RefusalCode.WINDOW_TOO_SHORT,
            RefusalCode.TOO_FEW_SETTLED,
            RefusalCode.GAPS_TOO_LARGE,
        }

    def test_seventy_nine_markets_against_a_floor_of_two_hundred(self) -> None:
        finding = next(
            f for f in self.report().refusals if f.code is RefusalCode.TOO_FEW_MARKETS
        )
        assert finding.measured == "79 markets"
        assert finding.threshold == "200 markets"

    def test_nine_point_eight_hours_against_a_floor_of_thirty_days(self) -> None:
        finding = next(
            f
            for f in self.report().refusals
            if f.code is RefusalCode.WINDOW_TOO_SHORT
        )
        assert finding.measured == "9.8 hours"
        assert finding.threshold == "30 days"

    def test_zero_settled_markets_is_the_constraint_ingest_cannot_fix(self) -> None:
        """The other three refusals clear with time. This one needs markets in
        the sample to resolve, and the deployment has recorded no settlements
        at all."""
        report = self.report()
        assert report.settled_count == 0
        assert report.unsettled_count == 79
        finding = next(
            f for f in report.refusals if f.code is RefusalCode.TOO_FEW_SETTLED
        )
        assert finding.measured == "0 settled markets"

    def test_the_book_is_sampled_every_twenty_six_minutes_not_every_second(
        self,
    ) -> None:
        """1,873 rows over 79 markets and 9.8 hours is roughly 24 snapshots a
        market. A replay at a 1-second cadence would be interpolating across
        26-minute holes and calling the result a fill."""
        report = self.report()
        assert report.tolerated_gap == MAX_GAP_FLOOR
        assert report.median_gap is not None
        assert timedelta(minutes=25) < report.median_gap < timedelta(minutes=27)

    def test_survivorship_does_not_fire_because_nothing_has_settled(self) -> None:
        """The mirror image of the survivorship trap: there is no bias in the
        resolved sample because there is no resolved sample."""
        assert RefusalCode.SURVIVORSHIP not in codes(self.report())

    def test_single_series_does_not_fire_because_the_watchlist_spans_series(
        self,
    ) -> None:
        report = self.report()
        assert report.series_count == 20
        assert RefusalCode.SINGLE_SERIES not in codes(report)

    def test_it_warns_that_the_gap_figures_are_optimistic(self) -> None:
        assert warns(self.report()) == {
            RefusalCode.GAP_ESTIMATE_IMPRECISE,
            RefusalCode.PARTIAL_WINDOW,
        }

    def test_the_data_covers_half_a_percent_of_the_requested_window(self) -> None:
        report = self.report()
        assert report.window_fill == pytest.approx(9.8 / (90 * 24), abs=1e-4)

    def test_the_summary_reads_as_an_explanation(self) -> None:
        text = "\n".join(self.report().summary_lines())
        assert "79" in text
        assert "1873" in text
        assert "9.8 hours" in text
        assert "VERDICT: refused" in text


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


class TestAssertUsable:
    def test_it_raises_on_any_refusal(self) -> None:
        with pytest.raises(CoverageRefused):
            assert_usable(run(healthy(markets=10, settled=5)))

    def test_every_refusal_is_reachable_from_the_exception(self) -> None:
        """An operator fixing them one at a time waits a week per round trip,
        since the fix for most of them is 'collect more data'."""
        report = run(
            healthy(
                markets=10,
                series=1,
                settled=10,
                span=timedelta(hours=2),
                observations=2,
                max_gap=None,
            )
        )
        with pytest.raises(CoverageRefused) as excinfo:
            assert_usable(report)
        assert set(excinfo.value.codes) == {
            RefusalCode.TOO_FEW_MARKETS,
            RefusalCode.WINDOW_TOO_SHORT,
            RefusalCode.TOO_FEW_SETTLED,
            RefusalCode.SURVIVORSHIP,
            RefusalCode.SINGLE_SERIES,
            RefusalCode.GAPS_TOO_LARGE,
        }
        assert len(excinfo.value.refusals) == 6

    def test_the_exception_message_lists_every_refusal(self) -> None:
        report = run(healthy(markets=10, settled=5, span=timedelta(hours=2)))
        with pytest.raises(CoverageRefused) as excinfo:
            assert_usable(report)
        text = str(excinfo.value)
        assert text.count("  - ") == len(report.refusals)
        assert "10 markets" in text

    def test_the_exception_carries_the_whole_report(self) -> None:
        """The numbers are what an operator needs to decide whether to wait."""
        with pytest.raises(CoverageRefused) as excinfo:
            assert_usable(run(healthy(markets=10, settled=5)))
        assert excinfo.value.report.market_count == 10

    def test_warnings_alone_do_not_block(self) -> None:
        report = run(healthy(max_gap=None, span=timedelta(days=40)))
        assert report.warnings
        assert assert_usable(report) is None

    def test_a_report_is_returned_rather_than_raised_on_thin_data(self) -> None:
        """Coverage has to be renderable without catching an exception, or the
        only way to see the numbers is to fail."""
        report = run([])
        assert isinstance(report, CoverageReport)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_summary_lines_are_plain_strings(self) -> None:
        lines = run(healthy()).summary_lines()
        assert lines and all(isinstance(line, str) for line in lines)

    def test_the_healthy_verdict_is_usable(self) -> None:
        assert "VERDICT: usable" in "\n".join(run(healthy()).summary_lines())

    def test_measurements_precede_findings(self) -> None:
        """An operator reading this is deciding whether another month of ingest
        would help — a question about the numbers, not about which rule
        tripped."""
        lines = run(healthy(markets=10, settled=5)).summary_lines()
        first_finding = next(
            i for i, line in enumerate(lines) if line.startswith("REFUSAL")
        )
        assert lines[0].startswith("window requested")
        assert first_finding > 0

    def test_every_finding_renders_its_severity_and_code(self) -> None:
        report = run(healthy(markets=10, settled=5, max_gap=None))
        for finding in report.refusals:
            assert finding.render().startswith("REFUSAL ")
            assert finding.blocking is True
        for finding in report.warnings:
            assert finding.render().startswith("WARNING ")
            assert finding.blocking is False

    def test_severity_and_codes_are_plain_strings(self) -> None:
        """StrEnum so a dashboard payload serialises without a custom encoder."""
        assert Severity.REFUSAL == "refusal"
        assert RefusalCode.TOO_FEW_SETTLED == "too_few_settled"

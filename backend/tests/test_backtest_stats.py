"""Tests for the backtest report card.

The report card's failure mode is the same one the longshot screen has: not a
wrong number but a *confident* one. Twenty-four trades, a cheerful mean, a
symmetric interval computed as if a two-point bet were a bell curve, and a
verdict that reads like permission. So most of what follows pins what the
module refuses to say, and how far the honest interval differs from the easy
one on the samples this system actually produces.

The P&L constants below are not round on purpose. A 10c longshot that settles
YES returns 90c less a taker fee that rounds up to a centicent, so the real
number is 89.9776c, and nothing in this module may assume otherwise.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.stats import (
    Expectancy,
    Verdict,
    bootstrap_interval,
    brier_decomposition,
    brier_score,
    describe,
    expectancy,
    max_drawdown,
    normal_interval,
    verdict,
)

#: A 10c contract bought and held to a YES settlement: +90c gross, less the
#: taker fee (0.07 * 1 * 0.10 * 0.90 = $0.0063 -> 0.63c) rounded up to a
#: centicent. Not 90.
LONGSHOT_WIN = Decimal("89.9776")
#: The same contract settling NO: the 10c stake plus the same entry fee.
LONGSHOT_LOSS = Decimal("-10.0224")

#: The mirror image: a 90c favourite. Frequent small wins, rare large loss.
FAVOURITE_WIN = Decimal("9.9776")
FAVOURITE_LOSS = Decimal("-90.0224")


def longshot(wins: int, losses: int) -> list[Decimal]:
    """A two-point P&L series: `wins` rare large wins, `losses` small losses."""
    return [LONGSHOT_WIN] * wins + [LONGSHOT_LOSS] * losses


def favourite(wins: int, losses: int) -> list[Decimal]:
    """The left-skewed mirror: frequent small wins, rare large losses."""
    return [FAVOURITE_WIN] * wins + [FAVOURITE_LOSS] * losses


# ---------------------------------------------------------------------------
# Expectancy arithmetic
# ---------------------------------------------------------------------------


class TestExpectancyArithmetic:
    def test_total_and_mean_on_a_hand_checkable_series(self) -> None:
        """+10, -5, +2.50, -1.25 sums to 6.25 over 4 trades: 1.5625c/trade."""
        pnls = [
            Decimal("10.00"),
            Decimal("-5.00"),
            Decimal("2.50"),
            Decimal("-1.25"),
        ]
        exp = expectancy(pnls, resamples=200)
        assert exp.n == 4
        assert exp.total_cents == Decimal("6.25")
        assert exp.mean_cents == Decimal("1.5625")

    def test_the_mean_keeps_centicent_precision(self) -> None:
        """Fees round to a centicent, so a mean that rounds to a cent has
        thrown away real money. 1/3 of a cent must not come back as 0.33."""
        exp = expectancy([Decimal("1"), Decimal("0"), Decimal("0")], resamples=200)
        assert exp.mean_cents == Decimal("0.3333")

    def test_the_total_is_an_exact_decimal_sum(self) -> None:
        """Summing 0.1 a hundred times must be 10, not 9.99999999999998."""
        exp = expectancy([Decimal("0.1")] * 100, resamples=200)
        assert exp.total_cents == Decimal("10.0")

    def test_the_standard_error_is_the_sample_sd_over_root_n(self) -> None:
        """1,2,3,4: s = sqrt(5/3) = 1.290994, stderr = s/2 = 0.645497."""
        exp = expectancy(
            [Decimal(1), Decimal(2), Decimal(3), Decimal(4)], resamples=200
        )
        assert exp.stderr_cents == Decimal("0.6455")

    def test_money_is_never_float(self) -> None:
        """A float P&L has already lost the precision the ledger preserved."""
        with pytest.raises(TypeError, match="never float"):
            expectancy([Decimal("1.0"), 2.5])  # type: ignore[list-item]

    def test_a_bool_is_refused_as_pnl(self) -> None:
        """`bool` is an `int`. An outcomes list handed to a money function
        would otherwise average to 0.5c of profit and look plausible."""
        with pytest.raises(TypeError, match="outcomes sequence"):
            expectancy([True, False])  # type: ignore[list-item]

    def test_an_unknown_interval_method_is_refused(self) -> None:
        """A silently substituted interval is a silently substituted verdict."""
        with pytest.raises(ValueError, match="unknown interval method"):
            expectancy([Decimal(1), Decimal(2)], method="bca")


class TestExpectancyEdgeCases:
    def test_no_trades_produce_no_claim(self) -> None:
        exp = expectancy([])
        assert exp.n == 0
        assert exp.total_cents == Decimal(0)
        assert exp.stderr_cents is None
        assert exp.ci_low_cents is None and exp.ci_high_cents is None
        assert exp.method == "none"

    def test_one_trade_has_a_mean_but_no_spread(self) -> None:
        """A standard error of zero on one trade would read as certainty."""
        exp = expectancy([LONGSHOT_WIN])
        assert exp.n == 1
        assert exp.mean_cents == LONGSHOT_WIN
        assert exp.stderr_cents is None
        assert exp.ci_low_cents is None and exp.ci_high_cents is None
        assert exp.method == "none"

    def test_one_trade_can_never_reach_a_positive_verdict(self) -> None:
        """Even with the floor dropped to 1, a lone +90c trade proves nothing."""
        exp = expectancy([LONGSHOT_WIN])
        assert verdict(exp, min_trades=1) is Verdict.INSUFFICIENT_EVIDENCE

    def test_neither_interval_exists_below_two_trades(self) -> None:
        assert normal_interval([LONGSHOT_WIN]) is None
        assert bootstrap_interval([LONGSHOT_WIN]) is None
        assert normal_interval([]) is None
        assert bootstrap_interval([]) is None

    def test_two_identical_trades_give_a_zero_width_interval(self) -> None:
        """Degenerate but honest: a sample with no spread supports no spread.
        The verdict below is EDGE_SHOWN, and correctly so — every observation
        agreed. The sample floor is what stops that being reportable."""
        pnls = [Decimal("5"), Decimal("5")]
        assert normal_interval(pnls) == (Decimal("5.0000"), Decimal("5.0000"))
        assert bootstrap_interval(pnls) == (Decimal("5.0000"), Decimal("5.0000"))


# ---------------------------------------------------------------------------
# The bootstrap — why the module has two intervals
# ---------------------------------------------------------------------------


class TestBootstrapDeterminism:
    def test_the_same_seed_gives_the_same_interval(self) -> None:
        """A report card that changed its verdict between two runs on identical
        data would be indistinguishable from one that changed because the data
        changed."""
        pnls = longshot(wins=4, losses=26)
        first = bootstrap_interval(pnls, resamples=2_000, seed=7)
        second = bootstrap_interval(pnls, resamples=2_000, seed=7)
        assert first == second

    def test_the_default_seed_is_fixed(self) -> None:
        pnls = longshot(wins=4, losses=26)
        assert bootstrap_interval(pnls) == bootstrap_interval(pnls, seed=0)

    def test_a_different_seed_moves_the_endpoint_by_at_most_one_lattice_step(
        self,
    ) -> None:
        """Determinism is a choice, not an artefact of the interval being
        insensitive to resampling noise. But the sensitivity is bounded by the
        discreteness of the bet, not by Monte-Carlo error: with 30 trades there
        are only 31 possible win counts, so every resample mean is a multiple
        of (win - loss)/n = 100/30 = 3.3333c and a percentile can only ever
        move between adjacent lattice points. Measured: seed 0 gives an upper
        bound of 23.3109 and seed 12345 gives 26.6443 — exactly one step.

        This is worth knowing before reading an interval off a 30-trade sample
        as though its endpoints were precise to the centicent they are printed
        at. They are precise to about 3c.
        """
        pnls = longshot(wins=6, losses=24)
        # One lattice step, plus the centicent the reported bound is
        # quantized to (100/30 is 3.3333... and comes back as 3.3334).
        step = (LONGSHOT_WIN - LONGSHOT_LOSS) / Decimal(len(pnls)) + Decimal("0.0001")
        a = bootstrap_interval(pnls, seed=0)
        b = bootstrap_interval(pnls, seed=12345)
        assert a is not None and b is not None
        assert abs(a[0] - b[0]) <= step
        assert abs(a[1] - b[1]) <= step

    def test_resamples_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="resamples"):
            bootstrap_interval(longshot(4, 26), resamples=0)

    def test_a_confidence_outside_the_unit_interval_is_refused(self) -> None:
        """95, not 0.95 — the usual way this gets called wrong."""
        with pytest.raises(ValueError, match="confidence"):
            bootstrap_interval(longshot(4, 26), confidence=95.0)
        with pytest.raises(ValueError, match="confidence"):
            normal_interval(longshot(4, 26), confidence=95.0)

    def test_a_higher_confidence_widens_both_intervals(self) -> None:
        pnls = longshot(wins=5, losses=25)
        for interval in (normal_interval, bootstrap_interval):
            narrow = interval(pnls, confidence=0.95)
            wide = interval(pnls, confidence=0.99)
            assert narrow is not None and wide is not None
            assert wide[0] <= narrow[0] and wide[1] >= narrow[1]


class TestBootstrapVersusNormal:
    """The reason this module carries two intervals instead of one.

    Measured on a 10c longshot series of 3 wins in 30 (mean -0.0224c/trade):

        normal    [-10.9411, +10.8963]   width 21.8374
        bootstrap [-10.0224, +13.3109]   width 23.3333

    The normal interval's lower bound is an *impossible* outcome: a 10c
    contract cannot lose more than 10.0224c per trade, so no sequence of trades
    averages -10.94. The bootstrap cannot report it, because no resample of
    this sample produces it.
    """

    SAMPLE = longshot(wins=3, losses=27)

    def test_the_bootstrap_is_wider_on_this_skewed_two_point_sample(self) -> None:
        """Here the skew lengthens the right arm by more than the bounded
        support shortens the left, so the bootstrap comes out wider.

        This is NOT a universal property and the module does not claim it is:
        swept over 175 longshot samples the percentile bootstrap is narrower
        than Wald more often than wider (128 vs 47), because to leading order
        skew shifts both endpoints by the same amount and leaves the width
        alone. The robust difference is asymmetry, pinned below.
        """
        nrm = normal_interval(self.SAMPLE)
        boot = bootstrap_interval(self.SAMPLE)
        assert nrm is not None and boot is not None
        assert (boot[1] - boot[0]) > (nrm[1] - nrm[0])

    def test_the_bootstrap_is_asymmetric_about_the_mean(self) -> None:
        """The property that actually justifies the bootstrap. On a right-
        skewed sample the arm towards the rare large win is the longer one;
        Wald's two arms are equal by construction, whatever the data does."""
        exp = expectancy(self.SAMPLE)
        assert exp.ci_low_cents is not None and exp.ci_high_cents is not None
        lower_arm = exp.mean_cents - exp.ci_low_cents
        upper_arm = exp.ci_high_cents - exp.mean_cents
        assert upper_arm > lower_arm
        assert upper_arm == Decimal("13.3333")
        assert lower_arm == Decimal("10.0000")

    def test_the_bootstrap_respects_the_support_of_the_data(self) -> None:
        """Its lower bound is exactly the all-losses mean — the worst average
        that can physically happen. Wald reports one that cannot."""
        nrm = normal_interval(self.SAMPLE)
        boot = bootstrap_interval(self.SAMPLE)
        assert nrm is not None and boot is not None
        assert boot[0] == LONGSHOT_LOSS
        assert nrm[0] < LONGSHOT_LOSS

    def test_a_longshot_series_where_the_two_disagree_about_zero(self) -> None:
        """10 wins in 45 at 10c. Measured:

            normal    [-0.0843, +24.4839]  -> straddles zero, NO_EDGE_SHOWN
            bootstrap [+1.0887, +25.5332]  -> excludes zero, EDGE_SHOWN

        Same data, opposite verdicts. Wald's lower bound is dragged below zero
        by the symmetry it assumes; the bootstrap, which knows the losses are
        capped at 10c, keeps it above.
        """
        pnls = longshot(wins=10, losses=35)

        by_normal = expectancy(pnls, method="normal")
        by_bootstrap = expectancy(pnls, method="bootstrap")

        assert by_normal.ci_low_cents == Decimal("-0.0843")
        assert by_bootstrap.ci_low_cents == Decimal("1.0887")

        assert verdict(by_normal, min_trades=20) is Verdict.NO_EDGE_SHOWN
        assert verdict(by_bootstrap, min_trades=20) is Verdict.EDGE_SHOWN

    def test_a_favourite_series_where_wald_claims_an_edge_and_the_bootstrap_does_not(
        self,
    ) -> None:
        """The dangerous direction, and the reason the gate uses the bootstrap.

        29 wins of +9.9776c and one loss of -90.0224c. Measured:

            normal    [+0.1111, +13.1775]  -> EDGE_SHOWN
            bootstrap [-0.0224,  +9.9776]  -> NO_EDGE_SHOWN

        Selling longshots looks like free money until the rare leg lands. Wald
        sees 30 numbers with a positive mean; the bootstrap sees that one draw
        in thirty is worth -90c and that a sample containing two of them is
        entirely ordinary.
        """
        pnls = favourite(wins=29, losses=1)

        by_normal = expectancy(pnls, method="normal")
        by_bootstrap = expectancy(pnls, method="bootstrap")

        assert by_normal.ci_low_cents == Decimal("0.1111")
        assert by_bootstrap.ci_low_cents == Decimal("-0.0224")

        assert verdict(by_normal, min_trades=20) is Verdict.EDGE_SHOWN
        assert verdict(by_bootstrap, min_trades=20) is Verdict.NO_EDGE_SHOWN

    def test_the_default_method_is_the_bootstrap(self) -> None:
        """Because of the test above. The default must not be the optimistic
        one."""
        pnls = favourite(wins=29, losses=1)
        assert expectancy(pnls).method == "bootstrap"
        assert expectancy(pnls).ci_low_cents == expectancy(
            pnls, method="bootstrap"
        ).ci_low_cents

    def test_the_two_intervals_converge_on_a_symmetric_sample(self) -> None:
        """No skew, no disagreement. The divergence above is caused by the
        shape of the bet, not by the bootstrap being a different animal."""
        pnls = [Decimal("10")] * 200 + [Decimal("-10")] * 200
        nrm = normal_interval(pnls)
        boot = bootstrap_interval(pnls)
        assert nrm is not None and boot is not None
        assert abs(boot[0] - nrm[0]) < Decimal("0.5")
        assert abs(boot[1] - nrm[1]) < Decimal("0.5")


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class TestBrierScore:
    def test_a_perfect_forecaster_scores_zero(self) -> None:
        assert brier_score([1.0, 0.0, 1.0, 0.0], [True, False, True, False]) == 0.0

    def test_a_coin_flipper_scores_a_quarter(self) -> None:
        """0.25 is the number to beat before any claim of skill."""
        score = brier_score([0.5] * 6, [True, False, True, True, False, False])
        assert score == pytest.approx(0.25)

    def test_a_coin_flipper_scores_a_quarter_whatever_happened(self) -> None:
        """Saying 50% is immune to the outcomes; nothing else is."""
        assert brier_score([0.5] * 4, [True] * 4) == pytest.approx(0.25)

    def test_confidently_wrong_scores_one(self) -> None:
        assert brier_score([1.0, 0.0], [False, True]) == pytest.approx(1.0)

    def test_it_is_the_mean_squared_error(self) -> None:
        """0.9 on a miss and 0.2 on a hit: (0.81 + 0.64) / 2."""
        assert brier_score([0.9, 0.2], [False, True]) == pytest.approx(0.725)

    def test_a_length_mismatch_is_refused(self) -> None:
        """Scoring each forecast against somebody else's market."""
        with pytest.raises(ValueError, match="same length"):
            brier_score([0.5, 0.5], [True])

    def test_an_empty_sequence_is_refused(self) -> None:
        with pytest.raises(ValueError, match="zero markets"):
            brier_score([], [])

    @pytest.mark.parametrize("bad", [1.5, -0.1, 30.0])
    def test_a_prediction_outside_the_unit_interval_is_refused(
        self, bad: float
    ) -> None:
        """30 is a price in cents, not a probability. Scoring it would hide
        the units bug rather than surface it."""
        with pytest.raises(ValueError, match="not a probability"):
            brier_score([0.5, bad], [True, False])


class TestBrierDecomposition:
    def test_the_murphy_identity_holds_exactly(self) -> None:
        """brier = reliability - resolution + uncertainty.

        The whole reason to compute three numbers instead of eyeballing a
        reliability diagram: they are checkable against the score. Forecasts
        here sit on bucket-constant values, which is the classical setting in
        which the identity is exact.
        """
        predictions = [0.05, 0.05, 0.05, 0.35, 0.35, 0.75, 0.75, 0.95, 0.95, 0.95]
        outcomes = [
            False, False, True,
            False, True,
            True, False,
            True, True, True,
        ]
        parts = brier_decomposition(predictions, outcomes)
        identity = parts.reliability - parts.resolution + parts.uncertainty
        assert identity == pytest.approx(brier_score(predictions, outcomes), abs=1e-12)
        assert parts.n == 10

    def test_the_identity_holds_for_the_coin_flipper(self) -> None:
        """Algebraically: (0.5 - b)^2 - 0 + b(1 - b) = 0.25 for any base rate
        b, so this is an exact check with no arithmetic luck in it."""
        outcomes = [True, True, False, True, False, False, False, True]
        predictions = [0.5] * 8
        parts = brier_decomposition(predictions, outcomes)
        identity = parts.reliability - parts.resolution + parts.uncertainty
        assert identity == pytest.approx(0.25, abs=1e-12)

    def test_a_constant_forecaster_has_no_resolution(self) -> None:
        """It distinguishes nothing from the base rate, which is exactly what
        zero resolution means. It can still be perfectly reliable."""
        outcomes = [True, True, False, False]
        parts = brier_decomposition([0.5] * 4, outcomes)
        assert parts.resolution == pytest.approx(0.0)
        assert parts.reliability == pytest.approx(0.0)

    def test_a_perfect_forecaster_has_resolution_equal_to_uncertainty(self) -> None:
        """Reliability 0 and resolution == uncertainty is what a Brier score of
        zero decomposes into."""
        predictions = [1.0, 1.0, 0.0, 0.0]
        outcomes = [True, True, False, False]
        parts = brier_decomposition(predictions, outcomes)
        assert parts.reliability == pytest.approx(0.0)
        assert parts.resolution == pytest.approx(parts.uncertainty)
        assert parts.uncertainty == pytest.approx(0.25)

    def test_uncertainty_belongs_to_the_markets_not_the_detector(self) -> None:
        """Two detectors, same markets, wildly different skill, identical
        uncertainty. A detector must not be credited for a sample of
        near-certain markets, which is what a raw Brier comparison does."""
        outcomes = [True, True, True, False]
        good = brier_decomposition([0.9, 0.9, 0.9, 0.1], outcomes)
        bad = brier_decomposition([0.1, 0.1, 0.1, 0.9], outcomes)
        assert good.uncertainty == bad.uncertainty
        assert good.reliability < bad.reliability

    def test_reliability_is_zero_when_stated_rates_match_observed_ones(self) -> None:
        """'When it said 30%, did it happen 30% of the time?' — 3 of 10 did."""
        predictions = [0.3] * 10
        outcomes = [True] * 3 + [False] * 7
        parts = brier_decomposition(predictions, outcomes)
        assert parts.reliability == pytest.approx(0.0, abs=1e-12)

    def test_reliability_is_large_when_a_detector_is_overconfident(self) -> None:
        """It said 90% ten times and was right three times: (0.9-0.3)^2."""
        parts = brier_decomposition([0.9] * 10, [True] * 3 + [False] * 7)
        assert parts.reliability == pytest.approx(0.36)

    def test_a_forecast_of_exactly_one_lands_in_the_top_bucket(self) -> None:
        """int(1.0 * 10) is 10, which is not a bucket. Off-by-one here would
        raise IndexError on the most common confident forecast there is."""
        parts = brier_decomposition([1.0, 1.0, 0.0], [True, True, False])
        assert parts.n == 3
        assert parts.reliability == pytest.approx(0.0)

    def test_binning_a_continuous_forecast_leaves_a_named_residual(self) -> None:
        """Documenting the limit of the identity rather than hiding it.

        When forecasts vary *inside* a bucket the identity picks up a residual
        of (within-bucket forecast variance - 2 * within-bucket covariance of
        forecast and outcome). That is a property of the binning, not of the
        detector: it shrinks as buckets get finer.
        """
        predictions = [0.11, 0.17, 0.62, 0.68, 0.64, 0.13]
        outcomes = [False, True, True, True, False, False]
        parts = brier_decomposition(predictions, outcomes, buckets=10)
        residual = brier_score(predictions, outcomes) - (
            parts.reliability - parts.resolution + parts.uncertainty
        )

        # Compute the named term directly from the two buckets in play.
        expected = 0.0
        for group in ([0, 1, 5], [2, 3, 4]):
            n_k = len(group)
            mean_p = sum(predictions[i] for i in group) / n_k
            mean_o = sum(float(outcomes[i]) for i in group) / n_k
            variance = sum((predictions[i] - mean_p) ** 2 for i in group)
            covariance = sum(
                (predictions[i] - mean_p) * (float(outcomes[i]) - mean_o)
                for i in group
            )
            expected += variance - 2 * covariance
        assert residual == pytest.approx(expected / len(predictions), abs=1e-12)

        # And it vanishes when each bucket holds a single forecast value.
        fine = brier_decomposition(predictions, outcomes, buckets=1000)
        assert brier_score(predictions, outcomes) == pytest.approx(
            fine.reliability - fine.resolution + fine.uncertainty, abs=1e-12
        )

    def test_a_bucket_count_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="buckets"):
            brier_decomposition([0.5], [True], buckets=0)

    def test_empty_buckets_do_not_contribute(self) -> None:
        """Most buckets are empty at report-card sample sizes; a divide by
        zero here would take out the whole report."""
        parts = brier_decomposition([0.05, 0.05], [True, False], buckets=10)
        assert parts.n == 2
        assert parts.uncertainty == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_an_empty_curve_has_no_drawdown(self) -> None:
        assert max_drawdown([]) == Decimal(0)

    def test_a_single_point_has_no_drawdown(self) -> None:
        """One observation is not a decline; it is not a survival either."""
        assert max_drawdown([Decimal("500")]) == Decimal(0)

    def test_a_monotonically_rising_curve_has_no_drawdown(self) -> None:
        curve = [Decimal(x) for x in (0, 10, 25, 25, 90, 400)]
        assert max_drawdown(curve) == Decimal(0)

    def test_it_picks_the_deeper_of_two_troughs(self) -> None:
        """Peak 150 -> trough 90 is 60. Peak 200 -> trough 120 is 80."""
        curve = [Decimal(x) for x in (100, 150, 90, 200, 120, 210)]
        assert max_drawdown(curve) == Decimal(80)

    def test_it_picks_the_deeper_trough_when_it_comes_first(self) -> None:
        """The same curve reversed in emphasis: 200 -> 50 is 150, and the
        later 120 -> 100 dip of 20 must not overwrite it."""
        curve = [Decimal(x) for x in (100, 200, 50, 120, 100)]
        assert max_drawdown(curve) == Decimal(150)

    def test_the_result_is_a_non_negative_magnitude(self) -> None:
        """A drawdown of 400 is reported as +400, not -400. A sign here would
        get added to something."""
        curve = [Decimal("1000"), Decimal("600")]
        assert max_drawdown(curve) == Decimal("400")
        assert max_drawdown(curve) > 0

    def test_a_curve_that_only_falls_draws_down_the_whole_way(self) -> None:
        curve = [Decimal("0"), Decimal("-10.0224"), Decimal("-20.0448")]
        assert max_drawdown(curve) == Decimal("20.0448")

    def test_it_keeps_centicent_precision(self) -> None:
        curve = [Decimal("89.9776"), Decimal("0")]
        assert max_drawdown(curve) == Decimal("89.9776")

    def test_it_is_not_order_independent(self) -> None:
        """The documented footgun, pinned so nobody 'tidies' a query by
        dropping its ORDER BY. Sorted ascending, every curve looks flawless."""
        curve = [Decimal(x) for x in (100, 150, 90, 200, 120, 210)]
        assert max_drawdown(curve) == Decimal(80)
        assert max_drawdown(sorted(curve)) == Decimal(0)

    def test_a_float_equity_point_is_refused(self) -> None:
        with pytest.raises(TypeError, match="never float"):
            max_drawdown([Decimal(0), 10.5])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# The verdict — the fail-closed gate
# ---------------------------------------------------------------------------


class TestVerdict:
    def test_below_the_sample_floor_is_insufficient_evidence(self) -> None:
        """THE test in this file.

        Four trades that all won at +90c is a mean of +90c/trade and an
        interval that excludes zero, because every observation agreed. It is
        also four trades, and four trades of a 10c longshot is a completely
        ordinary run of luck at a 10% hit rate. A report card that showed this
        as an edge would be the most persuasive wrong thing this system can
        say, so the floor is checked before the interval and there is no
        override.
        """
        exp = expectancy([LONGSHOT_WIN] * 4)
        assert exp.mean_cents == LONGSHOT_WIN
        assert exp.ci_low_cents is not None and exp.ci_low_cents > 0
        assert verdict(exp, min_trades=20) is Verdict.INSUFFICIENT_EVIDENCE

    def test_the_floor_beats_a_spectacular_mean_at_any_size(self) -> None:
        for n in (1, 2, 5, 19):
            exp = expectancy([Decimal("500")] * n)
            assert verdict(exp, min_trades=20) is Verdict.INSUFFICIENT_EVIDENCE

    def test_at_the_floor_the_interval_decides(self) -> None:
        exp = expectancy([Decimal("500")] * 20)
        assert exp.n == 20
        assert verdict(exp, min_trades=20) is Verdict.EDGE_SHOWN

    def test_a_straddling_interval_shows_no_edge(self) -> None:
        """Not 'a small edge'. The data does not distinguish this detector
        from one that does nothing."""
        exp = expectancy(longshot(wins=4, losses=26))
        assert exp.mean_cents > 0
        assert verdict(exp, min_trades=20) is Verdict.NO_EDGE_SHOWN

    def test_an_interval_entirely_above_zero_shows_an_edge(self) -> None:
        exp = expectancy(longshot(wins=10, losses=35))
        assert verdict(exp, min_trades=20) is Verdict.EDGE_SHOWN

    def test_an_interval_entirely_below_zero_is_losing(self) -> None:
        exp = expectancy(longshot(wins=1, losses=44))
        assert exp.mean_cents < 0
        assert verdict(exp, min_trades=20) is Verdict.LOSING

    def test_a_bound_sitting_on_zero_counts_as_touching_it(self) -> None:
        """Mirrors BucketStat.mispriced in the longshot screen: the question
        is whether zero is a value the data still supports, and on the
        boundary it is."""
        exp = Expectancy(
            n=40,
            total_cents=Decimal("124"),
            mean_cents=Decimal("3.1"),
            stderr_cents=Decimal("1.5"),
            ci_low_cents=Decimal("0.0000"),
            ci_high_cents=Decimal("14.4"),
            method="bootstrap",
        )
        assert verdict(exp, min_trades=20) is Verdict.NO_EDGE_SHOWN

    def test_a_missing_interval_is_insufficient_evidence(self) -> None:
        """Fail closed: an unquantified mean is not evidence, even if a caller
        hands over a hand-built Expectancy that clears the floor."""
        exp = Expectancy(
            n=100,
            total_cents=Decimal("10000"),
            mean_cents=Decimal("100"),
            stderr_cents=None,
            ci_low_cents=None,
            ci_high_cents=None,
            method="none",
        )
        assert verdict(exp, min_trades=20) is Verdict.INSUFFICIENT_EVIDENCE

    def test_no_trades_at_all_is_insufficient_evidence(self) -> None:
        assert verdict(expectancy([]), min_trades=20) is Verdict.INSUFFICIENT_EVIDENCE

    def test_a_floor_below_one_is_refused(self) -> None:
        """There is no legitimate zero floor here, unlike the longshot screen
        where it is a documented footgun for exploratory use."""
        with pytest.raises(ValueError, match="min_trades"):
            verdict(expectancy([Decimal(1), Decimal(2)]), min_trades=0)

    def test_the_verdict_is_a_string_enum(self) -> None:
        """It goes into JSON for the dashboard; it must serialise as its
        value, not as 'Verdict.EDGE_SHOWN'."""
        assert Verdict.EDGE_SHOWN == "edge_shown"
        assert f"{Verdict.NO_EDGE_SHOWN}" == "no_edge_shown"


class TestDescribe:
    def test_it_names_the_mean_the_count_and_the_interval(self) -> None:
        exp = Expectancy(
            n=24,
            total_cents=Decimal("74.4"),
            mean_cents=Decimal("3.1"),
            stderr_cents=Decimal("5.7"),
            ci_low_cents=Decimal("-8.2"),
            ci_high_cents=Decimal("14.4"),
            method="bootstrap",
        )
        sentence = describe(Verdict.NO_EDGE_SHOWN, exp)
        assert sentence == (
            "+3.10c/trade over 24 trades, but the 95% interval "
            "[-8.20c, +14.40c] includes zero — no edge shown."
        )

    def test_a_non_default_confidence_is_named_honestly(self) -> None:
        """Hardcoding '95%' would mislabel every report run at another level."""
        exp = expectancy(longshot(wins=4, losses=26), confidence=0.99)
        assert "99% interval" in describe(Verdict.NO_EDGE_SHOWN, exp)

    def test_an_edge_reads_as_an_edge(self) -> None:
        exp = expectancy(longshot(wins=10, losses=35))
        sentence = describe(Verdict.EDGE_SHOWN, exp)
        assert "entirely above zero" in sentence
        assert sentence.endswith("edge shown.")

    def test_a_losing_detector_is_not_described_as_noise(self) -> None:
        exp = expectancy(longshot(wins=1, losses=44))
        assert "losing, not noise" in describe(Verdict.LOSING, exp)

    def test_insufficient_evidence_never_quotes_an_interval(self) -> None:
        """A four-trade run has an interval and it must not appear in a
        sentence, because the sentence is what gets read."""
        exp = expectancy([LONGSHOT_WIN] * 4)
        sentence = describe(Verdict.INSUFFICIENT_EVIDENCE, exp)
        assert "insufficient evidence" in sentence
        assert "not reportable" in sentence
        assert "interval" not in sentence

    def test_no_trades_says_so_plainly(self) -> None:
        assert describe(Verdict.INSUFFICIENT_EVIDENCE, expectancy([])) == (
            "No trades — nothing to evaluate."
        )

    def test_one_trade_is_singular(self) -> None:
        sentence = describe(Verdict.INSUFFICIENT_EVIDENCE, expectancy([LONGSHOT_WIN]))
        assert "1 trade is" in sentence

    @pytest.mark.parametrize(
        "decision",
        [Verdict.INSUFFICIENT_EVIDENCE, Verdict.NO_EDGE_SHOWN, Verdict.LOSING],
    )
    def test_nothing_but_an_edge_reads_as_an_endorsement(
        self, decision: Verdict
    ) -> None:
        """The one rule this function has."""
        exp = expectancy(longshot(wins=4, losses=26))
        sentence = describe(decision, exp)
        assert "entirely above zero" not in sentence
        assert not sentence.endswith("— edge shown.")

    def test_a_hand_built_expectancy_with_no_interval_does_not_format_none(
        self,
    ) -> None:
        """Unreachable through verdict(), but describe() must not crash or
        print 'None' if a caller assembles an Expectancy itself."""
        exp = Expectancy(
            n=50,
            total_cents=Decimal("50"),
            mean_cents=Decimal("1"),
            stderr_cents=None,
            ci_low_cents=None,
            ci_high_cents=None,
            method="none",
        )
        sentence = describe(Verdict.NO_EDGE_SHOWN, exp)
        assert "None" not in sentence
        assert "insufficient evidence" in sentence


# ---------------------------------------------------------------------------
# End to end: the shape a report card actually has
# ---------------------------------------------------------------------------


class TestReportCard:
    def test_a_plausible_detector_run_produces_a_coherent_card(self) -> None:
        """Nothing exotic — just the four numbers together, so a change that
        makes them disagree with each other shows up somewhere."""
        pnls = longshot(wins=6, losses=24)
        exp = expectancy(pnls)
        decision = verdict(exp, min_trades=20)

        assert exp.n == 30
        assert exp.total_cents == sum(pnls)
        assert exp.mean_cents == (exp.total_cents / 30).quantize(Decimal("0.0001"))
        assert exp.ci_low_cents is not None and exp.ci_high_cents is not None
        assert exp.ci_low_cents < exp.mean_cents < exp.ci_high_cents
        assert decision is Verdict.NO_EDGE_SHOWN
        assert describe(decision, exp).endswith("no edge shown.")

    def test_the_equity_curve_of_that_run_has_a_real_drawdown(self) -> None:
        """Losses first, then the wins: the curve bottoms out at 24 losses.

        Note the opening 0 — see the test below for why it has to be there.
        """
        pnls = [LONGSHOT_LOSS] * 24 + [LONGSHOT_WIN] * 6
        equity: list[Decimal] = [Decimal(0)]
        running = Decimal(0)
        for pnl in pnls:
            running += pnl
            equity.append(running)
        assert max_drawdown(equity) == LONGSHOT_LOSS * -24

    def test_omitting_the_opening_balance_hides_the_first_trade(self) -> None:
        """A curve built from cumulative P&L *after each trade* has no point
        for "before any trade", so the first trade's result becomes the
        starting peak and its loss is invisible. Off by one trade every time,
        and always in the flattering direction.
        """
        pnls = [LONGSHOT_LOSS] * 24 + [LONGSHOT_WIN] * 6
        running = Decimal(0)
        after_each: list[Decimal] = []
        for pnl in pnls:
            running += pnl
            after_each.append(running)

        assert max_drawdown(after_each) == LONGSHOT_LOSS * -23
        assert max_drawdown([Decimal(0), *after_each]) == LONGSHOT_LOSS * -24

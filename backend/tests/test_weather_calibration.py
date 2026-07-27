"""Tests for weather forecast-error calibration.

The module's failure mode is not an off-by-a-degree sigma, it is a sigma that
exists at all when it should not — a global default, a neighbouring bucket
borrowed to fill a gap, a 0.0 from a single sample. Each of those prices a
market with confidence nobody measured, so most of what follows pins down what
the module refuses to say and which direction the bias points.
"""

from __future__ import annotations

import math

import pytest

from app.weather.calibration import (
    DEFAULT_LEAD_EDGES,
    ErrorStats,
    ForecastError,
    calibrate,
    debias,
    lead_bucket,
    sigma_for,
)

BASE_F = 70.0


def errs(lead: float, values: list[float]) -> list[ForecastError]:
    """Forecast errors of exactly `values` degrees, all at one lead.

    Actual is pinned at `BASE_F` and the forecast is moved, so
    `error_f == value` by the module's forecast-minus-actual convention.
    """
    return [
        ForecastError(lead_hours=lead, forecast_f=BASE_F + v, actual_f=BASE_F)
        for v in values
    ]


def stat(
    bucket: int, *, samples: int, bias: float = 0.0, sigma: float = 2.0
) -> ErrorStats:
    return ErrorStats(
        lead_bucket_hours=bucket,
        samples=samples,
        bias_f=bias,
        sigma_f=sigma,
        mae_f=abs(bias),
    )


# ---------------------------------------------------------------------------
# The sign convention
# ---------------------------------------------------------------------------


class TestForecastError:
    def test_error_is_forecast_minus_actual(self) -> None:
        e = ForecastError(lead_hours=6.0, forecast_f=72.0, actual_f=70.0)
        assert e.error_f == pytest.approx(2.0)

    def test_a_warm_forecast_gives_a_positive_error(self) -> None:
        """The whole module hangs off this sign; `debias` subtracts it."""
        e = ForecastError(lead_hours=6.0, forecast_f=75.0, actual_f=70.0)
        assert e.error_f > 0

    def test_a_cold_forecast_gives_a_negative_error(self) -> None:
        e = ForecastError(lead_hours=6.0, forecast_f=65.0, actual_f=70.0)
        assert e.error_f < 0


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


class TestLeadBucket:
    @pytest.mark.parametrize(
        ("lead", "expected"),
        [(0.0, 6), (3.0, 6), (6.5, 12), (13.0, 24), (30.0, 48), (100.0, 168)],
    )
    def test_a_lead_lands_in_the_bucket_named_by_its_upper_edge(
        self, lead: float, expected: int
    ) -> None:
        assert lead_bucket(lead) == expected

    @pytest.mark.parametrize("edge", DEFAULT_LEAD_EDGES)
    def test_a_lead_exactly_on_an_edge_takes_the_tighter_bucket(
        self, edge: int
    ) -> None:
        """Buckets are half-open `(previous, edge]`, so 24.0 is the 24 bucket."""
        assert lead_bucket(float(edge)) == edge

    def test_a_zero_lead_is_a_real_forecast_and_is_bucketed(self) -> None:
        assert lead_bucket(0.0) == DEFAULT_LEAD_EDGES[0]

    def test_a_negative_lead_is_refused(self) -> None:
        """A 'forecast' made after the fact would flatter the nearest bucket."""
        assert lead_bucket(-1.0) is None

    def test_a_lead_past_the_last_edge_is_refused_not_clamped(self) -> None:
        """Squeezing 300h into the 168h bucket understates a bucket that prices."""
        assert lead_bucket(300.0) is None

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_lead_is_refused(self, bad: float) -> None:
        assert lead_bucket(bad) is None

    def test_empty_edges_are_refused(self) -> None:
        assert lead_bucket(5.0, edges=()) is None

    def test_non_ascending_edges_are_refused(self) -> None:
        """A transposed config must not bucket everything under one label."""
        assert lead_bucket(5.0, edges=(24, 12, 6)) is None

    def test_duplicate_edges_are_refused(self) -> None:
        assert lead_bucket(5.0, edges=(6, 6, 12)) is None

    @pytest.mark.parametrize("edges", [(0, 12), (-6, 12)])
    def test_a_non_positive_first_edge_is_refused(self, edges: tuple[int, ...]) -> None:
        assert lead_bucket(5.0, edges=edges) is None

    def test_custom_edges_are_honoured(self) -> None:
        assert lead_bucket(5.0, edges=(3, 9)) == 9


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class TestCalibrate:
    def test_empty_input_gives_no_buckets(self) -> None:
        assert calibrate([], min_samples=30) == []

    def test_buckets_come_back_ascending_by_lead(self) -> None:
        sample = errs(100.0, [1.0, 2.0]) + errs(3.0, [1.0, 2.0]) + errs(20.0, [1.0, 2.0])
        got = [s.lead_bucket_hours for s in calibrate(sample, min_samples=2)]
        assert got == [6, 24, 168]

    def test_input_order_does_not_change_the_statistics(self) -> None:
        forward = errs(3.0, [1.0, -2.0, 4.0, 0.5])
        shuffled = [forward[2], forward[0], forward[3], forward[1]]
        assert calibrate(forward, min_samples=2) == calibrate(shuffled, min_samples=2)

    def test_bias_is_the_mean_signed_error_not_the_mean_absolute_one(self) -> None:
        """Errors of +3 and -3 are a station with no bias, not a 3 degree one."""
        [s] = calibrate(errs(3.0, [3.0, -3.0]), min_samples=2)
        assert s.bias_f == pytest.approx(0.0)
        assert s.mae_f == pytest.approx(3.0)

    def test_a_warm_station_reports_a_positive_bias(self) -> None:
        [s] = calibrate(errs(3.0, [2.0, 2.0, 3.0, 1.0]), min_samples=2)
        assert s.bias_f == pytest.approx(2.0)

    def test_sigma_is_the_spread_about_the_measured_bias_not_about_zero(self) -> None:
        """Errors +1 and +3: bias 2, spread sqrt(2). RMSE about zero is sqrt(5).

        Folding the bias into sigma would report 2.236 instead of 1.414 —
        hiding a correctable error and widening the bucket towards a coin flip.
        """
        [s] = calibrate(errs(3.0, [1.0, 3.0]), min_samples=2)
        assert s.bias_f == pytest.approx(2.0)
        assert s.sigma_f == pytest.approx(math.sqrt(2.0))
        assert s.sigma_f != pytest.approx(math.sqrt(5.0))

    def test_sigma_uses_the_sample_denominator_not_the_population_one(self) -> None:
        """Same data: n-1 gives sqrt(2) ~ 1.414, n would give 1.0."""
        [s] = calibrate(errs(3.0, [1.0, 3.0]), min_samples=2)
        assert s.sigma_f == pytest.approx(math.sqrt(2.0))
        assert s.sigma_f != pytest.approx(1.0)

    def test_leads_in_different_buckets_are_never_pooled(self) -> None:
        sample = errs(3.0, [0.5, -0.5]) + errs(100.0, [8.0, -8.0])
        near, far = calibrate(sample, min_samples=2)
        assert near.sigma_f < far.sigma_f

    def test_leads_with_no_bucket_are_dropped_not_repaired(self) -> None:
        sample = errs(3.0, [1.0, 3.0]) + errs(-4.0, [0.0]) + errs(500.0, [0.0])
        [s] = calibrate(sample, min_samples=2)
        assert s.lead_bucket_hours == 6
        assert s.samples == 2

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_a_non_finite_observation_is_dropped_not_propagated(
        self, bad: float
    ) -> None:
        """One NaN would otherwise poison the bucket's mean and sigma to NaN."""
        sample = [
            *errs(3.0, [1.0, 3.0]),
            ForecastError(lead_hours=3.0, forecast_f=bad, actual_f=BASE_F),
            ForecastError(lead_hours=3.0, forecast_f=BASE_F, actual_f=bad),
            ForecastError(lead_hours=bad, forecast_f=BASE_F, actual_f=BASE_F),
        ]
        [s] = calibrate(sample, min_samples=2)
        assert s.samples == 2
        assert math.isfinite(s.sigma_f)
        assert s.sigma_f == pytest.approx(math.sqrt(2.0))


class TestCalibrateRefusals:
    def test_a_single_sample_bucket_has_no_sigma(self) -> None:
        """One observation has no spread; 0.0 would price the bucket at 0 or 1."""
        [s] = calibrate(errs(3.0, [2.0]), min_samples=1)
        assert s.samples == 1
        assert math.isnan(s.sigma_f)
        assert not s.usable

    def test_a_single_sample_bucket_still_reports_bias_and_mae(self) -> None:
        """Coverage diagnostics survive the refusal; only pricing inputs vanish."""
        [s] = calibrate(errs(3.0, [2.0]), min_samples=1)
        assert s.bias_f == pytest.approx(2.0)
        assert s.mae_f == pytest.approx(2.0)

    def test_a_bucket_below_min_samples_is_reported_but_has_no_sigma(self) -> None:
        """The row exists so the operator can see the coverage gap."""
        [s] = calibrate(errs(3.0, [1.0, 2.0, 3.0]), min_samples=30)
        assert s.samples == 3
        assert math.isnan(s.sigma_f)
        assert not s.usable

    def test_the_missing_sigma_is_nan_and_never_zero(self) -> None:
        """NaN breaks a price visibly; 0.0 reads as an enormous tradeable edge."""
        [s] = calibrate(errs(3.0, [1.0, 2.0]), min_samples=30)
        assert s.sigma_f != 0.0
        assert math.isnan(s.sigma_f)

    def test_a_bucket_clearing_the_floor_is_usable(self) -> None:
        [s] = calibrate(errs(3.0, [1.0, 2.0, 3.0]), min_samples=3)
        assert s.usable
        assert s.sigma_f == pytest.approx(1.0)

    def test_identical_errors_give_a_zero_sigma_that_is_not_usable(self) -> None:
        """On real data a perfectly flat error is a duplicated-row bug."""
        [s] = calibrate(errs(3.0, [2.0] * 5), min_samples=2)
        assert s.sigma_f == pytest.approx(0.0)
        assert not s.usable

    def test_min_samples_below_two_cannot_buy_a_one_sample_sigma(self) -> None:
        """A floor of 0 or 1 does not make a standard deviation exist."""
        for floor in (0, 1):
            [s] = calibrate(errs(3.0, [2.0]), min_samples=floor)
            assert math.isnan(s.sigma_f)


# ---------------------------------------------------------------------------
# Selecting a sigma
# ---------------------------------------------------------------------------


class TestSigmaFor:
    def test_it_returns_the_sigma_of_the_bucket_the_lead_falls_in(self) -> None:
        stats = calibrate(
            errs(3.0, [1.0, 3.0]) + errs(100.0, [10.0, 20.0]), min_samples=2
        )
        assert sigma_for(stats, 4.0, min_samples=2) == pytest.approx(math.sqrt(2.0))

    def test_the_sigma_steps_at_a_bucket_edge(self) -> None:
        """6.0h and 6.1h are adjacent leads and deliberately different sigmas.

        This is the whole point of bucketing: a global sigma would return the
        same number either side, and the same number at 100h.
        """
        stats = calibrate(
            errs(3.0, [1.0, 3.0]) + errs(10.0, [10.0, 20.0]), min_samples=2
        )
        near = sigma_for(stats, 6.0, min_samples=2)
        far = sigma_for(stats, 6.1, min_samples=2)
        assert near == pytest.approx(math.sqrt(2.0))
        assert far == pytest.approx(math.sqrt(50.0))


class TestSigmaForRefusals:
    def test_a_bucket_below_min_samples_refuses(self) -> None:
        """The gate. The caller's answer to `None` is to not price the market."""
        stats = calibrate(errs(3.0, [1.0, 2.0, 3.0]), min_samples=2)
        assert stats[0].usable
        assert sigma_for(stats, 4.0, min_samples=30) is None

    def test_a_missing_bucket_does_not_borrow_a_neighbour(self) -> None:
        """The 48h sigma is not an approximation of the 24h one."""
        stats = calibrate(
            errs(3.0, [1.0, 3.0]) + errs(30.0, [5.0, 9.0]), min_samples=2
        )
        assert {s.lead_bucket_hours for s in stats} == {6, 48}
        assert sigma_for(stats, 20.0, min_samples=2) is None

    def test_an_empty_calibration_has_no_default_sigma(self) -> None:
        assert sigma_for([], 4.0, min_samples=2) is None

    def test_a_negative_lead_refuses(self) -> None:
        stats = calibrate(errs(3.0, [1.0, 3.0]), min_samples=2)
        assert sigma_for(stats, -1.0, min_samples=2) is None

    def test_a_lead_past_the_last_edge_refuses(self) -> None:
        stats = calibrate(errs(3.0, [1.0, 3.0]), min_samples=2)
        assert sigma_for(stats, 400.0, min_samples=2) is None

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_a_non_finite_lead_refuses(self, bad: float) -> None:
        stats = calibrate(errs(3.0, [1.0, 3.0]), min_samples=2)
        assert sigma_for(stats, bad, min_samples=2) is None

    def test_a_nan_sigma_refuses(self) -> None:
        stats = [stat(6, samples=99, sigma=math.nan)]
        assert sigma_for(stats, 4.0, min_samples=2) is None

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_a_non_positive_sigma_refuses(self, bad: float) -> None:
        """A zero sigma would price every bucket at exactly 0 or 1."""
        assert sigma_for([stat(6, samples=99, sigma=bad)], 4.0, min_samples=2) is None

    def test_two_entries_for_one_bucket_refuse_rather_than_pick(self) -> None:
        """Duplicate labels mean two stations were concatenated."""
        stats = [stat(6, samples=99, sigma=1.0), stat(6, samples=99, sigma=9.0)]
        assert sigma_for(stats, 4.0, min_samples=2) is None

    def test_mismatched_edges_refuse_rather_than_return_a_wrong_bucket(self) -> None:
        """Calibrated on custom edges, queried on the defaults: labels disagree."""
        stats = calibrate(errs(2.0, [1.0, 3.0]), min_samples=2, edges=(3, 9))
        assert stats[0].lead_bucket_hours == 3
        assert sigma_for(stats, 2.0, min_samples=2) is None
        assert sigma_for(stats, 2.0, min_samples=2, edges=(3, 9)) == pytest.approx(
            math.sqrt(2.0)
        )


# ---------------------------------------------------------------------------
# Bias correction
# ---------------------------------------------------------------------------


class TestDebias:
    def test_a_warm_station_is_corrected_downwards(self) -> None:
        """bias_f is forecast minus actual, so the correction subtracts."""
        [s] = calibrate(errs(3.0, [2.0, 2.0]), min_samples=2)
        assert debias(80.0, s) == pytest.approx(78.0)

    def test_a_cold_station_is_corrected_upwards(self) -> None:
        [s] = calibrate(errs(3.0, [-2.0, -2.0]), min_samples=2)
        assert debias(80.0, s) == pytest.approx(82.0)

    def test_an_unbiased_station_is_left_alone(self) -> None:
        [s] = calibrate(errs(3.0, [3.0, -3.0]), min_samples=2)
        assert debias(80.0, s) == pytest.approx(80.0)

    def test_correcting_backwards_would_double_the_error(self) -> None:
        """Guards the sign: adding the bias lands 2x as far off as the raw value."""
        [s] = calibrate(errs(3.0, [2.0, 2.0]), min_samples=2)
        raw, actual = 80.0, 78.0
        assert abs(debias(raw, s) - actual) == pytest.approx(0.0)
        assert abs((raw + s.bias_f) - actual) == pytest.approx(2 * abs(raw - actual))

    def test_debiased_forecasts_have_no_bias_left(self) -> None:
        """Re-calibrating the corrected history gives a mean error of zero."""
        history = errs(3.0, [1.0, 2.0, 3.0, 6.0])
        [s] = calibrate(history, min_samples=2)
        corrected = [
            ForecastError(
                lead_hours=e.lead_hours,
                forecast_f=debias(e.forecast_f, s),
                actual_f=e.actual_f,
            )
            for e in history
        ]
        [after] = calibrate(corrected, min_samples=2)
        assert after.bias_f == pytest.approx(0.0)
        # Removing the bias does not change the spread — that is the point of
        # measuring them separately.
        assert after.sigma_f == pytest.approx(s.sigma_f)

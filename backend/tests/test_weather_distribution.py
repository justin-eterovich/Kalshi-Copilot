"""Tests for the integer-degree temperature distribution.

The bug this module exists to prevent is quiet: a continuous normal prices a
two-degree bucket at about half its true probability, on every bucket, with no
error anywhere. So the contrast between the discrete and continuous readings is
pinned explicitly below, and the refusals — bad sigma, inverted buckets,
buckets containing no integer, `custom`, unrecognised types, missing
boundaries, a degenerate clamp — are tested harder than the happy path.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.weather.distribution import (
    MAX_SPAN,
    degree_pmf,
    fair_price,
    norm_cdf,
    probability_between,
    probability_greater,
    probability_less,
)

FORECAST = 96.5
SIGMA = 3.0


def continuous_interval(low: float, high: float, forecast: float, sigma: float) -> float:
    """The naive reading: the continuous mass of the literal interval."""
    return norm_cdf((high - forecast) / sigma) - norm_cdf((low - forecast) / sigma)


# ---------------------------------------------------------------------------
# Normal CDF
# ---------------------------------------------------------------------------


class TestNormCdf:
    def test_zero_is_one_half(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)

    def test_is_symmetric_about_zero(self) -> None:
        for x in (0.5, 1.0, 1.96, 3.0):
            assert norm_cdf(-x) == pytest.approx(1.0 - norm_cdf(x), abs=1e-12)

    def test_matches_known_quantiles(self) -> None:
        assert norm_cdf(1.0) == pytest.approx(0.8413447, abs=1e-6)
        assert norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)
        assert norm_cdf(-2.326348) == pytest.approx(0.01, abs=1e-6)


# ---------------------------------------------------------------------------
# The pmf over integer degrees
# ---------------------------------------------------------------------------


class TestDegreePmf:
    def test_each_integer_takes_its_half_degree_band(self) -> None:
        """Integer k owns [k - 0.5, k + 0.5) — not [k, k + 1)."""
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=90, high=102)
        assert pmf is not None
        expected = continuous_interval(95.5, 96.5, FORECAST, SIGMA)
        assert pmf[96] == pytest.approx(expected, abs=1e-12)

    def test_the_band_is_not_the_interval_above_the_integer(self) -> None:
        """[k - 0.5, k + 0.5) and [k, k + 1) are different numbers, not noise."""
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=90, high=102)
        assert pmf is not None
        shifted = continuous_interval(96.0, 97.0, FORECAST, SIGMA)
        assert pmf[96] != pytest.approx(shifted, abs=1e-4)

    def test_bands_tile_without_overlap(self) -> None:
        """Summing a contiguous run equals the closed-form span."""
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=94, high=99)
        assert pmf is not None
        total = math.fsum(pmf.values())
        assert total == pytest.approx(
            continuous_interval(93.5, 99.5, FORECAST, SIGMA), abs=1e-12
        )

    def test_covers_the_inclusive_range(self) -> None:
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=94, high=99)
        assert pmf is not None
        assert sorted(pmf) == [94, 95, 96, 97, 98, 99]

    def test_mass_is_symmetric_about_a_half_degree_forecast(self) -> None:
        """f = 96.5 sits on a band edge, so 96 and 97 are mirror images."""
        pmf = degree_pmf(forecast=96.5, sigma=SIGMA, low=90, high=103)
        assert pmf is not None
        assert pmf[96] == pytest.approx(pmf[97], abs=1e-12)
        assert pmf[95] == pytest.approx(pmf[98], abs=1e-12)

    def test_the_modal_degree_is_the_forecast(self) -> None:
        pmf = degree_pmf(forecast=97.0, sigma=SIGMA, low=88, high=106)
        assert pmf is not None
        assert max(pmf, key=lambda k: pmf[k]) == 97

    def test_masses_do_not_sum_to_one_over_a_narrow_range(self) -> None:
        """Documented and deliberate: the tails outside the range are absent."""
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=96, high=97)
        assert pmf is not None
        assert math.fsum(pmf.values()) < 0.30

    def test_a_wide_range_approaches_one(self) -> None:
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=46, high=146)
        assert pmf is not None
        assert math.fsum(pmf.values()) == pytest.approx(1.0, abs=1e-9)

    def test_every_mass_is_non_negative(self) -> None:
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=-40, high=140)
        assert pmf is not None
        assert all(m >= 0.0 for m in pmf.values())


class TestDegreePmfRefusals:
    def test_refuses_zero_sigma(self) -> None:
        """A zero sigma claims the forecast is exact; what follows is a 0 or a 1."""
        assert degree_pmf(forecast=FORECAST, sigma=0.0, low=90, high=100) is None

    def test_refuses_negative_sigma(self) -> None:
        assert degree_pmf(forecast=FORECAST, sigma=-3.0, low=90, high=100) is None

    def test_refuses_non_finite_sigma(self) -> None:
        for bad in (float("nan"), float("inf")):
            assert degree_pmf(forecast=FORECAST, sigma=bad, low=90, high=100) is None

    def test_refuses_non_finite_forecast(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            assert degree_pmf(forecast=bad, sigma=SIGMA, low=90, high=100) is None

    def test_refuses_an_inverted_range(self) -> None:
        assert degree_pmf(forecast=FORECAST, sigma=SIGMA, low=100, high=90) is None

    def test_accepts_a_single_degree_range(self) -> None:
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=96, high=96)
        assert pmf is not None
        assert list(pmf) == [96]

    def test_refuses_a_span_that_cannot_be_degrees(self) -> None:
        """A range this wide means the caller passed something else entirely."""
        assert degree_pmf(forecast=FORECAST, sigma=SIGMA, low=0, high=MAX_SPAN) is None
        ok = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=0, high=MAX_SPAN - 1)
        assert ok is not None


# ---------------------------------------------------------------------------
# The bucket that the whole module is about
# ---------------------------------------------------------------------------


class TestProbabilityBetween:
    def test_two_integer_bucket_is_far_larger_than_the_continuous_interval(
        self,
    ) -> None:
        """The bug this module exists to prevent, pinned as a number.

        "between 96-97" resolves YES on {96, 97}: two integers, covering
        [95.5, 97.5). Pricing it as the one-degree interval [96, 97] halves it,
        identically on every bucket in the book.
        """
        discrete = probability_between(
            forecast=FORECAST, sigma=SIGMA, floor_strike=96, cap_strike=97
        )
        naive = continuous_interval(96.0, 97.0, FORECAST, SIGMA)
        assert discrete is not None
        assert discrete == pytest.approx(0.2611, abs=1e-4)
        assert naive == pytest.approx(0.1324, abs=1e-4)
        # Not a rounding difference: nearly double, i.e. ~13 cents of fair
        # value on a contract that trades between 0 and 100.
        assert discrete > 1.9 * naive

    def test_matches_the_span_of_the_bands_it_contains(self) -> None:
        p = probability_between(
            forecast=FORECAST, sigma=SIGMA, floor_strike=96, cap_strike=97
        )
        assert p == pytest.approx(
            continuous_interval(95.5, 97.5, FORECAST, SIGMA), abs=1e-12
        )

    def test_equals_the_sum_of_the_pmf_over_the_bucket(self) -> None:
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=94, high=95)
        p = probability_between(
            forecast=FORECAST, sigma=SIGMA, floor_strike=94, cap_strike=95
        )
        assert pmf is not None and p is not None
        assert p == pytest.approx(math.fsum(pmf.values()), abs=1e-12)

    def test_adjacent_buckets_tile_the_board(self) -> None:
        """{94,95}, {96,97}, {98,99} must not overlap and must not leave gaps."""
        parts = [
            probability_between(
                forecast=FORECAST, sigma=SIGMA, floor_strike=lo, cap_strike=lo + 1
            )
            for lo in (94, 96, 98)
        ]
        assert all(p is not None for p in parts)
        total = math.fsum(p for p in parts if p is not None)
        assert total == pytest.approx(
            continuous_interval(93.5, 99.5, FORECAST, SIGMA), abs=1e-12
        )

    def test_a_single_degree_bucket_is_one_band(self) -> None:
        p = probability_between(
            forecast=FORECAST, sigma=SIGMA, floor_strike=96, cap_strike=96
        )
        assert p == pytest.approx(
            continuous_interval(95.5, 96.5, FORECAST, SIGMA), abs=1e-12
        )

    def test_a_whole_board_of_buckets_sums_to_about_one(self) -> None:
        parts = [
            probability_between(
                forecast=FORECAST, sigma=SIGMA, floor_strike=lo, cap_strike=lo + 1
            )
            for lo in range(60, 134, 2)
        ]
        total = math.fsum(p for p in parts if p is not None)
        assert total == pytest.approx(1.0, abs=1e-6)


class TestProbabilityBetweenRefusals:
    def test_refuses_an_inverted_bucket(self) -> None:
        """Not a clamped 0.0 — a confident zero reads as free money on NO."""
        assert (
            probability_between(
                forecast=FORECAST, sigma=SIGMA, floor_strike=97, cap_strike=96
            )
            is None
        )

    def test_refuses_a_bad_sigma(self) -> None:
        for bad in (0.0, -1.0, float("nan")):
            assert (
                probability_between(
                    forecast=FORECAST, sigma=bad, floor_strike=96, cap_strike=97
                )
                is None
            )

    def test_refuses_a_non_finite_forecast(self) -> None:
        assert (
            probability_between(
                forecast=float("nan"), sigma=SIGMA, floor_strike=96, cap_strike=97
            )
            is None
        )


# ---------------------------------------------------------------------------
# Open tails
# ---------------------------------------------------------------------------


class TestProbabilityGreater:
    def test_greater_than_96_means_at_least_97(self) -> None:
        p = probability_greater(forecast=FORECAST, sigma=SIGMA, strike=96.0)
        assert p == pytest.approx(1.0 - norm_cdf((96.5 - FORECAST) / SIGMA), abs=1e-12)

    def test_the_continuous_reading_overstates_the_tail(self) -> None:
        """`T > 96.0` sweeps in half of 96's band, which 97-and-up does not own.

        Six cents of fair value on a contract quoted 0-100, in the same
        direction on every strike in the board.
        """
        discrete = probability_greater(forecast=FORECAST, sigma=SIGMA, strike=96.0)
        naive = 1.0 - norm_cdf((96.0 - FORECAST) / SIGMA)
        assert discrete is not None
        assert discrete < naive
        assert naive - discrete == pytest.approx(
            continuous_interval(96.0, 96.5, FORECAST, SIGMA), abs=1e-12
        )
        assert naive - discrete > 0.06

    def test_inclusive_and_strict_differ_by_a_whole_degree(self) -> None:
        """Unlike the continuous model in btc/vol.py, these are not the same."""
        strict = probability_greater(forecast=FORECAST, sigma=SIGMA, strike=96.0)
        inclusive = probability_greater(
            forecast=FORECAST, sigma=SIGMA, strike=96.0, inclusive=True
        )
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=96, high=96)
        assert strict is not None and inclusive is not None and pmf is not None
        assert inclusive - strict == pytest.approx(pmf[96], abs=1e-12)

    def test_a_fractional_strike_rounds_up_to_the_next_integer(self) -> None:
        """Kalshi encodes "83 or above" as `above 82.99`; both readings give 83."""
        strict = probability_greater(forecast=82.0, sigma=SIGMA, strike=82.99)
        inclusive = probability_greater(
            forecast=82.0, sigma=SIGMA, strike=82.99, inclusive=True
        )
        at_83 = probability_greater(forecast=82.0, sigma=SIGMA, strike=82.0)
        assert strict == inclusive
        assert strict == at_83

    def test_is_monotone_decreasing_in_the_strike(self) -> None:
        values = [
            probability_greater(forecast=FORECAST, sigma=SIGMA, strike=float(k))
            for k in range(90, 104)
        ]
        assert all(v is not None for v in values)
        assert values == sorted(values, reverse=True)


class TestProbabilityLess:
    def test_less_than_89_means_at_most_88(self) -> None:
        p = probability_less(forecast=FORECAST, sigma=SIGMA, strike=89.0)
        assert p == pytest.approx(norm_cdf((88.5 - FORECAST) / SIGMA), abs=1e-12)

    def test_inclusive_and_strict_differ_by_a_whole_degree(self) -> None:
        strict = probability_less(forecast=FORECAST, sigma=SIGMA, strike=89.0)
        inclusive = probability_less(
            forecast=FORECAST, sigma=SIGMA, strike=89.0, inclusive=True
        )
        pmf = degree_pmf(forecast=FORECAST, sigma=SIGMA, low=89, high=89)
        assert strict is not None and inclusive is not None and pmf is not None
        assert inclusive - strict == pytest.approx(pmf[89], abs=1e-12)

    def test_a_fractional_strike_rounds_down_to_the_previous_integer(self) -> None:
        """A market reading "below 89.01" must still pay out on 89."""
        p = probability_less(forecast=FORECAST, sigma=SIGMA, strike=89.01)
        assert p == pytest.approx(norm_cdf((89.5 - FORECAST) / SIGMA), abs=1e-12)


class TestTailsPartitionTheLine:
    def test_greater_and_less_are_exact_complements_on_the_grid(self) -> None:
        """T >= 97 and T <= 96 partition the integers with nothing between."""
        above = probability_greater(forecast=FORECAST, sigma=SIGMA, strike=96.0)
        below = probability_less(
            forecast=FORECAST, sigma=SIGMA, strike=96.0, inclusive=True
        )
        assert above is not None and below is not None
        assert above + below == pytest.approx(1.0, abs=1e-12)

    def test_a_board_of_two_tails_and_the_buckets_between_sums_to_one(self) -> None:
        low_tail = probability_less(forecast=FORECAST, sigma=SIGMA, strike=90.0)
        high_tail = probability_greater(forecast=FORECAST, sigma=SIGMA, strike=99.0)
        middle = [
            probability_between(
                forecast=FORECAST, sigma=SIGMA, floor_strike=lo, cap_strike=lo + 1
            )
            for lo in (90, 92, 94, 96, 98)
        ]
        parts = [low_tail, high_tail, *middle]
        assert all(p is not None for p in parts)
        assert math.fsum(p for p in parts if p is not None) == pytest.approx(
            1.0, abs=1e-12
        )


class TestTailRefusals:
    def test_greater_refuses_a_bad_sigma(self) -> None:
        for bad in (0.0, -2.0, float("inf"), float("nan")):
            assert probability_greater(forecast=FORECAST, sigma=bad, strike=96.0) is None

    def test_less_refuses_a_bad_sigma(self) -> None:
        for bad in (0.0, -2.0, float("nan")):
            assert probability_less(forecast=FORECAST, sigma=bad, strike=96.0) is None

    def test_both_refuse_a_non_finite_strike(self) -> None:
        assert (
            probability_greater(forecast=FORECAST, sigma=SIGMA, strike=float("nan"))
            is None
        )
        assert (
            probability_less(forecast=FORECAST, sigma=SIGMA, strike=float("inf"))
            is None
        )

    def test_both_refuse_a_non_finite_forecast(self) -> None:
        assert (
            probability_greater(forecast=float("inf"), sigma=SIGMA, strike=96.0) is None
        )
        assert probability_less(forecast=float("nan"), sigma=SIGMA, strike=96.0) is None


# ---------------------------------------------------------------------------
# fair_price
# ---------------------------------------------------------------------------


class TestFairPrice:
    def test_prices_a_between_bucket_on_the_integers_it_contains(self) -> None:
        price = fair_price(
            strike_type="between",
            floor_strike=Decimal(96),
            cap_strike=Decimal(97),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert price == Decimal("0.2611")

    def test_returns_decimal_not_float(self) -> None:
        price = fair_price(
            strike_type="between",
            floor_strike=Decimal(96),
            cap_strike=Decimal(97),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert isinstance(price, Decimal)

    def test_quantizes_to_four_decimals(self) -> None:
        price = fair_price(
            strike_type="greater",
            floor_strike=Decimal(96),
            cap_strike=None,
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert price is not None
        assert price.as_tuple().exponent == -4

    def test_greater_and_greater_or_equal_are_not_the_same_price(self) -> None:
        """The contrast with btc/vol.py, where the strike carries no mass."""
        strict = fair_price(
            strike_type="greater",
            floor_strike=Decimal(96),
            cap_strike=None,
            forecast=FORECAST,
            sigma=SIGMA,
        )
        inclusive = fair_price(
            strike_type="greater_or_equal",
            floor_strike=Decimal(96),
            cap_strike=None,
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert strict is not None and inclusive is not None
        assert inclusive - strict > Decimal("0.10")

    def test_less_reads_its_boundary_from_cap_strike(self) -> None:
        price = fair_price(
            strike_type="less",
            floor_strike=None,
            cap_strike=Decimal(95),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        expected = probability_less(forecast=FORECAST, sigma=SIGMA, strike=95.0)
        assert price is not None and expected is not None
        assert float(price) == pytest.approx(expected, abs=1e-4)

    def test_less_falls_back_to_floor_strike(self) -> None:
        """Matches resolve_strike, which falls back the same way."""
        with_floor = fair_price(
            strike_type="less",
            floor_strike=Decimal(95),
            cap_strike=None,
            forecast=FORECAST,
            sigma=SIGMA,
        )
        with_cap = fair_price(
            strike_type="less",
            floor_strike=None,
            cap_strike=Decimal(95),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert with_floor is not None
        assert with_floor == with_cap

    def test_a_fractional_between_prices_only_the_integers_inside_it(self) -> None:
        loose = fair_price(
            strike_type="between",
            floor_strike=Decimal("95.6"),
            cap_strike=Decimal("97.4"),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        tight = fair_price(
            strike_type="between",
            floor_strike=Decimal(96),
            cap_strike=Decimal(97),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert loose == tight

    def test_is_case_and_whitespace_insensitive(self) -> None:
        price = fair_price(
            strike_type="  BETWEEN ",
            floor_strike=Decimal(96),
            cap_strike=Decimal(97),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert price == Decimal("0.2611")


class TestFairPriceNeverAssertsCertainty:
    def test_a_hopeless_bucket_floors_above_zero(self) -> None:
        price = fair_price(
            strike_type="between",
            floor_strike=Decimal(30),
            cap_strike=Decimal(31),
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert price == Decimal("0.0100")

    def test_a_certain_tail_caps_below_one(self) -> None:
        price = fair_price(
            strike_type="greater",
            floor_strike=Decimal(30),
            cap_strike=None,
            forecast=FORECAST,
            sigma=SIGMA,
        )
        assert price == Decimal("0.9900")

    def test_the_clamp_is_symmetric(self) -> None:
        high = fair_price(
            strike_type="greater",
            floor_strike=Decimal(30),
            cap_strike=None,
            forecast=FORECAST,
            sigma=SIGMA,
            max_fair=Decimal("0.95"),
        )
        low = fair_price(
            strike_type="less",
            floor_strike=None,
            cap_strike=Decimal(30),
            forecast=FORECAST,
            sigma=SIGMA,
            max_fair=Decimal("0.95"),
        )
        assert high == Decimal("0.9500")
        assert low == Decimal("0.0500")

    def test_no_price_anywhere_on_a_board_is_zero_or_one(self) -> None:
        for lo in range(40, 160, 2):
            price = fair_price(
                strike_type="between",
                floor_strike=Decimal(lo),
                cap_strike=Decimal(lo + 1),
                forecast=FORECAST,
                sigma=SIGMA,
            )
            assert price is not None
            assert Decimal("0.0100") <= price <= Decimal("0.9900")


class TestFairPriceRefusals:
    def test_refuses_custom(self) -> None:
        """`custom` carries its rules in prose. There is nothing safe to compute."""
        assert (
            fair_price(
                strike_type="custom",
                floor_strike=Decimal(96),
                cap_strike=Decimal(97),
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_an_unrecognised_strike_type(self) -> None:
        for kind in ("structured", "functional", "between_or_equal", "GREATER_THAN"):
            assert (
                fair_price(
                    strike_type=kind,
                    floor_strike=Decimal(96),
                    cap_strike=Decimal(97),
                    forecast=FORECAST,
                    sigma=SIGMA,
                )
                is None
            )

    def test_refuses_a_missing_strike_type(self) -> None:
        for kind in (None, "", "   "):
            assert (
                fair_price(
                    strike_type=kind,
                    floor_strike=Decimal(96),
                    cap_strike=Decimal(97),
                    forecast=FORECAST,
                    sigma=SIGMA,
                )
                is None
            )

    def test_refuses_between_with_a_missing_boundary(self) -> None:
        assert (
            fair_price(
                strike_type="between",
                floor_strike=Decimal(96),
                cap_strike=None,
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )
        assert (
            fair_price(
                strike_type="between",
                floor_strike=None,
                cap_strike=Decimal(97),
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_greater_with_no_floor_strike(self) -> None:
        assert (
            fair_price(
                strike_type="greater",
                floor_strike=None,
                cap_strike=Decimal(97),
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_less_with_no_boundary_at_all(self) -> None:
        assert (
            fair_price(
                strike_type="less",
                floor_strike=None,
                cap_strike=None,
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_an_inverted_between(self) -> None:
        assert (
            fair_price(
                strike_type="between",
                floor_strike=Decimal(97),
                cap_strike=Decimal(96),
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_a_between_that_contains_no_integer(self) -> None:
        """96.2 to 96.8 is a range the report can never produce."""
        assert (
            fair_price(
                strike_type="between",
                floor_strike=Decimal("96.2"),
                cap_strike=Decimal("96.8"),
                forecast=FORECAST,
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_a_bad_sigma(self) -> None:
        for bad in (0.0, -3.0, float("nan"), float("inf")):
            assert (
                fair_price(
                    strike_type="between",
                    floor_strike=Decimal(96),
                    cap_strike=Decimal(97),
                    forecast=FORECAST,
                    sigma=bad,
                )
                is None
            )

    def test_refuses_a_non_finite_forecast(self) -> None:
        assert (
            fair_price(
                strike_type="between",
                floor_strike=Decimal(96),
                cap_strike=Decimal(97),
                forecast=float("nan"),
                sigma=SIGMA,
            )
            is None
        )

    def test_refuses_a_degenerate_clamp(self) -> None:
        """max_fair outside (0.5, 1) would quietly rewrite every price."""
        for bad in (Decimal("0.5"), Decimal("0.4"), Decimal(1), Decimal("1.5")):
            assert (
                fair_price(
                    strike_type="between",
                    floor_strike=Decimal(96),
                    cap_strike=Decimal(97),
                    forecast=FORECAST,
                    sigma=SIGMA,
                    max_fair=bad,
                )
                is None
            )

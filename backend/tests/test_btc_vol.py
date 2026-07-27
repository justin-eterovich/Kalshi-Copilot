"""Tests for the Bitcoin volatility model.

The failure this module exists to prevent is a confident number produced from
data that cannot support one, so the refusals are tested harder than the happy
path: short samples, bad decay factors, zero sigma, inverted brackets,
unrecognised strike types, and the clamp that keeps certainty off the tape.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.btc.vol import (
    bracket_probability,
    ewma_variance,
    ewma_volatility,
    fair_price,
    log_returns,
    norm_cdf,
    scale_volatility,
    terminal_probability,
)

SPOT = Decimal(60000)
LAM = 0.94


def noisy(n: int, size: float = 0.001) -> list[float]:
    """A deterministic alternating return series with a known scale."""
    return [size if i % 2 == 0 else -size for i in range(n)]


# ---------------------------------------------------------------------------
# Normal CDF
# ---------------------------------------------------------------------------


class TestNormCdf:
    def test_zero_is_one_half(self) -> None:
        assert norm_cdf(0.0) == pytest.approx(0.5)

    def test_is_symmetric_about_zero(self) -> None:
        for x in (0.25, 1.0, 1.96, 3.0):
            assert norm_cdf(-x) == pytest.approx(1.0 - norm_cdf(x), abs=1e-12)

    def test_matches_known_quantiles(self) -> None:
        assert norm_cdf(1.0) == pytest.approx(0.8413447, abs=1e-6)
        assert norm_cdf(1.959964) == pytest.approx(0.975, abs=1e-6)
        assert norm_cdf(-2.326348) == pytest.approx(0.01, abs=1e-6)

    def test_is_monotone(self) -> None:
        xs = [-4.0, -1.0, -0.1, 0.0, 0.1, 1.0, 4.0]
        values = [norm_cdf(x) for x in xs]
        assert values == sorted(values)

    def test_the_far_tail_saturates(self) -> None:
        """Documented, and harmless only because `fair_price` clamps."""
        assert norm_cdf(40.0) == 1.0
        assert norm_cdf(-40.0) == 0.0


# ---------------------------------------------------------------------------
# Log returns
# ---------------------------------------------------------------------------


class TestLogReturns:
    def test_computes_consecutive_log_returns(self) -> None:
        r = log_returns([Decimal(100), Decimal(110)])
        assert r == pytest.approx([math.log(1.1)])

    def test_returns_one_fewer_than_prices(self) -> None:
        assert len(log_returns([Decimal(i) for i in range(100, 110)])) == 9

    def test_a_flat_series_has_zero_returns(self) -> None:
        assert log_returns([Decimal(100)] * 5) == [0.0, 0.0, 0.0, 0.0]

    def test_a_single_price_yields_nothing(self) -> None:
        assert log_returns([Decimal(100)]) == []

    def test_an_empty_series_yields_nothing(self) -> None:
        assert log_returns([]) == []

    def test_a_zero_price_is_rejected(self) -> None:
        """A dropped field parsed as 0 is corrupt data, not a price."""
        with pytest.raises(ValueError, match="positive"):
            log_returns([Decimal(100), Decimal(0), Decimal(100)])

    def test_a_negative_price_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            log_returns([Decimal(-1), Decimal(100)])

    def test_the_error_names_the_index(self) -> None:
        with pytest.raises(ValueError, match="index 2"):
            log_returns([Decimal(100), Decimal(101), Decimal(0)])


# ---------------------------------------------------------------------------
# EWMA
# ---------------------------------------------------------------------------


class TestEwmaVariance:
    def test_a_short_sample_is_refused(self) -> None:
        """A short window does not give a noisy sigma, it gives an arbitrary
        one — and an arbitrary sigma prices a strike with total confidence."""
        assert ewma_variance(noisy(29), lam=LAM, min_samples=30) is None

    def test_exactly_min_samples_is_accepted(self) -> None:
        assert ewma_variance(noisy(30), lam=LAM, min_samples=30) is not None

    def test_an_empty_sample_is_refused(self) -> None:
        assert ewma_variance([], lam=LAM, min_samples=0) is None

    @pytest.mark.parametrize("lam", [0.0, 1.0, -0.1, 1.5, float("nan")])
    def test_a_decay_factor_outside_the_unit_interval_is_refused(
        self, lam: float
    ) -> None:
        assert ewma_variance(noisy(50), lam=lam, min_samples=30) is None

    def test_a_non_finite_return_is_refused(self) -> None:
        """A NaN comes out the far end as a NaN probability, which compares
        False against every threshold and silently disables the guards."""
        bad = noisy(50)
        bad[10] = float("nan")
        assert ewma_variance(bad, lam=LAM, min_samples=30) is None
        bad[10] = float("inf")
        assert ewma_variance(bad, lam=LAM, min_samples=30) is None

    def test_a_flat_series_has_zero_variance(self) -> None:
        assert ewma_variance([0.0] * 50, lam=LAM, min_samples=30) == 0.0

    def test_variance_matches_the_scale_of_the_returns(self) -> None:
        v = ewma_variance(noisy(200, size=0.002), lam=LAM, min_samples=30)
        assert v is not None
        assert v == pytest.approx(0.002**2, rel=1e-9)

    def test_recent_returns_dominate_old_ones(self) -> None:
        """The whole point of exponential weighting."""
        late = ewma_variance([0.0] * 40 + [0.05], lam=LAM, min_samples=30)
        early = ewma_variance([0.05] + [0.0] * 40, lam=LAM, min_samples=30)
        assert late is not None and early is not None
        assert late > early * 5

    def test_a_bigger_move_gives_a_bigger_variance(self) -> None:
        small = ewma_variance(noisy(100, size=0.001), lam=LAM, min_samples=30)
        large = ewma_variance(noisy(100, size=0.004), lam=LAM, min_samples=30)
        assert small is not None and large is not None
        assert large > small


class TestEwmaVolatility:
    def test_is_the_square_root_of_the_variance(self) -> None:
        returns = noisy(100, size=0.003)
        v = ewma_variance(returns, lam=LAM, min_samples=30)
        s = ewma_volatility(returns, lam=LAM, min_samples=30)
        assert v is not None and s is not None
        assert s == pytest.approx(math.sqrt(v))

    def test_a_refusal_propagates(self) -> None:
        assert ewma_volatility(noisy(5), lam=LAM, min_samples=30) is None
        assert ewma_volatility(noisy(50), lam=1.0, min_samples=30) is None


# ---------------------------------------------------------------------------
# Time scaling
# ---------------------------------------------------------------------------


class TestScaleVolatility:
    def test_scales_by_the_square_root_of_time(self) -> None:
        assert scale_volatility(0.01, 4.0) == pytest.approx(0.02)
        assert scale_volatility(0.001, 60.0) == pytest.approx(0.001 * math.sqrt(60))

    def test_a_single_period_is_unchanged(self) -> None:
        assert scale_volatility(0.017, 1.0) == pytest.approx(0.017)

    def test_zero_periods_gives_zero(self) -> None:
        """No time, no dispersion — and the consumer refuses on sigma <= 0."""
        assert scale_volatility(0.02, 0.0) == 0.0

    def test_a_negative_horizon_is_refused(self) -> None:
        assert scale_volatility(0.02, -1.0) is None

    def test_a_negative_sigma_is_refused(self) -> None:
        assert scale_volatility(-0.02, 4.0) is None

    def test_a_non_finite_input_is_refused(self) -> None:
        assert scale_volatility(float("nan"), 4.0) is None
        assert scale_volatility(0.02, float("inf")) is None


# ---------------------------------------------------------------------------
# Terminal probability
# ---------------------------------------------------------------------------


class TestTerminalProbability:
    def test_at_the_money_sits_just_below_a_coin_flip(self) -> None:
        """The -sigma^2/2 Ito term, not a bearish view: the median of a
        lognormal sits below its mean. People will read this as a bug."""
        p = terminal_probability(spot=SPOT, strike=SPOT, sigma=0.02, above=True)
        assert p is not None
        assert 0.49 < p < 0.5
        assert p == pytest.approx(norm_cdf(-0.01), abs=1e-12)

    def test_the_ito_drag_grows_with_the_horizon(self) -> None:
        near = terminal_probability(spot=SPOT, strike=SPOT, sigma=0.01, above=True)
        far = terminal_probability(spot=SPOT, strike=SPOT, sigma=0.20, above=True)
        assert near is not None and far is not None
        assert far < near < 0.5

    def test_above_and_below_are_complementary(self) -> None:
        up = terminal_probability(
            spot=SPOT, strike=Decimal(61000), sigma=0.03, above=True
        )
        down = terminal_probability(
            spot=SPOT, strike=Decimal(61000), sigma=0.03, above=False
        )
        assert up is not None and down is not None
        assert up + down == pytest.approx(1.0, abs=1e-12)

    def test_probability_rises_with_spot(self) -> None:
        prior = 0.0
        for spot in (Decimal(55000), Decimal(59000), Decimal(60000), Decimal(65000)):
            p = terminal_probability(
                spot=spot, strike=Decimal(60000), sigma=0.03, above=True
            )
            assert p is not None and p > prior
            prior = p

    def test_a_wider_sigma_pulls_a_far_strike_towards_a_coin_flip(self) -> None:
        tight = terminal_probability(
            spot=SPOT, strike=Decimal(61000), sigma=0.005, above=True
        )
        wide = terminal_probability(
            spot=SPOT, strike=Decimal(61000), sigma=0.10, above=True
        )
        assert tight is not None and wide is not None
        assert tight < wide < 0.5

    def test_a_zero_volatility_is_refused(self) -> None:
        """Zero sigma asserts the outcome is already determined, and the
        probability that follows is exactly 0 or 1."""
        assert terminal_probability(
            spot=SPOT, strike=Decimal(59000), sigma=0.0, above=True
        ) is None

    def test_a_negative_or_non_finite_volatility_is_refused(self) -> None:
        for sigma in (-0.01, float("nan"), float("inf")):
            assert terminal_probability(
                spot=SPOT, strike=Decimal(59000), sigma=sigma, above=True
            ) is None

    @pytest.mark.parametrize("spot", [Decimal(0), Decimal(-1)])
    def test_a_nonpositive_spot_is_refused(self, spot: Decimal) -> None:
        assert terminal_probability(
            spot=spot, strike=Decimal(59000), sigma=0.02, above=True
        ) is None

    @pytest.mark.parametrize("strike", [Decimal(0), Decimal(-1)])
    def test_a_nonpositive_strike_is_refused(self, strike: Decimal) -> None:
        assert terminal_probability(
            spot=SPOT, strike=strike, sigma=0.02, above=True
        ) is None


# ---------------------------------------------------------------------------
# Brackets
# ---------------------------------------------------------------------------


class TestBracketProbability:
    def test_a_band_around_spot_is_the_likeliest_place_to_land(self) -> None:
        p = bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(59000),
            cap_strike=Decimal(61000),
            sigma=0.02,
        )
        assert p is not None
        assert 0.5 < p < 0.7

    def test_a_band_is_the_difference_of_two_tails(self) -> None:
        above_floor = terminal_probability(
            spot=SPOT, strike=Decimal(59000), sigma=0.02, above=True
        )
        above_cap = terminal_probability(
            spot=SPOT, strike=Decimal(61000), sigma=0.02, above=True
        )
        p = bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(59000),
            cap_strike=Decimal(61000),
            sigma=0.02,
        )
        assert above_floor is not None and above_cap is not None and p is not None
        assert p == pytest.approx(above_floor - above_cap, abs=1e-12)

    def test_a_wider_band_is_likelier(self) -> None:
        narrow = bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(59900),
            cap_strike=Decimal(60100),
            sigma=0.02,
        )
        wide = bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(55000),
            cap_strike=Decimal(65000),
            sigma=0.02,
        )
        assert narrow is not None and wide is not None
        assert narrow < wide < 1.0

    def test_a_band_far_from_spot_is_near_zero(self) -> None:
        p = bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(90000),
            cap_strike=Decimal(95000),
            sigma=0.01,
        )
        assert p is not None
        assert p == pytest.approx(0.0, abs=1e-9)

    def test_a_degenerate_band_carries_no_mass(self) -> None:
        """floor == cap is not inverted, and a point has zero probability."""
        p = bracket_probability(
            spot=SPOT, floor_strike=SPOT, cap_strike=SPOT, sigma=0.02
        )
        assert p == 0.0

    def test_an_inverted_bracket_is_refused(self) -> None:
        """Two strikes read from the wrong fields. A confident 0.00 — free
        money on the NO side — is a much worse answer than nothing."""
        assert bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(61000),
            cap_strike=Decimal(59000),
            sigma=0.02,
        ) is None

    def test_bad_inputs_propagate_a_refusal(self) -> None:
        assert bracket_probability(
            spot=Decimal(0),
            floor_strike=Decimal(59000),
            cap_strike=Decimal(61000),
            sigma=0.02,
        ) is None
        assert bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(0),
            cap_strike=Decimal(61000),
            sigma=0.02,
        ) is None
        assert bracket_probability(
            spot=SPOT,
            floor_strike=Decimal(59000),
            cap_strike=Decimal(61000),
            sigma=0.0,
        ) is None


# ---------------------------------------------------------------------------
# Fair price
# ---------------------------------------------------------------------------


def fair(**overrides: object) -> Decimal | None:
    kwargs: dict = {
        "strike_type": "greater",
        "floor_strike": Decimal(60000),
        "cap_strike": None,
        "spot": SPOT,
        "sigma": 0.02,
    }
    kwargs.update(overrides)
    return fair_price(**kwargs)  # type: ignore[arg-type]


class TestFairPrice:
    def test_prices_a_greater_strike(self) -> None:
        p = fair(spot=Decimal(61000))
        assert p is not None
        assert Decimal("0.5") < p < Decimal("0.99")

    def test_greater_and_greater_or_equal_are_identical(self) -> None:
        """Under a continuous distribution P(S_T = K) = 0, so the strictness of
        the inequality cannot move the price. `resolve_strike` in
        `stale_quote.py` distinguishes them and is also right: it compares
        against one observed spot, where landing exactly on the strike is a
        real event. Neither should be "fixed" to match the other."""
        assert fair(strike_type="greater") == fair(strike_type="greater_or_equal")

    def test_less_and_less_or_equal_are_identical(self) -> None:
        kw = {"floor_strike": None, "cap_strike": Decimal(60000)}
        assert fair(strike_type="less", **kw) == fair(strike_type="less_or_equal", **kw)

    def test_greater_and_less_at_one_strike_sum_to_a_dollar(self) -> None:
        up = fair(strike_type="greater", floor_strike=Decimal(60500))
        down = fair(
            strike_type="less", floor_strike=None, cap_strike=Decimal(60500)
        )
        assert up is not None and down is not None
        assert abs(up + down - Decimal(1)) <= Decimal("0.0002")

    def test_prices_a_between_market(self) -> None:
        p = fair(
            strike_type="between",
            floor_strike=Decimal(59000),
            cap_strike=Decimal(61000),
        )
        assert p is not None
        assert Decimal("0.5") < p < Decimal("0.7")

    def test_the_strike_type_is_normalised(self) -> None:
        assert fair(strike_type="  GREATER  ") == fair(strike_type="greater")

    def test_the_result_is_quantized_to_four_decimals(self) -> None:
        p = fair(spot=Decimal("60123.45"))
        assert p is not None
        assert p.as_tuple().exponent == -4

    # -- refusals -----------------------------------------------------------

    def test_custom_strikes_are_refused(self) -> None:
        """The rules live in prose. There is nothing safe to compute."""
        assert fair(strike_type="custom") is None

    def test_an_unknown_strike_type_is_refused(self) -> None:
        """Not an invitation to assume `greater`."""
        assert fair(strike_type="somethingnew") is None
        assert fair(strike_type="") is None
        assert fair(strike_type=None) is None

    def test_a_missing_floor_is_refused_for_greater(self) -> None:
        assert fair(strike_type="greater", floor_strike=None) is None

    def test_less_falls_back_to_the_floor_strike(self) -> None:
        """Matching `resolve_strike`: the two must agree about which strike a
        one-sided market means, or they will disagree on the same market."""
        p = fair(strike_type="less", floor_strike=Decimal(60000), cap_strike=None)
        assert p is not None

    def test_less_with_no_boundary_at_all_is_refused(self) -> None:
        assert fair(strike_type="less", floor_strike=None, cap_strike=None) is None

    def test_between_needs_both_boundaries(self) -> None:
        assert fair(
            strike_type="between", floor_strike=Decimal(59000), cap_strike=None
        ) is None
        assert fair(
            strike_type="between", floor_strike=None, cap_strike=Decimal(61000)
        ) is None

    def test_an_inverted_between_is_refused(self) -> None:
        assert fair(
            strike_type="between",
            floor_strike=Decimal(61000),
            cap_strike=Decimal(59000),
        ) is None

    def test_a_zero_volatility_is_refused(self) -> None:
        assert fair(sigma=0.0) is None

    @pytest.mark.parametrize("spot", [Decimal(0), Decimal(-1)])
    def test_a_nonpositive_spot_is_refused(self, spot: Decimal) -> None:
        assert fair(spot=spot) is None

    @pytest.mark.parametrize("bad", [Decimal(1), Decimal("1.5"), Decimal("0.5"),
                                     Decimal("0.4"), Decimal(0)])
    def test_a_degenerate_max_fair_is_refused(self, bad: Decimal) -> None:
        """Outside (0.5, 1) the clamp is degenerate or inverted, and would
        quietly rewrite every price it touches."""
        assert fair(max_fair=bad) is None

    # -- the clamp ----------------------------------------------------------

    def test_a_near_certain_yes_is_capped_below_a_dollar(self) -> None:
        """The model knows a sigma from a window that had no reason to contain
        the move that matters. It does not know the last cent is free."""
        p = fair(spot=Decimal(120000), floor_strike=Decimal(60000), sigma=0.02)
        assert p == Decimal("0.9900")

    def test_a_near_certain_no_is_floored_above_zero(self) -> None:
        """The clamp is symmetric — otherwise the NO side gets the certainty
        the YES side is denied."""
        p = fair(spot=Decimal(30000), floor_strike=Decimal(60000), sigma=0.02)
        assert p == Decimal("0.0100")

    def test_the_clamp_bounds_are_symmetric_around_a_half(self) -> None:
        hi = fair(spot=Decimal(120000), max_fair=Decimal("0.95"))
        lo = fair(spot=Decimal(30000), max_fair=Decimal("0.95"))
        assert hi == Decimal("0.9500")
        assert lo == Decimal("0.0500")
        assert hi is not None and lo is not None
        assert hi + lo == Decimal(1)

    def test_no_price_is_ever_certain(self) -> None:
        for spot in (1, 100, 30000, 59999, 60000, 60001, 90000, 10**9):
            for kind in ("greater", "less", "between"):
                p = fair(
                    strike_type=kind,
                    spot=Decimal(spot),
                    floor_strike=Decimal(59000),
                    cap_strike=Decimal(61000),
                )
                if p is None:
                    continue
                assert Decimal("0.01") <= p <= Decimal("0.99")

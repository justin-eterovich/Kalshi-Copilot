"""Volatility model for Bitcoin strike markets: a probability, not a margin.

`stale_quote.py` decides whether a strike is "decisively" cleared with a fixed
percentage from config. That was always a placeholder: 1% past the strike is a
near-certainty with two minutes left and a coin flip with six hours left, and a
fixed margin cannot tell those apart. This module replaces the margin with
`P(S_T past K)` under a lognormal terminal distribution whose sigma comes from
realised returns.

**The dangerous version of this module is the one that always answers.** It
seeds an EWMA off four ticks, scales a one-minute sigma to a six-hour horizon
without saying so, fits a drift term to the last hour of tape, and hands back
0.9997 for a strike 3% away. Every one of those numbers *looks* like a
probability, prices like a probability, and is really an artifact of the
estimator. Downstream nothing can tell the difference: the EV arithmetic, the
fee math, and the proposal all render exactly the same whether the 0.9997 came
from data or from a seeding accident. So the decisions here are:

- **Zero drift, always.** Over the minutes-to-hours horizons these markets
  trade on, any drift estimate is an order of magnitude smaller than the
  standard error around it — you cannot measure a daily drift from an hour of
  ticks. Worse, drift is a free parameter, and a free parameter is how a model
  talks itself into a directional view it has no evidence for. The martingale
  assumption is not laziness; it is the only unbiased choice available and it
  is what the -sigma^2/2 term in `d2` encodes.
- **Refusals return `None`.** Too few returns, a lam outside (0,1), a
  non-positive spot or strike, a non-positive or non-finite sigma, an inverted
  bracket, an unrecognised strike type. None of these get a default.
- **No probability of 0 or 1 ever leaves `fair_price`.** It clamps into
  `[1 - max_fair, max_fair]`, symmetrically, mirroring `decisive_fair_price`.

**Known limitation, stated rather than hidden:** square-root-of-time scaling
assumes i.i.d. returns, and Bitcoin plainly is not — volatility clusters. A
sigma estimated over a calm window understates the risk of a violent one, and
one estimated across a liquidation cascade overstates the next quiet hour. The
error is largest exactly when it costs most, at short horizons around news.
Treat the output as a better-calibrated heuristic than a fixed margin, not as
a price. The realised-vol EWMA is also backward-looking by construction: it
cannot see a scheduled CPI print thirty minutes out.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "bracket_probability",
    "ewma_variance",
    "ewma_volatility",
    "fair_price",
    "log_returns",
    "norm_cdf",
    "scale_volatility",
    "terminal_probability",
]

ONE = Decimal(1)
#: Fair values are quoted to four decimals — Kalshi tick sizes go sub-cent
#: (`price_level_structure`), so two decimals would quantize away real prices.
QUANTUM = Decimal("0.0001")

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """Standard normal CDF.

    Accurate to roughly machine epsilon in the body of the distribution. The
    far left tail saturates to 0.0 below about x = -8.3, and the right tail to
    1.0 above +8.3; that is fine here only because `fair_price` clamps away
    from certainty regardless, so a saturated tail can never become the last
    cents of edge.
    """
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def log_returns(prices: Sequence[Decimal]) -> list[float]:
    """Consecutive log returns of a price series, oldest first.

    Raises on a non-positive price rather than letting `math.log` do it: a zero
    or negative spot is corrupt data (a dropped field parsed as `0`, a sentinel
    that leaked through), and the error should name that rather than surface as
    a domain error from the standard library ten frames down.

    Returns are `float` on purpose. They are statistics, not money — nothing
    downstream of here is ever quoted or settled — and `Decimal` has no `log`
    that would survive the variance recursion anyway.
    """
    out: list[float] = []
    previous: float | None = None
    for i, price in enumerate(prices):
        value = float(price)
        if not (value > 0.0) or not math.isfinite(value):
            raise ValueError(
                f"price at index {i} must be positive and finite, got {price!r}"
            )
        if previous is not None:
            out.append(math.log(value) - math.log(previous))
        previous = value
    return out


def ewma_variance(
    returns: Sequence[float], *, lam: float, min_samples: int = 30
) -> float | None:
    """RiskMetrics EWMA variance: `s2_t = lam*s2_{t-1} + (1 - lam)*r_t^2`.

    `None` when there are fewer than `min_samples` returns. A short sample does
    not produce a *noisy* volatility, it produces an arbitrary one — with
    lam=0.94 the seed still carries ~16% of its weight after 30 steps — and an
    arbitrary sigma prices a strike with total confidence.

    `lam` outside (0, 1) is refused: at lam >= 1 the recursion ignores the data
    entirely, and at lam <= 0 it *is* the last squared return.

    The recursion is seeded with the mean squared return of the window rather
    than with `r_0^2`, which would leave the estimate hostage to a single tick.
    This is not lookahead: every return in `returns` is history relative to the
    estimate, which is only ever used for the period *after* the last one.
    """
    if not math.isfinite(lam) or not (0.0 < lam < 1.0):
        return None
    n = len(returns)
    if n < max(min_samples, 1):
        return None
    if not all(math.isfinite(r) for r in returns):
        # A NaN anywhere poisons the whole recursion and comes out the far end
        # as a NaN probability, which compares False against every threshold
        # and therefore silently disables the guards downstream.
        return None

    variance = sum(r * r for r in returns) / n
    for r in returns:
        variance = lam * variance + (1.0 - lam) * r * r
    if not math.isfinite(variance) or variance < 0.0:
        return None
    return variance


def ewma_volatility(
    returns: Sequence[float], *, lam: float, min_samples: int = 30
) -> float | None:
    """Per-period sigma: the square root of `ewma_variance`, or `None`."""
    variance = ewma_variance(returns, lam=lam, min_samples=min_samples)
    if variance is None:
        return None
    return math.sqrt(variance)


def scale_volatility(per_period_sigma: float, periods: float) -> float | None:
    """Scale a per-period sigma to a horizon of `periods` periods.

    Square-root-of-time: `sigma_h = sigma * sqrt(periods)`. `periods` is in the
    same unit the returns were sampled at — 90 one-minute returns for a
    90-minute horizon — and getting that unit wrong is the single easiest way
    to be wrong by a factor of eight without anything looking odd.

    This assumes independent, identically distributed returns. Bitcoin's are
    neither (see the module docstring); the scaling is a defensible default,
    not a description of the process, and it degrades as the horizon grows.

    A negative horizon or a negative sigma is refused rather than passed to
    `sqrt`. Zero periods legitimately gives zero — no time, no dispersion —
    and the consumer refuses on `sigma <= 0` from there.
    """
    if not math.isfinite(per_period_sigma) or per_period_sigma < 0.0:
        return None
    if not math.isfinite(periods) or periods < 0.0:
        return None
    return per_period_sigma * math.sqrt(periods)


def terminal_probability(
    *, spot: Decimal, strike: Decimal, sigma: float, above: bool
) -> float | None:
    """`P(S_T > K)` (or `P(S_T < K)`) under a driftless lognormal.

    `sigma` must already be scaled to the horizon — this function has no notion
    of time, which keeps the units question in exactly one place
    (`scale_volatility`) instead of two.

        d2 = (ln(S/K) - sigma^2/2) / sigma,  P(S_T > K) = Phi(d2)

    The `-sigma^2/2` is the Ito correction, not a drift: it is what makes
    `E[S_T] = S` hold for a lognormal whose *log* has zero mean. Its practical
    effect is that an exactly at-the-money strike prices slightly *below* 50c,
    by more the wider the horizon. That is correct and people will read it as a
    bug — the median of a lognormal sits below its mean.

    `None` for a non-positive or non-finite sigma, spot, or strike. A zero
    sigma is the interesting refusal: it says the outcome is deterministic, and
    the answer that follows is a probability of exactly 0 or 1.
    """
    if not math.isfinite(sigma) or sigma <= 0.0:
        return None
    if spot <= 0 or strike <= 0:
        return None
    s = float(spot)
    k = float(strike)
    if not (math.isfinite(s) and math.isfinite(k)) or s <= 0.0 or k <= 0.0:
        return None

    d2 = (math.log(s / k) - 0.5 * sigma * sigma) / sigma
    p_above = norm_cdf(d2)
    return p_above if above else 1.0 - p_above


def bracket_probability(
    *, spot: Decimal, floor_strike: Decimal, cap_strike: Decimal, sigma: float
) -> float | None:
    """`P(floor <= S_T <= cap)` under the same driftless lognormal.

    Computed as `P(S_T > floor) - P(S_T > cap)`. The bounds are inclusive on
    paper and it makes no difference: under a continuous distribution the
    endpoints carry zero mass.

    An inverted bracket (floor > cap) is refused rather than returned as a
    negative probability clamped to zero. It means the two strikes were read
    from the wrong fields or the wrong market, and a confident 0.00 (a
    free-money NO) is a far worse answer than nothing.
    """
    if floor_strike > cap_strike:
        return None
    above_floor = terminal_probability(
        spot=spot, strike=floor_strike, sigma=sigma, above=True
    )
    above_cap = terminal_probability(
        spot=spot, strike=cap_strike, sigma=sigma, above=True
    )
    if above_floor is None or above_cap is None:
        return None
    # Floating subtraction of two near-equal tail values can land a hair either
    # side of the unit interval; the bracket itself is already validated.
    return min(1.0, max(0.0, above_floor - above_cap))


def fair_price(
    *,
    strike_type: str | None,
    floor_strike: Decimal | None,
    cap_strike: Decimal | None,
    spot: Decimal,
    sigma: float,
    max_fair: Decimal = Decimal("0.99"),
) -> Decimal | None:
    """Model fair value of the YES side, in dollars, or `None`.

    Mirrors `resolve_strike` in `stale_quote.py` in which strike types it will
    touch, and refuses the same ones: `custom` carries its rules in prose, and
    an unrecognised type is not an invitation to assume `greater`.

    **`greater` and `greater_or_equal` are deliberately identical here.** Under
    a continuous terminal distribution `P(S_T = K) = 0`, so the strictness of
    the inequality has no effect on the price. This contrasts with
    `resolve_strike`, where the same two types are compared against a single
    *observed* spot value and the strictness genuinely decides the outcome —
    spot lands exactly on a round strike more often than a continuous model
    would suggest. Both behaviours are right for their context. Do not
    "fix" one to match the other.

    The result is clamped into `[1 - max_fair, max_fair]` and quantized to four
    decimals. The clamp is symmetric on purpose: a near-certain NO floors at
    `1 - max_fair`, not at zero. A model that outputs 0.0000 is asserting that
    the last cent of an ask is pure profit, and the model does not know that —
    it knows a sigma estimated from a window that had no reason to contain the
    move that matters.
    """
    if not (Decimal("0.5") < max_fair < ONE):
        # Outside this range the clamp is degenerate (or inverted) and would
        # quietly rewrite every price it touches.
        return None
    if spot <= 0:
        return None

    kind = (strike_type or "").strip().lower()

    probability: float | None
    if kind in ("greater", "greater_or_equal"):
        if floor_strike is None:
            return None
        probability = terminal_probability(
            spot=spot, strike=floor_strike, sigma=sigma, above=True
        )
    elif kind in ("less", "less_or_equal"):
        # Kalshi puts the single boundary of a `less` market in `cap_strike`,
        # but not invariably; `resolve_strike` falls back to `floor_strike` for
        # the same reason and the two must agree about which strike they mean.
        boundary = cap_strike if cap_strike is not None else floor_strike
        if boundary is None:
            return None
        probability = terminal_probability(
            spot=spot, strike=boundary, sigma=sigma, above=False
        )
    elif kind == "between":
        if floor_strike is None or cap_strike is None:
            return None
        probability = bracket_probability(
            spot=spot, floor_strike=floor_strike, cap_strike=cap_strike, sigma=sigma
        )
    else:
        # `custom` and anything unrecognised.
        return None

    if probability is None or not math.isfinite(probability):
        return None

    high = max_fair.quantize(QUANTUM, rounding=ROUND_HALF_UP)
    low = (ONE - max_fair).quantize(QUANTUM, rounding=ROUND_HALF_UP)
    value = Decimal(str(probability))
    # Clamp before quantizing: quantization is monotone, so a value inside the
    # bounds cannot round outside them.
    clamped = min(max(value, low), high)
    return clamped.quantize(QUANTUM, rounding=ROUND_HALF_UP)

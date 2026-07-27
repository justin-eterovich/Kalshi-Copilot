"""Daily-temperature distributions on the grid the market actually settles on.

Kalshi's daily high/low temperature markets settle on the National Weather
Service Climatological Report, and that report gives **whole degrees
Fahrenheit**. Nothing in these markets is continuous. A bucket whose rules read
"is between 96-97°" — carried in our schema as ``strike_type="between",
floor_strike=96, cap_strike=97`` — pays YES on exactly two outcomes, ``{96,
97}``. Adjacent buckets tile the integers with no overlap (``{94,95}``,
``{96,97}``, ``{98,99}``) and the ends of the board are open tails
("greater than 96", "less than 89").

**The dangerous version of this module is the one that prices those buckets
with a continuous normal.** ``Phi((97 - f)/s) - Phi((96 - f)/s)`` is the mass of
a one-degree interval where the bucket owns two integers' worth of it, so it
comes out roughly *half* the true probability. It is wrong in the same
direction on every bucket in the book, which is exactly what a working model
looks like: no NaNs, no refusals, prices that sum to something less than a
dollar, and a screen full of buckets that all appear overpriced. Selling the
whole board on that is the trade this module exists to prevent.

So the model discretises first and prices second:

- The forecast uncertainty is continuous — a normal around the point forecast
  is the working assumption — but it is projected onto integers **before** any
  bucket is touched. Integer ``k`` owns the half-open band ``[k - 0.5, k +
  0.5)``, because that is the set of true temperatures the report rounds to
  ``k``. Half a degree of misplacement here biases every price in the book the
  same way.
- Strike text is read on the integer grid. "Greater than 96" means ``T >= 97``,
  not ``T > 96.0``; the difference is the entire mass at 96, which near the
  forecast is the single largest bucket on the board.
- Refusals return ``None``: a non-positive or non-finite sigma, a non-finite
  forecast, an inverted range, a missing boundary, ``custom``, and anything
  unrecognised. An unrecognised strike type is not an invitation to assume
  ``between``.
- ``fair_price`` never returns 0 or 1. It clamps symmetrically into ``[1 -
  max_fair, max_fair]``, so the last cents of edge are never manufactured out
  of an assumption of certainty.

**The normal is an assumption, not a fact.** Temperature forecast errors are
roughly normal through the middle of the distribution and have distinctly
fatter tails than normal at the extremes — a busted frontal timing or an
unforecast marine layer lives four sigma out far more often than 1-in-16,000.
That means the model is least trustworthy precisely on the cheap,
far-from-forecast buckets whose implied edge looks largest. Treat a 2c fair
value against a 6c ask as a reason to look, not as a 4c edge. Sigma itself is
the caller's problem (see ``app/weather/calibration.py``); nothing here
estimates it, and nothing here knows the horizon.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "degree_pmf",
    "fair_price",
    "norm_cdf",
    "probability_between",
    "probability_greater",
    "probability_less",
]

ONE = Decimal(1)
#: Fair values are quoted to four decimals, matching `app/btc/vol.py` — Kalshi
#: tick sizes go sub-cent, so two decimals would quantize away real prices.
QUANTUM = Decimal("0.0001")

#: Widest integer span `degree_pmf` will build. No daily-temperature market
#: spans anything close to this; a range wider than it means the caller passed
#: something that is not degrees (an epoch timestamp, a price in cents, a
#: strike from the wrong market), and allocating a dict for it is a worse
#: answer than refusing.
MAX_SPAN = 1000

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """Standard normal CDF, via ``math.erf``.

    Accurate to about machine epsilon through the body of the distribution;
    the tails saturate to 0.0 / 1.0 beyond roughly ±8.3. That saturation is
    tolerable only because ``fair_price`` clamps away from certainty anyway —
    and because, as the module docstring says, the normal is the wrong shape
    out there regardless.
    """
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _cdf_at(edge: float, forecast: float, sigma: float) -> float:
    """``P(T_continuous < edge)`` under the assumed normal."""
    return norm_cdf((edge - forecast) / sigma)


def _usable(forecast: float, sigma: float) -> bool:
    """Whether a (forecast, sigma) pair can be priced at all.

    A zero sigma is the interesting refusal: it claims the forecast is exact,
    and what follows from it is a probability of exactly 0 or 1 on every
    bucket. A NaN is the dangerous one — it compares ``False`` against every
    threshold downstream and so silently disables the guards it flows into.
    """
    if not math.isfinite(forecast):
        return False
    return math.isfinite(sigma) and sigma > 0.0


def degree_pmf(
    *, forecast: float, sigma: float, low: int, high: int
) -> dict[int, float] | None:
    """Probability mass per integer degree over the inclusive range ``[low, high]``.

    Integer ``k`` takes the continuous mass of ``[k - 0.5, k + 0.5)`` — the
    half-open half-degree band that rounds to ``k``. Half-open rather than
    closed so the bands tile without double-counting the boundary; under a
    continuous normal the single point carries no mass either way, but the
    convention has to be written down once so nothing downstream invents its
    own.

    **The masses do not sum to 1**, and are not normalised to. The range is
    finite, so whatever the normal puts outside ``[low - 0.5, high + 0.5)``
    is simply absent. Normalising would silently redistribute tail mass into
    the buckets on screen and make a narrow window look like a certainty.

    ``None`` for a non-positive or non-finite sigma, a non-finite forecast,
    an inverted range, or a span wider than ``MAX_SPAN``.
    """
    if not _usable(forecast, sigma):
        return None
    if low > high:
        return None
    if high - low + 1 > MAX_SPAN:
        return None

    # One CDF evaluation per band edge rather than two per degree: adjacent
    # bands share an edge, and sharing it makes the masses telescope exactly,
    # so summing a contiguous run of this pmf equals the closed-form interval
    # probability to the last bit.
    edges = [_cdf_at(k - 0.5, forecast, sigma) for k in range(low, high + 2)]
    pmf: dict[int, float] = {}
    for i, k in enumerate(range(low, high + 1)):
        # Differences of two near-equal tail values can land a hair below
        # zero; the ordering of the edges is guaranteed by construction.
        pmf[k] = max(0.0, edges[i + 1] - edges[i])
    return pmf


def probability_between(
    *, forecast: float, sigma: float, floor_strike: int, cap_strike: int
) -> float | None:
    """``P(floor <= T <= cap)`` over **integers** — both endpoints included.

    This is the inclusive integer reading, which for ``floor=96, cap=97`` is
    ``P(T in {96, 97})`` and covers the continuous span ``[95.5, 97.5)`` —
    two full degrees, not one. Pricing it as the interval ``[96, 97]`` is the
    error described in the module docstring and costs roughly half the
    bucket's probability.

    ``None`` on an inverted bucket (``floor > cap``) rather than a clamped
    0.0. An inverted bucket means the strikes came from the wrong fields or
    the wrong market, and a confident zero reads as free money on the NO side.
    """
    pmf = degree_pmf(forecast=forecast, sigma=sigma, low=floor_strike, high=cap_strike)
    if pmf is None:
        return None
    return min(1.0, max(0.0, math.fsum(pmf.values())))


def _greater_threshold(strike: float, *, inclusive: bool) -> int:
    """Smallest integer temperature that satisfies a "greater" strike.

    ``inclusive`` (``greater_or_equal``, ``T >= strike``) takes the smallest
    integer at or above the strike; the strict form (``T > strike``) takes the
    smallest integer strictly above it. On an integer strike those differ by
    exactly one degree, which is the whole mass at the strike itself.

    Fractional strikes are real: Kalshi encodes "83 or above" as ``above
    82.99``, and both readings of that land on 83, which is what makes the
    encoding safe to consume either way.
    """
    if inclusive:
        return math.ceil(strike)
    # `floor + 1` rather than `ceil`, because `ceil(96.0)` is 96 and 96 does
    # not satisfy "greater than 96".
    return math.floor(strike) + 1


def _less_threshold(strike: float, *, inclusive: bool) -> int:
    """Largest integer temperature that satisfies a "less" strike."""
    if inclusive:
        return math.floor(strike)
    return math.ceil(strike) - 1


def probability_greater(
    *, forecast: float, sigma: float, strike: float, inclusive: bool = False
) -> float | None:
    """``P(T > strike)`` — or ``P(T >= strike)`` — on the integer grid.

    "Greater than 96" on a report that only emits whole degrees means ``T >=
    97``, so the tail starts at the *band* edge 96.5, not at 96.0. The
    difference between those two answers is the entire mass at 96 — near the
    forecast, the largest single bucket on the board.
    """
    if not _usable(forecast, sigma):
        return None
    if not math.isfinite(strike):
        return None
    threshold = _greater_threshold(strike, inclusive=inclusive)
    # P(T >= m) is the mass above the bottom edge of m's band.
    return min(1.0, max(0.0, 1.0 - _cdf_at(threshold - 0.5, forecast, sigma)))


def probability_less(
    *, forecast: float, sigma: float, strike: float, inclusive: bool = False
) -> float | None:
    """``P(T < strike)`` — or ``P(T <= strike)`` — on the integer grid.

    The mirror of ``probability_greater``: "less than 89" means ``T <= 88``,
    so the tail ends at the *top* edge of 88's band, 88.5.
    """
    if not _usable(forecast, sigma):
        return None
    if not math.isfinite(strike):
        return None
    threshold = _less_threshold(strike, inclusive=inclusive)
    # P(T <= m) is the mass below the top edge of m's band.
    return min(1.0, max(0.0, _cdf_at(threshold + 0.5, forecast, sigma)))


def fair_price(
    *,
    strike_type: str | None,
    floor_strike: Decimal | None,
    cap_strike: Decimal | None,
    forecast: float,
    sigma: float,
    max_fair: Decimal = Decimal("0.99"),
) -> Decimal | None:
    """Model fair value of the YES side, in dollars, or ``None``.

    Dispatches on the same strike vocabulary as ``app/detectors/stale_quote.py``
    and ``app/btc/vol.py``, and refuses the same things: ``custom`` carries its
    rules in prose, and an unrecognised type is not an invitation to assume a
    default.

    Unlike ``vol.py``, ``greater`` and ``greater_or_equal`` are **not**
    equivalent here. There the terminal distribution is continuous and the
    strike carries no mass; here the outcome is an integer and the strictness
    of the inequality moves the answer by a whole degree of probability. Do not
    "simplify" these two branches together.

    ``between`` boundaries are projected onto the integers the bucket actually
    contains — ``ceil(floor)`` up to ``floor(cap)`` — so a bucket quoted with
    fractional edges still prices the integers inside it and nothing else. A
    bucket that contains no integer at all is refused rather than priced at
    zero.

    The result is clamped into ``[1 - max_fair, max_fair]`` and quantized to
    four decimals. The clamp is symmetric on purpose: a near-hopeless bucket
    floors at ``1 - max_fair``, not at 0. A model that prints 0.0000 is
    asserting that the last cent of an ask is pure profit, and this one knows
    only a normal fitted to forecast errors that are not normal in the tails.
    """
    if not (Decimal("0.5") < max_fair < ONE):
        # Outside this range the clamp is degenerate or inverted, and would
        # quietly rewrite every price it touches.
        return None
    if not _usable(forecast, sigma):
        return None

    kind = (strike_type or "").strip().lower()

    probability: float | None
    if kind in ("greater", "greater_or_equal"):
        if floor_strike is None:
            return None
        probability = probability_greater(
            forecast=forecast,
            sigma=sigma,
            strike=float(floor_strike),
            inclusive=kind == "greater_or_equal",
        )
    elif kind in ("less", "less_or_equal"):
        # A `less` market carries its single boundary in `cap_strike`, but not
        # invariably; `resolve_strike` falls back the same way and the two must
        # agree about which strike they mean.
        boundary = cap_strike if cap_strike is not None else floor_strike
        if boundary is None:
            return None
        probability = probability_less(
            forecast=forecast,
            sigma=sigma,
            strike=float(boundary),
            inclusive=kind == "less_or_equal",
        )
    elif kind == "between":
        if floor_strike is None or cap_strike is None:
            return None
        # `math.ceil`/`math.floor` on a Decimal are exact — no float round-trip
        # on the boundary, which is the one place a half-degree could be lost.
        low = math.ceil(floor_strike)
        high = math.floor(cap_strike)
        if low > high:
            # Either the bucket is inverted, or it spans no integer at all
            # (e.g. 96.2 to 96.8, which the report can never produce). Both are
            # data errors, and both would otherwise price as a free NO.
            return None
        probability = probability_between(
            forecast=forecast, sigma=sigma, floor_strike=low, cap_strike=high
        )
    else:
        # `custom` and anything unrecognised.
        return None

    if probability is None or not math.isfinite(probability):
        return None

    high_bound = max_fair.quantize(QUANTUM, rounding=ROUND_HALF_UP)
    low_bound = (ONE - max_fair).quantize(QUANTUM, rounding=ROUND_HALF_UP)
    value = Decimal(str(probability))
    # Clamp before quantizing: quantization is monotone, so a value inside the
    # bounds cannot round outside them.
    clamped = min(max(value, low_bound), high_bound)
    return clamped.quantize(QUANTUM, rounding=ROUND_HALF_UP)

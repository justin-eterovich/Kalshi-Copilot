"""Shared statistical primitives.

Small, pure, and deliberately in ``app/core`` for the same reason fee math is:
there must be exactly one of each.  A second copy of an interval formula that
drifts from the first is a silent disagreement between two screens that both
look authoritative — the detector calibration screen saying a bucket is fine
while the backtest report card says it is not, for no reason a reader could
ever find.

Nothing here knows about money, markets, or the database.  Callers hold the
units; these functions hold the arithmetic.  Money-valued statistics belong in
the module that owns the money (see ``app/backtest/stats.py``, which keeps its
P&L in :class:`~decimal.Decimal` and only borrows the quantiles from here).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist

__all__ = [
    "percentile_sorted",
    "wilson_interval",
    "z_for_confidence",
]


def wilson_interval(
    successes: int, n: int, *, z: float = 1.96
) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion.

    Returns ``None`` for a sample that cannot be interpreted: no trials, a
    negative count, or more successes than trials. Those are upstream bugs and
    guessing at what was meant would launder the bug into a number.

    Wilson rather than the normal approximation because every bucket this
    module cares about has p near 0 or 1, where Wald's interval is skewed,
    under-covers, and can leave [0, 1] entirely. Wilson cannot: at 0-for-n the
    lower bound is exactly 0 and at n-for-n the upper bound is exactly 1.
    """
    if n <= 0 or successes < 0 or successes > n:
        return None

    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    # Wilson is analytically inside [0, 1]; the clamp is only against floating
    # point drift at the endpoints, where center and half are nearly equal.
    return (max(0.0, center - half), min(1.0, center + half))


def z_for_confidence(confidence: float) -> float:
    """Two-sided normal quantile for a confidence level: 0.95 -> 1.959964...

    The literal ``1.96`` that appears elsewhere in the codebase is this
    function at its default. Anything that lets the caller pick a confidence
    level must compute the quantile rather than interpolate between remembered
    constants.

    Refuses a confidence outside ``(0, 1)`` exclusive: 0 and 1 are the
    degenerate cases (a zero-width interval and an infinite one), and both are
    far more likely to be a caller passing a percentage than an intent.
    """
    if not (0.0 < confidence < 1.0):
        raise ValueError(
            f"confidence must be strictly between 0 and 1, got {confidence!r} "
            f"(0.95, not 95)"
        )
    return NormalDist().inv_cdf(0.5 + confidence / 2.0)


def percentile_sorted(sorted_values: Sequence[float], q: float) -> float:
    """Linearly interpolated ``q``-quantile of an **already sorted** sequence.

    ``q`` is a fraction, not a percentage: ``0.025`` is the 2.5th percentile.

    The precondition is in the name on purpose. A percentile of unsorted data
    is not an error, it is a plausible-looking wrong number — which is the
    failure mode this codebase treats as worse than a crash. Callers that
    already sort (the bootstrap sorts once and reads both tails) should not pay
    to sort again, so the sorting is theirs to do and theirs to be sure of.

    Uses the same interpolation as NumPy's default 'linear' method, so a
    bootstrap interval computed here matches one anybody checks in a notebook.
    """
    if not sorted_values:
        raise ValueError("percentile of an empty sequence is undefined")
    if not (0.0 <= q <= 1.0):
        raise ValueError(f"q must be a fraction in [0, 1], got {q!r}")

    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]

    position = q * (n - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight

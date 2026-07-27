"""Longshot-bias calibration: does *this* market set misprice the tails?

Prediction markets are widely reported to overprice unlikely outcomes and
underprice near-certain ones. That is a claim about some population of
markets at some point in time, not a law. This module measures whether the
markets we have actually settled behave that way, bucket by bucket.

**The dangerous version of this module is the one that divides two small
integers and calls the answer a rate.** Take every settled market that once
traded at 5c, count the winners, print "5c contracts resolve YES 9% of the
time" — and you have built a sizing rule out of eleven observations and a
Wald interval that runs from -3% to +21%. The normal approximation is at its
worst exactly where longshot bias lives: near p=0 and p=1 it is skewed,
badly under-covered, and routinely produces bounds outside [0, 1]. Confident
nonsense, precisely where the strategy wants to act.

So three things are load-bearing here:

- **Wilson score intervals, never Wald.** Wilson is derived by inverting the
  score test rather than assuming normality of the estimate, so it stays
  inside [0, 1] at every count including 0-for-n and n-for-n, and keeps
  roughly nominal coverage in the tails. This is the whole reason the module
  exists in this shape: the interesting buckets (1-10c, 90-99c) are the ones
  the easy method gets wrong.
- **A sample floor.** `significant()` is the only function a caller should
  act on, and it drops any bucket below `min_samples` (config default 500).
  Two observations produce a beautiful rate and mean nothing.
- **A bucket is only "mispriced" when its own price sits outside the
  interval.** Not when the point estimate differs — point estimates always
  differ.

**This measures calibration, not profitability.** The implied rate of a
bucket is taken to be its cent value: bucket 5 -> 0.05. That treats the
price as the market's stated probability and ignores fees entirely. A
contract bought at 5c must win rather more than 5% of the time to break even
once the taker fee is paid, so a bucket can be genuinely mispriced here and
still be a losing trade. Fee math lives in `app/core/fees.py` and nothing in
this file substitutes for it. Do not read a trading rule out of these
numbers.

**Selection bias is the dominant risk and cannot be fixed downstream.**
Observations come from the markets this system happened to be watching --
`ingest.watchlist`, which is chosen for liquidity and multi-leg structure and
goes stale as events settle. A calibration built on a watchlist measures the
watchlist, not Kalshi. And only *settled* markets appear at all, so anything
still open, delisted, or voided is invisible to this by construction. A
striking result here is a reason to go look at the sample, not a finding.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.statistics import wilson_interval

# `wilson_interval` used to be defined here and is re-exported so that
# `from app.detectors.longshot_calibration import wilson_interval` keeps
# working. It moved to app/core/statistics.py when the backtest report card
# needed it too: two copies of an interval formula is two answers to the same
# question, and the second one is always the one nobody notices is stale.

__all__ = [
    "BucketStat",
    "Observation",
    "bucket_for",
    "calibrate",
    "significant",
    "wilson_interval",
]

# A price of 0c or 100c is a settled market, not a probability, and would give
# an implied rate the interval can never sit outside. Buckets live strictly
# between.
MIN_BUCKET_CENTS = 1
MAX_BUCKET_CENTS = 99


@dataclass(frozen=True, slots=True)
class Observation:
    """One settled market, reduced to the bucket it traded in and its outcome."""

    price_bucket_cents: int
    settled_yes: bool


@dataclass(frozen=True, slots=True)
class BucketStat:
    """What a single price bucket did, with an interval around it."""

    bucket_cents: int
    samples: int
    yes_count: int
    #: Fraction of this bucket's markets that settled YES.
    observed_rate: float
    #: The bucket's own price as a probability: bucket 5 -> 0.05. Ignores fees
    #: — see the module docstring.
    implied_rate: float
    ci_low: float
    ci_high: float

    @property
    def mispriced(self) -> bool:
        """Whether the market's own price falls outside the interval.

        Deliberately not "the observed rate differs from the price": at any
        finite sample it always does. The question is whether the price is a
        value the data can still support.
        """
        return not (self.ci_low <= self.implied_rate <= self.ci_high)


def bucket_for(
    price_cents: int, *, low_band: tuple[int, int], high_band: tuple[int, int]
) -> int | None:
    """Bucket a price, or ``None`` if it is outside both bands.

    Bands are inclusive ``(lo, hi)`` in cents. Only the tails are bucketed:
    the middle of the book is where the market is most likely to be well
    calibrated and least likely to be worth the sample budget.

    Refuses an inverted band rather than silently matching nothing — a
    transposed config would otherwise produce an empty calibration that looks
    like "no bias found" instead of "you configured it backwards".
    """
    for lo, hi in (low_band, high_band):
        if lo > hi:
            return None
    if not MIN_BUCKET_CENTS <= price_cents <= MAX_BUCKET_CENTS:
        return None
    for lo, hi in (low_band, high_band):
        if lo <= price_cents <= hi:
            return price_cents
    return None


def calibrate(observations: Sequence[Observation]) -> list[BucketStat]:
    """Aggregate observations into one stat per bucket, ascending by price.

    Rows whose bucket is not a real cent price are dropped, not repaired.
    Note that this returns *every* bucket present, including one-sample ones,
    because callers reasonably want to see coverage. Acting on the output is
    what `significant()` is for.
    """
    totals: Counter[int] = Counter()
    yeses: Counter[int] = Counter()
    for obs in observations:
        if not MIN_BUCKET_CENTS <= obs.price_bucket_cents <= MAX_BUCKET_CENTS:
            continue
        totals[obs.price_bucket_cents] += 1
        if obs.settled_yes:
            yeses[obs.price_bucket_cents] += 1

    stats: list[BucketStat] = []
    for bucket in sorted(totals):
        n = totals[bucket]
        yes = yeses[bucket]
        interval = wilson_interval(yes, n)
        if interval is None:
            # Unreachable given the counting above, but the alternative to a
            # skip is inventing bounds, and this file does not do that.
            continue
        low, high = interval
        stats.append(
            BucketStat(
                bucket_cents=bucket,
                samples=n,
                yes_count=yes,
                observed_rate=yes / n,
                implied_rate=bucket / 100.0,
                ci_low=low,
                ci_high=high,
            )
        )
    return stats


def significant(stats: Sequence[BucketStat], *, min_samples: int) -> list[BucketStat]:
    """The buckets a caller may actually believe.

    A bucket qualifies only if it clears the sample floor *and* the market's
    own price sits outside its interval. `min_samples` is the entire safety
    mechanism in this module — the project config defaults it to 500 — because
    a thin bucket with an extreme rate is the single most convincing way this
    analysis lies to you.
    """
    return [s for s in stats if s.samples >= min_samples and s.mispriced]

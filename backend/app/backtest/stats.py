"""The report card: has a detector actually earned money, or does it look like it?

This module answers one question — *given the trades this detector produced,
what may we honestly claim?* — and it is built around a single awkward fact
about binary markets.

**The P&L of one binary-market trade is a two-point distribution.**  You buy a
contract at 10c; it settles at 100c or at 0.  The trade returns +90c minus
fees, or -10c minus fees.  Nothing in between ever happens.  So the per-trade
P&L series a detector generates is not a bell curve with some noise on it, it
is a coin flip between two fixed numbers, and it is **heavily skewed** whenever
the price is far from 50c.  At 10c the distribution is one rare large win and
nine common small losses.

That matters because the normal approximation to the mean — the textbook
``mean ± 1.96 · s/√n`` — assumes the sampling distribution of the mean is
symmetric.  For a two-point variable it converges to that eventually, but
"eventually" is governed by the skewness over ``√n``, and a report card
operates in exactly the regime where it has not happened yet: **20 to 50
trades**.  Two specific failures follow, and both have the same shape, which
is that the interval is *confident in the wrong place*:

- The interval extends past what the trade can physically do.  A 10c longshot
  cannot lose more than 10c per contract, but Wald will happily report a lower
  bound of -19c/trade, an outcome with probability zero.
- The interval is symmetric when the truth is not, so the two ends do not
  carry the same amount of evidence, and whichever end the reader cares about
  (does it clear zero?) is the one being misplaced.

So this module provides both intervals and **the verdict uses the bootstrap**:

- :func:`normal_interval` — the cheap Wald interval.  Kept because it is what
  everyone expects to see and because a large divergence between the two is
  itself information about how skewed the sample is.  Optimistic at small n;
  do not gate on it.
- :func:`bootstrap_interval` — a seeded percentile bootstrap of the mean.  It
  assumes nothing about the shape of the distribution, only that the sample is
  representative of it, and it inherits the support of the data: a resample of
  a longshot series can never produce a mean below "every trade lost", because
  no such sample exists to draw.

Everything money-valued here is :class:`~decimal.Decimal` **cents**, and none
of it is a whole number: fees round up to a centicent (``$0.0001``), so a
realistic per-trade P&L looks like ``Decimal("89.9776")``.  Probabilities,
Brier scores and the bounds on them are ``float`` — they are not money.

**What this module cannot tell you.**  It measures a sample of trades.  It has
no view on whether that sample is representative: a backtest run over the
period a detector was tuned on will pass this gate and lose money live, and no
interval computed after the fact can detect that.  The verdict is a *necessary*
condition for believing a detector, never a sufficient one.

Pure by construction — standard library only, no database, no I/O — so a
report card can be computed in a test, in a notebook, or from a CSV with
nothing running.
"""

from __future__ import annotations

import enum
import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import fsum
from typing import Any, Final

from app.core.statistics import percentile_sorted, z_for_confidence

__all__ = [
    "BrierParts",
    "Expectancy",
    "Verdict",
    "bootstrap_interval",
    "brier_decomposition",
    "brier_score",
    "describe",
    "expectancy",
    "max_drawdown",
    "normal_interval",
    "verdict",
]

#: Reported money is quantized to a centicent — the same grain the exchange
#: bills fees at (see ``CENTICENT`` in app/core/fees.py, which this module
#: deliberately does not import: stats.py stays free of the schedule loader and
#: its YAML dependency). Anything finer would claim precision the underlying
#: money does not have; anything coarser would round a real fee away.
QUANTUM_CENTS: Final = Decimal("0.0001")

#: Default resample count for the bootstrap. Monte-Carlo error on a percentile
#: falls as 1/sqrt(resamples); 10k puts it well below a centicent for any
#: realistic P&L series, and costs milliseconds at report-card sample sizes.
DEFAULT_RESAMPLES: Final = 10_000

#: Fixed default seed. A report card that changed its verdict between two runs
#: on identical data would be indistinguishable from one that changed because
#: the data changed, so randomness here is always explicit and always pinned.
DEFAULT_SEED: Final = 0


# ---------------------------------------------------------------------------
# Expectancy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Expectancy:
    """Per-trade expectancy with an interval around it, all in Decimal cents."""

    n: int
    total_cents: Decimal
    #: Per-trade expectancy: total / n. ``Decimal(0)`` when ``n == 0``, which
    #: is a placeholder and not a claim — read ``n`` before reading this.
    mean_cents: Decimal
    #: ``None`` when ``n < 2``: one trade has no spread, and a standard error
    #: of zero would read as certainty.
    stderr_cents: Decimal | None
    ci_low_cents: Decimal | None
    ci_high_cents: Decimal | None
    #: Which interval produced ``ci_*``: "bootstrap", "normal", or "none".
    method: str
    #: Carried so :func:`describe` can name the level it is quoting. Hardcoding
    #: "95%" in the sentence would silently mislabel a report run at 99%.
    confidence: float = 0.95


def _validated(pnls: Sequence[Any], field: str = "pnls") -> list[Decimal]:
    """Coerce a P&L series to Decimal cents, refusing anything lossy.

    Floats are refused rather than converted. Every P&L in this system arrives
    from :mod:`app.trading.positions` or :mod:`app.trading.settlements` as a
    Decimal that already accounts for a centicent-rounded fee; a float in the
    sequence means somebody has been through ``float()`` on the way here, and
    accepting it would let the report card quietly disagree with the ledger it
    claims to summarise.

    ``bool`` is refused explicitly because it is an ``int``: a list of
    outcomes handed to an expectancy function is a real mistake, and
    ``[True, False]`` would otherwise average to 0.5c of profit.
    """
    out: list[Decimal] = []
    for i, value in enumerate(pnls):
        if isinstance(value, bool):
            raise TypeError(
                f"{field}[{i}]: refusing to read bool {value!r} as P&L — this "
                f"looks like an outcomes sequence passed to a money function"
            )
        if isinstance(value, Decimal):
            out.append(value)
        elif isinstance(value, int):
            out.append(Decimal(value))
        else:
            raise TypeError(
                f"{field}[{i}]: P&L must be Decimal cents, got "
                f"{type(value).__name__} {value!r}. Money is never float here."
            )
    return out


def _q(value: Decimal) -> Decimal:
    """Quantize a reported money figure to a centicent."""
    return value.quantize(QUANTUM_CENTS)


def normal_interval(
    pnls: Sequence[Decimal], *, confidence: float = 0.95
) -> tuple[Decimal, Decimal] | None:
    """Wald interval on the mean: ``mean ± z · s/√n``. ``None`` when n < 2.

    Provided for comparison and because readers expect it, **not** for gating.
    It is optimistic at small n on precisely the samples this module sees: the
    interval is symmetric about the mean, so on a skewed two-point P&L series
    it misplaces both ends, and it is unaware of the support of the data, so it
    will report per-trade losses larger than the position could sustain. When
    it and :func:`bootstrap_interval` disagree about whether zero is excluded,
    believe the bootstrap.

    Uses the sample standard deviation (n-1 divisor); with n < 2 there is no
    spread to estimate and this returns ``None`` rather than zero, because a
    zero-width interval around a single trade would read as certainty and pass
    the verdict gate.
    """
    values = _validated(pnls)
    n = len(values)
    if n < 2:
        return None

    mean = sum(values, Decimal(0)) / Decimal(n)
    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / Decimal(n - 1)
    stderr = variance.sqrt() / Decimal(n).sqrt()
    half = Decimal(str(z_for_confidence(confidence))) * stderr
    return (_q(mean - half), _q(mean + half))


def bootstrap_interval(
    pnls: Sequence[Decimal],
    *,
    confidence: float = 0.95,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[Decimal, Decimal] | None:
    """Seeded percentile bootstrap of the mean. ``None`` when n < 2.

    Draws ``resamples`` samples of size n with replacement, takes the mean of
    each, and reads the ``(1-confidence)/2`` and ``1-(1-confidence)/2``
    quantiles off the resulting distribution. That distribution is an estimate
    of the sampling distribution of the mean which makes **no** assumption
    about the shape of the per-trade P&L — which is the whole point, because
    the shape is two-point and skewed.

    Two consequences worth understanding before reading the output:

    - The interval inherits the support of the sample. A longshot series of
      +90c wins and -10c losses can never resample to a mean below -10c, and
      the bootstrap will not report one. Wald will.
    - The interval is **asymmetric about the mean** in the direction the data
      is skewed, and lumpy: with n trades there are only n+1 distinct win
      counts, so the resample means live on a lattice of spacing
      (win - loss)/n. At small n the endpoints visibly snap to it. That is the
      discreteness of the underlying bet showing through, not an artefact to
      smooth away.

    The resampling arithmetic is done in ``float``: 10,000 × n Decimal
    additions buy nothing here, because the bootstrap's own Monte-Carlo error
    is orders of magnitude larger than float rounding at cent magnitudes. The
    bounds are quantized back to Decimal centicents on the way out, so the
    caller never sees a float.

    Deterministic: same ``pnls``, same ``seed``, same interval, always.
    """
    values = _validated(pnls)
    n = len(values)
    if n < 2:
        return None
    if resamples < 1:
        raise ValueError(f"resamples must be at least 1, got {resamples!r}")
    # Validate the level before spending the resamples on it.
    z_for_confidence(confidence)

    rng = random.Random(seed)
    floats = [float(v) for v in values]
    # fsum rather than sum: the resample means are compared against zero at the
    # tails, and a drifting accumulation is exactly the kind of thing that
    # would move a boundary case across the gate for no reason.
    means = sorted(fsum(rng.choices(floats, k=n)) / n for _ in range(resamples))

    alpha = (1.0 - confidence) / 2.0
    low = percentile_sorted(means, alpha)
    high = percentile_sorted(means, 1.0 - alpha)
    return (_q(Decimal(str(low))), _q(Decimal(str(high))))


def expectancy(
    pnls: Sequence[Decimal],
    *,
    confidence: float = 0.95,
    method: str = "bootstrap",
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> Expectancy:
    """Summarise a P&L series into the numbers a report card shows.

    ``method`` selects the interval: ``"bootstrap"`` (the default, and what
    :func:`verdict` is calibrated for) or ``"normal"``. An unknown method
    raises rather than falling back, because a silently-substituted interval
    is a silently-substituted verdict.

    With fewer than two trades the interval fields are ``None`` and ``method``
    is reported as ``"none"``. That is deliberate and it is load-bearing:
    :func:`verdict` treats a missing interval as insufficient evidence, so a
    detector with one spectacular trade cannot reach a positive verdict by any
    path.
    """
    if method not in ("bootstrap", "normal"):
        raise ValueError(
            f"unknown interval method {method!r}; expected 'bootstrap' or 'normal'"
        )

    values = _validated(pnls)
    n = len(values)
    total = sum(values, Decimal(0))

    if n == 0:
        return Expectancy(
            n=0,
            total_cents=Decimal(0),
            mean_cents=Decimal(0),
            stderr_cents=None,
            ci_low_cents=None,
            ci_high_cents=None,
            method="none",
            confidence=confidence,
        )

    mean = total / Decimal(n)

    if n < 2:
        return Expectancy(
            n=n,
            total_cents=total,
            mean_cents=_q(mean),
            stderr_cents=None,
            ci_low_cents=None,
            ci_high_cents=None,
            method="none",
            confidence=confidence,
        )

    variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / Decimal(n - 1)
    stderr = variance.sqrt() / Decimal(n).sqrt()

    if method == "normal":
        interval = normal_interval(values, confidence=confidence)
    else:
        interval = bootstrap_interval(
            values, confidence=confidence, resamples=resamples, seed=seed
        )
    assert interval is not None  # n >= 2 was checked above
    low, high = interval

    return Expectancy(
        n=n,
        total_cents=total,
        mean_cents=_q(mean),
        stderr_cents=_q(stderr),
        ci_low_cents=low,
        ci_high_cents=high,
        method=method,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Calibration
#
# Probabilities, not money: these are float throughout, and the caller must not
# read a Brier score as a P&L.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrierParts:
    """Murphy's three-way decomposition of the Brier score.

    ``brier = reliability - resolution + uncertainty``. Each part answers a
    different question about a detector, and only two of them are about the
    detector at all:

    - **reliability** (lower is better): when it said 30%, did the thing happen
      30% of the time? This is calibration in the narrow sense. A detector with
      good reliability can still be useless.
    - **resolution** (higher is better): did its forecasts distinguish anything
      from the base rate? A detector that says "62%" about every market has
      zero resolution and can still be perfectly reliable. This is the part
      that says whether it knows something.
    - **uncertainty**: the variance of the outcomes themselves, ``p̄(1-p̄)``. It
      is a property of **the markets sampled, not of the detector**. A detector
      cannot change it, must not be credited for a low one, and must not be
      blamed for a high one — but it moves the total Brier score, which is why
      comparing raw Brier scores across different market sets is meaningless
      and this decomposition exists.
    """

    reliability: float
    resolution: float
    uncertainty: float
    n: int


def _validate_forecasts(
    predictions: Sequence[float], outcomes: Sequence[bool]
) -> tuple[list[float], list[float]]:
    """Shared validation. Refuses rather than repairing, as everywhere else."""
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions and outcomes must be the same length, got "
            f"{len(predictions)} and {len(outcomes)} — a misalignment here "
            f"scores each forecast against somebody else's market"
        )
    if not predictions:
        raise ValueError("no forecasts: a Brier score over zero markets is undefined")

    probs: list[float] = []
    for i, p in enumerate(predictions):
        value = float(p)
        if not (0.0 <= value <= 1.0):
            raise ValueError(
                f"predictions[{i}] = {p!r} is not a probability. A forecast "
                f"outside [0, 1] is a units bug (cents mistaken for a "
                f"probability is the usual one) and scoring it would hide that."
            )
        probs.append(value)

    return probs, [1.0 if bool(o) else 0.0 for o in outcomes]


def brier_score(predictions: Sequence[float], outcomes: Sequence[bool]) -> float:
    """Mean squared error of a probabilistic forecast. Lower is better.

    0 is a perfect forecaster (1.0 on everything that happened, 0.0 on
    everything that did not); 0.25 is what you get by saying 50% every time,
    which is the number to beat before any claim of skill; 1.0 is confidently
    and consistently wrong.

    Note that a coin-flip forecaster scores 0.25 *whatever the markets did*,
    while the base-rate forecaster scores the sample's own uncertainty — see
    :func:`brier_decomposition` before comparing scores across market sets.
    """
    probs, obs = _validate_forecasts(predictions, outcomes)
    return fsum((p - o) ** 2 for p, o in zip(probs, obs, strict=True)) / len(probs)


def brier_decomposition(
    predictions: Sequence[float],
    outcomes: Sequence[bool],
    *,
    buckets: int = 10,
) -> BrierParts:
    """Split a Brier score into reliability, resolution and uncertainty.

    Forecasts are grouped into ``buckets`` equal-width bins over [0, 1] (1.0
    lands in the top bin), and each bin contributes its count, its mean
    forecast and its observed rate.

    **On the identity.** ``brier = reliability - resolution + uncertainty``
    holds *exactly* when the forecasts do not vary within a bucket — which is
    the classical setting, where bins are the distinct values a forecaster
    emits. Binning a continuous forecast leaves a residual equal to the
    within-bucket forecast variance less twice the within-bucket
    forecast/outcome covariance. That residual is a property of the binning,
    not of the detector: it shrinks with more buckets and vanishes when each
    bucket holds one forecast value. The reason to compute the decomposition
    rather than eyeball a reliability diagram is that these three numbers are
    checkable against the score, and the tests check them.

    ``buckets`` trades bias against noise in the usual way. Ten is the
    convention and is already coarse: at report-card sample sizes several bins
    will hold two or three markets, and their observed rates are 0, 0.5 or 1.
    Reliability computed from those is dominated by noise — read it alongside
    the counts, not alone.
    """
    if buckets < 1:
        raise ValueError(f"buckets must be at least 1, got {buckets!r}")

    probs, obs = _validate_forecasts(predictions, outcomes)
    n = len(probs)

    sums: list[float] = [0.0] * buckets
    hits: list[float] = [0.0] * buckets
    counts: list[int] = [0] * buckets
    for p, o in zip(probs, obs, strict=True):
        # A forecast of exactly 1.0 belongs in the top bucket, not in a
        # nonexistent bucket[buckets].
        index = min(int(p * buckets), buckets - 1)
        sums[index] += p
        hits[index] += o
        counts[index] += 1

    base_rate = fsum(obs) / n

    reliability = 0.0
    resolution = 0.0
    for index in range(buckets):
        count = counts[index]
        if count == 0:
            continue
        mean_forecast = sums[index] / count
        observed_rate = hits[index] / count
        reliability += count * (mean_forecast - observed_rate) ** 2
        resolution += count * (observed_rate - base_rate) ** 2

    return BrierParts(
        reliability=reliability / n,
        resolution=resolution / n,
        uncertainty=base_rate * (1.0 - base_rate),
        n=n,
    )


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------


def max_drawdown(equity: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough decline in an equity curve, in cents.

    Returned as a **non-negative magnitude**: a curve that only ever rose has a
    drawdown of ``Decimal(0)``, and a curve that fell 400c off its high has a
    drawdown of ``Decimal(400)``, not ``-400``. An empty or single-point curve
    returns ``Decimal(0)`` — there is no decline to measure, which is not the
    same as having survived one.

    **Include the opening balance.** The first point is taken as the starting
    peak, so a curve built only from cumulative P&L *after each trade* has no
    "before any trade" point and the first trade's loss is silently invisible —
    off by one trade, always in the flattering direction. Prepend the opening
    equity (``Decimal(0)`` for a P&L curve).

    **The input must be ordered by time.** This is a path statistic and it is
    not order-independent: the same set of trades sorted by size, or grouped by
    market, or read back from a query with no ``ORDER BY``, produces a number
    that is arithmetically valid and describes a sequence of events that never
    happened. There is no way to detect that from the values, so there is no
    guard here — only this paragraph.
    """
    values = _validated(equity, field="equity")
    if len(values) < 2:
        return Decimal(0)

    peak = values[0]
    worst = Decimal(0)
    for value in values[1:]:
        if value > peak:
            peak = value
        decline = peak - value
        if decline > worst:
            worst = decline
    return worst


# ---------------------------------------------------------------------------
# The verdict — a fail-closed gate
# ---------------------------------------------------------------------------


class Verdict(enum.StrEnum):
    """What a detector's trade history entitles it to claim."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NO_EDGE_SHOWN = "no_edge_shown"
    EDGE_SHOWN = "edge_shown"
    LOSING = "losing"


def verdict(exp: Expectancy, *, min_trades: int) -> Verdict:
    """Decide what may be claimed. Fails closed at every ambiguity.

    - ``n < min_trades`` → ``INSUFFICIENT_EVIDENCE``, **whatever the mean is**.
      A +40c/trade mean over four trades is the most dangerous output this
      system can produce: it is the most persuasive thing a report card can
      say and it carries no information. There is deliberately no override, no
      "promising" state, and no path by which a large mean substitutes for a
      sample.
    - No interval at all (n < 2) → ``INSUFFICIENT_EVIDENCE``, even if
      ``min_trades`` were set to 1. An unquantified mean is not evidence.
    - Interval straddles zero → ``NO_EDGE_SHOWN``. Not "small edge", not
      "marginal": the data does not distinguish this detector from one that
      does nothing.
    - Interval entirely above zero → ``EDGE_SHOWN``.
    - Interval entirely below zero → ``LOSING``. Worth stating separately from
      ``NO_EDGE_SHOWN``, because it is the one verdict that is *itself* a
      finding — a reliably losing detector run backwards is a signal, and more
      usefully, it means the sample was large enough to prove something.

    A bound sitting exactly on zero counts as touching it, so the verdict is
    ``NO_EDGE_SHOWN``. This mirrors ``BucketStat.mispriced`` in the longshot
    screen: the question is whether zero is a value the data still supports,
    and on the boundary it is.
    """
    if min_trades < 1:
        raise ValueError(
            f"min_trades must be at least 1, got {min_trades!r}; a floor of "
            f"zero disables the only thing standing between a four-trade "
            f"fluke and the dashboard"
        )

    if exp.n < min_trades:
        return Verdict.INSUFFICIENT_EVIDENCE
    if exp.ci_low_cents is None or exp.ci_high_cents is None:
        return Verdict.INSUFFICIENT_EVIDENCE

    if exp.ci_low_cents > 0:
        return Verdict.EDGE_SHOWN
    if exp.ci_high_cents < 0:
        return Verdict.LOSING
    return Verdict.NO_EDGE_SHOWN


def _cents(value: Decimal) -> str:
    """Render cents for a human, always signed so the direction is unmissable."""
    return f"{value.quantize(Decimal('0.01')):+}c"


def describe(decision: Verdict, exp: Expectancy) -> str:
    """One plain-English sentence for the dashboard.

    Constrained by one rule: it must never read as an endorsement unless the
    verdict is ``EDGE_SHOWN``. A positive mean under a straddling interval is
    reported *with* the mean — hiding it would be its own kind of dishonesty,
    and the operator can see the number anyway — but the sentence always ends
    on what the evidence does not support.
    """
    level = f"{exp.confidence:.0%}"

    if decision is Verdict.INSUFFICIENT_EVIDENCE:
        if exp.n == 0:
            return "No trades — nothing to evaluate."
        trades = "trade" if exp.n == 1 else "trades"
        return (
            f"{exp.n} {trades} is too few to evaluate — insufficient evidence; "
            f"the {_cents(exp.mean_cents)}/trade mean is not reportable at this "
            f"sample size."
        )

    if exp.ci_low_cents is None or exp.ci_high_cents is None:
        # Unreachable via verdict(), which returns INSUFFICIENT_EVIDENCE
        # without an interval. Guarded anyway: a caller can build an
        # Expectancy by hand, and this function must not format a None.
        return (
            f"{_cents(exp.mean_cents)}/trade over {exp.n} trades with no "
            f"interval — insufficient evidence."
        )

    span = f"[{_cents(exp.ci_low_cents)}, {_cents(exp.ci_high_cents)}]"
    head = f"{_cents(exp.mean_cents)}/trade over {exp.n} trades"

    if decision is Verdict.EDGE_SHOWN:
        return (
            f"{head}; the {level} interval {span} is entirely above zero — "
            f"edge shown."
        )
    if decision is Verdict.LOSING:
        return (
            f"{head}; the {level} interval {span} is entirely below zero — "
            f"losing, not noise."
        )
    return f"{head}, but the {level} interval {span} includes zero — no edge shown."

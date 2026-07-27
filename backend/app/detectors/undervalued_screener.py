"""Undervalued screener: thin, wide, closing-soon markets, for a human to read.

**The dangerous version of this module is the one that calls a wide book a
mispricing.** It would take a market quoted 20/45, decide fair value is the
35c midpoint, notice the 20c bid, and report "15c of edge". That number is
manufactured out of the spread itself. An illiquid market is not mispriced,
it is *untraded* — nobody has bothered to have an opinion — and the spread
that makes it interesting to look at is the same spread you must cross to
take a position. The selection criterion *is* the cost of acting, so any
edge computed from it is circular by construction, and it would look most
attractive exactly where the book is emptiest and the fill worst.

So this module **computes no fair value, no edge, and no expected value**,
and exposes no field from which one could be read off. It emits a ranking
score that is deliberately dimensionless: an ordering for attention, not a
quantity of money. Deciding whether an untraded market is *wrong* requires a
view from outside the order book, which nothing here has.

What it does emit, per surviving market: the volume percentile, the spread in
cents, hours to close, the score, and a one-line reason a human can argue
with.

Two limitations worth stating plainly:

- **The percentile is computed across the set you pass in**, and nothing
  else. Screening a 100-market watchlist measures that watchlist, not Kalshi.
  "5th percentile volume" among your watchlist may be perfectly average for
  the exchange. There is no reference distribution here and the module does
  not pretend to one.
- **The score's absolute magnitude is meaningless.** It is a weighted blend
  of three normalised terms, scaled to 0-100 for readability. It is not
  cents, not a probability, and not a percentage of anything; a market at 70
  is not "twice as good" as one at 35. Only the ordering is intended to be
  read, and even that is a suggestion about where to spend attention.

Score composition (each term in ``[0, 1]``, weights sum to 1):

===========  ======  ==================================================
term         weight  meaning
===========  ======  ==================================================
thinness     0.40    ``1 - volume_percentile`` within the supplied set
spread       0.35    ``spread_cents / 25``, saturating at 1
urgency      0.25    ``1 - hours_to_close / max_hours_to_close``
===========  ======  ==================================================

The weights are a judgement call, not a calibration against any outcome. They
were chosen so no single term can carry a market into the top of the list on
its own, and they should be treated as arbitrary until something measures
them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "MarketSnapshot",
    "ScreenResult",
    "percentile_rank",
    "screen",
    "spread_cents",
]

HUNDRED = Decimal(100)

#: Spread at which the width term saturates. Past roughly a quarter of the
#: contract's range the book is not "wider", it is absent, and the extra cents
#: carry no additional information. Saturating also stops one absurd quote
#: from owning the top of the list.
SPREAD_SATURATION_CENTS = Decimal(25)

WEIGHT_THINNESS = 0.40
WEIGHT_SPREAD = 0.35
WEIGHT_URGENCY = 0.25

SCORE_SCALE = 100.0


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """One market as the screener sees it: quotes, turnover, time left."""

    ticker: str
    #: Contracts traded in the last 24h. Fractional to 0.01, like every other
    #: count on this exchange.
    volume_24h: Decimal
    #: Best bid/ask as YES prices in dollars, or ``None`` when that side of the
    #: book is empty. Both sides are required to screen; see `spread_cents`.
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    hours_to_close: float
    #: Carried for the human, deliberately absent from the score. Open interest
    #: counts positions outstanding, not interest in trading — a market can
    #: hold large OI from a handful of old fills and still be untradeable
    #: today, which is the opposite of what this screener is looking for.
    open_interest: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """A market worth a look. Not a signal, and not a price opinion.

    Note what is *not* here: no fair value, no edge, no EV. See the module
    docstring for why adding one would be circular.
    """

    ticker: str
    #: Rank of this market's 24h volume within the supplied set, in [0, 1].
    volume_percentile: float
    spread_cents: Decimal
    hours_to_close: float
    #: Dimensionless ordering weight in [0, 100]. Read the ranking, not the
    #: number.
    score: float
    #: Plain-language summary of why this row surfaced.
    reason: str


def spread_cents(yes_bid: Decimal | None, yes_ask: Decimal | None) -> Decimal | None:
    """Bid-ask spread in cents, or ``None`` when the book cannot be trusted.

    Refuses in three cases, all of which would otherwise produce a plausible
    number:

    - **One side missing.** A one-sided book has no spread. Substituting 0 for
      the absent side calls it maximally wide; substituting 1 calls it
      maximally tight. Both are lies, in opposite directions, and the screener
      would rank on whichever lie was chosen rather than on the market.
    - **Crossed book** (``bid > ask``). That is corrupt or mid-update data, not
      a free lunch. It is worth remembering that the screener would rank a
      crossed book at the top if the subtraction were allowed to go negative
      through a ``min_spread`` filter phrased the other way round.
    - **A price outside (0, 1).** A YES price is a dollar probability. A 0, a
      1, or a 1.40 came from somewhere other than a live book.
    """
    if yes_bid is None or yes_ask is None:
        return None
    for price in (yes_bid, yes_ask):
        # NaN raises on ordered comparison rather than comparing False, so it
        # has to be caught before the range check, not by it.
        if not price.is_finite() or not (Decimal(0) < price < Decimal(1)):
            return None
    if yes_bid > yes_ask:
        return None
    return (yes_ask - yes_bid) * HUNDRED


def percentile_rank(values: Sequence[Decimal], value: Decimal) -> float:
    """Fraction of ``values`` strictly less than ``value``, in ``[0, 1]``.

    Strict, so ties do not inflate the rank: in a set where fifty markets all
    traded zero contracts, each of them ranks at 0.0 rather than at 0.5. That
    matters here because zero-volume ties are the common case, and this
    screener's whole filter is "low percentile".

    Empty input returns **0.0** — with nothing to compare against, nothing is
    below it. That is the bottom of the range, so an empty reference set makes
    a market look maximally thin rather than maximally traded; callers that
    filter on a *low* percentile must not hand this an empty set and expect a
    refusal. `screen` builds the reference set from its own input, so a single
    market always ranks 0.0 against itself: screening a set of one measures
    nothing.

    Non-finite entries are dropped from the reference set rather than compared,
    since a Decimal NaN raises on ``<`` and would take the whole screen down
    instead of skipping one bad row.
    """
    finite = [v for v in values if v.is_finite()]
    if not finite:
        return 0.0
    if not value.is_finite():
        return 0.0
    below = sum(1 for v in finite if v < value)
    return below / len(finite)


def screen(
    markets: Sequence[MarketSnapshot],
    *,
    max_volume_percentile: float,
    min_spread_cents: Decimal,
    max_hours_to_close: float,
) -> list[ScreenResult]:
    """Rank markets that are thin, wide, and closing soon.

    ``max_volume_percentile`` is a **fraction in [0, 1]**, not a percent. A
    caller who means "the quietest 5%" and passes ``5`` would otherwise get
    every market in the set with no error, so that is rejected outright — this
    codebase has been bitten enough by unit confusion in money to not repeat it
    in a ranking.

    A non-positive ``max_hours_to_close`` returns ``[]`` rather than raising:
    "nothing closing in the next zero hours" is a coherent request with an
    empty answer, unlike a percentile expressed in the wrong unit.
    """
    # NaN fails this comparison too, which is the intent: an unusable bound is
    # a caller bug, not a row to skip.
    if not (0.0 <= max_volume_percentile <= 1.0):
        raise ValueError(
            "max_volume_percentile is a fraction in [0, 1], "
            f"got {max_volume_percentile!r}"
        )
    if not min_spread_cents.is_finite() or min_spread_cents < 0:
        raise ValueError(
            f"min_spread_cents must be finite and >= 0, got {min_spread_cents!r}"
        )
    if not markets:
        return []
    if not math.isfinite(max_hours_to_close) or max_hours_to_close <= 0:
        return []

    # The reference distribution is every volume that was handed in, including
    # markets that will be excluded below for want of a two-sided book. Those
    # are typically the thinnest names in the set, and dropping them from the
    # denominator would flatter the survivors' ranks downward — making them
    # look quieter than they are relative to their own neighbourhood.
    volumes = [m.volume_24h for m in markets if m.volume_24h.is_finite()]

    results: list[ScreenResult] = []
    for market in markets:
        result = _evaluate(
            market,
            volumes=volumes,
            max_volume_percentile=max_volume_percentile,
            min_spread_cents=min_spread_cents,
            max_hours_to_close=max_hours_to_close,
        )
        if result is not None:
            results.append(result)

    # Ties broken by ticker so the order does not depend on how the caller
    # happened to sort its query. Zero-volume ties are common here.
    results.sort(key=lambda r: (-r.score, r.ticker))
    return results


def _evaluate(
    market: MarketSnapshot,
    *,
    volumes: Sequence[Decimal],
    max_volume_percentile: float,
    min_spread_cents: Decimal,
    max_hours_to_close: float,
) -> ScreenResult | None:
    """Score one market, or refuse it. Every ``None`` below is a refusal."""
    if not market.volume_24h.is_finite() or market.volume_24h < 0:
        return None

    # A market at or past its close belongs to the resolution sniper, which
    # knows about settlement lag. Here it would only ever score maximum
    # urgency, which is precisely backwards: there is no time left to act.
    hours = market.hours_to_close
    if not math.isfinite(hours) or hours <= 0 or hours > max_hours_to_close:
        return None

    # You cannot screen what you cannot price. A market with one side of the
    # book empty is not "infinitely wide and therefore top of the list" — it is
    # unpriceable, and inventing a width for it would put the least
    # interpretable markets at the top of a list meant to direct attention.
    spread = spread_cents(market.yes_bid, market.yes_ask)
    if spread is None or spread < min_spread_cents:
        return None

    percentile = percentile_rank(volumes, market.volume_24h)
    if percentile > max_volume_percentile:
        return None

    thinness = 1.0 - percentile
    spread_term = min(1.0, float(spread / SPREAD_SATURATION_CENTS))
    urgency = 1.0 - (hours / max_hours_to_close)
    # The filters above already bound this in [0, 1); the clamp is here so the
    # score stays inside its documented range if a caller ever reuses the
    # scoring with different bounds.
    urgency = min(1.0, max(0.0, urgency))

    score = SCORE_SCALE * (
        WEIGHT_THINNESS * thinness
        + WEIGHT_SPREAD * spread_term
        + WEIGHT_URGENCY * urgency
    )

    return ScreenResult(
        ticker=market.ticker,
        volume_percentile=percentile,
        spread_cents=spread,
        hours_to_close=hours,
        score=score,
        reason=_reason(market, percentile=percentile, spread=spread, hours=hours),
    )


def _reason(
    market: MarketSnapshot,
    *,
    percentile: float,
    spread: Decimal,
    hours: float,
) -> str:
    """One line a human can disagree with.

    Phrased as observations — traded, wide, closes — with no claim about what
    the market is worth, because this module has no opinion about that.
    """
    parts = [
        f"{market.volume_24h.normalize():f} contracts traded in 24h "
        f"({percentile * 100:.0f}th pct of set)",
        f"{spread.normalize():f}c wide",
        f"closes in {hours:.1f}h",
    ]
    if market.open_interest is not None and market.open_interest.is_finite():
        parts.append(f"OI {market.open_interest.normalize():f}")
    return "; ".join(parts)

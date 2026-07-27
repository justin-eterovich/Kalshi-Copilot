"""Reading the spot series that the volatility estimator consumes.

Separate from :mod:`app.btc.vol` because the estimator is pure arithmetic and
this is the part that has to know what the database actually contains — which
is messier than the estimator's inputs suggest.

**The sampling interval is the whole point of this module.** The live poller
writes a row every three seconds; a day of it is ~28,800 observations. An EWMA
at the configured ``lambda`` of 0.94 has an effective memory of about
``1/(1-lambda)`` — roughly seventeen observations — so feeding it raw ticks
produces a volatility measured over the last *fifty seconds*, dominated by
quote noise, that will swing by multiples between one scan and the next. The
number would look like a volatility and behave like a random variable.

So everything here buckets to **one observation per minute**, taking the last
row in each bucket, regardless of how densely the source wrote. That also
makes the backfilled one-minute candles (see ``ingest.spot``) and the live
three-second ticks interchangeable as inputs, which is what lets the estimator
work at all on a cold start.

A gap in the series is left as a gap rather than interpolated. Interpolation
would invent returns of exactly zero across an outage, which drags the
estimate down precisely when the feed broke — and a feed breaking during a
violent move is not the unlikely case.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.btc.vol import ewma_volatility, log_returns, scale_volatility
from app.config import Config
from app.core.logging import get_logger
from app.db.models import ExternalPrice

log = get_logger(__name__)

__all__ = ["SigmaEstimate", "minute_closes", "horizon_sigma"]

#: The estimator's sample floor. Thirty minutes of history is not a lot, but
#: it is enough that the EWMA has converged past its seed; below it the
#: estimate is mostly the seed value.
MIN_RETURNS = 30


@dataclass(frozen=True, slots=True)
class SigmaEstimate:
    """A volatility estimate, with enough provenance to argue with it."""

    #: Sigma over the requested horizon, as a fraction of spot.
    sigma: float
    #: The underlying per-minute sigma, before scaling.
    per_minute_sigma: float
    #: How many one-minute returns went into it.
    samples: int
    #: Timestamp of the newest observation used. Consumers must check this
    #: themselves — a volatility computed from a series that stopped an hour
    #: ago is stale in the same way a stale spot price is, and just as able
    #: to manufacture an edge.
    as_of: datetime
    horizon_minutes: float


async def minute_closes(
    session: AsyncSession, *, symbol: str = "BTC-USD", minutes: int
) -> list[tuple[datetime, Decimal]]:
    """The last observation in each of the past ``minutes`` minute-buckets.

    Oldest first, which is the order :func:`log_returns` expects. Sources are
    deliberately not filtered: a backfilled candle close and a live poll are
    both "what BTC was worth in that minute", and the estimator needs an
    unbroken series more than it needs a single provenance.
    """
    if minutes <= 0:
        return []

    since = datetime.now(UTC) - timedelta(minutes=minutes)
    bucket = func.date_trunc("minute", ExternalPrice.ts)

    rows = (
        await session.execute(
            select(bucket.label("minute"), ExternalPrice.price)
            .where(ExternalPrice.symbol == symbol, ExternalPrice.ts >= since)
            # DISTINCT ON keeps the first row per bucket under the ORDER BY,
            # so ordering by ts descending within the bucket takes the close.
            .distinct(bucket)
            .order_by(bucket, ExternalPrice.ts.desc())
        )
    ).all()

    return sorted((ts, price) for ts, price in rows if price is not None)


async def horizon_sigma(
    session: AsyncSession,
    config: Config,
    *,
    minutes_to_close: float,
    symbol: str = "BTC-USD",
) -> SigmaEstimate | None:
    """Estimate sigma over the horizon a market actually has left.

    ``None`` whenever the answer would be invented: too little history, a
    degenerate series, or a non-positive horizon. Every one of those is a
    refusal rather than a fallback, because the consumer turns sigma directly
    into a fair price and a fair price built on a guessed volatility is
    indistinguishable from one built on a real one.
    """
    if minutes_to_close <= 0:
        return None

    closes = await minute_closes(
        session, symbol=symbol, minutes=config.bitcoin.vol_lookback_minutes
    )
    if len(closes) < MIN_RETURNS + 1:
        return None

    prices = [price for _, price in closes]
    try:
        returns = log_returns(prices)
    except ValueError as exc:
        # A non-positive price in the series. Corrupt data, not a quiet zero.
        log.warning("spot series for %s is unusable: %s", symbol, exc)
        return None

    per_minute = ewma_volatility(
        returns, lam=config.bitcoin.ewma_lambda, min_samples=MIN_RETURNS
    )
    if per_minute is None or per_minute <= 0:
        return None

    sigma = scale_volatility(per_minute, minutes_to_close)
    if sigma is None or sigma <= 0:
        return None

    return SigmaEstimate(
        sigma=sigma,
        per_minute_sigma=per_minute,
        samples=len(returns),
        as_of=closes[-1][0],
        horizon_minutes=minutes_to_close,
    )

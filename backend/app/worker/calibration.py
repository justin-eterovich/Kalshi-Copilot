"""Collecting the price-vs-outcome observations the calibration screen reads.

The longshot-bias question — do 5c contracts really win 5% of the time? — can
only be answered from settled markets, so this is a data-collection job whose
payoff is months away. It writes two things: an observation when a market is
first seen priced inside one of the watched bands, and the outcome when that
market eventually resolves.

**Each market contributes exactly one observation.** That is enforced by a
unique constraint and it is not a tidiness preference: a market that sits at
5c for a week, sampled every minute, would contribute ten thousand rows that
are all the same fact. The sample floor the screen refuses below would then be
met by a few dozen markets impersonating a thousand, and the confidence
interval — the entire point of using Wilson — would be computed from an `n`
that is a fiction. Resampling adds rows, not information.

The price recorded is the **midpoint of a two-sided quote**, not the last
trade. A last trade can be hours stale in exactly the thin markets this
screen is about, and a one-sided book has no midpoint at all, so those
markets are skipped rather than guessed at.

Note what this does *not* measure. Calibration is not profitability: a
contract bought at 5c must win more often than 5% of the time to cover the
fee, and nothing here accounts for that. And the observations come from
markets this system happened to be watching, so the result describes the
watchlist, not Kalshi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from app.core.logging import get_logger
from app.db.models import CalibrationLog, Market
from app.detectors.longshot_calibration import bucket_for

log = get_logger(__name__)

__all__ = ["record_observations", "backfill_outcomes", "sweep_calibration"]

HUNDRED = Decimal(100)

#: Ceiling on markets examined per sweep.
#:
#: This loop is the direct descendant of the incident CLAUDE.md records: an
#: unprojected ``select(Market)`` over 122,887 rows, each carrying the full
#: ``raw`` JSONB payload, killed the worker with no traceback. The version of
#: *this* query before it was fixed matched **152,263 rows carrying 201 MB of
#: JSONB**, and it ran every 300 seconds whether or not the detector was
#: enabled — larger than the query that already caused the outage, on a loop
#: nothing switched off.
#:
#: The band and anti-join filters below now do the real work; this cap is the
#: backstop for the case where they stop being selective.
MAX_CANDIDATES = 5_000

#: Ceiling on observations whose outcome is resolved per sweep. Unlike the
#: above this one genuinely defers work — a row not backfilled today is
#: backfilled tomorrow — so it is set high enough that the backlog drains.
MAX_BACKFILL = 5_000


def _bands(config: Config) -> tuple[tuple[int, int], tuple[int, int]]:
    cfg = config.detectors.longshot_calibration
    low = tuple(getattr(cfg, "low_band_cents", None) or (1, 10))
    high = tuple(getattr(cfg, "high_band_cents", None) or (90, 99))
    return (int(low[0]), int(low[1])), (int(high[0]), int(high[1]))


def midpoint_cents(
    yes_bid: Decimal | None, yes_ask: Decimal | None
) -> int | None:
    """Midpoint of a two-sided quote, in whole cents.

    ``None`` for a one-sided or crossed book. A missing side is not a zero and
    not a hundred; either substitution would place the market in a band it is
    not in, and the whole table is about which band a market was in.
    """
    if yes_bid is None or yes_ask is None:
        return None
    if yes_bid <= 0 or yes_ask >= 1 or yes_bid > yes_ask:
        return None
    return int(((yes_bid + yes_ask) / 2 * HUNDRED).to_integral_value())


async def record_observations(session: AsyncSession, config: Config) -> int:
    """Record markets currently priced inside a watched band.

    Only markets not already logged are considered, so this is idempotent and
    cheap to run on a loop.
    """
    low, high = _bands(config)
    now = datetime.now(UTC)

    midpoint = (Market.yes_bid + Market.yes_ask) / 2

    candidates = (
        await session.execute(
            select(
                Market.ticker,
                Market.yes_bid,
                Market.yes_ask,
                Market.close_time,
            )
            .where(
                Market.status == "active",
                Market.yes_bid.isnot(None),
                Market.yes_ask.isnot(None),
                # A one-sided or crossed book has no midpoint; midpoint_cents
                # refuses these anyway, but there is no reason to carry them
                # across the wire to be dropped in Python.
                Market.yes_bid > 0,
                Market.yes_ask < 1,
                Market.yes_bid <= Market.yes_ask,
                Market.close_time > now,
                # Only the tails are ever bucketed, so only the tails need
                # fetching. Widened by a cent on each side because the
                # authoritative midpoint is computed in Python with Decimal
                # rounding and this filter is not — a SQL band narrower than
                # the Python one would silently drop boundary observations
                # and the loss would be invisible.
                or_(
                    midpoint.between(
                        Decimal(low[0] - 1) / HUNDRED, Decimal(low[1] + 1) / HUNDRED
                    ),
                    midpoint.between(
                        Decimal(high[0] - 1) / HUNDRED, Decimal(high[1] + 1) / HUNDRED
                    ),
                ),
                # Already logged. An anti-join rather than a Python `set` of
                # every ticker ever recorded: the set grows without bound and
                # the rows it filters were fetched to be thrown away.
                ~select(CalibrationLog.id)
                .where(CalibrationLog.ticker == Market.ticker)
                .exists(),
            )
            # Each market is recorded once ever, so a cap only defers the
            # remainder to the next sweep 300s later — it never loses one.
            .limit(MAX_CANDIDATES)
        )
    ).all()

    added = 0
    for ticker, yes_bid, yes_ask, close_time in candidates:
        cents = midpoint_cents(yes_bid, yes_ask)
        if cents is None:
            continue
        bucket = bucket_for(cents, low_band=low, high_band=high)
        if bucket is None:
            continue

        hours_left: float | None = None
        if close_time is not None:
            close = close_time
            if close.tzinfo is None:
                close = close.replace(tzinfo=UTC)
            hours_left = (close - now).total_seconds() / 3600.0
            if hours_left <= 0:
                # Past close: whatever it is priced at now says nothing about
                # what the market believed while it was still a question.
                continue

        session.add(
            CalibrationLog(
                ticker=ticker,
                observed_at=now,
                price=Decimal(cents) / HUNDRED,
                price_bucket_cents=bucket,
                hours_to_close=hours_left,
            )
        )
        added += 1

    if added:
        try:
            await session.flush()
        except IntegrityError:
            # Another worker got there first. The constraint is doing its job;
            # losing this batch costs nothing since the observation exists.
            await session.rollback()
            return 0

    return added


async def backfill_outcomes(session: AsyncSession) -> int:
    """Fill in ``settled_yes`` for observations whose market has resolved.

    Projected and capped like everything else that touches ``markets``: the
    pending set only shrinks when markets settle, so on a catalog that is
    mostly open it is the *older* of the two unbounded queries here.
    """
    pending = (
        await session.execute(
            select(CalibrationLog.id, CalibrationLog.ticker)
            .where(CalibrationLog.settled_yes.is_(None))
            .order_by(CalibrationLog.id)
            .limit(MAX_BACKFILL)
        )
    ).all()
    if not pending:
        return 0

    tickers = [ticker for _, ticker in pending]
    results = dict(
        (
            await session.execute(
                select(Market.ticker, Market.result).where(Market.ticker.in_(tickers))
            )
        ).all()
    )

    now = datetime.now(UTC)
    settled_yes: list[int] = []
    settled_no: list[int] = []
    for row_id, ticker in pending:
        result = (results.get(ticker) or "").strip().lower()
        if result == "yes":
            settled_yes.append(row_id)
        elif result == "no":
            settled_no.append(row_id)
        # Anything else is unsettled, voided, or a scalar payout. A void is
        # not a "no" — the stake came back — and folding one in would bias the
        # observed rate downwards in exactly the low band the screen is about.

    for ids, outcome in ((settled_yes, True), (settled_no, False)):
        if not ids:
            continue
        # Two bulk updates rather than one per row: the rows were fetched as
        # tuples, not entities, so there is nothing to mutate in the session.
        await session.execute(
            update(CalibrationLog)
            .where(CalibrationLog.id.in_(ids))
            .values(settled_yes=outcome, settled_at=now)
        )

    return len(settled_yes) + len(settled_no)


async def sweep_calibration(
    sessions: async_sessionmaker[AsyncSession], config: Config
) -> tuple[int, int]:
    """One pass: record new observations, then resolve any that have settled."""
    async with sessions() as session:
        added = await record_observations(session, config)
        filled = await backfill_outcomes(session)
        await session.commit()

    if added or filled:
        log.info("calibration: %d new observation(s), %d settled", added, filled)
    return added, filled

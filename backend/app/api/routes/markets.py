"""Screener and market-page endpoints.

Money is serialised as strings, never floats: a price that loses precision in
JSON is a price that lies to whoever reads it next.

Candles, orderbook and tape **read through to Kalshi REST** when the local
store is empty or stale. The WebSocket needs credentials even for public
channels, but REST market data does not, so the market page works before any
API key exists and the stream simply upgrades it to real time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_scope
from app.db.models import Candle, Event, Market, OrderbookSnap, Tape
from app.ingest.backfill import VALID_INTERVALS_MIN, Backfiller

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(session_scope)]

SORTABLE = {
    "volume_24h": Market.volume_24h,
    "volume": Market.volume,
    "open_interest": Market.open_interest,
    "close_time": Market.close_time,
    "last_price": Market.last_price,
    "first_seen_at": Market.first_seen_at,
    "liquidity": Market.liquidity_dollars,
}


def _s(value: Decimal | None) -> str | None:
    """Serialise a Decimal as a string to preserve precision."""
    return None if value is None else str(value)


def _get_backfiller(request: Request) -> Backfiller | None:
    return getattr(request.app.state, "backfiller", None)


def _liquidity_score(m: Market) -> float | None:
    """Rough 0-100 tradeability score.

    Deliberately simple and readable rather than clever: a tight spread and
    real size are what make a market executable, and both are things the
    screener can see without a model. It ranks candidates for attention; it
    does not price anything.
    """
    if m.yes_bid is None or m.yes_ask is None:
        return None

    spread = float(m.yes_ask - m.yes_bid)
    # Crossed or locked books are real — a stale quote leaves bid >= ask. They
    # are not "infinitely liquid"; without the clamp the reciprocal below goes
    # negative and the score runs off the scale in both directions.
    spread_score = 1.0 / (1.0 + max(spread, 0.0) * 100)

    volume = float(m.volume_24h or 0)
    # Log-ish scaling: 100 contracts is meaningfully better than 10, but
    # 100k vs 10k matters less.
    volume_score = min(1.0, (volume / 10_000) ** 0.5) if volume > 0 else 0.0

    oi = float(m.open_interest or 0)
    oi_score = min(1.0, (oi / 10_000) ** 0.5) if oi > 0 else 0.0

    raw = 100 * (0.5 * spread_score + 0.3 * volume_score + 0.2 * oi_score)
    # Belt and braces: the score is documented as 0-100 and the UI renders it
    # as a percentage-width bar, so it must never leave the range.
    return round(min(100.0, max(0.0, raw)), 1)


def _market_row(m: Market) -> dict[str, Any]:
    spread = (
        m.yes_ask - m.yes_bid
        if m.yes_ask is not None and m.yes_bid is not None
        else None
    )
    hours_to_close = None
    if m.close_time is not None:
        hours_to_close = round(
            (m.close_time - datetime.now(UTC)).total_seconds() / 3600, 2
        )

    return {
        "ticker": m.ticker,
        "event_ticker": m.event_ticker,
        "series_ticker": m.series_ticker,
        "title": m.title,
        "yes_sub_title": m.yes_sub_title,
        "no_sub_title": m.no_sub_title,
        "category": m.category,
        "status": m.status,
        "yes_bid": _s(m.yes_bid),
        "yes_ask": _s(m.yes_ask),
        "no_bid": _s(m.no_bid),
        "no_ask": _s(m.no_ask),
        "last_price": _s(m.last_price),
        "previous_price": _s(m.previous_price),
        "spread": _s(spread),
        "volume": _s(m.volume),
        "volume_24h": _s(m.volume_24h),
        "open_interest": _s(m.open_interest),
        "liquidity_dollars": _s(m.liquidity_dollars),
        "liquidity_score": _liquidity_score(m),
        "close_time": m.close_time.isoformat() if m.close_time else None,
        "hours_to_close": hours_to_close,
        "first_seen_at": m.first_seen_at.isoformat() if m.first_seen_at else None,
    }


# ---------------------------------------------------------------------------
# Screener
# ---------------------------------------------------------------------------


@router.get("/markets")
async def list_markets(
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    category: str | None = None,
    status: str | None = "active",
    series: str | None = None,
    sort: str = "volume_24h",
    order: Literal["asc", "desc"] = "desc",
    max_spread: float | None = Query(None, description="max spread in dollars"),
    min_volume: float | None = None,
    max_hours_to_close: float | None = None,
) -> dict[str, Any]:
    """Screen the catalog."""
    if sort not in SORTABLE:
        raise HTTPException(400, f"sort must be one of {sorted(SORTABLE)}")

    filters = []
    if status:
        filters.append(Market.status == status)
    if category:
        filters.append(Market.category == category)
    if series:
        filters.append(Market.series_ticker == series)
    if q:
        pattern = f"%{q}%"
        filters.append(Market.ticker.ilike(pattern) | Market.title.ilike(pattern))
    if max_spread is not None:
        filters.append(
            (Market.yes_ask - Market.yes_bid) <= Decimal(str(max_spread))
        )
    if min_volume is not None:
        filters.append(Market.volume_24h >= Decimal(str(min_volume)))
    if max_hours_to_close is not None:
        cutoff = datetime.now(UTC).timestamp() + max_hours_to_close * 3600
        filters.append(
            Market.close_time <= datetime.fromtimestamp(cutoff, tz=UTC)
        )

    stmt = select(Market)
    count_stmt = select(func.count()).select_from(Market)
    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)

    column = SORTABLE[sort]
    stmt = (
        stmt.order_by(
            column.desc().nullslast() if order == "desc" else column.asc().nullsfirst()
        )
        .limit(limit)
        .offset(offset)
    )

    rows = (await session.execute(stmt)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "markets": [_market_row(m) for m in rows],
    }


@router.get("/categories")
async def list_categories(session: SessionDep) -> dict[str, Any]:
    """Categories with active-market counts, for the screener filter."""
    rows = (
        await session.execute(
            select(Market.category, func.count())
            .where(Market.status == "active", Market.category.isnot(None))
            .group_by(Market.category)
            .order_by(func.count().desc())
        )
    ).all()
    return {"categories": [{"category": c, "count": n} for c, n in rows]}


# ---------------------------------------------------------------------------
# Market page
# ---------------------------------------------------------------------------


@router.get("/markets/{ticker}")
async def get_market(ticker: str, session: SessionDep) -> dict[str, Any]:
    market = await session.get(Market, ticker)
    if market is None:
        raise HTTPException(404, f"market {ticker} not found in the local catalog")

    row = _market_row(market)
    row.update(
        {
            "rules_primary": market.rules_primary,
            "rules_secondary": market.rules_secondary,
            "price_level_structure": market.price_level_structure,
            "market_type": market.market_type,
            "open_time": market.open_time.isoformat() if market.open_time else None,
            "result": market.result,
            "strike_type": market.strike_type,
            "floor_strike": _s(market.floor_strike),
            "cap_strike": _s(market.cap_strike),
        }
    )

    if market.event_ticker:
        event = await session.get(Event, market.event_ticker)
        if event is not None:
            row["event"] = {
                "ticker": event.ticker,
                "title": event.title,
                "sub_title": event.sub_title,
                # Set-arbitrage only applies to exhaustive exclusive sets.
                "mutually_exclusive": event.mutually_exclusive,
            }
    return row


@router.get("/markets/{ticker}/siblings")
async def get_siblings(ticker: str, session: SessionDep) -> dict[str, Any]:
    """Other markets in the same event — the legs of a potential set-arb."""
    market = await session.get(Market, ticker)
    if market is None or not market.event_ticker:
        return {"markets": []}

    rows = (
        await session.execute(
            select(Market)
            .where(Market.event_ticker == market.event_ticker)
            .order_by(Market.last_price.desc().nullslast())
            .limit(60)
        )
    ).scalars().all()

    return {
        "event_ticker": market.event_ticker,
        "markets": [_market_row(m) for m in rows],
    }


@router.get("/markets/{ticker}/candles")
async def get_candles(
    ticker: str,
    session: SessionDep,
    request: Request,
    limit: int = Query(500, ge=1, le=5000),
    period_sec: int = Query(60),
    lookback_hours: int = Query(24, ge=1, le=24 * 90),
) -> dict[str, Any]:
    if period_sec // 60 not in VALID_INTERVALS_MIN:
        raise HTTPException(
            400,
            f"period_sec must be one of "
            f"{[m * 60 for m in VALID_INTERVALS_MIN]} (1m, 1h, 1d)",
        )

    backfiller = _get_backfiller(request)
    source = "local"
    if backfiller is not None:
        written = await backfiller.candles(
            session, ticker, period_sec=period_sec, lookback_hours=lookback_hours
        )
        if written:
            source = "kalshi"

    stmt = (
        select(Candle)
        .where(Candle.ticker == ticker, Candle.period_sec == period_sec)
        .order_by(Candle.ts.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()

    return {
        "ticker": ticker,
        "period_sec": period_sec,
        "source": source,
        "candles": [
            {
                "ts": int(c.ts.timestamp()),
                "open": _s(c.open),
                "high": _s(c.high),
                "low": _s(c.low),
                "close": _s(c.close),
                "volume": _s(c.volume),
            }
            for c in rows
        ],
    }


@router.get("/markets/{ticker}/tape")
async def get_tape(
    ticker: str,
    session: SessionDep,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    backfiller = _get_backfiller(request)
    if backfiller is not None:
        await backfiller.trades(session, ticker, limit=limit)

    rows = (
        await session.execute(
            select(Tape)
            .where(Tape.ticker == ticker)
            .order_by(Tape.ts.desc())
            .limit(limit)
        )
    ).scalars().all()

    return {
        "ticker": ticker,
        "trades": [
            {
                "ts": t.ts.isoformat(),
                "yes_price": _s(t.yes_price),
                "no_price": _s(t.no_price),
                "count": _s(t.count),
                "taker_side": t.taker_side,
            }
            for t in rows
        ],
    }


@router.get("/markets/{ticker}/orderbook")
async def get_orderbook(
    ticker: str, session: SessionDep, request: Request
) -> dict[str, Any]:
    """Current book, read through to REST when the stored snapshot is stale."""
    backfiller = _get_backfiller(request)
    if backfiller is not None:
        fresh = await backfiller.orderbook(session, ticker)
        if fresh is not None:
            return {"ticker": ticker, "source": "kalshi", **fresh}

    snap = (
        await session.execute(
            select(OrderbookSnap)
            .where(OrderbookSnap.ticker == ticker)
            .order_by(OrderbookSnap.ts.desc())
            .limit(1)
        )
    ).scalars().first()

    if snap is None:
        raise HTTPException(404, f"no orderbook available for {ticker}")

    return {
        "ticker": ticker,
        "source": "local",
        "ts": snap.ts.isoformat(),
        "seq": snap.seq,
        "yes": snap.yes_levels or [],
        "no": snap.no_levels or [],
    }


# ---------------------------------------------------------------------------
# Ingest scoreboard
# ---------------------------------------------------------------------------


@router.get("/catalog/stats")
async def catalog_stats(session: SessionDep) -> dict[str, Any]:
    now = datetime.now(UTC)

    async def count(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return (await session.execute(stmt)).scalar_one()

    newest_trade = (
        await session.execute(select(func.max(Tape.ts)))
    ).scalar_one_or_none()
    newest_candle = (
        await session.execute(select(func.max(Candle.ts)))
    ).scalar_one_or_none()

    by_category = (
        await session.execute(
            select(Market.category, func.count())
            .where(Market.status == "active")
            .group_by(Market.category)
            .order_by(func.count().desc())
            .limit(12)
        )
    ).all()

    recent_listings = (
        await session.execute(
            select(Market).order_by(Market.first_seen_at.desc()).limit(8)
        )
    ).scalars().all()

    # Markets we could not fee-price if a proposal appeared right now.
    uncategorised = await count(
        Market, Market.status == "active", Market.category.is_(None)
    )

    return {
        "markets": await count(Market),
        "markets_active": await count(Market, Market.status == "active"),
        "markets_uncategorised": uncategorised,
        "events": await count(Event),
        "candles": await count(Candle),
        "tape": await count(Tape),
        "orderbook_snaps": await count(OrderbookSnap),
        "newest_trade": newest_trade.isoformat() if newest_trade else None,
        "newest_candle": newest_candle.isoformat() if newest_candle else None,
        "trade_lag_sec": (
            round((now - newest_trade).total_seconds(), 1) if newest_trade else None
        ),
        "by_category": [
            {"category": c or "uncategorized", "count": n} for c, n in by_category
        ],
        "recent_listings": [
            {
                "ticker": m.ticker,
                "title": m.title,
                "category": m.category,
                "first_seen_at": (
                    m.first_seen_at.isoformat() if m.first_seen_at else None
                ),
            }
            for m in recent_listings
        ],
    }

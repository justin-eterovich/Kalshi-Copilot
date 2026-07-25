"""Catalog browser endpoints.

Read-only views over what ingest has stored — the proof that data is landing.
The full screener and market page arrive in M2.

Money is serialised as strings, never floats: a price that loses precision in
JSON is a price that lies to the detector reading it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_scope
from app.db.models import Candle, Event, Market, OrderbookSnap, Tape

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(session_scope)]

SORTABLE = {
    "volume_24h": Market.volume_24h,
    "volume": Market.volume,
    "open_interest": Market.open_interest,
    "close_time": Market.close_time,
    "last_price": Market.last_price,
    "first_seen_at": Market.first_seen_at,
}


def _s(value: Decimal | None) -> str | None:
    """Serialise a Decimal as a string to preserve precision."""
    return None if value is None else str(value)


def _market_row(m: Market) -> dict[str, Any]:
    spread = (
        m.yes_ask - m.yes_bid
        if m.yes_ask is not None and m.yes_bid is not None
        else None
    )
    hours_to_close = None
    if m.close_time is not None:
        delta = m.close_time - datetime.now(UTC)
        hours_to_close = round(delta.total_seconds() / 3600, 2)

    return {
        "ticker": m.ticker,
        "event_ticker": m.event_ticker,
        "series_ticker": m.series_ticker,
        "title": m.title,
        "yes_sub_title": m.yes_sub_title,
        "category": m.category,
        "status": m.status,
        "yes_bid": _s(m.yes_bid),
        "yes_ask": _s(m.yes_ask),
        "last_price": _s(m.last_price),
        "spread": _s(spread),
        "volume": _s(m.volume),
        "volume_24h": _s(m.volume_24h),
        "open_interest": _s(m.open_interest),
        "close_time": m.close_time.isoformat() if m.close_time else None,
        "hours_to_close": hours_to_close,
        "first_seen_at": m.first_seen_at.isoformat() if m.first_seen_at else None,
    }


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
) -> dict[str, Any]:
    """Browse the synced catalog."""
    if sort not in SORTABLE:
        raise HTTPException(400, f"sort must be one of {sorted(SORTABLE)}")

    stmt = select(Market)
    count_stmt = select(func.count()).select_from(Market)

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

    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)

    column = SORTABLE[sort]
    stmt = stmt.order_by(
        column.desc().nullslast() if order == "desc" else column.asc().nullsfirst()
    ).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).scalars().all()
    total = (await session.execute(count_stmt)).scalar_one()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "markets": [_market_row(m) for m in rows],
    }


@router.get("/markets/{ticker}")
async def get_market(ticker: str, session: SessionDep) -> dict[str, Any]:
    market = await session.get(Market, ticker)
    if market is None:
        raise HTTPException(404, f"market {ticker} not found in the local catalog")

    row = _market_row(market)
    row["rules_primary"] = market.rules_primary
    row["price_level_structure"] = market.price_level_structure
    row["open_time"] = market.open_time.isoformat() if market.open_time else None
    return row


@router.get("/markets/{ticker}/candles")
async def get_candles(
    ticker: str,
    session: SessionDep,
    limit: int = Query(500, ge=1, le=5000),
    period_sec: int = 60,
) -> dict[str, Any]:
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
        "candles": [
            {
                "ts": c.ts.isoformat(),
                "open": _s(c.open),
                "high": _s(c.high),
                "low": _s(c.low),
                "close": _s(c.close),
                "volume": _s(c.volume),
                "trades": c.trades,
            }
            for c in rows
        ],
    }


@router.get("/markets/{ticker}/tape")
async def get_tape(
    ticker: str,
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    stmt = (
        select(Tape)
        .where(Tape.ticker == ticker)
        .order_by(Tape.ts.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return {
        "ticker": ticker,
        "trades": [
            {
                "ts": t.ts.isoformat(),
                "yes_price": _s(t.yes_price),
                "count": _s(t.count),
                "taker_side": t.taker_side,
            }
            for t in rows
        ],
    }


@router.get("/markets/{ticker}/orderbook")
async def get_orderbook(ticker: str, session: SessionDep) -> dict[str, Any]:
    """Most recent stored book snapshot."""
    stmt = (
        select(OrderbookSnap)
        .where(OrderbookSnap.ticker == ticker)
        .order_by(OrderbookSnap.ts.desc())
        .limit(1)
    )
    snap = (await session.execute(stmt)).scalars().first()
    if snap is None:
        raise HTTPException(
            404,
            f"no orderbook snapshot stored for {ticker}. Add it to "
            f"ingest.watchlist in config.yaml for full-depth streaming.",
        )

    return {
        "ticker": ticker,
        "ts": snap.ts.isoformat(),
        "seq": snap.seq,
        "yes": snap.yes_levels,
        "no": snap.no_levels,
    }


@router.get("/catalog/stats")
async def catalog_stats(session: SessionDep) -> dict[str, Any]:
    """Ingest scoreboard — the M1 proof that data is landing."""
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
            select(Market)
            .order_by(Market.first_seen_at.desc())
            .limit(8)
        )
    ).scalars().all()

    return {
        "markets": await count(Market),
        "markets_active": await count(Market, Market.status == "active"),
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
                "first_seen_at": m.first_seen_at.isoformat() if m.first_seen_at else None,
            }
            for m in recent_listings
        ],
    }

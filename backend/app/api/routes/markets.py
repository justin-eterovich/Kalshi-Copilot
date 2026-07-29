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


def _complement(quoted: Decimal | None, other_side: Decimal | None) -> Decimal | None:
    """A NO quote, derived from the YES side when the exchange omits it.

    Kalshi quotes one book. A NO bid is the complement of the YES ask and a NO
    ask the complement of the YES bid, exactly — they are the same resting
    order described from the other side, not an approximation.

    This belongs here rather than in the browser because the browser has no
    exact arithmetic. The ticket used to derive it as ``(1 - Number(price))``
    and feed the result to ``limit_price``, which is the one place a price
    must not have passed through a float: ``toFixed(4)`` also truncated the
    two extra decimals the API supports, so a market quoting sub-cent ticks
    would have had its limit silently rounded. Deriving in ``Decimal`` and
    sending a string keeps the whole round trip exact.
    """
    if quoted is not None:
        return quoted
    if other_side is None:
        return None
    return Decimal(1) - other_side


def _get_backfiller(request: Request) -> Backfiller | None:
    return getattr(request.app.state, "backfiller", None)


#: Exactly the columns `_market_row` and `_liquidity_score` read.
#:
#: Screener queries select these rather than the whole entity. `select(Market)`
#: drags `raw` — the full API payload as JSONB — on every row, and nothing in
#: the response has ever read it: 660 KB and 0.30s for one page of 1,000. That
#: is the pattern CLAUDE.md names as the thing that killed the worker outright,
#: here on the endpoint the dashboard polls. A SQLAlchemy `Row` exposes its
#: columns as attributes, so the row builders below work unchanged.
_MARKET_COLUMNS = (
    Market.ticker,
    Market.event_ticker,
    Market.series_ticker,
    Market.title,
    Market.yes_sub_title,
    Market.no_sub_title,
    Market.category,
    Market.status,
    Market.yes_bid,
    Market.yes_ask,
    Market.no_bid,
    Market.no_ask,
    Market.last_price,
    Market.previous_price,
    Market.volume,
    Market.volume_24h,
    Market.open_interest,
    Market.liquidity_dollars,
    Market.close_time,
    Market.first_seen_at,
)


def _has_two_sided_quote(m: Any) -> bool:
    """Is anyone quoting both sides of this market right now?

    The precondition for making any tradeability claim at all, and it is asked
    as that question rather than as "are these fields populated", because those
    are not the same question and this code assumed they were.

    **A market nobody is quoting stores 0.000000, not NULL.** `_liquidity_score`
    guarded with `yes_bid is None or yes_ask is None`, which inspects the wrong
    thing and passes: 0.000000/0.000000 is two populated fields, a spread of
    exactly zero, and therefore a *full* score. Measured on the live catalog
    2026-07-29, that was **64,211 of 90,629 active markets** handed a
    tradeability number — most of them a high one, since volume and open
    interest carry half the weight and a long-dormant market can still have
    both — for a book with no bid and no ask.

    This is the tri-state trap CLAUDE.md names under "Fail closed": `''` for an
    open `Market.result`, not NULL, so `is not None` calls every open market
    settled. Same shape, same guard, same silence. Do not re-add an `is None`
    test beside this one — it is a strict subset of what is asked here, and
    having both invites the next reader to assume this one is about NULLs.

    A YES price is a dollar probability, so a live quote is strictly inside
    (0, 1): 0 means nobody will buy at any price, 1 means nobody will sell
    below a dollar. That is the same definition `_max_spread_filters` pushes
    into SQL, deliberately — the screener's spread *filter* and its liquidity
    *score* disagreeing about what counts as a quote is how one of them ends up
    ranking rows the other one hides. `TestQuoteDefinitionsAgree` in
    `tests/test_markets_screener.py` fails if they drift.

    Says nothing about whether the quote is *coherent* — a crossed book has two
    live sides that contradict each other, and is refused separately below.
    """
    return all(
        price is not None and 0 < price < 1
        for price in (m.yes_bid, m.yes_ask)
    )


def _liquidity_score(m: Any) -> float | None:
    """Rough 0-100 tradeability score, or ``None`` when there is nothing to score.

    Deliberately simple and readable rather than clever: a tight spread and
    real size are what make a market executable, and both are things the
    screener can see without a model. It ranks candidates for attention; it
    does not price anything.

    Refuses rather than guessing in two cases — no two-sided quote, and a
    crossed one. Both used to score the maximum.
    """
    if not _has_two_sided_quote(m):
        return None

    if m.yes_bid > m.yes_ask:
        # A crossed book is refused, not scored badly.
        #
        # Every finite score is a claim about tradeability and there is no true
        # one to make here. 100 says "perfectly tight" — which is what this
        # returned, because the `max(spread, 0.0)` below collapsed `bid > ask`
        # into a zero spread, the tightest value the term has. 0 says
        # "maximally wide", which a crossed book is not either. Anything
        # between says the quote was read and understood. Measured on the live
        # catalog 2026-07-29, `KXHORMUZNORM-26MAR17-B261101` quoted
        # 0.620000/0.340000 — crossed by 28c — and scored **100.0**, a full
        # green bar in a column headed LIQUIDITY.
        #
        # Nor is "give it the worst spread term" (`spread_score = 0.0`) an
        # answer: volume and open interest carry the remaining 0.5 weight, so a
        # busy crossed market would still score 50 and outrank a genuinely
        # tight quiet one. What is wrong with a crossed book is not that it is
        # wide, it is that its two sides contradict each other, so nothing
        # about it can be traded at any spread.
        #
        # Refusing is what this function already does one branch up for a
        # one-sided book, and what `_max_spread_filters`,
        # `undervalued_screener.spread_cents` and
        # `worker.calibration.midpoint_cents` all do with the same input. The
        # row carries `quote_crossed: true` alongside, so the UI can say *why*
        # the bar is blank rather than leaving the operator to guess.
        return None

    spread = float(m.yes_ask - m.yes_bid)
    # A locked book (bid == ask) deliberately keeps the full 1.0 spread term.
    # It is genuinely tight and genuinely executable — that it shares a `>=`
    # with the crossed case is an accident of arithmetic, not a shared defect,
    # and the two are now separated on purpose rather than by side effect.
    #
    # `max(spread, 0.0)` is unreachable now that crossed books return above.
    # It stays as defence, not as semantics: at a spread of exactly -0.01 the
    # denominator is 0 and this raises ZeroDivisionError. Being the *only*
    # guard is how the crossed book came to score the maximum in the first
    # place — a clamp added to keep the output in range silently doubled as the
    # answer to "how tight is this?", two lines under a comment disclaiming
    # exactly that.
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


def _max_spread_filters(max_spread: float) -> list[Any]:
    """SQL for "this market's spread is at most ``max_spread`` dollars".

    This was one condition — ``(yes_ask - yes_bid) <= max_spread`` — and it
    passed the two kinds of book that cannot be executed on at *any* spread,
    both of which then sort ahead of the real ones:

    - **A crossed book** (``bid > ask``) makes the subtraction negative, so it
      cleared every threshold including the tightest — and because the default
      sort is by 24h volume, those rows arrived *first*. Measured 2026-07-29,
      15 of the first 100 rows of ``max_spread=0.01`` were crossed, one of them
      by 28c. Crossing is a stale or broken quote, not extra liquidity.
    - **A book with no quotes at all**, which this table stores as
      ``0.000000/0.000000`` — a spread of exactly zero, i.e. tighter than
      anything real. Counted over the whole catalog the same day, that was
      **64,211 of 66,547 matching rows**: 96.5% of the result set was markets
      with no bid and no ask, against 1,638 genuinely two-sided ones and 35
      crossed. The crossed rows were the visible symptom; the phantom ones were
      the bulk.

    So these are *excluded*, not ``abs()``-ed or clamped. ``abs()`` would stop a
    crossed book outranking a real one but still answers the wrong question: an
    operator setting a max spread is asking which markets are executable within
    that spread right now, and a book crossed by half a cent is not a
    half-cent-tight market. Exclusion is also the answer the rest of the tree
    already gives — ``undervalued_screener.spread_cents`` and
    ``worker.calibration.midpoint_cents`` both refuse a crossed, one-sided or
    out-of-range book rather than transforming it, and ``spread_cents``'
    docstring names this exact failure ("the screener would rank a crossed book
    at the top if the subtraction were allowed to go negative"). The conditions
    below are that contract pushed into SQL; two definitions of "spread" in one
    codebase is how they drift.

    Note this constrains only the ``max_spread`` *query*. The screener still
    shows crossed books when nothing is filtering them out — the operator needs
    to be able to see one — it just stops serving them as the tightest quotes
    in the catalog.
    """
    return [
        # Both sides quoted. The subtraction below is NULL for a one-sided book
        # and so drops it anyway under SQL's three-valued logic; saying it keeps
        # the reason visible next to the ones that are not free.
        Market.yes_bid.isnot(None),
        Market.yes_ask.isnot(None),
        # A YES price is a dollar probability, so a real quote is strictly
        # inside (0, 1). This is what excludes the 0/0 no-book rows.
        #
        # Same definition as `_has_two_sided_quote`, which is the Python half of
        # it — kept in step by `TestQuoteDefinitionsAgree` rather than by hope,
        # since SQL expressions and Python booleans cannot share an
        # implementation.
        Market.yes_bid > 0,
        Market.yes_bid < 1,
        Market.yes_ask > 0,
        Market.yes_ask < 1,
        # Not crossed. A locked book (bid == ask) is kept: it is tight and it is
        # real, unlike the two cases above.
        Market.yes_bid <= Market.yes_ask,
        # Decimal(str(...)) rather than Decimal(float): the query parameter
        # arrives as a float and binding it directly would compare an exact
        # NUMERIC column against a binary approximation of 0.01.
        (Market.yes_ask - Market.yes_bid) <= Decimal(str(max_spread)),
    ]


def _market_row(m: Any) -> dict[str, Any]:
    spread = (
        m.yes_ask - m.yes_bid
        if m.yes_ask is not None and m.yes_bid is not None
        else None
    )
    # Decided here, in Decimal, rather than left to the browser. The client has
    # both prices, but only as strings — and it must keep them that way, so the
    # obvious `Number(bid) > Number(ask)` is exactly the float parse the whole
    # money path is built to avoid, while comparing the strings works only for
    # as long as every price arrives at the same width.
    #
    # True means the pair is not a book anyone could trade against, whatever the
    # spread arithmetic says: see `_max_spread_filters`. Both numbers should be
    # read as unreliable, not just the one that looks wrong.
    quote_crossed = (
        m.yes_bid > m.yes_ask
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
        "no_bid": _s(_complement(m.no_bid, m.yes_ask)),
        "no_ask": _s(_complement(m.no_ask, m.yes_bid)),
        "last_price": _s(m.last_price),
        "previous_price": _s(m.previous_price),
        "spread": _s(spread),
        "quote_crossed": quote_crossed,
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
        filters.extend(_max_spread_filters(max_spread))
    if min_volume is not None:
        filters.append(Market.volume_24h >= Decimal(str(min_volume)))
    if max_hours_to_close is not None:
        cutoff = datetime.now(UTC).timestamp() + max_hours_to_close * 3600
        filters.append(
            Market.close_time <= datetime.fromtimestamp(cutoff, tz=UTC)
        )

    stmt = select(*_MARKET_COLUMNS)
    count_stmt = select(func.count()).select_from(Market)
    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)

    column = SORTABLE[sort]
    stmt = (
        stmt.order_by(
            column.desc().nullslast() if order == "desc" else column.asc().nullsfirst(),
            # Tie-break on the primary key so paging is stable. Sorting by
            # `volume_24h` alone leaves thousands of rows tied at NULL or 0 in
            # no defined order, and page 2 could then repeat or skip rows from
            # page 1 for no reason the operator could see.
            Market.ticker,
        )
        .limit(limit)
        .offset(offset)
    )

    rows = (await session.execute(stmt)).all()
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
            select(*_MARKET_COLUMNS)
            .where(Market.event_ticker == market.event_ticker)
            .order_by(Market.last_price.desc().nullslast(), Market.ticker)
            .limit(60)
        )
    ).all()

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
            # Four columns, not the whole entity: this response renders four
            # fields and `raw` is the largest column in the table.
            select(
                Market.ticker,
                Market.title,
                Market.category,
                Market.first_seen_at,
            )
            .order_by(Market.first_seen_at.desc(), Market.ticker)
            .limit(8)
        )
    ).all()

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

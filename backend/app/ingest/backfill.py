"""On-demand REST backfill for candles, orderbooks and trades.

The WebSocket needs credentials even for public channels, but the REST
market-data endpoints are open. So the market page reads through to REST when
the local store is empty or stale, which means charts, depth and tape work
before any API key exists — the stream just upgrades them to real time.

Two details from the API worth knowing:

- ``end_period_ts`` on a candle is the **end** of its period, while everything
  internal keys candles by period **start** (matching the tape-built candles
  from the stream). :func:`period_start` reconciles the two.
- A candle's ``price`` OHLC is **null when no trades happened in that period**,
  which is most periods in a thin market. Falling back to the bid/ask midpoint
  is what stops illiquid markets rendering as an empty chart.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.money import parse_count, parse_dollars
from app.db.models import Candle, Market, OrderbookSnap, Tape
from app.ingest.normalize import normalize_trade
from app.kalshi.rest import KalshiApiError, KalshiRestClient

log = get_logger(__name__)

__all__ = ["Backfiller", "period_start", "normalize_candlestick"]

#: Kalshi supports exactly these candle intervals, in minutes.
VALID_INTERVALS_MIN: tuple[int, ...] = (1, 60, 1440)

#: Re-fetch a market's candles at most this often.
CANDLE_TTL = timedelta(seconds=45)
#: Orderbook snapshots are cheap and go stale fast.
BOOK_TTL = timedelta(seconds=10)
TAPE_TTL = timedelta(seconds=30)


def period_start(end_period_ts: int, period_sec: int) -> datetime:
    """Convert a candle's period *end* into its period *start*.

    Subtracting one second before flooring makes this correct whether the API
    reports an aligned end (``12:01:00``) or an inclusive last second
    (``12:00:59``) — both map to a ``12:00:00`` bucket.
    """
    aligned = ((end_period_ts - 1) // period_sec) * period_sec
    return datetime.fromtimestamp(aligned, tz=UTC)


def _maybe_price(container: Any, key: str) -> Decimal | None:
    if not isinstance(container, dict):
        return None
    value = container.get(key)
    if value in (None, ""):
        return None
    try:
        return parse_dollars(value, key)
    except ValueError:
        return None


def normalize_candlestick(
    raw: dict[str, Any], ticker: str, period_sec: int
) -> dict[str, Any] | None:
    """Map a ``MarketCandlestick`` into a ``candles`` row.

    Periods with no trades carry null price OHLC; those fall back to the
    bid/ask midpoint so the chart stays continuous instead of gapping.
    """
    end_ts = raw.get("end_period_ts")
    if end_ts is None:
        return None

    price = raw.get("price") or {}
    open_ = _maybe_price(price, "open_dollars")
    high = _maybe_price(price, "high_dollars")
    low = _maybe_price(price, "low_dollars")
    close = _maybe_price(price, "close_dollars")

    if close is None:
        # No trades this period. Use the quote midpoint so a thin market still
        # renders a line rather than a hole.
        bid = raw.get("yes_bid") or {}
        ask = raw.get("yes_ask") or {}
        bid_close = _maybe_price(bid, "close_dollars")
        ask_close = _maybe_price(ask, "close_dollars")

        if bid_close is not None and ask_close is not None:
            mid = (bid_close + ask_close) / Decimal(2)
        else:
            mid = bid_close or ask_close or _maybe_price(price, "previous_dollars")

        if mid is None:
            return None
        open_ = high = low = close = mid

    # Defensive: a period can report a close without a full OHLC set.
    open_ = open_ if open_ is not None else close
    high = high if high is not None else max(open_, close)
    low = low if low is not None else min(open_, close)

    volume = Decimal(0)
    if raw.get("volume_fp") not in (None, ""):
        try:
            volume = parse_count(raw["volume_fp"], "volume_fp")
        except ValueError:
            volume = Decimal(0)

    open_interest = None
    if raw.get("open_interest_fp") not in (None, ""):
        try:
            open_interest = parse_count(raw["open_interest_fp"], "open_interest_fp")
        except ValueError:
            open_interest = None

    return {
        "ticker": ticker,
        "ts": period_start(int(end_ts), period_sec),
        "period_sec": period_sec,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "open_interest": open_interest,
        "trades": 0,
    }


class Backfiller:
    """Read-through cache from Kalshi REST into Postgres."""

    def __init__(self, client: KalshiRestClient) -> None:
        self._client = client
        self._last_fetch: dict[tuple[str, str], datetime] = {}

    def _due(self, kind: str, ticker: str, ttl: timedelta) -> bool:
        last = self._last_fetch.get((kind, ticker))
        return last is None or datetime.now(UTC) - last >= ttl

    def _mark(self, kind: str, ticker: str) -> None:
        self._last_fetch[(kind, ticker)] = datetime.now(UTC)

    # -- candles ---------------------------------------------------------

    async def candles(
        self,
        session: AsyncSession,
        ticker: str,
        *,
        period_sec: int = 60,
        lookback_hours: int = 24,
        force: bool = False,
    ) -> int:
        """Fetch and store candles. Returns the number of rows written."""
        period_min = period_sec // 60
        if period_min not in VALID_INTERVALS_MIN:
            raise ValueError(
                f"period must be one of {VALID_INTERVALS_MIN} minutes, "
                f"got {period_min}"
            )

        if not force and not self._due("candles", ticker, CANDLE_TTL):
            return 0

        market = await session.get(Market, ticker)
        if market is None or not market.series_ticker:
            log.debug("no series ticker for %s; cannot fetch candles", ticker)
            return 0

        end_ts = int(time.time())
        start_ts = end_ts - lookback_hours * 3600

        try:
            payload = await self._client.get(
                f"/series/{market.series_ticker}/markets/{ticker}/candlesticks",
                start_ts=start_ts,
                end_ts=end_ts,
                period_interval=period_min,
            )
        except KalshiApiError as exc:
            log.warning("candle fetch failed for %s: %s", ticker, exc)
            self._mark("candles", ticker)  # do not hammer a failing endpoint
            return 0

        rows = [
            row
            for raw in (payload.get("candlesticks") or [])
            if (row := normalize_candlestick(raw, ticker, period_sec)) is not None
        ]
        self._mark("candles", ticker)

        if not rows:
            return 0

        stmt = insert(Candle).values(rows)
        await session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_candle",
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "open_interest": stmt.excluded.open_interest,
                },
            )
        )
        await session.commit()
        log.debug("backfilled %d candles for %s", len(rows), ticker)
        return len(rows)

    # -- orderbook -------------------------------------------------------

    async def orderbook(
        self, session: AsyncSession, ticker: str, *, force: bool = False
    ) -> dict[str, Any] | None:
        """Fetch the current book and store a snapshot."""
        if not force and not self._due("book", ticker, BOOK_TTL):
            return None

        try:
            book = await self._client.get_orderbook(ticker)
        except KalshiApiError as exc:
            log.warning("orderbook fetch failed for %s: %s", ticker, exc)
            self._mark("book", ticker)
            return None

        self._mark("book", ticker)

        # REST returns yes_dollars / no_dollars; the WS snapshot uses
        # yes_dollars_fp / no_dollars_fp. Accept either.
        yes = (
            book.get("yes_dollars") or book.get("yes_dollars_fp") or book.get("yes") or []
        )
        no = book.get("no_dollars") or book.get("no_dollars_fp") or book.get("no") or []

        def levels_of(raw: list[Any]) -> list[list[str]]:
            return [
                [str(lvl[0]), str(lvl[1])] for lvl in raw if lvl and len(lvl) >= 2
            ]

        levels = {"yes": levels_of(yes), "no": levels_of(no)}

        now = datetime.now(UTC)
        await session.execute(
            insert(OrderbookSnap).values(
                ticker=ticker,
                ts=now,
                seq=None,  # REST snapshots carry no sequence number
                yes_levels=levels["yes"],
                no_levels=levels["no"],
            )
        )
        await session.commit()
        # `seq` is included even though REST has none, so both this path and
        # the cached-snapshot path return the same shape to the frontend.
        return {"ts": now.isoformat(), "seq": None, **levels}

    # -- tape ------------------------------------------------------------

    async def trades(
        self,
        session: AsyncSession,
        ticker: str,
        *,
        limit: int = 200,
        force: bool = False,
    ) -> int:
        if not force and not self._due("tape", ticker, TAPE_TTL):
            return 0

        rows: list[dict[str, Any]] = []
        try:
            async for raw in self._client.get_trades(
                ticker=ticker, limit=min(limit, 1000), max_pages=1
            ):
                row = normalize_trade(raw)
                if row is not None:
                    rows.append(row)
        except KalshiApiError as exc:
            log.warning("trade fetch failed for %s: %s", ticker, exc)
            self._mark("tape", ticker)
            return 0

        self._mark("tape", ticker)
        if not rows:
            return 0

        stmt = insert(Tape).values(rows)
        await session.execute(
            stmt.on_conflict_do_nothing(constraint="uq_tape_trade_id")
        )
        await session.commit()
        return len(rows)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    async def has_candles(
        session: AsyncSession, ticker: str, period_sec: int = 60
    ) -> bool:
        result = await session.execute(
            select(func.count())
            .select_from(Candle)
            .where(Candle.ticker == ticker, Candle.period_sec == period_sec)
        )
        return result.scalar_one() > 0

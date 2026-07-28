"""WebSocket stream processing: tape, candles, orderbooks, ticker updates.

Design notes:

- **Candles are built from the tape**, not from ticker snapshots, so volume
  and OHLC reflect actual prints. Buckets are flushed once their minute has
  closed, which keeps writes bounded and makes restarts cheap.
- **Book snapshots are throttled** per market. Persisting every delta would
  write thousands of rows a second for no analytical gain; the detectors care
  about the book *now* (held in memory) and a periodic record for backtests.
- **A sequence gap invalidates every book on that subscription.** ``seq``
  counts the subscription, not the market, so the gap is detected in
  ``kalshi/ws.py`` and arrives here as a synthetic ``__resync__`` that marks
  all of them stale at once. What this module still refuses on its own is a
  delta it cannot apply — one before any snapshot, or a replayed ``seq`` —
  which puts that ticker in ``resync_needed`` rather than guessing state.
  Clearing that set is `_book_heal_loop`'s job in ``ingest/main.py`` — for
  three milestones nothing read it at all, and a book that went stale stayed
  stale for the life of the process, silently writing no snapshots.

Writes are batched and flushed on a timer so a busy market cannot turn every
message into its own transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.redis import CH_TICKS, get_redis
from app.db.models import Candle, Market, OrderbookSnap, Tape
from app.ingest.normalize import (
    TICKER_UPDATE_COLUMNS,
    normalize_ticker,
    normalize_trade,
)
from app.kalshi.orderbook import OrderBook

log = get_logger(__name__)

__all__ = ["StreamProcessor", "CandleBuilder"]

FLUSH_INTERVAL_SEC = 2.0
MAX_BATCH = 500
BOOK_SNAPSHOT_DEPTH = 10


class CandleBuilder:
    """Aggregates trades into fixed-period OHLCV buckets."""

    def __init__(self, period_sec: int = 60) -> None:
        self.period_sec = period_sec
        self._open: dict[tuple[str, datetime], dict[str, Any]] = {}

    def _bucket(self, ts: datetime) -> datetime:
        epoch = int(ts.timestamp())
        return datetime.fromtimestamp(
            epoch - (epoch % self.period_sec), tz=UTC
        )

    def add_trade(
        self, ticker: str, ts: datetime, price: Decimal, count: Decimal
    ) -> None:
        key = (ticker, self._bucket(ts))
        candle = self._open.get(key)

        if candle is None:
            self._open[key] = {
                "ticker": ticker,
                "ts": key[1],
                "period_sec": self.period_sec,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": count,
                "trades": 1,
            }
            return

        candle["high"] = max(candle["high"], price)
        candle["low"] = min(candle["low"], price)
        candle["close"] = price
        candle["volume"] += count
        candle["trades"] += 1

    def take_closed(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Remove and return buckets whose period has elapsed."""
        now = now or datetime.now(UTC)
        cutoff = self._bucket(now)

        closed = [c for key, c in self._open.items() if key[1] < cutoff]
        for key in [k for k in self._open if k[1] < cutoff]:
            del self._open[key]
        return closed

    def take_all(self) -> list[dict[str, Any]]:
        """Flush everything, including the in-progress bucket (on shutdown)."""
        out = list(self._open.values())
        self._open.clear()
        return out


class StreamProcessor:
    """Consumes decoded WebSocket messages and persists them."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        candle_period_sec: int = 60,
        book_throttle_ms: int = 1000,
    ) -> None:
        self._sessions = session_factory
        self._candles = CandleBuilder(candle_period_sec)
        self._book_throttle = timedelta(milliseconds=book_throttle_ms)

        self.books: dict[str, OrderBook] = {}
        self._last_book_write: dict[str, datetime] = {}

        self._tape_batch: list[dict[str, Any]] = []
        self._book_batch: list[dict[str, Any]] = []
        self._ticker_updates: dict[str, dict[str, Any]] = {}

        self.stats: dict[str, int] = defaultdict(int)
        #: Markets whose book needs a fresh snapshot after a gap.
        self.resync_needed: set[str] = set()

    # -- dispatch ---------------------------------------------------------

    async def handle(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        body = message.get("msg") or {}

        if msg_type == "__resync__":
            self._on_resync(message)
            return

        if msg_type == "trade":
            self._on_trade(body)
        elif msg_type == "ticker" or msg_type == "ticker_v2":
            await self._on_ticker(body)
        elif msg_type == "orderbook_snapshot":
            self._on_snapshot(body, message.get("seq"))
        elif msg_type == "orderbook_delta":
            self._on_delta(body, message.get("seq"))
        else:
            self.stats["ignored"] += 1
            return

        self.stats[str(msg_type)] += 1

    def _on_resync(self, message: dict[str, Any]) -> None:
        """Local state is untrustworthy — invalidate every book we hold."""
        reason = message.get("reason", "unknown")
        for ticker, book in self.books.items():
            book.mark_stale(reason)
            self.resync_needed.add(ticker)
        self.stats["resync"] += 1
        log.warning("resync: %s (%d books invalidated)", reason, len(self.books))

    # -- handlers ---------------------------------------------------------

    def _on_trade(self, body: dict[str, Any]) -> None:
        row = normalize_trade(body)
        if row is None:
            self.stats["trade_dropped"] += 1
            return

        self._tape_batch.append(row)
        self._candles.add_trade(
            row["ticker"], row["ts"], row["yes_price"], row["count"]
        )

    async def _on_ticker(self, body: dict[str, Any]) -> None:
        update = normalize_ticker(body)
        if update is None:
            return

        ticker = update.pop("ticker")
        update.pop("_ts", None)
        # Last write wins within a flush window; ticker updates are snapshots,
        # not increments, so collapsing them loses nothing.
        self._ticker_updates[ticker] = update

        # Never let Redis stall ingest.
        with contextlib.suppress(Exception):
            await get_redis().publish(
                CH_TICKS,
                json.dumps(
                    {"ticker": ticker, **{k: str(v) for k, v in update.items()}}
                ),
            )

    def _on_snapshot(self, body: dict[str, Any], seq: Any) -> None:
        ticker = body.get("market_ticker")
        if not ticker:
            return

        book = self.books.setdefault(ticker, OrderBook(ticker=ticker))
        book.apply_snapshot(body, int(seq) if seq is not None else None)
        self.resync_needed.discard(ticker)
        self._maybe_record_book(ticker, book)

    def _on_delta(self, body: dict[str, Any], seq: Any) -> None:
        ticker = body.get("market_ticker")
        if not ticker:
            return

        book = self.books.get(ticker)
        if book is None:
            # A delta before any snapshot: we cannot build state from it.
            self.resync_needed.add(ticker)
            return

        applied = book.apply_delta(body, int(seq) if seq is not None else None)
        if not applied:
            self.resync_needed.add(ticker)
            self.stats["book_gap"] += 1
            return

        self._maybe_record_book(ticker, book)

    def _maybe_record_book(self, ticker: str, book: OrderBook) -> None:
        """Persist a throttled top-N snapshot."""
        if book.stale:
            return

        now = datetime.now(UTC)
        last = self._last_book_write.get(ticker)
        if last is not None and now - last < self._book_throttle:
            return

        self._last_book_write[ticker] = now
        levels = book.top_n(BOOK_SNAPSHOT_DEPTH)
        self._book_batch.append(
            {
                "ticker": ticker,
                "ts": now,
                "seq": book.seq,
                "yes_levels": levels["yes"],
                "no_levels": levels["no"],
            }
        )

    # -- flushing ---------------------------------------------------------

    async def flush(self, final: bool = False) -> None:
        """Write everything buffered. Safe to call on an empty buffer."""
        candles = self._candles.take_all() if final else self._candles.take_closed()
        tape, self._tape_batch = self._tape_batch, []
        books, self._book_batch = self._book_batch, []
        tickers, self._ticker_updates = self._ticker_updates, {}

        if not any((candles, tape, books, tickers)):
            return

        async with self._sessions() as session:
            if tape:
                await self._insert_tape(session, tape)
            if candles:
                await self._insert_candles(session, candles)
            if books:
                await session.execute(insert(OrderbookSnap).values(books))
            if tickers:
                await self._update_tickers(session, tickers)
            await session.commit()

        self.stats["flushed"] += 1

    @staticmethod
    async def _insert_tape(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
        for chunk in _chunks(rows, MAX_BATCH):
            stmt = insert(Tape).values(chunk)
            # The same trade can arrive twice across a reconnect.
            await session.execute(
                stmt.on_conflict_do_nothing(constraint="uq_tape_trade_id")
            )

    @staticmethod
    async def _insert_candles(
        session: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        for chunk in _chunks(rows, MAX_BATCH):
            stmt = insert(Candle).values(chunk)
            # A bucket may be re-flushed after a restart; merge rather than
            # duplicate, keeping the widest range and largest volume seen.
            await session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_candle",
                    set_={
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume": stmt.excluded.volume,
                        "trades": stmt.excluded.trades,
                    },
                )
            )

    @staticmethod
    async def _update_tickers(
        session: AsyncSession, updates: dict[str, dict[str, Any]]
    ) -> None:
        # Every row carries the *same* columns, always, even when the ticker
        # message that produced it carried only two of them.
        #
        # `normalize_ticker` emits a variable key set — a field appears only if
        # it arrived and parsed — and the scanner subscribes every active
        # market, so a batch of heterogeneous rows is the normal case rather
        # than an edge one. A multi-row `insert().values(rows)` takes its
        # column list from the *first* row, while an `ON CONFLICT` set built
        # from the union of every row's keys can name a column the INSERT never
        # supplied. Padding to a fixed list removes that mismatch by
        # construction: there is nothing left for a union to disagree about.
        #
        # The padding is NULL, so the upsert coalesces: a column this message
        # did not carry keeps whatever is stored rather than being blanked. A
        # ticker message is a partial snapshot, never a statement that a quote
        # has gone away, so "absent" must not read as "cleared" — a NULLed
        # yes_bid would silently drop the market out of every detector query
        # that requires a two-sided book.
        rows = [
            {"ticker": t, **{c: vals.get(c) for c in TICKER_UPDATE_COLUMNS}}
            for t, vals in updates.items()
        ]
        for chunk in _chunks(rows, MAX_BATCH):
            stmt = insert(Market).values(chunk)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Market.ticker],
                    set_={
                        c: func.coalesce(stmt.excluded[c], Market.__table__.c[c])
                        for c in TICKER_UPDATE_COLUMNS
                    },
                )
            )

    async def run_flusher(self, stop: asyncio.Event) -> None:
        """Periodic flush loop; run alongside the stream consumer."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=FLUSH_INTERVAL_SEC)

            try:
                await self.flush(final=stop.is_set())
            except Exception as exc:  # noqa: BLE001 - a bad flush must not kill ingest
                log.exception("flush failed: %s", exc)


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]

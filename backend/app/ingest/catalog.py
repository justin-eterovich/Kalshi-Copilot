"""Catalog synchronisation: series, events and markets.

Two modes:

- **Full sync** on first run, walking every market via cursor pagination.
- **Incremental refresh** afterwards, using ``min_updated_ts`` so we ask only
  for what changed. The API documents that filter as incompatible with most
  others, so it is sent alone.

Newly listed markets are recorded via ``markets.first_seen_at`` and announced
on Redis. New listings are an opportunity feed in their own right: a market
nobody has looked at yet is the cheapest edge on the platform.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis import CH_SYSTEM, get_redis
from app.db.models import Event, Market, Series
from app.ingest.normalize import normalize_event, normalize_market, normalize_series
from app.kalshi.rest import KalshiRestClient

log = get_logger(__name__)

__all__ = ["CatalogSync"]

#: Upsert in batches so one huge statement never blocks the event loop.
BATCH_SIZE = 500


class CatalogSync:
    def __init__(self, client: KalshiRestClient) -> None:
        self._client = client

    # -- markets ---------------------------------------------------------

    async def sync_markets(
        self,
        session: AsyncSession,
        *,
        since: datetime | None = None,
        status: str | None = None,
        max_pages: int | None = None,
    ) -> tuple[int, int]:
        """Upsert markets. Returns ``(seen, newly_listed)``."""
        batch: list[dict[str, Any]] = []
        seen = 0
        new_tickers: list[str] = []

        # min_updated_ts is documented as incompatible with other filters.
        kwargs: dict[str, Any] = {"max_pages": max_pages}
        if since is not None:
            kwargs["min_updated_ts"] = int(since.timestamp())
        elif status is not None:
            kwargs["status"] = status

        async for raw in self._client.get_markets(**kwargs):
            row = normalize_market(raw)
            if row is None:
                continue

            seen += 1
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                new_tickers.extend(await self._upsert_markets(session, batch))
                # Commit per batch: a full sync walks tens of thousands of
                # markets, and an interrupted run should keep what it got.
                await session.commit()
                batch.clear()

        if batch:
            new_tickers.extend(await self._upsert_markets(session, batch))

        await session.commit()

        if new_tickers:
            await self._announce_new_listings(new_tickers)

        log.info(
            "catalog: %d markets seen, %d newly listed%s",
            seen,
            len(new_tickers),
            f" (since {since:%Y-%m-%d %H:%M})" if since else " (full sync)",
        )
        return seen, len(new_tickers)

    async def _upsert_markets(
        self, session: AsyncSession, rows: list[dict[str, Any]]
    ) -> list[str]:
        """Upsert a batch and return the tickers that were genuinely new.

        The previous version answered "is this new?" by loading **every
        ticker in the catalog** into a Python set on each sync — 217,258 rows
        every 300 seconds, growing with the catalog forever, to detect a
        handful of listings. The upsert already knows: Postgres exposes
        ``xmax = 0`` on a ``RETURNING`` row when that row was inserted rather
        than updated, so the answer comes back with the write for free.

        ``xmax`` is a system column and this is Postgres-specific. That is
        fine — the whole stack is Postgres/TimescaleDB — but it is the reason
        this is written out rather than left to look like ordinary SQL.
        """
        if not rows:
            return []
        stmt = insert(Market).values(rows)
        # first_seen_at is deliberately excluded: it must survive updates so
        # the "new listing" signal stays meaningful.
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in Market.__table__.columns
            if c.name not in ("ticker", "first_seen_at")
        }
        result = await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Market.ticker], set_=update_cols
            ).returning(Market.ticker, literal_column("(xmax = 0)").label("inserted"))
        )
        return [ticker for ticker, inserted in result.all() if inserted]

    async def _announce_new_listings(self, tickers: list[str]) -> None:
        try:
            await get_redis().publish(
                CH_SYSTEM,
                json.dumps(
                    {"event": "new_listings", "tickers": tickers[:100],
                     "count": len(tickers)}
                ),
            )
        except Exception as exc:  # noqa: BLE001 - never fail a sync on Redis
            log.warning("could not announce new listings: %s", exc)

    # -- events & series --------------------------------------------------

    async def sync_events(
        self, session: AsyncSession, *, max_pages: int | None = None
    ) -> int:
        batch: list[dict[str, Any]] = []
        seen = 0

        async for raw in self._client.get_events(max_pages=max_pages):
            row = normalize_event(raw)
            if row is None:
                continue
            seen += 1
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                await self._upsert(session, Event, batch, "ticker")
                await session.commit()
                batch.clear()

        if batch:
            await self._upsert(session, Event, batch, "ticker")
        await session.commit()

        log.info("catalog: %d events", seen)
        return seen

    async def sync_series_for(
        self, session: AsyncSession, series_tickers: list[str]
    ) -> int:
        """Fetch series metadata, which carries the settlement sources.

        The weather engine parses its station out of this text — it is the
        difference between modelling the right airport and the wrong one.
        """
        rows: list[dict[str, Any]] = []
        for ticker in series_tickers:
            try:
                raw = await self._client.get_series(ticker)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not fetch series %s: %s", ticker, exc)
                continue
            row = normalize_series(raw)
            if row is not None:
                rows.append(row)

        if rows:
            await self._upsert(session, Series, rows, "ticker")
            await session.commit()

        log.info("catalog: %d series", len(rows))
        return len(rows)

    @staticmethod
    async def _upsert(
        session: AsyncSession, model: Any, rows: list[dict[str, Any]], pk: str
    ) -> None:
        if not rows:
            return
        stmt = insert(model).values(rows)
        update_cols = {
            c.name: stmt.excluded[c.name]
            for c in model.__table__.columns
            if c.name != pk
        }
        await session.execute(
            stmt.on_conflict_do_update(index_elements=[pk], set_=update_cols)
        )

    # -- category propagation --------------------------------------------

    @staticmethod
    async def backfill_categories(session: AsyncSession) -> int:
        """Copy each event's category onto its markets.

        The ``/markets`` payload carries no category at all — it lives on the
        parent event — so without this step markets have none and the
        dashboard's filters, the screener's grouping and the stale-quote
        pre-filter all have nothing to work with.

        **Category does not price anything.** An earlier version of this
        docstring claimed ``fees.py`` selects the fee multiplier by category;
        it does not, and the design that did is the one that excluded ~50,000
        Crypto markets from proposals over a multiplier that does not exist.
        The fee schedule has no category dimension: it is keyed by **series
        ticker**, and ``fees.py`` calls ``series_of(ticker)``. The claim is
        recorded here as wrong because this function is the natural place
        someone would try to "restore" the behaviour.
        """
        result = await session.execute(
            text(
                """
                UPDATE markets AS m
                   SET category = e.category
                  FROM events AS e
                 WHERE m.event_ticker = e.ticker
                   AND e.category IS NOT NULL
                   AND (m.category IS NULL OR m.category <> e.category)
                """
            )
        )
        await session.commit()

        updated = result.rowcount or 0
        if updated:
            log.info("catalog: categorised %d markets from their events", updated)
        return updated

    @staticmethod
    async def uncategorised_count(session: AsyncSession) -> int:
        """Markets still lacking a category, which cannot be fee-priced."""
        result = await session.execute(
            select(func.count())
            .select_from(Market)
            .where(Market.category.is_(None), Market.status == "active")
        )
        return result.scalar_one()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    async def last_updated(session: AsyncSession) -> datetime | None:
        """Newest ``updated_time`` we hold, for incremental refresh."""
        result = await session.execute(select(func.max(Market.updated_time)))
        return result.scalar_one_or_none()

    @staticmethod
    def refresh_since(last: datetime | None) -> datetime | None:
        """Overlap the incremental window slightly to tolerate clock skew."""
        if last is None:
            return None
        return last - timedelta(minutes=5)

    @staticmethod
    async def active_tickers(session: AsyncSession, limit: int) -> list[str]:
        """Most liquid currently-active markets, for the scanner universe."""
        now = datetime.now(UTC)
        result = await session.execute(
            select(Market.ticker)
            .where(Market.status == "active", Market.close_time > now)
            .order_by(Market.volume_24h.desc().nullslast())
            .limit(limit)
        )
        return list(result.scalars().all())

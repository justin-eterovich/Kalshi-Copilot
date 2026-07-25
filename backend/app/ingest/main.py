"""Market-data ingest service.

Runs three cooperating loops:

1. **Catalog sync** — REST bootstrap then periodic incremental refresh.
2. **Stream consumer** — WebSocket tape/ticker/orderbook into Postgres.
3. **Flusher** — batched writes on a timer.

The watchlist gets full depth (orderbook deltas + tape + ticker). The wider
scanner universe gets ticker-level coverage only, which is all the screener
needs and a fraction of the bandwidth.

Without credentials the service still runs: REST public market data populates
the catalog, and the service says clearly that streaming is unavailable. The
Kalshi WebSocket requires authentication even for public channels.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.config import get_config
from app.core.logging import configure_logging, get_logger
from app.core.redis import beat, close_redis, get_redis
from app.db.base import dispose_engine, get_session_factory
from app.ingest.catalog import CatalogSync
from app.ingest.streams import StreamProcessor
from app.kalshi.client import build_rest_client, build_websocket
from app.kalshi.rest import KalshiRestClient
from app.settings import get_settings

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SEC = 15
SERVICE = "ingest"
#: Kalshi caps markets per subscription; keep full-depth sets modest.
MAX_FULL_DEPTH_MARKETS = 100


async def _heartbeat_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await beat(SERVICE)
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)


async def _catalog_loop(client: KalshiRestClient, stop: asyncio.Event) -> None:
    """Bootstrap the catalog, then refresh incrementally."""
    config = get_config()
    sessions = get_session_factory()
    sync = CatalogSync(client)
    bootstrapped = False

    while not stop.is_set():
        try:
            async with sessions() as session:
                if not bootstrapped:
                    last = await sync.last_updated(session)
                    if last is None:
                        log.info("catalog: full sync (first run, this takes a minute)")
                        await sync.sync_markets(session, status="open")
                    else:
                        log.info("catalog: resuming from %s", last)
                        await sync.sync_markets(session, since=sync.refresh_since(last))
                    await sync.sync_events(session)
                    bootstrapped = True
                else:
                    last = await sync.last_updated(session)
                    await sync.sync_markets(session, since=sync.refresh_since(last))

                # Categories live on the event, not the market, and fees are
                # priced per category — so this join has to run every cycle.
                await sync.backfill_categories(session)
                missing = await sync.uncategorised_count(session)
                if missing:
                    log.warning(
                        "%d active markets still have no category; their fees "
                        "fall back to the default multiplier",
                        missing,
                    )

        except Exception as exc:  # noqa: BLE001 - never kill the loop
            log.exception("catalog sync failed: %s", exc)

        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=config.ingest.catalog_refresh_sec
            )


async def _stream_loop(stop: asyncio.Event) -> None:
    """Consume the WebSocket into Postgres."""
    config = get_config()
    ws = build_websocket()

    if ws is None:
        log.warning(
            "streaming disabled: the Kalshi websocket requires credentials even "
            "for public channels. Catalog sync continues over REST."
        )
        return

    sessions = get_session_factory()
    processor = StreamProcessor(
        sessions,
        candle_period_sec=config.ingest.candle_interval_sec,
        book_throttle_ms=config.ingest.orderbook_snapshot_throttle_ms,
    )

    watchlist = list(config.ingest.watchlist)[:MAX_FULL_DEPTH_MARKETS]
    if watchlist:
        # Full depth for the markets we actually trade.
        ws.subscribe(["orderbook_delta", "trade", "ticker"], watchlist)
        log.info("full-depth subscriptions: %s", ", ".join(watchlist))
    else:
        log.info("watchlist empty — add tickers to config.yaml for full depth")

    if config.ingest.scanner.enabled:
        # Ticker-level coverage for everything else. Omitting market_tickers
        # subscribes to all markets, which is exactly what the screener wants.
        ws.subscribe(["ticker"])
        log.info("scanner: ticker-level coverage for all markets")

    flusher = asyncio.create_task(processor.run_flusher(stop))
    reporter = asyncio.create_task(_report_stats(processor, stop))

    try:
        async for message in ws.stream(stop):
            await processor.handle(message)
    finally:
        flusher.cancel()
        reporter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await flusher
        with contextlib.suppress(asyncio.CancelledError):
            await reporter
        await processor.flush(final=True)


async def _report_stats(processor: StreamProcessor, stop: asyncio.Event) -> None:
    """Periodic one-line summary so the logs show liveness, not silence."""
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=60)
        if stop.is_set():
            return

        stats = dict(processor.stats)
        if not stats:
            continue
        log.info(
            "stream: trades=%d ticks=%d books=%d gaps=%d resyncs=%d stale=%d",
            stats.get("trade", 0),
            stats.get("ticker", 0),
            stats.get("orderbook_delta", 0) + stats.get("orderbook_snapshot", 0),
            stats.get("book_gap", 0),
            stats.get("resync", 0),
            len(processor.resync_needed),
        )


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    log.info("ingest starting (env=%s)", settings.kalshi_env.value)
    log.info("rest=%s", settings.rest_url)
    log.info("ws=%s", settings.ws_url)

    await get_redis().ping()

    client = build_rest_client()
    log.info(
        "kalshi client ready (authenticated=%s, tier=%s)",
        client.authenticated,
        settings.kalshi_rate_tier,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(_heartbeat_loop(stop), name="heartbeat"),
        asyncio.create_task(_catalog_loop(client, stop), name="catalog"),
        asyncio.create_task(_stream_loop(stop), name="stream"),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("ingest shutting down")
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())

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
from app.ingest.news import sweep_headlines
from app.ingest.spot import fetch_spot, record_spot
from app.ingest.streams import StreamProcessor
from app.ingest.weather import backfill_actuals, sweep_weather, tracked_stations
from app.kalshi.client import build_rest_client, build_websocket
from app.kalshi.rest import KalshiRestClient
from app.kalshi.ws import KalshiWebSocket
from app.news.client import FeedClient
from app.settings import get_settings
from app.weather.client import NwsClient

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SEC = 15
SERVICE = "ingest"
#: Kalshi caps markets per subscription; keep full-depth sets modest.
MAX_FULL_DEPTH_MARKETS = 100
#: How often to check whether stale books have accumulated. Long, because the
#: remedy is a reconnect that briefly invalidates every book: healing five
#: markets is not worth costing the other seventy-five a snapshot interval.
BOOK_HEAL_INTERVAL_SEC = 300
#: How many books must be waiting before a reconnect is worth it. A handful of
#: stale markets is normal churn; a fifth of the watchlist is a leak.
BOOK_HEAL_MIN_STALE = 10
#: Spot is only useful to a detector while it is fresh, and the
#: stale-quote detector's default tolerance is seconds.
SPOT_POLL_SEC = 3
#: Feeds are other people's free servers, and an item that arrives thirty
#: seconds sooner is still an item published after the market moved.
NEWS_POLL_SEC = 300


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
        # subscribes to all markets, which is exactly what the screener wants:
        # it ranks a market against the whole exchange, and a percentile
        # computed over a subscribed subset is a percentile of that subset.
        ws.subscribe(["ticker"])
        scanner = config.ingest.scanner
        log.info("scanner: ticker-level coverage for all markets")
        if scanner.max_markets or scanner.series_filter:
            # Said out loud because the config claims otherwise. Neither key is
            # read by anything, so an operator who set `max_markets: 500` to
            # cut ingest load still gets every market on the exchange — a
            # setting that appears to work and does nothing is worse than one
            # that is absent. Which side is wrong is an operator decision
            # (subscribe narrowly, or delete the keys), so this reports rather
            # than picking one.
            log.warning(
                "ingest.scanner.max_markets=%d and series_filter=%s are NOT "
                "applied: the ticker channel is subscribed with no market "
                "list, i.e. every market on the exchange. Nothing in the "
                "backend reads either key.",
                scanner.max_markets,
                scanner.series_filter or "[]",
            )

    flusher = asyncio.create_task(processor.run_flusher(stop))
    reporter = asyncio.create_task(_report_stats(processor, stop))
    healer = asyncio.create_task(_book_heal_loop(ws, processor, stop))

    try:
        async for message in ws.stream(stop):
            await processor.handle(message)
    finally:
        for task in (flusher, reporter, healer):
            task.cancel()
        for task in (flusher, reporter, healer):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await processor.flush(final=True)


async def _book_heal_loop(
    ws: KalshiWebSocket, processor: StreamProcessor, stop: asyncio.Event
) -> None:
    """Reconnect when too many books have been stale for too long.

    A sequence gap marks a book stale, and `_maybe_record_book` then refuses
    to persist it — correctly, since a book that guesses across a gap looks
    plausible and is wrong. But nothing ever *cleared* the stale flag: the
    ticker went into `resync_needed` and stayed there, because the only thing
    that reads that set is the stats line. The module docstring claimed the
    consumer "requests a fresh snapshot"; it did not.

    Observed live: 28 of ~79 watched markets in this state, permanently, each
    contributing no orderbook snapshots at all. That is the direct cause of
    the backtester having 9.8 hours of data across 79 markets with an 85
    minute median sampling gap — the ingest was quietly recording a shrinking
    fraction of the watchlist.

    Healing is a reconnect rather than a per-market resubscribe because
    Kalshi sends a fresh snapshot on subscribe and the reconnect path is
    already exercised on every disconnect. A per-subscription resubscribe
    command would be a guess at a protocol we would not have verified.
    """
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=BOOK_HEAL_INTERVAL_SEC)
        if stop.is_set():
            return

        pending = len(processor.resync_needed)
        if pending < BOOK_HEAL_MIN_STALE:
            continue

        # Rate-limited by the interval above, so a market that Kalshi simply
        # never snapshots cannot turn this into a reconnect loop.
        await ws.force_reconnect(f"{pending} book(s) awaiting a fresh snapshot")


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


async def _spot_loop(stop: asyncio.Event) -> None:
    """Poll BTC spot into external_prices.

    Only runs when the bitcoin engine is switched on. Detectors check the
    *age* of the newest row rather than trusting it, so a poller that stalls
    degrades to "no signals" instead of "signals against a stale price".
    """
    config = get_config()
    if not config.bitcoin.enabled:
        log.info("bitcoin spot feed disabled (bitcoin.enabled=false)")
        return

    sessions = get_session_factory()
    source = config.bitcoin.spot_source
    log.info("bitcoin spot feed: %s", source)

    while not stop.is_set():
        try:
            price = await fetch_spot(source)
            async with sessions() as session:
                session.add(record_spot(source, price))
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            log.warning("spot fetch from %s failed: %s", source, exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SPOT_POLL_SEC)


async def _weather_loop(stop: asyncio.Event) -> None:
    """Poll NWS observations and forecasts for every station we trade.

    Runs on the *observation* interval and folds forecasts in on their own,
    slower schedule: a forecast is reissued a handful of times a day, so
    fetching it every five minutes would be a dozen wasted requests per
    station against a free public API for a number that has not changed.

    Every forecast issuance is kept. That history is the calibration dataset,
    and without it the engine has no measured sigma and refuses to price
    anything — which is the correct behaviour, and also useless, so the
    collection has to start long before the detector is switched on.
    """
    config = get_config()
    if not config.weather.enabled:
        log.info("weather engine disabled (weather.enabled=false)")
        return

    stations = tracked_stations()
    if not stations:
        log.warning(
            "weather enabled but no station has been asserted; nothing to poll"
        )
        return

    sessions = get_session_factory()
    log.info("weather feed: %d station(s) — %s", len(stations), ", ".join(stations))

    forecast_every = max(
        1, config.weather.refresh_forecast_sec // config.weather.refresh_observations_sec
    )
    tick = 0

    async with NwsClient(user_agent=config.weather.user_agent) as client:
        while not stop.is_set():
            want_forecast = tick % forecast_every == 0
            try:
                async with sessions() as session:
                    observed, forecasts = await sweep_weather(
                        session, client, forecasts=want_forecast
                    )
                    filled = await backfill_actuals(session)
                    await session.commit()
                if observed or forecasts or filled:
                    log.info(
                        "weather: %d observation(s), %d forecast(s), %d actual(s)",
                        observed,
                        forecasts,
                        filled,
                    )
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.warning("weather sweep failed: %s", exc)

            tick += 1
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=config.weather.refresh_observations_sec
                )


async def _news_loop(stop: asyncio.Event) -> None:
    """Poll the configured RSS feeds.

    Collection only. Nothing here scores a headline or spends money — the LLM
    triage is a separate step behind the budget guard, and it is off on this
    deployment. Storing the text is cheap and useful on its own: it is the
    record of what was public and when.
    """
    config = get_config()
    if not (config.news.enabled and config.news.headlines.enabled):
        log.info("news headlines disabled (news.headlines.enabled=false)")
        return
    if not config.news.headlines.rss_feeds:
        log.info("news enabled but no feeds configured; nothing to poll")
        return

    sessions = get_session_factory()
    log.info("news feeds: %d configured", len(config.news.headlines.rss_feeds))

    async with FeedClient(user_agent=config.news.user_agent) as client:
        while not stop.is_set():
            try:
                async with sessions() as session:
                    await sweep_headlines(session, client, config)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                log.warning("news sweep failed: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=NEWS_POLL_SEC)


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
        asyncio.create_task(_spot_loop(stop), name="spot"),
        asyncio.create_task(_weather_loop(stop), name="weather"),
        asyncio.create_task(_news_loop(stop), name="news"),
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

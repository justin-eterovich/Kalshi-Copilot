"""Market-data ingest service.

M0: process skeleton — boots, connects to its dependencies, and heartbeats so
compose can health-check it.  The Kalshi REST/WS clients and the persistence
loops land in M1.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.config import get_config
from app.core.logging import configure_logging, get_logger
from app.core.redis import beat, close_redis, get_redis
from app.db.base import dispose_engine
from app.settings import get_settings

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SEC = 15
SERVICE = "ingest"


async def _heartbeat_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await beat(SERVICE)
        except Exception as exc:
            log.warning("heartbeat failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    config = get_config()

    log.info("ingest starting (env=%s)", settings.kalshi_env.value)
    log.info("rest=%s", settings.rest_url)
    log.info("ws=%s", settings.ws_url)

    await get_redis().ping()
    log.info("redis connected")

    watchlist = config.ingest.watchlist
    log.info(
        "watchlist: %s | scanner: %s (max %d markets)",
        watchlist or "(empty — add tickers in config.yaml)",
        "on" if config.ingest.scanner.enabled else "off",
        config.ingest.scanner.max_markets,
    )
    log.info("M0 skeleton — streaming pipelines arrive in M1")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await _heartbeat_loop(stop)
    finally:
        log.info("ingest shutting down")
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())

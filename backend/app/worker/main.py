"""Detector / scanner worker.

M0: process skeleton with heartbeat.  The detector registry, risk sizing, and
proposal generation land in M4-M7.

Note that no code path in this service can place an order.  It emits signals;
the executor only acts after a human approves a proposal.
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
SERVICE = "worker"


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

    log.info("worker starting (mode=%s)", config.trading.mode)

    await get_redis().ping()
    log.info("redis connected")

    enabled = config.detectors.enabled_names()
    if enabled:
        log.info("detectors enabled: %s", ", ".join(enabled))
    else:
        log.info("no detectors enabled — enable them one at a time in config.yaml")

    if config.risk.kill_switch:
        log.warning("KILL SWITCH ENGAGED — no proposals will be generated")

    log.info("M0 skeleton — detector engine arrives in M4")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await _heartbeat_loop(stop)
    finally:
        log.info("worker shutting down")
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())

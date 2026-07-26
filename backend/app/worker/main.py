"""Detector / scanner worker.

Currently runs the execution rail's upkeep — proposal expiry, order
auto-cancel, exchange reconciliation.  The detector registry and proposal
generation land in M4-M7.

**No code path in this service can place an order.**  It expires, cancels and
reconciles; it never creates.  Detectors will emit signals here, and even
those only become proposals that a human must approve one at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.config import get_config
from app.core.logging import configure_logging, get_logger
from app.core.redis import beat, close_redis, get_redis
from app.db.base import dispose_engine, get_session_factory
from app.kalshi.client import build_rest_client
from app.settings import get_settings
from app.trading.executor import Executor
from app.trading.interlocks import InterlockError, resolve_route
from app.worker import maintenance

log = get_logger(__name__)

HEARTBEAT_INTERVAL_SEC = 15
SERVICE = "worker"
#: Proposals are short-lived (seconds), so the sweep has to be short too — a
#: TTL that is only enforced every minute is not really a TTL.
PROPOSAL_SWEEP_SEC = 5
#: Order upkeep talks to the exchange, so it runs slower and on the write
#: budget's terms.
ORDER_SWEEP_SEC = 10


async def _heartbeat_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await beat(SERVICE)
        except Exception as exc:
            log.warning("heartbeat failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)


async def _proposal_sweep_loop(stop: asyncio.Event) -> None:
    """Expire proposals whose TTL has elapsed."""
    sessions = get_session_factory()
    while not stop.is_set():
        try:
            await maintenance.sweep_proposals(sessions)
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            log.exception("proposal sweep failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=PROPOSAL_SWEEP_SEC)


async def _order_sweep_loop(executor: Executor, stop: asyncio.Event) -> None:
    """Reconcile working orders and retire the ones that have aged out."""
    sessions = get_session_factory()
    config = get_config()
    while not stop.is_set():
        try:
            await maintenance.sweep_orders(sessions, executor, config)
        except Exception as exc:  # noqa: BLE001
            log.exception("order sweep failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=ORDER_SWEEP_SEC)


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
        log.warning(
            "KILL SWITCH ENGAGED — no proposals, and working orders are being "
            "cancelled"
        )

    try:
        log.info("execution route: %s", resolve_route(settings, config).value)
    except InterlockError as exc:
        log.error("no usable execution route (%s): %s", exc.code, exc)

    client = build_rest_client()
    executor = Executor(client, settings, config)
    log.info("order maintenance ready (authenticated=%s)", client.authenticated)
    log.info("detector engine arrives in M4")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(_heartbeat_loop(stop), name="heartbeat"),
        asyncio.create_task(_proposal_sweep_loop(stop), name="proposal-sweep"),
        asyncio.create_task(_order_sweep_loop(executor, stop), name="order-sweep"),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("worker shutting down")
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())

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
from typing import Any

from app.config import get_config
from app.core.logging import configure_logging, get_logger
from app.core.redis import beat, close_redis, get_redis
from app.db.base import dispose_engine, get_session_factory
from app.detectors.base import propose_finding, record
from app.detectors.runner import (
    ResolutionSniperDetector,
    SetArbitrageDetector,
    StaleQuoteDetector,
)
from app.kalshi.client import build_rest_client
from app.settings import get_settings
from app.trading.executor import Executor
from app.trading.interlocks import InterlockError, resolve_route
from app.trading.proposals import ProposalError
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
#: Detector scans read books over REST for every leg of every watched
#: event, so they are the heaviest loop here.
DETECTOR_SCAN_SEC = 20
#: Settlement is not a fast-moving event — a market resolves once and the
#: payout does not change afterwards — so this is the slowest loop here. It
#: still has to exist, because the risk layer's loss limit is only as current
#: as the P&L it reads.
SETTLEMENT_SWEEP_SEC = 120


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


async def _settlement_loop(executor: Executor, stop: asyncio.Event) -> None:
    """Realise P&L on positions whose markets have resolved.

    Held-to-settlement is how most theses here are meant to pay off, and it
    produces no fill — without this loop the system records the cost of every
    such position and none of the proceeds.
    """
    sessions = get_session_factory()
    settings = get_settings()
    while not stop.is_set():
        try:
            await maintenance.sweep_settlements(
                sessions, executor, settings, get_config()
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("settlement sweep failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SETTLEMENT_SWEEP_SEC)


async def _detector_loop(detectors: list[Any], stop: asyncio.Event) -> None:
    """Scan with every enabled detector and record what they find.

    Detectors emit signals only. Nothing in this loop can create a proposal,
    let alone an order — that gap is the safety model, not an oversight.
    """
    sessions = get_session_factory()
    while not stop.is_set():
        config = get_config()
        active = [d for d in detectors if d.enabled(config)]
        refused: dict[str, int] = {}
        if active and not config.risk.kill_switch:
            for detector in active:
                try:
                    async with sessions() as session:
                        findings = await detector.scan(session, config)
                        for finding in findings:
                            sig = await record(session, finding)
                            # A multi-leg finding becomes one proposal, so the
                            # legs are approved together or not at all. The
                            # risk guards refuse routinely — a full queue or a
                            # duplicate is expected, not an error — so the
                            # signal is still recorded either way.
                            try:
                                await propose_finding(
                                    session, config, finding, sig
                                )
                            except ProposalError as exc:
                                refused[exc.code] = refused.get(exc.code, 0) + 1
                        await session.commit()
                    if findings:
                        log.info(
                            "%s: %d signal(s), best %.2fc/contract%s",
                            detector.name,
                            len(findings),
                            max(float(f.net_edge_cents) for f in findings),
                            (
                                f" ({', '.join(f'{n} {c}' for c, n in refused.items())})"
                                if refused
                                else ""
                            ),
                        )
                except Exception as exc:  # noqa: BLE001 - never kill the loop
                    log.exception("%s scan failed: %s", detector.name, exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=DETECTOR_SCAN_SEC)


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
    detectors: list[Any] = [
        SetArbitrageDetector(client),
        StaleQuoteDetector(client),
        ResolutionSniperDetector(),
    ]
    log.info("order maintenance ready (authenticated=%s)", client.authenticated)
    log.info(
        "detectors registered: %s",
        ", ".join(d.name for d in detectors) or "none",
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    tasks = [
        asyncio.create_task(_heartbeat_loop(stop), name="heartbeat"),
        asyncio.create_task(_proposal_sweep_loop(stop), name="proposal-sweep"),
        asyncio.create_task(_order_sweep_loop(executor, stop), name="order-sweep"),
        asyncio.create_task(_detector_loop(detectors, stop), name="detectors"),
        asyncio.create_task(_settlement_loop(executor, stop), name="settlements"),
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

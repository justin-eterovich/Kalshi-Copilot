"""Detector / scanner worker.

Runs the execution rail's upkeep — proposal expiry, order auto-cancel,
exchange reconciliation, settlement — alongside the detector registry and the
calibration collector.

**This service can place orders, and it is the only one that can do so
without a human.**  That was not true until autonomous trading was added, and
the sentence that used to be here — "no code path in this service can place an
order" — is the kind of comment that is load-bearing right up until it is
wrong.

What still constrains it:

- The autonomy sweep is the only loop that approves anything.  Expiry,
  auto-cancel, reconciliation and settlement move orders *toward* a terminal
  state and never create one.
- It approves nothing without a :class:`~app.trading.interlocks.MachineConsent`
  from ``app.trading.autonomy``, which is refused unless the route is armed in
  config *and* in the environment, the report card shows a measured edge for
  that detector on that route, backtest coverage is usable, and the hour's and
  the day's budget have room.
- Every interlock is still evaluated inside
  :meth:`~app.trading.executor.Executor.approve_and_execute`, which this
  service calls like any other caller and cannot bypass.
- Detectors still only emit signals and *pending* proposals.  The gap between
  a proposal and an order is now closable by the machine, but it is still a
  gap, and ``autonomous.min_proposal_age_sec`` is how long an operator has to
  see one before that happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from app.config import get_config
from app.core.logging import configure_logging, get_logger
from app.core.redis import beat, close_redis, get_kill_switch, get_redis
from app.db.base import dispose_engine, get_session_factory
from app.detectors.base import enabled_detector_names, propose_finding, record
from app.detectors.runner import (
    LeaderboardWatcherDetector,
    LongshotCalibrationDetector,
    ResolutionSniperDetector,
    SetArbitrageDetector,
    StaleQuoteDetector,
    UndervaluedScreenerDetector,
    WeatherDetector,
    WhaleFlowDetector,
)
from app.kalshi.client import build_rest_client
from app.settings import get_settings
from app.trading import autonomy
from app.trading.executor import Executor
from app.trading.interlocks import InterlockError, resolve_route
from app.trading.proposals import ProposalError
from app.worker import calibration, maintenance

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
#: Calibration records one observation per market, ever, and then waits for it
#: to settle. Nothing is gained by looking often — the interesting event is a
#: market appearing in a band for the first time, which is a listing, not a
#: tick.
CALIBRATION_SWEEP_SEC = 300
#: Evidence is expensive — six capped queries plus a 10k-resample bootstrap per
#: (detector, route) — and changes on the timescale of settlements, not ticks.
#: `autonomous.evidence.max_report_age_sec` defaults to three of these, so two
#: consecutive failures are survivable and three refuse.
EVIDENCE_REFRESH_SEC = autonomy.REFRESH_INTERVAL_SEC


async def _autonomy_evidence_loop(stop: asyncio.Event) -> None:
    """Keep the gate's evidence snapshot current.

    Runs whether or not autonomy is armed. The snapshot is what `/api/autonomy`
    shows an operator deciding *whether* to arm, and computing it only once
    armed would mean the decision to arm was made with no evidence in front of
    it — which is the decision that most needs some.
    """
    sessions = get_session_factory()
    while not stop.is_set():
        async with sessions() as session:
            await autonomy.refresh_once(session, get_config())
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=EVIDENCE_REFRESH_SEC)


async def _autonomy_sweep_loop(executor: Executor, stop: asyncio.Event) -> None:
    """Approve what the gate authorises.

    **This is the only loop in this service that can create an order.** Every
    other sweep moves orders toward a terminal state. It is gated all the way
    down — config, environment, route, evidence, budget — and each of those is
    re-checked inside `approve_and_execute`, which this calls like any other
    caller and cannot bypass.
    """
    sessions = get_session_factory()
    settings = get_settings()
    while not stop.is_set():
        config = get_config()
        try:
            async with sessions() as session:
                result = await autonomy.sweep(
                    session, executor, settings=settings, config=config
                )
            if result.approved or result.failed or result.disarmed:
                log.warning("autonomy sweep: %s", result.render())
            elif result.refusals:
                log.info("autonomy sweep: %s", result.render())
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            log.exception("autonomy sweep failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=config.autonomous.decision_interval_sec
            )


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


async def _calibration_loop(stop: asyncio.Event) -> None:
    """Collect price-vs-outcome observations for the longshot screen.

    Runs whether or not the detector is enabled: the screen refuses to say
    anything below 500 settled samples, and a detector that only starts
    collecting when switched on would be useless for months afterwards.
    Gathering the data is not the same as acting on it.
    """
    sessions = get_session_factory()
    while not stop.is_set():
        try:
            await calibration.sweep_calibration(sessions, get_config())
        except Exception as exc:  # noqa: BLE001
            log.exception("calibration sweep failed: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=CALIBRATION_SWEEP_SEC)


async def _detector_loop(detectors: list[Any], stop: asyncio.Event) -> None:
    """Scan with every enabled detector and record what they find.

    Detectors emit signals, and `propose_finding` turns the costable ones into
    *pending* proposals. Nothing here approves or places anything — this loop
    fills the queue and the autonomy sweep (or a person) empties it.

    The docstring here used to claim this loop could not create a proposal,
    which had not been true for several milestones: `propose_finding` is called
    below. Worth naming because the same sentence was the reason nobody looked
    at the kill-switch read underneath it.
    """
    sessions = get_session_factory()
    while not stop.is_set():
        config = get_config()
        active = [d for d in detectors if d.enabled(config)]
        # Both sources, not just the config floor.
        #
        # This read the config flag alone, which meant the runtime kill switch
        # — the only one an operator can reach on a running system — halted
        # approvals and cancelled resting orders but did not stop this loop
        # from producing more proposals. That was survivable while a human
        # stood between a proposal and an order, because the executor refused
        # them anyway. It is not survivable now: detector -> proposal ->
        # auto-approval -> order is one continuous path, and an emergency stop
        # has to break it at the first link as well as the last.
        halted = await get_kill_switch() or config.risk.kill_switch
        if active and not halted:
            for detector in active:
                # Per detector, not per cycle. Declared once outside this loop,
                # it accumulated: detector N's summary line reported the
                # refusals of detectors 1..N, so the last one in the registry
                # always looked like the one being refused.
                refused: dict[str, int] = {}
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

    enabled = enabled_detector_names(config)
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

    # Say plainly whether this process can trade without a person. Both halves
    # are named because either one alone means "no", and an operator debugging
    # why nothing is happening needs to know which one is missing.
    if config.autonomous.enabled and settings.autonomous_trading:
        armed = [
            name
            for name in ("simulated", "demo_exchange", "live_exchange")
            if getattr(config.autonomous.routes, name, False)
        ]
        log.warning(
            "AUTONOMOUS TRADING ARMED on: %s — this service can place orders "
            "with no human approval, subject to the evidence gate and budget",
            ", ".join(armed) or "no route (so nothing will fire)",
        )
    else:
        log.info(
            "autonomous trading off (config=%s, env=%s); every order needs a "
            "human approval",
            config.autonomous.enabled,
            settings.autonomous_trading,
        )

    client = build_rest_client()
    executor = Executor(client, settings, config)
    detectors: list[Any] = [
        SetArbitrageDetector(client),
        StaleQuoteDetector(client),
        ResolutionSniperDetector(),
        UndervaluedScreenerDetector(),
        WhaleFlowDetector(),
        LongshotCalibrationDetector(),
        LeaderboardWatcherDetector(),
        WeatherDetector(),
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
        asyncio.create_task(_calibration_loop(stop), name="calibration"),
        asyncio.create_task(_autonomy_evidence_loop(stop), name="autonomy-evidence"),
        asyncio.create_task(_autonomy_sweep_loop(executor, stop), name="autonomy-sweep"),
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

"""The autonomy gate: the only thing that issues a :class:`MachineConsent`.

Autonomous trading removes the human click. The click was not a formality —
until it was removed it was the **only** quality control in the trading path.
``Verdict.EDGE_SHOWN`` and ``CoverageReport.usable`` both existed, and both
were read by the dashboard and by nothing that could cause an order. So this
module's job is not to automate approval; it is to *replace a judgement with a
measurement*, and to refuse whenever the measurement is missing, stale, or
merely encouraging.

Everything here fails closed. The four questions it asks, in order of how
fundamental they are:

1. **Is the machine armed?** Config and environment, per route. Re-asked by
   :func:`~app.trading.interlocks.check_execution` afterwards and trusted from
   here by nothing.
2. **Has a human been given a chance to look?** ``min_proposal_age_sec``.
3. **Is there measured evidence for this exact (detector, route)?** The report
   card's bootstrap CI *lower bound* above zero — never the mean, never Wald.
4. **Is there budget left?** Derived from ``audit_log``, not counted.

Two things this module deliberately does not do
-----------------------------------------------
**It does not recompute evidence per decision.** ``detector_reports`` runs six
capped queries plus a 10k-resample bootstrap per pair; at a 10s decision
interval that would dominate the worker. :func:`refresh_evidence` holds a
snapshot in process and :func:`evaluate` reads only that. The snapshot is also
published to Redis for the dashboard — but the gate **never reads it back**,
so no stale key and no other process can authorise a trade. A failed refresh
leaves the previous snapshot in place to age out under
``max_report_age_sec``; it never installs a partial or empty one, because an
empty snapshot refuses identically to a genuine absence of evidence and the
operator has to be able to tell those apart.

**It does not write an audit row per refusal.** Every existing ``AuditLog``
kind is a state change. Ten refusals every ten seconds is not one, and it
would swamp the table ``/api/audit`` reads. Refusals are logged, counted, and
published live; what an operator wants is the current binding reason per pair,
not the same sentence 8,640 times a day.

The re-proposal hazard
----------------------
``proposals._guard_duplicate`` only refuses while a proposal is *pending*.
Today a human is the rate limiter and nothing in the code is. Without a
cooldown: detector proposes, the gate approves seconds later, the duplicate
guard clears, the detector re-derives the same edge on its next 20s scan, and
it approves again — repeatedly trading one market until the per-market
exposure cap finally binds. ``budget.repeat_cooldown_sec`` is the mitigation
and :func:`_check_cooldown` is where it lives. This hazard is invisible from
any single other file.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest import coverage as cov
from app.backtest import report as report_mod
from app.backtest.engine import collect_coverage
from app.backtest.stats import Verdict
from app.config import Config
from app.core.logging import get_logger
from app.core.redis import CH_SYSTEM, get_kill_switch, get_redis
from app.db.models import (
    AuditLog,
    Order,
    OrderStatus,
    Position,
    ProposalStatus,
    ProposedTrade,
)
from app.settings import Settings
from app.trading.executor import ExecutionError
from app.trading.interlocks import (
    ACTOR_AUTONOMOUS,
    ExecutionRoute,
    InterlockError,
    MachineConsent,
    resolve_route,
)

if TYPE_CHECKING:
    from app.backtest.report import DetectorReport

log = get_logger(__name__)

__all__ = [
    "GATE_VERSION",
    "Budget",
    "Evidence",
    "GateRefusal",
    "SweepResult",
    "budget_state",
    "cached_evidence",
    "disarm",
    "disarm_reason",
    "evaluate",
    "install_evidence",
    "publish_evidence",
    "published_evidence",
    "rearm",
    "refresh_evidence",
    "refresh_once",
    "sweep",
]

#: Bumped when the *meaning* of a consent changes, so an audit row from an
#: older gate is not mistaken for one this version would have issued.
GATE_VERSION: Final = "1"

#: The self-disarm latch. No TTL, for the same reason the kill switch has
#: none: a stop that times out and quietly re-arms trading is not a stop.
#: Distinct from the kill switch — that halts *everything* including manual
#: approvals and cancels resting orders; this stops only the machine.
DISARM_KEY: Final = "copilot:autonomy:disarmed"

#: Where the worker publishes its evidence snapshot **for the dashboard**.
#: The gate never reads this key. See :func:`published_evidence`.
EVIDENCE_KEY: Final = "copilot:autonomy:evidence"

#: Four refresh intervals. Long enough that one failed refresh does not blank
#: the dashboard, short enough that a dead worker stops looking current.
EVIDENCE_DISPLAY_TTL_SEC: Final = 1200

#: Coverage is measured over the same window the backtester uses by default.
COVERAGE_WINDOW: Final = timedelta(days=30)

#: Snapshot cadence. ``max_report_age_sec`` defaults to three of these, so two
#: consecutive failed refreshes are survivable and three are not.
REFRESH_INTERVAL_SEC: Final = 300

#: Order statuses that still hold exposure at the exchange.
_WORKING_STATUSES: Final = (
    OrderStatus.PENDING,
    OrderStatus.RESTING,
    OrderStatus.PARTIALLY_FILLED,
)

#: A manual ticket is a human's. The gate refuses it whatever the report card
#: says, because "manual" is not a strategy that can be measured — the rows
#: under that name are whatever an operator typed, and approving them without
#: a click would let the machine finish a trade a person started and then
#: declined to confirm.
_MANUAL_SOURCE: Final = "manual"


class GateRefusal(RuntimeError):
    """The gate refused to authorise. ``code`` is stable and machine-readable.

    Raised rather than returned as ``None``. A refusal that returns ``None``
    is not a refusal, it is a disappearance: an enabled gate that found an
    edge, sized it and then declined would be indistinguishable from one that
    found nothing. ``propose_finding`` made exactly that mistake for three
    milestones.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Evidence — computed on a slow loop, read on a fast one
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Evidence:
    """One measurement of what every detector has actually delivered.

    Immutable and replaced wholesale. A snapshot that could be mutated in
    place would let a refresh half-apply, and half a report card is not a
    weaker claim than none — it is a different one.
    """

    computed_at: datetime
    min_trades: int
    coverage_usable: bool
    coverage_refusals: tuple[str, ...]
    #: Keyed by ``(detector, route)``. Never merged across routes: a simulated
    #: fill and a demo-exchange fill are not the same evidence.
    reports: dict[tuple[str, str], DetectorReport] = field(default_factory=dict)

    def report_for(self, detector: str, route: ExecutionRoute) -> DetectorReport | None:
        return self.reports.get((detector, route.value))

    def age(self, now: datetime) -> timedelta:
        return now - self.computed_at

    def to_dict(self) -> dict[str, Any]:
        """For the dashboard. Display only — the gate never reads this back."""
        return {
            "computed_at": self.computed_at.isoformat(),
            "min_trades": self.min_trades,
            "coverage_usable": self.coverage_usable,
            "coverage_refusals": list(self.coverage_refusals),
            "pairs": [
                {
                    "detector": detector,
                    "route": route,
                    "verdict": rep.verdict.value,
                    "trades": rep.realised.n,
                    "ci_low_cents": (
                        str(rep.realised.ci_low_cents)
                        if rep.realised.ci_low_cents is not None
                        else None
                    ),
                    "mean_cents": str(rep.realised.mean_cents),
                }
                for (detector, route), rep in sorted(self.reports.items())
            ],
        }


#: The in-process snapshot. Module-level on purpose: it must not be reachable
#: from Redis, from the API, or from any request. Only `install_evidence`
#: writes it and only `cached_evidence` reads it.
_evidence: Evidence | None = None


def cached_evidence() -> Evidence | None:
    """The current snapshot, or None if no refresh has ever succeeded."""
    return _evidence


def install_evidence(evidence: Evidence | None) -> None:
    """Replace the snapshot atomically. Exposed for tests and the refresh loop."""
    global _evidence
    _evidence = evidence


async def refresh_evidence(
    session: AsyncSession,
    config: Config,
    *,
    now: datetime | None = None,
) -> Evidence:
    """Recompute the report card and coverage, and install the result.

    Raises rather than installing anything if either half fails. The caller
    logs and moves on, leaving the previous snapshot to age out — a transient
    database error must not read as "this detector has no edge", which is what
    installing an empty snapshot would mean.
    """
    now = now or datetime.now(UTC)
    min_trades = (
        config.autonomous.evidence.min_trades
        or config.backtest.report_card_min_trades
    )

    reports = await report_mod.detector_reports(
        session, min_trades=min_trades, now=now
    )
    markets = await collect_coverage(
        session, window_start=now - COVERAGE_WINDOW, window_end=now
    )
    coverage = cov.audit(
        markets,
        window_start=now - COVERAGE_WINDOW,
        window_end=now,
        expected_interval=timedelta(seconds=1),
    )

    evidence = Evidence(
        computed_at=now,
        min_trades=min_trades,
        coverage_usable=coverage.usable,
        coverage_refusals=tuple(code.value for code in coverage.refusal_codes),
        reports={(r.detector, r.route): r for r in reports},
    )
    install_evidence(evidence)
    return evidence


async def publish_evidence(evidence: Evidence) -> None:
    """Push the snapshot to the dashboard. Best-effort, and one-way.

    The gate reads :func:`cached_evidence`, never this. That split is the
    point: if authorisation could be sourced from Redis then any process able
    to write one key could authorise a trade, and a key that outlived its
    writer could authorise one after the evidence had gone.

    Both a key and a channel, because they answer different questions. The
    channel updates a dashboard that is already open; the key answers "what is
    the evidence right now?" for a page loaded later, which a pub/sub message
    cannot — a subscriber that was not listening at the moment of the publish
    never learns anything, and the API is usually not listening.
    """
    payload = json.dumps(evidence.to_dict())
    try:
        client = get_redis()
        # TTL rather than a permanent key: if the worker dies, its last report
        # card must not sit on the dashboard indefinitely looking current.
        # Generous enough that an ordinary failed refresh does not blank the
        # display, short enough that a dead writer is obvious.
        await client.set(EVIDENCE_KEY, payload, ex=EVIDENCE_DISPLAY_TTL_SEC)
        await client.publish(
            CH_SYSTEM, json.dumps({"event": "autonomy.evidence", "evidence": payload})
        )
    except Exception as exc:  # noqa: BLE001 - display only, never blocks a decision
        log.warning("could not publish autonomy evidence: %s", exc)


async def published_evidence() -> dict[str, Any] | None:
    """The last published snapshot, **for display only**.

    This exists so the API process can show an operator the numbers the worker
    measured; the two run in different processes and do not share memory. It
    is deliberately a plain dict rather than an :class:`Evidence`, so that it
    cannot be passed to :func:`evaluate` by accident — the gate's parameter is
    typed to the dataclass, and this shape simply does not fit.

    Nothing about authorisation may ever be sourced from here.
    """
    try:
        raw = await get_redis().get(EVIDENCE_KEY)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read published autonomy evidence: %s", exc)
        return None
    if not raw:
        return None
    try:
        parsed: dict[str, Any] = json.loads(raw)
    except ValueError:
        return None
    return parsed


# ---------------------------------------------------------------------------
# The disarm latch
# ---------------------------------------------------------------------------


async def disarm_reason() -> str | None:
    """Why the machine is latched off, or None if it is not.

    Fails **closed**: if Redis cannot be reached we cannot prove an operator
    has not latched it, and the safe reading of "unknown" is "stopped".
    """
    try:
        return await get_redis().get(DISARM_KEY)
    except Exception:  # noqa: BLE001
        return "redis unreachable, so the latch cannot be read"


async def disarm(reason: str) -> None:
    """Latch the machine off until a human clears it.

    A latch, not a cooldown: no TTL, and nothing here clears it. Two triggers
    fire at **one occurrence**, not at a threshold — an ambiguous submission
    (an order may exist that we cannot see, and trading on top of an unknown
    position is the worst available action) and a PARTIAL outcome (the
    executor already calls that "real, and it needs a person"; under autonomy
    it must actually get one).
    """
    log.error("autonomy disarmed: %s", reason)
    try:
        await get_redis().set(DISARM_KEY, reason)
        await get_redis().publish(
            CH_SYSTEM, json.dumps({"event": "autonomy.disarmed", "reason": reason})
        )
    except Exception as exc:  # noqa: BLE001
        # The latch failed to persist, but `disarm_reason` fails closed on the
        # same outage, so the machine still stops. Loud because the two facts
        # are only equivalent while Redis stays down.
        log.error("could not persist the disarm latch: %s", exc)


async def rearm() -> None:
    """Clear the latch. Only ever called for a human."""
    await get_redis().delete(DISARM_KEY)
    log.warning("autonomy re-armed by an operator")


# ---------------------------------------------------------------------------
# Budget — derived from the audit trail, never counted alongside it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """What the machine has already spent on one route, as the ledger says.

    Every figure comes from ``audit_log`` rows with ``kind='proposal.approved'``
    and ``actor='autonomous'``. That makes it exact across restarts, and makes
    the audit trail *the* ledger rather than a second source that can disagree
    with it. It is also why ``actor`` is derived inside the executor rather
    than accepted from a caller: it is not a label, it is the index.

    Note the direction of the write — ``proposal.approved`` is recorded
    *before* placement, so a failing placement burns budget rather than
    retrying forever. Conservative, and deliberate.
    """

    trades_this_hour: int
    detector_trades_this_hour: int
    daily_risk_cents: Decimal
    open_positions: int
    working_orders: int
    #: When the repeat cooldown on this (detector, ticker) expires, if it is
    #: running. None means no autonomous trade has touched that pair.
    cooldown_until: datetime | None

    def as_payload(self) -> dict[str, str]:
        """Flattened onto the consent, so the audit row says why it was allowed."""
        return {
            "trades_this_hour": str(self.trades_this_hour),
            "detector_trades_this_hour": str(self.detector_trades_this_hour),
            "daily_risk_cents": str(self.daily_risk_cents),
            "open_positions": str(self.open_positions),
            "working_orders": str(self.working_orders),
            "cooldown_until": (
                self.cooldown_until.isoformat() if self.cooldown_until else ""
            ),
        }


def _approved_rows(route: ExecutionRoute) -> Any:
    """Base predicate for autonomous approvals on one route."""
    return (
        (AuditLog.kind == "proposal.approved")
        & (AuditLog.actor == ACTOR_AUTONOMOUS)
        & (AuditLog.payload["route"].astext == route.value)
    )


async def budget_state(
    session: AsyncSession,
    *,
    route: ExecutionRoute,
    detector: str,
    ticker: str,
    now: datetime | None = None,
) -> Budget:
    """Measure what has already been spent. Pure read; decides nothing."""
    now = now or datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    base = _approved_rows(route)

    trades_hour = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(base, AuditLog.ts >= hour_ago)
    )
    detector_hour = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            base,
            AuditLog.ts >= hour_ago,
            AuditLog.payload["detector"].astext == detector,
        )
    )
    # Summed in SQL so the day's risk never depends on how many rows Python
    # happened to fetch. `max_loss_cents` is written as a string, hence the
    # cast — JSONB has no numeric type that survives a Decimal faithfully.
    daily_risk = await session.scalar(
        select(
            func.coalesce(
                func.sum(cast(AuditLog.payload["max_loss_cents"].astext, Numeric(20, 6))),
                0,
            )
        ).where(base, AuditLog.ts >= day_start)
    )
    last_same = await session.scalar(
        select(func.max(AuditLog.ts)).where(
            base,
            AuditLog.payload["detector"].astext == detector,
            AuditLog.ticker == ticker,
        )
    )
    open_positions = await session.scalar(
        select(func.count())
        .select_from(Position)
        .where(Position.route == route.value, Position.net_contracts != 0)
    )
    working = await session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.route == route.value, Order.status.in_(_WORKING_STATUSES))
    )

    return Budget(
        trades_this_hour=int(trades_hour or 0),
        detector_trades_this_hour=int(detector_hour or 0),
        daily_risk_cents=Decimal(str(daily_risk or 0)),
        open_positions=int(open_positions or 0),
        working_orders=int(working or 0),
        cooldown_until=_as_utc(last_same) if last_same else None,
    )


def _as_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def evaluate(
    session: AsyncSession,
    proposal: ProposedTrade,
    *,
    settings: Settings,
    config: Config,
    route: ExecutionRoute,
    evidence: Evidence | None = None,
    now: datetime | None = None,
) -> MachineConsent:
    """Authorise one proposal on one route, or raise :class:`GateRefusal`.

    ``evidence`` defaults to the in-process snapshot. It is a parameter only
    so tests can supply one; nothing in the running system passes it, and in
    particular nothing passes one sourced from Redis.
    """
    now = now or datetime.now(UTC)
    evidence = evidence if evidence is not None else cached_evidence()
    autonomy = config.autonomous

    # -- is the machine armed at all? ------------------------------------
    # Duplicated by `check_execution` afterwards, deliberately. This copy
    # stops the gate doing work it has no authority to do; that copy is what
    # actually protects the order, and neither is redundant with the other.
    if not autonomy.enabled:
        raise GateRefusal(
            "autonomy_disabled", "autonomous.enabled is false in config.yaml."
        )
    if not settings.autonomous_trading:
        raise GateRefusal(
            "autonomous_not_armed",
            "AUTONOMOUS_TRADING is not set in the environment. Config alone "
            "cannot arm the machine — that half is outside the dashboard's "
            "reach on purpose, because the dashboard has no auth in front of it.",
        )
    if not getattr(autonomy.routes, route.value, False):
        raise GateRefusal(
            "autonomous_route_not_armed",
            f"the machine is not armed on route {route.value}. Proving an edge "
            "on the simulator says nothing about the exchange, and vice versa.",
        )
    if route is ExecutionRoute.LIVE_EXCHANGE and not settings.autonomous_live_armed:
        raise GateRefusal(
            "autonomous_live_not_armed",
            "machine-driven live trading needs KALSHI_ENV=prod, LIVE_TRADING=true "
            "and AUTONOMOUS_TRADING=true together.",
        )

    latched = await disarm_reason()
    if latched:
        raise GateRefusal(
            "disarmed",
            f"the machine is latched off and needs a human to clear it: {latched}. "
            "This is not the kill switch — manual approvals still work.",
        )

    # -- has a person had the chance to veto? -----------------------------
    detector = (proposal.source or "").strip()
    if detector == _MANUAL_SOURCE or not detector:
        raise GateRefusal(
            "manual_proposal",
            "this proposal was typed by a person, not produced by a detector. "
            "There is no report card for a hand-written ticket, and finishing "
            "one a human declined to confirm is the opposite of the intent.",
        )

    created = _as_utc(proposal.created_at) if proposal.created_at else now
    age = (now - created).total_seconds()
    if age < autonomy.min_proposal_age_sec:
        raise GateRefusal(
            "veto_window_open",
            f"proposal {proposal.id} is {age:.0f}s old and the operator's veto "
            f"window is {autonomy.min_proposal_age_sec}s. The dashboard is "
            "counting it down.",
        )

    # -- is there measured evidence? --------------------------------------
    report = _check_evidence(evidence, config, detector, route, now)

    # -- is there budget? --------------------------------------------------
    budget = await budget_state(
        session, route=route, detector=detector, ticker=proposal.ticker, now=now
    )
    _check_budget(budget, config, proposal, now)

    realised = report.realised
    return MachineConsent(
        proposal_id=proposal.id,
        route=route,
        detector=detector,
        verdict=report.verdict.value,
        trades=realised.n,
        ci_low_cents=(
            str(realised.ci_low_cents) if realised.ci_low_cents is not None else None
        ),
        ci_high_cents=(
            str(realised.ci_high_cents) if realised.ci_high_cents is not None else None
        ),
        mean_cents=str(realised.mean_cents),
        min_trades=evidence.min_trades if evidence else 0,
        coverage_usable=evidence.coverage_usable if evidence else False,
        coverage_refusals=evidence.coverage_refusals if evidence else (),
        evidence_computed_at=evidence.computed_at if evidence else now,
        budget=budget.as_payload(),
        issued_at=now,
        gate_version=GATE_VERSION,
    )


def _check_evidence(
    evidence: Evidence | None,
    config: Config,
    detector: str,
    route: ExecutionRoute,
    now: datetime,
) -> DetectorReport:
    """The measurement that stands in for the human's judgement."""
    rules = config.autonomous.evidence

    if evidence is None:
        raise GateRefusal(
            "no_evidence",
            "no report card snapshot has been computed yet. The refresh loop "
            "runs every 300s; until one succeeds the machine has measured "
            "nothing and cannot claim otherwise.",
        )

    # Staleness refuses but does **not** latch. Latching on a transient
    # failure makes a human clear a condition that clears itself, which trains
    # them to clear latches.
    age = evidence.age(now).total_seconds()
    if age > rules.max_report_age_sec:
        raise GateRefusal(
            "evidence_stale",
            f"the evidence snapshot is {age:.0f}s old and the limit is "
            f"{rules.max_report_age_sec}s. Refusing rather than trading on a "
            "report card that may predate the trades it is meant to describe.",
        )

    if rules.require_coverage_usable and not evidence.coverage_usable:
        raise GateRefusal(
            "coverage_unusable",
            "backtest coverage does not support a conclusion: "
            f"{', '.join(evidence.coverage_refusals) or 'refused'}. Waiving "
            "this is permitted on the simulated route only.",
        )

    report = evidence.report_for(detector, route)
    if report is None:
        raise GateRefusal(
            "no_report",
            f"the report card has no row for {detector} on {route.value}. "
            "Evidence from another route does not transfer.",
        )

    if report.realised.n < evidence.min_trades:
        raise GateRefusal(
            "insufficient_trades",
            f"{detector} has {report.realised.n} decisions on {route.value} and "
            f"the floor is {evidence.min_trades}. A flattering mean over four "
            "trades is the most persuasive thing this system can produce and it "
            "carries no information.",
        )

    if rules.require_edge_shown and report.verdict is not Verdict.EDGE_SHOWN:
        raise GateRefusal(
            "no_edge_shown",
            f"{detector} on {route.value} reads {report.verdict.value}, not "
            "edge_shown.",
        )

    # The lower bound, explicitly — never the mean. A binary trade's P&L is a
    # two-point distribution skewed away from 50c, which is exactly the regime
    # a 20-trade report card lives in: measured, Wald claimed an edge on 29
    # wins and one loss where the bootstrap refused. `Verdict.EDGE_SHOWN`
    # already encodes this, and it is re-asserted here because the verdict is
    # one field and this is the property that must hold.
    low = report.realised.ci_low_cents
    if low is None or low <= 0:
        raise GateRefusal(
            "edge_not_measured",
            f"{detector} on {route.value} has a confidence interval lower bound "
            f"of {low if low is not None else 'none'}, which does not exclude "
            "zero. The mean is not consulted here on purpose.",
        )

    return report


def _check_budget(
    budget: Budget, config: Config, proposal: ProposedTrade, now: datetime
) -> None:
    """Ceilings. **Every one defaults to zero and zero refuses.**

    There is no way to express "unlimited" — same convention as
    ``news.headlines.daily_budget_usd``. A blank or half-written config must
    not be able to spend anything, and a ceiling that read 0 as "no limit"
    would make the most conservative-looking config the most dangerous one.
    """
    limits = config.autonomous.budget

    if budget.trades_this_hour >= limits.max_trades_per_hour:
        raise GateRefusal(
            "hourly_trade_cap",
            f"{budget.trades_this_hour} autonomous trades in the last hour "
            f"against a cap of {limits.max_trades_per_hour}. Zero means no "
            "allowance, never unlimited.",
        )

    if budget.detector_trades_this_hour >= limits.max_trades_per_detector_per_hour:
        raise GateRefusal(
            "detector_hourly_cap",
            f"this detector has {budget.detector_trades_this_hour} trades in the "
            f"last hour against a cap of "
            f"{limits.max_trades_per_detector_per_hour}.",
        )

    risk = Decimal(str(proposal.max_loss_cents or 0))
    projected = budget.daily_risk_cents + risk
    if projected > limits.max_daily_risk_cents:
        raise GateRefusal(
            "daily_risk_cap",
            f"this would take today's autonomous risk to {projected}c against a "
            f"ceiling of {limits.max_daily_risk_cents}c.",
        )

    if budget.open_positions >= limits.max_open_positions:
        raise GateRefusal(
            "open_position_cap",
            f"{budget.open_positions} open markets on this route against a cap "
            f"of {limits.max_open_positions}. Counted from any source, not just "
            "autonomous ones — a position does not remember who built it.",
        )

    if budget.working_orders >= limits.max_working_orders:
        raise GateRefusal(
            "working_order_cap",
            f"{budget.working_orders} working orders on this route against a cap "
            f"of {limits.max_working_orders}.",
        )

    _check_cooldown(budget, limits.repeat_cooldown_sec, proposal, now)


def _check_cooldown(
    budget: Budget, cooldown_sec: int, proposal: ProposedTrade, now: datetime
) -> None:
    """The mitigation for the re-proposal loop described in the module docstring.

    Without this the gate and the detector form a feedback loop that trades one
    market until the per-market exposure cap binds, because the duplicate guard
    clears the moment a proposal stops being pending.
    """
    if cooldown_sec <= 0 or budget.cooldown_until is None:
        return
    elapsed = (now - budget.cooldown_until).total_seconds()
    if elapsed < cooldown_sec:
        raise GateRefusal(
            "repeat_cooldown",
            f"this detector traded {proposal.ticker} {elapsed:.0f}s ago and the "
            f"cooldown is {cooldown_sec}s. The detector re-derives the same edge "
            "on every scan; without this it would trade it on every scan too.",
        )


# ---------------------------------------------------------------------------
# The refresh loop's body
# ---------------------------------------------------------------------------


async def refresh_once(session: AsyncSession, config: Config) -> Evidence | None:
    """One evidence refresh, swallowing failure by design.

    Returns the new snapshot, or None when the refresh failed — in which case
    the previous one is deliberately left in place to age out. Returning None
    rather than raising is right *here* and wrong in :func:`evaluate`: this is
    a background loop whose only caller would log and continue anyway, while
    there the absence of an answer must not look like permission.
    """
    try:
        evidence = await refresh_evidence(session, config)
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - a refresh failure must not stop the loop
        previous = cached_evidence()
        log.warning(
            "autonomy evidence refresh failed (%s); keeping the %s snapshot",
            exc,
            "previous" if previous else "absent",
        )
        return None
    await publish_evidence(evidence)
    return evidence


# ---------------------------------------------------------------------------
# The sweep — the only loop in this system that approves anything
# ---------------------------------------------------------------------------

#: Deliberate cap with a deliberate order. The queue is bounded by
#: `risk.max_pending_proposals` today, but a filter is not a cap: that bound
#: describes today's data, not this query. Oldest first, because those are the
#: ones closest to expiring — ordering the other way would starve them.
MAX_SWEEP_CANDIDATES: Final = 50


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one pass did, for the log line and the dashboard.

    ``refusals`` is a count per code rather than a row per refusal. Ten
    refusals every ten seconds is not a state change, and writing one audit
    row each would swamp the table `/api/audit` reads; what an operator wants
    is the current binding reason, not the same sentence 8,640 times a day.
    """

    considered: int = 0
    approved: int = 0
    failed: int = 0
    refusals: dict[str, int] = field(default_factory=dict)
    disarmed: str | None = None

    def render(self) -> str:
        parts = [f"considered {self.considered}", f"approved {self.approved}"]
        if self.failed:
            parts.append(f"failed {self.failed}")
        if self.refusals:
            reasons = ", ".join(
                f"{code} x{n}" for code, n in sorted(self.refusals.items())
            )
            parts.append(f"refused ({reasons})")
        if self.disarmed:
            parts.append(f"DISARMED: {self.disarmed}")
        return "; ".join(parts)


async def sweep(
    session: AsyncSession,
    executor: Any,
    *,
    settings: Settings,
    config: Config,
    now: datetime | None = None,
) -> SweepResult:
    """Consider every pending proposal and approve the ones that clear the gate.

    This is the function that makes the worker able to place an order without a
    human, so the constraints on it are worth stating plainly:

    - It never constructs its own permission. Every approval goes through
      :meth:`Executor.approve_and_execute`, which re-runs every interlock and
      derives ``actor`` itself.
    - It commits after each approval. The ``proposal.approved`` row *is* the
      budget ledger, so leaving it uncommitted while placing the next order
      would let one pass spend the hour's allowance several times over.
    - It stops the moment it disarms. A latch that kept trading for the rest
      of the pass would not be a latch.
    """
    now = now or datetime.now(UTC)
    result = SweepResult()
    refusals: dict[str, int] = {}

    if not config.autonomous.enabled or not settings.autonomous_trading:
        return result

    # The kill switch halts everything, including this. Checked once per pass
    # rather than per proposal: it is a global stop, not a per-trade question.
    if await get_kill_switch() or config.risk.kill_switch:
        return SweepResult(refusals={"kill_switch": 1})

    try:
        route = resolve_route(settings, config)
    except InterlockError as exc:
        return SweepResult(refusals={exc.code: 1})

    candidates = (
        (
            await session.execute(
                select(ProposedTrade)
                .where(ProposedTrade.status == ProposalStatus.PENDING)
                .order_by(ProposedTrade.created_at.asc())
                .limit(MAX_SWEEP_CANDIDATES)
            )
        )
        .scalars()
        .all()
    )
    result = SweepResult(considered=len(candidates))
    approved = 0
    failed = 0
    disarmed: str | None = None

    for proposal in candidates:
        try:
            consent = await evaluate(
                session,
                proposal,
                settings=settings,
                config=config,
                route=route,
                now=now,
            )
        except GateRefusal as exc:
            refusals[exc.code] = refusals.get(exc.code, 0) + 1
            log.debug("gate refused proposal %s: %s", proposal.id, exc)
            continue

        try:
            await executor.approve_and_execute(
                session, proposal, confirmed=False, machine_consent=consent
            )
            await session.commit()
        except InterlockError as exc:
            # The gate said yes and the interlocks said no. Not a bug in
            # either: the gate reads a snapshot and the interlocks read the
            # live world, and between the two the config, the kill switch or
            # the proposal's own status may have moved. Recorded as a refusal
            # so the operator sees the disagreement.
            await session.rollback()
            refusals[exc.code] = refusals.get(exc.code, 0) + 1
            log.warning("interlock refused a gated proposal %s: %s", proposal.id, exc)
            continue
        except ExecutionError as exc:
            # Commit before anything else. The executor writes Order rows
            # *before* placing precisely so an ambiguous failure leaves a
            # client order ID to reconcile against; rolling back here would
            # erase the only record that the attempt happened.
            await session.commit()
            failed += 1
            log.error("autonomous placement failed on %s: %s", proposal.id, exc)
            if await _was_ambiguous(session, proposal.id):
                disarmed = (
                    f"proposal {proposal.id} submitted ambiguously — an order may "
                    "exist at Kalshi that this system cannot see, and trading on "
                    "top of an unknown position is the worst available action"
                )
                await disarm(disarmed)
                break
            continue

        approved += 1
        if proposal.status is ProposalStatus.PARTIAL:
            # The executor already calls this "real, and it needs a person".
            # Under autonomy it has to actually get one.
            disarmed = (
                f"proposal {proposal.id} filled partially, leaving an unbalanced "
                "position that is now directional rather than hedged"
            )
            await disarm(disarmed)
            break

    return SweepResult(
        considered=result.considered,
        approved=approved,
        failed=failed,
        refusals=refusals,
        disarmed=disarmed,
    )


async def _was_ambiguous(session: AsyncSession, proposal_id: int | None) -> bool:
    """Did the failed placement leave an order that may exist at the exchange?

    Read back from the audit trail rather than inferred from the exception,
    because the executor is the thing that made the ambiguous/rejected
    judgement and re-deriving it here would be a second implementation of a
    distinction that must not drift. A definite 4xx rejection is safe to
    continue past; a timeout or a 5xx is not.
    """
    if proposal_id is None:
        return False
    found = await session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.kind == "order.submit_ambiguous",
            AuditLog.payload["proposal_id"].astext == str(proposal_id),
        )
    )
    return bool(found)

"""Execution routing and the safety interlocks in front of it.

Two questions live here, and they are deliberately separate:

1. **Where would an approved order go?** — :func:`resolve_route`.
2. **Is it allowed to go there right now?** — :func:`check_execution`.

Both are pure functions of settings, config, and the proposal.  They are
evaluated again inside the executor rather than trusted from the API layer,
because an interlock that can be bypassed by calling a different function is
not an interlock.

The routing table:

======================================  ====================
condition                               route
======================================  ====================
``mode=live`` + prod + ``LIVE_TRADING``  ``live_exchange``
``mode=live`` without both of those      **refused**
``mode=paper`` + demo env + credentials  ``demo_exchange``
``mode=paper`` + prod env                ``simulated``
anything else                            ``simulated``
======================================  ====================

The row that matters most is the fourth: **paper mode never touches a prod
exchange**, even when credentials are sitting right there.  "Paper" has to
mean paper regardless of what else is configured, otherwise flipping
``KALSHI_ENV`` for a data reason would quietly arm real money.

Routing says nothing about *who* may authorise a trade on that route.  There
are exactly two authorities, they are separately typed, and no order exists
without one of them:

===============  ==============================  ==================
authority        what it takes                   ``AuditLog.actor``
===============  ==============================  ==================
human            ``confirmed=True``, plus the    ``operator``
                 ticker typed back on live
machine          a :class:`MachineConsent` from  ``autonomous``
                 ``app.trading.autonomy``
both at once     **refused**                     —
===============  ==============================  ==================

The last row is not pedantry.  Which authority approved an order decides what
its audit row means, and a request claiming both cannot be resolved in either
direction without inventing an answer — so it is refused instead.

The two are not interchangeable.  The typed ticker is the *human* live
interlock and the machine can never synthesise it; the machine's live
interlock is the larger set in :func:`check_execution`, which the human never
satisfies.  Neither substitutes for the other, and a change that lets one
stand in for the other has removed an interlock rather than moved it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from app.config import Config
from app.db.models import ProposalStatus
from app.settings import Settings

if TYPE_CHECKING:
    from app.db.models import ProposedTrade

__all__ = [
    "ACTOR_AUTONOMOUS",
    "ACTOR_OPERATOR",
    "ExecutionRoute",
    "MachineConsent",
    "confirmation_target",
    "InterlockError",
    "resolve_route",
    "check_execution",
    "posture",
]

#: ``AuditLog.actor`` for a machine-approved order. String(32), so this fits
#: with room to spare — do not extend it to ``f"autonomous:{detector}"``, which
#: would not always. The detector is a payload field.
ACTOR_AUTONOMOUS: Final = "autonomous"
ACTOR_OPERATOR: Final = "operator"

#: How long a :class:`MachineConsent` remains valid after the gate issues it.
#:
#: The gate reads the book, the budget and the risk state, then hands the
#: executor a decision based on all three. Thirty seconds later any of them may
#: have moved. This is short because nothing legitimate needs longer: the gate
#: and the executor are called in the same function, microseconds apart. It
#: exists to bound a *bug* — a consent stashed on an object, retried from a
#: queue, or replayed — not to accommodate any real latency.
MAX_CONSENT_AGE: Final = timedelta(seconds=30)


class ExecutionRoute(StrEnum):
    """Where an approved order actually goes."""

    #: Filled against a local simulator using the live book. No API call.
    SIMULATED = "simulated"
    #: A real order on Kalshi's demo exchange. Real rail, play money.
    DEMO_EXCHANGE = "demo_exchange"
    #: A real order with real money.
    LIVE_EXCHANGE = "live_exchange"

    @property
    def is_paper(self) -> bool:
        return self is not ExecutionRoute.LIVE_EXCHANGE

    @property
    def hits_exchange(self) -> bool:
        return self is not ExecutionRoute.SIMULATED


@dataclass(frozen=True, slots=True)
class MachineConsent:
    """The autonomy gate's authorisation of ONE proposal on ONE route.

    Deliberately not a bool, and deliberately not the existing ``confirmed``
    flag, for two reasons that are both about what happens *after* the trade:

    1. A machine approval must never be indistinguishable from a human one in
       the audit log. ``actor`` is the only record of who authorised an order,
       and the executor derives it from the presence of this object rather
       than accepting it from a caller.
    2. A consent that does not name its subject can be applied to a trade
       nobody evaluated. This one names the proposal and the route, and
       :func:`check_execution` refuses if either has moved.

    Everything past ``gate_version`` is *evidence*, not permission. The
    interlocks re-derive every permission question from ``settings`` and
    ``config`` and trust none of it from here — so a consent built wrongly, or
    built for a config that has since changed, still cannot arm a route that
    is not armed. The evidence is carried because it is what the audit row
    needs to say *why* this was allowed, and reconstructing that after the
    fact is impossible once the report card has moved on.
    """

    #: What this consent authorises. Both re-checked against the live values.
    proposal_id: int
    route: ExecutionRoute

    #: Why the gate allowed it — recorded, never re-derived from.
    detector: str
    verdict: str
    trades: int
    ci_low_cents: str | None
    ci_high_cents: str | None
    mean_cents: str
    min_trades: int
    coverage_usable: bool
    coverage_refusals: tuple[str, ...]
    evidence_computed_at: datetime
    budget: dict[str, str] = field(default_factory=dict)

    issued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    gate_version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        """Flatten for the ``proposal.approved`` audit payload (JSONB)."""
        raw = asdict(self)
        raw["route"] = self.route.value
        raw["coverage_refusals"] = list(self.coverage_refusals)
        raw["evidence_computed_at"] = self.evidence_computed_at.isoformat()
        raw["issued_at"] = self.issued_at.isoformat()
        return raw


class InterlockError(RuntimeError):
    """An interlock refused the trade.

    ``code`` is a stable machine-readable reason so the UI can explain the
    refusal precisely instead of showing a stack trace.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_route(settings: Settings, config: Config) -> ExecutionRoute:
    """Decide where an approved order would go. Raises if the mode is unusable."""
    if config.trading.mode == "live":
        if not settings.live_trading_armed:
            raise InterlockError(
                "live_mode_not_armed",
                "config.yaml sets trading.mode=live but the environment "
                f"interlocks are not thrown (KALSHI_ENV={settings.kalshi_env.value}, "
                f"LIVE_TRADING={settings.live_trading}). Live trading needs both, "
                "plus per-trade confirmation. Refusing to trade rather than "
                "silently falling back to paper.",
            )
        return ExecutionRoute.LIVE_EXCHANGE

    # Paper. Never route to a production exchange from here, whatever else is
    # configured — see the module docstring.
    if settings.is_prod:
        return ExecutionRoute.SIMULATED

    if config.trading.paper_uses_demo_exchange and settings.credentials_present():
        return ExecutionRoute.DEMO_EXCHANGE

    return ExecutionRoute.SIMULATED


def check_execution(
    proposal: ProposedTrade,
    settings: Settings,
    config: Config,
    *,
    confirmed: bool,
    kill_switch: bool,
    machine_consent: MachineConsent | None = None,
    confirmation_phrase: str | None = None,
    now: datetime | None = None,
) -> ExecutionRoute:
    """Validate every interlock and return the route to use.

    Args:
        confirmed: The operator explicitly approved *this* proposal. Never
            defaulted true anywhere; a missing confirmation is a refusal.
        machine_consent: The autonomy gate's authorisation, when the machine
            is the approving authority. Mutually exclusive with ``confirmed``.
        confirmation_phrase: Required only on the live route *for a human*,
            where the UI makes the operator type the market ticker. A misclick
            cannot produce it.

    Raises:
        InterlockError: on the first failing check, with a stable ``code``.
    """
    now = now or datetime.now(UTC)

    # Exactly one authority. `machine_consent` defaults to None while
    # `kill_switch` deliberately has no default — the asymmetry is not an
    # oversight. A missing kill-switch argument would silently *bypass* the
    # emergency stop, whereas a missing consent can only make this function
    # stricter: None means "no machine authorisation", so the human
    # requirement below applies unchanged.
    if machine_consent is None and not confirmed:
        raise InterlockError(
            "not_confirmed",
            "no per-trade confirmation supplied. Every order requires explicit "
            "human approval of that specific trade.",
        )

    if machine_consent is not None and confirmed:
        raise InterlockError(
            "ambiguous_consent",
            "this approval carried both a human confirmation and a machine "
            "consent. Which one authorised the order decides what its audit "
            "row means, and there is no way to pick between them that is not "
            "invented, so it is refused rather than resolved.",
        )

    # Either source engages it. `config.risk.kill_switch` is the static floor
    # from config.yaml; `kill_switch` is the runtime flag in Redis, which is
    # the one an operator can actually reach on a running system. A config
    # that says true can never be released by the API — a deliberate one-way
    # door, so an operator who has halted trading in the file cannot be undone
    # by a click.
    #
    # `kill_switch` is a required argument with no default on purpose. There
    # is no safe default: `False` would mean a caller that forgot it silently
    # bypasses the emergency stop.
    if kill_switch or config.risk.kill_switch:
        raise InterlockError(
            "kill_switch",
            "the kill switch is engaged: no new orders are placed and resting "
            "orders are being cancelled.",
        )

    if proposal.status is not ProposalStatus.PENDING:
        raise InterlockError(
            "not_pending",
            f"proposal {proposal.id} is {proposal.status.value}, not pending. "
            "Only a pending proposal can be approved.",
        )

    if proposal.expires_at is not None and proposal.expires_at <= now:
        raise InterlockError(
            "expired",
            f"proposal {proposal.id} expired at "
            f"{proposal.expires_at.isoformat()}. The quote it was priced "
            "against is gone; re-propose rather than trading a stale edge.",
        )

    route = resolve_route(settings, config)

    if machine_consent is not None:
        _check_machine_consent(machine_consent, settings, config, proposal, route, now)

    # The third *human* interlock. Typing the ticker is the difference between
    # "I clicked something" and "I meant this market". For a multi-leg
    # proposal the event ticker is what identifies the trade, since no single
    # market does.
    #
    # Skipped under machine consent because there is nothing it could mean: a
    # machine typing a string it generated proves nothing about intent. The
    # machine's live interlock is the set above, which a human never satisfies
    # — the two are different questions, not two spellings of one.
    expected = confirmation_target(proposal)
    if (
        machine_consent is None
        and route is ExecutionRoute.LIVE_EXCHANGE
        and (confirmation_phrase or "").strip().upper() != expected.upper()
    ):
        raise InterlockError(
            "confirmation_phrase_mismatch",
            f"live trading requires typing {expected!r} to confirm.",
        )

    if route.hits_exchange and not settings.credentials_present():
        raise InterlockError(
            "no_credentials",
            f"route {route.value} needs Kalshi credentials for env="
            f"{settings.kalshi_env.value} and none are usable.",
        )

    return route


def _check_machine_consent(
    consent: MachineConsent,
    settings: Settings,
    config: Config,
    proposal: ProposedTrade,
    route: ExecutionRoute,
    now: datetime,
) -> None:
    """Validate a machine authorisation. Raises on the first failure.

    Every permission question here is re-derived from ``settings`` and
    ``config``; nothing is read off the consent to decide whether the machine
    *may* act. That split is the point of the object: it carries evidence, and
    a caller that forges one, reuses one, or holds one across a config change
    still cannot arm a route that is not armed.
    """
    if not config.autonomous.enabled:
        raise InterlockError(
            "autonomy_disabled",
            "a machine consent was supplied but autonomous.enabled is false.",
        )

    # The environment half. Deliberately not settable from config.yaml or the
    # settings UI: arming the machine takes an .env edit and a restart, so no
    # request on the LAN can do it. The dashboard has no auth in front of it
    # by design, which is exactly why this one lives outside its reach.
    if not settings.autonomous_trading:
        raise InterlockError(
            "autonomous_not_armed",
            "a machine consent was supplied but AUTONOMOUS_TRADING is not set "
            "in the environment. Config alone cannot arm the machine.",
        )

    # Field names on AutonomousRouteConfig are exactly the ExecutionRoute
    # values, so this cannot drift out of step with a new route the way a
    # mapping would.
    if not getattr(config.autonomous.routes, route.value, False):
        raise InterlockError(
            "autonomous_route_not_armed",
            f"the machine is not armed on route {route.value}. Arming a route "
            "is per-route on purpose: proving an edge on the simulator says "
            "nothing about the exchange, and vice versa.",
        )

    if route is ExecutionRoute.LIVE_EXCHANGE and not settings.autonomous_live_armed:
        raise InterlockError(
            "autonomous_live_not_armed",
            "machine-driven live trading needs KALSHI_ENV=prod, "
            "LIVE_TRADING=true and AUTONOMOUS_TRADING=true together "
            f"(env={settings.kalshi_env.value}, live={settings.live_trading}, "
            f"autonomous={settings.autonomous_trading}).",
        )

    if consent.proposal_id != proposal.id:
        raise InterlockError(
            "consent_proposal_mismatch",
            f"machine consent authorises proposal {consent.proposal_id}, but "
            f"this is proposal {proposal.id}. A consent names its subject so "
            "that it cannot be applied to a trade the gate never evaluated.",
        )

    if consent.route is not route:
        raise InterlockError(
            "consent_route_mismatch",
            f"machine consent was issued for route {consent.route.value} and "
            f"this order would go to {route.value}. The evidence it carries "
            "is per-route and does not transfer.",
        )

    age = now - consent.issued_at
    if age > MAX_CONSENT_AGE or age < -MAX_CONSENT_AGE:
        raise InterlockError(
            "consent_stale",
            f"machine consent was issued {age.total_seconds():.1f}s away from "
            f"now, outside ±{MAX_CONSENT_AGE.total_seconds():.0f}s. The book, "
            "the budget and the risk state it was measured against have moved; "
            "re-evaluate rather than acting on a stale reading.",
        )


def confirmation_target(proposal: ProposedTrade) -> str:
    """What the operator must type to confirm a live trade.

    The event ticker for a multi-leg proposal — a set arbitrage is one
    decision about an event, and no single market names it.
    """
    if (proposal.leg_count or 1) > 1 and proposal.event_ticker:
        return proposal.event_ticker
    return proposal.ticker


def posture(settings: Settings, config: Config) -> dict[str, object]:
    """Describe the current safety posture for the UI and the logs.

    Returns a description even when the configuration is refused, because
    "your config is unusable" is exactly what the dashboard needs to say.
    """
    route: str | None
    blocked: str | None
    try:
        route = resolve_route(settings, config).value
        blocked = None
    except InterlockError as exc:
        route = None
        blocked = exc.code

    # Autonomy, as far as settings and config can say. This function is called
    # on every /api/trading/state request and stays pure — whether the machine
    # is *currently* able to act also depends on the runtime disarm latch and
    # the kill switch in Redis, which the route overlays on top.
    auto = config.autonomous
    route_armed = bool(route) and getattr(auto.routes, route or "", False)

    return {
        "environment": settings.kalshi_env.value,
        "trading_mode": config.trading.mode,
        "live_trading_armed": settings.live_trading_armed,
        "kill_switch": config.risk.kill_switch,
        "credentials_present": settings.credentials_present(),
        "execution_route": route,
        "route_blocked_by": blocked,
        # True only on the one route that can lose real money.
        "real_money": route == ExecutionRoute.LIVE_EXCHANGE.value,
        # Human-path only. The machine never satisfies this and is never
        # asked to; see `check_execution`.
        "requires_typed_confirmation": route == ExecutionRoute.LIVE_EXCHANGE.value,
        "autonomy_enabled": auto.enabled,
        "autonomy_env_armed": settings.autonomous_trading,
        "autonomy_route_armed": route_armed,
        "autonomy_live_armed": settings.autonomous_live_armed,
        "autonomy_requires_edge_shown": auto.evidence.require_edge_shown,
        # Surfaced because it can be waived on the simulated route, and a
        # dashboard that did not say so would be claiming a gate that is off.
        "autonomy_coverage_enforced": auto.evidence.require_coverage_usable,
        "autonomy_min_proposal_age_sec": auto.min_proposal_age_sec,
        # Everything config and env can settle. Still not "armed" — that needs
        # the runtime latch, which lives in Redis.
        "autonomy_configured": bool(
            auto.enabled and settings.autonomous_trading and route_armed
        ),
        "proposal_ttl_sec": config.trading.default_proposal_ttl_sec,
        "time_in_force": config.trading.order.time_in_force,
        "auto_cancel_after_sec": config.trading.order.auto_cancel_after_sec,
    }

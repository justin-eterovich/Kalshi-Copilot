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

None of this can be relaxed into automatic execution.  Every route requires a
human decision on a specific proposal first; these checks only decide whether
that decision is permitted to have an effect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from app.config import Config
from app.db.models import ProposalStatus
from app.settings import Settings

if TYPE_CHECKING:
    from app.db.models import ProposedTrade

__all__ = [
    "ExecutionRoute",
    "InterlockError",
    "resolve_route",
    "check_execution",
    "posture",
]


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
    confirmation_phrase: str | None = None,
    now: datetime | None = None,
) -> ExecutionRoute:
    """Validate every interlock and return the route to use.

    Args:
        confirmed: The operator explicitly approved *this* proposal. Never
            defaulted true anywhere; a missing confirmation is a refusal.
        confirmation_phrase: Required only on the live route, where the UI
            makes the operator type the market ticker. A misclick cannot
            produce it.

    Raises:
        InterlockError: on the first failing check, with a stable ``code``.
    """
    now = now or datetime.now(UTC)

    if not confirmed:
        raise InterlockError(
            "not_confirmed",
            "no per-trade confirmation supplied. Every order requires explicit "
            "human approval of that specific trade.",
        )

    if config.risk.kill_switch:
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

    # The third interlock. Typing the ticker is the difference between
    # "I clicked something" and "I meant this market".
    if route is ExecutionRoute.LIVE_EXCHANGE and (
        confirmation_phrase or ""
    ).strip().upper() != proposal.ticker.upper():
        raise InterlockError(
            "confirmation_phrase_mismatch",
            "live trading requires typing the market ticker to confirm. "
            f"Expected {proposal.ticker!r}.",
        )

    if route.hits_exchange and not settings.credentials_present():
        raise InterlockError(
            "no_credentials",
            f"route {route.value} needs Kalshi credentials for env="
            f"{settings.kalshi_env.value} and none are usable.",
        )

    return route


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
        "requires_typed_confirmation": route == ExecutionRoute.LIVE_EXCHANGE.value,
        "proposal_ttl_sec": config.trading.default_proposal_ttl_sec,
        "time_in_force": config.trading.order.time_in_force,
        "auto_cancel_after_sec": config.trading.order.auto_cancel_after_sec,
    }

"""The portfolio-level risk limits.

Everything in :mod:`app.trading.proposals` asks "is *this* trade too big?".
This module asks the three questions that only make sense across the whole
book, and that no single proposal can answer about itself:

- **Total exposure** — how much capital is already committed, including
  proposals waiting for a decision. Ten trades each inside
  ``max_pct_per_market`` can still be the entire bankroll.
- **Daily loss limit** — how much has been lost today, net of fees. A limit
  that ignores fees is not a limit; on this exchange fees are a large fraction
  of the edge being chased.
- **Loss cooldown** — whether the last few closes were all losers. This is the
  only limit here that is about the operator rather than the money: a losing
  streak is when a human is most likely to approve something they would
  otherwise refuse.

Why this could not be built before now
--------------------------------------

Two of these read realised P&L, and until settlement ingestion existed the
only P&L this system recognised came from positions that were *traded out*.
A thesis held to resolution — the way most of these are meant to pay off —
realised nothing at all. A daily loss limit reading that number would have
been reassuring and blind, which is worse than absent. See
:mod:`app.trading.settlements`.

Where it is enforced
--------------------

In :meth:`app.trading.executor.Executor.approve_and_execute`, next to the
other interlocks, because that is the only place an order can come into
existence and an interlock that can be sidestepped by calling a different
function is not an interlock. The halt checks additionally run at proposal
creation, so that a halted system stops *filling the queue* rather than
building a backlog of trades it will refuse — but the enforcement that
matters is the one in the executor.

Every limit is evaluated **per route**. The simulated, demo and live books are
separate stacks of money; a simulated loss must not halt live trading, and a
live loss must certainly not be offset by a paper gain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.db.models import (
    Fill,
    Order,
    PnlDaily,
    Position,
    ProposalStatus,
    ProposedTrade,
    Settlement,
)
from app.settings import Settings
from app.trading.interlocks import InterlockError, resolve_route

log = get_logger(__name__)

__all__ = [
    "RiskError",
    "RiskState",
    "position_cost_cents",
    "consecutive_losses",
    "snapshot",
    "check_halted",
    "check_exposure",
    "guard_approval",
    "halt_state",
    "active_route",
]

HUNDRED = Decimal(100)
ONE = Decimal(1)

#: How far back to look for the losing streak. The streak itself is capped by
#: ``cooldown_after_consecutive_losses``, so this only has to be comfortably
#: larger than any sane value of it.
STREAK_WINDOW = 50


class RiskError(InterlockError):
    """A portfolio-level limit refused the trade.

    Subclasses :class:`~app.trading.interlocks.InterlockError` so the API layer
    already maps it to a 409 with a stable ``code``, and so nothing can catch
    "interlocks" and quietly miss the risk limits.
    """


def position_cost_cents(net_contracts: Decimal, avg_price: Decimal) -> Decimal:
    """Capital at risk in an open position, in cents.

    On a binary contract what you paid *is* your maximum loss, so cost basis
    and exposure are the same number — which is why this is not a mark-to-
    market figure. Marking to market would let a position that has moved in
    your favour free up room for more risk before it has actually paid out.

    Signed YES-equivalents make the NO side fall out: -5 contracts carried at
    a YES price of 0.70 is 5 NO contracts bought at 0.30, so 150 cents.
    """
    if net_contracts == 0:
        return Decimal(0)
    per_contract = avg_price if net_contracts > 0 else ONE - avg_price
    return abs(net_contracts) * per_contract * HUNDRED


def consecutive_losses(realized: list[Decimal]) -> int:
    """Count the losing streak at the head of ``realized`` (most recent first).

    Breakeven closes are skipped rather than treated as wins: a scratch does
    not clear a losing streak, and it is not evidence that anything has
    changed. A single winner does clear it.
    """
    streak = 0
    for value in realized:
        if value < 0:
            streak += 1
        elif value > 0:
            break
    return streak


@dataclass(frozen=True, slots=True)
class RiskState:
    """Everything the limits are computed from, for one route.

    Kept as data rather than a set of booleans so the dashboard can show the
    operator *how close* each limit is, not just whether it tripped. A limit
    that only speaks when it fires teaches nobody anything.
    """

    route: str
    day: date
    bankroll_cents: Decimal
    #: Capital tied up in open positions.
    exposure_cents: Decimal
    #: Capital that would be tied up if every pending proposal were approved.
    pending_cents: Decimal
    exposure_limit_cents: Decimal
    daily_realized_cents: Decimal
    daily_fees_cents: Decimal
    daily_loss_limit_cents: Decimal
    consecutive_losses: int
    cooldown_until: datetime | None
    now: datetime

    @property
    def committed_cents(self) -> Decimal:
        """Open exposure plus everything awaiting a decision.

        Pending proposals count against the limit because they are one click
        from being real, and a queue approved in a burst is exactly how a
        portfolio limit gets blown through while every individual trade was
        inside its own.
        """
        return self.exposure_cents + self.pending_cents

    @property
    def headroom_cents(self) -> Decimal:
        return self.exposure_limit_cents - self.committed_cents

    @property
    def daily_net_cents(self) -> Decimal:
        """Today's realised P&L **after fees** — the only honest version."""
        return self.daily_realized_cents - self.daily_fees_cents

    @property
    def daily_loss_breached(self) -> bool:
        if self.daily_loss_limit_cents <= 0:
            return False
        return self.daily_net_cents <= -self.daily_loss_limit_cents

    @property
    def in_cooldown(self) -> bool:
        return self.cooldown_until is not None and self.now < self.cooldown_until

    @property
    def halted(self) -> bool:
        return self.daily_loss_breached or self.in_cooldown

    def as_dict(self) -> dict[str, object]:
        return {
            "route": self.route,
            "day": self.day.isoformat(),
            "bankroll_cents": str(self.bankroll_cents),
            "exposure_cents": str(self.exposure_cents),
            "pending_cents": str(self.pending_cents),
            "committed_cents": str(self.committed_cents),
            "exposure_limit_cents": str(self.exposure_limit_cents),
            "headroom_cents": str(self.headroom_cents),
            "daily_realized_cents": str(self.daily_realized_cents),
            "daily_fees_cents": str(self.daily_fees_cents),
            "daily_net_cents": str(self.daily_net_cents),
            "daily_loss_limit_cents": str(self.daily_loss_limit_cents),
            "daily_loss_breached": self.daily_loss_breached,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": (
                self.cooldown_until.isoformat() if self.cooldown_until else None
            ),
            "in_cooldown": self.in_cooldown,
            "halted": self.halted,
        }


def active_route(settings: Settings, config: Config) -> str | None:
    """The route risk is accounted against, or ``None`` if none is usable.

    ``None`` is not a failure to handle here: a configuration with no usable
    route already refuses every approval in :func:`resolve_route`, so there is
    no trade for these limits to constrain.
    """
    try:
        return resolve_route(settings, config).value
    except InterlockError:
        return None


async def snapshot(
    session: AsyncSession,
    config: Config,
    *,
    route: str,
    now: datetime | None = None,
) -> RiskState:
    """Gather the current risk picture for one route."""
    now = now or datetime.now(UTC)
    today = now.date()
    bankroll_cents = Decimal(str(config.risk.bankroll_usd)) * HUNDRED

    open_positions = (
        await session.execute(
            select(Position.net_contracts, Position.avg_price).where(
                Position.route == route, Position.net_contracts != 0
            )
        )
    ).all()
    exposure = sum(
        (
            position_cost_cents(net or Decimal(0), avg or Decimal(0))
            for net, avg in open_positions
        ),
        Decimal(0),
    )

    pending = (
        await session.execute(
            select(ProposedTrade.max_loss_cents).where(
                ProposedTrade.status == ProposalStatus.PENDING
            )
        )
    ).scalars().all()
    pending_cents = sum((p or Decimal(0) for p in pending), Decimal(0))

    daily = (
        await session.execute(
            select(PnlDaily).where(PnlDaily.day == today, PnlDaily.route == route)
        )
    ).scalars().first()

    streak, last_loss_at = await _recent_losses(session, route=route)
    cooldown_until: datetime | None = None
    if streak >= config.risk.cooldown_after_consecutive_losses and last_loss_at:
        cooldown_until = last_loss_at + timedelta(minutes=config.risk.cooldown_minutes)

    return RiskState(
        route=route,
        day=today,
        bankroll_cents=bankroll_cents,
        exposure_cents=exposure,
        pending_cents=pending_cents,
        exposure_limit_cents=(
            bankroll_cents * Decimal(str(config.risk.max_total_exposure_pct))
        ),
        daily_realized_cents=(
            (daily.realized_pnl_cents or Decimal(0)) if daily else Decimal(0)
        ),
        daily_fees_cents=(
            (daily.fees_paid_cents or Decimal(0)) if daily else Decimal(0)
        ),
        daily_loss_limit_cents=(
            bankroll_cents * Decimal(str(config.risk.daily_loss_limit_pct))
        ),
        consecutive_losses=streak,
        cooldown_until=cooldown_until,
        now=now,
    )


async def _recent_losses(
    session: AsyncSession, *, route: str
) -> tuple[int, datetime | None]:
    """The current losing streak and when its most recent loss happened.

    A "close" is anything that realised P&L: a fill that reduced a position,
    or a settlement. Both count, because a thesis that lost money at
    resolution is exactly as much a loss as one sold out early — and on this
    system settlement is the more common of the two.
    """
    events: list[tuple[datetime, Decimal]] = []

    fills = (
        await session.execute(
            select(Fill.ts, Fill.realized_pnl_cents)
            .join(Order, Fill.order_id == Order.id)
            .where(Order.route == route, Fill.realized_pnl_cents != 0)
            .order_by(Fill.ts.desc())
            .limit(STREAK_WINDOW)
        )
    ).all()
    events.extend((ts, value or Decimal(0)) for ts, value in fills if ts)

    settled = (
        await session.execute(
            select(Settlement.created_at, Settlement.realized_pnl_cents)
            .where(Settlement.route == route)
            .order_by(Settlement.created_at.desc())
            .limit(STREAK_WINDOW)
        )
    ).all()
    events.extend((ts, value or Decimal(0)) for ts, value in settled if ts)

    if not events:
        return 0, None

    events.sort(key=lambda row: row[0], reverse=True)
    streak = consecutive_losses([value for _, value in events])
    if streak == 0:
        return 0, None
    return streak, events[0][0]


def check_halted(state: RiskState, config: Config) -> None:
    """Refuse while the book is halted by a loss limit or a cooldown.

    Both refusals are temporary and say so: the daily limit clears at
    midnight UTC, the cooldown at a stated time. Neither is a bug to work
    around, which is why the messages name the config key that set them
    rather than just reporting a number.
    """
    if state.daily_loss_breached:
        raise RiskError(
            "daily_loss_limit",
            f"today's realised P&L on {state.route} is "
            f"{state.daily_net_cents / HUNDRED:.2f} USD after fees, at or past "
            f"the {config.risk.daily_loss_limit_pct:.1%} daily loss limit "
            f"({-state.daily_loss_limit_cents / HUNDRED:.2f} USD). Trading is "
            "halted for the rest of the UTC day; raise "
            "risk.daily_loss_limit_pct only after deciding the limit was wrong, "
            "not because today was.",
        )

    if state.in_cooldown:
        assert state.cooldown_until is not None
        remaining = (state.cooldown_until - state.now).total_seconds() / 60
        raise RiskError(
            "loss_cooldown",
            f"{state.consecutive_losses} consecutive losing closes on "
            f"{state.route} (risk.cooldown_after_consecutive_losses = "
            f"{config.risk.cooldown_after_consecutive_losses}). Cooling off for "
            f"another {remaining:.0f} minute(s), until "
            f"{state.cooldown_until.isoformat()}.",
        )


def check_exposure(
    state: RiskState, config: Config, *, additional_cents: Decimal
) -> None:
    """Refuse a trade that would put more than the limit at risk in total.

    Compared against open exposure plus **this** trade only, deliberately not
    against ``committed_cents``. The proposal being approved is itself inside
    ``pending_cents``, so including the queue here would count it twice and
    refuse the very trade the headroom was reserved for. The queue still shows
    up in ``headroom_cents`` for display, where its job is to warn the
    operator that approving everything would not fit.
    """
    if state.exposure_limit_cents <= 0:
        return

    projected = state.exposure_cents + additional_cents
    if projected > state.exposure_limit_cents:
        raise RiskError(
            "exceeds_total_exposure",
            f"approving this would put {projected / HUNDRED:.2f} USD at risk on "
            f"{state.route}, over the {config.risk.max_total_exposure_pct:.1%} "
            f"total-exposure limit "
            f"({state.exposure_limit_cents / HUNDRED:.2f} USD). "
            f"{state.exposure_cents / HUNDRED:.2f} USD is already committed; "
            "close something or raise risk.max_total_exposure_pct.",
        )


async def guard_approval(
    session: AsyncSession,
    config: Config,
    settings: Settings,
    *,
    max_loss_cents: Decimal | None,
    now: datetime | None = None,
) -> RiskState | None:
    """Run every portfolio limit ahead of an approval.

    Returns the state it evaluated, or ``None`` when no route is usable — in
    which case :func:`resolve_route` has already refused and there is nothing
    to guard.
    """
    route = active_route(settings, config)
    if route is None:
        return None

    state = await snapshot(session, config, route=route, now=now)
    check_halted(state, config)
    check_exposure(
        state, config, additional_cents=max_loss_cents or Decimal(0)
    )
    return state


async def halt_state(
    session: AsyncSession, config: Config, settings: Settings
) -> RiskState | None:
    """Risk state for display and for the creation-time halt check."""
    route = active_route(settings, config)
    if route is None:
        return None
    return await snapshot(session, config, route=route)

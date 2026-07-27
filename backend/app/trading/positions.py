"""Position and P&L accounting from fills.

One signed number per market, in **YES-equivalent contracts**: positive is
long YES, negative is long NO.  That representation is not a stylistic
choice — on a binary market, buying NO genuinely offsets a YES position
rather than sitting beside it, so any scheme that tracks the two sides
separately has to special-case the netting and will eventually get it wrong.

``avg_price`` follows the same convention: it is the average **YES price** of
whatever is open, so a NO position bought at 30c is carried at 0.70.  The API
does it this way too (``position_fp`` is signed, negative meaning NO), which
means reconciliation against the exchange is a comparison rather than a
translation.

Realised P&L is recognised only when a fill **reduces** the position, priced
against ``avg_price``.  Fees are recognised immediately, on every fill,
because that is when they are charged.  Both facts matter for the report
card: a strategy that pays fees to build a position it never closes has lost
money, and the daily P&L should say so on the day it happened.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Fill, PnlDaily, Position, Side
from app.trading.direction import signed_contracts, to_yes_price

log = get_logger(__name__)

__all__ = ["apply_fill", "realized_from_fill", "position_view", "roll_daily"]

HUNDRED = Decimal(100)


def realized_from_fill(
    *,
    net_contracts: Decimal,
    avg_price: Decimal,
    delta: Decimal,
    fill_yes_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Apply one signed fill to a signed position.

    Args:
        net_contracts: Position before the fill, YES-equivalent and signed.
        avg_price: Average YES price of that position.
        delta: Signed size of this fill, YES-equivalent.
        fill_yes_price: Fill price expressed as a YES price.

    Returns:
        ``(new_net_contracts, new_avg_price, realized_cents)`` where
        ``realized_cents`` is exact and may be fractional.

    The three cases:

    - **Same direction** (or opening from flat): no realisation, the average
      moves.
    - **Reducing**: realise ``(fill - avg) * closed`` in the direction of the
      old position; the average is untouched, because the remaining lot was
      bought at the old price.
    - **Crossing through zero**: realise on the closed part only, then open
      the remainder at the fill price. Averaging across a flip would carry a
      long position's cost basis into a short one, which is nonsense.
    """
    if delta == 0:
        return net_contracts, avg_price, Decimal(0)

    # Opening from flat, or adding in the same direction.
    if net_contracts == 0 or (net_contracts > 0) == (delta > 0):
        new_net = net_contracts + delta
        # Weighted by absolute size: a short position's average is still an
        # average price, not a negative one.
        total = abs(net_contracts) + abs(delta)
        new_avg = (avg_price * abs(net_contracts) + fill_yes_price * abs(delta)) / total
        return new_net, new_avg, Decimal(0)

    closed = min(abs(delta), abs(net_contracts))
    # Long: profit when the exit price is above the average. Short: reversed.
    direction = Decimal(1) if net_contracts > 0 else Decimal(-1)
    realized = (fill_yes_price - avg_price) * closed * direction * HUNDRED

    new_net = net_contracts + delta

    if new_net == 0:
        return new_net, Decimal(0), realized
    if (new_net > 0) == (net_contracts > 0):
        # Partial close: the surviving lot keeps its original basis.
        return new_net, avg_price, realized
    # Crossed through flat: what is left was opened at this fill's price.
    return new_net, fill_yes_price, realized


async def apply_fill(session: AsyncSession, fill: Fill, *, route: str) -> Position:
    """Fold ``fill`` into the position and the day's P&L for ``route``.

    Positions are kept per route, not per ``is_paper``. A simulated fill and a
    demo-exchange fill are both "paper", but only one of them exists at
    Kalshi — netting them into one book makes reconciliation impossible and
    corrupts the report card.

    The caller is responsible for having persisted ``fill`` first; this is
    deliberately not idempotent on its own, so fills are deduplicated by the
    unique constraint on ``exchange_fill_id`` before they get here.
    """
    is_paper = route != "live_exchange"
    position = (
        await session.execute(
            select(Position).where(
                Position.ticker == fill.ticker, Position.route == route
            )
        )
    ).scalars().first()

    if position is None:
        position = Position(
            ticker=fill.ticker,
            route=route,
            is_paper=is_paper,
            net_contracts=Decimal(0),
            avg_price=Decimal(0),
            realized_pnl_cents=Decimal(0),
            fees_paid_cents=Decimal(0),
        )
        session.add(position)

    # A Fill records the side it took, the direction, and the price on that
    # side; all three have to become YES-equivalents before they can net.
    delta = signed_contracts(fill.side, fill.action, fill.contracts)
    fill_yes_price = to_yes_price(fill.side, fill.price)

    new_net, new_avg, realized = realized_from_fill(
        net_contracts=position.net_contracts or Decimal(0),
        avg_price=position.avg_price or Decimal(0),
        delta=delta,
        fill_yes_price=fill_yes_price,
    )

    position.net_contracts = new_net
    position.avg_price = new_avg
    position.realized_pnl_cents = (
        position.realized_pnl_cents or Decimal(0)
    ) + realized
    position.fees_paid_cents = (
        position.fees_paid_cents or Decimal(0)
    ) + fill.fee_cents

    # Stamped on the fill as well as rolled into the day, so the risk layer
    # can ask which of the recent closes lost money. A daily aggregate cannot
    # answer that, and "were the last three trades losers?" is a limit.
    fill.realized_pnl_cents = realized

    await roll_daily(
        session,
        day=(fill.ts or datetime.now(UTC)).date(),
        route=route,
        is_paper=is_paper,
        realized=realized,
        fee_cents=fill.fee_cents,
    )

    return position


async def roll_daily(
    session: AsyncSession,
    *,
    day: date,
    route: str,
    is_paper: bool,
    realized: Decimal,
    fee_cents: Decimal,
    kind: str = "fill",
) -> None:
    row = (
        await session.execute(
            select(PnlDaily).where(PnlDaily.day == day, PnlDaily.route == route)
        )
    ).scalars().first()

    if row is None:
        row = PnlDaily(
            day=day,
            route=route,
            is_paper=is_paper,
            realized_pnl_cents=Decimal(0),
            unrealized_pnl_cents=Decimal(0),
            fees_paid_cents=Decimal(0),
            trades=0,
            settlements=0,
        )
        session.add(row)

    row.realized_pnl_cents = (row.realized_pnl_cents or Decimal(0)) + realized
    row.fees_paid_cents = (row.fees_paid_cents or Decimal(0)) + fee_cents
    if kind == "settlement":
        row.settlements = (row.settlements or 0) + 1
    else:
        row.trades = (row.trades or 0) + 1


def position_view(
    position: Position, mark_yes_price: Decimal | None
) -> dict[str, object]:
    """Render a position the way a trader reads it, with unrealised P&L.

    The stored form is signed and YES-denominated; a trader wants "5 NO at
    30c". Both appear, because the signed number is what reconciles against
    the exchange and the readable one is what gets checked by eye.
    """
    net = position.net_contracts or Decimal(0)
    avg_yes = position.avg_price or Decimal(0)
    side = Side.YES if net >= 0 else Side.NO
    display_avg = avg_yes if side is Side.YES else Decimal(1) - avg_yes

    unrealized: Decimal | None = None
    if mark_yes_price is not None and net != 0:
        unrealized = (mark_yes_price - avg_yes) * net * HUNDRED

    return {
        "ticker": position.ticker,
        "route": position.route,
        "is_paper": position.is_paper,
        "net_contracts": str(net),
        "side": side.value,
        "contracts": str(abs(net)),
        "avg_price": str(display_avg),
        "avg_yes_price": str(avg_yes),
        "realized_pnl_cents": str(position.realized_pnl_cents or Decimal(0)),
        "unrealized_pnl_cents": None if unrealized is None else str(unrealized),
        "fees_paid_cents": str(position.fees_paid_cents or Decimal(0)),
        "updated_at": (
            position.updated_at.isoformat() if position.updated_at else None
        ),
    }

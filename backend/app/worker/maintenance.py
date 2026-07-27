"""Background upkeep for the execution rail.

Three jobs, none of which can create a trade:

- **Expire proposals** whose TTL has run out. A proposal carries a price that
  was executable when it was written; expiry is what stops a stale quote from
  remaining actionable.
- **Auto-cancel working orders** older than ``order.auto_cancel_after_sec``.
  An order left resting in a thin market is an option written for free, and
  it drifts away from the thesis that justified it.
- **Reconcile** orders that reached an exchange, because fills arrive after
  placement and we do not hold a portfolio websocket.

The kill switch is enforced here rather than only at approval time: engaging
it must also retire orders that are already working, otherwise "halt
everything" would leave live exposure sitting on the book.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from app.core.logging import get_logger
from app.core.money import parse_count, parse_dollars
from app.db.models import Fill, Order, OrderStatus
from app.kalshi.rest import KalshiApiError
from app.settings import Settings
from app.trading import positions, proposals, settlements
from app.trading.direction import from_yes_price
from app.trading.executor import (
    LIVE_ORDER_STATUSES,
    Executor,
    fee_cents_from_dollars,
)
from app.trading.interlocks import ExecutionRoute, InterlockError, resolve_route

log = get_logger(__name__)

__all__ = [
    "sweep_proposals",
    "sweep_orders",
    "sweep_settlements",
    "reconcile_order",
]


async def sweep_proposals(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        count = await proposals.expire_stale(session)
        await session.commit()
        return count


async def sweep_orders(
    sessions: async_sessionmaker[AsyncSession],
    executor: Executor,
    config: Config,
    *,
    now: datetime | None = None,
) -> int:
    """Reconcile working orders, then cancel the ones that have aged out."""
    now = now or datetime.now(UTC)
    deadline = now - timedelta(seconds=config.trading.order.auto_cancel_after_sec)
    cancelled = 0

    async with sessions() as session:
        working = (
            await session.execute(
                select(Order).where(Order.status.in_(LIVE_ORDER_STATUSES))
            )
        ).scalars().all()

        for order in working:
            # Reconcile first: an order that filled in the last few seconds
            # should be recorded as filled, not cancelled out from under the
            # fills that already happened.
            if order.route != ExecutionRoute.SIMULATED.value:
                try:
                    await reconcile_order(session, executor, order)
                except KalshiApiError as exc:
                    log.warning("could not reconcile order %s: %s", order.id, exc)

            if order.status not in LIVE_ORDER_STATUSES:
                continue

            expired = order.created_at is not None and order.created_at <= deadline
            if not (expired or config.risk.kill_switch):
                continue

            reason = (
                "kill switch engaged"
                if config.risk.kill_switch
                else f"unfilled after {config.trading.order.auto_cancel_after_sec}s"
            )
            try:
                await executor.cancel(session, order, reason=reason)
                cancelled += 1
            except (KalshiApiError, RuntimeError) as exc:
                log.warning("could not cancel order %s: %s", order.id, exc)

        await session.commit()

    if cancelled:
        log.info("auto-cancelled %d working order(s)", cancelled)
    return cancelled


async def sweep_settlements(
    sessions: async_sessionmaker[AsyncSession],
    executor: Executor,
    settings: Settings,
    config: Config,
) -> int:
    """Close out positions in markets that have resolved.

    Runs both books every pass. The simulated one always settles locally from
    the markets' own results; the exchange one is only swept when the current
    route actually reaches Kalshi, because a settlement feed belongs to the
    account it came from and applying it to some other book would realise P&L
    against contracts that book never held.
    """
    applied = 0
    async with sessions() as session:
        try:
            applied += await settlements.sync_local_settlements(session)
        except Exception as exc:  # noqa: BLE001 - one bad market must not stall the rest
            log.exception("local settlement sweep failed: %s", exc)

        client = executor.rest
        try:
            route = resolve_route(settings, config)
        except InterlockError:
            # An unusable trading config still has a simulated book worth
            # settling, which is why this comes after the local sweep.
            route = None

        if route is not None and route.hits_exchange and client is not None:
            try:
                applied += await settlements.sync_exchange_settlements(
                    session, client, route=route.value
                )
            except KalshiApiError as exc:
                log.warning("could not sync exchange settlements: %s", exc)

        await session.commit()

    if applied:
        log.info("settled %d position(s)", applied)
    return applied


async def reconcile_order(
    session: AsyncSession, executor: Executor, order: Order
) -> None:
    """Pull an exchange order's current state and record any new fills.

    Fills are deduplicated on ``exchange_fill_id``: the exchange is the
    source of truth and this may run many times over the same order, so
    applying a fill twice would corrupt the position permanently.
    """
    client = executor.rest
    if client is None or not order.exchange_order_id:
        return

    known = set(
        (
            await session.execute(
                select(Fill.exchange_fill_id).where(Fill.order_id == order.id)
            )
        ).scalars().all()
    )

    async for raw in client.get_fills(order_id=order.exchange_order_id, max_pages=2):
        fill_id = raw.get("fill_id") or raw.get("trade_id")
        if not fill_id or fill_id in known:
            continue

        count = parse_count(raw.get("count_fp") or "0", "count_fp")
        if count <= 0:
            continue

        # The wire quotes both a yes and a no price; take the one for the
        # side this order is on so the stored price matches the ticket.
        yes_price = parse_dollars(raw.get("yes_price_dollars"), "yes_price_dollars")

        fill = Fill(
            order_id=order.id,
            exchange_fill_id=fill_id,
            ticker=order.ticker,
            side=order.side,
            action=order.action,
            price=from_yes_price(order.side, yes_price),
            contracts=count,
            fee_cents=fee_cents_from_dollars(raw.get("fee_cost")),
            is_taker=bool(raw.get("is_taker", True)),
        )
        session.add(fill)
        await session.flush()
        known.add(fill_id)

        order.filled_contracts = (order.filled_contracts or Decimal(0)) + count
        await positions.apply_fill(session, fill, route=order.route)
        await proposals.audit(
            session,
            kind="fill.reconciled",
            ticker=order.ticker,
            actor="system",
            payload={
                "order_id": order.id,
                "exchange_fill_id": fill_id,
                "price": str(fill.price),
                "contracts": str(count),
                "fee_cents": fill.fee_cents,
            },
        )

    # Then take the exchange's word for the order's own status.
    try:
        remote = await client.get_order(order.exchange_order_id)
    except KalshiApiError as exc:
        if exc.status != 404:
            raise
        return

    status = str(remote.get("status") or "").lower()
    if status == "canceled":
        order.status = OrderStatus.CANCELED
    elif status == "executed":
        order.status = OrderStatus.FILLED
    elif (order.filled_contracts or Decimal(0)) > 0:
        order.status = OrderStatus.PARTIALLY_FILLED

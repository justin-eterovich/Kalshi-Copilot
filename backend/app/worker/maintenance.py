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
- **Recover orders whose POST never returned an ID.** A write that times out
  or 500s may still have reached the matching engine, so the REST client
  raises instead of retrying — and this is the other half of that rule.
  Without it, an order that may be live at Kalshi was recorded locally as
  REJECTED and never looked at again: REJECTED is not a live status, and
  ``reconcile_order`` returns immediately when there is no exchange order ID,
  which is exactly the timed-out case. Recovery is by **client order ID**,
  never a second POST.

The kill switch is enforced here rather than only at approval time: engaging
it must also retire orders that are already working, otherwise "halt
everything" would leave live exposure sitting on the book.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Config
from app.core.logging import get_logger
from app.core.money import parse_count, parse_dollars
from app.core.redis import get_kill_switch
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
    "recover_orphaned_orders",
]

#: How long after creation an order with no exchange ID is still worth asking
#: the exchange about. Beyond this the order is either long gone or its
#: pages have rolled off what `GET /portfolio/orders` will show us.
ORPHAN_LOOKBACK_SEC = 3600

#: Ceiling on client-order-ID lookups per sweep. Each one is a *paginated*
#: read against the account's order history, so this is a rate-limit bound
#: rather than a correctness one — an order missed this sweep is picked up on
#: the next.
MAX_ORPHAN_CHECKS = 5

#: Times one order is looked up before this process stops asking.
#:
#: The two reasons an order has no exchange ID are indistinguishable from the
#: database alone: the exchange refused it outright (nothing to find, ever) or
#: the response never arrived (something may be resting right now). Without a
#: bound, every genuine rejection would be re-scanned every sweep forever.
#: A restart resets the count, which is the right direction — after a crash we
#: would rather look again than assume.
MAX_ORPHAN_ATTEMPTS = 3

#: order id -> lookups already spent on it, for this process only.
_orphan_attempts: dict[int, int] = {}


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

    # The runtime kill switch, not just the config file. This is the half of
    # the switch that actually cancels resting orders, and it runs in the
    # worker — a different process, with its own `@lru_cache`d config. While
    # this read `config.risk.kill_switch` only, engaging the switch by editing
    # config.yaml and restarting `api` alone left every resting order working
    # while the dashboard reported "engaged".
    halted = await get_kill_switch() or config.risk.kill_switch

    async with sessions() as session:
        # First, adopt anything the exchange is holding under one of our client
        # order IDs. A recovered order becomes RESTING, so the age and
        # kill-switch rules below get to act on it in this same sweep rather
        # than ten seconds later.
        try:
            await recover_orphaned_orders(session, executor, now=now)
        except KalshiApiError as exc:
            log.warning("orphan recovery pass failed: %s", exc)

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
            if not (expired or halted):
                continue

            reason = (
                "kill switch engaged"
                if halted
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


async def recover_orphaned_orders(
    session: AsyncSession, executor: Executor, *, now: datetime | None = None
) -> int:
    """Ask the exchange what it holds under our client order IDs.

    This is the recovery path the no-retry rule depends on. ``rest.request``
    raises on a write timeout or 5xx because a second POST could double-place,
    which leaves a local row with **no exchange order ID** describing an order
    that may or may not exist at Kalshi. Until this pass existed the row was
    marked REJECTED and abandoned: not a live status, so no sweep revisited it,
    and ``reconcile_order`` returns early without an exchange ID anyway. The
    documented promise — "recovery is reconciliation by client order ID" — was
    written in three places and implemented in none.

    A match is *adopted*, not re-placed: the exchange's order ID is written
    onto our row and the order is treated as live until
    :func:`reconcile_order` says otherwise. Assuming live is the safe
    direction — an order wrongly believed dead is unmanaged exposure, while
    one wrongly believed alive is cancelled harmlessly on the next sweep.

    Nothing here can cause an order to exist. It only reads.

    Returns the number of orders recovered.
    """
    client = executor.rest
    if client is None or not client.authenticated:
        return 0

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=ORPHAN_LOOKBACK_SEC)

    candidates = (
        await session.execute(
            select(Order)
            .where(
                or_(
                    Order.exchange_order_id.is_(None),
                    Order.exchange_order_id == "",
                ),
                Order.route != ExecutionRoute.SIMULATED.value,
                # PENDING never got an answer; REJECTED is where a timed-out
                # placement lands. Both are "we do not know", and neither is
                # visited by any other sweep.
                Order.status.in_((OrderStatus.PENDING, OrderStatus.REJECTED)),
                Order.created_at.isnot(None),
                Order.created_at >= cutoff,
            )
            .order_by(Order.created_at.desc())
            # Fetched wider than MAX_ORPHAN_CHECKS because rows whose attempts
            # are already spent are skipped in Python, and a run of those must
            # not starve the ones still worth asking about.
            .limit(MAX_ORPHAN_CHECKS * 10)
        )
    ).scalars().all()

    recovered = 0
    checked = 0
    for order in candidates:
        if checked >= MAX_ORPHAN_CHECKS:
            break
        attempts = _orphan_attempts.get(order.id, 0)
        if attempts >= MAX_ORPHAN_ATTEMPTS:
            continue

        _orphan_attempts[order.id] = attempts + 1
        checked += 1

        created = order.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        # A minute of slack either side of our own clock: the exchange stamps
        # the order, not us, and a boundary miss here reads as "never placed".
        min_ts = int(created.timestamp()) - 60 if created else None

        try:
            remote = await client.find_order_by_client_id(
                order.client_order_id, ticker=order.ticker, min_ts=min_ts
            )
        except KalshiApiError as exc:
            log.warning(
                "could not look up order %s by client order ID: %s", order.id, exc
            )
            continue

        if remote is None:
            if attempts + 1 >= MAX_ORPHAN_ATTEMPTS:
                log.info(
                    "order %s (%s) not found at the exchange under client order "
                    "ID %s after %d lookups; treating the local %s as final",
                    order.id,
                    order.ticker,
                    order.client_order_id,
                    MAX_ORPHAN_ATTEMPTS,
                    order.status.value,
                )
            continue

        exchange_id = str(remote.get("order_id") or "").strip()
        if not exchange_id:
            # Matched our ID but carries no order ID of its own. Nothing here
            # can be reconciled against, and inventing one would attach fills
            # to the wrong row.
            log.warning(
                "order %s matched client order ID %s but the exchange payload "
                "has no order_id; leaving it alone",
                order.id,
                order.client_order_id,
            )
            continue

        previous = order.status
        order.exchange_order_id = exchange_id
        order.status = OrderStatus.RESTING
        recovered += 1
        log.warning(
            "recovered order %s (%s): the exchange holds %s under client order "
            "ID %s, but it was recorded locally as %s. The write that placed "
            "it never returned.",
            order.id,
            order.ticker,
            exchange_id,
            order.client_order_id,
            previous.value,
        )
        await proposals.audit(
            session,
            kind="order.recovered",
            ticker=order.ticker,
            actor="system",
            payload={
                "order_id": order.id,
                "exchange_order_id": exchange_id,
                "client_order_id": order.client_order_id,
                "previous_status": previous.value,
                "remote_status": str(remote.get("status") or ""),
            },
        )

        # Pull its fills and its real status now, so a recovered order that
        # already filled does not spend a sweep looking resting.
        try:
            await reconcile_order(session, executor, order)
        except KalshiApiError as exc:
            log.warning(
                "recovered order %s but could not reconcile it: %s", order.id, exc
            )

    return recovered


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

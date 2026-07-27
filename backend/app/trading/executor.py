"""The execution rail: approved proposal in, order and fills out.

This is the only module in the system that can cause an order to exist.  It
is reached from exactly one place — a human approving one specific proposal —
and it re-checks every interlock itself rather than trusting the caller,
because an interlock that lives only in the API layer is an interlock that
the next caller forgets.

Ordering of operations is load-bearing:

1. **Check the interlocks** (again).
2. **Write the Order row first, then place.**  The row carries the client
   order ID, so if the process dies between the two steps there is a record
   of an order that may exist on the exchange, findable by that ID.  Placing
   first and recording after would lose it.
3. **Never retry a placement.**  The REST client raises on a write timeout
   instead of retrying, because a timed-out order may have been accepted.
   The recovery path is reconciliation by client order ID, not a second POST.
4. **One order per proposal.**  A proposal with a live order refuses to
   produce another, which is what makes a double-clicked approve button
   harmless.

Fills are recorded per fill, with the fee per fill, because that is how the
exchange charges: an order that sweeps three levels rounds its fee up three
times.  Collapsing them into an average would understate the cost of size in
exactly the thin markets this system trades.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.core.money import format_count, parse_count, parse_dollars
from app.core.redis import CH_ORDERS, get_redis
from app.db.models import (
    Fill,
    Order,
    OrderStatus,
    ProposalStatus,
    ProposedTrade,
    Side,
)
from app.kalshi.rest import TIF_GTC, TIF_IOC, KalshiApiError, KalshiRestClient
from app.settings import Settings
from app.trading import paper, positions, proposals
from app.trading.direction import book_side, to_yes_price
from app.trading.interlocks import ExecutionRoute, InterlockError, check_execution

log = get_logger(__name__)

__all__ = [
    "Executor",
    "ExecutionError",
    "LIVE_ORDER_STATUSES",
    "fee_cents_from_dollars",
    "order_view",
    "fill_view",
]

#: Order states that are still working and must not be duplicated.
LIVE_ORDER_STATUSES = (
    OrderStatus.PENDING,
    OrderStatus.RESTING,
    OrderStatus.PARTIALLY_FILLED,
)

_TIF_WIRE = {"gtc": TIF_GTC, "ioc": TIF_IOC}

def wire_price(value: Decimal) -> str:
    """Serialise a price for the order API at its own natural precision.

    The exchange validates the *string's* decimal exponent against the
    market's price level structure and rejects anything finer. Zero-padding
    to a fixed width is therefore not harmless: sending ``"0.250000"`` to a
    ``linear_cent`` market fails with ``invalid dollar precision: -6``, even
    though the value is exactly 25c. Observed against the live demo API on
    the first real order placed.

    Padding is not the same as precision. ``normalize`` strips trailing
    zeros, so 25c goes as ``"0.25"`` while a genuine sub-cent price such as
    0.1234 keeps all four places. The ``f`` format is what stops ``normalize``
    emitting exponent notation.
    """
    return format(value.normalize(), "f")


class ExecutionError(RuntimeError):
    """Placement failed. ``code`` is stable for the UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def fee_cents_from_dollars(value: Any) -> Decimal:
    """Convert an API fee (fixed-point dollars) into cents, exactly.

    No rounding. Kalshi bills fractional cents — 2 contracts at 20c cost
    $0.022400, which is 2.24 cents, not 2 and not 3. Rounding to an integer
    here loses real money from the record in whichever direction it rounds,
    and the whole point of this column is to say what we were actually
    charged rather than what we predicted.
    """
    if value in (None, ""):
        return Decimal(0)
    return parse_dollars(value, "fee_dollars") * Decimal(100)


class Executor:
    """Places approved proposals. One instance per service."""

    def __init__(
        self,
        rest: KalshiRestClient | None,
        settings: Settings,
        config: Config,
    ) -> None:
        self._rest = rest
        self._settings = settings
        self._config = config

    @property
    def rest(self) -> KalshiRestClient | None:
        """The REST client, or None when no credentials are configured."""
        return self._rest

    # -- approval --------------------------------------------------------

    async def approve_and_execute(
        self,
        session: AsyncSession,
        proposal: ProposedTrade,
        *,
        confirmed: bool,
        confirmation_phrase: str | None = None,
        actor: str = "operator",
    ) -> Order:
        """Approve one proposal and place its order.

        Raises:
            InterlockError: a safety check refused. Nothing is placed and the
                proposal stays pending, so it can be approved again once the
                condition clears (or expire on its own).
            ExecutionError: placement itself failed. The proposal is marked
                FAILED and the order row records why.
        """
        route = check_execution(
            proposal,
            self._settings,
            self._config,
            confirmed=confirmed,
            confirmation_phrase=confirmation_phrase,
        )

        existing = await self._live_order_for(session, proposal)
        if existing is not None:
            # A double-click, or a retry after a UI timeout. Returning the
            # order that already exists is the whole point of recording it
            # before placing it.
            log.warning(
                "proposal %s already has order %s (%s); not placing a second",
                proposal.id, existing.id, existing.status.value,
            )
            return existing

        proposal.status = ProposalStatus.APPROVED
        proposal.decided_at = datetime.now(UTC)
        proposal.decision_reason = f"approved by {actor}"

        await proposals.audit(
            session,
            kind="proposal.approved",
            ticker=proposal.ticker,
            actor=actor,
            payload={
                "proposal_id": proposal.id,
                "route": route.value,
                "limit_price": str(proposal.limit_price),
                "contracts": str(proposal.contracts),
                "side": proposal.side.value,
                "action": proposal.action,
            },
        )

        try:
            order = await self._place(session, proposal, route, actor=actor)
        except ExecutionError:
            # The proposal was already marked APPROVED above. Leaving it there
            # would strand it: approved, no working order, and invisible to
            # both the queue and the expiry sweep. Mark the outcome before
            # re-raising so the state on disk matches what happened.
            proposal.status = ProposalStatus.FAILED
            proposal.decision_reason = "approved, but placement failed"
            await proposals.publish(proposal, event=ProposalStatus.FAILED.value)
            raise

        proposal.status = ProposalStatus.EXECUTED
        await proposals.publish(proposal, event=proposal.status.value)
        return order

    async def _live_order_for(
        self, session: AsyncSession, proposal: ProposedTrade
    ) -> Order | None:
        return (
            await session.execute(
                select(Order)
                .where(
                    Order.proposal_id == proposal.id,
                    Order.status.in_(LIVE_ORDER_STATUSES),
                )
                .order_by(Order.id.desc())
                .limit(1)
            )
        ).scalars().first()

    # -- placement -------------------------------------------------------

    async def _place(
        self,
        session: AsyncSession,
        proposal: ProposedTrade,
        route: ExecutionRoute,
        *,
        actor: str,
    ) -> Order:
        tif = self._config.trading.order.time_in_force

        order = Order(
            proposal_id=proposal.id,
            # The idempotency key. Generated and persisted before anything
            # leaves the process, so a crash mid-flight is recoverable.
            client_order_id=str(uuid.uuid4()),
            ticker=proposal.ticker,
            side=proposal.side,
            action=proposal.action,
            limit_price=proposal.limit_price,
            contracts=proposal.contracts,
            filled_contracts=Decimal(0),
            time_in_force=tif,
            status=OrderStatus.PENDING,
            is_paper=route.is_paper,
            route=route.value,
        )
        session.add(order)
        await session.flush()

        await proposals.audit(
            session,
            kind="order.submitted",
            ticker=order.ticker,
            actor=actor,
            payload={
                "order_id": order.id,
                "client_order_id": order.client_order_id,
                "route": route.value,
                "wire": {
                    "book_side": book_side(order.side, order.action),
                    "yes_price": str(to_yes_price(order.side, order.limit_price)),
                    "count": str(order.contracts),
                    "time_in_force": tif,
                },
            },
        )

        try:
            if route is ExecutionRoute.SIMULATED:
                await self._fill_simulated(session, order, proposal)
            else:
                await self._fill_exchange(session, order)
        except InterlockError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            order.status = OrderStatus.REJECTED
            order.error = str(exc)[:2000]
            await proposals.audit(
                session,
                kind="order.failed",
                ticker=order.ticker,
                actor="system",
                payload={"order_id": order.id, "error": order.error},
            )
            log.exception("order %s failed: %s", order.id, exc)
            raise ExecutionError("placement_failed", str(exc)) from exc

        await self._publish_order(order)
        return order

    # -- simulated fills -------------------------------------------------

    async def _fill_simulated(
        self, session: AsyncSession, order: Order, proposal: ProposedTrade
    ) -> None:
        """Fill against the live book locally. No API call is made."""
        book = await self._current_book(order.ticker)

        if book is None:
            # No book means nothing to fill against. The order rests and the
            # auto-cancel sweep will retire it — the same thing that happens
            # to an unmarketable real order.
            order.status = OrderStatus.RESTING
            log.info("order %s: no book available, resting", order.id)
            return

        sim = paper.simulate_fills(
            book=book,
            side=order.side,
            action=order.action,
            limit_price=order.limit_price,
            contracts=order.contracts,
            ticker=order.ticker,
            slippage_cents=Decimal(str(self._config.costs.slippage_buffer_cents)),
        )

        for index, hit in enumerate(sim):
            await self._record_fill(
                session,
                order,
                price=hit.price,
                contracts=hit.contracts,
                fee_cents=hit.fee_cents,
                exchange_fill_id=f"sim-{order.client_order_id}-{index}",
                is_taker=True,
            )

        self._settle_status(order)

    async def _current_book(self, ticker: str) -> dict[str, Any] | None:
        """Fetch the current book over public REST.

        Public market data needs no credentials, so the simulator works on a
        stack with no API key at all.
        """
        if self._rest is None:
            return None
        try:
            raw = await self._rest.get_orderbook(ticker)
        except KalshiApiError as exc:
            log.warning("could not fetch book for %s: %s", ticker, exc)
            return None
        return {
            "yes": raw.get("yes_dollars") or raw.get("yes") or [],
            "no": raw.get("no_dollars") or raw.get("no") or [],
        }

    # -- exchange fills --------------------------------------------------

    async def _fill_exchange(self, session: AsyncSession, order: Order) -> None:
        """Place a real order on the demo or live exchange."""
        if self._rest is None:
            raise ExecutionError(
                "no_client", "no Kalshi REST client is configured in this process"
            )

        wire_side = book_side(order.side, order.action)
        # The wire always speaks YES prices, whichever side we are taking.
        price_yes = to_yes_price(order.side, order.limit_price)

        log.warning(
            "placing %s order: %s %s %s @ %s (wire: %s @ %s) route=%s",
            order.route, order.action, order.contracts, order.side.value,
            order.limit_price, wire_side, wire_price(price_yes), order.route,
        )

        response = await self._rest.create_order(
            ticker=order.ticker,
            book_side=wire_side,
            price_dollars=wire_price(price_yes),
            count=format_count(order.contracts),
            client_order_id=order.client_order_id,
            time_in_force=_TIF_WIRE.get(order.time_in_force, TIF_GTC),
        )

        order.exchange_order_id = response.get("order_id")

        filled = parse_count(response.get("fill_count") or "0", "fill_count")
        if filled > 0:
            avg_price_yes = parse_dollars(
                response.get("average_fill_price"), "average_fill_price"
            )
            # average_fee_paid is per contract; the total is what we owe.
            fee_per_contract = response.get("average_fee_paid")
            fee_cents = (
                fee_cents_from_dollars(
                    parse_dollars(fee_per_contract, "average_fee_paid") * filled
                )
                if fee_per_contract not in (None, "")
                else Decimal(0)
            )
            await self._record_fill(
                session,
                order,
                # Back into the side's own units for storage and display.
                price=to_yes_price(order.side, avg_price_yes),
                contracts=filled,
                fee_cents=fee_cents,
                exchange_fill_id=f"{order.exchange_order_id}-immediate",
                is_taker=True,
            )

        self._settle_status(order)

    # -- shared ----------------------------------------------------------

    async def _record_fill(
        self,
        session: AsyncSession,
        order: Order,
        *,
        price: Decimal,
        contracts: Decimal,
        fee_cents: Decimal | int,
        exchange_fill_id: str,
        is_taker: bool,
    ) -> Fill:
        fill = Fill(
            order_id=order.id,
            exchange_fill_id=exchange_fill_id,
            ticker=order.ticker,
            side=order.side,
            action=order.action,
            price=price,
            contracts=contracts,
            fee_cents=fee_cents,
            is_taker=is_taker,
        )
        session.add(fill)
        await session.flush()

        order.filled_contracts = (order.filled_contracts or Decimal(0)) + contracts

        await positions.apply_fill(session, fill, route=order.route)
        await proposals.audit(
            session,
            kind="fill.recorded",
            ticker=order.ticker,
            actor="system",
            payload={
                "order_id": order.id,
                "fill_id": fill.id,
                "price": str(price),
                "contracts": str(contracts),
                "fee_cents": str(fee_cents),
                "route": order.route,
            },
        )
        return fill

    @staticmethod
    def _settle_status(order: Order) -> None:
        """Derive order status from what filled.

        The exchange's own status enum has only resting/canceled/executed, so
        "partially filled" is something we work out rather than read.
        """
        filled = order.filled_contracts or Decimal(0)
        if filled >= order.contracts:
            order.status = OrderStatus.FILLED
        elif filled > 0:
            order.status = OrderStatus.PARTIALLY_FILLED
        else:
            order.status = OrderStatus.RESTING

    # -- cancellation ----------------------------------------------------

    async def cancel(
        self, session: AsyncSession, order: Order, *, reason: str, actor: str = "system"
    ) -> Order:
        """Cancel a resting order. Idempotent from the caller's point of view.

        A cancel that fails because the order is already gone is a success:
        the desired state — no resting order — has been reached.
        """
        if order.status not in LIVE_ORDER_STATUSES:
            return order

        if order.route != ExecutionRoute.SIMULATED.value and order.exchange_order_id:
            if self._rest is None:
                raise ExecutionError("no_client", "no REST client to cancel with")
            try:
                await self._rest.cancel_order(
                    order.exchange_order_id, market_ticker=order.ticker
                )
            except KalshiApiError as exc:
                if exc.status not in (404, 400):
                    raise
                log.info(
                    "order %s already gone at the exchange (%s); treating the "
                    "cancel as done", order.id, exc.status,
                )

        order.status = OrderStatus.CANCELED
        order.error = None
        await proposals.audit(
            session,
            kind="order.canceled",
            ticker=order.ticker,
            actor=actor,
            payload={"order_id": order.id, "reason": reason},
        )
        await self._publish_order(order)
        return order

    async def _publish_order(self, order: Order) -> None:
        try:
            await get_redis().publish(CH_ORDERS, json.dumps(order_view(order)))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not publish order %s: %s", order.id, exc)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def order_view(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "proposal_id": order.proposal_id,
        "client_order_id": order.client_order_id,
        "exchange_order_id": order.exchange_order_id,
        "ticker": order.ticker,
        "side": order.side.value if isinstance(order.side, Side) else str(order.side),
        "action": order.action,
        "limit_price": str(order.limit_price),
        "contracts": str(order.contracts),
        "filled_contracts": str(order.filled_contracts or Decimal(0)),
        "time_in_force": order.time_in_force,
        "status": order.status.value,
        "is_paper": order.is_paper,
        "route": order.route,
        "error": order.error,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def fill_view(fill: Fill) -> dict[str, Any]:
    return {
        "id": fill.id,
        "order_id": fill.order_id,
        "exchange_fill_id": fill.exchange_fill_id,
        "ticker": fill.ticker,
        "side": fill.side.value if isinstance(fill.side, Side) else str(fill.side),
        "action": fill.action,
        "price": str(fill.price),
        "contracts": str(fill.contracts),
        "fee_cents": str(fill.fee_cents),
        "is_taker": fill.is_taker,
        "ts": fill.ts.isoformat() if fill.ts else None,
    }

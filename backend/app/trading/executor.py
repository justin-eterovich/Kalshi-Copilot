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
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.core.money import format_count, parse_count, parse_dollars
from app.core.redis import CH_ORDERS, get_kill_switch, get_redis
from app.db.models import (
    Fill,
    Order,
    OrderStatus,
    ProposalLeg,
    ProposalStatus,
    ProposedTrade,
    Side,
)
from app.kalshi.rest import TIF_GTC, TIF_IOC, KalshiApiError, KalshiRestClient
from app.settings import Settings
from app.trading import paper, positions, proposals, risk
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

#: Namespace for deterministic client order IDs. Arbitrary but fixed — it only
#: has to be stable across restarts of this deployment.
_COID_NAMESPACE: Final = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _client_order_id(proposal: ProposedTrade, seq: int) -> str:
    """A client order ID that is the same every time for the same leg.

    README calls client-supplied order IDs the reason "a network retry cannot
    double-place". That was not true while this was ``uuid.uuid4()`` per
    placement attempt: two approvals of one proposal produced two different
    IDs, so the exchange had no basis to reject the second and duly filled
    both.

    Deriving it from ``(proposal_id, leg_seq)`` makes the exchange itself the
    last line of defence behind the row lock in ``approve_and_execute``.
    ``created_at`` is folded in so that a rebuilt database — which restarts
    the proposal ID sequence — cannot mint an ID that collides with a real
    historical order still known to the exchange.
    """
    created = getattr(proposal, "created_at", None)
    if proposal.id is None or created is None:
        # Not yet persisted: nothing stable to derive from, and a random ID is
        # strictly better than a colliding one.
        return str(uuid.uuid4())
    return str(
        uuid.uuid5(_COID_NAMESPACE, f"{proposal.id}:{seq}:{created.isoformat()}")
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
                condition clears (or expire on its own). ``RiskError`` is a
                subclass, so the portfolio limits refuse the same way.
            ExecutionError: placement itself failed. The proposal is marked
                FAILED and the order row records why.
        """
        # Take a row lock on the proposal BEFORE any check reads its status.
        #
        # Every guard below is `if status is not PENDING: refuse`, evaluated
        # against whatever this session last read. Without a lock two
        # concurrent approvals both read PENDING, both pass, and both place —
        # verified against the live demo exchange: one proposal, two distinct
        # exchange order IDs, two fills, two contracts where the operator
        # authorised one. A double-click is inside the window, which spans the
        # whole exchange round-trip.
        #
        # `populate_existing` is load-bearing. `proposal` is already in the
        # identity map, and without it SQLAlchemy hands back the stale
        # in-memory attributes and the lock protects nothing.
        #
        # The lock is held across placement until the caller commits. That is
        # deliberate: a concurrent approver blocks, then re-reads APPROVED and
        # is refused by `check_execution` below. Holding one row for the
        # duration of an exchange round-trip is the cheap side of this trade.
        if proposal.id is not None:
            locked = (
                await session.execute(
                    select(ProposedTrade)
                    .where(ProposedTrade.id == proposal.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars().one_or_none()
            if locked is not None:
                proposal = locked

        route = check_execution(
            proposal,
            self._settings,
            self._config,
            confirmed=confirmed,
            kill_switch=await get_kill_switch(),
            confirmation_phrase=confirmation_phrase,
        )

        # The portfolio limits, which no single proposal can check about
        # itself. They live here rather than in the API layer for the same
        # reason as everything above: this is the only function that can cause
        # an order to exist, so it is the only place a check cannot be
        # sidestepped. A refusal leaves the proposal pending — the limits are
        # all temporary, and the trade may be fine in an hour.
        leg_tickers = (
            await session.execute(
                select(ProposalLeg.ticker).where(
                    ProposalLeg.proposal_id == proposal.id
                )
            )
        ).scalars().all()
        if not leg_tickers and proposal.ticker:
            leg_tickers = [proposal.ticker]

        await risk.guard_approval(
            session,
            self._config,
            self._settings,
            max_loss_cents=proposal.max_loss_cents,
            tickers=leg_tickers,
            event_ticker=proposal.event_ticker,
            proposal_id=proposal.id,
        )

        existing = await self._existing_order_for(session, proposal)
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
                "event_ticker": proposal.event_ticker,
                "leg_count": proposal.leg_count,
            },
        )

        try:
            orders = await self._place_legs(session, proposal, route, actor=actor)
        except ExecutionError:
            # The proposal was already marked APPROVED above. Leaving it there
            # would strand it: approved, no working order, and invisible to
            # both the queue and the expiry sweep. Mark the outcome before
            # re-raising so the state on disk matches what happened.
            proposal.status = ProposalStatus.FAILED
            proposal.decision_reason = "approved, but placement failed"
            await proposals.publish(proposal, event=ProposalStatus.FAILED.value)
            raise

        proposal.status = self._outcome(orders)
        if proposal.status is ProposalStatus.PARTIAL:
            # Real, and it needs a person: the legs no longer hedge each other.
            filled = [o.ticker for o in orders if (o.filled_contracts or 0) > 0]
            proposal.decision_reason = (
                f"UNBALANCED: {len(filled)} of {len(orders)} legs executed "
                f"({', '.join(filled)}). The remaining legs did not, so this "
                f"is now a directional position, not a hedge."
            )
            log.error(
                "proposal %s is unbalanced: %d/%d legs executed",
                proposal.id, len(filled), len(orders),
            )
        await proposals.publish(proposal, event=proposal.status.value)
        return orders[0]

    @staticmethod
    def _outcome(orders: list[Order]) -> ProposalStatus:
        """EXECUTED, PARTIAL or FAILED, judged across every leg.

        A single-leg proposal can only be executed or failed. A multi-leg one
        has a third outcome that matters more than either: some legs on, some
        off. The exchange has no atomic multi-order primitive, so that is a
        genuine state rather than an error to swallow.
        """
        if not orders:
            return ProposalStatus.FAILED
        if all(o.status is OrderStatus.REJECTED for o in orders):
            return ProposalStatus.FAILED
        if len(orders) == 1:
            return ProposalStatus.EXECUTED

        filled = [(o.filled_contracts or Decimal(0)) > 0 for o in orders]
        if all(filled) or not any(filled):
            # All legs on, or none — either way the set is balanced.
            return ProposalStatus.EXECUTED
        return ProposalStatus.PARTIAL

    async def _existing_order_for(
        self, session: AsyncSession, proposal: ProposedTrade
    ) -> Order | None:
        """Any order this proposal has already caused to exist.

        Deliberately unfiltered by status. This used to select only
        ``LIVE_ORDER_STATUSES`` (pending/resting/partially_filled), which
        excludes ``FILLED`` — the normal outcome for a taker order on a liquid
        book — so the guard did not fire in precisely the common case.

        ``REJECTED`` counts too, and that is the important one: a write that
        timed out is recorded REJECTED but **may still have reached the
        matching engine**. Treating it as licence to place again is the
        double-submit this codebase refuses to do anywhere else. If an order
        row exists at all, recovery is reconciliation, never a second POST.
        """
        return (
            await session.execute(
                select(Order)
                .where(Order.proposal_id == proposal.id)
                .order_by(Order.id.desc())
                .limit(1)
            )
        ).scalars().first()

    # -- placement -------------------------------------------------------

    async def _place_legs(
        self,
        session: AsyncSession,
        proposal: ProposedTrade,
        route: ExecutionRoute,
        *,
        actor: str,
    ) -> list[Order]:
        """Place every leg of the proposal.

        Multi-leg proposals go out in **one batch request with IOC**. That is
        the strongest guarantee available: the exchange has no atomic
        multi-order primitive, so the batch endpoint only collapses N round
        trips into one, and IOC stops any leg resting half-done. Leg risk is
        reduced, not removed, and an imbalance is reported rather than hidden.
        """
        legs = (
            await session.execute(
                select(ProposalLeg)
                .where(ProposalLeg.proposal_id == proposal.id)
                .order_by(ProposalLeg.seq)
            )
        ).scalars().all()
        if not legs:
            raise ExecutionError("no_legs", f"proposal {proposal.id} has no legs")

        multi = len(legs) > 1
        # A resting leg of an arb is an unhedged option written for free.
        tif = "ioc" if multi else self._config.trading.order.time_in_force

        orders: list[Order] = []
        for leg in legs:
            order = Order(
                proposal_id=proposal.id,
                leg_id=leg.id,
                # The idempotency key. Generated and persisted before anything
                # leaves the process, so a crash mid-flight is recoverable.
                client_order_id=_client_order_id(proposal, leg.seq),
                ticker=leg.ticker,
                side=leg.side,
                action=leg.action,
                limit_price=leg.limit_price,
                contracts=leg.contracts,
                filled_contracts=Decimal(0),
                time_in_force=tif,
                status=OrderStatus.PENDING,
                is_paper=route.is_paper,
                route=route.value,
            )
            session.add(order)
            orders.append(order)
        await session.flush()

        await proposals.audit(
            session,
            kind="order.submitted",
            ticker=proposal.ticker,
            actor=actor,
            payload={
                "proposal_id": proposal.id,
                "route": route.value,
                "leg_count": len(orders),
                "atomic": False,
                "time_in_force": tif,
                "legs": [
                    {
                        "order_id": o.id,
                        "client_order_id": o.client_order_id,
                        "ticker": o.ticker,
                        "book_side": book_side(o.side, o.action),
                        "yes_price": wire_price(to_yes_price(o.side, o.limit_price)),
                        "count": str(o.contracts),
                    }
                    for o in orders
                ],
            },
        )

        try:
            if route is ExecutionRoute.SIMULATED:
                for order in orders:
                    await self._fill_simulated(session, order)
            elif multi:
                await self._fill_exchange_batch(session, orders, tif)
            else:
                await self._fill_exchange(session, orders[0], tif)
        except InterlockError:
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            # A definite refusal and an ambiguous one are different facts and
            # must not be flattened into REJECTED together.
            #
            # A 4xx is the exchange saying "I did not accept this". A network
            # error or a 5xx says nothing at all: the order may well have
            # reached the matching engine, which is exactly why writes are
            # never retried. Marking those REJECTED asserts something we do
            # not know, and REJECTED is a terminal state the order sweep never
            # revisits — so a live order could sit on the book with a local
            # row claiming it was refused.
            #
            # Ambiguous failures stay PENDING so the reconciliation pass can
            # look them up by client order ID and find out what actually
            # happened.
            status_code = getattr(exc, "status", None)
            ambiguous = not (
                isinstance(status_code, int) and 400 <= status_code < 500
            )
            for order in orders:
                if order.status is OrderStatus.PENDING:
                    order.error = str(exc)[:2000]
                    if not ambiguous:
                        order.status = OrderStatus.REJECTED
            await proposals.audit(
                session,
                kind="order.submit_ambiguous" if ambiguous else "order.failed",
                ticker=proposal.ticker,
                actor="system",
                payload={
                    "proposal_id": proposal.id,
                    "error": str(exc)[:500],
                    "status": status_code,
                    "resolution": (
                        "left PENDING for reconciliation by client order ID"
                        if ambiguous
                        else "definitively rejected by the exchange"
                    ),
                },
            )
            log.exception("proposal %s placement failed: %s", proposal.id, exc)
            raise ExecutionError("placement_failed", str(exc)) from exc

        for order in orders:
            await self._publish_order(order)
        return orders

    # -- simulated fills -------------------------------------------------

    async def _fill_simulated(self, session: AsyncSession, order: Order) -> None:
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

    def _wire_order(self, order: Order, tif: str) -> dict[str, Any]:
        """One order in the shape the V2 API wants."""
        return {
            "ticker": order.ticker,
            "book_side": book_side(order.side, order.action),
            # The wire always speaks YES prices, whichever side we take.
            "price_dollars": wire_price(to_yes_price(order.side, order.limit_price)),
            "count": format_count(order.contracts),
            "client_order_id": order.client_order_id,
            "time_in_force": _TIF_WIRE.get(tif, TIF_GTC),
        }

    async def _fill_exchange(
        self, session: AsyncSession, order: Order, tif: str
    ) -> None:
        """Place a single real order on the demo or live exchange."""
        if self._rest is None:
            raise ExecutionError(
                "no_client", "no Kalshi REST client is configured in this process"
            )

        wire = self._wire_order(order, tif)
        log.warning(
            "placing %s order: %s %s %s @ %s (wire: %s @ %s)",
            order.route, order.action, order.contracts, order.side.value,
            order.limit_price, wire["book_side"], wire["price_dollars"],
        )
        response = await self._rest.create_order(**wire)
        await self._apply_response(session, order, response)

    async def _fill_exchange_batch(
        self, session: AsyncSession, orders: list[Order], tif: str
    ) -> None:
        """Place every leg in one request.

        Not atomic — see ``create_orders_batch``. Responses are matched back
        by client order ID rather than by position, because nothing promises
        the array comes back in the order it went out.
        """
        if self._rest is None:
            raise ExecutionError(
                "no_client", "no Kalshi REST client is configured in this process"
            )

        log.warning(
            "placing %d-leg %s order batch (IOC, NOT atomic): %s",
            len(orders), orders[0].route, ", ".join(o.ticker for o in orders),
        )
        results = await self._rest.create_orders_batch(
            [self._wire_order(o, tif) for o in orders]
        )
        by_client = {
            r.get("client_order_id"): r for r in results if r.get("client_order_id")
        }

        for order in orders:
            response = by_client.get(order.client_order_id)
            if response is None:
                # No result for this leg: it did not go on. Say so rather
                # than leaving it PENDING and letting the sweep guess.
                order.status = OrderStatus.REJECTED
                order.error = "no result returned for this leg in the batch"
                log.error("batch: no result for leg %s", order.ticker)
                continue
            await self._apply_response(session, order, response)

    async def _apply_response(
        self, session: AsyncSession, order: Order, response: dict[str, Any]
    ) -> None:
        """Record whatever the exchange said happened to one order."""
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
                # Keyed off the client order ID, not the exchange's. The
                # exchange ID is absent whenever the create response omits
                # `order_id`, and this then read literally "None-immediate" —
                # identical for every such fill, so `uq_fill_id` collided on
                # the second one and the fill was lost. The client order ID is
                # ours, always present, and unique per leg by construction.
                exchange_fill_id=f"{order.client_order_id}-immediate",
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

        An **IOC order that did not fill is CANCELED, not RESTING** — the
        exchange killed it on arrival and it is not sitting on any book.
        Calling it resting was observed live on an unfilled arb leg: the UI
        showed a working order that did not exist and the auto-cancel sweep
        would have gone looking for a ghost.
        """
        filled = order.filled_contracts or Decimal(0)
        if filled >= order.contracts:
            order.status = OrderStatus.FILLED
        elif filled > 0:
            order.status = (
                OrderStatus.CANCELED
                if order.time_in_force == "ioc"
                else OrderStatus.PARTIALLY_FILLED
            )
        elif order.time_in_force == "ioc":
            order.status = OrderStatus.CANCELED
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
                # 404 is evidence the order is gone. 400 is not — it means the
                # exchange rejected the *request*, and says nothing about
                # whether the order is still resting. Treating it as "already
                # gone" marked the order CANCELED locally while it stayed live
                # on the book, and `sweep_orders` never revisits a CANCELED
                # order, so nothing would ever correct it.
                #
                # That matters most under the kill switch, whose entire promise
                # is that resting orders are gone. Re-raise so the caller
                # reports a cancel it could not confirm rather than claiming
                # one it did not achieve.
                if exc.status != 404:
                    raise
                log.info(
                    "order %s already gone at the exchange (404); treating the "
                    "cancel as done", order.id,
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

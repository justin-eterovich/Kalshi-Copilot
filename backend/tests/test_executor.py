"""Tests for the execution rail.

The executor is the only module that can cause an order to exist, so the
properties here are the ones that keep that from happening by accident:

- Interlocks are re-checked inside the executor, not merely by its caller.
- A proposal that already has a working order never produces a second one,
  which is what makes a double-clicked approve button harmless.
- The order row exists before the placement call, so a crash mid-flight
  leaves something findable by client order ID.
- What goes on the wire is the YES-denominated form of what the operator
  approved, and nothing else.

The session here is a fake rather than a real database: these are assertions
about ordering and control flow, and a live Postgres would obscure them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config import Config
from app.db.models import (
    Market,
    Order,
    OrderStatus,
    ProposalLeg,
    ProposalStatus,
    ProposedTrade,
    Side,
)
from app.settings import KalshiEnv, Settings
from app.trading.executor import (
    ExecutionError,
    Executor,
    fee_cents_from_dollars,
    order_view,
)
from app.trading.interlocks import ExecutionRoute, InterlockError

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Just enough AsyncSession to exercise the executor's control flow."""

    def __init__(self, *, existing_orders: list[Order] | None = None) -> None:
        self.added: list[Any] = []
        self.flushes = 0
        self._existing = existing_orders or []
        self.objects: dict[tuple[type, Any], Any] = {}
        #: Ordering evidence: what had been added by the time of each flush.
        self.flush_snapshots: list[list[Any]] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # Identity columns are assigned by the database; fake them so code
        # that logs an id after flush() has something to log.
        if getattr(obj, "id", None) is None:
            obj.id = len(self.added)

    async def flush(self) -> None:
        self.flushes += 1
        self.flush_snapshots.append(list(self.added))

    async def get(self, model: type, key: Any) -> Any:
        return self.objects.get((model, key))

    #: Legs the executor will find for the proposal under test.
    legs: list[Any] = []

    async def execute(self, stmt: Any) -> FakeResult:
        # Several SELECTs reach here — the two the executor makes ("is there
        # a live order for this proposal", "what are its legs") plus the
        # portfolio queries the risk layer runs before them. Dispatch on the
        # driving table rather than on call order, which would be brittle, and
        # rather than on a substring, which would hand a list of Orders to the
        # risk layer's join against `orders` and unpack it as position rows.
        text = str(stmt).lower()
        if "from proposal_legs" in text:
            return FakeResult(self.legs)
        if "from orders" in text:
            return FakeResult(self._existing)
        # Everything else — positions, pnl_daily, fills, settlements — is an
        # empty book. These tests are about the executor's control flow; the
        # risk limits have their own.
        return FakeResult([])

    def of_type(self, model: type) -> list[Any]:
        return [o for o in self.added if isinstance(o, model)]


class FakeRest:
    """Records what was sent and replays a canned response."""

    def __init__(
        self,
        *,
        book: dict[str, Any] | None = None,
        create_response: dict[str, Any] | None = None,
        create_error: Exception | None = None,
        batch_fill: str = "0.00",
        batch_reversed: bool = False,
        batch_drop_second: bool = False,
        batch_fill_first_only: bool = False,
    ) -> None:
        self.book = book or {"yes_dollars": [], "no_dollars": []}
        self.create_response = create_response or {
            "order_id": "exch-1",
            "fill_count": "0.00",
            "remaining_count": "10.00",
        }
        self.create_error = create_error
        self.create_calls: list[dict[str, Any]] = []
        self.batch_calls: list[list[dict[str, Any]]] = []
        self.cancel_calls: list[str] = []
        self.batch_fill = batch_fill
        self.batch_reversed = batch_reversed
        self.batch_drop_second = batch_drop_second
        self.batch_fill_first_only = batch_fill_first_only

    @property
    def authenticated(self) -> bool:
        return True

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict:
        return self.book

    async def create_order(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return self.create_response

    async def create_orders_batch(
        self, orders: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self.batch_calls.append(orders)
        if self.create_error is not None:
            raise self.create_error
        results = []
        for i, order in enumerate(orders):
            fill = (
                "10.00" if (self.batch_fill_first_only and i == 0)
                else "0.00" if self.batch_fill_first_only
                else self.batch_fill
            )
            entry: dict[str, Any] = {
                "order_id": f"exch-{i}",
                "client_order_id": order["client_order_id"],
                "fill_count": fill,
                "remaining_count": "0.00",
            }
            if fill != "0.00":
                entry["average_fill_price"] = "0.5000"
                entry["average_fee_paid"] = "0.0175"
            results.append(entry)
        if self.batch_drop_second and len(results) > 1:
            results = results[:1]
        if self.batch_reversed:
            results.reverse()
        return results

    async def cancel_order(
        self, order_id: str, *, market_ticker: str | None = None
    ) -> dict:
        self.cancel_calls.append(order_id)
        return {"order_id": order_id, "reduced_by": "10.00"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "key.pem"
    path.write_text("existence is all that is checked")
    return path


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing is best-effort; keep it out of these tests entirely."""

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr("app.trading.proposals.publish", noop)
    monkeypatch.setattr("app.trading.executor.Executor._publish_order", noop)


def demo_settings(key_file: Path) -> Settings:
    settings = Settings(kalshi_env=KalshiEnv.DEMO, kalshi_demo_key_id="demo-key")
    settings.kalshi_demo_private_key_path = key_file
    return settings


def sim_settings() -> Settings:
    """No credentials -> the simulated route."""
    return Settings(kalshi_env=KalshiEnv.DEMO, kalshi_demo_key_id="")


def make_config(**trading: object) -> Config:
    return Config.model_validate({"trading": trading})


def make_proposal(**overrides: object) -> ProposedTrade:
    proposal = ProposedTrade(
        source="manual",
        ticker="TEST-MKT",
        leg_count=1,
        status=ProposalStatus.PENDING,
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    proposal.id = 1
    for key, value in overrides.items():
        setattr(proposal, key, value)
    return proposal


def make_leg(**overrides: Any) -> ProposalLeg:
    leg = ProposalLeg(
        proposal_id=1, seq=0, ticker="TEST-MKT", side=Side.YES, action="buy",
        limit_price=Decimal("0.50"), contracts=Decimal(10),
    )
    leg.id = 1
    for key, value in overrides.items():
        setattr(leg, key, value)
    return leg


def session_with_market(legs: list[ProposalLeg] | None = None, **kwargs: Any):
    session = FakeSession(**kwargs)
    market = Market(ticker="TEST-MKT", category="Sports", status="active")
    session.objects[(Market, "TEST-MKT")] = market
    session.legs = legs if legs is not None else [make_leg()]
    return session


# ---------------------------------------------------------------------------
# Interlocks are enforced here, not only upstream
# ---------------------------------------------------------------------------


class TestInterlocksAtExecution:
    async def test_unconfirmed_approval_places_nothing(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        with pytest.raises(InterlockError) as exc:
            await executor.approve_and_execute(
                session, make_proposal(), confirmed=False
            )

        assert exc.value.code == "not_confirmed"
        assert rest.create_calls == []
        assert session.of_type(Order) == []

    async def test_expired_proposal_places_nothing(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())
        expired = make_proposal(expires_at=datetime.now(UTC) - timedelta(seconds=1))

        with pytest.raises(InterlockError):
            await executor.approve_and_execute(session, expired, confirmed=True)

        assert rest.create_calls == []

    async def test_kill_switch_places_nothing(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest()
        config = Config.model_validate({"risk": {"kill_switch": True}})
        executor = Executor(rest, demo_settings(key_file), config)

        with pytest.raises(InterlockError) as exc:
            await executor.approve_and_execute(
                session, make_proposal(), confirmed=True
            )

        assert exc.value.code == "kill_switch"
        assert rest.create_calls == []

    async def test_a_refused_proposal_stays_pending(self, key_file: Path) -> None:
        """So it can be approved again once the condition clears."""
        session = session_with_market()
        proposal = make_proposal()
        executor = Executor(FakeRest(), demo_settings(key_file), make_config())

        with pytest.raises(InterlockError):
            await executor.approve_and_execute(session, proposal, confirmed=False)

        assert proposal.status is ProposalStatus.PENDING


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    async def test_a_proposal_with_a_working_order_does_not_place_a_second(
        self, key_file: Path
    ) -> None:
        """The double-clicked approve button."""
        existing = Order(
            proposal_id=1,
            client_order_id="already-placed",
            ticker="TEST-MKT",
            side=Side.YES,
            action="buy",
            limit_price=Decimal("0.50"),
            contracts=Decimal(10),
            status=OrderStatus.RESTING,
            route="demo_exchange",
        )
        existing.id = 99
        session = session_with_market(existing_orders=[existing])
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert order is existing
        assert rest.create_calls == []

    async def test_every_order_carries_a_unique_client_order_id(
        self, key_file: Path
    ) -> None:
        """The idempotency key the exchange dedupes on."""
        ids = set()
        for _ in range(5):
            session = session_with_market()
            executor = Executor(FakeRest(), demo_settings(key_file), make_config())
            order = await executor.approve_and_execute(
                session, make_proposal(), confirmed=True
            )
            ids.add(order.client_order_id)
        assert len(ids) == 5

    async def test_the_order_row_exists_before_the_placement_call(
        self, key_file: Path
    ) -> None:
        """A crash mid-flight must leave something findable.

        Asserted by checking that an Order had been added and flushed before
        create_order was reached.
        """
        session = session_with_market()

        class RecordingRest(FakeRest):
            async def create_order(self, **kwargs: Any) -> dict[str, Any]:
                # At this moment the order row must already be persisted.
                assert any(isinstance(o, Order) for o in session.added)
                assert session.flushes >= 1
                return await super().create_order(**kwargs)

        executor = Executor(RecordingRest(), demo_settings(key_file), make_config())
        await executor.approve_and_execute(session, make_proposal(), confirmed=True)


# ---------------------------------------------------------------------------
# What goes on the wire
# ---------------------------------------------------------------------------


class TestWireTranslation:
    async def test_buy_yes_is_sent_as_a_bid(self, key_file: Path) -> None:
        session = session_with_market(
            [make_leg(side=Side.YES, action="buy", limit_price=Decimal("0.56"))]
        )
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        sent = rest.create_calls[0]
        assert sent["book_side"] == "bid"
        assert Decimal(sent["price_dollars"]) == Decimal("0.56")

    async def test_buy_no_is_sent_as_an_ask_at_the_complement(
        self, key_file: Path
    ) -> None:
        """The operator approved 'buy NO at 30c'; the exchange gets ask@0.70."""
        session = session_with_market(
            [make_leg(side=Side.NO, action="buy", limit_price=Decimal("0.30"))]
        )
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        sent = rest.create_calls[0]
        assert sent["book_side"] == "ask"
        assert Decimal(sent["price_dollars"]) == Decimal("0.70")

    async def test_sub_cent_prices_are_not_rounded_on_the_wire(
        self, key_file: Path
    ) -> None:
        """Rounding to cents would send a price the operator did not approve,
        and might not even be on a valid tick."""
        session = session_with_market([make_leg(limit_price=Decimal("0.505"))])
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        assert Decimal(rest.create_calls[0]["price_dollars"]) == Decimal("0.505")

    async def test_the_wire_price_is_not_zero_padded(
        self, key_file: Path
    ) -> None:
        """Regression: the first real order to Kalshi demo was rejected.

        The exchange validates the *string's* decimal exponent against the
        market's tick structure, so ``"0.250000"`` fails a ``linear_cent``
        market with ``invalid dollar precision: -6`` even though the value is
        exactly 25c. Padding is not the same as precision.
        """
        session = session_with_market([make_leg(limit_price=Decimal("0.25"))])
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        assert rest.create_calls[0]["price_dollars"] == "0.25"

    async def test_the_client_order_id_is_sent(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert rest.create_calls[0]["client_order_id"] == order.client_order_id

    async def test_time_in_force_comes_from_config(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest()
        config = make_config(order={"time_in_force": "ioc"})
        executor = Executor(rest, demo_settings(key_file), config)

        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        assert rest.create_calls[0]["time_in_force"] == "immediate_or_cancel"


# ---------------------------------------------------------------------------
# Fills and status
# ---------------------------------------------------------------------------


class TestExchangeFills:
    async def test_an_unfilled_order_rests(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest(
            create_response={
                "order_id": "e1", "fill_count": "0.00", "remaining_count": "10.00"
            }
        )
        executor = Executor(rest, demo_settings(key_file), make_config())

        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert order.status is OrderStatus.RESTING
        assert order.exchange_order_id == "e1"

    async def test_a_fully_filled_order_is_filled(self, key_file: Path) -> None:
        session = session_with_market()
        rest = FakeRest(
            create_response={
                "order_id": "e1",
                "fill_count": "10.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.5000",
                "average_fee_paid": "0.0175",
            }
        )
        executor = Executor(rest, demo_settings(key_file), make_config())

        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert order.status is OrderStatus.FILLED
        assert order.filled_contracts == Decimal("10.00")

    async def test_a_partial_fill_is_recorded_as_partial(
        self, key_file: Path
    ) -> None:
        session = session_with_market()
        rest = FakeRest(
            create_response={
                "order_id": "e1",
                "fill_count": "4.00",
                "remaining_count": "6.00",
                "average_fill_price": "0.5000",
                "average_fee_paid": "0.0175",
            }
        )
        executor = Executor(rest, demo_settings(key_file), make_config())

        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert order.status is OrderStatus.PARTIALLY_FILLED

    async def test_the_fill_price_is_stored_in_the_side_s_own_units(
        self, key_file: Path
    ) -> None:
        """A NO fill is stored as a NO price, not the YES price off the wire."""
        from app.db.models import Fill

        session = session_with_market()
        rest = FakeRest(
            create_response={
                "order_id": "e1",
                "fill_count": "10.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.7000",  # YES price
                "average_fee_paid": "0.0147",
            }
        )
        executor = Executor(rest, demo_settings(key_file), make_config())

        session.legs = [
            make_leg(side=Side.NO, action="buy", limit_price=Decimal("0.30"))
        ]
        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        fill = session.of_type(Fill)[0]
        assert fill.price == Decimal("0.30")

    async def test_a_failed_placement_marks_the_order_rejected(
        self, key_file: Path
    ) -> None:
        session = session_with_market()
        rest = FakeRest(create_error=RuntimeError("exchange said no"))
        executor = Executor(rest, demo_settings(key_file), make_config())

        with pytest.raises(ExecutionError):
            await executor.approve_and_execute(
                session, make_proposal(), confirmed=True
            )

        order = session.of_type(Order)[0]
        assert order.status is OrderStatus.REJECTED
        assert "exchange said no" in (order.error or "")

    async def test_a_failed_placement_does_not_strand_the_proposal(
        self, key_file: Path
    ) -> None:
        """A proposal marked APPROVED with no working order is a limbo state.

        It is invisible to the queue (not pending) and to the expiry sweep
        (also not pending), so it would sit there forever looking like it
        traded. The failure has to be recorded on the proposal too, not only
        on the order.
        """
        session = session_with_market()
        proposal = make_proposal()
        executor = Executor(
            FakeRest(create_error=RuntimeError("exchange said no")),
            demo_settings(key_file),
            make_config(),
        )

        with pytest.raises(ExecutionError):
            await executor.approve_and_execute(session, proposal, confirmed=True)

        assert proposal.status is ProposalStatus.FAILED
        assert proposal.decision_reason == "approved, but placement failed"

    async def test_a_successful_placement_marks_the_proposal_executed(
        self, key_file: Path
    ) -> None:
        session = session_with_market()
        proposal = make_proposal()
        executor = Executor(FakeRest(), demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, proposal, confirmed=True)

        assert proposal.status is ProposalStatus.EXECUTED
        # An order that rests unfilled still counts as executed at the
        # proposal level: the decision was carried out. The order's own
        # status is what says whether it filled.
        assert session.of_type(Order)[0].status is OrderStatus.RESTING


class TestSimulatedFills:
    async def test_the_simulator_makes_no_api_call(self) -> None:
        session = session_with_market()
        rest = FakeRest(
            book={
                "yes_dollars": [["0.4000", "100.00"]],
                "no_dollars": [["0.5500", "80.00"]],
            }
        )
        executor = Executor(rest, sim_settings(), make_config())

        order = await executor.approve_and_execute(
            session, make_proposal(limit_price=Decimal("0.50")), confirmed=True
        )

        assert order.route == ExecutionRoute.SIMULATED.value
        assert rest.create_calls == []
        assert order.status is OrderStatus.FILLED

    async def test_simulated_orders_are_paper(self) -> None:
        session = session_with_market()
        executor = Executor(FakeRest(), sim_settings(), make_config())
        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )
        assert order.is_paper is True

    async def test_an_unmarketable_simulated_order_rests(self) -> None:
        session = session_with_market()
        rest = FakeRest(
            book={
                "yes_dollars": [["0.4000", "100.00"]],
                "no_dollars": [["0.5500", "80.00"]],
            }
        )
        executor = Executor(rest, sim_settings(), make_config())

        session.legs = [make_leg(limit_price=Decimal("0.10"))]
        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert order.status is OrderStatus.RESTING
        assert order.filled_contracts == Decimal(0)

    async def test_no_book_means_the_order_rests_rather_than_inventing_a_fill(
        self,
    ) -> None:
        session = session_with_market()
        executor = Executor(
            FakeRest(book={"yes_dollars": [], "no_dollars": []}),
            sim_settings(),
            make_config(),
        )

        order = await executor.approve_and_execute(
            session, make_proposal(), confirmed=True
        )

        assert order.status is OrderStatus.RESTING


# ---------------------------------------------------------------------------
# Units at the boundary
# ---------------------------------------------------------------------------


class TestFeeConversion:
    def test_dollars_become_cents(self) -> None:
        assert fee_cents_from_dollars("1.75") == Decimal("175")

    def test_missing_fee_is_zero_not_an_error(self) -> None:
        assert fee_cents_from_dollars(None) == Decimal(0)
        assert fee_cents_from_dollars("") == Decimal(0)

    def test_fractional_cents_are_kept_exactly(self) -> None:
        """Regression: observed against the live demo exchange.

        Two contracts at 20c were billed $0.022400 — 2.24 cents. Rounding
        that to 2 understates what we paid and flatters P&L; rounding to 3
        overstates it. The record has to say what we were actually charged.
        """
        assert fee_cents_from_dollars("0.022400") == Decimal("2.24")
        assert fee_cents_from_dollars("0.033600") == Decimal("3.36")


class TestOrderView:
    def test_money_serialises_as_strings(self) -> None:
        order = Order(
            proposal_id=1,
            client_order_id="c1",
            ticker="T",
            side=Side.YES,
            action="buy",
            limit_price=Decimal("0.505"),
            contracts=Decimal("2.50"),
            filled_contracts=Decimal("1.25"),
            status=OrderStatus.PARTIALLY_FILLED,
            route="simulated",
        )
        view = order_view(order)
        assert view["limit_price"] == "0.505"
        assert view["contracts"] == "2.50"
        assert view["filled_contracts"] == "1.25"

    def test_route_is_exposed_alongside_is_paper(self) -> None:
        """A demo fill and a simulated fill are different kinds of evidence."""
        order = Order(
            client_order_id="c1", ticker="T", side=Side.YES, action="buy",
            limit_price=Decimal("0.5"), contracts=Decimal(1),
            status=OrderStatus.FILLED, is_paper=True, route="demo_exchange",
        )
        view = order_view(order)
        assert view["is_paper"] is True
        assert view["route"] == "demo_exchange"


# ---------------------------------------------------------------------------
# Multi-leg proposals
# ---------------------------------------------------------------------------


class TestMultiLeg:
    """A set arbitrage is one decision that needs several orders.

    The exchange has no atomic multi-order primitive — the batch endpoint
    returns a separate result per order and promises nothing. So the design
    reduces leg risk (one round trip, IOC) and reports what it cannot remove
    (an imbalance), rather than claiming a guarantee it does not have.
    """

    @staticmethod
    def _legs() -> list[ProposalLeg]:
        legs = []
        for i, (ticker, price) in enumerate(
            (("KXEV-26-A", "0.55"), ("KXEV-26-B", "0.52"))
        ):
            leg = make_leg(
                seq=i, ticker=ticker, side=Side.YES, action="sell",
                limit_price=Decimal(price), contracts=Decimal(10),
            )
            leg.id = i + 1
            legs.append(leg)
        return legs

    @staticmethod
    def _proposal() -> ProposedTrade:
        return make_proposal(leg_count=2, event_ticker="KXEV-26")

    async def test_every_leg_becomes_an_order(self, key_file: Path) -> None:
        session = session_with_market(self._legs())
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, self._proposal(), confirmed=True)

        orders = session.of_type(Order)
        assert [o.ticker for o in orders] == ["KXEV-26-A", "KXEV-26-B"]

    async def test_legs_go_out_in_one_batch(self, key_file: Path) -> None:
        """One round trip, not N — the only mitigation available for the gap
        between legs."""
        session = session_with_market(self._legs())
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, self._proposal(), confirmed=True)

        assert len(rest.batch_calls) == 1
        assert len(rest.batch_calls[0]) == 2
        assert rest.create_calls == []  # not placed one at a time

    async def test_multi_leg_forces_ioc(self, key_file: Path) -> None:
        """A resting leg of an arb is an unhedged option written for free."""
        session = session_with_market(self._legs())
        rest = FakeRest()
        executor = Executor(
            rest, demo_settings(key_file),
            make_config(order={"time_in_force": "gtc"}),
        )

        await executor.approve_and_execute(session, self._proposal(), confirmed=True)

        assert all(
            o["time_in_force"] == "immediate_or_cancel" for o in rest.batch_calls[0]
        )

    async def test_results_are_matched_by_client_order_id(
        self, key_file: Path
    ) -> None:
        """Nothing promises the batch comes back in the order it went out."""
        session = session_with_market(self._legs())
        rest = FakeRest(batch_reversed=True, batch_fill="10.00")
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, self._proposal(), confirmed=True)

        orders = session.of_type(Order)
        assert all(o.status is OrderStatus.FILLED for o in orders)

    async def test_all_legs_filling_is_executed(self, key_file: Path) -> None:
        session = session_with_market(self._legs())
        proposal = self._proposal()
        executor = Executor(
            FakeRest(batch_fill="10.00"), demo_settings(key_file), make_config()
        )

        await executor.approve_and_execute(session, proposal, confirmed=True)

        assert proposal.status is ProposalStatus.EXECUTED

    async def test_no_legs_filling_is_still_balanced(self, key_file: Path) -> None:
        """Nothing on is as balanced as everything on."""
        session = session_with_market(self._legs())
        proposal = self._proposal()
        executor = Executor(
            FakeRest(batch_fill="0.00"), demo_settings(key_file), make_config()
        )

        await executor.approve_and_execute(session, proposal, confirmed=True)

        assert proposal.status is ProposalStatus.EXECUTED

    async def test_a_partial_set_is_flagged_unbalanced(self, key_file: Path) -> None:
        """The outcome the exchange cannot rule out, and the operator must see.

        One leg on and one off is not a hedge — it is a directional position
        nobody chose.
        """
        session = session_with_market(self._legs())
        proposal = self._proposal()
        executor = Executor(
            FakeRest(batch_fill_first_only=True), demo_settings(key_file),
            make_config(),
        )

        await executor.approve_and_execute(session, proposal, confirmed=True)

        assert proposal.status is ProposalStatus.PARTIAL
        assert "UNBALANCED" in (proposal.decision_reason or "")

    async def test_a_missing_batch_result_rejects_that_leg(
        self, key_file: Path
    ) -> None:
        """Silence about a leg means it did not go on. Say so."""
        session = session_with_market(self._legs())
        executor = Executor(
            FakeRest(batch_drop_second=True), demo_settings(key_file), make_config()
        )

        await executor.approve_and_execute(session, self._proposal(), confirmed=True)

        orders = session.of_type(Order)
        assert orders[1].status is OrderStatus.REJECTED
        assert "no result" in (orders[1].error or "")

    async def test_a_single_leg_proposal_still_uses_the_plain_endpoint(
        self, key_file: Path
    ) -> None:
        session = session_with_market()
        rest = FakeRest()
        executor = Executor(rest, demo_settings(key_file), make_config())

        await executor.approve_and_execute(session, make_proposal(), confirmed=True)

        assert len(rest.create_calls) == 1
        assert rest.batch_calls == []


class TestOrderBodyParity:
    """Single and batch placement must build identical bodies.

    Regression: the batch path once assembled its own dict and omitted
    `self_trade_prevention_type`, which the API requires. Every multi-leg
    order was rejected while single orders worked — the two paths had
    silently diverged.
    """

    def test_the_body_always_carries_self_trade_prevention(self) -> None:
        from app.kalshi.rest import build_order_body

        body = build_order_body(
            ticker="T", book_side="bid", price_dollars="0.25", count="2.00",
            client_order_id="c1",
        )
        assert body["self_trade_prevention_type"] == "taker_at_cross"

    def test_both_paths_use_the_same_builder(self) -> None:
        """Asserted structurally: one function, so they cannot drift."""
        import inspect

        from app.kalshi import rest

        single = inspect.getsource(rest.KalshiRestClient.create_order)
        batch = inspect.getsource(rest.KalshiRestClient.create_orders_batch)
        assert "build_order_body" in single
        assert "build_order_body" in batch

    def test_an_invalid_book_side_is_rejected(self) -> None:
        from app.kalshi.rest import build_order_body

        with pytest.raises(ValueError, match="book_side"):
            build_order_body(
                ticker="T", book_side="buy", price_dollars="0.25",
                count="1", client_order_id="c1",
            )

    def test_an_invalid_time_in_force_is_rejected(self) -> None:
        from app.kalshi.rest import build_order_body

        with pytest.raises(ValueError, match="time_in_force"):
            build_order_body(
                ticker="T", book_side="bid", price_dollars="0.25",
                count="1", client_order_id="c1", time_in_force="gtt",
            )


class TestFailureIsRecorded:
    """A failed placement must leave evidence, not vanish.

    The executor writes Order rows before placing so that an ambiguous
    failure — a timeout that may still have reached the matching engine —
    leaves a client order ID to reconcile against. Anything that rolls those
    rows back defeats the entire recovery design. Observed live: a rejected
    batch left zero order rows and the proposal still pending, as if nothing
    had been attempted.
    """

    async def test_rejected_orders_are_still_written(self, key_file: Path) -> None:
        session = session_with_market()
        executor = Executor(
            FakeRest(create_error=RuntimeError("exchange said no")),
            demo_settings(key_file),
            make_config(),
        )

        with pytest.raises(ExecutionError):
            await executor.approve_and_execute(
                session, make_proposal(), confirmed=True
            )

        orders = session.of_type(Order)
        assert len(orders) == 1
        assert orders[0].client_order_id  # the reconciliation key survives
        assert orders[0].status is OrderStatus.REJECTED

    async def test_every_leg_of_a_failed_batch_is_written(
        self, key_file: Path
    ) -> None:
        legs = TestMultiLeg._legs()
        session = session_with_market(legs)
        executor = Executor(
            FakeRest(create_error=RuntimeError("batch rejected")),
            demo_settings(key_file),
            make_config(),
        )

        with pytest.raises(ExecutionError):
            await executor.approve_and_execute(
                session, TestMultiLeg._proposal(), confirmed=True
            )

        orders = session.of_type(Order)
        assert len(orders) == 2
        assert all(o.status is OrderStatus.REJECTED for o in orders)
        assert len({o.client_order_id for o in orders}) == 2


class TestIocStatus:
    """An IOC order that did not fill is dead, not resting.

    Observed live on an unfilled arbitrage leg: the order was recorded as
    RESTING, so the dashboard showed a working order that did not exist and
    the auto-cancel sweep would have tried to cancel a ghost.
    """

    async def test_an_unfilled_ioc_leg_is_canceled_not_resting(
        self, key_file: Path
    ) -> None:
        session = session_with_market(TestMultiLeg._legs())
        executor = Executor(
            FakeRest(batch_fill="0.00"), demo_settings(key_file), make_config()
        )

        await executor.approve_and_execute(
            session, TestMultiLeg._proposal(), confirmed=True
        )

        orders = session.of_type(Order)
        assert all(o.time_in_force == "ioc" for o in orders)
        assert all(o.status is OrderStatus.CANCELED for o in orders)

    async def test_a_partially_filled_ioc_leg_is_also_canceled(
        self, key_file: Path
    ) -> None:
        """IOC cancels the remainder, so nothing is left working."""
        order = Order(
            client_order_id="c1", ticker="T", side=Side.YES, action="buy",
            limit_price=Decimal("0.5"), contracts=Decimal(10),
            filled_contracts=Decimal(4), time_in_force="ioc",
            status=OrderStatus.PENDING, route="demo_exchange",
        )
        Executor._settle_status(order)
        assert order.status is OrderStatus.CANCELED

    async def test_an_unfilled_gtc_order_still_rests(self, key_file: Path) -> None:
        order = Order(
            client_order_id="c1", ticker="T", side=Side.YES, action="buy",
            limit_price=Decimal("0.5"), contracts=Decimal(10),
            filled_contracts=Decimal(0), time_in_force="gtc",
            status=OrderStatus.PENDING, route="demo_exchange",
        )
        Executor._settle_status(order)
        assert order.status is OrderStatus.RESTING

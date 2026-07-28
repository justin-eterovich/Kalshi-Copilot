"""Tests for the proposal lifecycle.

Expiry is the property worth guarding: a proposal carries a price that was
executable when it was written, and a queue that keeps offering an hour-old
quote as actionable is worse than an empty one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Update

from app.config import Config
from app.db.models import (
    AuditLog,
    ProposalLeg,
    ProposalStatus,
    ProposedTrade,
    Side,
)
from app.trading import proposals as prop


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def one_or_none(self) -> Any:
        """The ``SELECT ... FOR UPDATE`` re-read lands here.

        A fake session cannot take a row lock, so it hands back nothing and
        the caller keeps the object it was given — which is what these tests
        want, since they are about the status re-check rather than about
        Postgres.
        """
        if len(self._rows) > 1:
            raise AssertionError("one_or_none() on multiple rows")
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.added: list[Any] = []
        self._rows = rows or []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, _stmt: Any) -> FakeResult:
        return FakeResult(self._rows)

    def audits(self) -> list[AuditLog]:
        return [o for o in self.added if isinstance(o, AuditLog)]


@pytest.fixture(autouse=True)
def no_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(prop, "publish", noop)


def make_proposal(**overrides: object) -> ProposedTrade:
    proposal = ProposedTrade(
        source="manual",
        ticker="TEST-MKT",
        leg_count=1,
        status=ProposalStatus.PENDING,
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        created_at=datetime.now(UTC),
    )
    proposal.id = 1
    for key, value in overrides.items():
        setattr(proposal, key, value)
    return proposal


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


class ExpirySession(FakeSession):
    """Emulates the conditional ``UPDATE ... RETURNING`` the sweep now runs.

    ``expire_stale`` used to be a read-modify-write, and it has two concurrent
    callers — the worker's sweep and ``GET /api/proposals``, which expires on
    read. Both selected the same rows and both wrote an audit entry for the
    same expiry: 214 ``proposal.expired`` rows for 182 distinct proposals, 15%
    redundant, 31 of 32 duplicates landing 21-25ms apart.

    So the fake applies the UPDATE's own SET values, taken out of the compiled
    statement, only to rows that still satisfy its WHERE. A row already
    EXPIRED is not matched a second time — which is what makes the
    "expire twice, audit once" test mean something.
    """

    def __init__(self, rows: list[ProposedTrade], *, now: datetime = NOW) -> None:
        super().__init__(rows)
        self._now = now
        self._won: list[ProposedTrade] = []
        self.statements: list[str] = []

    async def execute(self, stmt: Any) -> FakeResult:
        self.statements.append(str(stmt))
        if isinstance(stmt, Update):
            values = stmt.compile().params
            self._won = [
                row
                for row in self._rows
                if row.status is ProposalStatus.PENDING
                and row.expires_at is not None
                and row.expires_at <= self._now
            ]
            for row in self._won:
                row.status = values["status"]
                row.decided_at = values["decided_at"]
                row.decision_reason = values["decision_reason"]
            return FakeResult([row.id for row in self._won])
        # The re-read of the rows this transaction actually won.
        return FakeResult(list(self._won))


class TestExpiry:
    async def test_a_lapsed_proposal_is_expired(self) -> None:
        lapsed = make_proposal(expires_at=NOW - timedelta(seconds=1))
        session = ExpirySession([lapsed])

        count = await prop.expire_stale(session, now=NOW)

        assert count == 1
        assert lapsed.status is ProposalStatus.EXPIRED
        assert lapsed.decided_at is not None

    async def test_expiry_is_audited(self) -> None:
        """A proposal that lapsed unapproved is data the report card needs."""
        lapsed = make_proposal(expires_at=NOW - timedelta(seconds=1))
        session = ExpirySession([lapsed])

        await prop.expire_stale(session, now=NOW)

        kinds = [a.kind for a in session.audits()]
        assert "proposal.expired" in kinds

    async def test_nothing_to_expire_is_not_an_error(self) -> None:
        assert await prop.expire_stale(ExpirySession([]), now=NOW) == 0

    async def test_a_live_proposal_is_left_alone(self) -> None:
        live = make_proposal(expires_at=NOW + timedelta(seconds=60))
        session = ExpirySession([live])

        assert await prop.expire_stale(session, now=NOW) == 0
        assert live.status is ProposalStatus.PENDING
        assert session.audits() == []

    async def test_expiry_records_why(self) -> None:
        lapsed = make_proposal(expires_at=NOW - timedelta(seconds=1))
        await prop.expire_stale(ExpirySession([lapsed]), now=NOW)
        assert "ttl" in (lapsed.decision_reason or "")

    async def test_it_returns_the_number_of_rows_it_won(self) -> None:
        lapsed = [
            make_proposal(expires_at=NOW - timedelta(seconds=1)) for _ in range(3)
        ]
        for i, proposal in enumerate(lapsed, start=1):
            proposal.id = i
        session = ExpirySession(lapsed)

        assert await prop.expire_stale(session, now=NOW) == 3
        assert all(p.status is ProposalStatus.EXPIRED for p in lapsed)

    async def test_exactly_one_audit_row_per_expired_proposal(self) -> None:
        """Not two. The audit log is the evidence trail; a duplicated entry
        makes it lie about how many things happened."""
        lapsed = [
            make_proposal(expires_at=NOW - timedelta(seconds=1)) for _ in range(3)
        ]
        for i, proposal in enumerate(lapsed, start=1):
            proposal.id = i
        session = ExpirySession(lapsed)

        await prop.expire_stale(session, now=NOW)

        expired_rows = [a for a in session.audits() if a.kind == "proposal.expired"]
        assert len(expired_rows) == 3
        assert sorted(a.payload["proposal_id"] for a in expired_rows) == [1, 2, 3]

    async def test_expiring_twice_expires_nothing_the_second_time(self) -> None:
        """The duplicate-audit regression, in one assertion.

        The second caller's UPDATE matches no rows because the first already
        flipped them out of PENDING, so the database decides who won and only
        the winner logs it.
        """
        lapsed = make_proposal(expires_at=NOW - timedelta(seconds=1))
        session = ExpirySession([lapsed])

        assert await prop.expire_stale(session, now=NOW) == 1
        assert await prop.expire_stale(session, now=NOW) == 0

        expired_rows = [a for a in session.audits() if a.kind == "proposal.expired"]
        assert len(expired_rows) == 1

    async def test_the_status_change_is_one_conditional_update(self) -> None:
        """Asserted structurally, because the race is not reproducible here.

        A read-then-write cannot be told apart from a conditional write by its
        effects on a single-threaded fake — only by the shape of the statement
        it issues. The WHERE has to carry the status predicate, or two
        transactions can both win the same row.
        """
        lapsed = make_proposal(expires_at=NOW - timedelta(seconds=1))
        session = ExpirySession([lapsed])

        await prop.expire_stale(session, now=NOW)

        update_sql = next(s for s in session.statements if s.startswith("UPDATE"))
        assert "proposed_trades.status =" in update_sql
        assert "proposed_trades.expires_at <=" in update_sql
        assert "RETURNING" in update_sql


class TestRejection:
    async def test_rejection_is_recorded(self) -> None:
        """A detector whose signals are always declined is wrong in a way its
        P&L will never show."""
        proposal = make_proposal()
        session = FakeSession()

        await prop.reject(session, proposal, reason="spread too wide")

        assert proposal.status is ProposalStatus.REJECTED
        assert proposal.decision_reason == "spread too wide"
        assert "proposal.rejected" in [a.kind for a in session.audits()]

    async def test_rejecting_a_decided_proposal_is_refused(self) -> None:
        proposal = make_proposal(status=ProposalStatus.EXECUTED)
        with pytest.raises(prop.ProposalError) as exc:
            await prop.reject(FakeSession(), proposal)
        assert exc.value.code == "not_pending"

    async def test_a_rejection_without_a_reason_still_says_something(self) -> None:
        proposal = make_proposal()
        await prop.reject(FakeSession(), proposal)
        assert proposal.decision_reason

    async def test_rejecting_twice_is_refused_the_second_time(self) -> None:
        """The status is re-checked under the lock, not against a snapshot.

        Two concurrent rejects both saw PENDING, both wrote a
        ``proposal.rejected`` audit row 0.5ms apart with different reasons, and
        ``decision_reason`` ended up last-writer-wins. Nothing is placed by a
        reject, so this costs nothing — but the audit trail should say once
        what happened once.
        """
        proposal = make_proposal()
        session = FakeSession()

        await prop.reject(session, proposal, reason="first")
        with pytest.raises(prop.ProposalError) as exc:
            await prop.reject(session, proposal, reason="second")

        assert exc.value.code == "not_pending"
        assert proposal.decision_reason == "first"
        assert len([a for a in session.audits() if a.kind == "proposal.rejected"]) == 1

    async def test_the_status_is_re_read_under_a_row_lock(self) -> None:
        """Asserted structurally: the race is not reproducible single-threaded.

        Only the shape of the statement distinguishes a locked re-read from a
        plain SELECT, and without the lock two transactions both read PENDING
        and both proceed.
        """
        seen: list[str] = []

        class RecordingSession(FakeSession):
            async def execute(self, stmt: Any) -> FakeResult:
                seen.append(str(stmt))
                return await super().execute(stmt)

        await prop.reject(RecordingSession(), make_proposal())

        assert any("FOR UPDATE" in sql for sql in seen)

    async def test_an_unpersisted_proposal_skips_the_lock(self) -> None:
        """Nothing to lock against, and no row another transaction can hold."""
        proposal = make_proposal()
        proposal.id = None
        await prop.reject(FakeSession(), proposal)
        assert proposal.status is ProposalStatus.REJECTED


class TestProposalView:
    def test_money_serialises_as_strings(self) -> None:
        leg = ProposalLeg(
            proposal_id=1, seq=0, ticker="TEST-MKT", side=Side.YES, action="buy",
            limit_price=Decimal("0.505"), contracts=Decimal("2.50"),
        )
        view = prop.proposal_view(
            make_proposal(net_edge_cents=Decimal("3.2500")), [leg]
        )
        assert view["net_edge_cents"] == "3.2500"
        assert view["legs"][0]["limit_price"] == "0.505"
        assert view["legs"][0]["contracts"] == "2.50"

    def test_a_multi_leg_proposal_lists_every_leg(self) -> None:
        """The card has to name what it would trade, or approving it is blind."""
        legs = [
            ProposalLeg(proposal_id=1, seq=i, ticker=f"KXEV-26-{c}", side=Side.YES,
                        action="sell", limit_price=Decimal("0.55"),
                        contracts=Decimal(10))
            for i, c in enumerate("AB")
        ]
        view = prop.proposal_view(
            make_proposal(leg_count=2, event_ticker="KXEV-26"), legs
        )
        assert view["leg_count"] == 2
        assert view["event_ticker"] == "KXEV-26"
        assert [leg["ticker"] for leg in view["legs"]] == ["KXEV-26-A", "KXEV-26-B"]

    def test_countdown_is_exposed_for_the_ui(self) -> None:
        view = prop.proposal_view(
            make_proposal(expires_at=datetime.now(UTC) + timedelta(seconds=30))
        )
        assert 29 <= float(view["expires_in_sec"]) <= 30

    def test_a_lapsed_proposal_reports_a_negative_countdown(self) -> None:
        """The UI needs to be able to tell 'about to expire' from 'gone'."""
        view = prop.proposal_view(
            make_proposal(expires_at=datetime.now(UTC) - timedelta(seconds=5))
        )
        assert float(view["expires_in_sec"]) < 0

    def test_there_is_no_gross_edge_field(self) -> None:
        """Every edge shown anywhere is net. There is nothing else to show."""
        view = prop.proposal_view(make_proposal())
        assert "gross_edge_cents" not in view
        assert "net_edge_cents" in view


class TestBankrollShare:
    def test_uses_the_worst_case_not_the_notional(self) -> None:
        """Sizing should be judged on what can be lost."""
        from app.trading.pricing import price_ticket

        config = Config.model_validate(
            {"risk": {"bankroll_usd": 1000.0}, "costs": {"slippage_buffer_cents": 0}}
        )
        quote = price_ticket(
            ticker="T", side=Side.YES, action="buy",
            limit_price="0.50", contracts="100", category="Sports", config=config,
        )
        # 5175c risked against a 100000c bankroll.
        assert prop._pct_of_bankroll(quote, config) == pytest.approx(0.05175)


# ---------------------------------------------------------------------------
# Risk guards
# ---------------------------------------------------------------------------


def risk_config(**risk: object) -> Config:
    base = {"bankroll_usd": 1000.0, "max_pending_proposals": 3,
            "max_pct_per_market": 0.05}
    base.update(risk)
    return Config.model_validate({"risk": base})


class CountingSession(FakeSession):
    """FakeSession whose SELECT count() answers are scripted."""

    def __init__(self, counts: list[int]) -> None:
        super().__init__()
        self._counts = counts

    async def execute(self, _stmt: Any) -> Any:
        class R:
            def __init__(self, value: int) -> None:
                self._value = value

            def scalar_one(self) -> int:
                return self._value

        return R(self._counts.pop(0) if self._counts else 0)


class TestQueueDepthGuard:
    """The safety model is a human reading each proposal.

    A detector scanning every 20s produced 20 proposals per scan across 22
    watched events — a queue nobody reads is rubber-stamped, which removes
    the only real control in the system.
    """

    async def test_allows_while_below_the_cap(self) -> None:
        await prop._guard_queue_depth(CountingSession([2]), risk_config())

    async def test_refuses_at_the_cap(self) -> None:
        with pytest.raises(prop.ProposalError) as exc:
            await prop._guard_queue_depth(CountingSession([3]), risk_config())
        assert exc.value.code == "queue_full"

    async def test_the_message_names_the_knob(self) -> None:
        with pytest.raises(prop.ProposalError) as exc:
            await prop._guard_queue_depth(CountingSession([9]), risk_config())
        assert "max_pending_proposals" in str(exc.value)


class TestDuplicateGuard:
    """A detector re-derives the same opportunity on every scan."""

    async def test_allows_a_new_event(self) -> None:
        await prop._guard_duplicate(
            CountingSession([0]), source="set_arbitrage", key="KXEV-26"
        )

    async def test_refuses_one_already_pending(self) -> None:
        with pytest.raises(prop.ProposalError) as exc:
            await prop._guard_duplicate(
                CountingSession([1]), source="set_arbitrage", key="KXEV-26"
            )
        assert exc.value.code == "already_pending"


@pytest.fixture
def committed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Control what the market already has committed to it.

    ``_guard_market_size`` now issues a query — that is the entire fix — so
    the guard's own arithmetic is tested here against a scripted answer, and
    the query itself is tested in ``test_risk.py::TestMarketExposure``.
    ``active_route`` is pinned too so the guard does not depend on whatever
    credentials happen to be on the machine running the suite.
    """
    from app.trading import risk

    calls: dict[str, Any] = {"cents": Decimal(0)}

    async def fake_exposure(_session: Any, **kwargs: Any) -> Decimal:
        calls["tickers"] = kwargs.get("tickers")
        calls["event_ticker"] = kwargs.get("event_ticker")
        calls["route"] = kwargs.get("route")
        return calls["cents"]

    monkeypatch.setattr(risk, "market_exposure_cents", fake_exposure)
    monkeypatch.setattr(risk, "active_route", lambda *_a, **_k: "demo_exchange")
    return calls


def set_committed(committed: dict[str, Any], cents: Decimal) -> None:
    committed["cents"] = cents


class TestMarketSizeGuard:
    """`max_pct_per_market` measures the **market**, not one proposal.

    It used to divide a single proposal's ``max_loss_cents`` by the bankroll
    and issue no query at all — it took no session, so it could not see the
    market's existing position or the rest of the queue. The per-proposal
    boundary was exact, which is precisely why it looked like it worked:
    9,300 contracts accepted at 4.97%, 9,400 refused at 5.03%. Meanwhile eight
    individually compliant proposals on one ticker reached **39.79% of
    bankroll against a 5% cap**, every one of them reporting itself inside the
    limit.
    """

    async def test_allows_a_small_trade(self, committed: dict) -> None:
        # $10 of a $1000 bankroll is 1%, with nothing already committed.
        pct = await prop._guard_market_size(
            FakeSession(), Decimal(1000), risk_config(), what="T", tickers=["T"]
        )
        assert pct == pytest.approx(0.01)

    async def test_refuses_an_oversized_trade(self, committed: dict) -> None:
        # $100 of a $1000 bankroll is 10%, over the 5% limit.
        with pytest.raises(prop.ProposalError) as exc:
            await prop._guard_market_size(
                FakeSession(), Decimal(10_000), risk_config(),
                what="T", tickers=["T"],
            )
        assert exc.value.code == "exceeds_market_limit"

    async def test_the_message_reports_both_numbers(self, committed: dict) -> None:
        with pytest.raises(prop.ProposalError) as exc:
            await prop._guard_market_size(
                FakeSession(), Decimal(10_000), risk_config(),
                what="T", tickers=["T"],
            )
        assert "10.00%" in str(exc.value) and "5.00%" in str(exc.value)

    async def test_exactly_at_the_limit_is_allowed(self, committed: dict) -> None:
        await prop._guard_market_size(
            FakeSession(), Decimal(5000), risk_config(), what="T", tickers=["T"]
        )

    async def test_an_unknown_worst_case_is_not_silently_allowed_as_large(
        self, committed: dict
    ) -> None:
        """None means "not supplied", and returns zero rather than raising —
        the caller is responsible for supplying it. Asserted so the behaviour
        is deliberate rather than incidental."""
        assert await prop._guard_market_size(
            FakeSession(), None, risk_config(), what="T", tickers=["T"]
        ) == 0.0

    async def test_what_is_already_committed_counts_against_the_cap(
        self, committed: dict
    ) -> None:
        """The fix, in one assertion.

        4,000c is already in this market and the new proposal risks 1,500c.
        The proposal alone is 1.5% — comfortably inside the 5% cap — and the
        market ends up at 5.5%, which is not.
        """
        set_committed(committed, Decimal(4000))
        with pytest.raises(prop.ProposalError) as exc:
            await prop._guard_market_size(
                FakeSession(), Decimal(1500), risk_config(),
                what="T", tickers=["T"],
            )
        assert exc.value.code == "exceeds_market_limit"
        assert "5.50%" in str(exc.value)

    async def test_the_fraction_returned_includes_what_was_already_there(
        self, committed: dict
    ) -> None:
        """``pct_of_bankroll`` is stored on the proposal and rendered on the
        approval card. It has to describe the market's total, or the card
        reports a number the cap does not use."""
        set_committed(committed, Decimal(2000))
        pct = await prop._guard_market_size(
            FakeSession(), Decimal(1000), risk_config(), what="T", tickers=["T"]
        )
        assert pct == pytest.approx(0.03)

    async def test_n_individually_compliant_proposals_are_refused_together(
        self, committed: dict
    ) -> None:
        """The headline regression, replayed.

        Five proposals of 1,000c each are 1% of a $1,000 bankroll apiece, and
        every one of them passes on its own. The cap is 5%. Once the market
        holds 5,000c the sixth has to be refused — under the old guard all
        eight were accepted and the ticker reached 39.79%.
        """
        cfg = risk_config()
        accepted = 0
        for _ in range(8):
            try:
                await prop._guard_market_size(
                    FakeSession(), Decimal(1000), cfg, what="T", tickers=["T"]
                )
            except prop.ProposalError as exc:
                assert exc.code == "exceeds_market_limit"
                break
            accepted += 1
            # An accepted proposal is pending, so it is inside the next
            # proposal's committed total.
            set_committed(committed, Decimal(1000) * accepted)

        assert accepted == 5

    async def test_every_leg_of_a_set_is_passed_to_the_query(
        self, committed: dict
    ) -> None:
        """A set arbitrage touches several markets; the cap has to see the
        whole footprint rather than the event label alone."""
        await prop._guard_market_size(
            FakeSession(), Decimal(100), risk_config(),
            what="KXEV-26", tickers=["KXEV-26-A", "KXEV-26-B"],
            event_ticker="KXEV-26",
        )
        assert committed["tickers"] == ["KXEV-26-A", "KXEV-26-B"]
        assert committed["event_ticker"] == "KXEV-26"

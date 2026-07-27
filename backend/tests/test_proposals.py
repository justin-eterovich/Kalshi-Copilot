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


class TestExpiry:
    async def test_a_lapsed_proposal_is_expired(self) -> None:
        lapsed = make_proposal(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        session = FakeSession([lapsed])

        count = await prop.expire_stale(session)

        assert count == 1
        assert lapsed.status is ProposalStatus.EXPIRED
        assert lapsed.decided_at is not None

    async def test_expiry_is_audited(self) -> None:
        """A proposal that lapsed unapproved is data the report card needs."""
        lapsed = make_proposal(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        session = FakeSession([lapsed])

        await prop.expire_stale(session)

        kinds = [a.kind for a in session.audits()]
        assert "proposal.expired" in kinds

    async def test_nothing_to_expire_is_not_an_error(self) -> None:
        assert await prop.expire_stale(FakeSession([])) == 0

    async def test_expiry_records_why(self) -> None:
        lapsed = make_proposal(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        await prop.expire_stale(FakeSession([lapsed]))
        assert "ttl" in (lapsed.decision_reason or "")


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

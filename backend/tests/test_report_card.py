"""The report card's attribution and scoring.

The arithmetic of expectancy lives in ``tests/test_backtest_stats.py``. What
is tested here is the harder half: deciding *what counts as one trade* and
*whose trade it was*. Both feed straight into the sample size, and the sample
size is what the "is this detector allowed real money" gate is computed from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.report import (
    FillRow,
    Funnel,
    OrderRow,
    SettlementRow,
    attribute_events,
    build_report,
)
from app.backtest.stats import Verdict

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def fill(
    *,
    order_id: int,
    ticker: str,
    pnl: str = "0",
    fee: str = "0",
    minutes: int = 0,
) -> FillRow:
    return FillRow(
        order_id=order_id,
        ticker=ticker,
        fee_cents=Decimal(fee),
        realized_pnl_cents=Decimal(pnl),
        ts=at(minutes),
    )


class TestDecisionIsTheUnit:
    """One approval is one observation, however many legs it had."""

    def test_five_legs_of_one_arb_collapse_to_one_trade(self) -> None:
        """A five-leg set arbitrage is one thesis with one outcome.

        Counting the legs separately would quintuple ``n`` and narrow the
        confidence interval by sqrt(5) — on perfectly correlated outcomes.
        That is the single most dangerous arithmetic error available to this
        module, because the interval is what decides whether the detector
        gets real money.
        """
        orders = [OrderRow(id=i, proposal_id=77, route="simulated") for i in range(5)]
        fills = [
            fill(order_id=i, ticker=f"LEG-{i}", pnl="12", fee="1", minutes=i)
            for i in range(5)
        ]

        events, unattributed, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={77: "set_arbitrage"},
        )

        assert len(events) == 1
        assert events[0].legs == 5
        # 5 * (12 - 1) — net of the fee each leg paid.
        assert events[0].pnl_cents == Decimal(55)
        assert events[0].fees_cents == Decimal(5)
        assert not unattributed

    def test_two_separate_proposals_are_two_trades(self) -> None:
        """Grouping is by decision, not by detector — two approvals, two n."""
        orders = [
            OrderRow(id=1, proposal_id=10, route="simulated"),
            OrderRow(id=2, proposal_id=11, route="simulated"),
        ]
        fills = [
            fill(order_id=1, ticker="A", pnl="5", minutes=0),
            fill(order_id=2, ticker="B", pnl="7", minutes=1),
        ]
        events, _, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={10: "whale_flow", 11: "whale_flow"},
        )
        assert len(events) == 2
        assert sorted(e.pnl_cents for e in events) == [Decimal(5), Decimal(7)]

    def test_settlement_merges_into_the_decision_that_opened_it(self) -> None:
        """A held position settling is the *same* trade, not a second one.

        The entry fill and the settlement are two halves of one round trip.
        Counting them separately would report a losing entry (fees, no P&L)
        and a winning exit as two independent observations.
        """
        orders = [OrderRow(id=1, proposal_id=5, route="simulated")]
        fills = [fill(order_id=1, ticker="MKT-A", pnl="0", fee="2.24", minutes=0)]
        settlements = [
            SettlementRow(
                ticker="MKT-A",
                route="simulated",
                realized_pnl_cents=Decimal("70"),
                fee_cents=Decimal(0),
                ts=at(600),
            )
        ]
        events, _, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=settlements,
            proposal_source={5: "stale_quote"},
        )
        assert len(events) == 1
        # 70c realised at settlement, less the 2.24c entry fee. Fractional,
        # because Kalshi's fees round to a centicent and never to a cent.
        assert events[0].pnl_cents == Decimal("67.76")
        assert events[0].ts == at(600)


class TestAttribution:
    def test_a_contested_position_is_dropped_not_split(self) -> None:
        """Two detectors in one market on one route: neither gets the credit.

        There is no defensible way to divide the outcome, and any ratio we
        invented would be the number deciding whether to deploy capital.
        """
        orders = [
            OrderRow(id=1, proposal_id=1, route="simulated"),
            OrderRow(id=2, proposal_id=2, route="simulated"),
        ]
        fills = [
            fill(order_id=1, ticker="SHARED", minutes=0),
            fill(order_id=2, ticker="SHARED", minutes=1),
        ]
        settlements = [
            SettlementRow(
                ticker="SHARED",
                route="simulated",
                realized_pnl_cents=Decimal("100"),
                fee_cents=Decimal(0),
                ts=at(60),
            )
        ]
        events, unattributed, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=settlements,
            proposal_source={1: "set_arbitrage", 2: "stale_quote"},
        )

        assert unattributed[("set_arbitrage", "simulated")] == 1
        assert unattributed[("stale_quote", "simulated")] == 1
        # The 100c is nowhere: not added to either, not silently halved.
        assert all(e.pnl_cents == Decimal(0) for e in events)

    def test_manual_fills_belong_to_nobody(self) -> None:
        """A quick ticket is real money and is not any detector's evidence."""
        orders = [OrderRow(id=1, proposal_id=None, route="demo_exchange")]
        fills = [fill(order_id=1, ticker="MKT", pnl="40", minutes=0)]
        events, unattributed, extra = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={},
        )
        assert events == []
        assert not unattributed
        assert not extra

    def test_routes_are_never_merged(self) -> None:
        """A simulated fill and a demo fill are different kinds of evidence.

        Merging them produces a P&L belonging to no book that exists — the
        same failure ``Position.route`` was added to prevent.
        """
        orders = [
            OrderRow(id=1, proposal_id=1, route="simulated"),
            OrderRow(id=2, proposal_id=2, route="demo_exchange"),
        ]
        fills = [
            fill(order_id=1, ticker="A", pnl="10", minutes=0),
            fill(order_id=2, ticker="A", pnl="-10", minutes=1),
        ]
        events, _, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={1: "whale_flow", 2: "whale_flow"},
        )
        routes = {e.route: e.pnl_cents for e in events}
        assert routes == {
            "simulated": Decimal(10),
            "demo_exchange": Decimal(-10),
        }

    def test_settlement_on_an_untraded_market_is_ignored(self) -> None:
        """No detector opened it, so it says nothing about any detector."""
        events, unattributed, _ = attribute_events(
            order_rows=[],
            fill_rows=[],
            settlement_rows=[
                SettlementRow(
                    ticker="ORPHAN",
                    route="simulated",
                    realized_pnl_cents=Decimal("500"),
                    fee_cents=Decimal(0),
                    ts=at(1),
                )
            ],
            proposal_source={},
        )
        assert events == []
        assert not unattributed


class TestScoring:
    def _events(self, n: int, pnl: str) -> list:
        orders = [OrderRow(id=i, proposal_id=i, route="simulated") for i in range(n)]
        fills = [
            fill(order_id=i, ticker=f"M{i}", pnl=pnl, minutes=i) for i in range(n)
        ]
        events, _, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={i: "set_arbitrage" for i in range(n)},
        )
        return sorted(events, key=lambda e: e.ts)

    def test_below_the_floor_refuses_however_good_the_mean(self) -> None:
        """Four winning trades is not evidence, and must not read as any.

        This is the fail-closed gate. A great-looking mean over a handful of
        trades is the most dangerous output the system can produce, so the
        floor is checked before anything else and cannot be argued past.
        """
        report = build_report(
            detector="set_arbitrage",
            route="simulated",
            funnel=Funnel(signals=4, proposals=4, approved=4),
            events=self._events(4, "250"),
            claimed_edge=Decimal("2.5"),
            min_trades=20,
        )
        assert report.verdict is Verdict.INSUFFICIENT_EVIDENCE
        assert report.realised.n == 4
        assert report.realised.total_cents == Decimal(1000)
        # The sentence must not read as an endorsement.
        assert "insufficient" in report.headline.lower()

    def test_drawdown_uses_the_time_ordered_curve(self) -> None:
        """Peak-to-trough of the equity curve, not of a reordered one."""
        orders = [OrderRow(id=i, proposal_id=i, route="simulated") for i in range(4)]
        pnls = ["100", "-60", "-10", "40"]
        fills = [
            fill(order_id=i, ticker=f"M{i}", pnl=p, minutes=i)
            for i, p in enumerate(pnls)
        ]
        events, _, _ = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={i: "d" for i in range(4)},
        )
        report = build_report(
            detector="d",
            route="simulated",
            funnel=Funnel(),
            events=sorted(events, key=lambda e: e.ts),
            claimed_edge=None,
            min_trades=1,
        )
        # Curve is 100, 40, 30, 70 — peak 100, trough 30.
        assert report.max_drawdown_cents == Decimal(70)

    def test_a_losing_first_trade_counts_toward_drawdown(self) -> None:
        """The equity curve must start at zero, not at the first result.

        Without a leading zero the first trade's P&L *is* the starting peak,
        so a detector that lost on its opening trade reports a drawdown of
        nothing. The error only ever flatters, and it hides precisely the
        trade an operator would want to see.
        """
        report = build_report(
            detector="d",
            route="simulated",
            funnel=Funnel(),
            events=self._events(1, "-24"),
            claimed_edge=None,
            min_trades=1,
        )
        assert report.max_drawdown_cents == Decimal(24)

    def test_a_detector_that_never_traded_still_reports(self) -> None:
        """Silence must be distinguishable from being switched off."""
        report = build_report(
            detector="longshot_calibration",
            route="simulated",
            funnel=Funnel(signals=12, observations=340, proposals=0),
            events=[],
            claimed_edge=Decimal("0"),
            min_trades=20,
        )
        assert report.verdict is Verdict.INSUFFICIENT_EVIDENCE
        assert report.realised.n == 0
        assert report.funnel.signals == 12
        assert report.max_drawdown_cents == Decimal(0)

    def test_claimed_edge_is_reported_beside_the_realised_one(self) -> None:
        """The gap between the two is the finding, so both must survive."""
        report = build_report(
            detector="stale_quote",
            route="simulated",
            funnel=Funnel(),
            events=self._events(3, "-5"),
            claimed_edge=Decimal("8.0000"),
            min_trades=20,
        )
        assert report.avg_claimed_edge_cents == Decimal("8.0000")
        assert report.realised.mean_cents == Decimal(-5)

    def test_json_keeps_money_as_strings(self) -> None:
        """Parsing these into a JS number reintroduces the precision loss."""
        report = build_report(
            detector="d",
            route="simulated",
            funnel=Funnel(),
            events=self._events(2, "2.24"),
            claimed_edge=None,
            min_trades=20,
        )
        blob = report.to_dict()
        assert blob["total_pnl_cents"] == "4.48"
        assert isinstance(blob["mean_pnl_cents"], str)
        assert blob["verdict"] == "insufficient_evidence"


class TestFunnelCounters:
    def test_orders_and_fills_are_counted_per_detector(self) -> None:
        orders = [
            OrderRow(id=1, proposal_id=1, route="simulated"),
            OrderRow(id=2, proposal_id=1, route="simulated"),
            OrderRow(id=3, proposal_id=2, route="simulated"),
        ]
        fills = [
            fill(order_id=1, ticker="A", minutes=0),
            fill(order_id=3, ticker="B", minutes=1),
        ]
        _, _, extra = attribute_events(
            order_rows=orders,
            fill_rows=fills,
            settlement_rows=[],
            proposal_source={1: "alpha", 2: "beta"},
        )
        assert extra["alpha"] == {"orders": 2, "fills": 1}
        assert extra["beta"] == {"orders": 1, "fills": 1}

    def test_decided_excludes_expiry(self) -> None:
        """Lapsing is not a decision; a queue nobody read is not a rejection."""
        funnel = Funnel(proposals=10, approved=1, executed=2, rejected=1, expired=6)
        assert funnel.decided == 4


@pytest.mark.parametrize("route", ["simulated", "demo_exchange", "live_exchange"])
def test_every_route_survives_attribution(route: str) -> None:
    events, _, _ = attribute_events(
        order_rows=[OrderRow(id=1, proposal_id=1, route=route)],
        fill_rows=[fill(order_id=1, ticker="M", pnl="1", minutes=0)],
        settlement_rows=[],
        proposal_source={1: "d"},
    )
    assert events[0].route == route

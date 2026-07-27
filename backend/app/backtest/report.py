"""The report card: what each detector actually did with real money.

This is the module the README points at from "Going live":

    Before you do this, read the per-detector report card. A detector that
    has not proven positive expectancy on paper, net of fees, has not earned
    real money.

So it has one job, and it is not to produce an encouraging number. It is to
answer "has this detector demonstrated an edge?" in a way that says **no**
whenever the honest answer is "we cannot tell yet" — which, for a system that
has been running for days rather than months, is nearly always.

Three decisions here do most of the work, and all three make the reported
numbers *worse*. That is the intended direction.

Routes are never merged
-----------------------
Every figure is per ``(detector, route)``. A simulated fill and a demo-exchange
fill are not the same evidence: one of them happened at an exchange and one
of them happened in :mod:`app.trading.paper`. Summing them produces a P&L
belonging to no book that exists, which is the same mistake ``Position.route``
was added to prevent after a simulated -27 and a real +2 merged into -25 while
Kalshi held +2.

The unit of observation is a *decision*, not a fill
--------------------------------------------------
A five-leg set arbitrage settles as five rows. Counting those as five trades
inflates ``n`` fivefold and shrinks the confidence interval by :math:`\\sqrt 5`
— and the legs are not independent in the slightest, they are one thesis with
one outcome. Since the interval is precisely what decides whether a detector
gets real money, an inflated ``n`` is the single most dangerous arithmetic
error available here.

So realising events are grouped by the **proposal** that caused them and
summed. One approval, one observation, however many legs it had.

Ambiguous attribution is dropped, not split
-------------------------------------------
A settlement realises P&L against a *position*, and a position is keyed by
``(ticker, route)`` — it does not remember which proposal built it. When two
detectors have both traded the same market on the same route, there is no
defensible way to divide the outcome between them, so that event is counted
in ``unattributed`` and excluded from both. Apportioning it by contracts or
by cost would be inventing a number, and the number would be the one deciding
whether to deploy capital.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.stats import (
    Expectancy,
    Verdict,
    describe,
    expectancy,
    max_drawdown,
    verdict,
)
from app.core.logging import get_logger
from app.db.models import (
    Fill,
    Order,
    ProposalStatus,
    ProposedTrade,
    Settlement,
    Signal,
)

log = get_logger(__name__)

__all__ = [
    "Funnel",
    "RealisedEvent",
    "OrderRow",
    "FillRow",
    "SettlementRow",
    "DetectorReport",
    "attribute_events",
    "build_report",
    "detector_reports",
]

#: Ceiling on rows pulled for any one report. The report card is a page in a
#: dashboard, not an export: it must not be the query that kills the worker
#: the way an unprojected ``select(Market)`` once did at 122,887 rows. If a
#: deployment ever exceeds this many fills the cap is the wrong shape and the
#: aggregation belongs in SQL — but it will not be reached by anything this
#: system can currently produce.
MAX_ROWS = 20_000

#: How far back a report looks by default. Long enough that a detector
#: enabled last month is still judged on all of it.
DEFAULT_WINDOW = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class Funnel:
    """Signals in, decisions out — where a detector's output goes to die.

    Almost as informative as the P&L, and available far sooner. A detector
    emitting hundreds of signals that never become proposals is being filtered
    by risk or sizing; one whose proposals are all ``EXPIRED`` is finding
    things the operator does not believe, or is finding them faster than
    anyone can read them. Neither shows up in a P&L that is still empty.
    """

    signals: int = 0
    #: Sum of ``Signal.seen_count`` — how many scans re-derived the same
    #: findings. A large ratio to ``signals`` means an edge that persisted;
    #: a ratio of 1 means each was seen once and vanished.
    observations: int = 0
    proposals: int = 0
    approved: int = 0
    executed: int = 0
    partial: int = 0
    rejected: int = 0
    expired: int = 0
    pending: int = 0
    failed: int = 0
    orders: int = 0
    fills: int = 0

    @property
    def decided(self) -> int:
        """Proposals a human actually ruled on. Expiry is not a decision."""
        return self.approved + self.executed + self.partial + self.rejected


@dataclass(frozen=True, slots=True)
class RealisedEvent:
    """One closed decision: P&L that has actually been booked.

    ``pnl_cents`` is net of the fees charged on the fills that opened and
    closed it, because a gross number is not a result. It is signed and
    fractional; nothing about Kalshi's fees rounds to a whole cent.
    """

    key: str
    detector: str
    route: str
    ts: datetime
    pnl_cents: Decimal
    fees_cents: Decimal
    legs: int


@dataclass(frozen=True, slots=True)
class DetectorReport:
    detector: str
    route: str
    funnel: Funnel
    #: The edge the detector *claimed*, averaged over its signals. Kept beside
    #: the realised expectancy on purpose: the gap between what a detector
    #: says it has found and what it has delivered is the finding.
    avg_claimed_edge_cents: Decimal | None
    realised: Expectancy
    verdict: Verdict
    headline: str
    fees_paid_cents: Decimal
    max_drawdown_cents: Decimal
    #: Realising events that touched more than one detector on this route and
    #: were therefore excluded from both. Surfaced rather than buried: if this
    #: is large, the expectancy above is drawn from a minority of the trades.
    unattributed: int

    def to_dict(self) -> dict[str, Any]:
        """JSON for the dashboard. Money crosses the boundary as strings.

        Parsing these into a JS ``number`` would reintroduce exactly the
        precision loss the ``Decimal`` plumbing exists to avoid, so the
        frontend formats them and never computes with them.
        """
        exp = self.realised
        return {
            "detector": self.detector,
            "route": self.route,
            "funnel": {
                "signals": self.funnel.signals,
                "observations": self.funnel.observations,
                "proposals": self.funnel.proposals,
                "approved": self.funnel.approved,
                "executed": self.funnel.executed,
                "partial": self.funnel.partial,
                "rejected": self.funnel.rejected,
                "expired": self.funnel.expired,
                "pending": self.funnel.pending,
                "failed": self.funnel.failed,
                "orders": self.funnel.orders,
                "fills": self.funnel.fills,
                "decided": self.funnel.decided,
            },
            "avg_claimed_edge_cents": (
                str(self.avg_claimed_edge_cents)
                if self.avg_claimed_edge_cents is not None
                else None
            ),
            "trades": exp.n,
            "total_pnl_cents": str(exp.total_cents),
            "mean_pnl_cents": str(exp.mean_cents),
            "ci_low_cents": (
                str(exp.ci_low_cents) if exp.ci_low_cents is not None else None
            ),
            "ci_high_cents": (
                str(exp.ci_high_cents) if exp.ci_high_cents is not None else None
            ),
            "ci_method": exp.method,
            "fees_paid_cents": str(self.fees_paid_cents),
            "max_drawdown_cents": str(self.max_drawdown_cents),
            "verdict": str(self.verdict),
            "headline": self.headline,
            "unattributed": self.unattributed,
        }


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------


async def _funnels(
    session: AsyncSession, *, since: datetime
) -> tuple[dict[str, Funnel], dict[str, Decimal | None], dict[int, str]]:
    """Signal and proposal counts per detector, plus proposal -> detector.

    Every query here is a grouped aggregate rather than a row fetch: the
    counts are the whole answer, and pulling proposals into Python to length
    them would be the unbounded-query mistake with extra steps.
    """
    signal_rows = (
        await session.execute(
            select(
                Signal.detector,
                func.count(Signal.id),
                func.coalesce(func.sum(Signal.seen_count), 0),
                func.avg(Signal.net_edge_cents),
            )
            .where(Signal.created_at >= since)
            .group_by(Signal.detector)
        )
    ).all()

    counts: dict[str, dict[str, int]] = {}
    claimed: dict[str, Decimal | None] = {}
    for detector, n, seen, avg_edge in signal_rows:
        counts.setdefault(detector, {})["signals"] = int(n)
        counts[detector]["observations"] = int(seen)
        claimed[detector] = (
            Decimal(str(avg_edge)).quantize(Decimal("0.0001"))
            if avg_edge is not None
            else None
        )

    status_rows = (
        await session.execute(
            select(
                ProposedTrade.source,
                ProposedTrade.status,
                func.count(ProposedTrade.id),
            )
            .where(ProposedTrade.created_at >= since)
            .group_by(ProposedTrade.source, ProposedTrade.status)
        )
    ).all()

    for source, status, n in status_rows:
        bucket = counts.setdefault(source, {})
        bucket["proposals"] = bucket.get("proposals", 0) + int(n)
        bucket[ProposalStatus(status).value] = int(n)

    # proposal id -> detector. Bounded by MAX_ROWS and projected to two
    # columns; the mapping is needed row by row to attribute fills, and there
    # is no aggregate that can stand in for it.
    proposal_source = dict(
        (
            await session.execute(
                select(ProposedTrade.id, ProposedTrade.source)
                .where(ProposedTrade.created_at >= since)
                .order_by(ProposedTrade.id.desc())
                .limit(MAX_ROWS)
            )
        ).all()
    )

    funnels = {
        name: Funnel(
            signals=c.get("signals", 0),
            observations=c.get("observations", 0),
            proposals=c.get("proposals", 0),
            approved=c.get("approved", 0),
            executed=c.get("executed", 0),
            partial=c.get("partial", 0),
            rejected=c.get("rejected", 0),
            expired=c.get("expired", 0),
            pending=c.get("pending", 0),
            failed=c.get("failed", 0),
        )
        for name, c in counts.items()
    }
    return funnels, claimed, proposal_source


@dataclass(frozen=True, slots=True)
class OrderRow:
    """The three columns of an order the report card needs."""

    id: int
    proposal_id: int | None
    route: str


@dataclass(frozen=True, slots=True)
class FillRow:
    order_id: int | None
    ticker: str
    fee_cents: Decimal
    realized_pnl_cents: Decimal
    ts: datetime


@dataclass(frozen=True, slots=True)
class SettlementRow:
    ticker: str
    route: str
    realized_pnl_cents: Decimal
    fee_cents: Decimal
    ts: datetime


def attribute_events(
    *,
    order_rows: list[OrderRow],
    fill_rows: list[FillRow],
    settlement_rows: list[SettlementRow],
    proposal_source: dict[int, str],
) -> tuple[list[RealisedEvent], dict[tuple[str, str], int], dict[str, dict[str, int]]]:
    """Group realised P&L into decisions and attribute each to a detector.

    Pure, and deliberately so: this is the subtle half of the report card and
    it decides what ``n`` is, which decides how wide the confidence interval
    is, which decides whether a detector is allowed near real money. It is
    worth being able to test directly rather than through a database.

    Returns the events, a count of *unattributed* events per ``(detector,
    route)``, and per-detector order/fill counts for the funnel.

    ``fill_rows`` must be ordered by time: the first proposal to trade a
    market is the one a later settlement is merged into, and "first" is only
    meaningful in order.
    """
    order_proposal = {o.id: o.proposal_id for o in order_rows}
    order_route = {o.id: o.route or "simulated" for o in order_rows}

    funnel_extra: dict[str, dict[str, int]] = {}

    def _bump(detector: str, field: str) -> None:
        funnel_extra.setdefault(detector, {"orders": 0, "fills": 0})[field] += 1

    for order in order_rows:
        detector = (
            proposal_source.get(order.proposal_id)
            if order.proposal_id is not None
            else None
        )
        if detector:
            _bump(detector, "orders")

    # Which detectors touched each (ticker, route). A settlement realises
    # against the position, and the position does not remember who built it,
    # so this is how a settlement is attributed at all.
    touched: dict[tuple[str, str], set[str]] = {}
    #: (ticker, route) -> the single proposal that built it, when there was
    #: only one. Lets a settlement merge into the same observation as the
    #: fills of the decision it settles, rather than counting as a second.
    sole_proposal: dict[tuple[str, str], int | None] = {}

    # proposal-keyed accumulation of realising fills
    acc: dict[str, dict[str, Any]] = {}

    for fill in fill_rows:
        order_id = fill.order_id
        ticker = fill.ticker
        fee_cents = fill.fee_cents
        realized = fill.realized_pnl_cents
        ts = fill.ts
        # `is not None`, not truthiness: an order id of 0 is a real order.
        # Postgres identities start at 1 so this would not bite in production,
        # which is exactly what makes it the kind of bug that survives to
        # somewhere it does.
        has_order = order_id is not None
        route = order_route.get(order_id, "simulated") if has_order else "simulated"
        pid = order_proposal.get(order_id) if has_order else None
        detector = proposal_source.get(pid) if pid is not None else None
        if detector:
            _bump(detector, "fills")

        pos_key = (ticker, route)
        if detector:
            touched.setdefault(pos_key, set()).add(detector)
        if pos_key not in sole_proposal:
            sole_proposal[pos_key] = pid
        elif sole_proposal[pos_key] != pid:
            sole_proposal[pos_key] = None

        if detector is None:
            # A manual ticket, or a fill whose order predates proposal
            # linking. It is real money but it is nobody's report card.
            continue

        key = f"proposal:{pid}"
        entry = acc.setdefault(
            key,
            {
                "detector": detector,
                "route": route,
                "ts": ts,
                "pnl": Decimal(0),
                "fees": Decimal(0),
                "legs": 0,
            },
        )
        # Fees are charged on every fill; realised P&L only on the ones that
        # reduce a position. Both belong to the decision, and the expectancy
        # must be net of the fees the decision paid to get on — a thesis that
        # paid to build a position it never closed has lost money.
        entry["fees"] += Decimal(fee_cents or 0)
        entry["pnl"] += Decimal(realized or 0) - Decimal(fee_cents or 0)
        entry["legs"] += 1
        if ts > entry["ts"]:
            entry["ts"] = ts

    unattributed: dict[tuple[str, str], int] = {}

    for settlement in settlement_rows:
        ticker = settlement.ticker
        route = settlement.route or "simulated"
        realized = settlement.realized_pnl_cents
        fee_cents = settlement.fee_cents
        detectors = touched.get((ticker, route), set())
        if not detectors:
            # Held to settlement but never traded by a detector on this
            # route — a manual position, or one opened before the report
            # window. Not evidence about any detector.
            continue
        if len(detectors) > 1:
            for d in detectors:
                unattributed[(d, route)] = unattributed.get((d, route), 0) + 1
            continue

        detector = next(iter(detectors))
        pid = sole_proposal.get((ticker, route))
        # Merge into the decision's own observation when one proposal built
        # the position; otherwise the settlement stands alone rather than
        # being attached to an arbitrary one of several.
        key = f"proposal:{pid}" if pid is not None else f"settlement:{ticker}:{route}"
        entry = acc.setdefault(
            key,
            {
                "detector": detector,
                "route": route,
                "ts": settlement.ts,
                "pnl": Decimal(0),
                "fees": Decimal(0),
                "legs": 0,
            },
        )
        entry["fees"] += Decimal(fee_cents or 0)
        entry["pnl"] += Decimal(realized or 0) - Decimal(fee_cents or 0)
        entry["legs"] += 1
        if settlement.ts > entry["ts"]:
            entry["ts"] = settlement.ts

    events = [
        RealisedEvent(
            key=key,
            detector=str(v["detector"]),
            route=str(v["route"]),
            ts=v["ts"],
            pnl_cents=v["pnl"],
            fees_cents=v["fees"],
            legs=int(v["legs"]),
        )
        for key, v in acc.items()
    ]
    events.sort(key=lambda e: e.ts)
    return events, unattributed, funnel_extra


async def _realised_events(
    session: AsyncSession,
    *,
    since: datetime,
    proposal_source: dict[int, str],
) -> tuple[list[RealisedEvent], dict[tuple[str, str], int], dict[str, dict[str, int]]]:
    """Read the three tables :func:`attribute_events` needs and call it.

    Three projected, capped queries rather than one join: the cap then applies
    to each independently and no single query can fan out. This layer holds no
    logic — everything that could be wrong lives in the pure function.
    """
    orders = [
        OrderRow(id=oid, proposal_id=pid, route=route or "simulated")
        for oid, pid, route in (
            await session.execute(
                select(Order.id, Order.proposal_id, Order.route)
                .where(Order.created_at >= since)
                .order_by(Order.id.desc())
                .limit(MAX_ROWS)
            )
        ).all()
    ]

    fills = [
        FillRow(
            order_id=order_id,
            ticker=ticker,
            fee_cents=Decimal(fee or 0),
            realized_pnl_cents=Decimal(realized or 0),
            ts=ts,
        )
        for order_id, ticker, fee, realized, ts in (
            await session.execute(
                select(
                    Fill.order_id,
                    Fill.ticker,
                    Fill.fee_cents,
                    Fill.realized_pnl_cents,
                    Fill.ts,
                )
                .where(Fill.ts >= since)
                # Ordered because attribution merges a settlement into the
                # first proposal that traded the market, and "first" needs an
                # order. An unordered fetch would attribute nondeterministically.
                .order_by(Fill.ts)
                .limit(MAX_ROWS)
            )
        ).all()
    ]

    settlements = [
        SettlementRow(
            ticker=ticker,
            route=route or "simulated",
            realized_pnl_cents=Decimal(realized or 0),
            fee_cents=Decimal(fee or 0),
            # ``settled_at`` is when the market resolved; ``created_at`` is
            # when we noticed. The former is the truth and the latter is the
            # fallback, because a settlement synced late would otherwise sort
            # into the equity curve at the wrong place.
            ts=settled_at or created_at,
        )
        for ticker, route, realized, fee, settled_at, created_at in (
            await session.execute(
                select(
                    Settlement.ticker,
                    Settlement.route,
                    Settlement.realized_pnl_cents,
                    Settlement.fee_cents,
                    Settlement.settled_at,
                    Settlement.created_at,
                )
                .where(Settlement.created_at >= since)
                .order_by(Settlement.created_at)
                .limit(MAX_ROWS)
            )
        ).all()
    ]

    return attribute_events(
        order_rows=orders,
        fill_rows=fills,
        settlement_rows=settlements,
        proposal_source=proposal_source,
    )


# ---------------------------------------------------------------------------
# Assembling
# ---------------------------------------------------------------------------


def build_report(
    *,
    detector: str,
    route: str,
    funnel: Funnel,
    events: list[RealisedEvent],
    claimed_edge: Decimal | None,
    min_trades: int,
    unattributed: int = 0,
) -> DetectorReport:
    """Score one detector on one route. Pure — the tests drive this directly.

    ``events`` must already be filtered to this ``(detector, route)`` and
    ordered by time. Order matters twice: the drawdown of a reordered equity
    curve is a number about nothing, and the bootstrap is only reproducible
    over a stable sequence.
    """
    pnls = [e.pnl_cents for e in events]
    exp = expectancy(pnls)
    v = verdict(exp, min_trades=min_trades)

    # The curve starts at zero, before the first trade. Without that leading
    # point `max_drawdown` takes the first trade's *result* as the starting
    # peak, so a detector whose opening trade lost 24c reports no drawdown at
    # all — an error that always flatters, and always by exactly the trade
    # you would most want to see.
    equity: list[Decimal] = [Decimal(0)]
    running = Decimal(0)
    for pnl in pnls:
        running += pnl
        equity.append(running)

    return DetectorReport(
        detector=detector,
        route=route,
        funnel=funnel,
        avg_claimed_edge_cents=claimed_edge,
        realised=exp,
        verdict=v,
        headline=describe(v, exp),
        fees_paid_cents=sum((e.fees_cents for e in events), Decimal(0)),
        max_drawdown_cents=max_drawdown(equity),
        unattributed=unattributed,
    )


async def detector_reports(
    session: AsyncSession,
    *,
    min_trades: int,
    since: datetime | None = None,
    now: datetime | None = None,
) -> list[DetectorReport]:
    """Every detector's report card, one row per ``(detector, route)``.

    A detector that has emitted signals but never traded still gets a row:
    its funnel is the interesting part, and its absence from the table would
    be indistinguishable from it not being enabled.
    """
    now = now or datetime.now(UTC)
    since = since or (now - DEFAULT_WINDOW)

    funnels, claimed, proposal_source = await _funnels(session, since=since)
    events, unattributed, funnel_extra = await _realised_events(
        session, since=since, proposal_source=proposal_source
    )

    by_pair: dict[tuple[str, str], list[RealisedEvent]] = {}
    for event in events:
        by_pair.setdefault((event.detector, event.route), []).append(event)

    # Every detector with a funnel gets at least one row. When it has traded
    # on no route we still show it, under the route its orders would take,
    # because "enabled, signalling, never traded" is a real and common state
    # and hiding it makes the table look like the detector is off.
    pairs: set[tuple[str, str]] = set(by_pair)
    for name in funnels:
        if not any(d == name for d, _ in pairs):
            pairs.add((name, "simulated"))
    for pair in set(unattributed):
        pairs.add(pair)

    reports: list[DetectorReport] = []
    for name, route in sorted(pairs):
        base = funnels.get(name, Funnel())
        extra = funnel_extra.get(name, {})
        funnel = Funnel(
            signals=base.signals,
            observations=base.observations,
            proposals=base.proposals,
            approved=base.approved,
            executed=base.executed,
            partial=base.partial,
            rejected=base.rejected,
            expired=base.expired,
            pending=base.pending,
            failed=base.failed,
            orders=extra.get("orders", 0),
            fills=extra.get("fills", 0),
        )
        reports.append(
            build_report(
                detector=name,
                route=route,
                funnel=funnel,
                events=by_pair.get((name, route), []),
                claimed_edge=claimed.get(name),
                min_trades=min_trades,
                unattributed=unattributed.get((name, route), 0),
            )
        )

    # Most-traded first: a detector with evidence is what the operator came
    # to read. Ties fall back to signal volume so an idle detector with a
    # loud funnel does not sit above one that has actually traded.
    reports.sort(key=lambda r: (-r.realised.n, -r.funnel.proposals, r.detector))
    return reports

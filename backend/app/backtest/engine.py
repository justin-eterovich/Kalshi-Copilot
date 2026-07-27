"""Running a backtest over what this deployment actually stored.

The pure halves live elsewhere and have no idea a database exists:
:mod:`app.backtest.coverage` decides whether the data can support a
conclusion, :mod:`app.backtest.replay` walks the observations without letting
a strategy see the future, and :mod:`app.backtest.stats` scores the result.
This module is the only part that runs SQL, and it deliberately holds no
judgement of its own.

**Coverage is checked before the replay, not after.** That ordering is the
whole design. A backtest that runs first and reports coverage as a footnote
produces a number, and a number is what gets remembered — the footnote is
not. So :func:`run_backtest` refuses by default, and overriding that refusal
is an explicit argument with a name that says what it is.

What can be replayed
--------------------
Only markets whose book we stored, over the window we stored it. That is a
much smaller thing than "Kalshi's history": there is no historical book
endpoint, so the only orderbook data that will ever exist for a past moment
is the snapshot ingest happened to take. Nothing here can widen that, and a
backtest that quietly filled from a wider set would be measuring a strategy
that could not have been run.

Settlement is ground truth and comes from the market's own ``result``. It is
the one field in this system that is not a forecast.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest import coverage as cov
from app.backtest import replay as rp
from app.backtest import stats
from app.core.fees import series_of
from app.core.logging import get_logger
from app.db.models import Market, OrderbookSnap
from app.trading.paper import simulate_fills

log = get_logger(__name__)

__all__ = [
    "BacktestResult",
    "collect_coverage",
    "load_observations",
    "load_outcomes",
    "paper_filler",
    "run_backtest",
]

#: Cadence the orderbook snapshot ingest aims for, from
#: ``ingest.orderbook_snapshot_throttle_ms``. Handed to the coverage audit so
#: its gap check has something to measure against.
SNAPSHOT_INTERVAL = timedelta(seconds=1)

#: Ceiling on snapshots pulled into one replay. Snapshots are the largest
#: thing here by row count and the replay holds them all in order, so this is
#: a memory bound as much as a query bound. Same lesson as everywhere else in
#: this codebase: a filter is not a cap.
MAX_SNAPSHOTS = 200_000

#: Ceiling on markets in one run.
MAX_MARKETS = 5_000


@dataclass(frozen=True, slots=True)
class BacktestResult:
    coverage: cov.CoverageReport
    replay: rp.ReplayResult | None
    expectancy: stats.Expectancy | None
    verdict: stats.Verdict | None
    headline: str

    @property
    def ran(self) -> bool:
        return self.replay is not None

    def summary_lines(self) -> list[str]:
        lines = list(self.coverage.summary_lines())
        if self.replay is None:
            lines.append("")
            lines.append("Replay not run: coverage refused.")
            return lines
        r = self.replay
        lines += [
            "",
            f"observations replayed: {r.observations}",
            f"intents: {r.intents}, filled: {r.filled_intents}",
            f"settlements: {r.settlements}, unsettled at end: {len(r.unsettled)}",
            f"realised P&L: {r.realized_pnl_cents}c "
            f"(fees {r.fees_paid_cents}c)",
            f"max drawdown: "
            f"{stats.max_drawdown([e for _, e in r.equity_curve])}c",
            "",
            self.headline,
        ]
        return lines


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def collect_coverage(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    tickers: Sequence[str] | None = None,
) -> list[cov.MarketCoverage]:
    """One coverage row per market with snapshots in the window.

    The per-market aggregate is computed in SQL. Doing it in Python would
    mean fetching every snapshot to count them, which is the query this
    function exists to avoid making anyone write.

    ``max_gap`` is measured with a window function rather than implied from
    counts, because implied gaps are a mean and the thing that ruins a replay
    is the *worst* hole, not the average one. A run of dense sampling either
    side of a two-hour outage has a perfectly healthy mean.
    """
    lag_ts = func.lag(OrderbookSnap.ts).over(
        partition_by=OrderbookSnap.ticker, order_by=OrderbookSnap.ts
    )
    gaps = (
        select(
            OrderbookSnap.ticker.label("ticker"),
            (OrderbookSnap.ts - lag_ts).label("gap"),
        )
        .where(OrderbookSnap.ts >= window_start, OrderbookSnap.ts <= window_end)
        .subquery()
    )
    max_gaps = dict(
        (
            await session.execute(
                select(gaps.c.ticker, func.max(gaps.c.gap)).group_by(gaps.c.ticker)
            )
        ).all()
    )

    stmt = (
        select(
            OrderbookSnap.ticker,
            func.min(OrderbookSnap.ts),
            func.max(OrderbookSnap.ts),
            func.count(),
        )
        .where(OrderbookSnap.ts >= window_start, OrderbookSnap.ts <= window_end)
        .group_by(OrderbookSnap.ticker)
        .limit(MAX_MARKETS)
    )
    if tickers:
        stmt = stmt.where(OrderbookSnap.ticker.in_(list(tickers)))
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []

    meta = dict(
        (t, (s, r, c))
        for t, s, r, c in (
            await session.execute(
                select(
                    Market.ticker,
                    Market.series_ticker,
                    Market.result,
                    Market.close_time,
                ).where(Market.ticker.in_([r[0] for r in rows]))
            )
        ).all()
    )

    out: list[cov.MarketCoverage] = []
    for ticker, first_ts, last_ts, count in rows:
        series, result, close_time = meta.get(ticker, (None, None, None))
        out.append(
            cov.MarketCoverage(
                ticker=ticker,
                series_ticker=series,
                first_ts=_utc(first_ts),
                last_ts=_utc(last_ts),
                observations=int(count),
                resolved_outcome=_resolved(result),
                close_time=_utc(close_time) if close_time else None,
                max_gap=max_gaps.get(ticker),
            )
        )
    return out


def _resolved(result: str | None) -> bool | None:
    """``result`` as a tri-state outcome.

    An unsettled market carries the **empty string**, not NULL — 153,808 rows
    in this catalog do. Testing truthiness on it would be right by accident;
    testing ``is not None`` would call every open market settled.
    """
    value = (result or "").strip().lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def load_observations(
    session: AsyncSession,
    *,
    window_start: datetime,
    window_end: datetime,
    tickers: Sequence[str] | None = None,
) -> list[rp.Observation]:
    """Stored book snapshots, in time order, as replay observations.

    Ordered by ``ts`` in SQL. :func:`app.backtest.replay.replay` verifies the
    ordering itself and raises rather than sorting, so an unordered fetch
    here would be caught — but it would be caught as a crash in a pure module
    rather than as the look-ahead bug it actually is, so the order belongs in
    the query.
    """
    stmt = (
        select(
            OrderbookSnap.ticker,
            OrderbookSnap.ts,
            OrderbookSnap.yes_levels,
            OrderbookSnap.no_levels,
        )
        .where(OrderbookSnap.ts >= window_start, OrderbookSnap.ts <= window_end)
        .order_by(OrderbookSnap.ts, OrderbookSnap.ticker)
        .limit(MAX_SNAPSHOTS)
    )
    if tickers:
        stmt = stmt.where(OrderbookSnap.ticker.in_(list(tickers)))

    return [
        rp.Observation(
            ts=_utc(ts),
            ticker=ticker,
            # Reassembled into the shape `paper.simulate_fills` expects: the
            # orderbook endpoint's `{"yes": [...], "no": [...]}`, both sides
            # quoted as bids, values as strings. Stored split across two
            # columns; the simulator has one shape and it is that one.
            book=(
                None
                if yes_levels is None and no_levels is None
                else {"yes": yes_levels or [], "no": no_levels or []}
            ),
        )
        for ticker, ts, yes_levels, no_levels in (await session.execute(stmt)).all()
    ]


async def load_outcomes(
    session: AsyncSession, tickers: Sequence[str]
) -> dict[str, rp.Outcome]:
    """Settlement outcomes for markets that resolved.

    A market with no ``result`` yields no entry, which leaves any position in
    it in the replay's ``unsettled`` bucket rather than marking it to a price.
    That is the intended behaviour: an open position is not a result, and
    marking it to the last quote invents a profit nobody realised.

    ``close_time`` stands in for the settlement timestamp, which we do not
    store per market. It is the earliest moment the outcome could have been
    known and it errs in the safe direction — settling *later* than reality
    would let a strategy keep trading a market whose answer was already
    public.
    """
    if not tickers:
        return {}
    rows = (
        await session.execute(
            select(Market.ticker, Market.result, Market.close_time)
            .where(Market.ticker.in_(list(tickers)))
            .limit(MAX_MARKETS)
        )
    ).all()

    out: dict[str, rp.Outcome] = {}
    for ticker, result, close_time in rows:
        resolved = _resolved(result)
        if resolved is None or close_time is None:
            continue
        out[ticker] = rp.Outcome(
            ticker=ticker, settled_yes=resolved, settled_at=_utc(close_time)
        )
    return out


# ---------------------------------------------------------------------------
# Filling
# ---------------------------------------------------------------------------


def paper_filler(*, slippage_cents: Decimal = Decimal(0)) -> rp.Filler:
    """Adapter from the replay's ``Filler`` protocol to the paper simulator.

    The backtest does **not** get its own fill model. `app.trading.paper` is
    the one that walks the book, charges a taker fee per price level and
    refuses to fill through a limit, and a backtest scored against a second,
    friendlier model would be measuring the model. Any pessimism configured
    for live paper trading applies here unchanged, which is the point.
    """

    def fill(
        intent: rp.Intent, observation: rp.Observation
    ) -> list[rp.ExecutedFill]:
        if observation.book is None:
            # No book stored for this instant. Filling anyway would be
            # inventing a price, so nothing trades — the same answer the
            # simulator gives live when a market has no book.
            return []
        fills = simulate_fills(
            book=observation.book,
            side=str(intent.side),
            action=intent.action,
            limit_price=intent.limit_price,
            contracts=intent.contracts,
            ticker=intent.ticker,
            slippage_cents=slippage_cents,
        )
        return [
            rp.ExecutedFill(
                ticker=intent.ticker,
                side=intent.side,
                action=intent.action,
                price=f.price,
                contracts=f.contracts,
                fee_cents=f.fee_cents,
            )
            for f in fills
        ]

    return fill


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


async def run_backtest(
    session: AsyncSession,
    *,
    strategy: rp.Strategy,
    window_start: datetime,
    window_end: datetime,
    tickers: Sequence[str] | None = None,
    slippage_cents: Decimal = Decimal(0),
    min_trades: int = 20,
    ignore_coverage: bool = False,
    expected_interval: timedelta = SNAPSHOT_INTERVAL,
) -> BacktestResult:
    """Audit coverage, then replay, then score. In that order.

    ``ignore_coverage`` runs the replay over data the audit refused. It exists
    because "show me what it would have done anyway" is a legitimate thing to
    want while developing a strategy — but the resulting numbers are not
    evidence, and the returned :class:`BacktestResult` still carries every
    refusal so nothing downstream can present them as if they were.
    """
    markets = await collect_coverage(
        session,
        window_start=window_start,
        window_end=window_end,
        tickers=tickers,
    )
    report = cov.audit(
        markets,
        window_start=window_start,
        window_end=window_end,
        expected_interval=expected_interval,
    )

    if not report.usable and not ignore_coverage:
        return BacktestResult(
            coverage=report,
            replay=None,
            expectancy=None,
            verdict=None,
            headline=(
                "Refused: the stored data cannot support a conclusion. "
                + "; ".join(f.render() for f in report.refusals)
            ),
        )

    observations = await load_observations(
        session,
        window_start=window_start,
        window_end=window_end,
        tickers=tickers,
    )
    outcomes = await load_outcomes(
        session, sorted({o.ticker for o in observations})
    )

    result = rp.replay(
        observations,
        strategy=strategy,
        filler=paper_filler(slippage_cents=slippage_cents),
        outcomes=outcomes,
    )

    pnls = [t.realized_pnl_cents for t in result.trades]
    exp = stats.expectancy(pnls)
    decision = stats.verdict(exp, min_trades=min_trades)

    return BacktestResult(
        coverage=report,
        replay=result,
        expectancy=exp,
        verdict=decision,
        headline=stats.describe(decision, exp),
    )


def series_for(ticker: str) -> str | None:
    """Re-exported so a strategy can price fees without importing fees.py."""
    return series_of(ticker)


def to_dict(result: BacktestResult) -> dict[str, Any]:
    """JSON for a caller that wants to render this. Money stays a string."""
    body: dict[str, Any] = {
        "ran": result.ran,
        "headline": result.headline,
        "coverage": {
            "usable": result.coverage.usable,
            "markets": result.coverage.market_count,
            "settled": result.coverage.settled_count,
            "series": result.coverage.series_count,
            "observations": result.coverage.total_observations,
            "refusals": [f.render() for f in result.coverage.refusals],
            "warnings": [f.render() for f in result.coverage.warnings],
            "summary": result.coverage.summary_lines(),
        },
    }
    if result.replay is not None and result.expectancy is not None:
        r = result.replay
        body["replay"] = {
            "trades": len(r.trades),
            "observations": r.observations,
            "fills": len(r.fills),
            "settlements": r.settlements,
            "unsettled": len(r.unsettled),
            "realized_pnl_cents": str(r.realized_pnl_cents),
            "fees_paid_cents": str(r.fees_paid_cents),
            "mean_pnl_cents": str(result.expectancy.mean_cents),
            "verdict": str(result.verdict),
        }
    return body

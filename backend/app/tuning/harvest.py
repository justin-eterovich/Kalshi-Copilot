"""Reading the window out of Postgres, and nothing else.

The only module in ``app/tuning`` that runs SQL. Everything it produces is a
plain :class:`~app.tuning.mark.Pick` or :class:`~app.tuning.mark.Quote`, so the
scoring, the gate and the dossier stay testable without a database.

Three habits this codebase learned the hard way, all of them applied here:

**Project the columns.** ``select(Signal)`` would drag the whole ``evidence``
JSONB of every row into Python. That is the pattern that killed the worker
outright at 122,887 markets and then recurred in ``worker/calibration.py`` at
152,263 rows / 201 MB every 300 seconds. The window bounds it today; a bound is
not a cap, and an unbounded query arms itself as the data grows.

**Push the filter into SQL.** The window is a ``WHERE``, the proposal check is
an ``IN`` over ids, and the quotes are one keyed fetch — not a scan filtered in
Python.

**A cap needs an ``ORDER BY`` and a log line.** Ordered newest-first so a bound
cap truncates the *old* end of the window, which is at least a coherent shorter
window rather than an arbitrary slice; and it says so, loudly, because a
silently truncated harvest reports confident aggregates over whichever rows the
planner happened to return.

One deliberate non-use: ``Settlement`` is not read here. A settlement is a
*position's* payout and belongs to the book that held it, whereas a pick is a
claim about a market — most picks never became a position at all. The market's
own ``result`` is the right source for "was this claim right", and mixing the
two would produce a P&L belonging to neither.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.fees import FeeSchedule
from app.core.logging import get_logger
from app.db.models import Market, ProposedTrade, Signal
from app.tuning.mark import BUY, SELL, Mark, Pick, Quote, mark_pick
from app.tuning.window import TuningWindow

log = get_logger(__name__)

__all__ = ["MAX_SIGNAL_ROWS", "harvest", "harvest_picks"]

#: Hard cap on signal rows read for one run.
#:
#: Sized well above a plausible day (the busiest detector here writes ~109 rows
#: a day after dedupe folding) so it should never bind — which is exactly why
#: it must announce itself when it does. If it binds, the answer is a shorter
#: window, not a bigger cap: the aggregates would otherwise describe part of a
#: window while being labelled with all of it.
MAX_SIGNAL_ROWS: Final = 20_000

#: Market ``result`` values that mean "resolved, but to nothing". A void is the
#: absence of an outcome, not an outcome of zero — see
#: ``mark.Unmarkable.MARKET_VOID``.
VOID_RESULTS: Final = frozenset({"void", "voided", "cancelled", "canceled"})


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse a wire/JSONB value to Decimal, or ``None`` if it is not a price.

    Returns ``None`` rather than raising: a malformed price in a *signal's*
    evidence is that pick's problem alone, and it becomes a named refusal
    downstream (``ENTRY_PRICE_INVALID``) rather than aborting a whole run. That
    is the opposite of the rule on the trading path, where a malformed price
    must raise — nothing here can place an order.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _resolution(result: str | None) -> tuple[bool | None, bool]:
    """Map ``Market.result`` to ``(resolved_outcome, is_void)``.

    ``result`` is ``''`` for an open market — 153,808 rows of it — **not**
    NULL, which is why this cannot be an ``is not None`` check. That mistake
    calls every open market settled.
    """
    if result is None:
        return None, False
    token = result.strip().lower()
    if not token:
        return None, False
    if token in VOID_RESULTS:
        return None, True
    if token == "yes":
        return True, False
    if token == "no":
        return False, False
    # An unrecognised, non-empty result. Treat as unresolved rather than
    # guessing a direction: guessing wrong inverts the sign of that pick's
    # entire P&L, and a mark that is confidently backwards is worse than one
    # that is missing.
    log.warning(
        "tuning harvest: unrecognised market result %r; treating as unresolved.",
        result,
    )
    return None, False


async def harvest_picks(
    session: AsyncSession,
    window: TuningWindow,
    *,
    detectors: Sequence[str] | None = None,
    max_rows: int = MAX_SIGNAL_ROWS,
) -> list[Pick]:
    """Every signal written inside the window, reduced to picks.

    ``detectors`` restricts the harvest; ``None`` takes them all. Folded rows
    count once — the dedupe guard is what makes a repeated observation one
    pick, and ``seen_count`` carries how persistent it was.
    """
    stmt = (
        select(
            Signal.id,
            Signal.detector,
            Signal.ticker,
            Signal.side,
            Signal.net_edge_cents,
            Signal.confidence,
            Signal.size_hint,
            Signal.evidence,
            Signal.created_at,
            Signal.seen_count,
        )
        .where(
            Signal.created_at >= window.start,
            Signal.created_at < window.end,
        )
        .order_by(desc(Signal.created_at))
        .limit(max_rows)
    )
    if detectors:
        stmt = stmt.where(Signal.detector.in_(list(detectors)))

    rows = (await session.execute(stmt)).all()

    if len(rows) >= max_rows:
        log.warning(
            "tuning harvest: signal cap of %d rows BOUND for window %s. The "
            "oldest part of the window was dropped, so every aggregate "
            "describes a shorter window than the one it is labelled with. "
            "Shorten the window rather than raising the cap.",
            max_rows,
            window.describe(),
        )

    if not rows:
        return []

    tickers = {r.ticker for r in rows}
    market_rows = (
        await session.execute(
            select(Market.ticker, Market.event_ticker).where(
                Market.ticker.in_(tickers)
            )
        )
    ).all()
    events = {r.ticker: r.event_ticker for r in market_rows}

    signal_ids = [r.id for r in rows]
    proposed = {
        sid
        for (sid,) in (
            await session.execute(
                select(ProposedTrade.signal_id).where(
                    ProposedTrade.signal_id.in_(signal_ids)
                )
            )
        ).all()
        if sid is not None
    }

    picks: list[Pick] = []
    for row in rows:
        evidence = row.evidence or {}
        action = str(evidence.get("action") or BUY).lower()
        if action not in (BUY, SELL):
            # Not a direction we can score. Left as `buy` would silently
            # invert half of them; `direction.py` is emphatic that nothing
            # downstream catches an inversion.
            log.warning(
                "tuning harvest: signal %d has action %r, which is neither "
                "buy nor sell; skipping rather than assuming a direction.",
                row.id,
                action,
            )
            continue

        picks.append(
            Pick(
                signal_id=row.id,
                detector=row.detector,
                ticker=row.ticker,
                side=row.side,
                action=action,
                created_at=row.created_at,
                claimed_edge_cents=row.net_edge_cents,
                confidence=row.confidence,
                entry_price=_decimal_or_none(evidence.get("price")),
                contracts=row.size_hint,
                event_ticker=events.get(row.ticker),
                seen_count=row.seen_count or 1,
                became_proposal=row.id in proposed,
            )
        )

    return picks


async def quotes_for(
    session: AsyncSession, tickers: Sequence[str]
) -> dict[str, Quote]:
    """Current quote and resolution for each ticker, keyed by ticker.

    A ticker absent from the result is absent from the catalog, which the
    caller turns into ``MARKET_MISSING`` rather than a zero.
    """
    if not tickers:
        return {}

    rows = (
        await session.execute(
            select(
                Market.ticker,
                Market.yes_bid,
                Market.yes_ask,
                Market.no_bid,
                Market.no_ask,
                Market.result,
            ).where(Market.ticker.in_(list(set(tickers))))
        )
    ).all()

    quotes: dict[str, Quote] = {}
    for row in rows:
        resolved, is_void = _resolution(row.result)
        quotes[row.ticker] = Quote(
            ticker=row.ticker,
            yes_bid=row.yes_bid,
            yes_ask=row.yes_ask,
            no_bid=row.no_bid,
            no_ask=row.no_ask,
            resolved_outcome=resolved,
            is_void=is_void,
        )
    return quotes


async def harvest(
    session: AsyncSession,
    window: TuningWindow,
    *,
    slippage_cents: Decimal | str | int = 0,
    detectors: Sequence[str] | None = None,
    schedule: FeeSchedule | None = None,
    is_taker: bool = True,
    max_rows: int = MAX_SIGNAL_ROWS,
) -> list[Mark]:
    """Harvest the window and score every pick in it.

    Returns one :class:`Mark` per pick — including the unscoreable ones, which
    carry their reason. Nothing is dropped for being unscoreable; that is the
    whole point of the type.
    """
    picks = await harvest_picks(
        session, window, detectors=detectors, max_rows=max_rows
    )
    if not picks:
        return []

    quotes = await quotes_for(session, [p.ticker for p in picks])

    return [
        mark_pick(
            pick,
            quotes.get(pick.ticker),
            slippage_cents=slippage_cents,
            schedule=schedule,
            is_taker=is_taker,
        )
        for pick in picks
    ]

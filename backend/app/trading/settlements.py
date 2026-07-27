"""Settlement reconciliation — closing the positions that were never traded out.

Everything else in this package realises P&L when a fill *reduces* a position.
That covers the trades that get closed early and misses the ones that work:
a thesis held to resolution pays out at settlement, and until this module
existed the system recorded the cost of building that position and none of the
proceeds. Every risk limit downstream reads the resulting number, so a blind
spot here is a blind spot in the daily loss limit and the loss cooldown too.

Two books settle from two different places, and they are not interchangeable:

- **Exchange routes** (demo, live) settle from ``GET /portfolio/settlements``.
  That is the exchange's own record of what it paid us, and it is the only
  authority for a real book.
- **The simulated route** has no exchange record — those positions exist
  nowhere but in this database. They settle from the market's own
  ``result``/``settlement_value``, which catalog sync already stores.

The payout, not the cost basis
------------------------------

The settlement payload reports its own cost basis (``yes_total_cost_dollars``
and friends). We deliberately ignore it and price against our own
``avg_price``. The two disagree whenever a position was partly traded out
before settlement — the exchange's basis describes the contracts it still saw
open, ours describes what we recognised — and a book that sources its cost
basis from two places will eventually have the position row and the daily
total telling different stories about the same market. The payload is used
only for what it alone knows: that the market resolved, and what one YES
contract paid.

Units, again
------------

The settlement payload mixes conventions in a single object, which is a trap
worth naming: ``yes_total_cost_dollars`` and ``fee_cost`` are fixed-point
dollar **strings**, while ``revenue`` and ``value`` are **integer cents**.
Only ``value`` is read here, and only as a fallback — for a scalar market the
market's own ``settlement_value`` is fixed-point and carries the precision
that ``value`` has already rounded away.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.money import parse_dollars
from app.db.models import Market, Position, Settlement
from app.trading.interlocks import ExecutionRoute

log = get_logger(__name__)

__all__ = [
    "settled_yes_value",
    "realized_from_settlement",
    "apply_settlement",
    "sync_exchange_settlements",
    "sync_local_settlements",
    "settlement_view",
]

HUNDRED = Decimal(100)
ONE = Decimal(1)


def settled_yes_value(
    *,
    market_result: str | None,
    value_cents: object = None,
    settlement_value: Decimal | None = None,
) -> Decimal | None:
    """What one YES contract paid, in dollars.

    ``None`` when the result is missing or not one this can price — an
    unsettled market, or a result string nobody has seen before. Refusing is
    the point: a settlement priced by guesswork writes a permanent, wrong P&L
    into a book that has no correction path, and "assume it went to zero" is
    the guess that looks harmless and is not.

    For a scalar market the precise payout is the market's fixed-point
    ``settlement_value``; the ``value`` field on the settlement payload is
    integer cents and has already lost whatever sub-cent precision existed.
    """
    result = (market_result or "").strip().lower()

    if result == "yes":
        return ONE
    if result == "no":
        return Decimal(0)

    if result == "scalar":
        if settlement_value is not None:
            return settlement_value
        if value_cents not in (None, ""):
            return parse_dollars(value_cents, "value") / HUNDRED
        return None

    # "" (not settled yet), "void", "" — and anything unrecognised. A voided
    # market returns the stake rather than paying out, which is not a payout
    # value at all; it needs its own handling, not a number invented here.
    return None


def realized_from_settlement(
    *, net_contracts: Decimal, avg_price: Decimal, payout_yes: Decimal
) -> Decimal:
    """P&L in cents from holding ``net_contracts`` through settlement.

    Identical in shape to closing the position at ``payout_yes``, because that
    is exactly what settlement is: every contract is bought back at what it
    turned out to be worth. Signed YES-equivalents make the NO side fall out
    for free — 5 NO contracts carried at 0.70 that settle NO are
    ``(0 - 0.70) * -5 * 100`` = +350 cents.
    """
    return (payout_yes - avg_price) * net_contracts * HUNDRED


async def apply_settlement(
    session: AsyncSession,
    *,
    ticker: str,
    route: str,
    payout_yes: Decimal,
    event_ticker: str | None = None,
    market_result: str | None = None,
    fee_cents: Decimal = Decimal(0),
    settled_at: datetime | None = None,
    source: str = "exchange",
) -> Settlement | None:
    """Realise a settled market against the position on ``route``.

    Returns ``None`` — and writes nothing — when there is no open position or
    the settlement was already applied. Both are the normal case: settlements
    arrive for every market we ever touched, most of which are already flat,
    and this runs on a loop over the same window repeatedly.
    """
    existing = (
        await session.execute(
            select(Settlement).where(
                Settlement.ticker == ticker, Settlement.route == route
            )
        )
    ).scalars().first()
    if existing is not None:
        return None

    position = (
        await session.execute(
            select(Position).where(Position.ticker == ticker, Position.route == route)
        )
    ).scalars().first()

    net = (position.net_contracts if position else None) or Decimal(0)
    if position is None or net == 0:
        # Nothing was held. Recording a zero-P&L settlement row anyway would
        # bloat the table with every market the account ever closed out of.
        return None

    avg = position.avg_price or Decimal(0)
    realized = realized_from_settlement(
        net_contracts=net, avg_price=avg, payout_yes=payout_yes
    )

    row = Settlement(
        ticker=ticker,
        event_ticker=event_ticker,
        route=route,
        market_result=market_result,
        settled_yes_value=payout_yes,
        net_contracts=net,
        avg_price=avg,
        realized_pnl_cents=realized,
        fee_cents=fee_cents,
        source=source,
        settled_at=settled_at,
    )
    session.add(row)

    # The position is gone: settlement does not leave a residue.
    position.net_contracts = Decimal(0)
    position.avg_price = Decimal(0)
    position.realized_pnl_cents = (position.realized_pnl_cents or Decimal(0)) + realized
    position.fees_paid_cents = (position.fees_paid_cents or Decimal(0)) + fee_cents

    from app.trading.positions import roll_daily

    await roll_daily(
        session,
        day=(settled_at or datetime.now(UTC)).date(),
        route=route,
        is_paper=route != ExecutionRoute.LIVE_EXCHANGE.value,
        realized=realized,
        fee_cents=fee_cents,
        kind="settlement",
    )

    log.info(
        "settled %s on %s: %s contracts @ %s -> %s paid %s, realised %.4fc",
        ticker,
        route,
        net,
        avg,
        market_result or "?",
        payout_yes,
        float(realized),
    )
    return row


async def sync_exchange_settlements(
    session: AsyncSession,
    client: Any,
    *,
    route: str,
    max_pages: int | None = 5,
) -> int:
    """Pull settlements from the exchange and apply any that are new.

    Only ever called for a route that actually reaches Kalshi. The simulated
    book must not be settled from this feed: those positions do not exist at
    the exchange, so its settlements describe a different set of contracts
    entirely and would realise P&L against holdings we never had.
    """
    if route == ExecutionRoute.SIMULATED.value:
        raise ValueError("the simulated book does not settle from the exchange")

    applied = 0
    async for raw in client.get_settlements(max_pages=max_pages):
        ticker = raw.get("ticker")
        if not ticker:
            continue

        payout = settled_yes_value(
            market_result=raw.get("market_result"),
            value_cents=raw.get("value"),
            settlement_value=await _market_settlement_value(session, ticker),
        )
        if payout is None:
            log.warning(
                "skipping settlement for %s: unpriceable result %r",
                ticker,
                raw.get("market_result"),
            )
            continue

        row = await apply_settlement(
            session,
            ticker=ticker,
            route=route,
            payout_yes=payout,
            event_ticker=raw.get("event_ticker"),
            market_result=raw.get("market_result"),
            fee_cents=_fee_cents(raw.get("fee_cost")),
            settled_at=_parse_ts(raw.get("settled_time")),
            source="exchange",
        )
        if row is not None:
            applied += 1

    return applied


async def sync_local_settlements(session: AsyncSession) -> int:
    """Settle simulated positions from the markets' own results.

    The simulated book has no counterparty, so nothing will ever tell it that
    a market resolved; without this, a paper position in a settled market sits
    open forever and its cost is recorded while its payout never is. That
    makes the simulated report card systematically pessimistic, which is the
    one direction of wrong that looks responsible and still misleads.
    """
    route = ExecutionRoute.SIMULATED.value

    open_tickers = (
        await session.execute(
            select(Position.ticker).where(
                Position.route == route, Position.net_contracts != 0
            )
        )
    ).scalars().all()
    if not open_tickers:
        return 0

    markets = (
        await session.execute(select(Market).where(Market.ticker.in_(open_tickers)))
    ).scalars().all()

    applied = 0
    for market in markets:
        payout = settled_yes_value(
            market_result=market.result,
            settlement_value=market.settlement_value,
        )
        if payout is None:
            continue

        row = await apply_settlement(
            session,
            ticker=market.ticker,
            route=route,
            payout_yes=payout,
            event_ticker=market.event_ticker,
            market_result=market.result,
            # No settlement fee: the local simulator never charged one, so
            # inventing one here would take money out of a book that never
            # had it deducted.
            settled_at=market.settlement_ts,
            source="market",
        )
        if row is not None:
            applied += 1

    return applied


async def _market_settlement_value(
    session: AsyncSession, ticker: str
) -> Decimal | None:
    return (
        await session.execute(
            select(Market.settlement_value).where(Market.ticker == ticker)
        )
    ).scalars().first()


def _fee_cents(value: object) -> Decimal:
    """Settlement fees: fixed-point dollars on the wire, fractional cents here."""
    if value in (None, ""):
        return Decimal(0)
    return parse_dollars(value, "fee_cost") * HUNDRED


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def settlement_view(row: Settlement) -> dict[str, object]:
    return {
        "ticker": row.ticker,
        "event_ticker": row.event_ticker,
        "route": row.route,
        "result": row.market_result,
        "settled_yes_value": str(row.settled_yes_value),
        "net_contracts": str(row.net_contracts or Decimal(0)),
        "avg_price": str(row.avg_price or Decimal(0)),
        "realized_pnl_cents": str(row.realized_pnl_cents or Decimal(0)),
        "fee_cents": str(row.fee_cents or Decimal(0)),
        "source": row.source,
        "settled_at": row.settled_at.isoformat() if row.settled_at else None,
    }

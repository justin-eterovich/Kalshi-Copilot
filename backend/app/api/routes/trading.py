"""The human-in-the-loop trading API.

Every endpoint here is either read-only or requires an explicit, per-trade
human decision.  There is no bulk approve, no "approve all", and no endpoint
that creates and executes in one call — the proposal has to exist and be
looked at before it can be approved, because that gap *is* the safety model.

Errors are shaped so the dashboard can explain a refusal precisely:

- **409** with ``{"error": "<code>"}`` — an interlock refused. The trade is
  possible in principle; something about right now says no.
- **409 unverified_fee_category** — the market's fees are not trustworthy, so
  it cannot be proposed at all.
- **400** — the request itself is malformed (a cents-style price, say).

Money crosses this boundary as strings, in both directions.

**Mutating handlers commit explicitly before returning.** The ``session_scope``
dependency also commits, but FastAPI runs a yield-dependency's exit code
*after* the response has been sent — so a client that reads back immediately
races the commit and loses. That is not theoretical: the dashboard reloads the
queue the moment a proposal POST resolves, and without the explicit commit the
proposal it just created is reliably absent. Observed on a live stack, three
times out of three.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config, get_config
from app.core.fees import UnverifiedFeeCategory
from app.core.logging import get_logger
from app.db.base import session_scope
from app.db.models import (
    AuditLog,
    Fill,
    Market,
    Order,
    OrderStatus,
    PnlDaily,
    Position,
    ProposalStatus,
    ProposedTrade,
)
from app.kalshi.rest import KalshiApiError, KalshiRestClient
from app.settings import Settings, get_settings
from app.trading import proposals as prop
from app.trading.executor import ExecutionError, Executor, fill_view, order_view
from app.trading.interlocks import InterlockError, posture
from app.trading.positions import position_view

log = get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(session_scope)]
ConfigDep = Annotated[Config, Depends(get_config)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _rest(request: Request) -> KalshiRestClient | None:
    return getattr(request.app.state, "kalshi", None)


def _executor(request: Request, settings: Settings, config: Config) -> Executor:
    return Executor(_rest(request), settings, config)


def _fee_guard(exc: UnverifiedFeeCategory) -> HTTPException:
    """Fail closed at the API boundary, with the reason the operator needs."""
    return HTTPException(
        status_code=409,
        detail={
            "error": "unverified_fee_category",
            "category": exc.category,
            "message": str(exc),
        },
    )


def _interlock_error(exc: InterlockError) -> HTTPException:
    return HTTPException(
        status_code=409, detail={"error": exc.code, "message": str(exc)}
    )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class TicketRequest(BaseModel):
    """A trade ticket. Prices are dollar strings — ``"0.56"``, never ``56``."""

    ticker: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"] = "buy"
    limit_price: str
    contracts: str
    #: Optional operator or model fair value. Supplying it turns on the net
    #: edge figure; without it the card shows cost and breakeven only.
    fair_price: str | None = None
    rationale: str | None = None
    ttl_sec: int | None = Field(None, ge=5, le=3600)


class ApproveRequest(BaseModel):
    """Per-trade approval. ``confirm`` is never defaulted true."""

    confirm: bool = False
    #: Required on the live route only: the operator types the market ticker.
    #: A misclick cannot produce it.
    confirmation_phrase: str | None = None


class RejectRequest(BaseModel):
    reason: str | None = None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@router.get("/trading/state")
async def trading_state(
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    config: ConfigDep,
) -> dict[str, Any]:
    """Safety posture plus the queue depth, for the header and the badge."""
    pending = (
        await session.execute(
            select(ProposedTrade).where(
                ProposedTrade.status == ProposalStatus.PENDING,
                ProposedTrade.expires_at > datetime.now(UTC),
            )
        )
    ).scalars().all()

    working = (
        await session.execute(
            select(Order).where(
                Order.status.in_(
                    (
                        OrderStatus.PENDING,
                        OrderStatus.RESTING,
                        OrderStatus.PARTIALLY_FILLED,
                    )
                )
            )
        )
    ).scalars().all()

    state: dict[str, Any] = {
        **posture(settings, config),
        "pending_proposals": len(pending),
        "working_orders": len(working),
    }

    # Balance is a live call and entirely optional; the page must render
    # without it rather than fail because a key is missing.
    client = _rest(request)
    if client is not None and client.authenticated:
        try:
            balance = await client.get_balance()
            state["balance"] = {
                "cents": balance.get("balance"),
                "dollars": balance.get("balance_dollars"),
                "portfolio_value_cents": balance.get("portfolio_value"),
            }
        except KalshiApiError as exc:
            state["balance_error"] = str(exc)[:200]

    return state


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@router.post("/proposals/quote")
async def quote_ticket(
    session: SessionDep, config: ConfigDep, ticket: TicketRequest = Body(...)
) -> dict[str, Any]:
    """Price a ticket without creating anything.

    The ticket form calls this as the operator types, so the cost shown on
    the form and the cost recorded on the proposal come from one code path
    and cannot disagree.
    """
    try:
        market, quote = await prop.quote_for(
            session,
            config,
            ticker=ticket.ticker,
            side=ticket.side,
            action=ticket.action,
            limit_price=ticket.limit_price,
            contracts=ticket.contracts,
            fair_price=ticket.fair_price,
        )
    except UnverifiedFeeCategory as exc:
        raise _fee_guard(exc) from exc
    except prop.ProposalError as exc:
        raise HTTPException(
            404 if exc.code == "unknown_market" else 409,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            400, detail={"error": "bad_ticket", "message": str(exc)}
        ) from exc

    return {
        "quote": quote.as_dict(),
        "market": {
            "ticker": market.ticker,
            "title": market.title,
            "category": market.category,
            "status": market.status,
            "yes_bid": None if market.yes_bid is None else str(market.yes_bid),
            "yes_ask": None if market.yes_ask is None else str(market.yes_ask),
        },
    }


@router.post("/proposals", status_code=201)
async def create_proposal(
    session: SessionDep, config: ConfigDep, ticket: TicketRequest = Body(...)
) -> dict[str, Any]:
    """Create a pending proposal. **Nothing is sent to any exchange here.**"""
    if config.risk.kill_switch:
        raise HTTPException(
            409,
            detail={
                "error": "kill_switch",
                "message": "the kill switch is engaged; no new proposals.",
            },
        )

    try:
        proposal, quote = await prop.create_proposal(
            session,
            config,
            ticker=ticket.ticker,
            side=ticket.side,
            action=ticket.action,
            limit_price=ticket.limit_price,
            contracts=ticket.contracts,
            fair_price=ticket.fair_price,
            rationale=ticket.rationale,
            ttl_sec=ticket.ttl_sec,
            source="manual",
        )
    except UnverifiedFeeCategory as exc:
        raise _fee_guard(exc) from exc
    except prop.ProposalError as exc:
        raise HTTPException(
            404 if exc.code == "unknown_market" else 409,
            detail={"error": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            400, detail={"error": "bad_ticket", "message": str(exc)}
        ) from exc

    # Commit before responding: the caller reloads the queue as soon as this
    # returns, and the dependency's commit runs after the response is sent.
    await session.commit()
    return {"proposal": prop.proposal_view(proposal), "quote": quote.as_dict()}


@router.get("/proposals")
async def list_proposals(
    session: SessionDep,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """The approval queue. Pending first, newest first within that."""
    # Expire on read as well as on the worker's timer: the queue must never
    # display a proposal as actionable one second after its TTL ran out.
    await prop.expire_stale(session)

    stmt = select(ProposedTrade)
    if status:
        try:
            stmt = stmt.where(ProposedTrade.status == ProposalStatus(status))
        except ValueError as exc:
            raise HTTPException(400, f"unknown status {status!r}") from exc

    rows = (
        await session.execute(
            stmt.order_by(desc(ProposedTrade.created_at)).limit(limit)
        )
    ).scalars().all()
    # expire_stale above mutated rows; land it now rather than at teardown.
    await session.commit()

    views = [prop.proposal_view(p) for p in rows]
    views.sort(key=lambda v: (v["status"] != "pending", v["created_at"] or ""))
    return {"proposals": views}


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: int,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    config: ConfigDep,
    body: ApproveRequest = Body(...),
) -> dict[str, Any]:
    """Approve **one** proposal and place its order.

    This is the only path from a proposal to an order. It requires
    ``confirm: true`` for every route and, on the live route, the market
    ticker typed back.
    """
    proposal = await session.get(ProposedTrade, proposal_id)
    if proposal is None:
        raise HTTPException(404, f"proposal {proposal_id} not found")

    executor = _executor(request, settings, config)

    try:
        order = await executor.approve_and_execute(
            session,
            proposal,
            confirmed=body.confirm,
            confirmation_phrase=body.confirmation_phrase,
        )
    except InterlockError as exc:
        raise _interlock_error(exc) from exc
    except ExecutionError as exc:
        # The order row and audit trail already record the failure; surface
        # it rather than pretending the approval did not happen.
        raise HTTPException(
            502, detail={"error": exc.code, "message": str(exc)}
        ) from exc

    fills = (
        await session.execute(select(Fill).where(Fill.order_id == order.id))
    ).scalars().all()

    # The order, its fills and the position all have to be durable before the
    # operator is told the trade went through.
    await session.commit()

    return {
        "proposal": prop.proposal_view(proposal),
        "order": order_view(order),
        "fills": [fill_view(f) for f in fills],
    }


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: int, session: SessionDep, body: RejectRequest = Body(default=None)
) -> dict[str, Any]:
    proposal = await session.get(ProposedTrade, proposal_id)
    if proposal is None:
        raise HTTPException(404, f"proposal {proposal_id} not found")

    try:
        await prop.reject(session, proposal, reason=(body.reason if body else None))
    except prop.ProposalError as exc:
        raise HTTPException(
            409, detail={"error": exc.code, "message": str(exc)}
        ) from exc

    await session.commit()
    return {"proposal": prop.proposal_view(proposal)}


# ---------------------------------------------------------------------------
# Orders, fills, positions
# ---------------------------------------------------------------------------


@router.get("/orders")
async def list_orders(
    session: SessionDep,
    ticker: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    stmt = select(Order).order_by(desc(Order.created_at)).limit(limit)
    if ticker:
        stmt = stmt.where(Order.ticker == ticker)
    rows = (await session.execute(stmt)).scalars().all()
    return {"orders": [order_view(o) for o in rows]}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: int,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    config: ConfigDep,
) -> dict[str, Any]:
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(404, f"order {order_id} not found")

    executor = _executor(request, settings, config)
    try:
        await executor.cancel(
            session, order, reason="cancelled by operator", actor="operator"
        )
    except (KalshiApiError, ExecutionError) as exc:
        raise HTTPException(
            502, detail={"error": "cancel_failed", "message": str(exc)}
        ) from exc

    await session.commit()
    return {"order": order_view(order)}


@router.get("/fills")
async def list_fills(
    session: SessionDep,
    ticker: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    stmt = select(Fill).order_by(desc(Fill.ts)).limit(limit)
    if ticker:
        stmt = stmt.where(Fill.ticker == ticker)
    rows = (await session.execute(stmt)).scalars().all()
    return {"fills": [fill_view(f) for f in rows]}


@router.get("/positions")
async def list_positions(session: SessionDep) -> dict[str, Any]:
    """Open positions, marked to the current YES quote where one exists."""
    rows = (
        await session.execute(
            select(Position).where(Position.net_contracts != 0)
        )
    ).scalars().all()

    marks: dict[str, Decimal | None] = {}
    for position in rows:
        market = await session.get(Market, position.ticker)
        # Mark at the midpoint: the last trade can be stale in a thin market,
        # and marking at the bid or the ask flatters one direction.
        if market and market.yes_bid is not None and market.yes_ask is not None:
            marks[position.ticker] = (market.yes_bid + market.yes_ask) / Decimal(2)
        elif market and market.last_price is not None:
            marks[position.ticker] = market.last_price
        else:
            marks[position.ticker] = None

    views = [position_view(p, marks.get(p.ticker)) for p in rows]
    return {"positions": views}


@router.get("/pnl")
async def daily_pnl(
    session: SessionDep, days: int = Query(30, ge=1, le=365)
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(PnlDaily).order_by(desc(PnlDaily.day)).limit(days)
        )
    ).scalars().all()

    return {
        "days": [
            {
                "day": row.day.isoformat(),
                "is_paper": row.is_paper,
                "realized_pnl_cents": str(row.realized_pnl_cents or Decimal(0)),
                "fees_paid_cents": row.fees_paid_cents or 0,
                "trades": row.trades or 0,
            }
            for row in rows
        ]
    }


@router.get("/audit")
async def list_audit(
    session: SessionDep,
    kind: str | None = None,
    ticker: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """The append-only trail. Every signal, proposal, decision, order, fill.

    Ordered by id within a timestamp: ``ts`` defaults to ``now()``, which in
    Postgres is *transaction* start, so every entry written by one request
    shares a timestamp. Without the id tiebreak the trail renders effects
    before their causes — a fill above the approval that produced it.
    """
    stmt = select(AuditLog).order_by(desc(AuditLog.ts), desc(AuditLog.id)).limit(limit)
    if kind:
        stmt = stmt.where(AuditLog.kind == kind)
    if ticker:
        stmt = stmt.where(AuditLog.ticker == ticker)
    rows = (await session.execute(stmt)).scalars().all()

    return {
        "entries": [
            {
                "id": row.id,
                "ts": row.ts.isoformat() if row.ts else None,
                "kind": row.kind,
                "ticker": row.ticker,
                "actor": row.actor,
                "payload": row.payload,
            }
            for row in rows
        ]
    }

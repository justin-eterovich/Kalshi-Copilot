"""The human-in-the-loop trading API.

Every endpoint here is either read-only or requires an explicit, per-trade
human decision.  There is no bulk approve, no "approve all", and no endpoint
that creates and executes in one call — the proposal has to exist and be
looked at before it can be approved.

**Nothing here can produce an autonomous approval.**  The machine path needs a
``MachineConsent``, which only ``app.trading.autonomy`` issues and only the
worker's sweep uses; no request reaching this module can construct one.  That
is worth stating as a property rather than leaving as an accident, because the
dashboard has no auth in front of it by design (LAN-only), so "nothing on the
network can trigger an unattended trade" is doing real work.  A test in
``test_build_identity.py`` pins it.

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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest import report
from app.config import Config, get_config
from app.core.fees import UnknownSeries, UnverifiedFeeSchedule
from app.core.logging import get_logger
from app.core.redis import get_kill_switch, set_kill_switch
from app.db.base import session_scope
from app.db.models import (
    AuditLog,
    CalibrationLog,
    ExternalPrice,
    Fill,
    LlmSpend,
    Market,
    NewsHeadline,
    Order,
    OrderStatus,
    PnlDaily,
    Position,
    ProposalStatus,
    ProposedTrade,
    Settlement,
    Signal,
)
from app.detectors.stale_quote import REFERENCE_PREFIXES
from app.kalshi.rest import KalshiApiError, KalshiRestClient
from app.news.calendar import KNOWN_CATALYSTS, catalyst_for
from app.settings import Settings, get_settings
from app.trading import proposals as prop
from app.trading import risk
from app.trading.executor import (
    LIVE_ORDER_STATUSES,
    ExecutionError,
    Executor,
    fill_view,
    order_view,
)
from app.trading.interlocks import InterlockError, posture
from app.trading.positions import position_view
from app.trading.settlements import settlement_view

log = get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(session_scope)]
ConfigDep = Annotated[Config, Depends(get_config)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _rest(request: Request) -> KalshiRestClient | None:
    return getattr(request.app.state, "kalshi", None)


def _executor(request: Request, settings: Settings, config: Config) -> Executor:
    return Executor(_rest(request), settings, config)


def _fee_guard(exc: UnverifiedFeeSchedule | UnknownSeries) -> HTTPException:
    """Fail closed at the API boundary, with the reason the operator needs."""
    return HTTPException(
        status_code=409,
        detail={
            "error": (
                "unverified_fee_schedule"
                if isinstance(exc, UnverifiedFeeSchedule)
                else "unknown_series"
            ),
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
    """Per-trade approval. ``confirm`` is never defaulted true.

    ``StrictBool``, not ``bool``: pydantic's lax coercion accepted ``"true"``,
    ``"yes"``, ``"on"``, ``"1"``, ``1`` and ``1.0`` as consent, and each of
    those placed a real order. Nothing meaning "no" ever produced consent, so
    this was hardening rather than a hole — but the one field standing between
    a malformed request and a live order should read exactly one value.
    """

    confirm: StrictBool = False
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
    # Counted in SQL, not by materialising rows and calling len(). Both sets
    # are small today — the pending queue is capped at `max_pending_proposals`
    # — but this endpoint backs the dashboard header and is polled
    # continuously, and the working-order set has no cap at all if
    # reconciliation ever wedges.
    pending = (
        await session.execute(
            select(func.count())
            .select_from(ProposedTrade)
            .where(
                ProposedTrade.status == ProposalStatus.PENDING,
                ProposedTrade.expires_at > datetime.now(UTC),
            )
        )
    ).scalar_one()

    working = (
        await session.execute(
            select(func.count())
            .select_from(Order)
            .where(
                Order.status.in_(
                    (
                        OrderStatus.PENDING,
                        OrderStatus.RESTING,
                        OrderStatus.PARTIALLY_FILLED,
                    )
                )
            )
        )
    ).scalar_one()

    state: dict[str, Any] = {
        **posture(settings, config),
        "pending_proposals": pending,
        "working_orders": working,
    }
    # `posture` only knows the config file. The switch that an operator can
    # actually reach at runtime lives in Redis, and the header must show the
    # one that is really in force — a dashboard reading "safe" while the
    # runtime flag is set would be the exact failure this endpoint exists to
    # prevent.
    state["kill_switch"] = await get_kill_switch() or config.risk.kill_switch
    state["kill_switch_config_floor"] = config.risk.kill_switch

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
    except (UnverifiedFeeSchedule, UnknownSeries) as exc:
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


class KillSwitchRequest(BaseModel):
    """Engage or release the runtime kill switch."""

    engaged: StrictBool
    #: Deliberate action, same shape as an approval. Never defaulted true.
    confirm: StrictBool = False


@router.post("/kill-switch")
async def set_kill_switch_route(
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
    config: ConfigDep,
    body: KillSwitchRequest = Body(...),
) -> dict[str, Any]:
    """The emergency stop.

    This exists because there was previously **no way to engage the kill
    switch on a running system**. It lived only in ``config.yaml``, read
    through an ``lru_cache``d loader, with no endpoint and no UI control — so
    firing it meant editing a file and restarting containers. Worse, its two
    halves run in different processes, so restarting only ``api`` left resting
    orders working while the dashboard reported "engaged".

    Engaging does two things and reports both, because "it says engaged" and
    "the orders are gone" are different claims:

    1. Sets the shared flag, which halts new proposals and every approval in
       every process immediately.
    2. Cancels every working order now, rather than waiting for the worker's
       next sweep.
    """
    if not body.confirm:
        raise HTTPException(
            400,
            detail={
                "error": "not_confirmed",
                "message": (
                    "the kill switch requires an explicit confirmation, in "
                    "both directions. Releasing it re-arms trading."
                ),
            },
        )

    await set_kill_switch(body.engaged)

    canceled: list[int] = []
    failed: list[dict[str, Any]] = []
    if body.engaged:
        executor = _executor(request, settings, config)
        working = (
            await session.execute(
                select(Order).where(Order.status.in_(LIVE_ORDER_STATUSES))
            )
        ).scalars().all()
        for order in working:
            try:
                await executor.cancel(
                    session,
                    order,
                    reason="kill switch engaged",
                    actor="operator",
                )
                canceled.append(order.id)
            except (KalshiApiError, ExecutionError) as exc:
                # Report rather than swallow: an order this did not manage to
                # cancel is the single most important thing the operator needs
                # to know right now.
                failed.append({"order_id": order.id, "error": str(exc)})
                log.error("kill switch could not cancel order %s: %s", order.id, exc)

    await prop.audit(
        session,
        kind="kill_switch.engaged" if body.engaged else "kill_switch.released",
        ticker=None,
        actor="operator",
        payload={
            "engaged": body.engaged,
            "canceled_orders": canceled,
            "failed_cancels": failed,
        },
    )
    await session.commit()

    return {
        "kill_switch": body.engaged or config.risk.kill_switch,
        "config_floor": config.risk.kill_switch,
        "canceled_orders": len(canceled),
        "canceled_order_ids": canceled,
        "failed_cancels": failed,
    }


@router.post("/proposals", status_code=201)
async def create_proposal(
    session: SessionDep, config: ConfigDep, ticket: TicketRequest = Body(...)
) -> dict[str, Any]:
    """Create a pending proposal. **Nothing is sent to any exchange here.**"""
    if await get_kill_switch() or config.risk.kill_switch:
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
    except (UnverifiedFeeSchedule, UnknownSeries) as exc:
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
    return {
        "proposal": prop.proposal_view(
            proposal, await prop.legs_of(session, proposal.id)
        ),
        "quote": quote.as_dict(),
    }


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

    # One query for every row's legs, not one per row: this endpoint is polled
    # continuously and returns up to `limit` (500) proposals.
    legs_by_proposal = await prop.legs_for(session, [p.id for p in rows])
    views = [
        prop.proposal_view(p, legs_by_proposal.get(p.id, []))
        for p in rows
    ]
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
        # Commit the failure before surfacing it. The executor deliberately
        # writes the Order rows *before* placing, so that an ambiguous
        # failure — a timeout that may still have reached the matching
        # engine — leaves a client order ID to reconcile against. The
        # session dependency rolls back on exception, which would erase
        # precisely that record and defeat the whole design. Observed live:
        # a rejected batch left zero order rows and a proposal still marked
        # pending, as if nothing had been attempted.
        await session.commit()
        raise HTTPException(
            502, detail={"error": exc.code, "message": str(exc)}
        ) from exc

    fills = (
        await session.execute(
            select(Fill).join(Order, Order.id == Fill.order_id).where(
                Order.proposal_id == proposal.id
            )
        )
    ).scalars().all()

    # The order, its fills and the position all have to be durable before the
    # operator is told the trade went through.
    await session.commit()

    orders = (
        await session.execute(select(Order).where(Order.proposal_id == proposal.id))
    ).scalars().all()

    return {
        "proposal": prop.proposal_view(
            proposal, await prop.legs_of(session, proposal.id)
        ),
        "order": order_view(order),
        "orders": [order_view(o) for o in orders],
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
    return {
        "proposal": prop.proposal_view(
            proposal, await prop.legs_of(session, proposal.id)
        )
    }


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

    # One query for every mark rather than one per position: the loop was an
    # N+1 that grows with the book, on an endpoint the dashboard polls.
    tickers = [p.ticker for p in rows]
    quotes = (
        await session.execute(
            select(Market.ticker, Market.yes_bid, Market.yes_ask, Market.last_price)
            .where(Market.ticker.in_(tickers))
        )
    ).all() if tickers else []

    marks: dict[str, Decimal | None] = {}
    for ticker, yes_bid, yes_ask, last_price in quotes:
        # Mark at the midpoint: the last trade can be stale in a thin market,
        # and marking at the bid or the ask flatters one direction.
        if yes_bid is not None and yes_ask is not None:
            marks[ticker] = (yes_bid + yes_ask) / Decimal(2)
        elif last_price is not None:
            marks[ticker] = last_price
        else:
            marks[ticker] = None

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
                "route": row.route,
                "is_paper": row.is_paper,
                "realized_pnl_cents": str(row.realized_pnl_cents or Decimal(0)),
                "fees_paid_cents": str(row.fees_paid_cents or Decimal(0)),
                "net_pnl_cents": str(
                    (row.realized_pnl_cents or Decimal(0))
                    - (row.fees_paid_cents or Decimal(0))
                ),
                "trades": row.trades or 0,
                "settlements": row.settlements or 0,
            }
            for row in rows
        ]
    }


@router.get("/risk")
async def risk_state(
    session: SessionDep, settings: SettingsDep, config: ConfigDep
) -> dict[str, Any]:
    """The portfolio limits and how close each one is.

    Returned even when nothing is near a limit, because the dashboard's job
    is to show headroom continuously rather than to announce a halt after the
    fact. ``state`` is null only when no execution route is usable at all, in
    which case every approval is already refused upstream.
    """
    state = await risk.halt_state(session, config, settings)
    return {
        "state": state.as_dict() if state else None,
        "limits": {
            "bankroll_usd": config.risk.bankroll_usd,
            "max_pct_per_market": config.risk.max_pct_per_market,
            "max_total_exposure_pct": config.risk.max_total_exposure_pct,
            "daily_loss_limit_pct": config.risk.daily_loss_limit_pct,
            "cooldown_after_consecutive_losses": (
                config.risk.cooldown_after_consecutive_losses
            ),
            "cooldown_minutes": config.risk.cooldown_minutes,
            "kelly_fraction": config.risk.kelly_fraction,
            "max_pending_proposals": config.risk.max_pending_proposals,
        },
    }


@router.get("/settlements")
async def recent_settlements(
    session: SessionDep, limit: int = Query(50, ge=1, le=500)
) -> dict[str, Any]:
    """Markets that resolved while we held them.

    Separate from ``/fills`` on purpose: a settlement is an outcome, not a
    decision, and it is the only place a held-to-resolution thesis shows its
    result.
    """
    rows = (
        await session.execute(
            select(Settlement).order_by(desc(Settlement.created_at)).limit(limit)
        )
    ).scalars().all()
    return {"settlements": [settlement_view(row) for row in rows]}


@router.get("/engine")
async def engine_state(session: SessionDep, config: ConfigDep) -> dict[str, Any]:
    """Reference feeds and calibration coverage — the two things M6 gates on.

    Both answer a question the dashboard could not otherwise answer: *why is a
    detector silent?* A missing spot feed and an unmet sample floor are the
    normal reasons, and they look identical to a broken detector unless the
    state is shown.
    """
    now = datetime.now(UTC)
    max_age = float(
        getattr(config.detectors.stale_quote, "reference_max_age_sec", 5)
    )

    feeds: list[dict[str, Any]] = []
    for symbol in sorted(set(REFERENCE_PREFIXES.values())):
        row = (
            await session.execute(
                select(ExternalPrice)
                .where(ExternalPrice.symbol == symbol)
                .order_by(desc(ExternalPrice.ts))
                .limit(1)
            )
        ).scalars().first()
        age = None if row is None else (now - row.ts).total_seconds()
        feeds.append(
            {
                "symbol": symbol,
                "price": None if row is None else str(row.price),
                "source": None if row is None else row.source,
                "age_sec": age,
                # Stale counts as absent. A stale reference against a live
                # market invents an edge in whichever direction the market
                # already moved.
                "fresh": age is not None and age <= max_age,
            }
        )

    total, settled = (
        await session.execute(
            select(
                func.count(CalibrationLog.id),
                func.count(CalibrationLog.settled_yes),
            )
        )
    ).one()

    buckets = (
        await session.execute(
            select(
                CalibrationLog.price_bucket_cents,
                func.count(CalibrationLog.id),
            )
            .where(CalibrationLog.settled_yes.isnot(None))
            .group_by(CalibrationLog.price_bucket_cents)
            .order_by(CalibrationLog.price_bucket_cents)
        )
    ).all()

    floor = int(
        getattr(
            config.detectors.longshot_calibration,
            "min_samples_before_signalling",
            500,
        )
    )

    return {
        "reference_feeds": feeds,
        "bitcoin_enabled": config.bitcoin.enabled,
        "calibration": {
            "observations": int(total or 0),
            "settled": int(settled or 0),
            "min_samples": floor,
            "buckets": [
                {"cents": int(b), "settled": int(n), "ready": int(n) >= floor}
                for b, n in buckets
            ],
        },
    }


@router.get("/news")
async def news_state(
    session: SessionDep, settings: SettingsDep, config: ConfigDep
) -> dict[str, Any]:
    """Scheduled catalysts, recent headlines, and LLM spend.

    The catalyst list is a **deadline board**, not an opportunity feed. On
    Kalshi's scheduled releases the market closes minutes before the number
    publishes, so a row that has passed its close is a window the operator has
    missed — which is worth showing, and is not the same thing as an edge.
    """
    now = datetime.now(UTC)

    markets = (
        await session.execute(
            select(
                Market.ticker,
                Market.series_ticker,
                Market.title,
                Market.close_time,
                Market.status,
            ).where(
                Market.series_ticker.in_(list(KNOWN_CATALYSTS)),
                Market.close_time.isnot(None),
                Market.close_time >= now - timedelta(hours=12),
                Market.close_time <= now + timedelta(days=14),
            )
        )
    ).all()

    # Grouped by (series, close) rather than listed per market. One FOMC
    # meeting is 40 tradeable strikes but *one* deadline, and forty identical
    # rows is the same failure as an unfoldable signals table: a board nobody
    # reads. The market count is what the operator actually wants — "FOMC rate
    # decision, 11 markets, closes in 2d".
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for ticker, series, title, close, status in markets:
        cat = catalyst_for(
            series_ticker=series,
            close_time=close,
            now=now,
            settled=status in ("settled", "finalized"),
        )
        if cat is None:
            continue

        key = (cat.series_ticker, cat.close_time.isoformat())
        row = grouped.get(key)
        if row is None:
            grouped[key] = {
                # A representative market, so the row can still link somewhere.
                "ticker": ticker,
                "series_ticker": cat.series_ticker,
                "label": cat.label,
                "title": title,
                "market_count": 1,
                "close_time": cat.close_time.isoformat(),
                "expected_release": (
                    cat.expected_release.isoformat()
                    if cat.expected_release
                    else None
                ),
                "state": cat.state.value,
                "minutes_to_close": round(cat.minutes_to_close, 1),
                "actionable": cat.actionable,
            }
        else:
            row["market_count"] += 1

    catalysts = list(grouped.values())
    # Soonest deadline first among the ones still tradeable.
    catalysts.sort(key=lambda c: (not c["actionable"], c["minutes_to_close"]))

    headlines = (
        await session.execute(
            select(NewsHeadline)
            .order_by(desc(NewsHeadline.published_at))
            .limit(30)
        )
    ).scalars().all()

    spend = (
        await session.execute(
            select(LlmSpend).where(LlmSpend.day == now.date())
        )
    ).scalars().first()

    triaged = spend.triaged if spend else 0
    escalated = spend.escalated if spend else 0
    hcfg = config.news.headlines

    return {
        "catalysts": catalysts[:40],
        "headlines": [
            {
                "title": h.title,
                "source": h.source,
                "link": h.link,
                "published_at": h.published_at.isoformat(),
                "matched_tickers": h.matched_tickers or [],
            }
            for h in headlines
        ],
        "budget": {
            "enabled": bool(config.news.enabled and hcfg.enabled),
            # Surfaced so "the engine is silent" has a visible cause rather
            # than looking like a bug.
            "has_api_key": bool(settings.anthropic_api_key.strip()),
            "day": now.date().isoformat(),
            "spent_usd": str(spend.spent_usd if spend else Decimal(0)),
            "budget_usd": str(hcfg.daily_budget_usd),
            "triaged": triaged,
            "escalated": escalated,
            "escalation_rate": (escalated / triaged) if triaged else 0.0,
            "escalation_rate_cap": hcfg.escalation_rate_cap,
        },
        "feeds_configured": len(hcfg.rss_feeds),
    }


@router.get("/signals")
async def list_signals(
    session: SessionDep,
    detector: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """What the detectors have noticed.

    A signal is not a recommendation and not all of them become proposals —
    the resolution sniper deliberately emits research notes it will never act
    on. ``net_edge_cents`` is always net of fees; a zero means the detector
    declined to claim an edge rather than that it found one of zero.
    """
    stmt = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
    if detector:
        stmt = stmt.where(Signal.detector == detector)
    rows = (await session.execute(stmt)).scalars().all()

    return {
        "signals": [
            {
                "id": row.id,
                "detector": row.detector,
                "ticker": row.ticker,
                "side": row.side.value,
                "fair_price": str(row.fair_price),
                "net_edge_cents": str(row.net_edge_cents),
                "confidence": row.confidence,
                "size_hint": None if row.size_hint is None else str(row.size_hint),
                "rationale": row.rationale,
                "evidence": row.evidence,
                # A repeat sighting bumps these rather than adding a row, so
                # "seen 47x over 15m" replaces 47 near-identical entries.
                "seen_count": row.seen_count or 1,
                "last_seen_at": (
                    row.last_seen_at.isoformat() if row.last_seen_at else None
                ),
                "created_at": (
                    row.created_at.isoformat() if row.created_at else None
                ),
            }
            for row in rows
        ]
    }


@router.get("/report-card")
async def report_card(
    session: SessionDep,
    config: ConfigDep,
    days: int = Query(365, ge=1, le=3650),
) -> dict[str, Any]:
    """Per-detector evidence: what each one claimed, and what it delivered.

    The README sends the operator here before enabling live trading, so this
    endpoint's job is to be *unpersuadable*. Below
    ``backtest.report_card_min_trades`` every detector reports
    ``insufficient_evidence`` regardless of how good its mean looks, because
    a flattering average over four trades is the most dangerous number this
    system can produce.

    Figures are per ``(detector, route)`` and never summed across routes — a
    simulated fill and a demo-exchange fill are different kinds of evidence.
    """
    now = datetime.now(UTC)
    reports = await report.detector_reports(
        session,
        min_trades=config.backtest.report_card_min_trades,
        since=now - timedelta(days=days),
        now=now,
    )
    return {
        "min_trades": config.backtest.report_card_min_trades,
        "window_days": days,
        "generated_at": now.isoformat(),
        "detectors": [r.to_dict() for r in reports],
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

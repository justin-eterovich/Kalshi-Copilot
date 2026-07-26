"""Proposal lifecycle: create, expire, decide.

A proposal is a *request for a human decision*, and it is the only thing the
executor will act on.  Detector signals (M4+) and manual tickets both land
here, in the same queue, priced the same way — there is no fast path that
skips the queue, and adding one would defeat the point of the system.

Proposals expire.  That is not housekeeping: a proposal carries a price that
was executable when it was written, and approving a two-hour-old quote means
trading against a book that has moved. Expiry is what stops a stale edge from
being actionable at all, rather than merely inadvisable.

Every state change writes an :class:`~app.db.models.AuditLog` row and
publishes to Redis so the dashboard reflects it without polling.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.core.redis import CH_PROPOSALS, get_redis
from app.db.models import AuditLog, Market, ProposalStatus, ProposedTrade, Side
from app.trading.pricing import TicketQuote, price_ticket

log = get_logger(__name__)

__all__ = [
    "ProposalError",
    "create_proposal",
    "quote_for",
    "expire_stale",
    "reject",
    "proposal_view",
    "publish",
    "audit",
]

#: Statuses that still need a human decision.
OPEN_STATUSES = (ProposalStatus.PENDING,)


class ProposalError(RuntimeError):
    """The proposal cannot be created. ``code`` is stable for the UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _market_or_raise(session: AsyncSession, ticker: str) -> Market:
    market = await session.get(Market, ticker)
    if market is None:
        raise ProposalError(
            "unknown_market",
            f"{ticker} is not in the local catalog. Proposals price against "
            "stored market data, including the category the fee multiplier "
            "depends on.",
        )
    if market.status not in (None, "active", "open", "initialized"):
        raise ProposalError(
            "market_not_tradeable",
            f"{ticker} is {market.status}; it cannot be traded.",
        )
    return market


async def quote_for(
    session: AsyncSession,
    config: Config,
    *,
    ticker: str,
    side: Side | str,
    action: str,
    limit_price: Decimal | str,
    contracts: Decimal | str,
    fair_price: Decimal | str | None = None,
) -> tuple[Market, TicketQuote]:
    """Price a hypothetical ticket without creating anything.

    The ticket UI calls this on every keystroke so the operator sees the fee
    and breakeven before committing. It is the same code path that prices the
    real proposal, so the numbers on the confirmation cannot disagree with
    the numbers on the form.
    """
    market = await _market_or_raise(session, ticker)
    quote = price_ticket(
        ticker=ticker,
        side=side,
        action=action,
        limit_price=limit_price,
        contracts=contracts,
        # From the Event, joined onto the Market by catalog sync. If this is
        # null the fee falls back to the default multiplier, which is why
        # ingest warns about uncategorised markets.
        category=market.category,
        config=config,
        fair_price=fair_price,
    )
    return market, quote


async def create_proposal(
    session: AsyncSession,
    config: Config,
    *,
    ticker: str,
    side: Side | str,
    action: str,
    limit_price: Decimal | str,
    contracts: Decimal | str,
    source: str = "manual",
    fair_price: Decimal | str | None = None,
    rationale: str | None = None,
    ttl_sec: int | None = None,
    signal_id: int | None = None,
    actor: str = "operator",
) -> tuple[ProposedTrade, TicketQuote]:
    """Create a pending proposal. Nothing is sent to any exchange here.

    Raises:
        ProposalError: market unknown or untradeable.
        UnverifiedFeeCategory: the market's fees cannot be trusted, so it is
            excluded rather than proposed with a guessed cost.
    """
    _, quote = await quote_for(
        session,
        config,
        ticker=ticker,
        side=side,
        action=action,
        limit_price=limit_price,
        contracts=contracts,
        fair_price=fair_price,
    )

    ttl = ttl_sec if ttl_sec is not None else config.trading.default_proposal_ttl_sec
    now = datetime.now(UTC)

    proposal = ProposedTrade(
        signal_id=signal_id,
        source=source,
        ticker=ticker,
        side=Side(side),
        action=action,
        limit_price=quote.limit_price,
        contracts=quote.contracts,
        fair_price=quote.fair_price,
        net_edge_cents=quote.net_edge_cents,
        est_fee_cents=quote.est_fee_cents,
        pct_of_bankroll=_pct_of_bankroll(quote, config),
        rationale=rationale,
        status=ProposalStatus.PENDING,
        expires_at=now + timedelta(seconds=ttl),
    )
    session.add(proposal)
    await session.flush()

    await audit(
        session,
        kind="proposal.created",
        ticker=ticker,
        actor=actor,
        payload={"proposal_id": proposal.id, "source": source, **quote.as_dict()},
    )
    await publish(proposal, event="created")
    return proposal, quote


def _pct_of_bankroll(quote: TicketQuote, config: Config) -> float:
    """Share of bankroll this trade risks, as a fraction.

    Uses the worst case, not the notional: for a buy those are the same, but
    for a sell they are not, and sizing should be judged on what can be lost.
    Real sizing limits arrive with the risk layer in M5; this is the number
    the operator sees while approving in the meantime.
    """
    bankroll_cents = Decimal(str(config.risk.bankroll_usd)) * Decimal(100)
    if bankroll_cents <= 0:
        return 0.0
    return float(quote.max_loss_cents / bankroll_cents)


async def expire_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Move every pending proposal past its TTL to EXPIRED.

    Runs in the worker on a short timer, and is also applied defensively at
    approval time — a proposal that expired one second before the click must
    not execute just because the sweep had not run yet.
    """
    now = now or datetime.now(UTC)

    stale = (
        await session.execute(
            select(ProposedTrade).where(
                ProposedTrade.status == ProposalStatus.PENDING,
                ProposedTrade.expires_at.isnot(None),
                ProposedTrade.expires_at <= now,
            )
        )
    ).scalars().all()

    if not stale:
        return 0

    for proposal in stale:
        proposal.status = ProposalStatus.EXPIRED
        proposal.decided_at = now
        proposal.decision_reason = "ttl elapsed without a decision"
        await audit(
            session,
            kind="proposal.expired",
            ticker=proposal.ticker,
            actor="system",
            payload={"proposal_id": proposal.id},
        )
        await publish(proposal, event="expired")

    log.info("expired %d proposal(s) past their ttl", len(stale))
    return len(stale)


async def reject(
    session: AsyncSession,
    proposal: ProposedTrade,
    *,
    reason: str | None = None,
    actor: str = "operator",
) -> ProposedTrade:
    """Decline a proposal. Recorded, because a rejection is data too.

    The report card needs to know what the operator turned down, not just
    what they took — a detector whose signals are always rejected is a
    detector that is wrong in a way its P&L will never show.
    """
    if proposal.status is not ProposalStatus.PENDING:
        raise ProposalError(
            "not_pending",
            f"proposal {proposal.id} is already {proposal.status.value}.",
        )

    proposal.status = ProposalStatus.REJECTED
    proposal.decided_at = datetime.now(UTC)
    proposal.decision_reason = reason or "rejected by operator"

    await audit(
        session,
        kind="proposal.rejected",
        ticker=proposal.ticker,
        actor=actor,
        payload={"proposal_id": proposal.id, "reason": proposal.decision_reason},
    )
    await publish(proposal, event="rejected")
    return proposal


# ---------------------------------------------------------------------------
# Serialisation, audit, fan-out
# ---------------------------------------------------------------------------


def proposal_view(proposal: ProposedTrade) -> dict[str, Any]:
    """Serialise for the API. Money is a string; the browser must not compute."""
    now = datetime.now(UTC)
    expires_in = None
    if proposal.expires_at is not None:
        expires_in = round((proposal.expires_at - now).total_seconds(), 1)

    return {
        "id": proposal.id,
        "signal_id": proposal.signal_id,
        "source": proposal.source,
        "ticker": proposal.ticker,
        "side": proposal.side.value,
        "action": proposal.action,
        "limit_price": str(proposal.limit_price),
        "contracts": str(proposal.contracts),
        "fair_price": (
            None if proposal.fair_price is None else str(proposal.fair_price)
        ),
        # Always net of fees and slippage. There is no gross figure to show.
        "net_edge_cents": (
            None if proposal.net_edge_cents is None else str(proposal.net_edge_cents)
        ),
        "est_fee_cents": proposal.est_fee_cents,
        "pct_of_bankroll": proposal.pct_of_bankroll,
        "rationale": proposal.rationale,
        "status": proposal.status.value,
        "expires_at": (
            proposal.expires_at.isoformat() if proposal.expires_at else None
        ),
        "expires_in_sec": expires_in,
        "decided_at": (
            proposal.decided_at.isoformat() if proposal.decided_at else None
        ),
        "decision_reason": proposal.decision_reason,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
    }


async def audit(
    session: AsyncSession,
    *,
    kind: str,
    ticker: str | None,
    actor: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append to the audit log. Append-only: never updated, never deleted."""
    session.add(
        AuditLog(kind=kind, ticker=ticker, actor=actor, payload=_jsonable(payload or {}))
    )


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce Decimals to strings so JSONB never stores a lossy float."""
    return json.loads(json.dumps(payload, default=str))


async def publish(proposal: ProposedTrade, *, event: str) -> None:
    """Push to the dashboard. Best-effort — Redis being down must not block
    a trade decision from being recorded."""
    try:
        await get_redis().publish(
            CH_PROPOSALS,
            json.dumps({"event": event, "proposal": proposal_view(proposal)}),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not publish proposal %s: %s", proposal.id, exc)

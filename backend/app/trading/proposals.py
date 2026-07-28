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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.core.redis import CH_PROPOSALS, get_redis
from app.db.models import (
    AuditLog,
    Market,
    ProposalLeg,
    ProposalStatus,
    ProposedTrade,
    Side,
)
from app.trading.direction import book_side, to_yes_price
from app.trading.pricing import TicketQuote, price_ticket

log = get_logger(__name__)

__all__ = [
    "ProposalError",
    "create_proposal",
    "create_multi_leg_proposal",
    "legs_of",
    "legs_for",
    "leg_view",
    "quote_for",
    "expire_stale",
    "reject",
    "proposal_view",
    "publish",
    "audit",
]

#: Statuses that still need a human decision.
OPEN_STATUSES = (ProposalStatus.PENDING,)


#: Arbitrary fixed key for the proposal-creation advisory lock. Any constant
#: works; it only has to be the same in every process.
_CREATE_LOCK_KEY: Final = 0x6B63_5052_4F50  # "kcPROP"


async def _lock_proposal_creation(session: AsyncSession) -> None:
    """Serialise proposal creation for the rest of this transaction.

    Every creation-time guard below is ``SELECT count(...)`` followed by an
    ``INSERT``, which is only a guard if nothing else inserts in between.
    Nothing stopped that: 24 concurrent requests produced **21** proposals
    against a cap of 10, and the duplicate guard has the same shape. The
    per-market size guard reads the same aggregate and was equally exposed.

    A transaction-scoped advisory lock fixes all three at once, and is
    cheaper than getting three separate conditional-insert queries right.
    Creation is a rare event — a detector scan yields a handful — so
    serialising it costs nothing that matters.

    Skipped on non-PostgreSQL backends, and on the test doubles this module is
    exercised with, which have no bind at all. The races it prevents need real
    concurrency against a real database, so skipping there loses no coverage —
    and a lock that raised on a fake session would take the whole creation path
    down with it.
    """
    try:
        bind = session.get_bind()
        dialect = getattr(bind, "dialect", None)
    except Exception:  # noqa: BLE001 - a session double with no bind
        return
    if getattr(dialect, "name", None) != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CREATE_LOCK_KEY}
    )


async def _guard_queue_depth(session: AsyncSession, config: Config) -> None:
    """Refuse to add to a queue nobody can read.

    The whole safety model is a human evaluating each proposal. A detector
    that scans every 20 seconds can produce dozens a minute — observed: 20
    per scan across 22 watched events — and a queue that long is not reviewed,
    it is rubber-stamped. Capping it protects the one control that matters.
    """
    pending = (
        await session.execute(
            select(func.count())
            .select_from(ProposedTrade)
            .where(ProposedTrade.status == ProposalStatus.PENDING)
        )
    ).scalar_one()

    if pending >= config.risk.max_pending_proposals:
        raise ProposalError(
            "queue_full",
            f"{pending} proposals already await a decision "
            f"(risk.max_pending_proposals = "
            f"{config.risk.max_pending_proposals}). Decide on those first — a "
            f"queue longer than you will actually read is not a safety "
            f"mechanism.",
        )


async def _guard_duplicate(
    session: AsyncSession, *, source: str, key: str
) -> None:
    """Refuse to re-propose something already awaiting a decision.

    A detector re-derives the same opportunity on every scan. Without this the
    queue fills with the same handful of events over and over, which is both
    noise and a way to push a real proposal off the screen.
    """
    existing = (
        await session.execute(
            select(func.count())
            .select_from(ProposedTrade)
            .where(
                ProposedTrade.status == ProposalStatus.PENDING,
                ProposedTrade.source == source,
                (ProposedTrade.event_ticker == key)
                | (ProposedTrade.ticker == key),
            )
        )
    ).scalar_one()

    if existing:
        raise ProposalError(
            "already_pending",
            f"{source} already has a pending proposal for {key}.",
        )


async def _guard_halted(session: AsyncSession, config: Config) -> None:
    """Refuse to propose while the book is halted.

    The enforcement that matters is in the executor — nothing created here can
    reach an exchange without passing that check again. This exists so a
    halted system stops *producing* proposals rather than accumulating a
    backlog it will refuse one at a time, which would train the operator to
    click through refusals during exactly the drawdown the halt was called
    for.
    """
    from app.settings import get_settings
    from app.trading import risk

    state = await risk.halt_state(session, config, get_settings())
    if state is None:
        return
    try:
        risk.check_halted(state, config)
    except risk.RiskError as exc:
        raise ProposalError(exc.code, str(exc)) from exc


async def _guard_market_size(
    session: AsyncSession,
    max_loss_cents: Decimal | None,
    config: Config,
    *,
    what: str,
    tickers: Sequence[str],
    event_ticker: str | None = None,
) -> float:
    """Refuse a proposal that would push one market past its share of bankroll.

    Returns the fraction risked, *including* what is already committed to the
    same market. ``max_pct_per_market`` was displayed on the approval card but
    never enforced; that was tolerable while only a human could create
    proposals and is not now that detectors can.

    This used to divide **one proposal's** ``max_loss_cents`` by the bankroll
    and issue no query, so it measured the proposal rather than the market.
    The per-proposal boundary was exact — which is exactly why it looked like
    it worked — while eight individually compliant proposals on one ticker
    reached 39.79% of bankroll against a 5% cap, each reporting itself
    compliant. A cap that only ever sees one request at a time is not a cap.
    """
    bankroll_cents = Decimal(str(config.risk.bankroll_usd)) * Decimal(100)
    if bankroll_cents <= 0 or max_loss_cents is None:
        return 0.0

    from app.settings import get_settings
    from app.trading import risk

    route = risk.active_route(get_settings(), config)
    already = Decimal(0)
    if route is not None:
        already = await risk.market_exposure_cents(
            session,
            route=route,
            tickers=tickers,
            event_ticker=event_ticker,
        )

    projected = already + max_loss_cents
    fraction = float(projected / bankroll_cents)
    if fraction > config.risk.max_pct_per_market:
        raise ProposalError(
            "exceeds_market_limit",
            f"{what} would reach {fraction:.2%} of bankroll "
            f"({already / Decimal(100):.2f} USD already committed to it, "
            f"this proposal risks {max_loss_cents / Decimal(100):.2f} USD); "
            f"risk.max_pct_per_market is "
            f"{config.risk.max_pct_per_market:.2%}.",
        )
    return fraction


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
            "stored market data.",
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
        UnverifiedFeeSchedule: the fee table has never been checked against
            the official PDF, so no cost can be trusted.
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

    await _lock_proposal_creation(session)
    await _guard_halted(session, config)
    await _guard_queue_depth(session, config)
    if source != "manual":
        # A detector re-derives the same opportunity every scan, so it needs
        # the same one-live-proposal-per-market rule the multi-leg path has.
        # This was never called from here, which did not matter while no
        # single-leg detector could propose and matters now that they can.
        #
        # Manual tickets are exempt on purpose: a human proposing twice on one
        # market is making a second decision, not repeating a scan.
        await _guard_duplicate(session, source=source, key=ticker)
    pct = await _guard_market_size(
        session, quote.max_loss_cents, config, what=ticker, tickers=[ticker]
    )

    ttl = ttl_sec if ttl_sec is not None else config.trading.default_proposal_ttl_sec
    now = datetime.now(UTC)

    proposal = ProposedTrade(
        signal_id=signal_id,
        source=source,
        ticker=ticker,
        leg_count=1,
        net_edge_cents=quote.net_edge_cents,
        est_fee_cents=quote.est_fee_cents,
        max_loss_cents=quote.max_loss_cents,
        pct_of_bankroll=pct,
        rationale=rationale,
        status=ProposalStatus.PENDING,
        expires_at=now + timedelta(seconds=ttl),
    )
    session.add(proposal)
    await session.flush()

    session.add(
        ProposalLeg(
            proposal_id=proposal.id,
            seq=0,
            ticker=ticker,
            side=Side(side),
            action=action,
            limit_price=quote.limit_price,
            contracts=quote.contracts,
            fair_price=quote.fair_price,
            est_fee_cents=quote.est_fee_cents,
        )
    )
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

    The status change is a single conditional UPDATE rather than a read
    followed by writes. It has two concurrent callers — the worker's sweep
    and ``GET /api/proposals``, which expires on read — and as a
    read-modify-write both would select the same rows and both write an audit
    entry for the same expiry. Measured: 214 ``proposal.expired`` rows for
    182 distinct proposals, 15% of them redundant, 31 of 32 duplicates landing
    21-25ms apart. ``RETURNING`` makes the database decide who won, so only
    the transaction that actually changed the row logs it.
    """
    now = now or datetime.now(UTC)

    expired = (
        await session.execute(
            update(ProposedTrade)
            .where(
                ProposedTrade.status == ProposalStatus.PENDING,
                ProposedTrade.expires_at.isnot(None),
                ProposedTrade.expires_at <= now,
            )
            .values(
                status=ProposalStatus.EXPIRED,
                decided_at=now,
                decision_reason="ttl elapsed without a decision",
            )
            .returning(ProposedTrade.id)
            # The ORM must not serve these rows from its identity map; the
            # UPDATE is what decided them.
            .execution_options(synchronize_session=False)
        )
    ).scalars().all()

    if not expired:
        return 0

    # Re-read the rows this transaction actually won, so the published payload
    # keeps the same shape the dashboard already expects.
    rows = (
        await session.execute(
            select(ProposedTrade).where(ProposedTrade.id.in_(expired))
        )
    ).scalars().all()

    for proposal in rows:
        await audit(
            session,
            kind="proposal.expired",
            ticker=proposal.ticker,
            actor="system",
            payload={"proposal_id": proposal.id},
        )
        await publish(proposal, event="expired")

    log.info("expired %d proposal(s) past their ttl", len(expired))
    return len(expired)


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
    # Lock before reading the status, for the same reason the executor does:
    # two concurrent rejects both saw PENDING, both wrote a
    # ``proposal.rejected`` audit row 0.5ms apart with different reasons, and
    # `decision_reason` ended up last-writer-wins. Nothing is placed by a
    # reject, so this costs nothing but the audit trail should say once what
    # happened once.
    if proposal.id is not None:
        locked = (
            await session.execute(
                select(ProposedTrade)
                .where(ProposedTrade.id == proposal.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars().one_or_none()
        if locked is not None:
            proposal = locked

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


def proposal_view(
    proposal: ProposedTrade, legs: list[ProposalLeg] | None = None
) -> dict[str, Any]:
    """Serialise for the API. Money is a string; the browser must not compute.

    ``legs`` is where the tradeable detail lives. A manual ticket has one; a
    set arbitrage has one per market and they are approved together.
    """
    now = datetime.now(UTC)
    expires_in = None
    if proposal.expires_at is not None:
        expires_in = round((proposal.expires_at - now).total_seconds(), 1)

    return {
        "id": proposal.id,
        "signal_id": proposal.signal_id,
        "source": proposal.source,
        "ticker": proposal.ticker,
        "event_ticker": proposal.event_ticker,
        "leg_count": proposal.leg_count,
        "legs": [leg_view(leg) for leg in (legs or [])],
        # Always net of fees and slippage. There is no gross figure to show.
        "net_edge_cents": (
            None if proposal.net_edge_cents is None else str(proposal.net_edge_cents)
        ),
        "est_fee_cents": (
            None if proposal.est_fee_cents is None
            else str(proposal.est_fee_cents)
        ),
        "max_loss_cents": (
            None if proposal.max_loss_cents is None
            else str(proposal.max_loss_cents)
        ),
        # Serialised as a string like every other number crossing this
        # boundary. As a JSON float it came out in scientific notation
        # (`5.4e-6`) — the one value in the payload that did not follow the
        # convention, and the one shape a naive formatter renders verbatim.
        "pct_of_bankroll": (
            None
            if proposal.pct_of_bankroll is None
            else format(Decimal(str(proposal.pct_of_bankroll)), "f")
        ),
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


def leg_view(leg: ProposalLeg) -> dict[str, Any]:
    """Serialise a leg, including the form it will take on the wire.

    ``book_side`` and ``wire_price`` are what the exchange will actually
    receive: "buy NO at 30c" goes out as an **ask at 0.70**. The approval card
    shows both so a human can check the translation before committing, and
    CLAUDE.md is blunt that this is the *only* guard against an inverted
    position — the fee formula ``P(1-P)`` is symmetric, so a flipped direction
    produces the same fee, the same notional and a plausible confirmation.

    Computed here by calling ``direction.py``, never by re-deriving the
    mapping. There is exactly one copy of that table in the backend and this
    is not a second one.
    """
    return {
        "seq": leg.seq,
        "ticker": leg.ticker,
        "side": leg.side.value,
        "action": leg.action,
        "limit_price": str(leg.limit_price),
        "contracts": str(leg.contracts),
        "fair_price": None if leg.fair_price is None else str(leg.fair_price),
        "est_fee_cents": (
            None if leg.est_fee_cents is None else str(leg.est_fee_cents)
        ),
        "book_side": book_side(leg.side, leg.action),
        "wire_price": str(to_yes_price(leg.side, leg.limit_price)),
    }


async def legs_for(
    session: AsyncSession, proposal_ids: Sequence[int]
) -> dict[int, list[ProposalLeg]]:
    """Legs for many proposals in one query, grouped by proposal.

    The queue endpoint is polled continuously and renders up to 500 rows, and
    calling :func:`legs_of` per row inside a comprehension issued one query
    each. Same rows, one round trip.
    """
    if not proposal_ids:
        return {}
    rows = (
        await session.execute(
            select(ProposalLeg)
            .where(ProposalLeg.proposal_id.in_(proposal_ids))
            .order_by(ProposalLeg.proposal_id, ProposalLeg.seq)
        )
    ).scalars().all()
    grouped: dict[int, list[ProposalLeg]] = {}
    for leg in rows:
        grouped.setdefault(leg.proposal_id, []).append(leg)
    return grouped


async def legs_of(session: AsyncSession, proposal_id: int) -> list[ProposalLeg]:
    return list(
        (
            await session.execute(
                select(ProposalLeg)
                .where(ProposalLeg.proposal_id == proposal_id)
                .order_by(ProposalLeg.seq)
            )
        ).scalars().all()
    )


async def create_multi_leg_proposal(
    session: AsyncSession,
    config: Config,
    *,
    event_ticker: str,
    legs: list[dict[str, Any]],
    source: str,
    net_edge_cents: Decimal | None = None,
    est_fee_cents: Decimal | None = None,
    #: Worst case for the whole set, in cents. Required to enforce the
    #: per-market bankroll limit; without it the guard cannot judge size.
    max_loss_cents: Decimal | None = None,
    rationale: str | None = None,
    ttl_sec: int | None = None,
    signal_id: int | None = None,
    actor: str = "system",
) -> ProposedTrade:
    """Create one proposal covering several markets, approved as a unit.

    This exists because a set arbitrage is a single decision that happens to
    need several orders. Splitting it into independent proposals would let a
    human approve three legs of five and end up with a directional position
    where they thought they had a hedge.

    It does **not** make execution atomic — nothing can, on this exchange.
    The legs go out in one batch with IOC and any imbalance is reported.
    """
    if len(legs) < 2:
        raise ProposalError(
            "not_multi_leg",
            "create_multi_leg_proposal needs at least two legs; use "
            "create_proposal for a single-market ticket.",
        )

    await _lock_proposal_creation(session)
    await _guard_halted(session, config)
    await _guard_queue_depth(session, config)
    await _guard_duplicate(session, source=source, key=event_ticker)
    pct = await _guard_market_size(
        session,
        max_loss_cents,
        config,
        what=event_ticker,
        # Every market the set touches, so the cap sees the whole footprint
        # rather than the event label alone.
        tickers=[str(leg["ticker"]) for leg in legs if leg.get("ticker")],
        event_ticker=event_ticker,
    )

    now = datetime.now(UTC)
    ttl = ttl_sec if ttl_sec is not None else config.trading.default_proposal_ttl_sec

    proposal = ProposedTrade(
        signal_id=signal_id,
        source=source,
        event_ticker=event_ticker,
        ticker=str(legs[0]["ticker"]),
        leg_count=len(legs),
        net_edge_cents=net_edge_cents,
        est_fee_cents=est_fee_cents,
        max_loss_cents=max_loss_cents,
        pct_of_bankroll=pct,
        rationale=rationale,
        status=ProposalStatus.PENDING,
        expires_at=now + timedelta(seconds=ttl),
    )
    session.add(proposal)
    await session.flush()

    for seq, leg in enumerate(legs):
        session.add(
            ProposalLeg(
                proposal_id=proposal.id,
                seq=seq,
                ticker=str(leg["ticker"]),
                side=Side(leg["side"]),
                action=str(leg.get("action", "buy")),
                limit_price=Decimal(str(leg["limit_price"])),
                contracts=Decimal(str(leg["contracts"])),
                fair_price=(
                    None if leg.get("fair_price") is None
                    else Decimal(str(leg["fair_price"]))
                ),
                est_fee_cents=(
                    None if leg.get("est_fee_cents") is None
                    else Decimal(str(leg["est_fee_cents"]))
                ),
            )
        )
    await session.flush()

    await audit(
        session,
        kind="proposal.created",
        ticker=proposal.ticker,
        actor=actor,
        payload={
            "proposal_id": proposal.id,
            "source": source,
            "event_ticker": event_ticker,
            "leg_count": len(legs),
            "net_edge_cents": None if net_edge_cents is None else str(net_edge_cents),
            "legs": legs,
        },
    )
    await publish(proposal, event="created")
    return proposal


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

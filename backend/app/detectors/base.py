"""Detector plumbing: what a detector is and how its output reaches a human.

A detector emits :class:`~app.db.models.Signal` rows. It does **not** create
proposals and it certainly does not place orders — that separation is the
architecture, not an accident of layering:

    detectors -> signals -> risk/sizing -> proposed_trades -> human -> orders

Signals are cheap and may be wrong. A proposal is a request for a decision and
carries an executable price. Keeping them distinct is what lets a detector be
enabled, watched, and judged on its report card before anything it says is
allowed to become a trade.

Every detector ships **disabled**. Turning one on is a deliberate act.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config, get_config
from app.core.logging import get_logger
from app.core.redis import CH_SIGNALS, get_redis
from app.db.models import Side, Signal

log = get_logger(__name__)

__all__ = [
    "Finding",
    "Detector",
    "record",
    "publish_signal",
    "propose_finding",
    "is_material_change",
]


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a detector noticed, already costed.

    ``net_edge_cents`` is mandatory and is always net of fees and slippage.
    There is no gross field, because a gross edge is not information — it is
    a number that looks like information.
    """

    detector: str
    ticker: str
    side: Side
    fair_price: Decimal
    net_edge_cents: Decimal
    confidence: float
    rationale: str
    size_hint: Decimal | None = None
    ttl_sec: int = 120
    #: Everything needed to audit the finding later, including the legs of a
    #: multi-market opportunity.
    evidence: dict[str, Any] = field(default_factory=dict)


class Detector(Protocol):
    """A detector scans and returns findings. It never writes orders."""

    name: str

    def enabled(self, config: Config) -> bool: ...

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]: ...


def is_material_change(
    previous: Decimal | None, current: Decimal, *, threshold_cents: Decimal
) -> bool:
    """Whether an edge has moved enough to be a new observation.

    The judgement this whole guard rests on. A detector re-derives the same
    opportunity on every scan, so without a threshold every signal is "new"
    and the table fills with duplicates; with too coarse a threshold an edge
    that grows from 1c to 8c is silently folded into the row that reported
    1c, and the operator never learns it moved.

    The comparison is inclusive, so ``threshold_cents=0`` is always satisfied
    and disables folding entirely — the second way to turn the guard off,
    alongside ``dedupe_window_sec: 0``.
    """
    if previous is None:
        return True
    return abs(current - previous) >= threshold_cents


async def record(session: AsyncSession, finding: Finding) -> Signal:
    """Persist a finding as a Signal and push it to the dashboard.

    Repeats are folded rather than appended. A detector that scans every 20
    seconds re-derives the same observation every time — the undervalued
    screener wrote 180 near-identical rows in nine passes — and a signal
    table nobody reads is the same failure as an approval queue nobody
    reads, one step earlier in the pipeline.

    A repeat is the *same* observation only while its edge has not moved
    materially. When it has, a new row is written, because an edge going from
    1c to 8c is news and hiding it inside a counter would lose the thing
    worth looking at.

    The returned Signal is the folded one when a repeat is detected, so a
    proposal created from it links to the observation that actually started —
    and proposal creation is unaffected either way, since its own duplicate
    guard is what governs the queue.
    """
    config = get_config()
    window = int(getattr(config.detectors, "dedupe_window_sec", 900))
    threshold = Decimal(
        str(getattr(config.detectors, "dedupe_edge_change_cents", 1.0))
    )

    if window > 0:
        since = datetime.now(UTC) - timedelta(seconds=window)
        previous = (
            await session.execute(
                select(Signal)
                .where(
                    Signal.detector == finding.detector,
                    Signal.ticker == finding.ticker,
                    Signal.side == finding.side,
                    Signal.last_seen_at >= since,
                )
                .order_by(desc(Signal.last_seen_at))
                .limit(1)
            )
        ).scalars().first()

        if previous is not None and not is_material_change(
            previous.net_edge_cents,
            finding.net_edge_cents,
            threshold_cents=threshold,
        ):
            previous.last_seen_at = datetime.now(UTC)
            previous.seen_count = (previous.seen_count or 1) + 1
            # Deliberately not re-published: the dashboard already shows this
            # observation, and a fan-out per scan is the same noise in a
            # different channel.
            return previous

    signal = Signal(
        detector=finding.detector,
        ticker=finding.ticker,
        side=finding.side,
        fair_price=finding.fair_price,
        net_edge_cents=finding.net_edge_cents,
        confidence=finding.confidence,
        size_hint=finding.size_hint,
        ttl_sec=finding.ttl_sec,
        rationale=finding.rationale,
        evidence=json.loads(json.dumps(finding.evidence, default=str)),
    )
    session.add(signal)
    await session.flush()
    await publish_signal(finding, signal.id)
    return signal


async def publish_signal(finding: Finding, signal_id: int | None = None) -> None:
    """Best-effort fan-out. Redis being down must not lose the signal row."""
    try:
        await get_redis().publish(
            CH_SIGNALS,
            json.dumps(
                {
                    "id": signal_id,
                    "detector": finding.detector,
                    "ticker": finding.ticker,
                    "side": finding.side.value,
                    "net_edge_cents": str(finding.net_edge_cents),
                    "confidence": finding.confidence,
                    "rationale": finding.rationale,
                    "evidence": finding.evidence,
                    "ts": datetime.now(UTC).isoformat(),
                },
                default=str,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not publish signal for %s: %s", finding.ticker, exc)


async def propose_finding(
    session: AsyncSession,
    config: Config,
    finding: Finding,
    signal: Signal,
) -> Any:
    """Turn a multi-leg finding into one proposal the operator decides once.

    Only findings that carry ``evidence["legs"]`` become proposals here — a
    set arbitrage is several orders but one decision, and splitting it into
    independent proposals would let three legs of five be approved, leaving a
    directional position where the operator thought they had a hedge.

    Still nothing automatic: this creates a *pending* proposal. It reaches an
    exchange only after a human approves it.
    """
    from app.trading.proposals import create_multi_leg_proposal

    legs = finding.evidence.get("legs") or []
    if len(legs) < 2:
        return None

    direction = finding.evidence.get("direction", "sell")

    # Worst case for the whole set, so the per-market bankroll guard has a
    # number to judge. Selling a set collects `gross_sum` per set and pays out
    # at most $1, so the exposure is what is still owed if it goes against us.
    contracts = Decimal(str(finding.evidence.get("contracts", 0) or 0))
    gross = Decimal(str(finding.evidence.get("gross_sum", 0) or 0))
    fees = Decimal(str(finding.evidence.get("total_fee_cents", 0) or 0))
    shortfall = (Decimal(1) - gross) if direction == "sell" else gross
    max_loss_cents = max(Decimal(0), shortfall) * contracts * Decimal(100) + fees

    return await create_multi_leg_proposal(
        session,
        config,
        event_ticker=str(finding.evidence.get("event_ticker") or ""),
        legs=[
            {
                "ticker": leg["ticker"],
                # The set is priced in YES terms on both sides; selling the
                # set is selling YES on every leg.
                "side": "yes",
                "action": "sell" if direction == "sell" else "buy",
                "limit_price": leg["avg_price"],
                "contracts": leg["contracts"],
                "est_fee_cents": leg.get("fee_cents"),
            }
            for leg in legs
        ],
        source=finding.detector,
        net_edge_cents=finding.net_edge_cents,
        est_fee_cents=(
            Decimal(str(finding.evidence["total_fee_cents"]))
            if "total_fee_cents" in finding.evidence
            else None
        ),
        max_loss_cents=max_loss_cents,
        rationale=finding.rationale,
        ttl_sec=finding.ttl_sec,
        signal_id=signal.id,
        actor=finding.detector,
    )

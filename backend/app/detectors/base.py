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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.core.redis import CH_SIGNALS, get_redis
from app.db.models import Side, Signal

log = get_logger(__name__)

__all__ = ["Finding", "Detector", "record", "publish_signal", "propose_finding"]


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


async def record(session: AsyncSession, finding: Finding) -> Signal:
    """Persist a finding as a Signal and push it to the dashboard."""
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
        rationale=finding.rationale,
        ttl_sec=finding.ttl_sec,
        signal_id=signal.id,
        actor=finding.detector,
    )

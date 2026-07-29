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
    "enabled_detector_names",
    "record",
    "publish_signal",
    "propose_finding",
    "is_material_change",
]


def enabled_detector_names(config: Config) -> list[str]:
    """Every detector currently enabled, including those configured elsewhere.

    ``DetectorsConfig.enabled_names()`` walks the ``detectors:`` block and can
    only recognise ``DetectorConfig`` fields, so the weather engine — switched
    on at ``weather.enabled``, a ``WeatherConfig`` one level up — never
    appeared in the worker's boot log or in ``/api/system``'s
    ``enabled_detectors``. It scanned, and could signal, while the dashboard
    said no detectors were enabled.

    That is the same failure the ``leaderboard_watcher`` stub exists to
    prevent, pointed the other way: there, a detector missing from the registry
    looks like one that runs and finds nothing; here, a detector missing from
    the *status* looks like one that is switched off.
    """
    names = list(config.detectors.enabled_names())
    if config.weather.enabled and "weather" not in names:
        names.append("weather")
    return names


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


#: Detectors already told about the single-leg gap. The per-finding refusal
#: below is counted every scan; this narrates the *reason* once per process.
_SINGLE_LEG_WARNED: set[str] = set()


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

    This creates a *pending* proposal and nothing more. It reaches an exchange
    only after a human approves it — or after the autonomy gate does, which is
    a much larger set of conditions and not a shortcut past any of these.
    Either way, this function's output is a queue entry, never an order.

    A **single-leg** finding goes to :func:`create_proposal` instead, through
    exactly the same guards (halted, queue depth, duplicate, per-market size)
    and the same pending queue. It used to return ``None`` silently, so an
    enabled stale-quote detector that had found an edge, sized it with Kelly
    and named its binding cap looked from the operator's side exactly like one
    that had found nothing — five of the six detectors could never reach the
    queue, while the README described the proposals they would produce.

    A single-leg proposal is not a weaker decision than a multi-leg one; the
    reason set arbitrage must stay one proposal is that its legs *hedge each
    other*, which is a property of that strategy, not of proposals.
    """
    from app.trading.proposals import (
        ProposalError,
        create_multi_leg_proposal,
        create_proposal,
    )

    legs = finding.evidence.get("legs") or []
    if len(legs) < 2:
        price = finding.evidence.get("price")
        if price is None or not finding.size_hint:
            # Nothing to write a ticket from. Still not silent: a finding that
            # cannot be costed is a detector bug, not an absence of edge.
            if finding.detector not in _SINGLE_LEG_WARNED:
                _SINGLE_LEG_WARNED.add(finding.detector)
                log.warning(
                    "%s produced a finding with no executable price or size "
                    "(price=%r, size_hint=%r); recorded as a signal only.",
                    finding.detector, price, finding.size_hint,
                )
            raise ProposalError(
                "not_costable",
                f"{finding.detector} found an opportunity on {finding.ticker} "
                f"with no executable price or size, so no ticket can be "
                f"written; recorded as a signal only.",
            )

        proposal, _quote = await create_proposal(
            session,
            config,
            ticker=finding.ticker,
            side=finding.side,
            # These detectors buy the side their model says is right. A
            # detector that means to sell says so in its evidence.
            action=str(finding.evidence.get("action") or "buy"),
            limit_price=str(price),
            contracts=str(finding.size_hint),
            source=finding.detector,
            fair_price=finding.fair_price,
            rationale=finding.rationale,
            signal_id=signal.id,
            actor="system",
        )
        return proposal

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

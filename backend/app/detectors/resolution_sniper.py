"""Resolution sniper: markets whose event is over but which still trade.

Kalshi does not settle the instant a market closes. Between close and
settlement a contract can keep trading, and one sitting at 97c when the
outcome is already known is a few cents of settlement lag.

**The dangerous version of this detector is the one that treats price as
truth.** A market at 97c is not "decided" — it is a market where the crowd
thinks there is a 3% chance of being wrong, and buying it is a 33:1 bet, not
an arbitrage. Being right 97 times and wrong 3 makes no money at all after
fees; being wrong slightly more often loses steadily. That is the shape of
every blown-up longshot book.

So this detector separates two things the config conflates:

1. **Structural lag** — the market's close time has passed and it is still
   active. That is an observable fact and it is what makes settlement lag
   *possible*.
2. **Knowing the outcome** — which requires a settlement source, not a
   price. Nothing here has one; `settlement_sources` on the series names
   where a human would look, and reading it is M7/M8 work.

Without (2) the detector emits **research signals only, at low confidence,
and never a proposal**. It says "this market is past close and pinned",
which is a real thing to look at, and refuses to pretend that is an edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

__all__ = ["LagCandidate", "assess"]

HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class LagCandidate:
    """A market that is past close, still active, and priced at an extreme."""

    ticker: str
    #: Side the price is pinned towards.
    side: str
    #: The executable price on that side, in dollars.
    price: Decimal
    minutes_past_close: float
    #: Deliberately low, and never raised by price alone. Price is the
    #: crowd's opinion, not a settlement source.
    confidence: float
    #: True only when an independent settlement source confirmed the outcome.
    #: Always False today — nothing wires one in yet.
    source_confirmed: bool = False

    @property
    def actionable(self) -> bool:
        """Whether this may become a proposal rather than a research note.

        False until a settlement source confirms the outcome. Price alone
        never makes it True, which is the entire point of the split.
        """
        return self.source_confirmed


def assess(
    *,
    ticker: str,
    yes_bid: Decimal | None,
    yes_ask: Decimal | None,
    close_time: datetime | None,
    yes_threshold_cents: Decimal,
    no_threshold_cents: Decimal,
    now: datetime | None = None,
) -> LagCandidate | None:
    """Flag a market that is past close and pinned at an extreme.

    Returns ``None`` unless *both* hold: the close time has passed, and the
    executable price is beyond the configured threshold. The threshold is on
    the price you would actually pay — the ask when buying YES — not the mid,
    because a market quoted 96/99 is not a 97c opportunity.
    """
    if close_time is None:
        return None
    now = now or datetime.now(UTC)
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=UTC)
    if close_time > now:
        return None

    minutes_past = (now - close_time).total_seconds() / 60.0

    # Buying YES costs the ask; buying NO costs 1 - bid.
    if yes_ask is not None and 0 < yes_ask < 1:
        yes_cents = yes_ask * HUNDRED
        if yes_cents >= yes_threshold_cents:
            return LagCandidate(
                ticker=ticker,
                side="yes",
                price=yes_ask,
                minutes_past_close=minutes_past,
                confidence=_confidence(minutes_past),
            )

    if yes_bid is not None and 0 < yes_bid < 1:
        yes_cents = yes_bid * HUNDRED
        if yes_cents <= no_threshold_cents:
            return LagCandidate(
                ticker=ticker,
                side="no",
                price=Decimal(1) - yes_bid,
                minutes_past_close=minutes_past,
                confidence=_confidence(minutes_past),
            )

    return None


def _confidence(minutes_past_close: float) -> float:
    """Confidence in a lag candidate, capped low on purpose.

    Time past close is weak evidence that settlement is imminent, and no
    evidence at all about *which way*. The cap exists so that no amount of
    waiting can make a price-only observation look like knowledge.
    """
    if minutes_past_close <= 0:
        return 0.0
    return min(0.35, 0.10 + minutes_past_close / 600.0)

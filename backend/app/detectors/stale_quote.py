"""Stale-quote detection: a strike the spot price has already decided.

Kalshi's crypto markets are strikes on a reference price — ``greater`` with a
``floor_strike``, ``less`` with a ``cap_strike``, ``between`` with both. When
spot is far enough past the strike with little time left, the outcome is
effectively determined and the contract should trade near $1 or $0. A quote
that has not caught up is a stale quote.

**This is a heuristic, not a proof, and the module is written to keep that
visible.** "Far enough" is a fixed percentage from config, not a probability
from a volatility model — the vol model is M6. Bitcoin can move several
percent in minutes, so a margin that looks decisive over one horizon is not
over another. Two guards follow from that:

- both a **minimum margin** and a **maximum time to close** must hold, and
  the fair value is capped strictly below certainty, so the edge is never
  computed as if the outcome were already settled;
- confidence scales with margin, and the detector reports the margin and the
  time remaining in its evidence so a human can disagree with it.

The reference price must also be **fresh**. A stale spot quote compared
against a live market invents an edge in whichever direction the market has
already moved, which is precisely the failure this detector is supposed to
catch, pointed backwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.core.money import parse_dollars

__all__ = ["StrikeVerdict", "resolve_strike", "decisive_fair_price"]

ONE = Decimal(1)
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class StrikeVerdict:
    """Where spot sits relative to a strike, and by how much."""

    #: True when the strike condition currently holds.
    yes: bool
    #: Distance from the nearest boundary, as a fraction of spot. Zero when
    #: spot sits exactly on a boundary.
    margin_pct: Decimal


def resolve_strike(
    *,
    strike_type: str | None,
    floor_strike: Decimal | None,
    cap_strike: Decimal | None,
    spot: Decimal,
) -> StrikeVerdict | None:
    """Evaluate a strike against spot.

    Returns ``None`` for strike types this cannot evaluate — ``custom`` above
    all, which carries its own rules text and must never be guessed at. An
    unrecognised strike type is not an invitation to assume ``greater``.
    """
    if spot <= 0:
        return None
    kind = (strike_type or "").strip().lower()

    def pct(boundary: Decimal) -> Decimal:
        return abs(spot - boundary) / spot * HUNDRED

    if kind in ("greater", "greater_or_equal"):
        if floor_strike is None:
            return None
        holds = spot > floor_strike if kind == "greater" else spot >= floor_strike
        return StrikeVerdict(yes=holds, margin_pct=pct(floor_strike))

    if kind in ("less", "less_or_equal"):
        boundary = cap_strike if cap_strike is not None else floor_strike
        if boundary is None:
            return None
        holds = spot < boundary if kind == "less" else spot <= boundary
        return StrikeVerdict(yes=holds, margin_pct=pct(boundary))

    if kind == "between":
        if floor_strike is None or cap_strike is None:
            return None
        holds = floor_strike <= spot <= cap_strike
        # Inside the band the binding boundary is the nearer one; outside it
        # is the one that was breached.
        return StrikeVerdict(
            yes=holds, margin_pct=min(pct(floor_strike), pct(cap_strike))
        )

    # `custom` and anything unrecognised: the rules live in prose, so there is
    # nothing safe to compute here.
    return None


def decisive_fair_price(
    verdict: StrikeVerdict,
    *,
    min_margin_pct: Decimal,
    max_fair: Decimal = Decimal("0.98"),
) -> Decimal | None:
    """Fair value implied by a strike the spot has decisively cleared.

    ``None`` when the margin is too thin to call. Otherwise the fair value is
    bounded by ``max_fair`` rather than pinned at 1.00: the outcome is *very
    likely*, not settled, and pricing it as settled would manufacture the last
    couple of cents of edge out of nothing.
    """
    if verdict.margin_pct < min_margin_pct:
        return None
    return max_fair if verdict.yes else ONE - max_fair


def reference_is_fresh(
    observed_at: datetime | None, *, max_age_sec: float, now: datetime | None = None
) -> bool:
    """Whether a spot observation is recent enough to price against.

    A stale reference compared to a live market invents an edge in whichever
    direction the market has already moved — the same failure this detector
    hunts, pointed the wrong way.
    """
    if observed_at is None:
        return False
    now = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return (now - observed_at).total_seconds() <= max_age_sec


def parse_spot(value: object) -> Decimal:
    """Parse an external spot quote, refusing anything unusable."""
    price = parse_dollars(value, "spot")
    if price <= 0:
        raise ValueError(f"spot must be positive, got {value!r}")
    return price

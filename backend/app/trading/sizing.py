"""Position sizing — how many contracts, given an edge.

Until now every detector shipped a fixed ``size_hint``: ten contracts whether
the edge was one cent or thirty, whether the book was two deep or two hundred.
That is not a sizing rule, it is a placeholder, and it fails in both
directions — too large on a marginal edge, too small on a good one.

The Kelly criterion on a binary contract
----------------------------------------

A Kalshi contract pays exactly $1 if the thesis is right and $0 if it is
wrong, so the general Kelly formula collapses to something unusually clean.
Staking cost ``c`` per contract to win ``1 - c``, at probability ``p``::

    f* = (p - c) / (1 - c)

``f*`` is the fraction of bankroll to put at risk. Two things fall out of it
that are worth stating, because both are counterintuitive under a fixed size:

- **The denominator matters as much as the numerator.** A 5c edge on a 10c
  contract and a 5c edge on a 90c contract are not the same bet. The second
  risks nine times as much to win the same amount, and Kelly sizes it far
  smaller.
- **``c`` must be the price after fees.** Kelly is exquisitely sensitive
  near the breakeven point: a gross edge of 2c on a market charging 1.7c in
  fees is a real edge of 0.3c, and sizing off the gross number would stake
  roughly seven times too much. This is the "a gross edge is a lie" rule in
  its most expensive form.

Fractional Kelly
----------------

Full Kelly is optimal only if ``p`` is exactly right, and nothing in this
system knows ``p`` exactly — the stale-quote detector's fair value is a
fixed-margin heuristic, not a probability from a model. Kelly's downside when
``p`` is overestimated is severe and asymmetric: betting 2x the Kelly
fraction has **zero** expected long-run growth however good the edge was.
``risk.kelly_fraction`` (default 0.25) is the discount for that, and it is a
multiplier on the stake, not on the probability.

Everything here is a **ceiling**, never a floor. The three caps below can only
reduce the size, and a size that rounds to nothing returns zero contracts
rather than a minimum trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.config import Config
from app.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "SizeRecommendation",
    "full_kelly_fraction",
    "recommend_size",
]

HUNDRED = Decimal(100)
ONE = Decimal(1)
#: Contracts are fractional to 0.01 — the same increment the API quotes them
#: in. Sizes are rounded *down* to it, so a cap is never exceeded by rounding.
CONTRACT_TICK = Decimal("0.01")


def full_kelly_fraction(
    *, fair_price: Decimal, cost_per_contract: Decimal
) -> Decimal:
    """Full-Kelly fraction of bankroll to stake, before any discount.

    Args:
        fair_price: Probability the traded side wins, as a dollar price in
            ``(0, 1)``. Already oriented to the side being bought — for a NO
            ticket this is the probability NO resolves, not YES.
        cost_per_contract: All-in cost in dollars, **fees included**.

    Returns ``0`` for any bet that is not worth making: no edge, or a cost at
    or above $1. A contract costing a dollar or more cannot profit — it pays
    at most a dollar — and the formula's denominator goes to zero or negative
    there, which would otherwise produce a confidently enormous size from an
    impossible trade.
    """
    if cost_per_contract >= ONE or cost_per_contract <= 0:
        return Decimal(0)
    if fair_price <= cost_per_contract:
        return Decimal(0)
    return (fair_price - cost_per_contract) / (ONE - cost_per_contract)


@dataclass(frozen=True, slots=True)
class SizeRecommendation:
    """How large to go, and which limit decided it.

    ``binding_constraint`` exists so the approval card can say *why* a size is
    what it is. "8 contracts" tells the operator nothing; "8 contracts, capped
    by available depth" tells them the edge was bigger than the book, which is
    a different trade to consider.
    """

    contracts: Decimal
    stake_cents: Decimal
    #: Undiscounted f*, for display. Seeing 0.42 next to a staked 0.10 is the
    #: clearest available reminder that fractional Kelly is doing work.
    kelly_fraction: Decimal
    #: After the ``risk.kelly_fraction`` discount.
    scaled_fraction: Decimal
    #: kelly | market_cap | exposure | depth | none
    binding_constraint: str

    @property
    def is_tradeable(self) -> bool:
        return self.contracts >= CONTRACT_TICK


def recommend_size(
    *,
    fair_price: Decimal,
    cost_per_contract: Decimal,
    config: Config,
    available_contracts: Decimal | None = None,
    exposure_headroom_cents: Decimal | None = None,
) -> SizeRecommendation:
    """Size a ticket from its edge, then apply every cap that binds.

    Args:
        fair_price: Probability the traded side wins, oriented to that side.
        cost_per_contract: All-in dollar cost per contract, fees included.
        available_contracts: Depth actually executable at ``cost_per_contract``.
            Sizing past this is sizing into a fill that will not happen at the
            price the edge was computed from.
        exposure_headroom_cents: What the portfolio limit still allows. ``None``
            skips the check — used when the caller has already applied it.

    Returns a recommendation whose ``contracts`` may be zero, which is a real
    answer and the common one: most edges do not survive fees.
    """
    kelly = full_kelly_fraction(
        fair_price=fair_price, cost_per_contract=cost_per_contract
    )
    if kelly <= 0:
        return SizeRecommendation(
            contracts=Decimal(0),
            stake_cents=Decimal(0),
            kelly_fraction=Decimal(0),
            scaled_fraction=Decimal(0),
            binding_constraint="kelly",
        )

    scaled = kelly * Decimal(str(config.risk.kelly_fraction))
    bankroll_cents = Decimal(str(config.risk.bankroll_usd)) * HUNDRED

    # Each cap is expressed as a stake in cents so they compare directly, and
    # the smallest wins. Naming the winner is the point of the exercise.
    caps: list[tuple[str, Decimal]] = [("kelly", scaled * bankroll_cents)]

    caps.append(
        (
            "market_cap",
            bankroll_cents * Decimal(str(config.risk.max_pct_per_market)),
        )
    )

    if exposure_headroom_cents is not None:
        caps.append(("exposure", max(exposure_headroom_cents, Decimal(0))))

    cost_cents = cost_per_contract * HUNDRED
    if available_contracts is not None:
        caps.append(("depth", max(available_contracts, Decimal(0)) * cost_cents))

    binding, stake_cents = min(caps, key=lambda row: row[1])
    if stake_cents <= 0 or cost_cents <= 0:
        return SizeRecommendation(
            contracts=Decimal(0),
            stake_cents=Decimal(0),
            kelly_fraction=kelly,
            scaled_fraction=scaled,
            binding_constraint=binding,
        )

    # Down, never up: rounding a size up would step past whichever cap just
    # bound it, which is how a limit becomes advisory.
    contracts = (stake_cents / cost_cents).quantize(CONTRACT_TICK, rounding=ROUND_DOWN)
    if contracts < CONTRACT_TICK:
        return SizeRecommendation(
            contracts=Decimal(0),
            stake_cents=Decimal(0),
            kelly_fraction=kelly,
            scaled_fraction=scaled,
            binding_constraint=binding,
        )

    return SizeRecommendation(
        contracts=contracts,
        stake_cents=contracts * cost_cents,
        kelly_fraction=kelly,
        scaled_fraction=scaled,
        binding_constraint=binding,
    )

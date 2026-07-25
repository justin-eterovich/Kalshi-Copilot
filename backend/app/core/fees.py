"""Kalshi fee math.

Single source of truth for every cost figure in the system.  No detector,
sizing routine, or UI component may compute a fee itself — they all call in
here, so that "net edge" means the same thing everywhere.

The published taker formula is::

    fee = round_up_to_cent( M * 0.07 * C * P * (1 - P) )

where ``P`` is the contract price in dollars, ``C`` the contract count, and
``M`` a per-category multiplier.  Two details matter and are easy to get
wrong:

1. **The rounding is on the order aggregate, not per contract.**  One
   contract at 50c costs 2c (``ceil($0.0175)``); one hundred contracts at 50c
   cost exactly $1.75, not $2.00.
2. **Each fill is charged separately.**  An order that fills in three pieces
   rounds up three times.  :func:`taker_fee_cents` prices a single fill;
   callers modelling partial fills should sum per-fill costs.

All money is handled as :class:`~decimal.Decimal` and returned as whole
cents (``int``) so nothing drifts through float arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

__all__ = [
    "FeeSchedule",
    "UnverifiedFeeCategory",
    "load_fee_schedule",
    "taker_fee_cents",
    "maker_fee_cents",
    "round_trip_cost_cents",
    "net_edge_cents",
]

CENT: Final = Decimal("0.01")
DEFAULT_SCHEDULE_PATH: Final = Path("/app/data/fee_schedule.yaml")


class UnverifiedFeeCategory(RuntimeError):
    """Raised when a category's fee multiplier has not been verified.

    Fail-closed by design.  If we do not know a market's fee multiplier we
    cannot compute a trustworthy net edge, and an understated fee silently
    inflates every downstream EV number.  The market is excluded from
    proposals until ``scripts/refresh_fee_schedule.py`` fills the value in.
    """

    def __init__(self, category: str) -> None:
        super().__init__(
            f"Fee multiplier for category {category!r} is unverified. "
            f"Run `python scripts/refresh_fee_schedule.py` against the current "
            f"schedule PDF, then set it in data/fee_schedule.yaml. "
            f"Markets in this category are excluded from proposals until then."
        )
        self.category = category


def _round_up_cents(dollars: Decimal) -> int:
    """Round a dollar amount up to the next whole cent, returned as cents."""
    return int((dollars / CENT).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Immutable view of ``data/fee_schedule.yaml``."""

    base_taker_rate: Decimal
    maker_rate_fraction: Decimal
    category_multipliers: dict[str, Decimal | None]
    maker_free_categories: frozenset[str]
    verified_on: str | None
    schedule_revision: str | None

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FeeSchedule:
        formula = raw.get("formula") or {}
        meta = raw.get("meta") or {}
        categories = raw.get("categories") or {}

        if "default" not in categories:
            raise ValueError("fee_schedule.yaml: `categories` must define `default`")

        multipliers: dict[str, Decimal | None] = {}
        for name, value in categories.items():
            key = str(name).strip().lower()
            multipliers[key] = None if value is None else Decimal(str(value))

        if multipliers["default"] is None:
            raise ValueError(
                "fee_schedule.yaml: the `default` category multiplier cannot be "
                "null — the whole system would fail closed."
            )

        return cls(
            base_taker_rate=Decimal(str(formula.get("base_taker_rate", "0.07"))),
            maker_rate_fraction=Decimal(str(formula.get("maker_rate_fraction", "0.25"))),
            category_multipliers=multipliers,
            maker_free_categories=frozenset(
                str(c).strip().lower() for c in (raw.get("maker_free_categories") or [])
            ),
            verified_on=meta.get("verified_on"),
            schedule_revision=meta.get("schedule_revision"),
        )

    # -- lookups ---------------------------------------------------------

    @property
    def is_verified(self) -> bool:
        """True once the schedule has been confirmed against the official PDF."""
        return self.verified_on is not None

    def multiplier(self, category: str | None) -> Decimal:
        """Return the fee multiplier for ``category``.

        Unknown categories fall back to ``default``.  Categories explicitly
        present but set to ``null`` are treated as unverified and raise.
        """
        key = (category or "default").strip().lower()
        if key in self.category_multipliers:
            value = self.category_multipliers[key]
            if value is None:
                raise UnverifiedFeeCategory(key)
            return value
        default = self.category_multipliers["default"]
        assert default is not None  # validated in from_dict
        return default

    def taker_rate(self, category: str | None = None) -> Decimal:
        """Effective taker rate (multiplier applied) for ``category``."""
        return self.base_taker_rate * self.multiplier(category)

    def maker_rate(self, category: str | None = None) -> Decimal:
        """Effective maker rate for ``category``; zero where makers are free."""
        key = (category or "default").strip().lower()
        if key in self.maker_free_categories:
            return Decimal(0)
        return self.taker_rate(category) * self.maker_rate_fraction


@lru_cache(maxsize=4)
def load_fee_schedule(path: str | Path | None = None) -> FeeSchedule:
    """Load and cache the fee schedule from disk."""
    resolved = Path(path) if path is not None else DEFAULT_SCHEDULE_PATH
    if not resolved.exists():
        raise FileNotFoundError(
            f"Fee schedule not found at {resolved}. It is required: every edge "
            f"figure in the system is computed net of fees."
        )
    with resolved.open("r", encoding="utf-8") as fh:
        return FeeSchedule.from_dict(yaml.safe_load(fh) or {})


# ---------------------------------------------------------------------------
# Fee calculations
# ---------------------------------------------------------------------------


def _validate(price_cents: int | Decimal, contracts: int) -> tuple[Decimal, int]:
    price = Decimal(str(price_cents))
    if not (0 < price < 100):
        raise ValueError(
            f"price_cents must be strictly between 0 and 100, got {price_cents}"
        )
    if contracts < 0:
        raise ValueError(f"contracts must be non-negative, got {contracts}")
    return price, int(contracts)


def taker_fee_cents(
    price_cents: int | Decimal,
    contracts: int,
    category: str | None = None,
    schedule: FeeSchedule | None = None,
) -> int:
    """Taker fee, in whole cents, for a single fill.

    Args:
        price_cents: Execution price, 1-99.
        contracts: Number of contracts in this fill.
        category: Market category, used to pick the fee multiplier.
        schedule: Override schedule (tests); defaults to the loaded one.
    """
    price, qty = _validate(price_cents, contracts)
    if qty == 0:
        return 0
    sched = schedule or load_fee_schedule()

    p = price / Decimal(100)
    gross = sched.taker_rate(category) * Decimal(qty) * p * (Decimal(1) - p)
    return _round_up_cents(gross)


def maker_fee_cents(
    price_cents: int | Decimal,
    contracts: int,
    category: str | None = None,
    schedule: FeeSchedule | None = None,
) -> int:
    """Maker (resting order) fee in whole cents. Zero for maker-free categories."""
    price, qty = _validate(price_cents, contracts)
    if qty == 0:
        return 0
    sched = schedule or load_fee_schedule()

    rate = sched.maker_rate(category)
    if rate == 0:
        return 0

    p = price / Decimal(100)
    gross = rate * Decimal(qty) * p * (Decimal(1) - p)
    return _round_up_cents(gross)


def round_trip_cost_cents(
    entry_price_cents: int | Decimal,
    contracts: int,
    category: str | None = None,
    *,
    exit_price_cents: int | Decimal | None = None,
    entry_is_taker: bool = True,
    exit_is_taker: bool = True,
    schedule: FeeSchedule | None = None,
) -> int:
    """Total fees to open and close a position.

    A position held to settlement pays no exit fee — pass
    ``exit_price_cents=None`` for that case, which is the norm for the
    resolution sniper and set-arb detectors.
    """
    entry_fn = taker_fee_cents if entry_is_taker else maker_fee_cents
    total = entry_fn(entry_price_cents, contracts, category, schedule)

    if exit_price_cents is not None:
        exit_fn = taker_fee_cents if exit_is_taker else maker_fee_cents
        total += exit_fn(exit_price_cents, contracts, category, schedule)

    return total


def net_edge_cents(
    fair_price_cents: Decimal | float,
    executable_price_cents: int | Decimal,
    contracts: int,
    category: str | None = None,
    *,
    slippage_cents: Decimal | float = 0,
    is_taker: bool = True,
    schedule: FeeSchedule | None = None,
) -> Decimal:
    """Per-contract edge in cents, net of fees and slippage.

    This is *the* number the system is allowed to show.  Positive means the
    trade is expected to make money after costs; a gross edge without this
    correction is meaningless.

    Args:
        fair_price_cents: Model's fair value for the contract, 0-100.
        executable_price_cents: Price we would actually pay.
        contracts: Size, which matters because fees round up per fill.
        slippage_cents: Extra adverse fill assumed, per contract.

    Returns:
        Net edge per contract in cents. May be negative.
    """
    if contracts <= 0:
        return Decimal(0)

    fair = Decimal(str(fair_price_cents))
    price = Decimal(str(executable_price_cents))
    slip = Decimal(str(slippage_cents))

    fee_fn = taker_fee_cents if is_taker else maker_fee_cents
    fee_total = Decimal(fee_fn(price, contracts, category, schedule))

    gross_per_contract = fair - price
    fee_per_contract = fee_total / Decimal(contracts)

    return gross_per_contract - fee_per_contract - slip

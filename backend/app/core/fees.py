"""Kalshi fee math.

Single source of truth for every cost figure in the system.  No detector,
sizing routine, or UI component may compute a fee itself — they all call in
here, so that "net edge" means the same thing everywhere.

The published formulas are::

    taker = round_up( M * 0.07   * C * P * (1 - P) )
    maker = round_up( M * 0.0175 * C * P * (1 - P) )

where ``P`` is the contract price in dollars, ``C`` the contract count, and
``M`` a per-**series** multiplier.  Four details matter, and an earlier
version of this module got three of them wrong:

1. **Rounding is to a centicent** (``$0.0001``), not to a cent.  The schedule
   says the fee is rounded up "such that the fee + positionCost is rounded to
   a centicent".  A fee is therefore a fractional number of cents: one
   contract at 50c costs **1.75c**, not 2c.  Confirmed against the live demo
   exchange, which billed ``$0.022400`` for 2 contracts at 20c — cent
   rounding would have charged ``$0.03``.

2. **Rounding is on the order aggregate, not per contract.**  One hundred
   contracts at 50c cost exactly $1.75.

3. **Each fill is charged separately**, so an order that fills in three
   pieces rounds three times.  :func:`taker_fee_cents` prices a single fill;
   callers modelling partial fills sum per-fill costs.

4. **Multipliers are keyed by series ticker, not by category.**  The schedule
   has no category dimension.  It lists only *non-standard* series; anything
   absent takes the documented defaults of ``M=1`` for taker and — note —
   ``M=0`` for maker, which means **maker fees are not charged at all** on an
   unlisted series.

Units follow the API: **prices are Decimal dollars** (the wire format is a
fixed-point string with up to 6 decimals, so sub-cent prices are real), and
**contract counts are Decimal** because Kalshi supports fractional contracts
down to 0.01.  Fees come back as **Decimal cents**, which is what they are —
returning ``int`` here silently rounded every fee in the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from app.core.money import parse_count, parse_dollars

__all__ = [
    "FeeSchedule",
    "UnverifiedFeeSchedule",
    "UnknownSeries",
    "load_fee_schedule",
    "series_of",
    "taker_fee_cents",
    "maker_fee_cents",
    "round_trip_cost_cents",
    "net_edge_cents",
]

#: The unit fees round up to. Not a cent — see the module docstring.
CENTICENT: Final = Decimal("0.0001")
#: Where the schedule lives inside the container image. Used only when
#: settings are unavailable; :func:`load_fee_schedule` prefers the configured
#: path so a stack running outside Docker reads the same file.
DEFAULT_SCHEDULE_PATH: Final = Path("/app/data/fee_schedule.yaml")


def _default_schedule_path() -> Path:
    # Imported lazily: this module is the pure engine and must not take a
    # settings dependency at import time.
    try:
        from app.settings import get_settings

        return Path(get_settings().fee_schedule_path)
    except Exception:  # noqa: BLE001 - settings are optional for the engine
        return DEFAULT_SCHEDULE_PATH


class UnverifiedFeeSchedule(RuntimeError):
    """The schedule has never been checked against the official PDF.

    Fail-closed by design. Every edge figure in the system is computed net of
    fees, so an unverified schedule makes all of them untrustworthy.
    """

    def __init__(self) -> None:
        super().__init__(
            "data/fee_schedule.yaml has never been verified against the "
            "official PDF (meta.verified_on is null). Run "
            "`python scripts/refresh_fee_schedule.py`, fill in the series "
            "table, then `--mark-verified`. Proposals are refused until then."
        )


class UnknownSeries(RuntimeError):
    """A series is absent from a schedule that can no longer be defaulted.

    Normally an unlisted series is *not* an error: the schedule lists only
    non-standard series and documents a default of ``M=1``.  Because no listed
    multiplier currently exceeds that default, assuming it for an unlisted
    series can only ever **over**state a fee, which is the safe direction.

    If a future schedule introduces a multiplier above the default, that
    reasoning collapses — an unlisted series might be a premium one we have
    not seen — and this is raised instead of guessing.
    """

    def __init__(self, series: str | None) -> None:
        super().__init__(
            f"Series {series!r} is not in the fee schedule, and the schedule "
            f"now contains a multiplier above the default — so an unlisted "
            f"series can no longer be assumed standard without understating "
            f"its fee. Re-run scripts/refresh_fee_schedule.py."
        )
        self.series = series


def series_of(ticker: str | None) -> str | None:
    """Derive the series ticker from a market ticker.

    Kalshi market tickers are ``SERIES-EVENTSUFFIX-STRIKE``, e.g.
    ``KXFEDDECISION-26JUL-H25`` belongs to series ``KXFEDDECISION``. Fees are
    keyed by series, so this is how a market reaches its multiplier when the
    caller has only the market ticker to hand.
    """
    if not ticker:
        return None
    return ticker.split("-", 1)[0].strip().upper() or None


def _round_up_centicents(dollars: Decimal) -> Decimal:
    """Round a dollar fee up to the next centicent, returned as **cents**."""
    rounded = (dollars / CENTICENT).to_integral_value(rounding=ROUND_CEILING)
    return rounded * CENTICENT * Decimal(100)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Immutable view of ``data/fee_schedule.yaml``."""

    base_taker_rate: Decimal
    base_maker_rate: Decimal
    default_taker_multiplier: Decimal
    default_maker_multiplier: Decimal
    #: series ticker -> (maker multiplier, taker multiplier)
    series: dict[str, tuple[Decimal, Decimal]]
    verified_on: str | None
    schedule_revision: str | None

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FeeSchedule:
        formula = raw.get("formula") or {}
        meta = raw.get("meta") or {}
        defaults = raw.get("defaults") or {}

        table: dict[str, tuple[Decimal, Decimal]] = {}
        for name, value in (raw.get("series") or {}).items():
            key = str(name).strip().upper()
            if not isinstance(value, dict):
                raise ValueError(
                    f"fee_schedule.yaml: series {name!r} must be a mapping "
                    f"with `maker` and `taker` multipliers, got {value!r}"
                )
            table[key] = (
                Decimal(str(value.get("maker", 0))),
                Decimal(str(value.get("taker", 1))),
            )

        return cls(
            base_taker_rate=Decimal(str(formula.get("base_taker_rate", "0.07"))),
            base_maker_rate=Decimal(str(formula.get("base_maker_rate", "0.0175"))),
            default_taker_multiplier=Decimal(
                str(defaults.get("taker_multiplier", 1))
            ),
            default_maker_multiplier=Decimal(
                str(defaults.get("maker_multiplier", 0))
            ),
            series=table,
            verified_on=(
                None if meta.get("verified_on") is None
                else str(meta.get("verified_on"))
            ),
            schedule_revision=meta.get("schedule_revision"),
        )

    # -- lookups ---------------------------------------------------------

    @property
    def is_verified(self) -> bool:
        """True once the schedule has been confirmed against the official PDF."""
        return self.verified_on is not None

    @property
    def default_is_safe(self) -> bool:
        """True when assuming the default for an unlisted series cannot
        understate a fee — i.e. no listed multiplier exceeds the default."""
        if not self.series:
            return True
        return max(taker for _, taker in self.series.values()) <= (
            self.default_taker_multiplier
        )

    def _multipliers(self, series: str | None) -> tuple[Decimal, Decimal]:
        key = (series or "").strip().upper()
        if key in self.series:
            return self.series[key]
        if not self.default_is_safe:
            raise UnknownSeries(series)
        return self.default_maker_multiplier, self.default_taker_multiplier

    def taker_multiplier(self, series: str | None) -> Decimal:
        return self._multipliers(series)[1]

    def maker_multiplier(self, series: str | None) -> Decimal:
        return self._multipliers(series)[0]

    def taker_rate(self, series: str | None = None) -> Decimal:
        """Effective taker rate (multiplier applied) for ``series``."""
        return self.base_taker_rate * self.taker_multiplier(series)

    def maker_rate(self, series: str | None = None) -> Decimal:
        """Effective maker rate for ``series``.

        Zero for any series the schedule does not list, because the documented
        default maker multiplier is 0 — most markets charge no maker fee.
        """
        return self.base_maker_rate * self.maker_multiplier(series)


@lru_cache(maxsize=4)
def load_fee_schedule(path: str | Path | None = None) -> FeeSchedule:
    """Load and cache the fee schedule from disk.

    With no argument the path comes from settings, so callers that do not
    thread a schedule through — the paper fill simulator, ticket pricing —
    still read the same file the rest of the stack does.
    """
    resolved = Path(path) if path is not None else _default_schedule_path()
    if not resolved.exists():
        raise FileNotFoundError(
            f"Fee schedule not found at {resolved}. It is required: every edge "
            f"figure in the system is computed net of fees."
        )
    with resolved.open("r", encoding="utf-8") as fh:
        return FeeSchedule.from_dict(yaml.safe_load(fh) or {})


# ---------------------------------------------------------------------------
# Fee calculations
#
# Prices are Decimal DOLLARS (0 < P < 1). Counts are Decimal contracts and may
# be fractional. Fees come back as Decimal CENTS, to centicent precision.
# ---------------------------------------------------------------------------


def _validate(
    price_dollars: Decimal | str | int, contracts: Decimal | str | int
) -> tuple[Decimal, Decimal]:
    price = parse_dollars(price_dollars, "price_dollars")
    if not (Decimal(0) < price < Decimal(1)):
        raise ValueError(
            f"price_dollars must be strictly between 0 and 1, got {price_dollars!r}. "
            f"(Kalshi quotes are dollar strings like '0.5600', not cents.)"
        )

    qty = parse_count(contracts, "contracts")
    if qty < 0:
        raise ValueError(f"contracts must be non-negative, got {contracts!r}")

    return price, qty


def taker_fee_cents(
    price_dollars: Decimal | str | int,
    contracts: Decimal | str | int,
    series: str | None = None,
    schedule: FeeSchedule | None = None,
) -> Decimal:
    """Taker fee, in cents, for a single fill.

    Args:
        price_dollars: Execution price in dollars, exclusive of 0 and 1.
        contracts: Contracts in this fill; may be fractional.
        series: Series ticker, which selects the multiplier. A market ticker
            works too — pass it through :func:`series_of` first, or let the
            caller do so.
        schedule: Override schedule (tests); defaults to the loaded one.

    Returns:
        Decimal cents, to centicent precision. Fractional, because the
        exchange bills fractional cents.
    """
    price, qty = _validate(price_dollars, contracts)
    if qty == 0:
        return Decimal(0)
    sched = schedule or load_fee_schedule()

    gross = sched.taker_rate(series) * qty * price * (Decimal(1) - price)
    return _round_up_centicents(gross)


def maker_fee_cents(
    price_dollars: Decimal | str | int,
    contracts: Decimal | str | int,
    series: str | None = None,
    schedule: FeeSchedule | None = None,
) -> Decimal:
    """Maker (resting order) fee in cents.

    Zero for any series not listed in the schedule, which is most of them —
    the documented default maker multiplier is 0.
    """
    price, qty = _validate(price_dollars, contracts)
    if qty == 0:
        return Decimal(0)
    sched = schedule or load_fee_schedule()

    rate = sched.maker_rate(series)
    if rate == 0:
        return Decimal(0)

    gross = rate * qty * price * (Decimal(1) - price)
    return _round_up_centicents(gross)


def round_trip_cost_cents(
    entry_price_dollars: Decimal | str | int,
    contracts: Decimal | str | int,
    series: str | None = None,
    *,
    exit_price_dollars: Decimal | str | int | None = None,
    entry_is_taker: bool = True,
    exit_is_taker: bool = True,
    schedule: FeeSchedule | None = None,
) -> Decimal:
    """Total fees to open and close a position, in cents.

    A position held to settlement pays no exit fee — pass
    ``exit_price_dollars=None`` for that case, which is the norm for the
    resolution sniper and set-arb detectors.
    """
    entry_fn = taker_fee_cents if entry_is_taker else maker_fee_cents
    total = entry_fn(entry_price_dollars, contracts, series, schedule)

    if exit_price_dollars is not None:
        exit_fn = taker_fee_cents if exit_is_taker else maker_fee_cents
        total += exit_fn(exit_price_dollars, contracts, series, schedule)

    return total


def net_edge_cents(
    fair_price_dollars: Decimal | str | int,
    executable_price_dollars: Decimal | str | int,
    contracts: Decimal | str | int,
    series: str | None = None,
    *,
    slippage_cents: Decimal | str | int = 0,
    is_taker: bool = True,
    schedule: FeeSchedule | None = None,
) -> Decimal:
    """Per-contract edge in **cents**, net of fees and slippage.

    This is *the* number the system is allowed to show. Positive means the
    trade is expected to make money after costs; a gross edge without this
    correction is meaningless.

    Args:
        fair_price_dollars: Model's fair value, in dollars.
        executable_price_dollars: Price we would actually pay, in dollars.
        contracts: Size, which matters because fees round up per fill.
        slippage_cents: Extra adverse fill assumed, per contract, in cents.

    Returns:
        Net edge per contract in cents. May be negative.
    """
    qty = parse_count(contracts, "contracts")
    if qty <= 0:
        return Decimal(0)

    fair = parse_dollars(fair_price_dollars, "fair_price_dollars")
    price = parse_dollars(executable_price_dollars, "executable_price_dollars")
    slip = parse_dollars(slippage_cents, "slippage_cents")

    fee_fn = taker_fee_cents if is_taker else maker_fee_cents
    fee_total_cents = fee_fn(price, qty, series, schedule)

    gross_per_contract_cents = (fair - price) * Decimal(100)
    fee_per_contract_cents = fee_total_cents / qty

    return gross_per_contract_cents - fee_per_contract_cents - slip

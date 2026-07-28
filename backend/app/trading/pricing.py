"""What a proposed trade actually costs.

Every number the approval card shows comes from here, and every one of them
is net of fees.  This module owns no fee math of its own — it calls
:mod:`app.core.fees`, which is the only place a fee may be computed — but it
owns the *composition*: cost, breakeven, worst case, and net edge.

Two things this refuses to do, both deliberately:

- **Price against an unverified schedule.** If ``fee_schedule.yaml`` has never
  been checked against the official PDF, :class:`UnverifiedFeeSchedule` is
  raised and nothing becomes a proposal. Every edge figure is net of fees, so
  an unchecked fee table makes all of them untrustworthy.
- **Report a gross edge.** ``net_edge_cents`` is populated only when the
  caller supplies a fair value, and it is always net. There is no gross field
  to accidentally render.

Fees are keyed by **series**, derived from the market ticker. That matters:
the previous design keyed them by category, which comes from the parent Event
and is null until a long sync lands — so a market could be priced before its
fee multiplier was knowable. Series is in the ticker, always.

Units, restated because this is where they meet: prices are Decimal dollars,
counts are Decimal contracts (fractional to 0.01), fees are Decimal cents to
centicent precision, and everything the caller reads back is a Decimal or a
string.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import Config
from app.core.fees import (
    FeeSchedule,
    UnverifiedFeeSchedule,
    load_fee_schedule,
    maker_fee_cents,
    net_edge_cents,
    series_of,
    taker_fee_cents,
)
from app.core.money import parse_count, parse_dollars
from app.db.models import Side
from app.trading.direction import BUY, book_side, to_yes_price

__all__ = ["TicketQuote", "price_ticket"]

ONE = Decimal(1)
HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class TicketQuote:
    """A fully costed trade ticket. Every money field is net of fees."""

    ticker: str
    side: Side
    action: str
    #: Price on the traded side, in dollars — what you pay per contract on a
    #: buy. Not the YES price unless the side is YES.
    limit_price: Decimal
    contracts: Decimal
    category: str | None
    is_taker: bool

    #: Series ticker, which is what the fee schedule is keyed by.
    series: str | None

    #: The same order as the exchange sees it. Carried here so the approval
    #: card can show the operator exactly what will be sent.
    wire_book_side: str
    wire_yes_price: Decimal

    #: Contracts * price, in cents. Exact — no rounding.
    notional_cents: Decimal
    est_fee_cents: Decimal
    #: Notional plus fee for a buy. What leaves the account.
    total_cost_cents: Decimal
    #: Price at which this trade breaks even, in cents, fee included. Above
    #: this the position needs to be right more often than it costs.
    breakeven_cents: Decimal
    #: Worst case and best case at settlement, in cents, fees included.
    max_loss_cents: Decimal
    max_win_cents: Decimal
    #: Only when a fair value was supplied. Always net of fees and slippage.
    net_edge_cents: Decimal | None
    fair_price: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for JSON. Money stays a string; the browser must not do
        arithmetic on it."""
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "action": self.action,
            "limit_price": str(self.limit_price),
            "contracts": str(self.contracts),
            "category": self.category,
            "series": self.series,
            "is_taker": self.is_taker,
            "wire": {
                "book_side": self.wire_book_side,
                "yes_price": str(self.wire_yes_price),
                "count": str(self.contracts),
            },
            "notional_cents": str(self.notional_cents),
            "est_fee_cents": str(self.est_fee_cents),
            "total_cost_cents": str(self.total_cost_cents),
            "breakeven_cents": str(self.breakeven_cents),
            "max_loss_cents": str(self.max_loss_cents),
            "max_win_cents": str(self.max_win_cents),
            "net_edge_cents": (
                None if self.net_edge_cents is None else str(self.net_edge_cents)
            ),
            "fair_price": None if self.fair_price is None else str(self.fair_price),
        }


def price_ticket(
    *,
    ticker: str,
    side: Side | str,
    action: str,
    limit_price: Decimal | str | int,
    contracts: Decimal | str | int,
    category: str | None,
    config: Config,
    fair_price: Decimal | str | int | None = None,
    is_taker: bool | None = None,
    schedule: FeeSchedule | None = None,
) -> TicketQuote:
    """Cost a ticket end to end.

    Args:
        limit_price: Price on the traded side, in dollars. For "buy NO at
            30c" this is ``0.30``; the YES price sent to the exchange is
            derived, not supplied.
        category: The market's category. Carried for display and for the
            detectors; it does *not* select the fee multiplier — series does.
        fair_price: Optional model or operator fair value on the traded side.
            Supplying it is what turns on ``net_edge_cents``.
        is_taker: Defaults to ``costs.assume_taker``. A resting order that
            never crosses pays the maker rate, but assuming that for a trade
            we intend to execute now would understate cost.

    Raises:
        UnverifiedFeeSchedule: The fee table has never been checked against
            the official PDF, so no cost here can be trusted.
        UnknownSeries: Only if the schedule gains a multiplier above the
            default, which would make the "unlisted means standard" assumption
            unsafe.
        ValueError: A price outside (0, 1) — including a cents-style ``56``,
            which is rejected rather than read as $56.
    """
    side = Side(side)
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")

    # Fees are keyed by series, which is derivable from the market ticker —
    # unlike the old category lookup, this never depends on the Event sync
    # having landed. `category` is still carried for display and for the
    # detectors, but it no longer gates pricing.
    series = series_of(ticker)

    # Fail closed on an unverified schedule: every edge figure is net of fees,
    # so numbers computed against an unchecked table are not trustworthy.
    sched = schedule or load_fee_schedule()
    if not sched.is_verified:
        raise UnverifiedFeeSchedule()

    price = parse_dollars(limit_price, "limit_price")
    qty = parse_count(contracts, "contracts")
    if qty <= 0:
        raise ValueError(f"contracts must be positive, got {contracts!r}")
    if not (Decimal(0) < price < Decimal(1)):
        raise ValueError(
            f"limit_price must be strictly between 0 and 1 dollars, got "
            f"{limit_price!r}. Kalshi quotes dollar strings like '0.5600'."
        )

    taker = config.costs.assume_taker if is_taker is None else is_taker
    fee_fn = taker_fee_cents if taker else maker_fee_cents
    # Fees round up per fill; this prices the whole order as one fill, which
    # is the optimistic end. The paper simulator prices each level separately
    # because that is what actually happens when an order sweeps a book.
    fee = fee_fn(price, qty, series, sched)

    notional_cents = price * qty * HUNDRED
    fee_per_contract = fee / qty

    if action == BUY:
        # Pay price + fee now; receive $1 per contract if it settles your way.
        total_cost_cents = notional_cents + fee
        breakeven_cents = price * HUNDRED + fee_per_contract
        max_loss_cents = total_cost_cents
        max_win_cents = (ONE - price) * qty * HUNDRED - fee
    else:
        # Selling an existing position: you receive the price and pay the fee.
        # Downside is bounded by what you gave up, not by the premium.
        total_cost_cents = fee - notional_cents
        breakeven_cents = price * HUNDRED - fee_per_contract
        max_loss_cents = (ONE - price) * qty * HUNDRED + fee
        max_win_cents = notional_cents - fee

    edge: Decimal | None = None
    fair: Decimal | None = None
    if fair_price is not None:
        fair = parse_dollars(fair_price, "fair_price")
        # The same domain check `limit_price` gets above, and for the same
        # reason. Without it `fair_price="56"` — the obvious slip for 56c —
        # was accepted and priced as $56 fair value against a 1.8c contract,
        # returning a claimed +$55.97/contract edge with HTTP 200. That number
        # is then written to `proposed_trades.net_edge_cents` and the audit
        # log, where it becomes the "claimed edge" the report card grades the
        # detector against.
        #
        # Strict bounds, matching `limit_price`: a fair value of exactly 0 or
        # 1 asserts certainty, and nothing in this system is allowed to
        # manufacture the last cents of edge out of an assumption of
        # settlement — the stale-quote detector caps fair at 0.98 for the
        # same reason.
        if not (Decimal(0) < fair < Decimal(1)):
            raise ValueError(
                f"fair_price must be strictly between 0 and 1 dollars, got "
                f"{fair_price!r}. Kalshi quotes dollar strings like '0.5600' "
                f"— '56' means $56, not 56 cents."
            )
        # net_edge_cents is written from the buyer's point of view. A sell at
        # p with fair f is exactly a buy of the opposite contract at 1-p with
        # fair 1-f, so the same function prices both — no second edge formula
        # to drift out of step, and the fee comes out identical because
        # P*(1-P) is symmetric.
        edge_fair, edge_price = (fair, price) if action == BUY else (
            ONE - fair,
            ONE - price,
        )
        edge = net_edge_cents(
            edge_fair,
            edge_price,
            qty,
            series,
            slippage_cents=Decimal(str(config.costs.slippage_buffer_cents)),
            is_taker=taker,
            schedule=sched,
        )

    return TicketQuote(
        ticker=ticker,
        side=side,
        action=action,
        limit_price=price,
        contracts=qty,
        category=category,
        series=series,
        is_taker=taker,
        wire_book_side=book_side(side, action),
        wire_yes_price=to_yes_price(side, price),
        notional_cents=notional_cents,
        est_fee_cents=fee,
        total_cost_cents=total_cost_cents,
        breakeven_cents=breakeven_cents,
        max_loss_cents=max_loss_cents,
        max_win_cents=max_win_cents,
        net_edge_cents=edge,
        fair_price=fair,
    )

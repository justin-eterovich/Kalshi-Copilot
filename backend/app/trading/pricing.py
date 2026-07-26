"""What a proposed trade actually costs.

Every number the approval card shows comes from here, and every one of them
is net of fees.  This module owns no fee math of its own — it calls
:mod:`app.core.fees`, which is the only place a fee may be computed — but it
owns the *composition*: cost, breakeven, worst case, and net edge.

Two things this refuses to do, both deliberately:

- **Guess a fee.** A market whose category has no verified multiplier raises
  :class:`~app.core.fees.UnverifiedFeeCategory` and never becomes a proposal.
  An understated fee inflates every downstream number, and the operator would
  have no way to see it.
- **Report a gross edge.** ``net_edge_cents`` is populated only when the
  caller supplies a fair value, and it is always net. There is no gross field
  to accidentally render.

Units, restated because this is where they meet: prices are Decimal dollars,
counts are Decimal contracts (fractional to 0.01), fees are integer cents,
and everything the caller reads back is either a Decimal or a string.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.config import Config
from app.core.fees import (
    FeeSchedule,
    UncategorisedMarket,
    maker_fee_cents,
    net_edge_cents,
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

    #: The same order as the exchange sees it. Carried here so the approval
    #: card can show the operator exactly what will be sent.
    wire_book_side: str
    wire_yes_price: Decimal

    #: Contracts * price, in cents. Exact — no rounding.
    notional_cents: Decimal
    est_fee_cents: int
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
            "is_taker": self.is_taker,
            "wire": {
                "book_side": self.wire_book_side,
                "yes_price": str(self.wire_yes_price),
                "count": str(self.contracts),
            },
            "notional_cents": str(self.notional_cents),
            "est_fee_cents": self.est_fee_cents,
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
        category: The market's category, which selects the fee multiplier.
            Comes from the Event, not the Market — see the catalog sync.
        fair_price: Optional model or operator fair value on the traded side.
            Supplying it is what turns on ``net_edge_cents``.
        is_taker: Defaults to ``costs.assume_taker``. A resting order that
            never crosses pays the maker rate, but assuming that for a trade
            we intend to execute now would understate cost.

    Raises:
        UnverifiedFeeCategory: The market cannot be priced, so it cannot be
            proposed.
        ValueError: A price outside (0, 1) — including a cents-style ``56``,
            which is rejected rather than read as $56.
    """
    side = Side(side)
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")

    # A market with no category cannot be priced. `fees.py` falls back to the
    # default multiplier for a category it does not recognise, which is right
    # for a real category that is simply not premium-rated — and wrong here,
    # because None means "not looked up yet", not "ordinary". Categories are
    # joined from the parent Event after a long sync, so during bootstrap
    # every Crypto market is indistinguishable from an ordinary one and would
    # price at the standard rate while `crypto` is still unverified.
    if category is None:
        raise UncategorisedMarket(ticker)

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
    fee = fee_fn(price, qty, category, schedule)

    notional_cents = price * qty * HUNDRED
    fee_per_contract = Decimal(fee) / qty

    if action == BUY:
        # Pay price + fee now; receive $1 per contract if it settles your way.
        total_cost_cents = notional_cents + fee
        breakeven_cents = price * HUNDRED + fee_per_contract
        max_loss_cents = total_cost_cents
        max_win_cents = (ONE - price) * qty * HUNDRED - fee
    else:
        # Selling an existing position: you receive the price and pay the fee.
        # Downside is bounded by what you gave up, not by the premium.
        total_cost_cents = Decimal(fee) - notional_cents
        breakeven_cents = price * HUNDRED - fee_per_contract
        max_loss_cents = (ONE - price) * qty * HUNDRED + fee
        max_win_cents = notional_cents - fee

    edge: Decimal | None = None
    fair: Decimal | None = None
    if fair_price is not None:
        fair = parse_dollars(fair_price, "fair_price")
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
            category,
            slippage_cents=Decimal(str(config.costs.slippage_buffer_cents)),
            is_taker=taker,
            schedule=schedule,
        )

    return TicketQuote(
        ticker=ticker,
        side=side,
        action=action,
        limit_price=price,
        contracts=qty,
        category=category,
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

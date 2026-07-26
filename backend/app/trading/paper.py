"""Pessimistic fill simulator for paper orders.

Simulated fills exist to answer one question honestly: *would this trade have
made money?*  A simulator that flatters itself is worse than no simulator,
because it produces a report card that says a detector works when it does
not.  So every modelling choice here is deliberately the unfavourable one:

- **We cross the spread.** A buy lifts offers, never rests at the bid. Taker
  fees, always.
- **We walk the book.** Size beyond the top level fills at worse prices,
  which is where slippage actually comes from.
- **Each price level is its own fill.** Fees round up to a whole cent per
  fill, so an order sweeping three levels rounds up three times. Modelling
  the order as one fill would understate the cost of size in a thin book,
  which is precisely the kind of market this system hunts in.
- **We never fill through the limit.** Levels worse than the limit price are
  left alone and the remainder rests, exactly as a real limit order would.
- **A stale book fills nothing.** :class:`~app.kalshi.orderbook.BookStaleError`
  propagates rather than being smoothed over; a fill invented from a book
  with a sequence gap is a fill that did not happen.

The one thing this cannot model is queue position: a resting paper order that
would have been filled by an incoming trade is not filled here. Paper orders
that do not immediately cross therefore just rest until the auto-cancel
sweep. That understates maker performance, which is the safe direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.fees import taker_fee_cents
from app.core.money import parse_count, parse_dollars
from app.db.models import Side
from app.trading.direction import BUY

__all__ = ["SimulatedFill", "simulate_fills", "levels_for"]


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """One fill at one price level."""

    price: Decimal
    contracts: Decimal
    fee_cents: int


def levels_for(
    book: dict[str, Any], side: Side | str, action: str
) -> list[tuple[Decimal, Decimal]]:
    """The levels a taker on ``(side, action)`` would hit, best first.

    Kalshi quotes both sides of the book as **bids**: ``yes`` levels are bids
    to buy YES, ``no`` levels are bids to buy NO. So:

    - To **buy YES** you lift the offers, which are the NO bids seen from the
      other side: a NO bid at ``p`` is an offer to sell YES at ``1 - p``.
    - To **sell YES** you hit the YES bids directly.
    - Buying NO and selling NO mirror that.

    Getting this backwards produces a simulator that fills every order at a
    great price and never disagrees with the operator, which looks like
    working software.
    """
    side = Side(side)
    is_buy = action == BUY

    # Buying a side means lifting offers. Offers are not quoted directly:
    # they are the *other* side's bids, seen from our side, so the price
    # inverts. Selling means hitting our own side's bids, already in the
    # right units.
    #   buy YES  -> `no` bids,  price 1-q      sell YES -> `yes` bids, price p
    #   buy NO   -> `yes` bids, price 1-p      sell NO  -> `no` bids,  price q
    source = "no" if (side is Side.YES) == is_buy else "yes"
    invert = is_buy
    raw = book.get(source) or []

    levels: list[tuple[Decimal, Decimal]] = []
    for entry in raw:
        if not entry or len(entry) < 2:
            continue
        price = parse_dollars(entry[0], "level price")
        size = parse_count(entry[1], "level size")
        if size <= 0:
            continue
        levels.append((Decimal(1) - price if invert else price, size))

    # Best first: cheapest when buying, richest when selling.
    levels.sort(key=lambda level: level[0], reverse=not is_buy)
    return levels


def simulate_fills(
    *,
    book: dict[str, Any],
    side: Side | str,
    action: str,
    limit_price: Decimal,
    contracts: Decimal,
    category: str | None,
    slippage_cents: Decimal = Decimal(0),
) -> list[SimulatedFill]:
    """Fill ``contracts`` against ``book``, returning one fill per level.

    Args:
        book: ``{"yes": [[price, size], ...], "no": [...]}`` as the orderbook
            endpoint returns it — both sides quoted as bids, values as
            strings.
        limit_price: On the traded side, in dollars. Never filled through.
        slippage_cents: Extra adverse cents per contract, applied to the
            price before the limit check. Configured pessimism: it makes a
            marginal trade fail to fill rather than fill at a fiction.

    Returns an empty list when nothing is executable, which is a legitimate
    outcome and not an error.
    """
    side = Side(side)
    is_buy = action == BUY
    slip = slippage_cents / Decimal(100)

    remaining = contracts
    fills: list[SimulatedFill] = []

    for price, size in levels_for(book, side, action):
        if remaining <= 0:
            break

        # Adverse slippage: pay more when buying, receive less when selling.
        effective = price + slip if is_buy else price - slip

        # A price outside (0, 1) is not a tradeable contract price; slippage
        # at the extremes can push it there. Skip rather than let it reach
        # the fee engine, which would (correctly) raise.
        if not (Decimal(0) < effective < Decimal(1)):
            continue

        # The limit is a hard boundary in both directions.
        if is_buy and effective > limit_price:
            break
        if not is_buy and effective < limit_price:
            break

        take = min(size, remaining)
        fills.append(
            SimulatedFill(
                price=effective,
                contracts=take,
                # Per level, because that is per fill, because that is how
                # the exchange rounds.
                fee_cents=taker_fee_cents(effective, take, category),
            )
        )
        remaining -= take

    return fills

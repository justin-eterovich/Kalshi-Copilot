"""Order direction: our (side, action) vocabulary vs Kalshi's single book.

This module exists because the translation is a one-liner that is wrong in
four different ways if you write it from memory, and every one of those ways
silently trades the opposite of what the operator approved.

**Kalshi's V2 order API quotes one book, from the YES side.**

- ``bid`` means *buy YES*.
- ``ask`` means *sell YES*, which is economically *buy NO at 1 - price*.
- ``price`` on the wire is **always the YES price**, whichever direction you
  are going. Two orders match at the same number; they just sit on opposite
  sides of it.

We keep ``side`` (yes/no) and ``action`` (buy/sell) internally because that is
how a trader reads a ticket — "buy 100 NO at 30c" — and because the fee and
P&L math wants the price actually paid.  The mapping is:

===========  ===========  ===================================
side/action  book side    wire price
===========  ===========  ===================================
buy YES      ``bid``      ``p``
sell YES     ``ask``      ``p``
buy NO       ``ask``      ``1 - p``
sell NO      ``bid``      ``1 - p``
===========  ===========  ===================================

Read that table twice.  "Buy NO at 0.30" goes to the exchange as an **ask at
0.70**, and an ask is the side that *sells* YES.  Both halves of that have to
be right or the position is inverted.

The fee formula ``M * 0.07 * C * P * (1 - P)`` is symmetric in ``P`` and
``1 - P``, so fees are the same either way — which is precisely why a
direction bug does not show up as a fee discrepancy.  Nothing catches it but
this table and the tests around it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal

from app.core.money import parse_dollars
from app.db.models import Side

__all__ = [
    "BUY",
    "SELL",
    "BID",
    "ASK",
    "book_side",
    "to_yes_price",
    "from_yes_price",
    "opposite_side",
    "signed_contracts",
]

BUY: Final = "buy"
SELL: Final = "sell"
BID: Final = "bid"
ASK: Final = "ask"

Action = Literal["buy", "sell"]
BookSide = Literal["bid", "ask"]

ONE: Final = Decimal(1)


def _check_action(action: str) -> str:
    if action not in (BUY, SELL):
        raise ValueError(f"action must be 'buy' or 'sell', got {action!r}")
    return action


def book_side(side: Side | str, action: str) -> BookSide:
    """Map ``(side, action)`` onto Kalshi's ``bid``/``ask``.

    ``bid`` when buying YES or selling NO; ``ask`` when selling YES or buying
    NO.  Equivalently: bid whenever the two inputs "agree".
    """
    _check_action(action)
    is_yes = Side(side) is Side.YES
    is_buy = action == BUY
    return BID if is_yes == is_buy else ASK


def to_yes_price(side: Side | str, price_dollars: Decimal | str | int) -> Decimal:
    """Convert a price quoted on ``side`` into the YES price the wire wants.

    A NO price of ``0.30`` is a YES price of ``0.70``.  YES prices pass
    through unchanged.
    """
    price = parse_dollars(price_dollars, "price_dollars")
    return price if Side(side) is Side.YES else ONE - price


def from_yes_price(side: Side | str, yes_price_dollars: Decimal | str | int) -> Decimal:
    """Inverse of :func:`to_yes_price`, for rendering exchange data locally.

    The operation is its own inverse, but both names exist so call sites read
    in the direction they actually mean.
    """
    return to_yes_price(side, yes_price_dollars)


def opposite_side(side: Side | str) -> Side:
    return Side.NO if Side(side) is Side.YES else Side.YES


def signed_contracts(side: Side | str, action: str, contracts: Decimal) -> Decimal:
    """Position delta in **YES-equivalent** contracts.

    Positions are carried as one signed number per market: positive is long
    YES, negative is long NO.  That is the only representation in which a
    market's exposure nets correctly, because buying NO really does cancel a
    YES position on this exchange rather than sitting beside it.
    """
    _check_action(action)
    magnitude = contracts if action == BUY else -contracts
    return magnitude if Side(side) is Side.YES else -magnitude

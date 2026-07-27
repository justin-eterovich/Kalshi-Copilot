"""Set arbitrage: pricing a whole mutually-exclusive event at once.

An event whose markets are mutually exclusive admits two candidate trades:

**Sell every leg.** Selling YES on each leg collects ``sum(bid_i)`` now. At
settlement at most one leg resolves YES, so at most $1 is paid out per set.
Collecting more than $1 plus costs is therefore riskless.

**Buy every leg.** Buying YES on each leg costs ``sum(ask_i)``. It returns $1
only if *some* leg resolves YES — which requires the set to be
**exhaustive**.

Those two are not symmetric, and the difference is the whole design of this
module. The API's ``mutually_exclusive`` flag says only *"only one market in
this event can resolve to 'yes'"* — **at most one**. It says nothing about
whether one must. In the live catalog many exclusive sets are plainly not
exhaustive: ``KXNEWPOPE-70`` lists 7 candidates whose asks sum to $4.12,
because there are more than 7 possible popes. Buying all 7 for $0.99 would
lose the lot if an eighth won.

So **the sell side is enabled by default and the buy side is not**. Buying
requires the operator to name the series as exhaustive in config, because
nothing in the API establishes it and guessing turns a "riskless arb" into an
uncovered short of the unlisted outcomes.

Every price here is executable — walked through the book for the size being
traded, not read off the top — and every edge is net of per-leg taker fees.
A set arb is a lot of small fees: N legs in and, if you unwind rather than
holding to settlement, N legs out.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from app.core.fees import FeeSchedule, series_of, taker_fee_cents
from app.core.money import parse_count, parse_dollars

__all__ = [
    "LegBook",
    "LegFill",
    "SetOpportunity",
    "Direction",
    "walk",
    "price_set",
    "max_executable_sets",
]

ONE = Decimal(1)
HUNDRED = Decimal(100)

#: ``sell`` = sell YES on every leg (safe under exclusivity alone).
#: ``buy``  = buy YES on every leg (needs the set to be exhaustive too).
Direction = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class LegBook:
    """One leg's order book, both sides quoted as bids in Kalshi's convention."""

    ticker: str
    #: [[price, size], ...] — bids to buy YES.
    yes: list[tuple[Decimal, Decimal]]
    #: [[price, size], ...] — bids to buy NO.
    no: list[tuple[Decimal, Decimal]]

    @classmethod
    def from_payload(cls, ticker: str, book: dict[str, Any]) -> LegBook:
        def levels(raw: Any) -> list[tuple[Decimal, Decimal]]:
            out: list[tuple[Decimal, Decimal]] = []
            for entry in raw or []:
                if not entry or len(entry) < 2:
                    continue
                price = parse_dollars(entry[0], "level price")
                size = parse_count(entry[1], "level size")
                if size > 0:
                    out.append((price, size))
            return out

        return cls(ticker=ticker, yes=levels(book.get("yes")), no=levels(book.get("no")))

    def executable(self, direction: Direction) -> list[tuple[Decimal, Decimal]]:
        """Levels we could hit, as **YES prices**, best first.

        Selling YES means hitting the YES bids — best is the highest.
        Buying YES means lifting offers, which are the NO bids inverted: a NO
        bid at ``q`` is an offer to sell YES at ``1 - q``. Best is the lowest.
        """
        if direction == "sell":
            return sorted(self.yes, key=lambda level: -level[0])
        offers = [(ONE - price, size) for price, size in self.no]
        return sorted(offers, key=lambda level: level[0])


@dataclass(frozen=True, slots=True)
class LegFill:
    """What one leg would actually execute at, for the whole set size."""

    ticker: str
    #: Volume-weighted YES price across the levels consumed.
    avg_price: Decimal
    contracts: Decimal
    #: Taker fee in cents, summed per price level because that is per fill.
    fee_cents: Decimal


@dataclass(frozen=True, slots=True)
class SetOpportunity:
    """A costed set trade. ``net_edge_cents`` is per set, after all fees."""

    event_ticker: str
    direction: Direction
    contracts: Decimal
    legs: tuple[LegFill, ...]
    #: Sum of executable YES prices across the legs, in dollars per set.
    gross_sum: Decimal
    total_fee_cents: Decimal
    #: Per set, net of every leg's fee. Positive means riskless profit.
    net_edge_cents: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_ticker": self.event_ticker,
            "direction": self.direction,
            "contracts": str(self.contracts),
            "gross_sum": str(self.gross_sum),
            "total_fee_cents": str(self.total_fee_cents),
            "net_edge_cents": str(self.net_edge_cents),
            "legs": [
                {
                    "ticker": leg.ticker,
                    "avg_price": str(leg.avg_price),
                    "contracts": str(leg.contracts),
                    "fee_cents": str(leg.fee_cents),
                }
                for leg in self.legs
            ],
        }


def walk(
    levels: list[tuple[Decimal, Decimal]],
    contracts: Decimal,
    *,
    slippage_cents: Decimal = Decimal(0),
    direction: Direction = "sell",
) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal]]] | None:
    """Consume ``contracts`` from ``levels``, best first.

    Returns ``(avg_price, filled, consumed_levels)``, or ``None`` if the book
    cannot fill the full size — a partial set is not an arb, it is an
    uncovered position in whatever legs did fill.

    Slippage is adverse in the direction of trade: selling receives less,
    buying pays more.
    """
    slip = slippage_cents / HUNDRED
    remaining = contracts
    cost = Decimal(0)
    consumed: list[tuple[Decimal, Decimal]] = []

    for price, size in levels:
        if remaining <= 0:
            break
        effective = price - slip if direction == "sell" else price + slip
        if not (Decimal(0) < effective < ONE):
            continue
        take = min(size, remaining)
        cost += take * effective
        consumed.append((effective, take))
        remaining -= take

    if remaining > 0:
        return None
    filled = contracts - remaining
    return cost / filled, filled, consumed


def price_set(
    *,
    event_ticker: str,
    books: list[LegBook],
    contracts: Decimal,
    direction: Direction,
    schedule: FeeSchedule | None = None,
    slippage_cents: Decimal = Decimal(0),
) -> SetOpportunity | None:
    """Cost a whole set at ``contracts`` per leg.

    Returns ``None`` when any leg cannot fill the full size. That is not a
    near miss to be reported optimistically: an arb that fills on four of five
    legs is not an arb, it is a directional bet nobody chose to make.

    The edge, per set of ``contracts``:

    - **sell**: ``(sum(bid_i) - 1) * contracts * 100 - fees``
    - **buy**:  ``(1 - sum(ask_i)) * contracts * 100 - fees``
    """
    if not books:
        return None
    qty = parse_count(contracts, "contracts")
    if qty <= 0:
        return None

    fills: list[LegFill] = []
    gross_sum = Decimal(0)
    total_fee = Decimal(0)

    for book in books:
        walked = walk(
            book.executable(direction),
            qty,
            slippage_cents=slippage_cents,
            direction=direction,
        )
        if walked is None:
            return None
        avg_price, filled, consumed = walked

        # Fees are charged per fill, and a book walk is one fill per level.
        # Summing per level is what the exchange actually does.
        series = series_of(book.ticker)
        fee = sum(
            (taker_fee_cents(price, size, series, schedule) for price, size in consumed),
            Decimal(0),
        )

        fills.append(
            LegFill(
                ticker=book.ticker,
                avg_price=avg_price,
                contracts=filled,
                fee_cents=fee,
            )
        )
        gross_sum += avg_price
        total_fee += fee

    if direction == "sell":
        gross_cents = (gross_sum - ONE) * qty * HUNDRED
    else:
        gross_cents = (ONE - gross_sum) * qty * HUNDRED

    return SetOpportunity(
        event_ticker=event_ticker,
        direction=direction,
        contracts=qty,
        legs=tuple(fills),
        gross_sum=gross_sum,
        total_fee_cents=total_fee,
        net_edge_cents=gross_cents - total_fee,
    )


def max_executable_sets(books: list[LegBook], direction: Direction) -> Decimal:
    """Largest set size every leg can fill — the thinnest leg decides.

    A set trade is only as big as its most illiquid leg, which is usually far
    smaller than any single leg's depth suggests.
    """
    if not books:
        return Decimal(0)
    depths = [
        sum((size for _, size in book.executable(direction)), Decimal(0))
        for book in books
    ]
    return min(depths)

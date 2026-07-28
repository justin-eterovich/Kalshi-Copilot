"""What a detector's pick turned out to be worth.

This is the module the tuner rests on. The LLM call is easy; turning "the
detector said buy YES at 26c" into a number that means "and it was wrong by
this much" is where a tuner is made honest or useless.

Three rules, and each one makes the reported numbers **worse**. That is the
intended direction.

Mark at the exit, never the mid
-------------------------------
A pick is scored against the price you could actually get out at: the **bid**
on the traded side for a long, the **ask** for a short. Marking at the midpoint
credits half the spread as profit on every pick, and these detectors trade wide
books — so a mid-based tuner rewards precisely the picks that cannot be exited,
and then tightens thresholds towards more of them.

This is not hypothetical here. The live report card shows ``stale_quote`` at
**-46.21c over 13 trades against exactly 46.21c of fees paid**: gross was flat
and the costs ate all of it. Any marking scheme that is not fee-aware scores
those trades at roughly zero and concludes the detector is fine.

A pick that cannot be marked is refused, not zeroed
---------------------------------------------------
No bid, no market row, a void resolution — each returns a :class:`Mark` whose
basis is :attr:`MarkBasis.UNMARKABLE` and which carries the reason. It is never
silently a P&L of zero. A zero is a claim that the pick was exactly neutral;
"we cannot say" is a different statement, and collapsing the two is the
``return None`` failure this codebase already has a section about — a refusal
that disappears looks identical to a finding of no edge.

Settled and open are different evidence and never merge
--------------------------------------------------------
A resolved market yields a realised number. An open one yields a mark against a
book that will move again before it means anything. Both are computed here and
both are labelled; anything that averages them is claiming a confidence it does
not have. See :class:`MarkBasis`.

Purity
------
No I/O, no clock, no ORM. The caller reduces a signal to a :class:`Pick` and a
market to a :class:`Quote` and passes both in; the fee schedule is an argument.
Everything interesting is therefore testable without a database — which matters
because the interesting cases are the refusals, and refusals are exactly what a
live database does not reliably contain when you want to test them.

Units, restated because this is where they meet: prices are ``Decimal``
dollars, contracts are ``Decimal`` and may be fractional, and every P&L figure
is ``Decimal`` **cents** to centicent precision, because fees are.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.core.fees import FeeSchedule, maker_fee_cents, series_of, taker_fee_cents
from app.core.money import parse_count, parse_dollars
from app.db.models import Side

__all__ = [
    "BUY",
    "SELL",
    "MarkBasis",
    "Mark",
    "Pick",
    "Quote",
    "Unmarkable",
    "mark_pick",
]

BUY: Final = "buy"
SELL: Final = "sell"

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)
HUNDRED: Final = Decimal(100)


class MarkBasis(enum.StrEnum):
    """What kind of evidence a mark is. Never sum across these."""

    #: The market resolved. Realised, and the only ground truth here.
    SETTLED = "settled"
    #: The market is open; marked against the current exit quote. Provisional
    #: — the book moves again tomorrow and so does this number.
    OPEN = "open"
    #: Could not be marked at all. Carries a :class:`Unmarkable` reason and no
    #: P&L. Counted, reported, and excluded from every aggregate.
    UNMARKABLE = "unmarkable"


class Unmarkable(enum.StrEnum):
    """Why a pick could not be scored.

    These strings are an interface: they are counted per detector, shown to the
    operator, and handed to the model as evidence about its own sample. Each is
    a *different* operator action, which is why they are not one "skipped" flag.
    """

    #: The signal carried no executable price in its evidence. Research-only
    #: detectors emit these deliberately (the resolution sniper's notes), so
    #: this is expected volume rather than a defect — but a pick with no entry
    #: price was never a tradeable claim and must not be scored as one.
    NO_ENTRY_PRICE = "no_entry_price"

    #: The entry price is outside ``(0, 1)`` exclusive, so it is not a price a
    #: contract could have traded at. Refused rather than clamped: a malformed
    #: price that quietly reads as a boundary looks like free money.
    ENTRY_PRICE_INVALID = "entry_price_invalid"

    #: No row for the ticker. Usually a market aged out of the catalog between
    #: the pick and the mark.
    MARKET_MISSING = "market_missing"

    #: The market is open but the side we would exit on has no quote, or the
    #: quote is zero. **A zero bid is not a price of zero, it is the absence of
    #: a buyer** — marking against it books a total loss for a position that
    #: was never exitable, which flatters nothing but is still fiction.
    NO_EXIT_QUOTE = "no_exit_quote"

    #: The market resolved ``void``. Nobody was paid and nobody was right; a
    #: void is the absence of an outcome, not an outcome of zero.
    MARKET_VOID = "market_void"

    #: The finding carried a size of zero or less. Refused rather than
    #: defaulted to one contract: a detector that sized a position at zero has
    #: said something, and quietly inventing a size on its behalf turns a
    #: non-trade into a scored trade. Distinct from *no* size hint, which is
    #: the normal case for detectors that leave sizing to the risk layer.
    SIZE_INVALID = "size_invalid"


@dataclass(frozen=True, slots=True)
class Pick:
    """One thing a detector claimed, reduced to what scoring needs.

    Built from a :class:`~app.db.models.Signal` row by ``harvest.py``. The
    ``action``/``side`` pair is the trader's-eye view the rest of this codebase
    uses — see ``app/trading/direction.py`` — and ``entry_price`` is the price
    on the **traded side**, so "buy NO at 30c" is ``side=NO, action=buy,
    entry_price=0.30``. It is never the YES price unless the side is YES.
    """

    signal_id: int
    detector: str
    ticker: str
    side: Side
    action: str
    created_at: datetime
    #: What the detector claimed, per contract, net of fees. From the signal;
    #: this module never recomputes it, it only measures against it.
    claimed_edge_cents: Decimal
    confidence: float
    #: Price on the traded side the detector said it could get. ``None`` when
    #: the finding carried no executable price — see
    #: :attr:`Unmarkable.NO_ENTRY_PRICE`.
    entry_price: Decimal | None
    #: Size the detector suggested. ``None`` falls back to one contract, which
    #: only affects fee rounding (fees round up to a centicent per fill), never
    #: the per-contract figures the dossier reports.
    contracts: Decimal | None
    event_ticker: str | None = None
    #: How many scans re-derived this same observation. One pick, whatever it
    #: says — the dedupe fold is what keeps it one.
    seen_count: int = 1
    #: Whether this signal ever became a proposal a human could approve. Most
    #: did not, and a mark on one that did not is hypothetical.
    became_proposal: bool = False

    @property
    def series_ticker(self) -> str | None:
        """Series, which is what the fee schedule is keyed by.

        Derived from the ticker, never from ``category``: category comes from
        the parent Event, is backfilled, and says nothing about what a market
        tracks.
        """
        return series_of(self.ticker)

    @property
    def sized_contracts(self) -> Decimal:
        """Size to score at: the hint, or one contract when there was none.

        Note the check is ``is None``, not truthiness. A ``size_hint`` of
        exactly zero is falsy, and treating it as "absent" would invent a
        one-contract position for a detector that explicitly sized at nothing —
        the same tri-state confusion that makes ``Market.result == ''`` read as
        settled. Zero falls through to :attr:`Unmarkable.SIZE_INVALID`.
        """
        return ONE if self.contracts is None else self.contracts


@dataclass(frozen=True, slots=True)
class Quote:
    """The market as of the mark instant.

    All four sides are carried because which one is the exit depends on the
    direction, and getting that mapping wrong inverts the sign of every number
    downstream without changing its plausibility.

    ``resolved_outcome`` is tri-state and deliberately **not** named
    ``settled``: ``True`` is YES, ``False`` is NO, ``None`` is still open. A
    boolean called ``settled`` holding ``False`` for "settled NO" invites
    ``if q.settled:``, which silently drops every NO-resolved market and biases
    the surviving sample towards YES. Ask :attr:`is_resolved` instead.
    """

    ticker: str
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    resolved_outcome: bool | None = None
    #: Resolved, but to nothing. Distinct from ``resolved_outcome is None``,
    #: which means still trading.
    is_void: bool = False

    @property
    def is_resolved(self) -> bool:
        """Whether the outcome is known, either way."""
        return self.resolved_outcome is not None

    def exit_price_for(self, side: Side, action: str) -> Decimal | None:
        """The quote you would actually close against.

        A long is closed by selling, which hits the **bid** on that side; a
        short is closed by buying, which lifts the **ask**. Both sides of the
        book are quoted separately by the exchange, so this reads the stored
        quote rather than complementing the YES side — a complement would be
        right about the price and wrong about the size behind it.
        """
        if action == BUY:
            return self.yes_bid if side is Side.YES else self.no_bid
        return self.yes_ask if side is Side.YES else self.no_ask


@dataclass(frozen=True, slots=True)
class Mark:
    """A pick, scored — or a named reason it could not be.

    Every money field is ``None`` exactly when :attr:`basis` is
    :attr:`MarkBasis.UNMARKABLE`, so a consumer that forgets to check the basis
    gets a ``TypeError`` rather than a plausible zero.
    """

    pick: Pick
    basis: MarkBasis
    refusal: Unmarkable | None = None
    detail: str | None = None

    #: Price the position was closed at, on the traded side. For a settled
    #: market this is the payout, 1 or 0 — not a quote.
    exit_price: Decimal | None = None
    contracts: Decimal | None = None

    #: Gross move in cents across the whole position, before costs.
    gross_cents: Decimal | None = None
    #: Entry fee plus exit fee. A position held to settlement pays no exit fee.
    fee_cents: Decimal | None = None
    #: Assumed adverse fill on the exit only — see :func:`mark_pick`.
    slippage_cents: Decimal | None = None
    #: Gross less fees less slippage, across the position.
    net_cents: Decimal | None = None
    #: The comparable number: net cents **per contract**, which is the unit
    #: ``Signal.net_edge_cents`` is quoted in.
    net_per_contract_cents: Decimal | None = None

    @property
    def scoreable(self) -> bool:
        return self.basis is not MarkBasis.UNMARKABLE

    @property
    def edge_error_cents(self) -> Decimal | None:
        """Claimed edge minus realised, per contract.

        The headline number of this whole milestone. Positive means the
        detector over-claimed, which live is the case by roughly 28c per
        contract for ``stale_quote``.
        """
        if self.net_per_contract_cents is None:
            return None
        return self.pick.claimed_edge_cents - self.net_per_contract_cents


def _unmarkable(pick: Pick, reason: Unmarkable, detail: str) -> Mark:
    return Mark(pick=pick, basis=MarkBasis.UNMARKABLE, refusal=reason, detail=detail)


def _fee_cents(
    price: Decimal,
    contracts: Decimal,
    series: str | None,
    schedule: FeeSchedule | None,
    *,
    is_taker: bool,
) -> Decimal:
    """Fee for one fill, or zero at a price where no fee can exist.

    The schedule's formula is ``rate * qty * P * (1 - P)``, which is zero at
    ``P = 0`` and ``P = 1`` — and ``core.fees`` refuses those prices outright
    rather than returning the zero, because a *quoted* price of 0 or 1 is
    almost always a parse error rather than a real market. Here the boundary is
    reached legitimately: a settled position exits at exactly 1 or 0. So the
    zero is supplied directly rather than by widening the guard in ``fees.py``,
    which every other caller depends on staying strict.

    Note the formula is symmetric in ``P`` and ``1 - P``, so passing the
    traded-side price gives the same fee as passing the YES price. That
    symmetry is also why a flipped direction cannot be caught here — see
    ``app/trading/direction.py``, which is the only guard that can.
    """
    if not (ZERO < price < ONE):
        return ZERO
    fee_fn = taker_fee_cents if is_taker else maker_fee_cents
    return fee_fn(price, contracts, series, schedule)


def mark_pick(
    pick: Pick,
    quote: Quote | None,
    *,
    slippage_cents: Decimal | str | int = 0,
    schedule: FeeSchedule | None = None,
    is_taker: bool = True,
) -> Mark:
    """Score one pick against the world as of the mark instant.

    Args:
        quote: The market now, or ``None`` when no row exists for the ticker.
        slippage_cents: Extra adverse fill assumed **on the exit only**, per
            contract. Not applied to the entry: the entry price is the one the
            detector claimed it could get, and the detector's own
            ``net_edge_cents`` already netted slippage out of it. Charging it
            twice would make every detector look worse by a constant, which
            moves no threshold in the right direction but does make the
            claimed-vs-realised gap unreadable.
        is_taker: Whether both legs cross the spread. Taker by default, which
            is the pessimistic assumption and the one ``costs.assume_taker``
            ships with.

    Returns:
        A :class:`Mark`. Always — a pick that cannot be scored comes back with
        :attr:`MarkBasis.UNMARKABLE` and a reason, never as ``None``.
    """
    if pick.entry_price is None:
        return _unmarkable(
            pick,
            Unmarkable.NO_ENTRY_PRICE,
            "the finding carried no executable price, so it was never a "
            "tradeable claim",
        )

    try:
        entry = parse_dollars(pick.entry_price, "entry_price")
    except (ValueError, ArithmeticError) as exc:
        return _unmarkable(pick, Unmarkable.ENTRY_PRICE_INVALID, str(exc))
    if not (ZERO < entry < ONE):
        return _unmarkable(
            pick,
            Unmarkable.ENTRY_PRICE_INVALID,
            f"entry price {entry} is outside (0, 1) exclusive and is not a "
            f"price a contract could have traded at",
        )

    if quote is None:
        return _unmarkable(
            pick,
            Unmarkable.MARKET_MISSING,
            f"no market row for {pick.ticker} at the mark instant",
        )
    if quote.is_void:
        return _unmarkable(
            pick,
            Unmarkable.MARKET_VOID,
            "the market resolved void: nobody was paid and nobody was right",
        )

    contracts = parse_count(pick.sized_contracts, "contracts")
    if contracts <= ZERO:
        return _unmarkable(
            pick,
            Unmarkable.SIZE_INVALID,
            f"size hint is {contracts}; a position of zero or fewer contracts "
            f"is not a trade to score",
        )

    series = pick.series_ticker
    entry_fee = _fee_cents(entry, contracts, series, schedule, is_taker=is_taker)

    if quote.is_resolved:
        # Held to resolution: the exit is a payout, not a trade. No exit fee
        # and no slippage, because no fill happened — `round_trip_cost_cents`
        # documents the same asymmetry for the same reason.
        won = (quote.resolved_outcome is True) == (pick.side is Side.YES)
        exit_price = ONE if won else ZERO
        exit_fee = ZERO
        slip_total = ZERO
        basis = MarkBasis.SETTLED
    else:
        raw_exit = quote.exit_price_for(pick.side, pick.action)
        if raw_exit is None or raw_exit <= ZERO:
            return _unmarkable(
                pick,
                Unmarkable.NO_EXIT_QUOTE,
                f"no {'bid' if pick.action == BUY else 'ask'} on the "
                f"{pick.side.value} side to exit against; a zero quote is the "
                f"absence of a counterparty, not a price",
            )

        slip_per_contract = parse_dollars(slippage_cents, "slippage_cents") / HUNDRED
        # Adverse in whichever direction hurts: you sell lower, or buy higher.
        if pick.action == BUY:
            exit_price = max(ZERO, raw_exit - slip_per_contract)
        else:
            exit_price = min(ONE, raw_exit + slip_per_contract)

        exit_fee = _fee_cents(
            exit_price, contracts, series, schedule, is_taker=is_taker
        )
        slip_total = slip_per_contract * HUNDRED * contracts
        basis = MarkBasis.OPEN

    # Direction: a long makes money when the exit is above the entry; a short
    # when it is below. `side` does not appear here at all — it was already
    # spent choosing which side's book to read.
    if pick.action == BUY:
        gross = (exit_price - entry) * HUNDRED * contracts
    else:
        gross = (entry - exit_price) * HUNDRED * contracts

    fees = entry_fee + exit_fee
    net = gross - fees - slip_total

    return Mark(
        pick=pick,
        basis=basis,
        exit_price=exit_price,
        contracts=contracts,
        gross_cents=gross,
        fee_cents=fees,
        slippage_cents=slip_total,
        net_cents=net,
        net_per_contract_cents=net / contracts,
    )

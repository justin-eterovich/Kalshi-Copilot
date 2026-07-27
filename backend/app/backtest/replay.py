"""Pure event replay — the core of the backtester.

A backtester's characteristic failure is not a bug in the arithmetic. It is
that the strategy is allowed to see the future, and the resulting equity curve
looks wonderful. Every arrangement in this module exists to make look-ahead
*structurally impossible* rather than merely discouraged:

- **The stream must already be sorted, and that is verified.** An unsorted
  stream raises rather than being sorted here. If the caller's query came back
  unordered that is a bug in the caller, and silently sorting it hides both the
  bug and the look-ahead it caused everywhere else in that caller.
- **The strategy is never handed an outcome.** Settlement results live in a
  separate ``outcomes`` mapping that only :func:`replay` reads, and only once
  replay time has reached ``settled_at``. The guarantee is enforced by *type*:
  :class:`StrategyState` has three fields and none of them can carry an
  :class:`Outcome`, transitively.
- **No history is provided at all.** A strategy that needs history accumulates
  it from the observations it is handed, and it is handed observations one at a
  time in time order — so its history is truncated at ``now`` by construction,
  not by a filter that someone could forget to apply. A pre-built history slice
  would have to be truncated on *every* read; a stream cannot be un-truncated.
- **Replay time advances only from observation timestamps.** Nothing in this
  module reads a clock. ``datetime.now()`` does not appear, and must not: a
  backtest that consults the wall clock is not reproducible.

Two more things this module deliberately does *not* do:

- **It does not simulate fills.** ``filler`` is injected and the real engine
  passes an adapter over :func:`app.trading.paper.simulate_fills`, which is the
  single pessimistic fill model. A backtester with its own second fill model
  would eventually disagree with the paper book, and the friendlier of the two
  would be the one quoted in the report card.
- **It does not mark open positions to market.** A position still open when the
  stream ends is reported in :attr:`ReplayResult.unsettled` with its cost basis
  and contributes exactly nothing to realised P&L or to the equity curve.
  Marking it to the last quote invents a profit that was never realised, which
  is the flattering-simulator failure this whole package is written against.

Units follow the house rules and the Kalshi wire format exactly:

- **Prices are ``Decimal`` dollars** (``0.5600``, up to 6 decimals). Sub-cent
  ticks are real, so nothing here rounds a price.
- **Contract counts are ``Decimal`` and fractional** to 0.01. Nothing assumes
  an integer size.
- **P&L and fees are ``Decimal`` cents**, and fractional — a price difference
  times a fractional count is not a whole number of cents.
- ``float`` never appears. Every money input is required to be a ``Decimal``
  and an ``int`` is rejected too, for the same reason ``taker_fee_cents(56,
  ...)`` raises: ``56`` almost always meant cents.

Three formulas are duplicated here
----------------------------------

``app.trading.positions.realized_from_fill``,
``app.trading.settlements.realized_from_settlement`` and the ``(side, action)``
direction mapping in ``app.trading.direction`` are the source of truth for what
this module computes. All three modules import SQLAlchemy at module scope
(``app.db.models``), so importing them would make this module non-pure. The
formulas are therefore *mirrored* below, with a comment at each site naming the
original.

A copy that can drift is a liability, so it is pinned rather than trusted:
``tests/test_backtest_replay.py`` imports the originals — tests have no purity
constraint — and asserts the mirrors agree with them across every case,
including all four ``(side, action)`` combinations. CLAUDE.md warns that
nothing downstream catches an inverted direction, because the fee formula
``P*(1-P)`` is symmetric and a flipped trade produces the same fee, the same
notional, and a plausible confirmation. A differential test is the only thing
that would notice, so there is one.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Protocol

__all__ = [
    "Side",
    "BUY",
    "SELL",
    "Observation",
    "Intent",
    "ExecutedFill",
    "Outcome",
    "OpenPosition",
    "StrategyState",
    "ClosedTrade",
    "UnsettledPosition",
    "ReplayResult",
    "Strategy",
    "Filler",
    "ReplayError",
    "UnsortedObservations",
    "NaiveTimestamp",
    "LookAheadError",
    "FillMismatch",
    "replay",
]

HUNDRED: Final = Decimal(100)
ONE: Final = Decimal(1)
ZERO: Final = Decimal(0)

BUY: Final = "buy"
SELL: Final = "sell"


class Side(enum.StrEnum):
    """Local, dependency-free mirror of ``app.db.models.Side``.

    Defined here rather than imported because ``app.db.models`` pulls in
    SQLAlchemy and this module must import with the standard library alone.
    Both are ``StrEnum`` over the same two values, so the two interoperate
    without conversion: ``models.Side.YES == Side.YES`` is ``True`` because
    both are the string ``"yes"``. Every public entry point accepts
    ``Side | str`` and normalises through :func:`_side`, so the engine can pass
    ORM enums straight through and a test can pass ``"no"``.
    """

    YES = "yes"
    NO = "no"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReplayError(RuntimeError):
    """Base for every refusal in this module.

    Replay fails closed. A backtest that patches over bad input produces a
    number that looks like a result, and a wrong report card is worse than no
    report card because it is the thing that decides whether real money gets
    deployed.
    """


class UnsortedObservations(ReplayError):
    """The observation stream went backwards in time."""


class NaiveTimestamp(ReplayError):
    """A timestamp had no timezone, so its ordering is not defined."""


class LookAheadError(ReplayError):
    """The strategy tried to act on information it could not have had."""


class FillMismatch(ReplayError):
    """The injected filler returned something that is not this intent's fill."""


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observation:
    """One market's state at one instant. The only source of replay time.

    ``book`` is the raw orderbook payload in Kalshi's own convention (both
    sides quoted as bids, values as strings) — exactly what
    :func:`app.trading.paper.simulate_fills` wants, so the filler adapter can
    pass it through untouched. ``None`` means no book was captured at this
    instant, which a filler is free to treat as "nothing fills".

    ``last_price`` is a YES price in dollars, or ``None``. Candle OHLC is null
    in any period with no trades, which in a thin market is most of them, so
    this is genuinely optional and must not be defaulted to zero — a price that
    quietly reads as zero looks like free money.

    Note what is *absent*: nothing on this type says how the market resolved.
    That is not an oversight, it is the guarantee. See the module docstring.
    """

    ts: datetime
    ticker: str
    book: dict[str, Any] | None = None
    last_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Intent:
    """What a strategy wants to do. Not a fill — the filler decides that.

    ``side``/``action`` is the trader's vocabulary ("buy 100 NO at 30c") and
    ``limit_price`` is the price on the **traded side**: ``0.30`` for "buy NO
    at 30c", not the ``0.70`` the wire would carry. The conversion belongs at
    the wire and nowhere else; see ``app/trading/direction.py``.
    """

    ticker: str
    side: Side
    action: str
    limit_price: Decimal
    contracts: Decimal


@dataclass(frozen=True, slots=True)
class ExecutedFill:
    """One fill, as the injected filler reports it.

    ``price`` is on the traded side, in dollars, and ``fee_cents`` is the fee
    that fill was charged — fractional, because fees round up to a centicent
    and not to a cent. Replay never computes a fee: ``app/core/fees.py`` is the
    only module allowed to, and the filler adapter is what calls it.
    """

    ticker: str
    side: Side
    action: str
    price: Decimal
    contracts: Decimal
    fee_cents: Decimal


@dataclass(frozen=True, slots=True)
class Outcome:
    """Ground truth. :func:`replay` reads this; the strategy never sees it.

    ``settled_at`` is when the market resolved. Replay recognises the payout at
    the first observation at or after that instant, because replay time only
    advances from observations — so an outcome that lands after the last
    observation is never applied and its position is reported unsettled.

    Only binary resolution is representable. A scalar market pays something
    between $0 and $1 and this type cannot say what, which is deliberate: the
    payout would have to be guessed, and a settlement priced by guesswork
    writes a permanently wrong P&L into a book with no correction path. Feed
    scalar markets in only once ``Outcome`` grows a real payout field.
    """

    ticker: str
    settled_yes: bool
    settled_at: datetime

    @property
    def payout_yes(self) -> Decimal:
        """What one YES contract paid, in dollars: $1.00 or $0.00."""
        return ONE if self.settled_yes else ZERO


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """A position as the strategy sees it: one signed number per market.

    ``net_contracts`` is YES-equivalent and signed — positive long YES,
    negative long NO — and ``avg_price`` is the average **YES** price of what
    is open, so 5 NO bought at 30c is ``net_contracts=-5, avg_price=0.70``.
    That is the convention in ``app/trading/positions.py`` and it is the only
    representation in which a market's exposure nets correctly, because buying
    NO genuinely offsets a YES position rather than sitting beside it.
    """

    ticker: str
    net_contracts: Decimal
    avg_price: Decimal


@dataclass(frozen=True, slots=True)
class StrategyState:
    """Everything the strategy is allowed to know, and nothing else.

    Three fields. ``now`` is replay time, ``observation`` is the market state
    that produced it, and ``positions`` is a read-only view of the strategy's
    own open positions keyed by ticker (flat markets are absent, never present
    with a zero).

    There is no history, no forward slice, and no route to an :class:`Outcome`
    from any field or any field of any field. A strategy that wants history
    keeps its own, built from the observations it has been handed — which are
    only ever the ones at or before ``now``.
    """

    now: datetime
    observation: Observation
    positions: Mapping[str, OpenPosition]


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One realisation: a round-trip closed by trading, or by settlement.

    ``ts`` is **replay time** — the instant the backtest recognised the P&L —
    rather than an outcome's ``settled_at``, so that ``trades`` is ordered the
    same way ``equity_curve`` is. For a settlement the two differ by at most
    one observation gap, and the true resolution time is in the ``Outcome`` the
    caller already holds.

    ``contracts`` is the absolute number of YES-equivalent contracts closed;
    both prices are YES prices, matching ``avg_price``.
    """

    ticker: str
    ts: datetime
    contracts: Decimal
    entry_yes_price: Decimal
    exit_yes_price: Decimal
    realized_pnl_cents: Decimal
    kind: str  # "fill" | "settlement"


@dataclass(frozen=True, slots=True)
class UnsettledPosition:
    """A position still open when the stream ended. Never marked to anything.

    Reported so the caller can see what the run was still carrying, and
    deliberately excluded from realised P&L and from the equity curve. Whether
    it was a winner is not known, and the last quote is not an answer — that is
    the number a flattering simulator would book as profit.

    ``cost_basis_cents`` is cash actually paid to open, always non-negative:
    ``abs(net_contracts)`` at the price on the traded side. For a NO position
    that is ``(1 - avg_price)``, so 5 NO at 30c carries a basis of 150c, not
    the signed −350c that the YES-equivalent numbers would suggest.
    """

    ticker: str
    net_contracts: Decimal
    avg_price: Decimal
    cost_basis_cents: Decimal


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """The report card. Realised only.

    ``equity_curve`` is ordered by time and expressed in ``Decimal`` cents,
    which is what ``app/backtest/stats.py`` consumes. It holds **exactly one
    point per observation**, appended after that observation has been fully
    processed (settlements first, then the strategy's fills), so its length
    equals ``observations`` and its timestamps are non-decreasing.

    Equity is ``starting_equity_cents + realised P&L − fees``. Fees are
    subtracted the moment they are charged, as they are in
    ``app/trading/positions.py``: a strategy that pays fees to build a position
    it never closes has lost money, and the curve should say so on the day it
    happened.
    """

    trades: tuple[ClosedTrade, ...]
    fills: tuple[tuple[datetime, ExecutedFill], ...]
    equity_curve: tuple[tuple[datetime, Decimal], ...]
    unsettled: tuple[UnsettledPosition, ...]
    starting_equity_cents: Decimal
    ending_equity_cents: Decimal
    realized_pnl_cents: Decimal
    fees_paid_cents: Decimal
    observations: int = 0
    intents: int = 0
    filled_intents: int = 0
    settlements: int = 0


class Strategy(Protocol):
    """The caller's logic. Sees a :class:`StrategyState`, returns intents."""

    def __call__(self, state: StrategyState, /) -> Sequence[Intent] | None: ...


class Filler(Protocol):
    """Turns an intent into fills against one observation.

    Injected so this module never invents a fill. The real engine wraps
    :func:`app.trading.paper.simulate_fills`; a test passes something trivial.
    Returning an empty sequence is a legitimate outcome, not an error — it is
    what a limit order that does not cross looks like.
    """

    def __call__(
        self, intent: Intent, observation: Observation, /
    ) -> Sequence[ExecutedFill] | None: ...


# ---------------------------------------------------------------------------
# Mirrored formulas — see the module docstring for why these are copies
# ---------------------------------------------------------------------------


def _signed_contracts(side: Side, action: str, contracts: Decimal) -> Decimal:
    """Position delta in YES-equivalent contracts.

    Mirror of ``app.trading.direction.signed_contracts``, which is the source
    of truth. Buying YES and selling NO are positive; selling YES and buying NO
    are negative.
    """
    magnitude = contracts if action == BUY else -contracts
    return magnitude if side is Side.YES else -magnitude


def _to_yes_price(side: Side, price: Decimal) -> Decimal:
    """A price quoted on ``side``, expressed as a YES price.

    Mirror of ``app.trading.direction.to_yes_price``. A NO price of 0.30 is a
    YES price of 0.70; YES prices pass through.
    """
    return price if side is Side.YES else ONE - price


def _realized_from_fill(
    *,
    net_contracts: Decimal,
    avg_price: Decimal,
    delta: Decimal,
    fill_yes_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Apply one signed fill to a signed position.

    Mirror of ``app.trading.positions.realized_from_fill``, which is the source
    of truth and carries the full explanation. In brief: adding realises
    nothing and moves the average; reducing realises ``(fill - avg) * closed``
    in the direction of the old position and leaves the surviving lot's basis
    alone; crossing through flat realises on the closed part only and opens the
    remainder at the fill price, because averaging across a flip would carry a
    long's cost basis into a short.
    """
    if delta == 0:
        return net_contracts, avg_price, ZERO

    if net_contracts == 0 or (net_contracts > 0) == (delta > 0):
        new_net = net_contracts + delta
        total = abs(net_contracts) + abs(delta)
        new_avg = (avg_price * abs(net_contracts) + fill_yes_price * abs(delta)) / total
        return new_net, new_avg, ZERO

    closed = min(abs(delta), abs(net_contracts))
    direction = ONE if net_contracts > 0 else -ONE
    realized = (fill_yes_price - avg_price) * closed * direction * HUNDRED

    new_net = net_contracts + delta
    if new_net == 0:
        return new_net, ZERO, realized
    if (new_net > 0) == (net_contracts > 0):
        return new_net, avg_price, realized
    return new_net, fill_yes_price, realized


def _realized_from_settlement(
    *, net_contracts: Decimal, avg_price: Decimal, payout_yes: Decimal
) -> Decimal:
    """P&L in cents from holding a position through settlement.

    Mirror of ``app.trading.settlements.realized_from_settlement``. Identical
    in shape to closing at ``payout_yes``, because that is what settlement is:
    every contract bought back at what it turned out to be worth. Signed
    YES-equivalents make the NO side fall out for free — 5 NO carried at 0.70
    that settle NO is ``(0 - 0.70) * -5 * 100`` = +350 cents.

    No exit fee is charged, here or by the caller. A position held to
    resolution pays only its entry fee; see ``round_trip_cost_cents`` in
    ``app/core/fees.py``, which documents the same rule.
    """
    return (payout_yes - avg_price) * net_contracts * HUNDRED


# ---------------------------------------------------------------------------
# Input validation — every one of these refuses rather than repairing
# ---------------------------------------------------------------------------


def _money(value: object, what: str) -> Decimal:
    """Require a ``Decimal``. ``float`` and ``int`` are both rejected.

    ``float`` because money that drifts cannot be audited. ``int`` because a
    bare ``56`` for a price almost always meant 56 *cents*, and the whole
    reason this codebase exists is that the Kalshi API uses no integer cents
    anywhere. ``taker_fee_cents`` refuses ints for exactly this reason.
    """
    if not isinstance(value, Decimal):
        raise ReplayError(
            f"{what} must be a Decimal in dollars/cents, got "
            f"{type(value).__name__} {value!r}"
        )
    if value.is_nan() or value.is_infinite():
        raise ReplayError(f"{what} is not a finite number: {value!r}")
    return value


def _aware(ts: object, what: str) -> datetime:
    if not isinstance(ts, datetime):
        raise ReplayError(f"{what} must be a datetime, got {type(ts).__name__} {ts!r}")
    # A naive datetime has no defined ordering against an aware one, so a
    # stream mixing them cannot be checked for sortedness at all — which is
    # the check that keeps look-ahead out.
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise NaiveTimestamp(
            f"{what} is a naive datetime ({ts!r}); timestamps must be "
            "timezone-aware (UTC). A naive timestamp has no defined ordering, "
            "so the sortedness check that prevents look-ahead cannot run."
        )
    return ts


def _side(value: object, what: str) -> Side:
    """Normalise ``Side | str``, including ``app.db.models.Side``.

    Both enums are ``StrEnum`` over ``"yes"``/``"no"``, so an ORM ``Side``
    arrives as a plain string and converts cleanly.
    """
    try:
        return Side(value)
    except ValueError:
        raise ReplayError(f"{what} must be 'yes' or 'no', got {value!r}") from None


def _action(value: object, what: str) -> str:
    if value not in (BUY, SELL):
        raise ReplayError(f"{what} must be 'buy' or 'sell', got {value!r}")
    return str(value)


def _check_observation(obs: object, index: int) -> Observation:
    if not isinstance(obs, Observation):
        raise ReplayError(
            f"observation {index} must be an Observation, got {type(obs).__name__}"
        )
    _aware(obs.ts, f"observation[{index}].ts")
    if not obs.ticker:
        raise ReplayError(f"observation {index} has no ticker")
    if obs.last_price is not None:
        _money(obs.last_price, f"observation[{index}].last_price")
    return obs


def _check_intent(intent: object, obs: Observation, settled: frozenset[str]) -> Intent:
    if not isinstance(intent, Intent):
        raise ReplayError(f"strategy returned {type(intent).__name__}, not an Intent")

    if intent.ticker != obs.ticker:
        # The filler is handed exactly one observation. Filling ticker A from
        # ticker B's book is not slippage, it is a different instrument — the
        # same class of mistake as pricing an ETH contract against BTC spot.
        raise ReplayError(
            f"intent on {intent.ticker!r} was returned for an observation of "
            f"{obs.ticker!r}; a strategy may only act on the market it is shown"
        )

    if intent.ticker in settled:
        raise LookAheadError(
            f"intent on {intent.ticker!r} at {obs.ts.isoformat()}, after that "
            "market settled. The resolution is already known at this point in "
            "replay time, so any P&L from this trade is look-ahead."
        )

    side = _side(intent.side, "intent.side")
    action = _action(intent.action, "intent.action")
    limit = _money(intent.limit_price, "intent.limit_price")
    contracts = _money(intent.contracts, "intent.contracts")

    if not (ZERO <= limit <= ONE):
        raise ReplayError(
            f"intent.limit_price {limit} is outside $0.00-$1.00; a binary "
            "contract cannot trade there"
        )
    if contracts <= 0:
        raise ReplayError(f"intent.contracts must be positive, got {contracts}")

    return Intent(
        ticker=intent.ticker,
        side=side,
        action=action,
        limit_price=limit,
        contracts=contracts,
    )


def _check_fill(raw: object, intent: Intent) -> ExecutedFill:
    """Validate one fill against the intent that produced it.

    These guards exist because a filler is *injected*, so an adapter bug lands
    here undetected otherwise. In particular an inverted direction is invisible
    downstream: the fee formula ``P*(1-P)`` is symmetric, so a flipped fill has
    the same fee and the same notional as the right one and the backtest simply
    reports the opposite trade with a straight face.
    """
    if not isinstance(raw, ExecutedFill):
        raise FillMismatch(
            f"filler returned {type(raw).__name__}, not an ExecutedFill"
        )

    side = _side(raw.side, "fill.side")
    action = _action(raw.action, "fill.action")

    if raw.ticker != intent.ticker:
        raise FillMismatch(
            f"filler returned a fill on {raw.ticker!r} for an intent on "
            f"{intent.ticker!r}"
        )
    if side is not intent.side or action != intent.action:
        raise FillMismatch(
            f"filler returned a {action} {side.value} fill for a "
            f"{intent.action} {intent.side.value} intent on {intent.ticker}; "
            "an inverted direction is invisible downstream, because the fee "
            "formula is symmetric"
        )

    price = _money(raw.price, "fill.price")
    contracts = _money(raw.contracts, "fill.contracts")
    fee = _money(raw.fee_cents, "fill.fee_cents")

    if not (ZERO < price < ONE):
        raise FillMismatch(
            f"fill price {price} is not a tradeable contract price on "
            f"{intent.ticker}; must be strictly between $0.00 and $1.00"
        )
    if contracts <= 0:
        raise FillMismatch(f"fill contracts must be positive, got {contracts}")
    if fee < 0:
        raise FillMismatch(f"fill fee_cents must not be negative, got {fee}")

    # A limit is a hard boundary in both directions; simulate_fills already
    # honours it, and a filler that does not is inventing price improvement.
    if action == BUY and price > intent.limit_price:
        raise FillMismatch(
            f"fill at {price} is through the buy limit {intent.limit_price} "
            f"on {intent.ticker}"
        )
    if action == SELL and price < intent.limit_price:
        raise FillMismatch(
            f"fill at {price} is through the sell limit {intent.limit_price} "
            f"on {intent.ticker}"
        )

    return ExecutedFill(
        ticker=raw.ticker,
        side=side,
        action=action,
        price=price,
        contracts=contracts,
        fee_cents=fee,
    )


def _settlement_schedule(outcomes: Mapping[str, Outcome]) -> tuple[Outcome, ...]:
    """Outcomes in the order replay will recognise them.

    Sorted by ``(settled_at, ticker)`` so that two markets resolving at the
    same instant are always applied in the same order — determinism is a
    requirement, and dict order is a property of how the caller built the
    mapping, not of the data.
    """
    checked: list[Outcome] = []
    for key, outcome in outcomes.items():
        if not isinstance(outcome, Outcome):
            raise ReplayError(
                f"outcomes[{key!r}] must be an Outcome, got "
                f"{type(outcome).__name__}"
            )
        if outcome.ticker != key:
            # A mapping whose key disagrees with its value settles the wrong
            # market, and every number that follows is arithmetically correct
            # about the wrong instrument.
            raise ReplayError(
                f"outcomes[{key!r}] carries ticker {outcome.ticker!r}; the key "
                "and the outcome must name the same market"
            )
        _aware(outcome.settled_at, f"outcomes[{key!r}].settled_at")
        checked.append(outcome)

    return tuple(sorted(checked, key=lambda o: (o.settled_at, o.ticker)))


# ---------------------------------------------------------------------------
# The replay loop
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Book:
    """Mutable working state. Never handed out; snapshots are what escape."""

    positions: dict[str, OpenPosition] = field(default_factory=dict)
    realized_cents: Decimal = ZERO
    fees_cents: Decimal = ZERO

    def snapshot(self) -> Mapping[str, OpenPosition]:
        """A read-only view for the strategy.

        ``MappingProxyType`` over a fresh dict: the strategy cannot mutate the
        book, and cannot hold a reference that keeps changing under it either.
        """
        return MappingProxyType(dict(self.positions))


def replay(
    observations: Iterable[Observation],
    *,
    strategy: Strategy,
    filler: Filler,
    outcomes: Mapping[str, Outcome] | None = None,
    starting_equity_cents: Decimal = ZERO,
) -> ReplayResult:
    """Replay ``observations`` through ``strategy``, returning the report card.

    Args:
        observations: Time-ordered market states. **Must** already be sorted
            ascending by ``ts``; an out-of-order element raises
            :class:`UnsortedObservations` rather than being sorted here.
            Consumed lazily, so a generator over a cursor is fine. Ties are
            allowed — several markets can share an instant.
        strategy: Called once per observation with a :class:`StrategyState`.
            Returns the intents to submit at that instant, or an empty sequence
            (or ``None``) for "do nothing".
        filler: Called once per intent with ``(intent, observation)``. Returns
            the fills that intent got. The real engine adapts
            :func:`app.trading.paper.simulate_fills`; replay never simulates a
            fill itself.
        outcomes: How markets resolved, keyed by ticker. Read only by this
            function, and only once replay time reaches ``settled_at``. The
            strategy has no route to it.
        starting_equity_cents: Where the equity curve begins, in cents.

    Returns:
        A :class:`ReplayResult`. Realised P&L only — nothing open is marked.

    Ordering within one observation, which matters:

    1. Replay time advances to ``obs.ts``.
    2. Any outcome whose ``settled_at`` has now passed is applied. A market
       that resolved is resolved *before* the strategy is asked what to do, so
       the strategy sees the position gone rather than a ghost it could try to
       trade.
    3. ``strategy`` is called, its intents are validated and filled, and each
       fill is folded into the book.
    4. One equity point is recorded.

    Raises:
        UnsortedObservations: The stream went backwards in time.
        NaiveTimestamp: A timestamp had no timezone.
        LookAheadError: The strategy acted on an already-settled market.
        FillMismatch: The filler returned something that is not this intent's
            fill — wrong ticker, inverted direction, or through the limit.
        ReplayError: Any other refusal; all of the above subclass it.
    """
    _money(starting_equity_cents, "starting_equity_cents")
    schedule = _settlement_schedule(outcomes or {})

    book = _Book()
    trades: list[ClosedTrade] = []
    fills_log: list[tuple[datetime, ExecutedFill]] = []
    equity_curve: list[tuple[datetime, Decimal]] = []

    settled: set[str] = set()
    next_settlement = 0

    n_obs = 0
    n_intents = 0
    n_filled = 0
    n_settlements = 0

    previous_ts: datetime | None = None

    for index, raw_obs in enumerate(observations):
        obs = _check_observation(raw_obs, index)
        now = obs.ts

        if previous_ts is not None and now < previous_ts:
            raise UnsortedObservations(
                f"observation stream is not sorted by ts: {obs.ticker} at "
                f"{now.isoformat()} follows {previous_ts.isoformat()}. Sort the "
                "query; replay will not sort it, because an out-of-order "
                "stream means the caller already showed the strategy the "
                "future and hiding that here hides the look-ahead too."
            )
        previous_ts = now
        n_obs += 1

        # --- 1. Settlements that replay time has now reached --------------
        while (
            next_settlement < len(schedule)
            and schedule[next_settlement].settled_at <= now
        ):
            outcome = schedule[next_settlement]
            next_settlement += 1
            settled.add(outcome.ticker)

            position = book.positions.pop(outcome.ticker, None)
            if position is None or position.net_contracts == 0:
                continue

            payout = outcome.payout_yes
            realized = _realized_from_settlement(
                net_contracts=position.net_contracts,
                avg_price=position.avg_price,
                payout_yes=payout,
            )
            book.realized_cents += realized
            n_settlements += 1
            trades.append(
                ClosedTrade(
                    ticker=outcome.ticker,
                    ts=now,
                    contracts=abs(position.net_contracts),
                    entry_yes_price=position.avg_price,
                    exit_yes_price=payout,
                    realized_pnl_cents=realized,
                    kind="settlement",
                )
            )
            # No exit fee is added: a position held to resolution pays only
            # what it paid to get in. See round_trip_cost_cents in fees.py.

        # --- 2. Ask the strategy ------------------------------------------
        state = StrategyState(
            now=now, observation=obs, positions=book.snapshot()
        )
        intents = strategy(state) or ()

        # --- 3. Fill and account ------------------------------------------
        frozen_settled = frozenset(settled)
        for raw_intent in intents:
            intent = _check_intent(raw_intent, obs, frozen_settled)
            n_intents += 1

            # Validate every fill before applying any of them, so a bad batch
            # cannot leave the book half-updated and unrecoverable.
            checked = [_check_fill(f, intent) for f in (filler(intent, obs) or ())]
            total = sum((f.contracts for f in checked), ZERO)
            if total > intent.contracts:
                raise FillMismatch(
                    f"filler overfilled {intent.ticker}: {total} contracts "
                    f"against an intent for {intent.contracts}"
                )
            if not checked:
                continue
            n_filled += 1

            for executed in checked:
                _apply(book, executed, now, trades)
                fills_log.append((now, executed))

        # --- 4. One equity point, after everything at this instant ---------
        equity_curve.append(
            (
                now,
                starting_equity_cents + book.realized_cents - book.fees_cents,
            )
        )

    unsettled = tuple(
        UnsettledPosition(
            ticker=position.ticker,
            net_contracts=position.net_contracts,
            avg_price=position.avg_price,
            # Cash paid, on the traded side, always non-negative.
            cost_basis_cents=(
                position.avg_price
                if position.net_contracts > 0
                else ONE - position.avg_price
            )
            * abs(position.net_contracts)
            * HUNDRED,
        )
        for position in sorted(book.positions.values(), key=lambda p: p.ticker)
        if position.net_contracts != 0
    )

    return ReplayResult(
        trades=tuple(trades),
        fills=tuple(fills_log),
        equity_curve=tuple(equity_curve),
        unsettled=unsettled,
        starting_equity_cents=starting_equity_cents,
        ending_equity_cents=(
            starting_equity_cents + book.realized_cents - book.fees_cents
        ),
        realized_pnl_cents=book.realized_cents,
        fees_paid_cents=book.fees_cents,
        observations=n_obs,
        intents=n_intents,
        filled_intents=n_filled,
        settlements=n_settlements,
    )


def _apply(
    book: _Book, fill: ExecutedFill, now: datetime, trades: list[ClosedTrade]
) -> None:
    """Fold one fill into the book, recording any realisation it caused.

    A fill records the side it took, the direction, and the price on that side;
    all three become YES-equivalents before they can net. Fees are recognised
    immediately because that is when they are charged.
    """
    prior = book.positions.get(fill.ticker)
    prior_net = prior.net_contracts if prior else ZERO
    prior_avg = prior.avg_price if prior else ZERO

    delta = _signed_contracts(fill.side, fill.action, fill.contracts)
    fill_yes_price = _to_yes_price(fill.side, fill.price)

    # Captured before the update: how much of the old lot this fill closed.
    reducing = prior_net != 0 and delta != 0 and (prior_net > 0) != (delta > 0)
    closed = min(abs(delta), abs(prior_net)) if reducing else ZERO

    new_net, new_avg, realized = _realized_from_fill(
        net_contracts=prior_net,
        avg_price=prior_avg,
        delta=delta,
        fill_yes_price=fill_yes_price,
    )

    book.realized_cents += realized
    book.fees_cents += fill.fee_cents

    if new_net == 0:
        # A flat market is absent from the book rather than present as a zero,
        # so the strategy's view never contains a position it cannot trade.
        book.positions.pop(fill.ticker, None)
    else:
        book.positions[fill.ticker] = OpenPosition(
            ticker=fill.ticker, net_contracts=new_net, avg_price=new_avg
        )

    if closed > 0:
        trades.append(
            ClosedTrade(
                ticker=fill.ticker,
                ts=now,
                contracts=closed,
                entry_yes_price=prior_avg,
                exit_yes_price=fill_yes_price,
                realized_pnl_cents=realized,
                kind="fill",
            )
        )

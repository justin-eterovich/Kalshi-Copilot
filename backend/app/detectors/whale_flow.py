"""Whale flow: outsized prints and sweeps on a market's public trade tape.

Somebody just bought 5,000 contracts through four price levels in two
seconds. That is a fact, and it is worth a human looking at.

**The dangerous version of this module is the one that turns that fact into
a recommendation** — "follow the smart money" — because the inference it
smuggles in is false in both halves.

*Smart*: size is not skill. The largest print of the day is as likely to be
the one that gets run over as the one that called it.

*Money going somewhere*: a large trade is not information about value. It is
information that somebody with a different opinion — or a different **need**:
hedging an exposure booked elsewhere, unwinding, rebalancing at a mandate
boundary, meeting a margin call — transacted. Nothing on a public tape
distinguishes conviction from obligation. And the tape is symmetric: every
one of those contracts was sold by somebody, at that price, on purpose. The
seller may be the informed party. Trading with the buyer because the buyer
was louder is momentum, not edge.

So flow here is an **input to a human's judgement, never an instruction**.
Three things enforce that rather than merely asserting it:

- `analyse` returns a `FlowEvent` — an observation with a rationale. It
  computes no fair value, no edge, no size, and no direction to trade. It
  cannot become a proposal without a human deciding it should.
- Confidence is hard-capped by the caller's `base_confidence`. No amount of
  size and no number of levels swept can push it past that ceiling, because
  the ceiling encodes how much this *kind* of evidence is worth, and no
  instance of the evidence gets to argue with that.
- Every refusal returns `None`. A flat tape, a short tape, an unreadable
  taker side — none of them degrade into a small confident number.

The z-score in particular is a convenience, not a model: trade sizes are
heavily right-skewed, so 3 sigma shows up on a real tape far more often than
a normal distribution says it should. That is named here rather than hidden,
and it is an argument for the confidence cap, not against the metric.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import mean, pstdev

__all__ = ["FlowEvent", "Sweep", "Trade", "analyse", "detect_sweep", "size_zscore"]

ZERO = Decimal(0)
ONE = Decimal(1)

BUY = "buy"
SELL = "sell"
#: Direction reported when a size outlier's taker side is unreadable. Never
#: silently rendered as "buy" — see `_direction`.
UNKNOWN = "unknown"

_CAVEAT = (
    "Flow only, not a valuation: the other side of this trade may be the "
    "informed one, and the taker may have been hedging or unwinding rather "
    "than expressing a view."
)


@dataclass(frozen=True, slots=True)
class Trade:
    """One print from the public tape.

    `yes_price` is the YES price in dollars, as everything internal is;
    `count` is fractional to 0.01, as the wire is. `taker_side` is nullable
    on the wire and is nullable here — the absence is load-bearing.
    """

    ts: datetime
    yes_price: Decimal
    count: Decimal
    #: Aggressor side as reported by the exchange, or None when it is not.
    #: Accepted spellings are the tape's ("yes"/"no") and the trader's
    #: ("buy"/"sell"); anything else reads as unknown.
    taker_side: str | None


@dataclass(frozen=True, slots=True)
class Sweep:
    """A run of aggressive same-side prints walking through the book."""

    #: Pressure on the YES price: "buy" lifted it, "sell" hit it down.
    direction: str
    #: Count of *distinct* prices touched, not of trades. This is the number
    #: that separates a sweep from one order filling in pieces.
    levels: int
    contracts: Decimal
    first_price: Decimal
    last_price: Decimal
    seconds: float


@dataclass(frozen=True, slots=True)
class FlowEvent:
    """An observation about flow, for a human to weigh.

    Two different observations arrive in this shape and must not be confused,
    so read `sweep` before acting on anything:

    - `sweep is None` — a **size** outlier. One large print at one price.
      Somebody had a lot to do and did it, possibly patiently, possibly on a
      resting order that happened to get filled at once.
    - `sweep is not None` — **aggression**. Somebody paid up through
      successive levels, which costs money and therefore says something about
      urgency, though still nothing about whether they are right.
    """

    #: "buy", "sell", or UNKNOWN. Direction of *pressure*, not of a trade
    #: this module is suggesting.
    direction: str
    contracts: Decimal
    #: Size z-score of the tape's largest print. Reported for context even
    #: when it was the sweep rather than the size that triggered the event,
    #: and None whenever it could not be computed honestly.
    zscore: float | None
    sweep: Sweep | None
    #: Never exceeds the caller's `base_confidence`.
    confidence: float
    rationale: str


# Not the order-direction mapping — that lives in `app/trading/direction.py`
# and converts a ticket to the wire. This only reads which way a print
# pushed the YES price: a taker buying YES lifts it, a taker buying NO (i.e.
# selling YES) pushes it down.
_TAKER_SIDES = {"buy": BUY, "yes": BUY, "sell": SELL, "no": SELL}


def _direction(taker_side: str | None) -> str | None:
    """Normalise a tape `taker_side`, or None when it cannot be read.

    `taker_side` is nullable on the wire, and a null must never be bucketed
    as a buy. Half the tape defaulted to "buy" would produce a permanent,
    entirely fictional buy-side bias that looks exactly like real flow.
    An unrecognised spelling is treated the same way as a null, for the same
    reason: this returns what the exchange said or nothing.
    """
    if taker_side is None:
        return None
    return _TAKER_SIDES.get(taker_side.strip().lower())


def _usable(trade: Trade) -> bool:
    """Whether a print can be trusted enough to reason about.

    A price outside (0, 1) or a non-positive count is a malformed row, not a
    small one. Such a print is dropped from the size baseline and breaks a
    sweep run rather than being coerced into something plausible.
    """
    return ZERO < trade.yes_price < ONE and trade.count > ZERO


def _utc(ts: datetime) -> datetime:
    """Naive timestamps are treated as UTC, as everywhere else in the app."""
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts


def _ordered(trades: Sequence[Trade]) -> list[Trade]:
    """Oldest first. The caller's ordering is not assumed to be anything."""
    return sorted(trades, key=lambda t: _utc(t.ts))


def size_zscore(
    counts: Sequence[Decimal], value: Decimal, *, min_samples: int = 20
) -> float | None:
    """How unusual `value` is against a sample of trade sizes.

    Returns None — never a number — in the two cases where a z-score would
    be manufactured rather than measured:

    - **Fewer than `min_samples` counts.** A handful of prints has no shape
      to be an outlier from, and the first big trade on a quiet market would
      otherwise score enormously against the three small ones before it.
    - **Zero standard deviation.** A tape where every trade is the same size
      contains no outliers by construction; dividing by a zero spread would
      turn the flattest possible evidence into infinite conviction.

    The z-score is descriptive only. Trade sizes are right-skewed and
    nothing like normal, so the usual "3 sigma is a 1-in-370 event" reading
    does not hold here — expect 3 sigma regularly on a live tape. Callers
    should treat the threshold as a knob tuned against real tapes, not as a
    p-value, which is why `analyse` caps confidence independently of it.
    """
    if len(counts) < min_samples:
        return None
    # Kept in Decimal until the last step: the ratio is dimensionless and a
    # float is fine, but the mean and spread are money-shaped and must not
    # pick up binary rounding on the way there.
    spread = pstdev(counts)
    if spread == ZERO:
        return None
    return float((value - mean(counts)) / spread)


def _extends(run: list[Trade], trade: Trade, *, window_sec: float) -> bool:
    """Whether `trade` continues the run in progress."""
    if not run:
        return False
    if not _usable(trade):
        return False
    direction = _direction(run[0].taker_side)
    if direction is None or _direction(trade.taker_side) != direction:
        return False
    last = run[-1]
    if (_utc(trade.ts) - _utc(last.ts)).total_seconds() > window_sec:
        return False
    # Equal prices are allowed and simply do not add a level: a block filling
    # against several resting orders at one price is one order, not a walk.
    if direction == BUY:
        return trade.yes_price >= last.yes_price
    return trade.yes_price <= last.yes_price


def _runs(ordered: Sequence[Trade], *, window_sec: float) -> Iterator[list[Trade]]:
    """Split the tape into maximal same-side, in-window, monotonic runs.

    A print whose side cannot be read **breaks** the run rather than being
    skipped over. Skipping would splice two unrelated runs into one and
    invent a sweep that nobody executed; breaking can only hide a sweep that
    did happen. Both are wrong, but only one of them puts a fabricated
    observation in front of a human, so this is the right direction to be
    wrong in.
    """
    run: list[Trade] = []
    for trade in ordered:
        if _extends(run, trade, window_sec=window_sec):
            run.append(trade)
            continue
        if run:
            yield run
        seeds = _usable(trade) and _direction(trade.taker_side) is not None
        run = [trade] if seeds else []
    if run:
        yield run


def _as_sweep(run: Sequence[Trade], *, min_levels: int) -> Sweep | None:
    """A run becomes a sweep only once it has walked enough distinct prices."""
    direction = _direction(run[0].taker_side) if run else None
    if direction is None:
        return None
    levels = len({t.yes_price for t in run})
    if levels < min_levels:
        return None
    return Sweep(
        direction=direction,
        levels=levels,
        contracts=sum((t.count for t in run), ZERO),
        first_price=run[0].yes_price,
        last_price=run[-1].yes_price,
        seconds=(_utc(run[-1].ts) - _utc(run[0].ts)).total_seconds(),
    )


def detect_sweep(
    trades: Sequence[Trade], *, window_sec: float, min_levels: int
) -> Sweep | None:
    """The most recent qualifying sweep on the tape, if there is one.

    A sweep is a run of prints that share one readable taker side, arrive
    within `window_sec` of each other, and walk monotonically through at
    least `min_levels` distinct prices in the direction that side implies —
    a buy sweep lifts through increasing YES prices, a sell sweep hits down
    through decreasing ones.

    `min_levels` is the whole distinction between a sweep and a large fill.
    Twenty prints at one price is a single order meeting the queue at that
    price; counting them as twenty levels would report every block trade as
    a book-clearing sweep. Requiring *distinct* prices makes the metric
    measure aggression — paying progressively worse prices to get done — and
    aggression is at least a real thing that costs the taker money.
    """
    if min_levels < 2 or window_sec <= 0:
        # A one-level "sweep" is just a trade, and a non-positive window
        # cannot group anything. Both are caller error; refuse rather than
        # return something that will read as a finding.
        return None

    latest: Sweep | None = None
    for run in _runs(_ordered(trades), window_sec=window_sec):
        sweep = _as_sweep(run, min_levels=min_levels)
        if sweep is not None:
            latest = sweep  # runs arrive oldest-first, so the last one wins
    return latest


def _confidence(
    *,
    base_confidence: float,
    zscore: float | None,
    sweep: Sweep | None,
    zscore_threshold: float,
    min_levels: int,
) -> float:
    """Confidence in a flow observation, ceilinged by the caller.

    `base_confidence` says how much this class of evidence is worth at all,
    and the strength of one instance only ever scales *within* it. That is
    the structural version of the honesty requirement: a 40-sigma print
    sweeping nine levels is still just flow, and if the operator has decided
    flow is worth 0.3 then this returns at most 0.3.
    """
    base = min(1.0, max(0.0, base_confidence))
    if base <= 0.0:
        return 0.0

    outlier = zscore is not None and zscore >= zscore_threshold
    strength = 0.0
    if outlier and zscore is not None:
        # Saturating deliberately. Because sizes are right-skewed, a z of 12
        # is not four times the evidence of a z of 3 — past a point the extra
        # sigmas are the normality assumption failing, not the trade getting
        # more meaningful.
        strength = max(strength, min(1.0, 0.5 + 0.10 * (zscore - zscore_threshold)))
    if sweep is not None:
        strength = max(strength, min(1.0, 0.6 + 0.10 * (sweep.levels - min_levels)))
    if outlier and sweep is not None:
        # Size and aggression together are a slightly better story than
        # either alone, and that is all the corroboration is worth.
        strength = min(1.0, strength + 0.15)
    return base * strength


def analyse(
    trades: Sequence[Trade],
    *,
    zscore_threshold: float,
    window_sec: float,
    min_levels: int,
    base_confidence: float,
) -> FlowEvent | None:
    """Report notable flow on a tape, or None when there is none to report.

    An event is raised when either the largest print is a size outlier past
    `zscore_threshold`, or a sweep is present. Both may hold; `sweep` on the
    result is what tells them apart.

    Returns None on an empty or single-trade tape. One print is not unusual
    relative to anything, and the baseline for judging it would be itself.
    """
    ordered = _ordered(trades)
    if len(ordered) < 2:
        return None

    usable = [t for t in ordered if _usable(t)]
    candidate: Trade | None = None
    zscore: float | None = None
    if usable:
        # The largest print on the tape, not the newest. A whale two trades
        # ago is the same observation as a whale just now, and scoring only
        # the newest print would make the answer depend on when the caller
        # happened to ask. Ties go to the more recent print.
        idx = max(
            range(len(usable)), key=lambda i: (usable[i].count, _utc(usable[i].ts))
        )
        candidate = usable[idx]
        # The candidate is excluded from its own baseline: leaving it in
        # drags the mean towards itself and shrinks the very deviation being
        # measured, most severely on the short tapes where it matters most.
        baseline = [t.count for i, t in enumerate(usable) if i != idx]
        zscore = size_zscore(baseline, candidate.count)

    outlier = zscore is not None and zscore >= zscore_threshold
    sweep = detect_sweep(ordered, window_sec=window_sec, min_levels=min_levels)

    observations: list[str] = []
    if sweep is None:
        if not outlier or candidate is None:
            return None
        # A size outlier with an unreadable taker side is still a real
        # observation — that much volume did print — but its direction is
        # not known and is reported as not known.
        direction = _direction(candidate.taker_side) or UNKNOWN
        contracts = candidate.count
    else:
        direction = sweep.direction
        contracts = sweep.contracts
        observations.append(
            f"{sweep.direction} sweep of {sweep.contracts} contracts through "
            f"{sweep.levels} levels, {sweep.first_price} to {sweep.last_price}, "
            f"in {sweep.seconds:.1f}s"
        )

    if outlier and candidate is not None and zscore is not None:
        observations.append(
            f"largest print {candidate.count} contracts at {candidate.yes_price}, "
            f"{zscore:.1f} sigma above the tape's mean size"
        )

    return FlowEvent(
        direction=direction,
        contracts=contracts,
        zscore=zscore,
        sweep=sweep,
        confidence=_confidence(
            base_confidence=base_confidence,
            zscore=zscore,
            sweep=sweep,
            zscore_threshold=zscore_threshold,
            min_levels=min_levels,
        ),
        rationale="; ".join(observations) + ". " + _CAVEAT,
    )

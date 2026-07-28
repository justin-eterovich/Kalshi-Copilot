"""Which picks a tuning run is allowed to look at.

Two boundaries, and the gap between them is the whole idea:

    T-48h ────────── T-24h ══════════════════ T (now)
      [   harvested   ]      [ seasoning ]     ^ marked here

Picks are taken from ``[start, end)`` and marked at ``as_of``. The lag between
``end`` and ``as_of`` is not slack — it is the time a pick is given to be
wrong. A detector scored the instant it fires is scored against the book it
just read, which reports its own quote back at it and calls that an outcome.

**Half-open on purpose.** ``[start, end)`` means consecutive runs partition the
timeline exactly: a signal written at precisely ``end`` belongs to the *next*
run and to no other. A closed interval would double-count it, and a
double-counted pick is one that votes twice on a threshold change.

Pure, and the clock is a parameter. A window function that reads
``datetime.now()`` cannot be tested at the boundary that matters, and the
boundary is the only interesting part.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "DEFAULT_SEASONING",
    "DEFAULT_SPAN",
    "TuningWindow",
    "window_for",
]

#: How long a pick is left alone before it is marked.
#:
#: Judgement call, and the shakiest number in this module. Twenty-four hours is
#: long enough that the book has moved on from whatever the detector reacted to
#: — which is the failure this exists to prevent — and short enough to give the
#: operator daily feedback. It is *not* long enough for most picks to settle:
#: the watchlist is dominated by NFL and election markets closing weeks out, so
#: at this seasoning nearly every pick is marked to market rather than resolved.
#: See ``tuning/coverage.py``, which refuses a run holding no settled outcome at
#: all, and the open question in ``docs/m10-detector-tuner-plan.md``.
DEFAULT_SEASONING: Final = timedelta(hours=24)

#: How much of the timeline one run harvests.
#:
#: Twenty-four hours, so consecutive daily runs tile the timeline without gap
#: or overlap. Widening this without changing the run cadence means picks are
#: scored more than once and the journal stops being able to attribute a change
#: to a window.
DEFAULT_SPAN: Final = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class TuningWindow:
    """The half-open harvest interval and the instant it is marked at."""

    start: datetime
    end: datetime
    as_of: datetime

    def __post_init__(self) -> None:
        for label, ts in (
            ("start", self.start),
            ("end", self.end),
            ("as_of", self.as_of),
        ):
            if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
                raise ValueError(
                    f"TuningWindow.{label} is a naive datetime ({ts!r}). A "
                    f"naive timestamp is not a timestamp with a missing "
                    f"annotation, it is an instant nobody can place; coercing "
                    f"one shifts the whole window by the operator's local "
                    f"offset."
                )
        if self.end <= self.start:
            raise ValueError(
                f"TuningWindow is empty or inverted: start={self.start.isoformat()} "
                f"end={self.end.isoformat()}."
            )
        if self.as_of < self.end:
            raise ValueError(
                f"TuningWindow.as_of ({self.as_of.isoformat()}) precedes the end "
                f"of the harvest window ({self.end.isoformat()}). Marking a pick "
                f"before its window has closed scores it against a book that has "
                f"not moved yet, which is the look-ahead this lag exists to "
                f"prevent, pointed backwards."
            )

    @property
    def span(self) -> timedelta:
        return self.end - self.start

    @property
    def seasoning(self) -> timedelta:
        """How long the newest pick in the window had to be wrong."""
        return self.as_of - self.end

    def contains(self, ts: datetime) -> bool:
        """Half-open membership: ``start <= ts < end``."""
        return self.start <= ts < self.end

    def describe(self) -> str:
        return (
            f"{self.start.isoformat()} .. {self.end.isoformat()} "
            f"(marked at {self.as_of.isoformat()}, "
            f"seasoned {self.seasoning.total_seconds() / 3600:.1f}h)"
        )


def window_for(
    now: datetime,
    *,
    seasoning: timedelta = DEFAULT_SEASONING,
    span: timedelta = DEFAULT_SPAN,
) -> TuningWindow:
    """The window a run starting at ``now`` should harvest.

    ``now`` is required rather than read from the clock — see the module
    docstring. Both durations are parameters because the right seasoning is an
    open question this deployment cannot answer yet, and a value that can only
    be changed by editing a constant is a value nobody sweeps.
    """
    if seasoning < timedelta(0):
        raise ValueError(
            f"seasoning must not be negative, got {seasoning!r}. A negative "
            f"seasoning marks picks before they were made."
        )
    if span <= timedelta(0):
        raise ValueError(f"span must be positive, got {span!r}.")

    end = now - seasoning
    return TuningWindow(start=end - span, end=end, as_of=now)

"""When the trading window on a scheduled economic release shuts.

**The dangerous version of this module is a "catalyst detector"**: something
that notices CPI lands at 12:30 UTC, watches for the print, and computes an
edge from the gap between the number and the market's last price. That module
would fire constantly and would never once describe a trade anybody could
make, because of a fact worth stating plainly:

**Kalshi closes these markets *before* the data is published.**

Checked against the live catalog on 2026-07-27::

    KXCPI          closes 2026-08-12 12:25 UTC   CPI  published 12:30 UTC
    KXPAYROLLS     closes 2026-09-04 12:29 UTC   NFP  published 12:30 UTC
    KXGDP          closes 2026-07-30 12:29 UTC   GDP  published 12:30 UTC
    KXFEDDECISION  closes 2026-07-29 17:59 UTC   FOMC published 18:00 UTC
    KXADP          closes 2026-09-02 12:14 UTC   ADP  published 12:15 UTC

There is no "trade the news" window on these markets. By the time the number
exists, trading stopped one to five minutes ago. Any edge computed from the
print is an edge on a book that cannot be hit, and the confirmation you would
get from backtesting it is entirely fictional.

So this module refuses to be a detector. It computes no edge, no fair value
and no expected value — it does not even import :class:`~decimal.Decimal`,
which is the structural version of that promise, the same discipline as
``undervalued_screener``. A calendar tells you *when*, never *whether*.

What it does instead is the honest and genuinely useful thing: it reports the
**deadline**. A market closing in forty minutes ahead of CPI is a position
that has to be opened now or not at all, and that is a fact an operator can
act on. :data:`WindowState.CLOSED_PENDING_SETTLEMENT` exists precisely so the
UI can say *"you have missed this"* rather than *"here is an edge"* — it is
the state a naive catalyst detector would treat as its signal, and
:attr:`Catalyst.actionable` is False there on purpose.

Everything here is pure: no clock reads, no I/O, no database. ``now`` is a
required argument so that the caller owns the notion of the present and the
behaviour is reproducible under test.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

__all__ = [
    "KNOWN_CATALYSTS",
    "Catalyst",
    "WindowState",
    "catalyst_for",
    "expected_release_time",
    "upcoming",
]


class WindowState(StrEnum):
    """Where a market sits relative to its own trading deadline."""

    #: Trading, with more than ``closing_soon_minutes`` left.
    OPEN = "open"
    #: Trading, but the deadline is inside the caller's warning horizon.
    CLOSING_SOON = "closing_soon"
    #: Past close, not yet settled. **This is not an opportunity.** It is the
    #: window in which the release becomes public while the book is already
    #: untradeable — the exact state a naive catalyst detector fires on. It is
    #: modelled explicitly so the UI can render "missed" instead of "edge".
    CLOSED_PENDING_SETTLEMENT = "closed_pending_settlement"
    #: Settled. The caller tells us; we cannot observe it from a close time.
    SETTLED = "settled"


#: States in which the operator can still actually place an order.
_ACTIONABLE_STATES = frozenset({WindowState.OPEN, WindowState.CLOSING_SOON})


@dataclass(frozen=True, slots=True)
class Catalyst:
    """A scheduled release and the deadline it imposes, never an edge."""

    series_ticker: str
    #: Human name for the release, e.g. "CPI". From :data:`KNOWN_CATALYSTS`.
    label: str
    #: Timezone-aware UTC. The moment trading stops.
    close_time: datetime
    #: **Inferred, not authoritative.** See :func:`expected_release_time`.
    #: ``None`` when the close time does not match the known convention.
    #: This is never a settlement time and must not be displayed as one.
    expected_release: datetime | None
    state: WindowState
    #: Signed: positive before the close, negative after, so a UI can render
    #: both "closes in 40 min" and "closed 12 min ago" from one number.
    minutes_to_close: float

    @property
    def actionable(self) -> bool:
        """Whether the operator can still trade this.

        True only while the book is open. Deliberately False in
        :attr:`WindowState.CLOSED_PENDING_SETTLEMENT` even though that is the
        moment the number becomes public — public and tradeable are different
        things here, and conflating them is the whole failure mode this
        module exists to avoid.
        """
        return self.state in _ACTIONABLE_STATES


#: Series ticker -> human label for scheduled data releases.
#:
#: Every entry is a **claim**, in the same sense as
#: ``app.weather.stations.SERIES_STATIONS``: the API does not tell us that a
#: series tracks a scheduled publication, so a human asserted it after reading
#: the series title and confirming its close time sits just before a known
#: release slot. Verified against the live catalog on 2026-07-27.
#:
#: An unlisted series is **refused**, not defaulted. There is no such thing as
#: a generic release time, and inventing one would attach a confident,
#: plausible, wrong timestamp to an arbitrary market.
KNOWN_CATALYSTS: dict[str, str] = {
    # -- US CPI family: all print together at 08:30 ET with the CPI report --
    "KXCPI": "CPI",
    "KXCPICORE": "Core CPI",
    "KXCPIYOY": "CPI year-over-year",
    "KXCPICOREYOY": "Core CPI year-over-year",
    "KXCPICOMBO": "CPI headline and core combination",
    "KXCPICOREHEAD": "Core vs headline CPI",
    "KXCPINDEX": "CPI index level",
    "KXECONSTATCPI": "CPI month-over-month",
    "KXECONSTATCPICORE": "Core CPI month-over-month",
    "KXECONSTATCPIYOY": "CPI year-over-year",
    "KXECONSTATCORECPIYOY": "Core CPI year-over-year",
    # Component series of the same BLS report; they close at :29 alongside it.
    "KXAIRFARECPI": "CPI airline fares",
    "KXSHELTERCPI": "CPI shelter",
    "KXUSEDCARCPI": "CPI used cars and trucks",
    "KXUSGASCPI": "CPI gasoline",
    # -- US labour ------------------------------------------------------
    "KXPAYROLLS": "Nonfarm payrolls",
    "KXLFPRATE": "Labor force participation rate",
    "KXADP": "ADP employment change",
    "KXJOBLESSCLAIMS": "Initial jobless claims",
    # -- US output and prices -------------------------------------------
    "KXGDP": "Real GDP",
    "KXNGDPQ": "Nominal GDP",
    "KXPCECORE": "Core PCE",
    "KXUSPPIYOY": "PPI year-over-year",
    "KXUSRETAIL": "US retail sales",
    # -- FOMC: statement drops 14:00 ET, books close 17:55/17:59 UTC -----
    "KXFED": "Federal funds rate (upper bound)",
    "KXFEDDECISION": "FOMC rate decision",
    "KXFEDCOMBO": "FOMC decision and dissents",
    "KXFEDDISSENT": "FOMC dissents",
    "KXFOMCDISSENTCOUNT": "FOMC dissent count",
    "KXFOMCGUIDE": "FOMC statement guidance",
    # -- Non-US statistical agencies, same convention in local slots -----
    "KXDEGDPQOQF": "Germany GDP QoQ (flash)",
    "KXDEGDPYOYF": "Germany GDP YoY (flash)",
    "KXFRGDPQOQP": "France GDP QoQ (preliminary)",
    "KXFRGDPYOYP": "France GDP YoY (preliminary)",
    "KXITGDPQOQA": "Italy GDP QoQ (advance)",
    "KXITGDPYOYA": "Italy GDP YoY (advance)",
    "KXBRAZILGDP": "Brazil GDP YoY",
    "KXSAGDPQOQ": "South Africa GDP QoQ",
    "KXSARETAIL": "South Africa retail sales MoM",
    "KXUKRETAIL": "UK retail sales MoM",
    "KXARMOMINF": "Argentina inflation MoM",
}

# Deliberately NOT listed, though their close times fit the arithmetic:
#
# * KXDXYFOMC / KXSPXFOMC / KX2YFOMC — these settle on how much a *price*
#   moved around the FOMC, so their close marks the end of a measurement
#   window, not a publication. Reporting an "expected release" for them would
#   name a timestamp at which nothing is published.
# * KXGDPYEAR, KXGDPUSMAX, KXHIGHINFLATION, KXLCPIMAXYOY, KXMORTGAGERATE —
#   year-long aggregates whose close lands near a release only because the
#   final print resolves them. The deadline is real; calling it a catalyst
#   release would overstate what we know.
#
# Both exclusions are the fail-closed choice: a missing entry costs a
# calendar row, a wrong entry costs a confident lie about the clock.

#: Release slots observed in the catalog are always on a quarter hour — 12:30
#: (08:30 ET), 12:15 (08:15 ET), 18:00 (14:00 ET), and the European 05:30 /
#: 07:00 / 08:00 / 11:00 equivalents. Nothing lands off a :00/:15/:30/:45.
_SLOT_MINUTES = 15

#: Largest observed gap between a market close and the release that follows
#: it: five minutes (KXCPI at :25 into a :30 print). A close further than this
#: from the next slot is not following the pre-release convention, and we
#: refuse rather than stretch the rule to fit.
_MAX_LEAD_MINUTES = 5


def _as_utc(ts: datetime) -> datetime | None:
    """Normalise an aware datetime to UTC, or refuse a naive one.

    A naive datetime is refused rather than assumed to be UTC. Everything in
    this module is a wall-clock deadline compared against another wall-clock
    instant; guessing the zone on one side is an off-by-hours error that
    produces a perfectly plausible countdown to the wrong minute.
    """
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        return None
    return ts.astimezone(UTC)


def _next_slot(ts: datetime) -> datetime:
    """The first quarter-hour boundary strictly after ``ts``.

    Strictly after, so a close sitting exactly on a boundary does not claim
    the boundary it is standing on as its release.
    """
    floor = ts.replace(minute=(ts.minute // _SLOT_MINUTES) * _SLOT_MINUTES,
                       second=0, microsecond=0)
    return floor + timedelta(minutes=_SLOT_MINUTES)


def expected_release_time(
    close_time: datetime,
    *,
    series_ticker: str,
) -> datetime | None:
    """Best estimate of when the data actually publishes.

    **This is inferred from the close time and is not published by the API.**
    Kalshi exposes no release timestamp anywhere on the market or the series;
    what it exposes is a close time that, on every scheduled release in the
    catalog, sits one to five minutes before the release slot. This function
    reads that convention backwards.

    Treat the result as an annotation on a deadline, never as ground truth and
    never as a settlement time — settlement happens later, separately, and
    nothing here observes it.

    Returns ``None`` — a refusal the caller must honour by showing no release
    time at all — when:

    * ``series_ticker`` is not in :data:`KNOWN_CATALYSTS`. An unrecognised
      series is not an invitation to assume a default release time.
    * ``close_time`` is naive. See :func:`_as_utc`.
    * ``close_time`` is more than five minutes before the next quarter-hour
      slot, meaning it does not follow the observed pre-release convention.
      A close at 15:00 tells us nothing about a publication time, and saying
      "15:15" would be arithmetic dressed up as evidence.
    """
    if _lookup_label(series_ticker) is None:
        return None
    close_utc = _as_utc(close_time)
    if close_utc is None:
        return None

    slot = _next_slot(close_utc)
    lead_minutes = (slot - close_utc).total_seconds() / 60.0
    if lead_minutes > _MAX_LEAD_MINUTES:
        return None
    return slot


def _lookup_label(series_ticker: str | None) -> str | None:
    """Exact, case-normalised lookup of a claimed catalyst series.

    Exact on purpose. A prefix match would be actively harmful: ``KXFED``
    is the funds-rate series but ``KXFEDTWEETS``, ``KXFEDEND`` and
    ``KXFEDERALCHARGE`` are all live series about entirely different things,
    and ``startswith("KXFED")`` would hand every one of them a fabricated
    FOMC release time. Series tickers are names, not a namespace.
    """
    if not series_ticker:
        return None
    return KNOWN_CATALYSTS.get(series_ticker.strip().upper())


def catalyst_for(
    *,
    series_ticker: str,
    close_time: datetime,
    now: datetime,
    closing_soon_minutes: int = 60,
    settled: bool = False,
) -> Catalyst | None:
    """Describe the trading deadline on a scheduled release, or refuse.

    ``now`` is required rather than read from the clock: this module is pure,
    and a deadline calculation that silently depends on wall time is one that
    cannot be tested at the boundary.

    Returns ``None`` when:

    * the series is not a claimed catalyst (:data:`KNOWN_CATALYSTS`),
    * either datetime is naive,
    * ``closing_soon_minutes`` is negative, which has no meaning as a horizon.

    The state boundaries, all of which are tested:

    * ``now == close_time`` exactly is **closed**. At the close instant
      trading has stopped, and rounding that in the operator's favour would
      offer a trade that no longer exists.
    * ``minutes_to_close == closing_soon_minutes`` exactly is
      ``CLOSING_SOON`` — the warning is inclusive, because a warning that
      arrives late is not a warning.
    * ``settled`` wins over everything. It is the caller's observation; a
      close time in the past is evidence of closure, never of settlement.
    """
    label = _lookup_label(series_ticker)
    if label is None:
        return None
    if closing_soon_minutes < 0:
        return None

    close_utc = _as_utc(close_time)
    now_utc = _as_utc(now)
    if close_utc is None or now_utc is None:
        return None

    minutes_to_close = (close_utc - now_utc).total_seconds() / 60.0

    if settled:
        state = WindowState.SETTLED
    elif minutes_to_close <= 0:
        state = WindowState.CLOSED_PENDING_SETTLEMENT
    elif minutes_to_close <= closing_soon_minutes:
        state = WindowState.CLOSING_SOON
    else:
        state = WindowState.OPEN

    return Catalyst(
        series_ticker=series_ticker.strip().upper(),
        label=label,
        close_time=close_utc,
        expected_release=expected_release_time(close_utc, series_ticker=series_ticker),
        state=state,
        minutes_to_close=minutes_to_close,
    )


def upcoming(catalysts: Sequence[Catalyst], *, limit: int = 20) -> list[Catalyst]:
    """Order catalysts for display: actionable first, then soonest first.

    The primary key is :attr:`Catalyst.actionable`, not time, because the
    whole point of the list is the deadline. A market that closed four minutes
    ago is chronologically nearer than one closing in an hour and is worth
    strictly less attention — it belongs below the fold, not at the top.

    Within each group the order is by close time ascending, so the tightest
    live deadline leads. ``limit <= 0`` returns an empty list rather than
    slicing from the end, which is what a bare ``[:limit]`` would quietly do
    for a negative value.
    """
    if limit <= 0:
        return []
    ordered = sorted(catalysts, key=lambda c: (not c.actionable, c.close_time))
    return ordered[:limit]

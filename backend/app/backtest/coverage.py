"""Does the data behind a backtest mean anything at all?

The obvious failure mode of a backtester is arithmetic: it computes P&L
wrongly. That one announces itself. The dangerous failure mode is the
opposite — **it computes P&L perfectly correctly over data far too thin to
mean anything, and prints a number that looks like evidence.** A Sharpe ratio
carries no marker of the sample it came from. Nothing downstream of the
replay loop can tell 10 hours of book data on 79 markets from three years on
twenty thousand; both arrive as a float.

This module is the gate in front of that. It takes a per-market summary of
what we actually hold, measures it, and **refuses** — naming every measured
number against the threshold it missed.

The state of this deployment's database, measured on 2026-07-27::

    markets in catalog        217,211
    markets with a result      63,403
    orderbook snapshots         1,873 rows, 79 tickers, spanning 9.8 hours
    candles                     8,650 rows, 34 tickers, ~22 hours
    tape prints                23,577
    fills ever executed             2
    settlements recorded            0

A backtest run today therefore replays **ten hours of book data on 79
markets, none of which has settled**. Any hit rate, expectancy or Sharpe out
of that is noise wearing a suit. The right answer today is "no", and this
module is where that answer is computed rather than remembered.

**Why thresholds and not a hardcoded "not yet".** The operator is
accumulating data continuously. A flag saying "M9 is not ready" would have to
be found and flipped by a human who correctly judged that the data had
matured — which is the judgement this module exists to make. Expressed as
thresholds over measured coverage, the same code that refuses today becomes
quietly useful in some months with no edit, and keeps refusing on the
specific axis that is still short. That is also why every refusal carries its
measurement: "insufficient data" tells an operator nothing, while "79 markets
against a floor of 200" tells them whether to wait a week or change the
config.

**Clearing these floors is necessary, not sufficient.** None of these numbers
certifies a result. A hit rate over 200 settled outcomes still carries a 95%
band of roughly ±7 percentage points, which is wider than any edge the
detectors claim to find. The floors mark where a result is *certainly* noise,
not where it becomes trustworthy. Passing this gate means the obvious ways of
fooling yourself have been excluded; it does not mean the backtest is right.

**Purity.** Standard library only, no I/O, and the evaluation window is a
parameter — there is no ``datetime.now()`` here. A gate that reads the clock
cannot be tested for the boundary case that matters. The caller (the database
layer) reduces each market to one :class:`MarketCoverage` row and passes them
in.

**One naming note for the caller.** The outcome field is
``resolved_outcome: bool | None``, not ``settled``. A tri-state field named
``settled`` holding ``False`` for "settled NO" is precisely the guard-that-
inspects-the-wrong-thing failure CLAUDE.md warns about: someone writes
``if m.settled:`` and silently drops every NO-resolved market, halving the
outcome sample and biasing what remains toward YES. Under the name
``resolved_outcome`` that line does not typecheck as intent. Use
:attr:`MarketCoverage.is_settled` to ask the settled question.

No money appears in this module, so nothing here is a ``Decimal``. Fractions
(window fill, series concentration) are plain floats: they are diagnostics
for a human, never inputs to a cost.
"""

from __future__ import annotations

import enum
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Final

__all__ = [
    "MAX_GAP_FLOOR",
    "MAX_GAP_MULTIPLE",
    "MAX_SERIES_CONCENTRATION",
    "MIN_DISTINCT_SERIES",
    "MIN_MARKETS",
    "MIN_OBSERVED_SPAN",
    "MIN_SETTLED_MARKETS",
    "MIN_WINDOW_FILL",
    "UNKNOWN_SERIES",
    "CoverageRefused",
    "CoverageReport",
    "Finding",
    "MarketCoverage",
    "RefusalCode",
    "Severity",
    "assert_usable",
    "audit",
    "gap_tolerance",
]


# ---------------------------------------------------------------------------
# Thresholds
#
# Every one of these is a judgement call, and each comment says so where it is
# one. This codebase prefers "here is a guess and its reasoning" to a bare
# literal, because a bare literal cannot be argued with when the operator
# wants to know whether waiting another month would help.
# ---------------------------------------------------------------------------

#: Minimum distinct markets in the sample.
#:
#: Judgement call. The reasoning: a backtest is scored per market, and the
#: standard error of a hit rate is about ``1 / (2 * sqrt(N))``. At 200 markets
#: the 95% band on a hit rate is roughly ±7 percentage points — still far too
#: wide to separate a 55% strategy from a coin flip, but at least the same
#: order as the effect. Below 200 the result is dominated by which handful of
#: markets happened to be in the watchlist, and you are measuring those
#: markets rather than a strategy. Today's sample is 79.
MIN_MARKETS: Final = 200

#: Minimum markets whose outcome is known.
#:
#: Outcomes are the only ground truth a backtest has. A market still open
#: contributes a cost basis and no result: it can lose money in the replay and
#: can never be shown to have made any. In practice this is the binding
#: constraint — this deployment holds 63,403 settled markets in the *catalog*
#: but has recorded **zero** settlements and only two fills, so a replay
#: window drawn from observed book data contains almost no resolved outcomes.
#:
#: Deliberately lower than :data:`MIN_MARKETS`, which looks backwards until
#: you notice it is forced: settlement lags observation. Requiring the two to
#: be equal would mean a window could only pass once nearly everything in it
#: had resolved — which is exactly the all-settled sample that
#: :attr:`RefusalCode.SURVIVORSHIP` refuses. The two floors have to leave room
#: between them or they contradict each other.
MIN_SETTLED_MARKETS: Final = 100

#: Minimum *observed* span (not the requested window).
#:
#: Judgement call, and on the generous side. Thirty days contains one CPI
#: print and roughly one FOMC meeting — a sample size of one on the events
#: most likely to move this exchange — plus four weekly cycles. It is the
#: point below which a window cannot contain a regime at all, not the point at
#: which it contains enough of them. Ninety days is what you would actually
#: want. Today's span is 9.8 hours, which is short by a factor of about 73.
MIN_OBSERVED_SPAN: Final = timedelta(days=30)

#: Tolerated observation gap, as a multiple of the caller's expected interval.
#:
#: Judgement call. Twenty missed periods is a websocket reconnect or an ingest
#: restart, not a broken dataset. It is a multiple rather than an absolute
#: duration because the same code audits 1-second book snapshots and 1-minute
#: candles, and "20 seconds" and "20 minutes" are the same defect at those two
#: cadences. Note the limitation at *low* cadence: against a daily feed this
#: tolerates 20 missing days, which is far too loose — such a caller should
#: pass a smaller ``max_gap_multiple``.
MAX_GAP_MULTIPLE: Final = 20

#: Absolute floor on the tolerated gap, whatever the cadence.
#:
#: Judgement call. At a 1-second cadence the multiple alone would refuse on
#: any 21-second hiccup, which is normal operation and does not measurably
#: distort a replay. This stops the rule being absurdly strict on high-cadence
#: feeds. Tolerance is ``max(interval * multiple, floor)`` — see
#: :func:`gap_tolerance`.
MAX_GAP_FLOOR: Final = timedelta(minutes=5)

#: Minimum distinct series tickers in the sample.
#:
#: This is the literal degenerate-case floor and nothing more. Two series is
#: not a portfolio; the check catches only the sample that is entirely one
#: event's idiosyncrasy. CLAUDE.md is emphatic that the series ticker is the
#: only field that says what a market *tracks* — ``category`` does not, which
#: is how the stale-quote detector once priced ETH contracts against Bitcoin
#: spot — so series is the right axis to count on, but counting to 2 certifies
#: nothing. Real diversity is what :attr:`RefusalCode.SERIES_CONCENTRATION`
#: warns about, because a hard gate at 2 is satisfied by adding one market.
MIN_DISTINCT_SERIES: Final = 2

#: Warn when one series holds more than this share of the sample.
#:
#: Judgement call. At 80% the other series are rounding error and the result
#: describes one series' behaviour whatever the distinct count says.
MAX_SERIES_CONCENTRATION: Final = 0.80

#: Warn when the observed span covers less than this fraction of the window.
#:
#: Judgement call. Below 75% the label on the result is wrong — a run
#: described as "2024-2026" that holds six months of data is a
#: misrepresentation even when every absolute floor is cleared.
MIN_WINDOW_FILL: Final = 0.75

#: Bucket for markets whose series ticker is unknown. They group together,
#: which is the conservative direction: unknown series can only make a sample
#: look *less* diverse, never more.
UNKNOWN_SERIES: Final = "<unknown>"


class Severity(enum.StrEnum):
    """Whether a finding blocks a backtest or merely annotates it."""

    REFUSAL = "refusal"
    WARNING = "warning"


class RefusalCode(enum.StrEnum):
    """Every finding this module can produce.

    Warnings share the enum with refusals so a caller has one vocabulary to
    switch on; :attr:`Finding.severity` is what says whether a finding blocks.
    """

    #: Nothing at all in the window. Reported alone — see :func:`audit`.
    NO_DATA = "no_data"

    #: Too few distinct markets. A backtest over a handful of markets measures
    #: those markets, not a strategy: their liquidity, their series, their
    #: particular resolution luck.
    TOO_FEW_MARKETS = "too_few_markets"

    #: The *observed* span is too short. Measured on the data actually held,
    #: never on the window the caller asked for — asking for three years does
    #: not produce three years. Ten hours cannot contain a regime; it can
    #: barely contain a news cycle.
    WINDOW_TOO_SHORT = "window_too_short"

    #: Too few markets with a known outcome. Outcomes are the only ground
    #: truth; markets still open contribute a cost basis and no result. This
    #: is the binding constraint in practice.
    TOO_FEW_SETTLED = "too_few_settled"

    #: The typical market is sampled far more sparsely than the caller's
    #: expected interval implies. Ingest restarts, watchlist churn and
    #: websocket drops leave holes, and a replay across a hole silently
    #: assumes the book did not move — the same failure as the orderbook
    #: sequence-gap guard, which marks a book stale rather than interpolating
    #: across the gap. A book that guesses across a gap looks plausible and is
    #: wrong, which is exactly how a backtest talks you into a strategy that
    #: does not exist.
    GAPS_TOO_LARGE = "gaps_too_large"

    #: **Every** market in the sample has settled.
    #:
    #: This is the least obvious refusal here, because it sounds like the good
    #: case — full ground truth, no open positions to mark. It is a trap. A
    #: sample in which everything resolved inside the window was, by
    #: construction, drawn from markets that *resolve quickly*: short-dated
    #: ones, dailies, hourlies. Every long-dated market that was open and
    #: tradeable throughout the window is absent, because it had not resolved
    #: when the sample was taken. Scoring a strategy on that slice scores it on
    #: short-dated markets and reports the number as if it were the strategy's.
    #:
    #: The refusal cannot distinguish this from the legitimate case — a window
    #: far enough in the past that everything in it has genuinely matured —
    #: because both look identical in a summary row. So it fails closed and
    #: names what the operator must confirm: that the sample was selected by
    #: *observation window* and not by *resolution date*. The message reports
    #: how many markets closed inside the window as supporting evidence.
    SURVIVORSHIP = "survivorship"

    #: Every market shares one series ticker. That is one event's
    #: idiosyncrasy, not a portfolio.
    SINGLE_SERIES = "single_series"

    # -- warnings -----------------------------------------------------------

    #: The observed span covers only part of the requested window. Informative
    #: rather than blocking: the absolute floors above are the real gate, and
    #: a caller who asked for a generous window is not thereby wrong. What
    #: this catches is the *label* — a result presented as covering the
    #: requested window when it covers a slice of it.
    PARTIAL_WINDOW = "partial_window"

    #: No market supplied a measured ``max_gap``, so gaps are implied means
    #: (span / (observations - 1)) and the true worst gap may be far larger: a
    #: market with one six-hour hole and otherwise perfect sampling shows a
    #: small mean. A warning rather than a refusal because the implied mean is
    #: a strictly weaker test that still refuses in the common case — today's
    #: data trips :attr:`GAPS_TOO_LARGE` on means alone — and refusing here
    #: would block every caller who has not yet written the window function.
    #: It tells the operator the gap numbers are a lower bound.
    GAP_ESTIMATE_IMPRECISE = "gap_estimate_imprecise"

    #: Some individual market has a gap beyond tolerance while the typical
    #: market is fine. Warns rather than refuses because the refusal is
    #: deliberately keyed to the *median*: one market's ingest hiccup should
    #: not void a whole run, whereas a median beyond tolerance means the
    #: typical market is undersampled, which is the condition that makes a
    #: replay meaningless. The worst gap is reported so the operator can go
    #: look at that market.
    ISOLATED_GAPS = "isolated_gaps"

    #: One series dominates the sample even though more than one is present.
    SERIES_CONCENTRATION = "series_concentration"


@dataclass(frozen=True, slots=True)
class Finding:
    """One measured deficiency, rendered for both a CLI and a dashboard.

    ``measured`` and ``threshold`` are pre-formatted strings rather than raw
    numbers because the values are heterogeneous — counts, durations,
    fractions — and every consumer of this type displays them rather than
    computing on them. ``message`` is the full sentence; the two fields are
    for a panel that wants to lay the comparison out itself.
    """

    code: RefusalCode
    severity: Severity
    message: str
    measured: str
    threshold: str

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.REFUSAL

    def render(self) -> str:
        return f"{self.severity.upper()} {self.code}: {self.message}"


def _require_utc(ts: datetime, label: str) -> None:
    """Reject a naive datetime rather than assuming what it meant.

    A naive timestamp is not a timestamp with a missing annotation, it is an
    instant nobody can place. Coercing one to UTC is a guess that silently
    shifts an entire dataset by the operator's local offset — which, for a
    coverage span measured in hours, is the difference between passing and
    failing this gate.
    """
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(
            f"{label} is a naive datetime ({ts!r}). All timestamps crossing "
            f"this boundary must be timezone-aware UTC; a naive one is a hard "
            f"error rather than something to coerce, because assuming a zone "
            f"shifts the whole sample by an unknown offset."
        )


@dataclass(frozen=True, slots=True)
class MarketCoverage:
    """What we hold for one market inside the audited window.

    The caller (the database layer) produces one of these per market, already
    restricted to the window under audit. Rows are validated on construction:
    a coverage row that contradicts itself is an upstream bug, and repairing
    it here would launder that bug into a coverage number.
    """

    ticker: str
    series_ticker: str | None
    #: First observation held in the window.
    first_ts: datetime
    #: Last observation held in the window.
    last_ts: datetime
    #: Rows held for this market in the window. Must be at least 1 — a market
    #: with nothing observed should simply not be emitted.
    observations: int
    #: The resolved outcome: ``True`` for YES, ``False`` for NO, ``None`` for
    #: still open. **Not** a "has settled" flag — see the module docstring for
    #: why that naming matters. Ask :attr:`is_settled` instead.
    resolved_outcome: bool | None
    close_time: datetime | None = None
    #: Largest measured gap between consecutive observations, if the caller
    #: computed one (a ``lag()`` window function over the observation
    #: timestamps). ``None`` means gaps must be implied from counts, which
    #: raises :attr:`RefusalCode.GAP_ESTIMATE_IMPRECISE`.
    max_gap: timedelta | None = None

    def __post_init__(self) -> None:
        _require_utc(self.first_ts, f"{self.ticker}.first_ts")
        _require_utc(self.last_ts, f"{self.ticker}.last_ts")
        if self.close_time is not None:
            _require_utc(self.close_time, f"{self.ticker}.close_time")
        if self.last_ts < self.first_ts:
            raise ValueError(
                f"{self.ticker}: last_ts {self.last_ts.isoformat()} precedes "
                f"first_ts {self.first_ts.isoformat()}."
            )
        if self.observations < 1:
            raise ValueError(
                f"{self.ticker}: observations={self.observations}. A coverage "
                f"row with no observations is not coverage — omit the market "
                f"rather than emitting an empty row, so that it is absent from "
                f"the market count too."
            )
        if self.max_gap is not None and self.max_gap < timedelta(0):
            raise ValueError(f"{self.ticker}: max_gap is negative ({self.max_gap}).")

    @property
    def is_settled(self) -> bool:
        """Whether the outcome is known, either way."""
        return self.resolved_outcome is not None

    @property
    def span(self) -> timedelta:
        return self.last_ts - self.first_ts

    @property
    def series_key(self) -> str:
        return self.series_ticker or UNKNOWN_SERIES


def gap_tolerance(
    expected_interval: timedelta,
    *,
    multiple: int = MAX_GAP_MULTIPLE,
    floor: timedelta = MAX_GAP_FLOOR,
) -> timedelta:
    """Largest observation gap that does not invalidate a replay.

    ``max(interval * multiple, floor)`` — the multiple keeps the rule
    proportionate across cadences, the floor keeps it from being absurdly
    strict on a 1-second feed. See the constants for the defence of each.
    """
    if expected_interval <= timedelta(0):
        raise ValueError(
            f"expected_interval must be positive, got {expected_interval!r}."
        )
    if multiple < 1:
        raise ValueError(f"multiple must be at least 1, got {multiple!r}.")
    return max(expected_interval * multiple, floor)


def _fmt_unit(value: float, plural: str) -> str:
    text = f"{value:.3g}"
    return f"{text} {plural}" if text != "1" else f"{text} {plural[:-1]}"


def _fmt_duration(td: timedelta) -> str:
    """Human duration at a sensible unit. Display only."""
    seconds = td.total_seconds()
    if seconds < 0:
        return f"-{_fmt_duration(-td)}"
    if seconds < 90:
        return _fmt_unit(seconds, "seconds")
    minutes = seconds / 60
    if minutes < 90:
        return _fmt_unit(minutes, "minutes")
    hours = minutes / 60
    if hours < 48:
        return _fmt_unit(hours, "hours")
    return _fmt_unit(hours / 24, "days")


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_pct(fraction: float) -> str:
    return f"{fraction * 100:.3g}%"


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Measured coverage, plus every finding it produced.

    The report is always returned in full, refusals or not — the numbers are
    useful to an operator deciding whether to wait, and hiding them behind an
    exception would mean the only way to see coverage was to fail.
    """

    # -- the window asked for, and the one actually held -------------------
    window_start: datetime
    window_end: datetime
    observed_start: datetime | None
    observed_end: datetime | None

    # -- breadth ------------------------------------------------------------
    market_count: int
    series_count: int
    settled_count: int
    unsettled_count: int
    #: Share of the sample held by its largest series, 0.0 when empty.
    largest_series_share: float
    #: Markets that closed inside the audited window. Supporting evidence for
    #: :attr:`RefusalCode.SURVIVORSHIP`; ``None`` when no close times were
    #: supplied at all.
    closed_in_window: int | None

    # -- depth --------------------------------------------------------------
    total_observations: int
    expected_interval: timedelta
    tolerated_gap: timedelta
    median_gap: timedelta | None
    worst_gap: timedelta | None
    #: True when at least one market supplied a measured ``max_gap``. When
    #: False the gap figures are implied means and are a lower bound.
    gaps_measured: bool
    #: Markets holding a single observation, whose gap cannot be implied from
    #: counts and is taken to be the whole requested window.
    single_observation_markets: int

    refusals: tuple[Finding, ...]
    warnings: tuple[Finding, ...]

    @property
    def requested_span(self) -> timedelta:
        return self.window_end - self.window_start

    @property
    def observed_span(self) -> timedelta:
        """Span actually covered by data, which may be far shorter than asked."""
        if self.observed_start is None or self.observed_end is None:
            return timedelta(0)
        return self.observed_end - self.observed_start

    @property
    def window_fill(self) -> float:
        """Fraction of the requested window the data actually spans."""
        requested = self.requested_span.total_seconds()
        if requested <= 0:
            return 0.0
        return min(1.0, self.observed_span.total_seconds() / requested)

    @property
    def observations_per_market(self) -> float:
        if self.market_count == 0:
            return 0.0
        return self.total_observations / self.market_count

    @property
    def usable(self) -> bool:
        """Whether a backtest may run. Warnings do not block."""
        return not self.refusals

    @property
    def refusal_codes(self) -> tuple[RefusalCode, ...]:
        return tuple(f.code for f in self.refusals)

    @property
    def warning_codes(self) -> tuple[RefusalCode, ...]:
        return tuple(f.code for f in self.warnings)

    def summary_lines(self) -> list[str]:
        """Plain strings for a CLI or a dashboard panel.

        Deliberately renders the measurements *before* the findings: an
        operator reading this is usually deciding whether another month of
        ingest would help, and that is a question about the numbers rather
        than about which rule tripped.
        """
        gap_note = "" if self.gaps_measured else ", implied from counts (lower bound)"
        lines = [
            f"window requested: {_fmt_ts(self.window_start)} .. "
            f"{_fmt_ts(self.window_end)} ({_fmt_duration(self.requested_span)})",
            f"window observed:  {_fmt_ts(self.observed_start)} .. "
            f"{_fmt_ts(self.observed_end)} ({_fmt_duration(self.observed_span)}, "
            f"{_fmt_pct(self.window_fill)} of requested)",
            f"markets: {self.market_count} "
            f"({self.settled_count} settled, {self.unsettled_count} open, "
            f"{self.series_count} series, largest "
            f"{_fmt_pct(self.largest_series_share)} of sample)",
            f"observations: {self.total_observations} total, "
            f"{self.observations_per_market:.1f} per market",
            f"gaps: median "
            f"{_fmt_duration(self.median_gap) if self.median_gap else '-'}, "
            f"worst {_fmt_duration(self.worst_gap) if self.worst_gap else '-'}, "
            f"tolerated {_fmt_duration(self.tolerated_gap)}{gap_note}",
        ]
        lines.extend(f.render() for f in self.refusals)
        lines.extend(f.render() for f in self.warnings)
        lines.append(
            "VERDICT: usable" if self.usable else f"VERDICT: refused "
            f"({len(self.refusals)} refusal(s))"
        )
        return lines


class CoverageRefused(RuntimeError):
    """The data cannot support a backtest.

    Fail-closed by design, in the shape of :class:`UnverifiedFeeSchedule` in
    ``app/core/fees.py``: an unverified fee schedule makes every downstream
    edge figure untrustworthy, and thin coverage makes every downstream
    performance figure untrustworthy in exactly the same way — silently, and
    with a plausible number attached.

    **Every** refusal is carried, not just the first. An operator fixing them
    one at a time is an operator waiting a week per round trip, since the fix
    for most of them is "collect more data and come back".
    """

    def __init__(self, report: CoverageReport) -> None:
        self.report = report
        self.refusals: tuple[Finding, ...] = report.refusals
        self.codes: tuple[RefusalCode, ...] = report.refusal_codes
        detail = "\n".join(f"  - {f.message}" for f in report.refusals)
        super().__init__(
            f"Coverage is insufficient for a backtest "
            f"({len(report.refusals)} refusal(s)):\n{detail}"
        )


def assert_usable(report: CoverageReport) -> None:
    """Raise :class:`CoverageRefused` unless the report carries no refusals.

    The gate. Call it before a replay loop runs, not after — a backtest that
    produces a number and then discovers the number is meaningless has
    already put the number in front of a human.
    """
    if report.refusals:
        raise CoverageRefused(report)


def _finding(
    code: RefusalCode,
    severity: Severity,
    message: str,
    measured: str,
    threshold: str,
) -> Finding:
    return Finding(
        code=code,
        severity=severity,
        message=message,
        measured=measured,
        threshold=threshold,
    )


def audit(
    markets: Sequence[MarketCoverage],
    *,
    window_start: datetime,
    window_end: datetime,
    expected_interval: timedelta,
    min_markets: int = MIN_MARKETS,
    min_settled_markets: int = MIN_SETTLED_MARKETS,
    min_observed_span: timedelta = MIN_OBSERVED_SPAN,
    max_gap_multiple: int = MAX_GAP_MULTIPLE,
    max_gap_floor: timedelta = MAX_GAP_FLOOR,
    min_distinct_series: int = MIN_DISTINCT_SERIES,
    max_series_concentration: float = MAX_SERIES_CONCENTRATION,
    min_window_fill: float = MIN_WINDOW_FILL,
) -> CoverageReport:
    """Measure coverage and collect every finding, blocking or not.

    ``expected_interval`` is the cadence the caller believes the underlying
    feed has — 1 second for orderbook snapshots, 1 minute for candles. It is
    required rather than defaulted because there is no cadence that is right
    for both, and a wrong default would make the gap check silently
    meaningless in one direction or the other.

    Thresholds are keyword arguments over module-level defaults so a caller
    may loosen one deliberately and visibly. Loosening them does not make a
    thin backtest mean more; it makes the refusal stop saying so.

    Returns a report; it never raises on thin data. :func:`assert_usable` is
    the part that refuses, so that a caller can render coverage without
    handling an exception.
    """
    _require_utc(window_start, "window_start")
    _require_utc(window_end, "window_end")
    if window_end <= window_start:
        raise ValueError(
            f"window_end {window_end.isoformat()} must be after window_start "
            f"{window_start.isoformat()}."
        )
    tolerated = gap_tolerance(
        expected_interval, multiple=max_gap_multiple, floor=max_gap_floor
    )

    requested_span = window_end - window_start
    market_count = len(markets)
    total_observations = sum(m.observations for m in markets)

    # A window with nothing in it fails every check below trivially, and a
    # list of seven refusals buries the one that matters. Report it alone.
    if market_count == 0 or total_observations == 0:
        no_data = _finding(
            RefusalCode.NO_DATA,
            Severity.REFUSAL,
            f"No observations at all between {_fmt_ts(window_start)} and "
            f"{_fmt_ts(window_end)} ({market_count} market(s), "
            f"{total_observations} observation(s)). There is nothing to "
            f"replay; every other coverage check is vacuous.",
            measured=f"{market_count} markets / {total_observations} observations",
            threshold="any data at all",
        )
        return CoverageReport(
            window_start=window_start,
            window_end=window_end,
            observed_start=None,
            observed_end=None,
            market_count=market_count,
            series_count=0,
            settled_count=0,
            unsettled_count=0,
            largest_series_share=0.0,
            closed_in_window=None,
            total_observations=total_observations,
            expected_interval=expected_interval,
            tolerated_gap=tolerated,
            median_gap=None,
            worst_gap=None,
            gaps_measured=False,
            single_observation_markets=0,
            refusals=(no_data,),
            warnings=(),
        )

    observed_start = min(m.first_ts for m in markets)
    observed_end = max(m.last_ts for m in markets)
    observed_span = observed_end - observed_start

    settled_count = sum(1 for m in markets if m.is_settled)
    unsettled_count = market_count - settled_count

    series_counts = Counter(m.series_key for m in markets)
    series_count = len(series_counts)
    largest_series_share = max(series_counts.values()) / market_count

    with_close = [m for m in markets if m.close_time is not None]
    closed_in_window: int | None = None
    if with_close:
        closed_in_window = sum(
            1
            for m in with_close
            if m.close_time is not None
            and window_start <= m.close_time <= window_end
        )

    # -- gaps ---------------------------------------------------------------
    # A measured max_gap wins where the caller supplied one. Otherwise the gap
    # is implied as span / (observations - 1), which is a *mean* and therefore
    # a lower bound on the true worst gap — hence GAP_ESTIMATE_IMPRECISE.
    #
    # A market with a single observation is the interesting case: it has no
    # interior gap to measure, and treating that as "no gap" would let a
    # sample of one-snapshot markets sail through the check. One snapshot is
    # one frozen book, and replaying it across the window assumes the book
    # never moved — precisely what this check exists to catch. So its gap is
    # taken to be the whole requested window.
    gaps: list[timedelta] = []
    gaps_measured = False
    single_observation_markets = 0
    for m in markets:
        if m.max_gap is not None:
            gaps.append(m.max_gap)
            gaps_measured = True
        elif m.observations >= 2:
            gaps.append(m.span / (m.observations - 1))
        else:
            single_observation_markets += 1
            gaps.append(requested_span)

    median_gap = median(gaps) if gaps else None
    worst_gap = max(gaps) if gaps else None

    refusals: list[Finding] = []
    warnings: list[Finding] = []

    # -- breadth ------------------------------------------------------------
    if market_count < min_markets:
        refusals.append(
            _finding(
                RefusalCode.TOO_FEW_MARKETS,
                Severity.REFUSAL,
                f"{market_count} markets in the window, against a floor of "
                f"{min_markets}. A backtest over this few markets measures "
                f"those markets — their liquidity, their series, their "
                f"resolution luck — not a strategy.",
                measured=f"{market_count} markets",
                threshold=f"{min_markets} markets",
            )
        )

    if observed_span < min_observed_span:
        refusals.append(
            _finding(
                RefusalCode.WINDOW_TOO_SHORT,
                Severity.REFUSAL,
                f"Data spans {_fmt_duration(observed_span)} "
                f"({_fmt_ts(observed_start)} to {_fmt_ts(observed_end)}), "
                f"against a floor of {_fmt_duration(min_observed_span)}. Note "
                f"this is the observed span, not the "
                f"{_fmt_duration(requested_span)} requested. A span this "
                f"short cannot contain a regime.",
                measured=_fmt_duration(observed_span),
                threshold=_fmt_duration(min_observed_span),
            )
        )

    # -- outcomes -----------------------------------------------------------
    if settled_count < min_settled_markets:
        refusals.append(
            _finding(
                RefusalCode.TOO_FEW_SETTLED,
                Severity.REFUSAL,
                f"{settled_count} of {market_count} markets have a known "
                f"outcome, against a floor of {min_settled_markets}. Outcomes "
                f"are the only ground truth a backtest has; the other "
                f"{unsettled_count} contribute a cost basis and no result.",
                measured=f"{settled_count} settled markets",
                threshold=f"{min_settled_markets} settled markets",
            )
        )

    if unsettled_count == 0:
        closed_note = (
            f" {closed_in_window} of them closed inside the window."
            if closed_in_window is not None
            else ""
        )
        refusals.append(
            _finding(
                RefusalCode.SURVIVORSHIP,
                Severity.REFUSAL,
                f"All {market_count} markets in the sample have settled and "
                f"none is still open.{closed_note} That is the signature of a "
                f"sample drawn by resolution date rather than by observation "
                f"window: it keeps only markets short-dated enough to resolve "
                f"inside it, and drops every long-dated market that was "
                f"tradeable throughout. Confirm the selection was by window; "
                f"a genuinely matured historical window looks identical here "
                f"and cannot be told apart from a summary.",
                measured=f"{settled_count}/{market_count} settled, 0 open",
                threshold="at least 1 unsettled market",
            )
        )

    # -- diversity ----------------------------------------------------------
    if series_count < min_distinct_series:
        only = next(iter(series_counts))
        refusals.append(
            _finding(
                RefusalCode.SINGLE_SERIES,
                Severity.REFUSAL,
                f"All {market_count} markets belong to series {only!r} "
                f"({series_count} distinct series, floor {min_distinct_series}). "
                f"That is one event's idiosyncrasy, not a portfolio — and the "
                f"series ticker is the only field that says what a market "
                f"tracks.",
                measured=f"{series_count} series",
                threshold=f"{min_distinct_series} series",
            )
        )
    elif largest_series_share > max_series_concentration:
        dominant, dominant_n = series_counts.most_common(1)[0]
        warnings.append(
            _finding(
                RefusalCode.SERIES_CONCENTRATION,
                Severity.WARNING,
                f"Series {dominant!r} holds {dominant_n} of {market_count} "
                f"markets ({_fmt_pct(largest_series_share)}), above "
                f"{_fmt_pct(max_series_concentration)}. The other "
                f"{series_count - 1} series are rounding error, so this result "
                f"generalises to {dominant!r} and not beyond it. If that is "
                f"deliberate, this warning is your evidence for the caveat.",
                measured=_fmt_pct(largest_series_share),
                threshold=_fmt_pct(max_series_concentration),
            )
        )

    # -- gaps ---------------------------------------------------------------
    if median_gap is not None and median_gap > tolerated:
        extra = (
            f" {single_observation_markets} market(s) hold a single "
            f"observation and are counted as a full-window gap."
            if single_observation_markets
            else ""
        )
        refusals.append(
            _finding(
                RefusalCode.GAPS_TOO_LARGE,
                Severity.REFUSAL,
                f"The typical market is sampled every "
                f"{_fmt_duration(median_gap)} (median), against a tolerance of "
                f"{_fmt_duration(tolerated)} for an expected interval of "
                f"{_fmt_duration(expected_interval)}.{extra} A replay across a "
                f"hole assumes the book did not move, which looks plausible "
                f"and is wrong.",
                measured=f"median gap {_fmt_duration(median_gap)}",
                threshold=_fmt_duration(tolerated),
            )
        )
    elif worst_gap is not None and worst_gap > tolerated:
        warnings.append(
            _finding(
                RefusalCode.ISOLATED_GAPS,
                Severity.WARNING,
                f"The typical market is sampled every "
                f"{_fmt_duration(median_gap) if median_gap else '-'} and clears "
                f"the {_fmt_duration(tolerated)} tolerance, but some market has "
                f"a gap of {_fmt_duration(worst_gap)}. One market's ingest "
                f"hiccup should not void a run; go look at it before trusting "
                f"per-market results.",
                measured=f"worst gap {_fmt_duration(worst_gap)}",
                threshold=_fmt_duration(tolerated),
            )
        )

    if not gaps_measured:
        warnings.append(
            _finding(
                RefusalCode.GAP_ESTIMATE_IMPRECISE,
                Severity.WARNING,
                f"No market supplied a measured max_gap, so gap figures are "
                f"means implied from counts and are a lower bound on the true "
                f"worst gap — a market with one long hole and otherwise dense "
                f"sampling shows a small mean. Reported median "
                f"{_fmt_duration(median_gap) if median_gap else '-'} is "
                f"therefore optimistic.",
                measured="implied mean gaps",
                threshold="measured max_gap per market",
            )
        )

    # -- window fill --------------------------------------------------------
    fill = min(1.0, observed_span.total_seconds() / requested_span.total_seconds())
    if fill < min_window_fill:
        warnings.append(
            _finding(
                RefusalCode.PARTIAL_WINDOW,
                Severity.WARNING,
                f"Data covers {_fmt_pct(fill)} of the requested window "
                f"({_fmt_duration(observed_span)} of "
                f"{_fmt_duration(requested_span)}). Any result must be "
                f"described by the observed span, not the requested one.",
                measured=_fmt_pct(fill),
                threshold=_fmt_pct(min_window_fill),
            )
        )

    return CoverageReport(
        window_start=window_start,
        window_end=window_end,
        observed_start=observed_start,
        observed_end=observed_end,
        market_count=market_count,
        series_count=series_count,
        settled_count=settled_count,
        unsettled_count=unsettled_count,
        largest_series_share=largest_series_share,
        closed_in_window=closed_in_window,
        total_observations=total_observations,
        expected_interval=expected_interval,
        tolerated_gap=tolerated,
        median_gap=median_gap,
        worst_gap=worst_gap,
        gaps_measured=gaps_measured,
        single_observation_markets=single_observation_markets,
        refusals=tuple(refusals),
        warnings=tuple(warnings),
    )

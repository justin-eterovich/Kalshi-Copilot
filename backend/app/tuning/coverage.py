"""Is there enough here to tune anything?

The dangerous failure of an LLM tuner is not that it returns nonsense. It is
that it returns a **plausible, well-argued threshold change derived from eleven
picks**, and a threshold change carries no marker of the sample it came from.
Downstream, "raise ``min_net_edge_cents`` from 2.0 to 3.5" looks identical
whether it was inferred from a month of settled outcomes or from one quiet
Tuesday.

This module is the gate in front of that, and it is deliberately the same shape
as ``app/backtest/coverage.py``: measure, compare each number to its floor, and
**name the number and the floor** rather than saying "insufficient data". The
operator's next question is always "so do I wait a week, or change the config?"
and only the specific number answers it.

The state of this deployment, measured 2026-07-28 against the live API:

    signals table, entire history      187 rows
    span                               ~20 hours (2026-07-27T10:02 .. 07-28T06:00)
    stale_quote                        109 signals -> 20 proposals -> 13 trades
    undervalued_screener                67
    resolution_sniper / whale_flow /
      longshot_calibration               6 / 3 / 2
    set_arbitrage                        0

So the harvest window ``[T-48h, T-24h)`` is **empty today** — the whole table
is younger than 48 hours — and even the one detector with volume produced 13
scoreable trades, which the report card itself calls ``insufficient_evidence``
against a floor of 20. A run today refuses on every detector, and that is the
feature working rather than a thing to route around.

**Per detector, not per run.** Tuning is per-detector, so the gate is too: a
run where ``stale_quote`` has 200 picks and ``whale_flow`` has three should
tune the first and refuse the second, not average them into one verdict that is
wrong about both.

**Clearing these floors is necessary, not sufficient.** Thirty picks is where a
sample stops being anecdote; it is nowhere near where it becomes evidence. The
floors mark where a tuning suggestion is *certainly* noise, not where it starts
being trustworthy.

Pure: standard library only, no I/O, and no clock — the window is a parameter.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

__all__ = [
    "MAX_TICKER_CONCENTRATION",
    "MAX_UNMARKABLE_SHARE",
    "MIN_DISTINCT_EVENTS",
    "MIN_DISTINCT_MARKETS",
    "MIN_MARKABLE_PICKS",
    "MIN_PICKS",
    "MIN_SETTLED_PICKS",
    "CoverageReport",
    "DetectorCoverage",
    "DetectorSample",
    "Finding",
    "RefusalCode",
    "Severity",
    "TuningRefused",
    "audit",
    "audit_detector",
]


# ---------------------------------------------------------------------------
# Thresholds. Each is a judgement call and each comment says what it is
# defending against, because a bare literal cannot be argued with by an
# operator asking whether another week of data would help.
# ---------------------------------------------------------------------------

#: Minimum picks in the window, per detector.
#:
#: Judgement call. Below thirty, a single pick moves every average the model
#: will reason over — and the model reasons over *means*, which is exactly the
#: statistic a small sample lies about most confidently. Thirty is also roughly
#: where a detector firing on a normal day produces enough for a daily cadence
#: to make sense at all; a detector emitting ten picks a day should be tuned
#: weekly, not daily, and this floor is what surfaces that.
MIN_PICKS: Final = 30

#: Minimum picks that could actually be scored.
#:
#: The binding constraint in practice, and the one worth understanding. Picks
#: with no executable price (the resolution sniper emits these by design) and
#: picks on markets with no exit bid are refused by ``mark.py``, so a detector
#: can clear :data:`MIN_PICKS` and still have almost nothing to learn from.
#: Deliberately below :data:`MIN_PICKS`: requiring them to be equal would mean
#: any research-only output disqualified a detector entirely.
MIN_MARKABLE_PICKS: Final = 20

#: Minimum distinct markets among the markable picks.
#:
#: Judgement call, and the one that catches the most seductive bad sample.
#: Forty picks on one market is n=1 wearing a bigger number: the detector
#: re-derived the same observation as the book moved, and every one of those
#: picks shares a single outcome. The dedupe fold already collapses *identical*
#: observations, but an edge that walks from 1c to 8c legitimately writes
#: several rows on one ticker.
MIN_DISTINCT_MARKETS: Final = 10

#: Minimum distinct events among the markable picks.
#:
#: The same argument one level up, and it binds where the market floor does
#: not: the watchlist is built from mutually-exclusive events, so twelve legs
#: of one NFL game are twelve markets and **one** correlated outcome. Five is a
#: degenerate-case floor, not diversity.
MIN_DISTINCT_EVENTS: Final = 5

#: Minimum picks whose market actually resolved.
#:
#: One. Not because one settled outcome is meaningful — it is not — but
#: because zero means every number in the dossier is a mark against a book that
#: has not been tested by reality even once, and the model should not be shown
#: a page of provisional numbers with no anchor. This floor is expected to bind
#: for a long time on this deployment: the watchlist is dominated by markets
#: closing weeks out, and the seasoning is 24 hours. If it binds forever, the
#: window is wrong rather than the detector — see ``window.py``.
MIN_SETTLED_PICKS: Final = 1

#: Warn when this share or more of a detector's picks could not be marked.
#:
#: Judgement call. At half, the scoreable picks are a minority of what the
#: detector did, and they are not a random minority — they are the ones on
#: markets liquid enough to quote a bid, which is a systematically easier
#: sample than the detector's real output. The mean over them is biased and the
#: warning says so.
MAX_UNMARKABLE_SHARE: Final = 0.50

#: Warn when one ticker holds more than this share of the markable picks.
#:
#: Judgement call. Past 40% the detector's measured performance is one market's
#: idiosyncrasy with a sample size painted on.
MAX_TICKER_CONCENTRATION: Final = 0.40


class Severity(enum.StrEnum):
    """Whether a finding blocks tuning or merely annotates it."""

    REFUSAL = "refusal"
    WARNING = "warning"


class RefusalCode(enum.StrEnum):
    """Every finding this module can produce.

    A separate enum from ``backtest.coverage.RefusalCode`` even though the
    machinery rhymes: these are different questions about different data, and
    one shared enum would mean a caller switching on a code could not tell
    which gate produced it.
    """

    #: The detector emitted nothing in the window. Reported alone — every other
    #: floor would also fail and listing six refusals for an idle detector is
    #: noise, not detail.
    NO_PICKS = "no_picks"

    #: Too few picks to average over.
    TOO_FEW_PICKS = "too_few_picks"

    #: Too few picks could be scored. The binding one in practice.
    TOO_FEW_MARKABLE = "too_few_markable"

    #: Too few distinct markets: the sample is a handful of tickers observed
    #: repeatedly, not a cross-section of what the detector does.
    TOO_FEW_MARKETS = "too_few_markets"

    #: Too few distinct events. Legs of one exclusive event share an outcome,
    #: so market count overstates independence.
    TOO_FEW_EVENTS = "too_few_events"

    #: Nothing in the sample has resolved, so no number in it has been tested
    #: against reality.
    NO_SETTLED_OUTCOME = "no_settled_outcome"

    # -- warnings -----------------------------------------------------------

    #: Most of the detector's output could not be scored, so the scoreable part
    #: is a biased slice of it.
    MOSTLY_UNMARKABLE = "mostly_unmarkable"

    #: One ticker dominates the markable picks.
    TICKER_CONCENTRATION = "ticker_concentration"

    #: No pick in the sample ever became a proposal, so every number is
    #: hypothetical — a paper mark on a trade nobody was offered. Legitimate
    #: (most signals never reach the queue) and worth saying out loud, because
    #: it is the difference between "the detector is wrong" and "the detector
    #: is wrong about trades that were never taken".
    ALL_HYPOTHETICAL = "all_hypothetical"

    #: The sample spans much less of the window than it should, which usually
    #: means ingest was down for part of it rather than that the detector was
    #: quiet.
    SPARSE_WINDOW = "sparse_window"


@dataclass(frozen=True, slots=True)
class Finding:
    """One measured deficiency, rendered for both a CLI and a dashboard.

    ``measured`` and ``threshold`` are pre-formatted strings because the values
    are heterogeneous — counts, shares, durations — and every consumer displays
    them rather than computing on them.
    """

    detector: str
    code: RefusalCode
    severity: Severity
    message: str
    measured: str
    threshold: str

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.REFUSAL

    def render(self) -> str:
        return f"{self.severity.upper()} {self.detector}/{self.code}: {self.message}"


class TuningRefused(RuntimeError):
    """Raised when a run is asked to tune a detector the data cannot support."""

    def __init__(self, report: CoverageReport) -> None:
        self.report = report
        names = ", ".join(sorted(report.refused_detectors)) or "every detector"
        super().__init__(
            f"tuning refused for {names}; "
            f"{len(report.refusals)} floor(s) missed. See the report for each "
            f"measured number against the threshold it missed."
        )


@dataclass(frozen=True, slots=True)
class DetectorSample:
    """What one detector produced in the window, already counted.

    Plain numbers, no ``Mark`` objects: this module stays standard-library-only
    so that the gate can be tested at every boundary with no database, no fee
    schedule and no ORM. ``dossier.py`` builds these from marks.
    """

    detector: str
    picks: int
    markable: int
    settled: int
    open_marks: int
    unmarkable: int
    distinct_markets: int
    distinct_events: int
    #: Picks held by the single most-represented ticker among markable picks.
    largest_ticker_picks: int
    #: Picks that became a proposal a human could have approved.
    proposed: int
    first_pick_at: datetime | None = None
    last_pick_at: datetime | None = None
    #: Counts keyed by ``Unmarkable`` value, so the operator can see *why* a
    #: detector is unscoreable rather than only that it is.
    unmarkable_by_reason: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.picks < 0 or self.markable < 0 or self.unmarkable < 0:
            raise ValueError(f"{self.detector}: negative counts in sample.")
        if self.markable + self.unmarkable != self.picks:
            raise ValueError(
                f"{self.detector}: markable ({self.markable}) + unmarkable "
                f"({self.unmarkable}) != picks ({self.picks}). A pick is "
                f"either scored or refused; a sample where the two do not "
                f"account for every pick has lost one, and a lost pick is the "
                f"silent-zero failure this gate exists to prevent."
            )
        if self.settled + self.open_marks != self.markable:
            raise ValueError(
                f"{self.detector}: settled ({self.settled}) + open "
                f"({self.open_marks}) != markable ({self.markable})."
            )

    @property
    def unmarkable_share(self) -> float:
        return self.unmarkable / self.picks if self.picks else 0.0

    @property
    def ticker_concentration(self) -> float:
        return self.largest_ticker_picks / self.markable if self.markable else 0.0

    @property
    def observed_span(self) -> timedelta:
        if self.first_pick_at is None or self.last_pick_at is None:
            return timedelta(0)
        return self.last_pick_at - self.first_pick_at


@dataclass(frozen=True, slots=True)
class DetectorCoverage:
    """One detector's verdict, with the numbers behind it."""

    sample: DetectorSample
    findings: tuple[Finding, ...]

    @property
    def detector(self) -> str:
        return self.sample.detector

    @property
    def refusals(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.blocking)

    @property
    def usable(self) -> bool:
        return not self.refusals


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Every detector's verdict for one run.

    Returned in full whether or not anything passed — the measured numbers are
    the useful output when the answer is no, and hiding them behind an
    exception would mean the only way to see coverage was to fail.
    """

    window_start: datetime
    window_end: datetime
    as_of: datetime
    detectors: tuple[DetectorCoverage, ...]

    @property
    def refusals(self) -> tuple[Finding, ...]:
        return tuple(f for d in self.detectors for f in d.refusals)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for d in self.detectors for f in d.warnings)

    @property
    def usable_detectors(self) -> tuple[str, ...]:
        return tuple(d.detector for d in self.detectors if d.usable)

    @property
    def refused_detectors(self) -> tuple[str, ...]:
        return tuple(d.detector for d in self.detectors if not d.usable)

    @property
    def usable(self) -> bool:
        """Whether **any** detector cleared its floors.

        Deliberately "any" rather than "all": a run that can tune one detector
        is worth making. The per-detector verdict is what decides which.
        """
        return bool(self.usable_detectors)

    def summary_lines(self) -> list[str]:
        lines = [
            f"tuning coverage  window {self.window_start.isoformat()} .. "
            f"{self.window_end.isoformat()}  marked {self.as_of.isoformat()}",
        ]
        if not self.detectors:
            lines.append("  no detector produced a single pick in the window.")
            return lines
        for cov in self.detectors:
            s = cov.sample
            verdict = "TUNABLE" if cov.usable else "REFUSED"
            lines.append(
                f"  {s.detector:<24} {verdict:<8} "
                f"picks={s.picks} markable={s.markable} "
                f"(settled={s.settled} open={s.open_marks}) "
                f"markets={s.distinct_markets} events={s.distinct_events}"
            )
            for finding in cov.findings:
                lines.append(
                    f"      {finding.severity.upper():<8} {finding.code}: "
                    f"{finding.measured} against {finding.threshold}"
                )
        return lines


def _finding(
    detector: str,
    code: RefusalCode,
    severity: Severity,
    message: str,
    measured: str,
    threshold: str,
) -> Finding:
    return Finding(
        detector=detector,
        code=code,
        severity=severity,
        message=message,
        measured=measured,
        threshold=threshold,
    )


def audit_detector(
    sample: DetectorSample,
    *,
    window_span: timedelta | None = None,
    min_picks: int = MIN_PICKS,
    min_markable: int = MIN_MARKABLE_PICKS,
    min_markets: int = MIN_DISTINCT_MARKETS,
    min_events: int = MIN_DISTINCT_EVENTS,
    min_settled: int = MIN_SETTLED_PICKS,
    max_unmarkable_share: float = MAX_UNMARKABLE_SHARE,
    max_ticker_concentration: float = MAX_TICKER_CONCENTRATION,
) -> DetectorCoverage:
    """Measure one detector's sample and collect every finding.

    Thresholds are keyword arguments over the module defaults so a caller may
    loosen one deliberately and visibly. Loosening a floor does not make a thin
    sample mean more; it makes the refusal stop saying so.
    """
    d = sample.detector

    if sample.picks == 0:
        return DetectorCoverage(
            sample=sample,
            findings=(
                _finding(
                    d,
                    RefusalCode.NO_PICKS,
                    Severity.REFUSAL,
                    f"{d} emitted no signal in the window, so there is nothing "
                    f"to tune it against. Either it is disabled, or its "
                    f"refusal conditions held all window — both are answers, "
                    f"neither is a threshold problem.",
                    "0 picks",
                    "at least 1",
                ),
            ),
        )

    findings: list[Finding] = []

    if sample.picks < min_picks:
        findings.append(
            _finding(
                d,
                RefusalCode.TOO_FEW_PICKS,
                Severity.REFUSAL,
                f"{sample.picks} picks is too few to average over — one pick "
                f"moves every mean the model would reason from. A detector "
                f"this quiet should be tuned on a longer window, not a daily "
                f"one.",
                f"{sample.picks} picks",
                f"at least {min_picks}",
            )
        )

    if sample.markable < min_markable:
        findings.append(
            _finding(
                d,
                RefusalCode.TOO_FEW_MARKABLE,
                Severity.REFUSAL,
                f"only {sample.markable} of {sample.picks} picks could be "
                f"scored; the rest carried no executable price or had no exit "
                f"quote. An unscoreable pick is not a pick that broke even.",
                f"{sample.markable} markable",
                f"at least {min_markable}",
            )
        )

    if sample.distinct_markets < min_markets:
        findings.append(
            _finding(
                d,
                RefusalCode.TOO_FEW_MARKETS,
                Severity.REFUSAL,
                f"{sample.markable} markable picks span only "
                f"{sample.distinct_markets} markets. Repeated observations of "
                f"a few tickers share their outcomes, so the effective sample "
                f"is the market count, not the pick count.",
                f"{sample.distinct_markets} markets",
                f"at least {min_markets}",
            )
        )

    if sample.distinct_events < min_events:
        findings.append(
            _finding(
                d,
                RefusalCode.TOO_FEW_EVENTS,
                Severity.REFUSAL,
                f"markable picks span only {sample.distinct_events} events. "
                f"Legs of one mutually-exclusive event resolve together, so "
                f"market count overstates how many independent outcomes are "
                f"really in the sample.",
                f"{sample.distinct_events} events",
                f"at least {min_events}",
            )
        )

    if sample.settled < min_settled:
        findings.append(
            _finding(
                d,
                RefusalCode.NO_SETTLED_OUTCOME,
                Severity.REFUSAL,
                f"nothing in the sample has resolved, so every number in it is "
                f"a mark against a book that will move again. Provisional marks "
                f"are worth showing; they are not worth tuning on alone.",
                f"{sample.settled} settled",
                f"at least {min_settled}",
            )
        )

    # -- warnings -----------------------------------------------------------

    if sample.picks and sample.unmarkable_share >= max_unmarkable_share:
        findings.append(
            _finding(
                d,
                RefusalCode.MOSTLY_UNMARKABLE,
                Severity.WARNING,
                f"{sample.unmarkable_share:.0%} of picks could not be scored. "
                f"The ones that could are not a random subset — they are the "
                f"liquid ones — so the mean over them flatters the detector.",
                f"{sample.unmarkable_share:.0%} unmarkable",
                f"below {max_unmarkable_share:.0%}",
            )
        )

    if sample.markable and sample.ticker_concentration > max_ticker_concentration:
        findings.append(
            _finding(
                d,
                RefusalCode.TICKER_CONCENTRATION,
                Severity.WARNING,
                f"one ticker holds {sample.ticker_concentration:.0%} of the "
                f"markable picks; the measured performance is largely that "
                f"market's.",
                f"{sample.ticker_concentration:.0%} in one ticker",
                f"below {max_ticker_concentration:.0%}",
            )
        )

    if sample.proposed == 0:
        findings.append(
            _finding(
                d,
                RefusalCode.ALL_HYPOTHETICAL,
                Severity.WARNING,
                f"no pick became a proposal, so every figure is a paper mark "
                f"on a trade nobody was offered. Expected — most signals never "
                f"reach the queue — but it is the difference between a "
                f"detector being wrong and being wrong about untaken trades.",
                "0 proposed",
                "at least 1 for realised evidence",
            )
        )

    if window_span and window_span > timedelta(0):
        fill = sample.observed_span / window_span
        if fill < 0.5:
            findings.append(
                _finding(
                    d,
                    RefusalCode.SPARSE_WINDOW,
                    Severity.WARNING,
                    f"picks span only {fill:.0%} of the window, which is more "
                    f"often an ingest outage than a quiet detector. Check the "
                    f"worker was up for the whole window before reading "
                    f"anything into the counts.",
                    f"{fill:.0%} of window",
                    "at least 50%",
                )
            )

    return DetectorCoverage(sample=sample, findings=tuple(findings))


def audit(
    samples: Sequence[DetectorSample],
    *,
    window_start: datetime,
    window_end: datetime,
    as_of: datetime,
    **thresholds: object,
) -> CoverageReport:
    """Audit every detector's sample for one run.

    ``thresholds`` is forwarded to :func:`audit_detector` unchanged so a caller
    loosens a floor for the whole run in one place.
    """
    span = window_end - window_start
    covers = tuple(
        audit_detector(sample, window_span=span, **thresholds)  # type: ignore[arg-type]
        for sample in sorted(samples, key=lambda s: s.detector)
    )
    return CoverageReport(
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
        detectors=covers,
    )

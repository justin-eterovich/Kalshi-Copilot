"""The page the model reads — and the report a human reads when it refuses.

One JSON object per run: what each detector is currently configured to do,
what it claimed in the window, what those claims turned out to be worth, and
what could not be scored at all. Nothing here decides anything; it is the
evidence, assembled once so that the coverage gate, the operator's report and
(in M10b) the model all see exactly the same numbers.

Four rules it inherits from the rest of the codebase:

**Prices stay strings.** Every money figure is serialised with ``str()``. The
consumer is JSON — a browser or a language model — and parsing a fixed-point
dollar string into a float re-introduces the precision loss the whole backend
exists to avoid. Format for display; never compute downstream.

**Settled and open are never summed.** Two separate aggregates with separate
``n``. Averaging a resolved outcome with a mark-to-market produces a number
belonging to no book that exists.

**The cap is deliberate, ordered, and announced.** A dossier has a size budget,
so per-detector pick rows are capped. They are ordered by absolute claimed edge
then recency — towards the picks the detector was most confident about, which
is what a tuning question is about — and when the cap binds it is stated in the
payload *and* logged. A silent truncation hands the model a biased sample of
its own evidence and lets it conclude something confident about the 23% that
happened to survive.

**Unscoreable picks are counted, not dropped.** ``unmarkable_by_reason`` is in
the payload for every detector. A model that sees only the scoreable picks
cannot tell a detector that fires rarely and well from one that fires constantly
into markets with no bid.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from app.config import Config
from app.core.logging import get_logger
from app.tuning.coverage import CoverageReport, DetectorSample
from app.tuning.mark import Mark, MarkBasis
from app.tuning.window import TuningWindow

log = get_logger(__name__)

__all__ = [
    "MAX_PICK_ROWS",
    "DetectorAggregate",
    "build_dossier",
    "detector_config_snapshot",
    "sample_for",
    "summarise",
]

#: Most pick rows carried per detector.
#:
#: Judgement call, sized for the token budget rather than the statistics: at
#: roughly 40 tokens a row this keeps a six-detector dossier near 30k input
#: tokens, which is about $0.15 of Opus 5 input. The *aggregates* are computed
#: over every pick regardless — the cap truncates the itemised rows only, so a
#: bound cap costs the model detail, never accuracy about the totals.
MAX_PICK_ROWS: Final = 120

ZERO: Final = Decimal(0)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


@dataclass(frozen=True, slots=True)
class DetectorAggregate:
    """Claimed versus realised, for one detector, split by evidence class.

    ``*_settled`` figures are realised. ``*_open`` figures are provisional
    marks. They are separate fields rather than a combined mean plus a flag
    because a combined mean is the thing this milestone must not produce.
    """

    detector: str
    claimed_edge_mean: Decimal | None
    #: Realised, resolved markets only.
    realised_mean_settled: Decimal | None
    n_settled: int
    #: Mark-to-market, open markets only. Provisional.
    realised_mean_open: Decimal | None
    n_open: int
    #: Over every markable pick, both classes. Reported because it is the
    #: honest headline when nothing has settled — and labelled ``mixed`` so
    #: nobody mistakes it for a realised figure.
    realised_mean_mixed: Decimal | None
    n_mixed: int
    fees_paid_cents: Decimal
    slippage_cents: Decimal
    winners: int
    losers: int

    @property
    def edge_error_mean(self) -> Decimal | None:
        """Claimed minus realised, per contract, over every markable pick.

        The headline of the whole milestone. Live, ``stale_quote`` claims
        24.56c and realises -3.55c, so this reads about +28c: the detector is
        not slightly optimistic, it is describing a different trade from the
        one that happens.
        """
        if self.claimed_edge_mean is None or self.realised_mean_mixed is None:
            return None
        return self.claimed_edge_mean - self.realised_mean_mixed

    def as_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        return {
            "claimed_edge_cents_mean": s(self.claimed_edge_mean),
            "realised": {
                "settled": {
                    "mean_cents_per_contract": s(self.realised_mean_settled),
                    "n": self.n_settled,
                    "basis": "resolved outcomes; realised",
                },
                "open": {
                    "mean_cents_per_contract": s(self.realised_mean_open),
                    "n": self.n_open,
                    "basis": "mark-to-market at the exit quote; provisional",
                },
                "mixed": {
                    "mean_cents_per_contract": s(self.realised_mean_mixed),
                    "n": self.n_mixed,
                    "basis": (
                        "settled and open together; provisional wherever "
                        "n_open > 0 and not comparable to a report-card figure"
                    ),
                },
            },
            "edge_error_cents_mean": s(self.edge_error_mean),
            "fees_paid_cents": str(self.fees_paid_cents),
            "slippage_cents": str(self.slippage_cents),
            "winners": self.winners,
            "losers": self.losers,
        }


def summarise(detector: str, marks: Sequence[Mark]) -> DetectorAggregate:
    """Aggregate one detector's marks. Never mixes evidence classes silently."""
    settled = [
        m.net_per_contract_cents
        for m in marks
        if m.basis is MarkBasis.SETTLED and m.net_per_contract_cents is not None
    ]
    opens = [
        m.net_per_contract_cents
        for m in marks
        if m.basis is MarkBasis.OPEN and m.net_per_contract_cents is not None
    ]
    scoreable = [m for m in marks if m.scoreable]
    mixed = [
        m.net_per_contract_cents
        for m in scoreable
        if m.net_per_contract_cents is not None
    ]

    return DetectorAggregate(
        detector=detector,
        # Claimed edge is averaged over the *markable* picks only, so it is
        # compared against realised on the same sample. Averaging claims over
        # picks that could not be scored would compare two different sets.
        claimed_edge_mean=_mean([m.pick.claimed_edge_cents for m in scoreable]),
        realised_mean_settled=_mean(settled),
        n_settled=len(settled),
        realised_mean_open=_mean(opens),
        n_open=len(opens),
        realised_mean_mixed=_mean(mixed),
        n_mixed=len(mixed),
        fees_paid_cents=sum((m.fee_cents or ZERO for m in scoreable), ZERO),
        slippage_cents=sum((m.slippage_cents or ZERO for m in scoreable), ZERO),
        winners=sum(1 for v in mixed if v > 0),
        losers=sum(1 for v in mixed if v < 0),
    )


def sample_for(detector: str, marks: Sequence[Mark]) -> DetectorSample:
    """Reduce marks to the plain counts the coverage gate audits."""
    scoreable = [m for m in marks if m.scoreable]
    tickers = Counter(m.pick.ticker for m in scoreable)
    events = {
        m.pick.event_ticker or m.pick.ticker  # a market with no event is its own
        for m in scoreable
    }
    reasons = Counter(
        m.refusal.value for m in marks if m.refusal is not None
    )
    stamps = [m.pick.created_at for m in marks]

    return DetectorSample(
        detector=detector,
        picks=len(marks),
        markable=len(scoreable),
        settled=sum(1 for m in marks if m.basis is MarkBasis.SETTLED),
        open_marks=sum(1 for m in marks if m.basis is MarkBasis.OPEN),
        unmarkable=len(marks) - len(scoreable),
        distinct_markets=len(tickers),
        distinct_events=len(events),
        largest_ticker_picks=max(tickers.values(), default=0),
        proposed=sum(1 for m in marks if m.pick.became_proposal),
        first_pick_at=min(stamps, default=None),
        last_pick_at=max(stamps, default=None),
        unmarkable_by_reason=dict(reasons),
    )


def detector_config_snapshot(config: Config, detector: str) -> dict[str, Any]:
    """The knobs a detector is currently running with.

    Read through ``config`` rather than the YAML file so that whatever
    precedence the loader applies is what the model is shown. The weather
    engine is configured one level up, outside the ``detectors:`` block — the
    same wrinkle ``detectors.base.enabled_detector_names`` exists to paper
    over — so it is special-cased here rather than silently reported as absent.
    """
    block = (
        config.weather
        if detector == "weather"
        else getattr(config.detectors, detector, None)
    )
    if block is None:
        return {}
    return {k: _jsonable(v) for k, v in block.model_dump().items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _pick_row(mark: Mark) -> dict[str, Any]:
    p = mark.pick

    def s(v: Decimal | None) -> str | None:
        return None if v is None else str(v)

    row: dict[str, Any] = {
        "signal_id": p.signal_id,
        "ticker": p.ticker,
        "event_ticker": p.event_ticker,
        "series": p.series_ticker,
        "side": p.side.value,
        "action": p.action,
        "created_at": p.created_at.isoformat(),
        "seen_count": p.seen_count,
        "confidence": round(p.confidence, 4),
        "claimed_edge_cents": str(p.claimed_edge_cents),
        "entry_price": s(p.entry_price),
        "basis": mark.basis.value,
        # Explicit, because the distinction decides how much weight a number
        # deserves and an omitted field reads as "no".
        "hypothetical": not p.became_proposal,
    }
    if mark.scoreable:
        row |= {
            "exit_price": s(mark.exit_price),
            "contracts": s(mark.contracts),
            "gross_cents": s(mark.gross_cents),
            "fee_cents": s(mark.fee_cents),
            "net_cents_per_contract": s(mark.net_per_contract_cents),
            "edge_error_cents": s(mark.edge_error_cents),
        }
    else:
        row |= {
            "refusal": mark.refusal.value if mark.refusal else None,
            "refusal_detail": mark.detail,
        }
    return row


def _ranked(marks: Sequence[Mark]) -> list[Mark]:
    """Order towards what the dossier is for, then by recency.

    A cap needs an ordering or it truncates in whatever order the database
    returned — and the undervalued screener has already demonstrated what that
    costs, discarding 68,110 of 88,110 eligible markets in arrival order while
    ranking on a percentile computed over whichever 23% arrived.

    Here the question is "which claims were wrong", so the ordering is by
    absolute claimed edge: the boldest claims first, whichever direction they
    were wrong in. Recency breaks ties.
    """
    return sorted(
        marks,
        key=lambda m: (abs(m.pick.claimed_edge_cents), m.pick.created_at),
        reverse=True,
    )


def build_dossier(
    marks_by_detector: dict[str, list[Mark]],
    *,
    config: Config,
    window: TuningWindow,
    coverage: CoverageReport,
    max_pick_rows: int = MAX_PICK_ROWS,
) -> dict[str, Any]:
    """Assemble the run's evidence into one JSON-ready object.

    Built whether or not the coverage gate passed. When it refuses, this is the
    report the operator reads — which is the whole reason the gate returns a
    populated report instead of only raising.

    **The coverage report decides the roster**, not ``marks_by_detector``: a
    detector absent from ``coverage`` is absent from the payload even if it has
    marks. That is deliberate — it keeps one authority on which detectors a run
    covers — but it means the caller must audit every detector it intends to
    report, including enabled ones that produced nothing, or a silent detector
    vanishes instead of appearing refused with ``NO_PICKS``.
    """
    detectors: list[dict[str, Any]] = []

    for cov in coverage.detectors:
        name = cov.detector
        marks = marks_by_detector.get(name, [])
        ranked = _ranked(marks)
        shown = ranked[:max_pick_rows]
        capped = len(ranked) > len(shown)

        if capped:
            # Say so where an operator will see it, not only in the payload.
            log.warning(
                "tuning dossier: %s pick rows capped at %d of %d; aggregates "
                "still cover every pick, itemised rows do not.",
                name,
                len(shown),
                len(ranked),
            )

        detectors.append(
            {
                "detector": name,
                "enabled": bool(
                    getattr(
                        getattr(config.detectors, name, None), "enabled", False
                    )
                    or (name == "weather" and config.weather.enabled)
                ),
                "config": detector_config_snapshot(config, name),
                "counts": {
                    "picks": cov.sample.picks,
                    "markable": cov.sample.markable,
                    "settled": cov.sample.settled,
                    "open": cov.sample.open_marks,
                    "unmarkable": cov.sample.unmarkable,
                    "distinct_markets": cov.sample.distinct_markets,
                    "distinct_events": cov.sample.distinct_events,
                    "became_proposal": cov.sample.proposed,
                    "unmarkable_by_reason": cov.sample.unmarkable_by_reason,
                },
                "performance": summarise(name, marks).as_dict(),
                "coverage": {
                    "tunable": cov.usable,
                    "refusals": [
                        {
                            "code": f.code.value,
                            "measured": f.measured,
                            "threshold": f.threshold,
                            "message": f.message,
                        }
                        for f in cov.refusals
                    ],
                    "warnings": [
                        {
                            "code": f.code.value,
                            "measured": f.measured,
                            "threshold": f.threshold,
                            "message": f.message,
                        }
                        for f in cov.warnings
                    ],
                },
                "picks": [_pick_row(m) for m in shown],
                "picks_truncated": capped,
                "picks_total": len(ranked),
            }
        )

    return {
        "schema_version": 1,
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "marked_at": window.as_of.isoformat(),
            "seasoning_hours": round(window.seasoning.total_seconds() / 3600, 2),
            "note": (
                "Picks are harvested from [start, end) and marked at "
                "marked_at. Open marks are provisional: the book moves again."
            ),
        },
        "costs": {
            "slippage_buffer_cents": str(config.costs.slippage_buffer_cents),
            "assume_taker": config.costs.assume_taker,
            "note": (
                "Every net figure here is after fees and after the slippage "
                "buffer on the exit. A gross edge is not reported anywhere."
            ),
        },
        "tunable_detectors": list(coverage.usable_detectors),
        "refused_detectors": list(coverage.refused_detectors),
        "detectors": detectors,
    }


def marks_by_detector(marks: Iterable[Mark]) -> dict[str, list[Mark]]:
    """Group marks by the detector that produced them."""
    grouped: dict[str, list[Mark]] = {}
    for mark in marks:
        grouped.setdefault(mark.pick.detector, []).append(mark)
    return grouped

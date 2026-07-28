"""Tests for the tuning coverage gate.

The gate's failure mode is permissiveness: a well-argued threshold change
derived from eleven picks looks exactly like one derived from a month of
settled outcomes, because a threshold carries no marker of its sample. So most
of what follows is about what the gate refuses — and that a healthy sample
passes, because a gate that always says no is a gate the operator switches off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.tuning.coverage import (
    MIN_DISTINCT_EVENTS,
    MIN_DISTINCT_MARKETS,
    MIN_MARKABLE_PICKS,
    MIN_PICKS,
    DetectorSample,
    RefusalCode,
    Severity,
    audit,
    audit_detector,
)

WINDOW_START = datetime(2026, 7, 26, 6, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 27, 6, 0, tzinfo=UTC)
AS_OF = datetime(2026, 7, 28, 6, 0, tzinfo=UTC)


def sample(
    *,
    detector: str = "stale_quote",
    picks: int = 60,
    markable: int = 50,
    settled: int = 10,
    markets: int = 25,
    events: int = 12,
    largest_ticker: int = 5,
    proposed: int = 8,
    span: timedelta = timedelta(hours=20),
) -> DetectorSample:
    """A sample that clears every floor, with one knob per refusal."""
    return DetectorSample(
        detector=detector,
        picks=picks,
        markable=markable,
        settled=settled,
        open_marks=markable - settled,
        unmarkable=picks - markable,
        distinct_markets=markets,
        distinct_events=events,
        largest_ticker_picks=largest_ticker,
        proposed=proposed,
        first_pick_at=WINDOW_START,
        last_pick_at=WINDOW_START + span,
    )


def codes(cov) -> set[RefusalCode]:
    return {f.code for f in cov.findings}


class TestHealthy:
    def test_a_healthy_sample_is_tunable(self) -> None:
        cov = audit_detector(sample())

        assert cov.usable is True
        assert cov.refusals == ()

    def test_warnings_do_not_block(self) -> None:
        # One ticker holds 30 of 50 markable picks: 60%, past the 40% warning.
        cov = audit_detector(sample(largest_ticker=30))

        assert RefusalCode.TICKER_CONCENTRATION in codes(cov)
        assert cov.usable is True


class TestRefusals:
    def test_an_idle_detector_reports_one_finding_not_six(self) -> None:
        """Listing every floor for a detector that emitted nothing is noise.

        The useful statement is "it said nothing", which is an answer about the
        detector rather than about the thresholds.
        """
        cov = audit_detector(
            DetectorSample(
                detector="set_arbitrage",
                picks=0,
                markable=0,
                settled=0,
                open_marks=0,
                unmarkable=0,
                distinct_markets=0,
                distinct_events=0,
                largest_ticker_picks=0,
                proposed=0,
            )
        )

        assert codes(cov) == {RefusalCode.NO_PICKS}
        assert cov.usable is False

    def test_too_few_picks(self) -> None:
        cov = audit_detector(sample(picks=MIN_PICKS - 1, markable=25, settled=5))

        assert RefusalCode.TOO_FEW_PICKS in codes(cov)
        assert cov.usable is False

    def test_too_few_markable(self) -> None:
        """A detector can clear the pick floor and still have nothing to learn."""
        cov = audit_detector(
            sample(picks=60, markable=MIN_MARKABLE_PICKS - 1, settled=5, markets=15)
        )

        assert RefusalCode.TOO_FEW_MARKABLE in codes(cov)

    def test_too_few_markets(self) -> None:
        cov = audit_detector(sample(markets=MIN_DISTINCT_MARKETS - 1))

        assert RefusalCode.TOO_FEW_MARKETS in codes(cov)

    def test_too_few_events(self) -> None:
        """Legs of one exclusive event resolve together, so markets overstate n."""
        cov = audit_detector(sample(markets=25, events=MIN_DISTINCT_EVENTS - 1))

        assert RefusalCode.TOO_FEW_EVENTS in codes(cov)

    def test_nothing_settled(self) -> None:
        cov = audit_detector(sample(settled=0))

        assert RefusalCode.NO_SETTLED_OUTCOME in codes(cov)
        assert cov.usable is False

    def test_every_refusal_names_its_measurement_and_threshold(self) -> None:
        """'Insufficient data' does not tell an operator whether to wait."""
        cov = audit_detector(sample(picks=5, markable=4, settled=0, markets=2, events=1))

        assert cov.refusals
        for finding in cov.refusals:
            assert finding.measured
            assert finding.threshold
            assert finding.severity is Severity.REFUSAL


class TestWarnings:
    def test_mostly_unmarkable_is_flagged(self) -> None:
        """The scoreable picks are the liquid ones — a biased subset."""
        cov = audit_detector(sample(picks=100, markable=45, settled=10, markets=25))

        assert RefusalCode.MOSTLY_UNMARKABLE in codes(cov)

    def test_all_hypothetical_is_flagged(self) -> None:
        cov = audit_detector(sample(proposed=0))

        assert RefusalCode.ALL_HYPOTHETICAL in codes(cov)
        assert cov.usable is True  # expected, not disqualifying

    def test_a_sparse_window_is_flagged(self) -> None:
        """Usually an ingest outage rather than a quiet detector."""
        cov = audit_detector(
            sample(span=timedelta(hours=2)), window_span=timedelta(hours=24)
        )

        assert RefusalCode.SPARSE_WINDOW in codes(cov)


class TestSampleValidation:
    def test_a_lost_pick_is_a_hard_error(self) -> None:
        """markable + unmarkable must account for every pick.

        A pick that is neither is one that vanished, and a vanished pick is the
        silent-zero failure this gate exists to prevent, one layer up.
        """
        with pytest.raises(ValueError, match="!= picks"):
            DetectorSample(
                detector="d",
                picks=10,
                markable=4,
                settled=0,
                open_marks=4,
                unmarkable=3,
                distinct_markets=1,
                distinct_events=1,
                largest_ticker_picks=1,
                proposed=0,
            )

    def test_settled_and_open_must_account_for_every_markable_pick(self) -> None:
        with pytest.raises(ValueError, match="!= markable"):
            DetectorSample(
                detector="d",
                picks=10,
                markable=10,
                settled=3,
                open_marks=3,
                unmarkable=0,
                distinct_markets=1,
                distinct_events=1,
                largest_ticker_picks=1,
                proposed=0,
            )


class TestReport:
    def test_a_run_is_usable_when_any_detector_is(self) -> None:
        """One tunable detector is worth a run; the per-detector verdict says
        which."""
        report = audit(
            [sample(detector="stale_quote"), sample(detector="whale_flow", picks=3,
                                                    markable=2, settled=0, markets=2,
                                                    events=1, largest_ticker=1,
                                                    proposed=0)],
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            as_of=AS_OF,
        )

        assert report.usable is True
        assert report.usable_detectors == ("stale_quote",)
        assert report.refused_detectors == ("whale_flow",)

    def test_a_run_with_nothing_tunable_is_not_usable(self) -> None:
        report = audit(
            [sample(settled=0)],
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            as_of=AS_OF,
        )

        assert report.usable is False

    def test_summary_lines_carry_the_numbers(self) -> None:
        report = audit(
            [sample(settled=0)],
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            as_of=AS_OF,
        )
        text = "\n".join(report.summary_lines())

        assert "stale_quote" in text
        assert "REFUSED" in text
        assert "no_settled_outcome" in text

    def test_an_empty_run_says_so_rather_than_printing_a_blank_table(self) -> None:
        report = audit(
            [], window_start=WINDOW_START, window_end=WINDOW_END, as_of=AS_OF
        )

        assert report.usable is False
        assert "no detector produced a single pick" in "\n".join(
            report.summary_lines()
        )

    def test_thresholds_can_be_loosened_deliberately_and_visibly(self) -> None:
        thin = sample(picks=10, markable=8, settled=1, markets=4, events=2)

        strict = audit_detector(thin)
        loose = audit_detector(
            thin, min_picks=5, min_markable=5, min_markets=3, min_events=2
        )

        assert strict.usable is False
        assert loose.usable is True

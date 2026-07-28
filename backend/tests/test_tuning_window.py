"""Tests for the tuning window.

The window has exactly one interesting property — its boundaries — and every
bug it can have lives there: a closed interval double-counts a pick, a naive
timestamp shifts the whole window by the operator's offset, and an ``as_of``
before the window's end scores picks against the book they were derived from.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.tuning.window import (
    DEFAULT_SEASONING,
    DEFAULT_SPAN,
    TuningWindow,
    window_for,
)

NOW = datetime(2026, 7, 28, 6, 0, tzinfo=UTC)


class TestWindowFor:
    def test_default_window_is_the_previous_day_lagged_by_a_day(self) -> None:
        win = window_for(NOW)

        assert win.end == NOW - DEFAULT_SEASONING
        assert win.start == NOW - DEFAULT_SEASONING - DEFAULT_SPAN
        assert win.as_of == NOW
        assert win.span == DEFAULT_SPAN
        assert win.seasoning == DEFAULT_SEASONING

    def test_durations_are_parameters_because_the_right_one_is_unknown(self) -> None:
        win = window_for(NOW, seasoning=timedelta(days=7), span=timedelta(days=7))

        assert win.seasoning == timedelta(days=7)
        assert win.span == timedelta(days=7)

    def test_negative_seasoning_is_refused(self) -> None:
        # Would mark picks before they were made.
        with pytest.raises(ValueError, match="seasoning"):
            window_for(NOW, seasoning=timedelta(hours=-1))

    def test_zero_span_is_refused(self) -> None:
        with pytest.raises(ValueError, match="span"):
            window_for(NOW, span=timedelta(0))


class TestBoundaries:
    def test_membership_is_half_open(self) -> None:
        """Consecutive runs must partition the timeline, not overlap it.

        A pick written at exactly ``end`` belongs to the next run and to no
        other. Under a closed interval it would be harvested twice and would
        vote twice on a threshold change.
        """
        win = window_for(NOW)

        assert win.contains(win.start) is True
        assert win.contains(win.end - timedelta(microseconds=1)) is True
        assert win.contains(win.end) is False

    def test_consecutive_windows_tile_without_gap_or_overlap(self) -> None:
        today = window_for(NOW)
        yesterday = window_for(NOW - timedelta(hours=24))

        assert yesterday.end == today.start

        boundary = today.start
        assert today.contains(boundary) is True
        assert yesterday.contains(boundary) is False


class TestRefusals:
    def test_naive_timestamps_are_a_hard_error(self) -> None:
        naive = datetime(2026, 7, 28, 6, 0)

        with pytest.raises(ValueError, match="naive"):
            TuningWindow(start=naive, end=naive + timedelta(hours=1), as_of=NOW)

    def test_marking_before_the_window_closes_is_refused(self) -> None:
        """`as_of` earlier than `end` is look-ahead, pointed backwards."""
        start = NOW - timedelta(hours=48)
        end = NOW - timedelta(hours=24)

        with pytest.raises(ValueError, match="precedes the end"):
            TuningWindow(start=start, end=end, as_of=end - timedelta(seconds=1))

    def test_marking_exactly_at_the_window_end_is_allowed(self) -> None:
        # Zero seasoning is a bad idea, not an impossible one — the coverage
        # gate is what argues about whether it means anything.
        start = NOW - timedelta(hours=24)
        win = TuningWindow(start=start, end=NOW, as_of=NOW)

        assert win.seasoning == timedelta(0)

    def test_inverted_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty or inverted"):
            TuningWindow(start=NOW, end=NOW - timedelta(hours=1), as_of=NOW)

    def test_a_non_utc_aware_zone_is_accepted_and_compared_correctly(self) -> None:
        """Aware is the requirement, not UTC specifically."""
        eastern = timezone(timedelta(hours=-5))
        start = datetime(2026, 7, 26, 1, 0, tzinfo=eastern)
        win = TuningWindow(start=start, end=start + timedelta(hours=24), as_of=NOW)

        assert win.span == timedelta(hours=24)

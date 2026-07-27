"""Tests for the scheduled-release calendar.

The thing this module must never do is present a closed market as an
opportunity, so most of what follows is about the refusals: unknown series,
naive datetimes, close times that do not follow the pre-release convention,
and the state where the number is public but the book is shut.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from app.news import calendar as cal
from app.news.calendar import (
    KNOWN_CATALYSTS,
    Catalyst,
    WindowState,
    catalyst_for,
    expected_release_time,
    upcoming,
)

# Real close times pulled from the live catalog on 2026-07-27.
CPI_CLOSE = datetime(2026, 8, 12, 12, 25, tzinfo=UTC)
PAYROLLS_CLOSE = datetime(2026, 9, 4, 12, 29, tzinfo=UTC)
GDP_CLOSE = datetime(2026, 7, 30, 12, 29, tzinfo=UTC)
FOMC_CLOSE = datetime(2026, 7, 29, 17, 59, tzinfo=UTC)
ADP_CLOSE = datetime(2026, 9, 2, 12, 14, tzinfo=UTC)


def _make(
    ticker: str = "KXCPI",
    close: datetime = CPI_CLOSE,
    minutes_before: float = 120.0,
    **kwargs: object,
) -> Catalyst | None:
    """Build a catalyst positioned ``minutes_before`` its close."""
    return catalyst_for(
        series_ticker=ticker,
        close_time=close,
        now=close - timedelta(minutes=minutes_before),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# The claim table
# ---------------------------------------------------------------------------


class TestKnownCatalysts:
    def test_an_unlisted_series_is_refused_not_defaulted(self) -> None:
        """The core refusal: no such thing as a generic release time."""
        assert catalyst_for(
            series_ticker="KXBTCD",
            close_time=CPI_CLOSE,
            now=CPI_CLOSE - timedelta(hours=1),
        ) is None
        assert expected_release_time(CPI_CLOSE, series_ticker="KXBTCD") is None

    def test_lookup_is_exact_and_never_a_prefix_match(self) -> None:
        """KXFEDTWEETS is not an FOMC release, however it starts."""
        assert "KXFED" in KNOWN_CATALYSTS
        for impostor in ("KXFEDTWEETS", "KXFEDEND", "KXFEDERALCHARGE", "KXFEDMEET"):
            assert impostor not in KNOWN_CATALYSTS
            assert _make(impostor, FOMC_CLOSE) is None

    def test_ticker_is_normalised_for_case_and_whitespace(self) -> None:
        c = _make("  kxcpi  ")
        assert c is not None
        assert c.series_ticker == "KXCPI"
        assert c.label == "CPI"

    def test_empty_ticker_is_refused(self) -> None:
        assert _make("") is None
        assert expected_release_time(CPI_CLOSE, series_ticker="   ") is None

    def test_price_reaction_series_are_deliberately_excluded(self) -> None:
        """Their close ends a measurement window; nothing publishes then."""
        for ticker in ("KXDXYFOMC", "KXSPXFOMC", "KX2YFOMC"):
            assert ticker not in KNOWN_CATALYSTS

    def test_every_label_is_a_nonempty_human_name(self) -> None:
        assert KNOWN_CATALYSTS
        for ticker, label in KNOWN_CATALYSTS.items():
            assert ticker == ticker.strip().upper()
            assert label.strip()


# ---------------------------------------------------------------------------
# Inferring the release time
# ---------------------------------------------------------------------------


class TestExpectedReleaseTime:
    def test_cpi_closes_five_minutes_before_the_print(self) -> None:
        assert expected_release_time(CPI_CLOSE, series_ticker="KXCPI") == datetime(
            2026, 8, 12, 12, 30, tzinfo=UTC
        )

    def test_payrolls_and_gdp_close_one_minute_before(self) -> None:
        assert expected_release_time(
            PAYROLLS_CLOSE, series_ticker="KXPAYROLLS"
        ) == datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
        assert expected_release_time(GDP_CLOSE, series_ticker="KXGDP") == datetime(
            2026, 7, 30, 12, 30, tzinfo=UTC
        )

    def test_fomc_statement_lands_on_the_hour(self) -> None:
        assert expected_release_time(
            FOMC_CLOSE, series_ticker="KXFEDDECISION"
        ) == datetime(2026, 7, 29, 18, 0, tzinfo=UTC)

    def test_adp_resolves_to_the_quarter_hour_not_the_half(self) -> None:
        """08:15 ET is a real release slot; rounding to :30 would be wrong."""
        assert expected_release_time(ADP_CLOSE, series_ticker="KXADP") == datetime(
            2026, 9, 2, 12, 15, tzinfo=UTC
        )

    def test_a_naive_close_time_is_refused_not_assumed_utc(self) -> None:
        naive = CPI_CLOSE.replace(tzinfo=None)
        assert expected_release_time(naive, series_ticker="KXCPI") is None

    def test_a_close_far_from_a_slot_is_refused(self) -> None:
        """A close at :00 tells us nothing about a publication time."""
        flat = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
        assert expected_release_time(flat, series_ticker="KXCPI") is None

    def test_a_close_exactly_on_a_slot_does_not_claim_that_slot(self) -> None:
        on_slot = datetime(2026, 8, 12, 12, 30, tzinfo=UTC)
        assert expected_release_time(on_slot, series_ticker="KXCPI") is None

    def test_six_minutes_of_lead_is_beyond_the_observed_convention(self) -> None:
        five = datetime(2026, 8, 12, 12, 25, tzinfo=UTC)
        six = datetime(2026, 8, 12, 12, 24, tzinfo=UTC)
        assert expected_release_time(five, series_ticker="KXCPI") is not None
        assert expected_release_time(six, series_ticker="KXCPI") is None

    def test_result_is_always_timezone_aware_utc(self) -> None:
        out = expected_release_time(CPI_CLOSE, series_ticker="KXCPI")
        assert out is not None and out.tzinfo is not None
        assert out.utcoffset() == timedelta(0)

    def test_a_non_utc_aware_close_is_converted_not_rejected(self) -> None:
        """08:25 ET is the same instant as 12:25 UTC and must infer 12:30."""
        eastern = timezone(timedelta(hours=-4))
        same_instant = datetime(2026, 8, 12, 8, 25, tzinfo=eastern)
        assert expected_release_time(
            same_instant, series_ticker="KXCPI"
        ) == datetime(2026, 8, 12, 12, 30, tzinfo=UTC)

    def test_release_is_strictly_after_close_for_every_known_series(self) -> None:
        """The defining property: you cannot trade the print."""
        for ticker in KNOWN_CATALYSTS:
            out = expected_release_time(CPI_CLOSE, series_ticker=ticker)
            assert out is not None and out > CPI_CLOSE


# ---------------------------------------------------------------------------
# Window state
# ---------------------------------------------------------------------------


class TestWindowState:
    def test_far_in_the_future_is_open(self) -> None:
        c = _make(minutes_before=60 * 24 * 30)
        assert c is not None and c.state is WindowState.OPEN
        assert c.actionable is True

    def test_inside_the_horizon_is_closing_soon(self) -> None:
        c = _make(minutes_before=40)
        assert c is not None and c.state is WindowState.CLOSING_SOON
        assert c.actionable is True
        assert c.minutes_to_close == 40.0

    def test_the_closing_soon_boundary_is_inclusive(self) -> None:
        """A warning that arrives late is not a warning."""
        at = _make(minutes_before=60)
        just_outside = _make(minutes_before=60.001)
        assert at is not None and at.state is WindowState.CLOSING_SOON
        assert just_outside is not None and just_outside.state is WindowState.OPEN

    def test_horizon_is_configurable(self) -> None:
        c = _make(minutes_before=120, closing_soon_minutes=180)
        assert c is not None and c.state is WindowState.CLOSING_SOON

    def test_now_exactly_at_close_is_already_closed(self) -> None:
        """Trading has stopped at the close instant; do not round in favour."""
        c = _make(minutes_before=0)
        assert c is not None
        assert c.state is WindowState.CLOSED_PENDING_SETTLEMENT
        assert c.actionable is False
        assert c.minutes_to_close == 0.0

    def test_past_close_is_pending_settlement_with_negative_minutes(self) -> None:
        c = _make(minutes_before=-12)
        assert c is not None
        assert c.state is WindowState.CLOSED_PENDING_SETTLEMENT
        assert c.minutes_to_close == -12.0

    def test_settled_overrides_an_open_window(self) -> None:
        """Settlement is the caller's observation, not ours to infer."""
        c = _make(minutes_before=500, settled=True)
        assert c is not None and c.state is WindowState.SETTLED
        assert c.actionable is False

    def test_a_past_close_alone_never_implies_settled(self) -> None:
        c = _make(minutes_before=-10_000)
        assert c is not None and c.state is WindowState.CLOSED_PENDING_SETTLEMENT


# ---------------------------------------------------------------------------
# The trap this module exists to avoid
# ---------------------------------------------------------------------------


class TestActionable:
    def test_actionable_is_false_once_the_number_is_public(self) -> None:
        """The naive catalyst detector's signal is precisely this state.

        One minute after KXPAYROLLS closes the NFP print is seconds away, and
        thirty seconds later it is public knowledge. Neither instant is
        tradeable, and both must report ``actionable is False``.
        """
        release = expected_release_time(PAYROLLS_CLOSE, series_ticker="KXPAYROLLS")
        assert release is not None

        at_release = catalyst_for(
            series_ticker="KXPAYROLLS", close_time=PAYROLLS_CLOSE, now=release
        )
        after_release = catalyst_for(
            series_ticker="KXPAYROLLS",
            close_time=PAYROLLS_CLOSE,
            now=release + timedelta(minutes=30),
        )
        for c in (at_release, after_release):
            assert c is not None
            assert c.state is WindowState.CLOSED_PENDING_SETTLEMENT
            assert c.actionable is False

    def test_actionable_holds_only_for_open_and_closing_soon(self) -> None:
        actionable = {WindowState.OPEN, WindowState.CLOSING_SOON}
        for state in WindowState:
            c = Catalyst(
                series_ticker="KXCPI",
                label="CPI",
                close_time=CPI_CLOSE,
                expected_release=None,
                state=state,
                minutes_to_close=1.0,
            )
            assert c.actionable is (state in actionable)

    def test_a_catalyst_carries_its_inferred_release(self) -> None:
        c = _make()
        assert c is not None
        assert c.expected_release == datetime(2026, 8, 12, 12, 30, tzinfo=UTC)
        assert c.expected_release > c.close_time

    def test_expected_release_is_none_when_close_breaks_convention(self) -> None:
        """A known series with an odd close still refuses to guess."""
        c = _make(close=datetime(2026, 8, 12, 15, 0, tzinfo=UTC))
        assert c is not None and c.expected_release is None


# ---------------------------------------------------------------------------
# Argument refusals
# ---------------------------------------------------------------------------


class TestArgumentRefusals:
    def test_a_naive_close_time_is_refused(self) -> None:
        assert catalyst_for(
            series_ticker="KXCPI",
            close_time=CPI_CLOSE.replace(tzinfo=None),
            now=CPI_CLOSE - timedelta(hours=1),
        ) is None

    def test_a_naive_now_is_refused(self) -> None:
        assert catalyst_for(
            series_ticker="KXCPI",
            close_time=CPI_CLOSE,
            now=(CPI_CLOSE - timedelta(hours=1)).replace(tzinfo=None),
        ) is None

    def test_a_negative_horizon_is_refused(self) -> None:
        assert _make(closing_soon_minutes=-1) is None

    def test_a_zero_horizon_is_allowed_and_never_warns(self) -> None:
        c = _make(minutes_before=0.5, closing_soon_minutes=0)
        assert c is not None and c.state is WindowState.OPEN

    def test_mixed_zones_compare_by_instant(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        c = catalyst_for(
            series_ticker="KXCPI",
            close_time=CPI_CLOSE,
            now=datetime(2026, 8, 12, 8, 0, tzinfo=eastern),  # == 12:00 UTC
        )
        assert c is not None
        assert c.minutes_to_close == 25.0
        assert c.state is WindowState.CLOSING_SOON

    def test_close_time_is_normalised_to_utc_on_the_result(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        c = _make(close=datetime(2026, 8, 12, 8, 25, tzinfo=eastern))
        assert c is not None
        assert c.close_time == CPI_CLOSE
        assert c.close_time.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestUpcoming:
    def _mixed(self) -> list[Catalyst]:
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        made = [
            catalyst_for(series_ticker=t, close_time=c, now=now)
            for t, c in (
                ("KXGDP", GDP_CLOSE),  # open, soonest of the live ones
                ("KXCPI", CPI_CLOSE),  # open, later
                ("KXPAYROLLS", PAYROLLS_CLOSE),  # open, latest
                ("KXFEDDECISION", now - timedelta(minutes=5)),  # already closed
            )
        ]
        out = [c for c in made if c is not None]
        assert len(out) == 4
        return out

    def test_closed_markets_sort_below_open_ones(self) -> None:
        """Chronologically nearest is not the same as most useful."""
        ordered = upcoming(self._mixed())
        assert [c.series_ticker for c in ordered] == [
            "KXGDP",
            "KXCPI",
            "KXPAYROLLS",
            "KXFEDDECISION",
        ]
        assert ordered[-1].actionable is False

    def test_limit_truncates_from_the_front(self) -> None:
        ordered = upcoming(self._mixed(), limit=2)
        assert [c.series_ticker for c in ordered] == ["KXGDP", "KXCPI"]

    def test_a_nonpositive_limit_returns_nothing(self) -> None:
        """A bare slice would quietly drop from the end instead."""
        assert upcoming(self._mixed(), limit=0) == []
        assert upcoming(self._mixed(), limit=-1) == []

    def test_empty_input_is_fine(self) -> None:
        assert upcoming([]) == []

    def test_input_sequence_is_not_mutated(self) -> None:
        items = self._mixed()
        before = list(items)
        upcoming(items)
        assert items == before


# ---------------------------------------------------------------------------
# Structural discipline
# ---------------------------------------------------------------------------


class TestNoEdgeSurface:
    def test_the_module_exposes_nothing_that_values_a_trade(self) -> None:
        """A calendar tells you when, never whether."""
        forbidden = {"edge", "ev", "fair", "value", "profit", "pnl", "price"}
        tokens: set[str] = set()
        for name in dir(cal):
            tokens.update(name.lower().strip("_").split("_"))
        assert not tokens & forbidden

    def test_decimal_is_not_imported(self) -> None:
        """Structural proof there is no money math here to get wrong."""
        assert not hasattr(cal, "Decimal")

    def test_catalyst_has_no_monetary_fields(self) -> None:
        assert set(Catalyst.__dataclass_fields__) == {
            "series_ticker",
            "label",
            "close_time",
            "expected_release",
            "state",
            "minutes_to_close",
        }

    def test_catalyst_is_frozen(self) -> None:
        c = _make()
        assert c is not None
        try:
            c.state = WindowState.OPEN  # type: ignore[misc]
        except AttributeError:
            return
        raise AssertionError("Catalyst must be immutable")

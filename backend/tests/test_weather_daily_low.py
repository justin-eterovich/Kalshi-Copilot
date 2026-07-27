"""Tests for the daily-low forecast assignment.

Separate from the rest of the NWS parsing tests because the rule this file
protects is counterintuitive and easy to "correct" into a bug: a daily **low**
is assigned by the forecast period's END date, where a daily **high** is
assigned by its START date.

An NWS night period runs 18:00 local to 06:00 the next morning and reports
that night's minimum. The coldest hour is just before sunrise, so "Monday
Night" reports a temperature that falls in *Tuesday's* calendar day and which
the Climatological Report attributes to Tuesday. Filing it under Monday would
shift every low in the book by a whole day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.weather.nws_parse import ForecastPeriod, daily_high, daily_low

CHICAGO = -5.0


def period(
    name: str,
    *,
    start_local: datetime,
    hours: float,
    daytime: bool | None,
    temp: float | None,
) -> ForecastPeriod:
    """Build a period from a *local* start, converting to the aware UTC the
    parser produces."""
    start = start_local - timedelta(hours=CHICAGO)
    return ForecastPeriod(
        number=None,
        name=name,
        start=start.replace(tzinfo=UTC),
        end=(start + timedelta(hours=hours)).replace(tzinfo=UTC),
        is_daytime=daytime,
        temperature_f=temp,
        raw_temperature=temp,
        raw_unit="F",
        source_utc_offset_hours=CHICAGO,
    )


def kmdw_forecast() -> list[ForecastPeriod]:
    """The real KMDW shape observed on 2026-07-27, offset -5.

        Overnight       Mon 04:00 -> Mon 06:00   78F
        Monday          Mon 06:00 -> Mon 18:00   90F
        Monday Night    Mon 18:00 -> Tue 06:00   71F
        Tuesday         Tue 06:00 -> Tue 18:00   77F
        Tuesday Night   Tue 18:00 -> Wed 06:00   67F
    """
    d = lambda h, m=0, day=27: datetime(2026, 7, day, h, m)  # noqa: E731
    return [
        period("Overnight", start_local=d(4), hours=2, daytime=False, temp=78.0),
        period("Monday", start_local=d(6), hours=12, daytime=True, temp=90.0),
        period(
            "Monday Night", start_local=d(18), hours=12, daytime=False, temp=71.0
        ),
        period(
            "Tuesday", start_local=d(6, day=28), hours=12, daytime=True, temp=77.0
        ),
        period(
            "Tuesday Night",
            start_local=d(18, day=28),
            hours=12,
            daytime=False,
            temp=67.0,
        ),
    ]


class TestDailyLowAssignment:
    def test_a_night_period_reports_the_following_days_low(self) -> None:
        """Monday Night's 71F is Tuesday's minimum, not Monday's.

        The single claim this whole module exists to get right.
        """
        assert daily_low(kmdw_forecast(), day=date(2026, 7, 28),
                         tz_offset_hours=CHICAGO) == 71.0

    def test_a_truncated_overnight_period_belongs_to_the_day_it_ends_on(
        self,
    ) -> None:
        """A forecast issued during the night carries an "Overnight" period
        that both starts and ends the same day.

        This is why the rule is "ends on the day" rather than the simpler
        "starts the day before", which would silently drop it.
        """
        assert daily_low(kmdw_forecast(), day=date(2026, 7, 27),
                         tz_offset_hours=CHICAGO) == 78.0

    def test_the_third_night_lands_on_the_third_day(self) -> None:
        assert daily_low(kmdw_forecast(), day=date(2026, 7, 29),
                         tz_offset_hours=CHICAGO) == 67.0

    def test_high_and_low_for_one_day_come_from_different_periods(self) -> None:
        """The asymmetry, stated as a single assertion.

        Tuesday's high is the Tuesday daytime period; Tuesday's low is the
        period *named* "Monday Night".
        """
        periods = kmdw_forecast()
        day = date(2026, 7, 28)
        assert daily_high(periods, day=day, tz_offset_hours=CHICAGO) == 77.0
        assert daily_low(periods, day=day, tz_offset_hours=CHICAGO) == 71.0

    def test_a_daytime_period_never_supplies_a_low(self) -> None:
        """A daytime period reports a high; including it would mix two
        different statistics."""
        only_day = [
            p for p in kmdw_forecast() if p.is_daytime
        ]
        assert daily_low(only_day, day=date(2026, 7, 27),
                         tz_offset_hours=CHICAGO) is None


class TestDailyLowRefusals:
    def test_a_day_with_no_period_ending_on_it_is_refused(self) -> None:
        assert daily_low(kmdw_forecast(), day=date(2026, 8, 15),
                         tz_offset_hours=CHICAGO) is None

    def test_an_unknown_daytime_flag_refuses_the_day(self) -> None:
        """It can be neither included nor excluded honestly."""
        periods = kmdw_forecast()
        periods[0] = period(
            "Overnight",
            start_local=datetime(2026, 7, 27, 4),
            hours=2,
            daytime=None,
            temp=78.0,
        )
        assert daily_low(periods, day=date(2026, 7, 27),
                         tz_offset_hours=CHICAGO) is None

    def test_an_unreadable_temperature_refuses_rather_than_understating(
        self,
    ) -> None:
        """An overstated low from a partial minimum is entirely believable."""
        periods = kmdw_forecast()
        periods[2] = period(
            "Monday Night",
            start_local=datetime(2026, 7, 27, 18),
            hours=12,
            daytime=False,
            temp=None,
        )
        assert daily_low(periods, day=date(2026, 7, 28),
                         tz_offset_hours=CHICAGO) is None

    def test_a_period_with_no_end_cannot_be_placed(self) -> None:
        periods = kmdw_forecast()
        p = periods[2]
        periods[2] = ForecastPeriod(
            number=p.number, name=p.name, start=p.start, end=None,
            is_daytime=p.is_daytime, temperature_f=p.temperature_f,
            raw_temperature=p.raw_temperature, raw_unit=p.raw_unit,
            source_utc_offset_hours=p.source_utc_offset_hours,
        )
        assert daily_low(periods, day=date(2026, 7, 28),
                         tz_offset_hours=CHICAGO) is None

class TestEndBasedAssignmentIsTheRobustChoice:
    """Why the low rule keys on the end rather than the start.

    A night period ends at 06:00 local, which is late morning in UTC for every
    US station — far from a date boundary in either frame. Its *start* is
    18:00 local, which for a Pacific station is already past midnight UTC. So
    a start-based rule is sensitive to the offset being right and an end-based
    one largely is not.
    """

    def test_a_pacific_night_starts_on_the_next_utc_day(self) -> None:
        """The fact that makes a start-based rule fragile: 18:00 PDT is 01:00
        UTC *tomorrow*."""
        start_local = datetime(2026, 7, 27, 18)
        start_utc = (start_local + timedelta(hours=7)).replace(tzinfo=UTC)
        assert start_utc.date() == date(2026, 7, 28)
        assert start_local.date() == date(2026, 7, 27)

    def test_the_low_is_correct_even_with_the_offset_omitted(self) -> None:
        """Same periods, read with the deliberately-wrong UTC default: the
        answer survives, because 06:00 local is mid-morning UTC."""
        periods = kmdw_forecast()
        assert daily_low(periods, day=date(2026, 7, 28),
                         tz_offset_hours=CHICAGO) == 71.0
        assert daily_low(periods, day=date(2026, 7, 28),
                         tz_offset_hours=0.0) == 71.0

    def test_a_start_based_rule_would_file_a_pacific_night_a_day_early(
        self,
    ) -> None:
        """Demonstrates the bug the end-based rule avoids.

        A Pacific night forecast for 2026-07-28's low starts 2026-07-27 18:00
        local. Keyed by start it is the 27th's; keyed by end it is the 28th's,
        which is the day the Climatological Report attributes it to.
        """
        pacific = -7.0
        start = (datetime(2026, 7, 27, 18) - timedelta(hours=pacific)).replace(
            tzinfo=UTC
        )
        night = ForecastPeriod(
            number=None, name="Monday Night", start=start,
            end=start + timedelta(hours=12), is_daytime=False,
            temperature_f=54.0, raw_temperature=54.0, raw_unit="F",
            source_utc_offset_hours=pacific,
        )
        shift = timedelta(hours=pacific)
        assert (night.start + shift).date() == date(2026, 7, 27)
        assert night.end is not None
        assert (night.end + shift).date() == date(2026, 7, 28)
        assert daily_low([night], day=date(2026, 7, 28),
                         tz_offset_hours=pacific) == 54.0
        assert daily_low([night], day=date(2026, 7, 27),
                         tz_offset_hours=pacific) is None

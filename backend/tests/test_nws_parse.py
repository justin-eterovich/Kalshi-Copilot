"""Tests for the NWS parsing layer.

The fixtures below are trimmed copies of real ``api.weather.gov`` responses
captured for KMDW (Chicago Midway) and KNYC (NYC Central Park) — the two
stations these markets actually settle on — so the shapes are not guesses.

Refusals are tested harder than the happy path on purpose: every failure this
module exists to prevent produces a number that looks like ordinary weather.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.weather.nws_parse import (
    ForecastPeriod,
    celsius_to_fahrenheit,
    daily_high,
    parse_forecast_periods,
    parse_observation,
    running_high_f,
    temperature_to_fahrenheit,
)

# --- captured payloads -----------------------------------------------------

# api.weather.gov/stations/KMDW/observations/latest, trimmed.
KMDW_LATEST: dict = {
    "id": "https://api.weather.gov/stations/KMDW/observations/2026-07-27T09:50:00+00:00",
    "type": "Feature",
    "properties": {
        "station": "https://api.weather.gov/stations/KMDW",
        "stationId": "KMDW",
        "stationName": "Chicago, Chicago Midway Airport",
        "timestamp": "2026-07-27T09:50:00+00:00",
        "textDescription": "Fog/Mist",
        "temperature": {"unitCode": "wmoUnit:degC", "value": 26, "qualityControl": "V"},
        "dewpoint": {"unitCode": "wmoUnit:degC", "value": 25, "qualityControl": "V"},
        # Real payload: null with a `Z` flag, and no CLI-style daily extremes.
        "windGust": {"unitCode": "wmoUnit:km_h-1", "value": None, "qualityControl": "Z"},
        "maxTemperatureLast24Hours": {"unitCode": "wmoUnit:degC", "value": None},
        "minTemperatureLast24Hours": {"unitCode": "wmoUnit:degC", "value": None},
    },
}

# api.weather.gov/stations/KNYC/observations/latest, trimmed.
KNYC_LATEST: dict = {
    "type": "Feature",
    "properties": {
        "stationId": "KNYC",
        "timestamp": "2026-07-27T08:51:00+00:00",
        "temperature": {
            "unitCode": "wmoUnit:degC",
            "value": 21.7,
            "qualityControl": "V",
        },
        "rawMessage": "KNYC 270851Z 00000KT 10SM CLR 22/16 A2984 RMK AO2 SLP095",
    },
}

# api.weather.gov/gridpoints/LOT/72,69/forecast, trimmed. Note the shape:
# a bare `temperature` beside a one-letter `temperatureUnit`, and start/end
# stamped in the forecast office's LOCAL offset, not UTC.
LOT_FORECAST: dict = {
    "type": "Feature",
    "properties": {
        "units": "us",
        "periods": [
            {
                "number": 1,
                "name": "Overnight",
                "startTime": "2026-07-27T03:00:00-05:00",
                "endTime": "2026-07-27T06:00:00-05:00",
                "isDaytime": False,
                "temperature": 78,
                "temperatureUnit": "F",
            },
            {
                "number": 2,
                "name": "Monday",
                "startTime": "2026-07-27T06:00:00-05:00",
                "endTime": "2026-07-27T18:00:00-05:00",
                "isDaytime": True,
                "temperature": 90,
                "temperatureUnit": "F",
            },
            {
                "number": 3,
                "name": "Monday Night",
                "startTime": "2026-07-27T18:00:00-05:00",
                "endTime": "2026-07-28T06:00:00-05:00",
                "isDaytime": False,
                "temperature": 71,
                "temperatureUnit": "F",
            },
            {
                "number": 4,
                "name": "Tuesday",
                "startTime": "2026-07-28T06:00:00-05:00",
                "endTime": "2026-07-28T18:00:00-05:00",
                "isDaytime": True,
                "temperature": 77,
                "temperatureUnit": "F",
            },
        ],
    },
}


def _period(
    start: str,
    *,
    is_daytime: bool | None = True,
    temperature_f: float | None = 80.0,
) -> ForecastPeriod:
    return ForecastPeriod(
        number=None,
        name=None,
        start=datetime.fromisoformat(start).astimezone(UTC),
        end=None,
        is_daytime=is_daytime,
        temperature_f=temperature_f,
        raw_temperature=temperature_f,
        raw_unit="F",
        source_utc_offset_hours=-5.0,
    )


class TestCelsiusToFahrenheit:
    def test_converts_the_reference_points_exactly(self) -> None:
        assert celsius_to_fahrenheit(0.0) == 32.0
        assert celsius_to_fahrenheit(100.0) == 212.0
        assert celsius_to_fahrenheit(-40.0) == -40.0

    def test_converts_a_real_observed_value(self) -> None:
        # KMDW reported 26C; the market trades in F.
        assert celsius_to_fahrenheit(26.0) == pytest.approx(78.8)


class TestTemperatureToFahrenheit:
    def test_trusts_the_unit_code_not_the_field_name(self) -> None:
        assert temperature_to_fahrenheit(90, "wmoUnit:degF") == 90.0
        assert temperature_to_fahrenheit(32, "wmoUnit:degC") == pytest.approx(89.6)

    def test_accepts_the_bare_letters_the_forecast_product_uses(self) -> None:
        assert temperature_to_fahrenheit(90, "F") == 90.0
        assert temperature_to_fahrenheit(26, "C") == pytest.approx(78.8)

    def test_refuses_an_unrecognised_unit_rather_than_assuming_celsius(self) -> None:
        # Assuming Celsius here is a ~40-degree error that reads as weather.
        assert temperature_to_fahrenheit(295, "wmoUnit:K") is None
        assert temperature_to_fahrenheit(295, "K") is None
        assert temperature_to_fahrenheit(26, "wmoUnit:degrees") is None

    def test_refuses_a_missing_or_malformed_unit(self) -> None:
        assert temperature_to_fahrenheit(26, None) is None
        assert temperature_to_fahrenheit(26, "") is None
        assert temperature_to_fahrenheit(26, "wmoUnit:") is None
        assert temperature_to_fahrenheit(26, 42) is None

    def test_refuses_null_rather_than_returning_zero(self) -> None:
        # The whole point: null is routine, and 0F is a plausible reading.
        assert temperature_to_fahrenheit(None, "wmoUnit:degC") is None

    def test_refuses_a_boolean_masquerading_as_a_number(self) -> None:
        # isinstance(True, int) is True in Python; True must not be 1 degree.
        assert temperature_to_fahrenheit(True, "wmoUnit:degC") is None
        assert temperature_to_fahrenheit(False, "wmoUnit:degF") is None

    def test_refuses_a_numeric_string(self) -> None:
        # Nothing in the real payloads is a string; accepting one invites
        # "22,2" to be read as 222.
        assert temperature_to_fahrenheit("26", "wmoUnit:degC") is None

    def test_refuses_nan_and_infinity(self) -> None:
        # Python's json module parses these literals by default, and a NaN
        # slides through max() without complaint.
        assert temperature_to_fahrenheit(float("nan"), "wmoUnit:degC") is None
        assert temperature_to_fahrenheit(float("inf"), "wmoUnit:degC") is None

    def test_zero_celsius_survives_conversion(self) -> None:
        # A real 0C reading must not be confused with "missing".
        assert temperature_to_fahrenheit(0, "wmoUnit:degC") == 32.0


class TestParseObservation:
    def test_parses_a_real_kmdw_payload_into_fahrenheit(self) -> None:
        obs = parse_observation(KMDW_LATEST)
        assert obs is not None
        assert obs.station_id == "KMDW"
        assert obs.temperature_f == pytest.approx(78.8)
        assert obs.raw_value == 26.0
        assert obs.raw_unit_code == "wmoUnit:degC"

    def test_parses_a_real_knyc_payload(self) -> None:
        obs = parse_observation(KNYC_LATEST)
        assert obs is not None
        assert obs.temperature_f == pytest.approx(71.06)

    def test_returns_timezone_aware_utc(self) -> None:
        obs = parse_observation(KMDW_LATEST)
        assert obs is not None
        assert obs.observed_at.tzinfo is not None
        assert obs.observed_at == datetime(2026, 7, 27, 9, 50, tzinfo=UTC)

    def test_normalises_an_offset_timestamp_to_utc(self) -> None:
        payload = {
            "properties": {
                "timestamp": "2026-07-27T04:50:00-05:00",
                "temperature": {"unitCode": "wmoUnit:degC", "value": 26},
            }
        }
        obs = parse_observation(payload)
        assert obs is not None
        assert obs.observed_at == datetime(2026, 7, 27, 9, 50, tzinfo=UTC)

    def test_surfaces_the_quality_control_flag_without_filtering_on_it(self) -> None:
        obs = parse_observation(KMDW_LATEST)
        assert obs is not None
        assert obs.quality_control == "V"

        suspect = {
            "properties": {
                "timestamp": "2026-07-27T09:50:00+00:00",
                "temperature": {
                    "unitCode": "wmoUnit:degC",
                    "value": 26,
                    "qualityControl": "S",
                },
            }
        }
        flagged = parse_observation(suspect)
        assert flagged is not None
        # Surfaced, still parsed. We do not have documented semantics that
        # would justify discarding it here.
        assert flagged.quality_control == "S"
        assert flagged.temperature_f == pytest.approx(78.8)

    def test_accepts_a_bare_properties_dict(self) -> None:
        obs = parse_observation(KMDW_LATEST["properties"])
        assert obs is not None
        assert obs.temperature_f == pytest.approx(78.8)

    # --- refusals ---

    def test_null_temperature_yields_none_not_zero(self) -> None:
        payload = {
            "properties": {
                "timestamp": "2026-07-27T09:50:00+00:00",
                "temperature": {
                    "unitCode": "wmoUnit:degC",
                    "value": None,
                    "qualityControl": "Z",
                },
            }
        }
        obs = parse_observation(payload)
        assert obs is not None
        assert obs.temperature_f is None
        assert obs.raw_value is None
        assert obs.quality_control == "Z"

    def test_unknown_unit_refuses_but_keeps_the_evidence(self) -> None:
        payload = {
            "properties": {
                "timestamp": "2026-07-27T09:50:00+00:00",
                "temperature": {"unitCode": "wmoUnit:K", "value": 299.15},
            }
        }
        obs = parse_observation(payload)
        assert obs is not None
        assert obs.temperature_f is None
        # raw_value present + temperature_f absent is how the caller tells an
        # API change apart from a routine null.
        assert obs.raw_value == 299.15
        assert obs.raw_unit_code == "wmoUnit:K"

    def test_refuses_a_naive_timestamp(self) -> None:
        payload = {
            "properties": {
                "timestamp": "2026-07-27T09:50:00",
                "temperature": {"unitCode": "wmoUnit:degC", "value": 26},
            }
        }
        # Assuming UTC would be a silent off-by-hours error downstream.
        assert parse_observation(payload) is None

    def test_refuses_a_missing_or_unparseable_timestamp(self) -> None:
        temp = {"unitCode": "wmoUnit:degC", "value": 26}
        assert parse_observation({"properties": {"temperature": temp}}) is None
        assert (
            parse_observation({"properties": {"timestamp": "", "temperature": temp}})
            is None
        )
        assert (
            parse_observation(
                {"properties": {"timestamp": "yesterday", "temperature": temp}}
            )
            is None
        )
        # Epoch seconds are not an NWS timestamp; not an invitation to guess.
        epoch = {"properties": {"timestamp": 1769509800, "temperature": temp}}
        assert parse_observation(epoch) is None

    def test_refuses_a_non_dict_payload(self) -> None:
        assert parse_observation({}) is None
        assert parse_observation(None) is None  # type: ignore[arg-type]
        assert parse_observation([]) is None  # type: ignore[arg-type]

    def test_missing_temperature_block_is_none_not_an_error(self) -> None:
        payload = {"properties": {"timestamp": "2026-07-27T09:50:00+00:00"}}
        obs = parse_observation(payload)
        assert obs is not None
        assert obs.temperature_f is None


class TestParseForecastPeriods:
    def test_parses_the_real_forecast_product(self) -> None:
        periods = parse_forecast_periods(LOT_FORECAST)
        assert len(periods) == 4
        monday = periods[1]
        assert monday.name == "Monday"
        assert monday.is_daytime is True
        # Already Fahrenheit on the wire: passed through, not converted.
        assert monday.temperature_f == 90.0
        assert monday.raw_unit == "F"

    def test_converts_a_period_reported_in_celsius(self) -> None:
        # `?units=si` flips temperatureUnit to "C" with no other change. A
        # parser that assumed the US default would be 30+ degrees out.
        payload = {
            "properties": {
                "periods": [
                    {
                        "number": 2,
                        "name": "Monday",
                        "startTime": "2026-07-27T06:00:00-05:00",
                        "endTime": "2026-07-27T18:00:00-05:00",
                        "isDaytime": True,
                        "temperature": 32,
                        "temperatureUnit": "C",
                    }
                ]
            }
        }
        (period,) = parse_forecast_periods(payload)
        assert period.temperature_f == pytest.approx(89.6)

    def test_start_and_end_become_utc(self) -> None:
        periods = parse_forecast_periods(LOT_FORECAST)
        assert periods[1].start == datetime(2026, 7, 27, 11, 0, tzinfo=UTC)
        assert periods[1].end == datetime(2026, 7, 27, 23, 0, tzinfo=UTC)

    def test_preserves_the_offices_local_offset(self) -> None:
        periods = parse_forecast_periods(LOT_FORECAST)
        assert periods[1].source_utc_offset_hours == -5.0

    # --- refusals ---

    def test_keeps_a_period_whose_temperature_is_unusable(self) -> None:
        # Dropping it would let daily_high compute a confident max over the
        # rest. It must survive as an explicit None so the day can be refused.
        payload = {
            "properties": {
                "periods": [
                    {
                        "startTime": "2026-07-27T06:00:00-05:00",
                        "isDaytime": True,
                        "temperature": None,
                        "temperatureUnit": "F",
                    }
                ]
            }
        }
        (period,) = parse_forecast_periods(payload)
        assert period.temperature_f is None
        assert period.raw_temperature is None

    def test_unknown_is_daytime_stays_none_rather_than_false(self) -> None:
        payload = {
            "properties": {
                "periods": [
                    {
                        "startTime": "2026-07-27T06:00:00-05:00",
                        "temperature": 90,
                        "temperatureUnit": "F",
                    }
                ]
            }
        }
        (period,) = parse_forecast_periods(payload)
        assert period.is_daytime is None

    def test_drops_only_periods_with_no_usable_start(self) -> None:
        payload = {
            "properties": {
                "periods": [
                    {"isDaytime": True, "temperature": 90, "temperatureUnit": "F"},
                    {
                        "startTime": "2026-07-27T06:00:00",  # naive: unplaceable
                        "isDaytime": True,
                        "temperature": 91,
                        "temperatureUnit": "F",
                    },
                    {
                        "startTime": "2026-07-27T06:00:00-05:00",
                        "isDaytime": True,
                        "temperature": 92,
                        "temperatureUnit": "F",
                    },
                ]
            }
        }
        periods = parse_forecast_periods(payload)
        assert len(periods) == 1
        assert periods[0].temperature_f == 92.0

    def test_returns_empty_for_a_payload_with_no_periods(self) -> None:
        assert parse_forecast_periods({}) == []
        assert parse_forecast_periods({"properties": {}}) == []
        assert parse_forecast_periods({"properties": {"periods": None}}) == []
        assert parse_forecast_periods(None) == []  # type: ignore[arg-type]


class TestDailyHigh:
    def test_returns_the_daytime_high_for_the_local_day(self) -> None:
        periods = parse_forecast_periods(LOT_FORECAST)
        got = daily_high(periods, day=date(2026, 7, 27), tz_offset_hours=-5.0)
        assert got == 90.0

    def test_reads_the_following_local_day(self) -> None:
        periods = parse_forecast_periods(LOT_FORECAST)
        got = daily_high(periods, day=date(2026, 7, 28), tz_offset_hours=-5.0)
        # Tuesday's daytime period, not Monday Night's 71 — which starts on
        # the 27th local and reports a low anyway.
        assert got == 77.0

    def test_ignores_night_periods_that_straddle_midnight(self) -> None:
        # "Monday Night" runs 18:00 Mon -> 06:00 Tue local at 71F. If it were
        # assigned to Tuesday it would still lose to 77, so make it win: a
        # parser that counted night periods would return 99 here.
        periods = [
            _period("2026-07-27T18:00:00-05:00", is_daytime=False, temperature_f=99.0),
            _period("2026-07-28T06:00:00-05:00", is_daytime=True, temperature_f=77.0),
        ]
        assert daily_high(periods, day=date(2026, 7, 28), tz_offset_hours=-5.0) == 77.0

    def test_the_offset_decides_the_day_and_the_answer(self) -> None:
        # A period starting 19:00 local on the 27th is 00:00 UTC on the 28th.
        # This is the off-by-one-day bug, made to fire.
        periods = [_period("2026-07-27T19:00:00-05:00", temperature_f=88.0)]
        assert daily_high(periods, day=date(2026, 7, 27), tz_offset_hours=-5.0) == 88.0
        assert daily_high(periods, day=date(2026, 7, 27)) is None
        assert daily_high(periods, day=date(2026, 7, 28)) == 88.0

    def test_takes_the_max_when_a_day_has_several_daytime_periods(self) -> None:
        periods = [
            _period("2026-07-27T06:00:00-05:00", temperature_f=84.0),
            _period("2026-07-27T12:00:00-05:00", temperature_f=91.0),
        ]
        assert daily_high(periods, day=date(2026, 7, 27), tz_offset_hours=-5.0) == 91.0

    # --- refusals ---

    def test_refuses_a_day_the_forecast_does_not_cover(self) -> None:
        periods = parse_forecast_periods(LOT_FORECAST)
        assert daily_high(periods, day=date(2026, 8, 15), tz_offset_hours=-5.0) is None

    def test_refuses_when_a_daytime_period_has_no_usable_temperature(self) -> None:
        # An understated max is entirely believable, so no max is given.
        periods = [
            _period("2026-07-27T06:00:00-05:00", temperature_f=84.0),
            _period("2026-07-27T12:00:00-05:00", temperature_f=None),
        ]
        assert daily_high(periods, day=date(2026, 7, 27), tz_offset_hours=-5.0) is None

    def test_refuses_when_any_period_on_the_day_has_unknown_daytime_flag(self) -> None:
        periods = [
            _period("2026-07-27T06:00:00-05:00", temperature_f=84.0),
            _period("2026-07-27T18:00:00-05:00", is_daytime=None, temperature_f=99.0),
        ]
        assert daily_high(periods, day=date(2026, 7, 27), tz_offset_hours=-5.0) is None

    def test_refuses_a_day_with_only_night_periods(self) -> None:
        periods = [
            _period("2026-07-27T18:00:00-05:00", is_daytime=False, temperature_f=71.0)
        ]
        assert daily_high(periods, day=date(2026, 7, 27), tz_offset_hours=-5.0) is None

    def test_refuses_an_empty_sequence(self) -> None:
        assert daily_high([], day=date(2026, 7, 27)) is None

    def test_a_freezing_forecast_is_a_number_not_a_refusal(self) -> None:
        periods = [_period("2026-01-15T06:00:00-06:00", temperature_f=0.0)]
        assert daily_high(periods, day=date(2026, 1, 15), tz_offset_hours=-6.0) == 0.0


class TestRunningHighF:
    def test_takes_the_max_of_usable_observations(self) -> None:
        obs = [parse_observation(KMDW_LATEST), parse_observation(KNYC_LATEST)]
        assert all(o is not None for o in obs)
        assert running_high_f([o for o in obs if o is not None]) == pytest.approx(78.8)

    def test_refuses_when_nothing_carries_a_temperature(self) -> None:
        payload = {
            "properties": {
                "timestamp": "2026-07-27T09:50:00+00:00",
                "temperature": {"unitCode": "wmoUnit:degC", "value": None},
            }
        }
        obs = parse_observation(payload)
        assert obs is not None
        # "we saw nothing" must not share a representation with "it was cold".
        assert running_high_f([obs]) is None

    def test_refuses_an_empty_sequence(self) -> None:
        assert running_high_f([]) is None

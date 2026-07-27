"""Tests for weather settlement-rule parsing.

Every string in here marked "live" is copied verbatim out of ``rules_primary``
in the local database, including its punctuation, because the punctuation is
the part that varies between series and the part a parser gets wrong.

The happy path is four sentence shapes. The interesting half is the refusals:
the module's whole job is to not hand back a plausible station for a market it
does not actually understand, and most of the ways to fail here produce an
answer that looks perfectly reasonable.
"""

from __future__ import annotations

from app.weather.rules import Measure, SettlementSource, StationRef, parse_rules

# ---------------------------------------------------------------------------
# Live rules text. Family A settles on the NWS; family B does not.
# ---------------------------------------------------------------------------

# Family A, source clause *after* the comparison (KXHIGHCHI).
CHI_HIGH = (
    "If the highest temperature recorded at Chicago Midway, IL for July 26, 2026, "
    "is between 89-90° according to the National Weather Service's Climatological "
    "Report (Daily), then the market resolves to Yes."
)
# Family A, source clause *before* the comparison, preposition "in" (KXHIGHAUS).
AUS_HIGH = (
    "If the highest temperature recorded in Austin Bergstrom for July 26, 2026 as "
    "reported by the National Weather Service's Climatological Report (Daily), is "
    "between 96-97°, then the market resolves to Yes."
)
# Family A, "maximum" rather than "highest", explicit unit (KXHIGHTHOU).
HOU_HIGH = (
    "If the maximum temperature recorded at Houston for Jul 25, 2026, is between "
    "93-94° fahrenheit according to the National Weather Service's Climatological "
    "Report (Daily), then the market resolves to Yes."
)
# Family A, daily low (KXLOWTLV).
LV_LOW = (
    "If the minimum temperature recorded at Las Vegas for Jul 25, 2026, is between "
    "82-83° fahrenheit according to the National Weather Service's Climatological "
    "Report (Daily), then the market resolves to Yes."
)
# Family B, settled by The Weather Company (KXTEMPNYCH).
NYC_HOURLY = (
    "If the temperature recorded at Central Park, New York City for Jul 26, 2026 "
    "1 PM EDT as reported by The Weather Company (for coordinates KNYC), is above "
    "75.99°, then the market resolves to Yes."
)
# Family B, different city and code (KXTEMPAUSH).
AUS_HOURLY = (
    "If the temperature recorded at Austin, TX for Jul 26, 2026 1 PM EDT as "
    "reported by The Weather Company (for coordinates KAUS), is above 84.99°, "
    "then the market resolves to Yes."
)

# Live rules that are not station temperature markets at all.
RAIN_DAILY = (
    "If the total precipitation at CLIATL in Atlanta in Jul 25, 2026 is strictly "
    "greater than 0 inches, then the market resolves to Yes."
)
RAIN_MONTHLY = (
    "If the total precipitation at Central Park, New York City in Aug 2026 is "
    "strictly greater than 1 inches, then the market resolves to Yes."
)
GLOBAL_INDEX = (
    "If the unsmoothed Land-Ocean Temperature Index value for 2026 reported by "
    "NASA's Goddard Institute for Space Studies (GISS) is above the 2025 value "
    "and 1.28 degrees Celsius, then the market resolves to Yes."
)
MONTH_RANGE_INDEX = (
    "If the Land Ocean-Temperature Index for Aug 2026 is above 1.30, then the "
    "market resolves to Yes."
)
WARMING = (
    "If the annual global mean surface temperature anomaly has reached or "
    "exceeded +2.0°C above pre-industrial levels (1850-1900 average) in any "
    "calendar year before Jan 1, 2050, then the market resolves to Yes."
)
SUPERCONDUCTOR = (
    "If a peer-reviewed paper published in an eligible journal reports the "
    "discovery of a material exhibiting the Meissner effect at ambient pressure "
    "with a critical temperature of at least 240 K before January 1, 2027, the "
    "market resolves to Yes."
)
LOWES = (
    "If Lowe's Companies Inc. reports Above 220000000 customer transactions in "
    "Q2 2026, then the market resolves to Yes."
)
SNOWFLAKE_INC = (
    "If Snowflake Inc. reports Above 805 customers with trailing 12-month revenue "
    "$1m in Q2 2027, then the market resolves to Yes."
)
CPI = "If any Consumer Price Index (CPI YoY) report is above 4.2% for 2026, "
CPI += "then the market resolves to Yes."


def parsed(rules: str) -> StationRef:
    """Parse and assert success, so the happy-path tests stay readable."""
    ref = parse_rules(rules)
    assert ref is not None, f"expected a StationRef for: {rules[:60]}..."
    return ref


class TestDailyMarketsParseAsNwsSettled:
    def test_highest_with_trailing_source_clause_is_a_daily_high(self) -> None:
        ref = parsed(CHI_HIGH)
        assert ref.measure is Measure.DAILY_HIGH
        assert ref.source is SettlementSource.NWS_CLIMATOLOGICAL
        assert ref.station_text == "Chicago Midway, IL"

    def test_leading_source_clause_and_preposition_in_parse_the_same(self) -> None:
        # KXHIGHAUS says "recorded in ... as reported by"; KXHIGHCHI says
        # "recorded at ... according to". Same market type, same answer.
        ref = parsed(AUS_HIGH)
        assert ref.measure is Measure.DAILY_HIGH
        assert ref.source is SettlementSource.NWS_CLIMATOLOGICAL
        assert ref.station_text == "Austin Bergstrom"

    def test_maximum_is_the_same_measure_as_highest(self) -> None:
        ref = parsed(HOU_HIGH)
        assert ref.measure is Measure.DAILY_HIGH
        assert ref.station_text == "Houston"

    def test_minimum_is_a_daily_low(self) -> None:
        ref = parsed(LV_LOW)
        assert ref.measure is Measure.DAILY_LOW
        assert ref.source is SettlementSource.NWS_CLIMATOLOGICAL
        assert ref.station_text == "Las Vegas"

    def test_lowest_is_accepted_as_the_mirror_of_highest(self) -> None:
        ref = parsed(
            "If the lowest temperature recorded at Denver, CO for July 26, 2026, "
            "is less than 55° according to the National Weather Service's "
            "Climatological Report (Daily), then the market resolves to Yes."
        )
        assert ref.measure is Measure.DAILY_LOW

    def test_daily_markets_settle_from_nws(self) -> None:
        for rules in (CHI_HIGH, AUS_HIGH, HOU_HIGH, LV_LOW):
            assert parsed(rules).settles_from_nws is True

    def test_no_station_code_is_invented_for_daily_markets(self) -> None:
        # "Chicago Midway, IL" is almost certainly KMDW. The parser is not
        # allowed to act on "almost certainly" — the caller resolves it.
        assert parsed(CHI_HIGH).station_code is None
        assert parsed(LV_LOW).station_code is None


class TestHourlyMarketsAreNotNwsSettled:
    def test_hourly_market_parses_with_its_coordinate_code(self) -> None:
        ref = parsed(NYC_HOURLY)
        assert ref.measure is Measure.HOURLY
        assert ref.source is SettlementSource.WEATHER_COMPANY
        assert ref.station_text == "Central Park, New York City"
        assert ref.station_code == "KNYC"

    def test_a_second_hourly_series_gets_its_own_code(self) -> None:
        ref = parsed(AUS_HOURLY)
        assert ref.station_code == "KAUS"
        assert ref.station_text == "Austin, TX"

    def test_hourly_markets_are_flagged_as_not_settling_from_nws(self) -> None:
        # The whole point of the module. api.weather.gov is a proxy for these
        # and a caller that treats it as settlement is reading the wrong number.
        assert parsed(NYC_HOURLY).settles_from_nws is False
        assert parsed(AUS_HOURLY).settles_from_nws is False

    def test_the_two_families_are_distinguishable_at_the_same_location(self) -> None:
        # Central Park appears in both a Weather Company hourly market and an
        # NWS daily market. Location alone must never decide the source.
        hourly = parsed(NYC_HOURLY)
        daily = parsed(
            "If the highest temperature recorded at Central Park, New York City "
            "for Jul 26, 2026, is between 89-90° according to the National "
            "Weather Service's Climatological Report (Daily), then the market "
            "resolves to Yes."
        )
        assert hourly.station_text == daily.station_text
        assert hourly.settles_from_nws is not daily.settles_from_nws


class TestRefusesNonTemperatureMarkets:
    def test_daily_rainfall_is_refused(self) -> None:
        assert parse_rules(RAIN_DAILY) is None

    def test_monthly_rainfall_at_a_known_station_is_refused(self) -> None:
        # This one names Central Park, a station we do model, and is still not
        # a temperature market. Recognising the place is not understanding it.
        assert parse_rules(RAIN_MONTHLY) is None

    def test_snowfall_is_refused(self) -> None:
        assert (
            parse_rules(
                "If the total snowfall at Chicago Midway, IL for Jan 2027 is "
                "strictly greater than 4 inches, then the market resolves to Yes."
            )
            is None
        )

    def test_wind_speed_is_refused(self) -> None:
        assert (
            parse_rules(
                "If the maximum wind gust recorded at Boston, MA for Jul 26, 2026 "
                "is above 40 mph according to the National Weather Service's "
                "Climatological Report (Daily), then the market resolves to Yes."
            )
            is None
        )


class TestRefusesTemperatureMarketsWithNoStation:
    """The word "temperature" is not evidence that a station is involved."""

    def test_global_land_ocean_index_is_refused(self) -> None:
        assert parse_rules(GLOBAL_INDEX) is None

    def test_monthly_land_ocean_index_is_refused(self) -> None:
        assert parse_rules(MONTH_RANGE_INDEX) is None

    def test_global_warming_anomaly_is_refused(self) -> None:
        assert parse_rules(WARMING) is None

    def test_superconductor_critical_temperature_is_refused(self) -> None:
        # Live KXMEISSNER text. "a critical temperature of at least 240 K".
        assert parse_rules(SUPERCONDUCTOR) is None


class TestTickerLookalikesAreIrrelevant:
    """Series tickers that read like weather and are not.

    The module never sees a ticker, and these prove nothing in the prose
    accidentally lets one in.
    """

    def test_lowes_earnings_is_refused(self) -> None:
        assert parse_rules(LOWES) is None  # series KXLOW

    def test_snowflake_earnings_is_refused(self) -> None:
        assert parse_rules(SNOWFLAKE_INC) is None  # series KXSNOWFLAKE

    def test_cpi_is_refused(self) -> None:
        assert parse_rules(CPI) is None  # series KXHIGHINFLATION


class TestRefusesUnnamedOrCrossedSettlementSources:
    def test_no_named_source_is_refused(self) -> None:
        # Structurally a perfect family-A sentence, with the organisation
        # removed. There is no default source to fall back on.
        assert (
            parse_rules(
                "If the highest temperature recorded at Chicago Midway, IL for "
                "July 26, 2026, is between 89-90°, then the market resolves to Yes."
            )
            is None
        )

    def test_both_sources_named_is_ambiguous_and_refused(self) -> None:
        assert (
            parse_rules(
                "If the highest temperature recorded at Chicago Midway, IL for "
                "July 26, 2026, is between 89-90° according to the National "
                "Weather Service's Climatological Report (Daily) as confirmed by "
                "The Weather Company, then the market resolves to Yes."
            )
            is None
        )

    def test_a_different_nws_product_is_refused(self) -> None:
        # The NWS publishes more than one thing. Only the Climatological
        # Report (Daily) is the number we ingest and the number that settles.
        assert (
            parse_rules(
                "If the highest temperature recorded at Chicago Midway, IL for "
                "July 26, 2026, is between 89-90° according to the National "
                "Weather Service's Local Climate Analysis Tool, then the market "
                "resolves to Yes."
            )
            is None
        )

    def test_hourly_market_attributed_to_the_nws_is_refused(self) -> None:
        # This is the crossed pair that matters most: accepting it would mean
        # publishing an hourly market as NWS-settled.
        assert (
            parse_rules(
                "If the temperature recorded at Central Park, New York City for "
                "Jul 26, 2026 1 PM EDT as reported by the National Weather "
                "Service's Climatological Report (Daily), is above 75.99°, then "
                "the market resolves to Yes."
            )
            is None
        )

    def test_daily_high_attributed_to_the_weather_company_is_refused(self) -> None:
        assert (
            parse_rules(
                "If the highest temperature recorded at Chicago Midway, IL for "
                "July 26, 2026, is between 89-90° as reported by The Weather "
                "Company (for coordinates KORD), then the market resolves to Yes."
            )
            is None
        )


class TestRefusesUnrecognisedShapes:
    def test_none_is_refused(self) -> None:
        assert parse_rules(None) is None

    def test_empty_and_whitespace_are_refused(self) -> None:
        assert parse_rules("") is None
        assert parse_rules("   \n\t ") is None

    def test_unrelated_prose_is_refused(self) -> None:
        assert parse_rules("This market resolves per the underlying event.") is None

    def test_an_unknown_aggregate_word_does_not_fall_through_to_hourly(self) -> None:
        # "average" is not one of the four words we know. Treating an
        # unrecognised aggregate as hourly would mislabel every future wording.
        assert (
            parse_rules(
                "If the average temperature recorded at Chicago Midway, IL for "
                "July 26, 2026, is between 89-90° according to the National "
                "Weather Service's Climatological Report (Daily), then the "
                "market resolves to Yes."
            )
            is None
        )

    def test_hourly_shape_without_a_clock_hour_is_refused(self) -> None:
        # No aggregate word and no hour named. We cannot tell which number of
        # the day this is about, so there is nothing to compare an observation
        # against.
        assert (
            parse_rules(
                "If the temperature recorded at Central Park, New York City for "
                "Jul 26, 2026 as reported by The Weather Company (for "
                "coordinates KNYC), is above 75.99°, then the market resolves "
                "to Yes."
            )
            is None
        )

    def test_two_stations_in_one_rule_is_refused(self) -> None:
        # A compound rule has a settlement question that cannot be answered by
        # taking whichever station came first.
        assert (
            parse_rules(
                "If the highest temperature recorded at Chicago Midway, IL for "
                "July 26, 2026 exceeds the highest temperature recorded at "
                "Denver, CO for July 26, 2026 according to the National Weather "
                "Service's Climatological Report (Daily), then the market "
                "resolves to Yes."
            )
            is None
        )

    def test_a_missing_station_is_refused(self) -> None:
        assert (
            parse_rules(
                "If the highest temperature recorded at  for July 26, 2026, is "
                "between 89-90° according to the National Weather Service's "
                "Climatological Report (Daily), then the market resolves to Yes."
            )
            is None
        )

    def test_a_runaway_station_capture_is_refused_not_returned(self) -> None:
        # No " for <date>" clause, so the non-greedy capture runs on until the
        # next " for " much later in the sentence. The length and content
        # bounds turn that into a refusal instead of a sentence fragment
        # presented as a place name.
        ref = parse_rules(
            "If the highest temperature recorded at Chicago Midway, IL on July "
            "26, 2026 is between 89-90° according to the National Weather "
            "Service's Climatological Report (Daily), then the market resolves "
            "to Yes for the purposes of settlement."
        )
        assert ref is None


class TestFormattingTolerance:
    """Tolerant of formatting, never of meaning."""

    def test_line_wrapping_and_repeated_spaces_do_not_matter(self) -> None:
        wrapped = CHI_HIGH.replace(" for July", "\n   for  July")
        ref = parsed(wrapped)
        assert ref.station_text == "Chicago Midway, IL"
        assert ref.measure is Measure.DAILY_HIGH

    def test_leading_and_trailing_whitespace_do_not_matter(self) -> None:
        assert parsed("\n  " + NYC_HOURLY + "  \n").station_code == "KNYC"

    def test_the_trailing_comma_before_the_comparison_is_optional(self) -> None:
        without = CHI_HIGH.replace("July 26, 2026, is", "July 26, 2026 is")
        assert parsed(without).station_text == "Chicago Midway, IL"

    def test_source_name_casing_does_not_matter(self) -> None:
        shouted = CHI_HIGH.replace(
            "National Weather Service's Climatological Report (Daily)",
            "NATIONAL WEATHER SERVICE'S CLIMATOLOGICAL REPORT (DAILY)",
        )
        assert parsed(shouted).source is SettlementSource.NWS_CLIMATOLOGICAL

    def test_a_lowercase_coordinate_code_is_normalised(self) -> None:
        ref = parsed(NYC_HOURLY.replace("coordinates KNYC", "coordinates knyc"))
        assert ref.station_code == "KNYC"


class TestStationRefIsAValueObject:
    def test_it_is_frozen(self) -> None:
        ref = parsed(NYC_HOURLY)
        try:
            ref.station_text = "Somewhere Else"  # type: ignore[misc]
        except (AttributeError, TypeError):
            pass
        else:  # pragma: no cover - a mutable result would be a real bug
            raise AssertionError("StationRef must be immutable")

    def test_enum_members_are_stable_strings(self) -> None:
        # These values get persisted and compared across restarts.
        assert SettlementSource.NWS_CLIMATOLOGICAL == "nws_climatological"
        assert SettlementSource.WEATHER_COMPANY == "weather_company"
        assert Measure.DAILY_HIGH == "daily_high"
        assert Measure.DAILY_LOW == "daily_low"
        assert Measure.HOURLY == "hourly"

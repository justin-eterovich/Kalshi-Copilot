"""Tests for the series -> weather station table.

Every row in ``SERIES_STATIONS`` is **a claim about the world**, not something
the API told us — the same status as ``set_arbitrage.exhaustive_series``.
Nothing in a market payload identifies the thermometer it settles on, so a
human asserted it, and a wrong assertion fails silently: the model prices
Atlanta's weather off Athens' station and every number it produces looks
entirely reasonable.

The property this file mainly exists to keep is the one CLAUDE.md states and
only a test can enforce: **the lookup is exact and must stay exact.** Series
tickers are names, not a namespace. ``KXLOW`` is Lowe's Companies Inc. and
``KXSNOWFLAKE`` is Snowflake Inc.; a prefix match would route two earnings
markets to a weather station, and the resulting temperature forecast for a
retailer's quarterly results would be perfectly well-formed.
"""

from __future__ import annotations

import re

import pytest

from app.weather.stations import (
    SERIES_STATIONS,
    StationClaim,
    station_for_series,
)

#: NWS station identifiers are a K followed by three letters.
STATION_ID = re.compile(r"^K[A-Z]{3}$")


class TestExactLookup:
    def test_a_listed_series_resolves(self) -> None:
        claim = station_for_series("KXHIGHCHI")
        assert claim is not None
        assert claim.station_id == "KMDW"

    def test_an_unlisted_series_is_refused(self) -> None:
        """``None`` is a refusal the caller must honour by declining to price.

        There is deliberately no fallback to a nearby station or a city-name
        lookup.
        """
        assert station_for_series("KXHIGHTNOWHERE") is None

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_a_missing_series_is_refused(self, value: str | None) -> None:
        assert station_for_series(value) is None

    def test_case_and_whitespace_are_normalised(self) -> None:
        """Normalisation is not the same as fuzzy matching: it maps a value to
        exactly one key, or to nothing."""
        assert station_for_series("kxhighchi") == SERIES_STATIONS["KXHIGHCHI"]
        assert station_for_series("  KXHIGHCHI  ") == SERIES_STATIONS["KXHIGHCHI"]


class TestSeriesTickersAreNamesNotANamespace:
    """The trap, pinned. A prefix match here would be a disaster."""

    def test_lowes_is_not_a_weather_station(self) -> None:
        """``KXLOW`` is Lowe's Companies Inc.

        It is a prefix of ``KXLOWTATL``, ``KXLOWTBOS`` and eleven others, so a
        ``startswith`` in either direction routes an earnings market to a
        thermometer.
        """
        assert station_for_series("KXLOW") is None

    def test_snowflake_is_not_a_weather_station(self) -> None:
        """``KXSNOWFLAKE`` is Snowflake Inc., and reads like weather."""
        assert station_for_series("KXSNOWFLAKE") is None

    @pytest.mark.parametrize(
        "prefix", ["KXLOW", "KXHIGH", "KXHIGHT", "KXLOWT", "KX", "K"]
    )
    def test_a_bare_prefix_of_a_real_key_does_not_match(self, prefix: str) -> None:
        """Each of these is a genuine prefix of at least one listed series."""
        assert any(key.startswith(prefix) for key in SERIES_STATIONS), prefix
        assert station_for_series(prefix) is None

    def test_a_longer_string_starting_with_a_real_key_does_not_match(self) -> None:
        """Matching in the other direction is just as wrong."""
        assert station_for_series("KXHIGHCHICAGOSOMETHING") is None

    def test_a_full_market_ticker_is_not_a_series(self) -> None:
        """Callers pass ``Market.series_ticker``, not ``Market.ticker``.

        Accepting the full ticker would quietly make the lookup a prefix
        match by another name.
        """
        assert station_for_series("KXHIGHCHI-26JUL27-B86") is None

    def test_the_lookup_is_a_dict_get_not_a_scan(self) -> None:
        """Structural, because the behaviour above can be reproduced by a
        careful loop that a later edit then loosens."""
        import inspect

        source = inspect.getsource(station_for_series)
        assert "SERIES_STATIONS.get(" in source
        assert "startswith" not in source.split('"""')[-1]


class TestTableIsWellFormed:
    def test_every_key_is_upper_case_and_unpadded(self) -> None:
        """The lookup upper-cases its argument, so a lower-case key would be
        unreachable — a station nobody could ever resolve, which looks
        identical to a series nobody asserted."""
        for key in SERIES_STATIONS:
            assert key == key.strip().upper(), key

    def test_every_station_id_looks_like_an_nws_station(self) -> None:
        for key, claim in SERIES_STATIONS.items():
            assert STATION_ID.match(claim.station_id), (key, claim.station_id)

    def test_every_row_says_where_it_came_from(self) -> None:
        """``described_as`` is what a reviewer eyeballs against the ticker."""
        for key, claim in SERIES_STATIONS.items():
            assert claim.described_as.strip(), key

    def test_the_explicit_rows_are_the_ones_naming_a_station(self) -> None:
        """``explicit_in_rules`` marks a reading rather than an inference.

        Three series name their station outright; everything else is a human
        deciding which thermometer "Atlanta" means, and that decision can be
        wrong.
        """
        explicit = {k for k, v in SERIES_STATIONS.items() if v.explicit_in_rules}
        assert explicit == {"KXHIGHCHI", "KXHIGHMIA", "KXHIGHPHIL"}

    def test_chicago_has_two_different_thermometers(self) -> None:
        """Deliberate, and the clearest illustration of why a city name is not
        a station: ``KXHIGHCHI`` settles on Midway because its own rules say
        so, while the low market uses O'Hare, Chicago's climate site. They
        routinely disagree by a degree or two, which on two-degree buckets is
        the whole contract.
        """
        assert SERIES_STATIONS["KXHIGHCHI"].station_id == "KMDW"
        assert SERIES_STATIONS["KXLOWTCHI"].station_id == "KORD"

    def test_the_hourly_family_is_not_listed(self) -> None:
        """``KXTEMPNYCH`` settles on **The Weather Company**, not the NWS.

        We have no feed for it, so NWS data there is a proxy for a different
        source. Listing it would mean pricing one source's market off
        another's readings.
        """
        assert "KXTEMPNYCH" not in SERIES_STATIONS
        assert station_for_series("KXTEMPNYCH") is None

    def test_a_claim_is_immutable(self) -> None:
        """A frozen row cannot be edited in place by a caller that thinks it
        knows better at runtime."""
        claim = SERIES_STATIONS["KXHIGHCHI"]
        with pytest.raises((AttributeError, TypeError)):
            claim.station_id = "KORD"  # type: ignore[misc]

    def test_the_table_is_not_empty(self) -> None:
        """A table that silently emptied would refuse every weather market and
        look like "no signals today"."""
        assert len(SERIES_STATIONS) >= 20
        assert all(isinstance(v, StationClaim) for v in SERIES_STATIONS.values())

"""Which weather station a market actually settles on.

Kalshi's temperature markets name their location in prose, and they are not
consistent about how precisely. Some are unambiguous — "Miami International
Airport", "Chicago Midway, IL" — and some are just a city:

    If the maximum temperature recorded at *Atlanta* for Jul 26, 2026, is
    between 86-87° fahrenheit according to the National Weather Service's
    Climatological Report (Daily) ...

"Atlanta" is not a thermometer. The Climatological Report is issued per
station, and Atlanta's is Hartsfield-Jackson; a reading taken anywhere else in
the metro can differ by several degrees on the days that matter, which on a
book of two-degree buckets is the difference between the right bucket and a
confident loss.

**So every entry below is a claim about the world, not something the API told
us** — the same status as ``set_arbitrage.exhaustive_series``. Nothing in the
market payload identifies the station; this table is where a human asserts it.
A series that is not listed is **refused**, not guessed at, because the
failure mode of a wrong guess is silent: the model prices Atlanta's weather
using Athens' thermometer and every number it produces looks entirely
reasonable.

The one exception is the hourly family (``KXTEMPNYCH``), whose rules do name a
station code directly and which settles from **The Weather Company rather than
the NWS** — for those, NWS data is a proxy that can disagree with the thing
that actually settles the market. See :mod:`app.weather.rules`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["StationClaim", "SERIES_STATIONS", "station_for_series"]


@dataclass(frozen=True, slots=True)
class StationClaim:
    """An asserted mapping from a Kalshi series to an NWS station."""

    #: NWS station identifier, e.g. "KATL".
    station_id: str
    #: What the market's rules call it, for eyeballing against the ticker.
    described_as: str
    #: True when the rules name the station precisely enough that the mapping
    #: is a reading rather than an inference. False means a human decided
    #: which station "Atlanta" refers to, and that decision can be wrong.
    explicit_in_rules: bool = False


#: Series ticker -> station. Verified against the rules text in the catalog on
#: 2026-07-27. Add an entry only after reading that series' own rules; the
#: cost of a wrong row is a whole series priced off the wrong thermometer.
SERIES_STATIONS: dict[str, StationClaim] = {
    # -- rules name the station outright ---------------------------------
    "KXHIGHCHI": StationClaim("KMDW", "Chicago Midway, IL", True),
    "KXHIGHMIA": StationClaim("KMIA", "Miami International Airport", True),
    "KXHIGHPHIL": StationClaim("KPHL", "Philadelphia International Airport", True),
    # -- rules name only a city; the station is our assertion -------------
    # Each is that city's official NWS climate station, which is the site the
    # Climatological Report (Daily) is issued for.
    "KXHIGHTATL": StationClaim("KATL", "Atlanta"),
    "KXLOWTATL": StationClaim("KATL", "Atlanta"),
    "KXLOWTAUS": StationClaim("KAUS", "Austin"),
    "KXHIGHTBOS": StationClaim("KBOS", "Boston"),
    "KXLOWTBOS": StationClaim("KBOS", "Boston"),
    # Chicago's climate site is O'Hare. Note this deliberately differs from
    # KXHIGHCHI above, which says Midway in its own rules — same city, two
    # different thermometers, and they routinely disagree by a degree or two.
    "KXLOWTCHI": StationClaim("KORD", "Chicago"),
    "KXHIGHTDAL": StationClaim("KDFW", "Dallas"),
    "KXLOWTDAL": StationClaim("KDFW", "Dallas"),
    "KXHIGHTDC": StationClaim("KDCA", "Washington DC"),
    "KXLOWTDC": StationClaim("KDCA", "Washington DC"),
    "KXLOWTDEN": StationClaim("KDEN", "Denver"),
    "KXHIGHDEN": StationClaim("KDEN", "Denver"),
    "KXHIGHTHOU": StationClaim("KIAH", "Houston"),
    "KXLOWTHOU": StationClaim("KIAH", "Houston"),
    "KXHIGHTLV": StationClaim("KLAS", "Las Vegas"),
    "KXLOWTLV": StationClaim("KLAS", "Las Vegas"),
    "KXLOWTLAX": StationClaim("KLAX", "Los Angeles"),
    "KXLOWTMIA": StationClaim("KMIA", "Miami"),
    "KXHIGHTMIN": StationClaim("KMSP", "Minneapolis"),
    "KXLOWTMIN": StationClaim("KMSP", "Minneapolis"),
    "KXHIGHTNOLA": StationClaim("KMSY", "New Orleans"),
    "KXLOWTNOLA": StationClaim("KMSY", "New Orleans"),
    "KXLOWTNYC": StationClaim("KNYC", "New York City"),
    "KXHIGHTOKC": StationClaim("KOKC", "Oklahoma City"),
    "KXHIGHTPHX": StationClaim("KPHX", "Phoenix"),
    "KXHIGHTSATX": StationClaim("KSAT", "San Antonio"),
    "KXLOWTSATX": StationClaim("KSAT", "San Antonio"),
    "KXHIGHTSFO": StationClaim("KSFO", "San Francisco"),
    "KXLOWTSFO": StationClaim("KSFO", "San Francisco"),
    "KXHIGHTSEA": StationClaim("KSEA", "Seattle"),
}


def station_for_series(series_ticker: str | None) -> StationClaim | None:
    """The station a series settles on, or ``None`` if we have not asserted one.

    ``None`` is a refusal the caller must honour by declining to price the
    market. There is deliberately no fallback to a nearby station or a
    city-name lookup: a wrong station produces believable temperatures for the
    wrong place, and nothing downstream can tell.

    **The lookup is exact and must stay exact.** A prefix match would be a
    disaster here: ``KXLOW`` is Lowe's Companies Inc. and ``KXSNOWFLAKE`` is
    Snowflake Inc. — both earnings markets that ``startswith("KXLOW")`` and
    ``startswith("KXSNOW")`` would happily route to a weather station. Series
    tickers are not a namespace; they are names.
    """
    if not series_ticker:
        return None
    return SERIES_STATIONS.get(series_ticker.strip().upper())

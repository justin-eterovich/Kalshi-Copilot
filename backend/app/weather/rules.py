"""Reading a Kalshi weather market's settlement rules out of its prose.

Weather markets do not expose their settlement source as a field. It is a
sentence in ``rules_primary``, and there are two families of sentence that
look nearly identical and settle from **different organisations**:

- **Daily high/low** settles on the National Weather Service's Climatological
  Report (Daily). Our own data comes from ``api.weather.gov``, so for these
  markets we read the same source that settles them.
- **Hourly temperature** settles on *The Weather Company*, and names a
  station code (``for coordinates KNYC``) that is a coordinate hint, not an
  NWS station identity. For these, ``api.weather.gov`` is a **proxy** that
  can and does disagree with the thing that actually pays out.

**The dangerous version of this module is the one that greps for a place name
and a temperature word and hands back a station.** It would work on every
example anyone tests it against, because both families really do contain a
place, a date and the word "temperature". It would then quietly label the
hourly markets as NWS-settled, and a model built on that reads a settlement
feed for a market that settles somewhere else. That error is invisible
downstream: the observation is a real temperature, at a real station, on the
right day, and it is simply not the number that decides the contract. The
disagreement shows up as a small persistent mispricing, which is exactly what
a detector is built to chase.

So the source is never inferred from the shape of the market, only from the
organisation the rules **name in words**, and it is cross-checked against the
measure: a daily-high market attributed to The Weather Company, or an hourly
market attributed to the NWS, is a phrasing we have never seen and this
refuses it rather than picking whichever half looked more familiar.

Everything else refuses. Precipitation, snowfall and wind are not
temperature. Neither is a "Land-Ocean Temperature Index", a "global mean
surface temperature anomaly", or a superconductor's "critical temperature of
at least 240 K" — all three are real strings from live ``rules_primary`` that
contain the word *temperature* and have no station behind them at all. Half
of a weather market is not worth having; ``None`` is.

Note also what this module deliberately does **not** do: match on the series
ticker. ``KXLOW`` is Lowe's Companies Inc. and ``KXSNOWFLAKE`` is Snowflake
Inc. The ticker is not evidence about the weather.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Measure", "SettlementSource", "StationRef", "parse_rules"]


class SettlementSource(StrEnum):
    """The organisation whose number actually settles the market.

    Only these two appear in live rules text for station temperature markets.
    Nothing here is a default: a market whose source is not named in words is
    refused, not assigned.
    """

    #: National Weather Service, Climatological Report (Daily). This is the
    #: same source we ingest from api.weather.gov, so it settles what we see.
    NWS_CLIMATOLOGICAL = "nws_climatological"
    #: The Weather Company. We have no feed for it; api.weather.gov is a
    #: proxy that may disagree with settlement.
    WEATHER_COMPANY = "weather_company"


class Measure(StrEnum):
    """Which statistic of the day the market is written on."""

    DAILY_HIGH = "daily_high"
    DAILY_LOW = "daily_low"
    #: Temperature at one named clock hour, not an aggregate over the day.
    HOURLY = "hourly"


#: Wording seen for each daily aggregate. "highest"/"maximum" and
#: "minimum" all appear across live series; "lowest" does not appear today but
#: is the obvious mirror of "highest" and costs nothing to accept.
_MEASURE_WORDS: dict[str, Measure] = {
    "highest": Measure.DAILY_HIGH,
    "maximum": Measure.DAILY_HIGH,
    "lowest": Measure.DAILY_LOW,
    "minimum": Measure.DAILY_LOW,
}

#: The one sentence shape both families share:
#:
#:   "If the highest temperature recorded in Austin Bergstrom for July 26, ..."
#:   "If the maximum temperature recorded at Houston for Jul 25, 2026, is ..."
#:   "If the temperature recorded at Central Park, New York City for Jul 26 ..."
#:
#: The aggregate word is optional — its *absence* is what marks an hourly
#: market — and the preposition is "in" or "at" depending on the series, with
#: no meaning attached to which. The station runs to the first " for ", which
#: is the token that introduces the date in every observed variant.
_STATION_RE = re.compile(
    r"\bthe\s+(?P<measure>[a-z]+\s+)?temperature\s+recorded\s+"
    r"(?:in|at)\s+(?P<station>.+?)\s+for\s+",
    re.IGNORECASE,
)

#: A clock hour, e.g. "1 PM EDT" or "12:00 AM". Required before any market is
#: called hourly: an hourly contract has to say which hour, and a rules string
#: that merely omitted the word "highest" is a phrasing we do not understand.
_CLOCK_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM)\b", re.IGNORECASE)

#: Coordinate hint, family B only. Never synthesised for family A — see
#: parse_rules.
_COORDS_RE = re.compile(r"\bfor\s+coordinates\s+(?P<code>[A-Z0-9]{3,6})\b", re.IGNORECASE)

#: A station name is a short label. This is a sanity bound on the non-greedy
#: capture above, so that a rules string phrased in some way we have not seen
#: yields a refusal rather than half a sentence presented as a place name.
_STATION_CHARS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ,.'/-]*$")
_MAX_STATION_LEN = 60

#: Words that cannot occur inside a place name, but can easily be swept up by
#: a capture that ran past the end of the clause it was meant to stop at.
_STATION_STOPWORDS = frozenset({"market", "resolves", "then", "temperature", "if"})


@dataclass(frozen=True, slots=True)
class StationRef:
    """What the rules text says about where and how a market settles."""

    #: The location exactly as the rules wrote it — "Chicago Midway, IL",
    #: "Central Park, New York City". Not normalised, because the caller
    #: resolving it to a station needs to see what the market actually said.
    station_text: str
    #: Coordinate hint from the rules, when they give one. ``None`` for
    #: NWS-settled markets, which never do.
    station_code: str | None
    source: SettlementSource
    measure: Measure

    @property
    def settles_from_nws(self) -> bool:
        """Whether api.weather.gov is the settlement source or only a proxy.

        False does not mean the market is unusable — it means any comparison
        against our own observations is a proxy comparison, and an edge that
        depends on the last tenth of a degree is not real.
        """
        return self.source is SettlementSource.NWS_CLIMATOLOGICAL


def parse_rules(rules: str | None) -> StationRef | None:
    """Read settlement source, measure and station out of ``rules_primary``.

    Returns ``None`` for anything not recognised as a station temperature
    market. That covers the easy cases — precipitation, snowfall, an earnings
    market that happens to be called Snowflake — and the harder ones: a global
    temperature index has no station, and a rules string that names no
    settlement organisation cannot be assigned one.
    """
    if not rules or not rules.strip():
        return None

    # Collapse whitespace so line wrapping and non-breaking spaces cannot
    # break a match. Case is preserved: station_text is quoted back verbatim.
    text = " ".join(rules.split())

    matches = _STATION_RE.findall(text)
    if len(matches) != 1:
        # Zero: not a station temperature market. More than one: a compound
        # rule naming several stations, which has a settlement question we
        # cannot answer by picking the first one.
        return None
    match = _STATION_RE.search(text)
    if match is None:  # pragma: no cover - findall and search agree
        return None

    station_text = _clean_station(match.group("station"))
    if station_text is None:
        return None

    measure = _measure_from(match.group("measure"), text)
    if measure is None:
        return None

    source = _source_from(text)
    if source is None:
        return None

    # The cross-check. Each family has exactly one settlement organisation in
    # live data; a crossed pair is an unseen phrasing, and guessing which half
    # to believe is how a Weather-Company market gets labelled NWS-settled.
    expected = (
        SettlementSource.WEATHER_COMPANY
        if measure is Measure.HOURLY
        else SettlementSource.NWS_CLIMATOLOGICAL
    )
    if source is not expected:
        return None

    # Only ever read from the text. There is no geocoding fallback here on
    # purpose: "Chicago Midway, IL" is almost certainly KMDW, and "almost
    # certainly" is the wrong confidence to bake into a settlement path.
    coords = _COORDS_RE.search(text)
    station_code = coords.group("code").upper() if coords else None

    return StationRef(
        station_text=station_text,
        station_code=station_code,
        source=source,
        measure=measure,
    )


def _clean_station(raw: str) -> str | None:
    """Validate the captured location, or refuse it.

    The capture is non-greedy up to " for ", which is right for every phrasing
    we have seen and unverifiable for ones we have not. These bounds turn a
    runaway match into a refusal instead of a confident wrong place name.
    """
    station = raw.strip().strip(",").strip()
    if not station or len(station) > _MAX_STATION_LEN:
        return None
    if not _STATION_CHARS_RE.match(station):
        return None
    words = {w.strip(",.").lower() for w in station.split()}
    if words & _STATION_STOPWORDS:
        return None
    return station


def _measure_from(word: str | None, text: str) -> Measure | None:
    """Map the aggregate word to a measure; its absence means hourly.

    An unrecognised adjective ("the average temperature recorded at ...")
    refuses rather than falling through to hourly, because falling through
    would turn every future wording into a silently mislabelled market.
    """
    if word is not None and word.strip():
        return _MEASURE_WORDS.get(word.strip().lower())
    # No aggregate word: this is the hourly family, but only if the rules name
    # an hour. Without one there is nothing to check an observation against.
    return Measure.HOURLY if _CLOCK_RE.search(text) else None


def _source_from(text: str) -> SettlementSource | None:
    """Identify the settling organisation by name, or refuse.

    Both named at once is ambiguous and refused: one of them settles the
    market and the text does not make it clear which.
    """
    low = text.lower()
    nws = "national weather service" in low
    twc = "the weather company" in low
    if nws == twc:  # neither named, or both named
        return None
    if nws:
        # The NWS publishes several products. Only the Climatological Report
        # (Daily) is what these markets settle on, and only that report is
        # what our ingest reads. Any other NWS product is a different number.
        return (
            SettlementSource.NWS_CLIMATOLOGICAL
            if "climatological report (daily)" in low
            else None
        )
    return SettlementSource.WEATHER_COMPANY

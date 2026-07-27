"""Parse api.weather.gov payloads into typed, Fahrenheit-denominated values.

Pure functions over already-fetched JSON. No network, no I/O, no clock — the
HTTP client lives elsewhere, so every branch here is testable against a
captured payload.

**The dangerous version of this module** reads ``payload["properties"]
["temperature"]["value"]``, assumes Celsius because that is what the station
happened to return last time it was looked at, and falls back to ``0.0`` when
the value is ``null``. Every one of those three shortcuts produces a number
that looks exactly like weather:

- a ``degF`` value converted as if it were Celsius turns 90F into 194F, and a
  ``degC`` value passed through as Fahrenheit turns 32C into 32F — a ~40-degree
  error in the middle of the range where these markets actually trade;
- ``null`` is the *normal* case in NWS payloads, not an exceptional one, and
  ``0.0`` degrees is a perfectly plausible reading that would silently drive a
  model into pricing a January contract in July;
- the two products do not even agree on how to spell a unit (see below), so
  "the unit I saw last time" is not a stable assumption.

So: units are read from the payload's own unit field and **refused** when
unrecognised, missing measurements stay ``None`` all the way to the caller,
and nothing here ever substitutes a default for a number the API declined to
give us.

Two payload shapes, deliberately not unified
--------------------------------------------
Observations use the GeoJSON quantitative-value shape::

    "temperature": {"unitCode": "wmoUnit:degC", "value": 26, "qualityControl": "V"}

The ``/forecast`` product does not. It reports a **bare** number beside a
one-letter unit::

    "temperature": 90, "temperatureUnit": "F"

and that letter flips to ``"C"`` when the request carried ``?units=si``. The
caller controls that query parameter, this module does not see it, and the
default (``"units": "us"``) is a request-time choice rather than a property of
the data. Hence: trust the unit field on each period, per period, every time.
``parse_forecast_periods`` never assumes Fahrenheit just because the US
default usually is.

(A third shape exists on the raw gridpoint product — ``maxTemperature`` there
carries its unit under the key ``uom``, and reports ``wmoUnit:degC`` even when
``/forecast`` for the same grid cell is reporting F. This module does not
parse gridpoints; if that is ever added, it needs its own unit lookup, not a
reuse of either of these.)

What settles these markets
--------------------------
Kalshi's daily temperature markets settle on the NWS **Climatological Report
(Daily)** (product ``CLI``) — a once-a-day, human-reviewed product. The hourly
observations parsed here are a **proxy** for it and are not guaranteed to
agree: the running maximum of the hourly obs can differ from the reported
daily high because the CLI is computed from the station's own 1-minute
extremes, is subject to late correction, and uses a local-midnight day that
this module cannot infer from an observation alone. Treat ``Observation`` as
evidence about the settlement value, never as the settlement value.

Quality control
---------------
Observations carry a ``qualityControl`` flag; values seen in live payloads
include ``V``, ``C``, ``S`` and ``Z`` (``Z`` accompanies every ``null`` value
observed so far). NWS documents the field but not, anywhere this module's
author could find, a normative mapping from flag to trustworthiness. So the
flag is **surfaced verbatim and never filtered on**. Inventing a filter here —
"drop anything that isn't V" — would be a guess dressed as a safety check, and
a guard that silently discards good readings is worse than no guard. If a
policy is wanted, it belongs in the caller, written against documented
semantics.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

__all__ = [
    "Observation",
    "ForecastPeriod",
    "celsius_to_fahrenheit",
    "temperature_to_fahrenheit",
    "parse_observation",
    "parse_forecast_periods",
    "daily_high",
    "running_high_f",
]

#: Unit tokens we are willing to act on, after stripping any ``prefix:``.
#:
#: Both vocabularies are represented because both are real: ``wmoUnit:degC``
#: from observations, bare ``C``/``F`` from ``/forecast``. Anything not in
#: these sets is refused rather than assumed — Kelvin in particular is absent
#: on purpose. It has never appeared in a payload inspected for this module,
#: and a unit added on speculation is a conversion nobody has ever checked.
_CELSIUS_TOKENS = frozenset({"degc", "c", "celsius"})
_FAHRENHEIT_TOKENS = frozenset({"degf", "f", "fahrenheit"})


@dataclass(frozen=True, slots=True)
class Observation:
    """One station observation, with its provenance attached.

    ``temperature_f is None`` means *no usable temperature*, for one of two
    reasons the caller can tell apart:

    - ``raw_value is None`` — the API reported ``null``. Routine.
    - ``raw_value is not None`` — a number arrived under a unit code we refuse
      to interpret. That is a bug or an API change, and ``raw_unit_code`` says
      which.

    Neither case is ever papered over with a default.
    """

    #: ``stationId`` when present; the payload does not always carry it.
    station_id: str | None
    #: Timezone-aware UTC. Never naive — see ``_parse_timestamp``.
    observed_at: datetime
    #: Converted to Fahrenheit, unrounded. The CLI report rounds to whole
    #: degrees; that rounding is settlement policy and belongs to the caller.
    temperature_f: float | None
    #: The number exactly as it arrived, before conversion.
    raw_value: float | None
    #: The unit exactly as it arrived, e.g. ``"wmoUnit:degC"``.
    raw_unit_code: str | None
    #: Verbatim ``qualityControl`` flag. Surfaced, never filtered on.
    quality_control: str | None


@dataclass(frozen=True, slots=True)
class ForecastPeriod:
    """One period of the ``/forecast`` product.

    Periods are kept even when their temperature is unusable. Dropping them
    would let ``daily_high`` compute a confident maximum over whatever
    happened to parse, which is the quiet kind of wrong: an understated
    forecast high is still a plausible temperature.
    """

    #: ``number``/``name`` as reported, e.g. 2 / ``"Monday"``.
    number: int | None
    name: str | None
    #: Timezone-aware UTC.
    start: datetime
    end: datetime | None
    #: ``isDaytime``. ``None`` when the payload omitted it — unknown, not false.
    is_daytime: bool | None
    temperature_f: float | None
    raw_temperature: float | None
    #: ``temperatureUnit`` as reported — a bare ``"F"`` or ``"C"`` here, not a
    #: ``wmoUnit:`` code.
    raw_unit: str | None
    #: UTC offset carried by ``startTime`` itself, in hours. The forecast
    #: office stamps its own local offset (``-05:00`` for Chicago in July),
    #: which is the most authoritative local-day signal in the payload — but
    #: ``daily_high`` will not use it implicitly. See that docstring.
    source_utc_offset_hours: float | None


def celsius_to_fahrenheit(c: float) -> float:
    """Exact C -> F. No rounding, no clamping."""
    return c * 9.0 / 5.0 + 32.0


def _unit_token(unit: object) -> str | None:
    """Normalise a unit field to a bare lowercase token.

    ``"wmoUnit:degC"`` -> ``"degc"``, ``"F"`` -> ``"f"``. Returns ``None`` for
    anything that is not a non-empty string, so a structurally wrong unit
    field lands in the same refusal path as an unrecognised one.
    """
    if not isinstance(unit, str):
        return None
    token = unit.rsplit(":", 1)[-1].strip().lower()
    return token or None


def _as_number(value: object) -> float | None:
    """Coerce a JSON number, refusing everything that is not one.

    Three refusals worth naming:

    - ``bool`` is rejected before ``int``, because ``isinstance(True, int)`` is
      ``True`` in Python and a JSON ``true`` would otherwise become 1.0 degrees;
    - strings are rejected outright. No numeric field in any inspected NWS
      payload is a string, and accepting one invites a locale-formatted
      ``"22,2"`` to be read as 222;
    - NaN and infinity are rejected. Python's ``json`` module parses the
      non-standard ``NaN``/``Infinity`` literals by default, and a NaN
      propagates through ``max()`` silently.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def temperature_to_fahrenheit(value: object, unit: object) -> float | None:
    """Convert a temperature to Fahrenheit using its declared unit.

    ``None`` when the value is missing/unusable **or** the unit is one we do
    not recognise. The unit is authoritative: a field named ``temperature``
    tells you nothing about its scale, and this is the single function where
    that rule is enforced.
    """
    number = _as_number(value)
    if number is None:
        return None
    token = _unit_token(unit)
    if token in _CELSIUS_TOKENS:
        return celsius_to_fahrenheit(number)
    if token in _FAHRENHEIT_TOKENS:
        return number
    # Unrecognised, missing, or malformed unit. Refuse. Defaulting to Celsius
    # here would be a ~40-degree error that reads as ordinary weather.
    return None


def _parse_timestamp(value: object) -> datetime | None:
    """ISO-8601 with offset -> aware UTC datetime, or ``None``.

    A timestamp without an offset is **refused**, not assumed to be UTC. NWS
    always stamps one (``+00:00`` on observations, the office's local offset
    on forecast periods); a naive datetime reaching the caller becomes an
    off-by-hours error that no downstream check would notice, and hours are
    exactly the resolution at which a daily high is decided.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _utc_offset_hours(value: object) -> float | None:
    """The UTC offset carried by an ISO-8601 string, in hours."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    return offset.total_seconds() / 3600.0


def _properties(payload: object) -> dict[str, Any] | None:
    """Accept either a GeoJSON Feature or its ``properties`` directly.

    The station endpoints return a Feature; callers that have already unpacked
    a collection hand over the inner dict. Supporting both costs one branch
    and removes a whole class of "empty result, no error" confusion.
    """
    if not isinstance(payload, dict):
        return None
    inner = payload.get("properties")
    if isinstance(inner, dict):
        return inner
    return payload


def parse_observation(payload: dict[str, Any]) -> Observation | None:
    """Parse a station's latest observation.

    Returns ``None`` only for *structural* failure — not a dict, or no usable
    timestamp. A reading that cannot be placed in time is not a reading, and
    there is nothing sensible for the caller to do with it.

    A missing or uninterpretable **temperature** is different: it is routine,
    so it yields an ``Observation`` with ``temperature_f=None`` and the raw
    value/unit preserved. The caller sees an explicit ``None`` it must handle,
    rather than a silent zero or a silently absent observation.
    """
    props = _properties(payload)
    if props is None:
        return None

    observed_at = _parse_timestamp(props.get("timestamp"))
    if observed_at is None:
        return None

    temp = props.get("temperature")
    if not isinstance(temp, dict):
        # Some payloads omit the block entirely rather than nulling `value`.
        temp = {}

    unit_code = temp.get("unitCode")
    quality = temp.get("qualityControl")
    station_id = props.get("stationId")

    return Observation(
        station_id=station_id if isinstance(station_id, str) else None,
        observed_at=observed_at,
        temperature_f=temperature_to_fahrenheit(temp.get("value"), unit_code),
        raw_value=_as_number(temp.get("value")),
        raw_unit_code=unit_code if isinstance(unit_code, str) else None,
        quality_control=quality if isinstance(quality, str) else None,
    )


def parse_forecast_periods(payload: dict[str, Any]) -> list[ForecastPeriod]:
    """Parse the ``periods`` array of the ``/forecast`` product.

    A period is dropped **only** when it has no usable ``startTime``, since an
    unplaceable period cannot be assigned to a day at all. Everything else —
    null temperature, unknown unit, missing ``isDaytime`` — is carried through
    as ``None`` so that ``daily_high`` can see the gap and refuse. Silently
    skipping a malformed period is how a maximum over a partial set comes to
    look authoritative.
    """
    props = _properties(payload)
    if props is None:
        return []
    raw_periods = props.get("periods")
    if not isinstance(raw_periods, list):
        return []

    periods: list[ForecastPeriod] = []
    for raw in raw_periods:
        if not isinstance(raw, dict):
            continue
        start = _parse_timestamp(raw.get("startTime"))
        if start is None:
            continue

        unit = raw.get("temperatureUnit")
        number = raw.get("number")
        name = raw.get("name")
        is_daytime = raw.get("isDaytime")

        periods.append(
            ForecastPeriod(
                # `isinstance(True, int)` again: a JSON `true` must not become
                # period number 1.
                number=(
                    number
                    if isinstance(number, int) and not isinstance(number, bool)
                    else None
                ),
                name=name if isinstance(name, str) else None,
                start=start,
                end=_parse_timestamp(raw.get("endTime")),
                # Only a real JSON boolean counts. A missing or oddly-typed
                # `isDaytime` is *unknown*, and unknown must not read as night.
                is_daytime=is_daytime if isinstance(is_daytime, bool) else None,
                temperature_f=temperature_to_fahrenheit(raw.get("temperature"), unit),
                raw_temperature=_as_number(raw.get("temperature")),
                raw_unit=unit if isinstance(unit, str) else None,
                source_utc_offset_hours=_utc_offset_hours(raw.get("startTime")),
            )
        )
    return periods


def daily_high(
    periods: Iterable[ForecastPeriod],
    *,
    day: date,
    tz_offset_hours: float = 0.0,
) -> float | None:
    """Forecast high for one local calendar day, in Fahrenheit.

    **How a period is assigned to a day.** This is where an off-by-one-day bug
    hides, so the rule is stated rather than implied:

    1. Each period's ``start`` (aware UTC) is shifted by ``tz_offset_hours``
       and its **local calendar date** is taken. Only the start matters; ends
       are ignored for assignment, because NWS night periods span midnight
       (``"Monday Night"`` runs 18:00 Monday to 06:00 Tuesday local) and no
       single-date answer for those is defensible.
    2. Of the periods landing on ``day``, only **daytime** ones
       (``is_daytime is True``) contribute. An NWS daytime period covers
       roughly 06:00–18:00 local and reports that period's **high**; a night
       period reports a **low**, and its start-date assignment is the
       arbitrary half of a straddle. Including them would mix two different
       statistics and attribute the next day's small hours to today.
    3. The result is the maximum over those periods — normally exactly one,
       but the product occasionally splits a day, so a max rather than a
       "first match".

    ``tz_offset_hours`` defaults to ``0.0``, i.e. **UTC**, which is a
    deliberately wrong-for-every-US-station default that makes the omission
    visible in tests rather than plausible in production. Callers should pass
    the station's current offset (``-5.0`` for Chicago in July). Each parsed
    period also carries ``source_utc_offset_hours``, the forecast office's own
    local offset — the best available signal — but it is **not** applied
    implicitly here, because a function whose day boundary is decided by data
    the caller cannot see is a function nobody can reason about. If you want
    it, read it off the period and pass it in.

    Refusals (all return ``None``):

    - no period lands on ``day`` at all — the forecast does not reach that far,
      or the offset is wrong; either way there is no answer to give;
    - any period landing on ``day`` has an unknown ``is_daytime``, since it
      cannot be included or excluded honestly;
    - any contributing daytime period has no usable temperature. A maximum
      over the remainder would be understated and entirely believable, which
      is worse than no number at all.

    **This is a forecast, not the settlement value.** These markets settle on
    the Climatological Report (Daily); see the module docstring.
    """
    shift = timedelta(hours=tz_offset_hours)
    on_day = [p for p in periods if (p.start + shift).date() == day]
    if not on_day:
        return None

    # Unknown daytime flag: refuse the whole day rather than guess which
    # statistic the period reports.
    if any(p.is_daytime is None for p in on_day):
        return None

    daytime = [p for p in on_day if p.is_daytime]
    if not daytime:
        return None

    temps: list[float] = []
    for period in daytime:
        if period.temperature_f is None:
            # One unreadable daytime period poisons the maximum. Fail closed.
            return None
        temps.append(period.temperature_f)
    return max(temps)


def running_high_f(observations: Sequence[Observation]) -> float | None:
    """Highest usable temperature across some observations, or ``None``.

    A convenience for the proxy described in the module docstring — and only a
    proxy. ``None`` when no observation carries a temperature, because "we saw
    nothing" and "we saw a cold day" are different facts and must not share a
    representation.
    """
    temps = [o.temperature_f for o in observations if o.temperature_f is not None]
    return max(temps) if temps else None

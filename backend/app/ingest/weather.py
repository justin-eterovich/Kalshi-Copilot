"""Collecting NWS observations and forecasts for the stations we trade.

Two loops with different jobs and different reasons for existing:

- **Observations** are the record of what actually happened, and they are what
  turns a forecast into a *measured* error. Without them the calibration has
  no right-hand side and the model has no sigma.
- **Forecasts** are the model input, and **every issuance is kept** rather than
  overwritten. A table holding only the current forecast can never answer "how
  wrong is a forecast at this lead time?", which is the one question the
  pricing depends on.

Only stations we have actually asserted a mapping for are polled. The station
table is a set of claims about the world (see :mod:`app.weather.stations`), and
polling a station no market settles on would be wasted requests against a free,
politely-rate-limited public API.

**These observations are a proxy, not the settlement source.** These markets
settle on the Climatological Report (Daily), a separate once-a-day product; a
running maximum over hourly observations is not guaranteed to equal the daily
high that report publishes. Good enough to calibrate a forecast against, not
good enough to declare a market decided — which is why nothing here writes a
settlement.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import WeatherForecast, WeatherObservation
from app.weather.client import NwsClient, NwsError
from app.weather.nws_parse import (
    daily_high,
    parse_forecast_periods,
    parse_observation,
)
from app.weather.stations import SERIES_STATIONS

log = get_logger(__name__)

__all__ = [
    "tracked_stations",
    "sync_observation",
    "sync_forecast",
    "backfill_actuals",
]


def tracked_stations() -> list[str]:
    """Every station some series has been asserted to settle on."""
    return sorted({claim.station_id for claim in SERIES_STATIONS.values()})


async def sync_observation(
    session: AsyncSession, client: NwsClient, station_id: str
) -> bool:
    """Record the latest observation for one station.

    Returns whether a row was written. Deduplicated on
    ``(station_id, observed_at)`` in the database rather than by checking
    first: the poll interval and the station's reporting interval do not
    divide evenly, so re-seeing the same observation is the normal case, not
    an error worth a round trip to detect.
    """
    payload = await client.latest_observation(station_id)
    observation = parse_observation(payload)
    if observation is None:
        log.warning("no usable observation for %s", station_id)
        return False

    if observation.temperature_f is None and observation.raw_value is not None:
        # A number arrived under a unit we refuse to interpret. That is an API
        # change or a bug, not routine missing data, and it should be loud.
        log.error(
            "%s reported %s under unrecognised unit %r — not recording a "
            "temperature rather than guessing the scale",
            station_id,
            observation.raw_value,
            observation.raw_unit_code,
        )

    stmt = (
        pg_insert(WeatherObservation)
        .values(
            station_id=station_id,
            observed_at=observation.observed_at,
            temperature_f=(
                None
                if observation.temperature_f is None
                else Decimal(str(observation.temperature_f))
            ),
            quality_control=observation.quality_control,
        )
        .on_conflict_do_nothing(constraint="uq_weather_obs")
    )
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def sync_forecast(
    session: AsyncSession,
    client: NwsClient,
    station_id: str,
    *,
    days_ahead: int = 7,
    now: datetime | None = None,
) -> int:
    """Record forecast highs for the next ``days_ahead`` local days.

    One row per (station, day, issuance). Issuances are never overwritten —
    that history *is* the calibration dataset, and a table that keeps only the
    newest forecast can measure nothing about forecast error.
    """
    now = now or datetime.now(UTC)
    payload = await client.forecast_for_station(station_id)
    periods = parse_forecast_periods(payload)
    if not periods:
        log.warning("no forecast periods for %s", station_id)
        return 0

    # The forecast office stamps its own local offset on every period, which
    # is the most authoritative local-day signal available without a timezone
    # database. Taken from the first period that carries one.
    offset = next(
        (
            p.source_utc_offset_hours
            for p in periods
            if p.source_utc_offset_hours is not None
        ),
        None,
    )
    if offset is None:
        log.warning(
            "forecast for %s carries no UTC offset; refusing to guess which "
            "local day each period belongs to",
            station_id,
        )
        return 0

    local_today = (now + timedelta(hours=offset)).date()
    written = 0

    for delta in range(days_ahead):
        target = local_today + timedelta(days=delta)
        high = daily_high(periods, day=target, tz_offset_hours=offset)
        if high is None:
            continue

        # Lead time from now to the end of the target local day, which is when
        # the outcome is finally determined.
        target_end = datetime.combine(
            target, datetime.min.time(), tzinfo=UTC
        ) + timedelta(days=1, hours=-offset)
        lead_hours = (target_end - now).total_seconds() / 3600.0

        stmt = (
            pg_insert(WeatherForecast)
            .values(
                station_id=station_id,
                target_date=target,
                measure="high",
                issued_at=now,
                temperature_f=Decimal(str(high)),
                lead_hours=lead_hours,
            )
            .on_conflict_do_nothing(constraint="uq_weather_forecast")
        )
        result = await session.execute(stmt)
        written += bool(result.rowcount)

    return written


async def backfill_actuals(
    session: AsyncSession, *, lookback_days: int = 14, now: datetime | None = None
) -> int:
    """Fill in ``actual_f`` for forecasts whose day has finished.

    The actual is the maximum observation recorded on that local day — a
    proxy for the Climatological Report, and labelled as one everywhere it is
    used. It is good enough to measure forecast error against, which is all
    this column is for.
    """
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=lookback_days)).date()

    pending = (
        await session.execute(
            select(WeatherForecast).where(
                WeatherForecast.actual_f.is_(None),
                WeatherForecast.target_date >= since,
                # Only days that are unambiguously over. A day still in
                # progress has a running maximum that is not yet the high, and
                # recording it as the actual would teach the calibration that
                # forecasts run hot.
                WeatherForecast.target_date < (now - timedelta(days=1)).date(),
            )
        )
    ).scalars().all()
    if not pending:
        return 0

    filled = 0
    for row in pending:
        actual = await _observed_high(session, row.station_id, row.target_date)
        if actual is None:
            continue
        row.actual_f = actual
        filled += 1

    return filled


async def _observed_high(
    session: AsyncSession, station_id: str, day: date
) -> Decimal | None:
    """Highest observation recorded on ``day`` UTC for a station.

    Deliberately UTC-bounded rather than local: the observations table stores
    aware UTC timestamps, and a station's local day is a window this function
    does not have the offset to compute. The mismatch shifts the boundary by a
    few hours, which matters least for a daily *high* — the hottest hours of a
    local day sit well inside any reasonable window — but it is a real
    approximation and callers should read the number as such.
    """
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    rows = (
        await session.execute(
            select(WeatherObservation.temperature_f).where(
                WeatherObservation.station_id == station_id,
                WeatherObservation.observed_at >= start,
                WeatherObservation.observed_at < start + timedelta(days=1),
                WeatherObservation.temperature_f.isnot(None),
            )
        )
    ).scalars().all()

    values = [v for v in rows if v is not None]
    return max(values) if values else None


async def sweep_weather(
    session: AsyncSession, client: NwsClient, *, forecasts: bool
) -> tuple[int, int]:
    """One pass over every tracked station.

    A failure on one station must not stop the others: the NWS occasionally
    500s on a single site while the rest are fine, and losing the whole sweep
    to that would starve the calibration of every station's data instead of
    one's.
    """
    observed = 0
    forecast_rows = 0

    for station_id in tracked_stations():
        try:
            if await sync_observation(session, client, station_id):
                observed += 1
        except NwsError as exc:
            log.warning("observation sync failed for %s: %s", station_id, exc)

        if forecasts:
            try:
                forecast_rows += await sync_forecast(session, client, station_id)
            except NwsError as exc:
                log.warning("forecast sync failed for %s: %s", station_id, exc)

    return observed, forecast_rows

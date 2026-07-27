"""HTTP access to api.weather.gov.

Transport only. Everything that interprets a payload lives in
:mod:`app.weather.nws_parse`, so the shape of the NWS's JSON is pinned by
tests that do not need a network.

Two things about this API are worth knowing before changing anything here:

- **A User-Agent is mandatory and is expected to identify you.** The NWS asks
  for something contactable and returns 403 without one. It is configurable
  (``weather.user_agent``) rather than hard-coded because it is, in effect, an
  API key made of prose.
- **It is free, unauthenticated, and rate-limited by good behaviour.** There is
  no published quota and no ``Retry-After``, so the polite intervals in
  ``config.yaml`` (15 minutes for forecasts, 5 for observations) are the whole
  rate-limiting story. A tighter loop is not a performance improvement, it is
  a way to get the homelab's IP blocked and the engine silently starved of the
  data every price depends on.

Failures raise. A weather engine that carries on with a stale forecast is
worse than one that stops: the forecast is the entire model input, and an hour
old is a different day's weather in a thunderstorm.
"""

from __future__ import annotations

from typing import Any, Final

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["NwsClient", "NwsError"]

BASE_URL: Final = "https://api.weather.gov"


class NwsError(RuntimeError):
    """A request to the NWS failed or returned something unusable."""


class NwsClient:
    """Minimal async client for the endpoints the weather engine needs."""

    def __init__(
        self,
        *,
        user_agent: str,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        if not user_agent.strip():
            # The API refuses an empty one, and failing here names the config
            # key rather than surfacing an opaque 403 on every request.
            raise ValueError(
                "weather.user_agent must be set; api.weather.gov requires a "
                "User-Agent that identifies the caller"
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                # The GeoJSON variant is what the documented schemas describe.
                "Accept": "application/geo+json",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> NwsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            response = await self._client.get(path)
        except httpx.HTTPError as exc:
            raise NwsError(f"GET {path} failed: {exc}") from exc

        if response.status_code != 200:
            raise NwsError(
                f"GET {path} -> HTTP {response.status_code}: {response.text[:300]}"
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise NwsError(f"GET {path} returned {type(payload).__name__}, not an object")
        return payload

    async def latest_observation(self, station_id: str) -> dict[str, Any]:
        """Most recent observation for a station."""
        return await self._get(f"/stations/{station_id}/observations/latest")

    async def station(self, station_id: str) -> dict[str, Any]:
        """Station metadata, which carries the coordinates and time zone.

        The time zone matters more than it looks: these markets settle on a
        *local* calendar day, so deciding which readings belong to "July 27"
        needs the station's own zone rather than UTC or the host's.
        """
        return await self._get(f"/stations/{station_id}")

    async def point(self, latitude: float, longitude: float) -> dict[str, Any]:
        """Grid metadata for a coordinate, including the forecast URLs."""
        return await self._get(f"/points/{latitude},{longitude}")

    async def forecast_for_station(self, station_id: str) -> dict[str, Any]:
        """Resolve a station to its gridpoint forecast in two hops.

        The NWS does not expose a forecast per station; you look up the grid
        cell for the station's coordinates and fetch that. Both hops are done
        here so callers never hold a half-resolved station.
        """
        meta = await self.station(station_id)
        coords = (meta.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            raise NwsError(f"station {station_id} has no coordinates")
        # GeoJSON is [longitude, latitude] — the opposite order to every URL
        # the NWS then asks you to build. Reversing this silently produces a
        # forecast for somewhere in the ocean rather than an error.
        longitude, latitude = float(coords[0]), float(coords[1])

        grid = await self.point(latitude, longitude)
        url = (grid.get("properties") or {}).get("forecast")
        if not url:
            raise NwsError(f"no forecast URL for {station_id} at {latitude},{longitude}")
        # No `units` query parameter, deliberately. `/forecast` reports a bare
        # number beside a one-letter `temperatureUnit`, and `?units=si` flips
        # that same field from "F" to "C" — the parser reads the unit rather
        # than assuming, so this stays correct either way, but adding the
        # parameter here silently changes the scale of every forecast in the
        # database against the ones already stored. Verified against KMDW:
        # the same period returns 90/"F" by default and 26/"C" with units=si.
        return await self._get(str(url))

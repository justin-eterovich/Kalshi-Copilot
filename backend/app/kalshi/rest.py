"""Kalshi REST client.

Covers the market-data surface M1 needs: series, events, markets, orderbooks
and trades. Portfolio and order endpoints arrive with the execution rail in
M3.

Behaviour worth knowing:

- **Auth is optional per call.** Public market data works unauthenticated;
  we sign when a signer is configured so the same client serves both.
- **Cursor pagination.** List endpoints return an opaque ``cursor``; an empty
  or repeated cursor means the end. We guard against a server that returns
  the same cursor forever, which would otherwise loop until the heat death of
  the universe.
- **429 handling.** No ``Retry-After`` header exists, so we drain the local
  bucket and back off exponentially with jitter.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.kalshi.auth import KalshiSigner
from app.kalshi.ratelimit import DEFAULT_TOKEN_COST, RateLimiter

log = get_logger(__name__)

__all__ = ["KalshiRestClient", "KalshiApiError"]

MAX_PAGE_LIMIT: Final = 1000
MAX_RETRIES: Final = 5
#: Endpoints that mutate state draw from the write budget.
_WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class KalshiApiError(RuntimeError):
    """A non-retryable error response from the API."""

    def __init__(self, status: int, method: str, path: str, body: str) -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:400]}")
        self.status = status
        self.path = path
        self.body = body


class KalshiRestClient:
    """Async REST client with signing, rate limiting, and pagination."""

    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._signer = signer
        self._limiter = rate_limiter or RateLimiter()
        self._throttle_count = 0
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "kalshi-copilot/0.1"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> KalshiRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def authenticated(self) -> bool:
        return self._signer is not None

    # -- core request ----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _auth_headers(self, method: str, url: str) -> dict[str, str]:
        if self._signer is None:
            return {}
        # The signature covers the path only — never the query string.
        return self._signer.headers(method, url)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        cost: int = DEFAULT_TOKEN_COST,
    ) -> dict[str, Any]:
        url = self._url(path)
        is_write = method.upper() in _WRITE_METHODS

        clean_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            waited = await self._limiter.acquire(is_write, cost)
            if waited > 0.5:
                log.debug("rate limiter held %s %s for %.2fs", method, path, waited)

            try:
                response = await self._client.request(
                    method,
                    url,
                    params=clean_params,
                    json=json,
                    headers=self._auth_headers(method, url),
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_error = exc
                delay = self._backoff(attempt)
                log.warning(
                    "%s %s network error (%s), retry %d/%d in %.1fs",
                    method, path, type(exc).__name__, attempt + 1, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code == 429:
                delay = self._backoff(attempt)
                # No Retry-After header exists, so the limiter both drains the
                # bucket and halves its sustained rate; it converges on the
                # real limit instead of re-offending on every request.
                self._limiter.penalize(is_write, delay)
                self._throttle_count += 1
                # Logged at debug: a handful of these is the limiter finding
                # the ceiling, which is working as designed, not an incident.
                log.debug(
                    "%s %s rate limited, backing off %.1fs (attempt %d/%d)",
                    method, path, delay, attempt + 1, MAX_RETRIES,
                )
                if self._throttle_count in (25, 100, 500):
                    log.warning(
                        "%d rate-limit responses so far; limiter now at %s",
                        self._throttle_count,
                        self._limiter.describe(),
                    )
                await asyncio.sleep(delay)
                continue

            if 500 <= response.status_code < 600:
                delay = self._backoff(attempt)
                log.warning(
                    "%s %s server error %d, retry %d/%d in %.1fs",
                    method, path, response.status_code, attempt + 1, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                # 4xx other than 429 will not improve on retry.
                raise KalshiApiError(
                    response.status_code, method, path, response.text
                )

            # A clean response lets the adaptive rate creep back up.
            self._limiter.record_success(is_write)

            if not response.content:
                return {}
            return response.json()

        raise KalshiApiError(
            0, method, path, f"exhausted {MAX_RETRIES} retries: {last_error}"
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter, capped so we never stall forever."""
        base = min(2.0**attempt, 30.0)
        return base * (0.5 + random.random() * 0.5)

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    # -- pagination ------------------------------------------------------

    async def paginate(
        self,
        path: str,
        item_key: str,
        *,
        limit: int = MAX_PAGE_LIMIT,
        max_pages: int | None = None,
        **params: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item across cursor-paginated pages.

        Stops on an empty cursor, an empty page, or a repeated cursor (which
        would otherwise loop forever).
        """
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0

        while True:
            payload = await self.get(
                path, limit=min(limit, MAX_PAGE_LIMIT), cursor=cursor, **params
            )
            items = payload.get(item_key) or []
            for item in items:
                yield item

            pages += 1
            cursor = payload.get("cursor") or None

            if not cursor or not items:
                return
            if max_pages is not None and pages >= max_pages:
                log.debug("stopping %s pagination at max_pages=%d", path, max_pages)
                return
            if cursor in seen_cursors:
                log.warning(
                    "%s returned a repeated cursor; stopping to avoid a loop", path
                )
                return
            seen_cursors.add(cursor)

    # -- market data -----------------------------------------------------

    async def get_markets(
        self,
        *,
        limit: int = MAX_PAGE_LIMIT,
        status: str | None = None,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        min_updated_ts: int | None = None,
        tickers: str | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate markets.

        ``min_updated_ts`` enables incremental catalog sync, but the API
        documents it as incompatible with most other filters — pass it alone.
        """
        async for market in self.paginate(
            "/markets",
            "markets",
            limit=limit,
            max_pages=max_pages,
            status=status,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
            min_updated_ts=min_updated_ts,
            tickers=tickers,
        ):
            yield market

    async def get_market(self, ticker: str) -> dict[str, Any]:
        payload = await self.get(f"/markets/{ticker}")
        return payload.get("market", payload)

    async def get_events(
        self,
        *,
        limit: int = 200,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self.paginate(
            "/events",
            "events",
            limit=limit,
            max_pages=max_pages,
            status=status,
            series_ticker=series_ticker,
            with_nested_markets=with_nested_markets or None,
        ):
            yield event

    async def get_event(self, event_ticker: str) -> dict[str, Any]:
        payload = await self.get(f"/events/{event_ticker}")
        return payload.get("event", payload)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        payload = await self.get(f"/series/{series_ticker}")
        return payload.get("series", payload)

    async def get_orderbook(
        self, ticker: str, depth: int | None = None
    ) -> dict[str, Any]:
        """Return ``{yes_dollars: [[price, count], ...], no_dollars: [...]}``."""
        payload = await self.get(f"/markets/{ticker}/orderbook", depth=depth)
        return payload.get("orderbook_fp") or payload.get("orderbook") or {}

    async def get_trades(
        self,
        *,
        ticker: str | None = None,
        limit: int = MAX_PAGE_LIMIT,
        min_ts: int | None = None,
        max_ts: int | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for trade in self.paginate(
            "/markets/trades",
            "trades",
            limit=limit,
            max_pages=max_pages,
            ticker=ticker,
            min_ts=min_ts,
            max_ts=max_ts,
        ):
            yield trade

    async def get_exchange_status(self) -> dict[str, Any]:
        return await self.get("/exchange/status")

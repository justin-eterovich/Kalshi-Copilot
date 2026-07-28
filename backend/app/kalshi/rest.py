"""Kalshi REST client.

Covers market data (series, events, markets, orderbooks, trades) and the
portfolio surface the execution rail needs (orders, fills, positions,
balance).

Behaviour worth knowing:

- **Auth is optional per call.** Public market data works unauthenticated;
  we sign when a signer is configured so the same client serves both.
  Everything under ``/portfolio`` requires a signer and says so loudly.
- **Cursor pagination.** List endpoints return an opaque ``cursor``; an empty
  or repeated cursor means the end. We guard against a server that returns
  the same cursor forever, which would otherwise loop until the heat death of
  the universe.
- **429 handling.** No ``Retry-After`` header exists, so we drain the local
  bucket and back off exponentially with jitter.

**Order placement uses the V2 path**, ``POST /portfolio/events/orders``.  The
legacy ``POST /portfolio/orders`` is deprecated (no earlier than 2026-05-06)
and, more importantly, speaks integer cents — the exact unit mistake this
codebase exists to avoid.  V2 quotes fixed-point dollars.  Note that
``GET /portfolio/orders`` is still the read path; only creation moved.

**Writes are never retried automatically.**  The retry loop in
:meth:`request` covers network errors and 429s, which is safe for reads and
emphatically not for order placement: a request that timed out may still have
reached the matching engine.  ``POST``/``DELETE`` therefore fail fast and let
the caller decide, with the client order ID as the idempotency key.
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

__all__ = [
    "KalshiRestClient",
    "build_order_body",
    "KalshiApiError",
    "TIF_GTC",
    "TIF_IOC",
    "TIF_FOK",
    "VALID_TIF",
]

MAX_PAGE_LIMIT: Final = 1000
MAX_RETRIES: Final = 5
#: Endpoints that mutate state draw from the write budget.
_WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Time-in-force values the V2 order API accepts. `GTT` is an internal
#: execution type and is *not* a valid API value — an expiring order is
#: `good_till_canceled` plus an `expiration_time`.
TIF_GTC: Final = "good_till_canceled"
TIF_IOC: Final = "immediate_or_cancel"
TIF_FOK: Final = "fill_or_kill"
VALID_TIF: Final = frozenset({TIF_GTC, TIF_IOC, TIF_FOK})

#: Order create/cancel are documented at 10 tokens, same as the default, but
#: they draw on the *write* budget, which on the basic tier holds only about a
#: second of burst. Named so the cost is visible at the call site.
ORDER_TOKEN_COST: Final = 10


def build_order_body(
    *,
    ticker: str,
    book_side: str,
    price_dollars: str,
    count: str,
    client_order_id: str,
    time_in_force: str = TIF_GTC,
    post_only: bool = False,
    self_trade_prevention: str = "taker_at_cross",
    expiration_time: int | None = None,
) -> dict[str, Any]:
    """Build one V2 order body.

    Single and batch placement both go through here. They did not always:
    the batch path once assembled its own dict and omitted
    ``self_trade_prevention_type``, which the API requires, so every multi-leg
    order was rejected while single orders worked. One builder is the fix.
    """
    if book_side not in ("bid", "ask"):
        raise ValueError(f"book_side must be 'bid' or 'ask', got {book_side!r}")
    if time_in_force not in VALID_TIF:
        raise ValueError(
            f"time_in_force must be one of {sorted(VALID_TIF)}, got {time_in_force!r}"
        )

    body: dict[str, Any] = {
        "ticker": ticker,
        "side": book_side,
        "price": price_dollars,
        "count": count,
        "client_order_id": client_order_id,
        "time_in_force": time_in_force,
        "self_trade_prevention_type": self_trade_prevention,
        "post_only": post_only,
    }
    if expiration_time is not None:
        body["expiration_time"] = expiration_time
    return body


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
                if is_write:
                    # A write that timed out may already have reached the
                    # matching engine. Retrying could double-place an order,
                    # so we surface the ambiguity instead of resolving it by
                    # guessing; the caller reconciles against the exchange
                    # using the client order ID.
                    raise KalshiApiError(
                        0, method, path, f"network error on a write: {exc!r}"
                    ) from exc
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
                if is_write:
                    # Same ambiguity as a timeout: the engine may have
                    # accepted the order before the error was generated.
                    raise KalshiApiError(
                        response.status_code, method, path, response.text
                    )
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

    # -- portfolio -------------------------------------------------------
    #
    # Everything below needs a signer. Calling any of it unauthenticated is a
    # programming error, not a runtime condition, so it raises immediately
    # rather than producing a confusing 401 from the far end.

    def _require_auth(self, what: str) -> None:
        if self._signer is None:
            raise KalshiApiError(
                401,
                "AUTH",
                what,
                "no Kalshi credentials configured; portfolio endpoints require "
                "an API key and private key for the active environment",
            )

    async def create_order(
        self,
        *,
        ticker: str,
        book_side: str,
        price_dollars: str,
        count: str,
        client_order_id: str,
        time_in_force: str = TIF_GTC,
        post_only: bool = False,
        self_trade_prevention: str = "taker_at_cross",
        expiration_time: int | None = None,
    ) -> dict[str, Any]:
        """Place one order via ``POST /portfolio/events/orders`` (V2).

        Args:
            book_side: ``"bid"`` (buy YES) or ``"ask"`` (sell YES). Kalshi
                quotes a single book from the YES side; buying NO *is* an ask.
                See :mod:`app.trading.direction` — getting this wrong inverts
                the trade.
            price_dollars: The **YES** price as a fixed-point dollar string,
                whichever direction the trade is. Never cents.
            count: Contract count as a fixed-point string; may be fractional.
            client_order_id: Idempotency key. Supplying it is what makes a
                retry safe, so it is required here rather than optional.

        Returns the ``CreateOrderV2Response``: ``order_id``, ``fill_count``,
        ``remaining_count``, and — only when something filled —
        ``average_fill_price`` and ``average_fee_paid`` (both *per contract*).
        """
        self._require_auth("/portfolio/events/orders")
        body = build_order_body(
            ticker=ticker,
            book_side=book_side,
            price_dollars=price_dollars,
            count=count,
            client_order_id=client_order_id,
            time_in_force=time_in_force,
            post_only=post_only,
            self_trade_prevention=self_trade_prevention,
            expiration_time=expiration_time,
        )
        return await self.request(
            "POST", "/portfolio/events/orders", json=body, cost=ORDER_TOKEN_COST
        )

    async def create_orders_batch(
        self, orders: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Submit several orders in one request.

        **This is not atomic.** The response carries a separate result per
        order — its own ``order_id``, ``fill_count``, and nullable fields —
        and nothing in the API promises all-or-nothing. It is a rate-limit
        convenience, not a transaction.

        It is still the right primitive for a multi-leg trade, because it
        collapses the window between legs to a single round trip instead of N.
        That reduces leg risk; it cannot remove it. Callers must check every
        result and handle an unbalanced outcome.

        Billed 10 tokens *per order*, so the whole batch draws N x 10 from the
        write budget.
        """
        self._require_auth("/portfolio/events/orders/batched")
        if not orders:
            return []
        payload = await self.request(
            "POST",
            "/portfolio/events/orders/batched",
            json={"orders": [build_order_body(**o) for o in orders]},
            cost=ORDER_TOKEN_COST * len(orders),
        )
        return list(payload.get("orders") or [])

    async def cancel_order(
        self, order_id: str, *, market_ticker: str | None = None
    ) -> dict[str, Any]:
        """Cancel a resting order. Returns ``{order_id, reduced_by, ts_ms}``.

        ``reduced_by`` is the count that was actually cancelled — for a
        partially filled order that is the remainder, not the original size.
        """
        self._require_auth("/portfolio/events/orders/{order_id}")
        return await self.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": market_ticker},
            cost=ORDER_TOKEN_COST,
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        self._require_auth("/portfolio/orders/{order_id}")
        payload = await self.get(f"/portfolio/orders/{order_id}")
        return payload.get("order", payload)

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 200,
        max_pages: int | None = 5,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate orders. ``status`` is ``resting``, ``canceled`` or ``executed``.

        ``min_ts`` / ``max_ts`` are Unix **seconds**, per the OpenAPI spec's
        ``MinTsQuery`` / ``MaxTsQuery``.
        """
        self._require_auth("/portfolio/orders")
        async for order in self.paginate(
            "/portfolio/orders",
            "orders",
            limit=limit,
            max_pages=max_pages,
            ticker=ticker,
            status=status,
            min_ts=min_ts,
            max_ts=max_ts,
        ):
            yield order

    async def find_order_by_client_id(
        self,
        client_order_id: str,
        *,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_pages: int | None = 5,
    ) -> dict[str, Any] | None:
        """Find an order the exchange may hold under one of our own IDs.

        This is the read half of the no-retry rule. A write that times out or
        500s may still have reached the matching engine, so :meth:`request`
        raises rather than re-POSTing — and the only safe way to learn what
        actually happened is to ask the exchange what it has under the
        ``client_order_id`` we generated before sending. A second POST would
        resolve the ambiguity by creating a second order.

        **The filter is applied here, not by the API.** ``GET
        /portfolio/orders`` takes ``ticker``, ``event_tickers``, ``min_ts``,
        ``max_ts`` and ``status`` and nothing else — checked against
        ``docs.kalshi.com/openapi.yaml`` on 2026-07-28 — so the match is made
        locally over the returned pages. ``client_order_id`` is a *required*
        field on the Order schema, so every row can be tested.

        Narrow it with ``ticker`` and ``min_ts`` whenever the caller knows
        them: unfiltered, this walks the account's whole recent order history
        and stops at ``max_pages`` regardless, at which point a miss means
        "not found in the pages we looked at", not "does not exist".

        Returns ``None`` when no order carries that ID in the pages scanned.
        A ``None`` is therefore *not* proof the order was never placed, and a
        caller must not treat it as one.
        """
        wanted = (client_order_id or "").strip()
        if not wanted:
            return None
        async for order in self.get_orders(
            ticker=ticker, min_ts=min_ts, max_pages=max_pages
        ):
            if str(order.get("client_order_id") or "").strip() == wanted:
                return order
        return None

    async def get_fills(
        self,
        *,
        ticker: str | None = None,
        order_id: str | None = None,
        min_ts: int | None = None,
        limit: int = 200,
        max_pages: int | None = 5,
    ) -> AsyncIterator[dict[str, Any]]:
        self._require_auth("/portfolio/fills")
        async for fill in self.paginate(
            "/portfolio/fills",
            "fills",
            limit=limit,
            max_pages=max_pages,
            ticker=ticker,
            order_id=order_id,
            min_ts=min_ts,
        ):
            yield fill

    async def get_settlements(
        self,
        *,
        ticker: str | None = None,
        event_ticker: str | None = None,
        min_ts: int | None = None,
        limit: int = 200,
        max_pages: int | None = 5,
    ) -> AsyncIterator[dict[str, Any]]:
        """Iterate settled markets the account held a position in.

        Mind the units: this payload mixes them. ``yes_total_cost_dollars``,
        ``no_total_cost_dollars`` and ``fee_cost`` are fixed-point dollar
        strings, but ``revenue`` and ``value`` are **integer cents** — two
        conventions in one object, and the only place in the API where that
        happens. Parsing a cents field as dollars understates it a
        hundredfold, silently.
        """
        self._require_auth("/portfolio/settlements")
        async for row in self.paginate(
            "/portfolio/settlements",
            "settlements",
            limit=limit,
            max_pages=max_pages,
            ticker=ticker,
            event_ticker=event_ticker,
            min_ts=min_ts,
        ):
            yield row

    async def get_positions(
        self, *, ticker: str | None = None, count_filter: str | None = "position"
    ) -> dict[str, Any]:
        """Market and event positions. Counts are fixed-point strings.

        ``position_fp`` is signed: positive is YES contracts, negative is NO.
        """
        self._require_auth("/portfolio/positions")
        return await self.get(
            "/portfolio/positions", ticker=ticker, count_filter=count_filter
        )

    async def get_balance(self) -> dict[str, Any]:
        """Available balance. ``balance`` is integer cents, ``balance_dollars``
        the fixed-point string; both describe the same money."""
        self._require_auth("/portfolio/balance")
        return await self.get("/portfolio/balance")

"""External reference prices — currently Bitcoin spot.

The crypto markets are strikes on a reference price, so a detector that
prices them needs a spot feed that is independent of Kalshi. Public endpoints
only, no credentials, and the source is recorded on every row: a price whose
provenance is unknown cannot be audited later, and "which exchange said so"
is exactly the question you ask when a signal turns out wrong.

Freshness is the property that matters. A stale reference compared against a
live market manufactures an edge in whichever direction the market has
already moved, so every observation is timestamped and consumers check the
age rather than trusting the newest row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.core.money import parse_dollars
from app.db.models import ExternalPrice

log = get_logger(__name__)

__all__ = ["SPOT_SOURCES", "fetch_spot", "record_spot"]

#: source -> (url, json path). Public, unauthenticated endpoints.
SPOT_SOURCES: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "coinbase": (
        "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        ("data", "amount"),
    ),
    "kraken": (
        "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
        ("result", "XXBTZUSD", "c", "0"),
    ),
    "binance": (
        "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
        ("price",),
    ),
}


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        payload = payload[int(key)] if isinstance(payload, list) else payload[key]
    return payload


async def fetch_spot(source: str, *, timeout: float = 8.0) -> Decimal:
    """Fetch BTC spot from ``source``.

    Raises rather than returning a sentinel: a spot price that quietly reads
    as zero would make every strike look decisively breached.
    """
    if source not in SPOT_SOURCES:
        raise ValueError(
            f"unknown spot source {source!r}; expected one of "
            f"{sorted(SPOT_SOURCES)}"
        )
    url, path = SPOT_SOURCES[source]
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        raw = _dig(response.json(), path)

    price = parse_dollars(raw, f"{source} spot")
    if price <= 0:
        raise ValueError(f"{source} returned a non-positive spot: {raw!r}")
    return price


def record_spot(source: str, price: Decimal, symbol: str = "BTC-USD") -> ExternalPrice:
    return ExternalPrice(
        ts=datetime.now(UTC), source=source, symbol=symbol, price=price
    )

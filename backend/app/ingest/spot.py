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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx

from app.core.logging import get_logger
from app.core.money import parse_dollars
from app.db.models import ExternalPrice

log = get_logger(__name__)

__all__ = [
    "SPOT_SOURCES",
    "fetch_spot",
    "record_spot",
    "fetch_minute_candles",
    "backfill_minutes",
]

#: Coinbase's public candle endpoint, which caps a response at 300 buckets.
#: Backfilling a day of minutes therefore takes several windowed requests.
CANDLE_URL: Final = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
CANDLE_MAX_BUCKETS: Final = 300

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


async def fetch_minute_candles(
    *, minutes: int, timeout: float = 15.0
) -> list[tuple[datetime, Decimal]]:
    """Fetch the last ``minutes`` one-minute closes, oldest first.

    The volatility model needs a return series, and the live poller only
    produces one going forward — on a cold start it has nothing, so a
    freshly-deployed detector would sit silent for a day. It would sit silent
    *correctly* (the estimator refuses below its sample floor rather than
    guessing from six ticks), but a limit that is right and useless for
    twenty-four hours is worth avoiding.

    One minute is the sampling interval on purpose. The live poller runs every
    three seconds, and an EWMA at lambda=0.94 has an effective memory of only
    about 1/(1-lambda) ~ 17 observations — seventeen three-second ticks is
    under a minute of history, which produces a volatility estimate that
    swings wildly on microstructure noise rather than measuring anything.
    Consumers bucket by minute for the same reason; see the vol reader.
    """
    if minutes <= 0:
        return []

    end = datetime.now(UTC)
    out: dict[datetime, Decimal] = {}

    async with httpx.AsyncClient(timeout=timeout) as client:
        remaining = minutes
        while remaining > 0:
            span = min(remaining, CANDLE_MAX_BUCKETS)
            start = end - timedelta(minutes=span)
            response = await client.get(
                CANDLE_URL,
                params={
                    "granularity": 60,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                break

            # [time, low, high, open, close, volume], newest first.
            for row in rows:
                ts = datetime.fromtimestamp(int(row[0]), tz=UTC)
                close = parse_dollars(row[4], "coinbase candle close")
                if close <= 0:
                    # A zero close is corrupt, and a zero spot makes every
                    # strike look decisively breached. Drop the bucket rather
                    # than carry it into a return series.
                    continue
                out[ts] = close

            end = start
            remaining -= span

    return sorted(out.items())


def backfill_minutes(
    candles: list[tuple[datetime, Decimal]],
    *,
    source: str = "coinbase_1m",
    symbol: str = "BTC-USD",
) -> list[ExternalPrice]:
    """Turn fetched candles into rows.

    Recorded under a distinct ``source`` so a backfilled close is never
    mistaken for a live tick. They are not the same observation: a candle
    close is the last trade in a minute that has already ended, whereas a
    poll is a quote as of now, and the freshness checks that gate every
    detector must never treat the former as the latter.
    """
    return [
        ExternalPrice(ts=ts, source=source, symbol=symbol, price=price)
        for ts, price in candles
    ]

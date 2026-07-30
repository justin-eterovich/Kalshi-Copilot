"""External reference prices — crypto spot for the strike detectors.

The crypto markets are strikes on a reference price, so a detector that
prices them needs a spot feed that is independent of Kalshi. Public endpoints
only, no credentials, and the source is recorded on every row: a price whose
provenance is unknown cannot be audited later, and "which exchange said so"
is exactly the question you ask when a signal turns out wrong.

**Every (source, symbol) pair is written out longhand below rather than
templated from the symbol.** Building `.../prices/{symbol}/spot` would look
tidier and would silently invent an endpoint for any symbol asked of it —
including one the venue does not list, which returns something parseable often
enough to matter. It would also not survive Kraken, whose result keys are
irregular in a way no template expresses: BTC is `XXBTZUSD`, ETH is
`XETHZUSD`, XRP is `XXRPZUSD`, and SOL is plain `SOLUSD` with neither prefix.
A wrong-asset price is the single most expensive bug this repository has
shipped — an ETH strike priced against BTC spot reported a 72c edge — so the
table is explicit and an unlisted pair is a refusal.

Freshness is the property that matters. A stale reference compared against a
live market manufactures an edge in whichever direction the market has
already moved, so every observation is timestamped and consumers check the
age rather than trusting the newest row.

**This is not the settlement source, and the difference is a real basis.**
Kalshi's crypto markets settle on a **CF Benchmarks** index — the Bitcoin
Real-Time Index (BRTI) for BTC, ETHUSD_RTI for ETH — and usually on the
*simple average of the sixty seconds* of that index before a stated instant.
What is polled here is one exchange's **last trade**. Three differences, all
in the same direction:

1. Different publisher: one venue's tape versus a multi-venue index.
2. Different statistic: an instantaneous print versus a 60-second mean.
3. Different instrument on two of the three sources — Binance quotes BTC**USDT**,
   which is a stablecoin pair, not USD.

Near a strike this basis is the same size as the edge being claimed, so the
detector that reads these rows treats a *decisive* margin as a precondition
rather than pricing the last basis point. The weather engine refuses markets
on exactly this ground (`KXTEMPNYCH` settles on The Weather Company, so NWS
data there is a proxy for a different source); the crypto path does not refuse,
because a decisive move is still decisive under any of these measures — but
nothing here should ever be described as "the settlement price".
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
    "SPOT_SYMBOLS",
    "supported_symbols",
    "fetch_spot",
    "record_spot",
    "fetch_minute_candles",
    "backfill_minutes",
]

#: Coinbase's public candle endpoint, which caps a response at 300 buckets.
#: Backfilling a day of minutes therefore takes several windowed requests.
#: Templated on the product because a candle series carries no asset label of
#: its own — backfilling ETH from the BTC product would produce a return series
#: that looks entirely reasonable and describes the wrong coin.
CANDLE_URL_TEMPLATE: Final = (
    "https://api.exchange.coinbase.com/products/{product}/candles"
)
CANDLE_MAX_BUCKETS: Final = 300

#: Every symbol any source here can price. This is the vocabulary; which of
#: them are actually polled is `bitcoin.spot_symbols` in config.
SPOT_SYMBOLS: Final[tuple[str, ...]] = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD")

#: source -> symbol -> (url, json path). Public, unauthenticated endpoints.
#:
#: Verified live on 2026-07-30 for coinbase and kraken, including the response
#: shape of every path below. The **binance** rows are unverified: the endpoint
#: refuses this host's region outright ("Service unavailable from a restricted
#: location"), so those entries are the documented symbol substitution and
#: nothing more. They fail loudly if wrong — `_dig` raises on a missing key —
#: which is the correct direction, but do not treat them as tested.
SPOT_SOURCES: Final[dict[str, dict[str, tuple[str, tuple[str, ...]]]]] = {
    "coinbase": {
        "BTC-USD": (
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            ("data", "amount"),
        ),
        "ETH-USD": (
            "https://api.coinbase.com/v2/prices/ETH-USD/spot",
            ("data", "amount"),
        ),
        "SOL-USD": (
            "https://api.coinbase.com/v2/prices/SOL-USD/spot",
            ("data", "amount"),
        ),
        "XRP-USD": (
            "https://api.coinbase.com/v2/prices/XRP-USD/spot",
            ("data", "amount"),
        ),
    },
    # Kraken's result keys are the reason this table is not templated. Three of
    # the four carry the legacy X/Z asset-class prefixes and SOL does not.
    "kraken": {
        "BTC-USD": (
            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
            ("result", "XXBTZUSD", "c", "0"),
        ),
        "ETH-USD": (
            "https://api.kraken.com/0/public/Ticker?pair=ETHUSD",
            ("result", "XETHZUSD", "c", "0"),
        ),
        "SOL-USD": (
            "https://api.kraken.com/0/public/Ticker?pair=SOLUSD",
            ("result", "SOLUSD", "c", "0"),
        ),
        "XRP-USD": (
            "https://api.kraken.com/0/public/Ticker?pair=XRPUSD",
            ("result", "XXRPZUSD", "c", "0"),
        ),
    },
    # Note these are USD**T** pairs — a stablecoin quote, not USD. The module
    # docstring's basis warning applies with one extra term on this source.
    "binance": {
        "BTC-USD": (
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            ("price",),
        ),
        "ETH-USD": (
            "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
            ("price",),
        ),
        "SOL-USD": (
            "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
            ("price",),
        ),
        "XRP-USD": (
            "https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT",
            ("price",),
        ),
    },
}


def supported_symbols(source: str) -> tuple[str, ...]:
    """Symbols ``source`` can price, in :data:`SPOT_SYMBOLS` order.

    Empty for an unknown source. Callers use this to refuse an unpollable
    configuration once at startup rather than logging the same failure every
    poll interval.
    """
    table = SPOT_SOURCES.get(source, {})
    return tuple(symbol for symbol in SPOT_SYMBOLS if symbol in table)


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        payload = payload[int(key)] if isinstance(payload, list) else payload[key]
    return payload


async def fetch_spot(
    source: str,
    symbol: str = "BTC-USD",
    *,
    timeout: float = 8.0,
    client: httpx.AsyncClient | None = None,
) -> Decimal:
    """Fetch ``symbol`` spot from ``source``.

    Raises rather than returning a sentinel: a spot price that quietly reads
    as zero would make every strike look decisively breached. An unlisted
    (source, symbol) pair raises for the same reason — there is no sensible
    fallback symbol, and reaching for the nearest one is how an ETH strike got
    priced against Bitcoin.

    ``client`` lets a caller share one connection pool. The poller fans out
    across every configured symbol on a three-second timer, and a fresh
    ``AsyncClient`` per symbol per tick is a TLS handshake per symbol per tick
    against someone else's free endpoint.
    """
    table = SPOT_SOURCES.get(source)
    if table is None:
        raise ValueError(
            f"unknown spot source {source!r}; expected one of "
            f"{sorted(SPOT_SOURCES)}"
        )
    entry = table.get(symbol)
    if entry is None:
        raise ValueError(
            f"{source} has no feed for {symbol!r}; it prices "
            f"{list(supported_symbols(source))}"
        )
    url, path = entry

    if client is not None:
        response = await client.get(url)
        response.raise_for_status()
        raw = _dig(response.json(), path)
    else:
        async with httpx.AsyncClient(timeout=timeout) as owned:
            response = await owned.get(url)
            response.raise_for_status()
            raw = _dig(response.json(), path)

    price = parse_dollars(raw, f"{source} {symbol} spot")
    if price <= 0:
        raise ValueError(f"{source} returned a non-positive {symbol} spot: {raw!r}")
    return price


def record_spot(source: str, price: Decimal, symbol: str = "BTC-USD") -> ExternalPrice:
    return ExternalPrice(
        ts=datetime.now(UTC), source=source, symbol=symbol, price=price
    )


async def fetch_minute_candles(
    *, minutes: int, symbol: str = "BTC-USD", timeout: float = 15.0
) -> list[tuple[datetime, Decimal]]:
    """Fetch the last ``minutes`` one-minute closes for ``symbol``, oldest first.

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
    if symbol not in SPOT_SYMBOLS:
        raise ValueError(
            f"no candle product for {symbol!r}; expected one of {list(SPOT_SYMBOLS)}"
        )

    # Coinbase's product id is the symbol as we spell it, but assert rather
    # than assume: a mismatch here backfills one asset's history under
    # another's name, which no downstream check can detect.
    url = CANDLE_URL_TEMPLATE.format(product=symbol)
    end = datetime.now(UTC)
    out: dict[datetime, Decimal] = {}

    async with httpx.AsyncClient(timeout=timeout) as client:
        remaining = minutes
        while remaining > 0:
            span = min(remaining, CANDLE_MAX_BUCKETS)
            start = end - timedelta(minutes=span)
            response = await client.get(
                url,
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
                close = parse_dollars(row[4], f"coinbase {symbol} candle close")
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

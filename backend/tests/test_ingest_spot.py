"""Spot feed table and fetch behaviour.

The thing under test is mostly a *table*, and the reason it is worth testing is
that the table's failure mode is silent. A wrong URL or a mistyped JSON path
does not produce a crash — it produces a plausible number for the wrong asset,
which is how an ETH strike priced against Bitcoin once reported a 72c edge on a
26c contract. So these tests assert that every pair is reachable by name, that
no two symbols share an endpoint, and that an unlisted pair refuses.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.ingest.spot import (
    SPOT_SOURCES,
    SPOT_SYMBOLS,
    fetch_minute_candles,
    fetch_spot,
    record_spot,
    supported_symbols,
)


def test_every_source_prices_every_symbol() -> None:
    """A source that covers only some symbols silently halves the universe."""
    for source, table in SPOT_SOURCES.items():
        assert set(table) == set(SPOT_SYMBOLS), (
            f"{source} is missing {set(SPOT_SYMBOLS) - set(table)}"
        )


def test_no_two_symbols_share_an_endpoint() -> None:
    """The wrong-asset bug, caught at the table.

    Copy-pasting a row and forgetting to change the URL yields two symbols
    quoting one asset. Nothing downstream can detect that: both prices are
    real, finite, and positive.
    """
    for source, table in SPOT_SOURCES.items():
        urls = [url for url, _ in table.values()]
        assert len(set(urls)) == len(urls), f"{source} reuses an endpoint: {urls}"


def test_no_two_symbols_share_a_kraken_result_key() -> None:
    """Kraken's keys are irregular enough to get wrong by pattern-matching."""
    paths = [path for _, path in SPOT_SOURCES["kraken"].values()]
    assert len(set(paths)) == len(paths), paths


@pytest.mark.parametrize("symbol", SPOT_SYMBOLS)
def test_symbol_appears_in_its_own_endpoint(symbol: str) -> None:
    """Each URL mentions the asset it claims to price.

    Deliberately checks the *base* only ("ETH"), not the full symbol: Kraken
    spells BTC as XBT and Binance quotes USDT, so the quote currency differs
    legitimately while the base never should.
    """
    base = symbol.split("-")[0]
    aliases = {"BTC": ("BTC", "XBT")}.get(base, (base,))
    for source, table in SPOT_SOURCES.items():
        url, _ = table[symbol]
        assert any(a in url.upper() for a in aliases), f"{source} {symbol}: {url}"


def test_supported_symbols_is_empty_for_an_unknown_source() -> None:
    assert supported_symbols("nasdaq") == ()


def test_supported_symbols_follows_the_canonical_order() -> None:
    """The poller logs this list; a wandering order makes logs hard to diff."""
    assert supported_symbols("coinbase") == SPOT_SYMBOLS


async def test_fetch_spot_refuses_an_unknown_source() -> None:
    with pytest.raises(ValueError, match="unknown spot source"):
        await fetch_spot("nasdaq", "BTC-USD")


async def test_fetch_spot_refuses_a_symbol_the_source_cannot_price() -> None:
    """Refusal, not a fallback to BTC. There is no safe default symbol."""
    with pytest.raises(ValueError, match="no feed for"):
        await fetch_spot("coinbase", "DOGE-USD")


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_fetch_spot_reads_the_symbol_it_was_asked_for() -> None:
    """The URL requested must be the one the table names for that symbol."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"amount": "1908.705"}})

    async with _client(handler) as client:
        price = await fetch_spot("coinbase", "ETH-USD", client=client)

    assert price == Decimal("1908.705")
    assert seen == [SPOT_SOURCES["coinbase"]["ETH-USD"][0]]
    assert "ETH-USD" in seen[0]


async def test_fetch_spot_walks_krakens_nested_path() -> None:
    """SOL is the row with no X/Z prefix, so it is the one to exercise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"SOLUSD": {"c": ["73.64", "1"]}}})

    async with _client(handler) as client:
        assert await fetch_spot("kraken", "SOL-USD", client=client) == Decimal("73.64")


async def test_fetch_spot_refuses_a_non_positive_price() -> None:
    """A zero spot makes every strike look decisively breached."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"amount": "0"}})

    async with _client(handler) as client:
        with pytest.raises(ValueError, match="non-positive"):
            await fetch_spot("coinbase", "XRP-USD", client=client)


async def test_fetch_spot_raises_when_the_json_path_is_missing() -> None:
    """The binance rows are unverified from this host; they must fail loudly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "restricted location"})

    async with _client(handler) as client:
        with pytest.raises(KeyError):
            await fetch_spot("binance", "ETH-USD", client=client)


async def test_fetch_spot_keeps_sub_cent_precision() -> None:
    """XRP trades near a dollar, so precision is the whole quote here."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"amount": "1.07455"}})

    async with _client(handler) as client:
        assert await fetch_spot("coinbase", "XRP-USD", client=client) == Decimal(
            "1.07455"
        )


def test_record_spot_labels_the_row_with_its_symbol() -> None:
    row = record_spot("coinbase", Decimal("73.64"), "SOL-USD")
    assert (row.symbol, row.source, row.price) == (
        "SOL-USD",
        "coinbase",
        Decimal("73.64"),
    )


async def test_candle_backfill_refuses_an_unknown_product() -> None:
    """Backfilling ETH from the BTC product is undetectable downstream."""
    with pytest.raises(ValueError, match="no candle product"):
        await fetch_minute_candles(minutes=10, symbol="DOGE-USD")

"""Translate Kalshi API payloads into ORM row dicts.

Kept separate from the network and database layers so it can be tested
against captured payloads with no I/O at all.

The API's fixed-point string fields are parsed into Decimals here, once, at
the boundary. Nothing downstream should ever see a raw wire string.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.money import parse_count, parse_dollars

__all__ = [
    "normalize_market",
    "normalize_event",
    "normalize_series",
    "normalize_trade",
    "normalize_ticker",
]


def _dt(value: Any) -> datetime | None:
    """Parse an API timestamp: ISO-8601 string or epoch seconds/millis."""
    if value in (None, "", 0):
        return None

    if isinstance(value, int | float):
        # Heuristic: anything past ~2001 in ms is > 1e12.
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    return None


def _price(payload: dict[str, Any], *keys: str) -> Decimal | None:
    """First present price field, parsed as dollars."""
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            try:
                return parse_dollars(value, key)
            except ValueError:
                continue
    return None


def _count(payload: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            try:
                return parse_count(value, key)
            except ValueError:
                continue
    return None


def normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a ``Market`` payload to a ``markets`` row."""
    ticker = raw.get("ticker")
    if not ticker:
        return None

    event_ticker = raw.get("event_ticker")

    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        # Kalshi series tickers are the event ticker's first dash-segment.
        "series_ticker": raw.get("series_ticker")
        or (event_ticker.split("-")[0] if event_ticker else None),
        "market_type": raw.get("market_type"),
        # `title`/`subtitle` are deprecated; the sub_title fields are current.
        "title": raw.get("title") or raw.get("yes_sub_title"),
        "yes_sub_title": raw.get("yes_sub_title"),
        "no_sub_title": raw.get("no_sub_title"),
        "category": raw.get("category"),
        "status": raw.get("status"),
        "open_time": _dt(raw.get("open_time")),
        "close_time": _dt(raw.get("close_time")),
        "expected_expiration_time": _dt(raw.get("expected_expiration_time")),
        "latest_expiration_time": _dt(raw.get("latest_expiration_time")),
        "updated_time": _dt(raw.get("updated_time")),
        "yes_bid": _price(raw, "yes_bid_dollars"),
        "yes_ask": _price(raw, "yes_ask_dollars"),
        "no_bid": _price(raw, "no_bid_dollars"),
        "no_ask": _price(raw, "no_ask_dollars"),
        "last_price": _price(raw, "last_price_dollars"),
        "previous_price": _price(raw, "previous_price_dollars"),
        "yes_bid_size": _count(raw, "yes_bid_size_fp"),
        "yes_ask_size": _count(raw, "yes_ask_size_fp"),
        "volume": _count(raw, "volume_fp"),
        "volume_24h": _count(raw, "volume_24h_fp"),
        "open_interest": _count(raw, "open_interest_fp"),
        "liquidity_dollars": _price(raw, "liquidity_dollars"),
        "result": raw.get("result") or None,
        "settlement_value": _price(raw, "settlement_value_dollars"),
        "settlement_ts": _dt(raw.get("settlement_ts")),
        "strike_type": raw.get("strike_type"),
        "floor_strike": _decimal_or_none(raw.get("floor_strike")),
        "cap_strike": _decimal_or_none(raw.get("cap_strike")),
        "price_level_structure": raw.get("price_level_structure"),
        "rules_primary": raw.get("rules_primary"),
        "rules_secondary": raw.get("rules_secondary"),
        "raw": raw,
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def normalize_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    ticker = raw.get("event_ticker") or raw.get("ticker")
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "series_ticker": raw.get("series_ticker")
        or (ticker.split("-")[0] if ticker else None),
        "title": raw.get("title"),
        "sub_title": raw.get("sub_title"),
        "category": raw.get("category"),
        # Drives the set-arbitrage detector: only exhaustive mutually
        # exclusive sets can be arbitraged as a complete basket.
        "mutually_exclusive": raw.get("mutually_exclusive"),
        "strike_date": _dt(raw.get("strike_date")),
        "raw": raw,
    }


def normalize_series(raw: dict[str, Any]) -> dict[str, Any] | None:
    ticker = raw.get("ticker") or raw.get("series_ticker")
    if not ticker:
        return None
    return {
        "ticker": ticker,
        "title": raw.get("title"),
        "category": raw.get("category"),
        "frequency": raw.get("frequency"),
        "settlement_sources": raw.get("settlement_sources"),
        "raw": raw,
    }


def normalize_trade(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a public trade (REST or WS ``trade`` channel) to a ``tape`` row."""
    ticker = raw.get("market_ticker") or raw.get("ticker")
    if not ticker:
        return None

    yes_price = _price(raw, "yes_price_dollars")
    if yes_price is None:
        return None

    count = _count(raw, "count_fp")
    if count is None:
        return None

    ts = _dt(raw.get("ts_ms") or raw.get("ts") or raw.get("created_time"))
    if ts is None:
        return None

    return {
        "trade_id": raw.get("trade_id"),
        "ticker": ticker,
        "ts": ts,
        "yes_price": yes_price,
        "no_price": _price(raw, "no_price_dollars"),
        "count": count,
        "taker_side": raw.get("taker_side"),
    }


def normalize_ticker(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a WS ``ticker`` message into a partial ``markets`` update."""
    ticker = raw.get("market_ticker") or raw.get("ticker")
    if not ticker:
        return None

    update: dict[str, Any] = {"ticker": ticker}
    field_map = {
        "last_price": ("price_dollars",),
        "yes_bid": ("yes_bid_dollars",),
        "yes_ask": ("yes_ask_dollars",),
    }
    for column, keys in field_map.items():
        value = _price(raw, *keys)
        if value is not None:
            update[column] = value

    count_map = {
        "volume": ("volume_fp",),
        "open_interest": ("open_interest_fp",),
        "yes_bid_size": ("yes_bid_size_fp",),
        "yes_ask_size": ("yes_ask_size_fp",),
    }
    for column, keys in count_map.items():
        value = _count(raw, *keys)
        if value is not None:
            update[column] = value

    # A ticker message with no usable field is not worth a database round trip.
    if len(update) == 1:
        return None

    update["_ts"] = _dt(raw.get("ts_ms") or raw.get("ts"))
    return update

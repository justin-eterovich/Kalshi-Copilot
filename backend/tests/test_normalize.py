"""Tests for API payload normalization.

Built against payload shapes taken from Kalshi's published OpenAPI/AsyncAPI
examples. The recurring risk here is a units mistake: reading a dollar string
as cents, or letting a malformed field become zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.ingest.normalize import (
    normalize_event,
    normalize_market,
    normalize_ticker,
    normalize_trade,
)

# Shape taken from the OpenAPI `Market` schema.
MARKET_PAYLOAD = {
    "ticker": "KXHIGHNY-25JUL25-B53.5",
    "event_ticker": "KXHIGHNY-25JUL25",
    "market_type": "binary",
    "yes_sub_title": "53.5° or above",
    "no_sub_title": "Below 53.5°",
    "status": "active",
    "open_time": "2026-07-25T04:00:00Z",
    "close_time": "2026-07-26T04:00:00Z",
    "updated_time": "2026-07-25T18:30:00Z",
    "yes_bid_dollars": "0.4200",
    "yes_ask_dollars": "0.4500",
    "no_bid_dollars": "0.5500",
    "no_ask_dollars": "0.5800",
    "last_price_dollars": "0.4300",
    "yes_bid_size_fp": "300.00",
    "volume_fp": "15234.00",
    "volume_24h_fp": "8120.00",
    "open_interest_fp": "20422.00",
    "liquidity_dollars": "5300.00",
    "rules_primary": "Settles per the NWS climate report for Central Park.",
    "price_level_structure": "penny",
}


class TestMarket:
    def test_parses_prices_as_dollars_not_cents(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert row["yes_bid"] == Decimal("0.4200")
        assert row["yes_ask"] == Decimal("0.4500")
        # The classic bug this guards against:
        assert row["yes_bid"] < 1

    def test_parses_counts(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert row["volume"] == Decimal("15234.00")
        assert row["open_interest"] == Decimal("20422.00")

    def test_derives_series_ticker_from_event(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert row["series_ticker"] == "KXHIGHNY"

    def test_explicit_series_ticker_wins(self) -> None:
        row = normalize_market({**MARKET_PAYLOAD, "series_ticker": "EXPLICIT"})
        assert row is not None
        assert row["series_ticker"] == "EXPLICIT"

    def test_falls_back_to_sub_title_when_title_deprecated_and_absent(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert row["title"] == "53.5° or above"

    def test_keeps_rules_text_for_the_weather_engine(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert "Central Park" in row["rules_primary"]

    def test_parses_timestamps_to_utc(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert row["close_time"] == datetime(2026, 7, 26, 4, 0, tzinfo=UTC)

    def test_missing_ticker_is_dropped(self) -> None:
        assert normalize_market({"event_ticker": "X"}) is None

    def test_absent_price_fields_are_none_not_zero(self) -> None:
        """A missing quote must never read as $0.00 — that looks like free money."""
        row = normalize_market({"ticker": "T", "event_ticker": "E"})
        assert row is not None
        assert row["yes_bid"] is None
        assert row["last_price"] is None

    def test_malformed_price_becomes_none(self) -> None:
        row = normalize_market({**MARKET_PAYLOAD, "yes_bid_dollars": "garbage"})
        assert row is not None
        assert row["yes_bid"] is None

    def test_raw_payload_is_retained(self) -> None:
        row = normalize_market(MARKET_PAYLOAD)
        assert row is not None
        assert row["raw"] == MARKET_PAYLOAD


class TestTrade:
    PAYLOAD = {
        "trade_id": "d91bc706-ee49-470d-82d8-11418bda6fed",
        "market_ticker": "HIGHNY-22DEC23-B53.5",
        "yes_price_dollars": "0.360",
        "no_price_dollars": "0.640",
        "count_fp": "136.00",
        "taker_side": "no",
        "ts": 1669149841,
        "ts_ms": 1669149841000,
    }

    def test_parses_ws_trade(self) -> None:
        row = normalize_trade(self.PAYLOAD)
        assert row is not None
        assert row["yes_price"] == Decimal("0.360")
        assert row["count"] == Decimal("136.00")
        assert row["taker_side"] == "no"

    def test_prefers_millisecond_timestamp(self) -> None:
        row = normalize_trade(self.PAYLOAD)
        assert row is not None
        assert row["ts"] == datetime.fromtimestamp(1669149841, tz=UTC)

    def test_second_timestamps_still_work(self) -> None:
        payload = {k: v for k, v in self.PAYLOAD.items() if k != "ts_ms"}
        row = normalize_trade(payload)
        assert row is not None
        assert row["ts"].year == 2022

    def test_trade_without_price_is_dropped(self) -> None:
        payload = {k: v for k, v in self.PAYLOAD.items() if k != "yes_price_dollars"}
        assert normalize_trade(payload) is None

    def test_trade_without_ticker_is_dropped(self) -> None:
        payload = {k: v for k, v in self.PAYLOAD.items() if k != "market_ticker"}
        assert normalize_trade(payload) is None

    def test_fractional_count_supported(self) -> None:
        row = normalize_trade({**self.PAYLOAD, "count_fp": "2.50"})
        assert row is not None
        assert row["count"] == Decimal("2.50")


class TestTickerMessage:
    PAYLOAD = {
        "market_ticker": "FED-23DEC-T3.00",
        "price_dollars": "0.480",
        "yes_bid_dollars": "0.450",
        "yes_ask_dollars": "0.530",
        "volume_fp": "33896.00",
        "open_interest_fp": "20422.00",
        "ts_ms": 1669149841000,
    }

    def test_maps_price_to_last_price(self) -> None:
        row = normalize_ticker(self.PAYLOAD)
        assert row is not None
        assert row["last_price"] == Decimal("0.480")
        assert row["yes_bid"] == Decimal("0.450")

    def test_includes_ticker_key(self) -> None:
        row = normalize_ticker(self.PAYLOAD)
        assert row is not None
        assert row["ticker"] == "FED-23DEC-T3.00"

    def test_message_with_no_useful_fields_is_dropped(self) -> None:
        """Avoid a database round trip for an empty update."""
        assert normalize_ticker({"market_ticker": "X"}) is None

    def test_partial_update_only_includes_present_fields(self) -> None:
        row = normalize_ticker(
            {"market_ticker": "X", "yes_bid_dollars": "0.100"}
        )
        assert row is not None
        assert "yes_bid" in row
        assert "yes_ask" not in row
        assert "last_price" not in row


class TestEvent:
    def test_captures_mutually_exclusive_flag(self) -> None:
        """Set-arbitrage depends entirely on this flag being right."""
        row = normalize_event(
            {
                "event_ticker": "KXHIGHNY-25JUL25",
                "title": "NYC high temp",
                "mutually_exclusive": True,
            }
        )
        assert row is not None
        assert row["mutually_exclusive"] is True
        assert row["series_ticker"] == "KXHIGHNY"

    def test_missing_flag_is_none_not_false(self) -> None:
        """Unknown must not masquerade as 'not exclusive'."""
        row = normalize_event({"event_ticker": "X-1"})
        assert row is not None
        assert row["mutually_exclusive"] is None

"""Tests for REST candle backfill.

Two behaviours matter here and both are easy to get subtly wrong:

- Candles are keyed by period *end* on the wire and period *start* internally.
  An off-by-one-period chart is the kind of bug nobody notices until a
  detector times a signal against the wrong minute.
- A period with no trades has null price OHLC. Thin markets are most of the
  platform, so falling back to the quote midpoint is what stops their charts
  rendering empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ingest.backfill import normalize_candlestick, period_start


class TestPeriodStart:
    def test_aligned_end_maps_to_previous_bucket(self) -> None:
        # 12:01:00 end of the 12:00 minute candle
        end = int(datetime(2026, 7, 25, 12, 1, 0, tzinfo=UTC).timestamp())
        assert period_start(end, 60) == datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

    def test_inclusive_last_second_maps_to_same_bucket(self) -> None:
        """Correct under either API convention."""
        end = int(datetime(2026, 7, 25, 12, 0, 59, tzinfo=UTC).timestamp())
        assert period_start(end, 60) == datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

    def test_hourly_bucket(self) -> None:
        end = int(datetime(2026, 7, 25, 13, 0, 0, tzinfo=UTC).timestamp())
        assert period_start(end, 3600) == datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

    def test_daily_bucket(self) -> None:
        end = int(datetime(2026, 7, 26, 0, 0, 0, tzinfo=UTC).timestamp())
        assert period_start(end, 86400) == datetime(2026, 7, 25, 0, 0, 0, tzinfo=UTC)

    def test_consecutive_ends_produce_consecutive_buckets(self) -> None:
        base = int(datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC).timestamp())
        starts = [period_start(base + 60 * i, 60) for i in range(1, 5)]
        deltas = {
            (starts[i + 1] - starts[i]).total_seconds() for i in range(len(starts) - 1)
        }
        assert deltas == {60}


class TestNormalizeCandlestick:
    END = int(datetime(2026, 7, 25, 12, 1, 0, tzinfo=UTC).timestamp())

    def traded(self) -> dict:
        return {
            "end_period_ts": self.END,
            "price": {
                "open_dollars": "0.4000",
                "high_dollars": "0.4600",
                "low_dollars": "0.3900",
                "close_dollars": "0.4500",
            },
            "yes_bid": {"close_dollars": "0.4400"},
            "yes_ask": {"close_dollars": "0.4600"},
            "volume_fp": "150.00",
            "open_interest_fp": "2000.00",
        }

    def test_parses_a_traded_period(self) -> None:
        row = normalize_candlestick(self.traded(), "T", 60)
        assert row is not None
        assert row["open"] == Decimal("0.4000")
        assert row["high"] == Decimal("0.4600")
        assert row["low"] == Decimal("0.3900")
        assert row["close"] == Decimal("0.4500")
        assert row["volume"] == Decimal("150.00")

    def test_uses_period_start_not_end(self) -> None:
        row = normalize_candlestick(self.traded(), "T", 60)
        assert row is not None
        assert row["ts"] == datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

    def test_untraded_period_falls_back_to_quote_midpoint(self) -> None:
        """Most periods in a thin market have no trades at all."""
        raw = {
            "end_period_ts": self.END,
            "price": {
                "open_dollars": None,
                "high_dollars": None,
                "low_dollars": None,
                "close_dollars": None,
            },
            "yes_bid": {"close_dollars": "0.4000"},
            "yes_ask": {"close_dollars": "0.5000"},
            "volume_fp": "0.00",
        }
        row = normalize_candlestick(raw, "T", 60)
        assert row is not None
        assert row["close"] == Decimal("0.45")
        # Flat bar: no trading happened, so it must not imply a range.
        assert row["open"] == row["high"] == row["low"] == row["close"]
        assert row["volume"] == Decimal(0)

    def test_untraded_with_one_sided_quote_uses_that_side(self) -> None:
        raw = {
            "end_period_ts": self.END,
            "price": {"close_dollars": None},
            "yes_bid": {"close_dollars": "0.3000"},
            "yes_ask": {},
        }
        row = normalize_candlestick(raw, "T", 60)
        assert row is not None
        assert row["close"] == Decimal("0.3000")

    def test_untraded_with_no_quotes_falls_back_to_previous(self) -> None:
        raw = {
            "end_period_ts": self.END,
            "price": {"close_dollars": None, "previous_dollars": "0.2500"},
            "yes_bid": {},
            "yes_ask": {},
        }
        row = normalize_candlestick(raw, "T", 60)
        assert row is not None
        assert row["close"] == Decimal("0.2500")

    def test_period_with_no_usable_price_is_dropped(self) -> None:
        """Better to omit a bar than invent one at zero."""
        raw = {
            "end_period_ts": self.END,
            "price": {"close_dollars": None},
            "yes_bid": {},
            "yes_ask": {},
        }
        assert normalize_candlestick(raw, "T", 60) is None

    def test_missing_timestamp_is_dropped(self) -> None:
        assert normalize_candlestick({"price": {"close_dollars": "0.5"}}, "T", 60) is None

    def test_partial_ohlc_is_completed_consistently(self) -> None:
        """A close without a full OHLC set must still yield a valid bar."""
        raw = {
            "end_period_ts": self.END,
            "price": {"close_dollars": "0.6000", "open_dollars": "0.5000"},
            "volume_fp": "10.00",
        }
        row = normalize_candlestick(raw, "T", 60)
        assert row is not None
        assert row["high"] >= max(row["open"], row["close"])
        assert row["low"] <= min(row["open"], row["close"])

    def test_malformed_volume_does_not_break_the_bar(self) -> None:
        raw = self.traded()
        raw["volume_fp"] = "not-a-number"
        row = normalize_candlestick(raw, "T", 60)
        assert row is not None
        assert row["volume"] == Decimal(0)

    def test_sub_cent_prices_survive(self) -> None:
        raw = self.traded()
        raw["price"]["close_dollars"] = "0.456700"
        row = normalize_candlestick(raw, "T", 60)
        assert row is not None
        assert row["close"] == Decimal("0.456700")

    @pytest.mark.parametrize("period_sec", [60, 3600, 86400])
    def test_period_recorded_on_the_row(self, period_sec: int) -> None:
        row = normalize_candlestick(self.traded(), "T", period_sec)
        assert row is not None
        assert row["period_sec"] == period_sec

"""Tests for the rate limiter, request signing, and candle aggregation."""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.ingest.streams import CandleBuilder
from app.kalshi.auth import KalshiSigner, SigningError
from app.kalshi.ratelimit import DEFAULT_TOKEN_COST, Bucket, RateLimiter

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_basic_tier_matches_documented_limits(self) -> None:
        """Basic: 200 read + 100 write tokens/sec, 10 tokens per request."""
        limiter = RateLimiter("basic")
        assert limiter.reads_per_sec == 20
        assert limiter.writes_per_sec == 10

    def test_basic_write_bucket_holds_only_one_second(self) -> None:
        """Documented exception: Basic write burst is 1s, not 2s."""
        basic = RateLimiter("basic")
        advanced = RateLimiter("advanced")
        assert basic.write.tokens == pytest.approx(100, abs=1)
        assert advanced.write.tokens == pytest.approx(600, abs=1)

    def test_read_and_write_budgets_are_independent(self) -> None:
        """Draining reads must not throttle order placement."""
        limiter = RateLimiter("basic")
        before = limiter.write.tokens
        asyncio.run(limiter.acquire(is_write=False, cost=100))
        assert limiter.write.tokens == pytest.approx(before, abs=1)

    def test_unknown_tier_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown rate tier"):
            RateLimiter("platinum")

    def test_tier_is_case_insensitive(self) -> None:
        assert RateLimiter("BASIC").tier == "basic"


class TestBucket:
    def test_allows_burst_up_to_capacity(self) -> None:
        async def run() -> float:
            bucket = Bucket(rate_per_sec=100, capacity=100)
            start = time.monotonic()
            for _ in range(10):
                await bucket.acquire(DEFAULT_TOKEN_COST)
            return time.monotonic() - start

        # 10 requests x 10 tokens = 100 tokens = exactly the burst capacity.
        assert asyncio.run(run()) < 0.05

    def test_throttles_beyond_capacity(self) -> None:
        async def run() -> float:
            bucket = Bucket(rate_per_sec=100, capacity=100)
            start = time.monotonic()
            for _ in range(15):
                await bucket.acquire(DEFAULT_TOKEN_COST)
            return time.monotonic() - start

        # The extra 50 tokens must be waited out at 100 tokens/sec.
        elapsed = asyncio.run(run())
        assert 0.3 < elapsed < 1.0

    def test_refills_over_time(self) -> None:
        async def run() -> float:
            bucket = Bucket(rate_per_sec=1000, capacity=1000)
            await bucket.acquire(1000)
            await asyncio.sleep(0.1)
            return bucket.tokens

        assert asyncio.run(run()) == pytest.approx(100, abs=40)

    def test_impossible_cost_is_rejected_not_hung(self) -> None:
        """A request larger than the bucket would wait forever; fail instead."""

        async def run() -> None:
            await Bucket(rate_per_sec=100, capacity=100).acquire(500)

        with pytest.raises(ValueError, match="could never be satisfied"):
            asyncio.run(run())

    def test_penalize_forces_a_wait(self) -> None:
        async def run() -> float:
            bucket = Bucket(rate_per_sec=100, capacity=100)
            bucket.penalize(0.2)
            start = time.monotonic()
            await bucket.acquire(10)
            return time.monotonic() - start

        assert asyncio.run(run()) > 0.15

    def test_rejects_non_positive_rate(self) -> None:
        with pytest.raises(ValueError):
            Bucket(rate_per_sec=0, capacity=10)


# ---------------------------------------------------------------------------
# Request signing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def key_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("secrets") / "test_key.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path


class TestSigner:
    def test_produces_all_three_headers(self, key_file: Path) -> None:
        headers = KalshiSigner("key-id", key_file).headers("GET", "/trade-api/v2/markets")
        assert set(headers) == {
            "KALSHI-ACCESS-KEY",
            "KALSHI-ACCESS-TIMESTAMP",
            "KALSHI-ACCESS-SIGNATURE",
        }

    def test_timestamp_is_milliseconds(self, key_file: Path) -> None:
        """Seconds instead of milliseconds is the classic auth failure."""
        headers = KalshiSigner("k", key_file).headers("GET", "/trade-api/v2/markets")
        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        now_ms = time.time() * 1000
        assert abs(ts - now_ms) < 5000
        assert ts > 1_000_000_000_000  # unmistakably milliseconds

    def test_signature_verifies_against_public_key(self, key_file: Path) -> None:
        signer = KalshiSigner("k", key_file)
        ts = 1703123456789
        signature = signer.sign("GET", "/trade-api/v2/portfolio/balance", ts)

        private = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        message = f"{ts}GET/trade-api/v2/portfolio/balance".encode()

        # Raises on mismatch.
        private.public_key().verify(
            base64.b64decode(signature),
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_query_string_is_excluded_from_signature(self, key_file: Path) -> None:
        """Docs are explicit: sign the path without query parameters."""
        signer = KalshiSigner("k", key_file)
        ts = 1703123456789
        bare = signer.sign("GET", "/trade-api/v2/markets", ts)
        with_query = signer.sign("GET", "/trade-api/v2/markets?limit=5&cursor=abc", ts)
        # PSS is randomised, so compare what was signed, not the bytes.
        private = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        for sig in (bare, with_query):
            private.public_key().verify(
                base64.b64decode(sig),
                b"1703123456789GET/trade-api/v2/markets",
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

    def test_full_url_signs_only_the_path(self, key_file: Path) -> None:
        signer = KalshiSigner("k", key_file)
        ts = 1703123456789
        sig = signer.sign(
            "GET", "https://external-api.kalshi.com/trade-api/v2/markets?limit=5", ts
        )
        private = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        private.public_key().verify(
            base64.b64decode(sig),
            b"1703123456789GET/trade-api/v2/markets",
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_method_is_uppercased(self, key_file: Path) -> None:
        signer = KalshiSigner("k", key_file)
        ts = 1
        assert signer.sign("get", "/x", ts) != signer.sign("post", "/x", ts)

    def test_missing_key_id_is_rejected(self, key_file: Path) -> None:
        with pytest.raises(SigningError, match="No Kalshi API key ID"):
            KalshiSigner("", key_file)

    def test_missing_key_file_gives_actionable_error(self, tmp_path: Path) -> None:
        with pytest.raises(SigningError, match="openssl genrsa"):
            KalshiSigner("k", tmp_path / "nope.pem")

    def test_repr_does_not_leak_key_material(self, key_file: Path) -> None:
        text = repr(KalshiSigner("supersecretkeyid", key_file))
        assert "supersecretkeyid" not in text
        assert "PRIVATE" not in text


# ---------------------------------------------------------------------------
# Candle aggregation
# ---------------------------------------------------------------------------


class TestCandleBuilder:
    @staticmethod
    def at(minute: int, second: int = 0) -> datetime:
        return datetime(2026, 7, 25, 12, minute, second, tzinfo=UTC)

    def test_single_trade_forms_a_candle(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("T", self.at(0), Decimal("0.50"), Decimal("10"))
        candles = b.take_all()
        assert len(candles) == 1
        c = candles[0]
        assert c["open"] == c["high"] == c["low"] == c["close"] == Decimal("0.50")
        assert c["volume"] == Decimal("10")

    def test_ohlc_tracks_extremes_in_order(self) -> None:
        b = CandleBuilder(60)
        for price in ("0.50", "0.60", "0.40", "0.55"):
            b.add_trade("T", self.at(0), Decimal(price), Decimal("1"))
        c = b.take_all()[0]
        assert c["open"] == Decimal("0.50")
        assert c["high"] == Decimal("0.60")
        assert c["low"] == Decimal("0.40")
        assert c["close"] == Decimal("0.55")
        assert c["trades"] == 4

    def test_volume_accumulates_fractional_contracts(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("T", self.at(0), Decimal("0.5"), Decimal("2.50"))
        b.add_trade("T", self.at(0), Decimal("0.5"), Decimal("0.25"))
        assert b.take_all()[0]["volume"] == Decimal("2.75")

    def test_trades_bucket_by_minute(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("T", self.at(0, 5), Decimal("0.5"), Decimal("1"))
        b.add_trade("T", self.at(0, 59), Decimal("0.6"), Decimal("1"))
        b.add_trade("T", self.at(1, 0), Decimal("0.7"), Decimal("1"))
        assert len(b.take_all()) == 2

    def test_markets_bucket_separately(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("A", self.at(0), Decimal("0.5"), Decimal("1"))
        b.add_trade("B", self.at(0), Decimal("0.5"), Decimal("1"))
        assert len(b.take_all()) == 2

    def test_take_closed_leaves_the_current_bucket_open(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("T", self.at(0), Decimal("0.5"), Decimal("1"))
        b.add_trade("T", self.at(5), Decimal("0.6"), Decimal("1"))

        closed = b.take_closed(now=self.at(5, 30))
        assert len(closed) == 1
        assert closed[0]["close"] == Decimal("0.5")

        # The in-progress bucket survives for later trades.
        remaining = b.take_all()
        assert len(remaining) == 1
        assert remaining[0]["close"] == Decimal("0.6")

    def test_take_closed_is_idempotent(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("T", self.at(0), Decimal("0.5"), Decimal("1"))
        assert len(b.take_closed(now=self.at(5))) == 1
        assert b.take_closed(now=self.at(5)) == []

    def test_bucket_timestamp_is_period_aligned(self) -> None:
        b = CandleBuilder(60)
        b.add_trade("T", self.at(3, 47), Decimal("0.5"), Decimal("1"))
        assert b.take_all()[0]["ts"] == self.at(3, 0)


# ---------------------------------------------------------------------------
# Adaptive rate control (AIMD)
#
# Regression tests for a real bug: the original limiter slept after a 429 but
# left its refill rate unchanged, so it re-offended on the very next request.
# A live sync against the demo API produced 304 rate-limit responses and made
# almost no progress. The rate itself has to adapt.
# ---------------------------------------------------------------------------


class TestAdaptiveRate:
    def test_429_halves_the_sustained_rate(self) -> None:
        bucket = Bucket(rate_per_sec=200, capacity=200)
        assert bucket.rate == 200
        bucket.penalize(1.0)
        assert bucket.rate == 100

    def test_repeated_429s_keep_decreasing(self) -> None:
        bucket = Bucket(rate_per_sec=200, capacity=200)
        for _ in range(4):
            bucket.penalize(1.0)
        assert bucket.rate == pytest.approx(12.5)

    def test_rate_never_falls_to_zero(self) -> None:
        """A floor keeps the client alive rather than wedged forever."""
        bucket = Bucket(rate_per_sec=200, capacity=200)
        for _ in range(50):
            bucket.penalize(1.0)
        assert bucket.rate > 0

    def test_successes_restore_the_rate(self) -> None:
        bucket = Bucket(rate_per_sec=200, capacity=200)
        bucket.penalize(1.0)
        throttled = bucket.rate

        for _ in range(Bucket.SUCCESSES_PER_INCREASE):
            bucket.record_success()
        assert bucket.rate > throttled

    def test_recovery_never_exceeds_the_configured_ceiling(self) -> None:
        bucket = Bucket(rate_per_sec=200, capacity=200)
        bucket.penalize(1.0)
        for _ in range(10_000):
            bucket.record_success()
        assert bucket.rate == 200

    def test_success_before_any_throttle_is_a_noop(self) -> None:
        bucket = Bucket(rate_per_sec=200, capacity=200)
        for _ in range(100):
            bucket.record_success()
        assert bucket.rate == 200

    def test_a_single_success_does_not_undo_a_throttle(self) -> None:
        """Recovery is deliberately slower than the decrease."""
        bucket = Bucket(rate_per_sec=200, capacity=200)
        bucket.penalize(1.0)
        bucket.record_success()
        assert bucket.rate == 100

    def test_throttle_events_are_counted(self) -> None:
        bucket = Bucket(rate_per_sec=200, capacity=200)
        bucket.penalize(1.0)
        bucket.penalize(1.0)
        assert bucket.throttle_events == 2

    def test_throttling_reads_leaves_writes_alone(self) -> None:
        """Losing read budget must never slow down order placement."""
        limiter = RateLimiter("basic")
        limiter.penalize(is_write=False, seconds=1.0)
        assert limiter.read.rate == 100
        assert limiter.write.rate == 100  # unchanged ceiling for basic writes

    def test_throttled_bucket_actually_slows_acquisition(self) -> None:
        """The point of the fix: a lower rate must mean longer waits."""

        async def run() -> float:
            bucket = Bucket(rate_per_sec=200, capacity=200)
            for _ in range(5):
                bucket.penalize(0)  # rate -> 6.25/s, tokens drained to 0
            start = time.monotonic()
            await bucket.acquire(10)
            return time.monotonic() - start

        # At 6.25 tokens/sec, 10 tokens takes ~1.6s.
        assert asyncio.run(run()) > 1.0

    def test_describe_reports_current_state(self) -> None:
        limiter = RateLimiter("basic")
        limiter.penalize(is_write=False, seconds=1.0)
        text = limiter.describe()
        assert "tier=basic" in text
        assert "throttles=1" in text

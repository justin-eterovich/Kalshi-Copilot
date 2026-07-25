"""Client-side token buckets sized from Kalshi's documented rate limits.

Per docs.kalshi.com, limits are **token buckets** with separate read and write
budgets. Most requests cost the default of 10 tokens. Basic tier gets 200
read tokens/sec and 100 write tokens/sec — roughly 20 reads and 10 writes per
second.

Burst capacity matters and is asymmetric: write buckets above Basic hold two
seconds of budget, while Basic write buckets and all read buckets hold only
one second. We model that exactly rather than assuming a uniform burst.

A 429 carries no ``Retry-After`` and no ``X-RateLimit-*`` headers, so the
client cannot learn its budget from responses — staying under the limit is
entirely our responsibility. Live values are available from
``GET /account/limits`` and ``GET /account/endpoint_costs``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final

__all__ = ["Bucket", "RateLimiter", "TIERS", "DEFAULT_TOKEN_COST"]

DEFAULT_TOKEN_COST: Final = 10


@dataclass(frozen=True, slots=True)
class TierLimits:
    read_per_sec: int
    write_per_sec: int
    #: Seconds of budget the write bucket may accumulate.
    write_burst_sec: float = 2.0
    #: Read buckets hold one second of budget at every tier.
    read_burst_sec: float = 1.0


#: Documented tier table. Basic is the default assumption.
TIERS: Final[dict[str, TierLimits]] = {
    "basic": TierLimits(200, 100, write_burst_sec=1.0),
    "advanced": TierLimits(300, 300),
    "expert": TierLimits(600, 600),
    "premier": TierLimits(1000, 1000),
    "paragon": TierLimits(2000, 2000),
    "prime": TierLimits(4000, 4000),
    "prestige": TierLimits(6000, 8000),
}


class Bucket:
    """A refilling token bucket with AIMD congestion control.

    The documented tier limits describe *authenticated* accounts, and some
    endpoints cost more than the default 10 tokens — neither of which the
    client can discover up front (a 429 carries no ``Retry-After`` and no
    ``X-RateLimit-*`` headers). A bucket that only sleeps after a 429 keeps
    its refill rate unchanged, so it re-offends on the very next request and
    every request costs a rejection.

    So the rate itself adapts: halve it on a 429 (multiplicative decrease),
    then creep back toward the configured ceiling while requests succeed
    (additive increase). It settles just under the true allowed rate with
    very few rejections, which is what keeps an API key in good standing.
    """

    #: Never throttle below this fraction of the configured rate.
    MIN_RATE_FRACTION: Final = 0.02
    #: Multiplier applied on each 429.
    DECREASE_FACTOR: Final = 0.5
    #: Fraction of the ceiling added back per recovery step.
    INCREASE_FRACTION: Final = 0.05
    #: Consecutive successes before the rate creeps up.
    SUCCESSES_PER_INCREASE: Final = 20

    def __init__(self, rate_per_sec: float, capacity: float) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._max_rate = rate_per_sec
        self._rate = rate_per_sec
        self._min_rate = max(rate_per_sec * self.MIN_RATE_FRACTION, 0.5)
        self._capacity = max(capacity, rate_per_sec * 0.1)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

        self._successes = 0
        self.throttle_events = 0

    @property
    def rate(self) -> float:
        """Current effective refill rate, after any throttling."""
        return self._rate

    @property
    def max_rate(self) -> float:
        return self._max_rate

    @property
    def tokens(self) -> float:
        """Current token count, refilled to now (for tests and telemetry)."""
        return min(
            self._capacity,
            self._tokens + (time.monotonic() - self._updated) * self._rate,
        )

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(
            self._capacity, self._tokens + (now - self._updated) * self._rate
        )
        self._updated = now

    async def acquire(self, cost: float = DEFAULT_TOKEN_COST) -> float:
        """Wait until ``cost`` tokens are available, then consume them.

        Returns the seconds spent waiting, which callers log to spot when the
        client is the bottleneck.
        """
        if cost > self._capacity:
            raise ValueError(
                f"a single request costs {cost} tokens but the bucket holds at "
                f"most {self._capacity:.0f} — it could never be satisfied"
            )

        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return waited
                deficit = cost - self._tokens
                delay = deficit / self._rate

            await asyncio.sleep(delay)
            waited += delay

    def penalize(self, seconds: float) -> None:
        """React to a 429: drain the bucket *and* halve the sustained rate."""
        self._refill()
        self._rate = max(self._min_rate, self._rate * self.DECREASE_FACTOR)
        self._tokens = min(self._tokens, -abs(seconds) * self._rate)
        self._successes = 0
        self.throttle_events += 1

    def record_success(self) -> None:
        """Creep the rate back up after a run of clean responses."""
        if self._rate >= self._max_rate:
            return

        self._successes += 1
        if self._successes < self.SUCCESSES_PER_INCREASE:
            return

        self._successes = 0
        self._refill()
        self._rate = min(
            self._max_rate, self._rate + self._max_rate * self.INCREASE_FRACTION
        )


class RateLimiter:
    """Separate read and write budgets, as the API enforces them."""

    def __init__(self, tier: str = "basic") -> None:
        key = (tier or "basic").strip().lower()
        limits = TIERS.get(key)
        if limits is None:
            raise ValueError(
                f"unknown rate tier {tier!r}; expected one of {sorted(TIERS)}"
            )
        self.tier = key
        self._limits = limits
        self.read = Bucket(
            limits.read_per_sec, limits.read_per_sec * limits.read_burst_sec
        )
        self.write = Bucket(
            limits.write_per_sec, limits.write_per_sec * limits.write_burst_sec
        )

    @property
    def reads_per_sec(self) -> float:
        """Approximate request rate at the default token cost."""
        return self._limits.read_per_sec / DEFAULT_TOKEN_COST

    @property
    def writes_per_sec(self) -> float:
        return self._limits.write_per_sec / DEFAULT_TOKEN_COST

    async def acquire(self, is_write: bool, cost: int = DEFAULT_TOKEN_COST) -> float:
        bucket = self.write if is_write else self.read
        return await bucket.acquire(cost)

    def penalize(self, is_write: bool, seconds: float) -> None:
        (self.write if is_write else self.read).penalize(seconds)

    def record_success(self, is_write: bool) -> None:
        (self.write if is_write else self.read).record_success()

    def describe(self) -> str:
        """One-line state for logs: where the adaptive rate has settled."""
        return (
            f"tier={self.tier} "
            f"read={self.read.rate:.0f}/{self.read.max_rate:.0f} tok/s "
            f"write={self.write.rate:.0f}/{self.write.max_rate:.0f} tok/s "
            f"throttles={self.read.throttle_events + self.write.throttle_events}"
        )

"""Redis client and the channel names used for UI fan-out."""

from __future__ import annotations

from typing import Final

import redis.asyncio as redis

from app.settings import get_settings

# Channels the API relays to browser WebSocket clients.
CH_SIGNALS: Final = "copilot:signals"
CH_PROPOSALS: Final = "copilot:proposals"
CH_TICKS: Final = "copilot:ticks"
CH_ORDERS: Final = "copilot:orders"
CH_SYSTEM: Final = "copilot:system"

# Per-service heartbeat keys, written by ingest/worker and read by healthchecks.
HEARTBEAT_KEY: Final = "copilot:heartbeat:{service}"
HEARTBEAT_TTL_SEC: Final = 45

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(
            get_settings().redis_url,
            decode_responses=True,
            health_check_interval=30,
        )
    return _client


async def beat(service: str) -> None:
    """Record a liveness heartbeat for ``service``."""
    await get_redis().set(
        HEARTBEAT_KEY.format(service=service), "1", ex=HEARTBEAT_TTL_SEC
    )


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None

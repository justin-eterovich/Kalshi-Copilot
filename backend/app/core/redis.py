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

#: The runtime kill switch.
#:
#: It lives in Redis rather than ``config.yaml`` because it has to be
#: engageable *on a running system* and visible to every process at once.
#: ``get_config()`` is ``@lru_cache(maxsize=1)``, so a config-file switch is
#: read once per process and never again — and the switch's two halves run in
#: different processes (``api`` refuses approvals, ``worker`` cancels resting
#: orders). Engaging it by editing the file and restarting one container left
#: resting orders live while the dashboard read "engaged", which is worse than
#: having no switch at all.
#:
#: No TTL: an emergency stop must not time out and quietly re-arm trading.
KILL_SWITCH_KEY: Final = "copilot:kill_switch"


async def get_kill_switch() -> bool:
    """Whether the runtime kill switch is engaged.

    Fails **closed**: if Redis cannot be reached we cannot prove the operator
    has not hit the switch, and the safe reading of "unknown" on an emergency
    stop is "engaged".
    """
    try:
        return bool(await get_redis().get(KILL_SWITCH_KEY))
    except Exception:
        return True


async def set_kill_switch(engaged: bool) -> None:
    """Engage or release the runtime kill switch."""
    client = get_redis()
    if engaged:
        await client.set(KILL_SWITCH_KEY, "1")
    else:
        await client.delete(KILL_SWITCH_KEY)

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

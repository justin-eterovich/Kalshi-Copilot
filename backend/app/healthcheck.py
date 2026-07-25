"""Container healthcheck probe.

Usage (from docker-compose healthchecks)::

    python -m app.healthcheck --http                # api
    python -m app.healthcheck --heartbeat ingest    # background services

Exits 0 when healthy, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

HTTP_TIMEOUT = 4.0


async def _check_http() -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get("http://127.0.0.1:8080/api/health")
            return resp.status_code == 200
    except Exception:
        return False


async def _check_heartbeat(service: str) -> bool:
    from app.core.redis import HEARTBEAT_KEY, close_redis, get_redis

    try:
        value = await get_redis().get(HEARTBEAT_KEY.format(service=service))
        return value is not None
    except Exception:
        return False
    finally:
        await close_redis()


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--heartbeat", metavar="SERVICE")
    args = parser.parse_args()

    if args.http:
        return 0 if await _check_http() else 1
    if args.heartbeat:
        return 0 if await _check_heartbeat(args.heartbeat) else 1

    parser.error("specify --http or --heartbeat SERVICE")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))

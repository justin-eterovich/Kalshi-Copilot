"""Browser WebSocket: relays Redis pub/sub to the dashboard.

One Redis subscription is shared across all connected browsers rather than
opened per client, so ten open tabs cost one subscription.

Clients filter with ``{"action": "watch", "tickers": [...]}``. Tick traffic
covers every market in the scanner universe, and a market page wants one of
them — pushing all of it to every tab would waste the LAN and the browser's
main thread.

A slow client is dropped rather than allowed to apply backpressure to the
shared reader: one stalled tab must never delay ticks for the others.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.core.redis import (
    CH_ORDERS,
    CH_PROPOSALS,
    CH_SIGNALS,
    CH_SYSTEM,
    CH_TICKS,
    get_redis,
)

log = get_logger(__name__)

router = APIRouter()

CHANNELS = (CH_TICKS, CH_SIGNALS, CH_PROPOSALS, CH_ORDERS, CH_SYSTEM)
#: Per-client queue depth before we consider the client too slow to keep.
CLIENT_QUEUE_MAX = 256


class Client:
    """One connected browser."""

    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        #: Empty means "everything" — used by the global feed panels.
        self.tickers: set[str] = set()
        self.dropped = 0

    def wants(self, channel: str, payload: dict[str, Any]) -> bool:
        # Signals, proposals, orders and system events bypass the filter: an
        # approval request or a fill must reach the operator regardless of
        # which page happens to be open.
        if channel != CH_TICKS or not self.tickers:
            return True
        return payload.get("ticker") in self.tickers

    def offer(self, message: str) -> None:
        """Non-blocking send. Drops on overflow instead of stalling the hub."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.dropped += 1


class Hub:
    """Fan-out from a single Redis subscription to every connected client."""

    def __init__(self) -> None:
        self._clients: set[Client] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def register(self, client: Client) -> None:
        async with self._lock:
            self._clients.add(client)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._pump(), name="ws-hub")

    async def unregister(self, client: Client) -> None:
        async with self._lock:
            self._clients.discard(client)
            if not self._clients and self._task is not None:
                self._task.cancel()
                self._task = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def _pump(self) -> None:
        """Read Redis once, fan out to everyone who wants it."""
        redis = get_redis()
        pubsub = redis.pubsub(ignore_subscribe_messages=True)

        try:
            await pubsub.subscribe(*CHANNELS)
            log.info("ws hub subscribed to %d channels", len(CHANNELS))

            async for message in pubsub.listen():
                if message is None or message.get("type") != "message":
                    continue

                channel = str(message.get("channel"))
                raw = message.get("data")
                if not isinstance(raw, str):
                    continue

                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                envelope = json.dumps({"channel": channel, "data": payload})
                for client in list(self._clients):
                    if client.wants(channel, payload):
                        client.offer(envelope)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - hub must not die silently
            log.exception("ws hub failed: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


hub = Hub()


@router.websocket("/ws")
async def websocket_endpoint(socket: WebSocket) -> None:
    await socket.accept()
    client = Client(socket)
    await hub.register(client)

    sender = asyncio.create_task(_send_loop(client))

    try:
        await socket.send_text(
            json.dumps({"channel": "__hello__", "data": {"channels": list(CHANNELS)}})
        )

        while True:
            raw = await socket.receive_text()
            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if command.get("action") == "watch":
                tickers = command.get("tickers") or []
                client.tickers = {str(t) for t in tickers if t}
                await socket.send_text(
                    json.dumps(
                        {
                            "channel": "__watching__",
                            "data": {"tickers": sorted(client.tickers)},
                        }
                    )
                )

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("ws client error: %s", exc)
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        await hub.unregister(client)


async def _send_loop(client: Client) -> None:
    """Drain this client's queue to its socket."""
    try:
        while True:
            message = await client.queue.get()
            await client.socket.send_text(message)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - disconnects are routine
        return

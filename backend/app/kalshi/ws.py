"""Kalshi WebSocket client.

Protocol notes from the AsyncAPI spec:

- **The connection itself requires authentication**, even for channels that
  carry only public market data. Sign ``{ts}GET/trade-api/ws/v2``.
- Subscribe with
  ``{"id": N, "cmd": "subscribe",
  "params": {"channels": [...], "market_tickers": [...]}}``.
- Every server message is ``{"type": ..., "sid": ..., "seq": ..., "msg": {...}}``.
  ``seq`` is per-subscription, so gap tracking is per ``sid``.
- The server sends a Ping frame every 10 seconds; if pings stop the
  connection is dead. The ``websockets`` library answers pings automatically
  and its own keepalive timeout surfaces a dead link as a clean exception.
- Error codes 10, 17 and 25 are **terminal** — the subscription is gone and
  must be re-established rather than waited on.

Reconnects use exponential backoff with jitter so that a Kalshi-side blip
does not turn every client into a synchronised thundering herd. Resubscribing
is idempotent: the desired subscription set is declarative state, replayed
after each reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import websockets
from websockets.asyncio.client import ClientConnection

from app.core.logging import get_logger
from app.kalshi.auth import KalshiSigner

log = get_logger(__name__)

__all__ = ["KalshiWebSocket", "Subscription", "TERMINAL_ERROR_CODES"]

#: Errors after which the subscription is gone and must be recreated.
TERMINAL_ERROR_CODES: Final = frozenset({10, 17, 25})

MAX_BACKOFF_SEC: Final = 60.0
#: Server pings every ~10s; allow generous slack before declaring death.
PING_TIMEOUT_SEC: Final = 30.0


@dataclass(frozen=True, slots=True)
class Subscription:
    """A declarative subscription request, replayed after every reconnect."""

    channels: tuple[str, ...]
    market_tickers: tuple[str, ...] = ()

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"channels": list(self.channels)}
        if self.market_tickers:
            params["market_tickers"] = list(self.market_tickers)
        return params


@dataclass
class _SidState:
    """Per-subscription sequence tracking."""

    channel: str | None = None
    last_seq: int | None = None
    gaps: int = 0


@dataclass
class KalshiWebSocket:
    """Self-healing WebSocket consumer.

    Iterate :meth:`stream` to receive decoded messages. Reconnects,
    resubscribes, and sequence-gap detection are handled internally; consumers
    see a continuous message stream plus explicit ``__resync__`` markers when
    state must be rebuilt.
    """

    url: str
    signer: KalshiSigner
    subscriptions: list[Subscription] = field(default_factory=list)

    _next_id: int = 1
    _sids: dict[int, _SidState] = field(default_factory=dict)
    _connection: ClientConnection | None = None

    # -- public API ------------------------------------------------------

    def subscribe(self, channels: list[str], tickers: list[str] | None = None) -> None:
        """Register a subscription. Takes effect on the next (re)connect."""
        self.subscriptions.append(
            Subscription(tuple(channels), tuple(tickers or ()))
        )

    async def stream(
        self, stop: asyncio.Event | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield messages forever, reconnecting as needed.

        Yields a synthetic ``{"type": "__resync__"}`` message whenever local
        state must be discarded — on reconnect, and on any sequence gap.
        """
        attempt = 0
        stop = stop or asyncio.Event()

        while not stop.is_set():
            try:
                async with self._connect() as conn:
                    self._connection = conn
                    attempt = 0
                    self._sids.clear()
                    await self._send_subscriptions(conn)

                    # Local state is meaningless across a reconnect.
                    yield {"type": "__resync__", "reason": "connected"}

                    async for raw in conn:
                        if stop.is_set():
                            break
                        for message in self._handle_raw(raw):
                            yield message

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                if stop.is_set():
                    break
                delay = self._backoff(attempt)
                attempt += 1
                log.warning(
                    "websocket disconnected (%s: %s); reconnecting in %.1fs",
                    type(exc).__name__,
                    exc,
                    delay,
                )
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
            finally:
                self._connection = None

    # -- connection ------------------------------------------------------

    def _connect(self) -> Any:
        headers = self.signer.ws_headers(self.url)
        return websockets.connect(
            self.url,
            additional_headers=headers,
            # The library answers server pings automatically; these bound how
            # long we wait before calling a silent connection dead.
            ping_interval=20,
            ping_timeout=PING_TIMEOUT_SEC,
            close_timeout=5,
            max_queue=4096,
        )

    async def _send_subscriptions(self, conn: ClientConnection) -> None:
        """Replay the declarative subscription set. Idempotent by construction."""
        for sub in self.subscriptions:
            command = {
                "id": self._next_id,
                "cmd": "subscribe",
                "params": sub.to_params(),
            }
            self._next_id += 1
            await conn.send(json.dumps(command))
            log.info(
                "subscribed: channels=%s markets=%s",
                ",".join(sub.channels),
                len(sub.market_tickers) or "all",
            )

    @staticmethod
    def _backoff(attempt: int) -> float:
        base = min(2.0**attempt, MAX_BACKOFF_SEC)
        return base * (0.5 + random.random() * 0.5)

    # -- message handling ------------------------------------------------

    def _handle_raw(self, raw: str | bytes) -> list[dict[str, Any]]:
        """Decode one frame into zero or more messages for the consumer."""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("dropping non-JSON websocket frame")
            return []

        msg_type = message.get("type")

        if msg_type == "error":
            return self._handle_error(message)

        if msg_type in ("subscribed", "unsubscribed", "ok"):
            sid = (message.get("msg") or {}).get("sid") or message.get("sid")
            channel = (message.get("msg") or {}).get("channel")
            if sid is not None:
                self._sids[int(sid)] = _SidState(channel=channel)
            log.debug("control frame: %s sid=%s channel=%s", msg_type, sid, channel)
            return []

        out: list[dict[str, Any]] = []
        sid = message.get("sid")
        seq = message.get("seq")

        if sid is not None and seq is not None:
            gap = self._check_seq(int(sid), int(seq))
            if gap is not None:
                out.append(
                    {
                        "type": "__resync__",
                        "reason": gap,
                        "sid": sid,
                        "channel": self._sids.get(int(sid), _SidState()).channel,
                    }
                )

        out.append(message)
        return out

    def _check_seq(self, sid: int, seq: int) -> str | None:
        """Track per-sid sequence numbers. Returns a reason string on a gap."""
        state = self._sids.setdefault(sid, _SidState())

        if state.last_seq is None:
            state.last_seq = seq
            return None

        expected = state.last_seq + 1
        if seq == expected:
            state.last_seq = seq
            return None

        if seq <= state.last_seq:
            # Duplicate or replay: ignore, do not treat as a gap.
            return None

        state.gaps += 1
        state.last_seq = seq
        reason = f"sequence gap on sid {sid}: expected {expected}, got {seq}"
        log.warning("%s — local state for this subscription is now untrusted", reason)
        return reason

    def _handle_error(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        body = message.get("msg") or {}
        code = body.get("code")
        text = body.get("msg") or body.get("message") or ""

        if code in TERMINAL_ERROR_CODES:
            log.error(
                "terminal websocket error %s (%s) — resubscribe required", code, text
            )
            return [{"type": "__resync__", "reason": f"terminal error {code}: {text}"}]

        log.warning("websocket error %s: %s", code, text)
        return []


async def run_forever(
    ws: KalshiWebSocket,
    handler: Callable[[dict[str, Any]], Any],
    stop: asyncio.Event | None = None,
) -> None:
    """Convenience driver: pump every message into ``handler``."""
    async for message in ws.stream(stop):
        result = handler(message)
        if asyncio.iscoroutine(result):
            await result

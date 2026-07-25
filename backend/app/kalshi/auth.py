"""Kalshi request signing (RSA-PSS over SHA-256).

Per docs.kalshi.com, every authenticated request carries three headers::

    KALSHI-ACCESS-KEY        the API key ID (a UUID)
    KALSHI-ACCESS-TIMESTAMP  current time in MILLISECONDS
    KALSHI-ACCESS-SIGNATURE  base64(RSA-PSS-SHA256(timestamp + METHOD + path))

Three details cause almost all signing failures:

1. The timestamp is **milliseconds**, not seconds.
2. The signed path **excludes the query string** — sign
   ``/trade-api/v2/markets`` even when requesting ``/markets?limit=5``.
3. The host clock must be NTP-synced; signatures are timestamp-sensitive and
   a skewed clock looks exactly like a bad credential.

The WebSocket handshake signs ``{timestamp}GET/trade-api/ws/v2`` — no query
string, no body.

The private key is loaded once and never logged. ``app.core.logging`` also
strips anything key-shaped from log records as a second line of defence.
"""

from __future__ import annotations

import base64
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

__all__ = ["KalshiSigner", "SigningError", "load_private_key"]


class SigningError(RuntimeError):
    """The private key could not be loaded or used."""


@lru_cache(maxsize=4)
def load_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    """Load an unencrypted PEM RSA private key from disk."""
    key_path = Path(path)
    if not key_path.is_file():
        raise SigningError(
            f"Kalshi private key not found at {key_path}. Generate one with "
            f"`openssl genrsa -out secrets/kalshi_demo_key.pem 2048`, upload the "
            f"public half in Kalshi's API Keys settings, and mount it read-only."
        )

    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001 - message must not leak key bytes
        raise SigningError(
            f"Could not read the private key at {key_path}: {type(exc).__name__}. "
            f"It must be an unencrypted PEM RSA key."
        ) from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise SigningError(
            f"{key_path} is a {type(key).__name__}, but Kalshi requires RSA."
        )
    return key


def _sign_path(url_or_path: str) -> str:
    """Return the path to sign: no scheme, no host, no query string."""
    parts = urlsplit(url_or_path)
    return parts.path


class KalshiSigner:
    """Produces the three auth headers for REST and WebSocket requests."""

    def __init__(self, key_id: str, private_key_path: str | Path) -> None:
        if not key_id:
            raise SigningError(
                "No Kalshi API key ID configured for the active environment. "
                "Set KALSHI_DEMO_KEY_ID (or KALSHI_PROD_KEY_ID) in .env."
            )
        self._key_id = key_id
        self._key = load_private_key(private_key_path)

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> str:
        """Return the base64 RSA-PSS signature for one request."""
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        message = f"{ts}{method.upper()}{_sign_path(url_or_path)}"

        signature = self._key.sign(
            message.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                # Kalshi expects a digest-length salt, not MAX_LENGTH.
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(
        self, method: str, url_or_path: str, timestamp_ms: int | None = None
    ) -> dict[str, str]:
        """Build the full auth header set for a request."""
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "KALSHI-ACCESS-SIGNATURE": self.sign(method, url_or_path, ts),
        }

    def ws_headers(self, ws_url: str) -> dict[str, str]:
        """Auth headers for the WebSocket handshake.

        The handshake signs the WS path with the GET method, e.g.
        ``{ts}GET/trade-api/ws/v2``.
        """
        return self.headers("GET", ws_url)

    def __repr__(self) -> str:  # pragma: no cover - never leak key material
        return f"KalshiSigner(key_id={self._key_id[:8]}…)"

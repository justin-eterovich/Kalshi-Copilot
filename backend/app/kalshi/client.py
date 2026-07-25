"""Factory helpers that wire settings into Kalshi clients."""

from __future__ import annotations

from app.core.logging import get_logger
from app.kalshi.auth import KalshiSigner, SigningError
from app.kalshi.ratelimit import RateLimiter
from app.kalshi.rest import KalshiRestClient
from app.kalshi.ws import KalshiWebSocket
from app.settings import Settings, get_settings

log = get_logger(__name__)

__all__ = ["build_signer", "build_rest_client", "build_websocket"]


def build_signer(settings: Settings | None = None) -> KalshiSigner | None:
    """Build a signer for the active environment, or None if unconfigured.

    Returning None is not an error: public market data works unauthenticated
    over REST, so the catalog and screener still function without credentials.
    The WebSocket does require auth, and callers check for that explicitly.
    """
    settings = settings or get_settings()

    if not settings.credentials_present():
        log.warning(
            "no Kalshi credentials for env=%s — REST public data only, "
            "no websocket and no portfolio access",
            settings.kalshi_env.value,
        )
        return None

    try:
        return KalshiSigner(settings.key_id, settings.private_key_path)
    except SigningError as exc:
        log.error("could not build Kalshi signer: %s", exc)
        return None


def build_rest_client(settings: Settings | None = None) -> KalshiRestClient:
    settings = settings or get_settings()
    return KalshiRestClient(
        base_url=settings.rest_url,
        signer=build_signer(settings),
        rate_limiter=RateLimiter(settings.kalshi_rate_tier),
    )


def build_websocket(settings: Settings | None = None) -> KalshiWebSocket | None:
    """Build a WebSocket client, or None when credentials are missing.

    Unlike REST, the Kalshi WebSocket requires authentication to establish the
    connection at all — even for public market-data channels.
    """
    settings = settings or get_settings()
    signer = build_signer(settings)
    if signer is None:
        return None
    return KalshiWebSocket(url=settings.ws_url, signer=signer)

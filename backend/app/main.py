"""FastAPI application: JSON API plus the built React dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import health, markets
from app.config import get_config
from app.core.fees import load_fee_schedule
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.db.base import dispose_engine
from app.db.bootstrap import init_db
from app.settings import get_settings

log = get_logger(__name__)

FRONTEND_DIR = Path("/app/frontend")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    log.info("kalshi-copilot api starting (env=%s)", settings.kalshi_env.value)
    await init_db()

    config = get_config()
    _log_safety_posture(settings, config)

    yield

    await close_redis()
    await dispose_engine()
    log.info("api stopped")


def _log_safety_posture(settings, config) -> None:
    """Make the operating mode impossible to misread in the logs."""
    if settings.live_trading_armed:
        log.warning(
            "LIVE TRADING ARMED (KALSHI_ENV=prod, LIVE_TRADING=true). "
            "Orders still require per-trade confirmation in the UI."
        )
    else:
        log.info(
            "safe mode: env=%s live_trading=%s -> no real orders possible",
            settings.kalshi_env.value,
            settings.live_trading,
        )

    if not settings.credentials_present():
        log.warning(
            "no usable credentials for env=%s (key id set: %s, key file: %s) — "
            "public market data will work, portfolio and orders will not",
            settings.kalshi_env.value,
            bool(settings.key_id),
            settings.private_key_path,
        )

    schedule = load_fee_schedule(settings.fee_schedule_path)
    if not schedule.is_verified:
        log.warning(
            "fee schedule has never been verified against the official PDF. "
            "Run `python scripts/refresh_fee_schedule.py`. Categories with an "
            "unknown multiplier are excluded from proposals (fail-closed)."
        )

    enabled = config.detectors.enabled_names()
    log.info("detectors enabled: %s", ", ".join(enabled) if enabled else "none")


app = FastAPI(
    title="kalshi-copilot",
    version="0.1.0",
    description="Self-hosted Kalshi analysis and trading copilot (human-in-the-loop).",
    lifespan=lifespan,
)

app.include_router(health.router, prefix="/api", tags=["system"])
app.include_router(markets.router, prefix="/api", tags=["catalog"])


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if (FRONTEND_DIR / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIR / "assets"),
        name="assets",
    )


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str) -> FileResponse:
    """Serve the SPA, letting client-side routing handle unknown paths."""
    candidate = FRONTEND_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)

    index = FRONTEND_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)

    raise RuntimeError(
        "Frontend build missing. Rebuild the image: docker compose up -d --build"
    )

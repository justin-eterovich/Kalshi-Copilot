"""Health and system-status endpoints.

``/api/health`` is the container probe: cheap and dependency-light.
``/api/system`` is what the dashboard shows — the safety posture of the whole
stack at a glance.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from app.config import get_config
from app.core.fees import UnverifiedFeeCategory, load_fee_schedule
from app.core.redis import HEARTBEAT_KEY, get_redis
from app.db.base import get_engine
from app.settings import get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + dependency reachability."""
    checks: dict[str, bool] = {}

    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = True
    except Exception:
        checks["db"] = False

    try:
        await get_redis().ping()
        checks["redis"] = True
    except Exception:
        checks["redis"] = False

    return {"status": "ok" if all(checks.values()) else "degraded", "checks": checks}


@router.get("/system")
async def system() -> dict[str, Any]:
    """Safety posture, service heartbeats, and fee-schedule provenance."""
    settings = get_settings()
    config = get_config()

    heartbeats: dict[str, bool] = {}
    for service in ("ingest", "worker"):
        try:
            value = await get_redis().get(HEARTBEAT_KEY.format(service=service))
            heartbeats[service] = value is not None
        except Exception:
            heartbeats[service] = False

    schedule = load_fee_schedule(settings.fee_schedule_path)
    unverified = [
        name
        for name, value in schedule.category_multipliers.items()
        if value is None
    ]

    return {
        "environment": settings.kalshi_env.value,
        "trading_mode": config.trading.mode,
        # Both interlocks. Even when armed, every order still needs per-trade
        # approval in the UI — there is no auto-trade path.
        "live_trading_armed": settings.live_trading_armed,
        "kill_switch": config.risk.kill_switch,
        "credentials_present": settings.credentials_present(),
        "enabled_detectors": config.detectors.enabled_names(),
        "heartbeats": heartbeats,
        "fees": {
            "verified_on": schedule.verified_on,
            "schedule_revision": schedule.schedule_revision,
            "base_taker_rate": float(schedule.base_taker_rate),
            "maker_rate_fraction": float(schedule.maker_rate_fraction),
            "unverified_categories": unverified,
        },
        "endpoints": {"rest": settings.rest_url, "ws": settings.ws_url},
    }


@router.get("/fees/quote")
async def fee_quote(
    price_cents: int,
    contracts: int,
    category: str = "default",
    is_taker: bool = True,
) -> dict[str, Any]:
    """Fee preview for the UI trade ticket.

    Exists so the frontend can never compute a fee itself and drift from the
    engine's numbers.
    """
    from app.core.fees import maker_fee_cents, taker_fee_cents

    schedule = load_fee_schedule(get_settings().fee_schedule_path)
    fn = taker_fee_cents if is_taker else maker_fee_cents

    try:
        fee = fn(price_cents, contracts, category, schedule)
    except UnverifiedFeeCategory as exc:
        return {
            "error": "unverified_fee_category",
            "category": exc.category,
            "detail": str(exc),
        }

    return {
        "fee_cents": fee,
        "fee_per_contract_cents": round(fee / contracts, 4) if contracts else 0,
        "category": category,
        "is_taker": is_taker,
    }

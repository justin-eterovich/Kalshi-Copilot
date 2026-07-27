"""Health and system-status endpoints.

``/api/health`` is the container probe: cheap and dependency-light.
``/api/system`` is what the dashboard shows — the safety posture of the whole
stack at a glance.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.config import get_config
from app.core.fees import (
    UnknownSeries,
    UnverifiedFeeSchedule,
    load_fee_schedule,
    series_of,
)
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
    # Multipliers are keyed by series and the table is complete (defaults plus
    # listed exceptions), so there is no per-category "unverified" list any
    # more. What matters is whether the table itself has been checked.
    fee_free = sorted(
        name for name, (mk, tk) in schedule.series.items() if mk == 0 and tk == 0
    )

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
            "base_maker_rate": float(schedule.base_maker_rate),
            "series_listed": len(schedule.series),
            "fee_free_series": fee_free,
            "default_is_safe": schedule.default_is_safe,
        },
        "endpoints": {"rest": settings.rest_url, "ws": settings.ws_url},
    }


@router.get("/fees/quote")
async def fee_quote(
    price_dollars: str,
    contracts: str,
    ticker: str | None = None,
    is_taker: bool = True,
) -> dict[str, Any]:
    """Fee preview for the UI trade ticket.

    Exists so the frontend can never compute a fee itself and drift from the
    engine's numbers.

    Args:
        price_dollars: Price as the API quotes it, e.g. ``0.5600``. Not cents.
        contracts: Contract count; fractional values down to 0.01 are valid.
        ticker: Market or series ticker. Fees are keyed by series; omit it to
            price at the default multiplier.
    """
    from decimal import Decimal

    from app.core.fees import maker_fee_cents, taker_fee_cents

    schedule = load_fee_schedule(get_settings().fee_schedule_path)
    fn = taker_fee_cents if is_taker else maker_fee_cents

    try:
        series = series_of(ticker)
        fee = fn(price_dollars, contracts, series, schedule)
    except (UnverifiedFeeSchedule, UnknownSeries) as exc:
        # Fail closed: excluded rather than priced with a guess.
        raise HTTPException(
            status_code=409,
            detail={
                "error": (
                    "unverified_fee_schedule"
                    if isinstance(exc, UnverifiedFeeSchedule)
                    else "unknown_series"
                ),
                "message": str(exc),
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    qty = Decimal(contracts)
    return {
        # Decimal cents to centicent precision — the exchange bills fractions.
        "fee_cents": str(fee),
        "fee_per_contract_cents": (str(round(fee / qty, 6)) if qty > 0 else "0"),
        "price_dollars": price_dollars,
        "contracts": contracts,
        "series": series,
        "is_taker": is_taker,
    }

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
from app.core.redis import HEARTBEAT_KEY, get_kill_switch, get_redis
from app.db.base import get_engine
from app.detectors.base import enabled_detector_names
from app.settings import get_settings
from app.trading import autonomy

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
        # Both interlocks for the *human* live path. Even when armed, an
        # operator-approved order still needs the ticker typed back.
        #
        # This says nothing about the machine: autonomous live trading needs
        # `autonomous_live_armed`, which is strictly stronger, plus the
        # evidence gate. See /api/autonomy for that side.
        "live_trading_armed": settings.live_trading_armed,
        "autonomous_live_armed": settings.autonomous_live_armed,
        # The effective switch, config floor OR the runtime flag in Redis.
        # Reporting only the config file made the header disagree with what
        # the executor would actually do.
        "kill_switch": await get_kill_switch() or config.risk.kill_switch,
        "kill_switch_config_floor": config.risk.kill_switch,
        "credentials_present": settings.credentials_present(),
        # Includes the weather engine, which is configured outside the
        # `detectors:` block and so was invisible here while it scanned.
        "enabled_detectors": enabled_detector_names(config),
        "heartbeats": heartbeats,
        "fees": {
            "verified_on": schedule.verified_on,
            "schedule_revision": schedule.schedule_revision,
            "base_taker_rate": float(schedule.base_taker_rate),
            "base_maker_rate": float(schedule.base_maker_rate),
            "series_listed": len(schedule.series),
            "fee_free_series": fee_free,
            "default_is_safe": schedule.default_is_safe,
            # Broken out because the two sides genuinely differ: listed maker
            # multipliers reach 1 against a documented default of 0, so an
            # unlisted series is safe to price as a taker and refused as a
            # maker. One combined flag reads as "the fee table is unsafe",
            # which is not what it means.
            "default_taker_is_safe": schedule.default_taker_is_safe,
            "default_maker_is_safe": schedule.default_maker_is_safe,
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
        # Checked here, before anything is priced. Neither `taker_fee_cents`
        # nor `maker_fee_cents` raises on an unverified table — only
        # `price_ticket` did — so this handler's fail-closed promise below was
        # unreachable: the ticket UI rendered fees from a table nobody had
        # checked while POST /api/proposals refused with a 409, which reads as
        # a bug rather than as the policy it is.
        if not schedule.is_verified:
            raise UnverifiedFeeSchedule()
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


@router.get("/autonomy")
async def autonomy_state() -> dict[str, Any]:
    """What the machine may do, and — when it may not — precisely why.

    Read-only. Nothing here can arm anything: the environment half of the
    interlock is outside this process's reach by design, and the config half
    needs a file edit and a restart. The dashboard has no auth in front of it,
    so "nothing on the LAN can start unattended trading" has to be a property
    of the surface, not a convention.

    The useful field is ``evidence``. An operator deciding whether to arm a
    route wants the measured numbers in front of them, and the gate refusing
    everything on this deployment is the expected reading rather than a fault
    — coverage fails on real thresholds and the report card has no settled
    trades to work from.

    The evidence shown here is read from Redis, because the gate runs in the
    *worker* and these are separate processes with separate memory. That is
    display only and one-way: the gate reads its own in-process snapshot and
    never this key. Authorisation and display are deliberately different
    paths, since a key any process could write must not be able to authorise
    a trade — and a key that outlived its writer must not authorise one after
    the evidence had gone.
    """
    settings = get_settings()
    config = get_config()
    evidence = await autonomy.published_evidence()

    return {
        "enabled": config.autonomous.enabled,
        "env_armed": settings.autonomous_trading,
        "live_armed": settings.autonomous_live_armed,
        "routes": {
            "simulated": config.autonomous.routes.simulated,
            "demo_exchange": config.autonomous.routes.demo_exchange,
            "live_exchange": config.autonomous.routes.live_exchange,
        },
        # Two different stops, and an operator reaching for the wrong one in a
        # hurry is a foreseeable failure — so both are reported, named, and
        # described. The kill switch halts everything including manual
        # approvals and cancels resting orders; the latch stops only the
        # machine and needs a human to clear it.
        "kill_switch": await get_kill_switch(),
        "disarmed_reason": await autonomy.disarm_reason(),
        "requires_manual_rearm": config.autonomous.require_manual_rearm,
        "min_proposal_age_sec": config.autonomous.min_proposal_age_sec,
        "decision_interval_sec": config.autonomous.decision_interval_sec,
        "evidence": evidence,
        "budget": {
            "max_trades_per_hour": config.autonomous.budget.max_trades_per_hour,
            "max_trades_per_detector_per_hour": (
                config.autonomous.budget.max_trades_per_detector_per_hour
            ),
            "max_daily_risk_cents": config.autonomous.budget.max_daily_risk_cents,
            "max_open_positions": config.autonomous.budget.max_open_positions,
            "max_working_orders": config.autonomous.budget.max_working_orders,
            "repeat_cooldown_sec": config.autonomous.budget.repeat_cooldown_sec,
        },
    }

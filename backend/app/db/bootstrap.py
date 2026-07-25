"""Schema creation and TimescaleDB setup.

Plain ``create_all`` for now; migrations arrive with Alembic in M1 once the
schema stops moving. Hypertable conversion is best-effort — the stack runs
fine on stock Postgres, it just loses time-partitioning on the hot tables.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.logging import get_logger
from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base, get_engine

log = get_logger(__name__)

# table -> time column
_HYPERTABLES: dict[str, str] = {
    "candles": "ts",
    "tape": "ts",
    "orderbook_snaps": "ts",
    "external_prices": "ts",
}


async def init_db() -> None:
    """Create tables and, where available, convert hot tables to hypertables."""
    engine = get_engine()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    log.info("schema ready (%d tables)", len(Base.metadata.tables))
    await _try_timescale()


async def _try_timescale() -> None:
    """Convert the hot tables to hypertables, best-effort.

    Each conversion runs in its own transaction: a failure on one table
    aborts only that transaction, so the rest still get converted.
    """
    engine = get_engine()

    async with engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        except Exception as exc:  # pragma: no cover - depends on image
            log.warning("TimescaleDB unavailable, using plain Postgres: %s", exc)
            return

    converted = 0
    for table, time_col in _HYPERTABLES.items():
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "SELECT create_hypertable(:t, :c, "
                        "if_not_exists => TRUE, migrate_data => TRUE)"
                    ),
                    {"t": table, "c": time_col},
                )
            converted += 1
        except Exception as exc:  # pragma: no cover
            log.warning("could not convert %s to hypertable: %s", table, exc)

    log.info("hypertables ready: %d/%d", converted, len(_HYPERTABLES))

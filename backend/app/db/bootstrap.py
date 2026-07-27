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
    await _sync_enum_labels()
    await _try_timescale()


def _quote_ident(name: str) -> str:
    """Quote a Postgres identifier. Names come from our own models."""
    return '"' + name.replace('"', '""') + '"'


async def _sync_enum_labels() -> None:
    """Add any enum labels the code knows about but the database does not.

    ``create_all`` creates a Postgres enum type once and never touches it
    again, so adding a member to a Python enum leaves the database type
    stale. That does not fail at boot — it fails much later, the first time
    something tries to *write* the new value, as a 500 in the middle of a
    trade. Adding ``PARTIAL`` to proposal_status did exactly that.

    Labels are only ever added, never removed or reordered, so this is safe
    to run on every start. It is not a substitute for migrations; it closes
    the one gap that bites hardest while there are none.
    """
    from sqlalchemy import Enum as SAEnum

    engine = get_engine()
    wanted: dict[str, list[str]] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, SAEnum) and column.type.name:
                wanted.setdefault(column.type.name, list(column.type.enums))

    added = 0
    for type_name, labels in wanted.items():
        try:
            async with engine.begin() as conn:
                existing = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT e.enumlabel FROM pg_enum e "
                                "JOIN pg_type t ON t.oid = e.enumtypid "
                                "WHERE t.typname = :name"
                            ),
                            {"name": type_name},
                        )
                    ).scalars()
                )
            if not existing:
                continue  # type not created yet; create_all will handle it
            for label in labels:
                if label in existing:
                    continue
                # Cannot run inside a transaction block on older servers, and
                # each label is independent, so give each its own connection.
                # ALTER TYPE ... ADD VALUE takes a literal, not a bind
                # parameter. Both names come from our own declarative models,
                # never from input, and the quoting below is belt and braces.
                safe_type = _quote_ident(type_name)
                safe_label = label.replace("'", "''")
                async with engine.connect() as conn:
                    await conn.execution_options(isolation_level="AUTOCOMMIT")
                    await conn.execute(
                        text(
                            f"ALTER TYPE {safe_type} "
                            f"ADD VALUE IF NOT EXISTS '{safe_label}'"
                        )
                    )
                log.warning(
                    "enum %s was missing label %r; added it", type_name, label
                )
                added += 1
        except Exception as exc:  # noqa: BLE001 - best effort, never block boot
            log.warning("could not sync enum %s: %s", type_name, exc)

    if added:
        log.warning("added %d missing enum label(s)", added)


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

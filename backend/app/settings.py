"""Environment-derived settings (secrets, endpoints, connection strings).

Split from :mod:`app.config` on purpose: this module holds things that come
from ``.env`` and never appear in the UI, while ``config.yaml`` holds tunables
the operator edits at runtime.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KalshiEnv(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- environment ----------------------------------------------------
    kalshi_env: KalshiEnv = KalshiEnv.DEMO
    live_trading: bool = False
    #: Whether this machine may approve trades without a human.
    #:
    #: In the environment rather than config.yaml on purpose. The split is
    #: that the environment says *this deployment may act on its own at all*
    #: — which requires editing .env and restarting — while config.yaml says
    #: which rails and at what budget. An operator tuning the budget from the
    #: settings UI cannot arm the machine, and arming it is not a thing that
    #: can happen from a browser on the LAN.
    autonomous_trading: bool = False
    service_role: str = "api"

    # -- credentials ----------------------------------------------------
    kalshi_demo_key_id: str = ""
    kalshi_demo_private_key_path: Path = Path("/run/secrets/kalshi_demo_key.pem")
    kalshi_prod_key_id: str = ""
    kalshi_prod_private_key_path: Path = Path("/run/secrets/kalshi_prod_key.pem")

    # -- endpoints (docs.kalshi.com recommends the external-api hosts) ---
    kalshi_prod_rest_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_prod_ws_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    kalshi_demo_rest_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    kalshi_demo_ws_url: str = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    kalshi_rate_tier: str = "basic"

    # -- external services ----------------------------------------------
    anthropic_api_key: str = ""

    # -- datastores -----------------------------------------------------
    database_url: str = (
        "postgresql+psycopg://copilot:change-me-before-first-run@db:5432/kalshi_copilot"
    )
    redis_url: str = "redis://redis:6379/0"

    # -- paths ----------------------------------------------------------
    config_path: Path = Path("/app/config.yaml")
    fee_schedule_path: Path = Path("/app/data/fee_schedule.yaml")

    # -- observability --------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "console"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # -- derived --------------------------------------------------------

    @property
    def is_prod(self) -> bool:
        return self.kalshi_env is KalshiEnv.PROD

    @property
    def rest_url(self) -> str:
        return self.kalshi_prod_rest_url if self.is_prod else self.kalshi_demo_rest_url

    @property
    def ws_url(self) -> str:
        return self.kalshi_prod_ws_url if self.is_prod else self.kalshi_demo_ws_url

    @property
    def key_id(self) -> str:
        return self.kalshi_prod_key_id if self.is_prod else self.kalshi_demo_key_id

    @property
    def private_key_path(self) -> Path:
        return (
            self.kalshi_prod_private_key_path
            if self.is_prod
            else self.kalshi_demo_private_key_path
        )

    @property
    def live_trading_armed(self) -> bool:
        """Both interlocks thrown.

        Even when this is True, no order is placed without per-trade
        confirmation in the UI — or, on the autonomous path, without
        everything :attr:`autonomous_live_armed` additionally requires. This
        only unlocks the possibility.
        """
        return self.is_prod and self.live_trading

    @property
    def autonomous_live_armed(self) -> bool:
        """All three environment interlocks for machine-driven real money.

        Strictly stronger than :attr:`live_trading_armed`: a deployment can be
        armed for a human to trade live while the machine is not. There is no
        combination in which this is true and that is false, which is the
        property that keeps "the robot may trade real money" from being
        reachable by fewer facts than "a person may".

        Even when this is True, an order still needs a ``MachineConsent`` from
        the autonomy gate — the route armed in config, a report card showing a
        measured edge, usable coverage, and budget left.
        """
        return self.is_prod and self.live_trading and self.autonomous_trading

    def credentials_present(self) -> bool:
        """True when the active environment has a key ID and a readable key file.

        Never raises. ``Path.is_file()`` propagates ``PermissionError`` when a
        parent directory is not traversable, and this is called from
        ``resolve_route`` — which runs at startup and on every request to
        ``/api/trading/state``. Letting it escape took the whole API down,
        public market data included, because a key file was mode 600 and the
        container runs as a different uid.

        An unreadable key is reported as "no usable credentials", which is
        exactly what it is. It is logged at ERROR rather than swallowed,
        because "you have a key but I cannot read it" is a different problem
        from "you have no key" and needs a different fix.
        """
        if not self.key_id:
            return False
        try:
            return self.private_key_path.is_file()
        except OSError as exc:
            from app.core.logging import get_logger

            get_logger(__name__).error(
                "private key at %s exists but cannot be read (%s). The "
                "containers run as uid 10001; a key that is mode 600 and owned "
                "by another user is unreadable to them. Treating this as no "
                "usable credentials.",
                self.private_key_path,
                exc.__class__.__name__,
            )
            return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

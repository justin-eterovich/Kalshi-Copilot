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
        confirmation in the UI — this only unlocks the possibility.
        """
        return self.is_prod and self.live_trading

    def credentials_present(self) -> bool:
        """True when the active environment has a key ID and a readable key file."""
        return bool(self.key_id) and self.private_key_path.is_file()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

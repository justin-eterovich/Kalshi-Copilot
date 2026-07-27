"""Loader for ``config.yaml`` — the operator-editable tunables.

Validated with pydantic so a typo in the YAML fails at boot with a clear
message rather than silently disabling a risk limit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderConfig(_Base):
    time_in_force: Literal["gtc", "ioc"] = "gtc"
    auto_cancel_after_sec: int = Field(90, ge=1)
    allow_price_improvement_only: bool = True


class TradingConfig(_Base):
    # There is no third value here and there must never be one. A test
    # asserts that; see tests/test_config.py.
    mode: Literal["paper", "live"] = "paper"
    default_proposal_ttl_sec: int = Field(120, ge=5)
    #: In paper mode, place real orders on the *demo* exchange when demo
    #: credentials exist, instead of filling against the local simulator.
    #: Demo is play money, so this exercises the real order rail for free.
    #: Set false to keep everything local. It cannot route to production —
    #: paper mode never does, whatever this says.
    paper_uses_demo_exchange: bool = True
    order: OrderConfig = OrderConfig()


class RiskConfig(_Base):
    bankroll_usd: float = Field(1000.0, gt=0)
    #: Hard ceiling on proposals awaiting a decision. The safety model rests
    #: on a human genuinely reading each one; a detector scanning every 20s
    #: can fill a queue faster than anyone can, and approval fatigue is a
    #: failure mode no test catches.
    max_pending_proposals: int = Field(10, ge=1)
    kelly_fraction: float = Field(0.25, gt=0, le=1)
    max_pct_per_market: float = Field(0.05, gt=0, le=1)
    max_total_exposure_pct: float = Field(0.40, gt=0, le=1)
    daily_loss_limit_pct: float = Field(0.05, gt=0, le=1)
    cooldown_after_consecutive_losses: int = Field(3, ge=1)
    cooldown_minutes: int = Field(60, ge=0)
    kill_switch: bool = False


class CostsConfig(_Base):
    slippage_buffer_cents: float = Field(0.5, ge=0)
    assume_taker: bool = True


class ScannerConfig(_Base):
    enabled: bool = True
    max_markets: int = Field(500, ge=0)
    series_filter: list[str] = Field(default_factory=list)


class IngestConfig(_Base):
    watchlist: list[str] = Field(default_factory=list)
    scanner: ScannerConfig = ScannerConfig()
    catalog_refresh_sec: int = Field(300, ge=30)
    orderbook_snapshot_throttle_ms: int = Field(1000, ge=0)
    candle_interval_sec: int = Field(60, ge=1)


class DetectorConfig(_Base):
    """Base for every detector block. All ship disabled."""

    model_config = ConfigDict(extra="allow")
    enabled: bool = False


class DetectorsConfig(_Base):
    model_config = ConfigDict(extra="allow")

    #: How long an identical observation is folded into the existing signal
    #: row instead of writing a new one. Detectors re-derive the same finding
    #: on every scan; without this the signals table fills with duplicates and
    #: stops being read, which is the approval-fatigue failure one step
    #: earlier in the pipeline. Zero disables folding entirely.
    dedupe_window_sec: int = Field(900, ge=0)
    #: How far the net edge must move for a repeat to count as a *new*
    #: observation rather than the same one. Too coarse and an edge growing
    #: from 1c to 8c disappears into a counter.
    dedupe_edge_change_cents: float = Field(1.0, ge=0)

    set_arbitrage: DetectorConfig = DetectorConfig()
    stale_quote: DetectorConfig = DetectorConfig()
    resolution_sniper: DetectorConfig = DetectorConfig()
    undervalued_screener: DetectorConfig = DetectorConfig()
    whale_flow: DetectorConfig = DetectorConfig()
    leaderboard_watcher: DetectorConfig = DetectorConfig()
    longshot_calibration: DetectorConfig = DetectorConfig()

    def enabled_names(self) -> list[str]:
        names: list[str] = []
        for name, value in self:
            if isinstance(value, DetectorConfig) and value.enabled:
                names.append(name)
        return names


class BitcoinConfig(_Base):
    enabled: bool = False
    spot_source: Literal["coinbase", "binance", "kraken"] = "coinbase"
    ewma_lambda: float = Field(0.94, gt=0, lt=1)
    vol_lookback_minutes: int = Field(1440, ge=1)
    min_net_edge_cents: float = Field(2.0, ge=0)
    use_deribit_implied: bool = False


class WeatherConfig(_Base):
    enabled: bool = False
    user_agent: str = "kalshi-copilot (self-hosted)"
    refresh_forecast_sec: int = Field(900, ge=60)
    refresh_observations_sec: int = Field(300, ge=60)
    min_net_edge_cents: float = Field(2.0, ge=0)
    min_calibration_samples: int = Field(30, ge=0)


class CalendarConfig(_Base):
    enabled: bool = False
    sources: list[str] = Field(default_factory=list)


class FeedConfig(_Base):
    """One RSS/Atom feed. ``source`` is the label stored on every headline.

    Provenance is not decoration: "which feed said so" is the first question
    asked when a headline turns out to be wrong or duplicated, and a headline
    whose origin is unknown cannot be audited later.
    """

    source: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1)


class HeadlinesConfig(_Base):
    enabled: bool = False
    rss_feeds: list[FeedConfig] = Field(default_factory=list)
    triage_model: str | None = None
    scoring_model: str | None = None
    #: Zero is not "unlimited" — the budget guard refuses everything at zero,
    #: deliberately, so a blank config cannot spend money.
    daily_budget_usd: float = Field(2.0, ge=0)
    escalation_rate_cap: float = Field(0.10, gt=0, le=1)


class NewsConfig(_Base):
    enabled: bool = False
    #: Sent on every feed request. Not cosmetic: BLS and SEC serve an HTML
    #: "Access Denied" page **with HTTP 200** to unrecognised agents, so a
    #: missing or generic UA fails silently rather than loudly. The parser
    #: catches it on the root tag, but the fix is to identify properly.
    user_agent: str = "kalshi-copilot (self-hosted)"
    calendar: CalendarConfig = CalendarConfig()
    headlines: HeadlinesConfig = HeadlinesConfig()


class NotificationsConfig(_Base):
    enabled: bool = True
    #: Defaults off. Web Push needs a secure context (HTTPS or localhost) and
    #: the dashboard is plain HTTP on a LAN address by design, so a service
    #: worker cannot register. Defaulting this true would promise alerts with
    #: the tab closed that the browser silently refuses to deliver.
    web_push_enabled: bool = False
    audio_enabled: bool = True
    favicon_badge: bool = True


class BacktestConfig(_Base):
    pessimistic_fills: bool = True
    report_card_min_trades: int = Field(20, ge=1)


class Config(_Base):
    trading: TradingConfig = TradingConfig()
    risk: RiskConfig = RiskConfig()
    costs: CostsConfig = CostsConfig()
    ingest: IngestConfig = IngestConfig()
    detectors: DetectorsConfig = DetectorsConfig()
    bitcoin: BitcoinConfig = BitcoinConfig()
    weather: WeatherConfig = WeatherConfig()
    news: NewsConfig = NewsConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    backtest: BacktestConfig = BacktestConfig()


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config.yaml not found at {p}")
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


@lru_cache(maxsize=1)
def get_config() -> Config:
    from app.settings import get_settings

    return load_config(get_settings().config_path)

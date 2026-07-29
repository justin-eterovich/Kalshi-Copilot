"""Loader for ``config.yaml`` — the operator-editable tunables.

Validated with pydantic so a typo in the YAML fails at boot with a clear
message rather than silently disabling a risk limit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderConfig(_Base):
    time_in_force: Literal["gtc", "ioc"] = "gtc"
    auto_cancel_after_sec: int = Field(90, ge=1)
    allow_price_improvement_only: bool = True


class TradingConfig(_Base):
    # There is no third value here and there must never be one. A test
    # asserts that; see tests/test_config.py.
    #
    # `mode` answers **where** an approved order goes. It deliberately does
    # not answer **who** approved it — that is `autonomous:` below. Folding
    # autonomy in here as a third value would conflate routing with consent,
    # and the two vary independently: the machine can be armed on the
    # simulator while the route is live, or disarmed while the route is
    # paper.
    mode: Literal["paper", "live"] = "paper"
    default_proposal_ttl_sec: int = Field(120, ge=5)
    #: In paper mode, place real orders on the *demo* exchange when demo
    #: credentials exist, instead of filling against the local simulator.
    #: Demo is play money, so this exercises the real order rail for free.
    #: Set false to keep everything local. It cannot route to production —
    #: paper mode never does, whatever this says.
    paper_uses_demo_exchange: bool = True
    order: OrderConfig = OrderConfig()


class AutonomousRouteConfig(_Base):
    """Which rails the machine may fire on, one flag each.

    The field names are exactly the ``ExecutionRoute`` values so the gate can
    do ``getattr(cfg.routes, route.value)`` with no lookup table in between.
    A mapping here would be a second place for the route names to live, and
    the two would eventually disagree.
    """

    simulated: bool = False
    demo_exchange: bool = False
    live_exchange: bool = False


class AutonomousEvidenceConfig(_Base):
    """What the machine must be able to prove before it may act.

    The human click is a judgement about *this* trade. Nothing the machine
    can do replaces that, so what stands in for it is a different kind of
    claim: that this detector, on this route, has already been measured to
    make money. That is what the report card computes and what nothing in
    the trading path consulted before now.
    """

    #: Refuse unless the report card says ``EDGE_SHOWN`` for this
    #: (detector, route) pair — i.e. the bootstrap CI's lower bound is above
    #: zero. Not the mean: see app/backtest/stats.py on why Wald and point
    #: estimates flatter a 20-trade sample.
    require_edge_shown: bool = True
    #: Refuse unless the backtest coverage gate passes. May be waived on the
    #: simulated route only (the root validator enforces that), which is the
    #: bring-up case — the same escape hatch `run_backtest(ignore_coverage=)`
    #: already offers, with the same honesty requirement: posture() reports
    #: the waiver so the dashboard says so.
    require_coverage_usable: bool = True
    #: None means "use backtest.report_card_min_trades". An explicit value may
    #: only *raise* that floor; the root validator refuses a lower one, because
    #: a knob that can weaken the evidence requirement is not an evidence
    #: requirement.
    min_trades: int | None = Field(None, ge=1)
    #: An evidence snapshot older than this is refused rather than used.
    #: Three refresh intervals, so two consecutive failed refreshes are
    #: survivable and three are not.
    max_report_age_sec: int = Field(900, ge=30)


class AutonomousBudgetConfig(_Base):
    """Ceilings on how much the machine may do before a human looks.

    **Every ceiling defaults to zero, and zero refuses** — the same
    convention as ``news.headlines.daily_budget_usd``. A blank or
    half-written config must not be able to spend anything. "Unlimited" is
    not expressible here on purpose.
    """

    max_trades_per_hour: int = Field(0, ge=0)
    max_trades_per_detector_per_hour: int = Field(0, ge=0)
    #: Sum of ``max_loss_cents`` over autonomous approvals in the UTC day.
    #: Deliberately not notional: on a sold arbitrage set the notional is
    #: nearly meaningless while max_loss is the number the whole risk layer
    #: already speaks.
    max_daily_risk_cents: int = Field(0, ge=0)
    #: Open markets on the armed route, from *any* source. Not "autonomous
    #: positions": a Position does not remember who built it, and inventing
    #: that attribution in order to raise a ceiling is the wrong direction.
    max_open_positions: int = Field(0, ge=0)
    #: Working orders on the route before autonomy stops adding. 1 means
    #: "place one, wait for it to resolve, then consider the next".
    max_working_orders: int = Field(1, ge=0)
    #: How long after an autonomous trade on a ticker the same detector is
    #: refused on that ticker again.
    #:
    #: This is not a nicety. The duplicate guard in proposals.py only refuses
    #: while a proposal is *pending*, so today the human is the rate limiter.
    #: Without a cooldown: detector proposes -> gate approves seconds later ->
    #: the duplicate guard clears -> the detector re-derives the same edge on
    #: its next scan -> approve again, repeatedly trading one market until the
    #: per-market exposure cap finally binds.
    repeat_cooldown_sec: int = Field(3600, ge=0)


class AutonomousConfig(_Base):
    """Machine approval: who may authorise an order, with no human click.

    Orthogonal to ``trading.mode``, which says where an approved order goes.
    Arming this on the live route needs four separate facts — ``enabled``
    here, ``routes.live_exchange`` here, and ``KALSHI_ENV=prod`` +
    ``LIVE_TRADING=true`` + ``AUTONOMOUS_TRADING=true`` in the environment —
    plus the evidence gate and the budget below.
    """

    enabled: bool = False
    routes: AutonomousRouteConfig = AutonomousRouteConfig()
    evidence: AutonomousEvidenceConfig = AutonomousEvidenceConfig()
    budget: AutonomousBudgetConfig = AutonomousBudgetConfig()
    decision_interval_sec: int = Field(10, ge=5)
    #: A proposal must have been visible for this long before the machine may
    #: act on it. This is the human veto window, and it is the reason
    #: autonomy does not mean "the operator never sees it".
    min_proposal_age_sec: int = Field(15, ge=0)

    # -- self-disarm ----------------------------------------------------
    disarm_after_consecutive_losses: int = Field(3, ge=1)
    disarm_after_failed_placements: int = Field(3, ge=1)
    #: Latch off when a pair that has already traded stops reading
    #: EDGE_SHOWN. The gate would refuse it anyway — but a detector whose
    #: edge vanished *after* it started trading size is the case where the
    #: premise failed, and that deserves a person rather than a silent stop.
    disarm_on_verdict_degradation: bool = True
    #: Once disarmed, stay disarmed until a human re-arms. Forced true
    #: whenever the live route is armed.
    require_manual_rearm: bool = True


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
    report_card_min_trades: int = Field(20, ge=1)


class Config(_Base):
    trading: TradingConfig = TradingConfig()
    autonomous: AutonomousConfig = AutonomousConfig()
    risk: RiskConfig = RiskConfig()
    costs: CostsConfig = CostsConfig()
    ingest: IngestConfig = IngestConfig()
    detectors: DetectorsConfig = DetectorsConfig()
    bitcoin: BitcoinConfig = BitcoinConfig()
    weather: WeatherConfig = WeatherConfig()
    news: NewsConfig = NewsConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    backtest: BacktestConfig = BacktestConfig()

    @model_validator(mode="after")
    def _autonomy_cannot_waive_its_own_gate(self) -> Config:
        """Refuse a config in which autonomy has relaxed what constrains it.

        Every check here is cross-block, which is why it cannot live on
        ``AutonomousConfig`` itself: the evidence floor comes from
        ``backtest``, and what may be waived depends on which route is armed.

        The shape to notice is that each rule refuses at *load* time rather
        than at decision time. A gate that can be talked out of its own
        threshold is not a gate, and the moment to discover that is boot, not
        the first autonomous order.
        """
        a = self.autonomous
        floor = self.backtest.report_card_min_trades

        if a.evidence.min_trades is not None and a.evidence.min_trades < floor:
            raise ValueError(
                f"autonomous.evidence.min_trades ({a.evidence.min_trades}) is "
                f"below backtest.report_card_min_trades ({floor}). The "
                "override exists to demand *more* evidence than the report "
                "card, never less."
            )

        hits_exchange = a.routes.demo_exchange or a.routes.live_exchange
        if hits_exchange and not a.evidence.require_coverage_usable:
            raise ValueError(
                "autonomous.evidence.require_coverage_usable cannot be false "
                "while demo_exchange or live_exchange is armed. Coverage may "
                "only be waived on the simulated route, where a bad decision "
                "costs nothing and the point is to generate the very evidence "
                "the gate is asking for."
            )

        if a.routes.live_exchange:
            if not a.evidence.require_edge_shown:
                raise ValueError(
                    "autonomous.evidence.require_edge_shown cannot be false "
                    "while live_exchange is armed. Unattended real money with "
                    "no measured edge is the configuration this gate exists "
                    "to prevent."
                )
            if not a.require_manual_rearm:
                raise ValueError(
                    "autonomous.require_manual_rearm cannot be false while "
                    "live_exchange is armed. A latch that clears itself would "
                    "resume trading real money on the same conditions that "
                    "stopped it, with nobody having looked."
                )

        return self


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

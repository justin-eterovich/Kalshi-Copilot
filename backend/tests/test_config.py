"""Tests for config.yaml loading and the safety defaults it encodes.

The point of these is not that YAML parses — it is that the shipped defaults
are the *safe* ones, and that a typo fails loudly instead of silently
disabling a risk limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Config, DetectorsConfig, load_config


def _find_shipped_config() -> Path:
    """Locate config.yaml from either the repo checkout or inside the image.

    On the host it sits at the repo root; in the container it is bind-mounted
    at /app/config.yaml.
    """
    candidates = [
        Path(__file__).resolve().parents[2] / "config.yaml",  # repo checkout
        Path("/app/config.yaml"),  # container
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"config.yaml not found in any of: {candidates}")


SHIPPED_CONFIG = _find_shipped_config()


@pytest.fixture
def shipped() -> Config:
    return load_config(SHIPPED_CONFIG)


# ---------------------------------------------------------------------------
# Shipped defaults must be the safe ones
# ---------------------------------------------------------------------------


def test_shipped_config_parses(shipped: Config) -> None:
    assert isinstance(shipped, Config)


def test_ships_in_paper_mode(shipped: Config) -> None:
    assert shipped.trading.mode == "paper"


def test_every_detector_ships_disabled(shipped: Config) -> None:
    assert shipped.detectors.enabled_names() == []


def test_engines_ship_disabled(shipped: Config) -> None:
    assert shipped.bitcoin.enabled is False
    assert shipped.weather.enabled is False
    assert shipped.news.enabled is False
    assert shipped.news.headlines.enabled is False


def test_kill_switch_starts_off(shipped: Config) -> None:
    """Off is correct: it is a lever to pull, not a default posture."""
    assert shipped.risk.kill_switch is False


def test_risk_limits_are_bounded(shipped: Config) -> None:
    assert 0 < shipped.risk.kelly_fraction <= 1
    assert 0 < shipped.risk.max_pct_per_market <= 1
    assert shipped.risk.max_pct_per_market <= shipped.risk.max_total_exposure_pct


def test_headline_layer_has_a_spend_cap(shipped: Config) -> None:
    assert shipped.news.headlines.daily_budget_usd > 0


def test_slippage_buffer_is_non_negative(shipped: Config) -> None:
    assert shipped.costs.slippage_buffer_cents >= 0


# ---------------------------------------------------------------------------
# Validation catches mistakes
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_is_rejected() -> None:
    """A typo'd section must not be silently ignored."""
    with pytest.raises(ValidationError):
        Config.model_validate({"risk": {}, "trading": {}, "riskk": {}})


def test_kelly_fraction_above_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"risk": {"kelly_fraction": 1.5}})


def test_negative_bankroll_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"risk": {"bankroll_usd": -100}})


def test_invalid_trading_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"trading": {"mode": "auto"}})


def test_there_is_no_autotrade_mode() -> None:
    """Guard the hard constraint: paper and live are the only modes."""
    import typing

    from app.config import TradingConfig

    mode_field = TradingConfig.model_fields["mode"]
    assert set(typing.get_args(mode_field.annotation)) == {"paper", "live"}


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/config.yaml")


# ---------------------------------------------------------------------------
# Detector enable/disable plumbing
# ---------------------------------------------------------------------------


def test_enabled_names_reports_only_enabled() -> None:
    detectors = DetectorsConfig.model_validate(
        {
            "set_arbitrage": {"enabled": True},
            "whale_flow": {"enabled": True},
            "resolution_sniper": {"enabled": False},
        }
    )
    assert sorted(detectors.enabled_names()) == ["set_arbitrage", "whale_flow"]


def test_detectors_default_to_off_when_absent() -> None:
    assert DetectorsConfig().enabled_names() == []

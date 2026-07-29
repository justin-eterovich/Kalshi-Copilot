"""Tests for config.yaml loading and the safety defaults it encodes.

The point of these is not that YAML parses — it is that the shipped defaults
are the *safe* ones, and that a typo fails loudly instead of silently
disabling a risk limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config import Config, DetectorsConfig, load_config


def _find_shipped_config() -> Path:
    """Locate `config.example.yaml` from the repo checkout or inside the image.

    Deliberately **not** `config.yaml`. That file is one deployment's live
    state — gitignored, edited to turn detectors on, and bind-mounted over
    whatever the image holds — so asserting "everything ships disabled"
    against it meant the assertion failed the moment an operator did the
    ordinary thing these very tests are meant to permit. The claim is about
    what a fresh clone gets, so it is made against the file a fresh clone
    gets.

    On the host that sits at the repo root; in the container it is baked in at
    /app/config.example.yaml by the Dockerfile (it is not bind-mounted, since
    the point is to test the committed copy).
    """
    candidates = [
        Path(__file__).resolve().parents[2] / "config.example.yaml",  # checkout
        Path("/app/config.example.yaml"),  # container
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"config.example.yaml not found in any of: {candidates}"
    )


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


def test_the_live_config_has_not_drifted_from_the_template() -> None:
    """Both files must carry the same *keys*, however their values differ.

    This is the cost of splitting them, and the reason the split is still
    worth it: two files can disagree, and the disagreement that matters is a
    key present in one and missing from the other. A key added only to
    `config.yaml` is invisible to a fresh clone; a key added only to the
    template is never exercised against a real boot. Values are expected to
    differ — that is the entire point — so only the shape is compared.

    Skipped when `config.yaml` is absent: it is gitignored, so a clean
    checkout legitimately has no deployment to compare against.
    """
    live = SHIPPED_CONFIG.with_name("config.yaml")
    if not live.is_file():
        pytest.skip("no config.yaml — nothing deployed to compare against")

    def keys(node: object, path: str = "") -> set[str]:
        if not isinstance(node, dict):
            return set()
        found = set()
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            found.add(here)
            found |= keys(value, here)
        return found

    template = keys(yaml.safe_load(SHIPPED_CONFIG.read_text()))
    deployed = keys(yaml.safe_load(live.read_text()))
    assert template - deployed == set(), (
        "config.yaml is missing keys the template ships: "
        f"{sorted(template - deployed)}"
    )
    assert deployed - template == set(), (
        "config.yaml has keys the template does not ship, so a fresh clone "
        f"would not get them: {sorted(deployed - template)}"
    )


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


def test_trading_mode_is_route_selection_only() -> None:
    """``mode`` says where an order goes, never who approved it.

    This assertion predates autonomous trading, where it guarded "there is no
    auto-trade mode". Autonomy exists now — in its own ``autonomous:`` block —
    and the assertion is kept verbatim because it now pins something else that
    matters just as much: that the two axes stayed separate.

    A third mode value would fold routing and consent into one knob, and they
    genuinely vary independently — the machine can be armed on the simulator
    while the route is live, or disarmed while the route is paper. Collapsing
    them would make "is this real money?" and "is anyone watching?" the same
    question, and they are not.
    """
    import typing

    from app.config import TradingConfig

    mode_field = TradingConfig.model_fields["mode"]
    assert set(typing.get_args(mode_field.annotation)) == {"paper", "live"}


class TestAutonomyShipsDisarmed:
    """A config that says nothing about autonomy must not be able to trade."""

    def test_autonomy_is_disabled_by_default(self) -> None:
        cfg = Config.model_validate({})
        assert cfg.autonomous.enabled is False

    def test_no_route_is_armed_by_default(self) -> None:
        routes = Config.model_validate({}).autonomous.routes
        assert (routes.simulated, routes.demo_exchange, routes.live_exchange) == (
            False,
            False,
            False,
        )

    def test_the_evidence_gate_is_on_by_default(self) -> None:
        ev = Config.model_validate({}).autonomous.evidence
        assert ev.require_edge_shown is True
        assert ev.require_coverage_usable is True

    @pytest.mark.parametrize(
        "field",
        [
            "max_trades_per_hour",
            "max_trades_per_detector_per_hour",
            "max_daily_risk_cents",
            "max_open_positions",
        ],
    )
    def test_every_spending_budget_defaults_to_zero(self, field: str) -> None:
        """Zero refuses. A blank config must not be able to spend anything.

        The gate treats zero as "no allowance", never as "unlimited" — the
        same convention as ``news.headlines.daily_budget_usd``. The direction
        matters: the other reading turns a half-written config into an
        uncapped one.
        """
        budget = Config.model_validate({}).autonomous.budget
        assert getattr(budget, field) == 0

    def test_an_unknown_autonomous_key_is_rejected(self) -> None:
        """extra="forbid" — a typo must fail at boot, not disable a limit."""
        with pytest.raises(ValidationError):
            Config.model_validate({"autonomous": {"enabledd": True}})

    def test_an_unknown_budget_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Config.model_validate(
                {"autonomous": {"budget": {"max_trades_per_day": 5}}}
            )


class TestAutonomyCannotWaiveItsOwnGate:
    """The evidence requirement is not adjustable downward from inside."""

    def test_min_trades_cannot_go_below_the_report_card_floor(self) -> None:
        with pytest.raises(ValidationError, match="below backtest"):
            Config.model_validate(
                {
                    "backtest": {"report_card_min_trades": 20},
                    "autonomous": {"evidence": {"min_trades": 5}},
                }
            )

    def test_min_trades_may_be_raised(self) -> None:
        cfg = Config.model_validate(
            {
                "backtest": {"report_card_min_trades": 20},
                "autonomous": {"evidence": {"min_trades": 50}},
            }
        )
        assert cfg.autonomous.evidence.min_trades == 50

    def test_min_trades_may_equal_the_floor(self) -> None:
        cfg = Config.model_validate(
            {
                "backtest": {"report_card_min_trades": 20},
                "autonomous": {"evidence": {"min_trades": 20}},
            }
        )
        assert cfg.autonomous.evidence.min_trades == 20

    @pytest.mark.parametrize("route", ["demo_exchange", "live_exchange"])
    def test_an_exchange_route_cannot_waive_coverage(self, route: str) -> None:
        with pytest.raises(ValidationError, match="require_coverage_usable"):
            Config.model_validate(
                {
                    "autonomous": {
                        "enabled": True,
                        "routes": {route: True},
                        "evidence": {"require_coverage_usable": False},
                    }
                }
            )

    def test_the_simulated_route_may_waive_coverage(self) -> None:
        """The one permitted waiver, and only where it costs nothing.

        Coverage refuses until the deployment has months of data. On the
        simulator that is a deadlock — the route exists to *generate* the
        evidence the gate wants, and it cannot spend money doing it.
        """
        cfg = Config.model_validate(
            {
                "autonomous": {
                    "enabled": True,
                    "routes": {"simulated": True},
                    "evidence": {"require_coverage_usable": False},
                }
            }
        )
        assert cfg.autonomous.evidence.require_coverage_usable is False

    def test_live_cannot_waive_edge_shown(self) -> None:
        with pytest.raises(ValidationError, match="require_edge_shown"):
            Config.model_validate(
                {
                    "autonomous": {
                        "enabled": True,
                        "routes": {"live_exchange": True},
                        "evidence": {"require_edge_shown": False},
                    }
                }
            )

    def test_live_cannot_waive_manual_rearm(self) -> None:
        with pytest.raises(ValidationError, match="require_manual_rearm"):
            Config.model_validate(
                {
                    "autonomous": {
                        "enabled": True,
                        "routes": {"live_exchange": True},
                        "require_manual_rearm": False,
                    }
                }
            )

    def test_the_simulated_route_may_self_rearm(self) -> None:
        cfg = Config.model_validate(
            {
                "autonomous": {
                    "enabled": True,
                    "routes": {"simulated": True},
                    "require_manual_rearm": False,
                }
            }
        )
        assert cfg.autonomous.require_manual_rearm is False


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

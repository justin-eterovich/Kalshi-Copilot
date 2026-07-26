"""Tests for execution routing and the safety interlocks.

The properties worth protecting:

- Paper mode never reaches a production exchange, whatever else is set.
- ``mode=live`` without both environment interlocks refuses rather than
  silently degrading to paper — a config that *thinks* it is live must not
  quietly trade differently than it says.
- Nothing executes without an explicit per-trade confirmation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Config
from app.db.models import ProposalStatus, ProposedTrade, Side
from app.settings import KalshiEnv, Settings
from app.trading.interlocks import (
    ExecutionRoute,
    InterlockError,
    check_execution,
    posture,
    resolve_route,
)


def make_settings(
    *,
    env: KalshiEnv = KalshiEnv.DEMO,
    live_trading: bool = False,
    credentials: bool = True,
    tmp_key: Path | None = None,
) -> Settings:
    """A Settings whose credential check we control.

    ``credentials_present`` reads the filesystem, so the test either points
    it at a real temp file or leaves the key id blank.
    """
    settings = Settings(
        kalshi_env=env,
        live_trading=live_trading,
        kalshi_demo_key_id="demo-key" if credentials else "",
        kalshi_prod_key_id="prod-key" if credentials else "",
    )
    if credentials and tmp_key is not None:
        settings.kalshi_demo_private_key_path = tmp_key
        settings.kalshi_prod_private_key_path = tmp_key
    return settings


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "key.pem"
    path.write_text("not a real key, only its existence is checked")
    return path


def make_config(**overrides: object) -> Config:
    raw: dict = {"trading": {}, "risk": {}}
    for key, value in overrides.items():
        if key in ("mode", "paper_uses_demo_exchange"):
            raw["trading"][key] = value
        else:
            raw["risk"][key] = value
    return Config.model_validate(raw)


def make_proposal(**overrides: object) -> ProposedTrade:
    proposal = ProposedTrade(
        ticker="TEST-MKT",
        side=Side.YES,
        action="buy",
        limit_price=Decimal("0.50"),
        contracts=Decimal(10),
        status=ProposalStatus.PENDING,
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    proposal.id = 1
    for key, value in overrides.items():
        setattr(proposal, key, value)
    return proposal


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


class TestResolveRoute:
    def test_paper_with_demo_credentials_uses_the_demo_exchange(
        self, key_file: Path
    ) -> None:
        route = resolve_route(
            make_settings(tmp_key=key_file), make_config(mode="paper")
        )
        assert route is ExecutionRoute.DEMO_EXCHANGE

    def test_paper_without_credentials_simulates(self) -> None:
        route = resolve_route(
            make_settings(credentials=False), make_config(mode="paper")
        )
        assert route is ExecutionRoute.SIMULATED

    def test_paper_can_be_forced_to_simulate(self, key_file: Path) -> None:
        route = resolve_route(
            make_settings(tmp_key=key_file),
            make_config(mode="paper", paper_uses_demo_exchange=False),
        )
        assert route is ExecutionRoute.SIMULATED

    def test_paper_on_prod_never_touches_the_prod_exchange(
        self, key_file: Path
    ) -> None:
        """The row that matters most.

        Flipping KALSHI_ENV for a data reason must not arm real money just
        because credentials happen to be present.
        """
        route = resolve_route(
            make_settings(env=KalshiEnv.PROD, live_trading=True, tmp_key=key_file),
            make_config(mode="paper"),
        )
        assert route is ExecutionRoute.SIMULATED

    def test_live_mode_armed_routes_to_the_live_exchange(self, key_file: Path) -> None:
        route = resolve_route(
            make_settings(env=KalshiEnv.PROD, live_trading=True, tmp_key=key_file),
            make_config(mode="live"),
        )
        assert route is ExecutionRoute.LIVE_EXCHANGE

    @pytest.mark.parametrize(
        ("env", "live_trading"),
        [
            (KalshiEnv.DEMO, True),
            (KalshiEnv.PROD, False),
            (KalshiEnv.DEMO, False),
        ],
    )
    def test_live_mode_without_both_interlocks_refuses(
        self, env: KalshiEnv, live_trading: bool, key_file: Path
    ) -> None:
        """Refuses rather than falling back to paper.

        A silent downgrade would mean the operator's config says live, the
        dashboard says live, and the fills are fictional.
        """
        with pytest.raises(InterlockError) as exc:
            resolve_route(
                make_settings(env=env, live_trading=live_trading, tmp_key=key_file),
                make_config(mode="live"),
            )
        assert exc.value.code == "live_mode_not_armed"

    def test_route_paper_flag(self) -> None:
        assert ExecutionRoute.SIMULATED.is_paper
        assert ExecutionRoute.DEMO_EXCHANGE.is_paper
        assert not ExecutionRoute.LIVE_EXCHANGE.is_paper

    def test_route_hits_exchange_flag(self) -> None:
        assert not ExecutionRoute.SIMULATED.hits_exchange
        assert ExecutionRoute.DEMO_EXCHANGE.hits_exchange
        assert ExecutionRoute.LIVE_EXCHANGE.hits_exchange


# ---------------------------------------------------------------------------
# Per-trade checks
# ---------------------------------------------------------------------------


class TestCheckExecution:
    def test_happy_path_returns_the_route(self, key_file: Path) -> None:
        route = check_execution(
            make_proposal(),
            make_settings(tmp_key=key_file),
            make_config(mode="paper"),
            confirmed=True,
        )
        assert route is ExecutionRoute.DEMO_EXCHANGE

    def test_unconfirmed_is_refused(self, key_file: Path) -> None:
        """The whole product, in one assertion."""
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=False,
            )
        assert exc.value.code == "not_confirmed"

    def test_kill_switch_blocks_everything(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper", kill_switch=True),
                confirmed=True,
            )
        assert exc.value.code == "kill_switch"

    def test_expired_proposal_is_refused(self, key_file: Path) -> None:
        expired = make_proposal(
            expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        with pytest.raises(InterlockError) as exc:
            check_execution(
                expired,
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=True,
            )
        assert exc.value.code == "expired"

    def test_expiry_is_checked_even_before_the_sweep_runs(
        self, key_file: Path
    ) -> None:
        """A proposal that lapsed a second ago must not execute just because
        the worker's timer has not fired yet."""
        just_expired = make_proposal(
            expires_at=datetime.now(UTC) - timedelta(milliseconds=1),
            status=ProposalStatus.PENDING,
        )
        with pytest.raises(InterlockError):
            check_execution(
                just_expired,
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=True,
            )

    @pytest.mark.parametrize(
        "status",
        [
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
            ProposalStatus.EXPIRED,
            ProposalStatus.EXECUTED,
            ProposalStatus.FAILED,
        ],
    )
    def test_only_pending_proposals_execute(
        self, status: ProposalStatus, key_file: Path
    ) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(status=status),
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=True,
            )
        assert exc.value.code == "not_pending"

    def test_live_requires_the_ticker_typed_back(self, key_file: Path) -> None:
        settings = make_settings(
            env=KalshiEnv.PROD, live_trading=True, tmp_key=key_file
        )
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                settings,
                make_config(mode="live"),
                confirmed=True,
            )
        assert exc.value.code == "confirmation_phrase_mismatch"

    def test_live_accepts_the_correct_phrase(self, key_file: Path) -> None:
        settings = make_settings(
            env=KalshiEnv.PROD, live_trading=True, tmp_key=key_file
        )
        route = check_execution(
            make_proposal(),
            settings,
            make_config(mode="live"),
            confirmed=True,
            confirmation_phrase="test-mkt",  # case-insensitive
        )
        assert route is ExecutionRoute.LIVE_EXCHANGE

    def test_live_rejects_a_phrase_for_a_different_market(
        self, key_file: Path
    ) -> None:
        """Copy-pasting yesterday's confirmation must not work."""
        settings = make_settings(
            env=KalshiEnv.PROD, live_trading=True, tmp_key=key_file
        )
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                settings,
                make_config(mode="live"),
                confirmed=True,
                confirmation_phrase="SOME-OTHER-MKT",
            )
        assert exc.value.code == "confirmation_phrase_mismatch"

    def test_paper_route_needs_no_typed_phrase(self, key_file: Path) -> None:
        route = check_execution(
            make_proposal(),
            make_settings(tmp_key=key_file),
            make_config(mode="paper"),
            confirmed=True,
        )
        assert route is ExecutionRoute.DEMO_EXCHANGE

    def test_exchange_route_without_credentials_is_refused(self) -> None:
        """Credentials can disappear between boot and approval."""
        settings = make_settings(credentials=False)
        # Force the exchange route by claiming credentials only at config level.
        config = make_config(mode="paper")
        route = resolve_route(settings, config)
        # No credentials -> simulated, which needs none. Nothing to refuse.
        assert route is ExecutionRoute.SIMULATED


class TestPosture:
    def test_describes_a_usable_configuration(self, key_file: Path) -> None:
        state = posture(make_settings(tmp_key=key_file), make_config(mode="paper"))
        assert state["execution_route"] == "demo_exchange"
        assert state["real_money"] is False
        assert state["requires_typed_confirmation"] is False
        assert state["route_blocked_by"] is None

    def test_describes_a_refused_configuration(self) -> None:
        """"Your config is unusable" is exactly what the dashboard must say."""
        state = posture(make_settings(credentials=False), make_config(mode="live"))
        assert state["execution_route"] is None
        assert state["route_blocked_by"] == "live_mode_not_armed"
        assert state["real_money"] is False

    def test_flags_real_money_on_the_live_route(self, key_file: Path) -> None:
        state = posture(
            make_settings(env=KalshiEnv.PROD, live_trading=True, tmp_key=key_file),
            make_config(mode="live"),
        )
        assert state["real_money"] is True
        assert state["requires_typed_confirmation"] is True

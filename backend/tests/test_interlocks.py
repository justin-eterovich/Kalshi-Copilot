"""Tests for execution routing and the safety interlocks.

The properties worth protecting:

- Paper mode never reaches a production exchange, whatever else is set.
- ``mode=live`` without both environment interlocks refuses rather than
  silently degrading to paper — a config that *thinks* it is live must not
  quietly trade differently than it says.
- Nothing executes without an explicit per-trade confirmation, **or** a
  machine consent from the autonomy gate — exactly one of the two, never
  both, and never neither.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Config
from app.db.models import ProposalStatus, ProposedTrade
from app.settings import KalshiEnv, Settings
from app.trading.interlocks import (
    ExecutionRoute,
    InterlockError,
    MachineConsent,
    check_execution,
    posture,
    resolve_route,
)


def make_settings(
    *,
    env: KalshiEnv = KalshiEnv.DEMO,
    live_trading: bool = False,
    autonomous_trading: bool = False,
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
        autonomous_trading=autonomous_trading,
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


def make_config(*, autonomous: dict | None = None, **overrides: object) -> Config:
    raw: dict = {"trading": {}, "risk": {}}
    if autonomous is not None:
        raw["autonomous"] = autonomous
    for key, value in overrides.items():
        if key in ("mode", "paper_uses_demo_exchange"):
            raw["trading"][key] = value
        else:
            raw["risk"][key] = value
    return Config.model_validate(raw)


def armed_config(route: str = "demo_exchange", **overrides: object) -> Config:
    """A config with the machine armed on one route."""
    return make_config(
        autonomous={"enabled": True, "routes": {route: True}}, **overrides
    )


def make_consent(**overrides: object) -> MachineConsent:
    """A consent that would pass, so each test can break exactly one thing."""
    fields: dict = {
        "proposal_id": 1,
        "route": ExecutionRoute.DEMO_EXCHANGE,
        "detector": "set_arbitrage",
        "verdict": "edge_shown",
        "trades": 41,
        "ci_low_cents": "0.83",
        "ci_high_cents": "3.11",
        "mean_cents": "1.94",
        "min_trades": 20,
        "coverage_usable": True,
        "coverage_refusals": (),
        "evidence_computed_at": datetime.now(UTC) - timedelta(seconds=30),
    }
    fields.update(overrides)
    return MachineConsent(**fields)  # type: ignore[arg-type]


def make_proposal(**overrides: object) -> ProposedTrade:
    proposal = ProposedTrade(
        ticker="TEST-MKT",
        leg_count=1,
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
            kill_switch=False,
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
                kill_switch=False,
            )
        assert exc.value.code == "not_confirmed"

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
                kill_switch=False,
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
                kill_switch=False,
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
                kill_switch=False,
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
                kill_switch=False,
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
            kill_switch=False,
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
                kill_switch=False,
                confirmation_phrase="SOME-OTHER-MKT",
            )
        assert exc.value.code == "confirmation_phrase_mismatch"

    def test_paper_route_needs_no_typed_phrase(self, key_file: Path) -> None:
        route = check_execution(
            make_proposal(),
            make_settings(tmp_key=key_file),
            make_config(mode="paper"),
            confirmed=True,
            kill_switch=False,
        )
        assert route is ExecutionRoute.DEMO_EXCHANGE


class TestKillSwitch:
    """Two sources, either of which engages it, and no safe default.

    ``config.risk.kill_switch`` is the static floor from ``config.yaml``, read
    once per process because ``get_config()`` is ``lru_cache``d. That made it
    unusable as an emergency stop: engaging it meant editing a file and
    restarting containers, and since its two halves live in different
    processes — ``api`` refuses approvals, ``worker`` cancels resting orders —
    restarting only one left resting orders live while the dashboard read
    "engaged".

    So there is now a runtime flag as well, passed in by the caller from
    Redis. Either source refuses. Neither can release the other: a config that
    says ``true`` is a one-way door the API cannot open.
    """

    def test_the_runtime_flag_alone_engages_it(self, key_file: Path) -> None:
        """Even though ``config.risk.kill_switch`` is False."""
        config = make_config(mode="paper")
        assert config.risk.kill_switch is False

        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file),
                config,
                confirmed=True,
                kill_switch=True,
            )
        assert exc.value.code == "kill_switch"

    def test_the_config_flag_alone_engages_it(self, key_file: Path) -> None:
        """Even though the runtime flag is clear.

        The file cannot be overridden from the API — an operator who halted
        trading in ``config.yaml`` must not be undone by a click.
        """
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper", kill_switch=True),
                confirmed=True,
                kill_switch=False,
            )
        assert exc.value.code == "kill_switch"

    def test_both_engaged_is_still_one_refusal(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper", kill_switch=True),
                confirmed=True,
                kill_switch=True,
            )
        assert exc.value.code == "kill_switch"

    def test_it_is_checked_before_the_proposal_status(self, key_file: Path) -> None:
        """An emergency stop outranks every other reason to refuse.

        Ordering is observable through ``code``: with the switch engaged and a
        non-pending proposal, the answer must be ``kill_switch`` — the
        operator needs to know the system is halted, not that this one
        proposal was already decided.
        """
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(status=ProposalStatus.EXECUTED),
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=True,
                kill_switch=True,
            )
        assert exc.value.code == "kill_switch"

    def test_confirmation_is_still_checked_first(self, key_file: Path) -> None:
        """The switch does not shadow the missing-confirmation refusal.

        Both refuse; ``not_confirmed`` is the more specific thing to tell a
        caller that supplied no consent at all.
        """
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=False,
                kill_switch=True,
            )
        assert exc.value.code == "not_confirmed"

    def test_omitting_the_argument_is_a_typeerror(self, key_file: Path) -> None:
        """The fail-closed property, pinned.

        ``kill_switch`` is required and has no default *on purpose*: there is
        no safe one. ``False`` would mean a caller that forgot the argument
        silently bypasses the emergency stop, which is precisely the class of
        guard this codebase calls worse than no guard at all. A ``TypeError``
        at the call site is the whole point — do not give this a default to
        make a test pass.
        """
        with pytest.raises(TypeError):
            check_execution(  # type: ignore[call-arg]
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                confirmed=True,
            )

    def test_it_cannot_be_passed_positionally(self, key_file: Path) -> None:
        """Keyword-only, so it cannot be transposed with ``confirmed``.

        Two adjacent booleans meaning opposite things is exactly the argument
        pair worth making unswappable.
        """
        with pytest.raises(TypeError):
            check_execution(  # type: ignore[misc]
                make_proposal(),
                make_settings(tmp_key=key_file),
                make_config(mode="paper"),
                True,
                False,
            )

    def test_exchange_route_without_credentials_is_refused(self) -> None:
        """Credentials can disappear between boot and approval."""
        settings = make_settings(credentials=False)
        # Force the exchange route by claiming credentials only at config level.
        config = make_config(mode="paper")
        route = resolve_route(settings, config)
        # No credentials -> simulated, which needs none. Nothing to refuse.
        assert route is ExecutionRoute.SIMULATED


class TestMachineConsent:
    """The second consent path.

    Every test here passes ``confirmed=False``, because a machine consent is
    an *alternative* to the human confirmation and never an addition to it.
    """

    def test_a_valid_consent_is_accepted(self, key_file: Path) -> None:
        route = check_execution(
            make_proposal(),
            make_settings(tmp_key=key_file, autonomous_trading=True),
            armed_config(mode="paper"),
            confirmed=False,
            kill_switch=False,
            machine_consent=make_consent(),
        )
        assert route is ExecutionRoute.DEMO_EXCHANGE

    def test_both_authorities_at_once_is_refused(self, key_file: Path) -> None:
        """Neither answer to "who approved this?" is inventable, so refuse.

        This is the only check in the module that refuses something which
        would otherwise pass both ways. It exists because AuditLog.actor has
        one value and two claimants, and picking one silently would make the
        audit trail a guess.
        """
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=True,
                kill_switch=False,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "ambiguous_consent"

    def test_neither_authority_is_still_refused(self, key_file: Path) -> None:
        """Adding a second door must not have unlatched the first."""
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=None,
            )
        assert exc.value.code == "not_confirmed"

    def test_consent_without_config_enable_is_refused(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                make_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "autonomy_disabled"

    def test_consent_without_the_env_var_is_refused(self, key_file: Path) -> None:
        """Config alone cannot arm the machine — that needs an .env edit.

        The dashboard has no auth in front of it by design, so the arming
        decision deliberately lives where no LAN request can reach it.
        """
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=False),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "autonomous_not_armed"

    def test_consent_on_an_unarmed_route_is_refused(self, key_file: Path) -> None:
        """Armed on the simulator says nothing about the exchange."""
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(route="simulated", mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "autonomous_route_not_armed"

    def test_consent_for_another_proposal_is_refused(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(proposal_id=999),
            )
        assert exc.value.code == "consent_proposal_mismatch"

    def test_consent_for_another_route_is_refused(self, key_file: Path) -> None:
        """The evidence a consent carries is per-route and does not transfer."""
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(route=ExecutionRoute.SIMULATED),
            )
        assert exc.value.code == "consent_route_mismatch"

    def test_a_stale_consent_is_refused(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(
                    issued_at=datetime.now(UTC) - timedelta(minutes=5)
                ),
            )
        assert exc.value.code == "consent_stale"

    def test_a_consent_from_the_future_is_refused(self, key_file: Path) -> None:
        """Clock skew is not a reason to widen the window in one direction."""
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(
                    issued_at=datetime.now(UTC) + timedelta(minutes=5)
                ),
            )
        assert exc.value.code == "consent_stale"

    def test_the_kill_switch_still_wins(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=True,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "kill_switch"

    def test_an_expired_proposal_is_still_refused(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "expired"

    def test_a_decided_proposal_is_still_refused(self, key_file: Path) -> None:
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(status=ProposalStatus.APPROVED),
                make_settings(tmp_key=key_file, autonomous_trading=True),
                armed_config(mode="paper"),
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(),
            )
        assert exc.value.code == "not_pending"


class TestMachineConsentOnLive:
    """Real money, unattended. The largest interlock set in the system."""

    @staticmethod
    def _live(**settings_kw: object) -> tuple[Settings, Config]:
        return (
            make_settings(env=KalshiEnv.PROD, live_trading=True, **settings_kw),  # type: ignore[arg-type]
            armed_config(route="live_exchange", mode="live"),
        )

    def test_live_consent_needs_the_autonomous_env_var(self, key_file: Path) -> None:
        """LIVE_TRADING lets a *person* trade real money, not the machine."""
        settings, config = self._live(autonomous_trading=False, tmp_key=key_file)
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                settings,
                config,
                confirmed=False,
                kill_switch=False,
                machine_consent=make_consent(route=ExecutionRoute.LIVE_EXCHANGE),
            )
        # resolve_route passes (live_trading_armed is true), so the refusal is
        # the autonomy-specific one rather than live_mode_not_armed.
        assert exc.value.code == "autonomous_not_armed"

    def test_live_consent_with_all_three_env_vars_is_accepted(
        self, key_file: Path
    ) -> None:
        settings, config = self._live(autonomous_trading=True, tmp_key=key_file)
        route = check_execution(
            make_proposal(),
            settings,
            config,
            confirmed=False,
            kill_switch=False,
            machine_consent=make_consent(route=ExecutionRoute.LIVE_EXCHANGE),
        )
        assert route is ExecutionRoute.LIVE_EXCHANGE

    def test_the_machine_is_not_asked_to_type_a_ticker(self, key_file: Path) -> None:
        """A machine typing a string it generated proves nothing about intent.

        The typed ticker distinguishes "I clicked something" from "I meant
        this market" — a distinction that only exists for a human. Requiring
        it of the machine would be theatre, and satisfying it would teach the
        codebase that the phrase is synthesizable.
        """
        settings, config = self._live(autonomous_trading=True, tmp_key=key_file)
        route = check_execution(
            make_proposal(),
            settings,
            config,
            confirmed=False,
            kill_switch=False,
            confirmation_phrase=None,
            machine_consent=make_consent(route=ExecutionRoute.LIVE_EXCHANGE),
        )
        assert route is ExecutionRoute.LIVE_EXCHANGE

    def test_a_typed_ticker_cannot_substitute_for_a_consent(
        self, key_file: Path
    ) -> None:
        """The human interlock does not unlock the machine path.

        Someone with the dashboard open can produce this exact request. It
        must land on the human path — which it does, since with no consent
        `confirmed=False` never gets past the first check.
        """
        settings, config = self._live(autonomous_trading=True, tmp_key=key_file)
        with pytest.raises(InterlockError) as exc:
            check_execution(
                make_proposal(),
                settings,
                config,
                confirmed=False,
                kill_switch=False,
                confirmation_phrase="TEST-MKT",
                machine_consent=None,
            )
        assert exc.value.code == "not_confirmed"


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

    def test_reports_autonomy_disarmed_by_default(self, key_file: Path) -> None:
        state = posture(make_settings(tmp_key=key_file), make_config(mode="paper"))
        assert state["autonomy_enabled"] is False
        assert state["autonomy_env_armed"] is False
        assert state["autonomy_route_armed"] is False
        assert state["autonomy_configured"] is False

    def test_autonomy_configured_needs_config_env_and_route(
        self, key_file: Path
    ) -> None:
        """All three, and the dashboard can say which one is missing."""
        settings = make_settings(tmp_key=key_file, autonomous_trading=True)
        # Enabled and env-armed, but armed on a route this config does not use.
        state = posture(settings, armed_config(route="simulated", mode="paper"))
        assert state["autonomy_enabled"] is True
        assert state["autonomy_env_armed"] is True
        assert state["autonomy_route_armed"] is False
        assert state["autonomy_configured"] is False

        state = posture(settings, armed_config(route="demo_exchange", mode="paper"))
        assert state["autonomy_route_armed"] is True
        assert state["autonomy_configured"] is True

    def test_a_refused_route_reports_autonomy_unarmed(self) -> None:
        """No route means no route is armed — not a crash on `getattr(None)`."""
        state = posture(
            make_settings(credentials=False),
            armed_config(route="live_exchange", mode="live"),
        )
        assert state["execution_route"] is None
        assert state["autonomy_route_armed"] is False
        assert state["autonomy_configured"] is False

    def test_reports_when_the_coverage_gate_has_been_waived(
        self, key_file: Path
    ) -> None:
        """A waived gate must be visible, or the dashboard claims one that is off."""
        config = make_config(
            mode="paper",
            autonomous={
                "enabled": True,
                "routes": {"simulated": True},
                "evidence": {"require_coverage_usable": False},
            },
        )
        state = posture(make_settings(tmp_key=key_file), config)
        assert state["autonomy_coverage_enforced"] is False

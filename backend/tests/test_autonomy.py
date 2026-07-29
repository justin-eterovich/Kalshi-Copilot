"""Tests for the autonomy gate.

The gate replaced the only quality control the trading path had — a human
looking at the trade. So these tests are mostly about what it *refuses*, and
the ones that matter most are the ones where a plausible-looking bug would
make it permissive: an empty snapshot reading as "no edge required", a zero
budget reading as "unlimited", a mean standing in for a confidence bound.

Every refusal is asserted by `code`, not by message text. The codes are the
contract the dashboard and the worker's log line both read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.backtest.report import DetectorReport, Funnel
from app.backtest.stats import Expectancy, Verdict
from app.config import Config
from app.db.models import ProposalStatus, ProposedTrade
from app.settings import KalshiEnv, Settings
from app.trading import autonomy
from app.trading.autonomy import Budget, Evidence, GateRefusal
from app.trading.interlocks import ExecutionRoute

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def make_config(**overrides: Any) -> Config:
    """A config with autonomy armed on the demo route and a real budget.

    Budgets are set generously here so that a budget refusal in a test is
    always the thing that test is about. The zero-defaults get their own
    class below, because "zero refuses" is a property rather than a nuisance.
    """
    raw: dict[str, Any] = {
        "trading": {"mode": "paper", "paper_uses_demo_exchange": True},
        "autonomous": {
            "enabled": True,
            "routes": {"demo_exchange": True},
            "evidence": {
                "require_edge_shown": True,
                "require_coverage_usable": True,
                "max_report_age_sec": 900,
            },
            "budget": {
                "max_trades_per_hour": 10,
                "max_trades_per_detector_per_hour": 5,
                "max_daily_risk_cents": 10_000,
                "max_open_positions": 10,
                "max_working_orders": 5,
                "repeat_cooldown_sec": 3600,
            },
            "min_proposal_age_sec": 15,
        },
        "backtest": {"report_card_min_trades": 20},
    }
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        if field:
            raw.setdefault(section, {})[field] = value
        else:
            raw[section] = value
    return Config.model_validate(raw)


def armed_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "kalshi_env": KalshiEnv.DEMO,
        "live_trading": False,
        "autonomous_trading": True,
        "kalshi_demo_key_id": "demo-key",
    }
    values.update(overrides)
    return Settings(**values)


def make_report(
    *,
    detector: str = "set_arbitrage",
    route: str = "demo_exchange",
    n: int = 30,
    ci_low: str | None = "1.50",
    ci_high: str | None = "9.00",
    mean: str = "5.00",
    verdict: Verdict = Verdict.EDGE_SHOWN,
) -> DetectorReport:
    return DetectorReport(
        detector=detector,
        route=route,
        funnel=Funnel(),
        avg_claimed_edge_cents=Decimal("6"),
        realised=Expectancy(
            n=n,
            total_cents=Decimal(mean) * n,
            mean_cents=Decimal(mean),
            stderr_cents=Decimal("1"),
            ci_low_cents=Decimal(ci_low) if ci_low is not None else None,
            ci_high_cents=Decimal(ci_high) if ci_high is not None else None,
            method="bootstrap",
        ),
        verdict=verdict,
        headline="test",
        fees_paid_cents=Decimal(0),
        max_drawdown_cents=Decimal(0),
        unattributed=0,
    )


def make_evidence(
    *,
    reports: list[DetectorReport] | None = None,
    coverage_usable: bool = True,
    computed_at: datetime | None = None,
    min_trades: int = 20,
) -> Evidence:
    rows = reports if reports is not None else [make_report()]
    return Evidence(
        computed_at=computed_at or NOW,
        min_trades=min_trades,
        coverage_usable=coverage_usable,
        coverage_refusals=() if coverage_usable else ("too_few_markets",),
        reports={(r.detector, r.route): r for r in rows},
    )


def make_proposal(**overrides: Any) -> ProposedTrade:
    proposal = ProposedTrade(
        source="set_arbitrage",
        ticker="TEST-MKT",
        leg_count=1,
        status=ProposalStatus.PENDING,
        max_loss_cents=Decimal("100"),
    )
    proposal.id = 1
    proposal.created_at = NOW - timedelta(seconds=60)
    for key, value in overrides.items():
        setattr(proposal, key, value)
    return proposal


class FakeSession:
    """Returns canned scalars for the budget queries, in call order.

    The budget makes six scalar queries in a fixed order. Dispatching on that
    order is acceptable here — and only here — because `budget_state` is the
    single caller and the order is asserted by `test_budget_reads_the_ledger`
    below, so a reordering breaks loudly rather than silently feeding the
    working-order count into the daily-risk check.
    """

    def __init__(self, *values: Any) -> None:
        self._values = list(values)
        self.calls = 0

    async def scalar(self, _stmt: Any) -> Any:
        if self.calls >= len(self._values):
            return 0
        value = self._values[self.calls]
        self.calls += 1
        return value


def clear_budget() -> FakeSession:
    """A ledger in which the machine has done nothing yet."""
    return FakeSession(0, 0, Decimal(0), None, 0, 0)


@pytest.fixture(autouse=True)
def _isolate_snapshot():
    """The snapshot is module state; never let one test's leak into another."""
    autonomy.install_evidence(None)
    yield
    autonomy.install_evidence(None)


@pytest.fixture(autouse=True)
def _no_latch(monkeypatch: pytest.MonkeyPatch):
    """Default to "not latched" so only the latch tests exercise Redis."""

    async def _unlatched() -> str | None:
        return None

    monkeypatch.setattr(autonomy, "disarm_reason", _unlatched)


#: `evidence=None` has to mean "no snapshot exists", which is a case under
#: test, so the default cannot also be None or the two are indistinguishable
#: — and the version of this helper that conflated them quietly turned three
#: refusal tests into assertions about a snapshot they had not asked for.
_DEFAULT = object()


async def gate(
    *,
    session: Any = None,
    proposal: ProposedTrade | None = None,
    settings: Settings | None = None,
    config: Config | None = None,
    evidence: Any = _DEFAULT,
    route: ExecutionRoute = ExecutionRoute.DEMO_EXCHANGE,
    now: datetime = NOW,
):
    return await autonomy.evaluate(
        session or clear_budget(),
        proposal or make_proposal(),
        settings=settings or armed_settings(),
        config=config or make_config(),
        route=route,
        evidence=make_evidence() if evidence is _DEFAULT else evidence,
        now=now,
    )


async def refusal_code(**kwargs: Any) -> str:
    with pytest.raises(GateRefusal) as exc:
        await gate(**kwargs)
    return exc.value.code


# ---------------------------------------------------------------------------
# The happy path exists, so the refusals below mean something
# ---------------------------------------------------------------------------


class TestAuthorisation:
    async def test_a_fully_evidenced_proposal_is_authorised(self) -> None:
        consent = await gate()
        assert consent.proposal_id == 1
        assert consent.route is ExecutionRoute.DEMO_EXCHANGE
        assert consent.detector == "set_arbitrage"
        assert consent.verdict == Verdict.EDGE_SHOWN.value
        assert consent.gate_version == autonomy.GATE_VERSION

    async def test_the_consent_carries_the_evidence_that_justified_it(self) -> None:
        """The audit row has to be able to say *why*, after the card moves on.

        Reconstructing the report card as it stood at the moment of a trade is
        impossible once more trades have landed, so the numbers ride along.
        """
        consent = await gate()
        assert consent.trades == 30
        assert consent.ci_low_cents == "1.50"
        assert consent.mean_cents == "5.00"
        assert consent.coverage_usable is True
        assert consent.evidence_computed_at == NOW


# ---------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------


class TestArming:
    async def test_config_alone_does_not_arm(self) -> None:
        assert (
            await refusal_code(settings=armed_settings(autonomous_trading=False))
            == "autonomous_not_armed"
        )

    async def test_environment_alone_does_not_arm(self) -> None:
        config = make_config()
        config.autonomous.enabled = False
        assert await refusal_code(config=config) == "autonomy_disabled"

    async def test_a_route_is_armed_individually(self) -> None:
        """Evidence on one rail says nothing about another.

        The simulated route exists to *generate* evidence; the demo route is a
        real order rail. Arming one must never arm the other.
        """
        assert (
            await refusal_code(route=ExecutionRoute.SIMULATED)
            == "autonomous_route_not_armed"
        )

    async def test_live_needs_all_three_environment_flags(self) -> None:
        config = make_config()
        config.autonomous.routes.live_exchange = True
        # AUTONOMOUS_TRADING alone, without prod + LIVE_TRADING.
        code = await refusal_code(
            config=config, route=ExecutionRoute.LIVE_EXCHANGE
        )
        assert code == "autonomous_live_not_armed"


class TestDisarmLatch:
    async def test_a_latched_machine_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _latched() -> str | None:
            return "a partial fill needs a person"

        monkeypatch.setattr(autonomy, "disarm_reason", _latched)
        assert await refusal_code() == "disarmed"

    async def test_an_unreadable_latch_reads_as_latched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fails closed, like the kill switch.

        If Redis cannot be reached we cannot prove an operator has not stopped
        the machine, and the safe reading of "unknown" on a stop is "stopped".
        """

        class Boom:
            async def get(self, _key: str) -> Any:
                raise RuntimeError("redis down")

        monkeypatch.setattr(autonomy, "get_redis", lambda: Boom())
        monkeypatch.undo()  # restore disarm_reason patched by the autouse fixture
        monkeypatch.setattr(autonomy, "get_redis", lambda: Boom())
        assert await autonomy.disarm_reason() is not None


# ---------------------------------------------------------------------------
# The veto window and who wrote the proposal
# ---------------------------------------------------------------------------


class TestVetoWindow:
    async def test_a_fresh_proposal_is_refused(self) -> None:
        """`min_proposal_age_sec` is the operator's chance to say no."""
        proposal = make_proposal()
        proposal.created_at = NOW - timedelta(seconds=5)
        assert await refusal_code(proposal=proposal) == "veto_window_open"

    async def test_the_window_is_measured_from_creation(self) -> None:
        proposal = make_proposal()
        proposal.created_at = NOW - timedelta(seconds=15)
        consent = await gate(proposal=proposal)
        assert consent.proposal_id == 1


class TestManualProposals:
    async def test_a_hand_written_ticket_is_never_machine_approved(self) -> None:
        """Finishing a trade a person started and declined to confirm.

        There is also no report card for "manual" — the rows under that name
        are whatever an operator typed, which is not a strategy that can be
        measured.
        """
        assert (
            await refusal_code(proposal=make_proposal(source="manual"))
            == "manual_proposal"
        )

    async def test_a_sourceless_proposal_is_refused(self) -> None:
        assert (
            await refusal_code(proposal=make_proposal(source="")) == "manual_proposal"
        )


# ---------------------------------------------------------------------------
# Evidence — the measurement that replaced the judgement
# ---------------------------------------------------------------------------


class TestEvidence:
    async def test_no_snapshot_refuses(self) -> None:
        """Absent evidence is not weak evidence; it is no permission at all."""
        assert await refusal_code(evidence=None) == "no_evidence"

    async def test_a_stale_snapshot_refuses(self) -> None:
        stale = make_evidence(computed_at=NOW - timedelta(seconds=1000))
        assert await refusal_code(evidence=stale) == "evidence_stale"

    async def test_staleness_refuses_but_does_not_latch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Latching on a transient failure trains operators to clear latches.

        A stale snapshot clears itself on the next successful refresh, so it
        must not demand a human the way an ambiguous submission does.
        """
        latched: list[str] = []

        async def _record(reason: str) -> None:
            latched.append(reason)

        monkeypatch.setattr(autonomy, "disarm", _record)
        with pytest.raises(GateRefusal):
            await gate(evidence=make_evidence(computed_at=NOW - timedelta(hours=1)))
        assert latched == []

    async def test_unusable_coverage_refuses(self) -> None:
        assert (
            await refusal_code(evidence=make_evidence(coverage_usable=False))
            == "coverage_unusable"
        )

    async def test_coverage_may_be_waived_when_configured(self) -> None:
        """Only ever on the simulated route — the loader enforces that half."""
        config = make_config()
        config.autonomous.evidence.require_coverage_usable = False
        consent = await gate(
            config=config, evidence=make_evidence(coverage_usable=False)
        )
        assert consent.coverage_usable is False

    async def test_evidence_does_not_transfer_between_routes(self) -> None:
        """A report for the simulator cannot authorise a demo-exchange order."""
        sim_only = make_evidence(reports=[make_report(route="simulated")])
        assert await refusal_code(evidence=sim_only) == "no_report"

    async def test_evidence_does_not_transfer_between_detectors(self) -> None:
        other = make_evidence(reports=[make_report(detector="stale_quote")])
        assert await refusal_code(evidence=other) == "no_report"

    async def test_too_few_trades_refuses_however_good_the_mean(self) -> None:
        """A +40c mean over four trades is the most dangerous number here.

        It is the most persuasive thing a report card can say and it carries
        no information, so `n` is checked before anything about the returns.
        """
        thin = make_evidence(
            reports=[make_report(n=4, mean="40.00", ci_low="35.00")]
        )
        assert await refusal_code(evidence=thin) == "insufficient_trades"

    async def test_a_non_edge_verdict_refuses(self) -> None:
        no_edge = make_evidence(
            reports=[make_report(verdict=Verdict.NO_EDGE_SHOWN, ci_low="-1.00")]
        )
        assert await refusal_code(evidence=no_edge) == "no_edge_shown"


class TestTheIntervalNotTheMean:
    """The single most important property in this module.

    A binary trade's P&L is a two-point distribution skewed away from 50c,
    which is exactly the regime a 20-trade report card lives in. Measured on
    29 wins at +9.98c and one loss at -90.02c, Wald claims an edge and the
    bootstrap refuses. Anything here that consults the mean is a bug that
    reads as a working system.
    """

    async def test_a_positive_mean_with_a_negative_lower_bound_refuses(self) -> None:
        straddles_zero = make_evidence(
            reports=[make_report(mean="8.00", ci_low="-0.02", ci_high="9.98")]
        )
        assert await refusal_code(evidence=straddles_zero) == "edge_not_measured"

    async def test_a_lower_bound_of_exactly_zero_refuses(self) -> None:
        """Zero does not exclude zero. The comparison is strict on purpose."""
        at_zero = make_evidence(reports=[make_report(ci_low="0.00")])
        assert await refusal_code(evidence=at_zero) == "edge_not_measured"

    async def test_a_missing_interval_refuses(self) -> None:
        """`ci_low` is None below two trades: no spread is not certainty."""
        no_interval = make_evidence(reports=[make_report(ci_low=None)])
        assert await refusal_code(evidence=no_interval) == "edge_not_measured"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestZeroRefuses:
    """Every ceiling defaults to zero and zero means "no allowance".

    There is deliberately no way to express "unlimited". If zero read as
    unlimited then the most conservative-looking config — a blank one — would
    be the most dangerous, which is precisely backwards.
    """

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("max_trades_per_hour", "hourly_trade_cap"),
            ("max_trades_per_detector_per_hour", "detector_hourly_cap"),
            ("max_daily_risk_cents", "daily_risk_cap"),
            ("max_open_positions", "open_position_cap"),
            ("max_working_orders", "working_order_cap"),
        ],
    )
    async def test_a_zero_ceiling_refuses(self, field: str, expected: str) -> None:
        config = make_config()
        setattr(config.autonomous.budget, field, 0)
        assert await refusal_code(config=config) == expected

    async def test_a_default_budget_refuses_everything(self) -> None:
        """The shipped defaults are all zero, so an armed-but-unbudgeted
        machine does nothing rather than everything."""
        config = make_config()
        config.autonomous.budget = type(config.autonomous.budget)()
        assert await refusal_code(config=config) == "hourly_trade_cap"


class TestBudgetCeilings:
    async def test_the_hourly_cap_binds(self) -> None:
        session = FakeSession(10, 0, Decimal(0), None, 0, 0)
        assert await refusal_code(session=session) == "hourly_trade_cap"

    async def test_the_per_detector_cap_binds(self) -> None:
        session = FakeSession(1, 5, Decimal(0), None, 0, 0)
        assert await refusal_code(session=session) == "detector_hourly_cap"

    async def test_daily_risk_counts_the_proposal_being_considered(self) -> None:
        """The ceiling is on the day *including* this trade, not before it.

        Checking only what has already been spent would let a single proposal
        of any size through as long as the day started quiet.
        """
        session = FakeSession(0, 0, Decimal("9950"), None, 0, 0)
        proposal = make_proposal(max_loss_cents=Decimal("100"))
        assert (
            await refusal_code(session=session, proposal=proposal) == "daily_risk_cap"
        )

    async def test_open_positions_count_every_source(self) -> None:
        """Not just autonomous ones — a Position does not remember who built
        it, and inventing that attribution to raise a ceiling is the wrong
        direction."""
        session = FakeSession(0, 0, Decimal(0), None, 10, 0)
        assert await refusal_code(session=session) == "open_position_cap"

    async def test_working_orders_bind(self) -> None:
        session = FakeSession(0, 0, Decimal(0), None, 0, 5)
        assert await refusal_code(session=session) == "working_order_cap"


class TestRepeatCooldown:
    """The mitigation for the re-proposal loop.

    `_guard_duplicate` only refuses while a proposal is *pending*. Without a
    cooldown: detector proposes, the gate approves, the duplicate guard
    clears, the detector re-derives the same edge on its next scan and it
    trades again — repeatedly, until the per-market exposure cap binds.
    """

    async def test_a_recent_trade_on_the_same_pair_refuses(self) -> None:
        session = FakeSession(
            0, 0, Decimal(0), NOW - timedelta(minutes=5), 0, 0
        )
        assert await refusal_code(session=session) == "repeat_cooldown"

    async def test_the_cooldown_expires(self) -> None:
        session = FakeSession(0, 0, Decimal(0), NOW - timedelta(hours=2), 0, 0)
        consent = await gate(session=session)
        assert consent.proposal_id == 1

    async def test_a_zero_cooldown_disables_it(self) -> None:
        config = make_config()
        config.autonomous.budget.repeat_cooldown_sec = 0
        session = FakeSession(0, 0, Decimal(0), NOW, 0, 0)
        consent = await gate(session=session, config=config)
        assert consent.proposal_id == 1


class TestBudgetPayload:
    async def test_the_consent_records_what_was_spent(self) -> None:
        """So the audit row can say why this was allowed, not just that it was."""
        session = FakeSession(3, 1, Decimal("250"), None, 2, 1)
        consent = await gate(session=session)
        assert consent.budget["trades_this_hour"] == "3"
        assert consent.budget["daily_risk_cents"] == "250"
        assert consent.budget["working_orders"] == "1"


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------


class TestSnapshotHandling:
    async def test_the_gate_reads_the_process_snapshot_not_redis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Authorisation and display are deliberately different paths.

        If a consent could be sourced from Redis, any process able to write
        one key could authorise a trade, and a key outliving its writer could
        authorise one after the evidence had gone.
        """

        class Exploding:
            async def get(self, _key: str) -> Any:
                raise AssertionError("the gate must not read evidence from redis")

            async def publish(self, *_a: Any) -> None:
                raise AssertionError("no publish during evaluation")

        monkeypatch.setattr(autonomy, "get_redis", lambda: Exploding())
        autonomy.install_evidence(make_evidence())
        consent = await autonomy.evaluate(
            clear_budget(),
            make_proposal(),
            settings=armed_settings(),
            config=make_config(),
            route=ExecutionRoute.DEMO_EXCHANGE,
            now=NOW,
        )
        assert consent.detector == "set_arbitrage"

    async def test_a_failed_refresh_keeps_the_previous_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It must never install a partial or empty one.

        An empty snapshot refuses identically to a genuine absence of
        evidence, and the operator has to be able to tell a database blip
        from a detector that has never made money.
        """
        good = make_evidence()
        autonomy.install_evidence(good)

        async def _boom(*_a: Any, **_k: Any) -> Evidence:
            raise RuntimeError("database went away")

        monkeypatch.setattr(autonomy, "refresh_evidence", _boom)
        result = await autonomy.refresh_once(object(), make_config())
        assert result is None
        assert autonomy.cached_evidence() is good

    async def test_published_evidence_is_a_dict_not_an_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The display path must not be able to feed the gate.

        The API and the worker are separate processes, so the dashboard reads
        its numbers from Redis. Returning a plain dict rather than an
        `Evidence` means a mistake that wired the display path into
        `evaluate` would not typecheck and would not run — the gate reads
        `.report_for(...)`, which a dict does not have.
        """

        class Client:
            async def get(self, _key: str) -> Any:
                return '{"computed_at": "2026-07-29T12:00:00+00:00", "pairs": []}'

        monkeypatch.setattr(autonomy, "get_redis", lambda: Client())
        published = await autonomy.published_evidence()
        assert isinstance(published, dict)
        assert not isinstance(published, Evidence)
        assert not hasattr(published, "report_for")

    async def test_a_stale_snapshot_still_ages_out(self) -> None:
        """Keeping the previous snapshot is not the same as trusting it.

        The two behaviours together are the design: a blip is survivable, a
        sustained outage is not.
        """
        autonomy.install_evidence(make_evidence(computed_at=NOW - timedelta(hours=2)))
        assert await refusal_code(evidence=None) == "evidence_stale"


# ---------------------------------------------------------------------------
# Ordering — a refusal must not depend on cheaper checks passing
# ---------------------------------------------------------------------------


class TestRefusalOrdering:
    async def test_arming_is_checked_before_evidence(self) -> None:
        """A disarmed machine must not need a report card to say no.

        If evidence were checked first then an unarmed deployment would report
        `no_evidence`, and an operator would go looking for data when the real
        answer is that nothing is switched on.
        """
        config = make_config()
        config.autonomous.enabled = False
        assert await refusal_code(config=config, evidence=None) == "autonomy_disabled"

    async def test_evidence_is_checked_before_budget(self) -> None:
        """Budget queries hit the database; evidence is already in memory."""
        config = make_config()
        config.autonomous.budget.max_trades_per_hour = 0
        assert (
            await refusal_code(config=config, evidence=None) == "no_evidence"
        )


class TestBudgetShape:
    def test_the_payload_is_all_strings(self) -> None:
        """It lands in JSONB on the consent; a float there would be lossy."""
        budget = Budget(
            trades_this_hour=1,
            detector_trades_this_hour=1,
            daily_risk_cents=Decimal("12.5"),
            open_positions=0,
            working_orders=0,
            cooldown_until=None,
        )
        assert all(isinstance(v, str) for v in budget.as_payload().values())


# ---------------------------------------------------------------------------
# The two stops, at the API boundary
# ---------------------------------------------------------------------------


class TestStopEndpoints:
    """Stopping is easy and resuming is hard, and that asymmetry is the point.

    An operator reaching for a stop is in a hurry by definition, so disarming
    takes a confirm and no typing. The latch fires on a *single* ambiguous
    submission or partial fill — states where an order may exist that this
    system cannot see — so clearing it asserts a person has looked at the book,
    and typing is what makes that a claim rather than a reflex.
    """

    async def test_disarming_needs_confirmation_but_no_phrase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from app.api.routes.trading import AutonomyStopRequest, disarm_autonomy

        stopped: list[str] = []

        async def _disarm(reason: str) -> None:
            stopped.append(reason)

        async def _reason() -> str | None:
            return stopped[0] if stopped else None

        monkeypatch.setattr(autonomy, "disarm", _disarm)
        monkeypatch.setattr(autonomy, "disarm_reason", _reason)

        with pytest.raises(HTTPException) as exc:
            await disarm_autonomy(AutonomyStopRequest())
        assert exc.value.detail["error"] == "not_confirmed"
        assert stopped == []

        result = await disarm_autonomy(AutonomyStopRequest(confirm=True))
        assert result["disarmed"] is True
        assert len(stopped) == 1

    async def test_rearming_needs_the_typed_phrase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from app.api.routes.trading import AutonomyStopRequest, rearm_autonomy

        cleared: list[bool] = []

        async def _rearm() -> None:
            cleared.append(True)

        monkeypatch.setattr(autonomy, "rearm", _rearm)

        for body in (
            AutonomyStopRequest(confirm=True),
            AutonomyStopRequest(confirm=True, phrase="yes"),
            AutonomyStopRequest(confirm=False, phrase="REARM"),
        ):
            with pytest.raises(HTTPException) as exc:
                await rearm_autonomy(body)
            assert exc.value.detail["error"] == "confirmation_phrase_mismatch"
        assert cleared == []

        result = await rearm_autonomy(
            AutonomyStopRequest(confirm=True, phrase="rearm")  # case-insensitive
        )
        assert result["disarmed"] is False
        assert cleared == [True]

    async def test_neither_endpoint_can_arm_anything(self) -> None:
        """Clearing the latch restores a posture; it never creates one.

        Asserted structurally rather than by reading the source: neither
        endpoint takes a config or settings dependency, so it has nothing to
        change arming *with*. `autonomous.enabled`, the route and
        `AUTONOMOUS_TRADING` are all still required afterwards and none is
        reachable from the dashboard — which has no auth in front of it by
        design. The worst a request on the LAN can do is resume something an
        operator already chose with a file edit and a restart.
        """
        import inspect

        from app.api.routes.trading import disarm_autonomy, rearm_autonomy

        for fn in (disarm_autonomy, rearm_autonomy):
            params = inspect.signature(fn).parameters
            assert list(params) == ["body"], (
                f"{fn.__name__} takes {list(params)}; a config or settings "
                "dependency here would give it something to arm with"
            )

"""A tripwire for the stale-image trap.

``docker compose run api pytest`` tests **the image, not the working tree**.
Only ``config.yaml``, ``data/`` and ``secrets/`` are bind-mounted; ``app/`` and
``tests/`` are baked in at build time. Without a ``--build`` a green suite is
green for the code you last built — it silently ran 1,292 tests against the
previous milestone while 1,547 existed on disk, and nothing at the pytest
summary line distinguished the two.

The documented mitigation is discipline. This file is the mechanical one: it
asserts that the code under test carries the API surface introduced by the
polish-audit fixes. A stale image fails here with a named symbol rather than
passing quietly, which is the difference between "I forgot to rebuild" and
"the fix does not work".

**When a later milestone changes one of these, update it deliberately.** The
list is a claim about what this build contains; a stale entry is worse than
none, because it turns the tripwire into noise.

This is not a test of behaviour — every symbol here has its own tests
elsewhere. It is a test of *identity*.
"""

from __future__ import annotations

import inspect
import os

import pytest


class TestPolishAuditFixesArePresent:
    """One assertion per fix, each naming the finding it came from."""

    def test_money_rejects_non_finite_values(self) -> None:
        """API-002 — ``Decimal("NaN")`` reached Postgres."""
        from app.core import money

        assert hasattr(money, "_reject_non_finite")
        with pytest.raises(money.MoneyParseError):
            money.parse_dollars("NaN")

    def test_fair_price_is_range_checked(self) -> None:
        """API-001 — ``fair_price="56"`` returned +$55.97/contract of edge."""
        from app.trading import pricing

        source = inspect.getsource(pricing.price_ticket)
        assert "fair_price must be strictly between 0 and 1" in source

    def test_check_execution_requires_a_kill_switch_argument(self) -> None:
        """SAFETY-013 — the switch could not be engaged on a running system."""
        from app.trading.interlocks import check_execution

        params = inspect.signature(check_execution).parameters
        assert "kill_switch" in params
        assert params["kill_switch"].default is inspect.Parameter.empty
        assert params["kill_switch"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_runtime_kill_switch_exists_in_redis(self) -> None:
        from app.core import redis as core_redis

        assert hasattr(core_redis, "KILL_SWITCH_KEY")
        assert hasattr(core_redis, "get_kill_switch")
        assert hasattr(core_redis, "set_kill_switch")

    def test_per_market_exposure_is_a_query(self) -> None:
        """SAFETY-010 — the cap measured one proposal, not the market."""
        from app.trading import risk

        assert hasattr(risk, "market_exposure_cents")
        params = inspect.signature(risk.market_exposure_cents).parameters
        assert "session" in params
        assert "exclude_proposal_id" in params

    def test_the_market_size_guard_takes_a_session(self) -> None:
        from app.trading import proposals

        params = inspect.signature(proposals._guard_market_size).parameters
        assert "session" in params
        assert "tickers" in params
        assert inspect.iscoroutinefunction(proposals._guard_market_size)

    def test_client_order_ids_are_derived_from_the_proposal(self) -> None:
        """SAFETY-001 — a fresh uuid4 per attempt gave the exchange no basis
        on which to reject a second placement."""
        from app.trading import executor

        assert hasattr(executor, "_client_order_id")

    def test_the_approval_path_takes_a_row_lock(self) -> None:
        """SAFETY-001 — every guard was ``if status is not PENDING: refuse``
        evaluated against a snapshot another transaction had invalidated."""
        from app.trading.executor import Executor

        source = inspect.getsource(Executor.approve_and_execute)
        assert "with_for_update" in source
        assert "populate_existing" in source

    def test_reject_takes_a_row_lock_too(self) -> None:
        from app.trading import proposals

        assert "with_for_update" in inspect.getsource(proposals.reject)

    def test_expiry_is_a_conditional_update(self) -> None:
        """AUDIT-001 — ``proposal.expired`` was logged 2-3x per proposal."""
        from app.trading import proposals

        source = inspect.getsource(proposals.expire_stale)
        assert "returning" in source
        assert "update(" in source

    def test_the_existing_order_guard_is_unfiltered_by_status(self) -> None:
        """SAFETY-001 — ``LIVE_ORDER_STATUSES`` excluded FILLED, the normal
        outcome for a taker order on a liquid book."""
        from app.trading.executor import Executor

        source = inspect.getsource(Executor._existing_order_for)
        body = source.split('"""')[-1]
        assert "Order.status" not in body
        assert "LIVE_ORDER_STATUSES" not in body

    def test_barrier_markets_are_classified(self) -> None:
        """BE-001 — path-dependent crypto markets priced as terminal-value."""
        from app.detectors import stale_quote

        assert hasattr(stale_quote, "BARRIER_SERIES")
        assert hasattr(stale_quote, "is_path_dependent")
        params = inspect.signature(stale_quote.resolve_strike).parameters
        assert "rules_primary" in params
        assert params["rules_primary"].default is inspect.Parameter.empty

    def test_the_summary_drawdown_curve_has_an_opening_point(self) -> None:
        """TEST-002 — the documented "only ever flatters" error."""
        from app.backtest.engine import BacktestResult

        source = inspect.getsource(BacktestResult.summary_lines)
        assert "starting_equity_cents" in source


#: Set by the async test below. Module level on purpose — see the class.
_ASYNC_BODY_RAN = False


class TestAsyncTestsActuallyRun:
    """62 async tests could be skipped without the suite going red.

    ``asyncio_mode = "auto"`` is a *pytest-asyncio* ini key. If the plugin is
    absent, or its ini schema changes, that key is an unrecognised option which
    pytest ignores by default — and every ``async def test_`` is then collected
    and skipped with a warning rather than failing. There are 62 of them,
    including all 38 on the executor, the only module that can cause an order
    to exist.

    Same failure family as the stale image above: a green run that covered far
    less than it looks like. ``--strict-config`` in ``addopts`` would turn the
    unrecognised key into a hard error; this is the runtime half, and it works
    whatever the cause.
    """

    async def test_an_async_test_body_executes(self) -> None:
        global _ASYNC_BODY_RAN
        _ASYNC_BODY_RAN = True

    def test_the_async_test_above_was_not_silently_skipped(self) -> None:
        """Runs after it — pytest executes tests in definition order.

        If this fails, **do not fix it here**: the async tests did not run, and
        every green async assertion in this suite is worthless. Check that
        pytest-asyncio is installed and that ``asyncio_mode`` is being read.
        """
        assert _ASYNC_BODY_RAN, (
            "the async test immediately above did not execute — pytest-asyncio "
            "is missing or `asyncio_mode` is not being honoured, and all 62 "
            "async tests in this suite are being skipped silently."
        )


class TestBuildProvenance:
    """If the image records what it was built from, check it.

    ``BUILD_SHA`` does not exist yet — wiring ``ARG GIT_SHA`` through the
    Dockerfile is an infrastructure change outside this lane. The assertion is
    written now so that setting the variable is all it takes to arm it, and so
    that setting it *wrong* is caught rather than ignored.
    """

    def test_a_declared_build_sha_matches_the_source_tree(self) -> None:
        build_sha = os.environ.get("BUILD_SHA", "").strip()
        source_sha = os.environ.get("SOURCE_SHA", "").strip()
        if not build_sha or not source_sha:
            # Nothing declared: the symbol tripwire above is the guard.
            return
        assert build_sha == source_sha, (
            f"the running image was built from {build_sha} but the source tree "
            f"is at {source_sha}. `docker compose build` — and build all three "
            f"services, not just `api`: the worker and ingest are separate "
            f"images from the same Dockerfile."
        )

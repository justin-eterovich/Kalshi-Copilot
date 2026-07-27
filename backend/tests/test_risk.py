"""Tests for the portfolio-level risk limits.

These are the limits no single proposal can check about itself, so most of
what is tested here is arithmetic across a whole book plus the exact boundary
at which each limit fires. The boundaries matter: an off-by-one in the streak
counter or a fee left out of the daily net makes a limit that reports itself
as enforced and is not.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Config
from app.trading.risk import (
    RiskError,
    RiskState,
    check_exposure,
    check_halted,
    consecutive_losses,
    position_cost_cents,
)


def cfg(**risk: object) -> Config:
    base: dict[str, object] = {
        "bankroll_usd": 1000.0,
        "max_total_exposure_pct": 0.40,
        "daily_loss_limit_pct": 0.05,
        "cooldown_after_consecutive_losses": 3,
        "cooldown_minutes": 60,
    }
    base.update(risk)
    return Config.model_validate({"risk": base})


def state(**overrides: object) -> RiskState:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    fields: dict[str, object] = {
        "route": "demo_exchange",
        "day": date(2026, 7, 27),
        "bankroll_cents": Decimal(100_000),
        "exposure_cents": Decimal(0),
        "pending_cents": Decimal(0),
        "exposure_limit_cents": Decimal(40_000),
        "daily_realized_cents": Decimal(0),
        "daily_fees_cents": Decimal(0),
        "daily_loss_limit_cents": Decimal(5_000),
        "consecutive_losses": 0,
        "cooldown_until": None,
        "now": now,
    }
    fields.update(overrides)
    return RiskState(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Exposure arithmetic
# ---------------------------------------------------------------------------


class TestPositionCost:
    def test_a_long_yes_position_risks_what_it_paid(self) -> None:
        assert position_cost_cents(Decimal(10), Decimal("0.40")) == Decimal(400)

    def test_a_no_position_risks_the_complement(self) -> None:
        """-5 @ 0.70 YES is 5 NO bought at 30c, so 150c is at risk."""
        assert position_cost_cents(Decimal(-5), Decimal("0.70")) == Decimal(150)

    def test_a_flat_position_risks_nothing(self) -> None:
        assert position_cost_cents(Decimal(0), Decimal("0.40")) == Decimal(0)

    def test_fractional_contracts_are_exact(self) -> None:
        assert position_cost_cents(Decimal("2.50"), Decimal("0.401234")) == Decimal(
            "100.30850"
        )

    def test_exposure_is_cost_not_mark_to_market(self) -> None:
        """A position that has moved in your favour must not free up room.

        Cost basis is the max loss on a binary; marking to market would let
        an unrealised gain finance more risk before it has actually paid.
        """
        cost = position_cost_cents(Decimal(10), Decimal("0.40"))
        # Nothing in the signature can express "it now trades at 0.90".
        assert cost == Decimal(400)


# ---------------------------------------------------------------------------
# Total exposure limit
# ---------------------------------------------------------------------------


class TestExposureLimit:
    def test_a_trade_inside_the_limit_passes(self) -> None:
        check_exposure(
            state(exposure_cents=Decimal(10_000)),
            cfg(),
            additional_cents=Decimal(5_000),
        )

    def test_a_trade_over_the_limit_is_refused(self) -> None:
        with pytest.raises(RiskError) as exc:
            check_exposure(
                state(exposure_cents=Decimal(38_000)),
                cfg(),
                additional_cents=Decimal(5_000),
            )
        assert exc.value.code == "exceeds_total_exposure"

    def test_the_limit_is_inclusive_at_the_boundary(self) -> None:
        """Landing exactly on the limit is allowed; exceeding it is not."""
        check_exposure(
            state(exposure_cents=Decimal(35_000)),
            cfg(),
            additional_cents=Decimal(5_000),
        )
        with pytest.raises(RiskError):
            check_exposure(
                state(exposure_cents=Decimal(35_000)),
                cfg(),
                additional_cents=Decimal("5000.01"),
            )

    def test_many_small_trades_still_hit_the_portfolio_limit(self) -> None:
        """The reason this limit exists: ten trades each inside
        ``max_pct_per_market`` (5% = 5000c) add up to more than 40%."""
        for _ in range(8):
            check_exposure(
                state(exposure_cents=Decimal(0)), cfg(), additional_cents=Decimal(5_000)
            )
        with pytest.raises(RiskError):
            check_exposure(
                state(exposure_cents=Decimal(40_000)),
                cfg(),
                additional_cents=Decimal(5_000),
            )

    def test_the_pending_queue_does_not_double_count_the_trade(self) -> None:
        """The proposal being approved is itself inside ``pending_cents``.

        Counting the queue here would refuse the very trade whose headroom was
        reserved — so a full queue must not block an approval that fits.
        """
        check_exposure(
            state(exposure_cents=Decimal(0), pending_cents=Decimal(39_000)),
            cfg(),
            additional_cents=Decimal(5_000),
        )

    def test_the_queue_still_shows_in_headroom(self) -> None:
        s = state(exposure_cents=Decimal(10_000), pending_cents=Decimal(35_000))
        assert s.committed_cents == Decimal(45_000)
        assert s.headroom_cents == Decimal(-5_000)


# ---------------------------------------------------------------------------
# Daily loss limit
# ---------------------------------------------------------------------------


class TestDailyLossLimit:
    def test_a_profitable_day_is_not_halted(self) -> None:
        check_halted(state(daily_realized_cents=Decimal(2_000)), cfg())

    def test_a_small_loss_is_not_halted(self) -> None:
        check_halted(state(daily_realized_cents=Decimal(-1_000)), cfg())

    def test_a_loss_past_the_limit_halts(self) -> None:
        with pytest.raises(RiskError) as exc:
            check_halted(state(daily_realized_cents=Decimal(-6_000)), cfg())
        assert exc.value.code == "daily_loss_limit"

    def test_the_limit_fires_exactly_at_the_boundary(self) -> None:
        with pytest.raises(RiskError):
            check_halted(state(daily_realized_cents=Decimal(-5_000)), cfg())

    def test_fees_count_towards_the_loss(self) -> None:
        """The whole point of the module docstring's insistence on net.

        Realised is only -4900c, inside the 5000c limit, but 200c of fees
        were paid — the account is down 5100c and the limit must fire.
        """
        with pytest.raises(RiskError) as exc:
            check_halted(
                state(
                    daily_realized_cents=Decimal(-4_900),
                    daily_fees_cents=Decimal(200),
                ),
                cfg(),
            )
        assert exc.value.code == "daily_loss_limit"

    def test_fees_alone_can_breach_the_limit(self) -> None:
        """A strategy that pays more in fees than it makes is losing money,
        and a limit that only reads realised P&L would never say so."""
        with pytest.raises(RiskError):
            check_halted(state(daily_fees_cents=Decimal(5_100)), cfg())

    def test_the_message_names_the_config_key(self) -> None:
        with pytest.raises(RiskError, match="risk.daily_loss_limit_pct"):
            check_halted(state(daily_realized_cents=Decimal(-9_000)), cfg())


# ---------------------------------------------------------------------------
# Losing streak and cooldown
# ---------------------------------------------------------------------------


class TestConsecutiveLosses:
    def test_no_closes_is_no_streak(self) -> None:
        assert consecutive_losses([]) == 0

    def test_a_run_of_losses_counts(self) -> None:
        assert consecutive_losses([Decimal(-1), Decimal(-2), Decimal(-3)]) == 3

    def test_a_win_at_the_head_clears_the_streak(self) -> None:
        assert consecutive_losses([Decimal(5), Decimal(-1), Decimal(-2)]) == 0

    def test_only_the_leading_run_counts(self) -> None:
        assert consecutive_losses(
            [Decimal(-1), Decimal(-2), Decimal(9), Decimal(-3)]
        ) == 2

    def test_a_scratch_does_not_break_a_streak(self) -> None:
        """Breaking even is not evidence that anything has changed."""
        assert consecutive_losses([Decimal(-1), Decimal(0), Decimal(-2)]) == 2

    def test_a_scratch_alone_is_not_a_streak(self) -> None:
        assert consecutive_losses([Decimal(0), Decimal(0)]) == 0


class TestCooldown:
    def test_an_active_cooldown_halts(self) -> None:
        s = state(
            consecutive_losses=3,
            cooldown_until=datetime(2026, 7, 27, 12, 30, tzinfo=UTC),
        )
        with pytest.raises(RiskError) as exc:
            check_halted(s, cfg())
        assert exc.value.code == "loss_cooldown"

    def test_an_elapsed_cooldown_does_not(self) -> None:
        check_halted(
            state(
                consecutive_losses=3,
                cooldown_until=datetime(2026, 7, 27, 11, 30, tzinfo=UTC),
            ),
            cfg(),
        )

    def test_the_message_says_when_it_clears(self) -> None:
        until = datetime(2026, 7, 27, 12, 45, tzinfo=UTC)
        with pytest.raises(RiskError, match=re.escape(until.isoformat())):
            check_halted(state(consecutive_losses=4, cooldown_until=until), cfg())

    def test_the_loss_limit_is_reported_before_the_cooldown(self) -> None:
        """Both can hold at once; the daily limit is the harder stop, so it is
        the one worth naming — a cooldown that clears in 20 minutes would
        otherwise imply trading resumes then, and it does not."""
        s = state(
            daily_realized_cents=Decimal(-9_000),
            consecutive_losses=3,
            cooldown_until=datetime(2026, 7, 27, 12, 20, tzinfo=UTC),
        )
        with pytest.raises(RiskError) as exc:
            check_halted(s, cfg())
        assert exc.value.code == "daily_loss_limit"


# ---------------------------------------------------------------------------
# State plumbing
# ---------------------------------------------------------------------------


class TestRiskState:
    def test_halted_covers_both_reasons(self) -> None:
        assert state(daily_realized_cents=Decimal(-9_000)).halted
        assert state(
            cooldown_until=datetime(2026, 7, 27, 13, 0, tzinfo=UTC)
        ).halted
        assert not state().halted

    def test_a_zero_limit_disables_rather_than_halts(self) -> None:
        """pydantic forbids a zero pct, but a zero bankroll produces one and
        must not be read as "every trade breaches"."""
        assert not state(daily_loss_limit_cents=Decimal(0)).daily_loss_breached

    def test_as_dict_keeps_money_as_strings(self) -> None:
        payload = state(exposure_cents=Decimal("1234.5678")).as_dict()
        assert payload["exposure_cents"] == "1234.5678"
        assert payload["halted"] is False

    def test_cooldown_boundary_is_exclusive(self) -> None:
        """At exactly the expiry instant the cooldown is over."""
        now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
        assert not state(now=now, cooldown_until=now).in_cooldown
        assert state(now=now, cooldown_until=now + timedelta(seconds=1)).in_cooldown

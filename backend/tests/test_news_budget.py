"""Tests for the headline engine's spend and escalation limits.

The failure this module exists to prevent is an unattended loop quietly
spending money all night, and the second failure is a limit that only notices
afterwards. So most of what follows pins down what the module *refuses* and,
just as importantly, **when** it refuses — before the call, on the estimate,
rather than after it on the receipt.

The happy paths are here too, but they are the smaller half. A budget guard
that allows everything passes every happy-path test ever written.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.news.budget import (
    MIN_ESCALATION_ALLOWANCE,
    DaySpend,
    ModelPricing,
    Refusal,
    check_escalation,
    check_triage,
    escalation_allowance,
    headroom_usd,
    utc_day,
)

TODAY = date(2026, 7, 26)
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
BUDGET = Decimal("2.00")
CAP = 0.10

#: A representative pair: a cheap triage model and an expensive scoring one.
TRIAGE_PRICING = ModelPricing(
    input_usd_per_mtok=Decimal("0.25"), output_usd_per_mtok=Decimal("1.25")
)
SCORING_PRICING = ModelPricing(
    input_usd_per_mtok=Decimal("3.00"), output_usd_per_mtok=Decimal("15.00")
)

#: One triage call: fractions of a cent, which is the point.
TRIAGE_COST = TRIAGE_PRICING.cost(400, 100)
SCORING_COST = SCORING_PRICING.cost(2000, 500)


def spend(
    *,
    spent: str = "0",
    triaged: int = 0,
    escalated: int = 0,
    day: date = TODAY,
) -> DaySpend:
    return DaySpend(
        day=day,
        spent_usd=Decimal(spent),
        triaged=triaged,
        escalated=escalated,
    )


def triage(
    s: DaySpend,
    *,
    budget: Decimal = BUDGET,
    cost: Decimal | None = None,
    key: bool = True,
    enabled: bool = True,
):
    return check_triage(
        s,
        budget_usd=budget,
        estimated_cost=TRIAGE_COST if cost is None else cost,
        has_api_key=key,
        enabled=enabled,
        now=NOW,
    )


def escalate(
    s: DaySpend,
    *,
    budget: Decimal = BUDGET,
    cost: Decimal | None = None,
    cap: float = CAP,
    key: bool = True,
    enabled: bool = True,
):
    return check_escalation(
        s,
        budget_usd=budget,
        estimated_cost=SCORING_COST if cost is None else cost,
        escalation_rate_cap=cap,
        has_api_key=key,
        enabled=enabled,
        now=NOW,
    )


class TestModelPricingIsPerMillionTokens:
    def test_a_million_input_tokens_costs_exactly_the_quoted_rate(self) -> None:
        assert SCORING_PRICING.cost(1_000_000, 0) == Decimal("3.00")

    def test_a_million_output_tokens_costs_exactly_the_quoted_rate(self) -> None:
        assert SCORING_PRICING.cost(0, 1_000_000) == Decimal("15.00")

    def test_a_thousand_tokens_is_a_thousandth_of_the_rate(self) -> None:
        """The three-orders-of-magnitude mistake, caught directly."""
        assert SCORING_PRICING.cost(1_000, 0) == Decimal("0.003")

    def test_input_and_output_are_priced_separately(self) -> None:
        assert SCORING_PRICING.cost(2_000, 500) == Decimal("0.0135")

    def test_the_cost_is_a_decimal_not_a_float(self) -> None:
        assert isinstance(SCORING_PRICING.cost(7, 3), Decimal)

    def test_sub_cent_costs_survive_rather_than_rounding_to_zero(self) -> None:
        """A quantized-to-cents version would make the budget immortal."""
        one_call = TRIAGE_PRICING.cost(400, 100)
        assert one_call == Decimal("0.000225")
        assert one_call > 0

    def test_enough_sub_cent_calls_do_reach_the_daily_budget(self) -> None:
        """The reason the sub-cent precision has to be kept: it accumulates."""
        total = TRIAGE_COST * 10_000
        assert total > BUDGET

    def test_negative_tokens_raise_rather_than_crediting_the_budget(self) -> None:
        with pytest.raises(ValueError):
            SCORING_PRICING.cost(-1_000_000, 0)
        with pytest.raises(ValueError):
            SCORING_PRICING.cost(0, -1_000_000)


class TestDaySpend:
    def test_the_rate_is_escalations_over_triaged(self) -> None:
        assert spend(triaged=200, escalated=20).escalation_rate == pytest.approx(0.1)

    def test_no_headlines_triaged_reports_a_rate_of_zero_not_a_crash(self) -> None:
        assert spend(triaged=0, escalated=0).escalation_rate == 0.0

    def test_a_negative_spend_is_not_trustworthy(self) -> None:
        assert not spend(spent="-1").trustworthy

    def test_a_nan_spend_is_not_trustworthy(self) -> None:
        assert not DaySpend(
            day=TODAY, spent_usd=Decimal("NaN"), triaged=1, escalated=0
        ).trustworthy

    def test_negative_counts_are_not_trustworthy(self) -> None:
        assert not spend(triaged=-1).trustworthy
        assert not spend(escalated=-1).trustworthy

    def test_an_ordinary_morning_is_trustworthy(self) -> None:
        assert spend(spent="0.40", triaged=620, escalated=9).trustworthy


class TestHeadroom:
    def test_headroom_is_budget_minus_spend(self) -> None:
        assert headroom_usd(spend(spent="0.75"), budget_usd=BUDGET) == Decimal("1.25")

    def test_overspend_reports_zero_rather_than_a_negative_allowance(self) -> None:
        assert headroom_usd(spend(spent="2.50"), budget_usd=BUDGET) == Decimal(0)

    def test_a_zero_budget_has_no_headroom(self) -> None:
        assert headroom_usd(spend(), budget_usd=Decimal(0)) == Decimal(0)

    def test_a_negative_budget_has_no_headroom_rather_than_being_unlimited(
        self,
    ) -> None:
        assert headroom_usd(spend(), budget_usd=Decimal("-5")) == Decimal(0)

    def test_untrustworthy_spend_reports_no_headroom_rather_than_inventing_it(
        self,
    ) -> None:
        garbage = DaySpend(
            day=TODAY, spent_usd=Decimal("NaN"), triaged=1, escalated=0
        )
        assert headroom_usd(garbage, budget_usd=BUDGET) == Decimal(0)


class TestUtcDayBoundary:
    def test_the_day_rolls_over_at_utc_midnight(self) -> None:
        assert utc_day(datetime(2026, 7, 26, 23, 59, 59, tzinfo=UTC)) == TODAY
        assert utc_day(datetime(2026, 7, 27, 0, 0, 1, tzinfo=UTC)) == date(2026, 7, 27)

    def test_an_aware_non_utc_time_is_converted_not_read_in_its_own_zone(self) -> None:
        """The free hour: 20:30 in UTC-5 is already the next UTC day."""
        minus_five = timezone(timedelta(hours=-5))
        local = datetime(2026, 7, 26, 20, 30, tzinfo=minus_five)
        assert utc_day(local) == date(2026, 7, 27)

    def test_it_agrees_with_the_risk_layers_definition_of_today(self) -> None:
        """`app.trading.risk` takes `datetime.now(UTC).date()`; so does this."""
        moment = datetime(2026, 3, 1, 4, 5, tzinfo=UTC)
        assert utc_day(moment) == moment.date()


class TestEscalationAllowance:
    def test_a_quiet_morning_still_gets_a_floor_of_escalations(self) -> None:
        """Three headlines at a 10% cap would otherwise earn zero."""
        assert escalation_allowance(3, escalation_rate_cap=CAP) == (
            MIN_ESCALATION_ALLOWANCE
        )

    def test_the_floor_applies_before_any_headline_is_triaged(self) -> None:
        assert escalation_allowance(0, escalation_rate_cap=CAP) == (
            MIN_ESCALATION_ALLOWANCE
        )

    def test_the_rate_takes_over_once_volume_exceeds_the_floor(self) -> None:
        assert escalation_allowance(200, escalation_rate_cap=CAP) == 20

    def test_the_allowance_is_floored_not_rounded(self) -> None:
        """75 * 0.10 == 7.5; rounding up would put the realised rate over cap."""
        assert escalation_allowance(75, escalation_rate_cap=CAP) == 7

    def test_a_nonsensical_cap_collapses_to_the_floor_not_to_everything(self) -> None:
        for bad in (0.0, -0.5, 1.5, float("nan")):
            assert escalation_allowance(
                1_000, escalation_rate_cap=bad
            ) == MIN_ESCALATION_ALLOWANCE

    def test_a_cap_of_one_permits_every_headline(self) -> None:
        assert escalation_allowance(1_000, escalation_rate_cap=1.0) == 1_000


class TestTriageRefusals:
    def test_a_disabled_engine_makes_no_calls(self) -> None:
        decision = triage(spend(), enabled=False)
        assert not decision.allowed
        assert decision.refusal is Refusal.DISABLED

    def test_a_missing_api_key_is_its_own_reason_not_an_exception(self) -> None:
        """The normal state of this deployment; it must be a named refusal."""
        decision = triage(spend(), key=False)
        assert not decision.allowed
        assert decision.refusal is Refusal.NO_API_KEY

    def test_disabled_is_reported_ahead_of_a_missing_key(self) -> None:
        decision = triage(spend(), enabled=False, key=False)
        assert decision.refusal is Refusal.DISABLED

    def test_a_spent_out_day_refuses(self) -> None:
        decision = triage(spend(spent="2.00", triaged=9_000))
        assert not decision.allowed
        assert decision.refusal is Refusal.DAILY_BUDGET_EXHAUSTED

    def test_an_overspent_day_refuses(self) -> None:
        decision = triage(spend(spent="2.40"))
        assert decision.refusal is Refusal.DAILY_BUDGET_EXHAUSTED

    def test_the_estimate_is_checked_before_the_call_not_after(self) -> None:
        """Money remains, but not enough for *this* call. A post-hoc guard
        would allow it and discover the overshoot on the receipt."""
        s = spend(spent="1.9999")
        assert headroom_usd(s, budget_usd=BUDGET) > 0
        decision = triage(s, cost=Decimal("0.0005"))
        assert not decision.allowed
        assert decision.refusal is Refusal.WOULD_EXCEED_BUDGET

    def test_a_call_landing_exactly_on_the_budget_is_allowed(self) -> None:
        """The boundary is `>`, not `>=`: spending the last cent is fine."""
        decision = triage(spend(spent="1.9990"), cost=Decimal("0.0010"))
        assert decision.allowed

    def test_a_zero_budget_refuses_rather_than_meaning_unlimited(self) -> None:
        decision = triage(spend(), budget=Decimal("0"))
        assert not decision.allowed
        assert decision.refusal is Refusal.DAILY_BUDGET_EXHAUSTED

    def test_a_negative_budget_refuses_rather_than_meaning_unlimited(self) -> None:
        decision = triage(spend(), budget=Decimal("-1.00"))
        assert not decision.allowed
        assert decision.refusal is Refusal.DAILY_BUDGET_EXHAUSTED

    def test_yesterdays_accounting_cannot_be_spent_against_today(self) -> None:
        decision = triage(spend(day=TODAY - timedelta(days=1)))
        assert not decision.allowed
        assert decision.refusal is Refusal.SPEND_UNKNOWN

    def test_tomorrows_accounting_is_refused_too(self) -> None:
        decision = triage(spend(day=TODAY + timedelta(days=1)))
        assert decision.refusal is Refusal.SPEND_UNKNOWN

    def test_a_corrupt_spend_figure_refuses_rather_than_being_clamped(self) -> None:
        garbage = DaySpend(
            day=TODAY, spent_usd=Decimal("NaN"), triaged=10, escalated=1
        )
        decision = triage(garbage)
        assert not decision.allowed
        assert decision.refusal is Refusal.SPEND_UNKNOWN

    def test_a_negative_spend_figure_refuses(self) -> None:
        decision = triage(spend(spent="-0.50"))
        assert decision.refusal is Refusal.SPEND_UNKNOWN

    def test_a_free_call_is_a_mispriced_call_and_is_refused(self) -> None:
        decision = triage(spend(), cost=Decimal("0"))
        assert not decision.allowed
        assert decision.refusal is Refusal.INVALID_ESTIMATE

    def test_a_negative_estimate_is_refused(self) -> None:
        decision = triage(spend(), cost=Decimal("-0.01"))
        assert decision.refusal is Refusal.INVALID_ESTIMATE

    def test_every_refusal_explains_itself(self) -> None:
        """A refusal that only says 'blocked' gets worked around."""
        for decision in (
            triage(spend(), enabled=False),
            triage(spend(), key=False),
            triage(spend(spent="2.00")),
            triage(spend(spent="1.9999"), cost=Decimal("0.5")),
            triage(spend(day=TODAY - timedelta(days=1))),
            triage(spend(), cost=Decimal("0")),
            escalate(spend(triaged=100, escalated=10)),
        ):
            assert not decision.allowed
            assert decision.refusal is not None
            assert len(decision.message) > 40


class TestTriageHappyPath:
    def test_an_ordinary_headline_on_a_fresh_day_is_allowed(self) -> None:
        decision = triage(spend())
        assert decision.allowed
        assert decision.refusal is None

    def test_triage_has_no_rate_cap_of_its_own(self) -> None:
        """Every novel headline is triaged; only money limits it."""
        decision = triage(spend(spent="0.50", triaged=50_000))
        assert decision.allowed


class TestEscalationRefusals:
    def test_escalating_past_the_rate_cap_refuses(self) -> None:
        decision = escalate(spend(triaged=100, escalated=10))
        assert not decision.allowed
        assert decision.refusal is Refusal.ESCALATION_RATE_EXCEEDED

    def test_the_cap_is_checked_against_the_escalation_about_to_happen(self) -> None:
        """Comparing the current count alone permits one past the cap, every
        time — the same mistake as checking spend after the call."""
        assert escalate(spend(triaged=100, escalated=9)).allowed
        assert not escalate(spend(triaged=100, escalated=10)).allowed

    def test_a_broken_triage_step_is_stopped_long_before_the_budget_is(self) -> None:
        """The whole reason the rate cap is not a second budget: barely any
        money has been spent and it still refuses."""
        s = spend(spent="0.01", triaged=40, escalated=40)
        assert headroom_usd(s, budget_usd=BUDGET) > Decimal("1.90")
        decision = escalate(s)
        assert decision.refusal is Refusal.ESCALATION_RATE_EXCEEDED

    def test_when_both_limits_trip_the_rate_is_reported_because_it_names_why(
        self,
    ) -> None:
        """Reporting 'budget exhausted' here points the operator at
        `daily_budget_usd`; raising it buys ten more minutes of the same bug."""
        decision = escalate(spend(spent="2.00", triaged=60, escalated=55))
        assert not decision.allowed
        assert decision.refusal is Refusal.ESCALATION_RATE_EXCEEDED

    def test_a_missing_key_outranks_the_rate_cap(self) -> None:
        decision = escalate(spend(triaged=100, escalated=99), key=False)
        assert decision.refusal is Refusal.NO_API_KEY

    def test_a_disabled_engine_outranks_the_rate_cap(self) -> None:
        decision = escalate(spend(triaged=100, escalated=99), enabled=False)
        assert decision.refusal is Refusal.DISABLED

    def test_untrustworthy_counts_refuse_as_unknown_not_as_a_rate_breach(self) -> None:
        """The rate cap has nothing to say about counts it cannot believe."""
        decision = escalate(spend(triaged=-5, escalated=99))
        assert decision.refusal is Refusal.SPEND_UNKNOWN

    def test_a_spent_out_day_still_refuses_an_in_cap_escalation(self) -> None:
        decision = escalate(spend(spent="2.00", triaged=1_000, escalated=1))
        assert not decision.allowed
        assert decision.refusal is Refusal.DAILY_BUDGET_EXHAUSTED

    def test_an_expensive_scoring_call_is_refused_on_thin_headroom(self) -> None:
        s = spend(spent="1.995", triaged=1_000, escalated=1)
        decision = escalate(s)
        assert not decision.allowed
        assert decision.refusal is Refusal.WOULD_EXCEED_BUDGET

    def test_a_zero_budget_refuses_escalation_too(self) -> None:
        decision = escalate(spend(triaged=1_000), budget=Decimal("0"))
        assert decision.refusal is Refusal.DAILY_BUDGET_EXHAUSTED


class TestEscalationHappyPath:
    def test_a_normal_hit_rate_escalates(self) -> None:
        decision = escalate(spend(spent="0.40", triaged=620, escalated=9))
        assert decision.allowed
        assert decision.refusal is None

    def test_a_quiet_morning_escalates_rather_than_looking_broken(self) -> None:
        """Four headlines, three of them real. The rate alone earns zero."""
        assert escalate(spend(triaged=4, escalated=0)).allowed
        assert escalate(spend(triaged=4, escalated=2)).allowed

    def test_the_floor_is_a_floor_and_not_an_open_door(self) -> None:
        s = spend(triaged=4, escalated=MIN_ESCALATION_ALLOWANCE)
        assert not escalate(s).allowed
        assert escalate(s).refusal is Refusal.ESCALATION_RATE_EXCEEDED

    def test_the_message_reports_the_allowance_it_was_measured_against(self) -> None:
        decision = escalate(spend(triaged=200, escalated=5))
        assert decision.allowed
        assert "20" in decision.message

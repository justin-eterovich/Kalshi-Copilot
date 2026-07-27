"""Spend and escalation limits for the two-tier headline engine.

The headline engine triages every novel headline with a cheap model and
escalates only the hits to a strong one that actually scores them. Both tiers
cost money per call, both run unattended, and the feed never stops. This module
is the thing that says no.

**The dangerous version of this module reconciles spend after the fact.** It
adds up what the calls cost, compares the total to `daily_budget_usd`, and logs
when the total goes over. Every number in it is correct. It is still not a
limit — it is a report, and the thing it reports is a bill that has already
been paid. A guard that notices the budget is blown once it is blown has
prevented nothing. So every check here takes the *estimated cost of the call
about to be made* and refuses **before** the call, never after.

Two limits, deliberately not the same limit twice:

- **`daily_budget_usd`** caps the money. It is the backstop, and it is the
  crude one: it does not care why the money went, only that it is gone.
- **`escalation_rate_cap`** caps the *proportion* of triaged headlines that
  reach the expensive model. This is a **correctness** guard, not a second
  budget. If triage starts escalating everything, triage has broken — a prompt
  regression, a swapped model, a feed that has gone to near-duplicates — and
  the right response is to stop and say so, not to spend the day's allowance in
  ten minutes and report a busy morning. The rate cap is meant to trip
  *before* the budget does, which is why `check_escalation` reports it first
  when both have tripped: only the rate cap names the cause.

**No API key is a refusal, not an exception.** This deployment has no
`anthropic_api_key` configured, which means the engine's normal state today is
"cannot run". That has to be a first-class :class:`Refusal` with its own reason
code, surfaced next to the others, rather than an authentication error thrown
from three layers down at call time. A missing key is a configuration fact the
engine knows before it opens a socket.

**On the estimate.** `estimated_cost` is an estimate: input tokens are known
before the call, output tokens are not known until it returns. Callers must
round **up** — price the maximum output the request permits, not the expected
output — because the direction of the error decides which way the guard fails.
Rounded up, a refusal may be a little early and a little money goes unspent.
Rounded down, every call is admitted slightly too cheaply and the day ends over
budget by the accumulated shortfall. The guard still holds when an individual
estimate is low, because actual spend is fed back into :class:`DaySpend` and
re-checked before the *next* call: the overshoot is bounded by one call's
underestimate, not by the day's.

Pure and stdlib-only by design — no I/O, no clock reads except an explicit
`now`, nothing to mock. A limit that needs a database to answer is a limit that
fails open when the database is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "MIN_ESCALATION_ALLOWANCE",
    "BudgetDecision",
    "DaySpend",
    "ModelPricing",
    "Refusal",
    "check_escalation",
    "check_triage",
    "escalation_allowance",
    "headroom_usd",
    "utc_day",
]

ZERO = Decimal(0)

#: Model pricing is quoted per **million** tokens, so this is the divisor —
#: not 1_000. Getting it wrong by three orders of magnitude would put every
#: call at 0.1% of its real cost and the budget would never move all day.
TOKENS_PER_MTOK = Decimal(1_000_000)

#: Absolute number of escalations allowed per UTC day regardless of the rate
#: cap. Without a floor the cap has a dead zone at the start of every day: at
#: `escalation_rate_cap: 0.10`, ten headlines must be triaged before the cap
#: permits even one escalation, so a quiet morning with four novel headlines —
#: three of which matter — escalates none of them, and the engine reads as
#: broken rather than idle.
#:
#: Five, chosen from both ends. It is small against a day's headline flow
#: (hundreds), so on any normal day the rate cap, not the floor, is what binds;
#: and five scoring calls is a bounded, cheap mistake in the pathological case
#: where triage is broken from the very first headline — a few cents, with
#: `daily_budget_usd` still behind it. One would make the floor useless on
#: exactly the quiet morning it exists for. Twenty would be more than 10% of an
#: entire day's headlines, meaning the rate cap would never bind at all and the
#: correctness guard would quietly degrade into a second, weaker budget.
MIN_ESCALATION_ALLOWANCE = 5


class Refusal(StrEnum):
    """Why a call was refused, as a stable machine-readable reason.

    These strings are an interface: they go on the dashboard, into logs, and
    into whatever alerting the operator hangs off them. Rename one and the
    history stops matching. Each is a *different* operator action, which is the
    whole point of not collapsing them into one "blocked" flag.
    """

    #: `news.headlines.enabled` is false. Nothing is wrong; nothing should run.
    DISABLED = "disabled"
    #: No `anthropic_api_key` on this deployment. The engine cannot run at all,
    #: and this is the expected state here — see the module docstring.
    NO_API_KEY = "no_api_key"
    #: Today's spend has reached `daily_budget_usd`, or the budget is zero or
    #: negative. Clears at midnight UTC.
    DAILY_BUDGET_EXHAUSTED = "daily_budget_exhausted"
    #: Some budget remains, but not enough for *this* call at its estimated
    #: cost. Distinct from exhaustion: a cheaper call might still be allowed.
    WOULD_EXCEED_BUDGET = "would_exceed_budget"
    #: This escalation would push the day's escalation rate past
    #: `escalation_rate_cap`. Suspect triage, not the budget.
    ESCALATION_RATE_EXCEEDED = "escalation_rate_exceeded"
    #: The spend figure handed in cannot be trusted — it is for another day, or
    #: it is negative or not a finite number. Refusing is the only safe reading
    #: of "I do not know what has been spent today".
    SPEND_UNKNOWN = "spend_unknown"
    #: The caller's cost estimate is zero, negative or not finite. A call that
    #: is free is a call that was mispriced; admitting it would make the budget
    #: unreachable.
    INVALID_ESTIMATE = "invalid_estimate"


def utc_day(now: datetime | None = None) -> date:
    """The day the budget is accounted against.

    **UTC, matching `app.trading.risk`'s daily loss limit.** Both are daily
    limits on the same dashboard and they must roll over at the same instant;
    two components disagreeing about when "today" starts is how a limit gets
    a free hour, and it is invisible unless you go looking at 00:30 local.

    An aware datetime is *converted* before the date is taken rather than
    having `.date()` called in its own zone — that is the free hour. A naive
    one is read as UTC, which is the convention everywhere else in this system.
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.date()


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """What one model charges, in USD per **million** tokens.

    Per million because that is the unit every published price list quotes —
    "$3.00 / MTok" — so the config can be transcribed without arithmetic, and
    nobody has to guess whether a number was already converted. The division by
    :data:`TOKENS_PER_MTOK` happens once, in :meth:`cost`, and nowhere else.

    `Decimal`, not `float`, all the way through: a budget guard whose arithmetic
    drifts is a budget that does not hold, and these numbers are summed over
    thousands of calls a day.
    """

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        """Exact USD cost of a call with these token counts.

        Deliberately **not quantized to cents**. A triage call on a cheap model
        costs a small fraction of a cent; rounding each one to the nearest cent
        would round it to zero, the day's spend would never move off 0.00, and
        the budget would be immortal. The fractional dollars are the whole
        signal here — quantize at the display layer if at all.

        Negative token counts raise rather than crediting the budget back: they
        can only come from a caller bug, and the fail-closed reading of a bug in
        the meter is to stop, not to bank the refund.
        """
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError(
                f"negative token count ({input_tokens=}, {output_tokens=}); "
                "a call cannot refund budget"
            )
        # One division at the end, so the two terms round together rather than
        # each shedding precision on its own.
        return (
            Decimal(input_tokens) * self.input_usd_per_mtok
            + Decimal(output_tokens) * self.output_usd_per_mtok
        ) / TOKENS_PER_MTOK


@dataclass(frozen=True, slots=True)
class DaySpend:
    """What the engine has done so far on one UTC day.

    Counts and money together, because the two limits are computed from
    different halves of it and the operator needs to see both: "$0.40 of $2.00,
    620 triaged, 9 escalated" is a healthy morning, and "$0.40 of $2.00, 12
    triaged, 11 escalated" is a broken triage step that has spent the same
    money. A budget-only view cannot tell them apart.
    """

    day: date
    spent_usd: Decimal
    triaged: int
    escalated: int

    @property
    def escalation_rate(self) -> float:
        """Escalations as a fraction of headlines triaged today.

        **Zero triaged is 0.0, not an error and not 1.0.** The rate is
        genuinely undefined with an empty denominator, and 0.0 is the reading
        that lets an empty day start; nothing is protected by refusing to
        divide, because the first calls of the day are gated by
        :data:`MIN_ESCALATION_ALLOWANCE` on the absolute count, which binds
        whatever this says. Reported as `float` because it is a proportion for
        display and comparison, not money — no dollar amount is derived from it.
        """
        if self.triaged <= 0:
            return 0.0
        return self.escalated / self.triaged

    @property
    def trustworthy(self) -> bool:
        """Whether these numbers can be spent against at all.

        A NaN or negative spend, or negative counts, mean the accounting
        upstream is broken. There is no repair that is safe: clamping a
        negative to zero invents headroom, and treating NaN as small admits
        every call. See :attr:`Refusal.SPEND_UNKNOWN`.
        """
        return (
            self.spent_usd.is_finite()
            and self.spent_usd >= ZERO
            and self.triaged >= 0
            and self.escalated >= 0
        )


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """The answer, with the reason attached whichever way it went.

    `message` is written for the operator reading the dashboard at 2am: it says
    which limit, what the numbers were, and which config key moves it. A
    refusal that only says "blocked" teaches nobody anything and gets worked
    around instead of understood.
    """

    allowed: bool
    refusal: Refusal | None
    message: str


def _allow(message: str) -> BudgetDecision:
    return BudgetDecision(allowed=True, refusal=None, message=message)


def _refuse(refusal: Refusal, message: str) -> BudgetDecision:
    return BudgetDecision(allowed=False, refusal=refusal, message=message)


def headroom_usd(spend: DaySpend, *, budget_usd: Decimal) -> Decimal:
    """USD left in today's budget, never below zero.

    Clamped at zero because this answers "how much more may I spend", and the
    answer to that is never negative. How far *past* the budget a day went is a
    real and interesting number, but it lives in `spend.spent_usd` where it
    cannot be mistaken for spendable room.

    A non-finite or negative `spent_usd` reports zero headroom rather than
    inventing some: the callers that gate on this are refused outright by
    :attr:`Refusal.SPEND_UNKNOWN`, and anything else reading it for display
    should see "nothing available", not a number computed from garbage.
    """
    if not budget_usd.is_finite() or budget_usd <= ZERO:
        return ZERO
    if not spend.trustworthy:
        return ZERO
    return max(ZERO, budget_usd - spend.spent_usd)


def escalation_allowance(triaged: int, *, escalation_rate_cap: float) -> int:
    """How many escalations today's triage volume has earned.

    ``max(MIN_ESCALATION_ALLOWANCE, floor(triaged * cap))``.

    **Floored, not rounded.** The cap is a ceiling on a proportion, so rounding
    0.5 up would let the realised rate sit above the configured cap on small
    volumes — which is precisely the volume at which a broken triage step shows
    up first. Flooring keeps the realised rate at or under the cap everywhere
    above the floor.

    The floor is :data:`MIN_ESCALATION_ALLOWANCE`; see its comment for why five.

    A cap that is not a sane proportion (non-positive, above 1, NaN) collapses
    to the floor alone rather than being trusted. `escalation_rate_cap: 0` in
    config reads as "never escalate", but that would also disable the small-
    sample floor that stops the engine looking broken, so the honest handling is
    to let the floor stand and let `enabled: false` be the way to turn the tier
    off.
    """
    if triaged <= 0:
        return MIN_ESCALATION_ALLOWANCE
    if not (0.0 < escalation_rate_cap <= 1.0):
        return MIN_ESCALATION_ALLOWANCE
    # int() truncates toward zero; `triaged` and the cap are both non-negative
    # here, so truncation is a floor.
    earned = int(triaged * escalation_rate_cap)
    return max(MIN_ESCALATION_ALLOWANCE, earned)


def _check_context(
    spend: DaySpend,
    *,
    estimated_cost: Decimal,
    has_api_key: bool,
    enabled: bool,
    now: datetime | None,
) -> BudgetDecision | None:
    """Can this call be reasoned about at all? `None` to carry on.

    Ordered most-fundamental first, so the message names the thing the operator
    would have to fix first: a disabled engine is not short of money, and an
    engine with no key is not short of anything else either. Everything here
    precedes both limits, because neither the budget nor the rate cap means
    anything when the inputs to them are missing or untrustworthy.
    """
    if not enabled:
        return _refuse(
            Refusal.DISABLED,
            "the headline engine is off (news.headlines.enabled = false). "
            "No calls are made and no budget is consumed.",
        )

    if not has_api_key:
        return _refuse(
            Refusal.NO_API_KEY,
            "no ANTHROPIC_API_KEY is configured, so neither model tier can be "
            "called. This is the expected state on this deployment; set the key "
            "in .env to enable the headline engine.",
        )

    today = utc_day(now)
    if not spend.trustworthy or spend.day != today:
        return _refuse(
            Refusal.SPEND_UNKNOWN,
            f"today's spend cannot be determined: the accounting handed in is "
            f"for {spend.day.isoformat()} (today is {today.isoformat()} UTC) or "
            f"carries impossible values (spent={spend.spent_usd}, "
            f"triaged={spend.triaged}, escalated={spend.escalated}). Refusing "
            "rather than spending against a figure that is not today's.",
        )

    if not estimated_cost.is_finite() or estimated_cost <= ZERO:
        return _refuse(
            Refusal.INVALID_ESTIMATE,
            f"estimated cost {estimated_cost} is not a positive amount. Every "
            "call costs something; a zero estimate would let the budget be "
            "spent without ever being reached.",
        )

    return None


def _check_money(
    spend: DaySpend, *, budget_usd: Decimal, estimated_cost: Decimal
) -> BudgetDecision | None:
    """The `daily_budget_usd` limit, or `None` to carry on.

    Assumes :func:`_check_context` has already run, so `spend` is today's and
    trustworthy and `estimated_cost` is a positive finite amount.
    """
    # Zero or negative budget means zero calls. It is emphatically not
    # "unlimited" — the reading that would turn a config typo, or a deliberate
    # `daily_budget_usd: 0` meant to stop all spending, into an all-night
    # spending spree.
    if not budget_usd.is_finite() or budget_usd <= ZERO:
        return _refuse(
            Refusal.DAILY_BUDGET_EXHAUSTED,
            f"news.headlines.daily_budget_usd is {budget_usd}, which permits no "
            "calls at all. A non-positive budget is not unlimited.",
        )

    headroom = headroom_usd(spend, budget_usd=budget_usd)
    if headroom <= ZERO:
        return _refuse(
            Refusal.DAILY_BUDGET_EXHAUSTED,
            f"today's headline spend is {spend.spent_usd:.4f} USD, at or past "
            f"the {budget_usd:.2f} USD daily budget "
            f"(news.headlines.daily_budget_usd). The engine is stopped for the "
            "rest of the UTC day.",
        )

    # The check that makes this a limit rather than a report: the *estimated
    # cost of the call about to be made*, compared before it is made. Checking
    # `spent_usd` alone would admit a call that lands exactly on the last cent
    # of the budget and overshoot by whatever that call cost.
    if estimated_cost > headroom:
        return _refuse(
            Refusal.WOULD_EXCEED_BUDGET,
            f"this call is estimated at {estimated_cost:.4f} USD but only "
            f"{headroom:.4f} USD of the {budget_usd:.2f} USD daily budget "
            "remains. Refused before the call, not reported after it.",
        )

    return None


def check_triage(
    spend: DaySpend,
    *,
    budget_usd: Decimal,
    estimated_cost: Decimal,
    has_api_key: bool,
    enabled: bool,
    now: datetime | None = None,
) -> BudgetDecision:
    """May the cheap model be called on one more headline?

    Money only. There is no rate cap on triage: triage runs on every novel
    headline by definition, so a "proportion of headlines triaged" limit has no
    meaning, and `daily_budget_usd` is the only thing standing between a runaway
    feed and an overnight bill.

    `estimated_cost` must be the caller's **rounded-up** estimate for this one
    call — see the module docstring on which direction the estimate errs and why
    it matters.
    """
    refusal = _check_context(
        spend,
        estimated_cost=estimated_cost,
        has_api_key=has_api_key,
        enabled=enabled,
        now=now,
    ) or _check_money(
        spend, budget_usd=budget_usd, estimated_cost=estimated_cost
    )
    if refusal is not None:
        return refusal

    headroom = headroom_usd(spend, budget_usd=budget_usd)
    return _allow(
        f"triage allowed: {headroom:.4f} USD of {budget_usd:.2f} USD remains "
        f"today, {spend.triaged} headlines triaged so far."
    )


def check_escalation(
    spend: DaySpend,
    *,
    budget_usd: Decimal,
    estimated_cost: Decimal,
    escalation_rate_cap: float,
    has_api_key: bool,
    enabled: bool,
    now: datetime | None = None,
) -> BudgetDecision:
    """May this headline be escalated to the expensive scoring model?

    Both limits apply, and the rate cap is evaluated **first**, deliberately.
    When triage regresses, both limits trip — the escalations blow the rate and
    then the money — but only the rate cap names the cause. Reporting
    "budget exhausted" in that situation is true and useless: it points the
    operator at `daily_budget_usd`, and raising it would buy another ten
    minutes of the same broken behaviour. Reporting the rate points at triage.

    `spend.triaged` is expected to already include the headline being decided
    on, since triage necessarily ran before escalation was considered.
    """
    refusal = _check_context(
        spend,
        estimated_cost=estimated_cost,
        has_api_key=has_api_key,
        enabled=enabled,
        now=now,
    )
    if refusal is not None:
        return refusal

    allowance = escalation_allowance(
        spend.triaged, escalation_rate_cap=escalation_rate_cap
    )
    # `+ 1` because the question is about the escalation that has not happened
    # yet. Comparing `spend.escalated` alone permits one escalation past the cap
    # every time, for the same reason a post-hoc budget check is a report.
    if spend.escalated + 1 > allowance:
        return _refuse(
            Refusal.ESCALATION_RATE_EXCEEDED,
            f"{spend.escalated} of {spend.triaged} headlines already escalated "
            f"today ({spend.escalation_rate:.1%}), at the "
            f"{escalation_rate_cap:.1%} cap "
            f"(news.headlines.escalation_rate_cap, allowance {allowance} "
            f"including a floor of {MIN_ESCALATION_ALLOWANCE}). This is a "
            "correctness limit, not a budget: check that triage is still "
            "discriminating before raising it.",
        )

    money = _check_money(
        spend, budget_usd=budget_usd, estimated_cost=estimated_cost
    )
    if money is not None:
        return money

    headroom = headroom_usd(spend, budget_usd=budget_usd)
    return _allow(
        f"escalation allowed: {spend.escalated + 1} of {allowance} permitted "
        f"today, {headroom:.4f} USD of {budget_usd:.2f} USD remains."
    )

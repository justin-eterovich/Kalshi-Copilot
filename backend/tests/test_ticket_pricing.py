"""Tests for trade-ticket costing.

Everything the approval card shows comes from here, so these tests are the
guard on the numbers a human reads before committing money. The properties
that matter:

- The fee comes from ``app.core.fees`` and nothing else recomputes it.
- Breakeven includes the fee. A breakeven that ignores fees is a lie with a
  decimal point on it.
- An unverified fee category refuses to price at all.
- Net edge appears only when a fair value was supplied, and is always net.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Config
from app.core.fees import (
    FeeSchedule,
    UnverifiedFeeSchedule,
    series_of,
    taker_fee_cents,
)
from app.db.models import Side
from app.trading.pricing import price_ticket


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {"costs": {"slippage_buffer_cents": 0.0, "assume_taker": True}}
    )


VERIFIED = FeeSchedule.from_dict(
    {
        "meta": {"verified_on": "2026-07-27"},
        "formula": {"base_taker_rate": "0.07", "base_maker_rate": "0.0175"},
        "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
        "series": {"KXFREE": {"maker": 0, "taker": 0}},
    }
)


def quote(config: Config, **overrides: object):
    kwargs: dict = {
        "schedule": VERIFIED,
        "ticker": "KXTEST-26JUL-A",
        "side": Side.YES,
        "action": "buy",
        "limit_price": "0.50",
        "contracts": "100",
        "category": "Sports",
        "config": config,
    }
    kwargs.update(overrides)
    return price_ticket(**kwargs)  # type: ignore[arg-type]


class TestCost:
    def test_notional_is_exact(self, config: Config) -> None:
        q = quote(config)
        assert q.notional_cents == Decimal("5000.00")

    def test_fee_matches_the_fee_engine(self, config: Config) -> None:
        """No second implementation. If these ever disagree, one is wrong."""
        q = quote(config)
        assert q.est_fee_cents == taker_fee_cents(
            "0.50", "100", "KXTEST", VERIFIED
        )
        assert q.est_fee_cents == Decimal(175)

    def test_total_cost_includes_the_fee(self, config: Config) -> None:
        q = quote(config)
        assert q.total_cost_cents == Decimal("5175.00")

    def test_breakeven_is_above_the_price_by_the_fee(self, config: Config) -> None:
        """The number that stops a 'free' 1c edge looking free."""
        q = quote(config)
        assert q.breakeven_cents == Decimal("51.75")

    def test_max_loss_on_a_buy_is_the_total_outlay(self, config: Config) -> None:
        q = quote(config)
        assert q.max_loss_cents == q.total_cost_cents

    def test_max_win_is_net_of_the_fee(self, config: Config) -> None:
        q = quote(config)
        # 100 contracts * 50c upside = 5000c, less the 175c fee.
        assert q.max_win_cents == Decimal("4825.00")

    def test_fractional_contracts_are_priced(self, config: Config) -> None:
        q = quote(config, contracts="2.50")
        assert q.contracts == Decimal("2.50")
        assert q.notional_cents == Decimal("125.000")

    def test_sub_cent_price_is_not_rounded_away(self, config: Config) -> None:
        q = quote(config, limit_price="0.505")
        assert q.limit_price == Decimal("0.505")
        assert q.notional_cents == Decimal("5050.000")


class TestWireTranslation:
    def test_buy_yes_is_a_bid_at_the_same_price(self, config: Config) -> None:
        q = quote(config, side=Side.YES, action="buy", limit_price="0.56")
        assert q.wire_book_side == "bid"
        assert q.wire_yes_price == Decimal("0.56")

    def test_buy_no_is_an_ask_at_the_complement(self, config: Config) -> None:
        """The operator sees 'buy NO at 30c'; the exchange sees ask at 70c."""
        q = quote(config, side=Side.NO, action="buy", limit_price="0.30")
        assert q.wire_book_side == "ask"
        assert q.wire_yes_price == Decimal("0.70")

    def test_the_ticket_keeps_the_price_the_operator_typed(
        self, config: Config
    ) -> None:
        q = quote(config, side=Side.NO, action="buy", limit_price="0.30")
        assert q.limit_price == Decimal("0.30")


class TestFeeSymmetry:
    def test_a_no_ticket_costs_the_same_fee_as_its_yes_mirror(
        self, config: Config
    ) -> None:
        """P*(1-P) is symmetric, which is exactly why a direction bug is
        invisible in the fee. Asserted so the reason is on record."""
        buy_no = quote(config, side=Side.NO, action="buy", limit_price="0.30")
        buy_yes = quote(config, side=Side.YES, action="buy", limit_price="0.70")
        assert buy_no.est_fee_cents == buy_yes.est_fee_cents


class TestNetEdge:
    def test_absent_without_a_fair_value(self, config: Config) -> None:
        assert quote(config).net_edge_cents is None

    def test_present_and_net_when_a_fair_value_is_given(self, config: Config) -> None:
        q = quote(config, limit_price="0.50", fair_price="0.55", contracts="100")
        # Gross 5c; fee is 175c over 100 contracts = 1.75c each.
        assert q.net_edge_cents == Decimal("3.25")

    def test_slippage_is_subtracted(self) -> None:
        config = Config.model_validate({"costs": {"slippage_buffer_cents": 0.5}})
        q = quote(config, limit_price="0.50", fair_price="0.55", contracts="100")
        assert q.net_edge_cents == Decimal("2.75")

    def test_a_thin_gross_edge_goes_negative_after_costs(
        self, config: Config
    ) -> None:
        """The entire point of reporting net rather than gross."""
        q = quote(config, limit_price="0.50", fair_price="0.51", contracts="100")
        assert q.net_edge_cents < 0

    def test_selling_yes_prices_identically_to_buying_no(
        self, config: Config
    ) -> None:
        """Selling YES at p *is* buying NO at 1-p, so the edge must match.

        This is the identity the sell path is implemented with, and it is
        also the one that catches a sign error: get it wrong and the two
        differ by twice the gross edge rather than by nothing.
        """
        sell_yes = quote(
            config, side=Side.YES, action="sell", limit_price="0.55",
            fair_price="0.50", contracts="100",
        )
        buy_no = quote(
            config, side=Side.NO, action="buy", limit_price="0.45",
            fair_price="0.50", contracts="100",
        )
        assert sell_yes.net_edge_cents == buy_no.net_edge_cents

    def test_selling_above_fair_earns_the_spread_less_costs(
        self, config: Config
    ) -> None:
        q = quote(
            config, action="sell", limit_price="0.55", fair_price="0.50",
            contracts="100",
        )
        # 5c gross. The fee at 0.55 is 174c over 100 contracts — lower than
        # the 175c at the money, because P*(1-P) peaks at 0.50.
        assert q.est_fee_cents == Decimal("173.25")
        assert q.net_edge_cents == Decimal("3.2675")

    def test_selling_below_fair_is_a_negative_edge(self, config: Config) -> None:
        q = quote(
            config, action="sell", limit_price="0.45", fair_price="0.55",
            contracts="100",
        )
        assert q.net_edge_cents < 0


class TestFailClosed:
    def test_an_unverified_schedule_refuses_to_price(self, config: Config) -> None:
        """Every edge figure is net of fees; an unchecked fee table makes all
        of them untrustworthy, so nothing may be proposed against one."""
        never_checked = FeeSchedule.from_dict(
            {"meta": {"verified_on": None}, "series": {}}
        )
        with pytest.raises(UnverifiedFeeSchedule):
            quote(config, schedule=never_checked)

    def test_a_null_category_no_longer_blocks_pricing(
        self, config: Config
    ) -> None:
        """Regression, in the opposite direction from before.

        Fees used to be keyed by category, which arrives from the Event sync
        and is null until it lands — so pricing depended on a join that might
        not have run. Fees are keyed by series now, which is in the ticker, so
        a missing category is irrelevant to cost.
        """
        q = quote(config, category=None)
        assert q.est_fee_cents == Decimal(175)

    def test_the_series_comes_from_the_ticker(self, config: Config) -> None:
        q = quote(config, ticker="KXFEDDECISION-26JUL-H25")
        assert q.series == "KXFEDDECISION"
        assert series_of("KXFEDDECISION-26JUL-H25") == "KXFEDDECISION"

    def test_a_fee_free_series_costs_nothing(self, config: Config) -> None:
        q = quote(config, ticker="KXFREE-26JUL-A")
        assert q.est_fee_cents == 0


class TestValidation:
    @pytest.mark.parametrize("bad", ["56", "0", "1", "-0.5", "1.5"])
    def test_a_price_outside_zero_to_one_is_rejected(
        self, config: Config, bad: str
    ) -> None:
        """'56' is the cents mistake this codebase exists to prevent."""
        with pytest.raises(ValueError, match="between 0 and 1"):
            quote(config, limit_price=bad)

    def test_nonsense_price_raises_rather_than_becoming_zero(
        self, config: Config
    ) -> None:
        with pytest.raises(ValueError):
            quote(config, limit_price="banana")

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_non_positive_size_is_rejected(self, config: Config, bad: str) -> None:
        with pytest.raises(ValueError, match="contracts must be positive"):
            quote(config, contracts=bad)

    def test_unknown_action_is_rejected(self, config: Config) -> None:
        with pytest.raises(ValueError, match="action"):
            quote(config, action="hodl")


class TestFairPriceValidation:
    """``fair_price`` gets the same domain check as ``limit_price``.

    It did not, for a whole milestone, thirty-two lines below the identical
    guard in the same function — so ``fair_price="56"``, the obvious operator
    slip for 56c, was priced as a $56 fair value and returned HTTP 200 with a
    claimed **+$55.97/contract** edge on a contract quoted at 1.8c. That number
    is then written to ``proposed_trades.net_edge_cents`` and the audit log,
    where it becomes the "claimed edge" the report card grades the detector
    against.
    """

    @pytest.mark.parametrize("bad", ["56", "999999", "-1", "0", "1", "1.5", "-0.5"])
    def test_a_fair_value_outside_zero_to_one_is_rejected(
        self, config: Config, bad: str
    ) -> None:
        with pytest.raises(ValueError, match="between 0 and 1"):
            quote(config, fair_price=bad)

    def test_the_headline_regression(self, config: Config) -> None:
        """The exact live repro: 1 contract at 1.8c with a fair value of "56".

        Must raise. Returning ~5597c of edge is the failure this pins.
        """
        with pytest.raises(ValueError):
            quote(
                config,
                ticker="KXMLB-26-HOU",
                limit_price="0.018",
                contracts="1",
                fair_price="56",
            )

    def test_the_message_says_what_56_actually_means(self, config: Config) -> None:
        """The refusal has to teach, or the operator retypes the same thing."""
        with pytest.raises(ValueError) as exc:
            quote(config, fair_price="56")
        assert "56" in str(exc.value)

    def test_the_boundaries_are_strict(self, config: Config) -> None:
        """A fair value of exactly 0 or 1 asserts certainty.

        Nothing here may manufacture the last cents of edge out of an
        assumption of settlement — the stale-quote detector caps fair at 0.98
        for the same reason.
        """
        with pytest.raises(ValueError):
            quote(config, fair_price="0")
        with pytest.raises(ValueError):
            quote(config, fair_price="1")

    def test_an_ordinary_fair_value_still_prices(self, config: Config) -> None:
        """The guard must not over-refuse: 0.56 is the documented wire form."""
        q = quote(config, limit_price="0.50", fair_price="0.56", contracts="100")
        assert q.fair_price == Decimal("0.56")
        # 6c gross, less the 1.75c per-contract fee.
        assert q.net_edge_cents == Decimal("4.25")

    def test_a_sub_cent_fair_value_is_still_accepted(self, config: Config) -> None:
        """Tick size varies per market, so sub-cent fair values are real."""
        q = quote(config, limit_price="0.018", fair_price="0.0195", contracts="1")
        assert q.fair_price == Decimal("0.0195")

    def test_a_non_finite_fair_value_is_refused(self, config: Config) -> None:
        """NaN reached Postgres through this argument. It parses before it is
        range-checked, so the money layer is what has to stop it."""
        from app.core.money import MoneyParseError

        with pytest.raises(MoneyParseError):
            quote(config, fair_price="NaN")
        with pytest.raises(MoneyParseError):
            quote(config, fair_price="Infinity")

    def test_a_non_finite_limit_price_is_refused_too(self, config: Config) -> None:
        from app.core.money import MoneyParseError

        with pytest.raises(MoneyParseError):
            quote(config, limit_price="NaN")

    def test_a_non_finite_contract_count_is_refused(self, config: Config) -> None:
        from app.core.money import MoneyParseError

        with pytest.raises(MoneyParseError):
            quote(config, contracts="NaN")

    def test_omitting_fair_price_is_still_legal(self, config: Config) -> None:
        """``None`` means "no fair value supplied", not "zero"."""
        assert quote(config, fair_price=None).net_edge_cents is None


class TestSerialisation:
    def test_money_serialises_as_strings(self, config: Config) -> None:
        """A price that becomes a JS number loses the precision the backend
        preserved."""
        payload = quote(config, fair_price="0.55").as_dict()
        for key in (
            "limit_price",
            "contracts",
            "notional_cents",
            "total_cost_cents",
            "breakeven_cents",
            "net_edge_cents",
        ):
            assert isinstance(payload[key], str), key

    def test_the_wire_form_is_exposed_for_the_confirmation(
        self, config: Config
    ) -> None:
        """The operator can see exactly what will be sent."""
        payload = quote(config, side=Side.NO, action="buy", limit_price="0.30")
        assert payload.as_dict()["wire"] == {
            "book_side": "ask",
            "yes_price": "0.70",
            "count": "100",
        }

    def test_fee_serialises_as_a_string(self, config: Config) -> None:
        """Fees are fractional cents now; an int would round them."""
        assert quote(config).as_dict()["est_fee_cents"] == "175.0000"

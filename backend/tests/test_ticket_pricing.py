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
    UncategorisedMarket,
    UnverifiedFeeCategory,
    taker_fee_cents,
)
from app.db.models import Side
from app.trading.pricing import price_ticket


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {"costs": {"slippage_buffer_cents": 0.0, "assume_taker": True}}
    )


def quote(config: Config, **overrides: object):
    kwargs: dict = {
        "ticker": "TEST-MKT",
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
        assert q.est_fee_cents == taker_fee_cents("0.50", "100", "Sports")
        assert q.est_fee_cents == 175

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
        assert q.est_fee_cents == 174
        assert q.net_edge_cents == Decimal("3.26")

    def test_selling_below_fair_is_a_negative_edge(self, config: Config) -> None:
        q = quote(
            config, action="sell", limit_price="0.45", fair_price="0.55",
            contracts="100",
        )
        assert q.net_edge_cents < 0


class TestFailClosed:
    def test_unverified_category_refuses_to_price(self, config: Config) -> None:
        """A market we cannot fee-price cannot be proposed."""
        with pytest.raises(UnverifiedFeeCategory):
            quote(config, category="Crypto")

    def test_a_market_with_no_category_refuses_to_price(
        self, config: Config
    ) -> None:
        """Regression: found on a live stack.

        Categories are joined from the parent Event after a long sync. Until
        that lands, a Crypto market has ``category = None`` and looks exactly
        like an ordinary one — and the default multiplier priced it happily,
        returning HTTP 200 for a Bitcoin market while ``crypto`` was still
        unverified. "Not looked up yet" is not "ordinary".
        """
        with pytest.raises(UncategorisedMarket):
            quote(config, category=None)

    def test_the_uncategorised_error_is_caught_by_existing_handlers(
        self, config: Config
    ) -> None:
        """It subclasses UnverifiedFeeCategory so every fail-closed path —
        the API's 409, the detector exclusion — catches it unmodified."""
        with pytest.raises(UnverifiedFeeCategory) as exc:
            quote(config, category=None)
        assert exc.value.category == "uncategorised"

    def test_a_recognised_category_still_prices_normally(
        self, config: Config
    ) -> None:
        """The fallback is intact for categories that exist but are not
        premium-rated; only None is refused."""
        q = quote(config, category="Politics")
        assert q.est_fee_cents == taker_fee_cents("0.50", "100", "Politics")


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

    def test_fee_stays_an_integer(self, config: Config) -> None:
        assert isinstance(quote(config).as_dict()["est_fee_cents"], int)

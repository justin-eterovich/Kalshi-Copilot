"""Tests for settlement accounting.

The money question these answer is "what did holding this to resolution
actually earn?", and the failure mode they guard against is a settlement
priced by guesswork — which writes a permanent, wrong number into a book with
no correction path.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.trading.settlements import realized_from_settlement, settled_yes_value


class TestSettledYesValue:
    def test_yes_pays_a_dollar(self) -> None:
        assert settled_yes_value(market_result="yes") == Decimal(1)

    def test_no_pays_nothing(self) -> None:
        assert settled_yes_value(market_result="no") == Decimal(0)

    def test_case_and_whitespace_do_not_matter(self) -> None:
        assert settled_yes_value(market_result=" YES ") == Decimal(1)

    def test_a_scalar_prefers_the_markets_fixed_point_value(self) -> None:
        """``value`` is integer cents and has already lost the precision."""
        assert settled_yes_value(
            market_result="scalar",
            value_cents=63,
            settlement_value=Decimal("0.634200"),
        ) == Decimal("0.634200")

    def test_a_scalar_falls_back_to_the_cents_field(self) -> None:
        assert settled_yes_value(market_result="scalar", value_cents=63) == Decimal(
            "0.63"
        )

    def test_a_scalar_with_no_value_at_all_is_refused(self) -> None:
        assert settled_yes_value(market_result="scalar") is None

    def test_an_unsettled_market_is_refused(self) -> None:
        assert settled_yes_value(market_result=None) is None
        assert settled_yes_value(market_result="") is None

    def test_a_void_result_is_refused(self) -> None:
        """Voiding returns the stake; it is not a payout and has no price."""
        assert settled_yes_value(market_result="void") is None

    def test_an_unknown_result_is_refused(self) -> None:
        """Not an invitation to assume it went to zero."""
        assert settled_yes_value(market_result="somethingnew") is None


class TestRealizedFromSettlement:
    def test_a_winning_yes_position_earns_the_rest_of_the_dollar(self) -> None:
        # 10 YES bought at 40c, settles YES: 60c of profit each.
        assert realized_from_settlement(
            net_contracts=Decimal(10),
            avg_price=Decimal("0.40"),
            payout_yes=Decimal(1),
        ) == Decimal(600)

    def test_a_losing_yes_position_loses_its_cost(self) -> None:
        assert realized_from_settlement(
            net_contracts=Decimal(10),
            avg_price=Decimal("0.40"),
            payout_yes=Decimal(0),
        ) == Decimal(-400)

    def test_a_winning_no_position_earns_through_the_sign(self) -> None:
        """5 NO at 30c is carried as -5 @ 0.70. Settling NO pays 70c each."""
        assert realized_from_settlement(
            net_contracts=Decimal(-5),
            avg_price=Decimal("0.70"),
            payout_yes=Decimal(0),
        ) == Decimal(350)

    def test_a_losing_no_position_loses_its_cost(self) -> None:
        assert realized_from_settlement(
            net_contracts=Decimal(-5),
            avg_price=Decimal("0.70"),
            payout_yes=Decimal(1),
        ) == Decimal(-150)

    def test_a_flat_position_realises_nothing(self) -> None:
        assert realized_from_settlement(
            net_contracts=Decimal(0),
            avg_price=Decimal("0.40"),
            payout_yes=Decimal(1),
        ) == Decimal(0)

    def test_fractional_contracts_stay_exact(self) -> None:
        """Counts go to 0.01 and prices to six decimals; neither may round."""
        realized = realized_from_settlement(
            net_contracts=Decimal("2.50"),
            avg_price=Decimal("0.401234"),
            payout_yes=Decimal(1),
        )
        assert realized == Decimal("149.6915")

    def test_a_scalar_settlement_can_be_a_partial_win(self) -> None:
        # Bought at 40c, settled at 63c: 23c each.
        assert realized_from_settlement(
            net_contracts=Decimal(10),
            avg_price=Decimal("0.40"),
            payout_yes=Decimal("0.63"),
        ) == Decimal(230)

    @pytest.mark.parametrize("payout", [Decimal(0), Decimal(1)])
    def test_a_hedged_pair_nets_to_the_same_loss_either_way(
        self, payout: Decimal
    ) -> None:
        """Buying both legs of a binary at prices summing over $1 loses the
        excess whichever way it resolves — the shape of a bad set arbitrage.

        10 YES at 55c and 10 NO at 50c: one leg pays $1, so 105c of cost
        returns 100c, a 50c loss across 10 contracts however it lands.
        """
        yes_leg = realized_from_settlement(
            net_contracts=Decimal(10), avg_price=Decimal("0.55"), payout_yes=payout
        )
        no_leg = realized_from_settlement(
            net_contracts=Decimal(-10), avg_price=Decimal("0.50"), payout_yes=payout
        )
        assert yes_leg + no_leg == Decimal(-50)

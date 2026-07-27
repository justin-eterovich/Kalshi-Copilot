"""Tests for signed position and realised-P&L accounting.

Positions are one signed number per market in YES-equivalent contracts.  The
cases that break naive implementations:

- Buying NO must *offset* a YES position, not sit beside it.
- Reducing a position realises P&L; the surviving lot keeps its original
  cost basis rather than being re-averaged.
- Crossing through flat realises on the closed part only, and re-opens the
  remainder at the fill price. Averaging across a flip carries a long's basis
  into a short, which is meaningless.
- Realised P&L is fractional. Sub-cent ticks and fractional contracts make it
  so, and rounding each realisation to whole cents accumulates error in the
  one number the report card is judged on.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import Position, Side
from app.trading.positions import position_view, realized_from_fill


def apply(net: str, avg: str, delta: str, price: str):
    return realized_from_fill(
        net_contracts=Decimal(net),
        avg_price=Decimal(avg),
        delta=Decimal(delta),
        fill_yes_price=Decimal(price),
    )


class TestOpening:
    def test_opening_from_flat_sets_the_average(self) -> None:
        net, avg, realized = apply("0", "0", "10", "0.40")
        assert (net, avg, realized) == (Decimal(10), Decimal("0.40"), Decimal(0))

    def test_opening_short_from_flat(self) -> None:
        """Buying NO at 30c is a short 10 at a YES price of 0.70."""
        net, avg, realized = apply("0", "0", "-10", "0.70")
        assert net == Decimal(-10)
        assert avg == Decimal("0.70")
        assert realized == 0

    def test_adding_averages_the_price(self) -> None:
        net, avg, realized = apply("10", "0.40", "10", "0.50")
        assert net == Decimal(20)
        assert avg == Decimal("0.45")
        assert realized == 0

    def test_adding_to_a_short_averages_on_absolute_size(self) -> None:
        """A short's average is still a price, never a negative number."""
        net, avg, _ = apply("-10", "0.70", "-10", "0.80")
        assert net == Decimal(-20)
        assert avg == Decimal("0.75")


class TestReducing:
    def test_closing_a_winner_realises_the_gain(self) -> None:
        net, avg, realized = apply("10", "0.40", "-10", "0.50")
        assert net == 0
        assert avg == 0
        assert realized == Decimal("100.00")  # a 10c move on 10 contracts

    def test_closing_a_loser_realises_the_loss(self) -> None:
        _, _, realized = apply("10", "0.40", "-10", "0.30")
        assert realized == Decimal("-100.00")

    def test_partial_close_keeps_the_original_basis(self) -> None:
        """The surviving lot was bought at the old price, not the new one."""
        net, avg, realized = apply("10", "0.40", "-4", "0.50")
        assert net == Decimal(6)
        assert avg == Decimal("0.40")
        assert realized == Decimal("40.00")

    def test_closing_a_short_profits_when_the_price_falls(self) -> None:
        """Long NO at a YES price of 0.70; YES falls to 0.60, NO gained."""
        net, avg, realized = apply("-10", "0.70", "10", "0.60")
        assert net == 0
        assert realized == Decimal("100.00")

    def test_closing_a_short_loses_when_the_price_rises(self) -> None:
        _, _, realized = apply("-10", "0.70", "10", "0.80")
        assert realized == Decimal("-100.00")


class TestCrossingThroughFlat:
    def test_realises_only_the_closed_part(self) -> None:
        net, avg, realized = apply("10", "0.40", "-25", "0.50")
        assert net == Decimal(-15)
        # 10 closed at a 10c gain; the other 15 is a new short.
        assert realized == Decimal("100.00")

    def test_reopens_the_remainder_at_the_fill_price(self) -> None:
        """Carrying the old basis across a flip would be nonsense."""
        _, avg, _ = apply("10", "0.40", "-25", "0.50")
        assert avg == Decimal("0.50")


class TestNetting:
    def test_buying_no_offsets_a_yes_position(self) -> None:
        """The property the signed representation exists for."""
        net, _, _ = apply("10", "0.40", "-10", "0.40")
        assert net == 0

    def test_a_zero_fill_changes_nothing(self) -> None:
        assert apply("10", "0.40", "0", "0.90") == (
            Decimal(10),
            Decimal("0.40"),
            Decimal(0),
        )


class TestFractionalPrecision:
    def test_sub_cent_move_on_a_fractional_size_is_kept_exactly(self) -> None:
        """Half a contract for a 1c move earns half a cent.

        Rounding this to a whole cent — in either direction — is the kind of
        drift that makes a report card untrustworthy after a few hundred
        trades.
        """
        _, _, realized = apply("0.50", "0.40", "-0.50", "0.41")
        assert realized == Decimal("0.5000")

    def test_six_decimal_prices_survive(self) -> None:
        _, _, realized = apply("1", "0.400000", "-1", "0.400001")
        assert realized == Decimal("0.0001")

    def test_repeated_small_realisations_do_not_drift(self) -> None:
        """Ten half-cent gains are exactly five cents, not four or six."""
        total = Decimal(0)
        for _ in range(10):
            _, _, realized = apply("0.50", "0.40", "-0.50", "0.41")
            total += realized
        assert total == Decimal("5.0000")


class TestPositionView:
    def test_a_long_reads_as_yes(self) -> None:
        position = Position(
            ticker="T", route="simulated", is_paper=True,
            net_contracts=Decimal(10), avg_price=Decimal("0.40"),
            realized_pnl_cents=Decimal(0), fees_paid_cents=Decimal(0),
        )
        view = position_view(position, None)
        assert view["side"] == "yes"
        assert view["contracts"] == "10"
        assert view["avg_price"] == "0.40"

    def test_a_short_reads_as_no_at_the_complement(self) -> None:
        """Stored as -10 @ 0.70; a trader reads '10 NO at 30c'."""
        position = Position(
            ticker="T", route="simulated", is_paper=True,
            net_contracts=Decimal(-10), avg_price=Decimal("0.70"),
            realized_pnl_cents=Decimal(0), fees_paid_cents=Decimal(0),
        )
        view = position_view(position, None)
        assert view["side"] == "no"
        assert view["contracts"] == "10"
        assert view["avg_price"] == "0.30"
        # The signed form stays available for reconciliation.
        assert view["net_contracts"] == "-10"
        assert view["avg_yes_price"] == "0.70"

    def test_unrealised_pnl_marks_a_long(self) -> None:
        position = Position(
            ticker="T", route="simulated", is_paper=True,
            net_contracts=Decimal(10), avg_price=Decimal("0.40"),
            realized_pnl_cents=Decimal(0), fees_paid_cents=Decimal(0),
        )
        view = position_view(position, Decimal("0.50"))
        assert view["unrealized_pnl_cents"] == "100.00"

    def test_unrealised_pnl_marks_a_short_the_other_way(self) -> None:
        position = Position(
            ticker="T", route="simulated", is_paper=True,
            net_contracts=Decimal(-10), avg_price=Decimal("0.70"),
            realized_pnl_cents=Decimal(0), fees_paid_cents=Decimal(0),
        )
        view = position_view(position, Decimal("0.60"))
        assert view["unrealized_pnl_cents"] == "100.00"

    def test_no_mark_means_no_invented_number(self) -> None:
        """A market with no quote does not get a made-up valuation."""
        position = Position(
            ticker="T", route="simulated", is_paper=True,
            net_contracts=Decimal(10), avg_price=Decimal("0.40"),
            realized_pnl_cents=Decimal(0), fees_paid_cents=Decimal(0),
        )
        assert position_view(position, None)["unrealized_pnl_cents"] is None

    def test_money_serialises_as_strings(self) -> None:
        position = Position(
            ticker="T", route="simulated", is_paper=True,
            net_contracts=Decimal("0.50"), avg_price=Decimal("0.405"),
            realized_pnl_cents=Decimal("1.5"), fees_paid_cents=Decimal(3),
        )
        view = position_view(position, Decimal("0.5"))
        for key in ("net_contracts", "avg_price", "realized_pnl_cents"):
            assert isinstance(view[key], str), key


def test_side_enum_round_trip() -> None:
    assert Side("yes") is Side.YES

"""The database layer of the backtester.

Most of this module is queries, which the live run exercises. What is tested
here is the handful of places it *translates* — and translation is where a
backtester goes quietly wrong, because a mistranslated outcome or a
misassembled book produces a plausible number rather than an error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest import replay as rp
from app.backtest.engine import _resolved, paper_filler

T0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class TestResolvedOutcome:
    """`Market.result` is a tri-state carried in a string column."""

    def test_settled_outcomes_map_both_ways(self) -> None:
        assert _resolved("yes") is True
        assert _resolved("no") is False

    def test_an_open_market_is_the_empty_string_not_null(self) -> None:
        """153,808 rows in this catalog carry `''`, not NULL.

        `is not None` would call every open market settled — and since a
        settled market is ground truth for the replay, that would hand the
        backtester 150,000 fabricated outcomes.
        """
        assert _resolved("") is None
        assert _resolved(None) is None
        assert _resolved("   ") is None

    def test_an_unrecognised_result_is_not_guessed_at(self) -> None:
        """Void and scalar payouts are not a 'no'.

        A void returns the stake; scoring it as a loss would bias every
        strategy that touches thin markets, which is most of them.
        """
        assert _resolved("void") is None
        assert _resolved("scalar") is None

    @pytest.mark.parametrize("value", ["YES", "Yes", " yes "])
    def test_case_and_whitespace_do_not_change_the_answer(self, value: str) -> None:
        assert _resolved(value) is True


class TestPaperFiller:
    """The backtest must not get its own, friendlier, fill model."""

    def _obs(self, book: dict | None) -> rp.Observation:
        return rp.Observation(ts=T0, ticker="MKT-A", book=book)

    def _intent(self, price: str = "0.60") -> rp.Intent:
        return rp.Intent(
            ticker="MKT-A",
            side=rp.Side.YES,
            action="buy",
            limit_price=Decimal(price),
            contracts=Decimal(10),
        )

    def test_no_stored_book_fills_nothing(self) -> None:
        """An instant we hold no book for is not an instant we can trade in.

        Filling anyway would be inventing a price — and with a median gap of
        85 minutes between snapshots in this deployment, most instants are
        ones we hold no book for.
        """
        assert paper_filler()(self._intent(), self._obs(None)) == []

    def test_it_charges_a_fee_per_price_level(self) -> None:
        """Per level, because that is per fill, because that is how Kalshi
        rounds. A single fee at the blended price is a different number."""
        book = {
            "yes": [],
            # Both sides quoted as bids. A NO bid at 0.45 is a YES offer at
            # 0.55, which is what a YES buy lifts.
            "no": [["0.45", "4"], ["0.42", "8"]],
        }
        fills = paper_filler()(self._intent(), self._obs(book))
        assert len(fills) == 2
        assert [f.price for f in fills] == [Decimal("0.55"), Decimal("0.58")]
        assert all(f.fee_cents > 0 for f in fills)
        # Fractional to a centicent, never a whole number of cents.
        assert any(f.fee_cents != f.fee_cents.to_integral_value() for f in fills)

    def test_it_never_fills_through_the_limit(self) -> None:
        book = {"yes": [], "no": [["0.30", "100"]]}  # YES offered at 0.70
        assert paper_filler()(self._intent("0.60"), self._obs(book)) == []

    def test_slippage_is_adverse_and_can_prevent_a_fill(self) -> None:
        """Configured pessimism makes a marginal trade *not fill*, rather
        than fill at a fiction. Same behaviour as live paper trading."""
        book = {"yes": [], "no": [["0.41", "50"]]}  # YES offered at 0.59
        assert paper_filler()(self._intent("0.60"), self._obs(book))
        assert (
            paper_filler(slippage_cents=Decimal(5))(
                self._intent("0.60"), self._obs(book)
            )
            == []
        )


class TestObservationShape:
    """The engine reassembles a book from two columns; replay consumes it."""

    def test_replay_accepts_what_the_engine_produces(self) -> None:
        """An end-to-end shape check across the module boundary.

        The two halves are developed separately and are pure, so nothing else
        would notice if the dict key the engine writes stopped being the one
        the simulator reads.
        """
        obs = [
            rp.Observation(
                ts=T0 + timedelta(minutes=i),
                ticker="MKT-A",
                book={"yes": [], "no": [["0.05", "50"]]},
            )
            for i in range(3)
        ]

        bought: list[str] = []

        def strategy(state: rp.StrategyState) -> list[rp.Intent]:
            # Once ever, not "once while flat". After settlement the position
            # is gone from `state.positions`, so a flat check would re-buy a
            # market whose answer is already public — and replay raises
            # `LookAheadError` rather than letting that produce a number.
            if state.observation.ticker in bought:
                return []
            bought.append(state.observation.ticker)
            return [
                rp.Intent(
                    ticker="MKT-A",
                    side=rp.Side.YES,
                    action="buy",
                    limit_price=Decimal("0.95"),
                    contracts=Decimal(10),
                )
            ]

        result = rp.replay(
            obs,
            strategy=strategy,
            filler=paper_filler(),
            outcomes={
                "MKT-A": rp.Outcome(
                    ticker="MKT-A",
                    settled_yes=True,
                    settled_at=T0 + timedelta(minutes=2),
                )
            },
        )
        assert result.filled_intents == 1
        assert result.settlements == 1
        # Bought YES at 0.95, settled YES: 5c a contract on 10, less fees.
        assert result.realized_pnl_cents > 0
        assert result.fees_paid_cents > 0

"""Tests for the pure event-replay core.

The arithmetic here is the easy part. What these tests are really pinning is
that the replay loop cannot show the strategy the future, because that is the
failure mode that produces a beautiful equity curve and a losing account:

- an unsorted stream is refused rather than quietly sorted;
- a naive timestamp is refused, because it has no defined ordering;
- the state object handed to the strategy has **no route to an outcome**,
  asserted structurally over the type graph rather than by reading the code;
- a position still open at the end is reported unsettled and marked to
  nothing at all.

The other half is the money. Three formulas in ``replay.py`` are deliberate
copies of code that cannot be imported without dragging in SQLAlchemy, so
``TestMirrorsAgreeWithTheOriginals`` diffs every copy against its original.
CLAUDE.md is explicit that nothing downstream catches an inverted direction —
``P*(1-P)`` is symmetric, so a flipped trade produces the same fee, the same
notional and a plausible confirmation — which makes that differential test the
only thing standing between a drifted copy and a backtest of the opposite
strategy.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, get_args, get_origin, get_type_hints

import pytest

from app.backtest.replay import (
    ClosedTrade,
    ExecutedFill,
    FillMismatch,
    Intent,
    LookAheadError,
    NaiveTimestamp,
    Observation,
    OpenPosition,
    Outcome,
    ReplayError,
    ReplayResult,
    Side,
    StrategyState,
    UnsettledPosition,
    UnsortedObservations,
    replay,
)

TICKER = "KXTEST-26JUL20-A"
OTHER = "KXTEST-26JUL20-B"
START = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

BOOK: dict[str, Any] = {
    "yes": [["0.4000", "100.00"], ["0.3900", "250.00"]],
    "no": [["0.5500", "80.00"], ["0.5400", "300.00"]],
}


# ---------------------------------------------------------------------------
# Fixtures and tiny helpers
# ---------------------------------------------------------------------------


def at(minutes: int) -> datetime:
    return START + timedelta(minutes=minutes)


def obs(
    minutes: int,
    ticker: str = TICKER,
    *,
    book: dict[str, Any] | None = None,
    last_price: Decimal | None = None,
    ts: datetime | None = None,
) -> Observation:
    return Observation(
        ts=ts if ts is not None else at(minutes),
        ticker=ticker,
        book=book,
        last_price=last_price,
    )


def buy_yes(
    price: str = "0.40", contracts: str = "10", ticker: str = TICKER
) -> Intent:
    return Intent(
        ticker=ticker,
        side=Side.YES,
        action="buy",
        limit_price=Decimal(price),
        contracts=Decimal(contracts),
    )


def buy_no(price: str = "0.30", contracts: str = "5", ticker: str = TICKER) -> Intent:
    """"Buy 5 NO at 30c" — limit_price is on the TRADED side, so 0.30."""
    return Intent(
        ticker=ticker,
        side=Side.NO,
        action="buy",
        limit_price=Decimal(price),
        contracts=Decimal(contracts),
    )


class OnceStrategy:
    """Submits its intents at the first observation, then nothing. Records state."""

    def __init__(self, *intents: Intent) -> None:
        self.intents = list(intents)
        self.fired = False
        self.states: list[StrategyState] = []

    def __call__(self, state: StrategyState) -> Sequence[Intent]:
        self.states.append(state)
        if self.fired:
            return []
        self.fired = True
        return self.intents


def never(state: StrategyState) -> Sequence[Intent]:
    return []


def fill_completely(
    fee_cents: str = "1.75", price: str | None = None
) -> Any:
    """A filler that fills the whole intent at its limit, for a fixed fee.

    Trivial on purpose: replay must not care how fills are produced, and a
    filler that models nothing is the cleanest way to show that the accounting
    under test is replay's and not the simulator's.
    """

    def _filler(
        intent: Intent, observation: Observation
    ) -> Sequence[ExecutedFill]:
        return [
            ExecutedFill(
                ticker=intent.ticker,
                side=intent.side,
                action=intent.action,
                price=Decimal(price) if price else intent.limit_price,
                contracts=intent.contracts,
                fee_cents=Decimal(fee_cents),
            )
        ]

    return _filler


def fill_nothing(intent: Intent, observation: Observation) -> Sequence[ExecutedFill]:
    return []


def yes_at(minutes: int, ticker: str = TICKER) -> dict[str, Outcome]:
    return {ticker: Outcome(ticker=ticker, settled_yes=True, settled_at=at(minutes))}


def no_at(minutes: int, ticker: str = TICKER) -> dict[str, Outcome]:
    return {ticker: Outcome(ticker=ticker, settled_yes=False, settled_at=at(minutes))}


# ---------------------------------------------------------------------------
# The stream itself
# ---------------------------------------------------------------------------


class TestStreamOrdering:
    """Replay verifies the ordering it depends on rather than assuming it."""

    def test_an_unsorted_stream_raises_and_names_the_problem(self) -> None:
        """Sorting silently would hide the caller bug *and* its look-ahead."""
        stream = [obs(0), obs(5), obs(3)]

        with pytest.raises(UnsortedObservations) as excinfo:
            replay(stream, strategy=never, filler=fill_nothing)

        message = str(excinfo.value)
        assert "not sorted" in message
        assert TICKER in message
        # Both timestamps appear, so the report says where to look.
        assert at(3).isoformat() in message
        assert at(5).isoformat() in message

    def test_unsorted_is_a_replay_error(self) -> None:
        with pytest.raises(ReplayError):
            replay([obs(5), obs(0)], strategy=never, filler=fill_nothing)

    def test_equal_timestamps_are_allowed(self) -> None:
        """Several markets legitimately share one instant; only going back is a bug."""
        result = replay(
            [obs(0, TICKER), obs(0, OTHER)], strategy=never, filler=fill_nothing
        )
        assert result.observations == 2

    def test_a_naive_timestamp_raises(self) -> None:
        naive = datetime(2026, 7, 20, 12, 0)  # noqa: DTZ001 - the point of the test

        with pytest.raises(NaiveTimestamp) as excinfo:
            replay([obs(0, ts=naive)], strategy=never, filler=fill_nothing)

        assert "naive" in str(excinfo.value)
        assert "timezone-aware" in str(excinfo.value)

    def test_a_naive_settlement_timestamp_raises(self) -> None:
        outcomes = {
            TICKER: Outcome(
                ticker=TICKER,
                settled_yes=True,
                settled_at=datetime(2026, 7, 20, 13, 0),  # noqa: DTZ001
            )
        }
        with pytest.raises(NaiveTimestamp):
            replay(
                [obs(0)], strategy=never, filler=fill_nothing, outcomes=outcomes
            )

    def test_an_outcome_keyed_under_the_wrong_ticker_raises(self) -> None:
        """A mis-keyed outcome settles a different market, correctly and wrongly."""
        outcomes = {
            OTHER: Outcome(ticker=TICKER, settled_yes=True, settled_at=at(1))
        }
        with pytest.raises(ReplayError) as excinfo:
            replay(
                [obs(0)], strategy=never, filler=fill_nothing, outcomes=outcomes
            )
        assert "same market" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The look-ahead guarantee, pinned structurally
# ---------------------------------------------------------------------------


def _flatten(hint: object) -> list[object]:
    """Every type mentioned by ``hint``, including generic parameters."""
    args = get_args(hint)
    if not args:
        return [hint]
    out: list[object] = [get_origin(hint)]
    for arg in args:
        out.extend(_flatten(arg))
    return out


def _reachable_types(cls: type, seen: set[object] | None = None) -> set[object]:
    """Every type reachable from ``cls`` by following dataclass fields."""
    seen = set() if seen is None else seen
    if cls in seen:
        return seen
    seen.add(cls)
    for hint in get_type_hints(cls).values():
        for candidate in _flatten(hint):
            if candidate in seen or candidate is None:
                continue
            if dataclasses.is_dataclass(candidate) and isinstance(candidate, type):
                _reachable_types(candidate, seen)
            else:
                seen.add(candidate)
    return seen


def _walk(obj: object, depth: int = 0) -> list[object]:
    """Every value reachable from ``obj`` at runtime, to a bounded depth."""
    found: list[object] = [obj]
    if depth >= 5:
        return found
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            found.extend(_walk(getattr(obj, f.name), depth + 1))
    elif isinstance(obj, Mapping):
        for value in obj.values():
            found.extend(_walk(value, depth + 1))
    elif isinstance(obj, list | tuple):
        for value in obj:
            found.extend(_walk(value, depth + 1))
    return found


class TestTheStrategyCannotSeeTheFuture:
    """The guarantee is enforced by the type, so it is asserted on the type."""

    def test_state_has_exactly_three_fields(self) -> None:
        assert set(StrategyState.__slots__) == {"now", "observation", "positions"}

    def test_no_type_reachable_from_the_state_is_an_outcome(self) -> None:
        """Follow every dataclass field transitively; Outcome must never appear."""
        reachable = _reachable_types(StrategyState)

        assert Observation in reachable
        assert OpenPosition in reachable
        assert Outcome not in reachable

    def test_no_field_anywhere_in_the_state_graph_names_a_result(self) -> None:
        """A field called ``result`` or ``settled_*`` would be the leak."""
        forbidden = ("outcome", "settle", "result", "resolution", "payout")
        for cls in (StrategyState, Observation, OpenPosition):
            for f in dataclasses.fields(cls):
                assert not any(word in f.name.lower() for word in forbidden), (
                    f"{cls.__name__}.{f.name} looks like it carries the answer"
                )

    def test_no_outcome_instance_is_reachable_at_runtime(self) -> None:
        """Even with outcomes supplied, none of them reaches the strategy."""
        strategy = OnceStrategy(buy_yes())
        replay(
            [obs(m, book=BOOK) for m in (0, 10, 20)],
            strategy=strategy,
            filler=fill_completely(),
            outcomes=yes_at(15),
        )

        assert strategy.states, "the strategy was never called"
        for state in strategy.states:
            for value in _walk(state):
                assert not isinstance(value, Outcome)

    def test_the_position_view_is_read_only(self) -> None:
        strategy = OnceStrategy(buy_yes())
        replay(
            [obs(0, book=BOOK), obs(10, book=BOOK)],
            strategy=strategy,
            filler=fill_completely(),
        )

        positions = strategy.states[-1].positions
        assert isinstance(positions, MappingProxyType)
        with pytest.raises(TypeError):
            positions[TICKER] = OpenPosition(  # type: ignore[index]
                ticker=TICKER, net_contracts=Decimal(1), avg_price=Decimal("0.5")
            )

    def test_the_position_view_is_a_snapshot_not_a_live_reference(self) -> None:
        """A state kept from tick 1 must not silently show tick 3's book."""
        strategy = OnceStrategy(buy_yes())
        replay(
            [obs(0, book=BOOK), obs(10), obs(20)],
            strategy=strategy,
            filler=fill_completely(),
            outcomes=yes_at(15),
        )

        after_entry = strategy.states[1].positions
        after_settlement = strategy.states[2].positions
        assert TICKER in after_entry
        assert after_settlement == {}
        # The earlier snapshot is unchanged by the settlement that followed it.
        assert after_entry[TICKER].net_contracts == Decimal(10)

    def test_trading_a_market_that_has_already_settled_raises(self) -> None:
        """The resolution is known by then, so any P&L from it is look-ahead."""

        def always(state: StrategyState) -> Sequence[Intent]:
            return [buy_yes()]

        with pytest.raises(LookAheadError) as excinfo:
            replay(
                [obs(0, book=BOOK), obs(20, book=BOOK)],
                strategy=always,
                filler=fill_completely(),
                outcomes=yes_at(10),
            )

        assert "look-ahead" in str(excinfo.value)

    def test_a_strategy_may_only_act_on_the_market_it_is_shown(self) -> None:
        """The filler gets one book; filling A from B's book is a different asset."""

        def wrong_market(state: StrategyState) -> Sequence[Intent]:
            return [buy_yes(ticker=OTHER)]

        with pytest.raises(ReplayError) as excinfo:
            replay(
                [obs(0, book=BOOK)],
                strategy=wrong_market,
                filler=fill_completely(),
            )
        assert OTHER in str(excinfo.value)


# ---------------------------------------------------------------------------
# The copied formulas must not drift from their originals
# ---------------------------------------------------------------------------


class TestMirrorsAgreeWithTheOriginals:
    """``replay.py`` copies three formulas it cannot import. Diff them all.

    Tests have no purity constraint, so they can import the SQLAlchemy-bound
    originals and compare. If a copy ever drifts, this fails rather than the
    backtest quietly reporting a different strategy than the one that ran.
    """

    def test_signed_contracts_matches_direction_for_all_four_combinations(
        self,
    ) -> None:
        from app.backtest.replay import _signed_contracts
        from app.db.models import Side as OrmSide
        from app.trading.direction import signed_contracts

        for side, orm_side in ((Side.YES, OrmSide.YES), (Side.NO, OrmSide.NO)):
            for action in ("buy", "sell"):
                for count in (Decimal("10"), Decimal("2.50")):
                    assert _signed_contracts(side, action, count) == signed_contracts(
                        orm_side, action, count
                    ), f"{action} {side} x{count} disagrees with direction.py"

    def test_signed_contracts_gets_the_no_side_right(self) -> None:
        """Buying NO is a *negative* YES-equivalent delta. Inverting is silent."""
        from app.backtest.replay import _signed_contracts

        assert _signed_contracts(Side.NO, "buy", Decimal(5)) == Decimal(-5)
        assert _signed_contracts(Side.NO, "sell", Decimal(5)) == Decimal(5)
        assert _signed_contracts(Side.YES, "buy", Decimal(5)) == Decimal(5)
        assert _signed_contracts(Side.YES, "sell", Decimal(5)) == Decimal(-5)

    def test_to_yes_price_matches_direction(self) -> None:
        from app.backtest.replay import _to_yes_price
        from app.db.models import Side as OrmSide
        from app.trading.direction import to_yes_price

        for price in ("0.30", "0.5600", "0.999999"):
            assert _to_yes_price(Side.NO, Decimal(price)) == to_yes_price(
                OrmSide.NO, Decimal(price)
            )
            assert _to_yes_price(Side.YES, Decimal(price)) == to_yes_price(
                OrmSide.YES, Decimal(price)
            )

    @pytest.mark.parametrize(
        ("net", "avg", "delta", "price"),
        [
            ("0", "0", "10", "0.40"),  # open long
            ("0", "0", "-10", "0.70"),  # open short (buy NO at 30c)
            ("10", "0.40", "10", "0.50"),  # add
            ("-10", "0.70", "-10", "0.80"),  # add to short
            ("10", "0.40", "-10", "0.50"),  # close a winner
            ("10", "0.40", "-4", "0.30"),  # partial close, a loser
            ("10", "0.40", "-25", "0.55"),  # cross through flat
            ("-8", "0.70", "3", "0.62"),  # partial close of a short
            ("2.5", "0.4", "-2.5", "0.55"),  # fractional
        ],
    )
    def test_realized_from_fill_matches_positions(
        self, net: str, avg: str, delta: str, price: str
    ) -> None:
        from app.backtest.replay import _realized_from_fill
        from app.trading.positions import realized_from_fill

        kwargs = {
            "net_contracts": Decimal(net),
            "avg_price": Decimal(avg),
            "delta": Decimal(delta),
            "fill_yes_price": Decimal(price),
        }
        assert _realized_from_fill(**kwargs) == realized_from_fill(**kwargs)

    @pytest.mark.parametrize(
        ("net", "avg", "payout"),
        [
            ("10", "0.40", "1"),
            ("10", "0.40", "0"),
            ("-5", "0.70", "0"),
            ("-5", "0.70", "1"),
            ("2.50", "0.3333", "1"),
        ],
    )
    def test_realized_from_settlement_matches_settlements(
        self, net: str, avg: str, payout: str
    ) -> None:
        from app.backtest.replay import _realized_from_settlement
        from app.trading.settlements import realized_from_settlement

        kwargs = {
            "net_contracts": Decimal(net),
            "avg_price": Decimal(avg),
            "payout_yes": Decimal(payout),
        }
        assert _realized_from_settlement(**kwargs) == realized_from_settlement(**kwargs)


# ---------------------------------------------------------------------------
# Positions, settlement, and the money
# ---------------------------------------------------------------------------


class TestSettlement:
    def test_a_long_yes_held_to_a_yes_settlement(self) -> None:
        """Realises (1.00 - entry) * contracts * 100 cents, minus the entry fee.

        And **no exit fee**: a position held to resolution is not traded out,
        so there is no second fill to charge. ``round_trip_cost_cents`` in
        ``app/core/fees.py`` documents the same rule.
        """
        strategy = OnceStrategy(buy_yes(price="0.40", contracts="10"))
        result = replay(
            [obs(0, book=BOOK), obs(10), obs(20)],
            strategy=strategy,
            filler=fill_completely(fee_cents="1.75"),
            outcomes=yes_at(15),
            starting_equity_cents=Decimal(0),
        )

        assert result.realized_pnl_cents == Decimal("600.00")  # 0.60 * 10 * 100
        assert result.fees_paid_cents == Decimal("1.75")
        assert result.ending_equity_cents == Decimal("598.25")
        assert result.settlements == 1
        assert result.unsettled == ()

        # Exactly one fill: the entry. A second one would be a phantom exit.
        assert len(result.fills) == 1
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.kind == "settlement"
        assert trade.exit_yes_price == Decimal(1)
        assert trade.entry_yes_price == Decimal("0.40")
        assert trade.contracts == Decimal(10)

    def test_a_no_position_settling_no_is_a_win_and_the_sign_works_out(self) -> None:
        """5 NO bought at 30c is carried as -5 @ 0.70 and pays +350c on a NO.

        Nothing downstream would catch this being inverted: the fee formula is
        symmetric in P and 1-P, so a flipped direction produces the same fee,
        the same notional, and a plausible confirmation.
        """
        strategy = OnceStrategy(buy_no(price="0.30", contracts="5"))
        result = replay(
            [obs(0, book=BOOK), obs(20)],
            strategy=strategy,
            filler=fill_completely(fee_cents="0.7350"),
            outcomes=no_at(15),
        )

        # The position, before it settled, in YES-equivalents.
        held = strategy.states[0]  # entry has not happened yet at state 0
        assert held.positions == {}
        assert result.trades[0].entry_yes_price == Decimal("0.70")
        assert result.trades[0].contracts == Decimal(5)

        assert result.realized_pnl_cents == Decimal("350.00")
        assert result.ending_equity_cents == Decimal("349.2650")

    def test_a_no_position_settling_yes_is_a_loss(self) -> None:
        """The mirror of the win: -5 @ 0.70 against a $1.00 payout is -150c."""
        strategy = OnceStrategy(buy_no(price="0.30", contracts="5"))
        result = replay(
            [obs(0, book=BOOK), obs(20)],
            strategy=strategy,
            filler=fill_completely(fee_cents="0"),
            outcomes=yes_at(15),
        )
        assert result.realized_pnl_cents == Decimal("-150.00")

    def test_a_long_yes_settling_no_loses_the_stake(self) -> None:
        strategy = OnceStrategy(buy_yes(price="0.40", contracts="10"))
        result = replay(
            [obs(0, book=BOOK), obs(20)],
            strategy=strategy,
            filler=fill_completely(fee_cents="0"),
            outcomes=no_at(15),
        )
        assert result.realized_pnl_cents == Decimal("-400.00")

    def test_settlement_happens_before_the_strategy_is_asked(self) -> None:
        """The strategy sees the position gone, not a ghost it could trade."""
        strategy = OnceStrategy(buy_yes())
        replay(
            [obs(0, book=BOOK), obs(10), obs(15)],
            strategy=strategy,
            filler=fill_completely(),
            outcomes=yes_at(15),
        )

        assert strategy.states[1].positions[TICKER].net_contracts == Decimal(10)
        # At exactly settled_at the market has resolved, so the book is flat.
        assert strategy.states[2].positions == {}

    def test_an_outcome_after_the_last_observation_is_never_applied(self) -> None:
        """Replay time only advances from observations. It never runs off the end."""
        strategy = OnceStrategy(buy_yes())
        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=strategy,
            filler=fill_completely(fee_cents="1.75"),
            outcomes=yes_at(999),
        )

        assert result.settlements == 0
        assert result.realized_pnl_cents == Decimal(0)
        assert len(result.unsettled) == 1

    def test_settlement_of_a_market_we_never_held_changes_nothing(self) -> None:
        result = replay(
            [obs(0), obs(20)],
            strategy=never,
            filler=fill_nothing,
            outcomes=yes_at(10),
        )
        assert result.settlements == 0
        assert result.realized_pnl_cents == Decimal(0)


class TestTradingOut:
    def test_closing_by_trading_realises_against_the_average_and_pays_both_fees(
        self,
    ) -> None:
        """A round-trip closed in the market pays an exit fee; settlement does not."""

        class Strategy:
            def __init__(self) -> None:
                self.tick = 0

            def __call__(self, state: StrategyState) -> Sequence[Intent]:
                self.tick += 1
                if self.tick == 1:
                    return [buy_yes(price="0.40", contracts="10")]
                if self.tick == 2:
                    return [
                        Intent(
                            ticker=TICKER,
                            side=Side.YES,
                            action="sell",
                            limit_price=Decimal("0.50"),
                            contracts=Decimal(10),
                        )
                    ]
                return []

        result = replay(
            [obs(0, book=BOOK), obs(10, book=BOOK), obs(20)],
            strategy=Strategy(),
            filler=fill_completely(fee_cents="1.75"),
        )

        assert result.realized_pnl_cents == Decimal("100.00")  # a 10c move on 10
        assert result.fees_paid_cents == Decimal("3.50")  # both sides charged
        assert result.ending_equity_cents == Decimal("96.50")
        assert result.unsettled == ()
        assert [t.kind for t in result.trades] == ["fill"]

    def test_a_partial_close_keeps_the_surviving_lot_at_its_original_basis(
        self,
    ) -> None:
        class Strategy:
            def __init__(self) -> None:
                self.tick = 0

            def __call__(self, state: StrategyState) -> Sequence[Intent]:
                self.tick += 1
                if self.tick == 1:
                    return [buy_yes(price="0.40", contracts="10")]
                if self.tick == 2:
                    return [
                        Intent(
                            ticker=TICKER,
                            side=Side.YES,
                            action="sell",
                            limit_price=Decimal("0.50"),
                            contracts=Decimal(4),
                        )
                    ]
                return []

        strategy = Strategy()
        result = replay(
            [obs(0, book=BOOK), obs(10, book=BOOK), obs(20)],
            strategy=strategy,
            filler=fill_completely(fee_cents="0"),
        )

        assert result.realized_pnl_cents == Decimal("40.00")
        assert len(result.unsettled) == 1
        remaining = result.unsettled[0]
        assert remaining.net_contracts == Decimal(6)
        assert remaining.avg_price == Decimal("0.40")

    def test_buying_no_offsets_a_yes_position_rather_than_sitting_beside_it(
        self,
    ) -> None:
        """The whole reason positions are one signed number per market."""

        class Strategy:
            def __init__(self) -> None:
                self.tick = 0

            def __call__(self, state: StrategyState) -> Sequence[Intent]:
                self.tick += 1
                if self.tick == 1:
                    return [buy_yes(price="0.40", contracts="10")]
                if self.tick == 2:
                    # Buying NO at 50c is selling YES at 50c: it closes.
                    return [buy_no(price="0.50", contracts="10")]
                return []

        result = replay(
            [obs(0, book=BOOK), obs(10, book=BOOK), obs(20)],
            strategy=Strategy(),
            filler=fill_completely(fee_cents="0"),
        )

        assert result.unsettled == ()
        assert result.realized_pnl_cents == Decimal("100.00")


class TestFractionalContracts:
    def test_fractional_size_survives_end_to_end(self) -> None:
        """Contracts are fractional to 0.01. Rounding one to an integer is a bug."""
        strategy = OnceStrategy(buy_yes(price="0.40", contracts="2.50"))
        result = replay(
            [obs(0, book=BOOK), obs(20)],
            strategy=strategy,
            filler=fill_completely(fee_cents="0.4375"),
            outcomes=yes_at(15),
        )

        assert result.fills[0][1].contracts == Decimal("2.50")
        assert result.trades[0].contracts == Decimal("2.50")
        assert result.realized_pnl_cents == Decimal("150.000")  # 0.60 * 2.50 * 100
        assert result.ending_equity_cents == Decimal("149.5625")

    def test_a_fractional_partial_close_realises_a_fractional_amount(self) -> None:
        """Sub-cent realisations are real and must not be rounded away."""

        class Strategy:
            def __init__(self) -> None:
                self.tick = 0

            def __call__(self, state: StrategyState) -> Sequence[Intent]:
                self.tick += 1
                if self.tick == 1:
                    return [buy_yes(price="0.4000", contracts="0.50")]
                if self.tick == 2:
                    return [
                        Intent(
                            ticker=TICKER,
                            side=Side.YES,
                            action="sell",
                            limit_price=Decimal("0.4100"),
                            contracts=Decimal("0.50"),
                        )
                    ]
                return []

        result = replay(
            [obs(0, book=BOOK), obs(10, book=BOOK)],
            strategy=Strategy(),
            filler=fill_completely(fee_cents="0"),
        )
        # 1c on half a contract is half a cent, and stays half a cent.
        assert result.realized_pnl_cents == Decimal("0.5000")


class TestUnsettled:
    def test_an_open_position_is_reported_and_contributes_no_realised_pnl(
        self,
    ) -> None:
        """Marking it to the last quote would invent a profit nobody took."""
        strategy = OnceStrategy(buy_yes(price="0.40", contracts="10"))
        result = replay(
            [obs(0, book=BOOK), obs(10, last_price=Decimal("0.95"))],
            strategy=strategy,
            filler=fill_completely(fee_cents="1.75"),
        )

        assert result.realized_pnl_cents == Decimal(0)
        # Fees are still real: they were charged when the position was opened.
        assert result.ending_equity_cents == Decimal("-1.75")
        assert len(result.unsettled) == 1

        held = result.unsettled[0]
        assert held == UnsettledPosition(
            ticker=TICKER,
            net_contracts=Decimal(10),
            avg_price=Decimal("0.40"),
            cost_basis_cents=Decimal("400.00"),
        )

    def test_an_open_no_position_reports_the_cash_it_actually_paid(self) -> None:
        """5 NO at 30c cost 150c, not the signed -350c the YES numbers suggest."""
        strategy = OnceStrategy(buy_no(price="0.30", contracts="5"))
        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=strategy,
            filler=fill_completely(fee_cents="0"),
        )

        held = result.unsettled[0]
        assert held.net_contracts == Decimal(-5)
        assert held.avg_price == Decimal("0.70")
        assert held.cost_basis_cents == Decimal("150.00")

    def test_unsettled_positions_are_ordered_by_ticker(self) -> None:
        """Determinism: dict order is a property of the caller, not the data."""

        def both(state: StrategyState) -> Sequence[Intent]:
            if state.observation.ticker in (TICKER, OTHER):
                return [buy_yes(ticker=state.observation.ticker)]
            return []

        result = replay(
            [obs(0, OTHER, book=BOOK), obs(1, TICKER, book=BOOK)],
            strategy=both,
            filler=fill_completely(fee_cents="0"),
        )
        assert [p.ticker for p in result.unsettled] == sorted([TICKER, OTHER])


# ---------------------------------------------------------------------------
# The equity curve
# ---------------------------------------------------------------------------


class TestEquityCurve:
    def test_one_point_per_observation_in_time_order(self) -> None:
        """``stats.max_drawdown`` consumes this, so its shape is documented API."""
        stream = [obs(m, book=BOOK) for m in (0, 5, 5, 30, 90)]
        strategy = OnceStrategy(buy_yes())
        result = replay(
            stream,
            strategy=strategy,
            filler=fill_completely(fee_cents="1.75"),
            outcomes=yes_at(60),
        )

        assert len(result.equity_curve) == len(stream) == result.observations
        timestamps = [ts for ts, _ in result.equity_curve]
        assert timestamps == [o.ts for o in stream]
        assert all(
            earlier <= later
            for earlier, later in zip(timestamps, timestamps[1:], strict=False)
        )

    def test_every_equity_point_is_a_decimal_in_cents(self) -> None:
        result = replay(
            [obs(0, book=BOOK), obs(20)],
            strategy=OnceStrategy(buy_yes()),
            filler=fill_completely(fee_cents="1.75"),
            outcomes=yes_at(10),
        )
        assert all(isinstance(value, Decimal) for _, value in result.equity_curve)
        assert result.equity_curve[-1][1] == result.ending_equity_cents

    def test_the_curve_starts_from_the_supplied_equity(self) -> None:
        result = replay(
            [obs(0), obs(10)],
            strategy=never,
            filler=fill_nothing,
            starting_equity_cents=Decimal("100000.00"),
        )
        assert [value for _, value in result.equity_curve] == [
            Decimal("100000.00"),
            Decimal("100000.00"),
        ]
        assert result.ending_equity_cents == Decimal("100000.00")

    def test_fees_move_the_curve_down_on_the_tick_they_are_charged(self) -> None:
        """A strategy that pays fees for a position it never closes has lost money."""
        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=OnceStrategy(buy_yes()),
            filler=fill_completely(fee_cents="1.75"),
            starting_equity_cents=Decimal(0),
        )
        assert [value for _, value in result.equity_curve] == [
            Decimal("-1.75"),
            Decimal("-1.75"),
        ]

    def test_an_empty_stream_produces_an_empty_curve(self) -> None:
        result = replay(
            [],
            strategy=never,
            filler=fill_nothing,
            starting_equity_cents=Decimal("500"),
        )
        assert result.equity_curve == ()
        assert result.ending_equity_cents == Decimal("500")
        assert result.observations == 0


# ---------------------------------------------------------------------------
# Fail-closed guards on the injected filler
# ---------------------------------------------------------------------------


class TestFillerGuards:
    """The filler is injected, so an adapter bug lands here or nowhere."""

    def _run(self, bad_fill: ExecutedFill) -> ReplayResult:
        def filler(
            intent: Intent, observation: Observation
        ) -> Sequence[ExecutedFill]:
            return [bad_fill]

        return replay(
            [obs(0, book=BOOK)],
            strategy=OnceStrategy(buy_yes(price="0.40", contracts="10")),
            filler=filler,
        )

    def test_an_inverted_side_raises(self) -> None:
        """The one bug nothing downstream catches: the fee formula is symmetric."""
        with pytest.raises(FillMismatch) as excinfo:
            self._run(
                ExecutedFill(
                    ticker=TICKER,
                    side=Side.NO,
                    action="buy",
                    price=Decimal("0.40"),
                    contracts=Decimal(10),
                    fee_cents=Decimal("1.75"),
                )
            )
        assert "inverted" in str(excinfo.value)

    def test_an_inverted_action_raises(self) -> None:
        with pytest.raises(FillMismatch):
            self._run(
                ExecutedFill(
                    ticker=TICKER,
                    side=Side.YES,
                    action="sell",
                    price=Decimal("0.40"),
                    contracts=Decimal(10),
                    fee_cents=Decimal("1.75"),
                )
            )

    def test_a_fill_on_another_ticker_raises(self) -> None:
        with pytest.raises(FillMismatch):
            self._run(
                ExecutedFill(
                    ticker=OTHER,
                    side=Side.YES,
                    action="buy",
                    price=Decimal("0.40"),
                    contracts=Decimal(10),
                    fee_cents=Decimal("1.75"),
                )
            )

    def test_a_fill_through_the_buy_limit_raises(self) -> None:
        """Price improvement a real limit order would never have got."""
        with pytest.raises(FillMismatch) as excinfo:
            self._run(
                ExecutedFill(
                    ticker=TICKER,
                    side=Side.YES,
                    action="buy",
                    price=Decimal("0.55"),
                    contracts=Decimal(10),
                    fee_cents=Decimal("1.75"),
                )
            )
        assert "through the buy limit" in str(excinfo.value)

    def test_a_fill_at_a_price_outside_zero_to_one_raises(self) -> None:
        with pytest.raises(FillMismatch):
            self._run(
                ExecutedFill(
                    ticker=TICKER,
                    side=Side.YES,
                    action="buy",
                    price=Decimal(0),
                    contracts=Decimal(10),
                    fee_cents=Decimal(0),
                )
            )

    def test_a_negative_fee_raises(self) -> None:
        with pytest.raises(FillMismatch):
            self._run(
                ExecutedFill(
                    ticker=TICKER,
                    side=Side.YES,
                    action="buy",
                    price=Decimal("0.40"),
                    contracts=Decimal(10),
                    fee_cents=Decimal("-1"),
                )
            )

    def test_overfilling_the_intent_raises(self) -> None:
        def filler(
            intent: Intent, observation: Observation
        ) -> Sequence[ExecutedFill]:
            return [
                ExecutedFill(
                    ticker=intent.ticker,
                    side=intent.side,
                    action=intent.action,
                    price=Decimal("0.40"),
                    contracts=Decimal(8),
                    fee_cents=Decimal(0),
                )
                for _ in range(2)
            ]

        with pytest.raises(FillMismatch) as excinfo:
            replay(
                [obs(0, book=BOOK)],
                strategy=OnceStrategy(buy_yes(contracts="10")),
                filler=filler,
            )
        assert "overfilled" in str(excinfo.value)

    def test_a_rejected_batch_leaves_the_book_untouched(self) -> None:
        """Validate everything before applying anything: no half-updated book."""
        applied: list[StrategyState] = []

        def filler(
            intent: Intent, observation: Observation
        ) -> Sequence[ExecutedFill]:
            good = ExecutedFill(
                ticker=intent.ticker,
                side=intent.side,
                action=intent.action,
                price=Decimal("0.40"),
                contracts=Decimal(4),
                fee_cents=Decimal(0),
            )
            bad = dataclasses.replace(good, ticker=OTHER)
            return [good, bad]

        def strategy(state: StrategyState) -> Sequence[Intent]:
            applied.append(state)
            return [buy_yes()]

        with pytest.raises(FillMismatch):
            replay([obs(0, book=BOOK)], strategy=strategy, filler=filler)
        assert applied[0].positions == {}

    def test_a_partial_fill_is_a_legitimate_outcome(self) -> None:
        def filler(
            intent: Intent, observation: Observation
        ) -> Sequence[ExecutedFill]:
            return [
                ExecutedFill(
                    ticker=intent.ticker,
                    side=intent.side,
                    action=intent.action,
                    price=Decimal("0.40"),
                    contracts=Decimal("3.25"),
                    fee_cents=Decimal("0.5687"),
                )
            ]

        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=OnceStrategy(buy_yes(contracts="10")),
            filler=filler,
        )
        assert result.filled_intents == 1
        assert result.unsettled[0].net_contracts == Decimal("3.25")

    def test_no_fill_at_all_is_not_an_error(self) -> None:
        result = replay(
            [obs(0, book=BOOK)],
            strategy=OnceStrategy(buy_yes()),
            filler=fill_nothing,
        )
        assert result.intents == 1
        assert result.filled_intents == 0
        assert result.fills == ()
        assert result.unsettled == ()


class TestMoneyTypes:
    """Money is Decimal. A float or a bare int is refused, not coerced."""

    def test_a_float_price_is_refused(self) -> None:
        with pytest.raises(ReplayError) as excinfo:
            replay(
                [obs(0, book=BOOK)],
                strategy=OnceStrategy(
                    Intent(
                        ticker=TICKER,
                        side=Side.YES,
                        action="buy",
                        limit_price=0.40,  # type: ignore[arg-type]
                        contracts=Decimal(10),
                    )
                ),
                filler=fill_completely(),
            )
        assert "Decimal" in str(excinfo.value)

    def test_an_int_price_is_refused(self) -> None:
        """``40`` almost always meant 40 cents. The API has no integer cents."""
        with pytest.raises(ReplayError):
            replay(
                [obs(0, book=BOOK)],
                strategy=OnceStrategy(
                    Intent(
                        ticker=TICKER,
                        side=Side.YES,
                        action="buy",
                        limit_price=40,  # type: ignore[arg-type]
                        contracts=Decimal(10),
                    )
                ),
                filler=fill_completely(),
            )

    def test_a_limit_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ReplayError):
            replay(
                [obs(0, book=BOOK)],
                strategy=OnceStrategy(buy_yes(price="1.40")),
                filler=fill_completely(),
            )

    def test_a_non_positive_size_is_refused(self) -> None:
        with pytest.raises(ReplayError):
            replay(
                [obs(0, book=BOOK)],
                strategy=OnceStrategy(buy_yes(contracts="0")),
                filler=fill_completely(),
            )

    def test_a_float_starting_equity_is_refused(self) -> None:
        with pytest.raises(ReplayError):
            replay(
                [obs(0)],
                strategy=never,
                filler=fill_nothing,
                starting_equity_cents=100.0,  # type: ignore[arg-type]
            )

    def test_a_string_side_is_accepted_and_normalised(self) -> None:
        """The engine may hand over an ORM ``Side``; both are StrEnum over yes/no."""
        intent = Intent(
            ticker=TICKER,
            side="no",  # type: ignore[arg-type]
            action="buy",
            limit_price=Decimal("0.30"),
            contracts=Decimal(5),
        )
        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=OnceStrategy(intent),
            filler=fill_completely(fee_cents="0"),
        )
        assert result.unsettled[0].net_contracts == Decimal(-5)

    def test_an_orm_side_is_accepted(self) -> None:
        from app.db.models import Side as OrmSide

        intent = Intent(
            ticker=TICKER,
            side=OrmSide.NO,  # type: ignore[arg-type]
            action="buy",
            limit_price=Decimal("0.30"),
            contracts=Decimal(5),
        )
        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=OnceStrategy(intent),
            filler=fill_completely(fee_cents="0"),
        )
        assert result.unsettled[0].avg_price == Decimal("0.70")


# ---------------------------------------------------------------------------
# Reproducibility, and the interface the real engine will use
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_the_same_inputs_produce_an_identical_result(self) -> None:
        """Same inputs, same outputs, always — no clock, no set iteration order."""

        def run() -> ReplayResult:
            return replay(
                [obs(m, book=BOOK) for m in (0, 5, 10, 20)],
                strategy=OnceStrategy(buy_yes()),
                filler=fill_completely(fee_cents="1.75"),
                outcomes=yes_at(15),
                starting_equity_cents=Decimal("1000"),
            )

        assert run() == run()

    def test_the_module_reads_no_clock(self) -> None:
        """A backtest that consults the wall clock is not reproducible.

        Checked over the parsed syntax tree rather than the text, so that
        prose about ``datetime.now()`` in a docstring does not fail the test
        and — more importantly — a real call cannot hide inside a comment-free
        string that a text search would miss.
        """
        import ast
        import inspect

        from app.backtest import replay as module

        tree = ast.parse(inspect.getsource(module))
        clocks = {"now", "utcnow", "today", "monotonic", "perf_counter", "time"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in clocks, (
                    f"replay.py calls .{node.func.attr}() — replay time must "
                    "come only from the observation stream"
                )

    def test_the_module_imports_nothing_outside_the_standard_library(self) -> None:
        """Purity is the contract: no db, no network, no settings, no I/O."""
        import inspect

        from app.backtest import replay as module

        for line in inspect.getsource(module).splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "app." not in stripped, f"non-pure import: {stripped}"
                assert "sqlalchemy" not in stripped, f"non-pure import: {stripped}"


class TestAgainstTheRealFillSimulator:
    """Replay must fit ``paper.simulate_fills`` without either side bending.

    The fee engine is stubbed with a hand-built schedule rather than the YAML
    file, because the fee math already has its own tests in ``test_fees.py``
    and this one is about the *interface*: that a ``SimulatedFill`` maps onto
    an ``ExecutedFill`` with no reinterpretation, and that replay's guards
    accept what the real simulator produces.
    """

    @pytest.fixture(autouse=True)
    def _schedule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core import fees

        schedule = fees.FeeSchedule.from_dict(
            {"meta": {"verified_on": "2026-01-01", "schedule_revision": "test"}}
        )
        monkeypatch.setattr(fees, "load_fee_schedule", lambda *a, **k: schedule)

    def test_a_paper_filler_adapter_drives_replay_end_to_end(self) -> None:
        from app.trading.paper import simulate_fills

        def paper_filler(
            intent: Intent, observation: Observation
        ) -> Sequence[ExecutedFill]:
            if observation.book is None:
                return []
            return [
                ExecutedFill(
                    ticker=intent.ticker,
                    side=intent.side,
                    action=intent.action,
                    price=sim.price,
                    contracts=sim.contracts,
                    fee_cents=sim.fee_cents,
                )
                for sim in simulate_fills(
                    book=observation.book,
                    side=intent.side.value,
                    action=intent.action,
                    limit_price=intent.limit_price,
                    contracts=intent.contracts,
                    ticker=intent.ticker,
                )
            ]

        # The fixture book offers YES at 0.45 (80 available) then 0.46 (300),
        # so 100 contracts walk two levels and produce two fills.
        result = replay(
            [obs(0, book=BOOK), obs(20)],
            strategy=OnceStrategy(buy_yes(price="0.50", contracts="100")),
            filler=paper_filler,
            outcomes=yes_at(10),
        )

        assert len(result.fills) == 2
        assert [f.price for _, f in result.fills] == [Decimal("0.45"), Decimal("0.46")]
        assert [f.contracts for _, f in result.fills] == [Decimal(80), Decimal(20)]

        # Blended entry: (80*0.45 + 20*0.46) / 100 = 0.4520, settling at $1.00.
        assert result.trades[0].entry_yes_price == Decimal("0.4520")
        assert result.realized_pnl_cents == Decimal("5480.00")
        assert result.fees_paid_cents > 0
        assert result.settlements == 1

    def test_a_no_intent_through_the_real_simulator_lands_on_the_short_side(
        self,
    ) -> None:
        """Buy NO -> the YES bids -> a negative YES-equivalent position."""
        from app.trading.paper import simulate_fills

        def paper_filler(
            intent: Intent, observation: Observation
        ) -> Sequence[ExecutedFill]:
            assert observation.book is not None
            return [
                ExecutedFill(
                    ticker=intent.ticker,
                    side=intent.side,
                    action=intent.action,
                    price=sim.price,
                    contracts=sim.contracts,
                    fee_cents=sim.fee_cents,
                )
                for sim in simulate_fills(
                    book=observation.book,
                    side=intent.side.value,
                    action=intent.action,
                    limit_price=intent.limit_price,
                    contracts=intent.contracts,
                    ticker=intent.ticker,
                )
            ]

        # Buying NO lifts the NO offer, which is 1 - 0.40 = 0.60.
        result = replay(
            [obs(0, book=BOOK), obs(10)],
            strategy=OnceStrategy(buy_no(price="0.65", contracts="10")),
            filler=paper_filler,
        )

        held = result.unsettled[0]
        assert held.net_contracts == Decimal(-10)
        assert held.avg_price == Decimal("0.40")  # NO at 0.60 is YES at 0.40
        assert held.cost_basis_cents == Decimal("600.0")


class TestCounters:
    def test_counters_describe_the_run(self) -> None:
        result = replay(
            [obs(m, book=BOOK) for m in (0, 5, 10, 20)],
            strategy=OnceStrategy(buy_yes()),
            filler=fill_completely(fee_cents="1.75"),
            outcomes=yes_at(15),
        )
        assert result.observations == 4
        assert result.intents == 1
        assert result.filled_intents == 1
        assert result.settlements == 1
        assert isinstance(result.trades[0], ClosedTrade)

    def test_a_strategy_returning_none_means_do_nothing(self) -> None:
        def quiet(state: StrategyState) -> None:
            return None

        result = replay([obs(0), obs(1)], strategy=quiet, filler=fill_nothing)
        assert result.intents == 0
        assert result.observations == 2

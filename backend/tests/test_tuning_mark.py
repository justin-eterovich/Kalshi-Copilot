"""Tests for pick marking.

Two classes of bug matter here and they fail in opposite directions.

The **flattering** one: marking at the mid, or forgetting a fee, or booking an
unscoreable pick as a break-even zero. Every one of those makes a detector look
better than it is, and the tuner would then tighten thresholds towards more of
whatever produced them.

The **inverting** one: reading the wrong side of the book for the exit. That
one is worse, because it is undetectable downstream — the P(1-P) fee formula is
symmetric, so a flipped direction produces the same fee, the same notional and
a perfectly plausible number with the wrong sign.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core.fees import FeeSchedule, taker_fee_cents
from app.db.models import Side
from app.tuning.mark import (
    MarkBasis,
    Pick,
    Quote,
    Unmarkable,
    mark_pick,
)

VERIFIED = FeeSchedule.from_dict(
    {
        "meta": {"verified_on": "2026-07-27"},
        "formula": {"base_taker_rate": "0.07", "base_maker_rate": "0.0175"},
        "defaults": {"taker_multiplier": 1, "maker_multiplier": 0},
        "series": {"KXFREE": {"maker": 0, "taker": 0}},
    }
)

CREATED = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
TICKER = "KXNFLGAME-26AUG13ARILV-ARI"
# A series with no listed multiplier, so it takes the documented defaults:
# taker M=1, maker M=0. Fees are real here, which is the point.
FREE_TICKER = "KXFREE-26AUG13-A"


def pick(
    *,
    side: Side = Side.YES,
    action: str = "buy",
    entry: str | None = "0.26",
    claimed: str = "10",
    contracts: str | None = "10",
    ticker: str = TICKER,
    proposed: bool = False,
) -> Pick:
    return Pick(
        signal_id=1,
        detector="stale_quote",
        ticker=ticker,
        side=side,
        action=action,
        created_at=CREATED,
        claimed_edge_cents=Decimal(claimed),
        confidence=0.5,
        entry_price=None if entry is None else Decimal(entry),
        contracts=None if contracts is None else Decimal(contracts),
        event_ticker="KXNFLGAME-26AUG13ARILV",
        became_proposal=proposed,
    )


def quote(**kw: object) -> Quote:
    base: dict = {
        "ticker": TICKER,
        "yes_bid": Decimal("0.40"),
        "yes_ask": Decimal("0.44"),
        "no_bid": Decimal("0.56"),
        "no_ask": Decimal("0.60"),
    }
    base.update(kw)
    return Quote(**base)  # type: ignore[arg-type]


def mark(p: Pick, q: Quote | None, **kw: object):
    kwargs: dict = {"schedule": VERIFIED, "slippage_cents": 0}
    kwargs.update(kw)
    return mark_pick(p, q, **kwargs)


class TestExitSide:
    """Which quote closes a position. Get this wrong and the sign flips."""

    def test_long_yes_exits_at_the_yes_bid(self) -> None:
        m = mark(pick(side=Side.YES, action="buy"), quote())
        assert m.exit_price == Decimal("0.40")

    def test_long_no_exits_at_the_no_bid(self) -> None:
        m = mark(pick(side=Side.NO, action="buy"), quote())
        assert m.exit_price == Decimal("0.56")

    def test_short_yes_exits_at_the_yes_ask(self) -> None:
        m = mark(pick(side=Side.YES, action="sell"), quote())
        assert m.exit_price == Decimal("0.44")

    def test_short_no_exits_at_the_no_ask(self) -> None:
        m = mark(pick(side=Side.NO, action="sell"), quote())
        assert m.exit_price == Decimal("0.60")

    def test_the_mid_is_never_used(self) -> None:
        """A long is marked at the bid, which is strictly worse than the mid.

        Marking at the mid would credit half the spread as profit on every
        pick — and these detectors trade wide books.
        """
        m = mark(pick(side=Side.YES, action="buy"), quote())
        mid = (Decimal("0.40") + Decimal("0.44")) / 2

        assert m.exit_price is not None
        assert m.exit_price < mid


class TestDirection:
    def test_a_long_that_rose_makes_money(self) -> None:
        m = mark(pick(entry="0.26"), quote())  # exits at 0.40

        assert m.gross_cents == Decimal("140")  # 14c * 10 contracts
        assert m.net_per_contract_cents is not None
        assert m.net_per_contract_cents > 0

    def test_a_short_that_rose_loses_money(self) -> None:
        """Same market move, opposite direction, opposite sign."""
        m = mark(pick(entry="0.26", action="sell"), quote())  # exits at 0.44

        assert m.gross_cents == Decimal("-180")  # -18c * 10
        assert m.net_per_contract_cents is not None
        assert m.net_per_contract_cents < 0


class TestCosts:
    def test_a_flat_move_still_loses_the_fees(self) -> None:
        """The stale_quote lesson, in one assertion.

        Live, that detector's entire -46.21c over 13 trades is exactly the
        46.21c of fees it paid: gross was flat and costs ate all of it. A mark
        that is not fee-aware scores this pick at zero and calls the detector
        healthy.
        """
        # Entry equals the exit quote, so the gross move is precisely zero.
        m = mark(pick(entry="0.40"), quote())

        assert m.gross_cents == Decimal(0)
        assert m.fee_cents is not None and m.fee_cents > 0
        assert m.net_cents == -m.fee_cents
        assert m.net_per_contract_cents is not None
        assert m.net_per_contract_cents < 0

    def test_net_is_gross_less_fees_less_slippage(self) -> None:
        m = mark(pick(), quote(), slippage_cents=Decimal("0.5"))

        assert m.gross_cents is not None
        assert m.fee_cents is not None
        assert m.slippage_cents is not None
        assert m.net_cents == m.gross_cents - m.fee_cents - m.slippage_cents

    def test_fees_are_charged_on_both_legs_of_an_open_mark(self) -> None:
        p = pick(entry="0.26", ticker=FREE_TICKER)
        q = quote(ticker=FREE_TICKER)
        m = mark(p, q)

        expected = taker_fee_cents(
            Decimal("0.26"), Decimal("10"), "KXFREE", VERIFIED
        ) + taker_fee_cents(Decimal("0.40"), Decimal("10"), "KXFREE", VERIFIED)
        assert m.fee_cents == expected

    def test_slippage_is_adverse_in_both_directions(self) -> None:
        """A long sells lower; a short buys higher. Never the flattering way."""
        long_mark = mark(pick(action="buy"), quote(), slippage_cents=Decimal("1"))
        short_mark = mark(pick(action="sell"), quote(), slippage_cents=Decimal("1"))

        assert long_mark.exit_price == Decimal("0.39")  # 0.40 - 1c
        assert short_mark.exit_price == Decimal("0.45")  # 0.44 + 1c

    def test_slippage_is_not_charged_on_the_entry(self) -> None:
        """The detector already netted slippage out of its claimed edge.

        Charging it twice shifts every detector by a constant, which moves no
        threshold in the right direction and makes the claimed-vs-realised gap
        unreadable.
        """
        m = mark(pick(contracts="10"), quote(), slippage_cents=Decimal("0.5"))

        # One leg's worth: 0.5c * 10 contracts, not two legs' worth.
        assert m.slippage_cents == Decimal("5.0")


class TestSettlement:
    def test_a_winning_yes_pays_one_and_owes_no_exit_fee(self) -> None:
        p = pick(side=Side.YES, entry="0.26", ticker=FREE_TICKER)
        m = mark(p, quote(ticker=FREE_TICKER, resolved_outcome=True))

        assert m.basis is MarkBasis.SETTLED
        assert m.exit_price == Decimal(1)
        # Entry fee only: a position held to resolution pays no exit fee.
        assert m.fee_cents == taker_fee_cents(
            Decimal("0.26"), Decimal("10"), "KXFREE", VERIFIED
        )
        assert m.slippage_cents == Decimal(0)

    def test_a_winning_no_pays_one(self) -> None:
        """Resolution NO with a NO position is a win, not a loss.

        The tri-state trap: `resolved_outcome is False` means settled NO, not
        "not settled".
        """
        m = mark(
            pick(side=Side.NO, entry="0.30"),
            quote(resolved_outcome=False),
        )

        assert m.basis is MarkBasis.SETTLED
        assert m.exit_price == Decimal(1)
        assert m.gross_cents == Decimal("700")  # (1 - 0.30) * 100 * 10

    def test_a_losing_pick_pays_nothing(self) -> None:
        m = mark(pick(side=Side.YES, entry="0.26"), quote(resolved_outcome=False))

        assert m.exit_price == Decimal(0)
        assert m.gross_cents == Decimal("-260")

    def test_a_short_that_expires_worthless_wins(self) -> None:
        m = mark(
            pick(side=Side.YES, action="sell", entry="0.26"),
            quote(resolved_outcome=False),
        )

        assert m.gross_cents == Decimal("260")  # sold at 0.26, settled at 0

    def test_an_open_market_is_marked_open_not_settled(self) -> None:
        m = mark(pick(), quote())

        assert m.basis is MarkBasis.OPEN


class TestRefusals:
    """An unscoreable pick is never a P&L of zero."""

    def test_no_executable_price_is_refused(self) -> None:
        m = mark(pick(entry=None), quote())

        assert m.basis is MarkBasis.UNMARKABLE
        assert m.refusal is Unmarkable.NO_ENTRY_PRICE
        assert m.net_cents is None
        assert m.scoreable is False

    @pytest.mark.parametrize("bad", ["0", "1", "-0.2", "1.5"])
    def test_a_price_outside_the_open_interval_is_refused(self, bad: str) -> None:
        m = mark(pick(entry=bad), quote())

        assert m.refusal is Unmarkable.ENTRY_PRICE_INVALID

    def test_a_missing_market_is_refused(self) -> None:
        m = mark(pick(), None)

        assert m.refusal is Unmarkable.MARKET_MISSING

    def test_a_void_resolution_is_refused_not_scored_as_zero(self) -> None:
        """A void is the absence of an outcome, not an outcome of zero."""
        m = mark(pick(), quote(is_void=True, resolved_outcome=None))

        assert m.refusal is Unmarkable.MARKET_VOID
        assert m.net_cents is None

    def test_a_zero_bid_is_refused_rather_than_booked_as_a_total_loss(self) -> None:
        """A zero bid is the absence of a buyer, not a price of zero.

        Marking against it books a 26c loss on a position that could not have
        been exited at all — fiction, and fiction that would push the tuner to
        avoid illiquid markets for a reason that never happened.
        """
        m = mark(pick(entry="0.26"), quote(yes_bid=Decimal("0")))

        assert m.refusal is Unmarkable.NO_EXIT_QUOTE
        assert m.net_cents is None

    def test_a_missing_bid_is_refused(self) -> None:
        m = mark(pick(entry="0.26"), quote(yes_bid=None))

        assert m.refusal is Unmarkable.NO_EXIT_QUOTE

    def test_the_other_side_being_empty_does_not_block_a_long(self) -> None:
        """Only the side we would exit on has to be quoted."""
        m = mark(pick(side=Side.YES, action="buy"), quote(no_bid=None, yes_ask=None))

        assert m.scoreable is True

    def test_every_money_field_is_none_when_unmarkable(self) -> None:
        """So a consumer that forgets to check gets a TypeError, not a zero."""
        m = mark(pick(entry=None), quote())

        assert m.gross_cents is None
        assert m.fee_cents is None
        assert m.net_per_contract_cents is None
        assert m.edge_error_cents is None


class TestEdgeError:
    def test_over_claiming_reads_positive(self) -> None:
        """The headline number: claimed minus realised, per contract.

        Live this is about +28c for stale_quote — a detector claiming 24.56c
        and realising -3.55c is not slightly optimistic, it is describing a
        different trade from the one that happens.
        """
        # Claims 20c; the market barely moved, so realised is near zero.
        m = mark(pick(entry="0.40", claimed="20"), quote())

        assert m.edge_error_cents is not None
        assert m.edge_error_cents > Decimal("19")

    def test_per_contract_is_independent_of_size(self) -> None:
        """Sizes differ across detectors; the comparable unit is per contract."""
        ten = mark(pick(contracts="10", ticker=FREE_TICKER), quote(ticker=FREE_TICKER))
        one = mark(pick(contracts="1", ticker=FREE_TICKER), quote(ticker=FREE_TICKER))

        assert ten.net_per_contract_cents is not None
        assert one.net_per_contract_cents is not None
        # Not exactly equal — fees round up to a centicent per fill — but the
        # per-contract figures must be within that rounding, not a factor of 10.
        assert abs(ten.net_per_contract_cents - one.net_per_contract_cents) < 1

    def test_a_missing_size_hint_falls_back_to_one_contract(self) -> None:
        m = mark(pick(contracts=None), quote())

        assert m.contracts == Decimal(1)

    def test_a_zero_size_hint_is_refused_not_treated_as_absent(self) -> None:
        """Zero is falsy, and that is the trap.

        A detector that sized a position at nothing has said something. Quietly
        substituting one contract turns a non-trade into a scored trade — the
        same tri-state confusion as `Market.result == ''` reading as settled.
        """
        m = mark(pick(contracts="0"), quote())

        assert m.refusal is Unmarkable.SIZE_INVALID
        assert m.net_cents is None

    def test_a_negative_size_hint_is_refused(self) -> None:
        m = mark(pick(contracts="-5"), quote())

        assert m.refusal is Unmarkable.SIZE_INVALID

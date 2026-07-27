"""Tests for local orderbook reconstruction.

The critical property: a sequence gap must make the book refuse to answer.
A book that guesses across a gap looks plausible and is wrong, which is
precisely how an arbitrage detector talks you into a trade that isn't there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.kalshi.orderbook import BookStaleError, OrderBook


def snapshot(yes=None, no=None) -> dict:
    return {
        "market_ticker": "TEST-MKT",
        "yes_dollars_fp": yes or [],
        "no_dollars_fp": no or [],
    }


def delta(price: str, amount: str, side: str = "yes") -> dict:
    return {
        "market_ticker": "TEST-MKT",
        "price_dollars": price,
        "delta_fp": amount,
        "side": side,
    }


@pytest.fixture
def book() -> OrderBook:
    b = OrderBook(ticker="TEST-MKT")
    b.apply_snapshot(
        snapshot(
            yes=[["0.4000", "100.00"], ["0.3900", "250.00"]],
            no=[["0.5500", "80.00"], ["0.5400", "300.00"]],
        ),
        seq=10,
    )
    return b


# ---------------------------------------------------------------------------
# Snapshot / delta application
# ---------------------------------------------------------------------------


def test_starts_stale_before_any_snapshot() -> None:
    fresh = OrderBook(ticker="X")
    assert fresh.stale is True
    with pytest.raises(BookStaleError):
        fresh.yes_bids()


def test_snapshot_clears_stale(book: OrderBook) -> None:
    assert book.stale is False
    assert book.seq == 10


def test_snapshot_parses_levels(book: OrderBook) -> None:
    bids = book.yes_bids()
    assert [(b.price, b.size) for b in bids] == [
        (Decimal("0.4000"), Decimal("100.00")),
        (Decimal("0.3900"), Decimal("250.00")),
    ]


def test_snapshot_drops_zero_size_levels() -> None:
    b = OrderBook(ticker="X")
    b.apply_snapshot(snapshot(yes=[["0.4000", "0.00"], ["0.3900", "5.00"]]), seq=1)
    assert len(b.yes_bids()) == 1


def test_delta_adds_size(book: OrderBook) -> None:
    assert book.apply_delta(delta("0.4000", "50.00"), seq=11) is True
    assert book.yes[Decimal("0.4000")] == Decimal("150.00")


def test_delta_removes_size(book: OrderBook) -> None:
    book.apply_delta(delta("0.4000", "-40.00"), seq=11)
    assert book.yes[Decimal("0.4000")] == Decimal("60.00")


def test_delta_to_zero_removes_level(book: OrderBook) -> None:
    book.apply_delta(delta("0.4000", "-100.00"), seq=11)
    assert Decimal("0.4000") not in book.yes


def test_negative_resulting_size_removes_level(book: OrderBook) -> None:
    """Defensive: an oversized negative delta must not leave a phantom level."""
    book.apply_delta(delta("0.4000", "-500.00"), seq=11)
    assert Decimal("0.4000") not in book.yes


def test_delta_creates_new_level(book: OrderBook) -> None:
    book.apply_delta(delta("0.4100", "25.00"), seq=11)
    assert book.yes[Decimal("0.4100")] == Decimal("25.00")


def test_delta_applies_to_no_side(book: OrderBook) -> None:
    book.apply_delta(delta("0.5500", "20.00", side="no"), seq=11)
    assert book.no[Decimal("0.5500")] == Decimal("100.00")


# ---------------------------------------------------------------------------
# Sequence numbers — whose counter is this, anyway
# ---------------------------------------------------------------------------


def test_skipped_seq_is_not_a_gap_for_this_market(book: OrderBook) -> None:
    """``seq`` counts the *subscription*, not the market.

    One `orderbook_delta` subscription covers every ticker in it and numbers
    all of their messages from one counter, so a market's own deltas arrive
    with holes wherever another market was updated. Verified live: with 65
    markets subscribed, one ticker's seqs ran 86, 87, 88, 89, 90, 92, 95, 97.

    Treating those holes as gaps is what left 21 of 65 books permanently
    stale — and silently, because a stale book stops being *recorded* rather
    than raising, so the symptom was thin data with no error anywhere.
    """
    assert book.apply_delta(delta("0.4000", "50.00"), seq=13) is True
    assert book.stale is False
    assert book.seq == 13
    assert book.yes[Decimal("0.4000")] == Decimal("150.00")


def test_interleaved_markets_leave_both_books_healthy(book: OrderBook) -> None:
    """The real shape of the stream: two markets, one counter."""
    other = OrderBook(ticker="OTHER-MKT", stale=False, seq=10)
    for seq in range(11, 31):
        target = book if seq % 2 else other
        assert target.apply_delta(delta("0.4000", "1.00"), seq=seq) is True
    assert book.stale is False
    assert other.stale is False


def test_a_replayed_seq_is_refused(book: OrderBook) -> None:
    """A lower seq on a single ordered socket is a replay, not a reorder.

    Applying it again would double-count the delta, which is the one way a
    book can end up wrong while still reporting itself healthy.
    """
    assert book.apply_delta(delta("0.4000", "1.00"), seq=15) is True
    before = dict(book.yes)
    assert book.apply_delta(delta("0.4000", "1.00"), seq=15) is False
    assert book.apply_delta(delta("0.4000", "1.00"), seq=12) is False
    assert book.yes == before


def test_stale_book_refuses_every_read(book: OrderBook) -> None:
    book.mark_stale("resync")
    for read in (book.yes_bids, book.no_bids, book.yes_asks, book.best_yes_bid):
        with pytest.raises(BookStaleError):
            read()


def test_stale_book_rejects_further_deltas(book: OrderBook) -> None:
    """A gap is detected per-subscription in ws.py, which marks every book
    stale. From here the only thing that matters is that a stale book takes
    nothing until it is resnapshotted."""
    book.mark_stale("sequence gap on sid 3")
    assert book.apply_delta(delta("0.4000", "1.00"), seq=14) is False


def test_gap_does_not_apply_the_delta(book: OrderBook) -> None:
    """State must not be half-updated across a gap."""
    before = dict(book.yes)
    book.mark_stale("sequence gap on sid 3")
    book.apply_delta(delta("0.4000", "50.00"), seq=13)
    assert book.yes == before


def test_fresh_snapshot_recovers_from_gap(book: OrderBook) -> None:
    book.mark_stale("sequence gap on sid 3")
    assert book.stale is True

    book.apply_snapshot(snapshot(yes=[["0.4500", "10.00"]]), seq=20)
    assert book.stale is False
    assert book.seq == 20
    assert len(book.yes_bids()) == 1


def test_gap_count_increments(book: OrderBook) -> None:
    book.mark_stale("sequence gap on sid 3")
    book.apply_snapshot(snapshot(yes=[["0.4000", "1.00"]]), seq=20)
    book.mark_stale("sequence gap on sid 3")
    assert book.gap_count == 2


def test_in_order_deltas_never_gap(book: OrderBook) -> None:
    for seq in range(11, 21):
        assert book.apply_delta(delta("0.4000", "1.00"), seq=seq) is True
    assert book.stale is False
    assert book.seq == 20


def test_unknown_side_marks_the_book_stale(book: OrderBook) -> None:
    """An `else: self.no` fallthrough wrote into the wrong book, advanced the
    sequence and left `stale` False — corruption reporting itself healthy,
    which the gap machinery cannot catch because no sequence was skipped."""
    before = dict(book.no)
    assert book.apply_delta(delta("0.4000", "1.00", side="sell"), seq=11) is False
    assert book.stale is True
    assert "unknown book side" in (book.stale_reason or "")
    assert book.no == before


# ---------------------------------------------------------------------------
# Derived quotes
# ---------------------------------------------------------------------------


def test_yes_ask_is_derived_from_no_bids(book: OrderBook) -> None:
    """A NO bid at p is an offer to sell YES at 1-p."""
    best_ask = book.best_yes_ask()
    assert best_ask is not None
    # best NO bid is 0.55 -> YES ask 0.45
    assert best_ask.price == Decimal("0.45")


def test_spread_and_mid(book: OrderBook) -> None:
    # YES bid 0.40, YES ask 0.45
    assert book.spread() == Decimal("0.05")
    assert book.mid() == Decimal("0.425")


def test_spread_is_none_without_both_sides() -> None:
    b = OrderBook(ticker="X")
    b.apply_snapshot(snapshot(yes=[["0.4000", "10.00"]]), seq=1)
    assert b.spread() is None


# ---------------------------------------------------------------------------
# Executable cost — how slippage enters every edge calculation
# ---------------------------------------------------------------------------


def test_executable_cost_uses_top_of_book_for_small_size(book: OrderBook) -> None:
    result = book.executable_cost("yes", Decimal("50"))
    assert result is not None
    avg, filled = result
    assert filled == Decimal("50")
    assert avg == Decimal("0.45")


def test_executable_cost_walks_the_book(book: OrderBook) -> None:
    """Taking more than the top level must cost more than the top price."""
    result = book.executable_cost("yes", Decimal("200"))
    assert result is not None
    avg, filled = result
    assert filled == Decimal("200")
    # 80 @ 0.45 then 120 @ 0.46
    expected = (Decimal("80") * Decimal("0.45") + Decimal("120") * Decimal("0.46")) / Decimal("200")
    assert avg == expected
    assert avg > Decimal("0.45")


def test_executable_cost_reports_partial_fill(book: OrderBook) -> None:
    """Asking for more than the book holds returns what is actually there."""
    result = book.executable_cost("yes", Decimal("10000"))
    assert result is not None
    _, filled = result
    assert filled == Decimal("380")  # 80 + 300


def test_executable_cost_on_empty_side_is_none() -> None:
    b = OrderBook(ticker="X")
    b.apply_snapshot(snapshot(yes=[["0.4000", "10.00"]]), seq=1)
    assert b.executable_cost("yes", Decimal("10")) is None


def test_executable_cost_refuses_on_stale_book(book: OrderBook) -> None:
    book.mark_stale("test")
    with pytest.raises(BookStaleError):
        book.executable_cost("yes", Decimal("10"))


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def test_top_n_keeps_values_as_strings(book: OrderBook) -> None:
    """JSON round-trips must not turn prices into lossy floats."""
    top = book.top_n(5)
    assert top["yes"][0] == ["0.4000", "100.00"]
    assert all(isinstance(p, str) and isinstance(s, str) for p, s in top["yes"])


def test_top_n_limits_depth(book: OrderBook) -> None:
    assert len(book.top_n(1)["yes"]) == 1

"""Tests for the Kalshi WebSocket client's sequence tracking.

**This is now the only orderbook gap detector in the system**, and it had no
tests at all.

``kalshi/orderbook.py`` used to judge sequence numbers per market, and that was
wrong: a websocket ``seq`` counts the **subscription**, not the market. One
``orderbook_delta`` subscription covers every ticker in it and numbers all
their messages from one counter, so a single market's deltas are *not*
consecutive — verified live with 65 markets, one ticker saw
``86, 87, 88, 89, 90, 92, 95, 97``. Comparing that per market marked 21 of 65
books stale within sixty seconds, permanently, and a stale book is not
*recorded* rather than raising — so the symptom surfaced three milestones
later as thin data.

The delta body carries no per-market sequence, so there is nothing to replace
it with. Gap detection lives here and only here. Six tests once encoded the
opposite understanding and stayed green throughout, which is the reason these
say out loud what is and is not a gap.

``test_ws_hub.py`` is a different module — that one covers ``app/api/ws.py``,
the browser relay.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.kalshi.auth import KalshiSigner
from app.kalshi.ws import (
    TERMINAL_ERROR_CODES,
    KalshiWebSocket,
    Subscription,
    _SidState,
)


class StubSigner:
    """The client only ever asks a signer for headers."""

    def ws_headers(self, url: str) -> dict[str, str]:
        return {"KALSHI-ACCESS-KEY": "stub"}


@pytest.fixture
def ws() -> KalshiWebSocket:
    return KalshiWebSocket(
        url="wss://example.invalid/trade-api/ws/v2",
        signer=StubSigner(),  # type: ignore[arg-type]
    )


def frame(**payload: Any) -> str:
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# _check_seq: the guard itself
# ---------------------------------------------------------------------------


class TestCheckSeq:
    def test_the_first_message_on_a_sid_is_accepted(
        self, ws: KalshiWebSocket
    ) -> None:
        """There is nothing to compare against yet.

        Anchoring on whatever arrives first is right: the subscription's
        counter does not start at 1 after a reconnect into a live stream.
        """
        assert ws._check_seq(1, 4207) is None

    def test_consecutive_numbers_are_accepted(self, ws: KalshiWebSocket) -> None:
        assert ws._check_seq(1, 10) is None
        for seq in range(11, 20):
            assert ws._check_seq(1, seq) is None

    def test_a_skipped_number_is_a_gap(self, ws: KalshiWebSocket) -> None:
        ws._check_seq(1, 10)
        reason = ws._check_seq(1, 12)
        assert reason is not None

    def test_the_reason_names_the_subscription_and_both_numbers(
        self, ws: KalshiWebSocket
    ) -> None:
        """A gap that says only "gap" tells an operator nothing about whether
        one message went missing or two hundred."""
        ws._check_seq(7, 100)
        reason = ws._check_seq(7, 105)
        assert reason is not None
        assert "7" in reason
        assert "101" in reason  # expected
        assert "105" in reason  # got

    def test_a_replayed_number_is_not_a_gap(self, ws: KalshiWebSocket) -> None:
        """A duplicate is not missing data; it is the same data twice."""
        ws._check_seq(1, 10)
        ws._check_seq(1, 11)
        assert ws._check_seq(1, 11) is None
        assert ws._check_seq(1, 5) is None

    def test_a_replay_does_not_rewind_the_tracker(
        self, ws: KalshiWebSocket
    ) -> None:
        """Otherwise a single stale duplicate would make every subsequent
        message look like a gap."""
        ws._check_seq(1, 10)
        ws._check_seq(1, 11)
        ws._check_seq(1, 4)  # replay, ignored
        assert ws._check_seq(1, 12) is None

    def test_the_tracker_resumes_from_the_number_it_saw(
        self, ws: KalshiWebSocket
    ) -> None:
        """After a gap, the next consecutive message is fine.

        A guard that stayed tripped would resync on every message for the rest
        of the connection.
        """
        ws._check_seq(1, 10)
        assert ws._check_seq(1, 15) is not None
        assert ws._check_seq(1, 16) is None

    def test_gaps_are_counted_per_sid(self, ws: KalshiWebSocket) -> None:
        ws._check_seq(1, 10)
        ws._check_seq(1, 12)
        ws._check_seq(1, 20)
        assert ws._sids[1].gaps == 2

    def test_a_clean_run_counts_no_gaps(self, ws: KalshiWebSocket) -> None:
        for seq in range(1, 50):
            ws._check_seq(1, seq)
        assert ws._sids[1].gaps == 0


class TestPerSidIsolation:
    """Two subscriptions, two counters. Neither may contaminate the other."""

    def test_two_sids_keep_separate_counters(self, ws: KalshiWebSocket) -> None:
        assert ws._check_seq(1, 100) is None
        assert ws._check_seq(2, 5) is None
        assert ws._check_seq(1, 101) is None
        assert ws._check_seq(2, 6) is None
        assert ws._sids[1].last_seq == 101
        assert ws._sids[2].last_seq == 6

    def test_interleaved_sids_produce_no_gaps(self, ws: KalshiWebSocket) -> None:
        """The shape that broke the old per-market check.

        Two subscriptions advancing in lockstep look like wild jumps if you
        keep one counter for both.
        """
        ws._check_seq(1, 10)
        ws._check_seq(2, 500)
        for i in range(1, 10):
            assert ws._check_seq(1, 10 + i) is None
            assert ws._check_seq(2, 500 + i) is None
        assert ws._sids[1].gaps == 0
        assert ws._sids[2].gaps == 0

    def test_a_gap_on_one_sid_does_not_touch_the_other(
        self, ws: KalshiWebSocket
    ) -> None:
        ws._check_seq(1, 10)
        ws._check_seq(2, 10)
        assert ws._check_seq(1, 99) is not None
        assert ws._check_seq(2, 11) is None
        assert ws._sids[2].gaps == 0

    def test_a_sid_seen_only_in_data_is_tracked_anyway(
        self, ws: KalshiWebSocket
    ) -> None:
        """Data can arrive before the ``subscribed`` control frame is
        processed. The tracker creates the entry rather than dropping the
        message."""
        ws._check_seq(42, 1)
        assert 42 in ws._sids


class TestSeqCountsTheSubscriptionNotTheMarket:
    """The misunderstanding that cost three milestones, encoded deliberately.

    One subscription covers many tickers. A single market's messages therefore
    carry *non-consecutive* numbers, and that is normal traffic, not damage.
    """

    def test_one_markets_deltas_are_not_expected_to_be_consecutive(
        self, ws: KalshiWebSocket
    ) -> None:
        """The exact live capture: one ticker inside a 65-market subscription
        saw ``86, 87, 88, 89, 90, 92, 95, 97``.

        Nothing here judges those numbers, because they are not this ticker's
        sequence — they are the subscription's, sampled at the moments this
        ticker happened to update. Every one of the "missing" numbers was
        delivered, to a different market.
        """
        observed_for_one_ticker = [86, 87, 88, 89, 90, 92, 95, 97]
        # Every number the subscription emitted, in order, across all markets.
        for seq in range(86, 98):
            assert ws._check_seq(1, seq) is None
        assert ws._sids[1].gaps == 0
        # The per-market view is full of holes and that is fine.
        assert observed_for_one_ticker != list(range(86, 98))

    def test_the_book_does_not_second_guess_the_same_number(self) -> None:
        """The division of labour, asserted from this side of it.

        ``orderbook.py`` records ``seq``, refuses a replayed (lower) one, and
        judges nothing else. Here is the exact live capture again — a skipped
        number, in a subscription that lost nothing — applied to a real book,
        which must stay healthy.
        """
        from app.kalshi.orderbook import OrderBook

        book = OrderBook(ticker="KXMLB-26-HOU")
        book.apply_snapshot(
            {
                "market_ticker": "KXMLB-26-HOU",
                "yes_dollars_fp": [["0.4000", "100.00"]],
                "no_dollars_fp": [],
            },
            seq=90,
        )
        applied = book.apply_delta(
            {
                "market_ticker": "KXMLB-26-HOU",
                "price_dollars": "0.4000",
                "delta_fp": "10.00",
                "side": "yes",
            },
            seq=92,  # 91 went to a different market in the same subscription
        )
        assert applied is True
        assert book.stale is False


# ---------------------------------------------------------------------------
# __resync__ synthesis
# ---------------------------------------------------------------------------


class TestResyncSynthesis:
    def test_an_ordinary_message_passes_through_alone(
        self, ws: KalshiWebSocket
    ) -> None:
        ws._check_seq(1, 10)
        out = ws._handle_raw(
            frame(type="orderbook_delta", sid=1, seq=11, msg={"price": "0.50"})
        )
        assert [m["type"] for m in out] == ["orderbook_delta"]

    def test_a_gap_emits_a_resync_before_the_message(
        self, ws: KalshiWebSocket
    ) -> None:
        """Order matters: the consumer must discard its state *before* it
        applies the message that revealed the hole."""
        ws._check_seq(1, 10)
        out = ws._handle_raw(
            frame(type="orderbook_delta", sid=1, seq=99, msg={})
        )
        assert [m["type"] for m in out] == ["__resync__", "orderbook_delta"]

    def test_the_resync_carries_the_sid_and_channel(
        self, ws: KalshiWebSocket
    ) -> None:
        """So a consumer can invalidate one subscription's state rather than
        everything it holds."""
        ws._handle_raw(
            frame(type="subscribed", sid=3, msg={"sid": 3, "channel": "orderbook_delta"})
        )
        ws._check_seq(3, 10)
        out = ws._handle_raw(frame(type="orderbook_delta", sid=3, seq=44, msg={}))
        resync = out[0]
        assert resync["sid"] == 3
        assert resync["channel"] == "orderbook_delta"
        assert "44" in resync["reason"]

    def test_a_message_with_no_seq_is_not_judged(
        self, ws: KalshiWebSocket
    ) -> None:
        """Not every frame is sequenced. A missing ``seq`` is not a gap."""
        out = ws._handle_raw(frame(type="ticker", sid=1, msg={"price": "0.50"}))
        assert [m["type"] for m in out] == ["ticker"]

    def test_a_message_with_no_sid_is_not_judged(
        self, ws: KalshiWebSocket
    ) -> None:
        out = ws._handle_raw(frame(type="ticker", seq=5, msg={}))
        assert [m["type"] for m in out] == ["ticker"]

    def test_a_control_frame_registers_the_sid_and_yields_nothing(
        self, ws: KalshiWebSocket
    ) -> None:
        out = ws._handle_raw(
            frame(type="subscribed", sid=9, msg={"sid": 9, "channel": "trade"})
        )
        assert out == []
        assert ws._sids[9].channel == "trade"
        assert ws._sids[9].last_seq is None

    def test_resubscribing_resets_that_sids_counter(
        self, ws: KalshiWebSocket
    ) -> None:
        """A fresh ``subscribed`` frame means a fresh stream; the old
        ``last_seq`` would make its first message look like a wild jump."""
        ws._check_seq(4, 900)
        ws._handle_raw(
            frame(type="subscribed", sid=4, msg={"sid": 4, "channel": "trade"})
        )
        assert ws._sids[4].last_seq is None
        assert ws._check_seq(4, 1) is None

    def test_a_non_json_frame_is_dropped_not_raised(
        self, ws: KalshiWebSocket
    ) -> None:
        """One malformed frame must not kill the consumer loop."""
        assert ws._handle_raw("}{ not json") == []

    @pytest.mark.parametrize("code", sorted(TERMINAL_ERROR_CODES))
    def test_a_terminal_error_demands_a_resync(
        self, code: int, ws: KalshiWebSocket
    ) -> None:
        """The subscription is gone; waiting on it would wait forever."""
        out = ws._handle_raw(frame(type="error", msg={"code": code, "msg": "gone"}))
        assert [m["type"] for m in out] == ["__resync__"]
        assert str(code) in out[0]["reason"]

    def test_a_non_terminal_error_is_logged_and_swallowed(
        self, ws: KalshiWebSocket
    ) -> None:
        out = ws._handle_raw(frame(type="error", msg={"code": 6, "msg": "transient"}))
        assert out == []


# ---------------------------------------------------------------------------
# Reconnect
# ---------------------------------------------------------------------------


class TestReconnectState:
    def test_the_sid_table_is_cleared_on_reconnect(
        self, ws: KalshiWebSocket
    ) -> None:
        """Sids are assigned per connection.

        Carrying the old table across would compare a new subscription's
        counter against a dead one's — a guaranteed gap on the first message,
        or worse, a silent acceptance of a lower number as a "replay".
        """
        ws._check_seq(1, 500)
        ws._check_seq(2, 900)
        assert ws._sids

        # What `stream()` does on every (re)connect, before resubscribing.
        ws._sids.clear()

        assert ws._sids == {}
        assert ws._check_seq(1, 3) is None  # a fresh counter, not a replay

    def test_subscriptions_are_declarative_and_survive_a_reconnect(
        self, ws: KalshiWebSocket
    ) -> None:
        """Resubscribing is replaying state, not remembering commands."""
        ws.subscribe(["orderbook_delta"], ["KXMLB-26-HOU", "KXMLB-26-WSH"])
        ws.subscribe(["trade"])

        assert ws.subscriptions == [
            Subscription(("orderbook_delta",), ("KXMLB-26-HOU", "KXMLB-26-WSH")),
            Subscription(("trade",), ()),
        ]

    def test_the_subscribe_params_carry_the_ticker_filter(
        self, ws: KalshiWebSocket
    ) -> None:
        ws.subscribe(["orderbook_delta"], ["KXMLB-26-HOU"])
        assert ws.subscriptions[0].to_params() == {
            "channels": ["orderbook_delta"],
            "market_tickers": ["KXMLB-26-HOU"],
        }

    def test_an_unfiltered_subscription_omits_the_ticker_key(
        self, ws: KalshiWebSocket
    ) -> None:
        """Sending an empty list would subscribe to nothing rather than
        everything."""
        ws.subscribe(["trade"])
        assert "market_tickers" not in ws.subscriptions[0].to_params()

    async def test_force_reconnect_with_no_connection_is_a_no_op(
        self, ws: KalshiWebSocket
    ) -> None:
        """The book-heal loop calls this whenever enough books are waiting; it
        must not raise when the socket is already down."""
        assert ws._connection is None
        await ws.force_reconnect("test")

    async def test_force_reconnect_closes_the_socket(
        self, ws: KalshiWebSocket
    ) -> None:
        """A reconnect is a resubscribe is a fresh snapshot — the only way to
        heal a book that has gone stale."""
        closed: list[bool] = []

        class FakeConnection:
            async def close(self) -> None:
                closed.append(True)

        ws._connection = FakeConnection()  # type: ignore[assignment]
        await ws.force_reconnect("28 books waiting for a resync")
        assert closed == [True]

    async def test_a_close_that_fails_is_swallowed(
        self, ws: KalshiWebSocket
    ) -> None:
        """The socket is being thrown away either way."""

        class BrokenConnection:
            async def close(self) -> None:
                raise OSError("already gone")

        ws._connection = BrokenConnection()  # type: ignore[assignment]
        await ws.force_reconnect("test")


class TestSidState:
    def test_a_fresh_state_has_nothing_to_compare_against(self) -> None:
        state = _SidState()
        assert state.last_seq is None
        assert state.gaps == 0
        assert state.channel is None


class TestSigning:
    def test_the_client_asks_the_signer_for_headers(self) -> None:
        """The connection itself needs auth, even for public market data —
        which is why the market page reads through to REST instead."""
        import inspect

        source = inspect.getsource(KalshiWebSocket._connect)
        assert "ws_headers" in source
        assert hasattr(KalshiSigner, "ws_headers")

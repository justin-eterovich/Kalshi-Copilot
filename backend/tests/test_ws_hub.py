"""Tests for the browser WebSocket relay.

The property that matters: one slow tab must never stall the shared Redis
reader. Tick traffic covers the whole scanner universe, so a client that
cannot keep up gets its messages dropped rather than being allowed to apply
backpressure to everyone else.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.ws import CLIENT_QUEUE_MAX, Client
from app.core.redis import CH_PROPOSALS, CH_TICKS


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def make_client(tickers: set[str] | None = None) -> Client:
    client = Client(FakeSocket())  # type: ignore[arg-type]
    client.tickers = tickers or set()
    return client


class TestFiltering:
    def test_unfiltered_client_wants_every_tick(self) -> None:
        client = make_client()
        assert client.wants(CH_TICKS, {"ticker": "ANY-MARKET"}) is True

    def test_watching_client_only_wants_its_markets(self) -> None:
        client = make_client({"WATCHED"})
        assert client.wants(CH_TICKS, {"ticker": "WATCHED"}) is True
        assert client.wants(CH_TICKS, {"ticker": "SOMETHING-ELSE"}) is False

    def test_proposals_bypass_the_ticker_filter(self) -> None:
        """A proposal must reach the operator regardless of the open page."""
        client = make_client({"WATCHED"})
        assert client.wants(CH_PROPOSALS, {"ticker": "SOMETHING-ELSE"}) is True

    def test_tick_without_ticker_is_filtered_out_when_watching(self) -> None:
        client = make_client({"WATCHED"})
        assert client.wants(CH_TICKS, {}) is False


class TestBackpressure:
    def test_messages_queue_up_normally(self) -> None:
        client = make_client()
        for i in range(10):
            client.offer(f"msg-{i}")
        assert client.queue.qsize() == 10
        assert client.dropped == 0

    def test_slow_client_drops_instead_of_blocking(self) -> None:
        """The hub must never be held hostage by one stalled browser tab."""
        client = make_client()
        for i in range(CLIENT_QUEUE_MAX + 50):
            client.offer(f"msg-{i}")

        assert client.queue.qsize() == CLIENT_QUEUE_MAX
        assert client.dropped == 50

    def test_offer_never_raises_on_overflow(self) -> None:
        client = make_client()
        for i in range(CLIENT_QUEUE_MAX * 2):
            client.offer(f"msg-{i}")  # must not raise
        assert client.dropped > 0

    def test_queue_is_bounded(self) -> None:
        client = make_client()
        assert client.queue.maxsize == CLIENT_QUEUE_MAX


class TestHubWiring:
    def test_hub_starts_with_no_clients(self) -> None:
        from app.api.ws import Hub

        assert Hub().client_count == 0

    def test_channels_include_ticks_and_proposals(self) -> None:
        from app.api.ws import CHANNELS

        assert CH_TICKS in CHANNELS
        assert CH_PROPOSALS in CHANNELS


class TestLiquidityScore:
    """The screener's ranking heuristic."""

    @staticmethod
    def market(**kwargs: Any) -> Any:
        from decimal import Decimal

        class M:
            yes_bid = Decimal("0.40")
            yes_ask = Decimal("0.42")
            volume_24h = Decimal("5000")
            open_interest = Decimal("5000")

        m = M()
        for k, v in kwargs.items():
            setattr(m, k, v)
        return m

    def test_tight_spread_scores_higher_than_wide(self) -> None:
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        tight = _liquidity_score(self.market(yes_ask=Decimal("0.41")))
        wide = _liquidity_score(self.market(yes_ask=Decimal("0.60")))
        assert tight is not None and wide is not None
        assert tight > wide

    def test_more_volume_scores_higher(self) -> None:
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        busy = _liquidity_score(self.market(volume_24h=Decimal("50000")))
        quiet = _liquidity_score(self.market(volume_24h=Decimal("10")))
        assert busy is not None and quiet is not None
        assert busy > quiet

    def test_missing_quotes_score_none(self) -> None:
        from app.api.routes.markets import _liquidity_score

        assert _liquidity_score(self.market(yes_bid=None)) is None

    def test_score_is_bounded_to_100(self) -> None:
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        score = _liquidity_score(
            self.market(
                yes_ask=Decimal("0.4001"),
                volume_24h=Decimal("10000000"),
                open_interest=Decimal("10000000"),
            )
        )
        assert score is not None
        assert 0 <= score <= 100

    def test_crossed_book_does_not_produce_a_negative_score(self) -> None:
        """Regression: a stale quote can leave bid > ask.

        The reciprocal spread term went negative and produced scores like
        -450 and 113, which the UI renders as a percentage-width bar.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        crossed = _liquidity_score(
            self.market(yes_bid=Decimal("0.18"), yes_ask=Decimal("0.07"))
        )
        assert crossed is not None
        assert 0 <= crossed <= 100

    def test_locked_book_is_scored_not_infinite(self) -> None:
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        locked = _liquidity_score(
            self.market(yes_bid=Decimal("0.50"), yes_ask=Decimal("0.50"))
        )
        assert locked is not None
        assert 0 <= locked <= 100

    @pytest.mark.parametrize(
        ("bid", "ask"),
        [
            ("0.01", "0.99"),  # maximally wide
            ("0.50", "0.50"),  # locked
            ("0.60", "0.40"),  # deeply crossed
            ("0.4999", "0.5001"),  # razor thin
        ],
    )
    def test_score_always_in_range(self, bid: str, ask: str) -> None:
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        score = _liquidity_score(
            self.market(yes_bid=Decimal(bid), yes_ask=Decimal(ask))
        )
        assert score is not None
        assert 0 <= score <= 100

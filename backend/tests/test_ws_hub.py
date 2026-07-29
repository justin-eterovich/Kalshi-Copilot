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

    @pytest.mark.parametrize(
        ("bid", "ask"),
        [
            ("0.00", "0.00"),  # nobody quoting either side — 64,211 markets
            ("0.00", "0.98"),  # live: KXGOVAK-26-NDAH, no bid at any price
            ("0.42", "0.00"),  # no ask
            ("1.00", "1.00"),  # settled, not quoted
            ("0.42", "1.00"),  # nobody selling below a dollar
        ],
    )
    def test_market_with_no_live_quote_is_refused(
        self, bid: str, ask: str
    ) -> None:
        """A price of 0 is an absent quote, not a cheap one.

        The guard here was `yes_bid is None or yes_ask is None`, and a market
        nobody is quoting stores 0.000000 rather than NULL — so it inspected the
        wrong thing and passed, handing a full-confidence score to a book with
        no bid and no ask. Same shape as the `Market.result == ''` tri-state
        trap CLAUDE.md documents.

        0 means nobody will buy at any price and 1 means nobody will sell below
        a dollar; neither is a quote you can trade against, so neither has a
        tradeability score.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        assert (
            _liquidity_score(
                self.market(yes_bid=Decimal(bid), yes_ask=Decimal(ask))
            )
            is None
        )

    def test_unquoted_market_never_outranks_a_real_quote(self) -> None:
        """Ordering, not range — a range assertion never caught this either.

        The unquoted book gets maximal volume and open interest and the real
        one almost none, because that is the case the old guard got wrong:
        0.000000/0.000000 read as a zero spread, took the full 0.5 spread
        weight, and volume and open interest carried the rest to 100.0 on a
        market with no book at all.

        `is None or <` so that a later decision to score these rather than
        refuse them stays open, provided they rank below a market someone is
        actually quoting.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        real = _liquidity_score(
            self.market(
                yes_bid=Decimal("0.40"),
                yes_ask=Decimal("0.41"),
                volume_24h=Decimal("100"),
                open_interest=Decimal("100"),
            )
        )
        unquoted = _liquidity_score(
            self.market(
                yes_bid=Decimal("0.00"),
                yes_ask=Decimal("0.00"),
                volume_24h=Decimal("10000000"),
                open_interest=Decimal("10000000"),
            )
        )
        assert real is not None
        assert unquoted is None or unquoted < real

    def test_locked_book_at_a_real_price_is_not_swallowed(self) -> None:
        """The refusal must not overreach onto a book that is merely tight.

        A locked book at 0.36 has two live sides that happen to agree. It is
        the one row on the screener an operator can hit immediately, and it
        differs from the unquoted 0/0 case only in the prices — which is
        exactly why a guard written as "is the spread zero?" would take both.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        assert (
            _liquidity_score(
                self.market(yes_bid=Decimal("0.36"), yes_ask=Decimal("0.36"))
            )
            is not None
        )

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

    @pytest.mark.parametrize(
        ("bid", "ask"),
        [
            ("0.18", "0.07"),
            ("0.60", "0.40"),
            ("0.6200", "0.3400"),  # live: KXHORMUZNORM-26MAR17-B261101
            ("0.5001", "0.4999"),  # crossed by the smallest amount there is
        ],
    )
    def test_crossed_book_is_refused_not_scored(self, bid: str, ask: str) -> None:
        """A stale quote leaves bid > ask, and it gets no score at all.

        Two bugs have lived on this line. First the reciprocal spread term went
        negative and produced scores like -450 and 113, which the UI renders as
        a percentage-width bar. The `max(spread, 0.0)` clamp fixed the range
        and introduced the second: it collapsed a crossed book to a *zero*
        spread, so `KXHORMUZNORM-26MAR17-B261101` — quoted 0.620000/0.340000,
        crossed by 28c — scored **100.0**, the maximum.

        Refusing is the answer because no number is true. See the comment in
        `_liquidity_score`; the ordering test below is what makes this stick.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        assert (
            _liquidity_score(
                self.market(yes_bid=Decimal(bid), yes_ask=Decimal(ask))
            )
            is None
        )

    def test_crossed_book_never_outranks_a_genuinely_tight_one(self) -> None:
        """The property that survives the next clamp-shaped fix.

        Deliberately an *ordering* assertion, not a range one: every version of
        this bug passed a range check. The crossed book here is given maximal
        volume and open interest and the tight book almost none, so any scheme
        that lets the other terms carry a broken quote fails.

        Both wrong answers are caught. The old clamp scores the crossed book
        100 against the tight book's 30. Zeroing the spread term instead —
        "call it maximally wide" — still scores it 50, because volume and open
        interest hold the remaining half of the weight.

        Phrased as `is None or <` so that a future decision to score a crossed
        book rather than refuse it is still allowed, provided it ranks below a
        real quote.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        tight = _liquidity_score(
            self.market(
                yes_bid=Decimal("0.40"),
                yes_ask=Decimal("0.41"),
                volume_24h=Decimal("100"),
                open_interest=Decimal("100"),
            )
        )
        crossed = _liquidity_score(
            self.market(
                yes_bid=Decimal("0.62"),
                yes_ask=Decimal("0.34"),
                volume_24h=Decimal("10000000"),
                open_interest=Decimal("10000000"),
            )
        )
        assert tight is not None
        assert crossed is None or crossed < tight

    def test_locked_book_keeps_the_full_spread_term(self) -> None:
        """bid == ask is tight and executable — a decision, not a side effect.

        It shares a `>=` with the crossed case and nothing else, so it must not
        be swept up by the refusal above: a locked book is the one thing on the
        screener an operator can hit immediately. It therefore has to score
        strictly better than a merely tight one.
        """
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        locked = _liquidity_score(
            self.market(yes_bid=Decimal("0.50"), yes_ask=Decimal("0.50"))
        )
        tight = _liquidity_score(
            self.market(yes_bid=Decimal("0.50"), yes_ask=Decimal("0.51"))
        )
        assert locked is not None and tight is not None
        assert locked > tight

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
            ("0.4999", "0.5001"),  # razor thin
            # The deeply-crossed case that used to live here now belongs to
            # `test_crossed_book_is_refused_not_scored`: it has no score to
            # keep in range, and asserting `is not None` here was what let the
            # 100.0 through.
        ],
    )
    def test_score_of_a_real_quote_is_always_in_range(
        self, bid: str, ask: str
    ) -> None:
        from decimal import Decimal

        from app.api.routes.markets import _liquidity_score

        score = _liquidity_score(
            self.market(yes_bid=Decimal(bid), yes_ask=Decimal(ask))
        )
        assert score is not None
        assert 0 <= score <= 100

"""Local orderbook reconstruction from WebSocket snapshots and deltas.

The protocol: subscribing to ``orderbook_delta`` yields one
``orderbook_snapshot`` establishing state, then a stream of
``orderbook_delta`` messages carrying signed size changes at a price level.
Every message on a subscription carries a monotonically increasing ``seq``.

**The rule that matters:** if a sequence number is skipped, no book on that
subscription is trustworthy. Guessing across a gap produces a book that looks
plausible and is wrong, which is exactly how an arbitrage detector talks you
into a trade that does not exist. So a gap marks books stale, and a stale book
refuses to answer questions until a fresh snapshot arrives.

**That judgement is not made here.** ``seq`` counts the *subscription*, not the
market, so the only place it can be judged is where subscriptions are tracked:
``_check_seq`` in :mod:`app.kalshi.ws`. This class records ``seq``, refuses a
replayed (lower) one, and judges nothing else. What follows is why.

One ``orderbook_delta`` subscription covers every ticker in it
and numbers all of their messages from one counter, so consecutive deltas
for a single market are *not* consecutive in ``seq``. Verified against the
live demo stream with 65 markets subscribed::

    KXNFLGAME-26SEP09NESEA-SEA   seqs = 86, 87, 88, 89, 90, 92, 95, 97
    KXNFLGAME-26AUG13INDNE-NE    seqs = 91, 94, 96, 99, 102, 104, 107, 110

Comparing those per market to ``self.seq + 1`` reports a gap on almost every
message once a second market is active. It did: 21 of 65 books went stale
within sixty seconds and never recovered, while the *per-subscription*
tracker in ``kalshi/ws.py`` — which is the one that is right — logged zero
gaps over the same period. The effect was silent, because a stale book simply
stops being recorded, so the symptom was thin data rather than an error.

There is also no per-market sequence available to switch to: the delta body
carries only ``market_ticker, market_id, price_dollars, delta_fp, side, ts,
ts_ms``. Gap detection therefore belongs entirely to the per-sid tracker in
``ws.py``, which raises a ``__resync__`` that invalidates every book. This
class keeps ``seq`` for observability and does not judge it.

Kalshi quotes both sides as *bids*: ``yes_dollars`` are bids to buy YES and
``no_dollars`` are bids to buy NO. A NO bid at price ``p`` is economically an
offer to sell YES at ``1 - p``, which is how :meth:`OrderBook.yes_ask` is
derived.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.money import parse_count, parse_dollars

__all__ = ["OrderBook", "BookStaleError", "Level"]


class BookStaleError(RuntimeError):
    """The local book has a sequence gap and must not be used."""


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """Mutable local view of one market's aggregated price levels."""

    ticker: str
    #: price -> size, both sides quoted as bids.
    yes: dict[Decimal, Decimal] = field(default_factory=dict)
    no: dict[Decimal, Decimal] = field(default_factory=dict)

    seq: int | None = None
    stale: bool = True
    #: Why the book went stale, surfaced in logs and the UI.
    stale_reason: str | None = "awaiting snapshot"
    gap_count: int = 0

    # -- state transitions ----------------------------------------------

    def apply_snapshot(self, msg: dict[str, Any], seq: int | None) -> None:
        """Replace all state from an ``orderbook_snapshot`` message."""
        self.yes = self._parse_levels(msg.get("yes_dollars_fp") or msg.get("yes") or [])
        self.no = self._parse_levels(msg.get("no_dollars_fp") or msg.get("no") or [])
        self.seq = seq
        self.stale = False
        self.stale_reason = None

    def apply_delta(self, msg: dict[str, Any], seq: int | None) -> bool:
        """Apply one ``orderbook_delta``.

        Returns True if applied, False if the book is not in a state to take
        it. A False return means the caller needs a fresh snapshot.

        ``seq`` is recorded but **not** checked against the previous one: it
        counts the subscription, not this market, so consecutive deltas for
        one ticker are not consecutive in ``seq``. See the module docstring —
        checking it here is what left most of the watchlist permanently
        stale. Gap detection lives in :mod:`app.kalshi.ws`, where the counter
        it compares is the one the number actually belongs to.
        """
        if self.stale:
            return False

        # Out-of-order delivery would be a real problem, but this is a single
        # ordered websocket: a lower seq than the last one means a replay, not
        # a reordering, and applying it twice would double-count the delta.
        if seq is not None and self.seq is not None and seq <= self.seq:
            return False

        price = parse_dollars(msg["price_dollars"], "price_dollars")
        delta = parse_count(msg["delta_fp"], "delta_fp")
        side = str(msg.get("side", "")).lower()
        if side not in ("yes", "no"):
            # An `else: self.no` fallthrough applied the delta to the NO book,
            # advanced the sequence, and left `stale` False — so the book went
            # on reporting itself trustworthy while carrying levels that were
            # never quoted. The gap machinery cannot catch that, because no
            # sequence was skipped. Kalshi's asyncapi constrains this field to
            # yes/no today; this refuses if that ever stops being true.
            self.mark_stale(f"unknown book side {side!r}")
            return False

        book = self.yes if side == "yes" else self.no
        new_size = book.get(price, Decimal(0)) + delta

        if new_size <= 0:
            book.pop(price, None)
        else:
            book[price] = new_size

        if seq is not None:
            self.seq = seq
        return True

    def mark_stale(self, reason: str) -> None:
        """Invalidate the book. It answers nothing until resnapshotted."""
        if not self.stale:
            self.gap_count += 1
        self.stale = True
        self.stale_reason = reason

    @staticmethod
    def _parse_levels(raw: list[Any]) -> dict[Decimal, Decimal]:
        levels: dict[Decimal, Decimal] = {}
        for entry in raw:
            if not entry or len(entry) < 2:
                continue
            price = parse_dollars(entry[0], "level price")
            size = parse_count(entry[1], "level size")
            if size > 0:
                levels[price] = size
        return levels

    # -- reads (all refuse to answer while stale) -------------------------

    def _check(self) -> None:
        if self.stale:
            raise BookStaleError(
                f"{self.ticker}: orderbook is stale ({self.stale_reason}); "
                f"awaiting a fresh snapshot"
            )

    def yes_bids(self) -> list[Level]:
        """Bids to buy YES, best (highest) first."""
        self._check()
        return [
            Level(p, s) for p, s in sorted(self.yes.items(), key=lambda kv: -kv[0])
        ]

    def no_bids(self) -> list[Level]:
        """Bids to buy NO, best (highest) first."""
        self._check()
        return [Level(p, s) for p, s in sorted(self.no.items(), key=lambda kv: -kv[0])]

    def yes_asks(self) -> list[Level]:
        """Offers to sell YES, best (lowest) first.

        Derived from the NO bids: a NO bid at ``p`` is an offer to sell YES at
        ``1 - p``.
        """
        self._check()
        return [
            Level(Decimal(1) - p, s)
            for p, s in sorted(self.no.items(), key=lambda kv: -kv[0])
        ]

    def best_yes_bid(self) -> Level | None:
        bids = self.yes_bids()
        return bids[0] if bids else None

    def best_yes_ask(self) -> Level | None:
        asks = self.yes_asks()
        return asks[0] if asks else None

    def spread(self) -> Decimal | None:
        """YES ask minus YES bid, in dollars."""
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return ask.price - bid.price

    def mid(self) -> Decimal | None:
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / Decimal(2)

    def executable_cost(
        self, side: str, contracts: Decimal
    ) -> tuple[Decimal, Decimal] | None:
        """Walk the book for ``contracts`` and return (avg_price, filled).

        This is how slippage enters every edge calculation: detectors price
        against what they could actually execute, not against the top of book.
        Returns ``None`` if the book cannot fill any size at all.
        """
        self._check()
        levels = self.yes_asks() if side.lower() == "yes" else self.no_bids()
        if not levels:
            return None

        remaining = contracts
        cost = Decimal(0)
        filled = Decimal(0)

        for level in levels:
            if remaining <= 0:
                break
            take = min(level.size, remaining)
            cost += take * level.price
            filled += take
            remaining -= take

        if filled <= 0:
            return None
        return (cost / filled, filled)

    def top_n(self, n: int = 10) -> dict[str, list[list[str]]]:
        """Serialisable top-N snapshot for persistence and the UI.

        Values stay as strings so JSON round-trips lose no precision.
        """
        yes = sorted(self.yes.items(), key=lambda kv: -kv[0])[:n]
        no = sorted(self.no.items(), key=lambda kv: -kv[0])[:n]
        return {
            "yes": [[str(p), str(s)] for p, s in yes],
            "no": [[str(p), str(s)] for p, s in no],
        }

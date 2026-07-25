"""Local orderbook reconstruction from WebSocket snapshots and deltas.

The protocol: subscribing to ``orderbook_delta`` yields one
``orderbook_snapshot`` establishing state, then a stream of
``orderbook_delta`` messages carrying signed size changes at a price level.
Every message on a subscription carries a monotonically increasing ``seq``.

**The rule that matters:** if a sequence number is skipped, the local book is
no longer trustworthy. Guessing across a gap produces a book that looks
plausible and is wrong, which is exactly how an arbitrage detector talks you
into a trade that does not exist. On any gap the book is marked stale, and it
refuses to answer questions until a fresh snapshot arrives.

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

        Returns True if applied, False if a gap was detected. A False return
        means the caller must resubscribe or request a fresh snapshot.
        """
        if self.stale:
            return False

        if seq is not None and self.seq is not None:
            expected = self.seq + 1
            if seq != expected:
                self.mark_stale(f"sequence gap: expected {expected}, got {seq}")
                return False

        price = parse_dollars(msg["price_dollars"], "price_dollars")
        delta = parse_count(msg["delta_fp"], "delta_fp")
        side = str(msg.get("side", "")).lower()

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

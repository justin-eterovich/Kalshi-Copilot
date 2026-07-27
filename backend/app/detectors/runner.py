"""The live detectors: set arbitrage, stale quote, resolution sniper.

Each wraps pure math with what it needs from the world, and each is
written to refuse rather than guess when the world does not supply it.

Set arbitrage: scanning live events for costed set trades.

Wraps the pure math in :mod:`app.detectors.set_arbitrage` with everything it
needs from the world — which events are exclusive, what their books look like
right now, and which of the two directions is actually safe to signal.

**The exhaustiveness rule is the important part.** Kalshi's
``mutually_exclusive`` means *at most one* leg resolves YES. That makes the
sell side riskless on its own: collect more than $1 for a set that pays out at
most $1. It does **not** make the buy side riskless — that needs *at least
one* leg to win, and the API never says so. Seven pope candidates whose asks
sum to $0.84 look like a 16c arb and are not one.

So the buy side only runs for series the operator has explicitly declared
exhaustive in ``config.yaml``. The list ships empty. Inferring exhaustiveness
from the flag would be exactly the kind of plausible-and-wrong reasoning this
system is built to refuse.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.fees import load_fee_schedule, series_of
from app.core.logging import get_logger
from app.db.models import Event, Market, Side
from app.detectors.base import Finding
from app.detectors.set_arbitrage import LegBook, max_executable_sets, price_set
from app.kalshi.rest import KalshiApiError, KalshiRestClient

log = get_logger(__name__)

__all__ = [
    "SetArbitrageDetector",
    "StaleQuoteDetector",
    "ResolutionSniperDetector",
]

#: Set sizes to price, largest first. A bigger set walks deeper into the book
#: and earns less per contract, so the best size is rarely the biggest.
CANDIDATE_SIZES = (Decimal(200), Decimal(100), Decimal(50), Decimal(20), Decimal(5))


class SetArbitrageDetector:
    """Prices every watched mutually-exclusive event, both directions."""

    name = "set_arbitrage"

    def __init__(self, rest: KalshiRestClient | None) -> None:
        self._rest = rest

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.set_arbitrage, "enabled", False))

    # -- configuration ---------------------------------------------------

    @staticmethod
    def _min_edge(config: Config) -> Decimal:
        raw = getattr(config.detectors.set_arbitrage, "min_net_edge_cents", 1.0)
        return Decimal(str(raw))

    @staticmethod
    def _exhaustive_series(config: Config) -> set[str]:
        """Series the operator has declared collectively exhaustive.

        Empty by default, and it must be: nothing in the API establishes it,
        and getting it wrong turns "buy the set" from an arb into an
        uncovered short of every outcome that was not listed.
        """
        raw = getattr(config.detectors.set_arbitrage, "exhaustive_series", []) or []
        return {str(s).strip().upper() for s in raw}

    # -- scanning --------------------------------------------------------

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        watchlist = set(config.ingest.watchlist)
        if not watchlist:
            return []

        events = await self._watched_exclusive_events(session, watchlist)
        if not events:
            log.debug("set_arb: no watched mutually-exclusive events")
            return []

        schedule = load_fee_schedule()
        slippage = Decimal(str(config.costs.slippage_buffer_cents))
        min_edge = self._min_edge(config)
        exhaustive = self._exhaustive_series(config)

        findings: list[Finding] = []
        for event_ticker, tickers in events.items():
            books = await self._books(tickers)
            if books is None or len(books) != len(tickers):
                # A set priced from a partial book set is not a set.
                continue

            directions: list[str] = ["sell"]
            if series_of(event_ticker) in exhaustive:
                directions.append("buy")

            for direction in directions:
                finding = self._best_for(
                    event_ticker=event_ticker,
                    books=books,
                    direction=direction,  # type: ignore[arg-type]
                    schedule=schedule,
                    slippage=slippage,
                    min_edge=min_edge,
                    declared_exhaustive=series_of(event_ticker) in exhaustive,
                )
                if finding is not None:
                    findings.append(finding)

        return findings

    def _best_for(
        self,
        *,
        event_ticker: str,
        books: list[LegBook],
        direction: Any,
        schedule: Any,
        slippage: Decimal,
        min_edge: Decimal,
        declared_exhaustive: bool,
    ) -> Finding | None:
        """Price several set sizes and keep the best total edge, if any."""
        cap = max_executable_sets(books, direction)
        if cap <= 0:
            return None

        best = None
        for size in CANDIDATE_SIZES:
            if size > cap:
                continue
            opp = price_set(
                event_ticker=event_ticker,
                books=books,
                contracts=size,
                direction=direction,
                schedule=schedule,
                slippage_cents=slippage,
            )
            if opp is None:
                continue
            if best is None or opp.net_edge_cents > best.net_edge_cents:
                best = opp

        if best is None:
            return None

        per_set = best.net_edge_cents / best.contracts
        if per_set < min_edge:
            return None

        verb = "sell" if direction == "sell" else "buy"
        rationale = (
            f"{len(best.legs)} legs of {event_ticker} {verb} for "
            f"${best.gross_sum} per set; at most one resolves YES. "
            f"Net {best.net_edge_cents:.2f}c over {best.contracts} sets "
            f"after {best.total_fee_cents:.2f}c of fees."
        )
        if direction == "buy":
            rationale += (
                " Buy side signalled because this series is declared "
                "exhaustive in config — it is only an arb if some leg must win."
            )

        return Finding(
            detector=self.name,
            # A set spans many markets; the event is the thing being traded.
            # The first leg is recorded as the ticker so the row is joinable.
            ticker=best.legs[0].ticker,
            side=Side.YES,
            fair_price=Decimal(1) / Decimal(len(best.legs)),
            net_edge_cents=per_set,
            confidence=0.9 if direction == "sell" else 0.6,
            size_hint=best.contracts,
            rationale=rationale,
            evidence={
                **best.as_dict(),
                "declared_exhaustive": declared_exhaustive,
                "safe_without_exhaustiveness": direction == "sell",
            },
        )

    # -- data ------------------------------------------------------------

    async def _watched_exclusive_events(
        self, session: AsyncSession, watchlist: set[str]
    ) -> dict[str, list[str]]:
        """Watched events that are exclusive, with **every** active leg.

        Every leg matters: an event whose watchlist coverage is partial cannot
        be arbitraged from the legs we happen to see.
        """
        rows = (
            await session.execute(
                select(Market.event_ticker, Market.ticker)
                .join(Event, Event.ticker == Market.event_ticker)
                .where(
                    Event.mutually_exclusive.is_(True),
                    Market.status == "active",
                )
            )
        ).all()

        by_event: dict[str, list[str]] = {}
        for event_ticker, ticker in rows:
            by_event.setdefault(event_ticker, []).append(ticker)

        return {
            event: sorted(tickers)
            for event, tickers in by_event.items()
            if len(tickers) > 1 and all(t in watchlist for t in tickers)
        }

    async def _books(self, tickers: list[str]) -> list[LegBook] | None:
        if self._rest is None:
            return None
        books: list[LegBook] = []
        for ticker in tickers:
            try:
                raw = await self._rest.get_orderbook(ticker)
            except KalshiApiError as exc:
                log.debug("set_arb: no book for %s (%s)", ticker, exc)
                return None
            books.append(
                LegBook.from_payload(
                    ticker,
                    {
                        "yes": raw.get("yes_dollars") or raw.get("yes") or [],
                        "no": raw.get("no_dollars") or raw.get("no") or [],
                    },
                )
            )
        return books


class StaleQuoteDetector:
    """Crypto strikes the spot price has decisively cleared.

    Needs a *fresh* independent spot quote. Without one it emits nothing —
    comparing a live market to a stale reference invents an edge in whichever
    direction the market already moved.
    """

    name = "stale_quote"

    def __init__(self, rest: KalshiRestClient | None) -> None:
        self._rest = rest

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.stale_quote, "enabled", False))

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import desc

        from app.core.fees import net_edge_cents
        from app.db.models import ExternalPrice
        from app.detectors.stale_quote import (
            decisive_fair_price,
            reference_is_fresh,
            resolve_strike,
        )

        cfg = config.detectors.stale_quote
        max_age = float(getattr(cfg, "reference_max_age_sec", 5))
        min_margin = Decimal(str(getattr(cfg, "decisive_margin_pct", 1.0)))
        max_minutes = float(getattr(cfg, "max_minutes_to_close", 30))
        min_edge = Decimal(str(getattr(cfg, "min_net_edge_cents", 2.0)))

        latest = (
            await session.execute(
                select(ExternalPrice)
                .where(ExternalPrice.symbol == "BTC-USD")
                .order_by(desc(ExternalPrice.ts))
                .limit(1)
            )
        ).scalars().first()

        if latest is None or not reference_is_fresh(latest.ts, max_age_sec=max_age):
            log.debug("stale_quote: no fresh BTC reference; emitting nothing")
            return []

        spot = latest.price
        horizon = datetime.now(UTC) + timedelta(minutes=max_minutes)
        markets = (
            await session.execute(
                select(Market).where(
                    Market.status == "active",
                    Market.category == "Crypto",
                    Market.close_time.isnot(None),
                    Market.close_time <= horizon,
                    Market.close_time > datetime.now(UTC),
                    Market.yes_ask.isnot(None),
                )
            )
        ).scalars().all()

        schedule = load_fee_schedule()
        slippage = Decimal(str(config.costs.slippage_buffer_cents))
        findings: list[Finding] = []

        for market in markets:
            verdict = resolve_strike(
                strike_type=market.strike_type,
                floor_strike=market.floor_strike,
                cap_strike=market.cap_strike,
                spot=spot,
            )
            if verdict is None:
                continue
            fair = decisive_fair_price(verdict, min_margin_pct=min_margin)
            if fair is None:
                continue

            # Buy the side the spot says is right, at what it actually costs.
            if verdict.yes:
                side, price = Side.YES, market.yes_ask
            else:
                side, price = Side.NO, (
                    None if market.yes_bid is None else Decimal(1) - market.yes_bid
                )
                fair = Decimal(1) - fair
            if price is None or not (Decimal(0) < price < Decimal(1)):
                continue

            mins_left = (
                market.close_time - datetime.now(UTC)
            ).total_seconds() / 60
            size = Decimal(str(getattr(cfg, "size_hint", 10)))
            edge = net_edge_cents(
                fair, price, size, series_of(market.ticker),
                slippage_cents=slippage, schedule=schedule,
            )
            if edge < min_edge:
                continue

            findings.append(
                Finding(
                    detector=self.name,
                    ticker=market.ticker,
                    side=side,
                    fair_price=fair,
                    net_edge_cents=edge,
                    # Bounded well below certainty: this is a fixed-margin
                    # heuristic, not a probability from a vol model. That
                    # arrives in M6.
                    confidence=min(0.75, 0.4 + float(verdict.margin_pct) / 20.0),
                    size_hint=size,
                    rationale=(
                        f"spot {spot} is {verdict.margin_pct:.2f}% past the "
                        f"{market.strike_type} strike with "
                        f"{mins_left:.0f}m left; {side.value} quoted at {price}"
                    ),
                    evidence={
                        "spot": str(spot),
                        "spot_source": latest.source,
                        "spot_ts": latest.ts.isoformat() if latest.ts else None,
                        "strike_type": market.strike_type,
                        "floor_strike": str(market.floor_strike),
                        "cap_strike": str(market.cap_strike),
                        "margin_pct": str(verdict.margin_pct),
                        "fair_price": str(fair),
                        "price": str(price),
                        "heuristic": "fixed margin, not a volatility model",
                    },
                )
            )

        return findings


class ResolutionSniperDetector:
    """Markets past close and still trading at an extreme.

    Emits research signals only. Turning one into a proposal needs a
    settlement source confirming the outcome, and nothing wires one in yet —
    a price of 98c is the crowd's opinion, and buying it because it is high
    is a 49:1 bet rather than an arbitrage.
    """

    name = "resolution_sniper"

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.resolution_sniper, "enabled", False))

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        from datetime import UTC, datetime

        from app.detectors.resolution_sniper import assess

        cfg = config.detectors.resolution_sniper
        yes_threshold = Decimal(str(getattr(cfg, "yes_threshold_cents", 97)))
        no_threshold = Decimal(str(getattr(cfg, "no_threshold_cents", 3)))

        markets = (
            await session.execute(
                select(Market).where(
                    Market.status == "active",
                    Market.close_time.isnot(None),
                    Market.close_time < datetime.now(UTC),
                )
            )
        ).scalars().all()

        findings: list[Finding] = []
        for market in markets:
            candidate = assess(
                ticker=market.ticker,
                yes_bid=market.yes_bid,
                yes_ask=market.yes_ask,
                close_time=market.close_time,
                yes_threshold_cents=yes_threshold,
                no_threshold_cents=no_threshold,
            )
            if candidate is None:
                continue

            findings.append(
                Finding(
                    detector=self.name,
                    ticker=candidate.ticker,
                    side=Side.YES if candidate.side == "yes" else Side.NO,
                    fair_price=candidate.price,
                    # No settlement source, so no edge is claimed. Reporting a
                    # number here would be inventing one from the price.
                    net_edge_cents=Decimal(0),
                    confidence=candidate.confidence,
                    rationale=(
                        f"past close by {candidate.minutes_past_close:.0f}m and "
                        f"still quoted at {candidate.price} on the "
                        f"{candidate.side} side. RESEARCH ONLY — no settlement "
                        f"source confirms the outcome, and price is not proof."
                    ),
                    evidence={
                        "minutes_past_close": candidate.minutes_past_close,
                        "side": candidate.side,
                        "price": str(candidate.price),
                        "source_confirmed": candidate.source_confirmed,
                        "actionable": candidate.actionable,
                    },
                )
            )

        return findings

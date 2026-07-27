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
from app.settings import get_settings
from app.trading import risk
from app.trading.sizing import recommend_size

log = get_logger(__name__)

__all__ = [
    "SetArbitrageDetector",
    "StaleQuoteDetector",
    "ResolutionSniperDetector",
    "UndervaluedScreenerDetector",
    "WhaleFlowDetector",
    "LongshotCalibrationDetector",
    "LeaderboardWatcherDetector",
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

        from app.btc.history import horizon_sigma
        from app.btc.vol import fair_price as vol_fair_price
        from app.core.fees import net_edge_cents
        from app.db.models import ExternalPrice
        from app.detectors.stale_quote import (
            REFERENCE_PREFIXES,
            decisive_fair_price,
            reference_is_fresh,
            reference_symbol_for,
            resolve_strike,
        )

        cfg = config.detectors.stale_quote
        max_age = float(getattr(cfg, "reference_max_age_sec", 5))
        min_margin = Decimal(str(getattr(cfg, "decisive_margin_pct", 1.0)))
        max_minutes = float(getattr(cfg, "max_minutes_to_close", 30))
        min_edge = Decimal(str(getattr(cfg, "min_net_edge_cents", 2.0)))
        # M6: price from an actual volatility estimate rather than the
        # fixed-margin placeholder M4 shipped. The placeholder survives only
        # as a *veto* below — see the comment at the pricing site.
        use_vol = bool(config.bitcoin.enabled)

        # One fresh reference per underlying. There is deliberately no global
        # "spot" any more: the previous version selected every market in the
        # Crypto *category* and priced all of them against BTC, which on a
        # live run turned an ETH contract quoted at 26c into a 71-cent edge
        # because $1,969 is a long way below $65,154. Category is not an
        # underlying; only the series says which asset a market tracks.
        references: dict[str, Decimal] = {}
        for symbol in set(REFERENCE_PREFIXES.values()):
            row = (
                await session.execute(
                    select(ExternalPrice)
                    .where(ExternalPrice.symbol == symbol)
                    .order_by(desc(ExternalPrice.ts))
                    .limit(1)
                )
            ).scalars().first()
            if row is not None and reference_is_fresh(row.ts, max_age_sec=max_age):
                references[symbol] = row.price

        if not references:
            log.debug("stale_quote: no fresh reference price; emitting nothing")
            return []

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

        # One snapshot for the whole scan. Sizing reads the portfolio's
        # remaining headroom so an already-committed book proposes smaller
        # rather than proposing something the executor will refuse — the
        # limit is enforced there regardless, this just stops the queue
        # filling with trades that cannot be approved.
        state = await risk.halt_state(session, config, get_settings())
        headroom = state.headroom_cents if state else None

        for market in markets:
            # Refuse anything whose underlying we cannot name, and anything
            # whose feed is missing or stale, rather than reaching for
            # whatever price happens to be nearest.
            symbol = reference_symbol_for(market.ticker)
            spot = references.get(symbol) if symbol else None
            if spot is None:
                continue

            verdict = resolve_strike(
                strike_type=market.strike_type,
                floor_strike=market.floor_strike,
                cap_strike=market.cap_strike,
                spot=spot,
            )
            if verdict is None:
                continue

            mins_left = (
                market.close_time - datetime.now(UTC)
            ).total_seconds() / 60

            # The margin check stays, but its job has changed. It is no longer
            # the model — it is a veto in front of one. A strike that spot has
            # barely cleared is exactly where a lognormal is least trustworthy
            # (it prices the last basis point of edge with total confidence
            # and no memory of the last jump), so the crude test still gets to
            # say "not this one" before the precise one gets to say a number.
            if decisive_fair_price(verdict, min_margin_pct=min_margin) is None:
                continue

            fair = None
            sigma_note: dict[str, Any] = {}
            if use_vol:
                estimate = await horizon_sigma(
                    session, config, minutes_to_close=mins_left, symbol=symbol
                )
                if estimate is not None:
                    fair = vol_fair_price(
                        strike_type=market.strike_type,
                        floor_strike=market.floor_strike,
                        cap_strike=market.cap_strike,
                        spot=spot,
                        sigma=estimate.sigma,
                    )
                    sigma_note = {
                        "sigma_horizon": f"{estimate.sigma:.6f}",
                        "sigma_per_minute": f"{estimate.per_minute_sigma:.8f}",
                        "vol_samples": estimate.samples,
                        "vol_as_of": estimate.as_of.isoformat(),
                    }

            if fair is None:
                # No usable volatility estimate, so no price. Falling back to
                # the fixed-margin heuristic here would be the worst of both:
                # the operator would see a detector that says "vol model" and
                # a number that came from a constant, with nothing on the card
                # distinguishing the two. Refusing is the honest answer, and
                # `bitcoin.enabled: false` is what silences this entirely.
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

            # Two passes, because fees round per fill and so the edge depends
            # slightly on the size. Probe at a nominal size to learn the
            # all-in cost, size against that, then re-price at the size we
            # actually mean to trade — the number shown must be the number
            # for this ticket, not for a hypothetical one.
            probe = Decimal(str(getattr(cfg, "size_hint", 10)))
            probe_edge = net_edge_cents(
                fair, price, probe, series_of(market.ticker),
                slippage_cents=slippage, schedule=schedule,
            )
            if probe_edge < min_edge:
                continue

            # cost = fair - edge, by construction: the net edge is exactly
            # fair minus everything the contract costs to acquire. Deriving it
            # this way keeps every fee in app.core.fees, where the rule says
            # it belongs, instead of recomputing one here.
            cost = fair - probe_edge / Decimal(100)
            recommendation = recommend_size(
                fair_price=fair,
                cost_per_contract=cost,
                config=config,
                available_contracts=market.yes_ask_size if verdict.yes else None,
                exposure_headroom_cents=headroom,
            )
            if not recommendation.is_tradeable:
                continue

            size = recommendation.contracts
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
                    # Still bounded well below certainty, for a different
                    # reason than in M4. The price now comes from a real
                    # estimator, but a driftless lognormal fitted to
                    # square-root-scaled minute returns is a model of a market
                    # that jumps and clusters — right on average and capable
                    # of being badly wrong exactly when it matters.
                    confidence=min(0.75, 0.4 + float(verdict.margin_pct) / 20.0),
                    size_hint=size,
                    rationale=(
                        f"spot {spot} is {verdict.margin_pct:.2f}% past the "
                        f"{market.strike_type} strike with "
                        f"{mins_left:.0f}m left; vol model fair {fair} vs "
                        f"{side.value} quoted at {price}. "
                        f"{size} contracts at "
                        f"{recommendation.scaled_fraction:.1%} of bankroll "
                        f"(capped by {recommendation.binding_constraint})"
                    ),
                    evidence={
                        "spot": str(spot),
                        "reference_symbol": symbol,
                        "strike_type": market.strike_type,
                        "floor_strike": str(market.floor_strike),
                        "cap_strike": str(market.cap_strike),
                        "margin_pct": str(verdict.margin_pct),
                        "fair_price": str(fair),
                        "price": str(price),
                        "all_in_cost": str(cost),
                        "full_kelly": str(recommendation.kelly_fraction),
                        "staked_fraction": str(recommendation.scaled_fraction),
                        "size_capped_by": recommendation.binding_constraint,
                        "model": "driftless lognormal, EWMA minute vol",
                        **sigma_note,
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


class UndervaluedScreenerDetector:
    """Thin, wide, closing-soon markets — a reading list, not a signal.

    Emits ``net_edge_cents=0`` always, and that is structural rather than a
    placeholder: an illiquid wide-spread market is not mispriced, it is
    *untraded*, and the spread that makes it interesting to look at is the
    same spread you would have to cross to act. Any edge reported here would
    be manufactured from the screener's own selection criterion.
    """

    name = "undervalued_screener"

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.undervalued_screener, "enabled", False))

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        from datetime import UTC, datetime

        from app.detectors.undervalued_screener import MarketSnapshot, screen

        cfg = config.detectors.undervalued_screener
        max_pct = float(getattr(cfg, "max_volume_percentile", 0.25))
        min_spread = Decimal(str(getattr(cfg, "min_spread_cents", 2)))
        max_hours = float(getattr(cfg, "max_hours_to_close", 168))

        now = datetime.now(UTC)
        rows = (
            await session.execute(
                select(Market).where(
                    Market.status == "active",
                    Market.close_time.isnot(None),
                    Market.yes_bid.isnot(None),
                    Market.yes_ask.isnot(None),
                )
            )
        ).scalars().all()

        snapshots: list[MarketSnapshot] = []
        for market in rows:
            close = market.close_time
            if close is None:
                continue
            if close.tzinfo is None:
                close = close.replace(tzinfo=UTC)
            snapshots.append(
                MarketSnapshot(
                    ticker=market.ticker,
                    volume_24h=market.volume_24h or Decimal(0),
                    yes_bid=market.yes_bid,
                    yes_ask=market.yes_ask,
                    hours_to_close=(close - now).total_seconds() / 3600.0,
                    open_interest=market.open_interest,
                )
            )

        results = screen(
            snapshots,
            max_volume_percentile=max_pct,
            min_spread_cents=min_spread,
            max_hours_to_close=max_hours,
        )

        # The screener is a ranking, so the tail of it is noise by
        # construction. Emitting all of it would bury every other detector's
        # signals under hundreds of "this market is quiet" notes.
        top = results[: int(getattr(cfg, "max_results", 20))]

        return [
            Finding(
                detector=self.name,
                ticker=r.ticker,
                # Nothing here has a view on direction; YES is the neutral
                # label the Signal schema requires, not a recommendation.
                side=Side.YES,
                fair_price=Decimal("0.5"),
                net_edge_cents=Decimal(0),
                confidence=0.1,
                rationale=(
                    f"RESEARCH ONLY — quiet and wide, not known to be "
                    f"mispriced. {r.reason} Score {r.score:.1f} is an ordering "
                    f"for attention, not cents and not a probability."
                ),
                evidence={
                    "volume_percentile": r.volume_percentile,
                    "spread_cents": str(r.spread_cents),
                    "hours_to_close": r.hours_to_close,
                    "score": r.score,
                    "note": "no edge is claimed; the spread is the cost of acting",
                },
            )
            for r in top
        ]


class WhaleFlowDetector:
    """Unusually large prints and sweeps on the public tape.

    Flow is an **input to judgement, never an instruction**. A large trade is
    not information about value — it is information that somebody with a
    different opinion, or a different need, transacted, and the counterparty
    may be the informed one. Confidence is hard-capped by ``base_confidence``
    and no amount of size can raise it.
    """

    name = "whale_flow"

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.whale_flow, "enabled", False))

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        from datetime import UTC, datetime, timedelta

        from app.db.models import Tape
        from app.detectors.whale_flow import Trade, analyse

        cfg = config.detectors.whale_flow
        threshold = float(getattr(cfg, "size_zscore_threshold", 3.0))
        window = float(getattr(cfg, "sweep_window_sec", 2.0))
        min_levels = int(getattr(cfg, "min_levels_cleared", 2))
        base_conf = float(getattr(cfg, "base_confidence", 0.35))
        lookback = int(getattr(cfg, "lookback_minutes", 30))

        since = datetime.now(UTC) - timedelta(minutes=lookback)
        rows = (
            await session.execute(
                select(Tape).where(Tape.ts >= since).order_by(Tape.ts)
            )
        ).scalars().all()

        by_ticker: dict[str, list[Trade]] = {}
        for row in rows:
            by_ticker.setdefault(row.ticker, []).append(
                Trade(
                    ts=row.ts,
                    yes_price=row.yes_price,
                    count=row.count,
                    taker_side=row.taker_side,
                )
            )

        findings: list[Finding] = []
        for ticker, trades in by_ticker.items():
            event = analyse(
                trades,
                zscore_threshold=threshold,
                window_sec=window,
                min_levels=min_levels,
                base_confidence=base_conf,
            )
            if event is None:
                continue

            findings.append(
                Finding(
                    detector=self.name,
                    ticker=ticker,
                    # The side flow *went*, which is an observation about who
                    # crossed the spread — not a recommendation to follow it.
                    side=Side.YES if event.direction == "buy" else Side.NO,
                    fair_price=trades[-1].yes_price,
                    # Flow says nothing about value, so there is no edge to
                    # report. A number here would be pure momentum dressed up.
                    net_edge_cents=Decimal(0),
                    confidence=event.confidence,
                    rationale=f"RESEARCH ONLY — {event.rationale}",
                    evidence={
                        "direction": event.direction,
                        "contracts": str(event.contracts),
                        "zscore": event.zscore,
                        "swept_levels": event.sweep.levels if event.sweep else None,
                        "note": (
                            "the counterparty to a large print may be the "
                            "informed side; this is momentum, not edge"
                        ),
                    },
                )
            )

        return findings


class LongshotCalibrationDetector:
    """Whether this market set actually shows longshot bias.

    Reads the observations the worker collects continuously (one per market,
    ever) and refuses to say anything until a bucket clears
    ``min_samples_before_signalling``. Two observations in a bucket produce a
    beautiful-looking rate and mean nothing.
    """

    name = "longshot_calibration"

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.longshot_calibration, "enabled", False))

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        from app.db.models import CalibrationLog
        from app.detectors.longshot_calibration import (
            Observation,
            calibrate,
            significant,
        )

        cfg = config.detectors.longshot_calibration
        min_samples = int(getattr(cfg, "min_samples_before_signalling", 500))

        rows = (
            await session.execute(
                select(
                    CalibrationLog.price_bucket_cents, CalibrationLog.settled_yes
                ).where(CalibrationLog.settled_yes.isnot(None))
            )
        ).all()

        stats = calibrate(
            [
                Observation(price_bucket_cents=int(b), settled_yes=bool(y))
                for b, y in rows
            ]
        )
        hits = significant(stats, min_samples=min_samples)
        if not hits:
            log.debug(
                "longshot_calibration: %d settled observation(s), none past the "
                "%d-sample floor",
                len(rows),
                min_samples,
            )
            return []

        return [
            Finding(
                detector=self.name,
                # A bucket is a claim about a price band, not about a market.
                # There is no ticker to attach it to, so the band names itself.
                ticker=f"BUCKET-{stat.bucket_cents}C",
                side=Side.YES,
                fair_price=Decimal(stat.observed_rate).quantize(Decimal("0.0001")),
                # Calibration is not profitability: a 5c contract must win
                # more than 5% of the time to cover the fee, and nothing here
                # accounts for that. Reporting an edge would skip that step.
                net_edge_cents=Decimal(0),
                confidence=0.3,
                rationale=(
                    f"RESEARCH ONLY — {stat.bucket_cents}c contracts settled YES "
                    f"{stat.observed_rate:.1%} of the time over {stat.samples} "
                    f"markets (95% CI {stat.ci_low:.1%}-{stat.ci_high:.1%}); the "
                    f"price implies {stat.implied_rate:.1%}. Calibration, not "
                    f"profitability — fees are not in this number."
                ),
                evidence={
                    "bucket_cents": stat.bucket_cents,
                    "samples": stat.samples,
                    "yes_count": stat.yes_count,
                    "observed_rate": stat.observed_rate,
                    "implied_rate": stat.implied_rate,
                    "ci_low": stat.ci_low,
                    "ci_high": stat.ci_high,
                    "note": (
                        "observations come from watched markets, so this "
                        "measures the watchlist, not Kalshi"
                    ),
                },
            )
            for stat in hits
        ]


class LeaderboardWatcherDetector:
    """Not implemented, and not buildable from anything official.

    Kalshi's API exposes no leaderboard, no public trader ranking, and no
    public profile surface; the only endpoints naming a counterparty are RFQ
    and block-trade negotiation, which are yours alone. Checked against
    docs.kalshi.com/openapi.yaml on 2026-07-27.

    The only way to build this is to scrape the web app, which this project
    does not do. So the class exists purely to say so out loud when someone
    enables it — a detector that is silently absent from the registry looks
    identical to one that is running and finding nothing, and that is the
    worse failure.
    """

    name = "leaderboard_watcher"

    def __init__(self) -> None:
        self._warned = False

    def enabled(self, config: Config) -> bool:
        return bool(getattr(config.detectors.leaderboard_watcher, "enabled", False))

    async def scan(self, session: AsyncSession, config: Config) -> list[Finding]:
        if not self._warned:
            log.warning(
                "leaderboard_watcher is enabled but cannot run: Kalshi publishes "
                "no leaderboard or trader-ranking endpoint, and this project "
                "does not scrape. Set detectors.leaderboard_watcher.enabled to "
                "false; nothing is being missed."
            )
            self._warned = True
        return []

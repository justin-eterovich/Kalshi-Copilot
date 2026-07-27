"""SQLAlchemy models.

Units mirror the Kalshi API exactly:

- **Prices are dollars** stored as ``Numeric(12, 6)``. The API quotes
  fixed-point dollar strings with up to 6 decimals and tick size varies by
  market, so sub-cent prices are real and must not be rounded on ingest.
- **Contract counts are ``Numeric(16, 2)``** because fractional contracts are
  supported down to 0.01.
- **Fees are integer cents**, which they exactly are: the exchange charges a
  whole number of cents per fill.
- **Realised P&L is Decimal cents**, which it exactly is not. P&L is a price
  difference times a count, and with sub-cent tick sizes and fractional
  contracts both factors can be fractional — closing 0.50 contracts for a
  1c move earns half a cent. Rounding each realisation to whole cents would
  quietly accumulate error in the one number the report card is judged on,
  so it is carried exactly and rounded only for display.

Nothing money-related is a float. Money that drifts is money you cannot audit.

Timescale hypertables (``candles``, ``tape``, ``orderbook_snaps``,
``external_prices``) are created by ``app.db.bootstrap``. Their primary keys
are composite ``(id, ts)`` because Timescale requires the partitioning column
in every unique index.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: Dollar price, up to 6 decimals (the API's documented maximum precision).
PriceType = Numeric(12, 6)
#: Contract count, 0.01 granularity.
QtyType = Numeric(16, 2)
#: Money in cents, carried exactly. Fractional because neither of the things
#: stored in it is a whole number of cents: P&L is a price difference times a
#: possibly-fractional count, and the exchange bills fees to six decimal
#: places of a dollar (observed on demo: 2 contracts at 20c cost $0.022400,
#: i.e. 2.24 cents, not 3).
CentsType = Numeric(20, 6)


class Side(enum.StrEnum):
    YES = "yes"
    NO = "no"


class ProposalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    #: Some legs executed and others did not. The exchange has no atomic
    #: multi-order primitive, so this is a real outcome, not a bug — and it
    #: needs a human, because the position is unbalanced.
    PARTIAL = "partial"
    FAILED = "failed"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


def _ts() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Catalog & market data
# ---------------------------------------------------------------------------


class Series(Base):
    __tablename__ = "series"

    ticker: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    frequency: Mapped[str | None] = mapped_column(String(32))
    # Settlement-source text. The weather engine parses the station out of
    # this rather than assuming one — the station *is* the market.
    settlement_sources: Mapped[list | None] = mapped_column(JSONB)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    __tablename__ = "events"

    ticker: Mapped[str] = mapped_column(String(128), primary_key=True)
    series_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    sub_title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    #: API: "If true, only one market in this event can resolve to 'yes'."
    #:
    #: Read that carefully — it says **at most one**, not exactly one. It does
    #: NOT imply the set is exhaustive, and in practice many are not:
    #: KXNEWPOPE-70 carries 7 legs whose asks sum to 4.12, because there are
    #: obviously more than 7 possible popes.
    #:
    #: That asymmetry decides what set-arbitrage may signal. Selling every leg
    #: is safe on exclusivity alone (at most one pays out, so collecting more
    #: than $1 is riskless). Buying every leg is only an arb if some leg is
    #: guaranteed to win — which needs exhaustiveness, and nothing in the API
    #: tells us that. An earlier comment here claimed this flag meant
    #: "exhaustive"; it does not.
    mutually_exclusive: Mapped[bool | None] = mapped_column(Boolean)
    strike_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (
        Index("ix_markets_status_close", "status", "close_time"),
        Index("ix_markets_series_status", "series_ticker", "status"),
    )

    ticker: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    series_ticker: Mapped[str | None] = mapped_column(String(128), index=True)

    market_type: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(Text)
    yes_sub_title: Mapped[str | None] = mapped_column(Text)
    no_sub_title: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str | None] = mapped_column(String(32), index=True)

    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    expected_expiration_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    latest_expiration_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: Server-side "last metadata change" marker, used for incremental sync.
    updated_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    # -- quotes (dollars) --
    yes_bid: Mapped[Decimal | None] = mapped_column(PriceType)
    yes_ask: Mapped[Decimal | None] = mapped_column(PriceType)
    no_bid: Mapped[Decimal | None] = mapped_column(PriceType)
    no_ask: Mapped[Decimal | None] = mapped_column(PriceType)
    last_price: Mapped[Decimal | None] = mapped_column(PriceType)
    previous_price: Mapped[Decimal | None] = mapped_column(PriceType)

    # -- sizes (contracts) --
    yes_bid_size: Mapped[Decimal | None] = mapped_column(QtyType)
    yes_ask_size: Mapped[Decimal | None] = mapped_column(QtyType)
    volume: Mapped[Decimal | None] = mapped_column(QtyType)
    volume_24h: Mapped[Decimal | None] = mapped_column(QtyType)
    open_interest: Mapped[Decimal | None] = mapped_column(QtyType)
    liquidity_dollars: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))

    # -- settlement / structure --
    result: Mapped[str | None] = mapped_column(String(16))
    settlement_value: Mapped[Decimal | None] = mapped_column(PriceType)
    settlement_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    strike_type: Mapped[str | None] = mapped_column(String(32))
    floor_strike: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    cap_strike: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    #: Defines the market's valid tick sizes; needed to round quotes legally.
    price_level_structure: Mapped[str | None] = mapped_column(String(64))

    #: Full settlement rules. The weather engine parses the station from here.
    rules_primary: Mapped[str | None] = mapped_column(Text)
    rules_secondary: Mapped[str | None] = mapped_column(Text)

    raw: Mapped[dict | None] = mapped_column(JSONB)

    #: When ingest first saw this ticker — new listings are an opportunity feed.
    first_seen_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Candle(Base):
    """1m OHLC built from the tape/ticker stream. Hypertable on ``ts``."""

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("ticker", "period_sec", "ts", name="uq_candle"),
        Index("ix_candles_ticker_ts", "ticker", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    period_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    open: Mapped[Decimal | None] = mapped_column(PriceType)
    high: Mapped[Decimal | None] = mapped_column(PriceType)
    low: Mapped[Decimal | None] = mapped_column(PriceType)
    close: Mapped[Decimal | None] = mapped_column(PriceType)
    volume: Mapped[Decimal] = mapped_column(QtyType, default=Decimal(0))
    open_interest: Mapped[Decimal | None] = mapped_column(QtyType)
    trades: Mapped[int] = mapped_column(Integer, default=0)


class OrderbookSnap(Base):
    """Throttled top-N book snapshots. Hypertable on ``ts``.

    Levels are stored as ``[[price_dollars, count], ...]`` with values kept as
    strings so no precision is lost round-tripping through JSON.
    """

    __tablename__ = "orderbook_snaps"
    __table_args__ = (Index("ix_book_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Sequence number from the WS stream. A gap means the local book is
    #: untrustworthy and must be rebuilt from a fresh snapshot.
    seq: Mapped[int | None] = mapped_column(BigInteger)
    yes_levels: Mapped[list | None] = mapped_column(JSONB)
    no_levels: Mapped[list | None] = mapped_column(JSONB)


class Tape(Base):
    """Full public trade tape. Hypertable on ``ts``."""

    __tablename__ = "tape"
    __table_args__ = (
        # ts is in the constraint because Timescale requires the partitioning
        # column in every unique index. A trade_id only ever carries one
        # timestamp, so this still dedupes replayed trades correctly.
        UniqueConstraint("trade_id", "ts", name="uq_tape_trade_id"),
        Index("ix_tape_ticker_ts", "ticker", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    trade_id: Mapped[str | None] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    yes_price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    no_price: Mapped[Decimal | None] = mapped_column(PriceType)
    count: Mapped[Decimal] = mapped_column(QtyType, nullable=False)
    #: Which side crossed the spread — the input the whale-flow monitor reads.
    taker_side: Mapped[str | None] = mapped_column(String(8))


class ExternalPrice(Base):
    """Reference prices from outside Kalshi (BTC spot, indices, ...)."""

    __tablename__ = "external_prices"
    __table_args__ = (Index("ix_ext_symbol_ts", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)


# ---------------------------------------------------------------------------
# Signals -> proposals -> orders -> fills
# ---------------------------------------------------------------------------


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_detector_ts", "detector", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    detector: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    side: Mapped[Side] = mapped_column(Enum(Side, name="side"), nullable=False)

    fair_price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    #: Cents per contract, always net of fees and slippage. There is no
    #: gross-edge column on purpose — a gross edge is a lie.
    net_edge_cents: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    size_hint: Mapped[Decimal | None] = mapped_column(QtyType)
    ttl_sec: Mapped[int] = mapped_column(Integer, default=120)

    rationale: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts()
    #: When this observation was last still true, and how many scans have
    #: seen it. A detector re-derives the same opportunity every scan — the
    #: screener produced 180 near-identical rows in nine passes — and a table
    #: that long is not read. Folding repeats into one row loses nothing:
    #: "seen 47 times over 15m" is strictly more informative than 47 rows
    #: that differ only in their timestamp, and it distinguishes an edge that
    #: has persisted from one that flickered once.
    last_seen_at: Mapped[datetime] = _ts()
    seen_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ProposedTrade(Base):
    """One decision for a human, covering one or more legs.

    A manual ticket has a single leg. A set arbitrage has one per market in
    the event, and they are approved together or not at all — approving three
    legs of a five-leg arb leaves an uncovered position, which is the exact
    opposite of what the arb was.

    The legs are the source of truth for what would be traded; this row holds
    only the aggregate economics and the decision state.
    """

    __tablename__ = "proposed_trades"
    __table_args__ = (Index("ix_proposals_status_ts", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL")
    )
    #: "manual" for quick-ticket proposals, else the detector name.
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")

    #: The event, when the legs span one. Null for a standalone ticket.
    event_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    #: Denormalised from the first leg purely so the queue can be listed and
    #: indexed without a join. Never write to it directly — legs decide.
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    leg_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    net_edge_cents: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    #: Estimated fee in cents, fractional (fees round to a centicent).
    est_fee_cents: Mapped[Decimal | None] = mapped_column(CentsType)
    #: Worst case for this decision, in cents, across every leg. Persisted
    #: rather than recomputed because the risk layer must evaluate the number
    #: the operator was actually shown on the card: a re-derivation prices
    #: against a book that has moved since, so the limit would be enforcing
    #: something nobody approved.
    max_loss_cents: Mapped[Decimal | None] = mapped_column(CentsType)
    pct_of_bankroll: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ProposalStatus] = mapped_column(
        Enum(ProposalStatus, name="proposal_status"),
        nullable=False,
        default=ProposalStatus.PENDING,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class ProposalLeg(Base):
    """One market's worth of a proposal.

    Every proposal has at least one. Multi-leg proposals are approved as a
    unit, but **the exchange has no atomic multi-order primitive** — the batch
    endpoint returns a separate result per order and promises nothing about
    all-or-nothing. Legs are therefore submitted together and immediately,
    with IOC time-in-force so nothing rests half-done, and any imbalance is
    surfaced rather than hidden. Leg risk is reduced, not eliminated.
    """

    __tablename__ = "proposal_legs"
    __table_args__ = (
        UniqueConstraint("proposal_id", "seq", name="uq_leg_seq"),
        Index("ix_leg_ticker", "ticker"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposed_trades.id", ondelete="CASCADE"), nullable=False
    )
    #: Submission order, stable so the audit trail reads the same way twice.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[Side] = mapped_column(Enum(Side, name="side"), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="buy")
    #: Price on the traded side, in dollars — what you pay per contract on a
    #: buy. "Buy NO at 30c" stores 0.30 here; the YES price the exchange
    #: wants is derived at the wire boundary. See app.trading.direction.
    limit_price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyType, nullable=False)
    #: Fair value on the traded side, when the source had one.
    fair_price: Mapped[Decimal | None] = mapped_column(PriceType)
    est_fee_cents: Mapped[Decimal | None] = mapped_column(CentsType)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposed_trades.id", ondelete="SET NULL")
    )
    #: Which leg of that proposal this order is. Null for pre-M4 rows.
    leg_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposal_legs.id", ondelete="SET NULL")
    )
    #: Client-supplied UUID: makes retries idempotent so a network blip can
    #: never double-place an order.
    client_order_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), index=True)

    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    side: Mapped[Side] = mapped_column(Enum(Side, name="side"), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="buy")
    limit_price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyType, nullable=False)
    filled_contracts: Mapped[Decimal] = mapped_column(QtyType, default=Decimal(0))
    time_in_force: Mapped[str] = mapped_column(String(8), default="gtc")

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    #: True unless this order reached the live exchange with real money.
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Which rail carried it: simulated | demo_exchange | live_exchange.
    #: `is_paper` collapses two of those into one bit; the audit trail wants
    #: to know which, because a demo fill and a simulated fill are different
    #: kinds of evidence.
    route: Mapped[str] = mapped_column(String(16), default="simulated")
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("exchange_fill_id", name="uq_fill_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL")
    )
    exchange_fill_id: Mapped[str | None] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    side: Mapped[Side] = mapped_column(Enum(Side, name="side"), nullable=False)
    #: buy | sell. Needed because ``side`` alone cannot tell "bought 10 YES"
    #: from "sold 10 YES", and those are opposite positions.
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="buy")
    #: Price on ``side``, in dollars — 0.30 for a NO fill at 30c.
    price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyType, nullable=False)
    #: What the exchange actually charged for this fill, in cents, exactly.
    #: Recorded per fill because fees are billed per fill. NOT an integer:
    #: Kalshi bills fractional cents (2 contracts at 20c cost 2.24c on demo),
    #: and truncating that to 2 quietly flatters every P&L number downstream.
    #: The *estimate* in app.core.fees still rounds up, deliberately — see the
    #: note in that module about which direction to be wrong in.
    fee_cents: Mapped[Decimal] = mapped_column(CentsType, default=Decimal(0))
    is_taker: Mapped[bool] = mapped_column(Boolean, default=True)
    #: P&L this fill realised against the position it hit — zero for a fill
    #: that opened or added. Stored per fill, not just rolled into the daily
    #: aggregate, because the risk layer has to ask "were the last N closes
    #: losers?" and an aggregate cannot answer that. See app.trading.risk.
    realized_pnl_cents: Mapped[Decimal] = mapped_column(CentsType, default=Decimal(0))
    ts: Mapped[datetime] = _ts()


class Position(Base):
    """One row per (market, book). Signed, in YES-equivalent contracts.

    ``net_contracts`` is positive for a long YES position and negative for a
    long NO one, because on this exchange buying NO genuinely cancels a YES
    position rather than sitting beside it — a single signed number is the
    only representation where exposure nets correctly.

    ``avg_price`` is therefore the average **YES price** of the open position
    whichever side it is on: 5 NO contracts bought at 30c are carried as
    ``net_contracts = -5, avg_price = 0.70``. Display converts back.
    """

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("ticker", "route", name="uq_position"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Which rail these fills came from. Positions are keyed on it because a
    #: simulated fill and a demo-exchange fill are not the same position:
    #: only one of them exists at Kalshi. Netting them into one book makes
    #: reconciliation against the exchange impossible and silently corrupts
    #: the report card. Observed live — a simulated -27 and a real +2 merged
    #: into -25 while Kalshi held +2.
    route: Mapped[str] = mapped_column(String(16), default="simulated", nullable=False)
    #: Convenience flag: False only for the live exchange. Derived from route.
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    net_contracts: Mapped[Decimal] = mapped_column(QtyType, default=Decimal(0))
    avg_price: Mapped[Decimal] = mapped_column(PriceType, default=Decimal(0))
    realized_pnl_cents: Mapped[Decimal] = mapped_column(
        CentsType, default=Decimal(0)
    )
    fees_paid_cents: Mapped[Decimal] = mapped_column(CentsType, default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WeatherObservation(Base):
    """A station reading, in Fahrenheit because that is how these settle.

    Stored in the settlement's own unit rather than the API's. The NWS reports
    Celsius; converting once on ingest means the conversion is auditable in one
    place instead of being repeated — and occasionally forgotten — everywhere a
    temperature is compared to a strike.

    These are hourly observations, which are a **proxy** for the product these
    markets actually settle on: the Climatological Report (Daily) is a separate
    once-a-day publication, and a running maximum of hourly readings is not
    guaranteed to equal the daily high it reports.
    """

    __tablename__ = "weather_observations"
    __table_args__ = (
        UniqueConstraint("station_id", "observed_at", name="uq_weather_obs"),
        Index("ix_weather_obs_station_ts", "station_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature_f: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    #: The NWS's own QC marker for the reading. Kept rather than filtered on,
    #: because which values mean "trustworthy" is not something to guess at.
    quality_control: Mapped[str | None] = mapped_column(String(8))
    fetched_at: Mapped[datetime] = _ts()


class WeatherForecast(Base):
    """A forecast high/low for one station and one local day.

    ``lead_hours`` is what makes the calibration possible: the same day is
    forecast repeatedly as it approaches, and a forecast three days out is far
    less certain than one made this morning. Keeping every issuance rather than
    overwriting is the entire point — a table that holds only the latest
    forecast can never measure how wrong forecasts are.
    """

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        UniqueConstraint(
            "station_id", "target_date", "measure", "issued_at",
            name="uq_weather_forecast",
        ),
        Index("ix_weather_fc_station_date", "station_id", "target_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Local calendar day at the station — the day the market names.
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: high | low
    measure: Mapped[str] = mapped_column(String(8), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature_f: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    lead_hours: Mapped[float | None] = mapped_column(Float)
    #: Filled in after the day resolves, so (forecast, actual) pairs can be
    #: read straight out of this table by the calibration.
    actual_f: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    fetched_at: Mapped[datetime] = _ts()


class Settlement(Base):
    """A market that resolved while we held a position in it.

    This table exists because **a position held to settlement never realises
    P&L through a fill**, and settlement is how most of this system's theses
    are meant to pay off. Without it the daily loss limit and the loss
    cooldown are blind to the main way money is made or lost, and the report
    card scores only the positions that happened to be traded out of early.

    Deduplicated on ``(ticker, route)``: a market settles exactly once per
    book, and applying a settlement twice would double the realised P&L
    permanently, with nothing downstream to catch it.

    P&L is computed against **our own** ``avg_price``, not against the cost
    basis the exchange reports. The two disagree whenever a position was
    partly traded out before settlement — the exchange's basis covers the
    contracts it still saw, ours covers what we recognised — and mixing the
    two would leave the position and the daily total telling different
    stories. The settlement payload is used only for the two things it alone
    knows: that the market resolved, and what a YES contract paid.
    """

    __tablename__ = "settlements"
    __table_args__ = (UniqueConstraint("ticker", "route", name="uq_settlement"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    route: Mapped[str] = mapped_column(String(16), default="simulated", nullable=False)
    #: yes | no | scalar, as the exchange reports it.
    market_result: Mapped[str | None] = mapped_column(String(16))
    #: What one YES contract paid, in dollars: 1 for yes, 0 for no, and
    #: somewhere between for a scalar market.
    settled_yes_value: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    #: The signed YES-equivalent position that was open when it settled, and
    #: the average YES price it was carried at. Both kept so a settlement row
    #: can be re-derived and argued with after the position row is flat.
    net_contracts: Mapped[Decimal] = mapped_column(QtyType, default=Decimal(0))
    avg_price: Mapped[Decimal] = mapped_column(PriceType, default=Decimal(0))
    realized_pnl_cents: Mapped[Decimal] = mapped_column(CentsType, default=Decimal(0))
    #: Settlement fees, where the series charges them. Fixed-point dollars on
    #: the wire, cents here, and fractional either way.
    fee_cents: Mapped[Decimal] = mapped_column(CentsType, default=Decimal(0))
    #: ``exchange`` when it came from /portfolio/settlements, ``market`` when
    #: it was derived locally from the market's own result. The simulated book
    #: has no exchange settlements — those positions exist nowhere but here.
    source: Mapped[str] = mapped_column(String(16), default="exchange")
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()


class PnlDaily(Base):
    __tablename__ = "pnl_daily"
    __table_args__ = (UniqueConstraint("day", "route", name="uq_pnl_day"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    #: Same reasoning as Position.route — simulated and real P&L are separate
    #: books and must never be summed together.
    route: Mapped[str] = mapped_column(String(16), default="simulated", nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    realized_pnl_cents: Mapped[Decimal] = mapped_column(
        CentsType, default=Decimal(0)
    )
    unrealized_pnl_cents: Mapped[Decimal] = mapped_column(
        CentsType, default=Decimal(0)
    )
    fees_paid_cents: Mapped[Decimal] = mapped_column(CentsType, default=Decimal(0))
    #: Fills applied on this day. Counted separately from settlements because
    #: they are different events: a fill is a decision, a settlement is an
    #: outcome, and a day of three settlements and no trades is not idle.
    trades: Mapped[int] = mapped_column(Integer, default=0)
    settlements: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Analytics & audit
# ---------------------------------------------------------------------------


class DetectorStat(Base):
    """Per-detector report card — the only thing that earns a detector money."""

    __tablename__ = "detector_stats"
    __table_args__ = (UniqueConstraint("detector", "day", name="uq_detector_day"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    detector: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    triggers: Mapped[int] = mapped_column(Integer, default=0)
    approved: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    net_pnl_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_net_edge_cents: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    max_drawdown_cents: Mapped[int] = mapped_column(BigInteger, default=0)


class CalibrationLog(Base):
    """Price-vs-settlement observations for the longshot calibration screen.

    **One row per market, ever.** The unique constraint is the whole
    statistical basis of the table: a market that sits at 5c for a week would
    otherwise contribute thousands of observations that are all the same
    observation, and the sample count the calibration screen refuses below
    (500 by default) would be satisfied by a few dozen markets pretending to
    be a thousand. Resampling adds rows, not information.
    """

    __tablename__ = "calibration_log"
    __table_args__ = (
        UniqueConstraint("ticker", name="uq_calibration_ticker"),
        Index("ix_calib_bucket", "price_bucket_cents"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    price_bucket_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    hours_to_close: Mapped[float | None] = mapped_column(Float)
    settled_yes: Mapped[bool | None] = mapped_column(Boolean)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only. Never updated, never deleted."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"), Index("ix_audit_kind", "kind"))

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime] = _ts()
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(128))
    actor: Mapped[str] = mapped_column(String(32), default="system")
    payload: Mapped[dict | None] = mapped_column(JSONB)


class ConfigKV(Base):
    """UI-editable overrides; takes precedence over config.yaml."""

    __tablename__ = "config_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

"""SQLAlchemy models.

Units mirror the Kalshi API exactly:

- **Prices are dollars** stored as ``Numeric(12, 6)``. The API quotes
  fixed-point dollar strings with up to 6 decimals and tick size varies by
  market, so sub-cent prices are real and must not be rounded on ingest.
- **Contract counts are ``Numeric(16, 2)``** because fractional contracts are
  supported down to 0.01.
- **Fees and realised P&L are integer cents**, which they exactly are.

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


class Side(enum.StrEnum):
    YES = "yes"
    NO = "no"


class ProposalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
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
    # True when the event's markets form an exhaustive mutually-exclusive set,
    # which is precisely the condition the set-arbitrage detector needs.
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


class ProposedTrade(Base):
    __tablename__ = "proposed_trades"
    __table_args__ = (Index("ix_proposals_status_ts", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL")
    )
    #: "manual" for quick-ticket proposals, else the detector name.
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")

    ticker: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    side: Mapped[Side] = mapped_column(Enum(Side, name="side"), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="buy")
    limit_price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyType, nullable=False)

    net_edge_cents: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    est_fee_cents: Mapped[int | None] = mapped_column(Integer)
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


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("proposed_trades.id", ondelete="SET NULL")
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
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
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
    price: Mapped[Decimal] = mapped_column(PriceType, nullable=False)
    contracts: Mapped[Decimal] = mapped_column(QtyType, nullable=False)
    #: Fees round up per fill, so this is recorded per fill, not per order.
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_taker: Mapped[bool] = mapped_column(Boolean, default=True)
    ts: Mapped[datetime] = _ts()


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("ticker", "is_paper", name="uq_position"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    net_contracts: Mapped[Decimal] = mapped_column(QtyType, default=Decimal(0))
    avg_price: Mapped[Decimal] = mapped_column(PriceType, default=Decimal(0))
    realized_pnl_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    fees_paid_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PnlDaily(Base):
    __tablename__ = "pnl_daily"
    __table_args__ = (UniqueConstraint("day", "is_paper", name="uq_pnl_day"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    realized_pnl_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    unrealized_pnl_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    fees_paid_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    trades: Mapped[int] = mapped_column(Integer, default=0)


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
    """Price-vs-settlement observations for the longshot calibration screen."""

    __tablename__ = "calibration_log"
    __table_args__ = (Index("ix_calib_bucket", "price_bucket_cents"),)

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

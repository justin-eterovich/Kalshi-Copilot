"""Screener filter semantics — what "max spread" is allowed to return.

These run the *real* SQLAlchemy conditions from `app.api.routes.markets`
against a real database rather than asserting on rendered SQL text, because
the bug being fixed here was a semantic one that a string comparison would
have happily reproduced: `(yes_ask - yes_bid) <= 0.01` is perfectly good SQL
and returns exactly the wrong rows.

The database is in-memory SQLite, which the standard library provides, so this
needs no service and no fixture. Two consequences are worth knowing:

- `markets.raw` is `JSONB`, which SQLite's type compiler cannot render, so the
  shim below maps it to TEXT for DDL only. Nothing here reads it.
- SQLite has no exact NUMERIC: `yes_ask - yes_bid` is float arithmetic here and
  `Decimal` arithmetic under Postgres. Every threshold case below is therefore
  chosen well clear of the boundary, so no assertion depends on which one runs.
  Exact-boundary behaviour is a Postgres property and is not claimed here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.api.routes.markets import _market_row, _max_spread_filters
from app.db.models import Market


@compiles(JSONB, "sqlite")
def _jsonb_as_text(type_: Any, compiler: Any, **kw: Any) -> str:
    """DDL-only shim: SQLite has no JSONB and no test here touches `raw`."""
    return "TEXT"


#: (ticker, yes_bid, yes_ask) — one row per kind of book the catalog holds.
#:
#: The two "phantom" rows are not hypothetical. Measured against the live
#: catalog on 2026-07-29, `max_spread=0.01` returned 66,547 rows of which
#: 64,211 were 0.000000/0.000000 and 35 were crossed, leaving 1,638 real ones.
_BOOKS: tuple[tuple[str, str | None, str | None], ...] = (
    ("TIGHT", "0.4000", "0.4050"),
    ("LOCKED", "0.3600", "0.3600"),
    ("WIDE", "0.1000", "0.5000"),
    ("CROSSED-SLIGHTLY", "0.0980", "0.0770"),
    ("CROSSED-DEEPLY", "0.6200", "0.3400"),
    ("NO-BOOK", "0.0000", "0.0000"),
    ("BID-ONLY", "0.4000", None),
    ("ASK-ONLY", None, "0.4050"),
    ("NO-QUOTE", None, None),
    ("NO-BID", "0.0000", "0.0100"),
    ("SETTLED-YES", "1.0000", "1.0000"),
)


@pytest.fixture(name="session")
def _session() -> Any:
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Market.__table__.create(engine)
    with Session(engine) as session:
        session.add_all(
            Market(
                ticker=ticker,
                status="active",
                yes_bid=None if bid is None else Decimal(bid),
                yes_ask=None if ask is None else Decimal(ask),
            )
            for ticker, bid, ask in _BOOKS
        )
        session.commit()
        yield session
    engine.dispose()


def _book(bid: str | None, ask: str | None) -> Any:
    """A stand-in for the projected row `_market_row`/`_liquidity_score` read.

    They take a SQLAlchemy `Row`, which exposes its columns as attributes, so
    anything with the right attribute names is a faithful double. Volume and
    open interest are absent on purpose: every test using this is about the
    quote, and a size term left set would let a book score for the wrong
    reason.
    """

    class M:
        ticker = "T"
        event_ticker = None
        series_ticker = None
        title = None
        yes_sub_title = None
        no_sub_title = None
        category = None
        status = "active"
        no_bid = None
        no_ask = None
        last_price = None
        previous_price = None
        volume = None
        volume_24h = None
        open_interest = None
        liquidity_dollars = None
        close_time = None
        first_seen_at = None

    m = M()
    m.yes_bid = None if bid is None else Decimal(bid)  # type: ignore[attr-defined]
    m.yes_ask = None if ask is None else Decimal(ask)  # type: ignore[attr-defined]
    return m


def _matches(session: Any, max_spread: float) -> set[str]:
    stmt = select(Market.ticker)
    for condition in _max_spread_filters(max_spread):
        stmt = stmt.where(condition)
    return set(session.execute(stmt).scalars().all())


class TestMaxSpreadFilter:
    def test_returns_only_genuinely_tight_two_sided_books(
        self, session: Any
    ) -> None:
        """The whole point of the filter, stated as an exact set.

        Asserting equality rather than membership is deliberate: every
        regression this guards against is a row that should not be here, and a
        `in` assertion would pass while the result was 97% noise.
        """
        assert _matches(session, 0.01) == {"TIGHT", "LOCKED"}

    def test_crossed_books_are_excluded_at_every_threshold(
        self, session: Any
    ) -> None:
        """The filed bug: a negative difference cleared even the tightest bar.

        A book crossed by 28c passed `<= 1c` and, sorted by volume, arrived
        ahead of every real quote — the least trustworthy rows in the catalog
        served first to an operator asking for the most trustworthy.
        """
        for threshold in (0.001, 0.01, 0.05, 0.5, 1.0):
            matched = _matches(session, threshold)
            assert "CROSSED-SLIGHTLY" not in matched
            assert "CROSSED-DEEPLY" not in matched

    def test_unquoted_market_is_not_a_zero_spread_market(
        self, session: Any
    ) -> None:
        """0.000000/0.000000 means nobody is quoting, not "perfectly tight"."""
        for threshold in (0.001, 0.01, 1.0):
            assert "NO-BOOK" not in _matches(session, threshold)

    def test_one_sided_books_are_excluded(self, session: Any) -> None:
        """A book with one side has no spread; a substituted side is a lie."""
        matched = _matches(session, 1.0)
        assert not matched & {"BID-ONLY", "ASK-ONLY", "NO-QUOTE"}

    def test_a_side_outside_zero_to_one_is_excluded(self, session: Any) -> None:
        """A YES price is a dollar probability; 0 and 1 are not live quotes.

        `NO-BID` is the asymmetric case worth keeping: an ask at 1c with no bid
        at all is a 1c difference, so it would clear a "tight" filter while
        being un-sellable at any price.
        """
        matched = _matches(session, 1.0)
        assert not matched & {"NO-BID", "SETTLED-YES"}

    def test_a_wide_book_is_excluded_for_the_ordinary_reason(
        self, session: Any
    ) -> None:
        """The filter must still do its original job."""
        assert "WIDE" not in _matches(session, 0.01)
        assert "WIDE" in _matches(session, 0.5)

    def test_locked_book_is_kept(self, session: Any) -> None:
        """bid == ask is tight and real, unlike a crossed or absent book.

        Excluding it would be the same over-correction in the other direction:
        this is a quote an operator can hit.
        """
        assert "LOCKED" in _matches(session, 0.0)

    def test_the_filter_this_replaces_did_return_the_bad_rows(
        self, session: Any
    ) -> None:
        """Characterisation of the bug, so the fix cannot be undone silently.

        Written against the condition that shipped rather than against an
        index into the new list, so it stays honest if the conditions are
        reordered or merged. If someone simplifies back towards a bare
        subtraction, this is what fails.
        """
        stmt = select(Market.ticker).where(
            (Market.yes_ask - Market.yes_bid) <= Decimal("0.01")
        )
        naive = set(session.execute(stmt).scalars().all())

        assert {"CROSSED-DEEPLY", "CROSSED-SLIGHTLY", "NO-BOOK", "NO-BID"} <= naive
        # And the fix strictly narrows: it removes rows, never adds one.
        assert _matches(session, 0.01) < naive


class TestQuoteDefinitionsAgree:
    """The SQL filter and the liquidity score must mean the same thing by "quote".

    `_max_spread_filters` decides which rows a spread query *returns*;
    `_has_two_sided_quote` decides which rows get a tradeability *score*. They
    cannot share an implementation — one is SQL, the other Python — so nothing
    but this test stops them drifting, and drift here is not cosmetic: it is a
    screener that hides a row from a filter while displaying a full-confidence
    score for it, or the reverse.

    Both got this wrong independently and in the same direction. The filter
    treated 0.000000/0.000000 as a zero spread and returned 64,211 of them; the
    score treated it as a zero spread and rated them tradeable. Neither bug is
    visible from the other's file.
    """

    @pytest.mark.parametrize(("ticker", "bid", "ask"), _BOOKS)
    def test_scored_if_and_only_if_the_spread_filter_would_return_it(
        self, session: Any, ticker: str, bid: str | None, ask: str | None
    ) -> None:
        from app.api.routes.markets import _liquidity_score

        # A threshold of 1.0 dollars cannot bind: the widest possible spread on
        # a market with both sides strictly inside (0, 1) is under a dollar. So
        # membership here is decided purely by what counts as a quote, which is
        # the thing under test.
        returned_by_filter = ticker in _matches(session, 1.0)

        scored = _liquidity_score(_book(bid, ask)) is not None
        assert scored is returned_by_filter


class TestQuoteCrossedFlag:
    """`quote_crossed` — the server deciding, so the browser need not.

    The client holds both prices as strings and must keep them that way, so it
    cannot compare them without either a float parse or an assumption about
    fixed-width formatting. This is that comparison, done once, in Decimal.
    """

    @staticmethod
    def row(bid: str | None, ask: str | None) -> dict[str, Any]:
        return _market_row(_book(bid, ask))

    def test_crossed_book_is_flagged(self) -> None:
        assert self.row("0.6200", "0.3400")["quote_crossed"] is True

    def test_normal_book_is_not_flagged(self) -> None:
        assert self.row("0.4000", "0.4050")["quote_crossed"] is False

    def test_locked_book_is_not_flagged(self) -> None:
        """bid == ask is not crossed. Off-by-one here would flag 64k markets."""
        assert self.row("0.3600", "0.3600")["quote_crossed"] is False

    def test_one_sided_book_is_unknown_not_false(self) -> None:
        """None, not False.

        `False` would tell the operator the quote had been checked and found
        sound, which is the opposite of what a missing side means. Same
        tri-state trap CLAUDE.md names for `Market.result` and `settled`.
        """
        assert self.row("0.4000", None)["quote_crossed"] is None
        assert self.row(None, "0.4050")["quote_crossed"] is None
        assert self.row(None, None)["quote_crossed"] is None

    def test_flag_agrees_with_the_sign_of_the_spread(self) -> None:
        """The two fields are derived separately and must not disagree."""
        for bid, ask in (("0.6200", "0.3400"), ("0.4000", "0.4050"), ("0.1", "0.1")):
            row = self.row(bid, ask)
            assert row["quote_crossed"] is (Decimal(str(row["spread"])) < 0)

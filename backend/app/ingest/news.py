"""Collecting headlines from the configured feeds.

Fetch, parse, deduplicate, store. Nothing here scores a headline, and nothing
here spends money: the LLM triage is a separate step behind
:mod:`app.news.budget`, and it does not run at all on this deployment because
no API key is configured.

**Collection is deliberately separated from interpretation.** Storing the text
is cheap, reversible and useful on its own — it is the record of what was
public and when, which is exactly what you want when a market moved and you
are trying to work out whether the information was available. Interpreting it
costs money and can be wrong. Keeping the two apart means the expensive half
can stay off indefinitely without the cheap half losing history.

The relevance pre-filter *is* run here, because it is pure arithmetic over
text we already hold. Its output is a list of tickers the headline might be
about, stored on the row, and its usual answer is the empty list: almost no
world news is about any particular Kalshi market.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Config
from app.core.logging import get_logger
from app.db.models import Market, NewsHeadline
from app.news.client import FeedClient
from app.news.feeds import dedupe, parse_feed
from app.news.relevance import MarketRef, match_headline

log = get_logger(__name__)

__all__ = ["sweep_headlines", "candidate_markets"]

#: Relevance floor for storing a possible match. Precision-first: a false
#: match spends the LLM budget on noise and puts an irrelevant note in front
#: of a human, and both are worse than the silence of no match at all.
MIN_RELEVANCE = 0.35

#: How far back to accept an item on first sight. This bounds the work of a
#: cold start; it is **not** a freshness judgement, and it was two days until
#: a live run showed why that was wrong. Official feeds are low-volume — the
#: Federal Reserve press feed's newest item was eleven days old — so a short
#: window silences exactly the sources whose releases actually settle markets,
#: while a chatty newswire is unaffected. Re-processing is already prevented
#: by the unique constraint on `guid`, so the only cost of a wider window is
#: one pass over items we will immediately recognise.
MAX_HEADLINE_AGE = timedelta(days=30)


async def candidate_markets(
    session: AsyncSession, *, limit: int = 3000
) -> list[MarketRef]:
    """Active markets a headline could be matched against.

    Bounded and projected — the same lesson as the screener, which killed the
    worker by loading 122,887 full ORM rows. Relevance scoring is relative to
    the set it is given, so this cap also bounds what "distinctive" means; a
    limitation the matcher's own docstring states.
    """
    rows = (
        await session.execute(
            select(Market.ticker, Market.series_ticker, Market.title)
            .where(Market.status == "active", Market.title.isnot(None))
            .order_by(Market.volume_24h.desc().nullslast())
            .limit(limit)
        )
    ).all()
    return [
        MarketRef(ticker=t, series_ticker=s, title=title)
        for t, s, title in rows
        if title
    ]


async def sweep_headlines(
    session: AsyncSession, client: FeedClient, config: Config
) -> tuple[int, int]:
    """Poll every configured feed once. Returns (new headlines, matched).

    A feed that fails is logged and skipped — one publisher returning 503 must
    not cost us every other source.
    """
    feeds = config.news.headlines.rss_feeds
    if not feeds:
        return 0, 0

    markets = await candidate_markets(session)
    cutoff = datetime.now(UTC) - MAX_HEADLINE_AGE

    collected: list[object] = []
    for feed in feeds:
        result = await client.fetch(source=feed.source, url=feed.url)
        if not result.ok:
            log.warning("feed %s failed: %s", feed.source, result.error)
            continue
        assert result.xml is not None
        collected.extend(parse_feed(result.xml, source=feed.source))

    fresh = [h for h in dedupe(collected) if h.published_at >= cutoff]  # type: ignore[attr-defined]

    added = 0
    matched = 0
    for headline in fresh:
        matches = (
            match_headline(headline.title, markets, min_score=MIN_RELEVANCE)
            if markets
            else []
        )
        tickers = [m.ticker for m in matches]

        stmt = (
            pg_insert(NewsHeadline)
            .values(
                guid=headline.guid,
                source=headline.source,
                title=headline.title,
                link=headline.link,
                summary=headline.summary,
                published_at=headline.published_at,
                matched_tickers=tickers,
            )
            # Seen before. Re-matching it would not change anything and
            # overwriting would lose the timestamp of first sighting, which is
            # the only thing that says when the information became public.
            .on_conflict_do_nothing(constraint="uq_headline_guid")
        )
        result_row = await session.execute(stmt)
        if result_row.rowcount:
            added += 1
            if tickers:
                matched += 1

    if added:
        log.info(
            "news: %d new headline(s), %d with a possible market match",
            added,
            matched,
        )
    return added, matched

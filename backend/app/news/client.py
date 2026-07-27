"""Fetching RSS/Atom feeds.

Transport only. Everything that interprets the XML lives in
:mod:`app.news.feeds`, so feed parsing is pinned by tests that need no
network.

Feeds are other people's servers, usually free, often run by government
agencies with no interest in our polling habits. So: a real User-Agent, a
short timeout, and a poll interval measured in minutes. There is nothing to
gain from polling faster — an item that appears thirty seconds sooner is
still an item published after the market moved, and on Kalshi's scheduled
economic releases the market has already closed by the time the number is
public.

Failures are returned, not raised. One feed being down must not stop the
others: a poller that dies on a 503 from a single publisher loses every
source it was watching, which is a much worse outcome than missing one.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["FeedResult", "FeedClient"]


@dataclass(frozen=True, slots=True)
class FeedResult:
    """The outcome of one fetch. ``xml`` is None exactly when ``error`` is set."""

    source: str
    url: str
    xml: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.xml is not None


class FeedClient:
    """Minimal async fetcher for a handful of feeds."""

    def __init__(self, *, user_agent: str, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> FeedClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def fetch(self, *, source: str, url: str) -> FeedResult:
        """Fetch one feed, returning any failure rather than raising it."""
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            return FeedResult(source, url, None, f"request failed: {exc}")

        if response.status_code != 200:
            return FeedResult(
                source, url, None, f"HTTP {response.status_code}"
            )

        # `response.text` decodes using the declared charset. Feeds are
        # routinely served as latin-1 or with a lying header, and httpx falls
        # back to a charset guess rather than throwing — which is the right
        # trade here, since a mojibake headline is still a readable headline
        # and a hard failure would drop the whole feed.
        return FeedResult(source, url, response.text)

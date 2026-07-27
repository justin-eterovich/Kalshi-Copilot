"""Parse RSS 2.0 and Atom documents into timestamped headlines. Nothing else.

Pure functions over already-fetched text. No network, no clock, no I/O — the
HTTP client lives elsewhere, so every branch here is testable against a
captured document.

**A headline is not an edge, and nothing here pretends otherwise.** By the time
an item reaches an RSS feed the market has already moved: the wire services,
the terminals and the algorithms saw it first, and a poller on a 60-second
cadence is reading history. For Kalshi's scheduled economic releases it is
worse than late — those markets **close minutes before the data publishes**, so
the headline announcing the print arrives when there is no book left to trade.
The only honest use of this module is as a *timestamped record of what was said
and when*: context for a human, a settlement breadcrumb, an input to something
that already had a reason to be interested. So this module computes no score,
no sentiment, no relevance and no direction. It emits text with a timestamp and
stops.

**The dangerous version of this module** is easy to write and every one of its
shortcuts produces something that looks exactly like news:

- It stamps an item that carried no usable date with ``datetime.now()``, or
  with a naive local-time datetime. Either makes a three-week-old republished
  item the freshest thing in the poller, which is precisely backwards: the
  items most likely to lack a clean date are the ones being re-served. Here, an
  item with no parseable, timezone-aware timestamp is **dropped**. A headline
  you cannot place in time is not a headline you can act on.
- It derives identity from the title, or from position in the feed. Feeds
  reorder, republish and revise constantly (the GitHub Atom feed used in the
  tests is not even in chronological order), so identity-by-position produces
  a fresh "breaking" item on every poll. See :func:`stable_guid`.
- It scores relevance inline, and the score inherits every parsing defect
  above without ever showing it. Scoring lives in another module, downstream of
  this one, working from data that has already refused what it cannot read.
- It string-matches tag names by suffix (``tag.endswith("link")``) and so
  happily reads an RSS channel's ``<atom:link rel="self">`` — a pointer to the
  feed itself — as the article URL. The BBC feed does exactly this. Namespaces
  are handled explicitly below.

Two formats, detected from the document
---------------------------------------
Feed format is read off the **root element**, never configured per source. A
source can and does change its generator without telling anyone, and a
per-feed setting that says "RSS" while the server now serves Atom fails as an
empty poll rather than an error.

- ``<rss version="2.0">`` — items at ``channel/item``, un-namespaced, dates in
  RFC 2822 (``Mon, 27 Jul 2026 14:21:32 GMT``).
- ``{http://www.w3.org/2005/Atom}feed`` — entries namespaced throughout, dates
  in ISO 8601 (``2026-07-18T07:57:43Z``), and the article URL is an
  **attribute** (``<link rel="alternate" href="..."/>``) with no text at all.

Detection is by root tag rather than by "does the document mention the Atom
namespace", because most RSS feeds in the wild *do* mention it: BBC, arXiv and
every WordPress feed declare ``xmlns:atom`` in order to carry a single
``<atom:link rel="self">``. Anything else at the root — an RSS 1.0/RDF
document, or the HTML "Access Denied" page that BLS and SEC serve to
unrecognised user agents with a 200 status — yields ``[]``. That HTML case is
the one to keep in mind: it is not a network error, nothing upstream will
notice it, and it parses cleanly as XML often enough to matter.

Within RSS, two namespaced elements are read deliberately because they are
common and useful: Dublin Core ``<dc:date>`` as a date fallback and
``<content:encoded>`` as a summary fallback. No other namespace is consulted.

Markup in titles and summaries
------------------------------
CDATA, ``&amp;`` and embedded HTML are all normal, in every combination. What
happens here, in order, and deliberately:

1. Tag-shaped runs are deleted by regex — ``</?[A-Za-z]...>`` and comments,
   never a bare ``<`` followed by a space, so prose like "3 < 4 > 2" survives.
2. HTML entities are unescaped **once** (``&nbsp;``, ``&#39;``).
3. Tag-shaped runs are deleted **once more**, and then no further. Feeds
   double-escape (Federal Register emits ``&amp;#39;``; CISA wraps whole HTML
   documents in an escaped ``<description>``), so one pass is not enough and an
   unbounded loop is an HTML sanitiser written by accident. Two passes, then
   whatever is left is text.
4. All whitespace runs — including the non-breaking spaces Google News pads
   with — collapse to single spaces.

**This is not an HTML parser and the output is not sanitised HTML.** It is
plain text, and consumers must render it as plain text. A feed that nests
markup three levels of escaping deep will leave literal angle brackets in the
string; that is the intended failure — visible, inert, and not a security
boundary anyone should be leaning on.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree as ET

__all__ = [
    "Headline",
    "parse_feed",
    "parse_timestamp",
    "stable_guid",
    "dedupe",
]

# Namespaces, spelled out once. ElementTree hands back `{uri}tag` names, so
# every qualified lookup below is built from these rather than matched by
# suffix — see the module docstring on `<atom:link rel="self">`.
_ATOM_NS = "http://www.w3.org/2005/Atom"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

_ATOM_FEED = f"{{{_ATOM_NS}}}feed"
_ATOM_ENTRY = f"{{{_ATOM_NS}}}entry"
_ATOM_ID = f"{{{_ATOM_NS}}}id"
_ATOM_TITLE = f"{{{_ATOM_NS}}}title"
_ATOM_LINK = f"{{{_ATOM_NS}}}link"
_ATOM_PUBLISHED = f"{{{_ATOM_NS}}}published"
_ATOM_UPDATED = f"{{{_ATOM_NS}}}updated"
_ATOM_SUMMARY = f"{{{_ATOM_NS}}}summary"
_ATOM_CONTENT = f"{{{_ATOM_NS}}}content"

_DC_DATE = f"{{{_DC_NS}}}date"
_CONTENT_ENCODED = f"{{{_CONTENT_NS}}}encoded"

#: A tag-shaped run: an element, a closing element, or a comment.
#:
#: The leading ``[A-Za-z]`` after the optional slash is load-bearing. The naive
#: ``<[^>]*>`` eats "< b and c >" out of ordinary prose, and a headline that
#: quietly loses its middle is worse than one that keeps a stray bracket.
_MARKUP_RE = re.compile(r"<(?:/?[A-Za-z][^<>]*|!--.*?--)>", re.DOTALL)

#: Atom ``rel`` values we will accept as "this is the article".
#:
#: RFC 4287: a ``<link>`` with no ``rel`` means ``alternate``. Everything else
#: (``self``, ``replies``, ``enclosure``, ``via``, ``hub``, ``edit``) points at
#: something that is not the document, and there is no ranking among them worth
#: guessing at — an unrecognised ``rel`` yields no link rather than a wrong one.
_DOCUMENT_RELS = frozenset({"alternate"})


@dataclass(frozen=True, slots=True)
class Headline:
    """One feed item that survived parsing, in full.

    Every field is either verified or absent. There is no partially-built
    ``Headline``: an item missing a title or a usable timestamp is dropped by
    :func:`parse_feed` rather than emitted with a placeholder.
    """

    #: Deduplication key from :func:`stable_guid` — ``"<kind>:<sha256>"``, where
    #: kind is ``guid``, ``link`` or ``title``. Scoped to ``source``.
    guid: str
    #: Plain text, markup stripped, whitespace collapsed. Never empty.
    title: str
    #: The article URL as the feed gave it, **unnormalised**. ``None`` when the
    #: feed carried none, which is legal in both formats.
    link: str | None
    #: Timezone-aware UTC. Never naive — see :func:`parse_timestamp`.
    published_at: datetime
    #: Caller-supplied feed identity, carried through verbatim. Part of
    #: ``guid``, so it must be stable across polls for dedupe to work.
    source: str
    #: Plain text like ``title``. ``None`` when absent or empty after
    #: stripping — a summary that was pure markup leaves nothing behind, and an
    #: empty string would read as "the publisher said nothing", which is a
    #: different claim.
    summary: str | None


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a feed timestamp to an aware UTC datetime, or ``None``.

    Handles both wire formats, tried in that order:

    - **RFC 2822** (RSS ``<pubDate>``): ``Mon, 27 Jul 2026 14:21:32 GMT``, via
      :func:`email.utils.parsedate_to_datetime`.
    - **ISO 8601** (Atom ``<updated>``, ``<dc:date>``):
      ``2026-07-18T07:57:43Z``. A trailing ``Z`` is rewritten to ``+00:00``
      first, because :meth:`datetime.fromisoformat` accepts the offset form but
      not the military-zone letter in every position.

    The formats are disjoint in practice — each parser raises ``ValueError`` on
    the other's input — so trying both costs one exception and removes the need
    for a per-feed setting that would eventually be wrong.

    **A timestamp without a timezone is refused, not assumed to be UTC.** This
    is the single most dangerous shortcut available here. Naive stamps arrive
    from three real sources: a ``<pubDate>`` with the zone simply omitted; the
    RFC 2822 zone ``-0000``, which means "local time, zone deliberately
    withheld" and which :func:`parsedate_to_datetime` correctly reports as
    naive; and an unrecognised alphabetic zone such as ``XYZ``. Reading any of
    them as UTC is a silent error of up to twelve hours, in a module whose only
    product is *when*, and it survives every downstream freshness check because
    the result is a perfectly ordinary-looking datetime.

    A date-only stamp (``2026-07-18``) is refused for the same reason: it is
    midnight in an unstated zone, and treating it as 00:00 UTC would report a
    whole day of items as having landed before they did.

    Values in the future are **kept**; see :func:`parse_feed`.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    parsed: datetime | None = None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None

    if parsed is None:
        iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # Naive. Refuse. See the docstring — this is not a case to paper over.
        return None
    return parsed.astimezone(UTC)


def stable_guid(*, guid: str | None, link: str | None, title: str, source: str) -> str:
    """Derive a deduplication key for one item.

    Returns ``"<kind>:<sha256 hexdigest>"``. The ``kind`` prefix (``guid``,
    ``link``, ``title``) is there so a human reading a stored key can tell how
    much to trust it, and so two bases can never collide with each other.

    **Preference order, strongest first**

    1. **The feed's own** ``<guid>`` / ``<id>``. It is the only field a
       publisher maintains *for* identity, and it survives the things that
       break everything else: a retitled item, a link that gains a tracking
       parameter, a reordered feed. Google News' guid is an opaque token
       unrelated to the URL, which is exactly the point.
    2. **The link.** Used verbatim, with no normalisation — no lowercasing, no
       stripping of query parameters. Which parameters are identity and which
       are tracking is a per-publisher guess, and a wrong guess merges two real
       articles into one. The cost of not guessing is stated below.
    3. **A hash of source + title.** Last resort, and lossy in the dangerous
       direction.

    ``source`` is mixed into **all three** bases. Feed GUIDs are only reliably
    unique *within* a feed — small publishers emit ``<guid>1</guid>`` — so an
    unscoped key would silently merge unrelated items from different feeds.

    **What this chain can detect:** the same item re-served on later polls,
    which is the overwhelmingly common case and the reason dedupe exists.

    **What it cannot:**

    - *A publisher that mints a new guid when only the title changed.* The
      revision arrives as a second headline. That is the safe failure — a
      duplicate is noise; a suppressed update would be a stale belief.
    - *The same story from two feeds.* Scoped keys keep them separate on
      purpose. Cross-source clustering is a relevance problem and this module
      refuses to guess at it.
    - *A link that carries per-request tracking parameters.* Identity churns
      every poll and the item looks new each time. Preferring the guid is the
      mitigation; a feed with neither guid nor stable link cannot be deduped by
      URL at all.
    - *Two genuinely different items with the same title in one feed* — under
      the title fallback only, they collapse into one and **the second is
      dropped**. This is not hypothetical: the Federal Register publishes
      "Sunshine Act Meetings" over and over, as distinct documents. That is why
      the title hash is last and why losing a real item is the one failure mode
      called out here.

    Raises ``ValueError`` when there is no basis at all — every candidate blank.
    An item with no guid, no link and no title has no identity to compute, and
    returning a hash of the empty string would hand back a key that silently
    merges every such item into one.
    """
    for kind, value in (("guid", guid), ("link", link), ("title", title)):
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            continue
        # NUL separators: no feed field can contain one, so no pair of
        # (source, value) can be re-split into a different pair and collide.
        material = f"{kind}\x00{source}\x00{text}".encode()
        return f"{kind}:{hashlib.sha256(material).hexdigest()}"
    raise ValueError("cannot derive an identity: guid, link and title are all empty")


def dedupe(headlines: Sequence[Headline]) -> list[Headline]:
    """Drop repeats by ``guid``, first occurrence wins, order preserved.

    First-wins rather than last-wins because the first sighting is the one
    whose timestamp and text are closest to publication; a later copy may carry
    an edited title or a re-stamped date, and preferring it would let a
    republished item present itself as fresh.

    Order is the input's order, which for a parsed feed is **document order,
    not chronological order** — real feeds are not sorted (the GitHub Atom feed
    in the tests is out of order by days). Sort by ``published_at`` if you need
    time order; this function will not do it silently.
    """
    seen: set[str] = set()
    unique: list[Headline] = []
    for headline in headlines:
        if headline.guid in seen:
            continue
        seen.add(headline.guid)
        unique.append(headline)
    return unique


def parse_feed(xml_text: str, *, source: str) -> list[Headline]:
    """Parse an RSS 2.0 or Atom document into headlines, in document order.

    Format is detected from the root element; see the module docstring.

    **Returns ``[]`` rather than raising, for every kind of bad document** —
    malformed XML, an HTML error page served with a 200, an empty body, a
    format we do not read. One feed going bad must not take down the poller
    that reads twenty others, and there is no partial answer to give: a
    document that failed to parse has no items, not fewer items.

    **Items are dropped individually** when they lack something a headline
    cannot exist without. Their siblings are kept — one malformed item must not
    cost a whole poll:

    - no usable title (missing, empty, or nothing left after stripping markup);
    - no timezone-aware timestamp (see :func:`parse_timestamp`);
    - no basis for an identity at all, which after the title check cannot
      happen, but is caught rather than assumed.

    Nothing is dropped for being **future-dated**. A publisher's clock skew and
    timezone bugs are ordinary, embargoed releases do go out early, and the
    items most worth seeing are the ones nearest to now — so silently hiding
    the ones that overshoot would blind the poller exactly where it matters.
    Clamping the stamp to "now" would be worse still: it fabricates a time and
    destroys the evidence that the feed is misconfigured. The timestamp is
    reported as parsed, in aware UTC, and a consumer whose freshness window
    cares about the future can see it and decide.

    ``xml_text`` must already be **decoded**. Bytes are refused (``[]``):
    reconciling an HTTP ``charset`` against the XML declaration is the
    fetcher's job, and guessing here would produce mojibake titles that parse
    fine and read as gibberish.

    Deduplication is deliberately *not* applied — compose with :func:`dedupe`.
    A caller polling several feeds wants one dedupe pass over the union, not
    one per feed that hides how much overlap there was.
    """
    if not isinstance(xml_text, str):
        return []
    # A byte-order mark or stray leading whitespace before `<?xml` is fatal to
    # expat, and the Federal Reserve's feed really does start with a BOM.
    text = xml_text.lstrip("﻿ \t\r\n")
    if not text:
        return []

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # Includes undefined-entity errors, so the classic entity-expansion
        # payloads land here. Size limits belong to the fetcher, not to us.
        return []

    if root.tag == "rss":
        return _parse_rss(root, source=source)
    if root.tag == _ATOM_FEED:
        return _parse_atom(root, source=source)
    # Anything else: RSS 1.0/RDF (which we do not read), an HTML error page
    # that happened to be well-formed, or a payload from somewhere else
    # entirely. Refuse rather than hunt for anything item-shaped inside it.
    return []


# --- internals -------------------------------------------------------------


def _element_text(element: ET.Element | None) -> str | None:
    """All text under an element, or ``None``.

    ``itertext`` rather than ``.text`` because a feed that emits *unescaped*
    markup inside ``<description>`` (invalid, and out there) parses as child
    elements, and ``.text`` would return only the fragment before the first
    tag — a headline truncated at its first bit of bold.
    """
    if element is None:
        return None
    joined = "".join(element.itertext())
    return joined or None


def _clean_text(raw: str | None) -> str | None:
    """Markup-stripped, entity-decoded, whitespace-collapsed text or ``None``.

    Two strip passes around one unescape pass — see the module docstring for
    why two and why not a loop. ``None`` when nothing survives, so "absent" and
    "present but empty" share one representation the caller cannot mistake for
    content.
    """
    if raw is None:
        return None
    text = _MARKUP_RE.sub(" ", raw)
    text = unescape(text)
    text = _MARKUP_RE.sub(" ", text)
    # `\s` covers the non-breaking spaces Google News pads its markup with.
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None


def _first_clean(element: ET.Element, *tags: str) -> str | None:
    """Cleaned text of the first of ``tags`` that yields anything."""
    for tag in tags:
        text = _clean_text(_element_text(element.find(tag)))
        if text is not None:
            return text
    return None


def _first_raw(element: ET.Element, *tags: str) -> str | None:
    """Uncleaned text of the first of ``tags`` that yields anything.

    Used for URLs and guids, which must not be entity-decoded twice or have
    anything tag-shaped removed. ``&amp;`` in a ``<link>`` is already resolved
    to ``&`` by the XML parser, and running :func:`_clean_text` over the result
    would be a second decode of a string that is not markup.
    """
    for tag in tags:
        text = _element_text(element.find(tag))
        if text is not None and text.strip():
            return text.strip()
    return None


def _first_timestamp(element: ET.Element, *tags: str) -> datetime | None:
    """First of ``tags`` that parses to an aware UTC datetime.

    First *parseable*, not first *present*: a feed carrying both a broken
    ``<pubDate>`` and a good ``<dc:date>`` should be read, not dropped. The
    fallback only ever adds items — an item with no parseable candidate at all
    is still refused.
    """
    for tag in tags:
        parsed = parse_timestamp(_first_raw(element, tag))
        if parsed is not None:
            return parsed
    return None


def _build(
    *,
    source: str,
    title: str | None,
    link: str | None,
    raw_guid: str | None,
    published_at: datetime | None,
    summary: str | None,
) -> Headline | None:
    """Assemble one headline, or refuse it. The only place refusals happen."""
    if not title:
        return None
    if published_at is None:
        return None
    try:
        guid = stable_guid(guid=raw_guid, link=link, title=title, source=source)
    except ValueError:
        # Unreachable while `title` is non-empty, but an identity we could not
        # compute must never become an item with a made-up one.
        return None
    return Headline(
        guid=guid,
        title=title,
        link=link,
        published_at=published_at,
        source=source,
        summary=summary,
    )


def _parse_rss(root: ET.Element, *, source: str) -> list[Headline]:
    """Items of every ``<channel>`` in an RSS 2.0 document, in order.

    Item children are un-namespaced; only ``<dc:date>`` and
    ``<content:encoded>`` are looked up qualified. ``iterfind`` over
    ``channel/item`` rather than a bare ``iter("item")`` so that an ``<item>``
    nested somewhere unexpected — inside a ``<content:encoded>`` block that a
    publisher forgot to escape, say — is not mistaken for a headline.
    """
    headlines: list[Headline] = []
    for item in root.iterfind("channel/item"):
        headline = _build(
            source=source,
            title=_first_clean(item, "title"),
            link=_first_raw(item, "link"),
            raw_guid=_first_raw(item, "guid"),
            # `<dc:date>` is ISO 8601 where `<pubDate>` is RFC 2822;
            # `parse_timestamp` reads either, so the fallback is free.
            published_at=_first_timestamp(item, "pubDate", _DC_DATE),
            summary=_first_clean(item, "description", _CONTENT_ENCODED),
        )
        if headline is not None:
            headlines.append(headline)
    return headlines


def _atom_link(entry: ET.Element) -> str | None:
    """The entry's article URL, from ``<link href=...>``.

    Atom puts the URL in an attribute and allows several links per entry. Only
    ``rel="alternate"`` and a missing ``rel`` (which RFC 4287 defines as
    ``alternate``) are accepted; see ``_DOCUMENT_RELS``.
    """
    for link in entry.iterfind(_ATOM_LINK):
        rel = link.get("rel")
        if rel is not None and rel not in _DOCUMENT_RELS:
            continue
        href = (link.get("href") or "").strip()
        if href:
            return href
    return None


def _parse_atom(root: ET.Element, *, source: str) -> list[Headline]:
    """Entries of an Atom feed, in document order.

    ``<published>`` is preferred over ``<updated>``, falling back when the feed
    omits it — which is common; GitHub's release feed carries only
    ``<updated>``. The preference is deliberate: ``<updated>`` moves when a
    publisher fixes a typo, and taking it would let a month-old entry present
    itself as breaking news every time it is touched. The cost is that a
    genuine revision is invisible to freshness checks, which is the direction
    to fail in — a poller that under-reacts to an edit is merely late, one that
    treats every edit as new news is permanently wrong about what just
    happened.
    """
    headlines: list[Headline] = []
    for entry in root.iterfind(_ATOM_ENTRY):
        headline = _build(
            source=source,
            title=_first_clean(entry, _ATOM_TITLE),
            link=_atom_link(entry),
            raw_guid=_first_raw(entry, _ATOM_ID),
            published_at=_first_timestamp(entry, _ATOM_PUBLISHED, _ATOM_UPDATED),
            summary=_first_clean(entry, _ATOM_SUMMARY, _ATOM_CONTENT),
        )
        if headline is not None:
            headlines.append(headline)
    return headlines

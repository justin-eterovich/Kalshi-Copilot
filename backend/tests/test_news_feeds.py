"""Tests for the news feed parser.

The fixtures below are trimmed copies of documents actually fetched from BBC
News, the Federal Reserve Board, the Federal Register, Google News and GitHub's
Atom endpoint, so the shapes — CDATA-wrapped dates, an ``<atom:link>`` inside
an RSS channel, an entry whose URL is only an attribute, double-escaped
entities — are observed rather than invented.

Refusals are tested harder than the happy path, because every failure this
module exists to prevent produces something that reads like ordinary news: a
naive datetime is a plausible datetime, an HTML error page is a plausible
empty feed, and an unstable identity is a plausible stream of breaking
headlines.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.news.feeds import Headline, dedupe, parse_feed, parse_timestamp, stable_guid

# --- captured documents ----------------------------------------------------

# feeds.bbci.co.uk/news/world/rss.xml, two items. Note the `xmlns:atom` on the
# <rss> element and the <atom:link rel="self"> inside <channel>: this is an RSS
# document that mentions Atom, which is why format detection reads the root tag.
BBC_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"
 xmlns:atom="http://www.w3.org/2005/Atom" version="2.0"
 xmlns:media="http://search.yahoo.com/mrss/">
    <channel>
        <title><![CDATA[BBC News]]></title>
        <link>https://www.bbc.co.uk/news/world</link>
        <lastBuildDate>Mon, 27 Jul 2026 15:00:56 GMT</lastBuildDate>
        <atom:link href="https://feeds.bbci.co.uk/news/world/rss.xml" rel="self"
         type="application/rss+xml"/>
        <item>
            <title><![CDATA[Devastating European wildfires in maps]]></title>
            <description><![CDATA[A blaze near Bordeaux has torn through more
            than 162 sq miles.]]></description>
            <link>https://www.bbc.co.uk/news/articles/cj638jx0l53o?at_medium=RSS&amp;at_campaign=rss</link>
            <guid isPermaLink="false">https://www.bbc.co.uk/news/articles/cj638jx0l53o#0</guid>
            <pubDate>Mon, 27 Jul 2026 14:21:32 GMT</pubDate>
            <media:thumbnail width="240" height="135" url="https://ichef.bbci.co.uk/a.jpg"/>
        </item>
        <item>
            <title><![CDATA[Oil price dives as US and Iran pause attacks]]></title>
            <description><![CDATA[The US says attacks have been halted.]]></description>
            <link>https://www.bbc.co.uk/news/articles/clyj834jn5lo</link>
            <guid isPermaLink="false">https://www.bbc.co.uk/news/articles/clyj834jn5lo#0</guid>
            <pubDate>Mon, 27 Jul 2026 14:22:24 GMT</pubDate>
        </item>
    </channel>
</rss>"""

# federalreserve.gov/feeds/press_all.xml. Starts with a UTF-8 BOM, wraps even
# <pubDate> in CDATA, and leaves trailing whitespace inside the element.
FED_RSS = """﻿<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0">
    <channel>
        <title>FRB: Press Release - All Releases</title>
        <item>
            <title>Agencies issue joint statement on sensitive information</title>
            <link><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260716a.htm]]></link>
            <guid><![CDATA[https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260716a.htm]]></guid>
            <description><![CDATA[Agencies issue joint statement.]]></description>
            <category>Banking and Consumer Regulatory Policy</category>
            <pubDate><![CDATA[Thu, 16 Jul 2026 18:00:00 GMT]]></pubDate>
        </item>
    </channel>
</rss>"""

# federalregister.gov RSS. `&amp;#39;` is a double escape: the XML parser
# yields `&#39;`, and only a second pass turns that into an apostrophe.
FEDERAL_REGISTER_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Federal Register Documents</title>
    <item>
      <title>Privacy Act of 1974; Matching Program</title>
      <link>https://www.federalregister.gov/documents/2026/07/27/2026-15146/privacy-act</link>
      <description>Provides the SPAAs with VA&amp;#39;s data.</description>
      <pubDate>Mon, 27 Jul 2026 04:00:00 GMT</pubDate>
      <guid>https://www.federalregister.gov/documents/2026/07/27/2026-15146/privacy-act</guid>
      <dc:creator>Department of Veterans Affairs</dc:creator>
    </item>
  </channel>
</rss>"""

# news.google.com/rss/search. The guid is an opaque token, unrelated to the
# link, and the description is an escaped HTML anchor padded with &nbsp;.
GOOGLE_NEWS_RSS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<title>"fed" - Google News</title>
<item><title>Federal Reserve is likely to hold interest rates steady - CNBC</title>
<link>https://news.google.com/rss/articles/CBMibEFVX3lxTE5kR3lXVW0?oc=5</link>
<guid isPermaLink="false">CBMibEFVX3lxTE5kR3lXVW0</guid>
<pubDate>Mon, 27 Jul 2026 12:15:01 GMT</pubDate>
<description>&lt;a href="https://news.google.com/rss/articles/CBMib?oc=5"&gt;Federal
Reserve is likely to hold interest rates steady&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font
color="#6f6f6f"&gt;CNBC&lt;/font&gt;</description>
</item></channel></rss>"""

# github.com/python/cpython/releases.atom. Entries carry <updated> only, the
# URL is an attribute on a <link rel="alternate"/>, and — worth noticing — the
# entries are NOT in chronological order.
GITHUB_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:media="http://search.yahoo.com/mrss/" xml:lang="en-US">
  <id>tag:github.com,2008:https://github.com/python/cpython/releases</id>
  <link type="application/atom+xml" rel="self"
        href="https://github.com/python/cpython/releases.atom"/>
  <title>Release notes from cpython</title>
  <updated>2026-07-18T07:57:43Z</updated>
  <entry>
    <id>tag:github.com,2008:Repository/81598961/v3.15.0b4</id>
    <updated>2026-07-18T07:57:43Z</updated>
    <link rel="alternate" type="text/html"
          href="https://github.com/python/cpython/releases/tag/v3.15.0b4"/>
    <title>v3.15.0b4</title>
    <content type="html">&lt;p&gt;Python 3.15.0b4&lt;/p&gt;</content>
    <author><name>hugovk</name></author>
    <media:thumbnail height="30" width="30" url="https://avatars.example/u.png"/>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/81598961/v3.14.6</id>
    <updated>2026-06-10T10:03:53Z</updated>
    <link rel="alternate" type="text/html"
          href="https://github.com/python/cpython/releases/tag/v3.14.6"/>
    <title>v3.14.6</title>
    <content type="html">&lt;p&gt;Python 3.14.6&lt;/p&gt;</content>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/81598961/v3.13.14</id>
    <updated>2026-06-10T12:24:04Z</updated>
    <link rel="alternate" type="text/html"
          href="https://github.com/python/cpython/releases/tag/v3.13.14"/>
    <title>v3.13.14</title>
  </entry>
</feed>"""

# What bls.gov and sec.gov actually return to an unrecognised user agent: an
# HTML page, with a 200 status, that nothing upstream will flag as an error.
ACCESS_DENIED_HTML = """<!DOCTYPE HTML>
<html lang="en-us"><head><title>Access Denied</title></head>
<body><h1>Bureau of Labor Statistics</h1><h2>Access Denied</h2>
<p>Automated retrieval programs are prohibited.</p></body></html>"""

# The same page as served in the wild: unclosed <meta>, so it is not even
# well-formed XML.
ACCESS_DENIED_HTML_MALFORMED = """<!DOCTYPE HTML>
<html lang="en-us"><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>Access Denied</title></head><body><h1>nope</h1></body></html>"""


def _headline(guid: str, *, when: str = "2026-07-27T12:00:00Z") -> Headline:
    """A minimal Headline for the dedupe tests."""
    parsed = parse_timestamp(when)
    assert parsed is not None
    return Headline(
        guid=guid,
        title="t",
        link=None,
        published_at=parsed,
        source="s",
        summary=None,
    )


class TestParseTimestampReadsBothWireFormats:
    def test_rfc_2822_pubdate_becomes_aware_utc(self) -> None:
        assert parse_timestamp("Mon, 27 Jul 2026 14:21:32 GMT") == datetime(
            2026, 7, 27, 14, 21, 32, tzinfo=UTC
        )

    def test_iso_8601_with_trailing_z_becomes_aware_utc(self) -> None:
        assert parse_timestamp("2026-07-18T07:57:43Z") == datetime(
            2026, 7, 18, 7, 57, 43, tzinfo=UTC
        )

    def test_iso_8601_with_explicit_offset_is_converted_not_relabelled(self) -> None:
        # The instant is preserved; only the representation changes.
        assert parse_timestamp("2026-07-18T07:57:43-05:00") == datetime(
            2026, 7, 18, 12, 57, 43, tzinfo=UTC
        )

    def test_rfc_2822_offset_zone_is_converted_to_utc(self) -> None:
        assert parse_timestamp("Mon, 27 Jul 2026 14:21:32 +0530") == datetime(
            2026, 7, 27, 8, 51, 32, tzinfo=UTC
        )

    def test_rfc_2822_named_us_zone_is_converted_to_utc(self) -> None:
        assert parse_timestamp("Mon, 27 Jul 2026 09:21:32 EST") == datetime(
            2026, 7, 27, 14, 21, 32, tzinfo=UTC
        )

    def test_fractional_seconds_survive(self) -> None:
        parsed = parse_timestamp("2026-07-18T07:57:43.123Z")
        assert parsed is not None
        assert parsed.microsecond == 123000

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        # The Federal Reserve feed pads its CDATA-wrapped pubDate.
        assert parse_timestamp("  Thu, 16 Jul 2026 18:00:00 GMT  ") == datetime(
            2026, 7, 16, 18, 0, 0, tzinfo=UTC
        )

    def test_every_result_carries_a_timezone(self) -> None:
        for value in (
            "Mon, 27 Jul 2026 14:21:32 GMT",
            "2026-07-18T07:57:43Z",
            "Mon, 27 Jul 2026 14:21:32 +0530",
        ):
            parsed = parse_timestamp(value)
            assert parsed is not None
            assert parsed.tzinfo is not None
            assert parsed.utcoffset() == timedelta(0)


class TestParseTimestampRefusesWhatItCannotPlace:
    def test_naive_rfc_2822_is_refused_not_assumed_utc(self) -> None:
        # Up to twelve hours of silent error, and the result would look normal.
        assert parse_timestamp("Mon, 27 Jul 2026 14:21:32") is None

    def test_minus_zero_zero_zero_zero_is_refused(self) -> None:
        # RFC 2822: `-0000` means "local time, zone withheld", not UTC.
        assert parse_timestamp("Mon, 27 Jul 2026 14:21:32 -0000") is None

    def test_unknown_alphabetic_zone_is_refused(self) -> None:
        assert parse_timestamp("Mon, 27 Jul 2026 14:21:32 XYZ") is None

    def test_naive_iso_is_refused(self) -> None:
        assert parse_timestamp("2026-07-18T07:57:43") is None

    def test_date_only_is_refused(self) -> None:
        # Midnight in an unstated zone is not a publication time.
        assert parse_timestamp("2026-07-18") is None

    def test_impossible_calendar_values_are_refused(self) -> None:
        assert parse_timestamp("Mon, 32 Jul 2026 99:00:00 GMT") is None

    def test_garbage_none_and_blank_are_refused(self) -> None:
        assert parse_timestamp("garbage") is None
        assert parse_timestamp(None) is None
        assert parse_timestamp("") is None
        assert parse_timestamp("   ") is None


class TestStableGuidPrefersTheStrongestIdentity:
    def test_feed_guid_wins_over_link_and_title(self) -> None:
        with_guid = stable_guid(guid="g1", link="https://x/a", title="T", source="s")
        guid_only = stable_guid(guid="g1", link=None, title="other", source="s")
        assert with_guid.startswith("guid:")
        # Neither link nor title contributes when a guid is present, so a
        # retitled or re-linked republication keeps the same identity.
        assert with_guid == guid_only

    def test_link_is_used_when_the_guid_is_missing_or_blank(self) -> None:
        from_missing = stable_guid(guid=None, link="https://x/a", title="T", source="s")
        from_blank = stable_guid(guid="   ", link="https://x/a", title="T", source="s")
        assert from_missing.startswith("link:")
        assert from_missing == from_blank

    def test_title_hash_is_the_last_resort(self) -> None:
        key = stable_guid(guid=None, link=None, title="Fed holds rates", source="s")
        assert key.startswith("title:")

    def test_the_three_bases_never_collide_with_each_other(self) -> None:
        same = "https://x/a"
        as_guid = stable_guid(guid=same, link=None, title="T", source="s")
        as_link = stable_guid(guid=None, link=same, title="T", source="s")
        as_title = stable_guid(guid=None, link=None, title=same, source="s")
        assert len({as_guid, as_link, as_title}) == 3

    def test_identity_is_scoped_to_the_source(self) -> None:
        # Small publishers really do emit <guid>1</guid>; unscoped keys would
        # merge unrelated items from different feeds.
        assert stable_guid(guid="1", link=None, title="A", source="a") != stable_guid(
            guid="1", link=None, title="B", source="b"
        )

    def test_the_same_input_always_gives_the_same_key(self) -> None:
        args = {"guid": "g", "link": "l", "title": "t", "source": "s"}
        assert stable_guid(**args) == stable_guid(**args)  # type: ignore[arg-type]

    def test_surrounding_whitespace_does_not_change_identity(self) -> None:
        assert stable_guid(
            guid=" g1\n", link=None, title="T", source="s"
        ) == stable_guid(guid="g1", link=None, title="T", source="s")

    def test_links_are_not_normalised_so_tracking_params_split_identity(self) -> None:
        # Documented limitation, asserted so it cannot change by accident:
        # guessing which query parameters are identity merges real articles.
        a = stable_guid(guid=None, link="https://x/a", title="T", source="s")
        b = stable_guid(guid=None, link="https://x/a?utm=1", title="T", source="s")
        assert a != b


class TestStableGuidRefusesToInventIdentity:
    def test_all_bases_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="identity"):
            stable_guid(guid=None, link=None, title="", source="s")

    def test_whitespace_only_bases_raise(self) -> None:
        with pytest.raises(ValueError, match="identity"):
            stable_guid(guid="  ", link="\t", title="\n", source="s")


class TestParseFeedReadsRealRss:
    def test_bbc_items_are_parsed_in_document_order(self) -> None:
        headlines = parse_feed(BBC_RSS, source="bbc-world")
        assert [h.title for h in headlines] == [
            "Devastating European wildfires in maps",
            "Oil price dives as US and Iran pause attacks",
        ]

    def test_cdata_titles_and_summaries_arrive_as_plain_text(self) -> None:
        first = parse_feed(BBC_RSS, source="bbc-world")[0]
        assert first.summary == (
            "A blaze near Bordeaux has torn through more than 162 sq miles."
        )

    def test_entity_escaped_link_is_decoded_exactly_once(self) -> None:
        first = parse_feed(BBC_RSS, source="bbc-world")[0]
        assert first.link == (
            "https://www.bbc.co.uk/news/articles/cj638jx0l53o"
            "?at_medium=RSS&at_campaign=rss"
        )

    def test_pubdate_becomes_aware_utc(self) -> None:
        first = parse_feed(BBC_RSS, source="bbc-world")[0]
        assert first.published_at == datetime(2026, 7, 27, 14, 21, 32, tzinfo=UTC)

    def test_source_is_carried_through_verbatim(self) -> None:
        headlines = parse_feed(BBC_RSS, source="bbc-world")
        assert all(h.source == "bbc-world" for h in headlines)

    def test_a_leading_byte_order_mark_does_not_break_parsing(self) -> None:
        # The Federal Reserve feed really does start with one.
        headlines = parse_feed(FED_RSS, source="frb")
        assert len(headlines) == 1
        assert headlines[0].published_at == datetime(2026, 7, 16, 18, tzinfo=UTC)

    def test_a_cdata_wrapped_link_is_read(self) -> None:
        link = parse_feed(FED_RSS, source="frb")[0].link
        assert link is not None and link.endswith("bcreg20260716a.htm")

    def test_google_news_opaque_guid_is_preferred_over_the_redirect_link(self) -> None:
        first = parse_feed(GOOGLE_NEWS_RSS, source="gnews")[0]
        assert first.guid.startswith("guid:")
        assert first.guid == stable_guid(
            guid="CBMibEFVX3lxTE5kR3lXVW0", link=None, title="x", source="gnews"
        )


class TestParseFeedReadsRealAtom:
    def test_atom_is_detected_from_the_root_element(self) -> None:
        assert len(parse_feed(GITHUB_ATOM, source="cpython")) == 3

    def test_namespaced_titles_and_ids_are_read(self) -> None:
        first = parse_feed(GITHUB_ATOM, source="cpython")[0]
        assert first.title == "v3.15.0b4"
        assert first.guid == stable_guid(
            guid="tag:github.com,2008:Repository/81598961/v3.15.0b4",
            link=None,
            title="x",
            source="cpython",
        )

    def test_the_link_comes_from_the_href_attribute(self) -> None:
        first = parse_feed(GITHUB_ATOM, source="cpython")[0]
        assert first.link == "https://github.com/python/cpython/releases/tag/v3.15.0b4"

    def test_updated_is_used_when_the_entry_has_no_published(self) -> None:
        first = parse_feed(GITHUB_ATOM, source="cpython")[0]
        assert first.published_at == datetime(2026, 7, 18, 7, 57, 43, tzinfo=UTC)

    def test_published_wins_over_updated_when_both_are_present(self) -> None:
        # A revision must not re-stamp an old entry as breaking news.
        doc = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>e1</id><title>Old news</title>
          <published>2026-01-01T00:00:00Z</published>
          <updated>2026-07-27T00:00:00Z</updated>
        </entry></feed>"""
        assert parse_feed(doc, source="a")[0].published_at == datetime(
            2026, 1, 1, tzinfo=UTC
        )

    def test_escaped_html_content_becomes_the_plain_text_summary(self) -> None:
        first = parse_feed(GITHUB_ATOM, source="cpython")[0]
        assert first.summary == "Python 3.15.0b4"

    def test_an_entry_with_no_content_has_no_summary(self) -> None:
        third = parse_feed(GITHUB_ATOM, source="cpython")[2]
        assert third.summary is None

    def test_document_order_is_preserved_even_when_it_is_not_chronological(self) -> None:
        # GitHub's feed is genuinely out of order; callers who want time order
        # must sort, and this asserts the parser does not do it for them.
        stamps = [h.published_at for h in parse_feed(GITHUB_ATOM, source="cpython")]
        assert stamps != sorted(stamps, reverse=True)

    def test_a_self_link_is_never_mistaken_for_the_article(self) -> None:
        doc = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>e1</id><title>T</title><updated>2026-07-27T00:00:00Z</updated>
          <link rel="self" href="https://x/feed.atom"/>
          <link rel="enclosure" href="https://x/audio.mp3"/>
        </entry></feed>"""
        assert parse_feed(doc, source="a")[0].link is None

    def test_a_link_with_no_rel_is_treated_as_the_article(self) -> None:
        doc = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <id>e1</id><title>T</title><updated>2026-07-27T00:00:00Z</updated>
          <link href="https://x/article"/>
        </entry></feed>"""
        assert parse_feed(doc, source="a")[0].link == "https://x/article"


class TestParseFeedRefusesBadDocuments:
    def test_malformed_xml_returns_empty_rather_than_raising(self) -> None:
        assert parse_feed("<rss><channel><item>", source="s") == []

    def test_the_malformed_html_error_page_returns_empty(self) -> None:
        assert parse_feed(ACCESS_DENIED_HTML_MALFORMED, source="bls") == []

    def test_a_well_formed_html_error_page_returns_empty(self) -> None:
        # 200 OK, parses as XML, contains no feed. The dangerous case: nothing
        # upstream flags it, so the root tag has to.
        assert parse_feed(ACCESS_DENIED_HTML, source="bls") == []

    def test_an_rdf_rss_1_0_document_is_refused_rather_than_half_read(self) -> None:
        doc = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns="http://purl.org/rss/1.0/"><item><title>T</title>
          <link>https://x/a</link></item></rdf:RDF>"""
        assert parse_feed(doc, source="s") == []

    def test_empty_blank_and_non_string_input_return_empty(self) -> None:
        assert parse_feed("", source="s") == []
        assert parse_feed("   \n ", source="s") == []
        assert parse_feed(b"<rss/>", source="s") == []  # type: ignore[arg-type]

    def test_a_feed_with_no_items_returns_empty(self) -> None:
        assert parse_feed("<rss version='2.0'><channel/></rss>", source="s") == []

    def test_an_undefined_entity_does_not_escape_as_an_exception(self) -> None:
        doc = "<rss version='2.0'><channel><item>&boom;</item></channel></rss>"
        assert parse_feed(doc, source="s") == []


class TestParseFeedRefusesUnusableItems:
    def _one_bad_of_three(self, bad_item: str) -> list[str]:
        doc = f"""<rss version="2.0"><channel>
          <item><title>Good one</title><guid>g1</guid>
            <pubDate>Mon, 27 Jul 2026 14:00:00 GMT</pubDate></item>
          {bad_item}
          <item><title>Good two</title><guid>g3</guid>
            <pubDate>Mon, 27 Jul 2026 16:00:00 GMT</pubDate></item>
        </channel></rss>"""
        return [h.title for h in parse_feed(doc, source="s")]

    def test_an_item_with_no_date_is_dropped_and_its_siblings_kept(self) -> None:
        titles = self._one_bad_of_three(
            "<item><title>Undated</title><guid>g2</guid></item>"
        )
        assert titles == ["Good one", "Good two"]

    def test_an_item_with_a_naive_date_is_dropped(self) -> None:
        # Not silently read as UTC — the whole point of parse_timestamp.
        titles = self._one_bad_of_three(
            "<item><title>Naive</title><guid>g2</guid>"
            "<pubDate>Mon, 27 Jul 2026 15:00:00</pubDate></item>"
        )
        assert titles == ["Good one", "Good two"]

    def test_an_item_with_an_unparseable_date_is_dropped(self) -> None:
        titles = self._one_bad_of_three(
            "<item><title>Junk date</title><guid>g2</guid>"
            "<pubDate>whenever</pubDate></item>"
        )
        assert titles == ["Good one", "Good two"]

    def test_an_item_with_no_title_is_dropped(self) -> None:
        titles = self._one_bad_of_three(
            "<item><guid>g2</guid><link>https://x/a</link>"
            "<pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate></item>"
        )
        assert titles == ["Good one", "Good two"]

    def test_a_title_that_is_only_markup_is_dropped(self) -> None:
        # Nothing survives stripping, so there is no headline to show.
        titles = self._one_bad_of_three(
            "<item><title>&lt;b&gt;&lt;/b&gt;</title><guid>g2</guid>"
            "<pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate></item>"
        )
        assert titles == ["Good one", "Good two"]

    def test_a_broken_pubdate_falls_back_to_dc_date_rather_than_dropping(self) -> None:
        doc = """<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
          <channel><item><title>T</title><guid>g</guid>
            <pubDate>not a date</pubDate>
            <dc:date>2026-07-27T15:00:00Z</dc:date>
          </item></channel></rss>"""
        headlines = parse_feed(doc, source="s")
        assert headlines[0].published_at == datetime(2026, 7, 27, 15, tzinfo=UTC)

    def test_no_headline_ever_carries_a_naive_datetime(self) -> None:
        for doc, source in (
            (BBC_RSS, "bbc"),
            (FED_RSS, "frb"),
            (GITHUB_ATOM, "cpython"),
            (GOOGLE_NEWS_RSS, "gnews"),
            (FEDERAL_REGISTER_RSS, "fr"),
        ):
            for headline in parse_feed(doc, source=source):
                assert headline.published_at.tzinfo is not None
                assert headline.published_at.utcoffset() == timedelta(0)


class TestParseFeedHandlesNamespacesExplicitly:
    def test_an_rss_channel_declaring_atom_is_still_read_as_rss(self) -> None:
        # BBC declares xmlns:atom and carries <atom:link rel="self">. A parser
        # that detected Atom by namespace presence would return nothing here.
        assert len(parse_feed(BBC_RSS, source="bbc")) == 2

    def test_the_channels_atom_self_link_never_becomes_an_article_link(self) -> None:
        links = [h.link for h in parse_feed(BBC_RSS, source="bbc")]
        assert all(link is not None and "rss.xml" not in link for link in links)

    def test_content_encoded_is_used_when_description_is_absent(self) -> None:
        doc = """<rss version="2.0"
          xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel><item><title>T</title><guid>g</guid>
            <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate>
            <content:encoded><![CDATA[<p>Full body text.</p>]]></content:encoded>
          </item></channel></rss>"""
        assert parse_feed(doc, source="s")[0].summary == "Full body text."

    def test_an_item_nested_outside_a_channel_is_not_read(self) -> None:
        doc = """<rss version="2.0"><item><title>Stray</title><guid>g</guid>
          <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate></item></rss>"""
        assert parse_feed(doc, source="s") == []


class TestParseFeedProducesPlainText:
    def test_escaped_html_markup_is_stripped_from_a_summary(self) -> None:
        summary = parse_feed(GOOGLE_NEWS_RSS, source="gnews")[0].summary
        assert summary is not None
        assert "<" not in summary and ">" not in summary
        assert "href" not in summary

    def test_non_breaking_spaces_collapse(self) -> None:
        summary = parse_feed(GOOGLE_NEWS_RSS, source="gnews")[0].summary
        assert summary is not None
        assert "nbsp" not in summary
        assert "\xa0" not in summary
        assert "  " not in summary

    def test_double_escaped_entities_are_resolved(self) -> None:
        # Federal Register emits `&amp;#39;` where it means an apostrophe.
        summary = parse_feed(FEDERAL_REGISTER_RSS, source="fr")[0].summary
        assert summary == "Provides the SPAAs with VA's data."

    def test_prose_angle_brackets_are_not_eaten_as_markup(self) -> None:
        # `<[^>]*>` would delete "< 4 and 5 >" and silently shorten the title.
        doc = """<rss version="2.0"><channel><item>
          <title>CPI comes in &lt; 4 and 5 &gt; forecast</title><guid>g</guid>
          <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate>
        </item></channel></rss>"""
        assert parse_feed(doc, source="s")[0].title == "CPI comes in < 4 and 5 > forecast"

    def test_unescaped_child_markup_does_not_truncate_a_title(self) -> None:
        # Invalid but real: markup that the XML parser turns into children.
        doc = """<rss version="2.0"><channel><item>
          <title>Fed <b>holds</b> rates</title><guid>g</guid>
          <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate>
        </item></channel></rss>"""
        assert parse_feed(doc, source="s")[0].title == "Fed holds rates"

    def test_a_summary_that_is_only_markup_becomes_none_not_empty_string(self) -> None:
        doc = """<rss version="2.0"><channel><item>
          <title>T</title><guid>g</guid>
          <description>&lt;div&gt;&lt;/div&gt;</description>
          <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate>
        </item></channel></rss>"""
        assert parse_feed(doc, source="s")[0].summary is None


class TestParseFeedKeepsSuspiciousTimestamps:
    def test_a_future_dated_item_is_kept_not_dropped(self) -> None:
        # Publisher clock skew is ordinary and the items nearest to now are the
        # ones worth seeing; dropping them would blind the poller where it
        # matters most.
        doc = """<rss version="2.0"><channel><item>
          <title>Embargo broke early</title><guid>g</guid>
          <pubDate>Tue, 01 Jan 2999 00:00:00 GMT</pubDate>
        </item></channel></rss>"""
        headlines = parse_feed(doc, source="s")
        assert len(headlines) == 1
        assert headlines[0].published_at.year == 2999

    def test_a_future_timestamp_is_reported_as_parsed_never_clamped(self) -> None:
        # Clamping to "now" would fabricate a time and hide the misconfigured
        # publisher that produced it.
        assert parse_timestamp("Tue, 01 Jan 2999 00:00:00 GMT") == datetime(
            2999, 1, 1, tzinfo=UTC
        )


class TestDedupeKeepsTheFirstSighting:
    def test_repeats_are_dropped_and_order_is_preserved(self) -> None:
        items = [_headline("a"), _headline("b"), _headline("a"), _headline("c")]
        assert [h.guid for h in dedupe(items)] == ["a", "b", "c"]

    def test_the_first_copy_is_the_one_kept(self) -> None:
        first = _headline("a", when="2026-07-27T10:00:00Z")
        restamped = _headline("a", when="2026-07-27T18:00:00Z")
        # A republished item must not present itself as fresh.
        assert dedupe([first, restamped])[0].published_at == first.published_at

    def test_an_empty_sequence_gives_an_empty_list(self) -> None:
        assert dedupe([]) == []

    def test_distinct_items_are_never_merged(self) -> None:
        items = [_headline("a"), _headline("b")]
        assert len(dedupe(items)) == 2

    def test_the_same_story_from_two_sources_survives_as_two(self) -> None:
        # Scoped identity, by design: cross-source clustering is a relevance
        # question this module refuses to answer.
        doc = """<rss version="2.0"><channel><item>
          <title>Fed holds rates</title><link>https://x/a</link>
          <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate>
        </item></channel></rss>"""
        both = parse_feed(doc, source="feed-a") + parse_feed(doc, source="feed-b")
        assert len(dedupe(both)) == 2

    def test_a_feed_parsed_twice_dedupes_to_one_copy_of_each_item(self) -> None:
        twice = parse_feed(BBC_RSS, source="bbc") + parse_feed(BBC_RSS, source="bbc")
        assert len(twice) == 4
        assert len(dedupe(twice)) == 2

    def test_a_retitled_item_keeps_its_identity_when_the_guid_is_stable(self) -> None:
        original = """<rss version="2.0"><channel><item>
          <title>Fed expected to hold</title><guid>g1</guid>
          <pubDate>Mon, 27 Jul 2026 15:00:00 GMT</pubDate></item></channel></rss>"""
        revised = original.replace("Fed expected to hold", "Fed holds rates steady")
        both = parse_feed(original, source="s") + parse_feed(revised, source="s")
        assert len(dedupe(both)) == 1

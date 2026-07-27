"""Tests for news-to-market relevance matching.

This module exists to *refuse*, so most of what follows tests refusals: the
headline about football, the headline made entirely of numbers, the empty
market set, the single shared rare word, and — the one the module was written
for — the plausible-but-wrong match between "Fed holds rates steady" and a
market about the Federal Reserve *building*.

The happy path gets one class. The bar is deliberately harder to clear than to
miss, so a change that makes these refusals stop refusing is a regression even
if every positive test still passes.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from app.news.relevance import (
    MIN_SHARED_TERMS,
    STOPWORDS,
    MarketRef,
    Match,
    match_headline,
    normalise,
    tokens,
)

# Real titles from the live catalog, markdown and odd capitalisation included,
# plus one deliberate decoy: a market about the Federal Reserve *building*.
# The decoy is the whole reason this module is not a bag-of-words matcher.
REAL_TITLES: list[tuple[str, str, str]] = [
    ("KXCPI-26JUL", "KXCPI", "Will CPI rise more than 0.3% in July 2026?"),
    (
        "KXCPIYOY-26AUG",
        "KXCPIYOY",
        "Will the rate of CPI inflation be above 5.0% for the year ending "
        "August 2026?",
    ),
    (
        "KXFEDBOUND-27APR",
        "KXFEDBOUND",
        "Will the upper bound of the federal funds rate be above 3.25% after "
        "the April 2027 meeting?",
    ),
    (
        "KXFED-26JUL",
        "KXFED",
        "Will the Federal Reserve Hike rates by 0bps at their July 2026 meeting?",
    ),
    ("KXGDP-26Q2", "KXGDP", "Will **real GDP** increase by more than 4.0% in Q2 2026?"),
    ("KXPAYROLL-26AUG", "KXPAYROLL", "Will above 40000 jobs be added in August 2026?"),
    (
        "KXU3-26AUG",
        "KXU3",
        "Will the unemployment rate (U-3) be above 5.0% in August?",
    ),
    ("KXHOTYEAR-26", "KXHOTYEAR", "Will 2026 be the hottest year on record?"),
    (
        "KXLOWES-26Q2",
        "KXLOWES",
        "Will Lowe's Companies Inc. report Above 236 million customer "
        "transactions in Q2 2026?",
    ),
    (
        "KXFEDBLDG-26",
        "KXFEDBLDG",
        "Will the Federal Reserve building renovation cost exceed $3.0 billion "
        "in 2026?",
    ),
]

CATALOG: list[MarketRef] = [MarketRef(t, s, title) for t, s, title in REAL_TITLES]

#: The sort of bar an operator would actually run in front of an LLM budget.
BAR = 0.25


def tickers(matches: list[Match]) -> list[str]:
    return [m.ticker for m in matches]


class TestNormaliseStripsTheCorpusMess:
    def test_markdown_emphasis_is_removed(self) -> None:
        # Titles really do arrive as "**real GDP**".
        assert normalise("Will **real GDP** increase?") == "will real gdp increase"

    def test_underscore_emphasis_is_removed(self) -> None:
        # \W does not match underscore, so it needs its own handling.
        assert normalise("__real__ GDP") == "real gdp"

    def test_case_and_punctuation_and_whitespace_collapse(self) -> None:
        assert normalise("  Lowe's   Companies, Inc.\n") == "lowe s companies inc"

    def test_digits_survive_normalisation(self) -> None:
        # tokens() drops them; normalise() reports what was there.
        assert normalise("above 3.25% in 2026") == "above 3 25 in 2026"

    def test_accents_are_preserved(self) -> None:
        # NFKC compatibility folding only; this is not an ASCII transliterator,
        # and silently mangling non-Latin text would lose whole markets.
        assert normalise("Café Élysée") == "café élysée"

    def test_empty_and_symbol_only_text_normalise_to_empty(self) -> None:
        assert normalise("") == ""
        assert normalise("   ") == ""
        assert normalise("*** — %$#") == ""


class TestTokensDropWhatCannotBeEvidence:
    def test_stopwords_are_dropped(self) -> None:
        assert tokens("Will the rate be above 5.0% in August?") == [
            "rate",
            "august",
        ]

    def test_a_title_of_pure_scaffolding_yields_nothing(self) -> None:
        assert tokens("Will it be above the most?") == []

    def test_bare_numerals_and_years_are_dropped(self) -> None:
        # The trap: "2026" and "3.25%" appear in dozens of unrelated titles,
        # and a match built on them looks specific while meaning nothing.
        assert tokens("above 3.25% in 2026") == []
        assert tokens("40000 jobs by the 1st") == ["jobs"]

    def test_mixed_alphanumerics_survive(self) -> None:
        # "0bps" is a word, not a quantity this module compares.
        assert tokens("Hike rates by 0bps") == ["hike", "rates", "0bps"]

    def test_single_characters_are_dropped(self) -> None:
        # The debris of "U-3" and "Lowe's".
        assert tokens("unemployment rate (U-3)") == ["unemployment", "rate"]

    def test_duplicates_are_preserved_by_the_tokeniser(self) -> None:
        # match_headline takes sets, so frequency cannot influence a score;
        # the tokeniser itself stays general.
        assert tokens("rate rate rate") == ["rate", "rate", "rate"]

    def test_no_stemming_is_applied(self) -> None:
        # Stated as a limitation in the docstring, asserted here so nobody
        # "fixes" it without reading why. "rates" will not match "rate".
        assert tokens("rates") == ["rates"]
        assert tokens("rate") == ["rate"]

    def test_stopword_list_contains_no_subject_matter(self) -> None:
        # A word a story could be *about* must never be a stopword; idf is the
        # tool for common-but-meaningful words.
        for word in ("rate", "record", "report", "jobs", "july", "2026", "fed"):
            assert word not in STOPWORDS


class TestPlausibleButWrongMatchesAreRejected:
    """The behaviour the module exists for.

    A bag-of-words matcher connects "Fed holds rates steady" to the Federal
    Reserve *building* renovation market, because both are full of Fed-shaped
    words. It must not.
    """

    def test_fed_rate_headline_does_not_reach_the_fed_building_market(self) -> None:
        matches = match_headline(
            "Fed holds rates steady at July meeting", CATALOG, min_score=BAR
        )
        assert "KXFEDBLDG-26" not in tickers(matches)
        assert tickers(matches) == ["KXFED-26JUL"]

    def test_fed_building_headline_does_not_reach_the_rate_markets(self) -> None:
        # And symmetrically: a story about the renovation is not a rates story,
        # even though it shares "federal" and "reserve" with three of them.
        matches = match_headline(
            "Renovation of the Federal Reserve building runs over budget",
            CATALOG,
            min_score=BAR,
        )
        assert tickers(matches) == ["KXFEDBLDG-26"]

    def test_shared_numbers_alone_never_match(self) -> None:
        # "3.25%" and "2026" are in the federal funds title verbatim. Nothing
        # else is shared, so nothing matches.
        assert match_headline("Bitcoin fell 3.25% in 2026", CATALOG, min_score=0.01) == []

    def test_off_topic_news_matches_nothing(self) -> None:
        # The expected outcome for the overwhelming majority of world news.
        assert (
            match_headline(
                "Manchester United sign a new striker", CATALOG, min_score=0.01
            )
            == []
        )


class TestOneSharedTermIsNotEnough:
    """The evidence bar, tested at the boundary from both sides."""

    markets = [
        MarketRef(
            "KXCHAIR-26",
            "KXCHAIR",
            "Will Jerome Powell remain Fed Chair through December 2026?",
        ),
        MarketRef("KXGDP-26Q2", "KXGDP", "Will **real GDP** increase by more than 4%?"),
        MarketRef(
            "KXHOTYEAR-26", "KXHOTYEAR", "Will 2026 be the hottest year on record?"
        ),
    ]

    def test_the_bar_is_two_distinct_terms(self) -> None:
        assert MIN_SHARED_TERMS == 2

    def test_one_shared_rare_term_is_refused_at_any_score(self) -> None:
        # "Powell" is the rarest token in this corpus and it is genuinely
        # shared — and it is still a coincidence, because the story is about a
        # museum. min_score is set as low as the API permits to show the term
        # count is doing the work, not the threshold.
        assert (
            match_headline(
                "Powell spotted at a Tokyo museum", self.markets, min_score=0.01
            )
            == []
        )

    def test_a_second_shared_term_flips_it(self) -> None:
        matches = match_headline(
            "Powell to remain in post, sources say", self.markets, min_score=0.01
        )
        assert tickers(matches) == ["KXCHAIR-26"]
        assert set(matches[0].matched_terms) == {"powell", "remain"}

    def test_repeating_a_shared_term_does_not_substitute_for_a_second(self) -> None:
        # Term frequency must never clear the bar: three mentions of one word
        # is still one coincidence.
        assert (
            match_headline(
                "Powell, Powell, Powell", self.markets, min_score=0.01
            )
            == []
        )


class TestMatchesThatShouldSucceed:
    def test_a_strong_headline_finds_its_market(self) -> None:
        matches = match_headline(
            "Powell says the Federal Reserve will hike rates at the July meeting",
            CATALOG,
            min_score=BAR,
        )
        assert matches[0].ticker == "KXFED-26JUL"
        assert "hike" in matches[0].matched_terms

    def test_unemployment_headline_finds_the_u3_market(self) -> None:
        matches = match_headline(
            "August unemployment rate ticks up", CATALOG, min_score=BAR
        )
        assert matches[0].ticker == "KXU3-26AUG"

    def test_matched_terms_are_rarest_first_and_are_the_audit_trail(self) -> None:
        matches = match_headline(
            "Hottest year on record confirmed by scientists", CATALOG, min_score=BAR
        )
        assert matches[0].ticker == "KXHOTYEAR-26"
        # "hottest" appears in one title, "record" in one, "year" in two.
        assert matches[0].matched_terms[-1] == "year"
        assert set(matches[0].matched_terms) <= {"hottest", "record", "year"}

    def test_matched_terms_never_contain_stopwords_or_numerals(self) -> None:
        matches = match_headline(
            "Powell says the Federal Reserve will hike rates at the July 2026 meeting",
            CATALOG,
            min_score=0.01,
        )
        assert matches
        for match in matches:
            for term in match.matched_terms:
                assert term not in STOPWORDS
                assert not term.isdigit()

    def test_results_are_ordered_by_score_then_ticker(self) -> None:
        matches = match_headline(
            "Powell says the Federal Reserve will hike rates at the July meeting",
            CATALOG,
            min_score=0.01,
        )
        scores = [m.score for m in matches]
        assert scores == sorted(scores, reverse=True)

    def test_max_matches_caps_the_list(self) -> None:
        matches = match_headline(
            "Powell says the Federal Reserve will hike rates at the July meeting",
            CATALOG,
            min_score=0.01,
            max_matches=1,
        )
        assert len(matches) == 1
        assert matches[0].ticker == "KXFED-26JUL"


class TestDegenerateInputs:
    def test_empty_headline(self) -> None:
        assert match_headline("", CATALOG, min_score=BAR) == []

    def test_whitespace_and_punctuation_only_headline(self) -> None:
        assert match_headline("   ***   ", CATALOG, min_score=BAR) == []

    def test_headline_of_only_stopwords_and_numerals(self) -> None:
        # "Above 3 in 2026" tokenises to nothing, so there is nothing to match
        # on — as opposed to matching every title that contains "above".
        assert match_headline("Above 3 in 2026", CATALOG, min_score=BAR) == []

    def test_empty_market_set(self) -> None:
        assert match_headline("CPI inflation cooled in July", [], min_score=BAR) == []

    def test_market_set_with_no_usable_titles(self) -> None:
        junk = [MarketRef("A", None, "2026"), MarketRef("B", None, "*** ###")]
        assert match_headline("CPI inflation cooled in July", junk, min_score=BAR) == []

    def test_untokenisable_titles_do_not_dilute_the_corpus(self) -> None:
        # A title that can never match must not sit in the idf denominator
        # pretending the corpus is larger than it is.
        junk = [MarketRef(f"J{i}", None, "2026 2027") for i in range(20)]
        headline = "CPI inflation cooled last month"
        assert (
            match_headline(headline, CATALOG, min_score=0.01)[0].score
            == match_headline(headline, CATALOG + junk, min_score=0.01)[0].score
        )

    def test_max_matches_zero_or_negative_returns_empty(self) -> None:
        # A coherent request with an empty answer, not a caller bug.
        assert (
            match_headline("CPI inflation", CATALOG, min_score=0.01, max_matches=0) == []
        )
        assert (
            match_headline("CPI inflation", CATALOG, min_score=0.01, max_matches=-3)
            == []
        )


class TestMinScoreIsValidated:
    """A filter broken in the safe direction still looks like "no news today"."""

    def test_a_percentage_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cosine"):
            match_headline("CPI inflation cooled", CATALOG, min_score=40)

    def test_zero_is_rejected(self) -> None:
        # Zero would disable the score filter entirely and leave only the term
        # count, which is not what any caller passing 0 believes it is doing.
        with pytest.raises(ValueError):
            match_headline("CPI inflation cooled", CATALOG, min_score=0.0)

    def test_negative_and_above_one_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            match_headline("CPI inflation cooled", CATALOG, min_score=-0.2)
        with pytest.raises(ValueError):
            match_headline("CPI inflation cooled", CATALOG, min_score=1.5)

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            match_headline("CPI inflation cooled", CATALOG, min_score=float("nan"))

    def test_one_is_accepted_as_the_upper_bound(self) -> None:
        # An exact-cosine bar is coherent, if useless. It must not raise.
        assert match_headline("CPI inflation cooled", CATALOG, min_score=1.0) == []


class TestScoringIsRelativeToTheSuppliedSet:
    """The documented limitation, asserted so it cannot be forgotten."""

    def test_a_term_gets_cheaper_as_the_set_fills_with_it(self) -> None:
        # Identical headline, identical market, different neighbours: "CPI"
        # is distinctive against a mixed catalog and nearly worthless once the
        # set is mostly CPI markets. Scores are not comparable across calls.
        headline = "CPI inflation cooled last month"
        crowd = [
            MarketRef(f"KXCPIX-{i}", "KXCPIX", f"Will CPI inflation be above {i}.0%?")
            for i in range(1, 13)
        ]
        sparse = match_headline(headline, CATALOG, min_score=0.01)
        dense = match_headline(headline, CATALOG + crowd, min_score=0.01)
        assert sparse and dense
        assert max(m.score for m in sparse) > max(m.score for m in dense)

    def test_a_market_set_of_one_measures_nothing(self) -> None:
        # Every term is in every title, so nothing distinguishes anything and
        # the module refuses rather than matching on boilerplate.
        target = MarketRef("KXCPI", "KXCPI", "Will CPI inflation be above 5.0%?")
        assert match_headline("CPI inflation cooled", [target], min_score=0.01) == []

    def test_a_single_series_set_refuses_on_the_words_that_define_it(self) -> None:
        # Same trap one step out: pre-filtering to one series makes that
        # series' own vocabulary universal, and therefore not evidence.
        only_cpi = [
            MarketRef(f"KXCPI-{i}", "KXCPI", f"Will CPI inflation be above {i}.0%?")
            for i in range(1, 6)
        ]
        assert match_headline("CPI inflation cooled", only_cpi, min_score=0.01) == []

    def test_scores_stay_within_the_documented_range(self) -> None:
        for headline in (
            "Powell says the Federal Reserve will hike rates at the July meeting",
            "Will CPI rise more than 0.3% in July 2026?",  # a title as a headline
            "August unemployment rate ticks up",
        ):
            for match in match_headline(headline, CATALOG, min_score=0.01):
                assert 0.0 <= match.score <= 1.0


class TestNoEdgeNoDirectionNoSentiment:
    """The standing guarantee: this module has no opinion about money."""

    def test_match_exposes_no_price_edge_or_direction_field(self) -> None:
        names = {f.name for f in fields(Match)}
        assert names == {"ticker", "score", "matched_terms", "reason"}
        for forbidden in (
            "price",
            "edge",
            "side",
            "action",
            "probability",
            "confidence",
            "sentiment",
            "direction",
            "ev",
        ):
            assert forbidden not in names

    def test_reason_states_what_was_shared_and_claims_nothing_more(self) -> None:
        matches = match_headline(
            "August unemployment rate ticks up", CATALOG, min_score=BAR
        )
        reason = matches[0].reason
        assert "unemployment" in reason
        assert "distinctive terms" in reason
        for forbidden in ("edge", "buy", "sell", "bullish", "bearish", "likely"):
            assert forbidden not in reason.casefold()

    def test_results_are_frozen(self) -> None:
        # Nothing downstream may edit a score into something it wasn't.
        matches = match_headline(
            "August unemployment rate ticks up", CATALOG, min_score=BAR
        )
        with pytest.raises(AttributeError):
            matches[0].score = 0.99  # type: ignore[misc]

    def test_market_ref_carries_no_quote(self) -> None:
        assert {f.name for f in fields(MarketRef)} == {
            "ticker",
            "series_ticker",
            "title",
        }

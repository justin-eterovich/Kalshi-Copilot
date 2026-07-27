"""News-to-market relevance: which markets could a headline plausibly be about?

This sits in front of an expensive LLM scoring step, which sits in front of a
human. Its only job is to throw work away.

**The dangerous version of this module is the bag-of-words matcher that always
returns a ranked list.** Score every headline against every market, sort, take
the top five, and you have something that never returns nothing — and whose top
result always looks plausible, because "plausible" is exactly what shared
vocabulary buys you. It will connect "Fed holds rates steady" to a market about
the *Federal Reserve building*, and it will do so with a confident-looking 0.71
next to it. Downstream, every one of those becomes an LLM call and then a note
in front of an operator, until the operator learns the feed is noise and stops
reading it. At that point the whole news pipeline is worse than absent, because
it costs money and consumes the attention it was supposed to direct.

So this module is precision-first and **returns an empty list as a normal, and
in fact the usual, outcome**. The overwhelming majority of world news is about
nothing Kalshi lists. Two rules do most of that work:

1. **Distinctiveness, not frequency.** Evidence is a *rare* token shared
   between headline and title — "CPI", "unemployment", "Powell". A common one —
   "rate", "above", "2026" — is not evidence, and no amount of repetition makes
   it so. Weights are inverse document frequency and the vectors are over token
   *sets*, so a title saying "rate" four times gains nothing.
2. **One shared term is never enough.** See `MIN_SHARED_TERMS`.

What this module does **not** do, and must never be extended to do:

- **No edge, no direction, no sentiment.** It answers "are these two texts
  about the same thing", never "is this bullish", "does this resolve YES", or
  "how much". Nothing here reads a price and nothing here should start.
- **`score` is not a probability.** It is a cosine between two idf-weighted
  token sets, so it lands in ``[0, 1]`` and is therefore *very* easy to
  misread as a confidence. It is not one: 0.5 does not mean "50% likely
  relevant". It is not calibrated against anything, it cannot be multiplied by
  a payoff, and it must not be rendered as a percentage next to money.

Two limitations to state plainly, in the spirit of
`undervalued_screener.percentile_rank`:

- **The scoring is relative to the market set you pass in.** Document
  frequencies are computed over exactly those titles and nothing else. Hand it
  twenty CPI markets and "CPI" becomes a common word with almost no weight;
  hand it one, and "CPI" is the rarest thing in the corpus. There is no
  reference corpus of English, and no reference corpus of Kalshi. The same
  headline against a different market set will score differently, and comparing
  scores across two calls is meaningless.

  The degenerate end of that: a term appearing in **every** supplied title
  distinguishes nothing and is weighted zero, so a market set of one always
  returns ``[]``, and a set drawn from a single series returns nothing on the
  words that define the series. Screening a set of one measures nothing, the
  same way `undervalued_screener.percentile_rank` does. Pass a mixed set.
- **No stemming, no lemmas, no synonyms.** "rates" does not match "rate",
  "jobs" does not match "job", and "Fed" does not match "Federal". That costs
  real recall and is the deliberate direction to err in: a stemmer that
  collapses "hiking" and "hikes" also collapses "reserves" and "reserve", and
  every loosening here multiplies false matches against thousands of titles.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "MIN_SHARED_TERMS",
    "STOPWORDS",
    "Match",
    "MarketRef",
    "match_headline",
    "normalise",
    "tokens",
]


#: A match needs at least this many *distinct* distinctive tokens in common.
#:
#: One is not enough, and this is the single most important number in the
#: module. A headline and a title share one rare word constantly and for
#: uninteresting reasons: a company name in an unrelated story, a place name, a
#: month. "Powell" alone links a Fed-chair market to any story mentioning
#: anyone named Powell; "oil" alone links an OPEC market to a story about an
#: oil spill. Neither is about the other, and both would score well on a single
#: high-idf hit because a short headline has few tokens to dilute it.
#:
#: Two independent rare terms is a much harder coincidence to manufacture, and
#: it is the cheapest available proxy for "the same subject *and* the same
#: aspect of it" — which is what the downstream LLM is being asked to judge.
#: The cost is real and accepted: a correct single-token link is dropped. A
#: dropped true match costs one missed note; a false match costs budget,
#: operator trust, and eventually the feed.
MIN_SHARED_TERMS = 2

#: Words that carry no information in this corpus.
#:
#: Kalshi titles are near-templated — "Will the ... be above ... in ...?" —
#: so the scaffolding appears in a large fraction of every market set. idf
#: would push most of these towards zero on its own, but not reliably: on a
#: small or single-series market set the templating words are exactly the ones
#: shared with any English sentence, and they would otherwise clear the
#: two-term bar by themselves. Two headlines about nothing in common share
#: "will" and "the" all day.
#:
#: Kept deliberately *small* and free of subject matter. Nothing here is a
#: word a story could be about. In particular "rate", "record", "report",
#: "jobs" and every month and year are **not** stopwords — they can be the
#: whole point of a headline, and idf is the right tool for them.
STOPWORDS: frozenset[str] = frozenset(
    {
        # articles, conjunctions, prepositions, pronouns
        "a", "about", "after", "an", "and", "any", "are", "as", "at", "be",
        "been", "before", "being", "between", "but", "by", "during", "each",
        "for", "from", "had", "has", "have", "he", "her", "his", "how", "if",
        "in", "into", "is", "it", "its", "not", "of", "on", "or", "our",
        "over", "per", "she", "than", "that", "the", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "to", "under",
        "until", "up", "was", "we", "were", "what", "when", "which", "while",
        "who", "whose", "will", "with", "within", "would", "you", "your",
        # Kalshi title scaffolding: comparators and quantity words that appear
        # in the majority of titles and in any sentence containing a number.
        "above", "below", "exactly", "fewer", "greater", "less", "lower",
        "more", "most", "least", "many", "much", "no", "yes",
    }
)

#: Markdown emphasis, links, punctuation, and every other non-word character.
#: Underscore is explicitly included because ``\W`` does not match it and it is
#: markdown emphasis in this corpus (``**real GDP**`` has a ``__`` sibling).
_NON_WORD_RE = re.compile(r"[_\W]+", re.UNICODE)

#: A token that is only digits, or digits with an ordinal suffix. After
#: `normalise` has split on punctuation, "3.25%" is already "3" and "25", and
#: "$40,000" is "40" and "000" — so this catches the fragments too.
_NUMERIC_RE = re.compile(r"^\d+(?:st|nd|rd|th)?$")

#: Single characters survive normalisation of things like "U-3" and "Lowe's"
#: as "u" and "s". They are never evidence.
_MIN_TOKEN_LENGTH = 2

#: idf below this counts as "in essentially every title" and is not evidence.
#: Only bites on small or single-series market sets, where a token can appear
#: in every row; there it stops a shared template word from being counted
#: towards `MIN_SHARED_TERMS`.
_MIN_EVIDENCE_IDF = 1e-9


@dataclass(frozen=True, slots=True)
class MarketRef:
    """The minimum a market needs to expose to be matched against text.

    Deliberately not a database row and deliberately not a quote. This module
    never sees a price, and adding one would be the first step towards it
    having an opinion about direction.
    """

    ticker: str
    #: Carried for the caller's benefit — grouping, dedupe, routing — and
    #: **not scored**. Exchange tickers are opaque codes, not natural language:
    #: substring-matching "KXOIL" or "KXHIGHNY" against headline text is a
    #: false-positive generator with no upside, since anything a ticker encodes
    #: is spelled out in the title anyway.
    series_ticker: str | None
    #: The market title as the catalog holds it, markdown and odd
    #: capitalisation included. `normalise` is expected to handle the mess;
    #: callers should not pre-clean it, because then the corpus statistics are
    #: computed over something other than what was matched.
    title: str


@dataclass(frozen=True, slots=True)
class Match:
    """One market a headline might be about. A topic link, nothing more."""

    ticker: str
    #: idf-weighted cosine in ``[0, 1]``. An ordering, not a probability and
    #: not a confidence — see the module docstring. Comparable only within a
    #: single call, because the weights come from the market set supplied to
    #: that call.
    score: float
    #: The distinctive tokens the two texts shared, rarest first. This is the
    #: audit trail: if a match looks wrong, these are the words that caused it.
    matched_terms: tuple[str, ...]
    #: One line a human can disagree with, in the same spirit as the screener's.
    reason: str


def normalise(text: str) -> str:
    """Fold text to lowercase words separated by single spaces.

    Removes: markdown emphasis and link syntax, all punctuation, all symbols,
    and repeated whitespace. ``"Will **real GDP** increase by more than 4.0%"``
    becomes ``"will real gdp increase by more than 4 0"``.

    Does **not** remove: digits (`tokens` handles those, so that a caller
    inspecting `normalise` output still sees what was there), accents or
    non-Latin scripts (NFKC compatibility folding only — "café" stays "café"),
    or stopwords. Does not stem, lemmatise, expand abbreviations, or resolve
    synonyms; see the module docstring for why not.

    Note what splitting on punctuation costs: "3.25%" becomes two tokens "3"
    and "25", and "Lowe's" becomes "lowe" and "s". Both are acceptable because
    `tokens` discards the fragments — numerals and single characters are never
    evidence here. It would not be acceptable if numbers were being compared
    for *value*, which is a thing this module deliberately does not attempt.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _NON_WORD_RE.sub(" ", folded).strip()


def tokens(text: str) -> list[str]:
    """Distinctive-candidate tokens from ``text``, in order, duplicates kept.

    Drops three classes of token, none of which can support a topic link:

    - **Stopwords** (`STOPWORDS`). "Will", "the", "be", "in", "above" appear in
      nearly every Kalshi title and in nearly every English sentence; left in,
      they dominate every comparison and every headline matches everything.
    - **Bare numerals** — "2026", "3", "25", "40000", "1st". Numbers and dates
      are the classic trap in this corpus: dozens of unrelated titles contain
      "2026", and "3.25%" shares its digits with any market whose threshold
      happens to be 3-point-something. Worse, a numeral match *feels*
      specific to a human reading the audit trail, so a false match built on
      one is unusually convincing. Note the consequence: this module cannot
      tell "CPI rose 0.3%" from "CPI rose 0.9%" and does not try — quantities
      are the LLM's job, and after that the human's.
    - **Single characters**, the debris of "U-3" and "Lowe's".

    Mixed alphanumerics like "0bps" and "g7" survive, since the letters make
    them words rather than quantities.

    Duplicates are preserved because this is a general-purpose tokeniser;
    `match_headline` takes sets and so is unaffected by term frequency.
    """
    return [
        token
        for token in normalise(text).split()
        if len(token) >= _MIN_TOKEN_LENGTH
        and token not in STOPWORDS
        and not _NUMERIC_RE.match(token)
    ]


def match_headline(
    headline: str,
    markets: Sequence[MarketRef],
    *,
    min_score: float,
    max_matches: int = 5,
) -> list[Match]:
    """Markets the headline could plausibly be about. Usually none.

    Returns at most ``max_matches`` matches, best first, ties broken by ticker
    so the order does not depend on how the caller happened to sort its query.
    A market qualifies only if it shares at least `MIN_SHARED_TERMS` distinct
    distinctive tokens with the headline *and* the resulting score reaches
    ``min_score``. Nothing qualifying means ``[]``, which is the expected
    answer for most news.

    ``min_score`` is a **cosine in (0, 1]**, not a percentage. A caller who
    means "0.4" and passes ``40`` would otherwise get silence forever with no
    error — a filter that is broken in the safe direction is still broken, and
    it would look exactly like "the news feed found nothing today". So it is
    rejected outright, as `undervalued_screener.screen` rejects a percentile
    handed in as a percent. Zero is rejected for the same reason in the other
    direction: it would disable the score filter and leave only the term count.

    Refusals, all returning ``[]``:

    - empty or whitespace-only ``headline``, and a headline whose every token
      is a stopword or a numeral ("Above 3 in 2026")
    - empty ``markets``, or a market set whose titles yield no tokens at all
    - ``max_matches <= 0`` — a coherent request with an empty answer
    """
    if not math.isfinite(min_score) or not (0.0 < min_score <= 1.0):
        raise ValueError(
            f"min_score is a cosine in (0, 1], got {min_score!r}"
        )
    if max_matches <= 0:
        return []

    headline_terms = frozenset(tokens(headline))
    if not headline_terms:
        return []

    # Only markets with usable text form the corpus. A title that tokenises to
    # nothing can never match, and leaving it in the denominator would inflate
    # every idf in the set by pretending the corpus is larger than it is.
    corpus: list[tuple[MarketRef, frozenset[str]]] = []
    for market in markets:
        title_terms = frozenset(tokens(market.title))
        if title_terms:
            corpus.append((market, title_terms))
    if not corpus:
        return []

    idf = _idf(term_sets=[terms for _, terms in corpus])

    # The headline's own norm is computed over *all* its terms, including ones
    # absent from every title — those get the maximum weight. That is the point:
    # a headline mostly about things this exchange does not list should score
    # low even where it does brush against a market, because most of what it is
    # about is elsewhere.
    headline_norm = _norm(headline_terms, idf=idf, corpus_size=len(corpus))
    if headline_norm <= 0.0:
        return []

    matches: list[Match] = []
    for market, title_terms in corpus:
        match = _score_one(
            market,
            title_terms=title_terms,
            headline_terms=headline_terms,
            headline_norm=headline_norm,
            idf=idf,
            corpus_size=len(corpus),
            min_score=min_score,
        )
        if match is not None:
            matches.append(match)

    matches.sort(key=lambda m: (-m.score, m.ticker))
    return matches[:max_matches]


def _score_one(
    market: MarketRef,
    *,
    title_terms: frozenset[str],
    headline_terms: frozenset[str],
    headline_norm: float,
    idf: dict[str, float],
    corpus_size: int,
    min_score: float,
) -> Match | None:
    """Score one market against one headline, or refuse it.

    Every ``None`` below is a refusal, and the two are not redundant: the term
    count is a floor on *how many independent coincidences* are required, and
    the score is a floor on *how rare* they were relative to the rest of the
    headline. A headline can clear either alone.
    """
    shared = headline_terms & title_terms
    # Terms in literally every title carry zero weight and must not count
    # towards the evidence bar either — otherwise a two-word overlap of
    # boilerplate satisfies a rule meant to require two rare words.
    evidence = [t for t in shared if idf.get(t, 0.0) > _MIN_EVIDENCE_IDF]
    if len(evidence) < MIN_SHARED_TERMS:
        return None

    title_norm = _norm(title_terms, idf=idf, corpus_size=corpus_size)
    if title_norm <= 0.0:
        return None

    overlap = sum(idf[t] ** 2 for t in evidence)
    score = overlap / (headline_norm * title_norm)
    # Cosine is bounded by construction; clamp only against float drift, so a
    # 1.0000000002 can never be shown to a human as a >100% anything.
    score = min(1.0, max(0.0, score))
    if score < min_score:
        return None

    ordered = tuple(sorted(evidence, key=lambda t: (-idf[t], t)))
    return Match(
        ticker=market.ticker,
        score=score,
        matched_terms=ordered,
        reason=_reason(market, terms=ordered, score=score),
    )


def _idf(*, term_sets: Sequence[frozenset[str]]) -> dict[str, float]:
    """Smoothed inverse document frequency over the supplied titles.

    ``log((N + 1) / (df + 1))``, which is non-negative everywhere and exactly
    zero for a term in every title — the right answer for a word that
    distinguishes nothing. Terms absent from every title are not in this map;
    `_weight` gives them the maximum, ``log(N + 1)``.

    Document frequency counts *titles containing the term*, not occurrences, so
    a title that says "rate" four times contributes one. That is what makes
    this distinctiveness rather than term frequency.
    """
    n = len(term_sets)
    counts: Counter[str] = Counter()
    for terms in term_sets:
        counts.update(terms)
    return {term: math.log((n + 1) / (df + 1)) for term, df in counts.items()}


def _weight(term: str, *, idf: dict[str, float], corpus_size: int) -> float:
    """Weight of one term, with unseen terms pinned to the corpus maximum.

    A headline word that appears in no title is maximally distinctive with
    respect to this market set — which in practice means "this headline is
    about something else", and giving it full weight is what makes the cosine
    fall away for off-topic news.
    """
    return idf.get(term, math.log(corpus_size + 1))


def _norm(
    terms: Iterable[str], *, idf: dict[str, float], corpus_size: int
) -> float:
    """Euclidean norm of the idf weights of a token set."""
    return math.sqrt(
        sum(_weight(t, idf=idf, corpus_size=corpus_size) ** 2 for t in terms)
    )


def _reason(market: MarketRef, *, terms: tuple[str, ...], score: float) -> str:
    """One line explaining the match, phrased as an observation.

    Says what was shared and nothing about what it implies. There is no verb
    here like "suggests" or "supports" on purpose: this module has established
    that two texts use the same rare words, which is not yet a claim that the
    news bears on the market at all — that judgement belongs to the LLM step
    and then to the human.
    """
    shared = ", ".join(terms)
    where = market.series_ticker or market.ticker
    return (
        f"shares {len(terms)} distinctive terms with {where} "
        f"({shared}); cosine {score:.2f}"
    )

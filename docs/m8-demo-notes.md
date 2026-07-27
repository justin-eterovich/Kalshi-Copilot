# M8 — news and catalyst engine

Status: **done**. Verified on the live stack against real feeds and the live
catalog on 2026-07-27.

Two halves with very different value: a **catalyst calendar** that says when
trading windows shut, and a **headline collector** that says what was public
and when. Neither claims an edge, and one of them structurally cannot.

---

## The finding that defines the milestone

**On scheduled economic releases, the Kalshi market closes minutes BEFORE the
data lands.** Measured against the live catalog:

```
KXCPI          closes 12:25 UTC   CPI released 12:30 UTC (08:30 ET)   -5 min
KXPAYROLLS     closes 12:29 UTC   NFP released 12:30 UTC              -1 min
KXGDP          closes 12:29 UTC   GDP released 12:30 UTC              -1 min
KXFEDDECISION  closes 17:59 UTC   FOMC statement 18:00 UTC            -1 min
```

So **there is no "trade the news" window on these markets at all.** Trading has
already stopped when the number is published. A catalyst detector that fires
when the data drops would light up precisely when it is too late to act — and
would look, on a chart, exactly like a working detector.

What the calendar is actually for is the honest, useful version: **a deadline
board.** A market that closes in 40 minutes ahead of CPI is a decision that
has to be made now or not at all. `CLOSED_PENDING_SETTLEMENT` is rendered as
"window shut — awaiting settlement", worded as a miss rather than an
opportunity, and `actionable` is False there.

### The inference rule

Rather than hard-code an offset per series, the calendar found the generating
pattern: **every scheduled release closes 1 or 5 minutes before a quarter-hour
boundary.** `expected_release_time` rounds the close strictly up to the next
:00/:15/:30/:45 and refuses if the gap exceeds five minutes.

Validated on all 41 claimed series — 41 inferred, 0 refused. It handles both
08:15 ET releases (ADP) and the EST/EDT shift with no timezone logic at all,
because the boundary is derived from the close itself.

`expected_release` is **inferred, not published**. Kalshi does not expose it,
and it is never a settlement time.

### Series tickers are names, again

`KXFED` is a real catalyst; `KXFEDTWEETS`, `KXFEDEND` and `KXFEDERALCHARGE`
are live series about other things. The lookup is exact — the same lesson as
`KXLOW` being Lowe's Companies Inc. in M7. Two milestones running, the naive
prefix match would have been wrong.

---

## Headlines: collection, deliberately separated from interpretation

Storing the text is cheap, reversible and useful on its own — it is the record
of what was public and when, which is what you want when a market moved and
you are asking whether the information was available. Interpreting it costs
money and can be wrong. Keeping the two apart lets the expensive half stay off
indefinitely without the cheap half losing history.

**There is no API key configured on this deployment**, so the LLM tier refuses
rather than running, and the dashboard says so rather than looking idle.

### What the real feeds taught us

- **BBC's RSS declares `xmlns:atom`** and carries `<atom:link rel="self">`
  inside `<channel>`; so do arXiv and every WordPress feed. Detecting Atom by
  "does the document mention the Atom namespace" returns zero items for BBC,
  and a suffix match on `endswith("link")` returns *the feed's own URL* as the
  article link. Detection is by root tag.
- **BLS and SEC return an HTML "Access Denied" page with HTTP 200** to
  unrecognised user agents. Nothing upstream flags it — a status check passes.
  Caught by the parser's root-tag check, and the reason `news.user_agent` is
  its own config key with a contact in it.
- **The Federal Reserve feed starts with a UTF-8 BOM** and wraps even
  `<pubDate>` in CDATA.
- **`-0000` means "zone withheld", not UTC**, and is correctly refused.
- **Double escaping is real**: Federal Register emits `&amp;#39;`.
- GitHub's Atom entries are **not in chronological order**, which kills any
  temptation to treat feed order as time order.

403 of 403 items parsed across 8 live feeds, zero naive datetimes.

### Relevance is precision-first and usually returns nothing

Two distinct shared tokens, each with non-zero idf. One rare shared token is a
coincidence generator — "Powell" links a Fed-chair market to any story about
anyone named Powell.

Probed against 3,000 real markets:

| headline | result |
|---|---|
| "Federal Reserve holds interest rates steady at July meeting" | **KXFEDDECISION** (0.55) |
| "Nonfarm payrolls report shows jobs added in August" | **KXPAYROLLS-26AUG** (0.53) |
| "Oil price dives as US and Iran pause attacks" | no match |
| "Carly Simon reveals Parkinson's diagnosis" | no match |
| "US consumer price index rose 0.4%, CPI inflation data shows" | **no match** |

That last row is a **genuine miss**, and it is the visible cost of the bar:
the market title tokenises to `{cpi, rise, july}`, the headline shares only
`cpi`, and there is no stemming so "rose" ≠ "rise". The trade was made
deliberately — a false match spends budget and trains the operator to ignore
the feed — but the bar is set where a real CPI headline slips through.

The matcher's own test suite includes the decisive case: a decoy market "Will
the Federal Reserve *building renovation* cost exceed $3.0 billion" is **not**
returned for "Fed holds rates steady", and symmetrically.

---

## The budget guard

Two independent limits that fail differently:

- **`daily_budget_usd`** caps the money. Checked *before* the call — a guard
  that notices the budget is blown once it is blown is a report, not a limit.
- **`escalation_rate_cap`** caps the proportion reaching the expensive model.
  This is a **correctness guard, not a second budget**: if triage starts
  escalating everything, triage has broken, and stopping beats spending the
  day's budget in ten minutes and calling it working.

Refusal ordering puts the rate cap *before* the money, so an operator sees the
real cause — reporting "budget exhausted" would point them at
`daily_budget_usd`, and raising it buys ten more minutes of the same bug.

Seven named refusals including `no_api_key` and `spend_unknown`. Cost is
**not quantized to cents**: a triage call is ~$0.000225 and rounding each to a
cent rounds it to zero, making the budget immortal. Escalation floor is 5, so
a quiet morning with four headlines does not read as broken.

`LlmSpend` is persisted per UTC day, matching `risk.py`, because a guard that
resets on restart is a per-uptime budget and an unattended crash loop would
spend without limit.

---

## A bug the live run caught

`MAX_HEADLINE_AGE` was two days. The Federal Reserve press feed parsed
perfectly — 20 items — and contributed **zero headlines**, because its newest
item was eleven days old. The cutoff was silencing exactly the sources whose
releases settle markets, while leaving a chatty newswire unaffected.

It is now 30 days, and the docstring says why: the window bounds cold-start
work, it is not a freshness judgement. Re-processing is already prevented by
the unique constraint on `guid`.

---

## Dashboard

A **Catalysts** panel on the trades page, grouped by *event* rather than
listed per market. One FOMC meeting is 40 tradeable strikes but one deadline;
40 identical rows is the same failure as an unfoldable signals table. Grouping
took it from 40 rows to 18, each with a market count.

The headline list carries no score, direction or sentiment — structurally, not
as unfinished work. The LLM stat line shows spend against budget and the
realised escalation rate against its cap, and says plainly when the engine is
silent because no key is configured.

---

## Verification

```
1292 tests passed
ruff check app/                                clean
mypy app/core app/config.py app/settings.py    clean
5 compose services healthy
```

Live: 2 feeds polled, 31 BBC headlines + 20 Fed items ingested and
deduplicated; 18 catalyst events from the live catalog; no console errors in
the browser. Config restored to `news.enabled: false` afterwards.

---

## Schema changes

```sql
CREATE TABLE news_headlines (...);  -- UNIQUE (guid)
CREATE TABLE llm_spend (...);       -- UNIQUE (day)
```

---

## Open items

- **The LLM tier is not built, only guarded.** `budget.py` is complete and
  tested; the code that actually calls a model is not written, because there
  is no key to call with. The guard is the hard part and it is done.
- **Relevance misses real CPI headlines** (above). Stemming, or a
  series-level synonym table, would fix it — both are ways to loosen a bar
  that was set tight on purpose, so it wants measuring rather than guessing.
- **The `series` table is empty** — 0 rows. Catalog sync never populates it,
  so `settlement_sources` is unavailable. Pre-existing, not M8, but it is why
  the calendar infers release times instead of reading them.
- **Cross-source clustering is not attempted.** The same story from two feeds
  stays two headlines; `guid` identity is per-feed by design.
- **`news.calendar.sources: [bls, bea, fed]` is unused.** The calendar reads
  Kalshi's own close times, which turned out to be the better source.

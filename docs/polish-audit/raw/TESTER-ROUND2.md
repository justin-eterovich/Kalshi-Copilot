# TESTER round 2 — live-browser verification

Real Chromium (`kalshi-pw:latest`, headless_shell 1148, cross-checked one item
against the full `chromium-1148` binary too) against the running stack at
`http://127.0.0.1:8080`. Stack confirmed `docker compose ps` → 5/5 healthy
before testing. Every finding below came from actually driving the UI and/or
curling the live API — nothing is inferred from source alone unless marked.

No `docker compose` state was touched, no config edited, no order approved or
placed. One pre-existing `proposal.approved` / `order.submitted` row for
KXMLB-26-HOU appears in the audit trail screenshot below — that predates this
session (visible with `05:48:` / `02:18:` timestamps well before I started)
and was not something I did.

Screenshots referenced below are in
`docs/polish-audit/screenshots/tester-round2/`. Scratch scripts live under the
session scratchpad (`scripts/r2_*.mjs`, `shots/r2_*.png`).

---

## Lead item: worse than before

**A new P1.** The Detector Signals table on `/trades` catastrophically
collapses below ~1100px width — not something round 1 flagged, and not one of
the round-2 fix targets, but a real regression surface this round's changes
sit next to (`.audit-detail`'s "wrap, don't truncate" fix, applied in
`bc47c1d`, is the mechanism). See **UI-054** below. This is the first thing
the manager should look at.

---

## Verdict per round-1 finding

```
UI-041  max_spread lets crossed books through          FIXED
UI-042  "1d" chart tab 422s on every market             FIXED
UI-043  Positions real. gross of fees, clipped          FIXED
UI-044  Negative spreads carry no visual signal         FIXED
UI-045  Header vs order book disagree, unreconciled     FIXED (by labeling, as designed)
UI-046  Three "detectors ship disabled" strings          FIXED
UI-047  table-scroll has no fade/scrollbar affordance    PARTIAL — see below
UI-048  Locale RangeError blanks the chart               FIXED
UI-049  Recent Decisions unfolded, 24,000px page         FIXED
UI-050  "liquidity" overpromises on a market with no bid  PARTIAL — see below
UI-051  Audit detail column unformatted JSON              FIXED
UI-052/053  attention score "—" for crossed/unquoted      FIXED
```

### UI-041 — max_spread filter — FIXED

Live UI test: Screener → spread filter → "≤ 1¢" now returns **1,293** rows (was
66,547; manager's snapshot said 1,209 — the gap is normal catalog drift between
measurements, not a regression), and **0 of the visible rows carry a
`crossed-flag`** (checked via DOM query on page 1). Every visible SPR value is
a small positive number (0.1–1.0¢). Screenshot:
`03-maxspread-1c-fixed.png`.

### UI-042 — "1d" chart tab — FIXED

Tested on three markets (`KXMLB-26-WSH`, `KXHORMUZNORM-26MAR17-B261101`,
`KXPRESNOMD-28-HBID`), all three tabs (1m/1h/1d) each. Zero console errors,
zero pageerrors, zero failed (4xx/5xx) requests across all nine loads. The
"1d" tab on `KXMLB-26-WSH` correctly shows **"No candles for this window.
Kalshi returns none for markets that have never traded."** — a real empty
result at 200, not the old 422. `MAX_LOOKBACK_HOURS = 24 * 90` in
`frontend/src/MarketPage.tsx:32` matches the API's `le=2160` cap exactly.
Screenshot: `04-chart-1d-no-longer-422.png`.

The `api.ts` `parseError` fix (`frontend/src/api.ts:634-672`) that turns
FastAPI's 422 array body into a readable `"loc: msg"` string is written
generically over both `getJson` and `postJson` — it is not endpoint-specific.
I could not find another **UI-reachable** 422 to exercise it against: every
other `Query(..., ge=/le=)` constraint in `markets.py` / `trading.py` is on a
param the frontend hardcodes or the user can't set out of range (page size,
offset, ticket fields are all `Literal`/dropdown-constrained). So this is
verified by code reading as globally applied, and by the one live case
(candles) as working; I did not find a second live 422 to cross-check.

### UI-043 — Positions gross/fees — FIXED

Header now reads **"real. (gross)"** with a tooltip; **"fees paid"** is the
8th column and reachable by scrolling the `.table-scroll` (confirmed
`clientWidth=492` vs `scrollWidth=630` at 1440px, scrolled programmatically to
reveal `FEES PAID` = `-20.88¢` for `KXNFLGAME-26AUG15DALSEA-SEA`). A prose
note beneath the table explains **why** subtracting the two columns is wrong
("a fee is charged the instant a fill happens — including fills that only
*open* a position — while realised P&L is booked only when a fill *reduces*
one"), and names where the real net figure lives (Risk panel's daily net,
report card's expectancy). Read this note specifically for confusion risk: it
is clear and does not invite the subtraction it argues against — it states
the wrong arithmetic, says why it's wrong, and points elsewhere for the right
number, in that order. Good writing for a caveat like this.
Screenshots: `08-positions-left.png`, `09-positions-scrolled-fees-reachable.png`.

### UI-044 — crossed spread visual signal — FIXED

Screener now renders crossed spreads in warn-orange with a `crossed` tag and a
tooltip explaining the summary-quote payload also reports negative bid sizes.
Confirmed live on the default screener view — `KXPRESNOMD-28-HBID` (-0.9),
`KXPRESNOMD-28-MK` (-2.6), etc. all correctly flagged; ordinary tight spreads
(0.1, 0.2) render plain. Screenshot: `01-screener-crossed-flags-1440.png`.

### UI-045 — header vs order book divergence — FIXED (by design: labeled, not reconciled)

Tested `KXHORMUZNORM-26MAR17-B261101` live. Header shows `62.0 / 34.0 ¢
bid/ask CROSSED`; order book shows `bid 56.0 ask 34.0 spread -22.0`. The two
still disagree on the bid (as CLAUDE.md and round 1 both note is expected —
two different feeds) — but now:
- the header carries a `crossed` flag with a tooltip,
- the Order Book panel has a `.quote-source-note` explicitly naming the two
  feeds and saying which one wins ("**this book wins**: it is the depth an
  order meets"),
- the trade ticket shows a **new warning box** not explicitly asked for in
  the brief: *"The summary quote for this market is crossed (bid above ask),
  so the price seeded above is derived from numbers the exchange is
  publishing but nobody can trade on. Check the order book before
  proposing."*

This is a good, proportionate fix for a divergence that is real exchange data
and not meant to be reconciled away. Screenshots:
`05-chart-hormuz-crossed-header.png`, `06-hormuz-fullpage-crossed-everywhere.png`.

Checked wrapping at 768px specifically (the brief's ask): the header's
`bid/ask CROSSED` line and the order-book panel's `.quote-source-note` both
wrap cleanly into extra lines with no clipping or overlap — see
`15-market-page-768-clean.png`. No `@media` query is involved; flexbox +
`max-width` absorb it.

### UI-046 — three "detectors ship disabled" strings — FIXED

All three checked live:
- `Trades.tsx` empty approval queue → *"6 detectors are enabled
  (set_arbitrage, stale_quote, resolution_sniper, undervalued_screener,
  whale_flow, longshot_calibration) — they propose only when they find an
  edge that survives fees..."* — confirmed rendered, `10-trades-detector-note-fixed.png`.
- `Trades.tsx` empty signals state — same `DetectorNote` component, same gate
  (signals list wasn't empty in this session so didn't render, but the code
  path is identical to the one just confirmed and reads `detectors` from the
  same `/api/system` fetch — verified in source, `Trades.tsx:690-698`).
- `SystemPanel.tsx` build-progress footnote → *"6 detectors are enabled
  above"* — confirmed, `13-system-page.png`.

### UI-047 — table-scroll fade + scrollbar — PARTIAL, harness-limited

14 sites confirmed via grep (matches the brief's count exactly).

**The fade is real but very subtle.** Pixel-sampled the Positions table's
right edge programmatically (drew the screenshot into a canvas and read
`getImageData`): background darkens smoothly from RGB(16,16,16) to RGB(8,8,8)
over the rightmost ~25px — a genuine gradient, correctly implemented per the
CSS (`background-attachment: local, local, scroll, scroll` two-layer trick),
but an 8-of-255 darkening on a near-black background is close to
imperceptible at normal screen brightness. I would not have found it without
sampling pixels; I don't think an operator glancing at the table would notice
it either. Consider this "technically fixed, weakly effective."

**The "always-visible" scrollbar did not render in this test harness at all**
— in either `chromium_headless_shell-1148` (`kalshi-pw` default) or the full
`chromium-1148` binary in headless mode. Measured directly: `.table-scroll`'s
`offsetHeight - clientHeight` = 2px, which is entirely accounted for by the 1px
border on each side — **zero pixels are reserved for a scrollbar**, meaning
Chromium is treating this as an overlay scrollbar regardless of the explicit
`::-webkit-scrollbar { height: 9px }` rule, and no overlay bar painted even
during an active `mouse.wheel()` scroll gesture. `scrollbar-width: thin` is a
Firefox-only property and does nothing here. This has the same profile as
round 1's UI-048 locale bug — a headless/CI-specific rendering quirk rather
than necessarily a real-desktop-Chrome problem — but I could not confirm it
either way without a real windowed browser, so **I am not marking this fixed
outright**. The CSS is correct and present; whether it visually manifests as
a persistent scrollbar on an operator's actual desktop Chrome is unverified.
Recommend a spot check on a real machine before closing this out fully.

### UI-048 — chart locale/blank — FIXED

`localization: { locale: "en-US" }` pinned in `PriceChart.tsx:52`. Zero
console errors, zero pageerrors across 9 chart loads (3 markets × 3 periods).
Candlesticks render with real data. Screenshots: `04-chart-1d-no-longer-422.png`
(also shows the 1m tab's rendered candles on the same market, one screenshot
prior — see scratch `r2_chart_mlb_1m.png`).

### UI-049 — Recent Decisions folding — FIXED

Confirmed live: 10 `.repeat-note` elements visible on one screen, reading e.g.
*"×13 identical — same market, same source, same outcome, since 20:30:24.
Showing the most recent; every one is still its own row in the audit trail."*
Screenshot: `11-recent-decisions-folded.png`. Total page height at 1440px
dropped from round 1's 24,920px... actually it's still **24,920px** at 1440 —
folding capped Recent Decisions itself to ~3,900px (`.slice(0, 20)` in
`Trades.tsx:280`), but the page is now dominated by Detector Signals
(5,403px) and Audit Trail (11,279px), both showing up to 50–100 rows each.
That's expected/by-design (nothing there needs folding — those are
naturally-bounded lists), not a regression.

**Pending/status invariants — verified in source, not by a live pending
proposal** (queue was empty throughout this session): `decided` in
`Trades.tsx:259-281` filters `p.status === "pending"` out **before** building
groups, and the group key includes `p.status`, so an `approved` and an
`expired` for the same market/source/leg-count key to different groups and
cannot fold together. Both invariants read correctly; I did not get to watch
a live approval join the fold (nothing to approve was live-decided during
testing, and I was not going to approve one myself to manufacture a test
case).

### UI-050 — "liquidity" → "attention" rename + "no bid" flag — PARTIAL

**Rename: FIXED.** Screener header now reads `attention`, sort dropdown reads
"attention score", tooltip explains it's an ordering, not tradability.
Confirmed live.

**"no bid" flag: mechanism present, could not observe it live, and here's
exactly why.** The brief's own trap note is correct that `KXGOVAK-26-NDAH`
moved (now `yes_bid=0.015`, score 50.5, confirmed no flag — see
`07-govak-real-bid-no-flag.png`, exactly matching the API). I went looking for
a *different* no-bid market to exercise the flag and found the backend fix
(refusing to score a one-sided book, `_has_two_sided_quote` in
`markets.py:112`) has an interesting side effect: **any market with a literal
`yes_bid = 0` now always gets `liquidity_score = None`** server-side (`0 <
price` fails for `price = 0`), which means a **fresh** page load can never
produce the specific combination round 1 found (a numeric score *and* no
bid) — I confirmed this on `KXGOVFLNOMR-26-JCOL` (bid 0.00, ask 0.01): score
renders as plain `—`, no flag, because there is no score to attach a flag to.

The frontend's `noBid` flag (`Screener.tsx:307`, `ScoreBar`) is computed from
the **live-merged** `yes_bid` while `score` comes from the **REST-fetched**
`m.liquidity_score`, which the Screener never repolls on an interval (only on
filter/page change). So the flag *is* reachable — precisely when a market's
bid gets pulled via a live websocket tick sometime after the page's last REST
fetch, while its cached score is still the earlier non-null value — but that
is a transient race, not the common case, and not something I could
manufacture or catch live in a ~40-second observation window over the
top-60-by-volume rows (which are liquid and don't lose their bid that fast).
I do not think this is a bug — the logic is sound and the comment in
`ScoreBar` correctly documents the intended case — but I want to flag plainly
that **the specific visual (a number + a "no bid" tag together) went
unobserved this round**, for the same reason it may be genuinely rare in
practice now that the backend also refuses to score these.

### UI-051 — Audit JSON pretty print — FIXED

Confirmed live: `JSON.stringify(entry.payload, null, 2)` inside a `<pre
className="audit-json">`, correctly indented, multi-line, readable at a
glance. Screenshot: `12-audit-json-pretty.png`. Height is capped
(`max-height: 14em`) so no single payload dominates the page — measured
~113px/row average at 1440px for the whole Audit Trail section (100 rows,
11,279px total), consistent with a working cap.

### UI-052 / UI-053 — attention shows "—" for crossed/unquoted — FIXED

Confirmed multiple ways:
- Individual no-quote market (`KXGOVFLNOMR-26-JCOL`) renders `—`, no bar, no
  number.
- Crossed market (`KXHORMUZNORM-26MAR17-B261101`) renders `—` on its own
  Details panel ("attention score  —") and on all 7 of its crossed sibling
  legs in the "Same event" table.
- Default screener view (sorted by 24h volume, the normal landing page):
  **zero dashes in the visible rows** — every row I checked in the first ~20
  had a numeric bar (96, 92, 75, etc.), matching the manager's "zero of top
  60" measurement.
- Sampled the **tail**: `GET /api/markets?sort=volume_24h&order=asc&limit=1000`
  (the lowest-volume 1,000 active markets) → **715 of 1,000 (71.5%) score
  `None`**. This is the deep-tail case the brief asked me to judge for
  "reads as deliberate vs. broken." A bare `—` is the same glyph the rest of
  the app already uses for "no data" (`asCount`, `centsNum`, `asTimeToClose`
  all fall back to it), so it reads as consistent with the app's own
  vocabulary rather than as a broken column — I'd call this a legible design
  choice, not a bug. One adjacent, minor observation: the screener's sort
  dropdown has no ascending option, so an operator cannot actually *page* into
  this tail from the UI in a reasonable number of clicks (I stopped at
  1,860/93,697 after 30 "next" clicks) — they'd have to search/filter down to
  a specific low-volume market instead, which works fine but isn't really
  "paging." Not a regression from this round, not filing it as new — just
  noting the tail is more of an API-verified fact than something I paged to
  and screenshotted end-to-end.

### Trap 1 — KXGOVAK-26-NDAH moved — CONFIRMED as briefed

`yes_bid=0.015000`, score 50.5 (renders "51"), **no "no bid" flag** — exactly
as the manager's note predicted. Screenshot: `07-govak-real-bid-no-flag.png`.

### Trap 2 — OrderBookLadder.tsx:29 float parse — CHECKED, RENDERS CLEAN, LEAVE AS-IS

`(1 - Number(p)) * 100` is a float parse of money, which is against the
letter of CLAUDE.md's units rule — but I ran the actual arithmetic rather than
just objecting to it on principle: swept 2,000,000 random 6-decimal price
strings (the full precision the wire format supports) through both the direct
and inverted paths and compared against exact rational arithmetic. **Maximum
observed error: 1.4 × 10⁻¹⁴**, roughly nine orders of magnitude below the
`toFixed(1)` rounding threshold (0.05) that the display actually uses. There
is no price at any tick size Kalshi supports where this produces a visible
`55.99999`-style artifact or an off-by-a-hundredth row. The file's own header
comment is right that this is display-only and cannot cause an order to
exist. I'm not filing this — it renders clean, full stop, and matches the
comment's own reasoning for why it stays.

---

## New findings

```
[P1] UI-054 — Detector Signals table becomes ~10x taller than normal and functionally
unreadable below ~1100px viewport width; a 768px operator sees a nearly-blank
page with isolated floating rows
Repro:    Load /trades at 768x1000 (or any width below ~1100px). Scroll to the
          "Detector signals" panel.
Expected: The table renders like every other table in the app — rows a
          reasonable height, readable without excessive scrolling. This is
          the same "why"/rationale-wrapping fix pattern already applied
          elsewhere in this round (UI-051's audit JSON, the "wrap don't
          truncate" note on `.audit-detail`), so it should degrade the same
          way News' headline column does (see below) — gracefully.
Actual:   Measured via DOM: at 1440px/1280px/1100px the "Detector signals"
          section is a normal ~5,400px for 50 rows (~108px/row). Below
          ~1100px it explodes:
            1100px -> 5,387px   (whyWidth 277px)
            1050px -> 6,200px   (whyWidth 227px)
            1000px -> 8,559px   (whyWidth 177px)
             900px -> 26,011px  (whyWidth 77px)
             768px -> 52,913px  (whyWidth 41px)
             640px and below -> ~52,929-52,949px, whyWidth pinned ~41-43px
             (page itself also gains a genuine horizontal scrollbar below
             640px: scrollWidth 704 vs clientWidth as low as 375)
          Root cause: the table has 9 columns, 8 of them `white-space:nowrap`
          (time/detector/ticker/side/net edge/score/conf/seen) and one
          flexible column (`why`, using `.audit-detail`: `white-space:
          normal; overflow-wrap: anywhere; max-width: 520px` — no minimum,
          no height cap). When the 8 rigid columns' combined natural width
          approaches the container width, the browser's auto table layout
          squeezes the one flexible column down toward zero rather than
          growing the table or wrapping the rigid columns, and
          `overflow-wrap: anywhere` then obliges by breaking a ~200-character
          rationale string into one two-or-three-character fragment per
          line — 40-60+ lines for a single cell. At 768px I confirmed a real
          rationale cell (`sg.rationale`, 207 characters) rendered at
          41.0px wide.
          Visually this does not look like "a long table" — it looks
          broken. A screenshot scrolled partway through shows an almost
          entirely blank black screen with one lone row's data floating in
          the middle (see evidence below): time, detector, ticker, side,
          score and confidence all appear once, correctly, then ~1000px of
          nothing before the next row.
          This directly contradicts the styles.css comment immediately above
          the relevant media query: "No tablet tier on purpose. Between
          roughly 640px and 1100px the auto-fit grids already do the right
          thing" (styles.css:1386-1389) — for this table specifically, that
          range is exactly where it breaks worst (768-900px is the peak, not
          a graceful degradation).
          For comparison: `NewsPanel.tsx`'s headline table also uses
          `.audit-detail` on a squeezable column, but with only 4 total
          columns (when/source/headline/possible-markets) rather than 9 —
          its "headline" column only narrows to 327px at 768px and the
          section height scales sanely (2,160px at 768 vs 1,683px at 1440).
          So this is not "every `.audit-detail` use is broken" — it is
          specifically the Detector Signals table's column count vs. its one
          flexible column.
Evidence: frontend/src/Trades.tsx:704-719 (nine `<th>`s, eight of them
          default `.table` nowrap, one `.audit-detail` on line 770)
          frontend/src/styles.css:1105-1111 (.audit-detail: no min-width, no
          height cap)
          frontend/src/styles.css:1386-1389 (the "no tablet tier" comment
          this contradicts, for this table)
          Measured via `getBoundingClientRect()` at 8 viewport widths (768
          through 1280), reproduced twice.
          Screenshots: docs/polish-audit/screenshots/tester-round2/
          14-REGRESSION-signals-table-broken-768.png (a representative
          scroll position: 06:53:41 undervalued_screener KXQUICKSETTLE-...
          floating alone against black)
Owner:    frontend. Two independent fixes would each resolve it: (a) give
          `.audit-detail` a `max-height` + `overflow: auto` when used inside
          this specific table, the same pattern already proven correct for
          `.audit-json` in the Audit Trail (a scrollable capped cell instead
          of an unbounded one), or (b) give the "why" column a `min-width`
          (e.g. 150px) so the browser is forced to let the table exceed
          `.table-scroll`'s container width and scroll horizontally instead
          of collapsing the one flexible column to near-zero. (a) is
          probably cheaper and matches an already-shipped precedent in the
          same file.
```

---

## Console/network hygiene (all pages, all viewports tested)

Zero console errors, zero pageerrors, zero failed (4xx/5xx) requests on every
page load performed this round:
- `/` (screener) at 1440x900, 1280x800, 768x1000, plus the ≤1¢ filter, plus a
  ticker search, plus 30 "next" clicks into the tail.
- `/trades` at 1440x900, 1280x800, 768x1000 (top-of-page and full-page).
- `/system` at 1440x900.
- `/market/KXMLB-26-WSH`, `/market/KXHORMUZNORM-26MAR17-B261101`,
  `/market/KXPRESNOMD-28-HBID` — each on 1m/1h/1d tabs, at 1440x900; the
  Hormuz market additionally at 768x1000 and 1280x800.

`Trades.tsx`'s new one-time `/api/system` fetch on mount produced no error and
no visible flash/layout shift; the detector note simply appears once the
response lands.

No `NaN`, `undefined`, raw epoch, or out-of-bounds 0–100 score was observed
anywhere.

---

## Not independently re-verified

- The backend-only facts the manager already confirmed by curl (crossed-book
  `liquidity_score: null` + `quote_crossed: true` on
  `KXHORMUZNORM-26MAR17-B261101`, the 66,547→~1,200 row count drop) were used
  as given per the brief; I checked how they *render* (done, throughout
  above) rather than re-deriving the backend numbers from scratch.
- `pytest`/`ruff`/`mypy` were not re-run — out of scope for this pass, which
  was UI-only per the brief.

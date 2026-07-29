# TESTER round 3 — live-browser verification of the frontend-only fix wave

Real Chromium (`kalshi-pw:latest`, `chromium_headless_shell-1148`) against the
running stack at `http://127.0.0.1:8080`. `docker compose ps` confirmed 5/5
healthy before testing. Every finding below came from actually driving the UI
and/or curling the live API — nothing is inferred from source alone unless
marked. No config edited, no order approved or placed, no interlock touched.

Screenshots referenced below are in
`docs/polish-audit/screenshots/tester-round3/`. Scratch scripts live under the
session scratchpad (`scripts/r3_*.mjs`, `shots/r3_*.png`).

**Bottom line: nothing is worse than before. Everything in scope is FIXED.**
No new P0/P1s. One small, genuinely optional design nudge on the fade
strength (see below), not a defect.

---

## 1. Scroll fade (UI-047) — FIXED, and now clearly visible

Round 2 measured the old fade as an 8-of-255 darkening — real but
imperceptible. The rebuild inverts it: a 2px `#333` hard rule (`--border-bright`,
RGB 51,51,51) at the true edge, plus a 20px `rgba(255,255,255,0.14)` wash
fading inward, painted over a `#101010` (RGB 16,16,16) panel.

**Pixel-sampled, not eyeballed.** I screenshotted `.table-scroll` elements at
their actual scroll position, decoded the PNG back through an in-page
`<canvas>`, and read `getImageData` across the edge on multiple body rows of
four different tables (Positions at 1440 — the only one with real overflow
at that width — and Detector Signals / Screener / Audit Trail at 768, where
narrower columns create real horizontal scroll). Consistent across all of
them:

```
dx from true edge:   0    1    2    3    4    5   ...  17   18   19   20+
pixel R value:       36   51   51   46   44   42  ...  18   17   16   16
```
(dx0 = the element's own 1px `--border` at 36; dx1-2 = the 2px hard rule at
51 = `#333` exactly; dx3 onward = the wash decaying from ~46 toward the
`#101010` background of 16.)

**Measured delta: ~35 levels (16→51), roughly 4x the old fade's 8-level
delta**, and unlike round 2, this one required no pixel-peeping to see —
it's visible at a normal screenshot zoom (see
`08-fade-zoom-detector-signals.png`, `07-fade-zoom-screener.png`).

**The show/hide logic also checked out.** At default scroll position
(`scrollLeft: 0`, i.e. scrolled to the start), the *left* edge of every
scrollable table read flat `16` (background, no rule, no wash) — the left
cover is correctly hiding the left marker since there's nothing to scroll to
on that side — while the *right* edge showed the full rule+wash, since
there's real content still to scroll to. This is the "cover slides over the
marker at rest" behavior the CSS comment describes, and it does what it
says.

**Confirmed sitting below the header, not spanning it.** Screenshotted the
transition directly: the header row (`TIME / DETECTOR / TICKER / ...`) has a
flat `--bg-raised` fill all the way to its right edge with no wash, and the
wash appears starting on the very next (body) row — see
`06-fade-header-vs-body.png`. This matches the CSS comment's stated intent
exactly and is not an accident of my crop; I deliberately scrolled the
table's own header row into frame below the sticky app topbar to check this.

**Judgement call, as asked: soft edge, not a smudge.** At this magnitude, the
wash reads as an intentional fade — it has a crisp anchor (the 2px rule)
that a viewer's eye lands on first, and the softer wash bridges gracefully
into the row background rather than looking like a dirty stripe. It's also
clearly distinct from the row-hover highlight (`--bg-raised` = 22, vs. the
wash's peak of ~46-51), so there's no risk of it reading as an accidental
"selected row." **I'd keep it at 0.14, not pull back to 0.10.** The whole
reason 0.14 was chosen was to have headroom over a fade that measurably
failed to register at 8/255; 0.14 clears that bar with room to spare without
tipping into looking gaudy. Pulling back to 0.10 buys nothing here and
reintroduces some of the same "will an operator on a real monitor actually
notice this" risk that sank the original. If it must move, I'd rather see it
proven too strong on a real desktop screen first.

Checked across the four `.table-scroll` sites that actually had scrollable
content in this session (Positions/1440, Detector Signals/768,
Screener/768, Audit Trail/768) — all four rendered the identical gradient
profile. The other tables at their tested widths had `scrollWidth ==
clientWidth` (nothing to scroll), so the "cover hides both markers, nothing
to see" state is correctly the only thing to check there, and that's what
rendered.

Note for whoever reads this next: the headless-Chromium overlay-scrollbar
quirk from round 2 (`::-webkit-scrollbar` reserving 0px) is unrelated to
this fix and unchanged — still can't be confirmed or denied without a real
windowed browser. Not re-litigating it here since it wasn't part of this
round's changes.

---

## 2. "no bid" flag (UI-050) — FIXED

Confirmed on a **fresh, unmanufactured page load** — no live-tick race
needed this time, which was the whole gap in round 2's PARTIAL verdict.

Searched to `APPLEUS-29DEC31` (confirmed via `GET /api/markets?q=...`:
`yes_bid: "0.000000"`, `liquidity_score: null`). Rendered row:
`— NO BID` with `score-cell` title exactly equal to `NO_BID_TITLE`:
> "No bid at any price: nobody will buy this from you right now, whatever
> the volume history says. This is why there is no attention score to
> show."

Screenshot: `09-nobid-flag-row.png`.

**Distinguished from the plain no-score case**, tested on
`KXHORMUZNORM-26MAR17-B261101` (`yes_bid: "0.620000"` — not zero, just
crossed, so `noBid` is correctly `false`): renders bare `—` with no flag,
title exactly `NO_SCORE_TITLE`:
> "No score. The server refuses to rank a market for attention unless both
> sides are being quoted inside (0, 1) — a populated 0.000000 is not a
> quote."

Screenshot: `10-noscore-crossed-row.png`. Both strings are accurate to what
each case actually means and are clearly distinguishable from each other —
no risk of confusing "nobody's bidding" with "the quote itself is
untrustworthy."

---

## 3. `.topbar-status` at 1440 (gap 16px → `8px 10px`) — FIXED, not cramped

Measured gap between adjacent pills: exactly 10px (column-gap), matching the
CSS. Visually (`02-topbar-1440-closeup.png`, `03-topbar-1440-full.png`) each
pill is its own bordered, colored box — kill switch in red, exchange/env/mode
in green — so the eye separates them by their borders and colors, not just
whitespace. 10px between distinct bordered chips at full width reads as
tight-and-tidy, not crowded. I would not change this.

At 768 the same cluster wraps as a single unit onto its own row below the
brand/nav row, right-aligned, with the OK pill fully inside the viewport
(measured `right: 752` against `window.innerWidth: 768` — no clipping).
Screenshot: `01-header-768-wrap.png`.

---

## 4. Detector Signals table at 768 (UI-054) — FIXED, and it looks right

This was round 2's lead P1 (a table exploding from ~5,400px to 52,913px
below ~1100px, with a 207-character rationale rendering at 41px wide, one
character per line). Re-measured this round:

- `.audit-detail` cell width at 768: **220px** (the fixed floor), same as the
  CSS comment describes.
- Table section height at 768: **5,680-5,736px** for 50 rows — in the same
  ballpark as the 1440/1280 layout (~5,400px), not the 52,913px collapse.
- The table now genuinely needs horizontal scroll at 768
  (`scrollWidth: 1024` vs `clientWidth: 700`) rather than squeezing its one
  flexible column — which is the intended fallback per the CSS comment
  ("scrolls sideways... instead of collapsing").

**Eyeballed, not just measured**: scrolled the `.table-scroll` horizontally
to bring the `why` column into view — rows are normal height, the rationale
text wraps over 4-6 lines and is fully readable, and critically **the
trailing caveat survives intact**: e.g. "...Score 99.0 is an ordering for
attention, not cents and not a probability." reads whole, not truncated.
Screenshots: `04-detector-signals-768-top.png` (vertical scroll, rows now a
sane height), `05-detector-signals-768-rationale.png` (horizontal-scrolled,
rationale column fully legible).

---

## 5. Regression sweep — clean across the board

**Console/network hygiene**, all three viewports (1440x900, 1280x800,
768x1000) × four surfaces (`/` screener, `/trades`, `/system`,
`/market/KXMLB-26-WSH`) = 12 page loads with `console`/`pageerror`/
`requestfailed`/`4xx-5xx` handlers attached throughout: **zero console
errors, zero pageerrors, zero failed requests, zero exceptions**, on every
single load.

```
markets@1440  bodyScrollHeight=2212      trades@1440  bodyScrollHeight=25080
markets@1280  bodyScrollHeight=2212      trades@1280  bodyScrollHeight=25080
markets@768   bodyScrollHeight=2344      trades@768   bodyScrollHeight=27382
system@*      bodyScrollHeight=2076-2365 market-mlb@* bodyScrollHeight=2665-3273
```

Round 1/2 items specifically re-checked live this round, all still correct:

- **`crossed`/`one-sided` flags**: 11 `.crossed-flag` elements on the default
  screener view at 1440, rendering `crossed` beside negative-spread rows
  exactly as before. Screenshot: `13-markets-1440-attention-crossed.png`
  (also shows the ATTENTION column's `—` for every crossed row, ✓ matches
  the manager's given finding).
- **Gated detector copy**: "detectors are enabled" text confirmed present on
  `/trades`.
- **Folded Recent Decisions**: 10 `.repeat-note` elements found, e.g. "×10
  identical — same market, same source, same outcome, since 19:34:43...".
- **Pretty-printed audit JSON**: `.audit-json` renders indented, multi-line
  (`{\n  "proposal_id": 1348\n}`).
- **1d chart tab**: still renders "No candles for this window. Kalshi
  returns none for markets that have never traded." at 200, no 422.
  Screenshot: `11-chart-1d-still-fixed.png`.
- **Positions `real. (gross)` header + fees column**: both headers present
  verbatim (`"real. (gross)"`, `"fees paid"`) in the live DOM.

Full-page screenshots of all three viewports for Markets, Trades, System,
and a market detail page are in the screenshots folder
(`12-market-page-768.png`, `14-markets-768.png`, `15-system-768.png`, etc.)
for anyone who wants to eyeball beyond what's called out above. Nothing in
them looked wrong.

---

## Verdict summary

```
UI-047  scroll fade (rebuilt, lighten not darken)        FIXED — visible, reads as intentional, keep at 0.14
UI-050  "no bid" flag reachable on fresh load            FIXED
topbar  .topbar-status gap 16px -> 8px/10px at 1440      FIXED — not cramped
UI-054  Detector Signals table at 768                    FIXED — looks right, not just measures right
Regression sweep (12 page/viewport combos)               CLEAN — 0 console errors, 0 failed requests, 0 regressions
```

No new findings this round. This is an honest "done" — I looked for reasons
to file something under each item and didn't find one worth writing up.

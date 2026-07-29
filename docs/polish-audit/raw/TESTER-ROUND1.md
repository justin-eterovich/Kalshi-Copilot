# TESTER round 1 — live-browser verification

Real Chromium (headless_shell, `kalshi-pw:latest`) against the running stack
at `http://127.0.0.1:8080`, HEAD `cb6308e`, image built ~1h before this pass
(after all polish-audit fix commits, including `bc47c1d`). Every finding below
was produced by driving the actual UI and/or curling the actual API — nothing
here is inferred from source alone unless explicitly marked.

Screenshots referenced below are copied to
`docs/polish-audit/screenshots/tester-round1/`. Full-resolution originals and
scratch scripts live under the session scratchpad
(`scripts/*.mjs`, `shots/*.png`) if anyone needs to re-run a check.

No `docker compose` state was touched. One harmless manual proposal was
created via the trade ticket (KXHORMUZNORM-26MAR17-B261101, 1 contract, buy
YES @ 0.34, rationale "TESTER round1 UI audit - do not approve") to verify the
ApprovalCard renders correctly; it was **not** approved or rejected and was
left to expire on its own TTL. No kill switch, autonomy arm/disarm, or
approve/reject control was clicked.

---

## Verified fixed, not re-filed

Confirmed via live DOM/API, so these are dropped from the backlog rather than
re-reported:

- **UI-004** (wire form on approval card) — confirmed. Buy NO ticket shows
  `sends as ask 10 @ 0.620000` for a NO buy at limit 0.38; sell YES shows
  `ask 10 @ 0.620000`; buy YES shows `bid 10 @ 0.340000`. Matches CLAUDE.md's
  direction table exactly. See `05-approval-card-wire-form-fixed.png`.
- **UI-008** (no worst-case on approval card) — confirmed fixed. Card shows
  PRICE / SIZE / MAX LOSS / FEE / NET EDGE / BANKROLL.
- **UI-001/002 fee rounding** — confirmed fixed on the approval card: a 1
  contract @ 0.34 order shows `fee 1.58¢`, not rounded to a whole cent, and
  matches the taker fee formula (`0.07 × 0.34 × 0.66 × 1 × 100 ≈ 1.57¢`).
- **UI-011** (Settlements missing `className="table"`) — confirmed fixed in
  source, `Trades.tsx:478`, with a comment marking the fix.
- **UI-033** (realised P&L of 0 rendered green) — confirmed fixed in source:
  `pnlClass()` (`Trades.tsx:49`) gives zero its own neutral class, not `up`.
- **UI-035** (ScoreBar clamps only the top) — confirmed fixed: `Screener.tsx`
  `ScoreBar` now does `Math.max(0, Math.min(100, score))`.
- **UI-017** (Engine panel says "no feed" on a row showing a price) — not
  reproducible. BTC-USD row shows `$63,928.64` / `fresh`; ETH/SOL/XRP
  correctly show `— / no feed — markets refused`.
- **UI-019** (News panel contradiction) — not reproducible; current copy
  explicitly explains headlines are collected-but-unscored while triage is
  off, doesn't contradict itself.
- **UI-022** (System page says M7-M9 not done) — not reproducible; Build
  Progress shows all of M0-M10 checked.
- Old audit's assumption that only the ladder shows live/cached provenance —
  not reproducible as stated; header, order book, and tape each show their
  own status independently (`SOCKET LIVE` / `live`↔`cached` / `POLLED · 5s`).

---

## P1

```
[P1] UI-041 — The screener's "max spread" filter passes crossed (negative-spread) books through every threshold, including the tightest
Repro:    GET /api/markets?max_spread=0.01&limit=100  (also reproduced by hand:
          Screener -> spread filter -> "≤ 1¢")
Expected: "≤ 1¢" should return only markets whose bid/ask are genuinely within
          1¢ of each other — the whole point of the filter is to surface
          tradeable, liquid quotes.
Actual:   14 of 100 rows returned have a NEGATIVE spread as steep as -2.1¢
          (KXPRESNOMD-28-KH: yes_bid 0.098, yes_ask 0.077, spread -0.021),
          i.e. more crossed/stale than most of the catalog, yet they pass a
          "≤ 1¢" filter because the SQL is a raw `<=` with no `abs()` or floor
          at 0. An operator using this filter to hunt for genuinely liquid
          markets is served the least trustworthy quotes in the catalog first.
Evidence: backend/app/api/routes/markets.py:208-211
          ("if max_spread is not None: ... (Market.yes_ask - Market.yes_bid) <= Decimal(str(max_spread))")
          curl "http://127.0.0.1:8080/api/markets?max_spread=0.01&limit=100" ->
          14/100 rows with spread < 0, e.g. KXPRESNOMD-28-HBID spread -0.009
          UI repro: docs/polish-audit/screenshots/tester-round1/02-maxspread-filter-crossed-books.png
          (row KXPRESNOMD-28-HBID visible with SPR -0.9 under the "≤ 1¢" filter)
Owner:    backend
```

```
[P1] UI-042 — The "1d" chart tab is broken on every market: the frontend requests a lookback the API always rejects
Repro:    Open any market page, click the "1d" tab. Or directly:
          curl "http://127.0.0.1:8080/api/markets/KXMLB-26-WSH/candles?period_sec=86400&lookback_hours=4320"
Expected: the daily candle view loads like 1m/1h do.
Actual:   HTTP 422: {"detail":[{"type":"less_than_equal","loc":["query","lookback_hours"],
          "msg":"Input should be less than or equal to 2160","input":"4320", "ctx":{"le":2160}}]}.
          The frontend always sends lookback_hours = 24*180 = 4320 for the "1d"
          period; the API caps lookback_hours at 2160 (openapi.json). Every
          click on "1d", for every market, hits this and shows "Could not load
          candles." even though candle data genuinely exists (confirmed via the
          same endpoint with a valid lookback).
Evidence: frontend/src/MarketPage.tsx:22-26
          ( PERIODS = [{1m,60,12}, {1h,3600,336}, {1d,86400,4320}] )
          frontend/src/MarketPage.tsx:104-107 (catch -> setChartNote("Could not load candles."))
          openapi.json: /api/markets/{ticker}/candles lookback_hours max=2160
          Screenshot: docs/polish-audit/screenshots/tester-round1/04-chart-1d-could-not-load.png
Owner:    either (cheapest fix is frontend: cap the "1d" lookback at <=2160h/90d;
          alternatively raise the backend cap if 180d of daily bars is wanted)
```

```
[P1] UI-043 — Positions' realised P&L column is gross of fees, not net, and the fees column needed to correct it is clipped off-screen by default
Repro:    Load /trades at 1440x900. Look at the Positions panel (left column
          of the two-up split under Working Orders).
Expected: hard constraint #5 in CLAUDE.md: "Every edge/EV number shown
          anywhere must be net of fees. A gross edge is a lie." The report
          card panel on the same page follows this (backend nets fees into
          `total_pnl_cents`: backend/app/backtest/report.py:434,
          `entry["pnl"] += realized - fee_cents`).
Actual:   Positions' "real." column is `realized_pnl_cents`, which
          backend/app/trading/positions.py's `realized_from_fill` computes
          from price movement only and never subtracts fees. The frontend
          added a "fees" column with the honest tooltip "already spent —
          subtract it from realised" (Trades.tsx:359) — i.e. it openly says
          the number the operator is looking at needs manual arithmetic to
          become the real figure. But that fees column is the 7th column in a
          7-column table inside a 492px-wide scroll container holding a 562px
          table (measured via DOM: scrollDiv.clientWidth=492,
          table.scrollWidth=562) — it is clipped with no visible scrollbar in
          the rendered page. An operator sees "real. $0.00" (as in the live
          KXMLB-26-WSH / KXMLB-26-HOU rows) with no on-screen indication that
          a fee needs subtracting or what it is.
Evidence: backend/app/trading/positions.py:41-96 (realized_from_fill never
          touches fee_cents)
          backend/app/backtest/report.py:433-434 (contrast: report card nets
          fees the report card already does)
          frontend/src/Trades.tsx:340-394 (Positions table, the fees <th> and
          its tooltip)
          DOM measurement: Positions .table-scroll clientWidth=492 vs
          table scrollWidth=562 -> 70px clipped, which is exactly the fees
          column
          Screenshot: docs/polish-audit/screenshots/tester-round1/06-positions-fees-column-clipped.png
          (crops at the true rendered edge of the container; no fees column,
          no scrollbar visible)
Owner:    frontend (render an already-netted figure, or at minimum stop
          clipping the column that makes the honest number reachable);
          backend could also expose a pre-netted `realized_pnl_net_cents` so
          no client has to remember to subtract
```

---

## P2

```
[P2] UI-044 — Negative ("crossed") spreads on the screener carry no visual signal that the quote is stale, not tight
Repro:    Load /, note SPR column values like -0.9, -1.1, -5.3 (e.g.
          KXPRESNOMD-28-HBID, KXHORMUZNORM-26MAR17-B261101).
Expected: given CLAUDE.md's own documentation that crossed books come from
          ticker partial-snapshots and are "real" but represent a stale quote,
          not free money or extra liquidity, an operator scanning a screener
          full of numbers has no way to tell "-0.9" apart from a genuinely
          tight, tradeable "0.1" spread except by noticing the minus sign.
Actual:   Both render identically — plain white/green text, no color, no
          asterisk, no tooltip. This is confirmed as real backend data, not a
          frontend miscalculation: curl shows KXPRESNOMD-28-HBID yes_bid=0.018,
          yes_ask=0.009, spread=-0.009, and the backend's own
          `_liquidity_score` has a comment acknowledging this ("Crossed or
          locked books are real — a stale quote leaves bid >= ask").
          Verdict on the brief's specific examples:
            - KXHORMUZNORM-26MAR17-B261101 (bid 62.0/ask 34.0/spr -28.0): real
              backend data, correctly computed by the API from stored
              yes_bid/yes_ask. Not a display bug.
            - The several -0.9/-1.1/-5.3 rows: same — all confirmed via
              /api/markets as genuine stored (yes_ask - yes_bid) values.
          So: correct-and-ugly at the backend layer (by design, per the code's
          own comment), but the frontend does nothing to distinguish
          "negative spread" from "tight spread" for the operator, which is a
          real gap given #UI-041 above shows the spread *filter* also treats
          them as equally good.
Evidence: curl "http://127.0.0.1:8080/api/markets?q=KXPRESNOMD-28-HBID" ->
          yes_bid":"0.018000","yes_ask":"0.009000","spread":"-0.009000"
          backend/app/api/routes/markets.py:116-119 (comment + clamp)
          Screenshot: docs/polish-audit/screenshots/tester-round1/01-screener-negative-spreads.png
Owner:    frontend (color/flag a negative spread distinctly, e.g. same muted
          treatment already used elsewhere for "not a real number")
```

```
[P2] UI-045 — The market-page headline quote and the order-book ladder can disagree on best bid for the same market at the same instant, with no reconciliation
Repro:    Open /market/KXHORMUZNORM-26MAR17-B261101.
Expected: the big headline price and the order book directly below it should
          describe the same tradeable market consistently (or the UI should
          flag when they diverge).
Actual:   Header shows "62.0 / 34.0 ¢ bid/ask" (from Market.yes_bid/yes_ask,
          the ticker-derived row). The order book ladder directly below,
          fetched from the full L2 snapshot, shows best bid 56.0¢ / best ask
          34.0¢ — the bid disagrees by 6¢. Both are labelled as current on
          the same page load; nothing tells the operator which one is
          tradeable. (The ask side agrees between both sources here, so it's
          specifically the bid/no_ask side that's at risk of being wrong.)
          This is the same root cause as UI-044: two different ingestion
          paths (ticker stream vs full-book REST) can genuinely diverge, and
          nothing surfaces the divergence.
Evidence: curl "http://127.0.0.1:8080/api/markets/KXHORMUZNORM-26MAR17-B261101"
          -> yes_bid":"0.620000","yes_ask":"0.340000"
          curl ".../orderbook" -> best "yes" level "0.5600" (56.0c); best
          implied ask from "no" book's top level 0.6600 -> 1-0.66=0.34
          (matches). Bid does not match (0.62 vs 0.56).
          The NO-buy default limit price in the trade ticket (0.38, i.e.
          1 - yes_bid) is seeded from the disagreeing headline field, not the
          live book.
Owner:    backend (reconcile the two sources, or the API should not present
          Market.yes_bid as equally "current" when it is 6c off the live book)
```

```
[P2] UI-046 — Three copies of "detectors ship disabled" contradict the same page's own live detector state
Repro:    Load /trades with an empty approval queue and an empty detector
          signals table; also load /system.
Expected: copy that reflects reality — /api/system already reports
          enabled_detectors as 6 names (set_arbitrage, stale_quote,
          resolution_sniper, undervalued_screener, whale_flow,
          longshot_calibration), and the Trades page's own "Recent Decisions"
          panel is visibly full of live stale_quote proposals expiring by TTL
          right next to this text.
Actual:   Three unconditional strings all say detectors are off:
            - Trades.tsx:269 "Detectors ship disabled — enable them one at a
              time in config.yaml..." (empty approval queue state)
            - Trades.tsx:541 "Nothing yet. Detectors ship disabled; enable one
              at a time in config.yaml..." (empty detector-signals state) —
              this one is *usually* not shown because /api/signals is not
              actually empty (4800+ live rows from undervalued_screener
              confirmed via curl), but the string is still wrong if it ever
              does render, e.g. after a restart before signals accumulate.
            - SystemPanel.tsx:212 "...every detector ships disabled..." — this
              one is unconditional and always renders, directly beneath the
              System page's own Detectors panel showing all six as ON.
          SystemPanel.tsx even has the correct pattern nearby (line 182-185,
          "None enabled..." gated on the actual list being empty) — the
          author clearly knew how to do this and just didn't apply it to the
          footnote.
Evidence: frontend/src/Trades.tsx:266-272, 539-543
          frontend/src/SystemPanel.tsx:179-186 (the correctly-gated sibling),
          211-215 (the unconditional stale copy)
          curl http://127.0.0.1:8080/api/system -> enabled_detectors: 6 names
          Screenshots: docs/polish-audit/screenshots/tester-round1/
          11-trades-approval-empty-state.png, 10-system-page.png
Owner:    frontend
```

```
[P2] UI-047 (downgrade of old UI-024) — No @media queries exist, but the layout mostly survives 768px anyway; the exceptions are real
Repro:    Resize to 1280x800 and 768x1000 for /, /market/:ticker, /trades.
Expected: per the brief, layout breakage at narrow widths is in scope.
Actual:   1280x800: no issues found on any of the three pages.
          768px: the app does NOT rely on @media queries (confirmed zero in
          styles.css, matching the old finding), but flexbox + existing
          `.table-scroll { overflow-x: auto }` wrapping already absorb most of
          the narrowing:
            - No page-level horizontal scrollbar on any page (scrollWidth
              769 vs clientWidth 768 — 1px, negligible).
            - Nav bar: "demo exchange" pill wraps to two lines inside its own
              border; ugly but fully contained, nothing clipped.
            - Trades page stacks cleanly; stat tiles wrap to 2 lines
              ("ROUTE: demo / exchange") and stay legible.
          The real, remaining problem: the screener table and several trades
          tables (Positions among them, see UI-043) hide their rightmost
          columns behind an invisible horizontal scroll with **no affordance**
          — no visible scrollbar, no fade edge, nothing hinting more columns
          exist. At 768px the screener hides Liquidity and Closes entirely
          behind that scroll.
Evidence: styles.css has zero `@media` (grep confirmed)
          Screenshots: docs/polish-audit/screenshots/tester-round1/
          07-screener-768w.png, 08-trades-768w.png, 09-navbar-768w.png
Owner:    frontend (a scroll-shadow/fade affordance on `.table-scroll` would
          fix this cheaply for every table at once, including UI-043's fees
          column)
```

```
[P2] UI-048 — Uncaught RangeError blanks the entire price chart in this environment; the app has no defensive fallback for a locale toLocaleString() can't parse
Repro:    Open any market page, any of the 1m/1h/1d tabs, with console/
          pageerror listeners attached.
Expected: the chart renders candlesticks and axis labels (confirmed the API
          candle data is present and valid, e.g. KXMLB-26-WSH 1m has real
          rows).
Actual:   Two uncaught pageerrors fire on every load:
          "Incorrect locale information provided" / RangeError, thrown inside
          the bundled `lightweight-charts` library's tick-mark formatter
          (`Date.toLocaleString()` internally), stack: formatTickmark ->
          ... (assets/index-*.js). The canvas renders completely blank — no
          candles, no gridlines, no axis labels, on 1m/1h despite valid data,
          and on 1d additionally shows "Could not load candles" (separate
          cause, see UI-042).
          Root cause traced to this test container: `navigator.language`
          resolves to "en-US@posix" because the container has no LANG set at
          all (`locale` inside kalshi-pw:latest reports LC_*=POSIX, LANG=
          empty) — Chromium falls back to a non-standard tag Intl/toLocaleString
          rejects. This is very likely a harness artifact, not something a
          real operator's desktop Chrome would produce (real installs report
          clean tags like "en-US"), so I am NOT filing this as a P1 general
          chart bug. But the underlying issue — the chart calls
          `Date.toLocaleString()` with no locale argument and no try/catch —
          is a real robustness gap: one bad locale string takes out the
          entire visualization silently, with zero on-page indication
          (nothing in the DOM says the chart failed; only devtools shows it).
Evidence: PAGEERROR: Incorrect locale information provided
          RangeError: Incorrect locale information provided at Date.toLocaleString
          navigator.language in the test container: "en-US@posix"
          `locale` inside kalshi-pw:latest: LANG=(empty), all LC_*=POSIX
          frontend/src/PriceChart.tsx (createChart call has no `localization`
          option set, so lightweight-charts falls back to browser locale
          detection)
          Screenshot: docs/polish-audit/screenshots/tester-round1/03-chart-blank-1m.png
Owner:    frontend (cheap defensive fix regardless of root cause: pass an
          explicit `localization: { locale: "en-US" }` to `createChart`
          rather than relying on implicit browser locale detection)
```

```
[P2] UI-049 — Detector re-proposes the same BTC markets every ~2 minutes for hours, filling "Recent Decisions" with hundreds of near-identical expired cards
Repro:    Load /trades, scroll the Recent Decisions panel.
Expected: this is the exact "re-proposal loop" hazard CLAUDE.md documents
          under Autonomy ("detector proposes -> ... -> re-derives the same
          edge on its next scan -> propose again, repeatedly") — so it is
          *expected behaviour*, not a new bug. Filing this only because it has
          a real, measurable UI cost: it makes /trades a 24,000+ px page (29
          screenshots to scroll through it), most of which is the same handful
          of KXBTCD-* stale_quote proposals repeated every ~2 minutes for over
          an hour, each an identical-looking card.
Actual:   Confirmed as expected/by-design (CLAUDE.md: "This hazard is
          invisible from any single file, which is why it is written down
          here" / mitigated only under autonomy via repeat_cooldown_sec,
          which does not apply here since nothing is auto-approving). Not
          re-filing the hazard itself. The only new observation: Recent
          Decisions has no collapse/grouping for "same ticker, same detector,
          repeated," which would make the page navigable without changing
          any safety behaviour.
Evidence: Screenshots: docs/polish-audit/screenshots/tester-round1/ trades_sec
          sequence (available in scratchpad if needed)
Owner:    frontend (pure display grouping, zero interlock risk)
```

---

## P3

```
[P3] UI-050 — KXGOVAK-26-NDAH: 98c spread scores 50.5/100 "liquidity" — correct per formula, but the label overpromises on a market with literally no bid
Repro:    curl "http://127.0.0.1:8080/api/markets?q=KXGOVAK-26-NDAH"
Verdict:  Arithmetically correct, not a bug. yes_bid=0.000000 (no buyer at any
          price), yes_ask=0.980000. _liquidity_score (markets.py:104-132):
          spread_score = 1/(1+0.98*100) = 0.01002 (weight 0.5);
          volume_24h=1,811,110.93 -> volume_score = min(1, sqrt(181.1)) = 1.0
          (weight 0.3); open_interest=8,995,306.04 -> oi_score = 1.0 (weight
          0.2). raw = 100*(0.5*0.01002 + 0.3*1 + 0.2*1) = 50.5 — matches the
          displayed score exactly.
          This is by design: volume/OI (0.5 combined weight) can fully
          saturate a score even when the current book is completely
          one-sided. Reasonable as an "attention ranking" (which is exactly
          what the code's docstring calls it — "ranks candidates for
          attention; it does not price anything"), but the axis is literally
          labelled "LIQUIDITY" on the screener, and 50/100 next to a market
          with zero executable bid reads as "medium liquidity" to an operator
          skimming the column, not "no one will buy this from you right now."
Evidence: backend/app/api/routes/markets.py:104-132
          curl output: yes_bid 0.000000, liquidity_score 50.5
Owner:    frontend (a title/tooltip distinguishing "attention score" from
          "current tradability" would resolve this without touching the
          formula, which is intentional)
```

```
[P3] UI-051 — Audit trail's detail column is complete but unformatted raw JSON
Repro:    /trades, scroll to Audit trail.
Verdict:  Not filing as a defect to reverse — `Trades.tsx`'s `.audit-detail`
          class deliberately removed truncation after a prior bug ate the
          caveat at the end of a rationale string (styles.css:948-952 explains
          this explicitly: "the same class truncated the audit trail's
          payload column, where the whole point is a record you can read").
          Completeness over brevity is the right call. The only residual nit:
          the payload is `JSON.stringify(entry.payload)` with no pretty
          printing, so a record like a 5-leg proposal's wire form renders as
          one dense unbroken line the operator has to visually parse
          character by character. A `JSON.stringify(payload, null, 2)` in a
          `<pre>`, or a collapsible `<details>`, would keep everything visible
          while being far more scannable — same content, easier to read.
Evidence: frontend/src/Trades.tsx:664-666
Owner:    frontend
```

---

## Console/network hygiene (all pages, all three viewports)

- Zero console errors and zero failed (4xx/5xx) network requests on: `/`
  (screener), `/trades`, `/system`, and multiple `/market/:ticker` pages,
  across 1440x900, 1280x800 and 768-wide, **except** the two
  `Incorrect locale information provided` pageerrors on every market-page
  load (UI-048, likely harness-specific).
- No `NaN`, `undefined`, `null`, or raw epoch timestamps observed anywhere in
  rendered text across screener, market page, trade ticket, positions, fills,
  settlements, report card, engine panel, news panel, or system page.
- No 0-100 bounded scale (liquidity score, chart price axis) was observed
  outside its bounds anywhere tested.

---

## Not filed (checked, ruled out)

- Considered whether the screener's `sort` dropdown could rank crossed
  spreads as "best" — there is no "sort by spread" option (only volume, OI,
  liquidity, close time, price, newest), so this specific risk doesn't exist;
  only the `max_spread` filter (UI-041) is affected.
- Considered filing the KXHORMUZNORM 8-leg "Same event" sibling table
  (visible on that market's page) for a horizontal-scroll problem (old
  UI-025) — at 1440px it fit without scrolling; did not have a screenshot at
  768px for this specific market to make a confident call either way, so
  leaving UI-025 as still open/unverified at narrow widths rather than
  claiming it fixed or broken.

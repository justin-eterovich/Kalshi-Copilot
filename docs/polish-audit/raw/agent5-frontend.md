# Agent 5 — Frontend rendering, number-language & UX audit

Area prefix: **UI**. Worktree: `/home/justin/kalshi-copilot/.claude/worktrees/polish-audit`.
Method: complete read of all 19 files under `frontend/src/` (4,174 lines), traced against
**real payloads from the live API** at `http://127.0.0.1:8080`, plus verification against
the **shipped bundle** (`/assets/index-BUjuHosI.js`, 390,395 bytes) where "what actually
runs" mattered. No browser was available: rendering-dependent items are marked
`UNVERIFIED — needs browser` and say exactly what to look at.

**Counts: 0 P0 · 5 P1 · 24 P2 · 6 P3.**

No P0. The backend money math is correct everywhere I could check it (`/api/proposals/quote`
returns `est_fee_cents: "1.7500"` for one contract at 50c — the centicent rule holds). Every
money defect below is at the **display boundary**, in the browser.

---

## 0. The cross-component number-language table

Every row was produced by feeding a **real live API value** through the actual formatter.
Rows marked ← are defects filed below.

### Prices

| Where | Code | Live input | Renders |
|---|---|---|---|
| Screener bid/ask/last/spread | `centsNum(x)` dp=1 | `"0.004000"` | `0.4` — no ¢ symbol |
| MarketPage headline quote | `centsNum(x)` dp=1 + `¢` | `"0.480000"` | `48.0¢` |
| MarketPage bid/ask, siblings | `centsNum(x)` dp=1 | `"0.470000"` | `47.0` |
| Ladder, YES side | `Number(p)*100`, `.toFixed(1)` | `"0.3700"` | `37.0` |
| Ladder, NO side (inverted) | `(1-Number(p))*100` | `"0.5300"` | `47.0` ← |
| Tape price | `centsNum(x)` dp=1 | `"0.480000"` | `48.0` |
| Chart | `Number(v)*100`, precision 1 | `"0.490000"` | `49.0` |
| Ticket breakeven | `Number(x).toFixed(2)` + `¢` | `"31.4700"` | `31.47¢` |
| Approval leg price | `centsNum(x, 2)` + `¢` | `"0.355000"` | `35.50¢` |
| Working orders price | `centsNum(x, 2)` | `"0.355000"` | `35.50` — no ¢ |
| Positions avg | `centsNum(x, 2)` | `"0.530000"` | `53.00` — no ¢ |
| Fills price | `centsNum(x, 2)` | `"0.005000"` | `0.50` |
| Settlements avg / paid | `asCents(x)` dp=1 + `¢` | `"0.530000"` | `53.0¢` |
| Engine reference feed | **raw string** | `"65264.80500000"` | `65264.80500000` ← |

Four different decimal counts and two different unit-symbol conventions for the same
concept. Sub-cent survives everywhere (`"0.005000"` → `0.5` / `0.50`) because the
finest live tick is 0.001 dollars; dp=1 would round a 0.0001 tick, which does not exist
in this catalog today.

### Counts

| Where | Code | Live input | Renders |
|---|---|---|---|
| Screener 24h vol / OI | `asCount` | `"28401036.24"` | `28.4M` |
| Tape size | `asCount` | `"484.69"` | `485` ← rounds |
| MarketPage 24h vol / OI | `asCount` | `"258546417.02"` | `258.5M` |
| Ladder size | `.toLocaleString()` | `"422805.58"` | `422,805.58` |
| Approval leg size | **raw string** | `"5.00"` | `5.00` |
| Positions size | **raw string** | `"12.00"` | `12.00` |
| Fills size | **raw string** | `"1.00"` | `1.00` |
| Working orders | **raw** `filled/total` | `"0.00"/"5.00"` | `0.00/5.00` |
| Settlements held | **raw, signed** | `"-12.00"` | `-12.00` ← |

Three conventions: compacted (`asCount`), grouped (`toLocaleString`), and raw. The trade
rail is entirely raw; the research rail is entirely formatted.

`asCount` probe against the brief's test values: `"10.50"`→`11`, `"0.50"`→`0.50`,
`"1.00"`→`1`, `"0"`→`0`, `"484.69"`→`485`. Nothing renders as `0` — the historical bug is
gone — but see UI-006.

### Money (fees / P&L / cost)

| Where | Code | Live input | Renders |
|---|---|---|---|
| Ticket cost / total / max win / max loss | `asDollars` | `"314.7000"` | `$3.15` |
| **Ticket fee** | `asDollars` | `"1.7500"` | **`$0.02`** ← |
| Ticket net edge | `asSignedCents` dp=2 | `"2.7500"` | `+2.75¢` |
| Approval fee | `asDollars` | `"16.040000"` | `$0.16` |
| Approval net edge | `asSignedCents` dp=2 | `"-3.2080"` | `-3.21¢` |
| **Fills fee** | `asDollars` | `"0.040000"` | **`$0.00`** ← |
| Positions unreal / real | `asDollars` | `"-6.00000000"` | `-$0.06` |
| **Positions fees paid** | *never rendered* | `"20.880000"` | — ← |
| Settlements P&L | `asSignedCents` dp=2 | — | `+0.00¢` |
| Signals net edge | `asSignedCents`, `0`→`—` | `"0.0000"` | `—` |
| ReportCard claimed / realised / CI | `asSignedCents` dp=2 | `"-10.4400"` | `-10.44¢` |
| **ReportCard total / max dd** | `asDollars` | `"-0.250000"` | **`-$0.00`** ← |
| **ReportCard fees paid** | *never rendered* | `"0.250000"` | — ← |
| Risk meters | `asDollars` | `"639.30000000"` | `$6.39` |
| Trades balance | `Number(x).toFixed(2)` | `"80.1232"` | `$80.12` |
| News LLM spend | `asDollars(String(Number(x)*100))` | `"2.0"` | `$2.00` |

**Two money languages coexist and the split is not principled**: anything routed through
`asDollars` is truncated to whole cents; anything routed through `asSignedCents` keeps
centicents. Inside the *same seven-row quote box* the fee is `$0.02` and the net edge is
`+2.75¢`.

### Every `Number(` / arithmetic site, classified

Grep: `frontend/src/**` — 24 sites, no `parseFloat`, no `parseInt`, no unary `+` on money.

**Produces a displayed money number (the P1 class):**
- `api.ts:683,691` `asCents`/`centsNum` — `Number(dollars) * 100`
- `api.ts:725` `asDollars` — `Number(cents) / 100`
- `api.ts:737` `asSignedCents` — `Number(cents)`
- `api.ts:704` `asCount` — `Number(count)`
- `TradeTicket.tsx:280` `Number(quote.breakeven_cents).toFixed(2)`
- `Trades.tsx:197` `Number(state.balance.dollars).toFixed(2)`
- `NewsPanel.tsx:171-172` `asDollars(String(Number(usd) * 100))` — double float round-trip
- `RiskPanel.tsx:85` `lossUsed = String(-netCents)` → fed to `asDollars` for display
- `OrderBookLadder.tsx:21` `(1 - Number(p)) * 100` → `.toFixed(1)` displayed

**Layout / chart-geometry only (acceptable):**
- `Screener.tsx:39` `Math.min(100, score)` bar width
- `RiskPanel.tsx:24,43,46` `num()` for meter width and sign tests
- `EnginePanel.tsx:30` calibration meter width
- `OrderBookLadder.tsx:56,78` depth-bar width, `Math.max` scaling
- `PriceChart.tsx:128,141` chart series values
- `TapeView.tsx:23,39` median for the "big print" highlight
- `ApprovalCard.tsx:34,130,163` countdown and colour class
- `Trades.tsx:326,337,450,514,517` colour class / zero test only

**Verdict on the number-language thesis:** the top-of-file comment in `api.ts:1-6`
("arithmetic does not happen here at all") and at `api.ts:676-678` ("none of it becomes a
number before display") are **both false of the code directly beneath them**. Every price,
fee and P&L in this dashboard is parsed into a JS `number` before it is shown. In practice
the precision that matters is lost not to IEEE-754 but to `toFixed(2)` on a dollars value
(UI-001/003) — but the guarantee the codebase claims does not hold.

---

## P1 findings

```
[P1] UI-001 — Every fee in the UI is rounded to a whole cent; the canonical 1.75¢ fee displays as $0.02, and a real 0.04¢ fee displays as $0.00
Repro:    curl -s -X POST http://127.0.0.1:8080/api/proposals/quote -H 'Content-Type: application/json' \
            -d '{"ticker":"KXNFLGAME-26AUG15DALSEA-SEA","side":"yes","action":"buy","limit_price":"0.50","contracts":"1"}'
          curl -s 'http://127.0.0.1:8080/api/fills?limit=3'
Expected: CLAUDE.md "The units trap": "One contract at 50c costs 1.75c, not 2c... Nothing
          here is a whole number of cents, including fees." The schedule rounds to a
          CENTICENT ($0.0001). The whole codebase exists to preserve this.
Actual:   The backend returns est_fee_cents "1.7500" exactly right. TradeTicket renders it
          with asDollars: 1.75/100 = 0.0175 -> toFixed(2) -> "$0.02". The UI displays
          precisely the number CLAUDE.md says is wrong, in the one place the operator
          reads a fee before committing.
          Worse on the Fills table: all three live fills have fee_cents "0.040000" and
          "0.130000"; asDollars gives "$0.00" for both. A real, billed, non-zero fee
          renders as zero.
Evidence: frontend/src/api.ts:723-729 (asDollars: Number(cents)/100, toFixed(2))
          frontend/src/TradeTicket.tsx:273-276  (fee row)
          frontend/src/Trades.tsx:379           (fills fee column)
          frontend/src/ApprovalCard.tsx:151-155 (approval fee stat)
          Live: est_fee_cents "1.7500"; fills fee_cents "0.040000", "0.130000"
Fix sketch: fees are a cents quantity; format them with asSignedCents-style cents
          precision (e.g. "1.75¢"), never asDollars.
```

```
[P1] UI-002 — fees_paid_cents is never rendered anywhere in the application
Repro:    grep -c fees_paid_cents on the shipped bundle -> 0
          curl -s http://127.0.0.1:8080/api/positions
Expected: CLAUDE.md hard constraint #5: "Every edge/EV number shown anywhere must be net
          of fees and slippage. A gross edge is a lie."
Actual:   PositionRow.fees_paid_cents (api.ts:476) and DetectorReport.fees_paid_cents
          (api.ts:430) are declared, returned by the API, and rendered by nothing.
          The live NFL position shows unrealized "-$0.06" / realized "$0.00" while
          fees_paid_cents is "20.880000" — 20.88c already spent, invisible. The operator
          reads a position that is down 6/100ths of a cent when it is down ~21c.
          The report card's fees column is likewise absent from the table.
          The one place fees are surfaced honestly is RiskPanel's daily-loss hint
          ("$0.00 realised less $0.21 of fees") — proof the pattern was understood.
Evidence: frontend/src/api.ts:430,476 (declared)
          frontend/src/Trades.tsx:299-307 (positions <thead>: ticker/side/size/avg/unreal./real. — no fees)
          frontend/src/ReportCard.tsx:120-132 (<thead>: no fees column)
          curl /assets/index-BUjuHosI.js | grep -c fees_paid_cents  ->  0
          Live position: {"net_contracts":"-12.00","unrealized_pnl_cents":"-6.00000000",
                          "realized_pnl_cents":"0","fees_paid_cents":"20.880000"}
Fix sketch: add a fees column to Positions and to the report card table.
```

```
[P1] UI-003 — The report card renders a real loss as "-$0.00" and a real drawdown as "$0.00"
Repro:    curl -s http://127.0.0.1:8080/api/report-card
Expected: The report card is the panel README sends you to before going live; it must be
          "hard to misread in the optimistic direction" (its own header comment).
Actual:   detector "manual", route demo_exchange: total_pnl_cents "-0.250000",
          max_drawdown_cents "0.250000". Both go through asDollars.
            -0.25/100 = -0.0025 ; sign "-" ; Math.abs(...).toFixed(2) = "0.00"
          The "total" cell renders the literal string  -$0.00  and "max dd" renders $0.00.
          A negative-signed zero is both a nonsense figure and reads as "no loss".
          Meanwhile the same row's mean and CI, via asSignedCents, correctly show
          "-0.08¢" and "[-0.13¢, -0.04¢]" — so one row contradicts itself.
Evidence: frontend/src/ReportCard.tsx:168-173 (total, max dd -> asDollars)
          frontend/src/ReportCard.tsx:160-167 (mean, CI -> asSignedCents)
          frontend/src/api.ts:723-729
          Live: {"detector":"manual","total_pnl_cents":"-0.250000","max_drawdown_cents":"0.250000",
                 "mean_pnl_cents":"-0.0833","ci_low_cents":"-0.1300","ci_high_cents":"-0.0400"}
Fix sketch: report card is a cents panel throughout; drop asDollars from these two cells.
```

```
[P1] UI-004 — The approval card does NOT show the wire form, contrary to README
Repro:    curl -s 'http://127.0.0.1:8080/api/proposals?limit=100'  (100 real proposals)
          curl -s /assets/index-BUjuHosI.js | grep -o book_side | wc -l   ->  1
Expected: README:713 — "The approval card shows the literal wire form so you can check it
          before committing." CLAUDE.md, order direction: an inversion is caught by
          nothing downstream (the P(1-P) fee is symmetric); the human check is the guard.
Actual:   ApprovalCard renders leg price as centsNum(leg.limit_price, 2)+"¢" and direction
          as "{action} {side}" — the TRADED-side view. It never shows book_side or the YES
          wire price. The ProposalLeg type (api.ts:197-206) has no wire field, so the API
          does not supply one either: a live leg is
            {"side":"yes","action":"sell","limit_price":"0.355000","contracts":"5.00"}
          with no bid/ask and no 1-p anywhere.
          The single occurrence of "book_side" in the shipped bundle is TradeTicket's
          "sends as" row (TradeTicket.tsx:293-297) — the PRE-propose preview, which is not
          on screen at approval time and does not exist at all for a detector-generated
          proposal. Detector proposals are the ones a human never priced by hand.
          Today's set-arb legs are all "sell yes", where wire price == limit price, so the
          gap is invisible; a "buy no" leg is where it bites.
Evidence: frontend/src/ApprovalCard.tsx:113-121, 139-143, 192-198
          frontend/src/api.ts:197-206 (ProposalLeg has no wire)
          frontend/src/TradeTicket.tsx:293-297 (the only wire display in the app)
          README:713
Fix sketch: return the wire tuple per leg from /api/proposals and render it on the card,
          in the mono "ask 10 @ 0.70" form the ticket already uses.
```

```
[P1] UI-005 — There is no kill switch control. Firing it means editing config.yaml and restarting containers.
Repro:    curl -s http://127.0.0.1:8080/openapi.json | jq -r '.paths|keys[]'   (28 paths)
          grep -rn kill_switch backend/app --include=*.py
Expected: README:820 safety table — "Kill switch | Halts all proposals and cancels resting
          orders." Presented as an operable control alongside the other safety mechanisms.
Actual:   `kill_switch` is sourced only from `config.risk.kill_switch` (backend/app/config.py:54).
          The only POST routes in the entire API are /api/proposals, /api/proposals/quote,
          /api/proposals/{id}/approve, /api/proposals/{id}/reject and
          /api/orders/{id}/cancel. There is no kill-switch endpoint and no UI control.
          The UI surfaces it three times, all read-only:
            SystemPanel.tsx:99-103   a pill reading "off"
            Trades.tsx:163-168       a banner when already engaged
            TradeTicket.tsx:167-172  a banner when already engaged
          So the emergency stop is: SSH in, edit config.yaml, `docker compose restart api
          worker`. Cancelling working orders one at a time via the per-order cancel button
          is the only in-UI approximation, and it does not stop new proposals.
Evidence: openapi.json path list (above); backend/app/config.py:54;
          frontend/src/SystemPanel.tsx:99-103; frontend/src/Trades.tsx:163-168
Fix sketch: a POST /api/trading/kill endpoint with a runtime override, and a control on
          the trades page; or amend README to say the kill switch is config-time only.
```

---

## P2 findings

```
[P2] UI-006 — asCount rounds fractional counts of 10 or more to an integer, contradicting its own docstring
Repro:    curl -s 'http://127.0.0.1:8080/api/markets/KXNFLGAME-26AUG15DALSEA-SEA/tape?limit=3'
Expected: api.ts:696-701 — "Kalshi supports fractional contracts down to 0.01, so small
          sizes keep their decimals — rounding 0.66 to '1' or 0.4 to '0' would misreport a
          real print as nothing."
Actual:   The decimal-preserving branch is `value < 10 && !Number.isInteger(value)`. At 10
          and above it falls through to toFixed(0). Live tape prints are 484.69, 181.97 —
          rendered "485" and "182". "10.50" renders "11".
          The stated principle (a fractional print is real) is applied only below 10.
Evidence: frontend/src/api.ts:702-711; live tape count "484.69"
```

```
[P2] UI-007 — Three count conventions across the app; the whole trade rail prints raw strings
Repro:    read the count column of every table
Expected: one number language.
Actual:   Screener/tape/market page use asCount ("28.4M"). The ladder uses toLocaleString
          ("422,805.58"). Approval legs, positions, fills and working orders print the raw
          API string ("12.00", "5.00", "0.00/5.00"). So the same 12 contracts read as "12"
          on one page and "12.00" on another, and a 258M open interest is compacted while a
          422,805.58 ladder level is not.
Evidence: Screener.tsx:245-246 · TapeView.tsx:49 · OrderBookLadder.tsx:59 ·
          ApprovalCard.tsx:147,197 · Trades.tsx:270,320,378
```

```
[P2] UI-008 — The approval card shows no worst case; the trade ticket does
Repro:    read ApprovalCard's stat grid
Expected: the approval is the decision point. README:823 — max_loss is "what the risk
          limits measure"; api.ts:219 says the same.
Actual:   The grid is price | size | fee | net edge | bankroll. `max_loss_cents` is
          declared (api.ts:221) and never rendered. TradeTicket does show "max loss"
          (TradeTicket.tsx:282) — so the operator sees the worst case while composing a
          manual ticket and loses it at the moment of approval, and never sees it at all
          for a detector proposal. On every live set-arb proposal max_loss_cents is null
          anyway, so nothing would show — but that is a second finding, not a defence.
Evidence: frontend/src/ApprovalCard.tsx:134-181; frontend/src/api.ts:221;
          live proposal id 253: "max_loss_cents": null
```

```
[P2] UI-009 — Two money languages inside one seven-row quote box
Repro:    open a market, type a ticket
Actual:   cost / fee / total / max win / max loss are dollars at 2dp; breakeven and net
          edge are cents at 2dp. Concretely, for 10 NO at 30c:
            cost $3.00 · fee $0.15 · total $3.15 · breakeven 31.47¢ · max win $6.85 ·
            max loss $3.15 · net edge (when priced) +2.75¢
          The operator has to switch units twice reading seven adjacent rows, and the two
          rows that keep sub-cent precision are the two that are not dollars.
Evidence: frontend/src/TradeTicket.tsx:270-298
```

```
[P2] UI-010 — Price columns are inconsistently unit-labelled; some carry ¢, some carry nothing
Repro:    compare Positions "avg" (53.00) with Settlements "avg" (53.0¢) — same field
Actual:   centsNum returns a bare number by design ("for dense table columns"), and callers
          disagree about whether to append ¢. Approval legs and the market-page headline
          add it; positions, fills, working orders and the screener do not. On the screener
          the bid/ask/last/spr headers carry no unit either, so a "0.4" is ambiguous
          between 0.4¢ and $0.40 to anyone who has not read the source.
Evidence: Trades.tsx:268,321,377 (no ¢) vs ApprovalCard.tsx:142,196 and
          MarketPage.tsx:147 (¢) vs Trades.tsx:446-447 (asCents, ¢)
```

```
[P2] UI-011 — The Settlements table is missing className="table" and renders unstyled
Repro:    grep -n '<table' frontend/src/Trades.tsx
Expected: every other table in the app carries className="table".
Actual:   Trades.tsx:422 is a bare `<table>`. Twelve other tables across the app have the
          class; this is the only one without. Consequences from styles.css:324-373: no
          border-collapse, no white-space:nowrap, no th styling (uppercase, background,
          sticky), no td padding/borders, and critically `.table .num` never matches — so
          the held / avg / paid / P&L columns lose right-alignment and tabular-nums and
          render left-aligned in a proportional-ish default.
          Not visible today (settlements is empty: `{"settlements": []}`), which is why it
          has survived. It will appear the first time a held position resolves.
Evidence: frontend/src/Trades.tsx:422 vs :246,:298,:358,:478,:558 · styles.css:324-373
```

```
[P2] UI-012 — The market page does not show live-vs-cached per panel; only the ladder does
Repro:    curl -s .../candles?period_sec=60&lookback_hours=12  ->  "source":"kalshi"
          grep -rn '\.source' frontend/src/*.tsx
Expected: README:650 — "The market page shows where each panel's data came from
          (`live` vs `cached`)."
Actual:   Only OrderBookLadder.tsx:101 reads `book.source`. CandlesResponse.source
          (api.ts:100) is returned by the API — the backend sets it to "kalshi" when the
          read-through wrote, "local" otherwise (backend/app/api/routes/markets.py:306-312)
          — and MarketPage discards it. The tape endpoint likewise. The only other
          indicator is the single `feedStatus` pill in the page header, which is the
          WEBSOCKET state and says nothing about where the chart's candles came from.
          One of three panels honours the documented behaviour.
Evidence: frontend/src/MarketPage.tsx:70-89 (result.source unused), :141-143 (single pill)
          frontend/src/OrderBookLadder.tsx:101
```

```
[P2] UI-013 — The market page's headline quote is fetched once and never refreshed by REST
Repro:    open /market/<ticker>, let the websocket drop
Actual:   loadCore() (MarketPage.tsx:58-68) runs in a useEffect keyed on [ticker] with no
          interval. The book and tape poll every 5s (:99-104), the trading state is
          fetched once, and the market row — last price, yes_bid, yes_ask, volume, status —
          updates only via websocket ticks (:51-56).
          So with the feed down, the 30px accent headline price and the bid/ask beneath it
          are frozen at page-load values, sitting directly above a ladder that is 5 seconds
          fresh. The only cue is a small "offline" pill in the sub-header. Given the
          websocket requires auth and the README's whole point is that the market page
          works without a key, this is the no-credentials default state.
Evidence: frontend/src/MarketPage.tsx:58-68, 91-104, 141-152
Fix sketch: poll loadCore on the same 5s interval as the book, or age-stamp the quote.
```

```
[P2] UI-014 — feedStatus reports socket liveness, not data liveness, and nothing shows data age
Repro:    read useLiveFeed
Actual:   status flips to "live" in socket.onopen and to "offline" only in onclose. If the
          ingest service stops publishing to Redis while the relay socket stays open, the
          Screener's "feed: live" stat and the MarketPage's green "live" pill both keep
          claiming live indefinitely. There is no last-message timestamp, no heartbeat
          timeout, and no staleness indicator anywhere in the UI.
          The reconnect path itself is correct: exponential backoff capped at 15s, and
          onopen re-sends the {"action":"watch"} filter, so the ticker subscription IS
          restored after a reconnect. That half is fine.
Evidence: frontend/src/useLiveFeed.ts:47-76; Screener.tsx:138-142; MarketPage.tsx:141-143
```

```
[P2] UI-015 — The in-tab-only alerting limitation is documented in a source comment and nowhere in the UI
Repro:    grep -rn 'tab is open\|web push\|secure context' on the JSX
Expected: CLAUDE.md open items — alerting is title + favicon + audio, and only while a tab
          is open; getting alerts with the tab closed needs TLS. This is an operator
          decision, so the operator has to know about it.
Actual:   The 38-line explanation lives in notify.ts:1-38 where only a developer sees it.
          No UI surface mentions it. The trades page says "every order requires your
          explicit per-trade approval" and shows a 120s TTL, and the audio ping requires a
          prior click on that page (Trades.tsx:117-125) — so a freshly-loaded background
          tab is silent by design. An operator learns the limit by losing a proposal to
          expiry. On the live evidence that is the normal outcome: 259 of 262 set-arb
          proposals ended "expired — ttl elapsed without a decision".
Evidence: frontend/src/notify.ts:1-38; frontend/src/Trades.tsx:117-125;
          live report card funnel: set_arbitrage expired=259 of 262
Fix sketch: one line under the approval queue — "alerts only fire while this tab is open".
```

```
[P2] UI-016 — The report card drops the headline for exactly the detectors that have no other explanation
Repro:    curl -s http://127.0.0.1:8080/api/report-card
Actual:   The explanatory <ul> is gated on `r.trades > 0` (ReportCard.tsx:188,191). The
          live undervalued_screener row has trades=0 and headline "No trades — nothing to
          evaluate." — which is dropped. That detector has emitted 24 signals and 180
          observations, and its row shows funnel "24 → 0 → 0 → 0", trades 0, every money
          cell "—", CI "—", and a grey "untested" pill with no sentence.
          The rows that DO get a sentence are the ones whose numbers already speak.
          The gate is backwards: an all-dashes row is the one needing prose.
Evidence: frontend/src/ReportCard.tsx:188-200; live undervalued_screener/simulated row
```

```
[P2] UI-017 — The engine panel prints "no feed — markets refused" on a row that is showing a price
Repro:    curl -s http://127.0.0.1:8080/api/engine
Actual:   Live BTC-USD row: price "65264.80500000", source "coinbase", age_sec 29258.7,
          fresh false (bitcoin.enabled is false, so the poller stopped). The row renders:
            BTC-USD | 65264.80500000 | coinbase | 8.1h ago | no feed — markets refused
          The "usable" cell is a boolean rendered as two absolute sentences, and the
          absent case ("we have never had an ETH price") and the stale case ("this BTC
          price is 8 hours old") get the identical words while the row's own price and
          source columns contradict it. The panel's docstring is explicitly about making
          "we have no ETH price" a visible fact; this wording undoes that for the
          rows that do have one.
Evidence: frontend/src/EnginePanel.tsx:68-70; live /api/engine BTC-USD row
Fix sketch: "stale — 8.1h old, refused" when price is non-null; "no feed" only when null.
```

```
[P2] UI-018 — The reference-feed price is rendered raw, at eight decimal places
Repro:    curl -s http://127.0.0.1:8080/api/engine
Actual:   EnginePanel.tsx:65 renders {f.price ?? "—"} with no formatter at all, giving
          "65264.80500000" in a right-aligned numeric column. It is the only money value
          in the application with no formatter, and it is a dollars-per-BTC quantity
          rendered next to columns of cents.
Evidence: frontend/src/EnginePanel.tsx:65; live price "65264.80500000"
```

```
[P2] UI-019 — The news panel says "the headline engine is off" and then lists 30 headlines
Repro:    curl -s http://127.0.0.1:8080/api/news
Actual:   Live: budget.enabled false, feeds_configured 0, headlines 30.
          The explanation chain (NewsPanel.tsx:111-126) is if/else, so only the first
          branch renders: "The headline engine is off (news.headlines.enabled)." The
          headline table below it is gated separately on headlines.length > 0 and renders
          all 30. The operator reads a categorical denial immediately above the thing
          denied, and the "no feeds configured" explanation for where 30 headlines with
          feeds_configured=0 came from never shows.
Evidence: frontend/src/NewsPanel.tsx:111-165; live /api/news
```

```
[P2] UI-020 — The signal rationale is truncated to one nowrap line, cutting the caveat that is the point of the row
Repro:    curl -s 'http://127.0.0.1:8080/api/signals?limit=3'
Expected: README:287 — the screener's score "is not cents, not a probability, and a 70 is
          not twice a 35." The detector puts that caveat in every rationale.
Actual:   The "why" cell is class audit-detail: white-space:nowrap; overflow:hidden;
          text-overflow:ellipsis; max-width:520px (styles.css:898-905). A live rationale is
          230 characters:
            "RESEARCH ONLY — quiet and wide, not known to be mispriced. 0 contracts traded
             in 24h (0th pct of set); 97c wide; closes in 3.9h; OI 1 Score 99.4 is an
             ordering for attention, not cents and not a probability."
          At 520px of 12px monospace roughly 70 characters survive. The disclaimer sits at
          the END of the sentence, so the part that gets cut is precisely the part stopping
          a 99.4 being read as a 99.4% chance. There is no title attribute and no expansion
          — the text is unreachable in the UI.
          The same class truncates the audit-trail payload column (Trades.tsx:577-579),
          where the whole point is an append-only record you can read.
Evidence: frontend/src/styles.css:898-905; Trades.tsx:534, 577-579; live signal id 609
Note:     exact truncation point is UNVERIFIED — needs browser; the mechanism is certain
          from the CSS.
```

```
[P2] UI-021 — The undervalued screener's score is never surfaced as a value
Repro:    curl -s 'http://127.0.0.1:8080/api/signals?limit=3'
Actual:   evidence.score = 99.42271178170469 is present on every signal and no component
          reads SignalRow.evidence at all. The score reaches the operator only as prose
          inside the rationale string that UI-020 truncates. So the brief's question —
          is the score presented in a way that respects that it is an ordering — has the
          answer "it is not presented". (Note the screener's liquidity_score, a different
          number on a different scale, IS rendered with a bar; the two could be confused.)
Evidence: frontend/src/api.ts:495 (evidence declared) — no reader in any .tsx
```

```
[P2] UI-022 — The system page tells the operator M7, M8 and M9 are not done
Repro:    curl -s http://127.0.0.1:8080/assets/index-BUjuHosI.js | grep -o 'M7","Weather engine",!1'
Expected: CLAUDE.md status table and README:29-31: M7, M8, M9 all done.
Actual:   SystemPanel's MILESTONES has M7/M8/M9 flagged false, so "Build progress" shows
          them unticked and greyed. Confirmed present in the deployed bundle:
            ["M7","Weather engine",!1],["M8","News + catalyst engine",!1],
            ["M9","Backtester + hardening",!1]
          The dashboard is the operator's only status surface and it is three milestones
          behind the code it is serving.
Evidence: frontend/src/SystemPanel.tsx:13-15; shipped bundle
```

```
[P2] UI-023 — The approval queue's empty state is stale and points at a milestone that shipped
Repro:    open /trades with an empty queue (the live default: pending_proposals 0)
Actual:   "Nothing awaiting a decision. Open a market and use the trade ticket to queue
          one; detectors start filling this queue in M4." M4 through M9 are all done.
          Present in the shipped bundle. The genuinely useful sentence here — that all
          detectors ship disabled and how to turn one on — is on the *signals* empty state
          twelve sections further down (Trades.tsx:472-475), where an operator with an
          empty queue has no reason to scroll.
Evidence: frontend/src/Trades.tsx:221-224; shipped bundle contains the string
```

```
[P2] UI-024 — No media queries at all; the layout has never been designed for a narrow viewport
Repro:    grep -c '@media' frontend/src/styles.css   ->   0
Actual:   Zero media queries in 1,018 lines. The layout leans entirely on
          `repeat(auto-fit, minmax(Npx, 1fr))`, which does degrade gracefully — .grid at
          260px, .split at 320px, .split-3 and .approvals at 300px all collapse to one
          column on a 390px phone. Tables are wrapped in .table-scroll {overflow-x:auto}.
          So a phone pass is *plausible*, with these specific risks:
          - header.topbar is a single non-wrapping flex row: brand + 3 tabs + up to 4
            pills. At 390px that is likely to overflow horizontally (the row has no
            flex-wrap and the pills have no shrink allowance).
          - .table .title has max-width:340px, .audit-detail max-width:520px — both wider
            than a 390px viewport minus 32px of padding.
          - .split-3 at minmax(300px,1fr) puts the ticket at ~358px usable; the "sends as"
            mono row and the .quote-box .row flex pairs are the tight ones.
          - body font-size is 13px and .stat-label/.table th are 10px — below the usual
            legibility floor on a phone.
Evidence: frontend/src/styles.css (no @media); :49-59 (.topbar), :369-373, :898-905
Status:   UNVERIFIED — needs browser for the actual overflow. Load / and /trades at 390px
          and 768px and check for horizontal body scroll; the topbar is the first suspect.
```

```
[P2] UI-025 — A multi-leg proposal's legs table has no horizontal scroll container
Repro:    read styles.css .legs and .approvals
Actual:   Every other table in the app sits inside .table-scroll {overflow-x:auto}. The
          legs table (ApprovalCard.tsx:188) sits inside .legs (styles.css:909-913), which
          has no overflow rule, inside an .approval card in a grid of minmax(300px,1fr).
          .table sets white-space:nowrap. A leg row is ticker + "sell yes" + price + size;
          a live ticker is KXTESTMATCH-26JUL251100PAKWI-PAK (32 chars). At 12px monospace
          (~7.2px/char) that column alone is ~230px before three more columns and padding,
          in a card that can be as narrow as 300px.
          The brief asks about 7 legs: leg COUNT is a vertical problem and fine; leg
          WIDTH is the one at risk, and it is already at risk at 2 legs.
          Largest live proposal is 3 legs (KXTESTMATCH-26JUL251100PAKWI).
Evidence: frontend/src/styles.css:909-926, :324-329, :783-787; ApprovalCard.tsx:183-203
Status:   UNVERIFIED — needs browser for whether it clips or forces the card wider.
```

```
[P2] UI-026 — No timezone is shown anywhere, while the system's day boundary is UTC
Repro:    grep -rn toLocale frontend/src/*.tsx
Actual:   Times render four different ways, all in browser-local, none labelled:
            asClock          -> 24h HH:MM:SS      (fills, settlements, signals, audit, tape)
            toLocaleString   -> full local        (NewsPanel close_time, published_at)
            toLocaleTimeString -> local time      (NewsPanel expected_release, RiskPanel cooldown)
            countdown/asTimeToClose -> relative   (screener, catalysts)
          Meanwhile the daily loss limit resets on the UTC day (RiskPanel.tsx:99 literally
          says "Clears at 00:00 UTC"), risk state carries day "2026-07-27", and every API
          timestamp is +00:00. An operator west of Greenwich reads a local clock beside a
          UTC-keyed limit with nothing telling them they differ.
Evidence: frontend/src/api.ts:756-764; NewsPanel.tsx:91,98,143; RiskPanel.tsx:99,102
```

```
[P2] UI-027 — RiskPanel computes a displayed money value in the browser, against its own stated rule
Repro:    read RiskPanel.tsx:24-27 vs :84-85
Expected: the function's own docstring: "The only place this file is allowed to make a
          number out of money, and it is for bar widths and sign tests — never for
          anything displayed."
Actual:   line 85: `const lossUsed = netCents < 0 ? String(-netCents) : "0";`
          lossUsed is passed as `used` to Meter, which displays it: `{asDollars(used)}`
          (line 54). So a browser-computed float round-trip (string -> Number -> negate ->
          String -> Number -> /100 -> toFixed) is exactly what the operator reads as
          today's loss. Live: daily_net_cents "-21.130000" -> "$0.21". Benign at 2dp; the
          rule the file states for itself does not hold.
Evidence: frontend/src/RiskPanel.tsx:21-27, 84-85, 53-55
```

```
[P2] UI-028 — The LLM budget does a double float round-trip to display dollars
Repro:    curl -s http://127.0.0.1:8080/api/news | jq .budget
Actual:   `asDollars(String(Number(budget.spent_usd) * 100))` — a dollars string is
          multiplied to cents in JS, stringified, then divided back by 100 inside
          asDollars. Two float operations to display a value that arrived correct. Live
          values ("0", "2.0") survive; a value like "0.07" goes 0.07 -> 7.000000000000001
          -> "$0.07" only because toFixed(2) hides it.
Evidence: frontend/src/NewsPanel.tsx:171-172
```

```
[P2] UI-029 — The fee-unverified dead end is only explained on /system
Repro:    grep -rn 'verified_on' frontend/src/*.tsx
Expected: README:137 — "Until it is verified, nothing can be proposed at all." That is the
          single most consequential first-run state.
Actual:   The banner with the remediation command lives only in SystemPanel (:49-57), which
          is the third tab. With an unverified schedule the screener and the trades page
          look completely normal: markets list, approval queue empty with the stale M4
          text (UI-023), risk meters drawn. The trade ticket does handle it well —
          `blocked` catches err.code "unverified_fee_schedule" and renders "This market
          cannot be priced" with the backend's message (TradeTicket.tsx:113-123, 174-178) —
          but only after the operator types a ticket and waits 250ms.
          Live stack has fees verified 2026-07-27, so this is the unverified branch read
          from source; SystemPanel.tsx:49 is a plain `=== null` test that will fire.
Fix sketch: hoist the banner to App.tsx beside the safe-mode banner, where every page sees it.
Evidence: frontend/src/SystemPanel.tsx:49-57; App.tsx:110-126 (banner slot); TradeTicket.tsx:113-123
```

```
[P2] UI-030 — Clearing a ticket field leaves the previous error on screen
Repro:    type limit price "abc" (backend 400 "limit_price: cannot parse 'abc'"), then
          clear the field
Actual:   refreshQuote's guard `if (!price || !contracts) { setQuote(null); return; }`
          returns before touching setError, so the stale red error persists against an
          empty form. On the success path the quote box simply vanishes with no message.
          Validation itself is sound and correctly server-side: every case I threw at
          /api/proposals/quote came back 400 with a specific message ("limit_price must be
          strictly between 0 and 1 dollars, got '56'. Kalshi quotes dollar strings like
          '0.5600'." / "contracts must be positive, got '-5'"), a sub-cent price 0.5555
          with 0.5 contracts was accepted and priced (notional_cents "27.77500"), and
          canPropose requires a non-null quote so the button is disabled while invalid.
          The cost preview does update as you type (250ms debounce) and is entirely
          backend-computed, exactly as README:146-149 claims.
Evidence: frontend/src/TradeTicket.tsx:101-125, 150-151
```

```
[P2] UI-031 — The settlements "held" column is the only place the signed position convention reaches the operator
Repro:    read Trades.tsx:443 vs :317-320
Expected: README:717 — "The UI converts back to '10 NO at 30¢' for display."
Actual:   The Positions table does this correctly: side "no", size "12.00", avg 53.00 (the
          NO price, from avg_price; avg_yes_price is correctly never shown). Good.
          Settlements renders `{s.net_contracts}` raw — the live position would show
          "-12.00" with no side column at all, so a NO holding reads as a negative
          quantity. Same underlying convention, opposite presentation, on the same page.
          Currently invisible (settlements is empty) — it appears on the first resolution.
Evidence: frontend/src/Trades.tsx:437-455 vs :310-343; live position net_contracts "-12.00"
```

```
[P2] UI-032 — Two-step approval is well judged; one detail undercuts it
Repro:    read ApprovalCard's confirm flow
Actual:   Filed as a positive with one caveat. The flow is deliberate rather than annoying:
          "approve…" reveals a confirm panel with route-specific wording, the paper copy
          names the actual route, the multi-leg copy explains non-atomic IOC in full, and
          the expired branch removes the button entirely rather than disabling it. The
          countdown genuinely ticks (250ms interval, ApprovalCard.tsx:25-41) and colours
          at 40s and 15s. The expired message explains why ("the quote it was priced
          against is gone. Re-propose rather than trading a stale edge"). This is good.
          The caveat: `requires_typed_confirmation` is false on the demo route (verified
          live), so on paper the second step is a second click on a button labelled
          "confirm & place" positioned exactly where "approve…" was. Two clicks in the
          same place is closer to one click than the design intends. A small deliberate
          offset or a different button position would cost nothing.
Evidence: frontend/src/ApprovalCard.tsx:213-283; live trading state
          "requires_typed_confirmation": false
```

```
[P2] UI-033 — Positions renders a realised P&L of exactly zero in green
Repro:    curl -s http://127.0.0.1:8080/api/positions
Actual:   `Number(p.realized_pnl_cents) >= 0 ? "num up" : "num down"`. All three live
          positions have realized_pnl_cents "0" and render "$0.00" in the accent green
          reserved for gains. Combined with UI-002 (fees hidden), the NFL position presents
          as a green $0.00 beside a -$0.06 unrealised while 20.88c of fees are already
          spent. Zero is not a gain.
Evidence: frontend/src/Trades.tsx:335-341
```

```
[P2] UI-034 — The order-direction inversion is inlined in the ladder
Repro:    read OrderBookLadder.tsx:18-34
Expected: CLAUDE.md — "The only guards are app/trading/direction.py and the tests around
          it. Never inline this mapping anywhere else."
Actual:   `price: invert ? (1 - Number(p)) * 100 : Number(p) * 100`. This is the NO->YES
          inversion, written in JavaScript, in floats, outside direction.py. It is
          display-only and cannot cause an order, and the file comment explains the
          economics correctly — but it is a second implementation of the one mapping the
          codebase says must have exactly one. If the convention ever changes, this is the
          copy nobody greps for.
          Also: (1 - 0.53) * 100 = 47.000000000000004, saved only by toFixed(1).
Evidence: frontend/src/OrderBookLadder.tsx:18-34; CLAUDE.md order-direction section
```

---

## P3 findings

```
[P3] UI-035 — ScoreBar clamps the top of the liquidity score but not the bottom
Actual:   `style={{ width: `${Math.min(100, score)}%` }}` — a negative score would emit
          width:-450%, an invalid declaration CSS drops, leaving the bar at its default
          width while the number reads "-450". This is the historical −450-on-a-0–100-scale
          bug's UI half. It cannot fire today: backend/app/api/routes/markets.py:99 clamps
          with `round(min(100.0, max(0.0, raw)), 1)` and a 300-market live scan found
          min 0.5, max 100.0. Latent only — the clamp lives entirely on the server.
Evidence: frontend/src/Screener.tsx:35-43; backend/app/api/routes/markets.py:72-100
```

```
[P3] UI-036 — liquidity_score is formatted on the screener and raw on the market page
Actual:   Screener: score.toFixed(0) -> "96". MarketPage.tsx:201: {market.liquidity_score
          ?? "—"} -> "95.5". Same field, same page-load, two renderings; the market page
          also gets no bar.
Evidence: frontend/src/Screener.tsx:41 vs MarketPage.tsx:201
```

```
[P3] UI-037 — .input:focus removes the outline; it is the only focus style in the stylesheet
Actual:   styles.css:287-290 sets outline:none and substitutes a 1px accent border. Every
          other interactive element (.btn, .seg-btn, .tab, links) has no focus rule, so
          those keep the UA default ring — inconsistent, and the replaced one is the
          weaker signal. It applies to the live-route typed-ticker input, the highest-stakes
          field in the application.
Evidence: frontend/src/styles.css:287-290; grep -c ':focus-visible' -> 0
Status:   UNVERIFIED — needs browser to judge the accent border's visibility on #0a0a0a.
```

```
[P3] UI-038 — A tape print with a null taker_side is coloured as a NO-side aggressor
Actual:   `t.taker_side === "yes" ? "up" : "down"` — null falls to "down", painting the row
          red while the side cell shows "—". Colour is not the sole carrier here (the side
          text is present) so this is cosmetic. Every live row has a taker_side; the tape's
          vocabulary is correctly yes/no, not buy/sell, matching CLAUDE.md.
Evidence: frontend/src/TapeView.tsx:44
```

```
[P3] UI-039 — Sticky table headers do nothing
Actual:   `.table th { position: sticky; top: 0 }` (styles.css:341-342) but .table-scroll
          sets only overflow-x with no max-height, so there is no vertical scroll container
          to stick within and the header scrolls away with the page. .tape-head's sticky
          does work (.tape has max-height:300px).
Evidence: frontend/src/styles.css:319-322, 341-342, 607-612
```

```
[P3] UI-040 — Colour is never the sole carrier of meaning — one check that passed
Actual:   Filed as a negative result. Every up/down, ok/bad and live/cached signal I traced
          has a text or symbolic partner: P&L via asSignedCents carries an explicit +/−,
          asDollars carries "−", route pills carry words ("LIVE — real money"), verdict
          pills carry words, buy/sell cells carry "buy yes", the ladder columns are
          labelled "bid (buy YES)" / "ask (sell YES)", feed status carries
          "live"/"connecting"/"offline". The one weak spot is `.approval.decided`, which
          recedes a card to opacity 0.55 — but the status chip still names the status.
Evidence: styles.css:474-480, 1003-1008; api.ts:732-754; OrderBookLadder.tsx:48
```

---

## Things I checked that turned out fine (so nobody re-checks them)

- **Null-candle handling is correct, in the backend.** `PriceChart.tsx:127` skips
  `close === null`, which would gap a thin market — but it never fires: `backfill.py:90-105`
  substitutes the bid/ask midpoint before storing. Live 1m candles for a thin market:
  190 rows, **0 null closes**. The chart's own `toCents = Number(v ?? c.close)` also
  back-fills a null open/high/low from close. No finding.
- **The chart axis cannot show −20¢.** Two independent guards:
  `autoscaleInfoProvider` clamps the price range to [0,100] (PriceChart.tsx:76-86) and
  `rightPriceScale.scaleMargins.bottom` is 0.02 with volume on its own scale
  (:51-58). Both carry comments naming the original bug.
- **Candle time is the period START.** The route serialises `int(c.ts.timestamp())` and
  ingest keys on `period_start(end_period_ts, period_sec)` (backfill.py:49,128). The chart
  labels the start. Correct per CLAUDE.md.
- **The websocket reconnects AND resubscribes.** `socket.onopen` re-sends the watch filter
  on every connect, backoff capped at 15s (useLiveFeed.ts:47-57, 67-73). The ticker filter
  is not lost. (What is missing is a data-staleness signal — UI-014.)
- **The trade ticket is entirely backend-priced.** No fee, cost, breakeven or edge is
  computed in the browser; every one comes from `POST /api/proposals/quote`. `suggestedPrice`
  explicitly refuses to derive the NO price locally, with a comment naming the float
  round-trip it used to do (TradeTicket.tsx:49-56).
- **Signals correctly render a zero edge as "—"** rather than "+0.00¢", matching
  README:216 ("a zero means the detector declined to claim an edge"). Verified against
  live `net_edge_cents: "0.0000"`.
- **The BUCKET- signal guard works** — calibration signals are not linked, avoiding a
  guaranteed 404 (Trades.tsx:503-509).
- **Positions use trader language**, not the signed convention: side "no", contracts
  "12.00", avg 53.00 (the NO price). `avg_yes_price` is correctly never displayed.
  Only Settlements leaks the signed form (UI-031).
- **The header safe-mode banner and route pills** correctly read the live interlocks
  (`real_money`, `live_trading_armed`, `execution_route`), and `routeLabel` renders the live
  route as "LIVE — real money" in a bad-styled pill.
- **The report card's structural honesty is intact**: verdict pill before the number, mean
  greyed while untested, interval always shown, claimed beside realised, "untested" rather
  than "pending", the unattributed-drop explanation, and the "no detector has demonstrated
  positive expectancy" note when proven=0. This panel was clearly designed against the
  failure mode it exists for.
```

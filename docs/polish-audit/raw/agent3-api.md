# Agent 3 — API contract & docs drift

Area prefix **API**. Live stack `http://127.0.0.1:8080`, `environment=demo`,
`trading_mode=paper`, `execution_route=demo_exchange`, fees verified 2026-07-27.
No docker, no browser (per ground rules). All runtime evidence is `curl` /
stdlib Python against the live API.

**Counts: 2 P0 · 4 P1 · 8 P2 · 9 P3.**

Note: a concurrent auditor was also writing to this stack. Every finding below
is evidenced by traffic **I** generated; where I observed another agent's rows I
say so and do not claim them.

---

## Verified correct (stated so the lead does not re-check)

- **Fee math is right end to end.** `GET /api/fees/quote`: 1 contract @ $0.50 →
  `1.7500`¢; 100 @ $0.50 → `175.0000`¢ (exactly $1.75, aggregate rounding);
  2 @ $0.20 → `2.2400`¢ (matches the real demo fill in CLAUDE.md); 10.5 @ $0.50
  → `18.3800`¢ (fractional counts); $0.5555 → `1.7300`¢ (sub-cent price);
  maker default M=0 → `0`; `KXBTCY`/`KXETHY` (and `KXBTCY-25JUL27`, series
  extraction) → `0.0000`. Every value matches
  `roundup(M × 0.07 × C × P × (1−P))` to a centicent, recomputed independently.
  `/api/system` reports `series_listed: 85`, 10 fee-free series, matching README.
  **No P0 on fees.**
- **Order-direction table matches the wire exactly**, verified live through
  `POST /api/proposals/quote` at limit `0.30`:
  `yes/buy→bid@0.30`, `yes/sell→ask@0.30`, `no/buy→ask@0.70`, `no/sell→bid@0.70`.
- **Execution-routing matrix**: all five README rows are implemented in
  `backend/app/trading/interlocks.py:89-111` (+ `settings.py:98-104`). The active
  row (`paper`+demo+creds → `demo_exchange`) is confirmed live.
- **Money is a string on every endpoint I could reach**, checked by JSON *type*
  not by eye, across `/api/markets`, `/{ticker}`, `/orderbook` (level pairs are
  `[str,str]`), `/candles` (OHLC+volume), `/tape`, `/positions`, `/pnl`,
  `/fills`, `/settlements`, `/orders`, `/proposals`, `/signals`, `/report-card`,
  `/risk.state.*`, `/engine`, `/fees/quote`, `/proposals/quote`. Exceptions in
  API-012 and API-014 only.
- **No `parseFloat`/`parseInt` on a money field** in `frontend/src/` or in the
  shipped bundle (`/assets/index-BUjuHosI.js`). The bundle's single `parseFloat`
  is inside lightweight-charts' rgba colour parser. Every money formatter in
  `frontend/src/api.ts:681-739` guards with `Number.isFinite` and renders `—`.
  **No P1 on browser money arithmetic.** See API-017/API-018 for the residue.
- **WebSocket behaves as documented**: handshake 101 + `__hello__`; ~97 ticks/s
  arrive; `{"action":"watch","tickers":[...]}` filters correctly (10s watching
  one ticker → 1 message, that ticker); a proposal created on an *unrelated*
  ticker still arrives on `copilot:proposals` while watching `ZZZ-NOTHING-AT-ALL`
  (filter bypass confirmed); 20 abrupt RST disconnects mid-stream leave
  `/api/health` `ok` and a fresh client still receives 505 msgs in 5s.
- **Per-trade approval, TTL, and queue cap hold sequentially**: approve with no
  `confirm` → 409 `not_confirmed`; approve after a 5s TTL → 409 `expired`;
  the 11th sequential proposal → 409 `queue_full` at exactly 10. (Concurrency is
  API-005.)
- **Exposure guard is conservative under concurrency, not leaky.**
  `risk.snapshot` (`risk.py:255-262`) sums `pending_cents` over *all* pending
  proposals and `guard_approval` then *adds* the approving proposal's
  `max_loss_cents` on top — it double-counts rather than under-counts. So
  API-005 does not open a money hole.
- **All detectors ship disabled** — `/api/system` `enabled_detectors: []`,
  matching README's "All ship disabled".
- `GET /api/signals`, `GET /api/report-card` and `/ws` all exist and behave as
  README describes. No documented endpoint is missing.

---

## P0

```
[P0] API-001 — `fair_price` is unvalidated: the exact units trap the codebase exists to prevent
Repro:    curl -s -XPOST -H 'content-type: application/json' \
            -d '{"ticker":"KXMLB-26-HOU","side":"yes","limit_price":"0.018","contracts":"1","fair_price":"56"}' \
            http://127.0.0.1:8080/api/proposals/quote
Expected: 400 bad_ticket, same as the sibling field:
          `limit_price` "56" -> 400 "must be strictly between 0 and 1 dollars,
          got '56'. Kalshi quotes dollar strings like '0.5600'."
Actual:   200 with "net_edge_cents":"5597.5700" — a claimed +$55.97/contract
          edge on a contract quoted at 1.8¢. Also accepted:
            fair_price "5"       -> net_edge_cents "497.5700"
            fair_price "999999"  -> net_edge_cents "99999897.5700"
            fair_price "-1"      -> net_edge_cents "-102.4300"
Evidence: backend/app/trading/pricing.py:210-211 parses fair_price and never
          range-checks it, 32 lines below the identical check on limit_price at
          pricing.py:174-182. UI surface: frontend/src/TradeTicket.tsx:245-253
          is a free-text `inputMode="decimal"` field labelled "fair value
          (optional)" / placeholder "your estimate, e.g. 0.62", with no client
          validation either — typing "62" for 62¢ is the obvious mistake.
Why P0:   CLAUDE.md's units section exists for exactly this: "`taker_fee_cents
          ("0.5600", ...)` is correct. `taker_fee_cents(56, ...)` raises —
          deliberately." The guard is present on the price the operator pays and
          absent on the price they are judging it against, and net edge is the
          number the approval decision rests on. It is also written to
          `proposed_trades.net_edge_cents` and into the audit log, i.e. it
          becomes the "claimed edge" column the report card grades against.
Fix sketch: apply the same `0 < p < 1` check to fair_price in `_validate`-style
          shared code so the two fields cannot drift again.
```

```
[P0] API-002 — `NaN` bypasses money parsing: 500s on read paths, and a NaN edge persists to the DB
Repro (500):
  curl -s -w '%{http_code}' 'http://127.0.0.1:8080/api/fees/quote?price_dollars=NaN&contracts=1'
    -> 500 Internal Server Error
  curl -s -w '%{http_code}' 'http://127.0.0.1:8080/api/fees/quote?price_dollars=0.5&contracts=NaN'
    -> 500 Internal Server Error
  curl -XPOST -d '{"ticker":"KXMLB-26-HOU","side":"yes","limit_price":"NaN","contracts":"1"}' \
       .../api/proposals/quote -> 500
Repro (persisted):
  curl -XPOST -H 'content-type: application/json' \
    -d '{"ticker":"KXMLB-26-HOU","side":"yes","limit_price":"0.018","contracts":"1","fair_price":"NaN"}' \
    http://127.0.0.1:8080/api/proposals
    -> 201, proposal 267, {"net_edge_cents":"NaN","legs":[{"fair_price":"NaN",...}]}
Expected: 400 with the same message the other malformed prices get. CLAUDE.md:
          "A malformed price from the wire raises rather than becoming 0."
Actual:   `Decimal("NaN")` is a *valid* Decimal, so app/core/money.py:69 accepts
          it. app/core/fees.py:267 then evaluates `Decimal(0) < price < Decimal(1)`
          on a NaN, which raises decimal.InvalidOperation — an ArithmeticError,
          not the ValueError the handlers catch (health.py:136,
          trading.py:296) — so it escapes as a 500. Where the value skips the
          range check entirely (fair_price, API-001) it reaches Postgres, which
          stores NaN happily in NUMERIC.
Evidence: backend/app/core/money.py:68-71 (no is_finite check);
          backend/app/core/fees.py:266-277; backend/app/trading/pricing.py:210.
          Contrast: the codebase already knows this class —
          app/detectors/undervalued_screener.py:144,171,174,206,220,249,313,
          app/backtest/replay.py:482 and app/news/budget.py:233,276,355,378 all
          call `.is_finite()`. The single money-parsing chokepoint does not.
Blast radius (measured): proposal 267 was approved and **filled on the demo
          exchange** (order 13, 1 contract KXMLB-26-HOU YES @ 0.018) carrying
          `net_edge_cents = NaN`. `app/backtest/report.py:243` computes
          `func.avg(...net_edge_cents)` — one NaN row makes
          `avg_claimed_edge_cents` NaN for that (detector, route) group, which is
          the report card the README sends the operator to before going live.
Fix sketch: reject non-finite in `money._to_decimal` — one place, everything
          downstream inherits it.
```

---

## P1

```
[P1] API-003 — six 500s from malformed query parameters
Repro / Actual (each `curl -s -o /dev/null -w '%{http_code}'`):
  /api/markets?offset=999999999999999999999      -> 500   (int64 overflow into SQL OFFSET)
  /api/markets?max_hours_to_close=1e308          -> 500   (datetime.fromtimestamp overflow)
  /api/markets?max_hours_to_close=-1e308         -> 500
  /api/markets?status=%00                        -> 500   (Postgres rejects NUL in text)
  /api/orders?ticker=%00                         -> 500
  /api/signals?detector=%00                      -> 500
  /api/audit?kind=%00                            -> 500
Expected: 422/400 with a useful body, like every other bad input on these
          routes (limit=0, limit=999999, sort=DROP TABLE, order=sideways all
          return clean 422/400 with a helpful message).
Evidence: offset has `Query(0, ge=0)` with no upper bound —
          backend/app/api/routes/markets.py:151. max_hours_to_close is an
          unbounded float fed to `datetime.fromtimestamp` at markets.py:183-186.
          The NUL cases are every `str | None` query param that reaches a SQL
          comparison: markets.py:154, trading.py:447, 813, 891.
Fix sketch: `le=` bounds on offset and max_hours_to_close; one shared
          validator that rejects NUL (and caps length) on free-text filters.
```

```
[P1] API-004 — WS: `tickers` as a string is silently exploded per character, and the client then receives nothing
Repro:    open /ws, send {"action":"watch","tickers":"KXMLB-26-HOU"}
Expected: an error, or the string treated as a one-element list. This is the
          single most natural client mistake against this protocol.
Actual:   server acks
          {"channel":"__watching__","data":{"tickers":["-","2","6","B","H","K","L","M","O","U"]}}
          and the client then receives **zero** ticks forever, because no ticker
          is one character. A silent, permanent, self-inflicted mute with a
          success acknowledgement.
Evidence: backend/app/api/ws.py:155-156
            tickers = command.get("tickers") or []
            client.tickers = {str(t) for t in tickers if t}
          A `str` is iterable, so the comprehension walks characters. Same shape
          for a dict: {"tickers":{"a":"b"}} acks `["a"]` (watches the keys).
Fix sketch: `if not isinstance(tickers, list): reject with an error frame`.
```

```
[P1] API-005 — the queue-depth cap does not hold under concurrency: 21 pending against a cap of 10
Repro:    24 simultaneous POST /api/proposals (barrier-synchronised raw sockets,
          all connections opened first, then released together), 1 contract each,
          ttl_sec=15, against an empty queue.
Expected: 10 × 201, 14 × 409 queue_full. README safety table: "Queue depth cap |
          risk.max_pending_proposals (default 10)". CLAUDE.md: "All risk limits
          are now enforced (M5)".
Actual:   21 × 201 Created, 3 × 409 Conflict.
          GET /api/trading/state -> "pending_proposals": 21   (cap = 10)
          Sequentially the cap is exact — the 11th of 11 sequential creates is
          409 queue_full, verified separately.
Evidence: backend/app/trading/proposals.py:58-83 — `_guard_queue_depth` does a
          `SELECT count(*)` and then the caller INSERTs, with no lock, no
          advisory lock, and no constraint. Under READ COMMITTED every
          concurrent transaction reads the same pre-burst count. Same
          check-then-insert shape in `_guard_duplicate` (proposals.py:87-110).
          Independently corroborated: I observed 19 proposals on one ticker
          created inside 270ms by a concurrent auditor, also past the cap.
Why not P0: no money escapes. Every one of the 21 still requires individual
          approval, and `risk.snapshot` counts all pending proposals into
          `pending_cents`, so a longer queue makes the exposure guard *more*
          conservative. What fails is the attention guard — which is precisely
          the failure README names ("a queue nobody reads is rubber-stamped
          rather than reviewed").
Fix sketch: a transactional advisory lock around count+insert, or a partial
          unique/exclusion constraint, in `proposals.py`.
```

```
[P1] API-006 — WebSocket proposal broadcast ships `leg_count: N` with `legs: []`
Repro:    watch /ws, POST /api/proposals; compare the copilot:proposals payload
          against GET /api/proposals for the same id.
Expected: the two views agree. `proposal_view`'s own docstring: "`legs` is where
          the tradeable detail lives. A manual ticket has one; a set arbitrage
          has one per market and they are approved together."
Actual:   WS: {"leg_count":1, "legs":[], "net_edge_cents":null, ...}
          REST: {"leg_count":1, "legs":[{"seq":0,"limit_price":"0.005000",
                 "contracts":"1.00", ...}]}
Evidence: backend/app/trading/proposals.py:595 —
          `json.dumps({"event": event, "proposal": proposal_view(proposal)})`
          with no `legs=` argument, so proposals.py:415 renders `legs or []`.
          Fires on every create/expire/reject broadcast (proposals.py:300, 351,
          387, 566).
Impact:   Not visible today — frontend/src/Trades.tsx:128-134 uses the message
          only as a trigger to re-`load()` from REST. But the payload is the one
          thing documented to reach the operator regardless of page
          (README "Real-time UI"), and frontend/src/ApprovalCard.tsx already
          reads `t.legs[0].limit_price` for single-leg cards — rendering that
          payload directly would show a price of "—". A five-leg set arb
          broadcasts as "5 legs" with none of them.
Fix sketch: pass legs into publish(), or drop `leg_count` from the broadcast so
          the payload cannot contradict itself.
```

---

## P2

```
[P2] API-007 — the trades page subscribes to the entire tick firehose it discards; there is no way to say "no ticks"
Repro:    frontend/src/Trades.tsx:128  `useLiveFeed([], (message) => {...})`
          -> useLiveFeed.ts:54 sends {"action":"watch","tickers":[]}
          -> ws.py:57 `if channel != CH_TICKS or not self.tickers: return True`
Expected: a page that only wants proposals/orders receives no ticks.
Actual:   empty list means "everything". Measured on the live stack: 774 tick
          messages in 8 seconds (~97/s, 537 distinct tickers), every one parsed
          by `JSON.parse` on the main thread in Trades.tsx's onmessage and then
          dropped by the channel check. The protocol has no "watch nothing".
Evidence: backend/app/api/ws.py:49-59; frontend/src/useLiveFeed.ts:51-56.
          README: "Clients send ... to filter the tick firehose down to what is
          on screen." The trades page wants zero and asks for all.
Fix sketch: a distinct sentinel (or `channels: []`) so "none" is expressible.
```

```
[P2] API-008 — an unauthenticated path parameter drives unbounded outbound Kalshi fetches and an unbounded in-process dict
Repro:    curl http://127.0.0.1:8080/api/markets/AGENT3JUNK1/orderbook
          -> 200 {"ticker":"AGENT3JUNK1","source":"kalshi","seq":null,"yes":[],"no":[]}
          second call for the same junk ticker -> "source":"local" (memoised)
          each *distinct* junk ticker -> another live Kalshi request
Expected: a ticker absent from the local catalog is a 404, as it is on the
          parent route (`/api/markets/NOSUCHTICKER` -> 404 "not found in the
          local catalog").
Actual:   every distinct string ever passed becomes a live REST call to Kalshi
          and a permanent entry in `Backfiller._last_fetch`, a plain dict with
          no eviction and no cap, keyed `(kind, ticker)` for three kinds.
Evidence: backend/app/ingest/backfill.py:145-152 (`self._last_fetch:
          dict[tuple[str,str], datetime]`, `_due`/`_mark`, never pruned);
          markets.py:376-406 does the read-through before the catalog check.
          This is the recurring class CLAUDE.md flags twice ("a filter is not a
          cap", "an unbounded query arms itself as the data grows") — here it
          grows with the catalog (218,516 markets × 3 kinds) in normal use and
          with arbitrary input under abuse. LAN-only, no auth, by design.
Fix sketch: 404 unknown tickers before read-through; bound `_last_fetch` (LRU).
```

```
[P2] API-009 — sub-resources return 200 for a ticker whose parent returns 404
Repro:    /api/markets/NOSUCH            -> 404 {"detail":"market NOSUCH not found..."}
          /api/markets/NOSUCH/siblings   -> 200 {"markets":[]}
          /api/markets/NOSUCH/tape       -> 200 {"ticker":"NOSUCH","trades":[]}
          /api/markets/NOSUCH/candles    -> 200 {"ticker":"NOSUCH","candles":[]}
          /api/markets/NOSUCH/orderbook  -> 200 (see API-008)
Expected: consistent 404. A mistyped ticker should not render as a real market
          page with four empty-but-valid panels.
Actual:   only the detail route validates existence.
Evidence: markets.py:233-237 (404) vs 267-272, 289-338, 341-373, 376-406 (no
          existence check).
```

```
[P2] API-010 — /openapi.json declares a typed response schema for 0 of 29 operations
Repro:    curl http://127.0.0.1:8080/openapi.json
          -> every 200 response is {"type":"object","additionalProperties":true}
          -> components.schemas = [ApproveRequest, HTTPValidationError,
             RejectRequest, TicketRequest, ValidationError] — request bodies only
Expected: for a project whose central invariant is "prices stay strings across
          the API boundary", the machine-readable contract should say so.
Actual:   every handler is annotated `-> dict[str, Any]`, so the schema permits
          any shape and any type. `/docs` and `/redoc` are served and are
          therefore useless as a reference. This is the structural reason
          API-012 was never caught.
Evidence: backend/app/api/routes/*.py — all 29 handlers return `dict[str, Any]`.
Fix sketch: Pydantic response models on the money endpoints at minimum, with
          price/fee/count fields typed `str`.
```

```
[P2] API-011 — a malformed WS frame closes the connection with no close frame and no error
Repro:    send {"action":"watch","tickers":12345}          -> socket closed
          send [1,2,3] / "hello" / 42 (valid JSON, wrong shape) -> socket closed
Expected: an error frame, or the command ignored — the documented drop reason is
          "a client that cannot keep up", not "a client that sent a typo".
Actual:   TCP FIN, no WebSocket close frame at all (verified: recv returns b""
          with no opcode 0x8). `str.get`/iteration raises, the broad
          `except Exception` at ws.py:168 swallows it at debug level, `finally`
          unregisters. The hub survives and other clients are unaffected — good
          — but the offending client learns nothing.
Evidence: backend/app/api/ws.py:147-174. Compare bad JSON (ws.py:150-152) which
          is correctly ignored and keeps the connection alive.
Aside:    frontend/src/useLiveFeed.ts:49 resets `attempt = 0` inside `onopen`,
          so a server that accepts and then immediately closes produces a 1s
          hot reconnect loop rather than backing off. Not reachable today (the
          frontend only ever sends string arrays); it becomes reachable the
          moment anything else does.
```

```
[P2] API-012 — /api/trading/state returns balance as lossy JSON numbers beside the exact string
Repro:    curl http://127.0.0.1:8080/api/trading/state
Expected: money as strings, per CLAUDE.md and per every other endpoint.
Actual:   "balance": {"cents": 8015, "dollars": "80.1587",
                      "portfolio_value_cents": 1895}
          `cents` and `portfolio_value_cents` are JSON *numbers*, and `cents`
          disagrees with `dollars` by 0.87¢ (80.1587 → 8015.87¢, not 8015).
Evidence: backend/app/api/routes/trading.py:199-203 — passes Kalshi's legacy
          integer-cents fields straight through. The truncation is the
          exchange's, but naming the lossy field `cents` invites the UI to use
          it. Today it does not: frontend/src/Trades.tsx:197 uses `.dollars`.
Verdict:  code should change (drop `cents`/`portfolio_value_cents`, or serialise
          them as strings). CLAUDE.md is explicit and is right.
```

```
[P2] API-013 — negative sizes cross the API boundary on the tick channel
Repro:    read /ws for 10s, parse copilot:ticks
Actual:   47 of 5,136 size/volume fields are negative (~0.9%), e.g.
          {"ticker":"KXPRESNOMD-28-GR","yes_ask_size":"-4777772.90"}
Expected: a quantity is not negative.
Evidence: backend/app/ingest/normalize.py:104-105 (`yes_bid_size_fp` /
          `yes_ask_size_fp`, no sign check); db/models.py:192-193.
Downstream, and why it matters here: backend/app/detectors/runner.py:498 feeds
          `market.yes_ask_size` in as `available_contracts`, i.e. executable
          depth for the sizing cap (README: "capped by ... executable depth").
          Latent only because all detectors are disabled.
Hand-off: root cause is ingest's; the sizing consequence is the detector/sizing
          agent's. Filed here because it is visible on the WS contract.
```

```
[P2] API-014 — /api/risk mixes JSON numbers and strings, and bankroll (money) is a float
Repro:    curl http://127.0.0.1:8080/api/risk
Actual:   "limits": {"bankroll_usd": 1000.0, "max_pct_per_market": 0.05,
                     "max_total_exposure_pct": 0.4, "daily_loss_limit_pct": 0.05, ...}
          "state":  {"bankroll_cents": "100000.0", "exposure_cents": "636.00000000", ...}
Verdict:  **partly defensible, partly drift.** The `*_pct` and `kelly_fraction`
          values are ratios, not money — a JSON number is fine. `bankroll_usd`
          is money and is a `float` all the way from config
          (backend/app/config.py:41 `bankroll_usd: float`). The tell is in the
          response: `"bankroll_cents": "100000.0"` — the trailing `.0` is the
          float origin surviving `Decimal(str(1000.0)) * 100`
          (risk.py:237, proposals.py:311). CLAUDE.md: "Money is `Decimal`,
          never `float`."
Also:     `/api/system` serialises the fee constants as floats —
          health.py:85-86 `float(schedule.base_taker_rate)` → `0.07`,
          `float(schedule.base_maker_rate)` → `0.0175`. Display-only, but it is
          the fee engine's own constants narrowed to binary floating point at
          the API boundary.
Fix sketch: `bankroll_usd: Decimal` in config; serialise as a string.
```

---

## P3

```
[P3] API-015 — README's "Fee fail-closed" safety row describes behaviour the code does not have (and CLAUDE.md says it should not)
README:   "Fee fail-closed | Unknown fee multiplier ⇒ market excluded, never guessed."
Actual:   `UnknownSeries` is raised only when `default_is_safe` is False —
          i.e. only when some *listed* series has a taker multiplier above the
          default. On the shipped schedule `/api/system` reports
          `"default_is_safe": true`, so an unlisted series is silently priced at
          the default and `UnknownSeries` can never fire.
          Confirmed: /api/fees/quote?...&ticker=NOSUCHSERIES -> 200, 1.7500¢.
Evidence: backend/app/core/fees.py:202-217.
Which side is wrong: **README**. CLAUDE.md is explicit and correct — "every
          series not listed takes the documented defaults" — and the
          `default_is_safe` guard is a sharper rule than the README's sentence
          (it refuses exactly when the default *could* understate). The README
          row is left over from the abandoned category-keyed design.
```

```
[P3] API-016 — README's routing matrix implies the live route depends on credentials; the code decides the route without them
README:   | live | prod | true | credentials: yes | live exchange |
Actual:   `resolve_route` (interlocks.py:89-111) returns LIVE_EXCHANGE from
          mode+env alone. The credential check is a separate interlock at
          approval time (interlocks.py:181-186, `no_credentials`). So
          /api/trading/state would advertise `execution_route: live_exchange`
          with no usable key, and only refuse when the operator clicks approve.
          Contrast the paper rows, where credentials *do* select the route
          (interlocks.py:108).
Which side is wrong: minor, and the code's behaviour is arguably safer (it does
          not quietly downgrade). README should add a note, or `posture()`
          should surface the credential state on the live row.
```

```
[P3] API-017 — 24 of 28 endpoints are documented nowhere
Actual:   only /api/signals, /api/report-card, /ws (README) and
          /api/pnl, /api/positions, /api/trading/state, /api/report-card (docs/)
          are named in any prose. No documented endpoint is missing — the drift
          is one-directional. There is no API reference section, and
          /openapi.json cannot serve as one (API-010).
```

```
[P3] API-018 — display formatters round away the sub-cent precision the backend preserves
Evidence: frontend/src/api.ts:682 `asCents(dollars, dp = 1)` — a market with
          `"price_level_structure": "deci_cent"` (confirmed live on
          KXMLB-26-WSH) quoting 0.5555 renders "55.6¢". The screener's bid/ask
          columns use the dp=1 default.
          frontend/src/api.ts:709-710 `asCount` — a fractional count ≥ 10
          renders via `toFixed(0)`, so 10.5 contracts displays as "11".
          CLAUDE.md notes "fractional sizes rendering as `0`" as a past bug;
          this is the same family, one notch less bad.
Note:     the float round trip itself is harmless at these magnitudes — every
          formatter guards `Number.isFinite` and the values are < 2^53. The
          loss here is the deliberate `dp`, not the double.
```

```
[P3] API-019 — the orderbook ladder re-derives the NO price in browser floats
Evidence: frontend/src/OrderBookLadder.tsx:21
            price: invert ? (1 - Number(p)) * 100 : Number(p) * 100
          This is the pattern backend/app/api/routes/markets.py:46-65
          (`_complement`) exists to eliminate — "This belongs here rather than in
          the browser because the browser has no exact arithmetic" — and which
          frontend/src/TradeTicket.tsx:51-56 documents as removed from the
          ticket. Display-only today (`l.price` only reaches `.toFixed(1)`), so
          P3; it is one edit away from feeding a `limit_price` again.
```

```
[P3] API-020 — `confirm` accepts any Pydantic-truthy value on the approval endpoint
Repro:    POST /api/proposals/267/approve {"confirm":"yes"}  -> 200, order placed
                                                                and filled
Actual:   "yes"/"y"/"on"/"1"/1/"true"/"TRUE" all coerce to True;
          "no"/"false"/"0"/0 coerce to False; 2/-1/[]/{}/null -> 422.
Verdict:  directionally safe (nothing that reads as "no" becomes True), and a
          client must still send the field with a truthy value — so this is not
          an approval bypass. But it is lax typing on the single most
          safety-critical field in the system, and `ApproveRequest`'s own
          docstring says "`confirm` is never defaulted true" while the type
          silently widens what counts as true.
Fix sketch: `confirm: StrictBool`.
```

```
[P3] API-021 — /api/fees/quote accepts absurd contract counts without complaint
Repro:    ?price_dollars=0.5&contracts=1e400
            -> 200 {"fee_cents":"1.7500E+400"}
          ?price_dollars=0.5&contracts=99999999999999999999999999
            -> 200 {"fee_cents":"174999999999999999999999998.2"}
          Same on POST /api/proposals/quote: contracts "1e400" -> 200 with
          "count":"1E+400" in the wire block, and "0.005" is accepted below the
          documented 0.01 granularity.
Expected: a bound. CLAUDE.md: counts are "fractional to 0.01", `Numeric(16,2)`.
          A quote the DB could not store is not a quote.
Actual:   only `contracts < 0` (fees) / `<= 0` (pricing) is checked.
Evidence: backend/app/core/fees.py:276-277; backend/app/trading/pricing.py:176-177.
```

```
[P3] API-022 — the WS watch set is unbounded
Repro:    {"action":"watch","tickers":[<1500 entries>]}      -> acked, all 1500 kept
          {"action":"watch","tickers":["X" * 2_000_000]}     -> acked, 2MB retained
Actual:   `client.tickers` is an unbounded per-client set built from client
          input (ws.py:156). Neither the count nor the element length is capped.
          Connection survives, hub unaffected. LAN-only and no auth by design,
          so severity is low — but it is per-client server memory an unauthenticated
          client chooses.
```

```
[P3] API-023 — the slow-client drop counter is never surfaced
Evidence: backend/app/api/ws.py:50,65 — `self.dropped += 1` on queue overflow,
          and nothing ever reads it. README promises "A client that cannot keep
          up has its messages dropped"; there is no way for the operator (or
          /api/system) to learn that it happened. A dropped proposal broadcast
          on a busy tab is silent — mitigated in practice only because
          Trades.tsx polls every 3s independently.
```

---

## Runtime state I changed (for the lead)

- **Nothing of mine is pending.** All proposals I created were rejected or
  expired. Final check: `pending_proposals: 0` from my traffic.
- **One demo-exchange fill is mine and persists**: proposal **267** →
  order **13** → 1 contract `KXMLB-26-HOU` YES @ 0.018, filled, route
  `demo_exchange`. It carries `net_edge_cents = "NaN"` — deliberately, as the
  evidence for API-002. **The lead may want that row removed before anyone
  reads the report card**, since it will make `avg_claimed_edge_cents` NaN for
  `(manual, demo_exchange)` once the position closes.
- Proposals 269, 271–281 (rejected) and ~21 short-TTL race-probe proposals on
  `KXMLB-26-MIA` (expired within 15s) are mine and are all decided.
- Junk tickers `AGENT3JUNK1..3`, `NOSUCH`, `NOSUCHTICKER` are now permanent
  entries in the API process's `Backfiller._last_fetch` (API-008) and wrote
  empty orderbook snapshot rows.
- Proposals **300–318**, **348**, order **17**, and the `KXMLB-26-WSH` /
  `KXNFLGAME-26AUG15DALSEA-SEA` positions are **not mine** — another agent was
  writing concurrently. I did not touch them.
- Kill switch untouched. `config.yaml` untouched. No code edited.

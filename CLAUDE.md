# kalshi-copilot — working notes for Claude

Self-hosted Kalshi analysis and trading copilot. Read this before changing
anything; several of the rules below were learned the expensive way.

---

## Hard constraints — do not violate these

1. **Two consent paths, and they are separately typed.** An order reaches
   Kalshi only through `Executor.approve_and_execute`, and only with exactly
   one of:
   - a human's explicit per-trade `confirmed=True`, or
   - a `MachineConsent` issued by `app/trading/autonomy.py`.

   **Never both** — an approval carrying both is refused (`ambiguous_consent`),
   because `AuditLog.actor` has one value and two claimants and picking one
   silently would make the audit trail a guess. `actor` is *derived* in the
   executor from which authority passed the interlocks, never accepted from
   the caller; a caller naming `autonomous` without a consent gets
   `actor_spoofed`.

   Autonomous mode is real and may fire on every route including
   `live_exchange`. It is not a flag — it is a gate, and the gate is what
   replaced the human's judgement. See §Autonomy.

   `trading.mode` remains `Literal["paper", "live"]` and a test still asserts
   exactly that. It now pins something different and just as important: mode
   says **where** an order goes, `autonomous:` says **who** approved it, and
   collapsing them would make "is this real money?" and "is anyone watching?"
   the same question. They are not.
2. **Live trading interlocks, two sets, neither substituting for the other.**
   - *Human live*: `KALSHI_ENV=prod` **and** `LIVE_TRADING=true` **and** the
     market ticker typed back in the UI.
   - *Machine live*: those first two **and** `AUTONOMOUS_TRADING=true` **and**
     `autonomous.enabled` **and** `autonomous.routes.live_exchange` **and** the
     evidence gate **and** budget remaining.

   The typed ticker is never asked of the machine — a machine typing a string
   it generated proves nothing about intent — and the machine's set is never
   satisfied by a human. A change that lets either stand in for the other has
   removed an interlock rather than moved it.
3. **Demo-first.** Default environment is Kalshi demo. Demo and prod use
   separate credentials; a demo key will not authenticate against prod.
   **Paper mode never touches a production exchange**, even with prod
   credentials present — it falls back to the local simulator. Changing
   `KALSHI_ENV` for a data reason must not quietly arm real money.
   Conversely `mode: live` without both env interlocks **refuses** rather
   than degrading to paper: a config that says live must not trade
   fictionally while the dashboard says otherwise.
4. **Docs win.** `docs.kalshi.com` is authoritative over anything written here
   or in the original spec. The machine-readable specs are the best source:
   `https://docs.kalshi.com/openapi.yaml` and `.../asyncapi.yaml`.
5. **Fee-aware everywhere.** Every edge/EV number shown anywhere must be net
   of fees and slippage. A gross edge is a lie.
6. **Secrets** live in `.env` and `secrets/` (both gitignored). The RSA key is
   mounted read-only and never logged. Never commit or echo either.
7. **LAN-only.** No public exposure, tunnels, or port-forwarding. The
   dashboard has no auth in front of it by design.
8. **Milestone by milestone.** Finish one, write demo notes, stop for review.

---

## The units trap — read before touching money

**The Kalshi API does not use integer cents anywhere.** This invalidated an
entire milestone's schema when it was discovered late; do not reintroduce it.

| Concept | Wire format | Internal |
|---------|-------------|----------|
| Price | `"0.5600"` — dollars, up to **6 decimals** | `Decimal` dollars, `Numeric(12,6)` |
| Count | `"10.00"` — **fractional to 0.01** | `Decimal`, `Numeric(16,2)` |
| Fee | `"0.022400"` dollars | `Decimal` cents, `Numeric(20,6)` |
| Realised P&L | — | `Decimal` cents, `Numeric(20,6)` |

**Nothing here is a whole number of cents, including fees.** The schedule
rounds fees up to a **centicent** (`$0.0001`), not a cent — "rounds up such
that the fee + positionCost is rounded to a centicent". One contract at 50c
costs **1.75c**, not 2c. Confirmed against real demo fills: 2 contracts at
20c were billed `$0.022400`.

This was wrong for three milestones and made every fee in the system ~14%
high on small orders.

- Tick size varies per market (`price_level_structure`), so sub-cent prices
  are real. Never round a quote on ingest.
- Contracts are fractional. Never assume integer size in fee or sizing math.
- Prices stay **strings** across the API boundary and in JSON to the frontend.
  Parsing to a JS `number` re-introduces the precision loss the backend
  avoided. Format for display; never compute in the browser.
- `taker_fee_cents("0.5600", ...)` is correct. `taker_fee_cents(56, ...)`
  raises — deliberately.

---

## Order direction — read before touching the execution rail

**Kalshi's order API quotes one book, from the YES side.** `bid` = buy YES,
`ask` = sell YES, and `price` on the wire is **always the YES price**,
whichever direction you are going.

| side/action | book side | wire price |
|-------------|-----------|------------|
| buy YES | `bid` | `p` |
| sell YES | `ask` | `p` |
| **buy NO** | **`ask`** | **`1 - p`** |
| sell NO | `bid` | `1 - p` |

"Buy NO at 30c" goes to the exchange as an **ask at 0.70**. Both halves have
to be right or the position is inverted — and **nothing catches an inversion
downstream**: the fee formula `P(1-P)` is symmetric, so a flipped direction
produces the same fee, the same notional, and a plausible confirmation. The
only guards are `app/trading/direction.py` and the tests around it. Never
inline this mapping anywhere else.

Internally we keep `side` (yes/no) + `action` (buy/sell) because that is how
a trader reads a ticket, and `limit_price` is the price on the **traded
side** — `0.30` for "buy NO at 30c". Conversion happens only at the wire.

Positions are the opposite convention: one **signed** number per market in
YES-equivalents (positive YES, negative NO), with `avg_price` as a YES price.
That is the only form in which exposure nets correctly, and it matches the
API's own signed `position_fp`.

---

## Non-obvious API facts (verified against live demo)

- **Order creation is `POST /portfolio/events/orders` (V2), not
  `/portfolio/orders`.** The legacy path is deprecated (no earlier than
  2026-05-06) and speaks integer cents — the exact unit mistake this codebase
  exists to avoid. V2 speaks fixed-point dollars. Confusingly, `GET
  /portfolio/orders` is still the read path; only creation moved.
- **`self_trade_prevention_type` is required** on order creation, and `GTT`
  is not a valid `time_in_force` — an expiring order is `good_till_canceled`
  plus an `expiration_time`.
- **`OrderStatus` from the API has only `resting|canceled|executed`.**
  "Partially filled" is something we derive from fill count, not read.
- **Writes are never retried.** A `POST` that times out may still have
  reached the matching engine, so `rest.py` raises on write timeouts and 5xx
  rather than retrying. 429 is safe to retry (rejected, not executed).
  Recovery is reconciliation by client order ID, never a second POST — and
  for three milestones that sentence was written in three places and
  implemented in none. The order was marked REJECTED, which is not a live
  status, so no sweep revisited it, and `reconcile_order` returns early
  without an exchange order ID, which is exactly the timed-out case. It now
  lives in `maintenance.recover_orphaned_orders`, which **adopts** what the
  exchange holds rather than re-placing it.
- **`GET /portfolio/orders` has no `client_order_id` filter.** Its parameters
  are `ticker`, `event_tickers`, `min_ts`, `max_ts`, `status`, `limit`,
  `cursor`, `subaccount` — checked against the OpenAPI spec on 2026-07-28.
  `client_order_id` *is* a required field on the returned Order object, so the
  match is made locally over recent pages. A miss therefore means "not in the
  pages we looked at", not "never placed", and the recovery pass must not
  treat one as proof.
- **The WebSocket requires auth even for public market-data channels.** REST
  public market data does not. This is why the market page reads through to
  REST: it works with no key at all.
- **`mutually_exclusive` means AT MOST one leg resolves YES**, not exactly
  one. It does not imply the set is exhaustive, and many are not:
  `KXNEWPOPE-70` is exclusive with 7 legs whose asks sum to $4.12. So
  *selling* every leg of an exclusive set is riskless (at most $1 pays out)
  but *buying* every leg is not — that needs some leg to be certain to win,
  which the API never tells you. See `app/detectors/set_arbitrage.py`.
- **Fees are keyed by SERIES, not category.** The schedule has no category
  dimension at all. It lists ~85 "Non-Standard Fees" *series tickers*; every
  series not listed takes the documented defaults of **taker M=1** and
  **maker M=0** — meaning most markets charge **no maker fee**. Ten series
  (including `KXBTCY` and `KXETHY`) are listed at 0/0 and charge nothing.
  An earlier design keyed this by category and left `crypto: null`, which
  excluded ~50,000 markets from proposals over a multiplier that does not
  exist. `category` is still on the Event, not the Market, and is still
  joined across for display and detectors — it just does not price anything.
- **`GET /portfolio/settlements` mixes units inside one object** — the only
  endpoint that does. `yes_total_cost_dollars`, `no_total_cost_dollars` and
  `fee_cost` are fixed-point dollar strings; `revenue` and `value` are
  **integer cents**. Parsing a cents field as dollars understates it 100x,
  silently. Settlement P&L is computed against our own `avg_price`, not the
  exchange's cost basis — the two diverge once a position is partly traded
  out, and a book with two sources of cost basis eventually contradicts
  itself. See `app/trading/settlements.py`.
- **A position held to settlement realises P&L through no fill at all.**
  `realized_from_fill` only fires when a position is *reduced by trading*, so
  until M5 the system booked the cost of every held-to-resolution thesis and
  none of its proceeds. The simulated book settles from the market's own
  `result`; exchange books settle from the endpoint above. Never cross them.
- **Kalshi temperature markets settle on INTEGER degrees.** "between 96-97°"
  is the two-outcome set {96, 97}, not the continuous interval — pricing it as
  continuous understates every bucket by ~2x (measured: 0.2611 vs 0.1324). And
  "greater than 96" means `T >= 97`, differing by the whole mass at 96. See
  `app/weather/distribution.py`.
- **Scheduled releases CLOSE the market before the data lands.** CPI's market
  stops trading at 12:25 UTC for a 12:30 print; FOMC at 17:59 for 18:00. There
  is no trade-the-news window — a catalyst is a *deadline*. Every such release
  closes 1-5 min before a quarter-hour boundary, which is how
  `app/news/calendar.py` infers the release time without a timezone database.
- **BLS and SEC serve an HTML "Access Denied" page with HTTP 200** to
  unrecognised user agents — a silent failure a status check passes. Identify
  properly via `news.user_agent`; the feed parser also refuses on root tag.
- **A daily LOW is assigned by the forecast period's END date; a daily HIGH
  by its START.** An NWS night period runs 18:00 local to 06:00 next morning
  and its minimum falls in the *following* calendar day — "Monday Night" is
  Tuesday's low. Keying lows by start files every one a day early. See
  `daily_low` in `app/weather/nws_parse.py`; forecast-error calibration is
  keyed by `(station, measure)` for the same reason.
- **Two weather families, two settlement sources.** Daily high/low settles on
  the **NWS** Climatological Report (Daily); the hourly family (`KXTEMPNYCH`)
  settles on **The Weather Company**, for which we have no feed — so NWS data
  there is a proxy for a different source and those markets are refused.
- **The NWS spells units three ways.** Observations use
  `{"unitCode": "wmoUnit:degC"}`; `/forecast` uses a bare number plus
  `"temperatureUnit": "F"`; the raw gridpoint product uses `uom`. Trust the
  unit code, refuse an unrecognised one. `?units=si` flips `/forecast` to
  Celsius, so the client deliberately sends no `units` parameter.
- **Series tickers are names, not a namespace.** `KXLOW` is Lowe's Companies
  Inc. and `KXSNOWFLAKE` is Snowflake Inc. A prefix match would route earnings
  markets to a weather station. `station_for_series` is an exact dict lookup
  and must stay one.
- **`category` is NOT an underlying.** "Crypto" contains BTC, ETH, SOL and
  XRP. The stale-quote detector selected on `category == "Crypto"` and priced
  all of them against `BTC-USD`; an ETH contract with a $1,969 strike against
  Bitcoin at $65,154 reads as decisively YES and reported a **+72c edge** on a
  market quoted at 26c. Every number was arithmetically right and they
  described different assets. 1,762 markets were affected. Only the **series
  ticker** says what a market tracks — see `reference_symbol_for()` in
  `app/detectors/stale_quote.py`, which refuses rather than defaulting. The
  detector's *selection* now uses the same ticker prefixes that guard names,
  not `category`: category is copied onto Market from the parent Event by a
  backfill, so 7,216 active markets were invisible to it at any given moment,
  and it is the field this file already says must not decide anything.
- **Crypto markets settle on a CF Benchmarks index, not on an exchange print.**
  BTC settles on the BRTI, ETH on ETHUSD_RTI, and usually as the *simple
  average of the sixty seconds* before a stated instant. `ingest/spot.py` polls
  one venue's **last trade** — different publisher, different statistic, and on
  the Binance source a different instrument (BTC**USDT**). Near a strike that
  basis is the size of the edge being claimed. It is a reference for deciding
  whether spot has decisively cleared a level; never call it the settlement
  price.
- **A `ticker` websocket message is a partial snapshot.** `normalize_ticker`
  emits only the fields that arrived and parsed, so a batch of rows for a
  multi-row upsert is heterogeneous by nature — and `insert().values(rows)`
  takes its columns from the *first* row while an `ON CONFLICT` set built from
  the union of the rows' keys can name a column the INSERT never supplied.
  `_update_tickers` pads every row to a fixed column list and coalesces on
  conflict, so an absent field keeps what is stored. "Absent" must never read
  as "cleared": a NULLed `yes_bid` drops the market out of every detector query
  that requires a two-sided book.
- **A websocket `seq` counts the SUBSCRIPTION, not the market.** One
  `orderbook_delta` subscription covers every ticker in it and numbers all
  their messages from one counter, so a single market's deltas are *not*
  consecutive — verified live with 65 markets: `86, 87, 88, 89, 90, 92, 95,
  97` for one ticker. `OrderBook.apply_delta` compared that per market and
  marked a book stale on nearly every message: **21 of 65 books stale within
  sixty seconds**, permanently. The per-sid tracker in `ws.py` logged zero
  gaps over the same period, which is how the two were told apart. It was
  silent because a stale book is not *recorded* rather than raising, so the
  symptom was thin data three milestones later. There is no per-market
  sequence to use instead — the delta body has none — so gap detection lives
  only in `ws.py`. `orderbook.py` records `seq`, refuses a replayed (lower)
  one, and judges nothing else. Six tests encoded the same misunderstanding
  and stayed green throughout.
- **`resync_needed` must actually be drained.** For three milestones nothing
  read it, so a book that went stale stayed stale for the process's life.
  `_book_heal_loop` in `ingest/main.py` forces a reconnect once enough books
  are waiting; a reconnect is a resubscribe is a fresh snapshot.
- **The tape's `taker_side` is `yes`/`no`, never `buy`/`sell`.** Verified
  against 9,604 live rows. A flow detector written to the obvious vocabulary
  finds zero sweeps forever and looks like it is working.
- **Candle `price` OHLC is null when no trades occurred** in that period,
  which is most periods in a thin market. Fall back to the quote midpoint or
  charts render empty.
- **Candle `end_period_ts` is the period END**; everything internal keys by
  period START. Use `period_start()`.
- **Rate limits**: documented tiers describe *authenticated* accounts, some
  endpoints cost more than 10 tokens, and 429s carry no `Retry-After`. The
  limiter is AIMD — halves on 429, creeps back on success. Do not replace it
  with a fixed rate; the first version produced 304 rejections in one sync.
- Signing: timestamp in **milliseconds**, sign the path **without** the query
  string, RSA-PSS with digest-length salt.
- Recommended hosts are `external-api.kalshi.com` /
  `external-api-ws.kalshi.com`; the `api.elections.kalshi.com` hosts are
  documented alternatives and also work.

---

## Fail closed, always

When a cost or a book cannot be trusted, the system **refuses** rather than
guessing. Preserve this in anything new:

- Unknown fee multiplier → `UnverifiedFeeCategory` raised, market excluded
  from proposals. An understated fee inflates every downstream EV number.
- Orderbook sequence gap → book marked stale, every read raises. A book that
  guesses across a gap looks plausible and is wrong, which is exactly how an
  arb detector talks you into a trade that does not exist.
- A malformed price from the wire raises rather than becoming `0` — a price
  that quietly reads as zero looks like free money.

A guard that silently inspects the wrong thing is worse than no guard. One
did exactly that (`--mark-verified` split on `"categories:"`, which also
matches inside `maker_free_categories:`) and happily marked an unverified
schedule as verified. Parse structured data; do not slice strings.

**And a guard that inspects a value with no effect is the same bug wearing a
better suit.** `--mark-verified` also required `formula.rounding_increment_dollars`
to be *set*, while `fees.py` hardcodes `CENTICENT` and never reads the key: an
operator changing it because the PDF had changed got a verification pass and
no behaviour change. It now compares the two and refuses on a mismatch. Ask
what a check would *fail* on before adding it.

**A refusal that returns `None` is not a refusal, it is a disappearance.**
`propose_finding` dropped every single-leg finding with a bare `return None`,
so an enabled detector that had found an edge, sized it with Kelly and named
its binding cap looked identical to one that had found nothing — for three
milestones. It now raises rather than returning, and the worker counts and
prints the refusal code.

**This paragraph named the wrong code and claimed the wrong containment for
several milestones, and both are corrected here.** The code is `not_costable`
— `single_leg_unsupported` exists nowhere in the tree. And there *is* a
single-leg proposal path: `detectors/base.py` routes a single-leg finding to
`create_proposal` through the same guards and the same pending queue, so set
arbitrage is **not** the only detector that can reach it. As of this writing:

| detector | enabled | reaches the queue? |
|---|---|---|
| `set_arbitrage` | yes | yes, multi-leg |
| `stale_quote` | yes | **yes, single-leg** — has both `price` and `size_hint` |
| `resolution_sniper` | yes | no — `not_costable`, it has a price but no `size_hint` |
| `weather` | no | no — has `size_hint`, but its evidence dict carries no `price` |
| everything else | no | no — neither |

Re-derive that table rather than trusting it; the question "which detectors
can reach the queue" is now also the question "what can the machine trade",
which makes a stale answer here considerably more expensive than it was.

The same shape shows up in **tri-state fields**. `Market.result` is `''` for
an open market, not NULL — 153,808 rows — so `is not None` calls every open
market settled. And a `settled: bool | None` field is a trap in the other
direction: `if m.settled:` drops every NO-resolved market, halving the
outcome sample and biasing what is left toward YES. Name the value
(`resolved_outcome`) and ask a separate question for "is it known".

The backtester refuses too, and that is its main job — see below.

---

## Autonomy

The machine can approve and place trades with no human click, on any route
including real money. What follows is why it is safe to have built that, and
what would make it unsafe again.

**The gate replaced a judgement, so it has to be one.** Before autonomy, the
human click was the *only* quality control in the system: `Verdict.EDGE_SHOWN`
and `CoverageReport.usable` existed but were read by the dashboard and by
nothing in the trading path. Removing the click without adding a gate would
have left zero. So `autonomy.evaluate()` refuses unless the report card has
**measured** a positive edge for that `(detector, route)` — bootstrap CI lower
bound above zero, never the mean — and backtest coverage is usable.

**Evidence is cached, never recomputed per decision.** `detector_reports` runs
six capped SQL queries plus a 10k-resample bootstrap per pair; at a 10s
decision interval that would dominate the worker. A 300s refresh loop holds the
snapshot in-process. It is *also* published to Redis for the API to display —
but **the gate never reads it from there**, so no stale key and no other
process can authorise a trade. A failed refresh leaves the previous snapshot
in place and lets it age out; it never writes a partial or empty one, because
an empty snapshot refuses identically to a genuine absence of evidence and the
operator needs to tell those apart.

**Publishing needs a key, not only a channel** — and this was got wrong first
time. The refresh loop runs in `worker`; `/api/autonomy` is served by `api`.
Different processes, no shared memory, so the dashboard cannot read
`cached_evidence()` and showed `evidence: null` forever. A pub/sub `publish` on
its own does not fix it either: a subscriber that was not listening at the
instant of the publish learns nothing, and the API is usually not listening.
So `publish_evidence` writes **both** — `copilot:autonomy:evidence` with a TTL
for "what is true now", and the channel for a dashboard already open. The TTL
matters: without it a dead worker's last report card sits on screen looking
current. `published_evidence()` returns a plain `dict`, deliberately not an
`Evidence`, so that wiring the display path into the gate would not typecheck
and would not run.

**The budget is derived from `audit_log`, not counted.** Trades per hour, per
detector per hour, daily risk and the repeat cooldown all come from
`kind='proposal.approved' AND actor='autonomous'` rows. Exact across restarts,
and the ledger *is* the audit trail rather than a second source that can
disagree with it. This is why `actor` is derived in the executor rather than
passed: it is not a label, it is the index.

Note the direction: `proposal.approved` is written *before* placement, so a
failing placement burns budget rather than retrying forever. Conservative, and
deliberate.

**Named hazard: the re-proposal loop.** `_guard_duplicate` only refuses while a
proposal is *pending*. Today a human is the rate limiter, and nothing in the
code was. Without a cooldown: detector proposes → gate approves seconds later →
the duplicate guard clears → the detector re-derives the same edge on its next
20s scan → approve again, repeatedly trading one market until the per-market
exposure cap finally binds. `budget.repeat_cooldown_sec` is the mitigation.
This hazard is invisible from any single file, which is why it is written down
here.

**Zero refuses.** Every budget ceiling defaults to 0 and 0 means "no
allowance", never "unlimited" — same convention as
`news.headlines.daily_budget_usd`. There is no way to express unlimited.

**The config cannot waive its own gate.** `Config`'s root validator refuses at
*load* time: `min_trades` below `backtest.report_card_min_trades`, coverage
waived while an exchange route is armed, `require_edge_shown` or
`require_manual_rearm` off while live is armed. A gate that can be talked out
of its own threshold is not a gate, and boot is the moment to find out.
Coverage may be waived on the **simulated route only** — that route exists to
*generate* the evidence the gate wants and cannot spend money doing it.

**Arming takes four facts, and config holds only two.** `autonomous.enabled`
and `autonomous.routes.<rail>` in `config.yaml`; `AUTONOMOUS_TRADING=true` (plus
`KALSHI_ENV`/`LIVE_TRADING` for live) in `.env`. The environment half is
deliberately outside the settings UI's reach: the dashboard has no auth in
front of it by design, so arming the machine must take a file edit and a
restart. Disarming, symmetrically, *is* reachable at runtime.

**The disarm latch is a latch, not a cooldown.** It does not clear itself
(`require_manual_rearm`, forced true whenever live is armed), and it has no TTL
in Redis — same reasoning as the kill switch. Two triggers fire at **1**, not
at a threshold: an `order.submit_ambiguous` (an order may exist that we cannot
see; trading on top of an unknown position is the worst available action) and
a `PARTIAL` outcome (the executor already calls that "real, and it needs a
person" — under autonomy it must actually get one).

Evidence staleness **refuses but does not latch**. Latching on a transient
failure makes a human clear a condition that clears itself, which trains them
to clear latches.

**The kill switch and the disarm latch are different stops.** The kill switch
halts everything including manual approvals and cancels resting orders; the
latch stops only the machine. The UI must say so — an operator reaching for
the wrong one in a hurry is a foreseeable failure.

**No audit row per gate refusal.** Every existing `AuditLog` kind is a state
change; ten refusals every ten seconds is not, and it would swamp the table
`/api/audit` reads. Refusals are logged and published live. What an operator
wants is the current binding reason per pair, not the same reason 8,640 times
a day.

**On this deployment the gate refuses everything, and that is it working.**
Coverage fails all four hard axes — measured 2026-07-29 with the gate armed on
`demo_exchange` in a throwaway container: `too_few_markets`,
`window_too_short`, `too_few_settled`, `gaps_too_large`. Demo and live autonomy
are therefore unreachable until the data matures — by construction, not by bug.
Do not "fix" this by lowering a threshold; the thresholds are the product.

The refusal ladder, measured on that run, is worth reading because each rung is
a different reason:

| proposal | refusal |
|---|---|
| a manual ticket | `manual_proposal` |
| `stale_quote`, coverage enforced | `coverage_unusable` |
| `stale_quote`, coverage waived | `insufficient_trades` |

That last one is the interesting one. `stale_quote` on `demo_exchange` reads a
bootstrap **lower bound of +61.6c** over 16 decisions — a spectacular number,
and refused, because the floor is 20. This is precisely the case the report
card exists for: the most persuasive figure the system can produce is also the
one carrying the least information, and there is no override.

---

## Layout

```
backend/app/
  main.py            FastAPI app + SPA serving
  settings.py        secrets/endpoints from .env
  config.py          validated config.yaml loader
  core/
    fees.py          ⭐ ALL fee math. Nothing else may compute a fee.
    money.py         fixed-point parsing
    logging.py       secret redaction
  kalshi/
    auth.py          RSA-PSS signing
    ratelimit.py     AIMD token buckets
    rest.py          REST client, pagination, 429 backoff
    ws.py            websocket, reconnect, per-sid sequence tracking
    orderbook.py     local book, gap detection
  ingest/
    catalog.py       catalog sync + category backfill
    normalize.py     API payloads -> ORM rows
    streams.py       tape, candles, book snapshots
    backfill.py      REST read-through for the market page
  btc/
    vol.py           ⭐ driftless lognormal + EWMA vol; refuses, never guesses
    history.py       minute-bucketed spot reader (sampling matters — see docs)
  weather/
    rules.py         ⭐ parses settlement rules; NWS vs The Weather Company
    stations.py      ⭐ series -> station. Every row is a CLAIM, not API data.
    distribution.py  ⭐ INTEGER-degree buckets; not a continuous interval
    calibration.py   measured forecast error by lead time; no default sigma
    nws_parse.py     api.weather.gov payloads; unit code is authoritative
    client.py        NWS HTTP transport
  detectors/
    set_arbitrage.py ⭐ pure set-arb math; sell side is safe, buy side is not
    stale_quote.py   spot-vs-strike; ⭐ owns the series->underlying map
    undervalued_screener.py  research feed; structurally exposes no edge
    whale_flow.py    large prints/sweeps; confidence hard-capped
    longshot_calibration.py  Wilson intervals; refuses below the sample floor
    resolution_sniper.py  settlement lag; research only without a source
    base.py          Detector protocol + signal recording
    runner.py        the live detectors, each with its refusal rules
  trading/
    direction.py     ⭐ (side, action) <-> bid/ask. Never inline this.
    interlocks.py    execution routing + every safety check
    autonomy.py      ⭐ the ONLY issuer of MachineConsent; gate + budget + latch
    risk.py          ⭐ portfolio limits: exposure, daily loss, cooldown
    sizing.py        Kelly sizing; every cap is a ceiling, never a floor
    pricing.py       fee-aware ticket costing (calls fees.py, owns no fee math)
    proposals.py     proposal lifecycle: create, expire, decide
    executor.py      ⭐ the ONLY module that can cause an order to exist
    paper.py         pessimistic fill simulator
    positions.py     signed position + realised P&L accounting
    settlements.py   ⭐ held-to-resolution P&L; two books, two sources
  backtest/
    coverage.py      ⭐ the gate: refuses a backtest whose data cannot support one
    replay.py        pure event replay; look-ahead blocked structurally
    stats.py         expectancy + bootstrap CI, Brier, drawdown, verdict
    engine.py        the only part of the backtester that runs SQL
    report.py        per-detector report card over real fills and settlements
  worker/
    maintenance.py   proposal expiry, order auto-cancel, reconciliation
  api/routes/        HTTP endpoints
  api/ws.py          browser relay (shared Redis subscription)
frontend/src/        React + Vite + TS
data/                fee_schedule.yaml
scripts/             operational scripts
```

---

## Verification — run these before claiming anything works

Everything runs in Docker; no host toolchain needed.

**`api`, `worker` and `ingest` are separate images from the same Dockerfile.**
`docker compose build api` alone leaves the worker running old code. That
happened twice while fixing the cross-asset bug above and made a correct fix
look like it had failed. Build all three, or just `docker compose build`.

```bash
docker compose up -d --build
docker compose ps                                              # 5 healthy

docker compose run --rm --no-deps api python -m pytest tests/ -q
docker compose run --rm --no-deps api ruff check app/
docker compose run --rm --no-deps api mypy app/core app/config.py app/settings.py

# maintenance container: only service with write access to data/
docker compose run --rm tools python scripts/refresh_fee_schedule.py

# backtester: refuses on thin data and says which threshold it missed
docker compose run --rm --no-deps api python scripts/backtest.py --days 30
```

**Two config files, and mixing them up breaks the safety tests.**
`config.example.yaml` is committed and holds the project's defaults —
everything disarmed. `config.yaml` is **gitignored**, is this box's live
state, and is what actually loads. `test_config.py` asserts the shipped-safe
posture against the *template*, and the template is baked into the image by
the Dockerfile (it is not bind-mounted, because the point is to test the
committed copy). While these were one file, enabling a detector made
`test_every_detector_ships_disabled` fail — the assertion and the thing it
asserted about were the same bytes. **Add any new key to both**; a drift test
compares the key sets and names what is missing.

**Gitignored does not mean safe from a checkout, and this destroyed the live
config once.** `.gitignore` stops git *tracking* a file; it does nothing to
stop a checkout writing over one. Every branch predating the split still
tracks `config.yaml`, so `git checkout <older-branch>` overwrites the
operator's live config with that branch's copy without a word, and a `git pull`
through the removal commit then deletes it. Observed on the merge that
introduced the split: `git checkout` of the base branch replaced it, the
fast-forward deleted it, and the only surviving copy was the one the running
containers still had bind-mounted —
`docker compose exec -T api cat /app/config.yaml > config.yaml`. With the stack
down it would have been gone. `cp config.yaml config.yaml.bak` before touching
branches, and treat the worktrees under `.claude/worktrees/` as carrying the
same hazard until the split is merged into each.

**`compose run api pytest` tests the image, not your working tree.** Only
`config.yaml`, `data/` and `secrets/` are bind-mounted; `app/`, `tests/` and
`config.example.yaml` are baked in at build time. Without the `--build` above, a green suite is
green for the code you last built — the run that exposed this reported 1,292
tests while the tree on disk held several hundred more. Either build first, or
bind-mount the source (`-v ./backend:/app` plus `config.yaml`, `data/`,
`scripts/`, since those live above `backend/`).

**Compare the collected count against the tree, not against a number in this
file.** At `1ee5cd3` that was **1,438 test functions** on disk, collecting
**~1,557** cases once `parametrize` is expanded. Any figure written here goes
stale within a milestone, which makes it useless as the tripwire it is being
offered as; `python -m pytest tests/ --collect-only -q | tail -1` is the
number that cannot be wrong.

**Actually run the stack and look at the result.** Every milestone so far has
surfaced bugs that passed tests and lint: hypertables silently not created,
a liquidity score of −450 on a 0–100 scale, fractional sizes rendering as
`0`, a chart axis showing −20¢ on a 0–100¢ instrument. Screenshot the UI
(Playwright) when changing it.

---

## Status

| Milestone | State |
|-----------|-------|
| M0 scaffold, compose, fees | done |
| M1 ingest + storage | done |
| M2 dashboard core | done |
| M3 HITL approval/execution rail | done |
| M4 detectors wave 1 | done (all three; none has signalled live yet) |
| M5 risk layer + notifications | done (Web Push deferred — needs TLS) |
| M6 BTC engine + detectors wave 2 | done (leaderboard has no API; not built) |
| M7 weather engine | done (needs ~30d of history before it prices) |
| M8 news/catalyst engine | done (LLM tier guarded, not built — no key) |
| M9 backtester + hardening | done (refuses on today's data — by design) |
| M10 autonomy gate | done (gate refuses on today's data — by design) |

Branch: `claude/kalshi-copilot-build-bgyv2d`

**The autonomy rail is now wired end to end**, and for a while it was not:
`MachineConsent`, the interlock guards and the config validators landed before
`app/trading/autonomy.py` existed, so nothing could mint a consent and three
docstrings described a gate that was not there. That was safe — nothing could
trade — but it is the failure mode this file warns about, a comment that is
load-bearing right up until it is wrong. The pieces now are: `autonomy.py`
issues consents, `_autonomy_sweep_loop` in `worker/main.py` is the only loop
that approves anything, `_autonomy_evidence_loop` keeps the snapshot fresh,
and `/api/autonomy` shows an operator the numbers and the binding reason.

### Open items for the operator

- **`leaderboard_watcher` cannot be built.** Kalshi publishes no leaderboard,
  trader ranking, or public profile endpoint — checked against the OpenAPI
  spec on 2026-07-27. The only surfaces naming a counterparty are RFQ and
  block trades, which are yours alone. It is registered as a detector that
  logs a refusal when enabled, because one silently missing from the registry
  looks identical to one that runs and finds nothing. Do not add scraping.
- **`ingest.scanner.max_markets` and `series_filter` are read by nothing.**
  The ticker channel is subscribed with no market list — i.e. every market on
  the exchange — which is deliberate (the screener ranks against the whole
  catalog) but makes both keys a lie: an operator capping the scanner at 500
  still gets 149,000. Ingest now logs a warning saying so at startup. Resolve
  it in one direction or the other: delete both keys, or subscribe narrowly
  and accept that the screener's percentile then describes the subscription.
  Same question, smaller stakes, for `set_arbitrage.require_full_depth`,
  `resolution_sniper.min_source_confidence`, `backtest.pessimistic_fills` and
  `bitcoin.use_deribit_implied` — all documented in `config.yaml`, none read.
  Note two of those would *weaken* a refusal if they were wired as written
  (partial-depth set arb, a friendlier backtest fill model), so deleting is
  the right direction for those.
- **The weather engine is enabled outside the `detectors:` block**, so
  `DetectorsConfig.enabled_names()` cannot see it and it was missing from
  `/api/system` and the worker's boot log while it scanned. Use
  `detectors.base.enabled_detector_names(config)`, which adds it; `app/main.py`
  still prints the narrow list.
- **All four crypto underlyings now have a spot feed** — BTC, ETH, SOL and XRP,
  polled per symbol from `bitcoin.spot_symbols` and fanned out concurrently on
  one 3s tick. Before this, only BTC was polled and the stale-quote detector
  refused **1,353 of the 1,730 markets it selected** for want of a reference;
  that refusal was correct and it was 78% of the universe.

  `SPOT_SOURCES` is now `source -> symbol -> (url, path)` and every pair is
  written out longhand. Do not template it from the symbol: Kraken's result
  keys are `XXBTZUSD`, `XETHZUSD`, `XXRPZUSD` and — with neither prefix —
  `SOLUSD`, and a templated URL silently invents an endpoint for any symbol
  handed to it. An unlisted pair raises; there is no default symbol, because
  reaching for the nearest one is the wrong-asset bug this file already
  describes twice. **Binance's rows are unverified**: that endpoint refuses
  this host's region, so they are the documented symbol substitution only.

  **A new symbol prices nothing for its first ~31 minutes.** `horizon_sigma`
  needs `MIN_RETURNS`+1 one-minute buckets and refuses below that, so a fresh
  feed is silent until the live poller has accumulated them — correctly silent,
  but it means "no signals yet" is the expected state right after a deploy, not
  a bug to chase. `fetch_minute_candles` exists to close that gap and is now
  per-symbol, but **it is still wired to nothing**; that is the follow-up.
- **Detector queries must be projected and bounded.** `select(Market)` with
  no columns and no cap killed the worker outright — 122,887 active markets
  each carrying the full `raw` JSONB payload, no traceback, just a process
  that died and restarted. It had worked an hour earlier at a smaller catalog:
  an unbounded query arms itself as the data grows. Project the columns, push
  the filter into SQL, cap the rows.

  **This recurred, bigger, and went unnoticed for three milestones.** The M9
  audit found the same pattern in `worker/calibration.py` matching **152,263
  rows / 201 MB of JSONB every 300 seconds**, on a loop that starts
  unconditionally whether or not the detector is enabled. Projecting and
  pushing an anti-join into SQL took it to **97 rows**. A filter is not a cap:
  every one of these queries *was* bounded, by a close-time horizon or a
  lookback window or a series list — bounds on today's data, not on the query.
  `ingest/catalog.py` had a third instance, loading all 217,258 tickers into a
  Python set every sync to detect new listings; Postgres reports that for free
  via `RETURNING (xmax = 0)`.

  **A cap also needs an `ORDER BY` and a log line.** Every `MAX_*_ROWS` in the
  detectors truncated in silence and in whatever order Postgres returned:
  measured, the undervalued screener discarded **68,110 of 88,110** eligible
  markets, and because its score is a volume percentile *relative to the rows
  it got*, which 23% arrived decided every rank it emitted. Deterministic
  ordering also has to point the right way — a flow detector fetching the tape
  `ORDER BY ts LIMIT n` throws away the recent end, which is the only end that
  matters. Cap deliberately, order towards what the query is for, and say so
  when the cap binds (`_warn_if_capped` in `detectors/runner.py`).

  The same applies to a *pending-work* queue. `backfill_outcomes` ordered by
  id and skipped rows whose market resolved `void` — so those permanently
  unfillable rows sat at the head forever, and once they exceeded the cap no
  newer observation would ever have been resolved again. Push the "can this
  row ever be finished?" test into SQL so the head of the queue always moves.
- **No detector has signalled on a genuine edge yet.** The full path was
  exercised by dropping `min_net_edge_cents` negative so set-arb would
  propose regardless — 42 multi-leg proposals from live books, one approved
  to the demo exchange. That proves the plumbing, not the strategy. A real
  edge has still never appeared, which on liquid two-sided books is the
  expected answer.
- **All risk limits are now enforced** (M5): `max_pct_per_market`,
  `max_total_exposure_pct`, `daily_loss_limit_pct` and
  `cooldown_after_consecutive_losses`, alongside the queue-depth cap and
  duplicate guard. See `app/trading/risk.py` and `docs/m5-demo-notes.md`.
- **Web Push is not built and `notifications.web_push_enabled` defaults
  false.** The Push API and service workers need a *secure context*; the
  dashboard is plain HTTP on a LAN address by design, so a service worker
  cannot register. Alerting is in-tab only (title, favicon badge, audio).
  Getting alerts with the tab closed needs TLS on the dashboard first — a
  self-signed cert or a local CA — which is an operator decision, not
  something to work around in the frontend.
- **The watchlist goes stale.** Set arbitrage only considers events where
  *every* active leg is in `ingest.watchlist`, so as events settle the
  coverage decays. Regenerate from the top mutually-exclusive events by 24h
  volume; the cap is 100 markets.
- **No migrations.** `create_all` plus a boot-time enum-label sync. Schema
  changes still need tables dropped by hand, and the trading tables have
  changed shape several times.
- `bitcoin.enabled: true` is what starts the spot poller. Without it the
  stale-quote detector has no reference and emits nothing.

- **The backtester refuses on this deployment's data, and that is the
  feature.** Kalshi has no historical orderbook endpoint, so the only book
  data that will ever exist for a past moment is the snapshot ingest happened
  to take: 1,938 rows over 79 markets across 9.8 hours. `coverage.py` names
  each measured number against its threshold rather than saying "insufficient
  data", because the operator needs to know whether to wait a week or change
  the config. `--ignore-coverage` runs it anyway and keeps the refusals
  attached; the result is not evidence.
- **Book snapshots were written far less often than configured**, and the
  cause was the `seq` bug above: stale books are not recorded, so a fifth of
  the watchlist wrote nothing. Measured median gap before the fix was **84.8
  minutes** against a 1-second throttle. Re-measure before trusting any
  backtest window that spans the fix.
- **The report card's unit is a decision, not a fill.** A five-leg set
  arbitrage settles as five rows; counting them as five trades inflates `n`
  fivefold and shrinks the confidence interval by √5 on perfectly correlated
  outcomes. The interval is what decides whether a detector sees real money,
  so this is the most dangerous arithmetic error available. Group by proposal.
  Where attribution is genuinely ambiguous — two detectors, one market, one
  route — the event is **dropped, not apportioned**.
- **Use the bootstrap interval, not Wald.** A binary trade's P&L is a
  two-point distribution and heavily skewed away from 50c, which is the regime
  a 20-trade report card lives in. Measured: on 29 wins at +9.98c and one loss
  at −90.02c, Wald claims an edge (`[+0.11, +13.18]`) and the bootstrap
  refuses (`[−0.02, +9.98]`). Wald also returns lower bounds below the
  worst average that can physically occur. Note the bootstrap is *not*
  generally wider — over 175 samples it was narrower in 128; the property that
  holds is asymmetry.
- **An equity curve for `max_drawdown` must start at zero**, before the first
  trade. Otherwise the first trade's result *is* the opening peak and an
  opening loss reports no drawdown at all — an error that only ever flatters.
  `report.py` got this right with a comment explaining why, and `engine.py`
  did not: `replay()` appends one point *after* the fills at each instant and
  no pre-trade point, so a run that paid 1.75c of fees reported "max drawdown:
  0c". Knowing the rule is not the same as applying it at every call site —
  `stats.max_drawdown` takes a caller-supplied curve, so every caller has to
  prepend `starting_equity_cents`.
- Notifications are **first-party only** (PWA Web Push, audio, favicon
  badge). The original spec mentioned ntfy/Telegram once in a milestone list;
  that contradicts two more detailed sections and was resolved as a drafting
  leftover. Do not add third-party push.

---

## Credentials on this machine

If this repo is on the test VM, it may hold **demo** Kalshi credentials.

- **Demo only. Never put production credentials here.** Demo has no real
  money, which is what makes agent shell access acceptable.
- Do not print, echo, or commit `.env` or anything in `secrets/`.
- Placing demo orders during M3+ verification is expected and fine. Placing
  production orders is not, and the interlocks in constraint #2 exist to make
  it hard to do by accident.

---

## Style

- Pragmatic, few dependencies. Match surrounding code.
- Unit tests for every detector's math and all fee/EV calculations.
- `mypy` strict on `app/core`, `app/config.py`, `app/settings.py`.
- Comments explain *why*, especially where a subtlety cost real debugging.
- Money is `Decimal`, never `float`.

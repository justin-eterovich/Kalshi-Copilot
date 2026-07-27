# kalshi-copilot — working notes for Claude

Self-hosted Kalshi analysis and trading copilot. Read this before changing
anything; several of the rules below were learned the expensive way.

---

## Hard constraints — do not violate these

1. **Human-in-the-loop, always.** No order reaches Kalshi without explicit
   per-trade approval. There is **no auto-trade mode and none may be added**,
   not even behind a flag. `trading.mode` is `Literal["paper", "live"]` and a
   test asserts that. If a task seems to require automatic execution, stop and
   ask.
2. **Live trading needs three interlocks**: `KALSHI_ENV=prod` **and**
   `LIVE_TRADING=true` **and** per-trade confirmation in the UI (typing the
   market ticker, on the live route).
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
  Recovery is reconciliation by client order ID, never a second POST.
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
- **`category` is NOT an underlying.** "Crypto" contains BTC, ETH, SOL and
  XRP. The stale-quote detector selected on `category == "Crypto"` and priced
  all of them against `BTC-USD`; an ETH contract with a $1,969 strike against
  Bitcoin at $65,154 reads as decisively YES and reported a **+72c edge** on a
  market quoted at 26c. Every number was arithmetically right and they
  described different assets. 1,762 markets were affected. Only the **series
  ticker** says what a market tracks — see `reference_symbol_for()` in
  `app/detectors/stale_quote.py`, which refuses rather than defaulting.
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
    risk.py          ⭐ portfolio limits: exposure, daily loss, cooldown
    sizing.py        Kelly sizing; every cap is a ceiling, never a floor
    pricing.py       fee-aware ticket costing (calls fees.py, owns no fee math)
    proposals.py     proposal lifecycle: create, expire, decide
    executor.py      ⭐ the ONLY module that can cause an order to exist
    paper.py         pessimistic fill simulator
    positions.py     signed position + realised P&L accounting
    settlements.py   ⭐ held-to-resolution P&L; two books, two sources
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
```

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
| M7 weather engine | pending |
| M8 news/catalyst engine | pending |
| M9 backtester + hardening | pending |

Branch: `claude/kalshi-copilot-build-bgyv2d`

### Open items for the operator

- **`leaderboard_watcher` cannot be built.** Kalshi publishes no leaderboard,
  trader ranking, or public profile endpoint — checked against the OpenAPI
  spec on 2026-07-27. The only surfaces naming a counterparty are RFQ and
  block trades, which are yours alone. It is registered as a detector that
  logs a refusal when enabled, because one silently missing from the registry
  looks identical to one that runs and finds nothing. Do not add scraping.
- **Only BTC has a spot feed.** ETH/SOL/XRP markets are correctly refused by
  the stale-quote detector, which is ~1,353 markets it can see and cannot
  price. Adding feeds means adding to `SPOT_SOURCES` and polling per symbol.
- **Signals have no duplicate guard.** Proposals do; signals do not, and the
  screener re-emits its top 20 every scan (180 rows in nine scans). Fix that
  before enabling `undervalued_screener` for real.
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

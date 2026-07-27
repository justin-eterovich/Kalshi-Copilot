# kalshi-copilot

Self-hosted Kalshi analysis and trading copilot. Ingests market data, runs
edge-detection algorithms, proposes trades with sizing and a written rationale,
and executes **only after you approve each trade individually**.

> **No order reaches Kalshi without explicit per-trade human approval.**
> There is no auto-trade mode, not even behind a flag. That is a design
> constraint, not a default.

The premise: you cannot out-speed professional market makers on flagship
markets, so this hunts edges that persist longer than your approval latency —
structural mispricings, thin and neglected markets, settlement lag, and
model-driven fair value.

---

## Status

| Milestone | Scope | State |
|-----------|-------|-------|
| **M0** | Scaffold, compose stack, fees module | ✅ done |
| **M1** | Ingest + storage | ✅ done |
| **M2** | Dashboard core (screener, market page, charts) | ✅ done |
| **M3** | HITL approval + execution rail | ✅ done |
| **M4** | Detectors wave 1 (set-arb, resolution sniper, BTC stale-quote) | ✅ done |
| M5 | Risk layer + PWA notifications | **next** |
| M6 | BTC engine + detectors wave 2 | pending |
| M7 | Weather engine | pending |
| M8 | News + catalyst engine | pending |
| M9 | Backtester, report card, hardening | pending |

---

## First run

### 1. Prerequisites

Docker and Docker Compose. Nothing else — Node and Python live inside the
images, so you never install a toolchain on the host.

Your host clock must be NTP-synced. Kalshi validates the request timestamp in
every signature; a skewed clock produces authentication failures that look
like credential problems.

```bash
timedatectl status | grep -i sync    # want: "System clock synchronized: yes"
```

### 2. Generate a Kalshi API key

Demo and production use **separate credentials** — a demo key will not
authenticate against production.

```bash
# Generate a keypair. Keep the private key out of the repo.
mkdir -p secrets
openssl genrsa -out secrets/kalshi_demo_key.pem 2048
openssl rsa -in secrets/kalshi_demo_key.pem -pubout -out secrets/kalshi_demo_key.pub.pem
chmod 600 secrets/kalshi_demo_key.pem
```

Then:

1. Log in to **[demo.kalshi.co](https://demo.kalshi.co)** → Settings → API Keys
2. Upload `secrets/kalshi_demo_key.pub.pem`
3. Copy the **API Key ID** (a UUID) it hands back

Repeat at [kalshi.com](https://kalshi.com) later for production, saving to
`secrets/kalshi_prod_key.pem`.

`secrets/` is gitignored and mounted read-only into the containers. The private
key is never logged — the log formatter strips anything key-shaped on its way
out.

### 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

At minimum set `POSTGRES_PASSWORD`, `KALSHI_DEMO_KEY_ID`, and make
`DATABASE_URL` use the same password.

Leave `KALSHI_ENV=demo` and `LIVE_TRADING=false`.

### 4. Bring it up

```bash
docker compose up -d --build
docker compose ps          # all five services healthy
```

First build takes a few minutes (Node builds the dashboard, Python installs
dependencies). Then open:

```
http://<homelab-lan-ip>:8080
```

### 5. Verify the fee schedule

Every edge number in this system is net of fees, so the fee table has to be
right. Nothing can be proposed until it has been checked against the official
PDF:

```bash
docker compose run --rm tools python scripts/refresh_fee_schedule.py
```

`tools` is a one-shot maintenance container — it never starts with
`docker compose up`, and it is the only service with write access to `data/`.
No Python on the host required.

Run it from the homelab, not a cloud host: kalshi.com serves a bot-protection
challenge to datacenter IPs. If the fetch is blocked, open the URL in a
browser, save the PDF into the repo, and point the script at it:

```bash
docker compose run --rm tools python scripts/refresh_fee_schedule.py \
    --file data/kalshi-fee-schedule.pdf
```

The script prints the formula constants, the rounding rule, and a ready-made
`series:` block — the whole non-standard fee table. Paste it into
`data/fee_schedule.yaml`, check the constants above it, then:

```bash
docker compose run --rm tools python scripts/refresh_fee_schedule.py --mark-verified
docker compose restart api worker
```

`--mark-verified` refuses while any rate, default, or series multiplier is
missing, so you cannot clear the warning while the table is incomplete.

Until it is verified, **nothing can be proposed at all**. See "Fees" below.

---

## Trading

Every order starts as a **proposal** — a request for a human decision. Nothing
reaches an exchange until you approve that specific proposal.

1. Open a market and fill in the **trade ticket**. The cost, fee, breakeven
   and worst case update as you type, computed by the backend so the fee
   engine stays the single source of those numbers.
2. Press **propose**. The trade goes into the queue on `/trades` and the nav
   badge (and the browser tab title) shows the pending count.
3. Approve it. That is a two-step interaction; on the live route it also
   requires typing the market ticker.

Proposals **expire** after `trading.default_proposal_ttl_sec` (120s). A
proposal carries a price that was executable when it was written — approving
a stale quote means trading against a book that has moved, so expiry makes it
un-actionable rather than merely inadvisable.

### Where an approved order actually goes

| `trading.mode` | `KALSHI_ENV` | `LIVE_TRADING` | credentials | route |
|---|---|---|---|---|
| `paper` | demo | — | yes | **demo exchange** — real orders, play money |
| `paper` | demo | — | no | **simulated** — local fill sim, no API call |
| `paper` | prod | — | — | **simulated** — never touches prod |
| `live` | prod | true | yes | **live exchange** — real money |
| `live` | anything else | | | **refused** |

The header shows which one is active. Two rows deserve emphasis:

- **Paper mode never touches a production exchange**, even with prod
  credentials present. Changing `KALSHI_ENV` for a data reason must not
  quietly arm real money.
- **`mode: live` without both environment interlocks refuses** rather than
  degrading to paper. A config that says live must not trade fictionally
  while the dashboard says otherwise.

Set `trading.paper_uses_demo_exchange: false` to force the local simulator
even when demo credentials exist.

### Paper fills are deliberately pessimistic

The simulator crosses the spread, walks the book, and charges a **separate
taker fee per price level**, because that is how the exchange bills. It never
fills through your limit, and a market with no book fills nothing rather than
inventing a price.

(Per-level pricing is the *accurate* model, not the conservative one: the fee
is concave in price, so a single fee at the blended VWAP actually comes out
slightly higher. It only looked conservative while fees rounded to a whole
cent.)

A simulator that flatters itself produces a report card saying a detector
works when it does not, and that report card is what decides whether real
money gets deployed.

---

## Detectors

**All ship disabled.** Enable one at a time in `config.yaml` and let the
report card earn your trust before it earns your money.

```yaml
detectors:
  set_arbitrage:
    enabled: true
```

then `docker compose restart worker`.

A detector emits **signals**, visible on the trades page and at
`GET /api/signals`. A signal is an observation, not a recommendation, and not
all of them can become trades. Every `net_edge_cents` is net of fees; a zero
means the detector *declined* to claim an edge, not that it found one worth
nothing.

### Set arbitrage

Prices whole mutually-exclusive events against their live books.

Kalshi's `mutually_exclusive` flag means **at most one leg resolves YES** —
not exactly one. That asymmetry is the whole design:

- **Selling every leg** collects `Σ bids` and pays out at most $1, so it is
  riskless on exclusivity alone. Enabled.
- **Buying every leg** returns $1 only if *some* leg wins, which needs the
  set to be **exhaustive** — and nothing in the API says that. `KXNEWPOPE-70`
  is exclusive, lists 7 candidates, and their asks sum to $4.12. Buying that
  set for 99¢ would lose everything if an eighth won.

So the buy side runs only for series you name in `exhaustive_series`, which
ships empty. Each entry is a claim about the world the exchange never made.

A set arb is one decision needing several orders, so it produces a
**multi-leg proposal** — approved as a unit, placed in one batch with IOC.
Kalshi has no atomic multi-order primitive, so a partial outcome is possible;
it becomes `PARTIAL` with an explicit `UNBALANCED: n of m legs executed`,
because that is a directional position nobody chose.

It needs full depth on **every** leg, so `ingest.watchlist` must cover the
whole event. A set priced from partial coverage is not a set.

### BTC stale quote

Compares a fresh independent spot price against a crypto strike. Spot is
polled from Coinbase into `external_prices` (set `bitcoin.enabled: true`),
and the detector checks the *age* of the newest observation rather than
trusting it — a stale reference against a live market invents an edge in
whichever direction the market already moved.

This is a **heuristic, not a probability**. "Decisively past the strike" is a
fixed percentage (`decisive_margin_pct`), not output from a volatility model;
that arrives in M6. So it requires both a margin *and* a short time to close,
and caps fair value at 0.98 rather than 1.00. `custom` strike types are
refused — their rules live in prose.

### Resolution sniper

Finds markets past their close time that are still trading at an extreme.
That structural lag is real. **It is not an edge on its own**, and this
detector refuses to pretend otherwise: 98¢ is the crowd's opinion, and buying
it because it is high is a 49:1 bet — being wrong 3% of the time loses money
steadily after fees.

Signals are **research only**, capped at 0.35 confidence with no edge
claimed, until an independent settlement source confirms the outcome. Nothing
wires one in yet (that is M7/M8), so today it never produces a proposal.

Its thresholds apply to the price you would actually **pay** — the ask when
buying YES — not the mid. A market quoted 96/99 is not a 97¢ chance.

---

## Going live

Live trading is deliberately awkward to enable. All three must be true:

1. `KALSHI_ENV=prod` in `.env`
2. `LIVE_TRADING=true` in `.env`
3. Confirmation in the UI, **per trade** — typing the market ticker

```bash
# only when you actually mean it
sed -i 's/KALSHI_ENV=demo/KALSHI_ENV=prod/' .env
sed -i 's/LIVE_TRADING=false/LIVE_TRADING=true/' .env
docker compose up -d
```

The dashboard header turns from green to amber and the banner changes. If it
still says "safe mode", one of the interlocks is not set.

Before you do this, read the per-detector report card (M9). A detector that has
not proven positive expectancy on paper, net of fees, has not earned real money.

---

## Architecture

```
Kalshi WS/REST ──> ingest ──> Postgres (catalog, candles, book snaps, tape)
external feeds ──> ingest      │
(spot BTC, BLS/Fed,            ▼
 NWS, RSS)              worker: detectors ──> signals ──> risk/sizing ──> proposed_trades
                                                                             │
                        UI approval queue + PWA push (first-party) <──────────┤
                                                                             ▼
                                              approve ──> executor ──> Kalshi order ──> fills ──> audit log
```

### Services

| Service | Role |
|---------|------|
| `db` | Postgres 16 + TimescaleDB (hypertables for candles/tape) |
| `redis` | signal fan-out to the UI, rate-limit buckets, dedupe |
| `api` | FastAPI; also serves the built React dashboard |
| `ingest` | Kalshi and external market-data ingestion |
| `worker` | detectors, scanners, risk and sizing |

Only `api` publishes a host port. `db` and `redis` stay on the internal compose
network and are not reachable from the host.

### Layout

```
backend/app/
  main.py            FastAPI app, serves the SPA
  settings.py        secrets + endpoints from .env
  config.py          validated loader for config.yaml
  healthcheck.py     container probe
  core/
    fees.py          ⭐ all fee math, single source of truth
    logging.py       log setup with secret redaction
    redis.py         client + pub/sub channel names
  db/
    base.py          engine, session factory
    models.py        schema
    bootstrap.py     create_all + hypertable setup
  core/money.py      fixed-point parsing (dollars / fractional contracts)
  kalshi/
    auth.py          RSA-PSS request signing
    ratelimit.py     adaptive token buckets (AIMD)
    rest.py          REST client, cursor pagination, 429 backoff
    ws.py            websocket, reconnect, per-sid sequence tracking
    orderbook.py     local book reconstruction, gap detection
    client.py        factories wiring settings -> clients
  ingest/
    main.py          service entrypoint (catalog + stream + spot + flush loops)
    spot.py          external BTC spot reference (public endpoints)
    catalog.py       series/events/markets sync, category backfill
    normalize.py     API payloads -> ORM rows
    streams.py       tape, candles, book snapshots
  detectors/
    set_arbitrage.py ⭐ set-arb math; sell side safe, buy side needs exhaustive
    stale_quote.py   spot vs strike; heuristic margin, no vol model yet
    resolution_sniper.py  settlement lag; research only without a source
    base.py          detector protocol + signal recording
    runner.py        the live detectors and their refusal rules
  trading/
    direction.py     ⭐ (side, action) <-> Kalshi's single bid/ask book
    interlocks.py    execution routing + every safety check
    pricing.py       fee-aware ticket costing
    proposals.py     proposal lifecycle: create, expire, decide
    executor.py      ⭐ the only module that can cause an order to exist
    paper.py         pessimistic fill simulator
    positions.py     signed position + realised P&L accounting
  api/routes/        HTTP endpoints
  worker/
    main.py          worker service entrypoint
    maintenance.py   proposal expiry, order auto-cancel, reconciliation
backend/tests/       pytest suite
frontend/            React + Vite + TS dashboard
data/                fee_schedule.yaml
scripts/             operational scripts
secrets/             RSA keys (gitignored, mounted read-only)
config.yaml          runtime tunables
```

---

## Fees

`backend/app/core/fees.py` is the only place fee math happens. Detectors, the
sizing layer, and the UI all call into it, so "net edge" means the same thing
everywhere.

The formulas, from the schedule PDF:

```
taker = round up(M × 0.07   × C × P × (1 - P))     M defaults to 1
maker = round up(M × 0.0175 × C × P × (1 - P))     M defaults to 0
```

`P` is a price in **dollars** and `C` may be **fractional**, because that is
what the API actually speaks — see "Units" below.

Four details the implementation gets right and most don't:

- **Rounding is to a centicent (`$0.0001`), not a cent.** The schedule says
  the fee rounds up "such that the fee + positionCost is rounded to a
  centicent". One contract at 50¢ costs **1.75¢**, not 2¢. Rounding to the
  cent overstates small orders by up to 14%.
- **Rounding is on the order aggregate, not per contract.** One hundred
  contracts at 50¢ cost exactly $1.75.
- **Each fill is charged separately.** An order filling in three pieces is
  three charges — a real cost of resting size in a thin book.
- **Maker fees default to zero.** The maker multiplier's documented default
  is 0, so most markets charge no maker fee at all.

**Multipliers are keyed by SERIES ticker, not category.** The schedule has no
category dimension. It lists ~85 non-standard series; anything absent takes
the defaults. Ten series — including `KXBTCY` and `KXETHY` — are listed at
0/0 and charge no trading fees whatsoever.

**An unverified schedule fails closed.** If `data/fee_schedule.yaml` has
never been checked against the PDF, nothing can be proposed: every edge
figure is net of fees, so an unchecked fee table makes all of them
untrustworthy.

```bash
cd backend && python -m pytest tests/test_fees.py -v
```

---

## Where market data comes from

Two paths feed the same tables, and which one you get depends on credentials:

| Path | Needs a key? | Gives you |
|------|--------------|-----------|
| REST catalog sync | no | series, events, markets, quotes |
| REST read-through | no | candles, orderbook, tape — fetched on demand when you open a market |
| WebSocket stream | **yes** | the same data live, plus book deltas and full tape |

The Kalshi WebSocket requires authentication **even for public market-data
channels**, but the REST market-data endpoints are open. So the dashboard is
fully usable before any API key exists — opening a market page pulls its
candles, depth and tape straight from Kalshi and caches them. Adding
credentials upgrades that from on-demand polling to a live stream; nothing in
the UI changes shape.

The market page shows where each panel's data came from (`live` vs `cached`).

### Real-time UI

The browser connects to `GET /ws`, which relays Redis pub/sub. One Redis
subscription is shared across every open tab. Clients send
`{"action":"watch","tickers":[...]}` to filter the tick firehose down to what
is on screen — the scanner streams every market and a market page wants one.

Proposals and signals ignore that filter by design: an approval request must
reach you regardless of which page you happen to have open. A client that
cannot keep up has its messages dropped rather than being allowed to slow the
shared reader down for everyone else.

---

## Units — read this before touching money

The API does **not** speak integer cents. Getting this wrong is the single
easiest way to produce a confident, wrong edge number.

| Concept | Wire format | Internal type |
|---------|-------------|---------------|
| Price | `FixedPointDollars`, e.g. `"0.5600"`, up to **6 decimals** | `Decimal` dollars, `Numeric(12,6)` |
| Contract count | `FixedPointCount`, e.g. `"10.00"`, **fractional to 0.01** | `Decimal`, `Numeric(16,2)` |
| Fee | `"0.022400"` dollars | `Decimal` cents, `Numeric(20,6)` |
| Realised P&L | — | `Decimal` cents, `Numeric(20,6)` |

**Neither fees nor P&L are whole cents.** Fees round up to a **centicent**
(`$0.0001`), so one contract at 50c costs 1.75c. P&L is a price difference
times a count, and with sub-cent ticks and fractional contracts both can be
fractional. Rounding either would accumulate drift in the numbers the report
card is judged on.

Consequences that are easy to miss:

- Tick size varies by market (`price_level_structure`), so sub-cent prices are
  real. Rounding a quote to the nearest cent on ingest loses information the
  detectors need.
- Contracts can be fractional, so fee and sizing math must not assume integers.
- Prices cross the API boundary as **strings**, and stay strings in JSON
  responses to the frontend. Parsing a price into a JS `number` re-introduces
  exactly the precision loss the backend avoided.
- `taker_fee_cents("0.5600", ...)` is right; `taker_fee_cents(56, ...)` raises
  rather than silently pricing a market at $56.

### Order direction

Kalshi's order API quotes **one book, from the YES side**: `bid` = buy YES,
`ask` = sell YES, and the wire price is *always* the YES price.

| side/action | book side | wire price |
|-------------|-----------|------------|
| buy YES | `bid` | `p` |
| sell YES | `ask` | `p` |
| **buy NO** | **`ask`** | **`1 − p`** |
| sell NO | `bid` | `1 − p` |

"Buy NO at 30¢" reaches the exchange as an **ask at 0.70**. Nothing
downstream catches an inversion: the fee formula `P(1−P)` is symmetric, so a
flipped direction produces the same fee, the same notional, and a plausible
confirmation. `backend/app/trading/direction.py` owns this mapping and it is
never inlined elsewhere. The approval card shows the literal wire form so you
can check it before committing.

Positions use the opposite convention — one **signed** number per market in
YES-equivalents (positive YES, negative NO), matching the API's own signed
`position_fp`. The UI converts back to "10 NO at 30¢" for display.

---

## Configuration

`.env` holds secrets and endpoints. `config.yaml` holds tunables and is
bind-mounted read-only, so edits need only a restart:

```bash
$EDITOR config.yaml
docker compose restart worker ingest
```

**All detectors ship disabled.** Enable them one at a time and let the report
card earn your trust before it earns your money.

### Kalshi endpoints

Defaults are the hosts `docs.kalshi.com` currently recommends:

| | REST | WebSocket |
|---|---|---|
| prod | `external-api.kalshi.com` | `external-api-ws.kalshi.com` |
| demo | `external-api.demo.kalshi.co` | `external-api-ws.demo.kalshi.co` |

The older `api.elections.kalshi.com` / `demo-api.kalshi.co` hosts remain valid
and are documented as alternatives. Override in `.env` if you need them.

### Rate limits

Basic tier is 200 read tokens/sec and 100 write tokens/sec, with most requests
costing 10 tokens — so roughly 20 reads/sec and 10 writes/sec. Basic write
buckets hold only one second of burst headroom. Read and write budgets are
tracked separately, so a heavy catalog sync can never throttle order placement.

Those numbers describe *authenticated* accounts, some endpoints cost more than
the default 10 tokens, and a 429 carries no `Retry-After` and no
`X-RateLimit-*` headers — so the real ceiling cannot be known up front. The
limiter therefore adapts: it halves its rate on a 429 and creeps back up while
requests succeed (AIMD). Against the live demo API this converges in ~25
rejections instead of one per request.

You will see one warning as it finds the ceiling, e.g.
`limiter now at read=27/200 tok/s`. That is the mechanism working, not a fault.

Live values are available from `GET /account/limits` and
`GET /account/endpoint_costs`.

---

## Networking

The dashboard binds to the LAN and has **no authentication in front of it**.
Reach it remotely over WireGuard. Do not port-forward it, do not put it behind
a tunnel, do not expose it publicly.

Narrow the binding further by setting `BIND_ADDR` to a specific LAN IP:

```bash
BIND_ADDR=192.168.1.50
```

---

## Development

```bash
# tests (no host toolchain needed)
docker compose run --rm --no-deps api python -m pytest tests/ -v

# lint + typecheck
docker compose run --rm api ruff check app/
docker compose run --rm api mypy app/core app/config.py app/settings.py

# logs
docker compose logs -f worker ingest

# rebuild after dependency changes
docker compose up -d --build
```

Run tests on the host without Docker:

```bash
cd backend
pip install -e '.[dev]'
python -m pytest tests/ -v
```

---

## Safety model

| Control | Behaviour |
|---------|-----------|
| Per-trade approval | Every order, always. Detector signals and manual tickets use the same queue. There is no bulk approve. |
| Environment interlock | Live needs `KALSHI_ENV=prod` **and** `LIVE_TRADING=true` **and** the ticker typed back, per trade. |
| Paper never hits prod | Paper mode routes to the simulator on a prod environment rather than trading it. |
| Live never degrades | `mode: live` without the env interlocks refuses, rather than silently trading on paper. |
| Proposal TTL | A proposal expires (120s default) and cannot then be approved. Checked at approval, not just by the sweep. |
| Multi-leg all-or-none | A set arb is one proposal. Execution is *not* atomic — Kalshi has no such primitive — so legs go out in one IOC batch and any imbalance is reported as `PARTIAL`, never hidden. |
| No write retries | A timed-out order POST may have been accepted, so it raises instead of retrying. Recovery is reconciliation by client order ID. |
| Kill switch | Halts all proposals and cancels resting orders. |
| Queue depth cap | `risk.max_pending_proposals` (default 10). Not a risk limit — it protects *attention*. A detector scanning every 20s produced 20 proposals per scan, and a queue nobody reads is rubber-stamped rather than reviewed. |
| Per-market size limit | `risk.max_pct_per_market` is enforced at proposal creation, not merely displayed. |
| Duplicate guard | A detector re-derives the same opportunity every scan; one live proposal per event per detector. |
| Detector flags | Each detector independently enabled; all off by default. |
| Fee fail-closed | Unknown fee multiplier ⇒ market excluded, never guessed. |
| Idempotent orders | Client-supplied order IDs; a network retry cannot double-place. |
| Stale orderbook | A websocket sequence gap marks the local book stale; it raises rather than answering from guessed state. |
| Audit log | Append-only record of every signal, proposal, decision, order, fill, and fee. |
| Secret redaction | Private keys and signature headers stripped from logs by the formatter. |

---

## License

Private. Not investment advice. You are responsible for your own trades.

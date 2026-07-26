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
| M3 | HITL approval + execution rail | pending |
| M4 | Detectors wave 1 (set-arb, resolution sniper, BTC stale-quote) | pending |
| M5 | Risk layer + PWA notifications | pending |
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
right. The shipped `data/fee_schedule.yaml` encodes the general formula but
leaves premium-category multipliers **unverified**:

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

Fill the reported multipliers into `data/fee_schedule.yaml`, then:

```bash
docker compose run --rm tools python scripts/refresh_fee_schedule.py --mark-verified
docker compose restart api worker
```

`--mark-verified` refuses while any multiplier is still `null`, so you cannot
accidentally clear the warning while markets remain unpriceable.

Until then, any category with an unknown multiplier is **excluded from
proposals** rather than priced with a guess. See "Fees" below.

---

## Going live

Live trading is deliberately awkward to enable. All three must be true:

1. `KALSHI_ENV=prod` in `.env`
2. `LIVE_TRADING=true` in `.env`
3. Confirmation in the UI modal, **per trade**

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
    main.py          service entrypoint (catalog + stream + flush loops)
    catalog.py       series/events/markets sync, category backfill
    normalize.py     API payloads -> ORM rows
    streams.py       tape, candles, book snapshots
  api/routes/        HTTP endpoints
  worker/main.py     worker service entrypoint
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

The taker formula:

```
fee = round_up_to_cent( M × 0.07 × C × P × (1 − P) )
```

`P` is a price in **dollars** and `C` may be **fractional**, because that is
what the API actually speaks — see "Units" below.

Two details the implementation gets right and most don't:

- **Rounding is on the order aggregate, not per contract.** One contract at 50¢
  costs 2¢; one hundred contracts at 50¢ cost exactly $1.75, not $2.00.
  Rounding per contract overstates fees by ~14% at the money.
- **Each fill is charged separately.** An order filling in three pieces rounds
  up three times, which is a real cost of resting size in a thin book.

Maker fees are a fraction of the taker rate, and some categories charge none.

**Category comes from the event, not the market.** The `/markets` payload
carries no category at all; it lives on the parent event. Ingest joins it
across on every sync, because `fees.py` selects the multiplier *by category* —
without that join every Crypto market would quietly price at the standard rate
and the guard below would never fire.

**Unverified categories fail closed.** If `data/fee_schedule.yaml` has a `null`
multiplier for a category, `fees.py` raises `UnverifiedFeeCategory` and those
markets are excluded from proposals. An understated fee silently inflates every
downstream EV number, so the system refuses to guess.

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
| Fee / realised P&L | — | integer **cents**, which they exactly are |

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
| Per-trade approval | Every order, always. Detector signals and manual tickets use the same queue. |
| Environment interlock | Live needs `KALSHI_ENV=prod` **and** `LIVE_TRADING=true` **and** UI confirmation. |
| Kill switch | Halts all proposals and cancels resting orders. |
| Detector flags | Each detector independently enabled; all off by default. |
| Fee fail-closed | Unknown fee multiplier ⇒ market excluded, never guessed. |
| Idempotent orders | Client-supplied order IDs; a network retry cannot double-place. |
| Stale orderbook | A websocket sequence gap marks the local book stale; it raises rather than answering from guessed state. |
| Audit log | Append-only record of every signal, proposal, decision, order, fill, and fee. |
| Secret redaction | Private keys and signature headers stripped from logs by the formatter. |

---

## License

Private. Not investment advice. You are responsible for your own trades.

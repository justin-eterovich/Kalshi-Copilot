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
| M1 | Ingest + storage | pending |
| M2 | Dashboard core (screener, market page, charts) | pending |
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
python scripts/refresh_fee_schedule.py
```

Run it from the homelab, not a cloud host — kalshi.com serves a bot-protection
challenge to datacenter IPs. Follow the printed instructions, then:

```bash
python scripts/refresh_fee_schedule.py --mark-verified
docker compose restart api worker
```

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
  kalshi/            REST + WS clients (M1)
  api/routes/        HTTP endpoints
  ingest/main.py     ingest service entrypoint
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

Two details the implementation gets right and most don't:

- **Rounding is on the order aggregate, not per contract.** One contract at 50¢
  costs 2¢; one hundred contracts at 50¢ cost exactly $1.75, not $2.00.
  Rounding per contract overstates fees by ~14% at the money.
- **Each fill is charged separately.** An order filling in three pieces rounds
  up three times, which is a real cost of resting size in a thin book.

Maker fees are a fraction of the taker rate, and some categories charge none.

**Unverified categories fail closed.** If `data/fee_schedule.yaml` has a `null`
multiplier for a category, `fees.py` raises `UnverifiedFeeCategory` and those
markets are excluded from proposals. An understated fee silently inflates every
downstream EV number, so the system refuses to guess.

```bash
cd backend && python -m pytest tests/test_fees.py -v
```

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
buckets hold only one second of burst headroom. The client-side token bucket
(M1) is sized from `KALSHI_RATE_TIER` and keeps read and write budgets separate.

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
# tests
docker compose run --rm api python -m pytest tests/ -v

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
| Audit log | Append-only record of every signal, proposal, decision, order, fill, and fee. |
| Secret redaction | Private keys and signature headers stripped from logs by the formatter. |

---

## License

Private. Not investment advice. You are responsible for your own trades.

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
| Fee | `"0.0175"` dollars on the wire | integer **cents** (it exactly is) |
| Realised P&L | — | `Decimal` cents, `Numeric(20,6)` |

Fees are whole cents because the exchange charges whole cents. **Realised P&L
is not** — it is a price difference times a count, and both factors can be
fractional (closing 0.50 contracts on a 1c move earns half a cent). M3
corrected this: rounding each realisation would accumulate drift in the one
number the report card is judged on, so it is carried exactly and rounded
only for display.

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
- **`category` is on the Event, not the Market.** `/markets` has no category
  field. `fees.py` picks the multiplier *by category*, so ingest joins it
  across on every sync. Without that join every market silently resolves to
  the default multiplier and the unverified-category guard never fires.
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
  trading/
    direction.py     ⭐ (side, action) <-> bid/ask. Never inline this.
    interlocks.py    execution routing + every safety check
    pricing.py       fee-aware ticket costing (calls fees.py, owns no fee math)
    proposals.py     proposal lifecycle: create, expire, decide
    executor.py      ⭐ the ONLY module that can cause an order to exist
    paper.py         pessimistic fill simulator
    positions.py     signed position + realised P&L accounting
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
| M4 detectors wave 1 | **next** |
| M5 risk layer + PWA notifications | pending |
| M6 BTC engine + detectors wave 2 | pending |
| M7 weather engine | pending |
| M8 news/catalyst engine | pending |
| M9 backtester + hardening | pending |

Branch: `claude/kalshi-copilot-build-bgyv2d`

### Open items for the operator

- **M3 ran against a live stack** (portable Postgres + Redis, real ingest
  against the Kalshi demo REST API, 125k markets). That found four bugs the
  test suite had passed — see `docs/m3-demo-notes.md`. Two gaps remain: **no
  demo order has ever been placed** (no credentials on the build machine, so
  only the `simulated` route was exercised) and **no UI screenshots** (no
  browser). Do both on the test VM before trusting M3.
- `data/fee_schedule.yaml` has `crypto: null` → ~5,900 Crypto markets are
  excluded from proposals until verified against the official PDF. Blocks the
  BTC detector in M4. It also means **Crypto markets cannot be proposed at
  all** — the trade ticket refuses them with a visible banner, which is a
  good way to see the fail-closed path working.
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

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
   `LIVE_TRADING=true` **and** per-trade confirmation in the UI.
3. **Demo-first.** Default environment is Kalshi demo. Demo and prod use
   separate credentials; a demo key will not authenticate against prod.
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
| Fee / realised P&L | — | integer **cents** (they exactly are) |

- Tick size varies per market (`price_level_structure`), so sub-cent prices
  are real. Never round a quote on ingest.
- Contracts are fractional. Never assume integer size in fee or sizing math.
- Prices stay **strings** across the API boundary and in JSON to the frontend.
  Parsing to a JS `number` re-introduces the precision loss the backend
  avoided. Format for display; never compute in the browser.
- `taker_fee_cents("0.5600", ...)` is correct. `taker_fee_cents(56, ...)`
  raises — deliberately.

---

## Non-obvious API facts (verified against live demo)

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
| M3 HITL approval/execution rail | **next** |
| M4 detectors wave 1 | pending |
| M5 risk layer + PWA notifications | pending |
| M6 BTC engine + detectors wave 2 | pending |
| M7 weather engine | pending |
| M8 news/catalyst engine | pending |
| M9 backtester + hardening | pending |

Branch: `claude/kalshi-copilot-build-bgyv2d`

### Open items for the operator

- `data/fee_schedule.yaml` has `crypto: null` → ~5,900 Crypto markets are
  excluded from proposals until verified against the official PDF. Blocks the
  BTC detector in M4.
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

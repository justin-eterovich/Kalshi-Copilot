# M3 — human-in-the-loop approval and execution rail

The system can now place orders. It can only do so after a human approves one
specific proposal, and every path to an order runs through
`backend/app/trading/executor.py`.

---

## What landed

| Piece | Where |
|-------|-------|
| Order direction translation | `app/trading/direction.py` |
| Execution routing + interlocks | `app/trading/interlocks.py` |
| Fee-aware ticket costing | `app/trading/pricing.py` |
| Proposal lifecycle | `app/trading/proposals.py` |
| The execution rail | `app/trading/executor.py` |
| Pessimistic fill simulator | `app/trading/paper.py` |
| Signed position + P&L accounting | `app/trading/positions.py` |
| Portfolio/order REST surface | `app/kalshi/rest.py` |
| HTTP endpoints | `app/api/routes/trading.py` |
| Expiry / auto-cancel / reconcile | `app/worker/maintenance.py` |
| Trade ticket, approval queue | `frontend/src/{TradeTicket,Trades,ApprovalCard}.tsx` |

---

## The safety model, concretely

**Where an approved order goes** is decided by `resolve_route`:

| condition | route |
|-----------|-------|
| `mode: live` + `KALSHI_ENV=prod` + `LIVE_TRADING=true` | `live_exchange` |
| `mode: live` without both of those | **refused** |
| `mode: paper` + demo env + credentials | `demo_exchange` |
| `mode: paper` + prod env | `simulated` |
| anything else | `simulated` |

Two rows are deliberate and worth restating:

- **Paper mode never touches a production exchange**, even with prod
  credentials sitting right there. Flipping `KALSHI_ENV` for a data reason
  must not quietly arm real money.
- **`mode: live` without the environment interlocks refuses** rather than
  falling back to paper. A silent downgrade would mean the config says live,
  the header says live, and the fills are fictional.

**Every approval** requires `confirm: true` — never defaulted, never implied.
On the live route it additionally requires the operator to type the market
ticker, which a misclick cannot produce.

**Every interlock is re-checked inside the executor**, not merely by the API
handler. An interlock that lives only at the call site is one the next caller
forgets.

---

## Verification

```
384 backend tests pass          (was 213 before M3; +171)
ruff check app/                 clean
mypy app/core app/config.py app/settings.py    clean
mypy app/trading app/worker/maintenance.py     clean  (beyond the strict scope)
tsc --noEmit                    clean
vite build                      clean
```

### Run against a live stack

No Docker on the build machine, so the stack was assembled from portable
Postgres 18 and Redis (conda-forge, no root) with the real `api`, `ingest` and
`worker` processes against the **live Kalshi demo REST API**. No credentials
were configured, so the execution route resolved to `simulated` — the whole
rail ran, filling against real order books.

Ingest pulled **125,609 markets** and 380,000+ events from demo. The full
propose → approve → fill → position → audit path was exercised end to end,
plus expiry, resting orders, auto-cancel and the kill switch.

**Four bugs were found this way that tests, ruff, mypy and tsc had all
passed.** They are described below because each one is a pattern worth not
repeating.

Confirmed working against real data:

- A buy of 25 YES at a 0.30 limit filled at **0.215** — the live 0.21 book ask
  plus the configured 0.5¢ slippage, and better than the limit. Fee 30¢,
  matching `0.07 × 25 × 0.215 × 0.785` rounded up.
- Buying 40 NO on the same market netted the position to **−15**, displayed as
  "15 NO @ 0.805", realising −50¢ on the 25 closed at 0.195 against a 0.215
  basis. The direction and signed-position handling are correct on real
  quotes.
- Sub-cent prices are real on demo: `KXMLB-26-BAL` quotes 0.008 / 0.009.
- An unmarketable order rested with zero fills rather than inventing one, and
  the worker auto-cancelled it after 90s.
- A 10s-TTL proposal expired on the worker sweep, and approving it afterwards
  returned `409 not_pending`.
- The kill switch refused both new proposals and approval of existing ones.
- Redis fan-out published on `copilot:proposals` as expected.
- TimescaleDB is absent on this stack; `bootstrap` degraded to plain Postgres
  with a warning, as documented.

### Bug 1 — an uncategorised market priced at the default multiplier

**The important one.** A Bitcoin market returned `HTTP 200` with a normal fee
while `crypto` was still an unverified multiplier. It should have failed
closed.

Categories live on the Event and are joined onto the Market after the event
sync — which takes many minutes on a fresh database. Until it lands,
`Market.category` is `NULL`, and `fees.py` treats an unrecognised category as
*"use the default"*. But `NULL` does not mean "ordinary category", it means
*"we have not looked yet"*, and conflating the two priced 55,000 Crypto
markets at the standard rate.

This is not only a bootstrap window: it applies to any newly listed ticker
seen before its event, or any event that failed to sync.

Fixed with `UncategorisedMarket`, which subclasses `UnverifiedFeeCategory` so
every existing fail-closed handler catches it unmodified. Proposals on a
market with no category now return 409.

### Bug 2 — the API returned 201 before committing

Every mutating endpoint returned its response *before* the row was durable.
`session_scope` is a FastAPI yield-dependency, and FastAPI runs the exit code
**after** the response has been sent. A client reading back immediately lost
the race — reproducibly, three times out of three.

That is exactly what the dashboard does: the trade ticket reloads the queue
the moment the POST resolves, so a freshly proposed trade would simply not
appear. Mutating handlers now commit explicitly before returning.

### Bug 3 — a failed placement stranded the proposal

Found in review rather than at runtime. `approve_and_execute` set the proposal
to `APPROVED`, then placed. If placement raised, the line setting the final
status never ran, leaving the proposal approved with no working order —
invisible to both the queue (not pending) and the expiry sweep (also not
pending), looking forever like it had traded. Now marked `FAILED` before the
exception propagates.

### Bug 4 — the audit trail showed effects before causes

`AuditLog.ts` defaults to `now()`, which in Postgres is *transaction* start,
so every entry from one request shares a timestamp and ties broke arbitrarily
— rendering `fill.recorded` above the `proposal.approved` that caused it. Now
ordered by `(ts desc, id desc)`.

### Still not verified

- **No demo order has been placed**, because no Kalshi credentials exist on
  this machine. The `demo_exchange` and `live_exchange` routes, the V2 order
  payload, and `reconcile_order` have unit coverage but have never met the
  real endpoint. **This is the biggest remaining gap** — do it first on the
  test VM.
- **No UI screenshots.** Chromium will not launch here (missing system
  libraries, needs root). The React components were mounted in jsdom against
  realistic payloads, which confirmed fractional sizes render as `0.50` not
  `0`, a `0.305` fill shows as `30.50¢`, the live card hides its typed-confirm
  input until "approve…" is clicked and keeps the button disabled until the
  ticker matches, and an expired proposal has no approve button at all. That
  is not the same as looking at it.
- The stack ran on **Postgres 18 without TimescaleDB**; compose uses
  TimescaleDB on PG16.

---

## Verification still outstanding on the real stack

The rail has been exercised end to end, but **never against a Kalshi account**
and never through a browser. Run this on the test VM, with demo credentials
configured so the route resolves to `demo_exchange`:

```bash
docker compose up -d --build
docker compose ps                       # want 5 healthy

docker compose run --rm --no-deps api python -m pytest tests/ -q
docker compose run --rm --no-deps api ruff check app/
docker compose run --rm --no-deps api mypy app/core app/config.py app/settings.py
```

Then walk the rail end to end:

1. **Check the header.** It should show a new route pill: `demo exchange`
   with demo credentials, `simulated` without. If it says
   `LIVE — real money` in red and you did not intend that, stop.
2. **Open a liquid market** and use the trade ticket in the third column.
   Type a size; the cost, fee, breakeven, max win/loss and "sends as" line
   should update as you type. The "sends as" line is the literal wire form —
   for a NO ticket it must read `ask … @ <1 - your price>`.
3. **Try a Crypto market.** It should refuse with "This market cannot be
   priced" — the unverified-fee-multiplier guard, failing closed.
4. **Propose the trade.** The nav badge and the browser tab title should show
   a pending count within a second (Redis fan-out, not polling).
5. **Watch it expire.** Leave it alone for the TTL (120s default). The
   countdown should turn amber then red, and the card should stop offering
   approve. Confirm the worker logged the expiry.
6. **Propose again and approve it.** Confirm the two-step interaction. On the
   demo route an order should appear in "Working orders" and, if marketable,
   fills and a position.
7. **Check the position reads correctly.** A NO position must display as
   `no / <count> / <no price>`, not as a negative YES position. Cross-check
   `avg_yes_price` in `GET /api/positions` — the stored form is signed and
   YES-denominated on purpose.
8. **Cancel a resting order**, and separately let one age past
   `auto_cancel_after_sec` (90s) so the worker retires it.
9. **Engage the kill switch** in `config.yaml`, restart the worker, and
   confirm resting orders get cancelled and new proposals are refused.
10. **Read the audit trail** at the bottom of the trades page. Every
    proposal, decision, order and fill should be there.
11. **Screenshot the trades page and the market page** with Playwright. That
    could not be done here — Chromium needs system libraries this machine
    does not have.

Things worth watching for specifically, given the pattern so far:

- The `split-3` layout on the market page is new. Check it does not squash
  the order book on a narrow window.
- `pct_of_bankroll` renders as a percentage; confirm it is not showing
  `517.50%` or `0.05%` for a 5% trade.
- Unrealised P&L on a NO position should have the sign a trader expects: NO
  gains when the YES price falls.

---

## Deliberate limitations

- **The simulator cannot model queue position.** A paper order that does not
  immediately cross just rests until the auto-cancel sweep, rather than being
  filled by an incoming trade. That understates maker performance, which is
  the safe direction to be wrong in.
- **Order reconciliation polls REST** rather than subscribing to a portfolio
  websocket. Fills appear within one sweep (10s). A websocket subscription is
  a reasonable M5 upgrade.
- **No risk limits are enforced yet.** `pct_of_bankroll` is shown on the
  approval card, but `max_pct_per_market`, `max_total_exposure_pct` and
  `daily_loss_limit_pct` are not checked. That is M5, and until then the only
  position limit is the operator reading the number before clicking.
- **Sizing is manual.** Kelly sizing arrives with the risk layer.
- **`GET /api/pnl` exists but nothing renders it.** The daily P&L table is
  populated on every fill; the report card that reads it is M9.

---

## One schema decision worth knowing about

`Position.realized_pnl_cents` and `PnlDaily.realized_pnl_cents` are now
`Numeric(20,6)` cents rather than integer cents, and `CLAUDE.md`'s units
table was corrected to match.

Fees genuinely are whole cents — the exchange charges whole cents. Realised
P&L is not: it is a price difference times a count, and with sub-cent tick
sizes and fractional contracts both factors can be fractional. Closing 0.50
contracts on a 1c move earns half a cent. Rounding each realisation to whole
cents would accumulate drift in precisely the number the report card is
judged on, so it is carried exactly and rounded only for display.

The tables were unused before M3, so this cost nothing to change now. There
are no migrations yet — `create_all` handles it. If you have an existing
database from M1/M2 with these tables created, drop them or recreate the
volume; nothing of value is in them.

Two columns were also added: `ProposedTrade.fair_price` (so the card can show
what the edge was measured against) and `Order.route` (`is_paper` collapses
`simulated` and `demo_exchange` into one bit, and the audit trail wants to
know which — a demo fill and a simulated fill are different kinds of
evidence). `Fill.action` was added because `side` alone cannot distinguish
"bought 10 YES" from "sold 10 YES", and those are opposite positions.

---

## New config

```yaml
trading:
  paper_uses_demo_exchange: true
```

In paper mode, send approved orders to the **demo exchange** when demo
credentials exist, rather than filling against the local simulator. Demo is
play money, so this exercises the real order rail — signing, rate limits,
partial fills — at no risk. Set it false to keep everything local.

It cannot route to production. Paper mode refuses to touch a prod exchange
regardless of this flag.

---

## What M3 did not add, and will not

No auto-trade mode, no bulk approve, no "approve all", and no endpoint that
creates and executes in one call. The gap between a proposal existing and a
human looking at it *is* the safety model. `trading.mode` remains
`Literal["paper", "live"]` and the test asserting that still passes.

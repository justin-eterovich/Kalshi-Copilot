# Agent 7 — Safety invariants (adversarial verification)

Area prefix `SAFETY`, numbering from 010 (001–002 taken by the lead).
All runtime work against the live stack `http://127.0.0.1:8080`,
`environment=demo`, `trading_mode=paper`, `route=demo_exchange`, play money.

**Counts: 1 P0, 3 P1, 6 P2, 3 P3.** Plus 12 controls verified HOLDS.

---

## HOLDS / BREAKS — README "Safety model" rows

| Row | Verdict | Evidence |
|---|---|---|
| Per-trade approval | **BREAKS** (lead SAFETY-001) | concurrent approve → 2 orders |
| Environment interlock (live needs prod+LIVE_TRADING+typed ticker) | **HOLDS** (code) | `interlocks.py:91-101,173-179` |
| Paper never hits prod | **HOLDS** (code) | `interlocks.py:105-106` — prod check precedes credential check |
| Live never degrades | **HOLDS** (code) | `interlocks.py:92-100` raises `live_mode_not_armed` |
| Proposal TTL, checked at approval | **HOLDS** (runtime) | SAFETY-H2 below — `expired` code fired live |
| Multi-leg all-or-none | not tested (no HTTP path to a multi-leg proposal) | — |
| No write retries | **HOLDS** (code) | `rest.py` raises on write timeout; not re-verified this run |
| **Kill switch** | **BREAKS** | SAFETY-013 — no runtime engage path exists at all |
| **Queue depth cap** | **BREAKS** under concurrency | SAFETY-012 — 19 pending against a cap of 10 |
| **Per-market size limit** | **BREAKS** | SAFETY-010 — 39.79% of bankroll queued in one market |
| Total exposure limit | UNVERIFIED (unreachable: limit $400, demo balance $80) | SAFETY-020 |
| Daily loss limit | UNVERIFIED (same) | SAFETY-020 |
| Loss cooldown | not tested (needs 3 losing closes) | — |
| Kelly-capped sizing | out of area | — |
| Duplicate guard | UNVERIFIED for concurrency; **absent** on the manual path | SAFETY-019 |
| Detector flags | **HOLDS** | `enabled_detectors: []` |
| Fee fail-closed | out of area | — |
| **Idempotent orders** | **BREAKS** (lead SAFETY-001) | fresh `uuid4()` per attempt |
| Stale orderbook | out of area | — |
| **Audit log** (every signal/proposal/decision/order/fill/fee) | **BREAKS** — gaps | SAFETY-016, SAFETY-017 |
| Secret redaction | **HOLDS** at every HTTP surface; one code gap | SAFETY-H5, SAFETY-018 |
| No auto-trade mode | **HOLDS** | `config.py:30`, `tests/test_config.py:110` |

---

# P0

## [P0] SAFETY-010 — `max_pct_per_market` measures the *proposal*, not the *market*: 39.79% of bankroll queued in one ticker against a 5% cap

`config.yaml` states the limit as *"5% of bankroll in any single market"*. It is
not that. `_guard_market_size` divides **this one proposal's** `max_loss_cents`
by the bankroll and never looks at any other proposal or position in the same
market. Splitting a position across N tickets defeats it entirely — no
concurrency, no race, no timing needed.

**Repro** (bankroll 100,000¢, `max_pct_per_market: 0.05` → 5,000¢):

```bash
# one proposal at 4.97% is accepted, one at 5.03% is refused — the per-proposal
# boundary is exact:
curl -s -X POST localhost:8080/api/proposals -H 'Content-Type: application/json' \
  -d '{"ticker":"KXMLB-26-WSH","side":"yes","action":"buy","limit_price":"0.005","contracts":"9300","ttl_sec":900}'
# -> 201  pct_of_bankroll=0.0497388
curl -s -X POST localhost:8080/api/proposals -H 'Content-Type: application/json' \
  -d '{"ticker":"KXMLB-26-WSH","side":"yes","action":"buy","limit_price":"0.005","contracts":"9400","ttl_sec":900}'
# -> 409 exceeds_market_limit
#    "KXMLB-26-WSH risks 5.03% of bankroll; risk.max_pct_per_market is 5.00%."

# now just do it eight times, same ticker, same size:
for i in 2 3 4 5 6 7 8; do curl -s -X POST localhost:8080/api/proposals \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"KXMLB-26-WSH","side":"yes","action":"buy","limit_price":"0.005","contracts":"9300","ttl_sec":900}'; done
```

**Expected:** the second proposal in the same market should be refused — the
market already has 4.97% of the bankroll committed against a 5% ceiling.

**Actual:** all eight accepted, HTTP 201, `pct_of_bankroll=0.0497388` each.

```bash
$ curl -s localhost:8080/api/risk
  "pending_cents": "39791.040000"      # 39.79% of a 100,000c bankroll
  "exposure_limit_cents": "40000.00"   # ...in ONE market, cap says 5%
```

Eight proposals, one ticker, **7.96× the stated per-market ceiling**, every one
of them reported as compliant. Nothing refused anything.

**Evidence:**
- `backend/app/trading/proposals.py:138-159` — `_guard_market_size` computes
  `fraction = float(max_loss_cents / bankroll_cents)` from the single quote it
  was handed. It issues no query. It cannot see the market's existing position
  or its other pending proposals.
- Call sites are `proposals.py:257` and `proposals.py:508` only — both pass one
  proposal's own worst case.

**Why this is P0 and not P1:** it is the only limit in the system that bounds
concentration in a *single* market. The total-exposure limit (40%) is the next
line, so the real ceiling on one market is 40% of bankroll, 8× what the config
says — and README's own gloss on total exposure ("Ten trades each inside the
per-market cap can still be the whole bankroll") shows the author believed the
per-market cap was per market. Being wrong about which of two limits is binding
is exactly how a portfolio ends up concentrated.

**Fix sketch:** `_guard_market_size` should sum `Position.net_contracts` cost
plus pending `max_loss_cents` for that ticker/event and compare the total.

---

# P1

## [P1] SAFETY-011 — the per-market limit is never re-checked in the executor, contradicting the module's own stated design

`risk.py`'s docstring (`backend/app/trading/risk.py:29-37`) is explicit: *"the
enforcement that matters is the one in the executor … an interlock that can be
sidestepped by calling a different function is not an interlock."* The executor
honours that for the halt and exposure limits and **not** for
`max_pct_per_market`.

**Evidence:**
- `executor.py:172-177` calls `risk.guard_approval`.
- `risk.py:403-426` — `guard_approval` runs `check_halted` and `check_exposure`
  and nothing else. There is no per-market check.
- `grep -rn max_pct_per_market backend/app/` returns `config.py:49`,
  `sizing.py:162`, `proposals.py:152-157`, `trading.py:576`. Never
  `executor.py`, never `risk.py`.

**Consequence:** the limit is evaluated once, at creation, against the bankroll
as it was then. Tightening `risk.max_pct_per_market` does not apply to anything
already in the queue, and any future path that creates a `ProposedTrade`
without going through `proposals.create_proposal` is unbounded per market.

---

## [P1] SAFETY-012 — the queue-depth cap does not survive concurrency: 19 pending against a cap of 10

Sibling of the lead's SAFETY-001, same root cause — an unlocked
read-then-write.

**Repro:**

```bash
# 9 pending, cap 10, fire 5 concurrently
for i in 1 2 3 4 5; do
  ( curl -s -X POST localhost:8080/api/proposals -H 'Content-Type: application/json' \
      -d '{"ticker":"KXMLB-26-WSH","side":"yes","action":"buy","limit_price":"0.005","contracts":"1","ttl_sec":900}' ) &
done; wait
```

**Expected:** one accepted, four refused with `queue_full`.

**Actual:** all five HTTP 201 (proposals 295, 296, 297, 298, 299).
`pending_proposals` → **14**.

From a clean queue, 25 concurrent requests against a cap of 10:

```
created=19  refused=6
PENDING AFTER: 19
```

**Sequentially the cap is exact** — #1..#10 return 201, #11 and #12 return
`409 queue_full`. So the guard is correct and only its atomicity is missing.

**Evidence:** `backend/app/trading/proposals.py:60-84` — `_guard_queue_depth`
is a bare `SELECT count(*) WHERE status = PENDING` with no lock, followed later
by an `INSERT`, in a session that commits at `api/routes/trading.py:303`.
Concurrent requests all read the same count. There is no unique constraint or
conditional insert that could reject the loser.

**Why P1 not P0:** README defines this row as *"Not a risk limit — it protects
*attention*"*, and no order is placed by a proposal. But it is a named
safety-model row and it does not hold: the failure mode it exists to prevent is
"a queue nobody reads is rubber-stamped", and 19 items is that queue. Reachable
from the dashboard with two tabs or a double-click.

Note the detector path is *not* affected today: `worker/main.py:151-168` scans
detectors sequentially in one session, so autoflush makes the count see the
uncommitted rows. The breach is the API path, which runs concurrently with the
worker under uvicorn.

---

## [P1] SAFETY-013 — the kill switch cannot be engaged at runtime; the config file documents a control surface that does not exist

README: *"Kill switch | Halts all proposals and cancels resting orders."*
There is no way to engage it on a running system.

**Evidence:**
- `backend/app/config.py:207-211` — `get_config()` is `@lru_cache(maxsize=1)`.
  Config is read from disk once per process and never reloaded.
- No endpoint mutates it. All five `POST` routes in the whole API are
  `/proposals/quote`, `/proposals`, `/proposals/{id}/approve`,
  `/proposals/{id}/reject`, `/orders/{id}/cancel`
  (`grep '@router\.\(post\|put\|patch\)' backend/app/api/routes/*.py`).
- The frontend only *displays* it — `SystemPanel.tsx:99-101`,
  `TradeTicket.tsx:150`, `Trades.tsx:163`. No control.
- `config.yaml:1-7` claims *"Most values are editable from the settings UI
  (which writes through to `config_kv` in Postgres and takes precedence over
  this file)"*. **That settings UI does not exist.** `ConfigKV`
  (`db/models.py:822-828`) is declared and referenced **nowhere else in the
  repo** — `grep -rn "ConfigKV\|config_kv" backend/ frontend/src/ scripts/`
  returns only the model definition. Nothing reads or writes that table.

So the only way to fire the emergency stop is to edit `config.yaml` and restart
containers — which per the ground rules is impossible on this host, and which
in general is not an emergency control.

**The worse half:** the control has two halves in two different processes.
`interlocks.py:144` (halt new orders) runs in `api`; `maintenance.py:92-101`
(cancel resting orders) runs in `worker`. Each reads its own process-local
`lru_cache`d config. A partial restart therefore engages *half* the kill
switch:

- restart `api` only → new approvals refused, **resting orders stay live on the
  exchange** while the dashboard header says the switch is engaged;
- restart `worker` only → resting orders cancelled, but the API keeps accepting
  approvals and placing new ones.

This is the "build all three images" footgun from CLAUDE.md applied to the
emergency stop, where the failure is live exposure rather than a stale test.

**Also inherent, worth stating:** `check_execution` reads the switch at
`interlocks.py:144`, before a placement round trip measured at 180–315 ms. An
approval already past that line completes regardless. A kill switch cannot stop
an in-flight order; it can only stop the next one. (Not tested — cannot engage.)

**Fix sketch:** a `POST /api/kill-switch` writing `config_kv`, with every
`config.risk.kill_switch` read going through a short-TTL DB-backed lookup rather
than the process-lifetime cache.

---

## [P1] SAFETY-014 — `executor.cancel` treats an exchange HTTP **400** as "already gone" and marks the order CANCELED locally

**Evidence:** `backend/app/trading/executor.py:630-639`

```python
except KalshiApiError as exc:
    if exc.status not in (404, 400):
        raise
    log.info(
        "order %s already gone at the exchange (%s); treating the "
        "cancel as done", order.id, exc.status,
    )
order.status = OrderStatus.CANCELED
```

**Expected:** 404 is legitimate evidence the order no longer exists at the
exchange. **400 is not** — it is *bad request*: a malformed order id, a bad
`market_ticker`, a validation change on Kalshi's side. None of those say
anything about whether the order is still resting.

**Actual:** any 400 causes the order to be recorded as CANCELED and audited as
such (`order.canceled`, `executor.py:640-646`) while it may still be working on
the book. Because `sweep_orders` selects only `LIVE_ORDER_STATUSES`
(`maintenance.py:74`), the order is then **never revisited** — not reconciled,
not re-cancelled. It becomes invisible exposure.

This is the exact shape CLAUDE.md warns about: *"A guard that silently inspects
the wrong thing is worse than no guard."* It matters most under the kill
switch, whose entire promise is that resting orders are gone.

**Verified reachable:** this is the same code path both the operator cancel
(`trading.py:471`) and the kill-switch sweep (`maintenance.py:101`) use. I
confirmed the happy path end to end — order 18 placed resting at the demo
exchange (`exchange_order_id 9ff9eff3-…`), `POST /api/orders/18/cancel` → 200,
status `canceled`. I could not induce a 400 from the exchange, so the *bad*
branch is code-level only.

**Fix sketch:** drop `400` from the tuple; on a 400, leave the order live and
re-query its status.

---

# P2

## [P2] SAFETY-015 — `confirm` is a lax bool: the string `"true"`, `"yes"`, `"on"`, `"1"`, and the ints `1` / `1.0` all place a real order

**Repro:**

```bash
curl -s -X POST localhost:8080/api/proposals/282/approve \
  -H 'Content-Type: application/json' -d '{"confirm":"true"}'
# -> HTTP 200. Order 16 placed on the demo exchange,
#    exchange_order_id abbfb361-5d88-4d52-9489-e844ad2a9449, filled 1.00
```

**Evidence:** `api/routes/trading.py:132` — `confirm: bool = False`. Pydantic v2
runs in lax mode, so the affirmative half of its bool vocabulary is accepted.
Full surface, mapped against an already-executed proposal so nothing more was
placed (`not_pending` ⇒ the value coerced to True; `not_confirmed` ⇒ read as
False; `422` ⇒ rejected):

```
true "true" "True" "TRUE" "t" "y" "yes" "on" "1" 1 1.0   -> not_pending   (TRUE)
false "off" "n" 0                                        -> not_confirmed (FALSE)
null [] {} 2 "2" "anything" " true "                     -> 422           (rejected)
{} (no confirm key)                                      -> not_confirmed
no body at all / empty body                              -> 422
```

**Deliberately not filed as P0.** The brief asked for P0 if any of these placed
an order, and one did — but rank honestly: **nothing that means "no", nothing
absent, and nothing malformed produced consent.** `confirm` is never defaulted
true; the docstring's actual claim (*"a missing confirmation is a refusal"*)
holds. The exposure is that a safety-critical boolean is not strictly typed, so
a client sending a JSON-ish `"true"` is treated as a human decision. One-word
fix: `StrictBool`.

---

## [P2] SAFETY-016 — orders that reach CANCELED without going through `executor.cancel` write **no audit row**

A gap is worse than a duplicate (cf. lead AUDIT-001, which is duplicates).

**Repro:**
```bash
curl -s "localhost:8080/api/orders?limit=500" > ord.json
curl -s "localhost:8080/api/audit?limit=1000" > aud.json
# join canceled orders against payload.order_id of kind=order.canceled
```

**Actual:** of 8 orders in status `canceled`, **3 have no `order.canceled` audit
row** — orders **9, 11 and 12**, all IOC legs on `demo_exchange` that went out
and came back unfilled.

**Expected:** README — *"Audit log | Append-only record of every signal,
proposal, decision, order, fill, and fee."*

**Evidence — two unaudited paths set CANCELED directly:**
- `executor.py:605-608` — `_settle_status`: an IOC that did not fill is set to
  `OrderStatus.CANCELED` inline. No `audit()` call. (This is the correct
  *status* per the comment above it; it is the missing audit row that is the
  bug.)
- `maintenance.py:230-232` — `reconcile_order` takes the exchange's word
  (`status == "canceled"` → `OrderStatus.CANCELED`) with no audit call either.

Only `executor.cancel` (`executor.py:640`) audits. Orders 7, 17 and 18 went
through it and are logged; 9, 11, 12 did not and are not.

---

## [P2] SAFETY-017 — signals and settlements never reach the audit log at all

**Evidence:** the complete set of audit kinds emitted anywhere in `backend/app/`
(`grep -rn 'kind="' backend/app/ --include=*.py`):

```
fill.reconciled  fill.recorded  order.canceled  order.failed  order.submitted
proposal.approved  proposal.created  proposal.expired  proposal.rejected
```

(`kind="fill"` / `kind="settlement"` also match, but those are
`backtest/replay.py:805,932` and a `roll_daily(kind=...)` argument at
`settlements.py:200` — not audit rows.)

- **No `signal.*` kind exists.** `detectors/base.py record()` writes a `Signal`
  row and publishes to Redis; it never calls `proposals.audit`. This deployment
  currently holds 5 signals (`GET /api/signals`) and **0** audit rows about
  them. README explicitly lists "every **signal**".
- **No settlement audit row.** `trading/settlements.py` contains no `audit(`
  call. Per CLAUDE.md, *"a position held to settlement realises P&L through no
  fill at all"* — settlement is the primary way this system realises money, and
  it leaves no trail in the log that is supposed to record every fee and
  decision. Latent here (0 settlements so far), certain once one lands.

---

## [P2] SAFETY-018 — the log formatters emit tracebacks unredacted, bypassing `RedactingFilter`

README: *"Secret redaction | Private keys and signature headers stripped from
logs by the formatter."* The filter scrubs `record.msg` and `record.args`
(`core/logging.py:33-48`); **neither formatter scrubs the exception text.**

**Evidence:**
- `core/logging.py:59-60` — `JsonFormatter.format`:
  `payload["exc"] = self.formatException(record.exc_info)`. `scrub()` is never
  applied to it.
- The console branch (`core/logging.py:70-75`) uses stock
  `logging.Formatter`, which appends `record.exc_text` after the scrubbed
  `%(message)s`, likewise unscrubbed.

Every `log.exception(...)` in the codebase therefore emits a raw traceback —
including `executor.py:374` on a placement failure, the one place a signed
request object is closest to hand. `KalshiApiError` itself is safe (`rest.py:121`
formats only method/path/body, no headers), so this is a latent gap rather than
an observed leak; an `httpx` or `cryptography` traceback carrying a request
repr or key bytes would go out in the clear.

**UNVERIFIED at runtime** — container logs are unreachable (no docker). Settled
by `docker compose logs api | grep -c 'BEGIN.*PRIVATE KEY\|KALSHI-ACCESS-SIGNATURE'`
after forcing a placement failure.

**Fix sketch:** `payload["exc"] = scrub(self.formatException(...))`, and a
`scrub()` on `record.exc_text` for the console path.

---

## [P2] SAFETY-019 — the duplicate guard has the same unlocked shape, and is absent entirely from the manual proposal path

**Two separate observations.**

**(a) Not applied to single-leg proposals.** `_guard_duplicate`
(`proposals.py:87-113`) is called only from `create_multi_leg_proposal`
(`proposals.py:507`). `create_proposal` — the path behind
`POST /api/proposals` — never calls it. Demonstrated in SAFETY-010: eight
identical `source="manual"` proposals on one ticker, all accepted. README scopes
the row to detectors (*"one live proposal per event per detector"*), so this is
arguably in spec for a manual ticket, but it means the guard's only coverage is
the multi-leg path.

**(b) Structurally identical to the cap that I proved breaks.**
`proposals.py:96-107` is a bare `SELECT count(*)` with no lock, no unique
constraint on `(source, event_ticker, status)`, and no conditional insert —
the same shape as `_guard_queue_depth`, which I demonstrated returns 19 against
a cap of 10 under concurrency (SAFETY-012).

**UNVERIFIED for concurrency** and, importantly, **not reachable today**: the
only caller is `worker/main.py:151-168`, which iterates detectors sequentially
in a single session, so autoflush makes the count see uncommitted siblings. It
would break with a second worker replica or any API path that creates multi-leg
proposals. Settled by adding a `POST` route for a multi-leg proposal, or by
running two worker replicas and firing one detector scan.

---

## [P2] SAFETY-020 — total-exposure and daily-loss limits are evaluated against an unlocked snapshot; UNVERIFIED at runtime because neither is reachable on this deployment

**Structural argument (definite):** `risk.guard_approval`
(`risk.py:403-426`) calls `snapshot()` (`risk.py:228-296`), which is a plain
`SELECT` over `Position` and `PnlDaily` with no `FOR UPDATE`, then
`check_exposure` compares `state.exposure_cents + additional_cents`
(`risk.py:390`). Two concurrent approvals in separate sessions read the same
pre-trade `Position` rows and each add only their own `additional_cents`. N
approvals each individually inside the headroom collectively exceed it. This is
the identical read-then-write with no lock that produces lead SAFETY-001 and
that I proved empirically for SAFETY-012.

**Why UNVERIFIED rather than confirmed:** the limits cannot be reached on this
stack. `max_total_exposure_pct: 0.40` × $1,000 bankroll = **$400**; the demo
account balance is **$80.13**, so no sequence of real fills can approach it
(current exposure 639.3¢). `daily_loss_limit_pct: 0.05` = **$50** of realised
loss, likewise unreachable. Lowering either threshold means editing
`config.yaml`, which the ground rules forbid, and would need a container
restart, which is impossible here.

**What would settle it:** set `risk.max_total_exposure_pct` to a value just
above current exposure (e.g. `0.0070`), `docker compose restart api worker`,
then fire two concurrent approvals of proposals each individually inside the
remaining headroom and check whether `exposure_cents` ends above
`exposure_limit_cents`.

---

# P3

## [P3] SAFETY-021 — `snapshot()` counts pending proposals from **every** route against the route being evaluated

`risk.py:255-262`:

```python
pending = await session.execute(
    select(ProposedTrade.max_loss_cents).where(
        ProposedTrade.status == ProposalStatus.PENDING
    )
)
```

Open positions two statements above are correctly filtered by
`Position.route == route`; pending proposals are not. The module docstring is
explicit that *"Every limit is evaluated **per route** … a simulated loss must
not halt live trading"* — this one is not. Invisible today (one route active),
and `pending_cents` feeds only `committed_cents`/`headroom_cents`, which are
display-only (`check_exposure` deliberately excludes the queue,
`risk.py:375-386`). So: display defect now, real if a second route is ever live.

## [P3] SAFETY-022 — the queue-depth guard and the header disagree about what "pending" means

`_guard_queue_depth` (`proposals.py:68-74`) counts every row with
`status == PENDING` regardless of `expires_at`. `/api/trading/state`
(`trading.py:160-169`) counts `PENDING AND expires_at > now()`. Between a
proposal's TTL elapsing and the worker's 5-second sweep, the header shows
headroom the creation guard will not grant, and `POST /api/proposals` returns
`queue_full` for a queue the dashboard shows as short.

## [P3] SAFETY-023 — audit payloads reference order/fill ids that are not stable across the manual table drops this project uses instead of migrations

Six `fill.recorded` audit rows reference `fill_id`s that no longer exist, and
`fill_id` values 1, 2 and 3 each appear twice under different routes:

```
id 7  05:30:10Z order 2 fill_id 1 route simulated
id 31 06:26:40Z order 8 fill_id 1 route demo_exchange
id 49 07:32:16Z order 8 fill_id 3 route demo_exchange
```

`audit_log` survives while `fills`/`orders` are dropped and their identity
sequences restart (CLAUDE.md: *"No migrations … schema changes still need
tables dropped by hand, and the trading tables have changed shape several
times"*). The log's referential integrity therefore silently degrades: an old
`order_id: 8` and today's order 8 are different orders. Consequence for the
report card and any forensic use of the trail. Recording `client_order_id` /
`exchange_fill_id` in the payload — both already globally unique and already
present on `order.submitted` — would survive a reset.

---

# Verified HOLDS (worth recording)

**SAFETY-H1 — `confirm` is never defaulted true.** `{}`, `{"confirm":false}`,
`{"confirmation_phrase":"KXMLB-26-WSH"}` with no `confirm` → `409 not_confirmed`.
No body, empty body, `null`, `[]`, `{}`, `2`, `"anything"` → `422`. See
SAFETY-015 for the one gap.

**SAFETY-H2 — proposal TTL is enforced at approval, live.** Both refusal paths
confirmed:
```bash
# ttl_sec=5, approved at t+5.4s (inside the 5s sweep window):
{"detail":{"error":"expired","message":"proposal 284 expired at
 2026-07-27T17:40:41.212471+00:00. The quote it was priced against is gone;
 re-propose rather than trading a stale edge."}}
# ttl_sec=5, approved at t+10s (sweep already ran):
{"detail":{"error":"not_pending","message":"proposal 283 is expired, not pending."}}
```
The `expired` code proves `interlocks.py:158` fires independently of the sweep,
validating the README row *"Checked at approval, not just by the sweep."*

**SAFETY-H3 — every non-pending state refuses.** already-`executed` → 409
`not_pending`; already-`rejected` → 409 `not_pending`; rejecting an `executed`
proposal → 409 `not_pending`; proposal `999999` and `-1` → 404. (Note: this is
the *sequential* case. The concurrent case is lead SAFETY-001.)

**SAFETY-H4 — the execution-routing matrix is implemented as documented.**
Verified by reading `backend/app/trading/interlocks.py:89-111` against README's
table. Every row correct, and two orderings are load-bearing and right:
- `if settings.is_prod: return SIMULATED` (line 105) sits **before** the
  credential check, so paper on prod cannot reach a prod exchange no matter what
  credentials exist — README's emphasised row.
- `mode=live` without `live_trading_armed` (`is_prod and live_trading`,
  `settings.py:98-104`) **raises** `live_mode_not_armed`; there is no branch
  that falls through to paper.
- `route is LIVE_EXCHANGE` additionally requires the typed ticker
  (`interlocks.py:172-179`, via `confirmation_target` which uses the *event*
  ticker for multi-leg) and credentials (`interlocks.py:181-186`).
Covered by 12 tests in `backend/tests/test_interlocks.py:91-155,255-307`.
**No config or `.env` was modified; this row is code reading only.**

**SAFETY-H5 — `trading.mode` admits exactly two values, with a test.**
`config.py:30` `mode: Literal["paper", "live"] = "paper"`;
`tests/test_config.py:110-118` asserts
`set(get_args(mode_field.annotation)) == {"paper", "live"}`, plus
`test_invalid_trading_mode_is_rejected` for `"auto"`. `TradingConfig` is
`extra="forbid"` (`config.py:18`), so no auto-trade flag can be smuggled in via
config.yaml either. No occurrence of `auto_trade`/`autotrade` anywhere.

**SAFETY-H6 — `executor.py` really is the only module that can cause an order
to exist.**
- `Order(` is constructed at exactly one site: `executor.py:306`.
- `create_order` / `create_orders_batch` are called only at `executor.py:465`
  and `executor.py:486`.
- `_place_legs` has one caller, `executor.py:208`, inside
  `approve_and_execute`.
- `approve_and_execute` has one caller in the whole app,
  `api/routes/trading.py:368`.
- `check_execution` is called at `executor.py:158`, the first statement of
  `approve_and_execute`, before any other work.
There is no path to a placement that skips it.

**SAFETY-H7 — no secret reaches any HTTP surface.** Swept:
- the shipped bundle `/assets/index-BUjuHosI.js` (390 KB) and
  `/assets/index-BT8cVkUQ.css` — **zero** base64-shaped runs ≥100 chars, zero
  UUID-shaped strings, zero `BEGIN … KEY`, zero `MII…`. The only hits for
  `SECRET`/`password`/`api_key` are React internals
  (`__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED`), an HTML input-type
  table, and UI copy naming `KALSHI_ENV` / `ANTHROPIC_API_KEY` as config keys.
- all 16 GET endpoints' bodies, plus every probe response collected this run —
  no `PRIVATE KEY`, no `KALSHI-ACCESS-*`, no `authorization`/`bearer`, no
  `*key*`/`*secret*`/`*token*`/`*signature*` JSON keys at all. The 32
  UUID-shaped strings are `client_order_id`s and exchange order/fill ids.
- `GET /api/system` reports env, mode, interlock booleans, fee-schedule
  metadata and the two **public** endpoint URLs. It does **not** expose the key
  id, key path, or any credential. `credentials_present: true` is a boolean.
- error bodies: 404/400/409/422 all return structured, internal-free detail.
  `/api/markets/%00%ff` returns a bare `500 Internal Server Error` with **no
  traceback, no file path, no SQL, no connection string** (the 500 itself is
  Agent 3's).

**SAFETY-H8 — the audit log has no UPDATE or DELETE path.** `AuditLog` is
imported in exactly three places (`db/models.py:808`, `api/routes/trading.py`
read-only, `trading/proposals.py:580` insert-only). `proposals.audit()` only
ever `session.add(...)`. There is no `session.delete`, no `delete(AuditLog)`,
no `update(AuditLog)` anywhere in `backend/app/`. Append-only holds.

**SAFETY-H9 — no *order* or *fill* is missing its primary audit row.** Across
703 audit rows / 12 orders / 6 fills: every `Order.id` appears in an
`order.submitted` payload and vice versa (both difference sets empty); every
`Fill.id` appears in a `fill.recorded` payload. The gaps are the *cancel*
transitions (SAFETY-016) and the *signal*/*settlement* kinds (SAFETY-017).

---

# State left behind

The stack was returned to a clean posture — verified:

```
pending_proposals 0   working_orders 0   kill_switch false   route demo_exchange
exposure_cents 639.30  pending_cents 0  halted false
```

- **1 extra contract of `KXMLB-26-WSH` @ $0.005** (~0.5¢), from the
  SAFETY-015 probe — order 16, exchange id `abbfb361-…`. Position on that
  ticker is now **3 contracts** (2 were the lead's SAFETY-001 residue). Left in
  place; unwinding means more trading.
- Orders **17** and **18** on `KXMLB-26-PIT` (1 and 400 contracts @ $0.005,
  placed below the bid to rest) were **both cancelled** and never filled. No
  position, no fee.
- ~60 proposals created for the cap and size probes, **all rejected** with
  reason `"SAFETY audit cleanup"` (plus proposals 283/284 expired by TTL,
  proposal 282 executed). None left pending.
- The kill switch was **never engaged** (no mechanism exists — SAFETY-013) and
  remains `false`. No `.env`, `config.yaml`, or code file was modified.

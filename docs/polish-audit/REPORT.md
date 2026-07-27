# kalshi-copilot — Polish Audit Report

**Date:** 2026-07-27 · **HEAD:** `1ee5cd3` · **Tree:** clean
**Method:** lead auditor + 5 focused subagents, static analysis and live-API
adversarial testing against the running demo stack.
**Scope note:** this run is an **audit, not a refactor**. No application code,
config, or test was modified. The only files added are these documents.

---

## Executive summary

**120 findings: 5 P0 · 20 P1 · 58 P2 · 37 P3.**

The system's *analytical* core is in better shape than its *operational*
shell. Fee math, order-direction mapping, the units discipline, tri-state
result handling, the routing matrix, the bootstrap statistics and the
backtester's refusal machinery were all attacked directly and all held — the
things CLAUDE.md says were learned the expensive way have stayed learned.
Every P0 lives somewhere else: in **concurrency**, in **input validation**,
and in the **operational controls** that were specified but never wired.

Three themes account for all five P0s and most of the P1s.

**1. The proposal state machine has no concurrency control of any kind.**
The string `with_for_update` does not appear anywhere in `backend/app/`.
There is no conditional `UPDATE ... WHERE status = ...`, no unique constraint
that would make a duplicate transition fail, and no advisory lock. Every
guard is `if status is not PENDING: refuse` evaluated against a snapshot
another transaction may already have invalidated. This produced the flagship
P0 — **two concurrent approvals of one proposal place two real orders** — and
recurs in the queue-depth cap (21 created against a cap of 10), the duplicate
guard, the expiry sweep, and the exposure snapshots. Each was verified at
runtime, not inferred.

**2. Money enters the system through a validated front door and an
unvalidated side door.** `limit_price` is range-checked `0 < p < 1`.
`fair_price`, thirty-two lines below in the same file, is not checked at all —
so `fair_price: "56"`, the obvious operator slip for 56¢, returns HTTP 200
with a claimed **+$55.97/contract** edge on a contract quoted at 1.8¢. And
because `Decimal("NaN")` is a valid Decimal, `NaN` passes the single
money-parsing chokepoint, 500s two public endpoints, and reaches Postgres:
proposal 267 is in the database right now, `status: executed`, with
`net_edge_cents: "NaN"`.

**3. Several controls are documented, displayed, and not actually
connected.** The **kill switch cannot be engaged on a running system** —
there is no endpoint, no UI control, and `get_config()` is
`@lru_cache(maxsize=1)`, so the only path is editing `config.yaml` and
restarting containers. Its two halves live in different processes, so a
partial restart leaves resting orders live while the header reads "engaged".
`risk.max_pct_per_market` measures **one proposal** rather than the market,
so eight individually-compliant proposals put 39.79% of bankroll into a
single ticker against a 5% cap. Order reconciliation by client order ID —
promised in README, CLAUDE.md and the executor's own docstring — does not
exist. And the approval card does not show the wire form, which CLAUDE.md
calls the only human guard against an inverted position.

A fourth theme is narrower but worth stating: **the UI systematically rounds
money to zero.** `asDollars` renders the backend's canonical
`est_fee_cents: "1.7500"` as **`$0.02`** — the precise number CLAUDE.md's
units section says is wrong — and renders real sub-cent fees as `$0.00`.
`fees_paid_cents` is rendered by nothing at all. The backend is right; three
display formatters undo it at the point the operator reads the number.

### What held

Recorded deliberately, because an audit that lists only defects
misrepresents the system:

- **Fee math**, including every part that is usually wrong: 1 @ 50¢ → 1.75¢,
  100 @ 50¢ → exactly $1.75, 2 @ 20¢ → 2.24¢ (matching real demo billing),
  centicent rounding, maker default M=0, series-keyed multipliers, fee-free
  series → 0, fractional counts, and `taker_fee_cents(56, …)` raising.
  Verified live *and* in the test suite.
- **Order direction** — all four rows, `no/buy` → `ask @ 0.70`, single-sourced
  in `direction.py` and inlined nowhere.
- **Prices as strings** end-to-end. No `parseFloat`/`parseInt` on any money
  field in the source or the shipped bundle.
- **The routing matrix** — all five rows, including paper-never-touches-prod
  and live-never-degrades-to-paper. No auto-trade mode exists.
- **Proposal TTL enforced at approval time**, not merely by the sweep.
- **Sequential** double-approve, over-size proposals, and expired proposals
  are all correctly refused.
- **Tri-state `result`/`settled` handling** everywhere — the documented trap
  is not repeated.
- **The backtester's refusals** name each measured number against its
  threshold; no bare "insufficient data" anywhere.
- **The report card** refuses correctly, uses the bootstrap interval, never
  sums routes, and exposes an `unattributed` counter.
- **The audit log is faithful** — it recorded the P0 breach in full, which is
  how the P0 was proven.
- **The websocket client reconnects *and* resubscribes** its ticker filter.
- **The six `seq` tests were rewritten, not deleted**, in the HEAD commit.

### Immediate recommendation

Fix the five P0s before anything else, and treat **SAFETY-001 and SAFETY-010
as blockers for any further trading**, even on demo — both cause the system
to take positions the operator did not authorise. `FIXLIST.md` orders the
whole backlog by severity then effort; the top five are all S or M.

Do not enable `stale_quote` until **BE-001** is fixed. It is filed P1 only
because two disabled flags gate it; the moment an operator follows the
documented "enable one detector at a time" workflow it becomes a P0.

---

## What this audit could NOT check

Two capabilities the brief assumed were unavailable on this host. Full detail,
with the exact unblock commands, is in **`BASELINE.md`**.

- **Docker is inaccessible** (`justin` is not in the `docker` group; `sudo`
  needs a password). So: no `docker compose build`, **no pytest / ruff / mypy
  baseline**, no `scripts/backtest.py` run, no container logs, no service
  restarts, and **no parallel `audit-fresh` stack — the empty / first-run
  state was never audited.**
- **No browser automation** (chromium cannot start — missing `libatk-1.0.so.0`;
  no node/npm/pip). So: **no screenshots, no UI interaction, no console
  check, no viewport testing, no 390px phone pass.**

**`docs/polish-audit/screenshots/` is therefore empty, and no finding cites a
screenshot.** Frontend findings rest on complete source reading (19 files,
4,174 lines) plus the real API payloads those components receive — stated as
"component X renders live value Y as Z" with the source line. Anything whose
visual result genuinely needs a browser is marked **`UNVERIFIED — needs
browser`** rather than guessed.

**Agent 4's first-person operator diary could not be produced.** It required
driving the UI. Its mandate was folded into the frontend agent as
source-traced operator-flow analysis (UI-011 through UI-024 are the friction
findings), but the walkthrough itself — the squinting, the hesitation, the
hunting for a control — is the one deliverable this run genuinely owes you.
It needs the browser fix.

Because the test suite could not be run, **its green/red status at HEAD is
unknown**; Agent 2's audit is entirely static. The route surface of the
running image matches HEAD exactly (29 operations, 28 paths), so the image is
at least not badly stale.

---

## Severity counts by area

| Area | P0 | P1 | P2 | P3 | Source |
|---|---|---|---|---|---|
| Safety / concurrency (SAFETY, AUDIT) | 3 | 3 | 8 | 3 | lead + Agent 7 |
| API contract & docs drift (API) | 2 | 4 | 8 | 9 | Agent 3 |
| Backend correctness (BE) | 0 | 4 | 11 | 9 | Agent 1 |
| Frontend & UX (UI) | 0 | 4 | 24 | 6 | Agent 5 |
| Tests & toolchain (TEST) | 0 | 4 | 7 | 9 | Agent 2 |
| Report card (RC) | 0 | 0 | 1 | 1 | lead |
| **Total** | **5** | **20** | **58** | **37** | |

**Deduplication applied.** Agents 3, 5 and 7 overlapped as expected. Two
findings were merged, keeping the stronger evidence:

- `API-005` (queue cap breaks under concurrency, 21 vs cap of 10) ≡
  `SAFETY-012` (9 pending + 5 concurrent = 14; 25 concurrent → 19). **Merged
  into SAFETY-012**, which carries both measurements.
- `UI-005` (no kill-switch control anywhere in the UI) ≡ `SAFETY-013` (kill
  switch cannot be engaged on a running system). **Merged into SAFETY-013**;
  UI-005 is the UI half of one defect, and two agents reaching it
  independently is why it was promoted to P0.

No other cross-agent duplicates were identified. Findings sharing the
*no-locking* root cause (SAFETY-001/011/012/019/020, AUDIT-001/002) are kept
separate because each needs its own fix and its own test, but they are grouped
as one theme in `FIXLIST.md`.

---

## P0 findings

### [P0] SAFETY-001 — Two concurrent approvals of ONE proposal place TWO real orders
*Confirmed at runtime by the lead. Full detail: `raw/lead-p0.md`.*

**Repro:** create a proposal; fire two `POST /api/proposals/{id}/approve`
concurrently.
**Expected:** one order. The second must be refused `not_pending`
(`interlocks.py:151`) or return the existing order (`executor.py:180`, whose
comment says catching a double-click is its entire purpose).
**Actual:** both returned **HTTP 200 and both placed**. Proposal 268 → orders
14 and 15, two distinct `client_order_id`s, two distinct **exchange** order
IDs, two fills, **2 contracts where 1 was authorised**, created 470µs apart.
**Evidence:** `GET /api/orders?ticker=KXMLB-26-WSH`; audit rows 537-542 show
two `proposal.approved`, two `order.submitted`, two `fill.recorded` for
proposal 268.
**Violates:** CLAUDE.md hard constraint #1; README *Per-trade approval*;
README *Idempotent orders* — `client_order_id` is a fresh `uuid.uuid4()` per
attempt (`executor.py:311`), not derived from the proposal, so the exchange
cannot dedupe.
**Cause:** TOCTOU. `interlocks.py:151` reads status; the flip to APPROVED is
at `executor.py:190` and uncommitted until after placement.
`_live_order_for` (`executor.py:257-270`) is an unlocked `SELECT`. No
`with_for_update` anywhere; no unique constraint on `Order.proposal_id`.
Aggravating: `LIVE_ORDER_STATUSES` (`executor.py:71-75`) excludes
`EXECUTED`/`FILLED`, so it does not match an order that already filled.
**Fix sketch:** re-read the proposal `FOR UPDATE` and re-check status under
the lock; partial unique index on `Order.proposal_id`; derive
`client_order_id` from `(proposal_id, leg_seq)`.

### [P0] SAFETY-010 — `max_pct_per_market` measures the proposal, not the market
*Agent 7; cause independently confirmed by the lead.*

**Repro:** submit eight identical proposals on `KXMLB-26-WSH`, each 4.97% of
bankroll. Sequential — no race required.
**Expected:** `config.yaml` states the limit as "5% of bankroll in any single
market"; README's per-market row says it is enforced at proposal creation.
**Actual:** all eight accepted; `pending_cents: "39791.040000"` — **39.79% of
bankroll in one ticker, 7.96× the stated ceiling**, every one reported
compliant.
**Cause:** `_guard_market_size` (`proposals.py:138-159`) divides *one
proposal's* `max_loss_cents` by the bankroll and **issues no query at all** —
it takes no `session` parameter, so it cannot see the market's existing
position or other pending proposals. The per-proposal boundary is exact
(9,300 contracts → accepted at 4.97%; 9,400 → `exceeds_market_limit` at
5.03%), which is why it looks like it works.
**Note:** README's gloss on *total* exposure ("Ten trades each inside the
per-market cap can still be the whole bankroll") shows the author believed
this cap was per market. The real per-market ceiling is the 40% total-exposure
limit.
**Fix sketch:** aggregate existing position cost basis + pending proposals for
the ticker before comparing to the cap.

### [P0] SAFETY-013 — The kill switch cannot be engaged on a running system
*Agent 7 (P1) + Agent 5 UI-005, merged and **promoted to P0** by the lead.*

**Expected:** README safety model — "Kill switch | Halts all proposals and
cancels resting orders."
**Actual:** there is **no way to engage it**. Verified: `get_config()` is
`@lru_cache(maxsize=1)` (`config.py:207-211`); no API route mutates
`kill_switch` — the only references read it (`health.py:78` displays,
`trading.py:266` checks); the UI surfaces it three times, all read-only; and
`config.yaml`'s own header documents a settings UI writing through to
`config_kv` **that does not exist** — `ConfigKV` is referenced nowhere outside
its model definition at `db/models.py:822`. Firing the emergency stop means
editing `config.yaml` and running `docker compose restart api worker`.
**Worse:** the two halves live in different processes (`api` halts approvals,
`worker` cancels resting orders), each with its own cached config. **Restart
`api` only and resting orders stay live while the header reads "engaged"** —
CLAUDE.md's "a guard that silently inspects the wrong thing is worse than no
guard", exactly.
**Promotion rationale:** an emergency control that requires a container
restart is not an emergency control, and the partial-restart mode actively
misreports its own state. Two agents reached it independently from opposite
directions.
**Not testable this run:** whether it truly cancels at the exchange, because
it cannot be engaged. Agent 7 did verify the underlying mechanism end-to-end
(order 18 placed resting → `POST /api/orders/18/cancel` → 200 → `canceled`,
through the same `executor.cancel` the sweep uses).

### [P0] API-001 — `fair_price` is unvalidated: the units trap, unguarded
*Agent 3; reproduced independently by the lead.*

**Repro:**
`POST /api/proposals/quote {"ticker":"KXMLB-26-HOU","side":"yes","action":"buy","limit_price":"0.018","contracts":"1","fair_price":"56"}`
**Actual:** **HTTP 200**, `net_edge_cents: "5597.5700"` — a claimed
**+$55.97/contract** edge on a contract quoted at 1.8¢. Also accepts
`"999999"` (→ 99,999,897¢) and `"-1"`.
**Cause:** `pricing.py:210-211` parses `fair_price` with no range check, **32
lines below the identical `0 < p < 1` check on `limit_price` at
`pricing.py:174-182`**. `TradeTicket.tsx:245-253` is free text with no client
validation either — typing "62" for 62¢ is the obvious operator mistake.
**Impact:** persists to `proposed_trades.net_edge_cents` and the audit log,
i.e. it becomes the "claimed edge" the report card grades against.
**Fix sketch:** apply the same `0 < p < 1` guard to `fair_price`.

### [P0] API-002 — `NaN` bypasses money parsing: 500s, and it persists to the DB
*Agent 3; **partially corrected** by the lead — see the note.*

**Repro:** `GET /api/fees/quote?price_dollars=NaN&contracts=1&series=KXMLB`
**Actual:** **HTTP 500**, bare `Internal Server Error` body. Same for
`contracts=NaN`. `Decimal("NaN")` is a valid Decimal so `money.py:69` accepts
it; `fees.py:267` then raises `InvalidOperation` — an `ArithmeticError`, which
the `ValueError` handlers do not catch.
**Worse:** where the range check is skipped (via API-001), NaN reaches
Postgres. **Proposal 267 is live in the database as `status: "executed"` with
`net_edge_cents: "NaN"` and `legs[0].fair_price: "NaN"`** — and it filled a
real demo order (order 13).
**Lead correction:** Agent 3 reported that one NaN row makes
`avg_claimed_edge_cents` NaN for its group via `report.py:243`. **This did not
reproduce.** The group containing proposal 267 reads `avg_claimed_edge=None`,
not NaN — manual proposals carry `signal_id: null` and contribute no *claimed*
edge. Severity kept at P0 (wrong money math persisted against an executed
trade, plus 500s on public endpoints) but **the blast radius is narrower than
reported, and the report-card symptom is not a valid regression test.**
**Note:** the codebase already knows this class — `undervalued_screener.py`,
`replay.py` and `news/budget.py` all call `.is_finite()`. The single
money-parsing chokepoint does not.

---

## P0-on-enable

### [P1 → P0 when enabled] BE-001 — Barrier crypto markets priced as terminal-value
*Agent 1; confirmed at runtime by the lead. Detail: `raw/lead-verification.md`.*

Live market `KXBTCMAXMON-BTC-26JUL31-7000000` has `strike_type: "greater"`,
`floor_strike: 70000`, and rules reading **"is *ever above* $70000.00, then
the market resolves to Yes"** — a path-dependent barrier on the running
maximum. Both the veto (`stale_quote.py:96`) and the model (`vol.py:205-207`,
`d2 = (ln(S/K) − σ²/2)/σ`) read only `strike_type` and treat it as "is spot
above K *right now*". `P(max S_t > K)` exceeds `P(S_T > K)` by up to the whole
contract value once the barrier is touched — Agent 1's worked example reaches
**+96¢ of phantom edge on a contract already resolved YES**, by flipping to
buy NO at 0.02 against a fair of 0.98.

This is the +72¢ ETH bug in a new dimension: right asset, **wrong statistic**.
The market's own title ("trimmed mean be above") contradicts its rules ("ever
above"); only the rules text carries the word that matters.

**Blast radius:** ~15-20 active markets (`KXBTCMAXMON` 8, `KXBTCMAXY` 7,
`KXBTCMAX100`). Small in count, unbounded in per-trade error.
**Reachability:** requires `stale_quote.enabled: true` **and**
`bitcoin.enabled: true`; both ship and are deployed `false`. That is the only
reason this is not filed P0 — and enabling detectors one at a time is the
*documented* workflow.
**Structural point:** the weather engine parses `rules_primary` and refuses
what it cannot classify. The crypto path parses no rules text at all. The
refusal machinery already exists in this codebase; it was not applied here.

---

## P1 findings

**Concurrency & safety**
- **SAFETY-011** — the per-market limit is never re-checked in the executor;
  `guard_approval` runs only `check_halted` + `check_exposure`, contradicting
  `risk.py`'s own docstring that executor enforcement is the one that matters.
- **SAFETY-012** *(merged with API-005)* — queue depth cap breaks under
  concurrency: 9 pending + 5 concurrent → **14**; from empty, 25 concurrent →
  **19 against a cap of 10**; 24 barrier-synchronised POSTs → **21**.
  Sequentially exact. Same unlocked `SELECT count(*)`-then-INSERT
  (`proposals.py:58-83`). No money escapes — each still needs individual
  approval — but the *attention* guard is what fails, and protecting attention
  is the cap's stated purpose.
- **SAFETY-014** — `executor.cancel` treats exchange **HTTP 400** as "already
  gone" (`executor.py:630-639`) and marks the order CANCELED locally. 404 is
  evidence; 400 is *bad request* and says nothing about whether the order
  still rests. `sweep_orders` never revisits it. Matters most under the kill
  switch, whose whole promise is that resting orders are gone.
- **AUDIT-001** — `proposal.expired` logged 2-3× per proposal: 214 rows for
  182 distinct proposals, **15% redundant**, 31 of 32 duplicates **21-25ms
  apart**. Two unlocked callers race — the worker sweep
  (`maintenance.py:54`) and `GET /api/proposals`, which expires on read
  (`trading.py:321`). Money-path audit kinds are **clean** (zero duplicates
  across created/approved/submitted/filled).

**Backend correctness**
- **BE-002** — the undervalued screener silently drops **68,110 of 88,110**
  eligible markets. `MAX_SCREENER_ROWS = 20_000` with **no ORDER BY and no
  log**; its score is a *relative* volume percentile, so which 23% it got
  decides every rank it emits.
- **BE-003** — "Recovery is reconciliation by client order ID" **does not
  exist**, though claimed in README's safety table, CLAUDE.md, and
  `executor.py`'s docstring. `client_order_id` is matched only *within a batch
  response*; `get_orders()` has no such filter. A timed-out POST is marked
  REJECTED (`executor.py:362-366`), REJECTED is not in `LIVE_ORDER_STATUSES`,
  and `reconcile_order` returns early when `exchange_order_id` is falsy
  (`maintenance.py:167`) — exactly the timed-out case.
- **BE-004** — **single-leg detector findings can never become proposals.**
  `base.py:211-213` returns `None` for `len(legs) < 2`, silently; `"legs"` is
  emitted **only** by `set_arbitrage.py:131`; `create_multi_leg_proposal`
  itself refuses <2 legs (`proposals.py:501`). *Independently confirmed by the
  lead.* So set-arb is the only detector that can ever reach the queue, while
  README describes stale-quote's Kelly sizing and "the binding cap is named in
  the proposal's rationale" — a proposal that detector cannot produce.
- **BE-005** — **UNVERIFIED, needs runtime.** `streams.py:303-316` builds a
  multi-row INSERT from `normalize_ticker`'s *variable* key sets with
  `ON CONFLICT SET` over the **union** of keys. Agent 1 sampled 5,000 live
  markets and found zero NULLed quote columns, so could not settle it without
  SQLAlchemy on the host. Blast radius if real: the scanner subscribes all
  149,360 markets so mixed batches are the norm, and a failure rolls back
  tape + candles + book snaps in the same flush.

**API contract**
- **API-003** — six 500s from malformed query params:
  `offset=999999999999999999999`, `max_hours_to_close=±1e308`, and `%00` in
  any free-text filter (`/api/markets?status`, `/api/orders?ticker`,
  `/api/signals?detector`, `/api/audit?kind`). Everything else (`limit=0`,
  `sort=DROP TABLE`, `order=sideways`) returns clean 422/400.
- **API-004** — WS `tickers` passed as a *string* is exploded per character:
  `"KXMLB-26-HOU"` → acked as `["-","2","6","B","H","K","L","M","O","U"]`, and
  the client then receives **zero** ticks forever (`ws.py:156`).
- **API-006** — WS proposal broadcasts ship `leg_count: N` with `legs: []`
  (`proposals.py:595` omits the `legs` argument). Invisible today only because
  `Trades.tsx:128` uses the message purely as a refetch trigger.

**Frontend — the "money rounds to zero" cluster**
- **UI-001** — **every fee in the UI is rounded to a whole cent.** The backend
  returns the canonical `est_fee_cents: "1.7500"` for 1 contract at 50¢;
  `asDollars` renders **`$0.02`** — literally the number CLAUDE.md says is
  wrong — in the one place the operator reads a fee before committing. All
  three live fills (`"0.040000"`, `"0.130000"`) render as **`$0.00`**.
  `api.ts:723-729`, `TradeTicket.tsx:273`, `Trades.tsx:379`,
  `ApprovalCard.tsx:151`.
- **UI-002** — **`fees_paid_cents` is rendered by nothing.** `grep -c` on the
  shipped bundle = **0**, though it is declared for positions (`api.ts:476`)
  and the report card (`api.ts:430`) and returned by the API. The live NFL
  position shows unrealised `-$0.06` / realised `$0.00` while its
  `fees_paid_cents` is `"20.880000"`. Violates hard constraint #5 — "every
  edge/EV number shown anywhere must be net of fees. A gross edge is a lie."
- **UI-003** — the report card renders a real loss as **`-$0.00`**. Live:
  `total_pnl_cents "-0.250000"` → `-$0.00`, while the *same row's* mean and CI
  correctly read `-0.08¢` and `[-0.13¢, -0.04¢]`. One row contradicts itself,
  in the panel that decides whether real money gets deployed.
  `ReportCard.tsx:168-173`.
- **UI-004** — **the approval card does not show the wire form**, though
  README:713 says it does and CLAUDE.md calls it the only human guard against
  an inversion. `ProposalLeg` has no wire field; `ApprovalCard` renders only
  the traded-side price and `"{action} {side}"`. `book_side` appears **once**
  in the entire shipped bundle — TradeTicket's pre-propose preview, which is
  not on screen at approval and does not exist for detector proposals.
  Invisible today only because every live set-arb leg is `sell yes`, where
  wire price == limit price.

**Tests & toolchain**
- **TEST-001** — `app/kalshi/ws.py` has **zero tests**, and after `1ee5cd3` it
  is the *only* orderbook gap detector. `_check_seq` (ws.py:247-266), the
  per-sid table, `__resync__` synthesis and `_sids.clear()` on reconnect are
  covered by nothing. (`test_ws_hub.py` is the *browser* relay, a different
  module.) The bug just fixed was silent for four milestones because a
  mis-judged seq does not raise; its replacement has no test.
- **TEST-002** — **the documented "only ever flatters" drawdown error is live
  in `engine.py:101`.** `replay()` appends one equity point per observation
  with no pre-trade zero, and `test_backtest_replay.py:863-874` *asserts* that
  shape. `stats.max_drawdown([-1.75,-1.75])` is `0`; with the leading zero it
  is `1.75`. `report.py:611-620` gets this right with a five-line comment and
  `test_backtest_stats.py:763-777` pins the error class explicitly — so the
  codebase knows, and `engine.py` is the call site that missed it.
- **TEST-003** — `app/weather/stations.py` (⭐) entirely untested. The
  `KXLOW` = Lowe's / `KXSNOWFLAKE` = Snowflake exact-lookup trap has no
  regression test though CLAUDE.md says the lookup "must stay one".
- **TEST-004** — secret redaction (hard constraint #6) untested.
  `core/logging.py:11-28` has five regexes, no test that any fires, and no
  test that redaction survives `%`-style lazy formatting — which is how the
  codebase logs.

---

## P2 and P3 findings

58 P2 and 37 P3 findings are recorded in full, with repro and evidence, in the
per-agent raw files. Index by area:

| Area | File | P2 | P3 |
|---|---|---|---|
| Backend correctness | `raw/agent1-backend.md` | BE-006 … BE-016 | BE-017 … BE-025 |
| Tests & toolchain | `raw/agent2-tests.md` | TEST-005 … TEST-011 | TEST-012 … TEST-020 |
| API & docs drift | `raw/agent3-api.md` | 8 findings | 9 findings |
| Frontend & UX | `raw/agent5-frontend.md` | UI-006 … UI-029 | 6 findings |
| Safety | `raw/agent7-safety.md` | SAFETY-015 … SAFETY-020 | 3 findings |
| Lead | `raw/lead-p0.md`, `raw/lead-observations.md` | AUDIT-002, RC-001 | RC-002 |

Highlights worth naming here:

- **SAFETY-015** — `confirm` is a lax bool: `"true"`, `"yes"`, `"on"`, `"1"`,
  `1`, `1.0` all place a real order (proved — order 16 on the demo exchange).
  **Deliberately P2, not P0**: the full surface was mapped and nothing meaning
  "no", nothing absent, and nothing malformed produced consent. `confirm` is
  never defaulted true. Fix is `StrictBool`.
- **SAFETY-016/017** — audit-log *gaps*, which are worse than duplicates: 3 of
  8 canceled orders have no `order.canceled` row, **no `signal.*` audit kind
  exists at all** (5 signals, 0 rows), and settlements write no audit row.
  README claims the log covers "every signal, proposal, decision, order, fill,
  and fee."
- **SAFETY-018** — both log formatters emit `formatException` output
  unscrubbed, bypassing `RedactingFilter`. No secret was found leaking at any
  HTTP surface or in the shipped bundle — this is the one code-level gap.
- **UI-022** — the system page reports **M7/M8/M9 as not done** (confirmed in
  the deployed bundle). **UI-023** — the empty queue says "detectors start
  filling this queue in M4". **UI-019** — NewsPanel says "the headline engine
  is off" directly above 30 headlines.
- **UI-024** — **zero `@media` queries in the entire stylesheet.** The 390px
  phone pass in the brief would have failed on layout alone.
- **UI-015** — the in-tab-only alerting limitation exists solely as a source
  comment, while **259 of 262 live set-arb proposals died to TTL**.
- **UI-011** — the Settlements table is missing `className="table"` and
  renders unstyled — invisible until the first resolution.
- **BE-010** — `--mark-verified` validates `rounding_increment_dollars`, which
  `fees.py` never reads (CENTICENT is hardcoded).
- **BE-006** — `stale_quote` still selects by `category == "Crypto"`; 7,216
  active markets have no category.
- **RC-001** — the report card's "funnel" is not a funnel: `approved: 0` sits
  beside `executed: 1` because the fields are *current-status* counts, not
  pass-through counts.
- **TEST-012** — `backend/config.yaml` is a 0-byte file committed in HEAD.
  Harmless in the container (the bind mount at `docker-compose.yml:39`
  shadows it) but a live trap for the host-run path the README documents.
- **UNVERIFIED, honest limits:** total-exposure and daily-loss limits are
  **not proven safe** — they are unreachable on this deployment (40% of
  bankroll is $400 against an $80 demo balance; the daily loss limit is $50)
  and settling them needs a `config.yaml` change plus a restart, which was
  forbidden and impossible here. Structurally they are the identical unlocked
  read-then-write as SAFETY-012.

---

## Latent, checked, and *not* currently broken

Recorded so nobody re-investigates:

- `reference_symbol_for` (`stale_quote.py:160-181`) resolves the underlying by
  **prefix match** (`series.startswith("KXBTC")`), contradicting both its own
  docstring and CLAUDE.md's "must stay an exact dict lookup" rule — in the very
  module whose wrong-asset bug motivated that rule. **It does not mis-route
  anything today**: every live series under each prefix is genuinely that
  asset, including the suspicious `KXSOLE` (10,100 markets), which really is
  Solana. Filed **P3** as latent risk. It is, however, the mechanism that
  sweeps the barrier markets of BE-001 into the detector.
- Null-candle handling: the backend midpoint-fills correctly; 190 live candles
  sampled, 0 nulls reaching the chart. The chart's 0-100 axis has two
  independent clamps. Candle `ts` is period START.
- `risk.snapshot()`'s unfiltered pending query is correct — `ProposedTrade`
  has no route column by design.
- Zero bare `except:` in the repo. Zero skip/xfail markers in the test suite.

---

## State left on the system by this audit

- **Proposal 267** — `status: executed`, `net_edge_cents: "NaN"`, filled order
  13 (1 contract `KXMLB-26-HOU` @ $0.018). **This is the evidence for API-002
  and was deliberately not removed** — the audit had no mandate to delete
  rows, and no DB access without docker. You may want it gone before anyone
  reads the report card.
- **3 contracts of `KXMLB-26-WSH` @ $0.005** (~1.5¢ total) — 2 from the
  SAFETY-001 P0 repro, 1 from Agent 7's SAFETY-015 probe.
- **1 contract of `KXMLB-26-HOU` @ $0.018** from proposal 267.
- Orders 17/18 on `KXMLB-26-PIT` were placed to rest and both cancelled
  unfilled.
- ~60 probe proposals, all rejected with reason `"SAFETY audit cleanup"`.
- **Final state verified clean:** `pending_proposals: 0`, `working_orders: 0`,
  `kill_switch: false`, `environment: demo`, `trading_mode: paper`,
  `live_trading_armed: false`. No `.env`, `config.yaml`, or application code
  was modified. No production surface was contacted at any point.
- The parallel `audit-fresh` compose project was **never created** (no docker),
  so there is nothing to tear down.

# Polish audit — what was fixed

Companion to `REPORT.md` (the findings) and `FIXLIST.md` (the backlog).
Fix pass run 2026-07-28 against HEAD `1ee5cd3`.

> **Nothing in this pass could be executed.** No docker means no `pytest`, no
> `ruff`, no `mypy`; the running stack serves a pre-built image, so even the
> live API could not exercise a single change. Every Python file was checked
> with `python3 -m py_compile`, import/name sanity was checked by AST, and two
> pure functions were extracted and run standalone (below) — but **treat all of
> this as unverified until you run the suite.** See `BASELINE.md` for the
> unblock commands.

---

## The five P0s

### SAFETY-001 — concurrent double-approve placed two real orders

Three independent layers, because the first two are application-level and the
third is not:

1. **Row lock before any check reads status.** `executor.approve_and_execute`
   now re-reads the proposal `SELECT … FOR UPDATE` with
   `execution_options(populate_existing=True)`. The `populate_existing` is
   load-bearing: the proposal is already in the identity map, and without it
   SQLAlchemy returns the stale in-memory attributes and the lock protects
   nothing. A concurrent approver now blocks, re-reads `APPROVED`, and is
   refused by the existing `not_pending` interlock.
2. **`_live_order_for` → `_existing_order_for`**, with the status filter
   removed. The old filter was `(PENDING, RESTING, PARTIALLY_FILLED)` — it
   excluded `FILLED`, the normal outcome for a taker order, so the guard
   missed the common case. `REJECTED` now counts too: a timed-out write may
   have reached the matching engine, and treating that as licence to place
   again is the double-submit this codebase refuses everywhere else.
3. **Deterministic `client_order_id`** from `(proposal_id, leg_seq,
   created_at)` via `uuid5`. This is the satisfying one — `client_order_id` is
   already `unique=True` in the schema, so making it deterministic **turns the
   existing constraint into the database-level duplicate guard with no
   migration**, which matters because this project has none. `created_at` is
   folded in so a rebuilt database cannot mint an ID colliding with a real
   historical order.

### SAFETY-010 / 011 — `max_pct_per_market` measured the proposal, not the market

New `risk.market_exposure_cents()` sums **position cost basis + pending
proposals** for the ticker set. `_guard_market_size` is now async, takes the
session, and compares the *projected* total; the multi-leg path passes every
leg's ticker so the cap sees the whole footprint. `guard_approval` re-checks it
at approval time (SAFETY-011) with `exclude_proposal_id`, so the proposal being
approved is not counted twice — the same trap `check_exposure` documents for
`pending_cents`.

### SAFETY-013 — the kill switch could not be engaged on a running system

Moved to Redis (`core/redis.KILL_SWITCH_KEY`) so both processes see one value,
and `get_kill_switch()` **fails closed** — if Redis is unreachable, "unknown"
on an emergency stop reads as engaged.

- `POST /api/kill-switch` engages/releases, cancels every working order, and
  reports `canceled_orders` **and `failed_cancels`** — a cancel that did not
  happen is the thing an operator most needs to see.
- `check_execution` takes a **required** `kill_switch: bool`. No default:
  `False` would mean a caller that forgot it silently bypasses the stop.
- The worker's cancel sweep and both `/api/system` and `/api/trading/state`
  now read the effective value, so the header cannot say "safe" while the
  runtime flag is set.
- `config.risk.kill_switch` remains a **one-way floor** the API cannot
  release.

### API-001 — `fair_price` was unvalidated

The same `0 < p < 1` check `limit_price` already had, 32 lines above. Error
text names the actual mistake: *"'56' means $56, not 56 cents"*.

### API-002 — `NaN` bypassed money parsing

`_to_decimal` is split into `_coerce_decimal` + `_reject_non_finite`, so the
check covers **every** path including the `Decimal` passthrough — which is how
a NaN constructed upstream would otherwise have slipped in untouched. Verified
`core/fees.py` routes all money through this chokepoint and that no legitimate
infinity sentinel exists anywhere in `app/`.

### BE-001 (P0-on-enable) — barrier markets priced as terminal-value

`stale_quote.is_path_dependent()` parses `rules_primary` for barrier language;
`resolve_strike` takes `rules_primary` as a **required** argument and refuses.
`BARRIER_SERIES` denylists the `MAX` families as a second layer.

**Behaviourally verified** — the one thing in this pass that was actually
executed. The function was extracted via AST and run standalone against the
real live rules text:

| input | expected | got |
|---|---|---|
| `"…is ever above $70000.00…"` (the live `KXBTCMAXMON` text) | refuse | refuse |
| `"…is above $70000.00 at 5pm ET"` | allow | allow |
| `"The maximum payout is $1 per contract."` | allow | allow |
| `"…at any point during the month"` | refuse | refuse |
| `None` / `""` | allow | allow |

The phrase list was deliberately tightened after a first draft — bare
`"maximum"`/`"minimum"`/`"high of"` would have refused ordinary settlement
prose, and a veto that fires on everything is the same as no detector.

---

## Also fixed by the lead

| ID | Fix |
|---|---|
| SAFETY-012 | One Postgres advisory lock (`pg_advisory_xact_lock`) serialises proposal creation, closing the queue-depth, duplicate **and** per-market races together. Skipped on non-Postgres and on session doubles. |
| SAFETY-019 | `create_proposal` now calls `_guard_duplicate` for detector sources. Manual tickets exempt — a human proposing twice is a second decision, not a repeated scan. |
| AUDIT-001 | `expire_stale` is a conditional `UPDATE … RETURNING`, so only the transaction that actually changed the row logs it. |
| AUDIT-002 | `reject()` locks and re-checks. |
| SAFETY-014 | `cancel` no longer treats HTTP **400** as "already gone". Only 404 is evidence. |
| SAFETY-015 | `confirm` and the kill-switch body are `StrictBool`. |
| BE-003 | Placement failures split: a definite 4xx → `REJECTED`; a network error or 5xx → left `PENDING` with an `order.submit_ambiguous` audit row, for reconciliation by client order ID. |
| BE-004 | Single-leg findings now become proposals via `create_proposal`, through the same guards. Five of six detectors could previously never reach the queue while the README described the proposals they would produce. |
| BE-018 | `default_is_safe` split into `default_taker_is_safe` / `default_maker_is_safe`. **See the note below — the obvious fix here was wrong.** |
| BE-022 | New `legs_for()` batch loader kills the N+1 on the polled queue endpoint. |
| BE-023 | `exchange_fill_id` derives from `client_order_id`, not the exchange ID which is often absent — it was literally `"None-immediate"` and collided on `uq_fill_id`. |
| BE-012 | Four genuinely-unread config keys commented out with the reason, and two dead fields removed from `config.py`. `DetectorConfig` is `extra="allow"`, which is why two lived only in YAML. |
| UI-004 (backend half) | `leg_view` now emits `book_side` and `wire_price`, computed by calling `direction.py` — never a second copy of that table. |
| — | `main.py` boot log uses `enabled_detector_names`, matching `/api/system`. |

### The fix that would have been a regression

Agent B's handoff proposed making `default_is_safe` check
`max(maker) <= default_maker` alongside taker. Measured against the real
schedule: **max listed maker is 1.0, the documented default is 0** — so that
change flips the flag to `False`, `_multipliers` raises `UnknownSeries` for
every unlisted series, and **nothing in the catalogue can be proposed**. That
is precisely the abandoned category-keyed design that once excluded ~50,000
markets over a multiplier that did not apply.

The correct fix is side-specific: an unlisted series is safe to price as a
**taker** (max listed taker 1.0 ≤ default 1.0) and must be refused as a
**maker** (1.0 > 0). `_multipliers(series, need=...)` now asks the narrower
question, so the taker path — which is what `costs.assume_taker` actually uses
— is unaffected, and only the side that can genuinely understate fails closed.

---

## Known residue and risk

- **Two bugs were found in my own new kill-switch endpoint** after writing it —
  a missing `LIVE_ORDER_STATUSES` import and a wrong `cancel()` signature.
  `py_compile` caught neither. That is the honest measure of how much
  confidence this pass deserves without a test run.
- `check_execution` gained a required argument, and `resolve_strike` did too.
  **Existing tests calling either will `TypeError`.** That is deliberate —
  fail-closed beats a silent default — but it means the suite will not be green
  until those call sites are updated.
- `asDollars` changed semantics in the frontend (renders cents under $1). It
  is the only way a 1.75¢ fee stops displaying as `$0.02`, but it changes many
  displays at once.
- The advisory lock serialises proposal creation. Correct, and creation is
  rare, but it is a new global serialisation point.
- Total-exposure and daily-loss limits remain **UNVERIFIED** — unreachable on
  an $80 demo balance.

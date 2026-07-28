# kalshi-copilot — Polish Audit Fix Backlog

Deduplicated, ordered by **severity, then effort** (cheapest first within a
severity band, so the quick wins land before the architectural ones).

Effort key: **S** ≈ under an hour · **M** ≈ half a day · **L** ≈ a day or more
(design decision required).

Every item cites its finding in `REPORT.md`; full repro and evidence are in
`raw/`.

---

## Blockers — fix before any further trading, demo included

| # | ID | Fix | Effort |
|---|----|-----|--------|
| 1 | **API-001** | Apply the existing `0 < p < 1` guard to `fair_price` in `pricing.py:210-211` — the identical check already sits 32 lines above at `:174-182`. Add the same validation to `TradeTicket.tsx:245-253`. | **S** |
| 2 | **API-002** | Reject non-finite Decimals at the money-parsing chokepoint (`core/money.py:69`) with `.is_finite()` — the pattern already exists in `undervalued_screener.py`, `replay.py` and `news/budget.py`. Catch `ArithmeticError`, not just `ValueError`, at the `/api/fees/quote` and `/api/proposals/quote` handlers. Backfill/remove the one corrupt row (proposal 267). | **S** |
| 3 | **SAFETY-001** | Re-read the proposal `SELECT … FOR UPDATE` at the top of `approve_and_execute` and re-check status under that lock. Add a partial unique index on `Order.proposal_id` for live+filled statuses as a database-level backstop. Derive `client_order_id` deterministically from `(proposal_id, leg_seq)` (`executor.py:311`) so the exchange rejects a duplicate even if the app races. Add `EXECUTED`/`FILLED` to the set `_live_order_for` matches, or query without the status filter. | **M** |
| 4 | **SAFETY-010** | Make `_guard_market_size` (`proposals.py:138-159`) take a `session` and aggregate the market's **existing position cost basis + other pending proposals** for that ticker before comparing to `max_pct_per_market`. Fix the docstring, which currently describes the per-proposal behaviour as if it were per-market. | **M** |
| 5 | **SAFETY-013** | Give the kill switch a real engage path and shared state. It cannot live in `@lru_cache`-d `config.yaml` when its two halves run in different processes — put the flag in Redis or `config_kv` (the `ConfigKV` model already exists, unused, at `db/models.py:822`), add a `POST /api/kill-switch` endpoint, and a UI control. Until then the header must not claim "engaged" when only one process has restarted. | **L** |

**Also treat as a blocker for the feature it gates:**

| # | ID | Fix | Effort |
|---|----|-----|--------|
| 6 | **BE-001** | Do not enable `stale_quote` until barrier markets are refused. Parse `rules_primary` for path-dependent language ("is ever above/below") and refuse what cannot be classified as terminal-value — the weather engine already does exactly this and the machinery can be reused. Cheaper interim: make `REFERENCE_PREFIXES` an exact dict lookup and omit the `MAX` series entirely (also fixes the latent P3 prefix-match risk). | **M** |

---

## P1 — functional bugs

Quick wins first; these are mostly small and several are one-liners.

| # | ID | Fix | Effort |
|---|----|-----|--------|
| 7 | **UI-001 / UI-002 / UI-003** | One cluster, one fix: the three display formatters in `api.ts:681-739`. `asDollars` must not `toFixed(2)` sub-cent money (1.75¢ is rendering as `$0.02`, real fills as `$0.00`, a real loss as `-$0.00`). Render `fees_paid_cents`, which is currently displayed **nowhere** despite being returned for positions and the report card — hard constraint #5 requires P&L be visibly net of fees. | **S** |
| 8 | **TEST-002** | Prepend the zero point to the equity curve in `engine.py:101` so `max_drawdown` cannot report 0 for an opening loss, and **correct** `test_backtest_replay.py:863-874`, which currently asserts the broken shape. `report.py:611-620` is the reference implementation. | **S** |
| 9 | **AUDIT-001** | Replace the read-modify-write in `expire_stale` (`proposals.py:318-350`) with a conditional `UPDATE … WHERE status='pending' AND expires_at <= now RETURNING id`, and audit only the returned rows. Also projects and bounds the query, fixing the unprojected `select(ProposedTrade)`. | **S** |
| 10 | **SAFETY-011** | Call the per-market guard from `guard_approval` as well as at creation — `risk.py`'s own docstring says executor enforcement is the one that matters. Depends on #4. | **S** |
| 11 | **SAFETY-014** | `executor.cancel` must not treat HTTP **400** as "already gone" (`executor.py:630-639`). 404 is evidence of absence; 400 is not. Re-query the order rather than marking it CANCELED locally. | **S** |
| 12 | **API-003** | Bound/validate the six params that 500: `offset` (cap it), `max_hours_to_close` (reject ±inf), and reject `%00` in free-text filters on `/api/markets`, `/api/orders`, `/api/signals`, `/api/audit`. | **S** |
| 13 | **API-004** | Type-check the WS `tickers` payload (`ws.py:156`) — a string is currently exploded per character and the client then silently receives zero ticks forever. Reject with an error frame. | **S** |
| 14 | **API-006** | Pass `legs` to the proposal broadcast (`proposals.py:595`), which currently ships `leg_count: N` alongside `legs: []`. | **S** |
| 15 | **TEST-003** | Add a regression test pinning `station_for_series` as an **exact** lookup (`KXLOW` ≠ weather, `KXSNOWFLAKE` ≠ weather). | **S** |
| 16 | **TEST-004** | Test that each of the five redaction regexes in `core/logging.py:11-28` actually fires, **including through `%`-style lazy formatting**, which is how the codebase logs. Hard constraint #6 is currently unasserted. | **S** |
| 17 | **SAFETY-012** | Enforce the queue-depth cap atomically — conditional insert, unique/exclusion constraint, or a lock around `proposals.py:58-83`. Same shape as #3, so fix them together. | **S–M** |
| 18 | **BE-005** | **Verify first** (needs SQLAlchemy — one compile check, blocked on docker). If real, make `streams.py:303-316` build the INSERT from a fixed column set rather than the union of variable key sets. | **S** to verify |
| 19 | **UI-004** | Add the wire form to `ProposalLeg` and render it on `ApprovalCard` — "buy NO 30¢ → **ask @ 0.70**". CLAUDE.md calls this the only human guard against an inverted position, and README:713 already claims it exists. | **M** |
| 20 | **BE-002** | Give the screener query an `ORDER BY` and log the truncation — it currently drops **68,110 of 88,110** eligible markets arbitrarily, and its score is a *relative* percentile, so the sample decides every rank. | **M** |
| 21 | **BE-004** | Decide and implement: either allow single-leg detector proposals (removing the silent `len(legs) < 2` drop at `base.py:211-213`) or document that only set-arbitrage can propose. Today README describes stale-quote proposals that cannot exist. | **M** |
| 22 | **TEST-001** | Test `app/kalshi/ws.py` — `_check_seq`, the per-sid table, `__resync__` synthesis, `_sids.clear()` on reconnect. After `1ee5cd3` it is the **only** orderbook gap detector and it has zero tests. | **M** |
| 23 | **BE-003** | Implement reconciliation by client order ID, or remove the claim from README, CLAUDE.md and `executor.py`'s docstring. Currently a timed-out POST is marked REJECTED and `reconcile_order` returns early on the exact case it exists to handle. | **M** |

---

## P2 — operator friction and integrity gaps

58 findings; full detail in `raw/`. Grouped by fix, roughly in value order.

| Theme | IDs | Fix | Effort |
|---|---|---|---|
| Stale UI copy | UI-022, UI-023, UI-019 | System page reports M7/M8/M9 as not done; the empty queue says detectors arrive "in M4"; NewsPanel says the engine is off above 30 headlines. Pure copy. | **S** |
| Unstyled settlements table | UI-011 | Missing `className="table"` — invisible until the first resolution. | **S** |
| `confirm` is a lax bool | SAFETY-015 | `StrictBool` on `ApproveRequest`. Nothing meaning "no" ever produced consent, so this is hardening, not a hole. | **S** |
| Audit-log gaps | SAFETY-016, SAFETY-017 | 3 of 8 cancels write no row; **no `signal.*` kind exists at all**; settlements write nothing. Gaps are worse than duplicates and README claims full coverage. | **S–M** |
| Exception logs bypass redaction | SAFETY-018 | Both formatters emit `formatException` unscrubbed. No leak was found at any HTTP surface, but this is the one code-level gap in constraint #6. | **S** |
| Report-card funnel is not a funnel | RC-001 | `approved: 0` beside `executed: 1` — the fields are current-status counts. Report pass-through counts or rename them. | **S** |
| Alerting limitation undiscoverable | UI-015 | In-tab-only alerting exists solely as a source comment while **259 of 262** set-arb proposals died to TTL. Surface it in the UI. | **S** |
| No responsive layout | UI-024 | **Zero `@media` queries.** The phone pass in the brief would fail on layout alone. | **M** |
| live/cached badges incomplete | UI-012, UI-013 | Only 1 of 3 market-page panels shows provenance (README:650 says all do); the headline quote is frozen at page load without a key. | **M** |
| Unbounded / unprojected queries | BE-007, BE-008, BE-016, BE-009 | Every `MAX_*_ROWS` cap truncates with no `ORDER BY` and no log; the longshot query has no `.limit()`; `select(Market)` unprojected in the screener endpoint. The documented recurring bug class. | **M** |
| Remaining unlocked guards | SAFETY-019, SAFETY-020, AUDIT-002 | Duplicate guard has the same shape and `create_proposal` never calls it; exposure/daily-loss snapshots unlocked; concurrent rejects both succeed. Fold into #3/#17. | **M** |
| Fee-schedule tooling drift | BE-010 | `--mark-verified` validates `rounding_increment_dollars`, which `fees.py` never reads. | **S** |
| Dead config & detector wiring | BE-011, BE-012, BE-013 | Weather detector never appears in `enabled_detectors`; seven dead config keys; the `ConfigKV` write-through documented in `config.yaml`'s header does not exist (see #5). | **M** |
| Test-suite hardening | TEST-005 … TEST-011 | 62 async tests could silently skip with no `addopts`; settlement mixed-units and cooldown-from-settlement unasserted; 27 `noqa`s suppress rules not in `select`; mypy sets 4 of ~11 strict flags; the 31 KB approval surface in `api/routes/trading.py` is untested; no tripwire for the stale-image trap. | **M–L** |

---

## P3 — cosmetic and latent

37 findings; see `raw/`. Notable:

- **BE-006 / prefix-match latent risk** — `reference_symbol_for` uses
  `startswith` against its own docstring and CLAUDE.md's exact-lookup rule.
  **Verified not mis-routing today** (`KXSOLE` really is Solana), but it is
  how BE-001's barrier markets reach the detector. Fixed by #6. **S**
- **TEST-012** — `git rm backend/config.yaml` (0-byte file committed in HEAD;
  harmless in the container, a trap for host runs). **S**
- **RC-002** — `pct_of_bankroll` is a JSON float in scientific notation
  (`5.4e-6`) in a response where everything else is a string. **S**
- **UI-006** — `asCount("10.50")` renders `"11"`, against its own docstring.
  (No longer `0`, so the historic bug is fixed — this is the residue.) **S**
- **TEST-013 / TEST-020** — two docstrings still assert pre-fix `seq`
  semantics; CLAUDE.md's "1,547 tests" is stale (1,438 on disk, ~1,557
  collected). **S**
- **BE-015** — Coinbase spot vs Kalshi's CF Benchmarks BRTI settlement index:
  an undeclared basis difference. Worth a documented note before the BTC
  detector is trusted. **S**

---

## Deferred — requires the blocked environment

These could not be assessed and should be re-run once docker and a browser
are available (unblock commands in `BASELINE.md`):

1. **The pytest / ruff / mypy baseline** — the suite's actual pass/fail at
   HEAD is unknown. Run this before acting on any test finding.
2. **Agent 4's operator walkthrough** — the first-person UX diary, the reason
   the audit was commissioned. Needs a browser.
3. **The empty / first-run state** — the parallel `audit-fresh` stack. The
   unverified-fee-schedule dead-end and every empty state are unaudited.
4. **Visual QA** — three viewports, loading/empty/populated/error states,
   console cleanliness, truncation, 7-leg cards, layout shift.
5. **Total-exposure and daily-loss limits** — unreachable on an $80 demo
   balance; needs a lowered threshold plus a restart. **Not proven safe.**
6. **Kill switch item (c)** — whether it truly cancels at the exchange.
   Blocked by #5 in the blocker table: it cannot be engaged at all.
7. **BE-005** — one SQLAlchemy compile check settles it either way.

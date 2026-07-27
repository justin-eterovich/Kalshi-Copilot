# Lead — additional findings and verified-good invariants

## Findings

### [P2] RC-001 — The report-card "funnel" is not a funnel: `approved` is a terminal-state count

**Repro:** `curl -s localhost:8080/api/report-card`

**Actual**, for `set_arbitrage` / `demo_exchange`:
```json
"funnel": {"proposals":262,"approved":0,"executed":1,"partial":2,
           "rejected":0,"expired":259,"pending":0,"failed":0,
           "orders":6,"fills":2,"decided":3}
```

**Expected:** a funnel narrows monotonically — `proposals >= approved >=
executed`. Here `approved: 0` sits directly beside `executed: 1` and
`partial: 2`, and `rejected: 0` beside `decided: 3`.

**Cause:** these are counts of proposals *currently in* each status, not
cumulative counts of proposals that *passed through* it. A proposal that was
approved and then executed is counted only under `executed`.

That is internally defensible but it is displayed as a funnel and labelled
with lifecycle verbs, so it reads as "nothing was ever approved, yet three
things executed" — the opposite of true. An operator checking whether their
approvals are converting cannot read this table.

**Fix sketch:** either report cumulative pass-through counts (`approved` =
approved + executed + partial + failed), or rename the terminal-state fields
so they do not read as funnel stages (`now_pending`, `now_approved`, ...).

### [P3] RC-002 — `pct_of_bankroll` is a JSON float in scientific notation

`POST /api/proposals` returns `"pct_of_bankroll": 5.4e-6` — a JSON **number**,
not a string, in a response where every other money-shaped field
(`est_fee_cents`, `max_loss_cents`, `limit_price`, `contracts`) is correctly a
string. Rendered naively in the browser this shows as `5.4e-6`.

Not a precision-critical field (it is a ratio for display, not money), which
is why this is P3 rather than P1 — but it is an inconsistency in the
"one number language" the project otherwise keeps rigorously.

**Evidence:** `curl -s -X POST localhost:8080/api/proposals -d '{...}'` →
`"pct_of_bankroll":5.4e-6`.

---

## Verified good — invariants that held under test

Recording these because a polish audit that only lists defects misrepresents
the system. Each was checked at runtime against the live demo stack.

**Fee math is right, including the parts that are usually wrong.**
1 contract @ $0.005 → `est_fee_cents: "0.0400"`. Raw taker fee is
`0.07 × 1 × 0.005 × 0.995 = $0.00034825 = 0.0348¢`, which rounds **up to the
centicent** (0.01¢) to give exactly 0.0400¢. A cent-rounding implementation
would have said 1¢ — 28x too high. An earlier manual ticket in the audit log
shows 2 contracts @ $0.50 → `est_fee_cents: "3.5000"`, i.e. 1.75¢/contract,
matching the documented worked example exactly.

**Prices are strings end-to-end.** Every money field checked on
`/api/positions`, `/api/proposals`, `/api/orders`, `/api/report-card` came
back as a JSON string (`"0.005000"`, `"-20.880000"`, `"-12.00"`). The one
exception found is RC-002 above.

**The signed-position convention holds and is presented in trader language.**
`/api/positions` for a NO position returns simultaneously:
```json
{"net_contracts":"-12.00","side":"no","contracts":"12.00",
 "avg_price":"0.530000","avg_yes_price":"0.470000"}
```
Signed YES-equivalent negative for NO, trader-facing `side`/`contracts`
positive, traded-side price 0.53 and YES-equivalent 0.47 summing to exactly
1.00. This is precisely the documented dual convention, exposed without
forcing the reader to do the conversion.

**TTL is enforced at approval time, not merely by the sweep.**
`interlocks.py:158` raises `expired` before routing, independently of
`expire_stale`. Matches the README safety-model row.

**Sequential double-approval is correctly refused.** `interlocks.py:151`
rejects any proposal whose status is not `PENDING`. (It is only the
*concurrent* case that breaks — see SAFETY-001.)

**The report card refuses exactly as documented.** `verdict:
"insufficient_evidence"` with headline *"2 trades is too few to evaluate —
insufficient evidence; the -10.44c/trade mean is not reportable at this
sample size"* — it names the measured number and the threshold rather than
saying "no data". `ci_method: "bootstrap"` as required. Routes are reported
separately (`demo_exchange` vs `simulated`), never summed. An `unattributed`
counter is exposed, implementing "ambiguous outcomes are dropped, not
apportioned".

Notably the CI `[-17.40, -3.48]` **excludes zero**, and the verdict is *still*
`insufficient_evidence` — the `min_trades: 20` gate fires first, which is the
documented behaviour ("No verdict is reported below
`report_card_min_trades` closed decisions, whatever the mean looks like").
`max_drawdown_cents` equals the total loss on an all-losing 2-trade sample,
consistent with an equity curve that starts at zero.

**The audit log is complete and faithful.** It recorded the SAFETY-001 breach
in full — both approvals, both order submissions, both fills — which is how
the P0 was proven. Every action this audit generated appears in it.

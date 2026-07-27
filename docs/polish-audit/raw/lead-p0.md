# Lead findings — confirmed P0

## [P0] SAFETY-001 — Two concurrent approvals of ONE proposal place TWO real orders

**This is a confirmed breach of the project's first hard constraint.** One
proposal, one human approval intent, two orders on the exchange.

**Repro** (run against the live demo stack, 2026-07-27):

```bash
# 1. create one proposal
curl -s -X POST localhost:8080/api/proposals -H 'Content-Type: application/json' \
  -d '{"ticker":"KXMLB-26-WSH","side":"yes","action":"buy",
       "limit_price":"0.005","contracts":"1","ttl_sec":300}'
# -> 201, proposal_id 268, status pending

# 2. approve it TWICE, concurrently
( curl -s -X POST localhost:8080/api/proposals/268/approve \
    -H 'Content-Type: application/json' -d '{"confirm":true}' -o a1.json ) &
( curl -s -X POST localhost:8080/api/proposals/268/approve \
    -H 'Content-Type: application/json' -d '{"confirm":true}' -o a2.json ) &
wait
```

**Expected:** exactly one order. The second request must be refused with the
`not_pending` interlock (`interlocks.py:151`), or return the already-existing
order without placing a second (`executor.py:180`, whose comment states this
is its entire purpose: *"A double-click, or a retry after a UI timeout.
Returning the order that already exists is the whole point of recording it
before placing it."*).

**Actual:** **both requests returned HTTP 200 and both placed an order.**

```
$ curl -s "localhost:8080/api/orders?ticker=KXMLB-26-WSH&limit=20"
order=14 proposal=268 status=filled count=1.00 filled=1.00
        coid=05a06fbb-db30-4bab-b65c-6377585657fd created=17:32:16.501702Z
order=15 proposal=268 status=filled count=1.00 filled=1.00
        coid=c10ccd46-402b-4f18-8fe4-1f677e253ab5 created=17:32:16.501232Z
```

Two distinct `client_order_id`s, two distinct **exchange** order IDs
(`70887e57-ca1a-47dc-8dea-43eeea54be10` and
`586b64b6-7fc6-407d-9296-81ba6cb2200a`), two fills, **2 contracts bought
where the operator authorised 1**. Orders created 470 microseconds apart.

The audit log records the breach faithfully — which is a point in the
system's favour, and also the proof:

```
id=538 17:32:16.501232Z proposal.approved  {'proposal_id': 268, 'route': 'demo_exchange'}
id=537 17:32:16.501702Z proposal.approved  {'proposal_id': 268, 'route': 'demo_exchange'}
id=541 17:32:16.501232Z order.submitted    {'proposal_id': 268}
id=539 17:32:16.501702Z order.submitted    {'proposal_id': 268}
id=542 17:32:16.501232Z fill.recorded      {'order_id': 15, 'price': '0.0050'}
id=540 17:32:16.501702Z fill.recorded      {'order_id': 14, 'price': '0.0050'}
```

**Invariants violated:**

- CLAUDE.md hard constraint #1 — "No order reaches Kalshi without explicit
  per-trade approval." The second order was never separately approved.
- README safety model, *Per-trade approval* — "Every order, always."
- README safety model, *Idempotent orders* — "Client-supplied order IDs; a
  network retry cannot double-place." It can. `client_order_id` is a fresh
  `uuid.uuid4()` **per placement attempt** (`executor.py:311`), not derived
  from the proposal, so the two placements carry different IDs and the
  exchange has no basis to dedupe them.

**Root cause — a TOCTOU with no lock anywhere:**

- `backend/app/trading/interlocks.py:151` reads `proposal.status is not
  ProposalStatus.PENDING` and refuses. The status flip to `APPROVED` happens
  later, at `executor.py:190`, and is not committed until after placement.
- `backend/app/trading/executor.py:179` calls `_live_order_for()`
  (`executor.py:257-270`), a plain `SELECT ... LIMIT 1` with no lock.
- **`with_for_update` does not appear anywhere in `backend/app/`** — verified
  by grep. There is no row locking in the codebase.
- There is **no unique constraint on `Order.proposal_id`** (`db/models.py:438`,
  `nullable=True`, no `UniqueConstraint`), so the database will not reject the
  second insert either.

Both requests therefore observe `PENDING` and "no live order", and both
proceed through the entire placement path. The window spans the full exchange
round-trip (~180-315ms measured here), which a UI double-click sits inside
comfortably.

**Aggravating:** `LIVE_ORDER_STATUSES = (PENDING, RESTING, PARTIALLY_FILLED)`
(`executor.py:71-75`) **excludes `EXECUTED`/`FILLED`**. So `_live_order_for`
does not match an order that already filled — the common case for a taker
order on a liquid book. The status check at `interlocks.py:151` is the only
thing blocking a *sequential* re-approval; that one does hold (verified
separately). But it means the second guard is weaker than it appears.

**Fix sketch:** re-read the proposal `FOR UPDATE` at the top of
`approve_and_execute` and re-check status under that lock; and/or add a
partial unique index on `Order.proposal_id` for live+filled statuses; and/or
derive `client_order_id` deterministically from `(proposal_id, leg_seq)` so
the exchange rejects the duplicate even if the app races.

**Note:** the sequential double-approve case is correctly refused, and TTL
**is** checked at approval time (`interlocks.py:158`), validating the README
row "Checked at approval, not just by the sweep."

**Residue from this test:** 2 contracts of `KXMLB-26-WSH` @ $0.005 held on the
demo exchange (~1¢ total). Left in place; unwinding would mean more trading.

---

## [P1] AUDIT-001 — `proposal.expired` is logged 2-3x per proposal (unguarded read-modify-write race)

**Repro:**
```bash
curl -s "localhost:8080/api/audit?limit=600" > audit.json
# group by (kind, ticker, payload)
```

**Expected:** the audit log is documented as an *append-only record of every
signal, proposal, decision, order, fill, and fee* — one event, one row.

**Actual:** of **214** `proposal.expired` rows, only **182** are distinct
proposals — **32 redundant rows, 15% of the expiry log**. Multiplicity: 160
proposals logged once, 12 logged twice, 10 logged **three times**.

**31 of the 32 duplicate pairs are 21-25 milliseconds apart** (median 0.025s);
the single outlier is 6,685s. That timing rules out a sweep interval and
identifies a concurrency race.

**Root cause:** `expire_stale()` (`backend/app/trading/proposals.py:318-350`)
is a read-modify-write with no lock:

```python
stale = (await session.execute(
    select(ProposedTrade).where(
        ProposedTrade.status == ProposalStatus.PENDING,
        ProposedTrade.expires_at.isnot(None),
        ProposedTrade.expires_at <= now,
    )
)).scalars().all()
for proposal in stale:
    proposal.status = ProposalStatus.EXPIRED
    await audit(session, kind="proposal.expired", ...)
```

It has **two independent concurrent callers**:
1. `backend/app/worker/maintenance.py:54` — the worker's timer sweep
2. `backend/app/api/routes/trading.py:321` — `GET /api/proposals`, which
   expires on every read ("Expire on read as well as on the worker's timer")

So every time the dashboard polls the queue while the worker sweep runs, both
transactions select the same pending rows and both write an audit entry.

**Severity rationale:** no money impact and no decision impact — the
money-path kinds are **clean**, verified: `proposal.created` (275 rows),
`proposal.approved` (11), `order.submitted` (11), `fill.recorded` (8) all have
**zero** redundant payloads. But the audit log is a named safety control, any
count derived from it is 15% overstated, and it is the same unguarded
read-modify-write shape that produces SAFETY-001 above.

**Fix sketch:** make the expiry a conditional `UPDATE ... WHERE status =
'pending' AND expires_at <= now RETURNING id`, and audit only the returned
rows.

---

## [P2] AUDIT-002 — Concurrent rejects also both succeed (same root cause)

**Repro:** create a proposal, fire two `POST /api/proposals/{id}/reject`
concurrently with different reasons.

**Actual:** proposal 270 — both requests returned HTTP 200, and **two**
`proposal.rejected` audit rows were written 0.5ms apart, with *different*
reasons:

```
544 17:34:01.863956Z proposal.created
545 17:34:02.109496Z proposal.rejected  reason="race probe B"
546 17:34:02.109966Z proposal.rejected  reason="race probe A"
```

**Expected:** the second reject should hit `ProposalError` and return 409 —
`reject()` is reached through the same non-PENDING check the approve path
uses.

No money impact (nothing is placed by a reject), and `decision_reason` simply
ends up last-writer-wins. Filed at P2 because it is the third instance of one
systemic defect.

---

## Systemic root cause tying SAFETY-001, AUDIT-001 and AUDIT-002 together

**The proposal state machine has no concurrency control of any kind.** Every
transition — approve, reject, expire — is a read-modify-write across separate
sessions with:

- no `SELECT ... FOR UPDATE` (the string `with_for_update` appears **nowhere**
  in `backend/app/`),
- no conditional `UPDATE ... WHERE status = ...` guarded by the DB,
- no unique constraint that would make a duplicate transition fail,
- no advisory lock or Redis lock on the proposal.

Every check is `if proposal.status is not PENDING: refuse` evaluated against a
snapshot that another transaction may already have invalidated. All three
observed defects are that one gap, and the money-losing one is SAFETY-001.

Any fix should address the state machine as a whole rather than patching the
approve path alone.

**Also noted:** that `select(ProposedTrade)` has no column projection and no
`LIMIT` — the documented "unbounded/unprojected query" anti-pattern. It is
bounded today only by `max_pending_proposals` (10), which is a bound on the
data, not on the query.

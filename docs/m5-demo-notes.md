# M5 — risk layer and notifications

Status: **done**. Verified on the live stack against the Kalshi demo
environment on 2026-07-27.

M5 turns three config keys that were decorative into limits that actually
refuse trades, and gives the approval queue a way to interrupt.

---

## The prerequisite that made this milestone bigger than it looked

Two of the three limits read realised P&L. Before this milestone the only
P&L the system recognised came from `realized_from_fill` — which realises
**only when a position is reduced by trading**.

A position held to settlement realised nothing at all. Ever. The system
recorded the cost of building it and none of the proceeds.

That is the way most of these theses are meant to pay off, so a daily loss
limit reading that number would have been reassuring and blind — worse than
absent, because it would have looked enforced. `GET /portfolio/settlements`
existed in the API and was never called.

So settlement ingestion is the first half of M5, not a follow-up.

### Two books, two sources

| book | settles from | why |
|------|--------------|-----|
| `demo_exchange`, `live_exchange` | `GET /portfolio/settlements` | the exchange's own record of what it paid |
| `simulated` | the market's `result` / `settlement_value` | those positions exist nowhere but this database, so nothing will ever tell them a market resolved |

Feeding the exchange's settlement list to the simulated book would realise
P&L against contracts that book never held, so `sync_exchange_settlements`
raises rather than accepting `route="simulated"`.

### The payout, not the cost basis

The settlement payload reports its own cost basis. We ignore it and price
against **our** `avg_price`. The two disagree whenever a position was partly
traded out before settlement — the exchange's basis covers the contracts it
still saw, ours covers what we recognised — and a book sourcing cost basis
from two places ends up with the position row and the daily total telling
different stories about the same market.

The payload is used only for what it alone knows: that the market resolved,
and what one YES contract paid.

### A units trap, in a new place

`GET /portfolio/settlements` **mixes unit conventions inside one object**:

| field | units |
|-------|-------|
| `yes_total_cost_dollars`, `no_total_cost_dollars`, `fee_cost` | fixed-point dollar **strings** |
| `revenue`, `value` | **integer cents** |

This is the only endpoint in the API where that happens. Parsing a cents
field as dollars understates it a hundredfold, silently. Only `value` is read
here, and only as a fallback for scalar markets — the market's own
`settlement_value` is fixed-point and carries precision that `value` has
already rounded away.

### Verified on real data

Two simulated positions inserted into markets that had genuinely resolved
`no` (`KXBNBD-26JUL2618-T659.99` / `-T664.99`), then the sweep run:

| position | carried at | settled | realised |
|----------|-----------|---------|----------|
| +10 YES | 0.40 | no | **−400.00¢** |
| −5 (5 NO @ 30c) | 0.70 YES | no | **+350.00¢** |

Both positions flattened, both `Settlement` rows written, the NO side earning
correctly through the sign convention. The exchange sweep also runs clean
against the live demo account (0 applied — nothing held has settled yet).

---

## The three limits

All evaluated **per route**. The simulated, demo and live books are separate
stacks of money: a simulated loss must not halt live trading, and a live loss
must certainly not be offset by a paper gain.

### `max_total_exposure_pct`

Exposure is **cost basis, not mark-to-market**. On a binary contract what you
paid is your maximum loss, so they are the same number — and marking to
market would let a position that has moved in your favour free up room for
more risk before it has actually paid out.

Verified live: with the limit tightened to 0.7% ($7.00) against $6.36 of real
demo exposure, a proposal risking $5.18 was refused:

```
HTTP 409 {"error": "exceeds_total_exposure",
  "message": "approving this would put 11.54 USD at risk on demo_exchange,
   over the 0.7% total-exposure limit (7.00 USD). 6.36 USD is already
   committed; close something or raise risk.max_total_exposure_pct."}
```

The proposal stayed `pending` — every limit here is temporary, so a refusal
must not consume the decision.

**Pending proposals count toward the displayed headroom but not toward the
check.** The proposal being approved is itself in `pending_cents`, so
including the queue in the check would count it twice and refuse the very
trade its headroom was reserved for. The queue shows up in `headroom_cents`
for display, where its job is to warn that approving everything would not fit.

### `daily_loss_limit_pct`

Net **of fees** — which is not a detail. Verified live with realised P&L at
−$49.00, inside the $50.00 limit, and $2.00 of fees paid:

```
HTTP 409 {"error": "daily_loss_limit",
  "message": "today's realised P&L on demo_exchange is -51.00 USD after
   fees, at or past the 5.0% daily loss limit (-50.00 USD)..."}
```

A limit reading only realised P&L would have let that through. A strategy
that pays more in fees than it makes is losing money and the limit has to say
so.

Refused at **both** enforcement points — approving an existing proposal and
creating a new one.

### `cooldown_after_consecutive_losses`

The only limit here that is about the operator rather than the money: a
losing streak is when a human is most likely to approve something they would
otherwise refuse.

A "close" is anything that realised P&L — a reducing fill **or** a settlement.
Both count, and on this system settlement is the more common of the two.
Verified live: three losing settlements produced

```
HTTP 409 {"error": "loss_cooldown",
  "message": "3 consecutive losing closes on demo_exchange
   (risk.cooldown_after_consecutive_losses = 3). Cooling off for another
   50 minute(s), until 2026-07-27T09:48:14+00:00."}
```

and one subsequent winning settlement cleared it (`streak 0`, `halted false`).
A **scratch does not break a streak** — breaking even is not evidence that
anything has changed — but a winner does.

### Where they are enforced

In `Executor.approve_and_execute`, beside the other interlocks, because that
is the only function that can cause an order to exist. `RiskError` subclasses
`InterlockError`, so nothing can catch "interlocks" and quietly miss the risk
limits, and the API layer's existing 409-with-a-code mapping already covers
them.

The **halt** checks additionally run at proposal creation, so a halted system
stops producing proposals rather than accumulating a backlog it will refuse
one at a time — which would train the operator to click through refusals
during exactly the drawdown the halt was called for.

---

## Kelly sizing

Detectors shipped a fixed `size_hint`: ten contracts whether the edge was one
cent or thirty. That is a placeholder, not a sizing rule.

On a binary contract Kelly collapses to `f* = (p - c) / (1 - c)`, where `c`
is the all-in cost per contract and `f*` is the fraction of bankroll to stake.

Two things about it are worth stating because both bit during implementation:

- **`c` must be after fees.** Kelly is exquisitely sensitive near breakeven:
  a 2c gross edge on a market charging 1.7c is a 0.3c real edge, and sizing
  off the gross number stakes roughly **seven times** too much. This is the
  "a gross edge is a lie" rule in its most expensive form.
- **The fraction rises with the win probability, not with the payout.** I got
  this backwards while writing the tests. A 5c edge on a 90c contract gets a
  *larger* Kelly fraction (0.50) than the same 5c edge on a 10c contract
  (0.056), because at 90c the stake is lost only 5% of the time. Full Kelly
  on a 98c-fair contract bought at 90c is **80% of bankroll** — from a
  heuristic fair value with no volatility model behind it. That number is why
  the caps exist.

Three caps, all ceilings, and the binding one is named in the proposal's
rationale so the card can say *why* a size is what it is:

| cap | source |
|-----|--------|
| `kelly` | `f* × risk.kelly_fraction` |
| `market_cap` | `risk.max_pct_per_market` |
| `exposure` | remaining portfolio headroom |
| `depth` | contracts actually executable at that price |

Sizes round **down** to the 0.01 contract tick — rounding up would step past
whichever cap just bound the size, which is how a limit becomes advisory.

Applied in `stale_quote`. **Not** applied in `set_arbitrage`: an arbitrage is
sized by executable depth, not by a probability, and Kelly does not describe
it.

---

## Notifications: what works, and what cannot

A proposal lives about two minutes. If nobody looks at the tab in that window
the decision is missed, so the queue needs to interrupt.

**Web Push cannot work here and is now defaulted off.** The Push API and
service workers require a *secure context*; the dashboard is served over
plain HTTP on a LAN address, by design. `notifications.web_push_enabled`
previously defaulted `true`, which was a promise the browser silently
refuses to keep.

What ships instead needs no service worker and no permission prompt, and
works only while a tab is open:

| mechanism | verified |
|-----------|----------|
| tab title `(1) kalshi-copilot` | yes |
| favicon badge (canvas → data URI) | yes |
| audio ping (WebAudio, two tones) | armed on first user gesture |

Alerting compares **proposal ids**, not counts: one proposal expiring while
another arrives leaves the count unchanged, and that is exactly a moment
worth a ping. The first poll never pings — a page load is not an event.

### A bug the live run caught

The app ships **no favicon**, so `baseFavicon` was captured as `""`. The
clear path read `if (baseFavicon) link.href = baseFavicon` — empty string is
falsy, so restoring was a no-op and the badge dot stuck on forever after the
queue emptied. Tests would not have caught it; driving the browser did.
Fixed by removing the injected `<link>` when there was nothing to restore to,
and re-verified: `cleared -> kalshi-copilot | (none)`.

### To get alerts with the tab closed

The dashboard needs TLS. Two options, both operator decisions:

| option | cost |
|--------|------|
| self-signed cert, accept the warning per device | ~1h, fiddly, per-device |
| `mkcert` local CA installed on your devices | ~2h, best UX |

Neither involves exposing anything publicly — the LAN-only constraint stands.

---

## Which day a settlement lands on

`PnlDaily` is rolled at the settlement's **settle date**, not the date we
noticed it. In normal operation the sweep runs every two minutes so these are
the same; after downtime they are not, and a settlement discovered late lands
on a past day and will not trip today's limit.

That is deliberate — the money moved when the market settled — but it has a
consequence worth knowing: **the losing streak uses `Settlement.created_at`
instead**, because "were the last three closes losers?" is a question about
what we have seen, not about when the exchange settled.

---

## Schema changes

No migrations, as before. This milestone added:

```sql
ALTER TABLE fills          ADD COLUMN realized_pnl_cents numeric(20,6) DEFAULT 0;
ALTER TABLE pnl_daily      ADD COLUMN settlements        integer       DEFAULT 0;
ALTER TABLE proposed_trades ADD COLUMN max_loss_cents    numeric(20,6);
-- plus the new `settlements` table, which create_all makes on boot
```

`Fill.realized_pnl_cents` exists because a daily aggregate cannot answer
"were the last N closes losers?". `ProposedTrade.max_loss_cents` was
previously computed for the per-market guard and thrown away; the risk layer
has to evaluate the number the operator was actually shown on the card,
because a re-derivation prices against a book that has moved since.

---

## Verification

```
575 tests passed
ruff check app/                     clean
mypy app/core app/config.py app/settings.py   clean
5 compose services healthy
```

Browser-driven, no console errors. Screenshots: `m5-risk.png`,
`m5-risk-halted.png`, `m5-settlements.png`, `m5-badge.png`.

All synthetic test data (streak settlements, fabricated positions, forced
`pnl_daily` rows) was removed afterwards; the book is back to the genuine
$6.36 of demo exposure and $0.21 of fees from M3/M4 verification.

---

## Open items

- **Detectors still ship disabled**, and no detector has yet signalled on a
  genuine edge. M5 changes what happens *after* a signal, not whether one
  exists.
- **Unrealised P&L is not in the exposure figure**, by design (see above),
  but `PnlDaily.unrealized_pnl_cents` is still never written. Nothing reads
  it yet either.
- **Web Push needs the TLS decision** before it can be built.
- **`max_pending_proposals` is global, not per route.** With one route active
  at a time this is not yet wrong, but it will be if that changes.

# M4 — detectors, wave 1 (in progress)

Set arbitrage is built and running against live books. The other two
detectors in this milestone (resolution sniper, BTC stale-quote) are not
started.

---

## The finding that shaped the design

Kalshi's `mutually_exclusive` flag means:

> "If true, only one market in this event can resolve to 'yes'."

**At most one — not exactly one.** It guarantees exclusivity and says nothing
about exhaustiveness. An earlier comment in `models.py` claimed it meant
"exhaustive"; it does not, and the live catalog makes that obvious:

| event | legs | Σ bids | Σ asks |
|---|---|---|---|
| KXNEWPOPE-70 | 7 | 0.75 | **4.12** |
| KXGOVAK-26 | 8 | 0.72 | 1.73 |
| NFL games | 2 | ~1.00 | ~1.01 |

Seven pope candidates whose asks sum to $4.12 are plainly not every possible
pope.

That asymmetry decides what may be signalled:

- **Sell every leg** — collect `Σ bids`, pay out at most $1 because at most
  one leg wins. **Riskless under exclusivity alone.** Enabled by default.
- **Buy every leg** — pay `Σ asks`, receive $1 *only if some leg wins*. That
  needs exhaustiveness. **Off unless the operator declares the series
  exhaustive** in `detectors.set_arbitrage.exhaustive_series`, which ships
  empty.

Each entry in that list is a claim about the world that the exchange never
made. Getting it wrong turns "riskless arb" into an uncovered short of every
outcome nobody listed.

---

## What landed

| Piece | Where |
|-------|-------|
| Set-arb math (pure, testable) | `app/detectors/set_arbitrage.py` |
| Detector protocol + signal recording | `app/detectors/base.py` |
| The live detector | `app/detectors/runner.py` |
| Worker scan loop | `app/worker/main.py` |
| Watchlist (65 markets, 22 events) | `config.yaml` |

Detectors emit **signals**. The worker turns a multi-leg finding into a
single *pending* proposal, which is the documented flow:

```
detectors -> signals -> risk/sizing -> proposed_trades -> human -> orders
```

Nothing in that chain places an order. A proposal is a request for a
decision; it reaches an exchange only after a human approves that specific
one, and the interlocks are re-checked at that point rather than trusted
from whatever created it.

---

## Verified live

The detector scans all 22 watched mutually-exclusive events, walks each leg's
real book, and prices both directions net of per-leg taker fees.

**It validated its own design on the first run.** `KXF1CONSTRUCTORS-26`:

| source | Σ bids | verdict |
|---|---|---|
| cached `markets.yes_bid` across 11 legs | **1.05** | looks like a 5¢ arb |
| live book walk | **0.985**, depth **1 contract** | no arb |

A screen built on cached top-of-book quotes would have fired a false signal.
Walking the book for the size actually being traded is the difference.

All 22 events priced negative; zero signals emitted, which is the correct
answer. Real arbs on liquid two-sided books are rare, and the fee model now
being accurate to the centicent makes the bar honest rather than pessimistic.

Other properties confirmed by test:

- A set that cannot fill **every** leg at the requested size prices as
  `None`. A partial set is not an arb; it is a directional position nobody
  chose.
- The thinnest leg caps the set size.
- Walking deeper erodes the edge — size is not free.
- Fees are charged per leg *and* per price level, because that is per fill.
- A gross edge of half a cent per set goes net-negative on two legs of taker
  fee near the money.

---

## Multi-leg proposals

A set arb is one decision that needs several orders, so a proposal now
carries **legs**. `proposal_legs` holds ticker/side/action/price/size; the
`proposed_trades` row holds only the aggregate economics and the decision
state. A manual ticket has one leg; a set arb has one per market. They are
approved together or not at all.

**Execution is not atomic, and nothing can make it so.** Kalshi's batch
endpoint returns a separate result per order and promises nothing about
all-or-nothing — it is a rate-limit convenience, not a transaction. So the
design does what is actually available:

- all legs go out in **one batch request**, collapsing the window between
  them from N round trips to one;
- multi-leg proposals force **IOC**, so no leg can rest half-done;
- a partial outcome becomes `ProposalStatus.PARTIAL` with an explicit
  `UNBALANCED: n of m legs executed` reason, because that is a directional
  position nobody chose and it needs a person.

Leg risk is reduced. It is not removed, and the UI says so on the
confirmation step rather than implying a guarantee that does not exist.

**Verified on the demo exchange — and it hit the unbalanced case on the
first live run.** A 2-leg NFL set went out as one IOC batch; `SEA` filled
10/10 at 0.47, `DAL` did not fill at all. The proposal came back `partial`
with the unbalanced reason, both order rows persisted, and the unfilled leg
was recorded `canceled` (IOC kills it at the exchange) rather than left as a
phantom working order.

---

## Bugs this surfaced

Four, all found by running it rather than by tests:

1. **The batch path built its own order body** and omitted
   `self_trade_prevention_type`, which the API requires — so every multi-leg
   order was rejected while single orders worked. Both paths now go through
   one `build_order_body`, so they cannot drift again.
2. **A failed placement was rolled back.** The executor deliberately writes
   Order rows *before* placing, so an ambiguous failure leaves a client order
   ID to reconcile against. The API's session dependency rolls back on
   exception and erased exactly that — a rejected batch left zero order rows
   and a proposal still marked pending, as if nothing had been attempted.
   Failures are now committed before being surfaced.
3. **A new enum label 500'd at runtime.** `create_all` never alters an
   existing Postgres enum, so adding `PARTIAL` to `proposal_status` failed on
   first *write*, mid-trade, rather than at boot. `bootstrap` now syncs
   missing enum labels at start-up and logs each one. Not a substitute for
   migrations — it closes the gap that bites hardest while there are none.
4. **An unfilled IOC order was recorded as `resting`.** IOC means the
   exchange killed it on arrival; it is not on any book. The dashboard showed
   a working order that did not exist and the auto-cancel sweep would have
   gone looking for a ghost.

## Not started in M4

- **Resolution sniper.** Needs settlement-source monitoring.
- **BTC stale-quote.** Needs an external spot feed (`bitcoin.spot_source`);
  the full BTC engine is M6, but stale-quote needs at least spot.
- **Signals in the UI.** They are recorded and published to
  `copilot:signals`, but nothing renders them yet.

---

## To run it

Set arbitrage ships **disabled**, like every detector.

```yaml
detectors:
  set_arbitrage:
    enabled: true
```

then `docker compose restart worker`. It scans every 20 seconds, which is a
REST book fetch per leg — 65 requests per scan on the current watchlist.

The watchlist is the top 22 mutually-exclusive events by 24h volume where
every leg is quoted, 65 markets total, capped at 100 by
`MAX_FULL_DEPTH_MARKETS`. Regenerate it if the events settle: the detector
only considers events where **every** active leg is in the watchlist, because
a set priced from partial coverage is not a set.

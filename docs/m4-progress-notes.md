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

Detectors emit **signals only**. Nothing in the worker can create a proposal,
let alone an order — that separation is the architecture:

```
detectors -> signals -> risk/sizing -> proposed_trades -> human -> orders
```

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

## The open design question

**A set arb is one decision but many orders.** The proposal and order schema
is single-leg: one ticker, one side, one price. Approving 3 legs of a 5-leg
arb leaves an uncovered position in those 3 — the exact opposite of riskless.

So the detector deliberately stops at signals. Turning a set-arb signal into
something executable needs one of:

1. **Multi-leg proposals** — a proposal that carries N legs and is approved
   as a unit, with the executor placing all N or none. This is the honest
   model and it is a real schema and executor change.
2. **Manual execution** — the operator reads the signal and places each leg
   through the existing ticket, accepting leg risk themselves.
3. **Nothing** — treat set arb as a research feed only.

I have not chosen. (1) is the right answer if set arb is meant to trade, and
it is a decision about the shape of the approval queue, so it needs your
call before I build it.

---

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

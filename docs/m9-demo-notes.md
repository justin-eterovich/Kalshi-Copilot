# M9 — backtester, report card, hardening

Status: **done**. 1,559 tests pass, `ruff check app/` clean, `mypy` clean on
the strict set, five services healthy, dashboard driven in a browser with no
console errors.

The milestone is three things: a backtester that replays stored book
snapshots, a per-detector report card over real trading history, and a
hardening sweep. The first two are built to refuse, and on this deployment
both of them do.

---

## The finding that shaped the milestone

Before writing anything, I measured what there actually is to backtest.

| what | reality |
|---|---|
| markets in catalog | 217,290 |
| markets with a settled result | 63,476 |
| **orderbook snapshots** | **1,938 rows, 79 tickers, 9.8-hour span** |
| candles | 8,650 rows, 34 tickers |
| tape prints | 23,577 |
| **fills ever executed** | **2** |
| settlements recorded | **0** |
| calibration observations with an outcome | 94 (floor is 500) |

There is no historical orderbook endpoint at Kalshi. The only book data that
will ever exist for a past moment is the snapshot ingest happened to take —
so the backtestable universe is ten hours across 79 markets, and the trading
history is two fills.

That is not a reason to skip the milestone; it is the specification for it.
**A backtester that runs on this data and prints a Sharpe ratio is worse than
no backtester**, because the number is what gets remembered and the caveat is
not. So the deliverable is a correct engine that says *no*, in specifics, and
becomes useful with no code change as data accumulates.

---

## What was built

```
app/backtest/
  stats.py      expectancy + bootstrap CI, Brier decomposition, drawdown, verdict
  replay.py     pure event replay; look-ahead blocked structurally
  coverage.py   the gate: measures coverage and refuses with numbers
  engine.py     the only part that runs SQL
  report.py     per-detector report card over real fills and settlements
app/core/statistics.py    wilson_interval, promoted from longshot_calibration
scripts/backtest.py       CLI
frontend/src/ReportCard.tsx
GET /api/report-card
```

Four Opus subagents built `stats`, `replay` and `coverage` in parallel and ran
the hardening audit; I wired the engine, the report card, the API and the UI.

### Running it today

```
$ python scripts/backtest.py --days 30

window requested: 2026-06-27T15:57Z .. 2026-07-27T15:57Z (30 days)
window observed:  2026-07-27T05:35Z .. 2026-07-27T15:23Z (9.8 hours, 1.36% of requested)
markets: 79 (7 settled, 72 open, 18 series, largest 30.4% of sample)
observations: 1873 total, 23.7 per market
gaps: median 84.8 minutes, worst 30 days, tolerated 5 minutes

REFUSAL too_few_markets: 79 markets in the window, against a floor of 200. A
  backtest over this few markets measures those markets — their liquidity,
  their series, their resolution luck — not a strategy.
REFUSAL window_too_short: Data spans 9.8 hours, against a floor of 30 days.
  Note this is the observed span, not the 30 days requested.
REFUSAL too_few_settled: 7 of 79 markets have a known outcome, against a floor
  of 100. Outcomes are the only ground truth a backtest has; the other 72
  contribute a cost basis and no result.
REFUSAL gaps_too_large: The typical market is sampled every 84.8 minutes
  (median), against a tolerance of 5 minutes for an expected interval of 1
  second. A replay across a hole assumes the book did not move.

VERDICT: refused (4 refusal(s))
Replay not run: coverage refused.
```

Every refusal names the measured number against the threshold, because
"insufficient data" without "79 against a floor of 200" does not tell an
operator whether to wait a week or change the config.

`--ignore-coverage` runs it anyway, which is legitimate while writing a
strategy. The refusals stay attached to the result:

```
observations replayed: 1938
intents: 15, filled: 15
settlements: 3, unsettled at end: 12
realised P&L: -145.0000c (fees 31.7200c)
max drawdown: 176.7200c

3 trades is too few to evaluate — insufficient evidence; the -48.33c/trade
mean is not reportable at this sample size.
```

---

## Three decisions that make the numbers worse on purpose

**The unit of observation is a decision, not a fill.** A five-leg set
arbitrage settles as five rows. Counting those as five trades inflates `n`
fivefold and narrows the confidence interval by √5 — on five perfectly
correlated outcomes that are one thesis. Since the interval is exactly what
decides whether a detector gets real money, that is the most dangerous
arithmetic available here. Realising events are grouped by the proposal that
caused them and summed.

**Ambiguous attribution is dropped, not split.** A settlement realises against
a *position*, and a position is keyed by `(ticker, route)` — it does not
remember which proposal built it. When two detectors have traded the same
market on the same route there is no defensible way to divide the outcome, so
it counts for neither and the dropped count is shown.

**Routes are never merged.** A simulated fill and a demo-exchange fill are
different evidence; only one of them happened at an exchange.

---

## Where the agents pushed back, and were right

**The percentile bootstrap is not generally wider than the normal interval.**
I asserted it would be, and told the agent to pin it as a test. It swept 175
longshot samples and found the bootstrap *narrower* in 128 of them: skew
shifts both endpoints together rather than stretching the interval, and the
residual width difference is dominated by the n vs n−1 divisor. It pinned the
property that actually holds — **asymmetry** — and documented the sweep so
nobody reads the one sample where "wider" is true as a law.

The bootstrap is still the right default, and the reason is sharper than the
one I gave. On a sample of 29 wins at +9.98c and one loss at −90.02c:

| | interval | verdict |
|---|---|---|
| Wald | `[+0.11, +13.18]` | **edge shown** |
| bootstrap | `[−0.02, +9.98]` | no edge shown |

Wald sees thirty numbers with a positive mean and claims an edge. The
bootstrap sees that one draw in thirty is worth −90c and refuses. On another
sample Wald's lower bound was −10.94c when the worst average that can
physically occur is −10.02c — an interval extending into outcomes of
probability zero.

**A tri-state field called `settled` is a trap.** I specified
`settled: bool | None`. The agent renamed it `resolved_outcome` and added
`is_settled`, because `if m.settled:` silently drops every NO-resolved market
— halving the outcome sample and biasing what remains toward YES. That is the
same shape as the fee-schedule guard that split on `"categories:"`.

**Purity vs. the single-source rule.** `trading/direction.py` imports `Side`
from `app.db.models`, so it pulls in SQLAlchemy and cannot be imported by a
pure module. CLAUDE.md says never to inline that mapping anywhere. The agent
mirrored the three formulas locally *and* wrote differential tests that import
the SQLAlchemy-bound originals and diff every case against the copies. If one
drifts the suite fails — which is the only mechanism that would notice, since
`P(1−P)` is symmetric and an inverted direction produces identical fees.

---

## Bugs found in my own code while building this

**The equity curve must start at zero.** `max_drawdown` takes the first point
as the opening peak, so a curve built only from cumulative P&L *after each
trade* hides the first trade's loss entirely. A detector whose opening trade
lost 24c reported no drawdown at all. The error only ever flatters, and it
hides precisely the trade you would want to see. Found by the stats agent in
its own docstring work; my `build_report` had it.

**`if order_id:` treats order 0 as absent.** Caught by a test that numbered
its fixtures from zero. Postgres identities start at 1 so it could not bite in
production — which is exactly what would have let it survive to somewhere it
could.

**The shipped backtest strategy re-bought settled markets.** It checked "am I
flat in this market?", but after settlement the position is gone from
`positions`, so a later snapshot would buy a market whose answer was already
public. `replay` raised `LookAheadError` rather than producing a number — the
guard doing its job — but only because this dataset happens to have no
observation after any settlement. Fixed to "once ever", not "once while flat".

---

## Hardening

A read-only audit agent traced nine categories. It confirmed seven defects and
verified four categories clean.

### The canonical incident had recurred, larger

`worker/calibration.py` ran an **unprojected `select(Market)` with no cap**,
matching **152,263 rows carrying 201 MB of JSONB**, every 300 seconds. The
query that killed the worker with no traceback in M6 was 122,887 rows. This
one is 24% bigger, and `_calibration_loop` starts unconditionally — its own
docstring says it runs whether or not the detector is enabled.

Fixed by projecting four columns, pushing the price band and an anti-join for
already-seen markets into SQL, and capping. Measured against the live
database:

| | rows | payload |
|---|---|---|
| before | 152,263 | 201 MB of JSONB |
| after | **97** | 4 scalar columns |

The band filter is deliberately widened by a cent on each side, because the
authoritative midpoint is computed in Python with `Decimal` rounding and the
SQL filter is not — a SQL band narrower than the Python one would silently
drop boundary observations, and the loss would be invisible.

### The one that explains the missing data

Chasing "why does the backtester only have 79 markets and an 85-minute
sampling gap?" led to a bug that had been degrading book ingestion since M1.

`OrderBook.apply_delta` marked a book stale whenever the incoming `seq` was
not exactly its previous `seq + 1`. **But `seq` counts the subscription, not
the market.** One `orderbook_delta` subscription covers every ticker in it and
numbers all of their messages from one counter, so a single market's deltas
arrive with holes wherever another market was updated. Probed against the live
demo stream with 65 markets subscribed:

```
KXNFLGAME-26SEP09NESEA-SEA   seqs = 86, 87, 88, 89, 90, 92, 95, 97
KXNFLGAME-26AUG13INDNE-NE    seqs = 91, 94, 96, 99, 102, 104, 107, 110
```

Every one of those holes was read as a gap. The result: **21 of 65 books went
stale within sixty seconds** — and never recovered, because nothing ever
drained `resync_needed` (below). Meanwhile the *per-subscription* tracker in
`kalshi/ws.py`, which compares the counter the number actually belongs to,
logged **zero** gaps over the same period. That asymmetry is what proved it.

It was silent because a stale book does not raise — `_maybe_record_book`
simply returns early. So the symptom was not an error but thin data, showing
up three milestones later as a backtester with nothing to replay.

There is also no per-market sequence to switch to: the delta body carries only
`market_ticker, market_id, price_dollars, delta_fp, side, ts, ts_ms`. Gap
detection belongs entirely to the per-sid tracker, which already emits
`__resync__`. The book now records `seq` for observability, refuses a
*replayed* (lower) seq because applying a delta twice would double-count it,
and judges nothing else.

Six tests in `test_orderbook.py` were pinning the wrong semantics and were
rewritten. That is worth noting: the tests were green throughout, because they
encoded the same misunderstanding as the code.

Measured before and after, on the same watchlist of 65 markets:

| | stream counters | snapshots written per minute |
|---|---|---|
| before | `gaps=1799 stale=21` after 60s; `gaps=3965 stale=21` after 120s | **65, once** — one per market at the initial snapshot, then nothing, because every book went stale on its first delta |
| after | `gaps=0 stale=0`, sustained across 11,950 book messages / six consecutive minutes | 855 snapshots over six minutes — **143/min** across the 25 markets actually trading |

The "65 then nothing" pattern is the whole bug in one line: the ingest was
recording each market exactly once per process restart, which is precisely
the shape of the sparse data the coverage audit refused on.

### Stale books never recovered

`resync_needed` was only ever appended to. Nothing read it — despite the
module docstring claiming the consumer "requests a fresh snapshot". A book
that went stale stayed stale for the life of the process.

Added `_book_heal_loop`: when ten or more books are waiting, force a
reconnect, since Kalshi sends a fresh snapshot on subscribe and the reconnect
path is already exercised on every disconnect. A per-subscription resubscribe
command would have been a guess at a protocol not verified against the docs.

### The rest

| finding | fix |
|---|---|
| `catalog._known_tickers` loaded **217,258 tickers** into a Python set every 300s | deleted; the upsert now returns `(xmax = 0)` so Postgres reports which rows were inserted |
| four detector queries selected whole ORM entities with no cap | projected + `MAX_DETECTOR_ROWS`; stale_quote was the worst — bounded only by an operator-editable config value sitting in front of 78,616 Crypto markets |
| set-arbitrage scanned **64,286 rows every 20s**, then filtered by watchlist in Python | narrowing subquery pushes the watchlist into SQL |
| `TradeTicket.tsx` computed `(1 - Number(price)).toFixed(4)` and fed it to `limit_price` | API now derives the NO side exactly in `Decimal`; the frontend derivation is gone. `toFixed(4)` also truncated the two extra decimals the API carries |
| an unrecognised orderbook `side` fell through to the **NO book**, advanced the sequence, and left `stale` False | refuses and marks stale; the gap machinery cannot catch this because no sequence is skipped |
| `/trading/state` counted by materialising rows; `/positions` was an N+1 | `select(func.count())` and one `IN` query |
| `backfill_categories` docstring claimed fees are keyed by category | corrected — that design is what excluded ~50,000 Crypto markets |

**Verified clean, by reading rather than assuming:** the live-trading
interlocks (no path to an order that skips any of the three), write-retry
behaviour, fee math centralisation, the order-direction mapping, float-money
on the backend, the fee-schedule guard, silent-failure paths, and async
correctness.

---

## Open items for the operator

- **The backtester will refuse until there is far more data.** Three of the
  four refusals clear with time alone. `too_few_settled` needs markets in the
  watchlist to actually resolve, and this deployment has recorded **zero**
  settlements.
- **Existing book history predates the sequence fix and is unrepresentative.**
  Everything in `orderbook_snaps` before 2026-07-27 16:24 UTC is roughly one
  row per market per restart. Any backtest window spanning that boundary is
  measuring two different ingest behaviours; the coverage audit will refuse on
  the gaps regardless, but do not read the refusal as a statement about the
  data collected since.
- **The watchlist is stale and is now the binding constraint.** 65 markets,
  heavy on NFL preseason and F1 constructors, and only ~21 of them trade in a
  given minute. CLAUDE.md already flags regenerating it from the top
  mutually-exclusive events by 24h volume; with book recording fixed, that is
  the next thing standing between here and a backtest that can run.
- **The report card says `insufficient_evidence` for everything**, correctly.
  `set_arbitrage` on the demo exchange shows 2 trades and −20.88c, which is
  entirely fees: both fills were sells that realised nothing, and a thesis
  that paid to build a position it never closed has lost money.
- **`report_card_min_trades: 20` is a floor, not a target.** Twenty closed
  decisions is where the arithmetic starts being reportable, not where it
  becomes convincing.
- The backtester replays **orderbook snapshots only**. Candles are stored and
  could drive a lower-fidelity replay over a wider set of markets, but a
  candle has no depth, so fills would need an assumed spread — a number that
  would have to be stated rather than buried. Not built.
- `scripts/backtest.py` ships one strategy, a longshot test. Wiring the live
  detectors into replay is not currently possible: they query the database per
  scan rather than taking an observation, so each would need a pure core split
  out the way the fee and direction modules already are.

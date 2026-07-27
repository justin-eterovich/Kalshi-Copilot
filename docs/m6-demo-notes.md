# M6 — BTC volatility engine and detectors wave 2

Status: **done**. Verified on the live stack against the Kalshi demo
environment on 2026-07-27.

M6 replaces the stale-quote detector's placeholder with an actual volatility
model, and adds three research detectors plus one that cannot be built.

---

## The bug this milestone found

**The stale-quote detector was pricing Ethereum and Solana contracts against
the Bitcoin spot price.** It selected markets by `category == "Crypto"` and
compared every one of them to `BTC-USD`.

An ETH contract with a $1,969 strike, held up against Bitcoin at $65,154,
resolves "decisively YES". The model priced it at $0.99 against a market
genuinely quoted at $0.26 and reported a **+72¢ edge**.

Nothing downstream could have caught it. Every number involved was
arithmetically correct — a valid strike, a valid spot, a valid lognormal, a
correctly-computed fee. They simply described two different assets.

It reached a live run and recorded 25 phantom signals with edges of 13–72¢
before it was caught. It stayed contained only because single-market findings
never become proposals (`propose_finding` requires ≥2 legs), so no human was
ever shown an approval card for one.

This was a **latent M4 bug**, not an M6 regression: the same code shipped in
wave 1. It had never fired because `bitcoin.enabled` was false, so there was
no spot price at all and the detector refused for lack of a reference. M6
turned the feed on and the bug became live on the first scan.

### The fix

`category` is not an underlying. "Crypto" contains at least BTC, ETH, SOL and
XRP, and the only thing that says which asset a market tracks is its **series
ticker**. `reference_symbol_for()` maps series prefix → reference symbol and
returns `None` for anything it cannot name; the detector holds one fresh
reference *per underlying* and skips any market whose feed is missing or
stale.

Measured on the live catalog immediately after the fix:

| underlying | markets in window | outcome |
|---|---:|---|
| BTC-USD | 377 | priced |
| ETH-USD | 601 | refused — no feed |
| SOL-USD | 601 | refused — no feed |
| XRP-USD | 151 | refused — no feed |
| unidentifiable series | 409 | refused |

**1,762 markets** were being priced against the wrong asset. Seven regression
tests in `test_detectors_wave1.py` pin the behaviour, including that matching
is on the series segment and not a substring — so a date or strike containing
"BTC" cannot pull a market into the wrong feed.

After the fix the detector emits **nothing**: the BTC markets it can price
show no edge past the 2¢ threshold, which is the expected answer on liquid
two-sided books.

---

## The volatility model

`app/btc/vol.py` — driftless lognormal, EWMA volatility, square-root-of-time
scaling. `f = P(S_T past strike)`, priced per strike type and clamped strictly
inside `[0.01, 0.99]`.

Three choices worth recording:

- **Zero drift, deliberately.** Over the minutes-to-hours horizon these
  markets trade on, any drift estimate is far smaller than the noise around
  it, and a drift term is a free parameter that would let the model talk
  itself into a directional view it has no basis for.
- **`greater` and `greater_or_equal` are identical here** — under a continuous
  distribution `P(S_T = K) = 0`. This deliberately *contrasts* with
  `resolve_strike` in `stale_quote.py`, where the same strike types are
  compared against a single observed spot and strictness genuinely decides the
  outcome. Both are correct; someone will otherwise "fix" one to match.
- **Square-root-of-time assumes i.i.d. returns and Bitcoin violates that.**
  Volatility clusters, so a calm-window estimate understates the risk of a
  violent one. Named in the docstring rather than hidden.

### Sampling is the part that matters

The live poller writes every 3 seconds — ~28,800 rows a day. An EWMA at
`lambda = 0.94` has an effective memory of about `1/(1-lambda)` ≈ 17
observations, so feeding it raw ticks measures volatility over the last *fifty
seconds*, dominated by quote noise. The number would look like a volatility
and behave like a random variable.

`app/btc/history.py` therefore buckets to **one observation per minute**,
taking the last row in each bucket. That also makes backfilled candles and
live ticks interchangeable. Gaps are left as gaps — interpolating would invent
returns of exactly zero across an outage, dragging the estimate down precisely
when the feed broke, and a feed breaking during a violent move is not the
unlikely case.

### Cold start

A freshly-deployed detector had no return series and would have refused
correctly but uselessly for a day. `fetch_minute_candles()` backfills 24h of
one-minute closes from Coinbase's public candle endpoint (300-bucket cap, so
several windowed requests). Recorded under source `coinbase_1m` so a
backfilled close is never mistaken for a live tick — they are not the same
observation, and the freshness checks that gate every detector must not treat
the former as the latter.

### Live output

1,439 minute buckets fetched, BTC $64,486 → $65,116:

```
per-minute sigma  0.0314%
   5m -> 0.070%      30m -> 0.172%
  15m -> 0.122%      60m -> 0.243%
1440m -> 1.191%   (~23% annualised — a calm day, and it was one)
```

Spot-checked against real markets: a 15-minute contract with spot $42 above
its strike and 6 minutes left priced at **0.7956** — spot sits 0.83σ above the
strike, and `Φ(0.83) ≈ 0.797`. Far strikes clamp to 0.01/0.99 as intended.

One honest disagreement worth recording: that market was quoted **0.934/0.984**
against the model's 0.796. Either the EWMA overstates current vol or there is
an edge; on a liquid 15-minute BTC market the market is the more likely to be
right. This is exactly why confidence is capped at 0.75, why the edge
threshold gates it, and why a human approves.

---

## Detectors wave 2

All three ship **disabled**, all emit `net_edge_cents = 0`, and none can ever
produce a proposal (single-market findings do not propose).

### `undervalued_screener` — a reading list

An illiquid, wide-spread market is **not** mispriced — it is *untraded*. The
wide spread is simultaneously why it is cheap to look at and why it is
expensive to trade, since you must cross it to take a position. Any edge
reported here would be manufactured from the screener's own selection
criterion, so the result type structurally has no edge, fair-value or EV
field, and a test asserts those names are absent so nothing downstream can
start reading one.

Live: flagged `KXINXU-...` strikes quoted 1c/97c, 0 contracts traded in 24h.
Correctly identified as no-book rather than cheap.

### `whale_flow` — flow is an input, never an instruction

A large trade is not information about value. It is information that somebody
with a different opinion — or a different *need*: hedging, unwinding, a margin
call — transacted, and the counterparty may be the informed side. Confidence
is hard-capped by `base_confidence` and no amount of size can raise it.

**A real API detail caught here:** Kalshi's tape reports `taker_side` as
`"yes"`/`"no"`, never `"buy"`/`"sell"`. Verified against 9,604 live tape rows
(7,756 `yes`, 1,848 `no`). A module written to the obvious vocabulary would
have found zero sweeps forever and looked like a working detector with nothing
to report.

### `longshot_calibration` — measurement, not a trading rule

Wilson score intervals, not the normal approximation, and that is the whole
reason the module is shaped this way: the interesting buckets are precisely
the extremes (1–10¢, 90–99¢) where the Wald interval is badly wrong and can
extend outside `[0, 1]` — confident nonsense exactly where the strategy wants
to act.

Two things it explicitly does not claim: **calibration is not profitability**
(a 5¢ contract must win more than 5% of the time to cover the fee), and the
observations come from watched markets, so it **measures the watchlist, not
Kalshi**.

`CalibrationLog` is now `UNIQUE (ticker)` — **one row per market, ever**. That
constraint is the statistical basis of the table: a market sitting at 5¢ for a
week, sampled every minute, would contribute ten thousand rows that are all
the same fact, and the 500-sample floor would be met by a few dozen markets
impersonating a thousand. Resampling adds rows, not information.

The collector runs whether or not the detector is enabled — the screen refuses
below 500 settled samples, and a detector that only starts collecting when
switched on would be useless for months afterwards. Live: **692 observations
recorded on the first pass**, 0 settled yet.

### `leaderboard_watcher` — cannot be built

Kalshi's OpenAPI spec exposes **no leaderboard, no public trader ranking, and
no public profile surface of any kind**. The only endpoints naming a
counterparty are RFQ and block-trade negotiation, which are yours alone.
Checked against `docs.kalshi.com/openapi.yaml` on 2026-07-27.

The only way to build it is to scrape the web app, which this project does
not do. It is registered as a detector that logs a clear refusal when enabled
rather than being silently absent — a detector missing from the registry looks
identical to one that is running and finding nothing, which is the worse
failure.

---

## Schema changes

```sql
ALTER TABLE calibration_log ADD CONSTRAINT uq_calibration_ticker UNIQUE (ticker);
```

---

## Verification

```
822 tests passed
ruff check app/                                clean
mypy app/core app/config.py app/settings.py    clean
5 compose services healthy
```

Live run with `bitcoin.enabled` and four detectors on: no scan errors, the
screener and calibration collector both produced real output, the vol model
priced 377 BTC markets and refused 1,762 non-BTC ones. Config restored to
all-disabled afterwards, and a test asserts that.

**Note on rebuilds:** `api`, `worker` and `ingest` are separate images from
the same Dockerfile. `docker compose build api` alone leaves the worker
running old code — which happened twice during this milestone and made the
cross-asset fix look like it had not worked. Build all three.

---

## Follow-up: the signal duplicate guard (and an OOM it exposed)

Signals now fold instead of appending. Within `detectors.dedupe_window_sec`
(900s) an identical observation bumps `seen_count` and `last_seen_at` on the
existing row rather than writing a new one — unless the net edge has moved by
`dedupe_edge_change_cents` (1.0), which makes it a genuinely new observation.

An edge going from 1¢ to 8¢ must never disappear into a counter, which is the
whole reason the threshold exists rather than a plain "same detector, same
ticker" match. A *shrinking* edge is equally news: it means the opportunity
is gone.

Measured live: **80 sightings folded into 23 rows** over five scans. (23 not
20 because the screener's top-20 shifts as quotes move — new entrants
correctly get their own rows.) The UI shows `3×`, `4×` in a `seen` column,
which is strictly more informative than the rows it replaces: a persisting
edge now looks different from one that flickered once.

`record()` returns the folded row, so proposal creation is unaffected — the
proposal links to the observation that actually started, and the queue is
still governed by its own duplicate guard.

### The OOM this uncovered

Enabling the screener for the test killed the worker outright — no traceback,
no log line, just a process that died and restarted and died again. The M6
wiring had `select(Market)` with no column projection, no horizon filter in
SQL, and no cap. The catalog had grown to **122,887 active markets with
two-sided quotes**, each row carrying the full API payload in a `raw` JSONB
column.

It had worked an hour earlier at a smaller catalog size, which is what makes
it worth recording: an unbounded query is a landmine that arms itself as the
data grows, not a slow path. Fixed with six projected columns, the horizon
filter pushed into SQL, and `MAX_SCREENER_ROWS = 20_000`. Same scan now
completes in **0.93s**.

## Open items
- **Only BTC has a spot feed.** ETH, SOL and XRP markets are correctly
  refused, but that is 1,353 markets the detector can see and cannot price.
  Adding feeds is small work — `SPOT_SOURCES` is a dict.
- **No detector has still signalled on a genuine edge**, which remains the
  expected answer on liquid two-sided books.
- **Vol lookback vs. horizon is untuned.** `vol_lookback_minutes: 1440` sets
  how much data is available; `ewma_lambda: 0.94` means the estimate is
  effectively a ~17-minute window regardless. Whether that is right for a
  6-minute market is an open empirical question and the 14¢ disagreement above
  is the first evidence about it.
- **`use_deribit_implied` is still unimplemented.** Implied vol from an
  options surface would replace the EWMA estimate with a forward-looking one,
  which is the natural next upgrade.

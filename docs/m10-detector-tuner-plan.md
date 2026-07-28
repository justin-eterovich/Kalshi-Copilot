# M10 — LLM detector tuner (plan)

Send yesterday's detector picks and their current marks to Opus 5; get back a
proposed change to the detector thresholds; a human applies it.

Status: **M10a is built** (`app/tuning/`, `scripts/tune_detectors.py`) — harvest,
mark, gate, dossier, no LLM. M10b and M10c are still plan only. Read the refusal
gate section (§6) before estimating value: on today's data this refuses on day
one, the same way the backtester does and for the same reason.

---

## 0. The shape of it

```
                T-48h        T-24h                     T (now)
                  |------------|-------------------------|
                  [  harvest   ]      [ let it season ]   ^ mark here
                     picks                                 current quotes
```

Five stages, each a module, each testable alone:

1. **Harvest** — every `Signal` created in `[T-48h, T-24h)`, folded rows included.
2. **Mark** — for each pick, what it would be worth *now*, at an exit price you
   could actually get, net of fees. Settled picks marked at settlement instead.
3. **Package** — one JSON dossier: per-detector config, funnel counts, per-pick
   rows, refusal counts. Prices as strings.
4. **Ask** — one Opus 5 call, structured output, strict schema.
5. **Propose** — the reply becomes a `TuningProposal` row and a card in the UI.
   A human approves it. Nothing else applies it.

The window is `[T-48h, T-24h)` rather than `[T-24h, T)` because a pick needs
time to be wrong. Twenty-four hours of seasoning is the minimum that makes the
mark mean anything, and it is still short — see §6.

---

## 1. The scoring problem is the whole feature

The LLM call is the easy part. What is hard, and what decides whether this
helps or hurts, is turning "the detector said buy YES at 26c" into a number
that means "and it was right/wrong by this much."

### 1.1 Mark at the exit, never the mid

A pick is scored against **the price you would get out at**: the bid on the
traded side for a long, the ask for a short, walked through
`app/trading/pricing.py` so the fee comes from `core/fees.py` and the slippage
buffer from `costs.slippage_buffer_cents`. Marking at the midpoint invents the
spread as profit, and these detectors trade wide books — a mid-based tuner
would reward exactly the picks that cannot be exited.

This is not a theoretical concern here. From the live report card, stale_quote's
realised P&L is **−46.21c on 13 trades, against 46.21c of fees paid** — the
entire loss, to the cent, is fees. The strategy's gross was flat and the costs
ate it. Any marking scheme that is not fee-aware would score those 13 trades as
roughly zero and conclude the detector is fine.

### 1.2 Three outcome classes, never summed

| Class | Source | How it scores |
|---|---|---|
| **Settled** | `Settlement`, or `Market.result` for the simulated book | Realised. The only hard evidence. |
| **Open** | current `Market` quotes | Provisional mark-to-market. Labelled as such everywhere. |
| **Unmarkable** | closed/void market, one-sided book, no quote | **Refused.** Counted by reason, excluded from every aggregate. |

A pick that cannot be marked must not silently become a zero. That is the
`return None` failure this codebase already has a section about — a refusal
that disappears looks identical to a finding of no edge.

Settled and open picks are reported side by side with separate `n`, never
averaged together. They are different grades of evidence and mixing them lets a
handful of settled outcomes hide behind a hundred soft marks.

### 1.3 The unit is the pick, not the fill

Reuse the law from `backtest/report.py`: **group by the decision.** A multi-leg
set-arb finding is one observation however many legs it has. A `Signal` folded
across 47 scans (`seen_count`) is one observation, not 47. Where a pick became
a proposal and then fills, attribution follows `report.py`'s rules exactly,
including dropping ambiguous attribution rather than apportioning it.

Practically: `report.py` already computes most of this for the executed subset.
The new work is scoring the **unexecuted** picks — the ones that never became a
proposal, which is most of them (109 stale_quote signals → 20 proposals). Those
are hypothetical and must be labelled `hypothetical: true` in the dossier so the
model cannot conflate a paper mark with a fill.

### 1.4 The number that matters

Per detector: **claimed edge vs realised.** Live, right now:

```
stale_quote / demo_exchange
  avg_claimed_edge_cents   24.5618
  mean_pnl_cents           -3.5546     ci [-5.99, -2.01] (bootstrap)
  verdict                  insufficient_evidence
```

A detector claiming +24.56c and delivering −3.55c is the signal the tuner
exists to act on. That gap — not the raw P&L — is the primary column in the
dossier.

---

## 2. What the model may change: the knob registry

A new `app/tuning/knobs.py`, pure and stdlib-only, listing every tunable key:

```python
Knob(
    path="detectors.stale_quote.min_net_edge_cents",
    kind=NUMERIC,
    lo=Decimal("0"),          # floor. Negative is the plumbing hack, not a setting.
    hi=Decimal("50"),
    max_step_pct=Decimal("0.25"),
    direction=TIGHTENING_IS_UP,
)
```

**Only listed keys are tunable.** A key the model invents, or one that exists in
`config.yaml` but not in the registry, is rejected outright — not clamped, not
ignored. `DetectorConfig` has `extra="allow"`, so pydantic will happily accept a
hallucinated key and silently do nothing with it; the registry is the only thing
standing between the model and that failure.

### 2.1 Never tunable

Structural, not advisory. These are absent from the registry and a test asserts
each one is absent:

- `trading.mode`, `trading.paper_uses_demo_exchange`, everything under
  `trading.order`
- everything under `risk.*` — bankroll, Kelly fraction, every cap,
  `kill_switch`
- `costs.*` — a tuner that can lower the assumed slippage can manufacture edge
- `backtest.report_card_min_trades`, and any coverage threshold
- `detectors.set_arbitrage.exhaustive_series` — each entry is a **claim about
  the world**, not a tunable. An LLM adding a series here is asserting that some
  leg is certain to resolve YES, which the API never tells anyone.
- `detectors.dedupe_*` — these decide what the tuner's own input looks like
- `notifications.*`, `ingest.*`, `news.headlines.daily_budget_usd`

### 2.2 Bounds are one-sided where loosening is dangerous

Every knob's `lo`/`hi` is set so that no legal value weakens a refusal:

- `min_net_edge_cents` floors at `0`. The negative value used to force set-arb
  to propose was a deliberate one-off to exercise the rail; it is not a setting
  a tuner may reach.
- `reference_max_age_sec` caps at its current value and may only go **down**.
  Raising it lets stale_quote price a live market against an old spot — the
  exact failure it hunts, pointed backwards.
- `min_samples_before_signalling` floors at its current value and may only go
  **up**. Same for `weather.min_calibration_samples`.
- `size_hint`, `max_results`, `lookback_minutes` are bounded both ways.

### 2.3 Step caps and cooldown

- At most **±25% of current value** per knob per run (per-knob override in the
  registry).
- At most **3 knobs** changed per run.
- A knob touched in the last **7 days** is frozen.
- One run per day, gated in the worker.

A daily tuner over a 24h window with no step cap is a random walk that ratchets
thresholds toward whatever yesterday looked like. The caps make the walk slow
enough that the journal (§5) can tell whether it is going anywhere.

---

## 3. The Opus 5 call

`app/tuning/llm.py`. Reuses `app/news/budget.py` verbatim — it is pure, has no
I/O, already models refuse-before-the-call, and already has a `NO_API_KEY`
refusal, which is this deployment's normal state (`settings.anthropic_api_key`
is empty; the `anthropic` package is not yet in `pyproject.toml` — adding it is
part of this milestone).

```python
resp = client.messages.create(
    model="claude-opus-5",                      # $5 / $25 per MTok
    max_tokens=16000,
    output_config={
        "effort": "high",
        "format": {"type": "json_schema", "schema": TUNING_SCHEMA},
    },
    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
    messages=[{"role": "user", "content": dossier_json}],
)
```

Notes that matter:

- **Structured outputs, not prose.** `output_config.format` with a strict
  `json_schema` (`additionalProperties: false`, every field `required`). The
  reply is then validated *again* against the knob registry — schema
  conformance is not authorisation.
- **Thinking is on by default on Opus 5.** `max_tokens` caps thinking + text
  together, so 16000 is sized for both. Do not pass `budget_tokens` (400) or
  `temperature`/`top_p` (400).
- **Cache the system prompt.** The knob registry and the rules are stable across
  runs; the dossier is not, so it goes last. Opus 5's minimum cacheable prefix
  is 512 tokens, which the registry alone clears.
- **Handle `stop_reason == "refusal"` before reading `content`.** Opus 5's
  classifiers can decline; an unconditional `content[0]` breaks. A refusal here
  is logged and the run ends with no proposal — it is not retried.
- **One call. No tools, no network, no multi-turn.** The model reads a dossier
  and returns a diff. It never touches the database or the exchange.
- **Cap the dossier.** Order picks by `|claimed_edge|` then recency, cap the
  rows, and log when the cap binds (`_warn_if_capped` style). A silent
  truncation would hand the model a biased sample of its own evidence.

### 3.1 The reply is untrusted data

Market titles, rationales, and (later) headlines flow into this prompt, and
they are text other people wrote. The output changes what the system proposes
to trade. So: parse to schema, check against the registry, clamp to bounds,
reject anything unrecognised, never `eval`, never let a returned string name a
config path that was not already in the registry.

---

## 4. Applying a change

**Tuning is a proposal, not an action.** Same law as trading: the LLM's output
reaches config only through an explicit human approval. Concretely:

- The run writes a `TuningProposal` row (new table: run id, window, dossier
  digest, per-knob `from`/`to`/`reason`, model id, token usage, status).
- The dashboard shows it as a diff card. Approve / reject, one click, logged to
  `AuditLog`.
- On approve, the change is written to **`config_kv`** — the table that already
  exists in `db/models.py` (`ConfigKV`, "UI-editable overrides; takes precedence
  over config.yaml") and is currently read by nothing. Wiring it is part of this
  milestone.
- `config.yaml` is bind-mounted **read-only**; nothing writes to it, ever.
- `get_config()` is `@lru_cache(maxsize=1)`, so an override needs a reload path:
  a `config_kv` version counter checked on each worker loop tick, invalidating
  the cache when it moves. This is the fiddliest part of the milestone and the
  place to expect the bug.

If wiring `config_kv` proves too large, the fallback is: emit the diff as a YAML
patch in the proposal card, operator edits `config.yaml` and restarts. Less
good, much smaller, and preserves every safety property.

---

## 5. Does the tuning help? (the part everyone skips)

Two mechanisms, both cheap:

**Journal.** Every applied change is recorded with the window it was derived
from. The next run's dossier includes the last 14 days of applied changes and
what happened to the detector's claimed-vs-realised gap since. If the gap is not
closing, the tuner is noise and the operator can see that rather than infer it.

**Counterfactual replay.** Before showing a proposal, re-run the proposed
thresholds over the *same* dossier and report how many of the window's picks
they would have removed, and what the claimed-vs-realised gap would have been on
what remained. Pure filtering over data already in memory — no SQL, no model
call. It catches the common failure where a plausible-sounding change does
nothing at all, and the less common one where it removes every pick.

The counterfactual is **in-sample by construction** and must be labelled that
way on the card. It is a sanity check, not evidence.

---

## 6. The refusal gate — this feature's `coverage.py`

Measured on this deployment today (2026-07-28, via `/api/signals`):

```
signals table, entire history      187 rows
oldest signal                      2026-07-27T10:02Z
newest signal                      2026-07-28T06:00Z
span                               ~20 hours
by detector    stale_quote 109 · undervalued_screener 67 · resolution_sniper 6
               whale_flow 3 · longshot_calibration 2 · set_arbitrage 0
```

**The window `[T-48h, T-24h)` contains zero signals right now.** The whole
table is younger than 48 hours. Run this feature today and it has nothing to
send.

Even once it does, the sample is thin in a way that matters: 109 stale_quote
signals produced **13 scoreable trades**, and the report card's own verdict on
those is `insufficient_evidence` against a floor of 20. A tuner adjusting
thresholds on a dozen noisy marks is a coin flip with a $25/MTok bill attached.

So the gate, modelled on `backtest/coverage.py` — measured number against
threshold, never "insufficient data":

| Floor | Why |
|---|---|
| ≥ 30 picks in window, per detector | below this, one pick moves every average |
| ≥ 20 **markable** picks | unmarkable picks are the silent killer |
| ≥ 10 distinct markets | 40 picks on one event is n=1 |
| ≥ 5 distinct events | same, one level up |
| ≥ 1 settled outcome | otherwise every number is provisional |

Below any floor: build the dossier, **write it to disk as a report, make no
model call, propose nothing**, and print each measured number against the floor
it missed — so the operator knows whether to wait a week or change the config.

`--ignore-coverage` runs it anyway, keeps the refusals attached to the output,
and marks the proposal `evidence: none`. Same contract as the backtester.

---

## 7. Layout

```
backend/app/tuning/
  window.py     pure: window boundaries, clock is a parameter
  harvest.py    signals in window -> PickRow (the only SQL here + mark.py)
  mark.py       ⭐ exit-price marking, fee-aware; refuses rather than zeroing
  dossier.py    pure: PickRow[] + config -> the JSON payload. Prices as strings.
  knobs.py      ⭐ the registry. Only listed keys are tunable; bounds one-sided.
  coverage.py   ⭐ the gate. Pure, stdlib only, thresholds as parameters.
  llm.py        the single Opus 5 call; budget guard from app/news/budget.py
  apply.py      validate reply -> TuningProposal; approval -> config_kv
  journal.py    applied changes + what happened since
backend/scripts/tune_detectors.py     CLI, mirrors scripts/backtest.py
backend/app/api/routes/tuning.py      GET proposals, POST approve/reject
frontend/src/...                      the diff card
```

New tables: `tuning_runs`, `tuning_proposals`. No migrations exist, so this
means `create_all` on a fresh schema — consistent with the rest of the project
and worth flagging to the operator before the first run.

## 8. Phasing

Milestone by milestone, demo notes at each stop.

- **M10a — dossier and scorer, no LLM. BUILT.** Harvest, mark, coverage gate,
  CLI that prints the report. Immediately useful on its own: it answers "what
  did the detectors claim yesterday and what happened" for the first time.
  Refuses today, by design, and that refusal is the deliverable's first real
  test. One bug found while testing and worth recording: a `size_hint` of
  exactly `0` is falsy, so `contracts or 1` silently scored a
  deliberately-unsized finding as a one-contract trade. It is an `is None`
  check now, and zero is a named refusal — the same tri-state trap as
  `Market.result == ''` reading as settled.
- **M10b — the Opus 5 call and the proposal.** Knob registry, structured
  output, `TuningProposal` rows, counterfactual replay, the UI card. Approval
  emits a YAML patch the operator applies by hand.
- **M10c — `config_kv` apply path.** Wire the override table and the cache
  reload. Only after M10b has produced proposals a human has read and judged.

## 9. Testing

- `window.py`, `dossier.py`, `knobs.py`, `coverage.py` are pure with the clock
  as a parameter — unit-testable with no database, like `news/budget.py`.
- `mark.py`: exit-side selection for all four (side, action) combinations,
  fee-aware round trip, and one test per refusal reason.
- `knobs.py`: for **every** never-tunable key, a test that a reply naming it is
  rejected; for every knob, a test that a value which would loosen a refusal is
  rejected rather than clamped.
- Adversarial LLM replies: unknown key, out-of-range value, negative
  `min_net_edge_cents`, a config path smuggled in a `reason` string, a reply
  that is valid JSON but not the schema.
- Coverage gate: each floor tested at the boundary.
- Then actually run it and look at the output, per CLAUDE.md — build all three
  images, not just `api`.

## 10. Open questions for the operator

1. **The window is a guess.** 24h of seasoning on markets that mostly close
   weeks out means nearly every pick is marked, not settled. A 7-day window
   would carry more settled outcomes at the cost of slower feedback. Worth
   making the window a CLI parameter and deciding after M10a shows real numbers.
2. **No API key on this deployment**, and no `anthropic` dependency. M10a needs
   neither; M10b needs both.
3. **Cost.** One Opus 5 call/day at ~30k input / 4k output is roughly $0.25/day
   ($5/$25 per MTok). Trivial against the bankroll, but it should still sit
   behind `budget.py` so a loop bug cannot spend a month's allowance in an hour.
4. **This tunes thresholds, not theses.** Nothing here can tell stale_quote that
   its 24.56c claimed edge is wrong *because* it is comparing a market to a
   reference it should not trust. It can only make the detector fire less. If
   the claimed-vs-realised gap survives several rounds of tightening, the answer
   is a detector change, not a threshold change — and the journal is what makes
   that visible.

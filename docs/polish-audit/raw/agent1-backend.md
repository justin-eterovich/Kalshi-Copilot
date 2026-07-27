# Agent 1 — Backend correctness auditor (static). Area prefix `BE`.

**Counts: P0 = 0 · P1 = 5 · P2 = 11 · P3 = 9.**

No P0 confirmed. **BE-001 is wrong money math and would be a P0 with
`detectors.stale_quote.enabled: true` + `bitcoin.enabled: true`; both are
false in the shipped `config.yaml`, so it is filed P1 as "wrong, currently
unreachable". Lead may want to promote it.**

Environment: no docker, no browser (per GROUND-RULES). Runtime checks were
done with `curl` against `http://127.0.0.1:8080` and, for two payload-shape
questions, against Kalshi's public **demo** REST (read-only, unauthenticated).
Live catalog at time of audit: 220,244 markets / 149,360 active / 597,542
events / 7,216 active markets with no category.

---

## P1

```
[P1] BE-001 — Barrier ("is ever above X") crypto markets are priced as terminal-value markets; produces a ~96c inverted phantom edge
Repro:    enable detectors.stale_quote + bitcoin.enabled, wait until a KXBTCMAXMON
          market is inside max_minutes_to_close (30).
          curl -s http://127.0.0.1:8080/api/markets/KXBTCMAXMON-BTC-26JUL31-7000000
Expected: A market whose rules read "if the price ... is **ever above** $70000.00
          ... then the market resolves to Yes" is a barrier/touch contract. Once
          the barrier has been touched it is resolved YES regardless of where spot
          sits now, and before it is touched its probability is the running-maximum
          probability (~2x the terminal one under a driftless lognormal).
Actual:   Both the veto and the model read only `strike_type`, which is the plain
          string "greater", and treat it as a terminal comparison against spot now.
          - backend/app/detectors/stale_quote.py:96
                holds = spot > floor_strike if kind == "greater" else spot >= floor_strike
          - backend/app/btc/vol.py:205-207
                d2 = (math.log(s / k) - 0.5 * sigma * sigma) / sigma
                p_above = norm_cdf(d2)
                return p_above if above else 1.0 - p_above
          Worked example with today's live data. Barrier 70000 already touched
          earlier in the month, spot now 65,000, market correctly quoted yes_bid
          0.98:
            resolve_strike -> verdict.yes = False (65000 > 70000 is False)
            margin_pct = |65000-70000|/65000*100 = 7.69%  >=  decisive_margin_pct 1.0
              -> decisive_fair_price veto PASSES (backend/app/detectors/runner.py:432)
            vol fair_price(YES) ~ 0.02
            runner.py:469-472  ->  side = NO, price = 1 - 0.98 = 0.02, fair = 1-0.02 = 0.98
            net edge ~ (0.98 - 0.02) * 100 = +96c, on a contract that has already
            resolved YES. Maximum loss, maximum confidence.
          This is the ETH/BTC cross-asset bug (CLAUDE.md, "+72c edge") in a
          different dimension: the reference *asset* is now right and the
          reference *statistic* is wrong, and nothing downstream catches it.
Evidence: backend/app/detectors/stale_quote.py:77-121 (resolve_strike)
          backend/app/btc/vol.py:239-310 (fair_price / terminal_probability)
          backend/app/detectors/runner.py:413-472
          Live rules text, KXBTCMAXMON-BTC-26JUL31-7000000:
            "If the price of BTC after issuance and through 11:59 PM ET on
             Jul 31, 2026 is ever above $70000.00, then the market resolves to Yes."
            strike_type = "greater", floor_strike = 70000.000000
          Affected active series seen live: KXBTCMAXMON (3), KXBTCMAX100 (1);
          KXBTCMAX150 is listed in data/fee_schedule.yaml.
          Currently unreachable: config.yaml has stale_quote.enabled=false and
          bitcoin.enabled=false, and /api/system reports enabled_detectors=[].
Fix sketch: do what the weather engine does — parse rules_primary and refuse
          anything not recognised as a terminal-value contract; `strike_type` says
          how to compare, never what is being compared.
```

```
[P1] BE-002 — Undervalued screener silently drops 68,110 of 88,110 eligible markets; the percentile it ranks on is computed over an arbitrary subset
Repro:    curl -s "http://127.0.0.1:8080/api/markets?limit=1&status=active&max_hours_to_close=168&max_spread=1.0"
          (the max_spread filter forces both quote sides non-null, matching the
          detector's WHERE clause exactly)
Expected: A cap that binds should say so. The screener's score is a *relative*
          volume percentile, so which rows it got decides every rank it emits.
Actual:   {"total": 88110}. MAX_SCREENER_ROWS = 20_000, and the query has
          **no ORDER BY** and logs nothing when the cap binds. 77% of the eligible
          set is discarded in whatever order Postgres happened to return.
Evidence: backend/app/detectors/runner.py:677-697
                .limit(MAX_SCREENER_ROWS)
          backend/app/detectors/runner.py:62-66 (MAX_SCREENER_ROWS = 20_000)
          config.yaml: undervalued_screener.max_hours_to_close: 168 (shipped default)
          The detector is disabled today, so this is deterministic-on-enable
          rather than currently happening.
Fix sketch: ORDER BY volume_24h ASC (the screener wants the thin tail anyway) and
          log a warning when len(rows) == the cap.
```

```
[P1] BE-003 — "Recovery is reconciliation by client order ID" is documented in three places and does not exist anywhere in the code
Repro:    grep -rn "client_order_id" backend/app/
Expected: README safety table: "No write retries | A timed-out order POST may have
          been accepted, so it raises instead of retrying. Recovery is
          reconciliation by client order ID."  CLAUDE.md: "Recovery is
          reconciliation by client order ID, never a second POST."
          executor.py module docstring, point 3: same claim.
Actual:   `client_order_id` is only ever (a) generated, (b) sent on create, and
          (c) used to match results *within a single batch response*. Nothing ever
          queries the exchange by it. Concretely, an order whose POST timed out:
            1. rest.py raises KalshiApiError(0, ...) on a write timeout — correct.
            2. executor._place_legs marks every still-PENDING order REJECTED
               (executor.py:362-366).
            3. maintenance.sweep_orders only looks at LIVE_ORDER_STATUSES
               (PENDING/RESTING/PARTIALLY_FILLED) — REJECTED is not in it.
            4. reconcile_order returns immediately when exchange_order_id is falsy
               (maintenance.py:167-168) — which is exactly the timed-out case.
          So an order that may be live at Kalshi is recorded as REJECTED and never
          looked at again. `KalshiRestClient.get_orders()` does not even accept a
          client_order_id filter (rest.py:531-549).
Evidence: backend/app/worker/maintenance.py:167
                if client is None or not order.exchange_order_id:
                    return
          backend/app/trading/executor.py:362-375
          backend/app/kalshi/rest.py:531-549
Fix sketch: add a reconciliation pass over REJECTED/PENDING orders with no
          exchange_order_id that scans GET /portfolio/orders for a matching
          client_order_id, or drop the claim from README/CLAUDE.md/executor.
```

```
[P1] BE-004 — Single-leg detector findings can never become proposals; stale_quote and weather do all the Kelly sizing work and it goes nowhere
Repro:    grep -rn "create_proposal\|create_multi_leg_proposal" backend/app/
Expected: README, "BTC stale quote": "Size comes from Kelly rather than a fixed
          size_hint, on the after-fee cost ... the per-market cap and the exposure
          headroom all cut it **before anything is proposed**. The binding cap is
          named in **the proposal's rationale**." That describes a proposal that
          this detector cannot produce.
Actual:   The only detector -> proposal path is `propose_finding`, which returns
          `None` for any finding with fewer than two legs:
            backend/app/detectors/base.py:211-213
                legs = finding.evidence.get("legs") or []
                if len(legs) < 2:
                    return None
          Only SetArbitrageDetector puts "legs" in its evidence. stale_quote,
          weather, resolution_sniper, whale_flow, undervalued_screener and
          longshot_calibration therefore emit signals only, forever. The other
          proposal entry point (`prop.create_proposal`) is called from exactly one
          place, POST /api/proposals, with source="manual".
          The early return is silent — no log, no counter — so from the operator's
          side an enabled stale-quote detector that has found a 5c edge and sized
          it to 12 contracts looks identical to one that found nothing.
Evidence: backend/app/detectors/base.py:193-215
          backend/app/api/routes/trading.py:276 (the only other caller)
          backend/app/detectors/runner.py:494-535 (the sizing that is discarded)
Fix sketch: either route single-leg findings through create_proposal, or say so in
          README and log the drop.
```

```
[P1] BE-005 — UNVERIFIED (needs runtime) — ticker flush builds a multi-row INSERT from heterogeneous dicts and an ON CONFLICT SET over the *union* of their keys
Repro:    (needs docker) docker compose run --rm --no-deps api python - <<'PY'
          from sqlalchemy.dialects.postgresql import insert
          from app.db.models import Market
          chunk = [{"ticker":"A","yes_bid":1,"yes_ask":2},{"ticker":"B","yes_bid":3}]
          stmt = insert(Market).values(chunk)
          cols = {k for r in chunk for k in r if k != "ticker"}
          print(stmt.on_conflict_do_update(index_elements=[Market.ticker],
                set_={c: stmt.excluded[c] for c in cols}).compile(
                dialect=__import__("sqlalchemy.dialects.postgresql",fromlist=["x"]).dialect()))
          PY
Expected: Each ticker update writes only the fields that arrived on the wire.
Actual:   `normalize_ticker` emits a *variable* key set — a field is added only if
          present and parseable (normalize.py:206-220). `_update_tickers` then does
          `insert(Market).values(chunk)` over that heterogeneous list. SQLAlchemy's
          multi-VALUES form takes its column list from the **first** dict; later
          dicts missing a column get the column default (NULL here), and the
          ON CONFLICT set_ is built from the **union** of every row's keys, so
          `SET volume = excluded.volume` can reference a column that was never in
          the INSERT — i.e. NULL — and overwrite a good stored quote.
          The union itself is the tell: computing it only makes sense if the rows
          differ, and if they differ then `.values(chunk)` is the wrong construct.
          There is no test for `_update_tickers` (backend/tests/test_normalize.py
          covers normalize_ticker only).
          Counter-evidence, honestly reported: I sampled 5,000 live active markets
          across five offsets and found **zero** NULLs in yes_bid / yes_ask /
          last_price / volume / volume_24h / open_interest. So either SQLAlchemy
          behaves differently than described, or ticker_v2 payloads happen to be
          homogeneous, or catalog sync repairs it within 300s. I could not settle
          it without SQLAlchemy on the host.
Evidence: backend/app/ingest/streams.py:303-316
                rows = [{"ticker": t, **vals} for t, vals in updates.items()]
                stmt = insert(Market).values(chunk)
                columns = {k for row in chunk for k in row if k != "ticker"}
          backend/app/ingest/normalize.py:200-227
          Blast radius if real: `_stream_loop` subscribes ["ticker"] with **no**
          market_tickers (ingest/main.py:145), i.e. every one of 149,360 active
          markets, so mixed batches are the normal case, and a CompileError in
          `_update_tickers` would roll back the whole flush — tape, candles and
          book snapshots included.
Fix sketch: executemany (`session.execute(stmt, rows)` per-row) or normalise every
          row to the same key set before batching.
```

---

## P2

```
[P2] BE-006 — stale_quote still selects its universe by `category == "Crypto"`; 7,216 active markets have no category at all and are invisible to it
Repro:    curl -s http://127.0.0.1:8080/api/catalog/stats  ->  markets_uncategorised: 7216
Expected: CLAUDE.md: "`category` is NOT an underlying ... Only the **series
          ticker** says what a market tracks." pricing.py:162-166 records the same
          lesson for fees: "unlike the old category lookup, this never depends on
          the Event sync having landed."
Actual:   The lesson was applied to fees and not to the detector's selection.
          backend/app/detectors/runner.py:377
                Market.category == "Crypto",
          `category` is copied onto Market by `backfill_categories` from the parent
          Event, so a newly listed crypto market is invisible until that join runs;
          7,216 active markets are in that state right now. The downstream guard
          (`reference_symbol_for`) is correct and refuses safely, so this is a
          false-negative rather than a mispricing — but it is the same field the
          codebase has already decided must not decide anything.
          Secondary: the filter selects from 74,284 active Crypto markets into a
          20,000-row cap (see BE-007).
Fix sketch: .where(Market.series_ticker.in_(...)) built from REFERENCE_PREFIXES,
          or an explicit series list; drop the category predicate.

[P2] BE-007 — Every MAX_*_ROWS cap truncates with no ORDER BY and no log line
Evidence: runner.py:260, 388, 597, 695, 789, 1034; report.py:287, 516, 543, 572;
          engine.py:159, 242, 288; worker/calibration.py:145, 204.
          Measured today (live): stale_quote at the shipped max_minutes_to_close=30
          matches 2,148 rows — safe. At 6h it is 11,660; at 24h it is 50,162, i.e.
          2.5x the 20,000 cap. `max_minutes_to_close` is an operator-editable
          number in config.yaml with nothing behind it. The comment at
          runner.py:68-77 anticipates exactly this and the cap it added is silent.
Fix sketch: `if len(rows) >= CAP: log.warning(...)` on every one, plus a
          deterministic ORDER BY so a truncated scan is at least reproducible.

[P2] BE-008 — LongshotCalibrationDetector.scan is the one remaining detector query with no .limit()
Evidence: backend/app/detectors/runner.py:870-876
                select(CalibrationLog.price_bucket_cents, CalibrationLog.settled_yes)
                .where(CalibrationLog.settled_yes.isnot(None))
          CalibrationLog is one row per market **ever** and is never pruned; the
          catalog is 220,244 markets and grows. Projected to two small columns so
          it will not kill the worker soon, but it is unbounded by construction and
          runs every DETECTOR_SCAN_SEC (20s) when enabled.
Fix sketch: aggregate in SQL — GROUP BY price_bucket_cents, count(*), count(*)
          FILTER (WHERE settled_yes) — the detector only needs those two numbers.

[P2] BE-009 — backfill_outcomes can starve: head-of-queue rows that will never resolve monopolise every sweep
Evidence: backend/app/worker/calibration.py:199-205
                .where(CalibrationLog.settled_yes.is_(None))
                .order_by(CalibrationLog.id)
                .limit(MAX_BACKFILL)   # 5000
          Rows whose market resolves "void" or "scalar" are deliberately never
          filled in (calibration.py:228-230), so they stay NULL forever at the head
          of the id ordering. Once un-backfilled rows exceed 5,000 the sweep
          re-reads the same permanently-stuck prefix every 300s and newer
          observations are never resolved. 1,020 observations exist today, so it
          has not bitten.
Fix sketch: a terminal marker (settled_at set with settled_yes NULL) or ORDER BY
          random()/observed_at DESC.

[P2] BE-010 — `--mark-verified` validates `formula.rounding_increment_dollars`, which fees.py never reads
Evidence: scripts/refresh_fee_schedule.py:214-216 requires it be set;
          data/fee_schedule.yaml:36 sets it to 0.0001;
          backend/app/core/fees.py:68  CENTICENT: Final = Decimal("0.0001")
          — hardcoded, and FeeSchedule.from_dict (fees.py:159-193) never touches
          the key. An operator who edits it because the PDF changed gets no
          behaviour change and a verification pass. This is the shape of the
          documented `--mark-verified` bug: a guard inspecting a value with no
          effect.
Fix sketch: read the increment into FeeSchedule and use it in
          `_round_up_centicents`, or delete the key and the check.

[P2] BE-011 — The weather detector never appears in `enabled_detectors` or in the worker's boot log
Evidence: backend/app/config.py:104-110  enabled_names() iterates DetectorsConfig
          fields and only counts `DetectorConfig` instances. `weather` is
          `Config.weather` (a WeatherConfig), not under `detectors:`.
          backend/app/worker/main.py:197-201 and app/main.py:112 both print
          enabled_names(); /api/system returns it as `enabled_detectors`.
          So with weather.enabled: true the detector scans and can signal while
          the dashboard says no detectors are enabled — the exact failure the
          leaderboard_watcher stub exists to avoid ("one silently missing from the
          registry looks identical to one that runs and finds nothing").

[P2] BE-012 — Seven config keys are documented in config.yaml and read by nothing
Evidence: grep across backend/app (excluding config.py) returns zero references for
          each:
            detectors.set_arbitrage.require_full_depth   (config.yaml:16, commented
              "only signal if every leg fills at quote"; behaviour is hardcoded)
            detectors.resolution_sniper.min_source_confidence (config.yaml:70)
            backtest.pessimistic_fills                   (config.yaml:363)
            ingest.scanner.max_markets                   (config.py:62; _stream_loop
              subscribes ticker with NO market_tickers, i.e. all 149,360)
            ingest.scanner.series_filter                 (config.py:63)
            notifications.web_push_enabled / audio_enabled / favicon_badge
              (config.yaml:344-360, "Everything below works today"; not read by the
              backend, and `grep -rn` over frontend/src finds no reference either)
            bitcoin.use_deribit_implied                  (config.yaml:288)
          `scanner.max_markets` is the one with teeth: an operator capping the
          scanner at 500 markets still gets the whole exchange streamed.

[P2] BE-013 — `ConfigKV` and the settings-UI write-through it promises do not exist
Evidence: backend/app/db/models.py:822-831 defines the table;
          config.yaml:4-6 states "Most values are editable from the settings UI
          (which writes through to config_kv in Postgres and takes precedence over
          this file)".
          `grep -rn "ConfigKV\|config_kv" backend/app frontend/src` (excluding
          models.py) returns nothing. `get_config()` is `@lru_cache(maxsize=1)` over
          config.yaml alone, so nothing "takes precedence" and nothing is editable
          at runtime.

[P2] BE-014 — /api/fees/quote claims to fail closed on an unverified schedule; the code path cannot raise
Evidence: backend/app/api/routes/health.py:118-135 catches UnverifiedFeeSchedule
          with the comment "Fail closed: excluded rather than priced with a guess."
          But `taker_fee_cents`/`maker_fee_cents` never raise it — only
          `price_ticket` checks `is_verified` (pricing.py:171). So with an
          unverified table the ticket UI happily renders fees while POST
          /api/proposals 409s, which reads as a bug rather than a policy.
Fix sketch: check `sched.is_verified` in the endpoint (or in load_fee_schedule).

[P2] BE-015 — The spot reference is Coinbase last-trade; every crypto market it prices settles on a CF Benchmarks 60-second index average, and nothing says so
Evidence: backend/app/ingest/spot.py:42-46
                "coinbase": ("https://api.coinbase.com/v2/prices/BTC-USD/spot", ...)
          Live demo rules, KXBTC-26JUL2714-B65550: "If the simple average of the
          sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before
          2 PM EDT is between 65500-65599.99 ..."; KXETH15M uses ETHUSDRTI;
          KXBTCMAX100 uses "the CF Bitcoin Real-Time Index".
          README calls the feed "a fresh **independent** spot price", which is
          honest about independence but not about the fact that it is a *different
          statistic from a different publisher* than the settlement source. The
          weather engine refuses markets on exactly this ground (KXTEMPNYCH settles
          on The Weather Company, "so NWS data there is a proxy for a different
          source and those markets are refused"); the crypto path does not apply
          the same rule to itself.

[P2] BE-016 — Screener endpoint uses unprojected `select(Market)`, dragging the full `raw` JSONB payload it never reads
Evidence: backend/app/api/routes/markets.py:188  `stmt = select(Market)`, up to
          limit=1000 rows, plus markets.py:276 (siblings, 60 rows) and
          markets.py:443 (8 rows). `_market_row` uses ~20 scalar columns and never
          touches `raw`. Measured: 0.30s and 660 KB for limit=1000 — bounded, so
          not urgent, but it is the exact pattern CLAUDE.md names, on the endpoint
          the dashboard polls.
```

---

## P3

```
[P3] BE-017 — report.py truncates orders newest-first (Order.id DESC) and fills oldest-first (Fill.ts ASC) at the same MAX_ROWS cap, so at the boundary fills reference orders that were not loaded and silently attribute to nobody. report.py:512-517 vs 528-543.
[P3] BE-018 — FeeSchedule.default_is_safe checks only the taker multiplier (fees.py:203-210) while UnknownSeries' docstring claims generality; the maker dimension is unguarded and listed series already carry maker=1 against a default of 0.
[P3] BE-019 — mark_verified parses the YAML to validate (good) then writes it back by regex (scripts/refresh_fee_schedule.py:252-267), re-introducing the text-slicing the same file's docstring warns about.
[P3] BE-020 — SigmaEstimate.as_of is documented "Consumers must check this themselves" (btc/history.py:61-65); the only consumer (runner.py:437-454) records it in evidence and never checks the age. Unreachable today because a fresh reference implies a fresh minute series from the same table.
[P3] BE-021 — worker/main.py:149 declares `refused` once per scan cycle, shared across detectors, so detector N's summary line reports refusals accumulated by detectors 1..N.
[P3] BE-022 — list_proposals does `await prop.legs_of(...)` inside a comprehension over up to 500 rows — N+1 on an endpoint the dashboard polls (trading.py:338-341). Same shape at trading.py:407 and 433.
[P3] BE-023 — executor._apply_response builds exchange_fill_id as f"{order.exchange_order_id}-immediate" (executor.py:531); when the response omits order_id this is literally "None-immediate", and `uq_fill_id` collides on the second such fill.
[P3] BE-024 — ingest/weather.py:205 `select(WeatherForecast)` unprojected with no .limit(); bounded only by "un-backfilled within lookback_days", which grows for any station whose observations stop.
[P3] BE-025 — ingest/weather.py:219-226 runs one `_observed_extreme` query per pending forecast row (N+1).
```

---

## Verified-clean (checked, no finding)

- **Order direction.** `app/trading/direction.py` is the only place the
  (side, action) -> bid/ask + `1-p` mapping exists. Every wire construction goes
  through `book_side`/`to_yes_price` (executor.py:344, 444; pricing.py:240-241).
  The `Decimal(1) - x` occurrences elsewhere are quote complements (NO price from
  a YES bid) or probability complements, not the wire mapping.
- **Money as float.** No `float()` on a price, fee, count or P&L outside
  documented model internals (`btc/vol.py`, `weather/distribution.py`, both of
  which quantize back to Decimal) and display-only paths. No `round()`, `//` or
  `%` on money. `backtest/stats.py:128-158` actively refuses a float P&L.
- **Fee math single-sourcing.** No `0.07`, `0.0175` or `P*(1-P)` shape outside
  `app/core/fees.py`. `pricing.py`, `paper.py`, `sizing.py` and every detector
  call into it.
- **Per-market `seq`.** `orderbook.py:107` compares `seq` only to reject a replay
  (lower value), which is correct on a single ordered socket; gap detection is
  per-`sid` in `ws.py:247-268` and nowhere else. No other module inspects `seq`
  except `streams.py` (records it) and the read endpoints (display it).
- **Write retries.** `rest.py:201-256` raises on write timeouts and 5xx and only
  retries 429, exactly as documented. No caller wraps a POST in a retry.
- **Tri-state `result`.** `backtest/engine.py:199-211` (`_resolved`) and
  `trading/settlements.py:90-107` (`settled_yes_value`) both normalise
  `'' / None / value` correctly. `CalibrationLog.settled_yes` (`bool | None`) is
  read with `.is_(None)` / `.isnot(None)` at every site — calibration.py:202,
  runner.py:874, trading.py:660 — never truthiness. `Event.mutually_exclusive`
  is read with `.is_(True)` (runner.py:256). No `if m.settled:` anywhere.
- **Interlocks.** `resolve_route` / `check_execution` match the README table
  exactly, including "paper on prod -> simulated" and "live without both env
  interlocks -> refuse". Re-checked inside the executor, not only at the API.
- **Report card arithmetic.** Unit is the proposal (`key = f"proposal:{pid}"`),
  ambiguous settlements are dropped not apportioned, routes are never summed, the
  equity curve is prepended with `Decimal(0)`, and the verdict gates on the
  bootstrap interval. All as documented.
- **Detector refusal rules vs docs.** set_arbitrage, resolution_sniper,
  whale_flow, undervalued_screener, longshot_calibration and weather all
  implement what README claims. Two drifts, both minor: README's BTC section
  still describes `decisive_margin_pct` as the fair-value source ("a fixed
  percentage ... that arrives in M6") when it is now only a veto and the vol
  model is mandatory (runner.py:456-463); and `reference_symbol_for` uses
  `series.startswith(prefix)` (stale_quote.py:172-175) where CLAUDE.md says
  series tickers are names not a namespace — I enumerated every active series
  matching KXBTC/KXETH/KXSOL/KXXRP in the live catalog and all are genuinely
  that asset, so this is currently benign, unlike `station_for_series` which is
  correctly an exact dict lookup.

---

## HAND TO LEAD FOR RUNTIME VERIFICATION

Only checkable with docker; the exact command that settles each.

1. **BE-005** — SQLAlchemy multi-VALUES semantics on heterogeneous dicts.
   ```
   docker compose run --rm --no-deps api python -c "
   from sqlalchemy.dialects import postgresql
   from sqlalchemy.dialects.postgresql import insert
   from app.db.models import Market
   chunk=[{'ticker':'A','yes_bid':1,'yes_ask':2,'volume':9},{'ticker':'B','yes_bid':3}]
   s=insert(Market).values(chunk)
   cols={k for r in chunk for k in r if k!='ticker'}
   print(s.on_conflict_do_update(index_elements=[Market.ticker],
        set_={c:s.excluded[c] for c in cols}).compile(dialect=postgresql.dialect()))"
   ```
   If it compiles and emits `volume = excluded.volume` while row B never supplied
   `volume`, BE-005 is confirmed as a NULL-overwrite. If it raises CompileError,
   BE-005 is confirmed as a whole-flush rollback instead — worse, and would also
   explain part of the "book snapshots written far less often than configured"
   note in CLAUDE.md.

2. **BE-005 blast radius** — whether ticker flushes are failing today.
   ```
   docker compose logs ingest --since 1h | grep -c "flush failed"
   ```

3. **BE-001** — confirm the barrier family is reached once enabled.
   ```
   docker compose run --rm --no-deps api python -c "
   from app.detectors.stale_quote import resolve_strike
   from decimal import Decimal as D
   print(resolve_strike(strike_type='greater', floor_strike=D(70000),
                        cap_strike=None, spot=D(65000)))"
   ```
   Expect `StrikeVerdict(yes=False, margin_pct=7.69...)` — i.e. it prices the
   already-touched barrier as a decisive NO.

4. **BE-002 / BE-007** — whether the caps bind in a real scan.
   ```
   docker compose run --rm --no-deps api python -m pytest tests/ -q   # baseline
   # then enable undervalued_screener in a scratch config and watch:
   docker compose logs worker -f | grep undervalued_screener
   ```
   (`docker compose build` all three images first — CLAUDE.md's warning.)

5. **BE-008 / BE-009** — row counts behind the two calibration queries.
   ```
   docker compose exec db psql -U copilot -d kalshi_copilot -c \
     "select count(*) total, count(*) filter (where settled_yes is null) pending
      from calibration_log;"
   ```
   BE-009 arms when `pending` exceeds MAX_BACKFILL (5,000).

6. **Query cost** for BE-016 / BE-006 — `EXPLAIN ANALYZE` on the screener's
   `select(Market)` with `ORDER BY volume_24h DESC NULLS LAST LIMIT 1000` over
   149,360 active rows (no index on volume_24h). Measured 0.30s end-to-end from
   the host, so this is characterisation rather than a suspected defect.

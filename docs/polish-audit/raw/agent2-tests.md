# Agent 2 — Test & toolchain integrity (area `TEST`)

Static audit at HEAD `1ee5cd3`. No docker on this host, so **nothing was
executed**: no pytest, no ruff, no mypy, no `scripts/backtest.py`. Every
finding below is from reading source, the git history, and an `ast`-built
inventory. Runtime-only claims are marked `UNVERIFIED — needs runtime`.

## Severity counts

| | count |
|---|---|
| **P0** | **0 — no fee/direction/position test asserts a wrong value** |
| P1 | 4 |
| P2 | 7 |
| P3 | 9 |

**No P0.** The specific thing I was sent to look for — a fee test still
pinning the pre-centicent `2¢` — does not exist. `tests/test_fees.py:80`
asserts `Decimal("1.75")`, `:86` asserts the real demo billing `2.24`, `:107`
asserts aggregate (not per-contract) rounding at `Decimal("175")`, `:210`
asserts the maker default of 0, and `:293` asserts fractional counts. The fee
suite encodes the documented behaviour correctly.

---

## 1. Test inventory

40 test modules + `__init__.py` in `backend/tests/`. All match pytest's
default discovery (`test_*.py`); all parse cleanly under Python 3.14 `ast`.

- **1,438** `def test_` / `async def test_` functions — matches the count in
  the brief exactly.
- **~1,557** collected cases after expanding `@pytest.mark.parametrize`
  (one parametrize takes a module-level constant list, `DEFAULT_LEAD_EDGES` in
  `test_weather_calibration.py:88`, and is counted as 1 — the real number is
  the 1,562 the HEAD commit message reports).
- **0** `pytest.mark.skip`, **0** `skipif`, **0** `xfail`, **0**
  `pytest.skip(...)`, **0** module-level `pytestmark` anywhere in
  `backend/`.
- **0** `Test*` classes with an `__init__` (which pytest skips silently), **0**
  `Test*` classes with zero test methods, **0** test functions nested inside
  another function or inside a non-`Test*` class.
- No `conftest.py` anywhere in the repo.
- No test files outside `backend/tests/`.

| file | test fns | parametrize-adjusted |
|---|---:|---:|
| test_api_fees.py | 12 | 16 |
| test_backfill.py | 16 | 18 |
| test_backtest_coverage.py | 81 | 82 |
| test_backtest_engine.py | 9 | 11 |
| test_backtest_replay.py | 63 | 75 |
| test_backtest_stats.py | 77 | 81 |
| test_btc_vol.py | 67 | 78 |
| test_config.py | 16 | 16 |
| test_detectors_wave1.py | 40 | 41 |
| test_direction.py | 14 | 20 |
| test_executor.py | 47 | 47 |
| test_fee_schedule_script.py | 9 | 12 |
| test_fees.py | 41 | 62 |
| test_interlocks.py | 22 | 28 |
| test_longshot_calibration.py | 38 | 49 |
| test_news_budget.py | 60 | 60 |
| test_news_calendar.py | 44 | 44 |
| test_news_feeds.py | 76 | 76 |
| test_news_relevance.py | 48 | 48 |
| test_normalize.py | 23 | 23 |
| test_nws_parse.py | 45 | 45 |
| test_orderbook.py | 30 | 30 |
| test_paper_fills.py | 25 | 25 |
| test_positions.py | 23 | 23 |
| test_proposals.py | 23 | 23 |
| test_ratelimit_and_auth.py | 39 | 39 |
| test_report_card.py | 16 | 18 |
| test_risk.py | 32 | 32 |
| test_set_arbitrage.py | 28 | 28 |
| test_settlements.py | 17 | 18 |
| test_signal_dedupe.py | 13 | 14 |
| test_sizing.py | 26 | 29 |
| test_ticket_pricing.py | 30 | 35 |
| test_undervalued_screener.py | 47 | 50 |
| test_weather_calibration.py | 48 | 59 |
| test_weather_daily_low.py | 12 | 12 |
| test_weather_distribution.py | 65 | 65 |
| test_weather_rules.py | 42 | 42 |
| test_whale_flow.py | 57 | 63 |
| test_ws_hub.py | 17 | 20 |
| **TOTAL** | **1,438** | **~1,557** |

### Modules never referenced by any test import

Resolved with `ast` (handles `from app.trading import proposals as prop`).
22 application modules, 130 KB of source, are never imported by the suite:

```
app.api.routes.trading   31,859   app.ingest.weather      11,148
app.ingest.main          15,609   app.worker.main         10,499
app.ingest.catalog       10,611   app.kalshi.ws           10,647
app.worker.calibration   10,216   app.worker.maintenance   8,338
app.ingest.spot           6,159   app.weather.client       5,826
app.weather.stations      5,739   app.btc.history          5,571
app.ingest.news           5,569   app.db.bootstrap         5,513
app.main                  5,271   app.core.statistics      4,278
app.core.money            4,039   app.news.client          2,879
app.core.logging          2,867   app.kalshi.client        2,142
app.db.base               1,941   app.healthcheck          1,465
```

Four of those are load-bearing enough to be findings in their own right
(TEST-001, TEST-003, TEST-004, TEST-010).

### Pytest / lint / type config

`backend/pyproject.toml` is the only config; no `pytest.ini`, no `setup.cfg`,
no `ruff.toml`, no `mypy.ini`.

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
filterwarnings = ["error::DeprecationWarning:app.*"]
```

`testpaths` and `filterwarnings` hide nothing — the filter *promotes*
`DeprecationWarning` from `app.*` to an error. There is no `addopts`, no
`--strict-config`, no `--strict-markers` (see TEST-005).

---

## 2. Starred modules — is the documented behaviour asserted?

| starred module | verdict |
|---|---|
| `core/fees.py` | **asserted, correctly.** centicent (`1.75`), demo ground truth (`2.24` / `3.36`), aggregate rounding (`175` for 100 @ 50¢), maker default 0, series keying + case-insensitivity, `default_is_safe` fail-closed, cents-style price rejection, fractional counts, `Decimal` return type. Only gap: the int form `taker_fee_cents(56, …)` (TEST-014). |
| `trading/direction.py` | **asserted, correctly.** All four rows of the truth table parametrized at `test_direction.py:30-40`; all four signs of `signed_contracts` at `:82-92`; NO→complement and a lossless YES/NO round-trip for `to_yes_price`/`from_yes_price`. Two nits: TEST-015, TEST-016. |
| `trading/positions.py` | **asserted, correctly.** Signed opens/adds/reduces, partial close keeping the original basis, crossing through flat re-opening at the fill price, NO offsetting YES, fractional precision with a no-drift loop, `position_view` rendering `-10 @ 0.70` back as "10 NO at 30¢". |
| `trading/settlements.py` | **partially asserted.** The pure functions are well covered (payout table, refusal on `void`/unknown/`""`, NO-side sign, hedged-pair identity). The two rules CLAUDE.md flags as traps are **not** asserted — see TEST-006. |
| `kalshi/orderbook.py` | **asserted, correctly, and the six wrong tests were rewritten not deleted.** `git show 1ee5cd3 -- backend/tests/` shows a +80/-33 diff on `test_orderbook.py`: the old `test_sequence_gap_marks_book_stale` etc. were replaced with `test_skipped_seq_is_not_a_gap_for_this_market`, `test_interleaved_markets_leave_both_books_healthy`, `test_a_replayed_seq_is_refused`, and the rest were re-driven through `mark_stale()` instead of a fake gap. Two new tests were added. **No coverage hole in `orderbook.py`** — but the gap detection that moved *out* of it landed in an untested module (TEST-001). |
| `trading/risk.py` | **asserted at the boundary, for the pure functions.** Exposure inclusive-at-limit + one cent past (`test_risk.py:115-127`), daily loss firing exactly at `-5000` (`:178`), fees-count-toward-the-loss and fees-alone-breaching (`:182`, `:198`), scratch-doesn't-break / winner-does-break (`:229`, `:221`), cooldown boundary exclusive (`:298`). **`snapshot()` and `_recent_losses()` are not tested at all** — TEST-007. |
| `trading/sizing.py` | **asserted, correctly.** After-fee Kelly, each of the four caps binding in isolation with the right `binding_constraint`, round-down (`7.999 → 7.99`), sub-tick → 0 not a minimum trade, negative headroom clamped. Missing only a monotonicity property (TEST-019). |
| `backtest/coverage.py` | **asserted, correctly and unusually well.** Every refusal has a fires/does-not-fire-at-the-floor pair plus a `finding.measured` / `finding.threshold` assertion (`:352-358`, `:421-424`, `:725-753`). The real deployment's numbers are pinned as a regression fixture: `"79 markets"` vs `"200 markets"`, `"9.8 hours"` vs `"30 days"`. |
| `backtest/stats.py` | **asserted, correctly**, including the one that matters most: `test_omitting_the_opening_balance_hides_the_first_trade` (`test_backtest_stats.py:763-777`) proves the flattering-drawdown error and pins both answers. Bootstrap-vs-Wald is pinned on the real 29-win/1-loss shape (`:285`), the seed is pinned (`:154`, `:163`), and `report.py`'s four unit rules (legs collapse, routes never merged, contested dropped, losing-first-trade counted) each have a test. **But `engine.py` commits the very error `stats.py` documents — TEST-002.** |
| `weather/distribution.py` | **asserted, correctly.** `0.2611` (discrete) vs `0.1324` (continuous) pinned to 1e-4 at `test_weather_distribution.py:170-171`; `test_greater_than_96_means_at_least_97` at `:259`; buckets shown non-overlapping and gapless at `:193`. |
| `weather/nws_parse.py` | **asserted, correctly.** `test_weather_daily_low.py` covers night→next-day, a truncated overnight, high and low for one day coming from different periods, a daytime period never supplying a low, and `test_a_start_based_rule_would_file_a_pacific_night_a_day_early` (`:195`) — the exact bug, pinned. |
| `weather/stations.py` | **not asserted at all** — TEST-003. |
| `detectors/set_arbitrage.py` | asserted (sell-side edge, buy-side non-exhaustive trap at `:239`, buy side off by default at `:357`, shipped config declares nothing exhaustive at `:370`). |
| `detectors/stale_quote.py` (`reference_symbol_for`) | asserted — BTC/ETH/SOL/XRP each mapped, non-crypto refused, and `KXNFLGAME-26BTC13-CLE` refused (no substring matching). |
| `trading/executor.py` | asserted (38 async tests) but see TEST-005 and TEST-016. |

---

## 3. Backtester refusal quality (by code reading — cannot run it)

Verified. `app/backtest/coverage.py` builds every refusal through
`_finding(code, severity, message, measured=…, threshold=…)` and every call
site supplies both:

```python
# coverage.py:759-768
_finding(
    RefusalCode.TOO_FEW_MARKETS, Severity.REFUSAL,
    f"{market_count} markets in the window, against a floor of "
    f"{min_markets}. A backtest over this few markets measures "
    f"those markets — their liquidity, their series, their "
    f"resolution luck — not a strategy.",
    measured=f"{market_count} markets",
    threshold=f"{min_markets} markets",
)
```

Same shape for `WINDOW_TOO_SHORT` (`:770-784`, observed span vs requested,
explicitly distinguished), `TOO_FEW_SETTLED` (`:786-799`), `SURVIVORSHIP`
(`:801-820`, `threshold="at least 1 unsettled market"`), `SINGLE_SERIES`
(`:823-837`), `GAPS_TOO_LARGE` (`:857-878`, median gap vs a tolerance derived
from the expected interval), plus warnings.

The only occurrence of the string "insufficient" in the refusal path is the
**header** of `CoverageRefused`, immediately followed by the itemised list:

```python
# coverage.py:589-593
detail = "\n".join(f"  - {f.message}" for f in report.refusals)
super().__init__(
    f"Coverage is insufficient for a backtest "
    f"({len(report.refusals)} refusal(s)):\n{detail}"
)
```

That is a count-and-detail header, not a bare "insufficient data". **No
regression.**

`--ignore-coverage` (`scripts/backtest.py:114`, `engine.py:363-395`) keeps the
refusals on the result (`engine.py:445` re-renders them into the JSON) and
`scripts/backtest.py` returns exit code `2` whenever `result.coverage.usable`
is false, ignore flag or not. As documented.

---

## 4. Suppression sweep

**0 bare `except:`** in `backend/` or `scripts/`.

| suppression | count | judgement |
|---|---:|---|
| `# noqa: BLE001` | 25 | inert (see TEST-008); each has a written reason and each is a "never kill the loop" or "must not leak key bytes" case — all legitimate false-positive suppressions *if the rule were on* |
| `# noqa: DTZ001` | 2 (tests) | inert; deliberate naive datetimes that are the point of the test |
| `# noqa: E731`, `F401`, `E402` | 5 | real rules, real false positives, fine |
| `# type: ignore[...]` | 29 | 25 in tests, all deliberate "pass the wrong type on purpose" cases proving a guard raises; 4 in `app/` |
| `# pragma: no cover` | 5 | 3 unreachable-by-design, 1 image-dependent, 1 `__repr__` that must never render key material — all fine |

App-side `type: ignore` worth naming:

- `app/detectors/runner.py:141` `direction=direction,  # type: ignore[arg-type]`
  — `str` → `Literal["buy","sell"]` narrowing over a list literally built from
  `["sell"]`/`["buy"]`. Known false positive. Fine.
- `app/ingest/news.py:105` `# type: ignore[attr-defined]` on
  `h.published_at >= cutoff` — hides that `dedupe()` returns an
  imprecisely-typed sequence. Real but low-stakes (P3, TEST-021 below is not
  filed separately; noted here).
- `app/core/fees.py:82` broad `except Exception` — see TEST-018.

`warn_unused_ignores = true` is set globally, so a stale `type: ignore` would
fail mypy. Good.

---

## 5. Findings

```
[P1] TEST-001 — app/kalshi/ws.py, now the ONLY orderbook gap detector, has zero tests
Repro:    ast scan of backend/tests/ for imports of `app.kalshi.ws` — no hits.
          `grep -rn "kalshi.ws\|kalshi import ws" backend/tests/` returns nothing.
Expected: CLAUDE.md, and the HEAD commit message, say gap detection "belongs
          entirely to the per-sid tracker in ws.py" after orderbook.py stopped
          judging seq. A guard that protects every book should be the
          best-tested code in the repo.
Actual:   `_check_seq` (ws.py:247-266), the per-sid `_SidState` table, the
          `__resync__` synthesis (ws.py:143, :237, :279) and the `self._sids.clear()`
          on reconnect (ws.py:139) are covered by nothing. `test_ws_hub.py` is
          about `app/api/ws.py`, the browser relay — a different module.
          The bug just fixed was *silent* for four milestones precisely because
          a mis-judged seq does not raise; the replacement judge has no test.
Evidence: backend/app/kalshi/ws.py:247-266; backend/tests/ (no importer);
          backend/tests/test_ws_hub.py:14 imports app.api.ws, not app.kalshi.ws
Fix sketch: unit-test `_check_seq` directly — first seq accepted, +1 accepted,
          a skip returns a reason naming sid/expected/got, a replay (<= last)
          returns a reason, and two sids do not contaminate each other.
```

```
[P1] TEST-002 — the backtester's max-drawdown curve has no opening point: the
     documented "only ever flatters" error, live in engine.py
Repro:    read app/backtest/engine.py:101 against app/backtest/replay.py:830-841
          and backend/tests/test_backtest_replay.py:864-874.
Expected: CLAUDE.md: "An equity curve for max_drawdown must start at zero,
          before the first trade. Otherwise the first trade's result *is* the
          opening peak and an opening loss reports no drawdown at all — an
          error that only ever flatters."  stats.max_drawdown's own docstring
          says "Prepend the opening equity (Decimal(0) for a P&L curve)."
Actual:   `replay()` appends exactly one equity point per observation, after
          the fills at that instant — there is no pre-trade point.
          `test_the_curve_starts_from_the_supplied_equity` (replay tests:850)
          and `test_fees_move_the_curve_down_on_the_tick_they_are_charged`
          (:863) both pin that shape; the latter asserts the curve for a run
          that paid 1.75c of fees is `[-1.75, -1.75]`.
          engine.py then does:
              f"{stats.max_drawdown([e for _, e in r.equity_curve])}c"
          max_drawdown([-1.75, -1.75]) == 0.  max_drawdown([0, -1.75, -1.75])
          == 1.75.  The backtest summary reports "max drawdown: 0c" for a run
          that drew down 1.75c.
          app/backtest/report.py:611-620 gets this right, with a five-line
          comment explaining why — so the codebase knows, and engine.py is the
          call site that missed it.
Evidence: backend/app/backtest/engine.py:101
          backend/app/backtest/replay.py:830-841 (one point per observation)
          backend/tests/test_backtest_replay.py:863-874 (asserts [-1.75,-1.75])
          backend/tests/test_backtest_stats.py:763-777 (asserts the error class)
          backend/app/backtest/report.py:611-620 (the correct call site)
          Arithmetic reproduced with stdlib Decimal: 0 vs 1.75.
Fix sketch: `stats.max_drawdown([r.starting_equity_cents, *(e for _, e in
          r.equity_curve)])`, and add an engine test for a first-observation loss.
Note:     graded P1 rather than P0 because it is a research statistic in a tool
          that refuses on this deployment's data. It is P0-shaped arithmetic.
```

```
[P1] TEST-003 — app/weather/stations.py (⭐) is entirely untested; the
     KXLOW/KXSNOWFLAKE prefix trap has no regression test
Repro:    ast scan — no test imports `app.weather.stations`. There is no
          tests/test_weather_stations.py.
Expected: CLAUDE.md: "Series tickers are names, not a namespace. KXLOW is
          Lowe's Companies Inc. and KXSNOWFLAKE is Snowflake Inc. A prefix
          match would route earnings markets to a weather station.
          `station_for_series` is an exact dict lookup and must stay one."
          "must stay one" is a claim only a test can keep.
Actual:   `station_for_series` is correct today (`SERIES_STATIONS.get(...)`,
          stations.py:~236) but nothing asserts it. Nothing asserts the
          21 rows are well-formed, that an unlisted series returns None, that
          `KXLOW`/`KXSNOWFLAKE` return None, or that the KXTEMPNYCH hourly
          family is excluded. The table is consumed by
          detectors/runner.py:1027 (`Market.series_ticker.in_(SERIES_STATIONS)`)
          and ingest/weather.py:58, so a bad row silently prices a whole
          series off the wrong thermometer.
Evidence: backend/app/weather/stations.py (whole file, 5,739 bytes, 0 tests)
          backend/app/detectors/runner.py:1027, :1050
          backend/app/ingest/weather.py:58
Fix sketch: a ~10-line test file: exact lookup, case/whitespace normalisation,
          None for KXLOW / KXSNOWFLAKE / "" / None, and every station_id
          matching ^K[A-Z]{3}$.
```

```
[P1] TEST-004 — secret redaction (hard constraint #6) is untested
Repro:    ast scan — no test imports `app.core.logging`.
Expected: CLAUDE.md constraint 6: "The RSA key is mounted read-only and never
          logged." README: "the log formatter strips anything key-shaped on
          its way out."  That is a security invariant enforced by five
          regexes and nothing else.
Actual:   `_REDACTIONS` (logging.py:11-22) covers PEM blocks,
          `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-KEY` and `sk-ant-…`, with
          no test that any of them fires, no test that a key spanning multiple
          log records is caught, and no test that the redaction survives
          `%`-style lazy formatting (the codebase logs with `log.info("...%s", x)`,
          so the substitution happens after the formatter sees the template).
Evidence: backend/app/core/logging.py:11-28; backend/tests/ (no importer)
Fix sketch: capture a record through the real formatter for each pattern and
          assert the secret substring is absent from the output.
```

```
[P2] TEST-005 — nothing stops 62 async tests (38 of them on the executor) from
     silently skipping
Repro:    read backend/pyproject.toml [tool.pytest.ini_options]; ast-count
          `async def test_*`.
Expected: "the suite is green" should mean the suite ran.
Actual:   `asyncio_mode = "auto"` is a pytest-asyncio ini key. If the plugin is
          absent or its ini schema changes, the key is an *unknown* option that
          pytest ignores by default, and every async test is collected and then
          skipped with a warning rather than failing. There are 62 of them:
          test_executor.py 38 (⭐ the only module that can cause an order to
          exist), test_api_fees.py 12, test_proposals.py 12. There is no
          `addopts`, no `--strict-config`, no `--strict-markers`, and the
          `filterwarnings` entry only escalates DeprecationWarning from `app.*`.
          This is the same failure family as the stale-image trap: a green run
          that covered less than it looks like.
Evidence: backend/pyproject.toml [tool.pytest.ini_options] (no addopts)
          38/12/12 async test split confirmed by ast
UNVERIFIED — needs runtime: `docker compose run --rm --no-deps api python -m
          pytest tests/ -q -p no:asyncio` would demonstrate the skip count.
Fix sketch: `addopts = "--strict-config --strict-markers"` — `--strict-config`
          turns an unrecognised `asyncio_mode` into a hard error.
```

```
[P2] TEST-006 — the settlement mixed-units trap and the "our own avg_price"
     rule are not asserted anywhere
Repro:    read backend/tests/test_settlements.py (17 tests) against
          backend/app/trading/settlements.py.
Expected: CLAUDE.md names `GET /portfolio/settlements` as "the only endpoint
          that mixes units inside one object" and says settlement P&L is
          computed "against our own avg_price, not the exchange's cost basis";
          also "The simulated book settles from the market's own result;
          exchange books settle from the endpoint above. Never cross them."
Actual:   The suite tests only `settled_yes_value` and
          `realized_from_settlement`. Not tested:
          - `_fee_cents` (settlements.py:332-336) — `fee_cost` is a fixed-point
            *dollar string* multiplied by 100; nothing pins that `"0.0224"`
            becomes `2.24` cents rather than `0.0224` or `224`.
          - that `yes_total_cost_dollars` / `no_total_cost_dollars` are ignored
            in favour of `Position.avg_price` — `apply_settlement`
            (settlements.py:124-213) is untested end to end.
          - `sync_exchange_settlements` raising on the SIMULATED route
            (settlements.py:230-231) — the "never cross the two books" guard.
          - `apply_settlement` idempotency (returns None if a Settlement row
            already exists) and the "flat position writes nothing" branch.
          The half that *is* tested is the right half of the units trap
          (`value_cents=63 → Decimal("0.63")`, test_settlements.py:36-39).
Evidence: backend/app/trading/settlements.py:230-231, :332-336, :124-213
          backend/tests/test_settlements.py (imports only two functions, line 15)
Fix sketch: three unit tests on `_fee_cents`, one `pytest.raises(ValueError)`
          on the simulated-route refusal, and one fake-session test of
          `apply_settlement` asserting the payload's own cost basis is unread.
```

```
[P2] TEST-007 — nothing asserts that a SETTLEMENT feeds the loss-cooldown streak
Repro:    read backend/tests/test_risk.py imports (line 19-26) — only
          RiskError, RiskState, check_exposure, check_halted,
          consecutive_losses, position_cost_cents.
Expected: README safety table: "A 'close' is a reducing fill **or a
          settlement**". CLAUDE.md: until M5 "the system booked the cost of
          every held-to-resolution thesis and none of its proceeds", and the
          risk module's own docstring says the limits were blind before
          settlement ingestion existed.
Actual:   `consecutive_losses()` — the pure counter — is tested six ways.
          `_recent_losses()` (risk.py:299-339), which is the function that
          UNIONs Fill rows with Settlement rows, sorts them by time and feeds
          that counter, is not tested at all. Neither is `snapshot()`. So the
          exact regression the module was built to prevent — settlements
          dropping out of the streak, leaving the cooldown reading only
          traded-out closes — would not be caught. Same for the
          `STREAK_WINDOW = 50` cap and the merge ordering between the two
          sources.
Evidence: backend/app/trading/risk.py:299-339 (untested)
          backend/tests/test_risk.py:19-26 (import list)
Fix sketch: a fake-session test returning 2 fills and 2 settlements with
          interleaved timestamps, asserting the streak counts across both and
          that a settlement *win* breaks a streak of fill losses.
```

```
[P2] TEST-008 — 27 noqa comments suppress ruff rules that are not enabled;
     blind-except and naive-datetime linting is off
Repro:    compare `grep -rn "# noqa" backend/ scripts/` against
          `[tool.ruff.lint] select = ["E","F","I","UP","B","SIM"]`.
Expected: a suppression exists because a rule fires.
Actual:   `BLE` (flake8-blind-except) and `DTZ` (flake8-datetimez) are **not**
          in `select`, so all 25 `# noqa: BLE001` and both `# noqa: DTZ001`
          comments are inert. The codebase reads as if blind-except linting is
          on — the comments are careful and reasoned — and it is not, so a new
          `except Exception:` with no justification passes lint silently.
          Naive-datetime linting is likewise off in a codebase whose backtest
          coverage module has a dedicated `_require_utc` guard because "for a
          span measured in hours, [tz] is the difference between passing and
          failing" (coverage.py:326-336).
          `RUF100` (unused-noqa), which would have surfaced this, is also not
          selected. Nothing in ruff's rule set catches float-vs-Decimal money;
          that gap is inherent, not a config error.
Evidence: backend/pyproject.toml [tool.ruff.lint] select
          backend/app/kalshi/ws.py:153, app/worker/main.py:82/95/115/133/181,
          app/ingest/main.py:65/106/246/302/337, app/api/ws.py:124/168/185,
          app/trading/executor.py:362/653, … (25 total)
          backend/tests/test_backtest_replay.py:205,218 (DTZ001)
Fix sketch: add "BLE","DTZ","RUF" to select and re-run; the existing noqas
          then do their job and the remaining hits are the real ones.
```

```
[P2] TEST-009 — mypy is not `strict` on the three paths the docs call strict
Repro:    read backend/pyproject.toml [tool.mypy] and the overrides block.
Expected: CLAUDE.md Style: "`mypy` strict on `app/core`, `app/config.py`,
          `app/settings.py`." README repeats it.
Actual:   The override for `["app.core.*", "app.config", "app.settings"]` sets
          four flags: disallow_untyped_defs, disallow_incomplete_defs,
          strict_equality, warn_return_any. `--strict` implies roughly eleven.
          Not set on the covered paths: disallow_untyped_calls,
          disallow_any_generics, disallow_subclassing_any,
          disallow_untyped_decorators, no_implicit_reexport, check_untyped_defs,
          extra_checks.  disallow_any_generics is the notable one — a bare
          `dict`/`list` annotation in fees.py or config.py passes today.
          Nothing in the covered paths is excluded and there is no
          `ignore_errors` or `follow_imports = skip` anywhere, so the coverage
          is genuine as far as it goes; it is the word "strict" that is wrong.
          `warn_unused_ignores = true` is set globally — good.
Evidence: backend/pyproject.toml, [tool.mypy] and [[tool.mypy.overrides]]
Fix sketch: replace the four flags with `strict = true` on that override, or
          amend both docs to say what is actually enforced.
```

```
[P2] TEST-010 — app/api/routes/trading.py (31 KB, the HTTP approval surface)
     has no tests
Repro:    ast scan — no test imports `app.api.routes.trading`.
Expected: this is the module that turns an HTTP body into an
          `approve_and_execute(confirmed=…, confirmation_phrase=…)` call — the
          outermost layer of interlock #2.
Actual:   `app/trading/interlocks.py` is well tested (including
          `confirmation_phrase_mismatch` at test_interlocks.py:266/296) and
          `executor.py` has 38 async tests, so the *guards* are covered. But
          nothing asserts the route defaults (`confirm: bool = False` at
          trading.py:132) or that the route actually forwards
          `body.confirmation_phrase` (trading.py:372) rather than a constant.
          A default flipped to `True` in that Pydantic model would be caught by
          no test in the repo.
Evidence: backend/app/api/routes/trading.py:130-135, :358-372 (0 tests)
Fix sketch: one httpx/ASGI test per route asserting the default is False and
          that a mismatched phrase yields 409 with the interlock's code.
```

```
[P2] TEST-011 — no tripwire for the stale-image trap the docs warn about
Repro:    read backend/Dockerfile against docker-compose.yml `x-backend-volumes`.
Expected: the trap that silently ran 1,292 of 1,547 tests should be detectable
          from inside the container, not only by remembering to `--build`.
Actual:   `COPY backend/app ./app` and `COPY backend/tests ./tests` bake both
          in at build time; the bind mounts are only `./config.yaml`, `./data`
          and `./secrets`. Nothing in the image records which commit it was
          built from, and no test compares a baked-in identity against the
          working tree, so a stale image is indistinguishable from a fresh one
          at the pytest summary line. The documented mitigation is discipline.
Evidence: backend/Dockerfile:39-42; docker-compose.yml:37-43
Fix sketch: `ARG GIT_SHA` → `ENV BUILD_SHA` in the Dockerfile, and a test that
          skips when BUILD_SHA is unset and otherwise compares it to the SHA in
          a bind-mounted file. Or just make the documented command
          `-v ./backend:/app`.
```

```
[P3] TEST-012 — backend/config.yaml is an empty tracked file added by HEAD
Repro:    `ls -l backend/config.yaml` → 0 bytes;
          `git log --oneline -- backend/config.yaml` → only 1ee5cd3.
Expected: config.yaml lives at the repo root and is bind-mounted to
          /app/config.yaml. There is no second one.
Actual:   `1ee5cd3` added a zero-byte `backend/config.yaml` (visible in
          `git show --stat` as `backend/config.yaml | 0`). It is not COPYed
          into the image and nothing reads it — `settings.config_path`
          defaults to `/app/config.yaml` and `test_config.py:_find_shipped_config`
          resolves `parents[2]/config.yaml`, i.e. the root one. Harmless today,
          but it is an accidental `git add` in the commit under review, and an
          empty config.yaml sitting next to the code is exactly the file a
          host-side `cd backend && pytest` run would eventually pick up.
Evidence: backend/config.yaml (0 bytes); git show --stat 1ee5cd3
Fix sketch: `git rm backend/config.yaml`.
```

```
[P3] TEST-013 — two docstrings still assert the pre-fix seq semantics
Repro:    read backend/app/kalshi/orderbook.py:8-12 and
          backend/tests/test_orderbook.py:3-5.
Expected: after 1ee5cd3, orderbook.py "records seq, refuses a replayed (lower)
          one, and judges nothing else".
Actual:   orderbook.py's opening paragraph still reads "**The rule that
          matters:** if a sequence number is skipped, the local book is no
          longer trustworthy… On any gap the book is marked stale" — which is
          the misunderstanding the next paragraph then corrects. And
          test_orderbook.py's module docstring still says "The critical
          property: a sequence gap must make the book refuse to answer", above
          a file that now tests the opposite. Both are about the
          per-*subscription* gap in ws.py and are true there, but a reader
          arriving at orderbook.py meets the wrong rule first.
Evidence: backend/app/kalshi/orderbook.py:8-12; backend/tests/test_orderbook.py:3-5
Fix sketch: reword to say gap detection is per-subscription and lives in ws.py.
```

```
[P3] TEST-014 — the int form of the cents-style price is not tested
Repro:    backend/tests/test_fees.py:307 parametrizes ["56","0","1","-0.5","1.5"] —
          all strings.
Expected: CLAUDE.md and README both write the rule as
          "`taker_fee_cents(56, ...)` raises — deliberately", with a bare int.
Actual:   The int path goes through a different `_to_decimal` branch
          (money.py:60-61) before hitting the same range check, so it does
          raise — but the literal form the docs use is untested.
Evidence: backend/tests/test_fees.py:307-313; backend/app/core/money.py:60-61
Fix sketch: add 56 and Decimal(56) to the parametrize list.
```

```
[P3] TEST-015 — direction.py validates no price range, and a test pins the
     nonsense as acceptable
Repro:    backend/tests/test_direction.py:72-76.
Expected: CLAUDE.md: "The only guards are app/trading/direction.py and the
          tests around it."
Actual:   `test_a_cents_style_price_is_not_silently_accepted` asserts
          `to_yes_price(Side.NO, 56) == Decimal(-55)` and comments "nonsense
          in, nonsense out", deferring the range check to the pricing layer.
          The assertion is honest about the design, but the test's *name*
          claims a guard the code does not have, and a -55 wire price would
          reach `rest.py` from any caller that skipped `price_ticket`.
Evidence: backend/tests/test_direction.py:72-76;
          backend/app/trading/direction.py:87-94 (no range check)
Fix sketch: either range-check in `to_yes_price` or rename the test.
```

```
[P3] TEST-016 — the wire-form integration test covers 2 of the 4 direction rows
Repro:    grep the executor tests for `book_side`.
Expected: four rows: buy YES→bid@p, sell YES→ask@p, buy NO→ask@1-p,
          sell NO→bid@1-p.
Actual:   test_executor.py:394 (`test_buy_yes_is_sent_as_a_bid`) and :407
          (`test_buy_no_is_sent_as_an_ask_at_the_complement`, asserting
          book_side=="ask" and price=="0.70"). The two sell rows are only
          covered at the direction.py unit level, so a call site that dropped
          the `to_yes_price` call on a sell would pass.
Evidence: backend/tests/test_executor.py:394-421
Fix sketch: parametrize the existing wire test over all four rows.
```

```
[P3] TEST-017 — a risk limit is decided by a float comparison
Repro:    backend/app/trading/proposals.py:151-152.
Expected: CLAUDE.md Style: "Money is `Decimal`, never `float`."
Actual:   `fraction = float(max_loss_cents / bankroll_cents)` then
          `if fraction > config.risk.max_pct_per_market`. Both sides are
          floats, so the `max_pct_per_market` boundary is decided in binary
          floating point. `test_exactly_at_the_limit_is_allowed`
          (test_proposals.py:290) passes because 5000/100000 and the literal
          0.05 land on the same double — a different bankroll/limit pair need
          not. `pct_of_bankroll` is also a `Float` column (models.py).
Evidence: backend/app/trading/proposals.py:151-152;
          backend/tests/test_proposals.py:290-291
Fix sketch: compare `Decimal(str(config.risk.max_pct_per_market))` against the
          Decimal ratio and convert to float only for display.
```

```
[P3] TEST-018 — fees.py swallows every settings error and silently changes
     which fee file is read
Repro:    backend/app/core/fees.py:75-83.
Expected: fail closed. The fee table is the one input that makes every edge
          number meaningful.
Actual:   `_default_schedule_path()` wraps `get_settings()` in
          `except Exception:  # noqa: BLE001 - settings are optional for the
          engine` and falls back to the hardcoded `/app/data/fee_schedule.yaml`.
          A genuine settings failure (bad .env, unreadable path) therefore
          reads a *different file* than the rest of the stack, without a log
          line. It is mostly saved by `load_fee_schedule` raising
          FileNotFoundError when that path does not exist, and by
          `lru_cache(maxsize=4)` keying on the resolved path — but the silence
          is the wrong default here.
Evidence: backend/app/core/fees.py:75-83
Fix sketch: narrow to ImportError, or log a warning naming the fallback path.
```

```
[P3] TEST-019 — no property test that a cap can only ever reduce a size
Repro:    read backend/tests/test_sizing.py.
Expected: "Every cap is a ceiling, never a floor."
Actual:   Each cap is tested binding in isolation with the right
          `binding_constraint`, and `test_the_stake_never_exceeds_the_binding_cap`
          checks one instance. There is no test that adding a *generous*
          `available_contracts` or `exposure_headroom_cents` leaves the size
          unchanged, i.e. that a cap never raises. `min(caps, …)` makes it
          structurally true today; the test is what keeps it true.
Evidence: backend/tests/test_sizing.py:142-273
Fix sketch: assert recommend_size(...) == recommend_size(...,
          available_contracts=Decimal(10**9), exposure_headroom_cents=Decimal(10**9)).
```

```
[P3] TEST-020 — the documented test count is stale
Repro:    ast inventory vs CLAUDE.md "Verification".
Expected: a number an operator can compare against a run.
Actual:   CLAUDE.md says "it silently ran 1,292 tests against the previous
          milestone while 1,547 existed on disk". HEAD has 1,438 functions /
          ~1,557 collected cases (the 1ee5cd3 commit message says 1,562, which
          matches once the one dynamic parametrize is expanded). The 1,547
          figure is a milestone behind, which makes it useless as the tripwire
          it is being offered as.
Evidence: CLAUDE.md line ~353; ast inventory above; git show 1ee5cd3 (message)
Fix sketch: quote the collected-case count, or drop the number and rely on
          TEST-011's tripwire.
```

---

## Things checked and found correct (no finding)

- The six `seq` tests were **rewritten, not deleted** — `git show 1ee5cd3 --
  backend/tests/` is a +80/−33 diff that replaces each old assertion with the
  correct one and adds two more (`test_a_replayed_seq_is_refused`,
  `test_unknown_side_marks_the_book_stale`). No coverage hole in `orderbook.py`.
- `app/backtest/replay.py:388-405` duplicates `signed_contracts` and
  `to_yes_price` — apparently violating "never inline this mapping anywhere
  else" — but the copies are labelled as mirrors and
  `test_backtest_replay.py:402-436` asserts equivalence against
  `app.trading.direction` for all four combinations and a spread of prices.
  Defensible; not filed.
- `risk.snapshot()`'s pending-proposal query is not route-filtered, but
  `ProposedTrade` has no route column (route is decided at approval by
  `resolve_route`, which returns exactly one), so this is correct.
- CLAUDE.md's "a test asserts that [`trading.mode` is Literal]" — it does:
  `test_config.py:111-118` reads `TradingConfig.model_fields["mode"]` and
  asserts `typing.get_args(...) == {"paper","live"}`.
- `filterwarnings = ["error::DeprecationWarning:app.*"]` hides nothing; it
  escalates.
- 0 bare `except:` in the repo.

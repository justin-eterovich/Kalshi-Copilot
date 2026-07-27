# M7 — the weather engine

Status: **done**. Verified on the live stack against the real NWS API on
2026-07-27.

Prices Kalshi's NWS-settled temperature markets from a forecast plus a
*measured* estimate of how wrong that forecast usually is. Ships disabled, and
says nothing for its first month even when enabled — by design.

---

## The fact the whole engine turns on: temperature is an integer

Kalshi's daily-temperature markets settle on the National Weather Service
Climatological Report (Daily), which reports **whole degrees Fahrenheit**. So
a market whose rules say *"is between 96-97°"* — stored as
`strike_type="between", floor_strike=96, cap_strike=97` — resolves YES on
exactly **two outcomes, {96, 97}**.

It is not the continuous interval [96, 97]. Treating it as one understates
every bucket in the book by roughly half, systematically, in the same
direction. Measured: at forecast 96.5 with σ=3.0, the discrete `{96,97}`
bucket prices at **0.2611** where the continuous interval gives **0.1324** —
a factor of 1.97.

The live book confirms the structure exactly:

```
KXHIGHCHI-26JUL27-T90    less     <90
KXHIGHCHI-26JUL27-B90.5  between  {90,91}
KXHIGHCHI-26JUL27-B92.5  between  {92,93}
KXHIGHCHI-26JUL27-B94.5  between  {94,95}
KXHIGHCHI-26JUL27-B96.5  between  {96,97}
KXHIGHCHI-26JUL27-T97    greater  >=98
```

Exhaustive and non-overlapping over the integers. So the model builds a
probability mass function over integer degrees — integer `k` owning the
continuous band `[k-0.5, k+0.5)` — and sums the integers a bucket actually
contains. A consequence worth stating: **`greater than 96` means `T >= 97`**,
not `T > 96.0`, and those differ by the entire mass at 96, which near the
forecast is the largest single bucket in the book.

**Verified against a real book.** Priced the six KXHIGHCHI tiles above from a
synthetic 60-day calibration (bias +1.41°F, σ 2.15°F, debiased forecast
88.59°F):

| tile | fair |
|---|---|
| `<90` | 0.6645 |
| `{90,91}` | 0.2479 |
| `{92,93}` | 0.0764 |
| `{94,95}` | 0.0104 |
| `{96,97}` | 0.0100 |
| `>=98` | 0.0100 |
| **sum** | **1.0192** |

The sum lands on 1 within the clamp's own contribution — the two far tiles are
lifted off ~0 to the 0.01 floor, which is the entire 0.019 excess. That is the
check that the discretisation has neither a half-degree gap nor a double
count.

---

## Two market families, two settlement sources

| family | example | settles from | shape |
|---|---|---|---|
| daily high/low | `KXHIGHCHI-26JUL27-B96.5` | **NWS** Climatological Report (Daily) | integer buckets + open tails |
| hourly | `KXTEMPNYCH-26JUL2614-T82.99` | **The Weather Company** | threshold; "above 82.99" = ≥83 |

Our data is `api.weather.gov`. For family A that **is** the settlement source.
For family B it is a **proxy that can disagree with the thing that actually
settles the market**, so the engine refuses those outright rather than quoting
a number that looks the same but isn't. `rules.py` cross-checks source against
measure and refuses crossed pairs (hourly attributed to NWS, daily attributed
to The Weather Company) rather than trusting either half.

Across the whole live catalog, no market names a settlement source other than
those two.

---

## Three refusals before anything is priced

1. **A station has been asserted for the series.** The rules name a *city*,
   not a thermometer.
2. **The market settles from the NWS.**
3. **We have measured this station's forecast error at this lead time**, over
   at least `weather.min_calibration_samples` days.

Verified live: **396 candidate markets, all 396 passing station lookup, rules
parsing and the source check — and 0 findings**, because the calibration is
empty. That is the correct answer on day one and the engine will keep giving
it for about a month. Collection therefore runs whenever `weather.enabled` is
true, independent of the detector, because a system that only starts gathering
when switched on is useless for months afterwards.

### "Atlanta" is not a thermometer

Some rules name the station outright (*"Chicago Midway, IL"*, *"Miami
International Airport"*); most name only a city (*"Atlanta"*, *"Boston"*).
The Climatological Report is issued **per station**, and a reading taken
elsewhere in the metro can differ by several degrees on exactly the days that
matter — which, on a book of two-degree buckets, is the difference between the
right bucket and a confident loss.

So `app/weather/stations.py` is a table of **claims about the world**, the same
status as `set_arbitrage.exhaustive_series`. 21 stations asserted. An unlisted
series is refused, never guessed at, because a wrong guess is silent: the
model prices Atlanta's weather off Athens' thermometer and every number it
produces looks entirely reasonable.

Note it deliberately holds two different Chicago stations: `KXHIGHCHI` says
Midway (KMDW) in its own rules, while bare "Chicago" resolves to O'Hare
(KORD), the city's official climate site. They routinely disagree by a degree
or two.

### A landmine found while building this

**`KXLOW` is Lowe's Companies Inc. and `KXSNOWFLAKE` is Snowflake Inc.** Both
are earnings markets that a `startswith("KXLOW")` / `startswith("KXSNOW")`
match would happily route to a weather station. Series tickers are names, not
a namespace. The lookup is an exact dict match and must stay one.

Similarly, `KXGTEMP`, `KXWARMING` and `KXMEISSNER` all contain the word
"temperature" and none has a station — a NASA land-ocean index, a global
warming threshold, and a superconductor. The rules parser refuses all three.

---

## Units: the NWS contradicts itself between products

- **Observations** use the GeoJSON quantitative shape:
  `{"unitCode": "wmoUnit:degC", "value": 26, "qualityControl": "V"}` — Celsius.
- **`/forecast`** does not: a bare number beside a **one-letter** unit,
  `{"temperature": 90, "temperatureUnit": "F"}` — Fahrenheit, no `wmoUnit:`
  prefix.
- **The raw gridpoint product** uses a third spelling: `maxTemperature` carries
  its unit under **`uom`**, reporting `degC` for the same grid cell where
  `/forecast` reports F.

So the parser trusts the unit code and **refuses an unrecognised one** rather
than assuming Celsius — an unrecognised unit silently treated as Celsius is a
40-degree error that still looks like weather.

There is a live trap in this: `?units=si` flips `/forecast` from `"F"` to
`"C"` for the same period (verified on KMDW: 90/F vs 26/C). The client
deliberately sends no `units` parameter, and says so, because adding one would
silently change the scale of every forecast relative to those already stored.

Also worth knowing: `maxTemperatureLast24Hours` is **null** at both KMDW and
KNYC. There is no shortcut to a daily high from the observation endpoint.

---

## Calibration: sigma is measured, never chosen

Sigma is the spread of `forecast - actual` **about the measured bias**, not
about zero, bucketed by lead time `(6, 12, 24, 48, 96, 168]`. A station that
runs 2°F warm has a real, correctable bias; folding it into sigma both hides
that and inflates the uncertainty, making every bucket look closer to a coin
flip than it is.

- **No neighbour fallback and no global default.** If the 24-hour bucket is
  empty, the 48-hour sigma is not an approximation of it — it is a different
  instrument's uncertainty wearing the right units.
- **NaN, not 0.0, for a missing sigma.** Both are wrong, but NaN compares
  False against every threshold and produces a visibly broken price, whereas
  0.0 prices every bucket at exactly 0 or 1 and reads as an enormous edge.
- Sample stdev (n−1), and a single sample refuses rather than reporting a
  spread of zero.

`WeatherForecast` keeps **every issuance** rather than overwriting. That
history *is* the calibration dataset; a table holding only the current
forecast can never measure how wrong forecasts are.

---

## Live verification

```
21 stations polled     KATL KAUS KBOS KDCA KDEN KDFW KIAH KLAS KLAX KMDW KMIA
                       KMSP KMSY KNYC KOKC KORD KPHL KPHX KSAT KSEA KSFO
first sweep            21 observations, 147 forecasts (21 x 7 days), 0 actuals
```

Spot-checked: KMDW forecast 90°F for Jul 27 (matches the API directly), KPHX
111°F, KBOS observation 59.0°F — exactly 15°C — and lead hours stepping by 24
across successive target days.

```
1052 tests passed
ruff check app/                                clean
mypy app/core app/config.py app/settings.py    clean
5 compose services healthy
```

Config restored to `weather.enabled: false` afterwards; a test enforces that
every engine ships disabled.

---

## Schema changes

```sql
CREATE TABLE weather_observations (...);  -- UNIQUE (station_id, observed_at)
CREATE TABLE weather_forecasts (...);     -- UNIQUE (station_id, target_date, measure, issued_at)
```

---

## Follow-up: low-temperature markets

Lows are now wired end to end. The interesting part was the day-assignment
rule, which is **not** the mirror of the one for highs.

An NWS night period runs 18:00 local to 06:00 the next morning and reports
that night's minimum. The coldest hour is just before sunrise, so the
temperature *"Monday Night"* reports falls in **Tuesday's** calendar day and
the Climatological Report attributes it to Tuesday. Observed at KMDW:

```
Overnight       Mon 04:00 -> Mon 06:00   78F   -> Monday's low
Monday Night    Mon 18:00 -> Tue 06:00   71F   -> Tuesday's low
Tuesday Night   Tue 18:00 -> Wed 06:00   67F   -> Wednesday's low
```

So `daily_low` assigns by the period's **end** date where `daily_high`
assigns by its **start**. Keying lows by start would file every one of them a
day early, on every station, in every bucket — and on a book of two-degree
tiles a whole-day shift is not a rounding error.

"Ends on the day" rather than the simpler "starts the day before" because a
forecast issued during the night carries a truncated `"Overnight"` period that
both starts *and* ends on the same day; the start-minus-one rule would
silently drop it.

It is also the more robust rule. A night ends at 06:00 local, which is
mid-morning UTC for every US station, so the assignment survives a wrong
offset — whereas 18:00 local is already past midnight UTC on the Pacific
coast, and a start-based rule would need the offset to be right. Both facts
are pinned by tests.

**Calibration is now keyed by `(station, measure)`.** A station's overnight
lows are not forecast with the same skill as its afternoon highs, and one
sigma covering both would be wrong for each in opposite directions.

Also worth stating: `_observed_extreme` bounds the day in UTC, and that
approximation is **worse for lows than for highs**. A local day's hottest
hours sit well inside any reasonable window; its minimum happens just before
dawn, right against the boundary, so a US station's UTC window can straddle
two nights and pick the colder. Still fine for calibration, where a consistent
bias is measured rather than assumed away — but the low side carries the
larger error and the docstring says so.

Verified live: **192 of 192 low markets now resolve a forecast** (previously
zero). KMDW stored 78/71/67 for Jul 27/28/29, exactly the values read off the
raw periods. A real KXLOWTCHI book priced from a synthetic 45-day calibration
put its mass on `{70,71}` at 0.3915 against a debiased forecast of 70.52°F,
with the tiling summing to 1.0150 — the excess again being the clamp lifting
two negligible tiles off zero.

## Open items

- **The engine cannot price for ~30 days after being enabled.** That is the
  design, but it means `weather.enabled: true` should be set *now* if the
  engine is wanted next month.
- **Daily highs are proxied from hourly observations.** The Climatological
  Report is a separate once-a-day product and a running max of hourly readings
  is not guaranteed to equal it. Good enough to calibrate against; not good
  enough to settle a market on, which is why nothing here writes a settlement.
- **`_observed_high` bounds the day in UTC, not station-local.** For a daily
  *high* the hottest hours sit well inside any reasonable window, so the
  approximation is mild — but it is one, and a local-day window needs the
  station's timezone (which `/stations/{id}` does return).
- **The hourly family is unpriced by design** — 5 series, settled by The
  Weather Company, which we have no feed for.

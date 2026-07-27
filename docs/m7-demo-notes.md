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
- **Low-temperature markets are half-wired.** Stations are mapped for them and
  the rules parser reads `DAILY_LOW`, but ingest only stores `measure="high"`
  forecasts, so `KXLOWT*` markets currently find no forecast and are skipped.
- **The hourly family is unpriced by design** — 5 series, settled by The
  Weather Company, which we have no feed for.

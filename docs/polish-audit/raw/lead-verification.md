# Lead — runtime verification of agent findings

## BE-001 (Agent 1) — CONFIRMED at runtime, and it is real

Agent 1 filed this P1 with a note that the lead may want to promote it. I
verified the central claim against live data.

```bash
curl -s localhost:8080/api/markets/KXBTCMAXMON-BTC-26JUL31-7000000
```
```
strike_type    'greater'
floor_strike   '70000.000000'
yes_bid/ask    '0.070000' / '0.080000'
status         'active'
title          'Will BTC trimmed mean be above $70000.00 by 11:59 PM ET on Jul 31, 2026?'
rules_primary  'If the price of BTC after issuance and through 11:59 PM ET on
                Jul 31, 2026 is ever above $70000.00, then the market resolves to Yes.'
```

**"is ever above"** — a path-dependent barrier on the running maximum. The
structured fields say only `strike_type: 'greater'`, which
`stale_quote.py:96` and `vol.py:205-207` both read as "is spot above K *right
now*", i.e. a terminal-value question. `P(max_{t<=T} S_t > K)` is strictly
greater than `P(S_T > K)` and can differ by the entire contract value once the
barrier has been touched. Agent 1's worked example (+96¢ edge on a contract
already resolved YES) follows directly.

Note the title and the rules disagree with each other too — the title says
"trimmed mean be above", the rules say "ever above". Only the rules settle it,
and only the rules text contains the word that matters.

**Blast radius (live, active markets):** `KXBTCMAXMON` 8, `KXBTCMAXY` 7, plus
`KXBTCMAX100` — roughly 15-20 active markets. Small in count, unbounded in
per-trade error.

**Reachability:** requires `stale_quote.enabled: true` **and**
`bitcoin.enabled: true`; both are `false` in the deployed `config.yaml`, so
this cannot fire today. That is the only reason it is not a P0 — and enabling
one detector at a time is the *documented* operator workflow, so it becomes a
P0 the first time somebody follows the README.

**Severity call: keep at P1, tagged P0-ON-ENABLE**, and place it at the top of
the fixlist beside SAFETY-001. Filing it P0 outright would dilute the one
defect that is live and exploitable right now.

**Structural point worth making in the fix:** the weather engine parses
`rules_primary` and refuses what it cannot classify (CLAUDE.md documents this
at length — NWS vs The Weather Company). The crypto path parses no rules text
at all and trusts `strike_type` alone. The refusal machinery already exists in
this codebase; it just was not applied here.

---

## Prefix-matching in `reference_symbol_for` — checked, NOT currently mis-routing

While confirming BE-001 I found that `reference_symbol_for`
(`backend/app/detectors/stale_quote.py:160-181`) resolves the underlying by
**prefix**:

```python
REFERENCE_PREFIXES = {"KXBTC": "BTC-USD", "KXETH": "ETH-USD",
                      "KXSOL": "SOL-USD", "KXXRP": "XRP-USD"}
...
for prefix, symbol in REFERENCE_PREFIXES.items():
    if series.startswith(prefix):
        return symbol
```

Two things are off on their face:

1. Its own docstring says matching is *"on the **series** ... rather than on a
   substring of the whole ticker, so a strike or date that happens to contain
   'BTC' cannot pull a market into the wrong feed."* That describes an exact
   lookup. The code does a **prefix** match on the series.
2. CLAUDE.md states the rule directly: *"Series tickers are names, not a
   namespace. `KXLOW` is Lowe's Companies Inc. and `KXSNOWFLAKE` is Snowflake
   Inc. A prefix match would route earnings markets to a weather station.
   `station_for_series` is an exact dict lookup and must stay one."* That is
   written about the weather map, but the hazard is identical here — and this
   is the very module whose wrong-asset bug (ETH strike vs BTC spot, 1,762
   markets, +72¢ phantom edge) motivated the rule.

**I checked whether it actually mis-routes anything today. It does not.**
Every live series captured by each prefix is genuinely that asset:

```
KXBTC → KXBTCD, KXBTCY, KXBTC, KXBTC15M, KXBTCMAXMON, KXBTCMAX100
KXETH → KXETHD, KXETHY, KXETH, KXETH15M
KXSOL → KXSOLD, KXSOLE, KXSOL15M
KXXRP → KXXRPD, KXXRP, KXXRP15M
```

The only suspicious one, `KXSOLE` (10,100 markets), is genuinely Solana —
`"SOL price on Jul 27, 2026?"`, category Crypto. No collision.

**So this is a latent risk, not a live defect** — filed P3. A future listing
such as an "Ethiopia" series (`KXETH…`) or any ticker that happens to begin
with those five characters would be silently routed to the wrong spot feed,
and the failure mode is the documented one: a large, arithmetically perfect,
completely wrong edge. It is also how `KXBTCMAXMON` gets swept into the
detector at all, which is the delivery mechanism for BE-001.

**Fix sketch:** make it an exact `dict` lookup with the barrier/`MAX` series
listed explicitly (and refused), matching `station_for_series`, and correct
the docstring either way.

---

## API-001 (Agent 3) — CONFIRMED P0, reproduced independently

```bash
curl -s -X POST localhost:8080/api/proposals/quote -H 'Content-Type: application/json' \
  -d '{"ticker":"KXMLB-26-HOU","side":"yes","action":"buy",
       "limit_price":"0.018","contracts":"1","fair_price":"56"}'
```
→ **HTTP 200**, `net_edge_cents: "5597.5700"`, `fair_price` echoed back as `56`.

A claimed **+$55.97 per contract** edge on a contract quoted at 1.8¢. `"56"`
is the obvious operator slip for 56¢ — the exact units trap the whole codebase
is built to prevent, and `limit_price` is range-checked (`0 < p < 1`,
`pricing.py:174-182`) 32 lines above where `fair_price` is parsed without any
check (`pricing.py:210-211`). Stands as **P0**.

## API-002 (Agent 3) — CONFIRMED in part; downstream claim did NOT reproduce

**Confirmed:** `NaN` passes the money-parsing chokepoint.

```bash
curl -s -o /dev/null -w '%{http_code}' \
  'localhost:8080/api/fees/quote?price_dollars=NaN&contracts=1&series=KXMLB'   # 500
curl -s -o /dev/null -w '%{http_code}' \
  'localhost:8080/api/fees/quote?price_dollars=0.50&contracts=NaN&series=KXMLB' # 500
```
Both return **HTTP 500** with a bare `Internal Server Error` body.

**Confirmed:** the NaN reaches Postgres and is served back. Proposal 267 is
live in the database as `status: "executed"` with `net_edge_cents: "NaN"` and
`legs[0].fair_price: "NaN"` — and it executed a real demo order (order 13).

**NOT reproduced — Agent 3's downstream claim.** Agent 3 stated one NaN row
makes `avg_claimed_edge_cents` NaN for its group via `report.py:243`. It does
not, in the current state:

```
manual                route=demo_exchange  trades=3  avg_claimed_edge=None
set_arbitrage         route=demo_exchange  trades=2  avg_claimed_edge=None
undervalued_screener  route=simulated      trades=0  avg_claimed_edge='0.0000'
```

Proposal 267 is `source: manual` and sits in exactly that group, yet the
average reads `None`, not `NaN` — most likely because manual proposals carry
`signal_id: null` and so contribute no *claimed* edge to average.

**Severity: kept at P0** — a `NaN` in a money column on an executed trade is
wrong money math persisted to the book, and two public endpoints 500 on it.
But the blast radius is **narrower than reported**: the report card is not
currently poisoned. Anyone fixing this should not expect the report-card
symptom as a regression test.

## TEST-012 (Agent 2) — refined, not escalated

`backend/config.yaml` is a 0-byte file committed in HEAD (`1ee5cd3`).

**In the deployed container it is harmless:** `docker-compose.yml:39` and
`:172` bind-mount the real `./config.yaml` over `/app/config.yaml`, and
`settings.py:59` defaults `config_path` to `/app/config.yaml`. The mount
shadows the empty file.

**It is a live trap for the host-run path the README documents**
(`cd backend && pip install -e '.[dev]' && python -m pytest`), where nothing
shadows it. Stays P3; the fix is `git rm backend/config.yaml`.

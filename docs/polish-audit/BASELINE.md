# Polish audit — baseline and environment record

Audit date: **2026-07-27**. Repo HEAD: **`1ee5cd3`** ("Fix the orderbook
sequence check: seq counts the subscription, not the market"), working tree
**clean**. Audit performed from worktree `polish-audit` off that commit.

---

## Environment: what this audit could and could not do

Two capabilities the audit brief assumes were **not available on this host**.
This is an environment limitation, not an app defect, and it is recorded here
because it bounds every conclusion below.

### 1. Docker is inaccessible

```
$ docker compose ps
permission denied while trying to connect to the Docker daemon socket
    at unix:///var/run/docker.sock

$ id -nG
justin adm cdrom sudo dip plugdev users lxd      # no 'docker' group

$ sudo -n true
sudo: interactive authentication is required      # sudo needs a password
```

Verified both inside and outside the tool sandbox — it is a real group
membership issue, not a sandbox artifact.

**Consequently NOT run, and NOT in this report:**

| Brief item | Status |
|---|---|
| `docker compose build` (all three images) | **not run** |
| `pytest tests/ -q` — the collected test count | **not run** |
| `ruff check app/` | **not run** |
| `mypy app/core app/config.py app/settings.py` | **not run** |
| `scripts/backtest.py --days 30` refusal quality | **not run** (audited by code reading instead) |
| Parallel `audit-fresh` stack — the **empty / first-run state** | **not built** |
| Container logs (secret sweep over service logs) | **unavailable** |
| Service restarts to apply config (cooldown / daily-loss-halt states) | **unavailable** |

**Unblock:** `sudo usermod -aG docker justin` then re-login (or a
`NOPASSWD` sudoers entry for `docker`).

### 2. No browser automation

```
$ ~/.cache/ms-playwright/chromium-1148/chrome-linux/chrome --version
error while loading shared libraries: libatk-1.0.so.0: cannot open shared
object file: No such file or directory
```

The Playwright chromium build is present but its system GTK/X libraries are
not, and installing them needs root. There is also no `node`, no `npm`, no
`npx`, and no `pip` (Python 3.14 has no `ensurepip`, so a venv cannot
bootstrap one). Host Python is **stdlib-only**.

**Consequently NOT in this report:** every screenshot, every UI interaction,
the three-viewport visual sweep, the 390px phone pass, the browser-console
cleanliness check, and the first-person operator walkthrough. Those parts of
the audit were re-scoped to static source review plus API-driven state
inspection, and every such finding is marked accordingly.

**Unblock:** install the chromium runtime deps as root, e.g.
`sudo apt-get install -y libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64
libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
libgbm1 libpango-1.0-0 libasound2t64` — or simply run the browser work
inside a container once docker access exists.

---

## Is the running stack built from HEAD?

Cannot be confirmed without docker. **Evidence that it is not badly stale:**
the live API exposes **29 operations across 28 paths**, and HEAD's source
declares **29** `@router.*` decorators across the same 28 paths — an exact
match, including `POST` and `GET` both on `/api/proposals`.

```
$ grep -rhoE '@router\.(get|post|put|delete)\(' backend/app/api/routes/ | wc -l
29
$ curl -s localhost:8080/openapi.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d['paths']), sum(len(v) for v in d['paths'].values()))"
28 29
```

`config.yaml` and `data/fee_schedule.yaml` are **tracked** and the working
tree is clean, so the bind-mounted config the containers read is byte-identical
to HEAD. Only the baked-in `app/` and `tests/` layers are unverifiable.

**Caveat that follows:** per CLAUDE.md, `compose run api pytest` tests the
image, not the tree. Nobody could run it this session either way, so **the
test suite's green/red status at HEAD is unknown** and Agent 2's audit is
purely static.

---

## Baseline measurements actually taken

### Test inventory (static, on disk at HEAD)

```
$ grep -rhoE "^\s*(async )?def (test_[A-Za-z0-9_]+)" backend/tests/ | wc -l
1438
$ ls backend/tests/*.py | wc -l
41
```

**1,438** test functions across **41** files. The parametrize-adjusted
collected estimate is in Agent 2's findings. CLAUDE.md's M9 note references
1,547 tests existing on disk at that time; the delta is unexplained and is
itself an audit item.

### Live stack state at audit start

`GET /api/system`:
```json
{"environment":"demo","trading_mode":"paper","live_trading_armed":false,
 "kill_switch":false,"credentials_present":true,"enabled_detectors":[],
 "heartbeats":{"ingest":true,"worker":true},
 "fees":{"verified_on":"2026-07-27","base_taker_rate":0.07,
         "base_maker_rate":0.0175,"series_listed":85,"default_is_safe":true},
 "endpoints":{"rest":"https://external-api.demo.kalshi.co/trade-api/v2"}}
```

`GET /api/trading/state`: `execution_route=demo_exchange`, `real_money=false`,
`proposal_ttl_sec=120`, `auto_cancel_after_sec=90`, `pending_proposals=0`,
`working_orders=0`, balance `$80.1587`, portfolio value 1895¢.

`GET /api/risk`: bankroll 100,000¢, exposure 636¢, headroom 39,364¢,
daily fees 20.88¢, `daily_loss_breached=false`, `consecutive_losses=0`,
`halted=false`. Limits: per-market 5%, total exposure 40%, daily loss 5%,
cooldown after 3, kelly fraction 0.25, max pending proposals 10.

`GET /api/catalog/stats`: 218,516 markets / 147,220 active / 5,276
uncategorised; 597,542 events; 9,144 candles; 27,079 tape rows; **9,914
orderbook snapshots**; trade lag 3.5s.

`GET /api/engine`: BTC-USD spot present but **stale — `age_sec` 27,878
(7.7 hours), `fresh: false`**; ETH/SOL/XRP null as documented.
`bitcoin_enabled=false`. Calibration: 1,020 observations, 139 settled,
min_samples 500, **no bucket ready**.

**Both data states were required by the brief; only the populated one was
reachable.** The empty / first-run state needed the parallel `audit-fresh`
compose project, which needs docker.

### Detector state

`enabled_detectors: []` — all six ship disabled and all six are disabled here,
matching README. No detector has signalled during this audit, and no live
signal path was exercised.

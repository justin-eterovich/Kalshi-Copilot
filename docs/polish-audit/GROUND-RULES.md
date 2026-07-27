# Polish audit — ground rules for every agent

Read these before doing anything. Then read `CLAUDE.md` **in full** and
`README.md` (it doubles as the spec this audit judges the app against).
Where this file conflicts with CLAUDE.md, **CLAUDE.md wins**.

## Where you are

- **Work in the worktree:** `/home/justin/kalshi-copilot/.claude/worktrees/polish-audit`
  It is a clean checkout of HEAD (`1ee5cd3`), identical to the primary checkout.
  `config.yaml` and `data/fee_schedule.yaml` are tracked and present.
- The primary checkout is `/home/justin/kalshi-copilot`. Read it only if you
  need `.env`-adjacent deployment truth — and even then **never read, print,
  echo, or commit `.env` or anything in `secrets/`**.
- **This run is an audit, not a refactor. Do not fix anything.** No edits to
  `backend/`, `frontend/`, `config.yaml`, or `scripts/`. Findings only.

## Environment constraints — READ, these will save you an hour

Two capabilities the audit brief assumes are **not available on this host**.
Do not try to work around them; do not burn time re-discovering them.

1. **Docker is inaccessible.** The `justin` user is not in the `docker` group
   and `sudo` requires a password. Every `docker`/`docker compose` command
   fails with a socket permission error. So there is **no** rebuild, no
   `compose run api pytest`, no `ruff`/`mypy` run, no container logs, no
   parallel fresh stack, no service restart, no `scripts/backtest.py` run.
2. **No browser automation.** There is no `node`/`npm`/`npx`, no `pip`
   (no `ensurepip`), and the bundled Playwright chromium at
   `~/.cache/ms-playwright/chromium-1148/` **cannot start** — missing
   `libatk-1.0.so.0` and friends, which need root to install. So there are
   **no screenshots and no UI interaction**. Do not attempt Playwright.

Also unavailable: host Python has no third-party packages at all (no pytest,
no fastapi, no requests). Python 3.14 **stdlib only**. `curl` works.

## What IS available

- **The live stack is up and healthy on `http://127.0.0.1:8080`** — API +
  built SPA. 28 endpoints (`GET /openapi.json`). It is richly populated:
  218,516 markets, 147,220 active, live tape (~3.5s lag), 9,914 orderbook
  snapshots, 1,020 calibration observations.
- Live state: `environment=demo`, `trading_mode=paper`, `live_trading=false`,
  `kill_switch=false`, `credentials_present=true`,
  `execution_route=demo_exchange`, fee schedule **verified** 2026-07-27,
  `enabled_detectors=[]`, `bitcoin_enabled=false`.
- The **built frontend bundle** is fetchable over HTTP
  (`/assets/index-*.js`, `/assets/index-*.css`) — this is the real shipped
  artifact, better evidence than the TS source for "what actually runs".
- Full static read of the repo, and `git log`/`git show` for history.

## Safety — non-negotiable

- `KALSHI_ENV=demo`, `LIVE_TRADING=false`, always. **Never touch production.**
  Placing **demo** orders to exercise a flow is expected and fine.
- **Never weaken an interlock**, even in test scaffolding, even temporarily.
  Never edit `config.yaml` to arm anything. Config-level reasoning only.
- Never run destructive DB operations. There is no `docker compose down -v`
  available to you anyway, but also: do not drop, truncate, or delete rows.
- Writes to the live API (creating proposals, approving on the demo route)
  are allowed **only if your brief explicitly says so**. If it does, stay on
  the `paper`/`demo_exchange` route and keep sizes tiny.

## Evidence or it doesn't exist

- Code findings: `file:line` (path relative to repo root).
- Runtime findings: the exact `curl` command and the actual response body.
- If a suspicion is runtime-checkable against the live API, **check it**
  before filing. If it is only checkable via docker/browser (unavailable),
  file it as `UNVERIFIED — needs runtime` and say exactly what command would
  settle it. Do not present a suspicion as a confirmed defect.
- Never fabricate a screenshot path. Screenshots are impossible this run.

## Finding format — use exactly this

```
[P0|P1|P2|P3] AREA-### — one-line title
Repro:    exact steps or command
Expected: what docs/spec/common sense says
Actual:   what happened
Evidence: file:line | curl + response | log excerpt
Fix sketch (optional, one line)
```

Severity:
- **P0** — wrong money math, or a safety invariant that does not hold.
- **P1** — functional bug.
- **P2** — UX friction a reasonable operator would curse at.
- **P3** — cosmetic.

`AREA` is your agent's area prefix, given in your brief. Number sequentially.

## Deliverable

1. Write your **full** findings to `docs/polish-audit/raw/<your-file>.md`
   (path given in your brief), in the format above.
2. Return **structured findings only** — no transcripts, no narration, no
   step-by-step of what you tried. Lead with a one-line count by severity.
   If you have more than ~15 findings, return the P0/P1s in full and
   summarise P2/P3 by title, pointing at your raw file.
3. If you confirm a **P0**, say so in the first line of your return.

Rank honestly. A finding you cannot evidence is not a finding — drop it or
mark it UNVERIFIED.

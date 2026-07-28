# Polish audit — FIX phase rules

**This supersedes the "audit only, do not fix anything" clause in
`GROUND-RULES.md`.** Everything else in that file still applies —
especially the environment constraints and the safety rules.

Read `CLAUDE.md` **in full** before touching anything. Where it conflicts
with this file, **CLAUDE.md wins**.

## You are now fixing, not auditing

The operator has greenlit fixes for the findings in
`docs/polish-audit/REPORT.md` and `docs/polish-audit/FIXLIST.md`. Read both,
plus the raw file for your area in `docs/polish-audit/raw/`.

## You cannot verify anything. Plan accordingly.

- **No docker** → you cannot run `pytest`, `ruff`, or `mypy`. Do not try.
- **No browser** → you cannot see the UI. Do not try Playwright.
- The **running stack on `:8080` serves a pre-built image**, so it will NOT
  reflect your changes. It is still useful for reading *current API payload
  shapes*, which is what the frontend consumes.
- Host Python is **stdlib-only 3.14**. `python3 -m py_compile <file>` works and
  is your minimum gate — **run it on every Python file you touch**.

Because nothing can be executed, **small surgical diffs beat clever
refactors**. A fix you cannot test should be one you can fully justify by
reading. If a finding needs a large or risky change to fix properly, prefer
the smallest correct change and note the residue.

## Hard constraints — unchanged, non-negotiable

1. **No auto-trade mode, ever.** `trading.mode` stays `Literal["paper","live"]`.
2. **Never weaken an interlock.** Every fix must leave the safety model at
   least as strong as it was. If a fix makes something *more* permissive,
   stop and flag it instead.
3. **Never touch `.env` or `secrets/`.** Never print or commit either.
4. **Do not change `config.yaml` values** that arm anything. Adding a *new*
   key with a safe default is fine; flipping `enabled: false` → `true` is not.
5. **Fee math lives only in `core/fees.py`.** Direction math lives only in
   `trading/direction.py`. Do not inline either anywhere.
6. **Money is `Decimal`, never `float`.** Prices stay strings across the API
   boundary and in JSON to the frontend.
7. Match surrounding code style. Comments explain **why**, especially where a
   subtlety cost real debugging — that is the house style here.

## File ownership — do not edit outside your lane

Three agents and the lead are working in **one shared worktree
simultaneously**. Editing a file outside your lane will collide.

| Lane | Owner | Files |
|---|---|---|
| P0s: concurrency, money validation, kill switch, barrier markets | **LEAD** | `backend/app/trading/proposals.py`, `executor.py`, `interlocks.py`, `risk.py`, `backend/app/core/money.py`, `backend/app/trading/pricing.py`, `backend/app/api/routes/trading.py`, `backend/app/db/models.py`, `backend/app/config.py`, `backend/app/detectors/stale_quote.py`, `backend/app/btc/vol.py`, `backend/app/core/redis.py` |
| Backend correctness | **Agent B** | `backend/app/detectors/` (**except `stale_quote.py`**), `backend/app/ingest/`, `backend/app/worker/`, `backend/app/backtest/`, `backend/app/weather/`, `backend/app/news/`, `backend/app/kalshi/`, `backend/app/core/logging.py`, `backend/app/api/routes/markets.py`, `health.py`, `scripts/`, `README.md`, `CLAUDE.md` |
| Frontend | **Agent F** | `frontend/` only |
| Tests | **Agent T** (later wave) | `backend/tests/` only |

If you believe a fix requires editing a file in someone else's lane,
**do not edit it** — report it in your return and the lead will apply it.

## Reporting

Return a concise list of what you changed, in this form:

```
FIXED <ID> — <one line: what changed and where>
  file:line — <the essential diff idea>
PARTIAL <ID> — <what was done, what remains and why>
SKIPPED <ID> — <why: not reproducible / needs another lane / risky without tests>
```

Then: any file you touched outside your own understanding, anything you think
the lead should double-check, and any finding you believe was **wrong**
(audits contain errors; say so if you find one — that is valuable).

Do not narrate your process. Do not paste large diffs.

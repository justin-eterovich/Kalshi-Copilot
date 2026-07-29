# Corrected bind-mount test recipe

For folding into CLAUDE.md's **Verification** section, replacing the
parenthetical that currently reads:

> Either build first, or bind-mount the source (`-v ./backend:/app` plus
> `config.yaml`, `data/`, `scripts/`, since those live above `backend/`).

That recipe does not work as written, and every run of it manufactures on the
host the exact zero-byte `backend/config.yaml` that commit `3a93b4e` exists to
prevent.

---

## The corrected command

```bash
docker compose run --rm --no-deps \
  -v ./backend/app:/app/app \
  -v ./backend/tests:/app/tests \
  api python -m pytest tests/ -q
```

Same shape for the other two checks:

```bash
docker compose run --rm --no-deps -v ./backend/app:/app/app api ruff check app/
docker compose run --rm --no-deps -v ./backend/app:/app/app \
  api mypy app/core app/config.py app/settings.py
```

Nothing else needs mounting. `compose run` inherits the service's own volumes,
and `x-backend-volumes` (docker-compose.yml:38-43) already binds
`./config.yaml`, `./data` and `./secrets` into the container.

## Why the current one breaks

Two independent problems, both caused by mounting **over** `/app`.

**1. It hides files the suite needs, so collection fails before a single test
runs.** `config.example.yaml` and `scripts/` are baked into the image at
`/app/` by the Dockerfile, not present under `backend/` on the host. Mounting
`./backend` onto `/app` masks both:

```
FileNotFoundError: config.example.yaml not found in any of:
  [PosixPath('/config.example.yaml'), PosixPath('/app/config.example.yaml')]
    tests/test_config.py:46
ERROR tests/test_fee_schedule_script.py - StopIteration
```

Mounting them back in is what causes problem 2, so this is not a fix.

**2. Every nested mount target is created on the host, inside `backend/`.**
Docker requires a mount point to exist and creates it if it does not. With
`/app` already bind-mounted to `./backend`, "creates it" means *on the host,
under `backend/`*, root-owned. Adding `-v ./config.yaml:/app/config.yaml`
therefore produces a zero-byte `backend/config.yaml`; `data/` and `scripts/`
produce empty root-owned directories; and fixing problem 1 with
`-v ./config.example.yaml:/app/config.example.yaml` produces a zero-byte
`backend/config.example.yaml` too.

Observed this session, four artifacts per run:

```
-rw-r--r-- 1 root root 0 backend/config.yaml
-rw-r--r-- 1 root root 0 backend/config.example.yaml
drwxr-xr-x 2 root root   backend/data/
drwxr-xr-x 2 root root   backend/scripts/
```

`backend/config.yaml` is gitignored (`.gitignore:40`, with a comment naming the
zero-byte file committed at `1ee5cd3`), so it does not show in `git status` and
is easy to leave behind. `backend/config.example.yaml` is **not** ignored and
does show up as untracked — one `git add -A` away from re-committing the empty
template whose tracked twin `test_config.py` asserts the shipped-safe posture
against.

The corrected command avoids both because `/app/app` and `/app/tests` already
exist in the image, so Docker creates nothing, and `/app` itself is untouched
so everything baked in stays visible.

## Verified

- Corrected command: **2002 passed**, and `ls backend/` before and after is
  byte-identical — `Dockerfile app kalshi_copilot.egg-info pyproject.toml
  tests`. Run twice from a cleaned baseline to confirm.
- `ruff check app/` and `mypy app/core app/config.py app/settings.py` both
  clean under the same mounts, with no `--cache-dir` override. The old recipe
  needed `--cache-dir /tmp/...` and `-p no:cacheprovider`, because it put a
  host-owned directory at `/app` that the container user cannot write:
  `PytestCacheWarning: [Errno 13] Permission denied: '/app/.pytest_cache/...'`.
  That symptom disappears with the mount point, and is worth recognising as the
  same root cause rather than a separate annoyance.

## Two things this does not change

- It still tests the **working tree**, not the image. `docker compose build`
  before shipping is still the real gate; this is the fast inner loop.
- It mounts `app/` and `tests/` only. A change to `pyproject.toml`,
  `Dockerfile`, `config.example.yaml`, `scripts/` or `data/` still needs a
  rebuild to be seen.

## Cleanup, if the old recipe has already been run

```bash
rm -f backend/config.yaml backend/config.example.yaml
rmdir backend/data backend/scripts 2>/dev/null
```

Check `git status --short backend/` afterwards, and confirm the real config is
intact — root `config.yaml` should be non-empty and match `config.yaml.bak`.
Nothing tracked is ever removed by this; the four paths above are mount-point
stubs only. Worth stating plainly in any report, because on filename alone
"deleted backend/config.yaml" is indistinguishable from the incident CLAUDE.md
records as having destroyed the live config.

# Screenshots

## 2026-07-29 run — `tester-round1/`, `tester-round2/`, `tester-round3/`

42 screenshots, captured in a real browser against the live stack. Every
finding in `raw/TESTER-ROUND*.md` that cites one, cites one of these.

The unblock is the one the 2026-07-27 note below predicted — the browser runs
**in a container**, not on the host. The host still cannot start chromium
(`libatk-1.0.so.0`, needs root), and three agents lost time rediscovering that
before it was written down. Do not try to `pip install playwright` or launch
`~/.cache/ms-playwright/.../chrome` directly.

Image `kalshi-pw:latest` is `node:22-slim` + the chromium system libs +
`playwright-core`, symlinked at `/node_modules` so ESM resolution finds it from
a bind-mounted workdir (`NODE_PATH` does not apply to ESM — that costs a
confusing failure if you skip it). Rebuild it with:

```dockerfile
FROM kalshi-browser:latest
RUN mkdir -p /opt/pw && cd /opt/pw && npm install playwright-core@1.49.0
RUN ln -s /opt/pw/node_modules /node_modules
WORKDIR /work
```

```bash
docker run --rm --network host \
  -v /home/justin/.cache/ms-playwright:/ms-playwright:ro \
  -v "$PWD/scripts":/work -v "$PWD/shots":/out \
  kalshi-pw:latest node /work/yourscript.mjs
```

Launch with `executablePath:
'/ms-playwright/chromium_headless_shell-1148/chrome-linux/headless_shell'` and
`args: ['--no-sandbox','--disable-dev-shm-usage']`. `--network host` puts the
app at `http://127.0.0.1:8080`.

**Always attach `console` and `pageerror` handlers.** A silent React render
failure is the class of bug this harness exists to catch, and a screenshot
alone will not show you one.

One caveat worth carrying forward: headless Chromium is not desktop Chrome.
It reserved 0px for scrollbars regardless of CSS, and it has no `LANG`, which
surfaced as an uncaught locale `RangeError` blanking a chart (UI-048 — a real
robustness gap, but triggered by the harness rather than by the app). Treat a
finding that only reproduces headless as unconfirmed until it is seen elsewhere.

---

## 2026-07-27 audit — intentionally empty

No screenshots were captured in the 2026-07-27 polish audit, and **no finding
in `REPORT.md` cites one.**

Browser automation was unavailable on that audit host: the bundled Playwright
chromium at `~/.cache/ms-playwright/chromium-1148/` cannot start (missing
`libatk-1.0.so.0` and other GTK/X libraries, which need root to install), and
there is no `node`, `npm`, `npx`, or `pip` (Python 3.14 here has no
`ensurepip`, so a venv cannot bootstrap one).

Frontend findings therefore rest on complete source reading plus the real API
payloads the components receive, and anything whose visual result genuinely
needs a browser is marked `UNVERIFIED — needs browser` in the report. Ten of
those were settled — confirmed or killed — by the 2026-07-29 run above.

To unblock on the host instead, as root:

```bash
apt-get install -y libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
  libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libgbm1 libpango-1.0-0 libasound2t64
```

See `../BASELINE.md` for the full environment record.

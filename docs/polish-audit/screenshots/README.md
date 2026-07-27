# Screenshots — intentionally empty

No screenshots were captured in the 2026-07-27 polish audit, and **no finding
in `REPORT.md` cites one.**

Browser automation was unavailable on the audit host: the bundled Playwright
chromium at `~/.cache/ms-playwright/chromium-1148/` cannot start (missing
`libatk-1.0.so.0` and other GTK/X libraries, which need root to install), and
there is no `node`, `npm`, `npx`, or `pip` (Python 3.14 here has no
`ensurepip`, so a venv cannot bootstrap one).

Frontend findings therefore rest on complete source reading plus the real API
payloads the components receive, and anything whose visual result genuinely
needs a browser is marked `UNVERIFIED — needs browser` in the report.

To unblock a future run, as root:

```bash
apt-get install -y libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
  libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libgbm1 libpango-1.0-0 libasound2t64
```

— or run the browser work inside a container once docker access exists.

See `../BASELINE.md` for the full environment record.

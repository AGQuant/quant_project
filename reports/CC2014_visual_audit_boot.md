# cc#2014 — visual-audit crawler crashed on boot: tzdata at import, then a browser path that does not exist in the image

Founder provisioned the visual-audit Railway service per cc#2012 item 8 (build green, 741MB
image). The container CRASHED on start, twice over, in this order:

1. **Import time:** `IST = ZoneInfo("Asia/Kolkata")` → `ZoneInfoNotFoundError` (`No module named
   tzdata`). The `playwright/python:v1.48.0-jammy` image is slim — no `/usr/share/zoneinfo`, no
   `tzdata` pip package — so the stdlib `zoneinfo` had no data source at all. Died before login.
2. **Launch time:** `p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/...")`. That
   path was copied from CC's own container. **Verified, not assumed:** `browsers.json` inside the
   `playwright-1.48.0` wheel (downloaded from PyPI and read here) says chromium revision **1140**,
   browserVersion 130.0.6723.31. 1194 does not exist in the image, so `launch()` raised on every
   boot; Railway restarted it 10 times and marked it Crashed. Nothing in the app named the cause.

## What this push lands

- **`Dockerfile.visualaudit`** — `RUN pip install --no-cache-dir "psycopg[binary]" tzdata`. One
  RUN-line change; `FROM`, `COPY`, `CMD` untouched, as the card requires. `tzdata` is the stdlib
  `zoneinfo` module's documented fallback data source (PEP 615); the code keeps `Asia/Kolkata` as
  the canon — no hardcoded `+05:30` (a second copy of a fact CLAUDE.md already owns). The stale
  "runs once and exits / Cron Job command" comment block is rewritten to describe the always-on
  worker cc#2012 made it (comment only).
- **`visual_audit.py`** — `CHROME = os.environ.get("CC_CHROMIUM") or None`: no default path any
  more. New `launch_browser(p)` — the boot preflight, before login: `executable_path` is passed to
  `launch()` **only if** `CC_CHROMIUM` is explicitly set (the one remaining `executable_path` in the
  file sits inside that `if`); on success it logs
  `browser ready: chromium <version> via <Playwright's bundled browser | CC_CHROMIUM=...>`; on
  failure it logs at ERROR the path it tried and the exception, then `raise SystemExit(1)` so the
  exit is non-zero and Railway's ON_FAILURE policy takes over. `worker()` calls it in place of the
  old hardcoded launch. Nothing else changed: THEMES, VIEWPORTS, the five checks, thresholds, the
  request poller and the 03:00 IST trigger are as cc#2012 left them.

## Verify — done here, against the real shipped function

`ast.parse` clean. This container's `/opt/pw-browsers` holds chromium **1194**; the Python
`playwright` here had drifted to 1.62.0 (expects 1234) from an earlier `pip install`, which made
the FIRST run of the test hit the new ERROR path for real — the exact mismatch class this card
fixes, in the other direction. Installed `playwright==1.56.0` (its own `browsers.json` says
chromium 1194 — checked before installing) so library and browsers match, as they do in the
official image, then ran the three cases:

- (a) `CC_CHROMIUM` unset → **bundled browser resolves with no `executable_path`**; log:
  `INFO scorr.visual_audit: browser ready: chromium 141.0.7390.37 via Playwright's bundled browser (no executable_path)`
- (b) `CC_CHROMIUM` set to the image's own revision path, which does not exist here →
  `ERROR scorr.visual_audit: browser launch FAILED using CC_CHROMIUM=/opt/pw-browsers/chromium-1140/chrome-linux/chrome: Error: BrowserType.launch: Failed to launch chromium because executable doesn't exist at ...` then `SystemExit(1)` — no bare traceback.
- (c) `CC_CHROMIUM` set to a valid path → honoured, same version line `via CC_CHROMIUM=...`.

cc#2012's own 18-check harness (`verify_cc2012.py`: capture/actions/retention/token/IST-clock)
re-run after the edit: still passes.

## Verify — NOT done here

The Deploy Logs on the next visual-audit build (this container cannot read Railway), which should
show, in order: no `ZoneInfoNotFoundError`, the `browser ready: chromium 130.0.6723.31 via
Playwright's bundled browser` line, `logged in`, then `visual_audit_requests` id=1 (`/m/home`
aquawhite mobile, pending since 08:33 UTC) flipping to `done` with a `capture_id` and one
`visual_audit_captures` row with `image_bytes NOT NULL`. Redeploy is automatic (`visual_audit.py`
and `Dockerfile.visualaudit` are both watch paths). CC keeps watching the request row every 60s
and logs the first-run evidence on cc#2012 when it lands (rule 9).

Founder: leave `CC_CHROMIUM` **unset** on the service. The workaround the card mentions
(`CC_CHROMIUM=/opt/pw-browsers/chromium-1140/...`) is no longer needed and would only pin the next
Playwright bump to break again.

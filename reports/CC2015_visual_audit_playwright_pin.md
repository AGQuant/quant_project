# cc#2015 — third boot crash: `No module named playwright`. The Playwright image ships browsers, not the library.

Deployment b461cf0a (founder screenshot 19:20 IST): with cc#2014's tzdata + chromium-path fixes in,
boot reached `worker()` and died at `from playwright.sync_api import sync_playwright` —
`ModuleNotFoundError: No module named 'playwright'`.

## Cause — H2, confirmed at the primary source (not inferred)

`microsoft/playwright-python`, `utils/docker/Dockerfile.jammy` at tag **v1.48.0**, read directly:

- line 9: `apt-get install -y python3 python3-distutils curl` — system python3 (3.10 on jammy);
  line 11: `python get-pip.py` — pip for that python.
- line 19: `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`.
- lines 28–35: `cd /ms-playwright-agent && pip install virtualenv && virtualenv venv &&
  . venv/bin/activate && … pip install /tmp/*manylinux1_x86_64*.whl` — the playwright wheel goes
  into a **virtualenv**.
- line 37: `playwright install --with-deps` — from that venv, populating `/ms-playwright`.
- line 49: `rm -rf /ms-playwright-agent` — **the venv, and the playwright package with it, is
  deleted from the shipped image.**

So the image is a BROWSER image: browsers at `/ms-playwright/chromium-1140`, their native libs,
system python3 + pip, and the env var. The Python library is the user's job. This single fact
explains all three crashes on this service in order: no `tzdata` (bare python3, cc#2014), no
`chromium-1194` (cc#2014), no `playwright` module (this card). Fable's ruling on the card
(RESOLVED_BY_FABLE_1935) is correct; the line numbers above are from my own read.

## What this push lands — `Dockerfile.visualaudit` only

- `RUN pip install --no-cache-dir "psycopg[binary]" tzdata playwright==1.48.0` — the library,
  into the SAME `python3` the `CMD` runs. Pin **equals the image tag**: the 1.48.0 wheel's
  `browsers.json` asks for chromium revision 1140 (read from the wheel under cc#2014), which is
  what `/ms-playwright` holds. No `playwright install` (browsers already present; would
  re-download ~300MB). No `CC_CHROMIUM` anywhere: with cc#2014's no-default-path launch,
  Playwright resolves `/ms-playwright` through the image's own `PLAYWRIGHT_BROWSERS_PATH`.
- A ||-guarded diagnostic `RUN` right after `FROM` (item 1, optional per the narrowed scope, kept
  because it is free): prints `/ms-playwright` contents, `which python3`/`python3 -V`,
  `which pip`/`pip -V`, and `python3 -c "import playwright"` → expected
  `PLAYWRIGHT_ABSENT_AT_BUILD` on this image. It can never fail the build; it makes the Build Logs
  the proof for the next base-image question.
- A comment block above the pin saying exactly why it exists, so nobody removes it as "redundant".
- `FROM`, `COPY`, `CMD` unchanged. `visual_audit.py` untouched — cc#2014 stays intact (no default
  `executable_path`, `CC_CHROMIUM` optional-only, the `browser ready: chromium …` boot line is the
  success signal).

## Verify — done here

`playwright==1.48.0`, `tzdata`, `psycopg[binary]` all resolve on PyPI (the 1.48.0 wheel was
downloaded under cc#2014). Dockerfile instruction lines checked (`FROM/RUN/WORKDIR/COPY/RUN/CMD`),
continuations intact. No Docker daemon in this container, so the image itself is not built here.

## Verify — NOT done here (Railway)

Build Logs: the diagnostic block with `PLAYWRIGHT_ABSENT_AT_BUILD`, then the pip line installing
playwright 1.48.0. Deploy Logs: no traceback; `browser ready: chromium 130.0.6723.31 via
Playwright's bundled browser (no executable_path)`; `logged in`; then `visual_audit_requests` id=1
→ `done` + `capture_id`, one `visual_audit_captures` row with `image_bytes NOT NULL`. CC polls the
request row every 60s and logs first-run evidence on cc#2012 when it lands.

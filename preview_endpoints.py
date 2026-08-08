"""
preview_endpoints.py — cc#866 PREVIEW ROUTE (founder 05-Aug-2026, P0).

ONE generic route so Claude.ai can push mobile screens into previews/ and the founder can open them
on his phone. Without it that folder is unreachable and the whole screen-review loop is blocked.

OWNERSHIP (ROLE_CHARTER_V2, session_log 16159): Claude.ai owns previews/*.html and keeps pushing
there. CC owns this file and the single include_router line in main.py. Generic by design — adding
a screen never needs a code change again.

NO CACHING, DELIBERATELY. The card is explicit that _HTML_CACHE must not be used here: a cached
preview would show the founder a stale screen after Claude.ai pushes an update, which defeats the
entire purpose of the loop. The file is read per request. These are review screens opened a handful
of times a day, so the read cost is irrelevant next to showing the wrong thing.

PATH SAFETY IS BY CONSTRUCTION, NOT BY FILTERING. `name` is matched against a strict allowlist
([a-z0-9_-]) and rejected with a 404 BEFORE the filesystem is touched. The path is then BUILT from
the validated name — never assembled from raw input — and the resolved path is additionally checked
to be inside PREVIEW_DIR, so even a bug in the pattern cannot escape the folder. `/preview/../main.py`
and `/preview/..%2Fmain.py` both fail the allowlist on the dot, before any I/O.
"""

import os
import re
import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("scorr.preview")
router = APIRouter()

PREVIEW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "previews")

# Allowlist, not a denylist. Anything outside this set is rejected — no slashes, no dots, no
# traversal sequences, no encoded variants (FastAPI has already URL-decoded by this point, so
# ..%2F arrives as ../ and fails on the dot and the slash alike).
_SAFE_NAME = re.compile(r"^[a-z0-9_-]+$")

_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
             "Pragma": "no-cache"}


def _safe_path(name: str):
    """Return an absolute path inside PREVIEW_DIR, or None if `name` is not acceptable."""
    if not name or not _SAFE_NAME.match(name):
        return None
    path = os.path.join(PREVIEW_DIR, name + ".html")
    # Belt and braces: even with a validated name, confirm the resolved path really is inside the
    # preview directory before reading it.
    if os.path.realpath(path).startswith(os.path.realpath(PREVIEW_DIR) + os.sep):
        return path
    return None


@router.get("/preview")
def preview_index():
    """List the screens that actually exist, so the founder never has to guess a URL."""
    try:
        names = sorted(
            f[:-5] for f in os.listdir(PREVIEW_DIR)
            if f.endswith(".html") and _SAFE_NAME.match(f[:-5])
        )
    except FileNotFoundError:
        names = []
    except Exception as e:
        log.warning("cc#866 preview index failed: %s", e)
        names = []
    return JSONResponse(
        {"screens": names, "count": len(names),
         "open": [f"/preview/{n}" for n in names],
         "note": ("Review screens pushed by Claude.ai into previews/. Dummy data by design — "
                  "real data is wired by a later card. Not in the site nav.")},
        headers=_NO_STORE,
    )


@router.get("/preview/{name}", response_class=HTMLResponse)
def preview_screen(name: str):
    path = _safe_path(name)
    if not path or not os.path.isfile(path):
        # 404, never a 500 — a bad name and a missing file are both "nothing here".
        return HTMLResponse(f"No preview screen named '{name}'.", status_code=404,
                            headers=_NO_STORE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), headers=_NO_STORE)
    except Exception as e:
        log.exception("cc#866 preview read failed for %s", name)
        return HTMLResponse(f"Preview '{name}' could not be read: {type(e).__name__}",
                            status_code=404, headers=_NO_STORE)


# ── cc#889/cc#892 WIRING SHIM (Claude direct push, CHARTER_OVERRIDE_08AUG2026, session_log 17783) ──
# mobile_home2.router and mobile_ext.router are included HERE, not in main.py, as a deliberate
# exception to rule 5: main.py (112KB) and mobile_endpoints.py (70KB) can only be pushed as FULL
# file contents through the github_push API, and retyping either whole file to add two lines is a
# bigger correctness risk than this shim. preview_endpoints is already mounted by main.py, is
# small enough to reproduce byte-exactly, and is mobile-adjacent. CC: when next touching main.py,
# move these pairs there (`import mobile_home2; app.include_router(mobile_home2.router)` and the
# same for mobile_ext) and delete this shim — the routers themselves need no change (cc#893 item 3).
import mobile_home2                                    # noqa: E402  (deliberate tail import)
router.include_router(mobile_home2.router)             # routes join before main.py includes us
import mobile_ext                                      # noqa: E402  (cc#892 breadth + cc#893 depth)
router.include_router(mobile_ext.router)

"""visual_audit_endpoints.py — cc#2007 item 12 + cc#2012 items 2/3.

cc#2007: GET /api/visual-audit/failures -- FAILURES only for the latest run, grouped by route.
Untouched by cc#2012 (its do_not_touch names this route and its auth explicitly).

cc#2012 (the PIXEL LENS): two NEW routes that serve the picture itself --
  GET /api/visual-audit/capture/{id}?token=...   -> the stored JPEG bytes, with the stored mime
  GET /api/visual-audit/runs/latest?token=...    -> JSON: run_id, captured_at, per-capture summary

cc#2016: /capture/{id} gained an optional ?encoding=base64 -- Fable's own fetch tool reads text/
JSON/HTML but cannot render a raw binary image response from an external URL, so the default
image/jpeg reply (for a human opening the link) was a dead end for Fable specifically. With
encoding=base64 the SAME route, SAME auth path, returns JSON {id, route, theme, viewport, mime,
data_base64} instead. No param (or any value other than "base64") -> unchanged raw-image behavior.

cc#2018: a full capture can be tall and its base64 text too large for Fable to reliably hand-copy
(cc#2016's own verified follow-up: a 55KB JPEG is ~73K base64 chars). Two more optional params on
the SAME route, composable with encoding=base64: ?w=<int> downscales (aspect-preserving, never
upscales) to that width; ?crop=top keeps only the page's top viewport-height (915px, matching
visual_audit.py's own mobile VIEWPORTS entry) before any downscale. Either param re-encodes the
result as JPEG quality 60 via Pillow -- CONFIRMED already an installed dependency of this web
service, not added for this card: weasyprint>=60,<63 (requirements.txt) declares Pillow>=9.1.0 in
its own requires_dist (checked against PyPI's release metadata, not assumed), so the web service's
existing `pip install -r requirements.txt` already pulls it in. If Pillow somehow is not importable
at request time, or the stored bytes do not decode, both params are silently ignored -- the request
degrades to the unchanged full image/full base64 reply, never a 500. Neither param on its own
touches the auth path or the database schema; no new column, nothing new is stored.

Both are gated by ONE query-param token compared CONSTANT-TIME (hmac.compare_digest) against the
web-service env var VISUAL_AUDIT_VIEW_TOKEN. Wrong or missing token -> 404, not 401, so the route
does not advertise itself. Env var unset -> 404 for every request, fail-closed. CC generates
nothing: the founder sets the token in the Railway console (>=32 random bytes, hex); it is never
in this repo, a task log or session_log. A screenshot of a logged-in app shows positions and P&L,
so the token IS a credential and is treated as one.

Site-auth exclusion, checked rather than assumed (cc#2012's own gate): main.py's auth_gate
middleware redirects to /login ONLY for paths in the exact-match PROTECTED set or under /preview/.
These two routes are not in PROTECTED and never will be, so they are outside the site-password gate
by construction -- the token is their only auth, exactly as the card asks -- and the middleware
itself needed no change at all. Its post-response HTML injection is also a no-op here: it keys on a
text/html content-type, and these routes return image/jpeg and application/json.

This is a normal lightweight DB-read router in the main web app -- the "not inside the web process"
restriction is about the Playwright WORKER (visual_audit.py), not these reads.
"""

import base64
import hmac
import io
import logging
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import deriv_metrics  # reuses the one shared _conn() helper, same convention as option_chain_grid.py

try:
    from PIL import Image  # cc#2018: confirmed already installed (weasyprint's own requires_dist),
    _PIL_IMPORT_ERROR = None  # never a new dependency added by this card -- see the module docstring.
except ImportError as _e:  # pragma: no cover -- defensive only; not expected on this image
    Image = None
    _PIL_IMPORT_ERROR = _e

log = logging.getLogger("scorr.visual_audit_endpoints")
router = APIRouter()

CROP_TOP_PX = 915          # cc#2018 item 5: matches visual_audit.py's mobile VIEWPORTS height
MAX_THUMB_WIDTH_PX = 2000  # cc#2018: a sanity ceiling on `w`, well above any real viewport used
_warned_no_pillow = False


def _conn():
    return deriv_metrics._conn()


def _thumbnail(data: bytes, w_param: str, crop_param: str):
    """cc#2018: apply ?w=<int> (downscale, aspect-preserving, never upscales) and/or ?crop=top
    (keep only the first CROP_TOP_PX rows) to the stored JPEG, re-encoding at quality 60. Returns
    (new_bytes, width, height) when a transform was actually applied, or None when neither param
    asked for one -- callers fall back to the ORIGINAL bytes/mime unchanged in that case, so the
    no-param request path never even reaches Pillow. Never raises: an unimportable Pillow, a bad
    `w` value, or bytes that fail to decode as an image all resolve to None (the full image is
    still served) rather than a 500 -- a malformed request degrades gracefully, per the card."""
    global _warned_no_pillow
    do_crop = crop_param.lower() == "top"
    try:
        target_w = int(w_param)
    except (TypeError, ValueError):
        target_w = 0
    if target_w <= 0 or target_w > MAX_THUMB_WIDTH_PX:
        target_w = 0
    if not do_crop and not target_w:
        return None
    if Image is None:
        if not _warned_no_pillow:
            log.warning("cc#2018: w/crop param requested but Pillow is not importable (%s) -- "
                        "serving the full image instead", _PIL_IMPORT_ERROR)
            _warned_no_pillow = True
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        log.warning("cc#2018: stored bytes did not decode as an image, serving unchanged: %s", e)
        return None
    if do_crop and img.height > CROP_TOP_PX:
        img = img.crop((0, 0, img.width, CROP_TOP_PX))
    if target_w and target_w < img.width:
        new_h = max(1, round(img.height * target_w / img.width))
        img = img.resize((target_w, new_h), Image.Resampling.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=60)
    return out.getvalue(), img.width, img.height


def _token_ok(token: str) -> bool:
    """Constant-time compare against VISUAL_AUDIT_VIEW_TOKEN. Unset env var, empty token, or any
    mismatch -> False. The two branches below deliberately do not short-circuit on the token
    length: compare_digest handles that, and a plain `==` would leak position of first mismatch."""
    expected = os.environ.get("VISUAL_AUDIT_VIEW_TOKEN", "")
    if not expected or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


@router.get("/api/visual-audit/capture/{capture_id}")
def visual_audit_capture(capture_id: int, token: str = "", encoding: str = "",
                          w: str = "", crop: str = ""):
    """cc#2012 item 2: the stored image, served with its stored mime. 404 on bad/missing token, on
    an unknown id, and on a row whose bytes have already been purged by retention -- all three read
    the same from outside, on purpose.

    cc#2016: ?encoding=base64 (exact match, case-insensitive; absent or any other value is the
    original raw-image path -- UNCHANGED) returns the same bytes as JSON instead of image/jpeg, for
    Fable's own fetch tool, which cannot render a raw binary response. Same _token_ok call, same
    order (checked before encoding is even read) -- one auth path, not a second weaker one. The
    base64 text is built at request time from image_bytes; nothing new is stored.

    cc#2018: ?w=<int> and/or ?crop=top ask for a downscaled/cropped re-encode (see _thumbnail);
    composable with encoding=base64. Neither param present -> _thumbnail returns None immediately,
    without ever touching Pillow or the stored bytes -- the response is byte-for-byte what it was
    before this card, in EITHER mode. When a thumbnail WAS produced, the base64 JSON gains `width`
    and `height` keys (informational only) that are absent whenever no thumbnail was requested, so
    the pre-existing encoding=base64 (no w/crop) response shape is also unchanged."""
    if not _token_ok(token):
        raise HTTPException(404)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT route, theme, viewport, image_bytes, image_mime "
                    "FROM visual_audit_captures WHERE id=%s", (capture_id,))
        row = cur.fetchone()
    if not row or row[3] is None:
        raise HTTPException(404)
    route, theme, viewport, data, mime = row
    mime = mime or "image/jpeg"
    data = bytes(data)
    thumb = _thumbnail(data, w, crop)
    if thumb is not None:
        data, thumb_w, thumb_h = thumb
        mime = "image/jpeg"
    if encoding.lower() == "base64":
        out = {"id": capture_id, "route": route, "theme": theme, "viewport": viewport,
               "mime": mime, "data_base64": base64.b64encode(data).decode("ascii")}
        if thumb is not None:
            out["width"], out["height"] = thumb_w, thumb_h
        return out
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "no-store"})


@router.get("/api/visual-audit/runs/latest")
def visual_audit_runs_latest(token: str = ""):
    """cc#2012 item 2: the latest run as JSON -- run_id, captured_at, and one entry per capture
    {id, route, theme, viewport, status, fail_count, content_hash, has_image, label, route_group,
    serving_file}. The last three come from Fable's app_route_map (cc_task_logs 6427) by LEFT JOIN
    on the route, so a capture of a route the map does not know still lists -- with nulls, never
    dropped. An on-demand request capture carries a 'req-…' run_id and is not a 'run' in this
    sense; this reads the latest FULL crawl."""
    if not _token_ok(token):
        raise HTTPException(404)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT run_id, MAX(captured_at) FROM visual_audit_captures
                       WHERE run_id NOT LIKE 'req-%%'
                       GROUP BY run_id ORDER BY MAX(captured_at) DESC LIMIT 1""")
        row = cur.fetchone()
        if not row:
            return {"run_id": None, "captured_at": None, "captures": [],
                    "note": "no full crawl has been recorded yet"}
        run_id, captured_at = row
        cur.execute("""SELECT c.id, c.route, c.theme, c.viewport, c.status, c.content_hash,
                              c.image_bytes IS NOT NULL,
                              (SELECT COUNT(*) FROM visual_audit_results r
                                WHERE r.capture_id = c.id AND r.status = 'FAIL'),
                              m.label, m.route_group, m.serving_file
                       FROM visual_audit_captures c
                       LEFT JOIN app_route_map m ON m.route = c.route
                       WHERE c.run_id = %s
                       ORDER BY COALESCE(m.nav_position, 999999), c.route, c.theme, c.viewport""", (run_id,))
        rows = cur.fetchall()
    return {"run_id": run_id, "captured_at": captured_at.isoformat() if captured_at else None,
            "captures": [{"id": r[0], "route": r[1], "theme": r[2], "viewport": r[3], "status": r[4],
                          "content_hash": r[5], "has_image": r[6], "fail_count": r[7],
                          "label": r[8], "route_group": r[9], "serving_file": r[10]} for r in rows]}


@router.get("/api/visual-audit/failures")
def visual_audit_failures():
    """Latest run's FAILURES only, grouped by route. A run with zero failures returns an empty
    `routes` list and states so explicitly -- never omitted, never confused with 'no run has
    happened yet' (that case returns run_id=null and a plain note instead)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT run_id, MAX(captured_at) FROM visual_audit_captures GROUP BY run_id ORDER BY MAX(captured_at) DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                return {"run_id": None, "captured_at": None, "routes": [],
                        "note": "no visual-audit run has been recorded yet"}
            run_id, captured_at = row
            cur.execute(
                """SELECT r.route, r.theme, r.viewport, r.check_name, r.selector, r.measured,
                          r.expected, r.detail, r.capture_id, c.image_path, c.status AS capture_status, c.error
                   FROM visual_audit_results r
                   JOIN visual_audit_captures c ON c.id = r.capture_id
                   WHERE r.run_id = %s AND (r.status = 'FAIL' OR c.status = 'error')
                   ORDER BY r.route, r.theme, r.viewport, r.check_name""",
                (run_id,))
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM visual_audit_captures WHERE run_id=%s", (run_id,))
            total_captures = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM visual_audit_results WHERE run_id=%s", (run_id,))
            total_checks = cur.fetchone()[0]
        by_route = {}
        for (route, theme, viewport, check_name, selector, measured, expected, detail,
             capture_id, image_path, capture_status, error) in rows:
            grp = by_route.setdefault(route, {"route": route, "failures": []})
            grp["failures"].append({
                "theme": theme, "viewport": viewport, "check": check_name, "selector": selector,
                "measured": measured, "expected": expected, "detail": detail,
                "capture_id": capture_id, "image_path": image_path,
                "capture_status": capture_status, "capture_error": error,
            })
        return {"run_id": run_id, "captured_at": captured_at.isoformat() if captured_at else None,
                "total_captures": total_captures, "total_checks": total_checks,
                "failure_count": len(rows), "routes": list(by_route.values())}
    except Exception as e:
        raise HTTPException(500, f"visual_audit_failures failed: {e}")

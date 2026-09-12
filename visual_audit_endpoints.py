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
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import deriv_metrics  # reuses the one shared _conn() helper, same convention as option_chain_grid.py

router = APIRouter()


def _conn():
    return deriv_metrics._conn()


def _token_ok(token: str) -> bool:
    """Constant-time compare against VISUAL_AUDIT_VIEW_TOKEN. Unset env var, empty token, or any
    mismatch -> False. The two branches below deliberately do not short-circuit on the token
    length: compare_digest handles that, and a plain `==` would leak position of first mismatch."""
    expected = os.environ.get("VISUAL_AUDIT_VIEW_TOKEN", "")
    if not expected or not token:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


@router.get("/api/visual-audit/capture/{capture_id}")
def visual_audit_capture(capture_id: int, token: str = "", encoding: str = ""):
    """cc#2012 item 2: the stored image, served with its stored mime. 404 on bad/missing token, on
    an unknown id, and on a row whose bytes have already been purged by retention -- all three read
    the same from outside, on purpose.

    cc#2016: ?encoding=base64 (exact match, case-insensitive; absent or any other value is the
    original raw-image path -- UNCHANGED) returns the same bytes as JSON instead of image/jpeg, for
    Fable's own fetch tool, which cannot render a raw binary response. Same _token_ok call, same
    order (checked before encoding is even read) -- one auth path, not a second weaker one. The
    base64 text is built at request time from image_bytes; nothing new is stored."""
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
    if encoding.lower() == "base64":
        return {"id": capture_id, "route": route, "theme": theme, "viewport": viewport,
                "mime": mime, "data_base64": base64.b64encode(bytes(data)).decode("ascii")}
    return Response(content=bytes(data), media_type=mime,
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

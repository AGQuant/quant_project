"""visual_audit_endpoints.py — cc#2007 item 12: a read endpoint returning only FAILURES for the
latest visual-audit run, grouped by route, with the capture reference so Fable and the founder can
look at the specific image. This is a normal lightweight DB-read endpoint in the main web app --
the do_not_touch restriction on running "inside the web process" is about the Playwright CRAWLER
(visual_audit.py, its own separate job/service), not this reporting endpoint. See that file's own
module docstring for the full card context.
"""

from fastapi import APIRouter, HTTPException

import deriv_metrics  # reuses the one shared _conn() helper, same convention as option_chain_grid.py

router = APIRouter()


def _conn():
    return deriv_metrics._conn()


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

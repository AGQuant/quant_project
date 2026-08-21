"""
qsr_endpoints.py — read surfaces for the QSR engine, plus the admin run triggers.

Mounted in main.py via app.include_router(qsr_router). main.py stays wiring only (rule 4): every
line of logic in this feature lives in qsr_engine.py or here, never there.

READ ROUTES ARE UNAUTHENTICATED AND DISPLAY-ONLY, the convention the V10 dashboard reads follow.
RUN ROUTES REQUIRE THE ADMIN TOKEN, because they move a paper book.
"""

import os
from typing import Optional

import psycopg
from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/qsr", tags=["qsr"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token: Optional[str]):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/funnel")
def qsr_funnel(days: int = 30):
    """The ledger. This is the route that answers 'why did the engine take nothing last night',
    which is the entire reason 27980 made the funnel non-negotiable — so it leads the file."""
    n = max(1, min(int(days or 30), 365))
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT scan_date, scan_ts, universe, s_quality, s_sector, s_returns,
                                  s_location, s_volume, qualified, entered, fails, notes
                           FROM qsr_funnel_daily ORDER BY scan_date DESC LIMIT %s""", (n,))
            rows = _rows(cur)
        return {"days": n, "count": len(rows), "funnel": rows,
                "stage_order": ["universe", "s_quality", "s_sector", "s_returns",
                                "s_location", "s_volume", "qualified", "entered"],
                # An empty list here means the engine has never run, which is a DIFFERENT fact from
                # a run that qualified nobody — and the two must never look alike on a surface.
                "never_run": not rows}
    except Exception as e:
        raise HTTPException(500, f"qsr_funnel failed: {e}")


@router.get("/positions")
def qsr_positions():
    """Open book with live unrealised P&L against the latest close."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT p.symbol, p.entry_date, p.entry_ts, p.entry_price, p.qty, p.notional,
                       p.stop_price, p.gvm_at_entry, p.dgvm_90d, p.segment, p.gates,
                       lp.close AS cmp, lp.price_date AS cmp_date,
                       g.gvm_score AS gvm_now
                FROM qsr_positions p
                LEFT JOIN LATERAL (
                    SELECT close, price_date FROM raw_prices r
                    WHERE r.symbol = p.symbol AND r.close IS NOT NULL
                    ORDER BY r.price_date DESC LIMIT 1) lp ON TRUE
                LEFT JOIN gvm_scores g ON g.symbol = p.symbol
                     AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
                WHERE p.status = 'OPEN'
                ORDER BY p.entry_date DESC, p.symbol
            """)
            rows = _rows(cur)
        total = 0.0
        priced = 0
        for r in rows:
            if r["cmp"] is not None and r["entry_price"]:
                pnl = (float(r["cmp"]) - float(r["entry_price"])) * int(r["qty"])
                r["unrealised"] = round(pnl, 2)
                r["unrealised_pct"] = round((float(r["cmp"]) / float(r["entry_price"]) - 1) * 100, 2)
                total += pnl
                priced += 1
            else:
                # NEVER a zero. An unpriced position has no P&L, and printing 0.00 would read as
                # flat rather than as unknown.
                r["unrealised"] = None
                r["unrealised_pct"] = None
        return {"open": len(rows), "positions": rows,
                "unrealised_total": round(total, 2) if priced else None,
                "priced": priced, "unpriced": len(rows) - priced,
                "max_open": 15}
    except Exception as e:
        raise HTTPException(500, f"qsr_positions failed: {e}")


@router.get("/trades")
def qsr_trades(limit: int = 200):
    n = max(1, min(int(limit or 200), 1000))
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT symbol, entry_date, entry_price, qty, exit_date, exit_price,
                                  exit_reason, held_sessions, pnl, pnl_pct, gvm_at_entry,
                                  gvm_at_exit, dgvm_90d, segment
                           FROM qsr_trades ORDER BY exit_date DESC NULLS LAST, id DESC
                           LIMIT %s""", (n,))
            rows = _rows(cur)
        decided = [t for t in rows if t["pnl"] is not None]
        wins = sum(1 for t in decided if float(t["pnl"]) > 0)
        return {"count": len(rows), "trades": rows,
                "summary": {
                    "closed": len(decided), "wins": wins, "losses": len(decided) - wins,
                    # A win rate on zero decided trades is not 0% — it does not exist yet.
                    "win_rate": (round(100.0 * wins / len(decided), 1) if decided else None),
                    "net_pnl": (round(sum(float(t["pnl"]) for t in decided), 2) if decided else None),
                }}
    except Exception as e:
        raise HTTPException(500, f"qsr_trades failed: {e}")


@router.get("/preview")
def qsr_preview():
    """Run the scan and return what it WOULD do, writing nothing to the book.

    This exists so the funnel can be inspected before the first live fire without opening a
    position — the ENGINE_LIVENESS_RULE wants first-run evidence, and evidence gathered by
    accidentally trading is not the kind anyone wants.
    """
    import qsr_engine
    try:
        f = qsr_engine.scan()
        return {"scan_ts": str(f["scan_ts"]), "universe": f["universe"], "stages": f["stages"],
                "qualified": f["qualified"], "nifty_week": f["nifty_week"],
                "segments_with_average": f["segments_with_average"],
                "fails_sample": {k: v[:10] for k, v in f["fails"].items()},
                "note": "preview only — nothing written to qsr_positions or qsr_funnel_daily"}
    except Exception as e:
        raise HTTPException(500, f"qsr_preview failed: {e}")


@router.post("/run-scan")
def qsr_run_scan(dry_run: bool = False, x_admin_token: Optional[str] = Header(None)):
    """The nightly scan. Writes the funnel ledger ALWAYS; opens positions unless dry_run."""
    _check_admin(x_admin_token)
    import qsr_engine
    try:
        return qsr_engine.run_qsr_scan(dry_run=dry_run)
    except Exception as e:
        raise HTTPException(500, f"qsr_run_scan failed: {e}")


@router.post("/run-exits")
def qsr_run_exits(x_admin_token: Optional[str] = Header(None)):
    """The nightly exit sweep: hard stop, quality break, time stop. No target by design."""
    _check_admin(x_admin_token)
    import qsr_engine
    try:
        return qsr_engine.run_qsr_exits()
    except Exception as e:
        raise HTTPException(500, f"qsr_run_exits failed: {e}")

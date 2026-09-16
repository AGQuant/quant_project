"""investment_score_eod.py -- cc#2134: Investment Score (IC V2 /10) computed EOD for the whole
active universe and persisted, one row per symbol per score_date.

The ENGINE is invest_check_v2.compute() and it is founder-locked (session_log 27979). This module
never touches it: it imports compute() and loops the universe through it, exactly as the
/api/investment-check-v2/batch endpoint does, minus that endpoint's 200-symbol cap.

UNIVERSE (registry-derived, never a list): every symbol in gvm_scores at its latest score_date.
That is invest_check_v2's own primary source -- compute() raises "not in GVM universe" for anything
else -- so it is the exact set the engine can score, and the same set the nightly GVM job just
rebuilt when this runs (chained after it in scheduler._bg_gvm, cc#2134).

SCORE_DATE = gvm_scores' latest score_date at run time: the trading date the inputs describe, not
the wall-clock date of the 01:30 IST run. A weekend or holiday therefore never produces a row of
its own -- the boundary rolls forward by construction (ENGINE_LIVENESS_RULE corollary).

ERRORS are listed, never dropped: a per-symbol failure is logged, counted, and the loop continues.
The run summary carries every failed symbol with its reason, same as the batch endpoint.
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.investment_score_eod")
router = APIRouter()

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

TABLE = "investment_check_v2_scores"

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    symbol              text        NOT NULL,
    score_date          date        NOT NULL,
    score10             numeric(5,2),
    band                text,
    gvm                 numeric,
    market_cap_cr       numeric,
    computable_weight   numeric,
    earned_weight       numeric,
    weighted            boolean,
    weight_source       text,
    excluded_components jsonb,
    components          jsonb,
    price_date          date,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, score_date)
);
CREATE INDEX IF NOT EXISTS idx_icv2_scores_date ON {TABLE} (score_date);
"""

UPSERT_SQL = f"""
INSERT INTO {TABLE}
    (symbol, score_date, score10, band, gvm, market_cap_cr, computable_weight, earned_weight,
     weighted, weight_source, excluded_components, components, price_date, created_at)
VALUES (%(symbol)s, %(score_date)s, %(score10)s, %(band)s, %(gvm)s, %(market_cap_cr)s,
        %(computable_weight)s, %(earned_weight)s, %(weighted)s, %(weight_source)s,
        %(excluded)s, %(components)s, %(price_date)s, now())
ON CONFLICT (symbol, score_date) DO UPDATE SET
    score10 = EXCLUDED.score10, band = EXCLUDED.band, gvm = EXCLUDED.gvm,
    market_cap_cr = EXCLUDED.market_cap_cr, computable_weight = EXCLUDED.computable_weight,
    earned_weight = EXCLUDED.earned_weight, weighted = EXCLUDED.weighted,
    weight_source = EXCLUDED.weight_source, excluded_components = EXCLUDED.excluded_components,
    components = EXCLUDED.components, price_date = EXCLUDED.price_date, created_at = now()
"""

_run_lock = threading.Lock()
_last_run = {"state": "idle"}


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


def universe(cur):
    """(score_date, [symbols]) -- gvm_scores at its latest score_date. Registry-derived."""
    cur.execute("SELECT MAX(score_date) FROM gvm_scores")
    d = cur.fetchone()[0]
    if d is None:
        return None, []
    cur.execute("SELECT symbol FROM gvm_scores WHERE score_date = %s ORDER BY symbol", (d,))
    return d, [r[0] for r in cur.fetchall()]


def _row(sym, d, score_date):
    return {
        "symbol": sym, "score_date": score_date,
        "score10": d.get("score10"), "band": d.get("band"), "gvm": d.get("gvm"),
        "market_cap_cr": d.get("market_cap_cr"),
        "computable_weight": d.get("computable_weight"), "earned_weight": d.get("earned_weight"),
        "weighted": d.get("weighted"), "weight_source": d.get("weight_source"),
        "excluded": Json(d.get("excluded_components") or []),
        "components": Json(d.get("components") or []),
        "price_date": (d.get("as_of") or {}).get("price_date"),
    }


def run(conn=None, limit: Optional[int] = None):
    """Score the whole universe and persist. Returns the run summary (never raises on a symbol)."""
    import invest_check_v2
    own = conn is None
    conn = conn or _conn()
    t0 = time.time()
    errors, n_ok = [], 0
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            conn.commit()
            score_date, syms = universe(cur)
            if limit:
                syms = syms[:limit]
            if not score_date or not syms:
                return {"status": "empty", "note": "gvm_scores has no rows", "scored": 0, "failed": 0}
            for sym in syms:
                try:
                    d = invest_check_v2.compute(cur, sym)
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    cur.execute(UPSERT_SQL, _row(sym, d, score_date))
                    conn.commit()
                    n_ok += 1
                except Exception as e:
                    conn.rollback()
                    errors.append({"symbol": sym, "error": f"{type(e).__name__}: {str(e)[:120]}"})
        for err in errors:
            log.warning("investment_score_eod %s: %s", err["symbol"], err["error"])
        return {"status": "ok", "score_date": str(score_date), "universe": len(syms),
                "scored": n_ok, "failed": len(errors), "errors": errors,
                "duration_s": round(time.time() - t0, 1)}
    finally:
        if own:
            conn.close()


def latest(cur):
    cur.execute(f"SELECT MAX(score_date) FROM {TABLE}")
    return cur.fetchone()[0]


@router.get("/api/investment-score/status")
def investment_score_status():
    """Latest persisted score_date, row and band counts, and the last in-process run summary."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT to_regclass('public.{TABLE}')")
        if cur.fetchone()[0] is None:
            return {"status": "no_table"}
        d = latest(cur)
        if d is None:
            return {"status": "empty", "last_run": _last_run}
        cur.execute(f"SELECT band, COUNT(*) FROM {TABLE} WHERE score_date = %s GROUP BY band", (d,))
        bands = {(k or "null"): v for k, v in cur.fetchall()}
        cur.execute(f"SELECT COUNT(DISTINCT score_date) FROM {TABLE}")
        n_dates = cur.fetchone()[0]
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        gvm_d = cur.fetchone()[0]
    return {"status": "ok", "score_date": str(d), "rows": sum(bands.values()), "bands": bands,
            "dates_stored": n_dates, "gvm_score_date": str(gvm_d),
            "behind_gvm": bool(gvm_d and gvm_d > d), "last_run": _last_run}


def _bg_run():
    global _last_run
    _last_run = {"state": "running", "started_at": datetime.utcnow().isoformat() + "Z"}
    try:
        res = run()
        _last_run = {"state": "done", **{k: v for k, v in res.items() if k != "errors"},
                     "error_symbols": [e["symbol"] for e in res.get("errors", [])][:50]}
    except Exception as e:
        log.error("investment_score_eod run failed: %s", e, exc_info=True)
        _last_run = {"state": "error", "error": str(e)[:300]}
    finally:
        _run_lock.release()


@router.post("/api/admin/run_investment_score_eod")
def run_investment_score_eod_endpoint(x_admin_token: Optional[str] = Header(None)):
    """Manual trigger (admin token). Runs in a background thread -- the full universe takes
    minutes, longer than a request may live. Poll /api/investment-score/status for the outcome."""
    _check_admin(x_admin_token)
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running", "last_run": _last_run}
    threading.Thread(target=_bg_run, name="investment_score_eod", daemon=True).start()
    return {"status": "started", "poll": "/api/investment-score/status"}

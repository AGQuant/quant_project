"""screener_expectations.py -- cc#1865 SCREENER EXPECTATIONS SNAPSHOT + V-button endpoint
(founder 09-Sep-2026, GULFOILLUB V-sheet screenshot; gated on cc#1864 sha a54b2949, cleared).

THE GAP THIS FIXES (session_log 41385, restated on this card):
    screener_raw is CLEAN-REPLACE -- verified: SELECT count(DISTINCT loaded_at::date) FROM
    screener_raw returns 1, every CSV upload destroys the prior expectations. Screener's own
    forward-looking numbers (expected_qtr_sales, expected_quarterly_net_profit,
    expected_quarterly_eps) were therefore never STORED before a result landed, so no source can
    ever be scored on accuracy after the fact. trendlyne_estimates already solved this on the
    Trendlyne side (append-only per capture_batch); screener_raw had no equivalent until this
    module. This module does NOT touch screener_raw's own clean-replace semantics or any column
    it writes -- it is a pure side-channel, append-only history built from the SAME load.

WHICH LOADER ACTUALLY WRITES expected_qtr_sales -- confirmed by reading the code, not the
    module name in this card's own text: screener_loader.py's SCREENER_COLUMNS has NO
    expected-quarterly mapping at all (grepped, zero hits) -- that module is not the live path.
    The REAL loader is gvm_nightly.py's _sql_clean_replace_screener_v2(), invoked from
    POST /api/admin/load_screener_from_drive (admin_data.py:84-96) on every real screener CSV
    upload. Its OWN SCREENER_COLUMNS dict (gvm_nightly.py:70-94) maps "Expected quarterly
    sales"/"Expected Quarterly Sales" -> expected_qtr_sales, but NOT a profit column that
    actually gets used -- the CSV's real profit/EPS headers slug dynamically (cc#804's "make the
    table match the file") to expected_quarterly_net_profit and expected_quarterly_eps, which are
    the columns actually populated in screener_raw today (confirmed via information_schema and
    matches results_endpoints.py's own cc#1191 fix note: "expected_qtr_profit DOES NOT EXIST in
    screener_raw. The real column is expected_quarterly_net_profit"). This module snapshots the
    REAL, populated column names, not the dead SCREENER_COLUMNS alias.

WHAT THIS DOES NOT DO (do_not_touch, card cc#1865):
    Does not touch trendlyne_estimates / trendlyne_symbol_map (read-only here). Does not touch
    input_raw.fy27_growth values -- displays them only; the FY27 reconciliation that rewrites
    them is separate and founder-gated. Does not touch gvm_scores computation. Does not change
    what the live loader writes to screener_raw -- snapshot_expectations() is called AFTER the
    clean-replace commits, using the same already-built DataFrame, as a pure additional write.
"""
import logging
from datetime import date
from typing import Optional, Dict, Any, List

import psycopg
from fastapi import APIRouter, HTTPException, Header

import os

log = logging.getLogger("scorr.screener_expectations")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
router = APIRouter(tags=["screener-expectations"])

_SNAPSHOT_COLS = ("nse_code", "expected_qtr_sales", "expected_quarterly_net_profit",
                  "expected_quarterly_eps", "last_result_quarter", "last_result_date",
                  "pe", "historical_pe", "segment_pe")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _f(v):
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").strip()
        if not s or s.lower() in ("nan", "none", "-"):
            return None
        f = float(s)
        return None if (f != f) else f   # NaN check without importing math/numpy here
    except Exception:
        return None


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS screener_expectations_history (
        id BIGSERIAL PRIMARY KEY,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        load_batch TEXT NOT NULL,
        nse_code TEXT NOT NULL,
        expected_qtr_sales NUMERIC,
        expected_quarterly_net_profit NUMERIC,
        expected_quarterly_eps NUMERIC,
        last_result_quarter TEXT,
        last_result_date DATE,
        pe NUMERIC,
        historical_pe NUMERIC,
        segment_pe NUMERIC,
        fy27_growth_at_capture NUMERIC,
        source_note TEXT,
        UNIQUE (load_batch, nse_code))""")


def _write_rows(rows: List[tuple]) -> dict:
    if not rows:
        return {"ok": True, "rows_written": 0}
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.executemany("""INSERT INTO screener_expectations_history
                (load_batch, nse_code, expected_qtr_sales, expected_quarterly_net_profit,
                 expected_quarterly_eps, last_result_quarter, last_result_date, pe, historical_pe,
                 segment_pe, fy27_growth_at_capture, source_note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (load_batch, nse_code) DO NOTHING""", rows)
            conn.commit()
        return {"ok": True, "rows_written": len(rows), "load_batch": rows[0][0]}
    except Exception as e:
        log.error(f"screener_expectations write failed: {e}")
        return {"ok": False, "error": str(e)}


def snapshot_expectations(df, load_batch: str, source_note: str = "screener_csv_upload") -> dict:
    """Append-only snapshot, one row per (load_batch, nse_code). Called from gvm_nightly.
    _sql_clean_replace_screener_v2() right after a real screener CSV upload's clean-replace
    commits, using the SAME final df (post-slug, post-typing) that was just written to
    screener_raw -- never re-derives the columns a second way. Best-effort: a snapshot failure
    must never fail or roll back the screener_raw load itself (caller wraps this in try/except)."""
    if "nse_code" not in df.columns:
        return {"ok": False, "error": "no nse_code column"}

    fy27_map: Dict[str, Any] = {}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT nse_code, fy27_growth FROM input_raw")
            fy27_map = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        log.warning(f"screener_expectations: fy27_growth lookup failed (capturing without it): {e}")

    rows = []
    for _, r in df.iterrows():
        nse = r.get("nse_code")
        if not nse:
            continue
        nse = str(nse).strip()
        rows.append((
            load_batch, nse,
            _f(r.get("expected_qtr_sales")) if "expected_qtr_sales" in df.columns else None,
            _f(r.get("expected_quarterly_net_profit")) if "expected_quarterly_net_profit" in df.columns else None,
            _f(r.get("expected_quarterly_eps")) if "expected_quarterly_eps" in df.columns else None,
            (r.get("last_result_quarter") or None) if "last_result_quarter" in df.columns else None,
            (r.get("last_result_date") or None) if "last_result_date" in df.columns else None,
            _f(r.get("pe")) if "pe" in df.columns else None,
            _f(r.get("historical_pe")) if "historical_pe" in df.columns else None,
            _f(r.get("segment_pe")) if "segment_pe" in df.columns else None,
            _f(fy27_map.get(nse)),
            source_note,
        ))
    return _write_rows(rows)


def backfill_current_batch(load_batch: str = "2026-09-08_full",
                            source_note: str = "backfill_baseline") -> dict:
    """One-off: snapshot the CURRENT screener_raw state as a baseline batch, so there is
    something on record to score the NEXT real upload against (card step 3). Reads live columns
    directly -- no CSV needed, since screener_raw already holds today's load."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute("""SELECT nse_code, expected_qtr_sales, expected_quarterly_net_profit,
                                  expected_quarterly_eps, last_result_quarter, last_result_date,
                                  pe, historical_pe, segment_pe
                           FROM screener_raw WHERE nse_code IS NOT NULL""")
            screener_rows = cur.fetchall()
            cur.execute("SELECT nse_code, fy27_growth FROM input_raw")
            fy27_map = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    rows = [(load_batch, nse, _f(eqs), _f(eqnp), _f(eeps), lrq, lrd, _f(pe), _f(hpe), _f(spe),
             _f(fy27_map.get(nse)), source_note)
            for (nse, eqs, eqnp, eeps, lrq, lrd, pe, hpe, spe) in screener_rows]
    return _write_rows(rows)


# ── V-button endpoint: Section A (Valuation & Potential Upside) + Section B (Growth Outlook) ────
@router.get("/api/gvm/expectations/{symbol}")
def gvm_expectations(symbol: str):
    sym = symbol.strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT upside_raw FROM gvm_scores WHERE symbol=%s
                       ORDER BY score_date DESC LIMIT 1""", (sym,))
        row = cur.fetchone()
        upside = _f(row[0]) if row else None

        cur.execute('SELECT pe, historical_pe, segment_pe FROM screener_raw WHERE nse_code=%s', (sym,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, f"{sym} not found in screener_raw")
        pe, hist_pe, seg_pe = row

        cur.execute("SELECT fy27_growth, gvm_segment, fy27_revenue_est, revenue_est_updated "
                     "FROM input_raw WHERE nse_code=%s", (sym,))
        row = cur.fetchone()
        fy27, segment, fy27_rev_est, rev_est_updated = row or (None, None, None, None)

        # cc#1864 canon: forward PE = pe / (1 + fy27_growth/100), the SAME formula
        # sector_brief_endpoints.py already uses -- not re-derived a second way.
        fwd_pe = None
        if pe is not None and fy27 is not None and float(fy27) > -100:
            fwd_pe = round(float(pe) / (1 + float(fy27) / 100.0), 1)

        # cc#1864/cc#1866 finding, carried here rather than re-litigated: screener_raw.segment_pe
        # is 100pct NULL universe-wide -- "PE vs sector" is a context box sharing the live peer-
        # computed median with "PE now" elsewhere on the page; this endpoint reports the raw
        # segment_pe column honestly (absent for everyone) rather than re-deriving that peer
        # median a second way here.
        section_a = {
            "pe_now": {"value": _f(pe)},
            "pe_next_year": {"value": fwd_pe, "basis": "pe / (1 + fy27_growth/100)"},
            "upside_to_fair_value": {
                "value": upside,
                "basis": "gvm_scores.upside_raw (fy27_growth * pe/historical_pe, cc#1864 canon)",
            },
            "pe_vs_own_5y_avg": {"value": _f(hist_pe)},
            "pe_vs_sector": {
                "value": _f(seg_pe),
                "available": seg_pe is not None,
                "note": ("screener_raw.segment_pe is not populated for this symbol (universe-wide "
                         "gap, cc#1864/1866) -- absent, not zero") if seg_pe is None else None,
            },
        }

        # ── Section B: Growth Outlook -- QUARTER / YEAR / SECTOR ───────────────────────────────
        cur.execute('SELECT expected_qtr_sales FROM screener_raw WHERE nse_code=%s', (sym,))
        row = cur.fetchone()
        exp_qtr_sales = _f(row[0]) if row else None

        cur.execute("""SELECT metrics->>'Sales' FROM fundamentals_history
                       WHERE symbol=%s AND period_label='Sep 2025' AND section='quarters'
                       ORDER BY consolidated DESC LIMIT 1""", (sym,))
        row = cur.fetchone()
        sep25_sales = _f(row[0]) if row else None

        if exp_qtr_sales is not None and sep25_sales:
            quarter_row = {
                "available": True,
                "yoy_pct": round((exp_qtr_sales - sep25_sales) / sep25_sales * 100, 1),
                "expected_qtr_sales": exp_qtr_sales, "base_sep_2025_sales": sep25_sales,
                "basis": "Q2FY27 expected revenue vs Sep-2025 actual (fundamentals_history), "
                         "NEVER screener_raw.sales_preceding_year_quarter (wrong denominator)",
            }
        else:
            quarter_row = {"available": False,
                            "reason": "expected_qtr_sales or Sep-2025 base missing for this symbol"}

        year_row: Dict[str, Any] = {
            "primary": _f(fy27), "primary_source": "input_raw.fy27_growth",
        }
        if fy27_rev_est is not None:
            year_row["second_opinion"] = _f(fy27_rev_est)
            year_row["second_opinion_source"] = f"Trendlyne (as of {rev_est_updated})" if rev_est_updated else "Trendlyne"

        sector_row: Dict[str, Any]
        if segment:
            cur.execute("SELECT fy27_growth FROM input_raw WHERE gvm_segment=%s AND fy27_growth IS NOT NULL",
                        (segment,))
            vals = sorted(_f(r[0]) for r in cur.fetchall() if _f(r[0]) is not None)
            if len(vals) >= 3:
                n = len(vals)
                median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
                sector_row = {"available": True, "segment": segment, "median": round(median, 1),
                              "constituents": n, "company": _f(fy27)}
            else:
                sector_row = {"available": False,
                              "reason": f"fewer than 3 segment constituents with a value ({len(vals)})"}
        else:
            sector_row = {"available": False, "reason": "no segment on record for this symbol"}

        section_b = {"quarter": quarter_row, "year": year_row, "sector": sector_row}

    return {"symbol": sym, "section_a": section_a, "section_b": section_b}


@router.post("/api/admin/screener_expectations/backfill")
def admin_backfill(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    return backfill_current_batch()

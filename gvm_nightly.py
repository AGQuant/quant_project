"""
GVM Nightly Recompute - Scorr
===============================
Self-contained FastAPI router. Server-side GVM recompute driven entirely by
the LIVE DB tables (no CSV on disk).

GVM = G + V + M, where:
  G (Growth)  + V (Value)  -> from screener_raw  (weekly upload; fundamentals)
  M (Momentum)             -> from momentum_scores (DAILY; price-driven)

This split is the core of the GVM TREND:
  - Fundamentals (G,V) step-change only when a new screener CSV is uploaded.
  - Momentum (M) recomputes daily from raw_prices via momentum_daily.py.
  - Combined daily snapshot -> gvm_history -> the trend line.

Workflow:
  1. POST /api/admin/load_screener_from_drive  {file_id}  -> clean-replace screener_raw (weekly)
  2. POST /api/gvm/recompute                              -> refresh momentum (daily) + score all:
         - gvm_history  (APPEND one dated row per stock; the trend table)
         - gvm_scores   (REPLACE latest snapshot; canonical read table)
         - sector_ratings (REPLACE; mcap-weighted segment GVM — wired daily)

Verdict framework (Arpit):
  >= 8.0  -> Excellent
  7.0-8.0 -> Good
  6.0-7.0 -> Average
  < 6.0   -> Weak
"""

import os
import re                              # cc#804: header slugging for the dynamic wide loader
import json                            # cc#828: column-drop guard writes a jsonb ops_log alert
import logging
from datetime import date, timedelta   # cc#779: timedelta for the trend-verdict window
from datetime import date as _date     # cc#804: explicit alias — `date` is shadowed by params below
from typing import Optional, Dict, List

import psycopg
from psycopg.rows import dict_row
import pandas as pd
import numpy as np
from fastapi import APIRouter, HTTPException, Request, Header

from gvm_engine import api_g_score, api_v_score
import momentum_daily

log = logging.getLogger("scorr.gvm_nightly")

router = APIRouter(tags=["gvm-nightly"])

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def _conn():
    return psycopg.connect(DATABASE_URL)


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


# ============================================================
# SCREENER COLUMN MAPPING (raw Screener header -> live screener_raw col)
# ============================================================
SCREENER_COLUMNS = {
    "Current Price": "price", "Sales growth 5Years": "sales_growth_5y",
    "Sales growth 3Years": "sales_growth_3y", "Profit growth 5Years": "profit_growth_5y",
    "Profit growth 3Years": "profit_growth_3y", "YOY Quarterly sales growth": "qoq_sales_growth",
    "YOY Quarterly profit growth": "qoq_profit_growth", "OPM": "opm",
    "OPM latest quarter": "opm_latest_q", "OPM preceding year quarter": "opm_prev_year_q",
    "Fixed Asset Growth": "fixed_asset_growth", "FII holding": "fii_holding",
    "DII holding": "dii_holding", "Change in FII holding": "fii_change",
    "Change in DII holding": "dii_change", "Return on capital employed": "roce",
    "Interest Coverage Ratio": "interest_coverage", "Dividend yield": "dividend_yield",
    "Price to Earning": "pe", "Historical PE 10Years": "historical_pe",
    "Industry PE": "segment_pe", "Return over 1year": "return_1y",
    "Return over 3years": "return_3y", "DMA 50": "dma_50", "DMA 200": "dma_200",
    "52w Index": "return_52w_vs_index", "Market Capitalization": "market_cap",
    "Industry Group": "industry_group",
    # cc#796: Screener's EXPECTED quarterly sales/profit. This is a mechanical trend projection, NOT
    # analyst consensus — the season measured a median deviation of -16% (155 beats / 320 misses).
    # Every surface must call it a projected run-rate, never "analyst estimates".
    # Several header spellings are mapped because the export has used more than one; _norm_screener_cols
    # below also catches case/spacing variants so a renamed header cannot silently drop the column.
    "Expected quarterly sales": "expected_qtr_sales",
    "Expected quarterly profit": "expected_qtr_profit",
    "Expected Quarterly Sales": "expected_qtr_sales",
    "Expected Quarterly Profit": "expected_qtr_profit",
}


def _norm_screener_cols(df):
    """cc#796: map any header that IS the expected-quarterly sales/profit column regardless of case or
    spacing. A fixed-string mapping alone would silently drop the column on a rename, and a silently
    missing expectation is indistinguishable on the card from a company that genuinely has none."""
    ren = {}
    for c in df.columns:
        k = " ".join(str(c).lower().split())
        if k in ("expected quarterly sales", "expected quarterly sale"):
            ren[c] = "expected_qtr_sales"
        elif k in ("expected quarterly profit", "expected quarterly pat"):
            ren[c] = "expected_qtr_profit"
    return df.rename(columns=ren) if ren else df

SCREENER_LIVE_COLS = [
    "company_name", "BSE Code", "nse_code", "ISIN Code", "industry_group", "Industry",
    "price", "market_cap", "pe", "historical_pe", "segment_pe", "Price to book value",
    "dividend_yield", "opm", "opm_latest_q", "opm_prev_year_q", "Debt to equity",
    "Operating profit growth", "interest_coverage", "roce", "Return on equity",
    "Promoter holding", "Unpledged promoter holding", "return_52w_vs_index", "fii_change",
    "dii_change", "Debt", "Working capital preceding year", "fii_holding", "dii_holding",
    "High price", "Sales growth", "Sales", "Profit after tax", "Enterprise Value", "EVEBITDA",
    "qoq_sales_growth", "qoq_profit_growth", "profit_growth_3y", "EPS last year",
    "EPS growth 5Years", "sales_growth_3y", "sales_growth_5y", "EPS", "Cfo by Pat",
    "PEG Ratio", "Average 5years dividend", "NPM last year", "No of Shareholder",
    "EPS growth 3Years", "EPS growth 7Years", "EPS growth 10Years", "profit_growth_5y",
    "return_1y", "return_3y", "dma_50", "dma_200", "RSI", "Number of equity shares",
    "fixed_asset_growth", "Return over 1month",
    # cc#796 — ingested only once the columns exist in screener_raw (they do not yet; the migration is
    # in the cc#796 task result). The loader intersects this list with the DataFrame's columns, so
    # naming them here ahead of the migration is inert rather than breaking.
    "expected_qtr_sales", "expected_qtr_profit",
]
SCREENER_TEXT_COLS = {"company_name", "BSE Code", "nse_code", "ISIN Code", "industry_group", "Industry"}

BFSI_SEGMENTS = {
    "PSU Banks", "Private Banks", "Small Finance Banks", "NBFC - Large",
    "MSME Finance - Large", "MSME Finance - Mid", "MSME Finance - Small",
    "Microfinance & MSME", "Housing Finance", "Life Insurance",
    "General & Health Insurance", "Capital Markets - Large",
    "Broking & Wealth Management", "Exchanges & Ratings - Mid", "REITs",
    "Holding Companies",
}

# G + V peer params only (M now comes from momentum_scores).
# cc#506: "pe" added -- feeds the LIVE segment MEDIAN PE that replaces the stale/mean-based
# screener_raw.segment_pe ("Industry PE") as score_pe's segment benchmark. See _stock_dict().
PEER_PARAMS = [
    "sales_growth_5y", "sales_growth_3y", "profit_growth_5y", "profit_growth_3y",
    "qoq_sales_growth", "qoq_profit_growth", "opm", "opm_expansion", "fixed_asset_growth",
    "inst_holding_abs", "inst_holding_change", "roce", "interest_coverage",
    "dividend_yield", "potential_upside", "pe",
]


# ============================================================
# LABELS + VERDICT + PUNCHLINE
# Verdict framework (Arpit):
#   >= 8.0  -> Excellent
#   7.0-8.0 -> Good
#   6.0-7.0 -> Average
#   < 6.0   -> Weak
# ============================================================
def _label_growth(s):
    return "Excellent" if s >= 8 else "Healthy" if s >= 6.5 else "Average" if s >= 5 else "Weak"

def _label_value(s):
    return "Attractive" if s >= 7.5 else "Fair" if s >= 6 else "Premium" if s >= 4.5 else "Expensive"

def _label_momentum(s):
    return "Strong" if s >= 8 else "Positive" if s >= 6 else "Neutral" if s >= 4 else "Weak"

def _label_gvm(s):
    return ("Excellent" if s >= 8 else "Good" if s >= 7 else "Average" if s >= 6 else "Poor")

def _verdict(s):
    return ("Excellent" if s >= 8 else "Good" if s >= 7 else "Average" if s >= 6 else "Weak")

def _punchline(verd, g_lbl, v_lbl, m_lbl, gvm_lbl):
    action = {
        "Excellent": "It is highly recommended to Buy",
        "Good":      "It is recommended to Buy",
        "Average":   "It is advisable to Watch and Accumulate on dips",
        "Weak":      "It is advisable to Exit or Avoid",
    }[verd]
    return (f"{action} for medium to long term perspective, considering its "
            f"{g_lbl} Growth, {v_lbl} Valuation, {m_lbl} Momentum & {gvm_lbl} overall GVM Rating.")


# ============================================================
# LOAD: clean-replace screener_raw from uploaded rows (raw headers)
# ============================================================
def _screener_slug(col: str) -> str:
    """cc#804: a stable, quotable Postgres identifier for an ARBITRARY CSV header.

    Canonical names (nse_code, market_cap, ...) pass through untouched so every downstream reader
    keeps working; anything else becomes lower_snake_case, trimmed to 63 chars (Postgres' identifier
    limit — a longer name would be silently truncated by the server and then never match on the next
    load)."""
    c = str(col).strip()
    if not c:
        return ""
    if c in SCREENER_TEXT_COLS or c in set(SCREENER_COLUMNS.values()):
        return c
    s = re.sub(r"[^0-9a-zA-Z]+", "_", c).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if s and s[0].isdigit():
        s = "c_" + s
    return s[:63]


def _parse_yyyymm(v):
    """cc#804 item 4: "Last result date" arrives as a YYYYMM NUMBER (202606.00 = Jun 2026 = Q1FY27).
    Returns (date_of_month_start, 'Q1FY27') or (None, None). Read as a float first because pandas
    types the column as numeric and 202606.00 must not become the string '202606.0'."""
    try:
        n = int(float(v))
    except Exception:
        return None, None
    y, m = n // 100, n % 100
    if not (1990 <= y <= 2100 and 1 <= m <= 12):
        return None, None
    # Indian FY runs Apr-Mar: Apr-Jun = Q1 of FY(y+1), Jan-Mar = Q4 of FY(y).
    q = (m - 1) // 3 + 1          # calendar quarter
    fq = {1: 4, 2: 1, 3: 2, 4: 3}[q]
    fy = y + 1 if m >= 4 else y
    return _date(y, m, 1), f"Q{fq}FY{str(fy)[-2:]}"


def _sql_clean_replace_screener(rows: List[dict]) -> int:
    res = _sql_clean_replace_screener_v2(rows)
    return res["rows_loaded"]


def _sql_clean_replace_screener_v2(rows: List[dict]) -> dict:
    """cc#804: DYNAMIC WIDE loader. Returns a diagnostics dict; the thin wrapper above preserves the
    old int-returning contract for existing callers.

    ROOT CAUSE this fixes. SCREENER_LIVE_COLS was a fixed ALLOWLIST and the insert was
    `[c for c in SCREENER_LIVE_COLS if c in df.columns and c in _live]`. Both filters are subtractive,
    so the loader could only ever write columns that were ALREADY in the list AND already in the
    table — it had no path by which a new CSV header could become a stored column. That is why the
    founder's 44-header file loaded 1,801 rows and produced ZERO new columns while reporting
    {"status":"ok"}: nothing failed, the extra headers were simply dropped on the floor in silence.
    The cc#790 claim that "the loader schema was updated" was wrong about THIS path.

    The schema is now derived from the CSV header on every load: unknown headers get a slugged
    column added to screener_raw (numeric unless the values are non-numeric), and every header in
    the file is stored. Adding a nullable column with no default is a catalogue-only change in
    Postgres — no table rewrite — and it runs under a short lock_timeout so it can never wedge
    behind an idle transaction the way the 10-Jul REINDEX did (MAINTENANCE_LOCK_RULE, cc#351).
    """
    df = pd.DataFrame(rows)
    rows_in_file = len(df)
    df = df.rename(columns={"NSE Code": "nse_code", "Name": "company_name",
                            "Current Price": "price"})   # cc#804: the new export's identity casing
    df = df.rename(columns=SCREENER_COLUMNS)
    df = _norm_screener_cols(df)   # cc#796: case/spacing-tolerant expected-column mapping

    # ── row accounting (cc#804 item 2): every dropped row is counted and reported, never silent.
    if "nse_code" not in df.columns:
        raise ValueError("load_screener: CSV has no NSE Code / nse_code column")
    _n0 = len(df)
    df = df[df["nse_code"].notna()].copy()
    df["nse_code"] = df["nse_code"].astype(str).str.strip()
    df = df[~df["nse_code"].isin(["", "nan", "NaN", "None", "-"])].copy()
    dropped_no_nse = _n0 - len(df)          # BSE-only rows: legitimate, kept as a filter, now logged
    _n1 = len(df)
    df = df.drop_duplicates(subset="nse_code", keep="first").reset_index(drop=True)
    dropped_dupe = _n1 - len(df)

    # ── cc#804 item 4: "Last result date" YYYYMM -> real date + quarter label
    result_quarter = None
    _lrd = next((c for c in df.columns
                 if " ".join(str(c).lower().split()) in ("last result date", "last_result_date")), None)
    if _lrd is not None:
        parsed = df[_lrd].map(lambda v: _parse_yyyymm(v))
        df["last_result_date"] = [p[0] for p in parsed]
        df["last_result_quarter"] = [p[1] for p in parsed]
        if _lrd not in ("last_result_date",):
            df = df.drop(columns=[_lrd])
        try:
            result_quarter = df["last_result_quarter"].dropna().mode().iloc[0]
        except Exception:
            result_quarter = None

    # ── typing: text columns stay text, everything else numeric-coerced
    _text_cols = set(SCREENER_TEXT_COLS) | {"last_result_quarter"}
    _date_cols = {"last_result_date"}
    for c in df.columns:
        if c not in _text_cols and c not in _date_cols:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # ── cc#804 item 3: QoQ growth from the preceding-quarter ABSOLUTES in the file, not from a
    # pre-computed chip. Verified on 20MICRONS: sales 244.72 latest vs 261.06 preceding -> -6.26%.
    def _qoq(latest_names, prev_names, out_col):
        lc = next((c for c in df.columns if c in latest_names), None)
        pc = next((c for c in df.columns if c in prev_names), None)
        if lc is None or pc is None:
            return False
        latest, prev = pd.to_numeric(df[lc], errors="coerce"), pd.to_numeric(df[pc], errors="coerce")
        # Guard the sign flip: a swing THROUGH zero (loss -> profit) has no meaningful percentage,
        # and dividing by a negative base silently inverts the direction of the move.
        df[out_col] = np.where((prev.notna()) & (latest.notna()) & (prev > 0),
                               (latest - prev) / prev * 100.0, np.nan)
        return True

    # cc#804 fix (found on the first real load): the profit pair slugs to
    # profit_after_tax_latest_quarter / profit_after_tax_preceding_quarter, NOT profit_latest_quarter.
    # The narrower name set never matched, so qoq_profit_growth silently kept the CSV's OWN
    # precomputed column — which is a different basis. Measured on the 02-Aug load: 1,569 of 1,801
    # rows carried a qoq_profit_growth contradicting the absolutes in their own row by >1pp, while
    # qoq_sales_growth (whose names did match) was exact. Both column-name families are listed now,
    # and the loader reports qoq_*_computed so a silent miss is visible in the response instead of
    # being discovered a season later.
    qoq_sales_ok = _qoq({"Sales latest quarter", "sales_latest_quarter"},
                        {"Sales preceding quarter", "sales_preceding_quarter"}, "qoq_sales_growth")
    qoq_profit_ok = _qoq({"Profit latest quarter", "profit_latest_quarter",
                          "Profit after tax latest quarter", "profit_after_tax_latest_quarter"},
                         {"Profit preceding quarter", "profit_preceding_quarter",
                          "Profit after tax preceding quarter", "profit_after_tax_preceding_quarter"},
                         "qoq_profit_growth")

    # ── cc#804: slug every header to a stable identifier, then MAKE THE TABLE MATCH THE FILE.
    # This is the actual fix. The old code intersected a fixed allowlist with the CSV columns AND
    # with the live table, so both filters could only ever REMOVE columns — a new header had no path
    # to becoming a stored column, which is precisely why a 44-header file added zero columns while
    # returning {"status":"ok"}.
    with _conn() as _c0, _c0.cursor() as _cur0:
        _cur0.execute("SELECT column_name FROM information_schema.columns WHERE table_name='screener_raw'")
        _live = {r[0] for r in _cur0.fetchall()}

    # ── cc#828 part_1 COLUMN-DROP GUARD ─────────────────────────────────────────────────────────
    # A clean-replace with a NARROWER export silently blanks every column the new file omits. On
    # 02-Aug that emptied return_1y / return_3y / return_52w_vs_index / dma_50 / dma_200 / RSI for
    # all 1,801 rows, and nothing said so: the GVM M-popout tiles just went blank and the pillar
    # quietly scored them neutral. The load reported {"status":"ok"} throughout.
    #
    # So: before replacing, diff the incoming header set against the columns that are CURRENTLY
    # well-populated. Anything previously populated and now absent is a casualty, named in the
    # response and raised to ops_log. Deliberately NOT a block — the founder may intend a narrower
    # file — but it can never again happen quietly.
    dropped_populated = []
    try:
        _num_live = [c for c in _live if c not in SCREENER_TEXT_COLS]
        if _num_live:
            _sel = ", ".join(f'COUNT("{c}") AS "{c}"' for c in _num_live)
            with _conn() as _c1, _c1.cursor() as _cur1:
                _cur1.execute(f"SELECT COUNT(*) AS _n, {_sel} FROM screener_raw")
                _row = _cur1.fetchone()
                _names = [d[0] for d in _cur1.description]
            _counts = dict(zip(_names, _row))
            _total = _counts.pop("_n", 0) or 0
            if _total:
                # Compare against BOTH the raw headers and their slugs. The rename to slugs happens
                # further down, so a stored column like return_1y would otherwise look "absent" from
                # a file whose header reads "Return 1y" — a false alarm on every single load.
                incoming = set(df.columns) | {_screener_slug(c) for c in df.columns}
                incoming.discard(None)
                incoming.discard("")
                for c, filled in _counts.items():
                    # ">90% populated" = a column the platform genuinely relies on today. A sparse
                    # column vanishing is not news; a full one vanishing is the incident.
                    if c not in incoming and (filled or 0) / _total > 0.90:
                        dropped_populated.append(c)
        dropped_populated.sort()
        if dropped_populated:
            log.error("SCREENER_LOAD_COLUMN_DROP: %d previously-populated column(s) absent from this "
                      "CSV and about to be blanked: %s", len(dropped_populated), ", ".join(dropped_populated))
            try:
                with _conn() as _c2, _c2.cursor() as _cur2:
                    _cur2.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                                     VALUES (CURRENT_DATE, NOW(), 'alert',
                                             'SCREENER_LOAD_COLUMN_DROP', %s::jsonb)""",
                                  (json.dumps({"cc": 828, "dropped": dropped_populated,
                                               "n_dropped": len(dropped_populated),
                                               "headers_in_file": len(df.columns),
                                               "note": "clean-replace will NULL these for every row"}),))
                    _c2.commit()
            except Exception as _e:
                log.warning("cc#828 ops_log alert failed (load continues): %s", _e)
    except Exception as _e:
        log.warning("cc#828 column-drop guard skipped (load continues): %s", _e)

    # An EXISTING column name wins over its slug. screener_raw already holds Title Case columns such
    # as "Price to book value", "BSE Code" and "Sales growth", and consumers reference them by that
    # exact quoted name (e.g. gvm_market_endpoints selects s."Price to book value"). Slugging those
    # would create a parallel price_to_book_value column, leave the original permanently NULL after
    # the clean-replace, and silently blank the field on every surface that reads it. Only genuinely
    # NEW headers get slugged.
    ren = {}
    for c in list(df.columns):
        if c in _live:
            continue
        s = _screener_slug(c)
        if not s:
            df = df.drop(columns=[c])
        elif s != c:
            ren[c] = s
    if ren:
        df = df.rename(columns=ren)
    df = df.loc[:, ~df.columns.duplicated()].copy()   # two headers can slug to one name; first wins

    missing = [c for c in df.columns if c not in _live]
    added = []
    if missing:
        # ADD COLUMN with no default is catalogue-only in Postgres (no rewrite). lock_timeout keeps
        # it from queueing behind an idle transaction and blocking readers — the failure mode
        # MAINTENANCE_LOCK_RULE (cc#351) exists to prevent. If the lock cannot be taken we skip the
        # column and still load everything else, rather than failing the whole ingest.
        with _conn() as _c1, _c1.cursor() as _cur1:
            _cur1.execute("SET lock_timeout = '5s'")
            for c in missing:
                if c in _date_cols:
                    typ = "DATE"
                elif c in _text_cols:
                    typ = "TEXT"
                else:
                    typ = "TEXT" if df[c].dtype == object else "NUMERIC"
                try:
                    _cur1.execute(f'ALTER TABLE screener_raw ADD COLUMN IF NOT EXISTS "{c}" {typ}')
                    _c1.commit()
                    added.append(c)
                except Exception as e:
                    _c1.rollback()
                    log.warning("load_screener: could not add column %s (%s): %s", c, typ, e)
        _live |= set(added)

    cols = [c for c in df.columns if c in _live]
    skipped = [c for c in df.columns if c not in _live]
    if skipped:
        log.warning("load_screener: %d column(s) not storable, skipped: %s", len(skipped), ", ".join(skipped))
    placeholders = ", ".join(["%s"] * len(cols))
    colnames = ", ".join('"' + c + '"' for c in cols)

    def cell(c, v):
        if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
            return None
        if c in _date_cols:
            return v
        if c in _text_cols or df[c].dtype == object:
            return str(v)
        try:
            f = float(v)
            return None if (np.isnan(f) or np.isinf(f)) else f
        except Exception:
            return None

    batch = [tuple(cell(c, r.get(c)) for c in cols) for _, r in df.iterrows()]

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM screener_raw")
        cur.executemany(f"INSERT INTO screener_raw ({colnames}) VALUES ({placeholders})", batch)
        conn.commit()

    log.info("load_screener: %d/%d rows (dropped %d no-nse_code, %d duplicate), %d columns "
             "(%d added: %s)", len(batch), rows_in_file, dropped_no_nse, dropped_dupe,
             len(cols), len(added), ", ".join(added) or "none")
    return {
        "rows_loaded": len(batch),
        "rows_in_file": rows_in_file,
        "dropped_no_nse_code": dropped_no_nse,   # BSE-only listings: an intentional filter, now visible
        "dropped_duplicate_nse_code": dropped_dupe,
        "columns_loaded": len(cols),             # cc#804 item 5: acceptance in one glance
        "columns_added": added,
        "columns_skipped": skipped,
        # cc#828 part_1: columns that WERE >90% populated and are not in this file. After the
        # clean-replace they are NULL for every row. Empty list = nothing was silently lost.
        "columns_dropped_populated": dropped_populated,
        "qoq_sales_computed": bool(qoq_sales_ok),
        "qoq_profit_computed": bool(qoq_profit_ok),
        "result_quarter": result_quarter,
    }


# ============================================================
# SECTOR RATINGS: mcap-weighted GVM per segment
# Runs after every gvm_scores update. Replaces sector_ratings table.
# ============================================================
def compute_sector_ratings(target_date: date) -> Dict:
    """
    Compute mcap-weighted GVM rating per segment from latest gvm_scores.
    Writes/replaces sector_ratings table.
    Returns: {segments, rows_written, date}
    """
    try:
        with _conn() as conn:
            df = pd.read_sql_query("""
                SELECT symbol, company_name, segment,
                       g_score, v_score, m_score, gvm_score, market_cap
                FROM gvm_scores
                WHERE gvm_score IS NOT NULL
            """, conn)

        if df.empty:
            return {"status": "warn", "message": "gvm_scores empty", "segments": 0}

        # mcap: use 1 as fallback if null (equal weight for mcap-unknown stocks)
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce").fillna(1)
        df["gvm_score"]  = pd.to_numeric(df["gvm_score"],  errors="coerce")
        df["g_score"]    = pd.to_numeric(df["g_score"],    errors="coerce")
        df["v_score"]    = pd.to_numeric(df["v_score"],    errors="coerce")
        df["m_score"]    = pd.to_numeric(df["m_score"],    errors="coerce")
        df = df.dropna(subset=["gvm_score"])

        rows = []
        for seg, grp in df.groupby("segment"):
            if seg in ("Unknown", "", None):
                continue
            total_mcap = grp["market_cap"].sum()
            if total_mcap <= 0:
                total_mcap = len(grp)  # fallback: equal weight

            def wt_avg(col):
                return round(float((grp[col] * grp["market_cap"]).sum() / total_mcap), 3)

            def simple_avg(col):
                return round(float(grp[col].mean()), 3)

            mcap_gvm = wt_avg("gvm_score")
            top_row = grp.nlargest(1, "gvm_score").iloc[0]

            rows.append((
                seg,
                int(len(grp)),
                mcap_gvm,
                wt_avg("g_score"),
                wt_avg("v_score"),
                wt_avg("m_score"),
                simple_avg("gvm_score"),
                round(float(total_mcap), 2),
                str(top_row["symbol"]),
                round(float(top_row["gvm_score"]), 2),
                _verdict(mcap_gvm),
                target_date,
            ))

        # Ensure table exists with correct schema
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sector_ratings (
                    id SERIAL PRIMARY KEY,
                    segment TEXT NOT NULL,
                    stocks_count INTEGER,
                    mcap_weighted_gvm NUMERIC,
                    weighted_g NUMERIC,
                    weighted_v NUMERIC,
                    weighted_m NUMERIC,
                    simple_avg_gvm NUMERIC,
                    total_mcap NUMERIC,
                    top_stock TEXT,
                    top_stock_gvm NUMERIC,
                    verdict TEXT,
                    score_date DATE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("DELETE FROM sector_ratings")
            cur.executemany("""
                INSERT INTO sector_ratings
                    (segment, stocks_count, mcap_weighted_gvm, weighted_g, weighted_v, weighted_m,
                     simple_avg_gvm, total_mcap, top_stock, top_stock_gvm, verdict, score_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, rows)
            conn.commit()

        log.info(f"sector_ratings: {len(rows)} segments written for {target_date}")
        return {"status": "ok", "segments": len(rows), "date": str(target_date)}

    except Exception as e:
        log.error(f"compute_sector_ratings failed: {e}")
        return {"status": "error", "message": str(e)}


# ============================================================
# RECOMPUTE: read DB -> score G+V (screener) + M (momentum_scores) -> write tables
# ============================================================
def _load_merged_df(target_date: date) -> pd.DataFrame:
    with _conn() as conn:
        inp = pd.read_sql_query(
            "SELECT nse_code, company_name, market_cap, gvm_segment, fy27_growth FROM input_raw", conn
        )
        scr = pd.read_sql_query("SELECT * FROM screener_raw", conn)
        # latest momentum on/before target date
        mom = pd.read_sql_query(
            "SELECT DISTINCT ON (symbol) symbol, m_score FROM momentum_scores "
            "WHERE score_date <= %s ORDER BY symbol, score_date DESC",
            conn, params=(target_date,),
        )

    for d in (inp, scr):
        d["nse_code"] = d["nse_code"].astype(str).str.strip()
    mom["symbol"] = mom["symbol"].astype(str).str.strip()

    df = inp.merge(scr, on="nse_code", how="inner", suffixes=("", "_scr"))
    df = df.merge(mom.rename(columns={"symbol": "nse_code", "m_score": "m_score_daily"}),
                  on="nse_code", how="left")
    df["gvm_segment"] = df["gvm_segment"].astype(str).str.strip().replace({"nan": "Unknown", "": "Unknown"})

    if "opm_latest_q" in df and "opm_prev_year_q" in df:
        df["opm_expansion"] = (df["opm_latest_q"] - df["opm_prev_year_q"]) * 100
    else:
        df["opm_expansion"] = np.nan

    if "fii_holding" in df and "dii_holding" in df:
        df["inst_holding_abs"] = df["fii_holding"].fillna(0) + df["dii_holding"].fillna(0)
        df.loc[df["fii_holding"].isna() & df["dii_holding"].isna(), "inst_holding_abs"] = np.nan
    else:
        df["inst_holding_abs"] = np.nan

    if "fii_change" in df and "dii_change" in df:
        df["inst_holding_change"] = df["fii_change"].fillna(0) + df["dii_change"].fillna(0)
        df.loc[df["fii_change"].isna() & df["dii_change"].isna(), "inst_holding_change"] = np.nan
    else:
        df["inst_holding_change"] = np.nan

    def _pu(row):
        fy27, pe, hist = row.get("fy27_growth"), row.get("pe"), row.get("historical_pe")
        if pd.isna(fy27):
            return np.nan
        if fy27 == 0:
            return 0.0
        # hist_pe from screener_raw has dirty values (0.01, 0.14) that blow up the
        # pe/hist multiplier (task #41: SKFINDUS 44399%). Only trust hist_pe in a
        # sane band; else fall back to raw fy27 growth. Cap final upside at 150%.
        mult = (pe / hist) if (pd.notna(pe) and pd.notna(hist) and 5 < hist < 500) else 1.0
        return round(min(float(fy27) * mult, 150.0), 4)

    df["potential_upside"] = df.apply(_pu, axis=1)
    return df


def _peer_averages(df: pd.DataFrame) -> Dict:
    """cc#506: MEDIAN peers (was a 10-90 percentile trimmed MEAN). Median is inherently robust
    to outliers, so the old trim-then-mean two-step collapses to a single vals.median() -- same
    goal (don't let one extreme peer skew the benchmark), simpler mechanism. Every caller of
    these values (gvm_engine.py's score_relative/param_score/score_pe/etc.) takes peer input as
    a plain scalar, so this one change is sufficient to flow medians through the whole engine."""
    out = {}
    for seg, grp in df.groupby("gvm_segment"):
        avgs = {}
        for p in PEER_PARAMS:
            if p in grp.columns:
                vals = pd.to_numeric(grp[p], errors="coerce").dropna()
                avgs[p] = round(vals.median(), 4) if len(vals) else None
        out[seg] = avgs
    return out


def _stock_dict(row, peer_avgs):
    """G + V inputs only (M comes from momentum_scores separately)."""
    seg = row.get("gvm_segment", "Unknown")
    peers = peer_avgs.get(seg, {})

    def p(c):
        v = peers.get(c)
        return float(v) if v is not None else None

    def v(c):
        val = row.get(c)
        if val is None:
            return None
        try:
            f = float(val)
            return None if pd.isna(f) else f
        except Exception:
            return None

    return {
        "name": row.get("company_name", "Unknown"), "price": v("price") or 0,
        "segment": seg, "is_bfsi": seg in BFSI_SEGMENTS,
        "sales_growth_5y": v("sales_growth_5y"), "peer_sales_growth_5y": p("sales_growth_5y"),
        "sales_growth_3y": v("sales_growth_3y"), "peer_sales_growth_3y": p("sales_growth_3y"),
        "profit_growth_5y": v("profit_growth_5y"), "peer_profit_growth_5y": p("profit_growth_5y"),
        "profit_growth_3y": v("profit_growth_3y"), "peer_profit_growth_3y": p("profit_growth_3y"),
        "qoq_sales_growth": v("qoq_sales_growth"), "peer_qoq_sales_growth": p("qoq_sales_growth"),
        "qoq_profit_growth": v("qoq_profit_growth"), "peer_qoq_profit_growth": p("qoq_profit_growth"),
        "opm": v("opm"), "peer_opm": p("opm"),
        "opm_expansion": v("opm_expansion"), "peer_opm_expansion": p("opm_expansion"),
        "fixed_asset_growth": v("fixed_asset_growth"), "peer_fixed_asset_growth": p("fixed_asset_growth"),
        "inst_holding_abs": v("inst_holding_abs"), "peer_inst_holding_abs": p("inst_holding_abs"),
        "inst_holding_change": v("inst_holding_change"), "peer_inst_holding_change": p("inst_holding_change"),
        "roce": v("roce"), "peer_roce": p("roce"),
        "interest_coverage": v("interest_coverage"), "peer_interest_coverage": p("interest_coverage"),
        "dividend_yield": v("dividend_yield"), "peer_dividend_yield": p("dividend_yield"),
        # cc#506: segment_pe is now the LIVE segment MEDIAN pe (p("pe"), computed above from the
        # SAME peer set every other param uses) -- replaces the stale/mean-based screener_raw
        # "Industry PE" passthrough. historical_pe is unaffected (still each stock's own value).
        "pe": v("pe"), "historical_pe": v("historical_pe"), "segment_pe": p("pe"),
        "potential_upside": v("potential_upside"), "peer_potential_upside": p("potential_upside"),
    }


def sync_gvm_cache(cur) -> Dict:
    """cc#406: rebuild gvm_cache + peer_averages from the canonical gvm_scores (latest), and stamp
    cache_metadata fresh. The cache feeds /api/scorr/query (Max, CIO chips, query library); it lost
    its writer when GVM moved to the gvm_scores/gvm_history model, so it froze on 08-Jun. Runs as a
    post-step of recompute_gvm (same cursor/txn) and one-shot repair. No GVM math — pure projection."""
    cur.execute("DELETE FROM gvm_cache")
    cur.execute("""INSERT INTO gvm_cache (symbol, gvm_score, growth, value, momentum, segment, last_updated)
                   SELECT symbol, gvm_score, g_score, v_score, m_score, segment, NOW() FROM gvm_scores""")
    cur.execute("SELECT COUNT(*) FROM gvm_cache"); n_cache = cur.fetchone()[0]

    cur.execute("DELETE FROM peer_averages")
    cur.execute("""INSERT INTO peer_averages (segment, avg_gvm, avg_growth, avg_value, avg_momentum, stock_count, last_updated)
                   SELECT segment, ROUND(AVG(gvm_score)::numeric,2), ROUND(AVG(g_score)::numeric,2),
                          ROUND(AVG(v_score)::numeric,2), ROUND(AVG(m_score)::numeric,2), COUNT(*), NOW()
                   FROM gvm_scores WHERE segment IS NOT NULL GROUP BY segment""")
    cur.execute("SELECT COUNT(*) FROM peer_averages"); n_peers = cur.fetchone()[0]

    for k, cnt in (("gvm_cache", n_cache), ("peer_averages", n_peers)):
        cur.execute("""INSERT INTO cache_metadata (key, last_sync, stock_count, status)
                       VALUES (%s, NOW(), %s, 'ok')
                       ON CONFLICT (key) DO UPDATE SET last_sync=NOW(), stock_count=EXCLUDED.stock_count, status='ok'""",
                    (k, cnt))
    return {"gvm_cache": n_cache, "peer_averages": n_peers}


def recompute_gvm(target_date: Optional[date] = None, refresh_momentum: bool = True) -> Dict:
    target_date = target_date or date.today()

    # 1. Refresh daily momentum from raw_prices (price-driven M).
    mom_result = {"status": "skipped"}
    if refresh_momentum:
        try:
            mom_result = momentum_daily.compute_momentum(target_date)
        except Exception as e:
            log.error(f"momentum refresh failed: {e}")
            mom_result = {"status": "error", "message": str(e)}

    # 2. Merge fundamentals (screener) + daily momentum.
    df = _load_merged_df(target_date)
    if df.empty:
        return {"status": "warn", "message": "merge empty - check input_raw / screener_raw", "scored": 0,
                "momentum": mom_result}

    peer_avgs = _peer_averages(df)
    history_rows, latest_rows, errors, m_missing = [], [], 0, 0

    for _, row in df.iterrows():
        try:
            sd = _stock_dict(row, peer_avgs)
            g = api_g_score(sd)["score"]
            vv = api_v_score(sd)["score"]
            # M from daily momentum_scores; neutral 5.0 fallback if missing.
            m_raw = row.get("m_score_daily")
            if m_raw is None or (isinstance(m_raw, float) and pd.isna(m_raw)):
                m = 5.0
                m_missing += 1
            else:
                m = round(float(m_raw), 2)
            total = round((g + vv + m) / 3, 2)
            verd = _verdict(total)
            punch = _punchline(verd, _label_growth(g), _label_value(vv),
                               _label_momentum(m), _label_gvm(total))
            sym = str(row.get("nse_code", "")).strip()
            seg = row.get("gvm_segment", "Unknown")
            cname = row.get("company_name", sym)
            price = row.get("price")
            mcap = row.get("market_cap")
            price = float(price) if pd.notna(price) else None
            mcap = float(mcap) if pd.notna(mcap) else None
            # potential_upside is computed in _load_merged_df (fy27_growth × pe/hist)
            # and already drives the V score — also persist it for display (task #39).
            upside_val = row.get("potential_upside")
            upside_val = float(upside_val) if pd.notna(upside_val) else None

            history_rows.append((sym, target_date, g, vv, m, total, verd, seg))
            latest_rows.append((sym, cname, seg, price, g, vv, m, total, verd, punch, mcap, target_date, upside_val))
        except Exception as e:
            errors += 1
            log.warning(f"GVM score {row.get('nse_code','?')}: {e}")

    with _conn() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO gvm_history (symbol, score_date, g_score, v_score, m_score, gvm_score, verdict, segment)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, score_date) DO UPDATE SET
                g_score=EXCLUDED.g_score, v_score=EXCLUDED.v_score, m_score=EXCLUDED.m_score,
                gvm_score=EXCLUDED.gvm_score, verdict=EXCLUDED.verdict, segment=EXCLUDED.segment
        """, history_rows)
        cur.execute("DELETE FROM gvm_scores")
        cur.executemany("""
            INSERT INTO gvm_scores
                (symbol, company_name, segment, price, g_score, v_score, m_score, gvm_score, verdict, punchline, market_cap, score_date, upside_raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, latest_rows)
        # cc#406: keep gvm_cache + peer_averages in lock-step with gvm_scores (same txn) so the
        # Max/query cache can never drift a month behind again.
        cache_result = sync_gvm_cache(cur)
        conn.commit()

    # 3. Recompute sector_ratings from the fresh gvm_scores — always runs after stock scoring.
    sector_result = compute_sector_ratings(target_date)

    return {
        "status": "ok", "score_date": str(target_date),
        "scored": len(history_rows), "errors": errors, "m_missing": m_missing,
        "momentum": mom_result,
        "sector_ratings": sector_result,
        "cache_sync": cache_result,
        "history_table": "gvm_history (appended)", "latest_table": "gvm_scores (replaced)",
    }


# ============================================================
# ROUTES
# ============================================================
@router.post("/api/admin/load_screener_json")
async def load_screener_json(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    body = await req.json()
    rows = body.get("rows")
    if not rows:
        raise HTTPException(400, "rows[] required")
    n = _sql_clean_replace_screener(rows)
    return {"status": "ok", "action": "clean_replace", "rows_loaded": n}


@router.post("/api/gvm/recompute")
def gvm_recompute(refresh_momentum: bool = True, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    return recompute_gvm(refresh_momentum=refresh_momentum)


# ── cc#779: chart trend-strength verdict (founder-locked 02-Aug, cell-by-cell) ────────────────
# Three inputs over the SELECTED chart timeframe, so the badge recomputes on every timeframe switch:
#   price trend  — close now vs close at window start, FLAT band +/-2%
#   GVM trend    — gvm_score now vs at window start, FLAT band ABSOLUTE +/-0.15 points
#   level        — current gvm_score: >=7 high / 6-7 mid / <6 low
# Full GVM is used (not G+V) for consistency with the rating shown everywhere else.
_TF_DAYS = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "ALL": 365 * 20}
_VERDICT_CACHE = {}   # (symbol, tf, date) -> payload; per spec "cached per symbol+timeframe per day"


def _trend_verdict(p_chg, g_now, g_then):
    """The 6 founder-locked labels. Order matters — the first matching rule wins."""
    if p_chg is None or g_now is None or g_then is None:
        return None
    dg = g_now - g_then
    p = "up" if p_chg > 2 else ("down" if p_chg < -2 else "flat")
    g = "up" if dg > 0.15 else ("down" if dg < -0.15 else "flat")
    lvl = "high" if g_now >= 7 else ("mid" if g_now >= 6 else "low")
    if p == "up" and g == "up" and lvl == "high":
        k, t, c = "1_VERY_STRONG", "VERY STRONG", "#0a7f4f"
    elif p == "up" and ((g == "up" and lvl == "mid") or (g == "flat" and lvl == "high")):
        k, t, c = "2_STRONG", "STRONG", "#12a05f"
    elif p == "up" and g == "down" and lvl in ("high", "mid"):
        # GVM falling AGAINST a momentum tailwind is a strong statement, hence its own label.
        k, t, c = "4_CAUTION", "CAUTION", "#e0913a"
    elif (p == "up" and g == "down" and lvl == "low") or (p == "down" and g == "down"):
        k, t, c = "5_WEAK", "WEAK", "#dd3a4a"
    elif p == "down" and g in ("up", "flat") and lvl == "high":
        k, t, c = "6_QUALITY_DIP", "QUALITY DIP", "#1f8fa8"
    else:
        # residual: price up with flat/rising-but-low quality, and price-down cells whose
        # mid/low quality is NOT falling. Flat-price windows land here too.
        k, t, c = "3_STEADY", "STEADY", "#8a94ad"
    return {"key": k, "label": t, "color": c,
            "price_state": p, "gvm_state": g, "level": lvl,
            "price_chg_pct": round(p_chg, 2), "gvm_now": round(g_now, 2),
            "gvm_then": round(g_then, 2), "gvm_delta": round(dg, 2)}


@router.get("/api/gvm/trend_verdict/{symbol}")
def gvm_trend_verdict(symbol: str, tf: str = "1Y"):
    """cc#779: trend-strength verdict for the chart badge, over the selected timeframe. Cached per
    symbol+timeframe per day. Returns null verdict (never a guess) when either series lacks a point
    at the window start — the badge simply doesn't render."""
    sym = (symbol or "").upper()
    tf = tf if tf in _TF_DAYS else "1Y"
    key = (sym, tf, date.today().isoformat())
    if key in _VERDICT_CACHE:
        return _VERDICT_CACHE[key]
    days = _TF_DAYS[tf]
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT close, price_date FROM raw_prices
                       WHERE symbol=%s AND close IS NOT NULL ORDER BY price_date DESC LIMIT 1""", (sym,))
        r = cur.fetchone()
        p_now, p_date = (float(r[0]), r[1]) if r else (None, None)
        p_then = None
        if p_date:
            cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND close IS NOT NULL
                           AND price_date <= %s ORDER BY price_date DESC LIMIT 1""",
                        (sym, p_date - timedelta(days=days)))
            rr = cur.fetchone()
            p_then = float(rr[0]) if rr else None
        cur.execute("""SELECT gvm_score, score_date FROM gvm_history
                       WHERE symbol=%s AND gvm_score IS NOT NULL ORDER BY score_date DESC LIMIT 1""", (sym,))
        g = cur.fetchone()
        g_now, g_date = (float(g[0]), g[1]) if g else (None, None)
        g_then = None
        if g_date:
            cur.execute("""SELECT gvm_score FROM gvm_history WHERE symbol=%s AND gvm_score IS NOT NULL
                           AND score_date <= %s ORDER BY score_date DESC LIMIT 1""",
                        (sym, g_date - timedelta(days=days)))
            gg = cur.fetchone()
            g_then = float(gg[0]) if gg else None
            if g_then is None:   # window predates our GVM history -> use the oldest point we hold
                cur.execute("""SELECT gvm_score FROM gvm_history WHERE symbol=%s AND gvm_score IS NOT NULL
                               ORDER BY score_date ASC LIMIT 1""", (sym,))
                gg = cur.fetchone()
                g_then = float(gg[0]) if gg else None
    p_chg = ((p_now - p_then) / p_then * 100.0) if (p_now and p_then) else None
    out = {"symbol": sym, "timeframe": tf, "verdict": _trend_verdict(p_chg, g_now, g_then)}
    _VERDICT_CACHE[key] = out
    if len(_VERDICT_CACHE) > 4000:
        _VERDICT_CACHE.clear()
    return out


@router.get("/api/gvm/history/{symbol}")
def gvm_history(symbol: str, days: int = 180):
    with _conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT score_date, g_score, v_score, m_score, gvm_score, verdict
            FROM gvm_history WHERE symbol = %s ORDER BY score_date DESC LIMIT %s
        """, (symbol.upper(), days))
        return {"symbol": symbol.upper(), "points": cur.fetchall()}

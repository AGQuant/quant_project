"""
mf_pipeline.py — V15 MF Intelligence, P0-A data layer (cc#466, spec id=3079).

Data layer ONLY (no scoring math, no page — those are cc#467 + next session). Sources:
  • AMFI NAVAll.txt  — daily NAV + scheme master basics (free, authoritative).
  • mfapi.in         — per-scheme historical NAV JSON (clean free backfill).
  • AMFI portfolio-disclosure — monthly holdings AMC excels (curated universe first).
  • Moneycontrol     — weekly cross-check (ER, category, manager, AUM, 1/3/5Y returns).
  • FINKHOZ MCP      — rating pull where the connector is available.

All scrapes are scheduled + resumable + ops_log-instrumented (earnings_refresh convention).
NSE-symbol resolution on holdings reuses the cc#462 tiered name-matching against screener_raw.
Nothing here touches worker/**. Tables are ensured idempotently at import + via /reconcile.
"""
import os
import re
import io
import json
import logging
from datetime import datetime, date

import psycopg
from fastapi import APIRouter

log = logging.getLogger("scorr.mf")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
_AMFI_NAV = "https://www.amfiindia.com/spages/NAVAll.txt"
_MFAPI = "https://api.mfapi.in/mf/{code}"


def _conn():
    return psycopg.connect(_DB)


def _oplog(cur, title, details, category="mf_pipeline"):
    try:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)",
                    (category, title, json.dumps(details, default=str)))
    except Exception as e:
        log.warning(f"oplog {title}: {e}")


# ── schema ────────────────────────────────────────────────────────────────────────
def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_master (
        scheme_code TEXT PRIMARY KEY, amfi_code TEXT, isin TEXT, name TEXT NOT NULL, amc TEXT,
        category TEXT, plan TEXT, expense_ratio NUMERIC, aum_cr NUMERIC, crisil_rank INTEGER,
        finkhoz_rating NUMERIC, manager TEXT, inception DATE, ret_1y NUMERIC, ret_3y NUMERIC,
        ret_5y NUMERIC, source TEXT, flags TEXT, curated BOOLEAN DEFAULT FALSE,
        updated_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_nav_history (
        scheme_code TEXT NOT NULL, nav_date DATE NOT NULL, nav NUMERIC,
        PRIMARY KEY (scheme_code, nav_date))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_holdings (
        scheme_code TEXT NOT NULL, as_of_month DATE NOT NULL, isin TEXT, company_name TEXT,
        pct_weight NUMERIC, resolved_nse_symbol TEXT, resolve_method TEXT,
        PRIMARY KEY (scheme_code, as_of_month, company_name))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_scores (
        scheme_code TEXT PRIMARY KEY, mqs NUMERIC, q_score NUMERIC, r_score NUMERIC,
        c_score NUMERIC, s_score NUMERIC, computed_at TIMESTAMPTZ)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_category_averages (
        category TEXT PRIMARY KEY, avg_expense_ratio NUMERIC, avg_ret_1y NUMERIC,
        avg_ret_3y NUMERIC, avg_ret_5y NUMERIC, n_funds INTEGER, updated_at TIMESTAMPTZ DEFAULT NOW())""")


# ── founder 12-fund seed (cc#466 build_5) ──────────────────────────────────────────
# Nippon India Large Cap appeared TWICE (rows 4 & 6) in the founder sheet — seeded once, flagged for
# resolution (rating not cleanly given on the duplicate). Banking&PSU pair ratings not given in seed.
SEED_FUNDS = [
    ("V15S01", "Edelweiss ELSS Tax Saver", "Edelweiss", "ELSS", 7.45, None),
    ("V15S02", "SBI Long Term Equity Fund (ELSS)", "SBI", "ELSS", 7.29, None),
    ("V15S03", "HDFC Flexi Cap Fund", "HDFC", "Flexi Cap", 7.22, None),
    ("V15S04", "Nippon India Large Cap Fund", "Nippon India", "Large Cap", None,
     "DUP_ANOMALY: appeared twice (rows 4 & 6) in founder sheet — seeded once; FINKHOZ rating not cleanly given for the duplicate; resolve rating + confirm single scheme"),
    ("V15S05", "ICICI Prudential Large Cap Fund", "ICICI Prudential", "Large Cap", 7.19, None),
    ("V15S06", "ICICI Prudential Midcap Fund", "ICICI Prudential", "Mid Cap", 7.46, None),
    ("V15S07", "HDFC Mid-Cap Opportunities Fund", "HDFC", "Mid Cap", 7.33, None),
    ("V15S08", "Union Small Cap Fund", "Union", "Small Cap", 7.74, None),
    ("V15S09", "Bandhan Small Cap Fund", "Bandhan", "Small Cap", 7.13, None),
    ("V15S10", "Bandhan Banking & PSU Debt Fund", "Bandhan", "Banking & PSU", None,
     "FINKHOZ rating not given in seed sheet"),
    ("V15S11", "UTI Banking & PSU Debt Fund", "UTI", "Banking & PSU", None,
     "FINKHOZ rating not given in seed sheet"),
]


def seed_curated(cur):
    for code, name, amc, cat, rating, flags in SEED_FUNDS:
        cur.execute("""INSERT INTO mf_master (scheme_code, name, amc, category, finkhoz_rating, curated, source, flags)
                       VALUES (%s,%s,%s,%s,%s,TRUE,'founder_seed_12jul',%s)
                       ON CONFLICT (scheme_code) DO UPDATE SET name=EXCLUDED.name, amc=EXCLUDED.amc,
                         category=EXCLUDED.category, finkhoz_rating=EXCLUDED.finkhoz_rating,
                         flags=EXCLUDED.flags, curated=TRUE, updated_at=NOW()""",
                    (code, name, amc, cat, rating, flags))


# ── AMFI daily NAV (cc#466 build_2) ────────────────────────────────────────────────
def _http_get(url, timeout=60):
    """requests preferred; httpx fallback — whichever the runtime has."""
    try:
        import requests
        return requests.get(url, timeout=timeout, headers={"User-Agent": "Scorr-MF/1.0"}).text
    except Exception:
        import httpx
        return httpx.get(url, timeout=timeout, headers={"User-Agent": "Scorr-MF/1.0"}).text


def run_amfi_nav(conn=None):
    """Daily: parse AMFI NAVAll.txt -> upsert mf_master basics (amfi_code/isin/name) + append
    mf_nav_history. NAVAll is ';'-delimited with AMC-name/blank header lines interleaved."""
    own = conn is None
    conn = conn or _conn()
    n_master = n_nav = 0
    latest = None
    try:
        txt = _http_get(_AMFI_NAV)
        with conn.cursor() as cur:
            ensure_tables(cur)
            for ln in txt.splitlines():
                parts = ln.split(";")
                if len(parts) < 6:
                    continue
                code = parts[0].strip()
                if not code.isdigit():
                    continue
                isin = (parts[1].strip() or parts[2].strip()) or None
                name = parts[3].strip()
                try:
                    nav = float(parts[4].strip())
                except Exception:
                    continue
                try:
                    nd = datetime.strptime(parts[5].strip(), "%d-%b-%Y").date()
                except Exception:
                    continue
                latest = nd if (latest is None or nd > latest) else latest
                # master upsert keyed by AMFI code (scheme_code = amfi code for AMFI-universe rows)
                cur.execute("""INSERT INTO mf_master (scheme_code, amfi_code, isin, name, source, updated_at)
                               VALUES (%s,%s,%s,%s,'amfi',NOW())
                               ON CONFLICT (scheme_code) DO UPDATE SET amfi_code=EXCLUDED.amfi_code,
                                 isin=COALESCE(mf_master.isin, EXCLUDED.isin), updated_at=NOW()""",
                            (code, code, isin, name))
                n_master += 1
                cur.execute("""INSERT INTO mf_nav_history (scheme_code, nav_date, nav) VALUES (%s,%s,%s)
                               ON CONFLICT (scheme_code, nav_date) DO UPDATE SET nav=EXCLUDED.nav""",
                            (code, nd, nav))
                n_nav += 1
            _oplog(cur, "MF_AMFI_NAV", {"schemes": n_master, "nav_rows": n_nav, "latest": str(latest)})
            conn.commit()
    except Exception as e:
        log.error(f"run_amfi_nav: {e}")
        with conn.cursor() as cur:
            _oplog(cur, "MF_AMFI_NAV_ERROR", {"error": str(e)[:300]}); conn.commit()
        if own:
            conn.close()
        return {"error": str(e)[:300]}
    if own:
        conn.close()
    return {"schemes": n_master, "nav_rows": n_nav, "latest": str(latest)}


def backfill_nav_mfapi(scheme_code, amfi_code=None):
    """mfapi.in per-scheme JSON backfill for one scheme (historical NAV). amfi_code defaults to
    scheme_code (AMFI-universe rows share the code). Idempotent upserts."""
    code = amfi_code or scheme_code
    inserted = 0
    with _conn() as conn, conn.cursor() as cur:
        try:
            d = json.loads(_http_get(_MFAPI.format(code=code)))
            for row in (d.get("data") or []):
                try:
                    nd = datetime.strptime(row["date"], "%d-%m-%Y").date()
                    nav = float(row["nav"])
                except Exception:
                    continue
                cur.execute("""INSERT INTO mf_nav_history (scheme_code, nav_date, nav) VALUES (%s,%s,%s)
                               ON CONFLICT (scheme_code, nav_date) DO NOTHING""", (scheme_code, nd, nav))
                inserted += 1
            _oplog(cur, "MF_NAV_BACKFILL", {"scheme_code": scheme_code, "amfi_code": code, "rows": inserted})
            conn.commit()
        except Exception as e:
            return {"error": str(e)[:200], "scheme_code": scheme_code}
    return {"scheme_code": scheme_code, "rows": inserted}


# ── seed <-> AMFI reconciliation ───────────────────────────────────────────────────
def _norm_fund(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    stop = {"fund", "the", "growth", "plan", "direct", "regular", "option", "idcw", "reinvestment",
            "payout", "scheme", "open", "ended", "an", "of", "india"}
    return [t for t in s.split() if t and t not in stop]


def reconcile_seed(conn=None):
    """Match each curated seed fund to its AMFI Direct-Growth scheme_code by fuzzy name, populate
    amfi_code so NAV history + returns can be joined. Best-effort token-overlap (no pg_trgm)."""
    own = conn is None
    conn = conn or _conn()
    out = []
    with conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("SELECT scheme_code, name, amc FROM mf_master WHERE curated AND amfi_code IS NULL")
        seeds = cur.fetchall()
        for sc, name, amc in seeds:
            toks = set(_norm_fund((amc or "") + " " + name))
            if not toks:
                continue
            longest = max(toks, key=len)
            cur.execute("""SELECT scheme_code, name FROM mf_master
                           WHERE source='amfi' AND name ILIKE %s
                             AND name ILIKE '%%direct%%' AND name ILIKE '%%growth%%' LIMIT 60""",
                        (f"%{longest}%",))
            best = None
            for acode, aname in cur.fetchall():
                aset = set(_norm_fund(aname))
                if not aset:
                    continue
                jac = len(toks & aset) / float(len(toks | aset))
                if best is None or jac > best[0]:
                    best = (jac, acode, aname)
            if best and best[0] >= 0.5:
                cur.execute("UPDATE mf_master SET amfi_code=%s, updated_at=NOW() WHERE scheme_code=%s",
                            (best[1], sc))
                out.append({"seed": sc, "name": name, "amfi_code": best[1], "matched": best[2], "score": round(best[0], 2)})
            else:
                out.append({"seed": sc, "name": name, "amfi_code": None,
                            "top": best[2] if best else None, "score": round(best[0], 2) if best else 0})
        _oplog(cur, "MF_RECONCILE_SEED", {"matched": sum(1 for x in out if x.get("amfi_code")), "total": len(seeds)})
        conn.commit()
    if own:
        conn.close()
    return {"results": out}


# ── holdings NSE-symbol resolution (reuse cc#462 tiered matching) ──────────────────
_STOP_CO = {"ltd", "limited", "india", "indian", "the", "co", "company", "corp", "corporation",
            "and", "pvt", "private", "enterprises", "industries", "&"}


def _resolve_nse(cur, name):
    up = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    if not up:
        return None, None
    cur.execute("SELECT nse_code FROM input_raw WHERE REGEXP_REPLACE(LOWER(company_name),'[^a-z0-9]','','g')=%s LIMIT 1", (up,))
    m = cur.fetchone()
    if m:
        return m[0], "company_name"
    toks = [t for t in re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower()).split() if t and t not in _STOP_CO]
    if not toks:
        return None, None
    longest = max(toks, key=len)
    cur.execute("SELECT nse_code, company_name FROM input_raw WHERE company_name ILIKE %s LIMIT 40", (f"%{longest}%",))
    tset = set(toks)
    best = None
    for nc, cn in cur.fetchall():
        cset = set(t for t in re.sub(r"[^a-z0-9 ]", " ", str(cn or "").lower()).split() if t and t not in _STOP_CO)
        if not cset:
            continue
        jac = len(tset & cset) / float(len(tset | cset))
        if best is None or jac > best[0]:
            best = (jac, nc)
    if best and best[0] >= 0.6:
        return best[1], "fuzzy"
    return None, None


def parse_holdings_xlsx(data):
    """Header-detection holdings parser (reuses the cc#462 approach): scan sheets for a header with an
    instrument/company column + a %-weight column; rows below until first empty. Returns [{company, pct, isin}]."""
    try:
        import pandas as pd
    except Exception:
        return [], "pandas unavailable"
    NAME_KW = ["name of the instrument", "instrument", "company", "security name", "scrip", "stock", "name"]
    PCT_KW = ["% to nav", "% to net assets", "% of nav", "percentage", "weight", "% to"]
    ISIN_KW = ["isin"]

    def pick(hdr, kws):
        low = [str(h or "").strip().lower() for h in hdr]
        for kw in kws:
            for i, h in enumerate(low):
                if kw in h:
                    return i
        return None
    out = []
    try:
        xl = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    except Exception as e:
        return [], f"read error: {str(e)[:120]}"
    for _sn, df in xl.items():
        grid = df.fillna("").values.tolist()
        for hi in range(min(len(grid), 40)):
            row = grid[hi]
            ni, pi = pick(row, NAME_KW), pick(row, PCT_KW)
            if ni is not None and pi is not None:
                ii = pick(row, ISIN_KW)
                for r in grid[hi + 1:]:
                    if all(str(c).strip() == "" for c in r):
                        break
                    if ni >= len(r):
                        continue
                    nm = re.sub(r"\s+", " ", str(r[ni] or "").strip())
                    if not nm or nm.lower() in ("total", "grand total", "subtotal", "nan"):
                        continue
                    try:
                        pct = float(str(r[pi] or "").replace("%", "").replace(",", "").strip()) if pi < len(r) else None
                    except Exception:
                        pct = None
                    isin = (str(r[ii]).strip() if (ii is not None and ii < len(r)) else None) or None
                    out.append({"company": nm, "pct": pct, "isin": isin})
                break
        if out:
            break
    return out, (None if out else "no holdings header found")


# ── admin triggers + read endpoints (cc#467 reads these) ───────────────────────────
@router.post("/api/v15/mf/ensure")
def mf_ensure():
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur); seed_curated(cur); conn.commit()
    return {"ok": True, "seeded": len(SEED_FUNDS)}


@router.post("/api/v15/mf/nav_refresh")
def mf_nav_refresh():
    return run_amfi_nav()


@router.post("/api/v15/mf/reconcile")
def mf_reconcile():
    return reconcile_seed()


@router.post("/api/v15/mf/backfill_curated_nav")
def mf_backfill_curated_nav():
    """mfapi.in NAV backfill for the curated funds that have an amfi_code (run after reconcile)."""
    out = []
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT scheme_code, amfi_code FROM mf_master WHERE curated AND amfi_code IS NOT NULL")
        rows = cur.fetchall()
    for sc, ac in rows:
        out.append(backfill_nav_mfapi(sc, ac))
    return {"backfilled": out}


@router.get("/api/v15/mf/search")
def mf_search(q: str = "", limit: int = 20):
    q = (q or "").strip()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        if q:
            cur.execute("""SELECT scheme_code, name, amc, category, finkhoz_rating, curated
                           FROM mf_master WHERE name ILIKE %s OR amc ILIKE %s OR category ILIKE %s
                           ORDER BY curated DESC, name LIMIT %s""",
                        (f"%{q}%", f"%{q}%", f"%{q}%", max(1, min(limit, 50))))
        else:
            cur.execute("""SELECT scheme_code, name, amc, category, finkhoz_rating, curated
                           FROM mf_master WHERE curated ORDER BY category, name LIMIT %s""", (max(1, min(limit, 50)),))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(rows), "results": rows}


@router.get("/api/v15/mf/curated")
def mf_curated():
    """The founder 12-fund curated screener rows (cc#467 screener table)."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT scheme_code, name, amc, category, finkhoz_rating, crisil_rank,
                       expense_ratio, aum_cr, ret_1y, ret_3y, ret_5y, flags
                       FROM mf_master WHERE curated ORDER BY category, name""")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(rows), "funds": rows}


@router.get("/api/v15/mf/fund/{scheme_code}")
def mf_fund(scheme_code: str):
    """Deep-dive read: master + look-through holdings joined to per-stock GVM (works today) + NAV series."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("SELECT * FROM mf_master WHERE scheme_code=%s", (scheme_code,))
        r = cur.fetchone()
        if not r:
            return {"error": "not found"}
        master = dict(zip([d[0] for d in cur.description], r))
        code = master.get("amfi_code") or scheme_code
        # look-through holdings + per-stock GVM (gvm_scores latest)
        cur.execute("""SELECT h.company_name, h.pct_weight, h.resolved_nse_symbol,
                              ROUND(g.gvm_score::numeric,2) AS gvm, g.segment, g.verdict
                       FROM mf_holdings h
                       LEFT JOIN gvm_scores g ON g.symbol=h.resolved_nse_symbol
                            AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                       WHERE h.scheme_code=%s
                         AND h.as_of_month=(SELECT MAX(as_of_month) FROM mf_holdings WHERE scheme_code=%s)
                       ORDER BY h.pct_weight DESC NULLS LAST""", (scheme_code, scheme_code))
        hcols = [d[0] for d in cur.description]
        holdings = [dict(zip(hcols, x)) for x in cur.fetchall()]
        # NAV series (last ~400 rows) for the returns-vs-category line
        cur.execute("""SELECT nav_date, nav FROM mf_nav_history WHERE scheme_code=%s
                       ORDER BY nav_date DESC LIMIT 400""", (code,))
        nav = [{"date": str(d), "nav": float(n)} for d, n in cur.fetchall()][::-1]
    # portfolio-weighted GVM (where resolved) — a real P0-A signal
    wsum = sum((h["pct_weight"] or 0) for h in holdings if h.get("gvm") is not None)
    pw_gvm = (sum((h["pct_weight"] or 0) * float(h["gvm"]) for h in holdings if h.get("gvm") is not None) / wsum) if wsum else None
    return {"master": master, "holdings": holdings, "nav": nav,
            "portfolio_weighted_gvm": round(pw_gvm, 2) if pw_gvm is not None else None,
            "resolved_pct": round(wsum, 1) if holdings else None}

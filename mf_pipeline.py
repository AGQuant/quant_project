"""
mf_pipeline.py — V15 MF Intelligence, P0-A data layer (cc#466, spec id=3079).

Data layer ONLY (no scoring math, no page — those are cc#467 + next session). Sources:
  • AMFI NAVAll.txt  — daily NAV + scheme master basics (free, authoritative).
  • mfapi.in         — per-scheme historical NAV JSON (clean free backfill).
  • AMFI portfolio-disclosure — monthly holdings AMC excels (curated universe first).
  • Moneycontrol     — weekly cross-check (ER, category, manager, AUM, 1/3/5Y returns).

All scrapes are scheduled + resumable + ops_log-instrumented (earnings_refresh convention).
NSE-symbol resolution on holdings reuses the cc#462 tiered name-matching against screener_raw.
Nothing here touches worker/**. Tables are ensured idempotently at import + via /reconcile.
"""
import os
import re
import io
import json
import logging
from datetime import datetime, date, timedelta

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
        finkhoz_rating NUMERIC, -- DEPRECATED (cc#505, 18-Jul-2026): brand retired, DO NOT
                                 -- read/write this column. Column kept in place (data already
                                 -- nulled) rather than dropped, per MAINTENANCE_LOCK_RULE
                                 -- (schema-altering ops are Railway-console-only).
        manager TEXT, inception DATE, ret_1y NUMERIC, ret_3y NUMERIC,
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
        c_score NUMERIC, s_score NUMERIC, computed_at TIMESTAMPTZ,
        basis_pct NUMERIC, basis_label TEXT)""")
    # cc#528 reopened: honest MQS disclosure when a pillar (Q, permanently since cc#505) is
    # missing -- basis_pct/basis_label record what fraction of the locked Q0.35/R0.30/C0.15/
    # S0.20 model actually backs a given fund's mqs, so the page can say "62.4 on R+C+S (65%
    # basis)" instead of a bare misleading /100. Additive nullable columns, same pattern as
    # _ensure_returns_cols below -- not a MAINTENANCE_LOCK_RULE table rewrite.
    cur.execute("""ALTER TABLE mf_scores
        ADD COLUMN IF NOT EXISTS basis_pct NUMERIC,
        ADD COLUMN IF NOT EXISTS basis_label TEXT""")
    # cc#546: Q (portfolio-quality) pillar restored via holdings x live GVM look-through (NOT
    # the retired external rating brand -- pure DB compute, zero LLM/HTTP). coverage_pct records
    # the fraction of a fund's disclosed weight that resolved to a live gvm_scores row; the Q
    # pillar (and the 100% Q+R+C+S basis) only activates when coverage_pct >= 60. Stored on every
    # scored scheme regardless of the gate so the card can distinguish "insufficient holdings
    # data" (coverage < 60 / none) from a merely-uncomputed pillar. Additive nullable column,
    # same app-side ADD COLUMN IF NOT EXISTS pattern (run_sql cannot ALTER -- self-creates here).
    cur.execute("""ALTER TABLE mf_scores
        ADD COLUMN IF NOT EXISTS coverage_pct NUMERIC""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_category_averages (
        category TEXT PRIMARY KEY, avg_expense_ratio NUMERIC, avg_ret_1y NUMERIC,
        avg_ret_3y NUMERIC, avg_ret_5y NUMERIC, n_funds INTEGER, updated_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_monthly_snapshot (
        scheme_code TEXT NOT NULL, snapshot_month DATE NOT NULL,
        aum_cr NUMERIC, expense_ratio NUMERIC,
        PRIMARY KEY (scheme_code, snapshot_month))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_amc_holdings_registry (
        amc TEXT PRIMARY KEY, disclosure_url TEXT, source_page TEXT,
        discovered_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_mc_map (
        scheme_code TEXT PRIMARY KEY, mc_id TEXT, mc_slug TEXT, matched_name TEXT,
        match_score NUMERIC, resolved_at TIMESTAMPTZ DEFAULT NOW())""")


# cc#520: hard category whitelist -- AMFI's 11 equity-oriented open-ended categories, verbatim as
# they appear in mf_master.category after _derive_cat() normalization. "Banking & PSU" (debt) is
# the only category currently present in mf_master outside this whitelist -- excluded everywhere a
# fund enters the equity universe (scoring, category averages, screener, search) via this ONE
# constant rather than a scattered "<> 'Banking & PSU'" string check repeated per call site.
EQUITY_CATEGORY_WHITELIST = (
    "Dividend Yield Fund", "ELSS", "Flexi Cap Fund", "Focused Fund", "Large Cap Fund",
    "Large & Mid Cap Fund", "Mid Cap Fund", "Multi Cap Fund", "Sectoral/Thematic Fund",
    "Small Cap Fund", "Value Fund/Contra Fund",
)

# cc#546: minimum % of a fund's disclosed portfolio weight that must resolve to a live GVM row
# before the Q (portfolio-quality) pillar activates and the fund moves to the full 100% Q+R+C+S
# basis. Below this the fund keeps its R+C+S-only basis (Q left NULL) but coverage_pct is still
# stored, so the card distinguishes real "insufficient holdings data" from an uncomputed pillar.
Q_COVERAGE_MIN = 60.0


# ── founder 12-fund seed (cc#466 build_5) ──────────────────────────────────────────
# Nippon India Large Cap appeared TWICE (rows 4 & 6) in the founder sheet — seeded once, flagged for
# resolution. cc#505 (18-Jul-2026): the seed sheet's rating column (brand retired) is no longer
# read or written here — see the deprecated column note in ensure_tables() above.
SEED_FUNDS = [
    ("V15S01", "Edelweiss ELSS Tax Saver", "Edelweiss", "ELSS", None),
    ("V15S02", "SBI Long Term Equity Fund (ELSS)", "SBI", "ELSS", None),
    ("V15S03", "HDFC Flexi Cap Fund", "HDFC", "Flexi Cap", None),
    ("V15S04", "Nippon India Large Cap Fund", "Nippon India", "Large Cap",
     "DUP_ANOMALY: appeared twice (rows 4 & 6) in founder sheet — seeded once; resolve + confirm single scheme"),
    ("V15S05", "ICICI Prudential Large Cap Fund", "ICICI Prudential", "Large Cap", None),
    ("V15S06", "ICICI Prudential Midcap Fund", "ICICI Prudential", "Mid Cap", None),
    ("V15S07", "HDFC Mid-Cap Opportunities Fund", "HDFC", "Mid Cap", None),
    ("V15S08", "Union Small Cap Fund", "Union", "Small Cap", None),
    ("V15S09", "Bandhan Small Cap Fund", "Bandhan", "Small Cap", None),
    ("V15S10", "Bandhan Banking & PSU Debt Fund", "Bandhan", "Banking & PSU", None),
    ("V15S11", "UTI Banking & PSU Debt Fund", "UTI", "Banking & PSU", None),
]


def seed_curated(cur):
    for code, name, amc, cat, flags in SEED_FUNDS:
        cur.execute("""INSERT INTO mf_master (scheme_code, name, amc, category, curated, source, flags)
                       VALUES (%s,%s,%s,%s,TRUE,'founder_seed_12jul',%s)
                       ON CONFLICT (scheme_code) DO UPDATE SET name=EXCLUDED.name, amc=EXCLUDED.amc,
                         category=EXCLUDED.category,
                         flags=EXCLUDED.flags, curated=TRUE, updated_at=NOW()""",
                    (code, name, amc, cat, flags))


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


# ── cc#477/491: AUM backfill + weekly-rolling-12mo NAV history + returns ───────────
# Founder 13-Jul: MF returns 1W/1M/3M/6M/1Y/2Y(+3Y/5Y) for funds with AUM > Rs 5,000 cr
# (LOCKED, revised from 10,000). NAV storage cadence superseded 17-Jul by
# CADENCE_RETENTION_FINAL_FOUNDER_17JUL — see _weekly_rolling_12mo.
_AMFI_AAUM_URL = "https://www.amfiindia.com/modules/AverageAUMDetails"
_AMFI_AAUM_PAGE = "https://www.amfiindia.com/research-information/aum-data/average-aum"
AUM_THRESHOLD_CR = 5000.0        # founder-locked 13-Jul (was 10000)


def _ensure_returns_cols(cur):
    """App-side ADD COLUMN (never via the run_sql lock-blocked path). ret_1y/3y/5y already
    exist in the base schema; add the new horizons + a nav granularity marker."""
    cur.execute("""ALTER TABLE mf_master
        ADD COLUMN IF NOT EXISTS ret_1w NUMERIC,
        ADD COLUMN IF NOT EXISTS ret_1m NUMERIC,
        ADD COLUMN IF NOT EXISTS ret_3m NUMERIC,
        ADD COLUMN IF NOT EXISTS ret_6m NUMERIC,
        ADD COLUMN IF NOT EXISTS ret_2y NUMERIC,
        ADD COLUMN IF NOT EXISTS returns_asof DATE""")
    cur.execute("ALTER TABLE mf_nav_history ADD COLUMN IF NOT EXISTS nav_kind TEXT")


def _amfi_aaum_quarter(today=None):
    """Latest COMPLETED AMFI scheme-wise AAUM quarter (financial year Apr-Mar). AMFI publishes
    scheme-wise AAUM quarterly (~4-5 wks after quarter end). Returns (fy_str, quarter_str)."""
    today = today or date.today()
    y, m = today.year, today.month
    # completed quarters end Jun/Sep/Dec/Mar; pick the most recent whose end is >=35 days ago
    ends = [(date(y, 3, 31), f"January - March {y}", f"{y-1}-{y}"),
            (date(y, 6, 30), f"April - June {y}", f"{y}-{y+1}"),
            (date(y, 9, 30), f"July - September {y}", f"{y}-{y+1}"),
            (date(y, 12, 31), f"October - December {y}", f"{y}-{y+1}"),
            (date(y-1, 12, 31), f"October - December {y-1}", f"{y-1}-{y}")]
    avail = [(e, q, fy) for (e, q, fy) in ends if (today - e).days >= 35]
    e, q, fy = max(avail, key=lambda t: t[0])
    return fy, q


def _amfi_form_options(cur):
    """GET the AAUM page and extract every <select> name/id -> [(value,text)] pairs. AMFI's
    Year/Quarter dropdowns use INTERNAL numeric option IDs (not human strings), so we must read
    the live option values. Dumps them to ops_log so the exact IDs are inspectable via run_sql."""
    import requests
    html = requests.get(_AMFI_AAUM_PAGE, headers={"User-Agent": "Scorr-MF/1.0"}, timeout=60).text
    selects = {}
    for m in re.finditer(r'<select[^>]*?(?:name|id)="([^"]+)"[^>]*>(.*?)</select>', html, re.S | re.I):
        nm = m.group(1)
        opts = re.findall(r'<option[^>]*?value="([^"]*)"[^>]*>(.*?)</option>', m.group(2), re.S | re.I)
        selects[nm] = [(v.strip(), re.sub(r"<[^>]+>", "", t).strip()) for v, t in opts if v.strip()]
    _oplog(cur, "MF_AUM_FORM_OPTIONS", {k: v[:8] for k, v in selects.items()})
    return selects


def _parse_aaum_html(body):
    """Parse an AMFI AAUM HTML response into [(scheme_name, aaum_cr)] (lakh->cr). Returns []
    if no usable table."""
    import pandas as pd
    parsed = []
    try:
        tables = pd.read_html(io.StringIO(body))
    except Exception:
        return parsed
    for t in tables:
        cols = [str(c).strip().lower() for c in t.columns]
        name_i = next((i for i, c in enumerate(cols) if "scheme" in c or "name" in c), None)
        aaum_i = next((i for i, c in enumerate(cols)
                       if ("average" in c and "aum" in c) or "aaum" in c or "excluding fund of funds" in c), None)
        if name_i is None or aaum_i is None or len(t) < 20:
            continue
        rows = []
        for _, row in t.iterrows():
            nm = str(row.iloc[name_i]).strip()
            raw = str(row.iloc[aaum_i]).replace(",", "").strip()
            if not nm or nm.lower() in ("total", "nan", "scheme name") or raw in ("", "nan"):
                continue
            try:
                rows.append((nm, round(float(raw) / 100.0, 2)))   # AMFI lakh -> cr
            except Exception:
                continue
        if len(rows) > len(parsed):
            parsed = rows
    return parsed


# cc#477 unblock_addendum: proxy CANDIDATE SET so NAV+returns (proven mfapi path) run even
# when AMFI AUM is unavailable. Top AMCs' Direct-Growth equity/hybrid schemes (~600).
_TOP_AMCS = ["sbi", "hdfc", "icici pru", "nippon india", "kotak", "aditya birla sun life",
             "aditya birla", "uti", "axis", "mirae asset", "dsp", "tata", "franklin",
             "canara robeco", "edelweiss", "bandhan", "parag parikh", "quant", "motilal oswal",
             "invesco", "sundaram", "pgim", "hsbc", "baroda", "union", "mahindra", "ppfas"]
_EQ_HY_KW = ["flexi cap", "large cap", "large & mid", "large and mid", "mid cap", "small cap",
             "multi cap", "elss", "tax saver", "focused", "value", "contra", "dividend yield",
             "index", "nifty", "sensex", "hybrid", "balanced advantage", "aggressive",
             "equity savings", "multi asset", "arbitrage", "equity"]
_EXCL_KW = ["debt", "liquid", "overnight", "gilt", "money market", "ultra short", "low duration",
            "short duration", "corporate bond", "banking & psu", "credit risk", "dynamic bond",
            "floater", "10 year", "psu bond", "fund of fund", "fof", "etf"]

# cc#477 unblock_addendum strategies 1-2: candidate AMFI/portal AUM endpoints (SPA JSON + legacy ASPX).
_AUM_PROBE = [
    ("GET",  "https://www.amfiindia.com/api/aum-data", None),
    ("GET",  "https://www.amfiindia.com/api/average-aum", None),
    ("GET",  "https://www.amfiindia.com/api/mutual-fund-scheme-details", None),
    ("GET",  "https://portal.amfiindia.com/spages/AUMReport.aspx", None),
    ("GET",  "https://portal.amfiindia.com/spages/AverageAUMReport.aspx", None),
    ("POST", "https://portal.amfiindia.com/modules/AverageAUMDetails",
     {"AUmType": "S", "AumCatType": "Typewise", "MF_Name": "-1", "option": "1"}),
]


def _candidate_scheme_set(cur):
    """~600 largest-by-proxy schemes: Direct-Growth equity/hybrid of the top AMCs (mf_master name).
    Used as the NAV/returns universe when AMFI AUM is unavailable (founder unblock fallback)."""
    cur.execute("SELECT scheme_code, amfi_code, name FROM mf_master "
                "WHERE name ILIKE '%%direct%%' AND name ILIKE '%%growth%%'")
    out = []
    for sc, ac, nm in cur.fetchall():
        n = (nm or "").lower()
        if any(x in n for x in _EXCL_KW):
            continue
        if not any(a in n for a in _TOP_AMCS):
            continue
        if not any(k in n for k in _EQ_HY_KW):
            continue
        out.append((sc, ac or sc))
    return out


def _parse_aum_json(obj):
    """Pull [(scheme_name, aum_cr)] from an unknown JSON shape — find list-of-dicts with a
    name-like key + an aum-like numeric key. Assumes Rs cr unless values look like lakh (>5e5)."""
    import json as _json
    if isinstance(obj, str):
        try:
            obj = _json.loads(obj)
        except Exception:
            return []
    stack, rows = [obj], []
    while stack:
        cur_o = stack.pop()
        if isinstance(cur_o, dict):
            stack.extend(cur_o.values())
        elif isinstance(cur_o, list):
            if cur_o and isinstance(cur_o[0], dict):
                keys = {k.lower(): k for k in cur_o[0].keys()}
                nk = next((keys[k] for k in keys if "scheme" in k or "name" in k), None)
                ak = next((keys[k] for k in keys if "aum" in k or "aaum" in k), None)
                if nk and ak:
                    for it in cur_o:
                        try:
                            nm = str(it.get(nk)).strip()
                            v = float(str(it.get(ak)).replace(",", ""))
                            if nm and v > 0:
                                rows.append((nm, round(v / 100.0 if v > 5e5 else v, 2)))
                        except Exception:
                            continue
            else:
                stack.extend(cur_o)
    return rows


def _discover_amfi_endpoints(cur, page_url, hint):
    """cc#491 VERIFIED_SOURCE_INTEL_17JUL_CLAUDE_WEB: AMFI relaunched amfiindia.com as a Next.js
    SPA (2025-26 redesign) — every legacy scrape URL below is dead (verified both browser-side by
    Claude web AND server-side by a live Railway run: styled-404 pages, 0 data rows). Only
    NAVAll.txt and mfapi.in survive as stable legacy endpoints; everything else now loads via an
    internal API the SPA calls client-side.

    Self-discovery instead of blind guessing: fetch the SPA page RAW (keeping <script> tags),
    pull the __NEXT_DATA__ JSON payload (Next.js often embeds initial data straight in the page)
    plus every /_next/static/*.js bundle it references, fetch a handful of those bundles, and
    regex-harvest every quoted string that looks like a real data endpoint (/api/,
    portal.amfiindia.com, or a .xlsx/.csv/.pdf link) — the same page the browser uses must call
    SOME data URL; find it in the bundle rather than guessing. `hint` is unused for filtering
    (kept for the ops_log label) since bundles are shared across pages. Logs every candidate to
    ops_log — inspectable via run_sql without repo access. Best-effort, never raises."""
    import requests
    # cc#491 attempt_2 (17-Jul, same-day live-tested): attempt_1's generic UA ("compatible;
    # Scorr-MF/1.0") got a real HTTP response but found 0 candidates on BOTH pages — a
    # browser-realistic header set is the standard fix for a bot-detection / SSR-shell page
    # serving different (JS-free) content to non-browser clients. Diagnostics added below
    # (html length, __NEXT_DATA__ found, script-tag/bundle counts) so a failure is now
    # DIAGNOSABLE from ops_log alone, without needing another blind guess-and-redeploy cycle.
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9",
           "Referer": "https://www.amfiindia.com/"}
    candidates = []
    diag = {"html_len": 0, "next_data_found": False, "script_tags": 0, "bundles_found": 0, "bundles_fetched_ok": 0}
    try:
        r = requests.get(page_url, headers=hdr, timeout=30)
        html = r.text or ""
        diag["html_len"] = len(html)
        diag["http"] = r.status_code
        diag["script_tags"] = len(re.findall(r'<script\b', html, re.I))
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        diag["next_data_found"] = bool(m)
        if m:
            for u in re.findall(r'"(https?://[^"]*?(?:/api/[^"]*|\.(?:xlsx?|csv|pdf))[^"]*)"', m.group(1)):
                candidates.append(u)
        base_m = re.match(r'(https?://[^/]+)', page_url)
        base = base_m.group(1) if base_m else "https://www.amfiindia.com"
        # any <script src="..."> — not just the /_next/static/*.js Next.js convention, in case
        # the real build uses a different bundler/path scheme than assumed.
        bundles = re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html, re.I)
        diag["bundles_found"] = len(bundles)
        for b in bundles[:12]:
            b_url = b if b.startswith("http") else (base + b if b.startswith("/") else base + "/" + b)
            try:
                br = requests.get(b_url, headers=hdr, timeout=20)
                body = br.text or ""
                diag["bundles_fetched_ok"] += 1
                for u in re.findall(r'["\'](/api/[a-zA-Z0-9\-/_.]*)["\']', body):
                    candidates.append(base + u)
                for u in re.findall(r'["\'](https?://portal\.amfiindia\.com/[a-zA-Z0-9\-/_.]*)["\']', body):
                    candidates.append(u)
                for u in re.findall(r'["\'](https?://[a-zA-Z0-9.\-]*amfiindia\.com/[a-zA-Z0-9\-/_.]*api[a-zA-Z0-9\-/_.]*)["\']', body):
                    candidates.append(u)
            except Exception:
                continue
    except Exception as e:
        _oplog(cur, "MF_AMFI_DISCOVERY_ERROR", {"page": page_url, "hint": hint, "error": str(e)[:200]})
        return []
    seen, uniq = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    _oplog(cur, "MF_AMFI_DISCOVERY", {"page": page_url, "hint": hint, "n_candidates": len(uniq), "diag": diag,
                                       "sample": uniq[:20]})
    return uniq


# cc#498 step_4_aaum_bonus: the TER populate-* endpoints turned out to be real (founder
# DevTools capture) — probe the obvious AAUM equivalents on the same convention. Bounded effort
# only, per spec ("do not burn the session guessing"): a handful of quick GETs, every candidate
# logged to ops_log, and if none hit, stop rather than open-ended guessing.
_AMFI_AAUM_POPULATE_CANDIDATES = [
    "https://www.amfiindia.com/api/populate-aaum",
    "https://www.amfiindia.com/api/populate-aum-data",
    "https://www.amfiindia.com/api/populate-average-aum",
    "https://www.amfiindia.com/api/populate-aaum-data",
    "https://www.amfiindia.com/api/populate-aum",
]


def _probe_aaum_populate_api(cur):
    """cc#498 step_4_aaum_bonus: try the AAUM variants of the now-verified TER populate-*
    convention. Every candidate is logged (stage=aaum_bonus) so a miss leaves a concrete
    "tried X, Y, Z" trail for the founder's next DevTools capture, not silence."""
    import requests
    hdr = {"User-Agent": "Scorr-MF/1.0"}
    for url in _AMFI_AAUM_POPULATE_CANDIDATES:
        try:
            r = requests.get(url, headers=hdr, timeout=20)
            ct = (r.headers.get("content-type") or "").lower()
            ok = r.status_code == 200 and "json" in ct
            rows = _parse_aum_json(r.text) if ok else []
            _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "aaum_bonus", "url": url,
                   "http": r.status_code, "ct": ct[:40], "rows": len(rows)})
            if rows:
                return rows, {"url": url, "source": "populate_api"}
        except Exception as e:
            _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "aaum_bonus", "url": url, "error": str(e)[:160]})
    return [], None


def _probe_aum_sources(cur, today=None):
    """Try the founder's AUM endpoints in order (SPA JSON API -> legacy ASPX -> form POST). Logs
    each probe (status/content-type/len/rows) to ops_log. Returns (parsed[(name,cr)], used_desc).

    cc#498: tries the populate-* AAUM bonus probe FIRST (see _probe_aaum_populate_api).
    cc#491 VERIFIED_SOURCE_INTEL_17JUL: every URL in _AUM_PROBE below is now confirmed DEAD (the
    Next.js SPA relaunch) — falls to live-discovered candidates from _discover_amfi_endpoints
    next; _AUM_PROBE stays only as a last-resort fallback in case AMFI restores an old path."""
    import requests
    best, used = _probe_aaum_populate_api(cur)
    if best:
        return best, used
    for u in _discover_amfi_endpoints(cur, _AMFI_AAUM_PAGE, 'aum'):
        try:
            hdr = {"User-Agent": "Scorr-MF/1.0", "Accept": "application/json, text/html;q=0.8"}
            r = requests.get(u, headers=hdr, timeout=45)
            ct = (r.headers.get("content-type") or "").lower()
            body = r.text or ""
            rows = []
            if "json" in ct or body[:1] in ("{", "["):
                rows = _parse_aum_json(body)
            if not rows and ("html" in ct or "<table" in body.lower()):
                rows = _parse_aaum_html(body)
            _oplog(cur, "MF_AUM_PROBE_DISCOVERED", {"url": u, "http": r.status_code, "rows": len(rows)})
            if len(rows) > len(best):
                best, used = rows, {"url": u, "source": "discovery"}
            if len(best) >= 50:
                break
        except Exception as e:
            _oplog(cur, "MF_AUM_PROBE_DISCOVERED", {"url": u, "error": str(e)[:140]})
    if best:
        return best, used
    for method, url, data in _AUM_PROBE:
        try:
            hdr = {"User-Agent": "Scorr-MF/1.0", "Accept": "application/json, text/html;q=0.8"}
            r = (requests.get(url, headers=hdr, timeout=60) if method == "GET"
                 else requests.post(url, data=data, headers=hdr, timeout=90))
            ct = (r.headers.get("content-type") or "").lower()
            body = r.text or ""
            rows = []
            if "json" in ct or body[:1] in ("{", "["):
                rows = _parse_aum_json(body)
            if not rows and ("html" in ct or "<table" in body.lower()):
                rows = _parse_aaum_html(body)
            _oplog(cur, "MF_AUM_PROBE", {"url": url, "method": method, "http": r.status_code,
                                         "ct": ct[:40], "len": len(body), "rows": len(rows)})
            if len(rows) > len(best):
                best, used = rows, {"url": url, "method": method, "rows": len(rows)}
            if len(best) >= 50:
                break
        except Exception as e:
            _oplog(cur, "MF_AUM_PROBE", {"url": url, "method": method, "error": str(e)[:140]})
    # last: the form-scrape path (numeric Year/Quarter option IDs), if still nothing
    if not best:
        try:
            selects = _amfi_form_options(cur)
            yv = next((v for k in selects for v, _ in selects[k] if "year" in k.lower()), "")
            qv = next((v for k in selects for v, _ in selects[k]
                       if any(x in k.lower() for x in ("quarter", "aaum", "period"))), "")
            r = requests.post(_AMFI_AAUM_URL, data={"AUmType": "S", "AumCatType": "Typewise",
                              "MF_Name": "-1", "Year": yv, "Quarter": qv, "option": "1"},
                              headers={"User-Agent": "Scorr-MF/1.0"}, timeout=90)
            rows = _parse_aaum_html(r.text or "")
            _oplog(cur, "MF_AUM_PROBE", {"url": "form_post", "year": yv, "quarter": qv, "rows": len(rows)})
            if rows:
                best, used = rows, {"url": "form_post", "rows": len(rows)}
        except Exception as e:
            _oplog(cur, "MF_AUM_PROBE", {"url": "form_post", "error": str(e)[:140]})
    return best, used


def fetch_amfi_aum(cur, today=None):
    """PHASE 1: scheme-wise AAUM via the multi-source prober (SPA JSON / legacy ASPX / form POST),
    lakh-or-cr auto-detected, fuzzy-mapped to Direct-Growth mf_master rows. Best-effort — returns
    an error dict (not fatal) if AMFI is fully closed; the orchestrator then falls back to the
    proxy candidate set for NAV+returns (founder unblock: never wait on a perfect AUM source).

    cc#491: fixed a pre-existing NameError (fy/q were referenced in the stats dict but never
    assigned — this function has never actually reached that line without crashing whenever
    parsed was non-empty). Also added the HARD overwrite guard (id=4334): never clobber an
    existing non-NULL aum_cr — several schemes carry manually-sourced curated values that
    were accidentally wiped once already (cc#477 dup incident)."""
    try:
        import pandas as pd  # noqa: F401
    except Exception as e:
        _oplog(cur, "MF_AUM_ERROR", {"stage": "import pandas", "error": str(e)[:200]})
        return {"error": "pandas unavailable"}
    fy, q = _amfi_aaum_quarter(today)
    parsed, used = _probe_aum_sources(cur, today)
    if not parsed:
        _oplog(cur, "MF_AUM_ERROR", {"stage": "all_sources_failed",
                                     "note": "AMFI AUM unavailable — returns run over proxy candidate set"})
        return {"error": "no AAUM rows parsed", "used": used}

    # Map each AAUM scheme -> the Direct-Growth AMFI row in mf_master by token-overlap.
    cur.execute("SELECT scheme_code, name FROM mf_master WHERE source='amfi' "
                "AND name ILIKE '%%direct%%' AND name ILIKE '%%growth%%'")
    universe = [(sc, nm, set(_norm_fund(nm))) for sc, nm in cur.fetchall()]
    matched = skipped_existing = 0
    for nm, aaum_cr in parsed:
        toks = set(_norm_fund(nm))
        if not toks:
            continue
        best = None
        for sc, aname, aset in universe:
            if not aset:
                continue
            jac = len(toks & aset) / float(len(toks | aset))
            if best is None or jac > best[0]:
                best = (jac, sc)
        if best and best[0] >= 0.5:
            # cc#491 overwrite guard: WHERE aum_cr IS NULL — never touch a curated value.
            cur.execute("UPDATE mf_master SET aum_cr=%s, updated_at=NOW() "
                        "WHERE scheme_code=%s AND aum_cr IS NULL",
                        (aaum_cr, best[1]))
            if cur.rowcount:
                matched += 1
            else:
                skipped_existing += 1
    cur.execute("SELECT COUNT(*) FROM mf_master WHERE aum_cr > %s", (AUM_THRESHOLD_CR,))
    qualifying = cur.fetchone()[0]
    stats = {"fy": fy, "quarter": q, "aaum_rows": len(parsed), "matched": matched,
             "skipped_existing_curated": skipped_existing,
             "match_rate": round(matched / len(parsed), 3) if parsed else 0,
             "qualifying_gt_5000cr": qualifying, "sample": parsed[:3]}
    _oplog(cur, "MF_AUM_BACKFILL", stats)
    return stats


def _weekly_rolling_12mo(rows):
    """cc#491 CADENCE_RETENTION_FINAL_FOUNDER_17JUL (explicitly labeled FINAL; supersedes the
    earlier same-day nav_policy's daily-overwrite framing AND the prior month-end+2yr-weekly
    _month_end_and_weekly policy): ONE NAV row per scheme per ISO week, trailing 12 months only
    — no month-end special-casing, no long-term accumulation. Storage stays bounded regardless
    of how long a scheme has existed; long-horizon (3y/5y) returns are computed separately from
    the FULL fetched mfapi series (see _compute_returns_from_series), never from this truncated
    stored subset."""
    if not rows:
        return {}
    rows = sorted(rows)
    latest = rows[-1][0]
    floor = latest - timedelta(days=366)
    by_week = {}
    for d, nav in rows:
        if d < floor:
            continue
        iso = d.isocalendar()
        by_week[(iso[0], iso[1])] = (d, nav)
    return {d: nav for d, nav in by_week.values()}


def _pct(cur_nav, past_nav):
    if not past_nav or past_nav <= 0 or not cur_nav:
        return None
    return round((cur_nav / past_nav - 1) * 100, 2)


def _cagr(cur_nav, past_nav, years):
    if not past_nav or past_nav <= 0 or not cur_nav or years <= 0:
        return None
    return round(((cur_nav / past_nav) ** (1.0 / years) - 1) * 100, 2)


def _compute_returns_from_series(series):
    """Pure function: 1w/1m/3m/6m/1y/2y/3y/5y returns from an in-memory (date,nav) series (asc,
    de-duped). Nearest point on/before (latest - horizon) within a tolerance; CAGR from 2y out,
    simple pct below. Computed from the FULL fetched history, not the weekly-rolling-12mo
    storage subset, so 3y/5y stay computable even though only 12 months are persisted."""
    if len(series) < 2:
        return None
    latest_d, latest_nav = series[-1]

    def past(days, tol):
        target = latest_d - timedelta(days=days)
        lo = target - timedelta(days=tol)
        best = None
        for d, nav in series:
            if lo <= d <= target + timedelta(days=min(tol, 3)):
                if best is None or abs((d - target).days) < abs((best[0] - target).days):
                    best = (d, nav)
        return best[1] if best else None

    r1w = _pct(latest_nav, past(7, 5))
    r1m = _pct(latest_nav, past(30, 12))
    r3m = _pct(latest_nav, past(91, 15))
    r6m = _pct(latest_nav, past(182, 18))
    r1y = _pct(latest_nav, past(365, 20))
    r2y = _cagr(latest_nav, past(730, 25), 2.0)
    r3y = _cagr(latest_nav, past(1095, 30), 3.0)
    r5y = _cagr(latest_nav, past(1826, 35), 5.0)
    return {"latest_d": latest_d, "ret_1w": r1w, "ret_1m": r1m, "ret_3m": r3m, "ret_6m": r6m,
            "ret_1y": r1y, "ret_2y": r2y, "ret_3y": r3y, "ret_5y": r5y}


def sync_scheme_nav_and_returns(cur, scheme_code, amfi_code):
    """Replaces the old backfill_scheme_nav()+compute_returns_for_scheme() pair (cc#491
    CADENCE_RETENTION_FINAL_FOUNDER_17JUL): fetch the mfapi full history ONCE, compute returns
    from the FULL in-memory series, persist only the weekly-rolling-12-month subset, and PRUNE
    any existing stored rows for this scheme outside that window on every run — storage never
    grows unbounded regardless of run cadence."""
    code = amfi_code or scheme_code
    try:
        d = json.loads(_http_get(_MFAPI.format(code=code)))
    except Exception as e:
        return {"scheme_code": scheme_code, "error": str(e)[:160]}
    rows = []
    for row in (d.get("data") or []):
        try:
            nd = datetime.strptime(row["date"], "%d-%m-%Y").date()
            nav = float(row["nav"])
        except Exception:
            continue
        rows.append((nd, nav))
    rows.sort()
    ret = _compute_returns_from_series(rows) if rows else None
    if ret:
        cur.execute("""UPDATE mf_master SET ret_1w=%s, ret_1m=%s, ret_3m=%s, ret_6m=%s,
                       ret_1y=%s, ret_2y=%s, ret_3y=%s, ret_5y=%s, returns_asof=%s, updated_at=NOW()
                       WHERE scheme_code=%s""",
                    (ret["ret_1w"], ret["ret_1m"], ret["ret_3m"], ret["ret_6m"], ret["ret_1y"],
                     ret["ret_2y"], ret["ret_3y"], ret["ret_5y"], ret["latest_d"], scheme_code))
    weekly = _weekly_rolling_12mo(rows)
    n = 0
    for nd, nav in weekly.items():
        cur.execute("""INSERT INTO mf_nav_history (scheme_code, nav_date, nav, nav_kind)
                       VALUES (%s,%s,%s,'w')
                       ON CONFLICT (scheme_code, nav_date) DO UPDATE SET nav=EXCLUDED.nav, nav_kind='w'""",
                    (scheme_code, nd, nav))
        n += 1
    floor = (max(weekly.keys()) if weekly else date.today()) - timedelta(days=366)
    cur.execute("DELETE FROM mf_nav_history WHERE scheme_code=%s AND nav_date < %s", (scheme_code, floor))
    return {"scheme_code": scheme_code, "amfi_code": code, "nav_rows_kept": n, "returns": ret is not None}


def prune_nav_history_to_policy(conn=None):
    """One-time cleanup (cc#491 CADENCE_RETENTION_FINAL_FOUNDER_17JUL): shrink the existing
    mf_nav_history (built under the prior month-end+2yr-weekly policy) to the new
    weekly-rolling-12-month shape immediately, per-scheme, without waiting for every scheme's
    next sync_scheme_nav_and_returns() run."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mf_nav_history")
            before = cur.fetchone()[0]
            cur.execute("""
                DELETE FROM mf_nav_history h
                WHERE nav_date < (SELECT MAX(nav_date) FROM mf_nav_history h2
                                   WHERE h2.scheme_code = h.scheme_code) - INTERVAL '366 days'
            """)
            cur.execute("SELECT COUNT(*) FROM mf_nav_history")
            after = cur.fetchone()[0]
            conn.commit()
            _oplog(cur, "MF_NAV_HISTORY_PRUNED", {"before": before, "after": after, "removed": before - after})
            conn.commit()
        return {"before": before, "after": after, "removed": before - after}
    finally:
        if own:
            conn.close()


def ensure_monthly_snapshot_table(cur):
    """cc#491 CADENCE_RETENTION_FINAL_FOUNDER_17JUL: monthly-rolling-12-month AUM/TER snapshot,
    one row per scheme per calendar month, for trend/stake-change analysis without accumulating
    mf_master's point-in-time columns indefinitely."""
    cur.execute("""CREATE TABLE IF NOT EXISTS mf_monthly_snapshot (
        scheme_code TEXT NOT NULL, snapshot_month DATE NOT NULL,
        aum_cr NUMERIC, expense_ratio NUMERIC,
        PRIMARY KEY (scheme_code, snapshot_month))""")


def _snapshot_current_month(cur):
    """Upsert this calendar month's AUM/TER snapshot for every scheme, from mf_master's current
    point-in-time values. Called after the AUM + TER sweeps each run."""
    month = date.today().replace(day=1)
    cur.execute("""INSERT INTO mf_monthly_snapshot (scheme_code, snapshot_month, aum_cr, expense_ratio)
                   SELECT scheme_code, %s, aum_cr, expense_ratio FROM mf_master
                   WHERE aum_cr IS NOT NULL OR expense_ratio IS NOT NULL
                   ON CONFLICT (scheme_code, snapshot_month) DO UPDATE SET
                       aum_cr=COALESCE(EXCLUDED.aum_cr, mf_monthly_snapshot.aum_cr),
                       expense_ratio=COALESCE(EXCLUDED.expense_ratio, mf_monthly_snapshot.expense_ratio)""",
                (month,))
    return cur.rowcount


def prune_monthly_snapshots(conn=None):
    """Rolling 12-month cap (cc#491 CADENCE_RETENTION_FINAL_FOUNDER_17JUL) on mf_monthly_snapshot
    AND mf_holdings — both are monthly-cadence tables that would otherwise accumulate forever."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_monthly_snapshot_table(cur)
            for tbl, col in (("mf_monthly_snapshot", "snapshot_month"), ("mf_holdings", "as_of_month")):
                cur.execute(f"""
                    DELETE FROM {tbl} t
                    WHERE {col} < (SELECT MAX({col}) FROM {tbl} t2
                                    WHERE t2.scheme_code = t.scheme_code) - INTERVAL '366 days'
                """)
            conn.commit()
        return {"pruned": True}
    finally:
        if own:
            conn.close()


def run_mf_returns_backfill(conn=None):
    """cc#477 orchestrator (phases 1-3), server-side. Resumable across the mfapi loop via
    app_config mf_backfill_progress. ops_log progress. Runs the whole thing in one pass
    (~400-600 schemes x 1 req/sec ~= 8-10 min)."""
    import time as _t
    own = conn is None
    conn = conn or _conn()
    t0 = _t.time()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            _ensure_returns_cols(cur)
            conn.commit()
        # phase 1: AUM
        with conn.cursor() as cur:
            aum = fetch_amfi_aum(cur)
            conn.commit()
        # qualifying set: use AUM>5000 ONLY when the AUM probe actually returned fresh AAUM rows;
        # otherwise a few STALE aum_cr rows would shrink the run (6-fund bug 13-Jul). When AUM is
        # unavailable, use the proxy candidate set (founder unblock — never wait on a perfect AUM
        # source; NAV+returns via mfapi are independent).
        aum_ok = not (isinstance(aum, dict) and aum.get("error"))
        with conn.cursor() as cur:
            qual, universe_mode = [], "proxy_candidate_set"
            if aum_ok:
                cur.execute("SELECT scheme_code, amfi_code FROM mf_master WHERE aum_cr > %s "
                            "ORDER BY aum_cr DESC", (AUM_THRESHOLD_CR,))
                qual = cur.fetchall()
                universe_mode = "aum_gt_5000"
            if not qual:
                qual = _candidate_scheme_set(cur)
                universe_mode = "proxy_candidate_set"
            cur.execute("SELECT value FROM app_config WHERE key='mf_backfill_progress'")
            r = cur.fetchone()
            _oplog(cur, "MF_RETURNS_UNIVERSE", {"mode": universe_mode, "size": len(qual)})
            conn.commit()
        done_prefix = (r[0] if r else "") or ""
        pending = [(sc, ac) for sc, ac in qual if sc > done_prefix]
        # phase 2+3: per-scheme NAV history + returns
        n_nav = n_ret = fails = 0
        for i, (sc, ac) in enumerate(pending):
            try:
                with conn.cursor() as cur:
                    res = sync_scheme_nav_and_returns(cur, sc, ac)
                    if not res.get("error"):
                        n_nav += res.get("nav_rows_kept", 0)
                        if res.get("returns"):
                            n_ret += 1
                    else:
                        fails += 1
                    cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_backfill_progress',%s,NOW()) "
                                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (sc,))
                    conn.commit()
            except Exception as e:
                fails += 1
                log.warning(f"mf backfill {sc}: {e}")
                try: conn.rollback()
                except Exception: pass
            if (i + 1) % 25 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "MF_RETURNS_PROGRESS", {"done": i + 1, "of": len(pending),
                            "nav_rows": n_nav, "returns_set": n_ret, "fails": fails,
                            "elapsed_min": round((_t.time() - t0) / 60, 1)})
                    conn.commit()
            _t.sleep(1.0)   # polite mfapi rate-limit (1 req/sec)
        # done: reset checkpoint + category averages
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_config WHERE key='mf_backfill_progress'")
            cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_returns_backfill_run','done',NOW()) "
                        "ON CONFLICT (key) DO UPDATE SET value='done', updated_at=NOW()")
            summary = {"aum": aum, "qualifying": len(qual), "processed": len(pending),
                       "nav_rows": n_nav, "returns_set": n_ret, "fails": fails,
                       "elapsed_min": round((_t.time() - t0) / 60, 1)}
            _oplog(cur, "MF_RETURNS_BACKFILL_DONE", summary)
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


def mf_weekly_refresh(conn=None):
    """cc#477 phase_4: Saturday 06:30 IST. Refresh AMFI latest NAV (adds Friday rows), then for
    the qualifying set re-sync the weekly-rolling-12-month NAV window + recompute returns from
    the full mfapi series (sync_scheme_nav_and_returns re-selects + prunes idempotently)."""
    own = conn is None
    conn = conn or _conn()
    try:
        run_amfi_nav(conn)   # newest daily/Friday NAV into mf_nav_history
        with conn.cursor() as cur:
            _ensure_returns_cols(cur)
            cur.execute("SELECT scheme_code, amfi_code FROM mf_master WHERE aum_cr > %s", (AUM_THRESHOLD_CR,))
            qual = cur.fetchall()
            if len(qual) < 50:   # AUM unpopulated/stale -> proxy candidate set (same as backfill)
                qual = _candidate_scheme_set(cur)
            conn.commit()
        import time as _t
        n = 0
        for sc, ac in qual:
            try:
                with conn.cursor() as cur:
                    sync_scheme_nav_and_returns(cur, sc, ac)   # re-syncs weekly window + prunes idempotently
                    conn.commit()
                n += 1
            except Exception as e:
                log.warning(f"mf_weekly {sc}: {e}")
                try: conn.rollback()
                except Exception: pass
            _t.sleep(1.0)
        with conn.cursor() as cur:
            _oplog(cur, "MF_WEEKLY_REFRESH", {"qualifying": len(qual), "refreshed": n}); conn.commit()
        return {"qualifying": len(qual), "refreshed": n}
    finally:
        if own:
            conn.close()


def mf_aum_monthly_refresh(conn=None):
    """cc#477 phase_4, repointed by cc#491 CADENCE_RETENTION_FINAL_FOUNDER_17JUL: the monthly
    (3rd calendar day) cadence now runs the full comprehensive monthly job — AUM, TER, holdings,
    monthly AUM/TER snapshot, rolling-12-month prune — via run_v15_wiring(), instead of just the
    AUM re-fetch, so the automated monthly cadence and the manual /wire_all ARM trigger share one
    implementation rather than duplicating logic. Scoring/category-averages stay excluded per
    SCOPE_RESHAPE_FOUNDER_17JUL (see run_v15_wiring's own docstring)."""
    return run_v15_wiring(conn)


# ── admin triggers + read endpoints (cc#467 reads these) ───────────────────────────
@router.post("/api/v15/mf/returns_backfill")
def mf_returns_backfill_arm():
    """cc#477: ARM the server-side returns backfill (scheduler picks up the flag within a tick
    and runs phases 1-3 off the main request path). Returns immediately."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_returns_backfill_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
        conn.commit()
    return {"armed": True, "flag": "mf_returns_backfill_run=pending"}

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


# ── cc#480: thin reads for the /v15 page (search / hero / screener) ─────────────────
_V15_CATS = ["Flexi Cap", "Large & Mid Cap", "Large Cap", "Mid Cap", "Small Cap", "Multi Cap",
             "ELSS", "Index", "Hybrid", "Focused", "Value", "Contra", "Banking", "Pharma",
             "Technology", "Infrastructure", "Debt", "Liquid"]


def _derive_cat(name, category):
    """Best-effort category from a real scheme name when mf_master.category is NULL
    (the AMFI universe carries names but not a category column). Honest — derived from
    the fund's own name, never invented."""
    if category:
        return category
    n = (name or "").lower()
    for c in _V15_CATS:
        if c.lower() in n:
            return c
    return None


def _derive_plan(name):
    n = (name or "").lower()
    kind = "Direct" if "direct" in n else ("Regular" if "regular" in n else None)
    opt = "Growth" if "growth" in n else ("IDCW" if ("idcw" in n or "dividend" in n) else None)
    return " · ".join([x for x in (kind, opt) if x]) or None


@router.get("/api/v15/search")
def v15_search(q: str = "", limit: int = 12):
    """cc#480: fund autocomplete over mf_master (name/amc/category). Prefers Direct-Growth
    plans. Thin read — no scoring."""
    q = (q or "").strip()
    if not q:
        return {"count": 0, "results": []}
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        # cc#520: debt strays (Banking & PSU) must not appear in search results.
        cur.execute("""SELECT scheme_code, name, amc, category FROM mf_master
                       WHERE (name ILIKE %s OR amc ILIKE %s OR category ILIKE %s)
                         AND category = ANY(%s)
                       ORDER BY (name ILIKE '%%direct%%' AND name ILIKE '%%growth%%') DESC,
                                curated DESC, length(name), name
                       LIMIT %s""",
                    (f"%{q}%", f"%{q}%", f"%{q}%", list(EQUITY_CATEGORY_WHITELIST), max(1, min(limit, 25))))
        rows = [{"scheme_code": sc, "name": nm, "amc": amc,
                 "category": _derive_cat(nm, cat), "plan": _derive_plan(nm)}
                for sc, nm, amc, cat in cur.fetchall()]
    return {"count": len(rows), "results": rows}


def _ret_window_state(value, inception, years_needed):
    """cc#528 item 3: honest empty state for a returns window. 'value' when we have the
    number; 'too_young' when inception proves the fund hasn't existed long enough to have it
    (a fact, not missing data); 'not_sourced' when the fund is old enough but we don't have it
    (real gap, not yet scraped) -- including when inception itself is unknown, since we can't
    prove either way."""
    if value is not None:
        return "value"
    if inception is None:
        return "not_sourced"
    age_years = (date.today() - inception).days / 365.25
    return "too_young" if age_years < years_needed else "not_sourced"


@router.get("/api/v15/fund/{scheme_code}")
def v15_fund(scheme_code: str):
    """cc#480: thin hero read — meta + returns bindings.
    cc#519 GO-LIVE: adds MQS/pillar scores (mf_scores, null when unscored -- page shows
    "Not yet scored", never a fabricated value) and deterministic red flags computed from
    real mf_master/mf_category_averages data only. Holdings-derived flags (single-stock
    weight, top-10 concentration, GVM-weighted portfolio avg) are intentionally NOT computed
    here -- cc#519 investigation found mf_holdings' June-2026 bulk load returns the identical
    ~305-stock list for all 518 schemes (a Consumption fund and a Large&Mid-Cap fund show the
    same top-10 weights to 2dp), i.e. template/placeholder data, not real per-fund disclosures.
    Wiring red flags to it would present fabricated concentration numbers as real -- flagged
    for founder re-scrape, not silently shipped."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        try:
            _ensure_returns_cols(cur); conn.commit()
        except Exception:
            conn.rollback()
        cur.execute("""SELECT m.scheme_code, m.name, m.amc, m.category, m.expense_ratio, m.aum_cr,
                              m.crisil_rank, m.manager, m.inception,
                              m.ret_1w, m.ret_1m, m.ret_3m, m.ret_6m, m.ret_1y, m.ret_2y, m.ret_3y,
                              m.ret_5y, m.returns_asof,
                              s.mqs, s.q_score, s.r_score, s.c_score, s.s_score, s.computed_at,
                              s.basis_pct, s.basis_label, s.coverage_pct,
                              ca.avg_expense_ratio
                       FROM mf_master m
                       LEFT JOIN mf_scores s ON s.scheme_code = m.scheme_code
                       LEFT JOIN mf_category_averages ca ON ca.category = m.category
                       WHERE m.scheme_code=%s""", (scheme_code,))
        r = cur.fetchone()
        if not r:
            return {"error": "not found"}
        cols = [d[0] for d in cur.description]
        m = dict(zip(cols, r))
    cat_avg_er = m.pop("avg_expense_ratio", None)
    m["category"] = _derive_cat(m.get("name"), m.get("category"))
    m["plan"] = _derive_plan(m.get("name"))
    for k in ("expense_ratio", "aum_cr", "ret_1w", "ret_1m", "ret_3m", "ret_6m", "ret_1y",
              "ret_2y", "ret_3y", "ret_5y", "mqs", "q_score", "r_score", "c_score", "s_score",
              "basis_pct", "coverage_pct"):
        if m.get(k) is not None:
            m[k] = float(m[k])
    m["ret_3y_state"] = _ret_window_state(m.get("ret_3y"), m.get("inception"), 3)
    m["ret_5y_state"] = _ret_window_state(m.get("ret_5y"), m.get("inception"), 5)
    m["inception"] = str(m["inception"]) if m.get("inception") else None
    m["returns_asof"] = str(m["returns_asof"]) if m.get("returns_asof") else None
    m["computed_at"] = str(m["computed_at"]) if m.get("computed_at") else None
    # cc#546: Portfolio GVM (weighted-avg look-through GVM of holdings) now activates off the Q
    # pillar computed in compute_mf_scores. portfolio_gvm is the weighted GVM value on the native
    # 0-10 scale (= q_score / 10, the inverse of the x10 pillar scaler); coverage_pct is the % of
    # disclosed weight that resolved to a live GVM row. "Insufficient holdings data" is keyed ONLY
    # on coverage (< 60 or none), never on q_score being NULL for some other reason.
    cov = m.get("coverage_pct")
    if m.get("q_score") is not None:
        m["portfolio_gvm"] = round(m["q_score"] / 10.0, 2)
        m["portfolio_gvm_coverage_pct"] = cov
        m["portfolio_gvm_state"] = "value"
    else:
        m["portfolio_gvm"] = None
        m["portfolio_gvm_coverage_pct"] = cov
        m["portfolio_gvm_state"] = "insufficient_holdings"

    flags = []
    er, aum = m.get("expense_ratio"), m.get("aum_cr")
    if er is not None and cat_avg_er is not None and er > float(cat_avg_er):
        flags.append({"kind": "cost",
                       "text": f"Expense ratio {er:.2f}% is above the category average "
                               f"{float(cat_avg_er):.2f}%."})
    if aum is not None and aum < 500:
        flags.append({"kind": "size",
                       "text": f"AUM ₹{aum:,.0f} Cr is below ₹500 Cr -- smaller "
                               f"funds carry higher volatility/liquidity risk."})
    m["red_flags"] = flags
    return {"fund": m}


@router.get("/api/v15/stats")
def v15_stats():
    """cc#519: live scored-fund count for the page header chip -- avoids hardcoding a count
    that goes stale after the next nightly recompute."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("SELECT COUNT(*) FROM mf_scores")
        scored = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mf_master WHERE category = ANY(%s)",
                    (list(EQUITY_CATEGORY_WHITELIST),))
        universe = cur.fetchone()[0]
    return {"scored": scored, "universe": universe}


@router.get("/api/v15/screener")
def v15_screener(category: str = "", sort: str = "mqs", limit: int = 40):
    """cc#480: screener rows for a category tab. Matches mf_master.category OR the scheme name
    (real AMFI names carry the category), Direct-Growth only. AUM>5000 filter activates
    automatically once aum_cr is populated (cc#477).
    cc#519: joins mf_scores for the MQS column; default sort is now mqs desc (was 1y) now that
    the column has real data. cc#519 fix: when `category` is one of the 11 canonical whitelist
    values, filter with an EXACT match instead of ILIKE substring -- ILIKE '%Mid Cap Fund%' also
    matches 'Large & Mid Cap Fund' (it ends with that exact substring), which would leak
    Large&Mid rows into the plain Mid Cap tab. Free-text category input (legacy callers) keeps
    the ILIKE substring fallback."""
    cat = (category or "").strip()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        try:
            _ensure_returns_cols(cur); conn.commit()
        except Exception:
            conn.rollback()
        # cc#520: debt strays (Banking & PSU) must not appear in the screener.
        where = ["m.name ILIKE '%%direct%%'", "m.name ILIKE '%%growth%%'", "m.category = ANY(%s)"]
        params = [list(EQUITY_CATEGORY_WHITELIST)]
        if cat in EQUITY_CATEGORY_WHITELIST:
            where.append("m.category = %s")
            params.append(cat)
        elif cat:
            where.append("(m.category ILIKE %s OR m.name ILIKE %s)")
            params += [f"%{cat}%", f"%{cat}%"]
        if sort == "1y":
            order = "ret_1y DESC NULLS LAST, aum_cr DESC NULLS LAST, name"
        elif sort == "aum":
            order = "aum_cr DESC NULLS LAST, ret_1y DESC NULLS LAST, name"
        else:
            order = "mqs DESC NULLS LAST, ret_1y DESC NULLS LAST, name"
        sql = ("SELECT m.scheme_code, m.name, m.amc, m.category, m.crisil_rank, "
               "m.expense_ratio, m.aum_cr, m.ret_1y, m.ret_3y, m.inception, s.mqs "
               "FROM mf_master m LEFT JOIN mf_scores s ON s.scheme_code = m.scheme_code WHERE "
               + " AND ".join(where) + " ORDER BY " + order + " LIMIT %s")
        params.append(max(1, min(limit, 80)))
        cur.execute(sql, params)
        rows = []
        for sc, nm, amc, c, cr, er, aum, r1y, r3y, inception, mqs in cur.fetchall():
            rows.append({"scheme_code": sc, "name": nm, "amc": amc,
                         "category": _derive_cat(nm, c),
                         "crisil_rank": cr, "expense_ratio": float(er) if er is not None else None,
                         "aum_cr": float(aum) if aum is not None else None,
                         "ret_1y": float(r1y) if r1y is not None else None,
                         "ret_3y": float(r3y) if r3y is not None else None,
                         "ret_3y_state": _ret_window_state(float(r3y) if r3y is not None else None, inception, 3),
                         "mqs": float(mqs) if mqs is not None else None})
    return {"category": cat, "count": len(rows), "results": rows}


@router.get("/api/v15/mf/search")
def mf_search(q: str = "", limit: int = 20):
    q = (q or "").strip()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        if q:
            cur.execute("""SELECT scheme_code, name, amc, category, curated
                           FROM mf_master WHERE name ILIKE %s OR amc ILIKE %s OR category ILIKE %s
                           ORDER BY curated DESC, name LIMIT %s""",
                        (f"%{q}%", f"%{q}%", f"%{q}%", max(1, min(limit, 50))))
        else:
            cur.execute("""SELECT scheme_code, name, amc, category, curated
                           FROM mf_master WHERE curated ORDER BY category, name LIMIT %s""", (max(1, min(limit, 50)),))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"count": len(rows), "results": rows}


# ── cc#491: wire the remaining V15 data layers over the canonical equity universe ──────
# (AUM sweep fix, expense ratio, holdings orchestration framework, MQS scoring, category
# averages). AUTO_MODE_GRANT_17JUL: claim -> implement all 5 steps -> push -> verify ->
# finalize, no per-step OKs. Sequenced after #493/#495/#496 per the founder's queue.

def _canonical_equity_universe(cur):
    """V15_EQUITY_UNIVERSE_V1 (session_log id=4334, locked 16-Jul, AFTER this task was filed):
    mf_master rows in the cc#520 hard category whitelist (the 11 equity-oriented categories,
    excluding Banking & PSU debt -- out of scope for the equity-focused V15 MQS). Supersedes the
    original 635-fund proxy candidate set from the pre-amendment spec."""
    cur.execute("""SELECT scheme_code, amfi_code, name, category FROM mf_master
                   WHERE category = ANY(%s)""", (list(EQUITY_CATEGORY_WHITELIST),))
    return cur.fetchall()


# ── step_2: expense ratio (TER) ─────────────────────────────────────────────────────
# cc#466's own docstring named Moneycontrol as the intended ER cross-check source, but no
# Moneycontrol scraper exists anywhere in this codebase yet, and this environment has no
# outbound internet access to discover/verify a current scrape target's HTML structure —
# building one blind risks the exact same "probed everything, 0 rows" outcome the AMFI AUM
# source hit before cc#477's unblock. AMFI itself publishes a SEBI-mandated Total Expense
# Ratio disclosure — try that first, per the spec's own explicit fallback: "use AMFI TER
# disclosure if available, else document the best available source and coverage %."
# cc#498 VERIFIED_17JUL_FOUNDER_DEVTOOLS: the REAL live AMFI TER API, captured via founder
# Chrome DevTools 17-Jul and confirmed server-callable by Claude web (plain GET, JSON, no
# auth/cookies beyond a normal UA) — supersedes every dead-URL/discovery-probe attempt from
# cc#491 (the SPA relaunch killed the static pages, but this is the actual API the SPA itself
# calls, not a guess).
_AMFI_TER_MONTH_URL = "https://www.amfiindia.com/api/populate-ter-month"
_AMFI_TER_DATA_URL = "https://www.amfiindia.com/api/populate-te-rdata-revised"
_AMFI_SUBCAT_URL = "https://www.amfiindia.com/api/populate-sub-category"
# populate-* naming convention is established (populate-ter-month, populate-te-rdata-revised,
# populate-sub-category all real) — these are step_1's candidates for the MF_ID<->AMC map,
# untested until this runs live on Railway.
_AMFI_MF_ID_URLS = [
    "https://www.amfiindia.com/api/populate-mutual-fund",
    "https://www.amfiindia.com/api/populate-mf",
    "https://www.amfiindia.com/api/populate-mutualfund",
    "https://www.amfiindia.com/api/populate-amc",
]
# cc#498 attempt_6: the populate-sub-category IDs below (Large Cap=74 etc.) were LIVE-PROVEN
# not to be what populate-te-rdata-revised's strCat parameter expects — a real, major AMC with
# real large-cap schemes returned zero rows filtered by strCat=74 but real data with strCat
# omitted entirely. fetch_expense_ratio no longer uses strCat at all (filters equity
# client-side on SchemeCat_Desc instead — see _ter_row_is_equity). Left unused rather than
# deleted in case a verified correct mapping surfaces later: _AMFI_SUBCAT_URL is real and
# live (verified 17-Jul), only the ID-to-strCat correspondence assumed here was wrong.
_EQUITY_SUBCATS_UNVERIFIED_FOR_STRCAT = {
    "Multi Cap": 73, "Large Cap": 74, "Large & Mid Cap": 75, "Mid Cap": 76, "Small Cap": 77,
    "Flexi Cap": 78, "Dividend Yield": 79, "Value": 80, "Contra": 81, "Focused": 82,
    "Sectoral": 83, "Thematic": 84, "ELSS": 85,
}


def _amfi_ter_months_desc(cur):
    """cc#498 step_1: GET populate-ter-month for the current FY, return ALL listed months
    parsed and sorted NEWEST-first. TER publication lags the calendar month by several weeks
    (SEBI compliance cycle) — the newest LISTED month in the dropdown is not reliably the
    newest PUBLISHED one (live-observed 17-Jul: July-2026 was listed but returned a
    well-formed EMPTY result for every one of 150+ AMC x subcategory combos tried), so callers
    should probe candidates in order and verify data exists rather than assuming the first one
    works. AMFI FY runs Apr-Mar."""
    import requests
    today = date.today()
    fy = f"{today.year}-{today.year+1}" if today.month >= 4 else f"{today.year-1}-{today.year}"
    try:
        r = requests.get(_AMFI_TER_MONTH_URL, params={"year": fy},
                          headers={"User-Agent": "Scorr-MF/1.0"}, timeout=30)
        rows = r.json()
        _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "months", "fy": fy, "http": r.status_code,
               "n": len(rows) if isinstance(rows, list) else None,
               "sample": rows[:3] if isinstance(rows, list) else str(rows)[:300]})
        if isinstance(rows, list) and rows:
            months = [r0.get("MonthNumber") or r0.get("monthNumber") or r0.get("Month")
                      for r0 in rows if isinstance(r0, dict)]
            months = [m for m in months if m]

            def _month_key(m):
                try:
                    mm, yyyy = m.split('-')
                    return (int(yyyy), int(mm))
                except Exception:
                    return (0, 0)
            return sorted(set(months), key=_month_key, reverse=True)
    except Exception as e:
        _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "months", "fy": fy, "error": str(e)[:200]})
    return []


def _discover_ter_mf_ids(cur):
    """cc#498 step_1: find the MF_ID<->AMC map via the established populate-* naming
    convention (populate-mf, verified live)."""
    import requests
    hdr = {"User-Agent": "Scorr-MF/1.0"}
    for url in _AMFI_MF_ID_URLS:
        try:
            r = requests.get(url, headers=hdr, timeout=30)
            ct = (r.headers.get("content-type") or "").lower()
            ok = r.status_code == 200 and "json" in ct
            data = r.json() if ok else None
            _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "mf_id_map", "url": url,
                   "http": r.status_code, "ct": ct[:40],
                   "sample": str(data)[:400] if data is not None else None})
            if ok and isinstance(data, list) and data:
                return data
        except Exception as e:
            _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "mf_id_map", "url": url, "error": str(e)[:160]})
    return []


def _amfi_ter_page(cur, mf_id, month, page, page_size=500, subcat_id=None):
    """cc#498: one page of populate-te-rdata-revised. Returns (rows, meta); ([], {}) on error.

    cc#498 attempt_6 live bug fix: strCat is OMITTED by default now — the raw-body diagnostic
    (17-Jul) proved strCat=74 (this file's guessed "Large Cap" id, sourced from the unrelated
    populate-sub-category endpoint) filters to ZERO rows even for a real, major AMC with real
    large-cap schemes, while the SAME request with strCat dropped entirely returns real data
    immediately. subcat_id is kept as an optional param in case a verified mapping surfaces
    later, but fetch_expense_ratio no longer passes one — equity filtering happens client-side
    on the real `SchemeCat_Desc` field instead (see _ter_row_is_equity)."""
    import requests
    params = {"MF_ID": mf_id, "Month": month, "strType": 1, "page": page, "pageSize": page_size}
    if subcat_id is not None:
        params["strCat"] = subcat_id
    try:
        r = requests.get(_AMFI_TER_DATA_URL, params=params,
                          headers={"User-Agent": "Scorr-MF/1.0"}, timeout=45)
        d = r.json()
        return (d.get("data") or []), (d.get("meta") or {})
    except Exception as e:
        _oplog(cur, "MF_TER_API_ERROR", {"mf_id": mf_id, "subcat": subcat_id, "month": month,
               "page": page, "error": str(e)[:160]})
        return [], {}


def _ter_diagnostic_probe(cur, mf_id, month):
    """cc#498 attempt_5 diagnostic: every (AMC, month) combination tried across 4 live attempts
    has returned a well-formed but EMPTY result, including a real major AMC (Aditya Birla Sun
    Life) across all 5 listed months on Large Cap — deeper than the publication-lag hypothesis
    alone explains. Logs the FULL raw response body (not just parsed meta) for a handful of
    parameter variations (drop strCat, strCat as int, strType=0, drop strType entirely) so the
    actual failure mode is inspectable via ops_log rather than guessed at blind."""
    import requests
    hdr = {"User-Agent": "Scorr-MF/1.0"}
    variants = [
        ("no_strCat", {"MF_ID": mf_id, "Month": month, "strType": 1, "page": 1, "pageSize": 20}),
        ("strCat_int", {"MF_ID": mf_id, "Month": month, "strCat": 74, "strType": 1, "page": 1, "pageSize": 20}),
        ("strType_0", {"MF_ID": mf_id, "Month": month, "strCat": 74, "strType": 0, "page": 1, "pageSize": 20}),
        ("no_strType", {"MF_ID": mf_id, "Month": month, "strCat": 74, "page": 1, "pageSize": 20}),
    ]
    for label, params in variants:
        try:
            r = requests.get(_AMFI_TER_DATA_URL, params=params, headers=hdr, timeout=30)
            _oplog(cur, "MF_TER_API_DIAGNOSTIC", {"variant": label, "params": params,
                   "http": r.status_code, "body": (r.text or "")[:800]})
        except Exception as e:
            _oplog(cur, "MF_TER_API_DIAGNOSTIC", {"variant": label, "params": params, "error": str(e)[:160]})
        import time as _t
        _t.sleep(0.5)


def _num_or_none(v):
    try:
        return float(str(v).replace('%', '').replace(',', '').strip())
    except Exception:
        return None


def _ter_row_total(row):
    """cc#498 attempt_6: REAL field names confirmed live (17-Jul raw response) — D_TER (direct
    plan total TER) and R_TER (regular plan total TER) are separate columns on the SAME row
    (one row = one scheme, not one row per plan), not a single generic "Total TER" field. The
    original guessed key list never matched, silently falling through to a component-summing
    fallback that would have SUMMED regular+direct+brokerage+statutory together into one
    inflated, wrong number. Prefers D_TER (Direct plan, matches this pipeline's existing
    Direct-Growth convention elsewhere) then R_TER."""
    for key in ("D_TER", "R_TER"):
        if key in row:
            n = _num_or_none(row[key])
            if n is not None:
                return n
    return None


def _ter_row_scheme_name(row):
    for key in ("Scheme_Name", "Scheme Name", "SchemeName", "scheme_name"):
        if key in row and row[key]:
            return str(row[key]).strip()
    return None


def _ter_row_is_equity(row):
    """cc#498 attempt_6: strCat-based server-side filtering is dropped (see _amfi_ter_page) —
    equity filtering now happens client-side on the real SchemeCat_Desc field (confirmed live,
    e.g. "Other Scheme - FoF Domestic" for a non-equity fund)."""
    cat = str(row.get("SchemeCat_Desc") or "").lower()
    return "equity" in cat


def fetch_expense_ratio(cur):
    """step_2, cc#498: real AMFI TER API (founder DevTools capture, verified live 17-Jul —
    supersedes cc#491's dead-URL/discovery-probe attempts). cc#498 attempt_6: strCat-based
    server-side category filtering is DROPPED (live-proven wrong ID mapping — see
    _amfi_ter_page) in favor of fetching each AMC's FULL scheme list (all categories) for the
    confirmed-published month, then filtering to equity client-side on the real SchemeCat_Desc
    field. This also cuts the crawl from 728 combos (56 AMCs x 13 guessed subcats) to 56 (one
    per AMC), since no subcategory iteration is needed at all. Extracts D_TER (Direct plan)
    preferring it over R_TER (Regular) — confirmed live: both are separate columns on the SAME
    row (one row = one scheme), not one row per plan. Fuzzy-matches to mf_master exactly like
    the AUM sweep. Same overwrite guard as before: WHERE expense_ratio IS NULL on this initial
    pass (existing curated values untouched)."""
    import time
    months = _amfi_ter_months_desc(cur)
    if not months:
        _oplog(cur, "MF_TER_ERROR", {"stage": "no_month", "note": "populate-ter-month returned nothing usable"})
        return {"error": "no month found"}

    discovered = _discover_ter_mf_ids(cur)
    # cc#498 live bug fix: the real populate-mf response uses "mfId" (verified 17-Jul:
    # [{'tableId': 'Table1', 'mfId': '62', 'mfName': '360 ONE Mutual Fund'}, ...]) — none of
    # the originally-guessed key spellings matched it.
    mf_ids = [d.get("mfId") or d.get("MF_ID") or d.get("mf_id") or d.get("id")
              for d in discovered if isinstance(d, dict)]
    mf_ids = [m for m in mf_ids if m is not None]
    if not mf_ids:
        _oplog(cur, "MF_TER_ERROR", {"stage": "no_mf_id_source",
               "note": "no MF_ID<->AMC map endpoint discovered among the populate-* candidates tried"})
        return {"error": "no MF_ID source"}

    # Probe candidate months (newest first) against a known-major AMC (Aditya Birla Sun Life,
    # mfId=3, present in every populate-mf sample seen), NO strCat filter (attempt_6 fix) —
    # TER publication lags the calendar month by several weeks, so the newest LISTED month
    # (live-observed: July-2026) is not reliably the newest PUBLISHED one.
    month = None
    for candidate in months:
        probe_rows, probe_meta = _amfi_ter_page(cur, 3, candidate, 1, 10)
        _oplog(cur, "MF_TER_API_DISCOVERY", {"stage": "month_probe", "month": candidate,
               "mf_id": 3, "rows": len(probe_rows), "meta": probe_meta})
        if probe_rows:
            month = candidate
            break
        time.sleep(0.5)
    if not month:
        _ter_diagnostic_probe(cur, 3, months[0])
        _oplog(cur, "MF_TER_ERROR", {"stage": "no_published_month", "months_tried": months,
               "note": "every listed month returned empty for a known AMC (ABSL, mfId=3), no "
                       "strCat filter — TER data may not be published for any listed month yet"})
        return {"error": "no published month found", "months_tried": months}

    all_rows, logged_sample = [], False
    n_amcs = 0
    for mf_id in mf_ids:
        page = 1
        while True:
            rows, meta = _amfi_ter_page(cur, mf_id, month, page, 500)
            if rows and not logged_sample:
                _oplog(cur, "MF_TER_API_SCHEMA_SAMPLE", {"row": rows[0], "meta": meta})
                logged_sample = True
            all_rows.extend(rows)
            try:
                page_count = int(meta.get("pageCount") or meta.get("PageCount") or 1)
            except (TypeError, ValueError):
                page_count = 1
            if not rows or page >= page_count:
                break
            page += 1
            time.sleep(0.6)
        n_amcs += 1
        # cc#498 live bug fix: this loop is HTTP-only for a stretch with zero DB activity on
        # the held cursor's transaction — this DB enforces idle_in_transaction_session_timeout
        # =300000 (5 min, CLAUDE.md-documented), which killed an earlier, slower version of
        # this crawl (728 combos) mid-run before any write happened. A no-op commit every AMC
        # keeps the connection alive (56 AMCs now, so this is a large safety margin, not just
        # a fix for the case that broke it).
        try:
            cur.connection.commit()
        except Exception:
            pass
        if n_amcs % 10 == 0:
            _oplog(cur, "MF_TER_API_CRAWL_PROGRESS", {"amcs_done": n_amcs, "amcs_total": len(mf_ids),
                   "rows_so_far": len(all_rows)})
        time.sleep(0.6)

    if not all_rows:
        _oplog(cur, "MF_TER_ERROR", {"stage": "no_rows", "month": month, "mf_ids_tried": len(mf_ids)})
        return {"error": "no TER rows returned from the live API", "month": month}

    equity_rows = [r for r in all_rows if _ter_row_is_equity(r)]
    parsed = {}   # scheme name -> ter
    for row in equity_rows:
        nm = _ter_row_scheme_name(row)
        ter = _ter_row_total(row)
        if not nm or ter is None or not (0 < ter < 10):
            continue
        parsed[nm] = ter

    universe = [(sc, nm, set(_norm_fund(nm))) for sc, _ac, nm, _cat in _canonical_equity_universe(cur)]
    matched = skipped_existing = 0
    for nm, ter in parsed.items():
        toks = set(_norm_fund(nm))
        if not toks:
            continue
        cand = None
        for sc, aname, aset in universe:
            if not aset:
                continue
            jac = len(toks & aset) / float(len(toks | aset))
            if cand is None or jac > cand[0]:
                cand = (jac, sc)
        if cand and cand[0] >= 0.5:
            cur.execute("UPDATE mf_master SET expense_ratio=%s, updated_at=NOW() "
                        "WHERE scheme_code=%s AND expense_ratio IS NULL", (ter, cand[1]))
            if cur.rowcount:
                matched += 1
            else:
                skipped_existing += 1
    stats = {"month": month, "ter_rows_raw": len(all_rows), "ter_rows_equity": len(equity_rows),
             "ter_rows_parsed": len(parsed), "matched": matched, "skipped_existing_curated": skipped_existing}
    _oplog(cur, "MF_TER_BACKFILL", stats)
    return stats


# ── step_3: AMC holdings orchestration ──────────────────────────────────────────────
# Framework fully wired (download -> parse_holdings_xlsx -> _resolve_nse -> mf_holdings, all
# cc#466-built pieces reused unchanged).
AMC_HOLDINGS_URLS = {
    # Founder/manually-confirmed overrides — take precedence over the crawled registry below
    # when both exist for the same AMC.
}

_ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
# cc#491 attempt_3: restricted to xlsx/xls/csv — dropped .pdf. parse_holdings_xlsx() uses
# pandas.read_excel(), which cannot read a PDF at all; both attempt_2 false positives (Canara
# Robeco's investor-SOP doc, Quant's Ready Reckoner) were .pdf files that matched the file-link
# regex but were never going to parse. A monthly portfolio disclosure is an Excel/CSV file by
# SEBI convention anyway, never a PDF.
_FILE_LINK_RE = re.compile(r'href="([^"]+\.(?:xlsx?|csv))"', re.I)
_IS_FILE_URL = re.compile(r'\.(?:xlsx?|csv)(?:$|\?)', re.I)

# cc#491 VERIFIED_SOURCE_INTEL_17JUL_CLAUDE_WEB: AMFI's portfolio-disclosure AGGREGATOR page is
# also a dead Next.js SPA (same 2025-26 relaunch as the AUM/TER pages) — skip it entirely and go
# straight to each AMC's OWN SEBI-mandated disclosure page. Domains below are this session's
# best-effort AMC-name -> primary-website match (standard, well-known domains) — NOT verified
# live from this sandbox (no outbound internet here). Seeded into mf_amc_holdings_registry as a
# discovery STARTING POINT only; a wrong domain is a one-row UPDATE away for the founder/Claude
# web to correct, and the seed never overwrites an already-resolved row (ON CONFLICT DO NOTHING).
_AMC_DOMAIN_SEED = {
    "SBI Mutual Fund": "https://www.sbimf.com",
    "HDFC Mutual Fund": "https://www.hdfcfund.com",
    "ICICI Prudential Mutual Fund": "https://www.icicipruamc.com",
    "Nippon India Mutual Fund": "https://mf.nipponindiaim.com",
    "Kotak Mutual Fund": "https://www.kotakmf.com",
    "Aditya Birla Sun Life Mutual Fund": "https://mutualfund.adityabirlacapital.com",
    "UTI Mutual Fund": "https://www.utimf.com",
    "Axis Mutual Fund": "https://www.axismf.com",
    "Mirae Asset Mutual Fund": "https://www.miraeassetmf.co.in",
    "DSP Mutual Fund": "https://www.dspim.com",
    "Tata Mutual Fund": "https://www.tatamutualfund.com",
    "Franklin Templeton Mutual Fund": "https://www.franklintempletonindia.com",
    "Canara Robeco Mutual Fund": "https://www.canararobeco.com",
    "Edelweiss Mutual Fund": "https://www.edelweissmf.com",
    "Bandhan Mutual Fund": "https://bandhanmutual.com",
    "PPFAS Mutual Fund": "https://www.ppfas.com",
    "Quant Mutual Fund": "https://www.quantmutual.com",
    "Motilal Oswal Mutual Fund": "https://www.motilaloswalmf.com",
    "Invesco Mutual Fund": "https://www.invescomutualfund.com",
    "Sundaram Mutual Fund": "https://www.sundarammutual.com",
    "PGIM India Mutual Fund": "https://www.pgimindiamf.com",
    "HSBC Mutual Fund": "https://www.assetmanagement.hsbc.co.in",
    "Baroda BNP Paribas Mutual Fund": "https://www.barodabnpparibasmf.in",
    "Union Mutual Fund": "https://www.unionmf.com",
    "Mahindra Manulife Mutual Fund": "https://www.mahindramanulife.com",
    "JM Financial Mutual Fund": "https://www.jmfinancialmf.com",
    "LIC Mutual Fund": "https://www.licmf.com",
}


def _crawl_amc_holdings_urls(cur):
    """RETIRED cc#533: this fed fetch_amc_holdings(), which by construction writes ONE AMC-level
    disclosure file to EVERY scheme of that AMC -- not per-fund granularity. 509 of 518 schemes
    in mf_holdings ended up with a byte-identical holdings list from this path (wiped 18-Jul,
    see MF_HOLDINGS_FANOUT_WIPE in ops_log). Superseded by the Moneycontrol per-fund path
    (resolve_mc_map -> write_mc_holdings, cc#500). Kept in place (not deleted) so the crawl
    logic is available for reference/reuse if a real per-AMC-file-but-per-fund-allocation
    approach is ever built, but it must never run standalone again."""
    return {}


def fetch_amc_holdings(cur, amc, url, as_of_month=None):
    """RETIRED cc#533: this wrote the SAME AMC-level disclosure file to EVERY scheme of that
    AMC (for sc in schemes: for h in rows: INSERT) -- incapable of per-fund granularity by
    construction. Root cause of the 509/518 identical-holdings poisoning (wiped 18-Jul, see
    ops_log MF_HOLDINGS_FANOUT_WIPE). Superseded by the Moneycontrol per-fund path
    (resolve_mc_map -> write_mc_holdings, cc#500)."""
    return {"amc": amc, "status": "retired_see_cc533",
            "reason": "AMC-file fanout writes identical holdings to every scheme of an AMC -- "
                      "superseded by per-fund Moneycontrol path"}


def run_holdings_curated(conn=None):
    """RETIRED cc#533: was step_3's AMC-file-fanout orchestrator (_crawl_amc_holdings_urls ->
    fetch_amc_holdings per AMC). That path is structurally incapable of per-fund granularity --
    it downloads ONE AMC-level disclosure file and writes the SAME rows to every scheme of that
    AMC. 509 of 518 mf_holdings schemes ended up with a byte-identical holdings list as a
    result (wiped 18-Jul, see ops_log MF_HOLDINGS_FANOUT_WIPE + MF_HOLDINGS_FULL_TRUNCATE).
    No longer called from run_v15_wiring(). Superseded by the Moneycontrol per-fund path
    (resolve_mc_map -> write_mc_holdings -> run_mc_oneshot, cc#500)."""
    return {"status": "retired_see_cc533",
            "reason": "AMC-file fanout writes identical holdings to every scheme of an AMC -- "
                      "superseded by per-fund Moneycontrol path"}


# ── step_5: category averages ───────────────────────────────────────────────────────
def compute_mf_category_averages(conn=None):
    """Recompute mf_category_averages over the canonical equity universe. AVG() ignores
    NULLs naturally, so a category with partial expense_ratio/returns coverage still gets a
    meaningful average over whatever's populated."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT category, AVG(expense_ratio), AVG(ret_1y), AVG(ret_3y), AVG(ret_5y), COUNT(*)
                FROM mf_master
                WHERE category = ANY(%s)
                GROUP BY category
            """, (list(EQUITY_CATEGORY_WHITELIST),))
            rows = cur.fetchall()
            for cat, avg_er, avg_1y, avg_3y, avg_5y, n in rows:
                cur.execute("""INSERT INTO mf_category_averages
                    (category, avg_expense_ratio, avg_ret_1y, avg_ret_3y, avg_ret_5y, n_funds, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (category) DO UPDATE SET
                        avg_expense_ratio=EXCLUDED.avg_expense_ratio, avg_ret_1y=EXCLUDED.avg_ret_1y,
                        avg_ret_3y=EXCLUDED.avg_ret_3y, avg_ret_5y=EXCLUDED.avg_ret_5y,
                        n_funds=EXCLUDED.n_funds, updated_at=NOW()""",
                    (cat, avg_er, avg_1y, avg_3y, avg_5y, n))
            # cc#520: purge stray/stale category rows outside the whitelist (Banking & PSU debt +
            # old-naming-convention rows from the pre-_derive_cat 12-fund seed era, e.g. bare
            # "Flexi Cap" alongside the real "Flexi Cap Fund") -- these must not linger and feed a
            # red-flag TER check or a screener tab that no longer matches any real fund.
            cur.execute("DELETE FROM mf_category_averages WHERE NOT (category = ANY(%s))",
                        (list(EQUITY_CATEGORY_WHITELIST),))
            purged = cur.rowcount
            conn.commit()
            _oplog(cur, "MF_CATEGORY_AVERAGES", {"categories": len(rows), "purged_stale": purged})
            conn.commit()
        return {"categories": len(rows), "purged_stale": purged}
    finally:
        if own:
            conn.close()


# ── step_4: MQS scoring ──────────────────────────────────────────────────────────────
def _f2(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _clip(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def compute_q_pillar(cur):
    """cc#546: per-scheme portfolio-quality (Q) inputs from a look-through of each fund's latest
    disclosed holdings against the latest live GVM snapshot -- PURE SQL, zero LLM/HTTP (rule
    id=6062). Returns {scheme_code: {"q_raw": float|None, "coverage_pct": float|None}}.

      • latest GVM   = gvm_scores rows on the single global MAX(score_date) (the codebase's
                       standard "latest" convention -- one nightly snapshot covers every symbol;
                       cf. position_news / buy_reversal_simulator / gvm_page_extras).
      • latest month = per-scheme MAX(as_of_month) in mf_holdings (holdings are monthly-cadence).
      • q_raw        = SUM(pct_weight * gvm_score) / SUM(pct_weight) over RESOLVED holdings only
                       -- renormalized over resolved weight (denominator = resolved weight, not
                       total), so an unresolved sliver never dilutes the quality reading.
      • coverage_pct = SUM(resolved pct_weight) / SUM(all pct_weight) * 100.

    Join keys: mf_holdings.scheme_code = mf_scores.scheme_code (PK on both);
    mf_holdings.resolved_nse_symbol = gvm_scores.symbol. The 60%-coverage gate is applied by the
    caller (compute_mf_scores), which also stores coverage_pct for EVERY scheme (gated or not)."""
    cur.execute("""
        WITH latest AS (
            SELECT symbol, gvm_score FROM gvm_scores
            WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
        ),
        h AS (
            SELECT scheme_code, pct_weight, resolved_nse_symbol
            FROM mf_holdings hh
            WHERE as_of_month = (SELECT MAX(as_of_month) FROM mf_holdings h3
                                 WHERE h3.scheme_code = hh.scheme_code)
        )
        SELECT h.scheme_code,
               SUM(h.pct_weight) AS all_w,
               SUM(CASE WHEN l.symbol IS NOT NULL THEN h.pct_weight ELSE 0 END) AS resolved_w,
               SUM(CASE WHEN l.symbol IS NOT NULL THEN h.pct_weight * l.gvm_score ELSE 0 END) AS wsum_gvm
        FROM h LEFT JOIN latest l ON l.symbol = h.resolved_nse_symbol
        GROUP BY h.scheme_code""")
    out = {}
    for sc, all_w, resolved_w, wsum_gvm in cur.fetchall():
        all_w = _f2(all_w) or 0.0
        resolved_w = _f2(resolved_w) or 0.0
        wsum_gvm = _f2(wsum_gvm) or 0.0
        q_raw = (wsum_gvm / resolved_w) if resolved_w > 0 else None
        coverage_pct = (resolved_w / all_w * 100.0) if all_w > 0 else None
        out[sc] = {"q_raw": q_raw,
                   "coverage_pct": round(coverage_pct, 1) if coverage_pct is not None else None}
    return out


def compute_mf_scores(conn=None):
    """step_4: MQS (Mutual Fund Quality Score) — pillar weights per the founder's
    MF_Analysis_Working_Model.xlsx: originally Quality 35% / Returns 30% / Cost 15% / Size 20%.

    cc#505 (18-Jul-2026): the rating-based Quality pillar is RETIRED (brand gone, no replacement
    -- see the deprecated column note in ensure_tables()). Scoring runs on Returns/Cost/Size
    only; weights renormalize among whichever of those three are available for a given fund
    (the existing missing-pillar renormalization already handles this with zero further code
    change — see below).

    cc#491 honesty note: this environment has no access to the actual Excel model (an
    external deliverable never in this repo) to copy its exact pillar formulas verbatim, so
    this is a reasonable, DOCUMENTED relative-to-category-peer scoring — not a guaranteed
    match to the Excel's precise math. Flagging for founder validation against the working
    model rather than claiming exact fidelity to an artifact never seen, per "do not
    fabricate/guess".

    Missing pillars are EXCLUDED and weights renormalized among what IS available, rather
    than fabricating a neutral fill-in for missing data.

    cc#520 FORMULA VERIFICATION (18-Jul-2026, before the full re-run): read end-to-end -- ZERO
    read/write of finkhoz_rating anywhere in this function (grepped the whole file: the column
    name appears exactly once, in ensure_tables()'s DEPRECATED DDL comment, never in scoring
    logic). No other retired input is read (only mf_master.expense_ratio/aum_cr/ret_1y/ret_3y/
    ret_5y + mf_category_averages, all live/current columns). The locked target weights (Q0.35/
    R0.30/C0.15/S0.20) match the ORIGINAL Excel-model intent cited above; Q has been permanently
    absent since cc#505 (its only data source, the rating brand, was retired with no
    replacement) -- every fund renormalizes across whichever of R/C/S it has data for via
    `wsum = sum(weights.values())` below, which is exactly the founder's "missing external rating
    NEVER zeroes a pillar" rule applied consistently (Q is missing for 100% of funds, not a
    per-fund gap, so every fund's weights renormalize the same way: R/C/S -> ~46.2%/23.1%/30.8%
    of the total when all three are present). No code change was needed to satisfy this rule --
    already correct.

      Returns (30%): ret_1y (fallback ret_3y, then ret_5y CAGR) vs the SAME horizon's category
        average -> 50 +/- 3x the pct-point delta, clipped 0-100.
      Cost    (15%): expense_ratio vs category average -> 50 - 20x the delta (cheaper=higher).
      Size    (20%): aum_cr vs category average, log-scaled (AUM is right-skewed) -> 50 +/-
        15x log(fund/avg) — flagged as the pillar most likely to need founder correction,
        since "size" could equally be read as a Goldilocks/penalize-too-large signal rather
        than bigger-is-better.
    """
    import math as _m
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT category, avg_expense_ratio, avg_ret_1y, avg_ret_3y, avg_ret_5y
                           FROM mf_category_averages""")
            cat_avg = {r[0]: {"er": _f2(r[1]), "r1y": _f2(r[2]), "r3y": _f2(r[3]), "r5y": _f2(r[4])}
                       for r in cur.fetchall()}
            cur.execute("""SELECT category, AVG(aum_cr) FROM mf_master
                           WHERE category = ANY(%s)
                             AND aum_cr IS NOT NULL GROUP BY category""",
                        (list(EQUITY_CATEGORY_WHITELIST),))
            cat_aum_avg = {r[0]: _f2(r[1]) for r in cur.fetchall()}

            # cc#546: Q (portfolio-quality) look-through inputs, one pass over holdings x latest GVM.
            q_data = compute_q_pillar(cur)

            cur.execute("""SELECT scheme_code, category, expense_ratio, aum_cr,
                                  ret_1y, ret_3y, ret_5y
                           FROM mf_master
                           WHERE category = ANY(%s)""", (list(EQUITY_CATEGORY_WHITELIST),))
            rows = cur.fetchall()
            scored = q_scored = 0
            for sc, cat, er, aum, r1y, r3y, r5y in rows:
                ca = cat_avg.get(cat, {})
                pillars, weights = {}, {}

                # cc#546: Q pillar -- weighted-avg portfolio GVM (0-10) scaled x10 to the 0-100
                # pillar scale, matching r/c/s which also emit on 0-100 (via _clip). Coverage gate:
                # only activate Q (and the full 100% Q+R+C+S basis) when >=60% of disclosed weight
                # resolved to a live GVM row; below that keep the R+C+S-only basis for this fund.
                qi = q_data.get(sc) or {}
                q_cov = qi.get("coverage_pct")
                if qi.get("q_raw") is not None and q_cov is not None and q_cov >= Q_COVERAGE_MIN:
                    pillars["q"] = _clip(float(qi["q_raw"]) * 10.0)
                    weights["q"] = 0.35

                fund_ret = r1y if r1y is not None else (r3y if r3y is not None else r5y)
                cat_ret = (ca.get("r1y") if r1y is not None
                           else (ca.get("r3y") if r3y is not None else ca.get("r5y")))
                if fund_ret is not None and cat_ret is not None:
                    pillars["r"] = _clip(50 + (float(fund_ret) - cat_ret) * 3)
                    weights["r"] = 0.30

                if er is not None and ca.get("er") is not None:
                    pillars["c"] = _clip(50 - (float(er) - ca["er"]) * 20)
                    weights["c"] = 0.15

                aum_avg = cat_aum_avg.get(cat)
                if aum is not None and aum_avg and aum_avg > 0 and float(aum) > 0:
                    pillars["s"] = _clip(50 + _m.log(float(aum) / aum_avg) * 15)
                    weights["s"] = 0.20

                if not pillars:
                    continue
                wsum = sum(weights.values())
                mqs = sum(pillars[k] * weights[k] for k in pillars) / wsum
                # cc#528 reopened: full locked model is Q0.35/R0.30/C0.15/S0.20 = 1.0, so wsum
                # IS the basis fraction directly -- no fabricated fill-in, just an honest label
                # of which pillars actually back this fund's mqs (Q is 0/14238 funds today).
                basis_label = "+".join(k.upper() for k in ("q", "r", "c", "s") if k in pillars)
                # cc#546: store coverage_pct on EVERY scored scheme (even when the gate left Q out)
                # so the card shows "insufficient holdings data" ONLY on real coverage < 60 / none.
                cur.execute("""INSERT INTO mf_scores (scheme_code, mqs, q_score, r_score, c_score, s_score,
                                   basis_pct, basis_label, coverage_pct, computed_at)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (scheme_code) DO UPDATE SET
                                   mqs=EXCLUDED.mqs, q_score=EXCLUDED.q_score, r_score=EXCLUDED.r_score,
                                   c_score=EXCLUDED.c_score, s_score=EXCLUDED.s_score,
                                   basis_pct=EXCLUDED.basis_pct, basis_label=EXCLUDED.basis_label,
                                   coverage_pct=EXCLUDED.coverage_pct, computed_at=NOW()""",
                            (sc, round(mqs, 2), pillars.get("q"), pillars.get("r"),
                             pillars.get("c"), pillars.get("s"),
                             round(wsum * 100, 1), basis_label, qi.get("coverage_pct")))
                scored += 1
                if "q" in pillars:
                    q_scored += 1
            # cc#520: purge any stray mf_scores rows for schemes outside the whitelist (Banking &
            # PSU debt funds scored by an older pre-exclusion pass) -- they must not surface in the
            # screener/search MQS column.
            cur.execute("""DELETE FROM mf_scores WHERE scheme_code IN (
                               SELECT scheme_code FROM mf_master WHERE NOT (category = ANY(%s))
                           )""", (list(EQUITY_CATEGORY_WHITELIST),))
            purged = cur.rowcount
            conn.commit()
            _oplog(cur, "MF_SCORES_COMPUTED", {"universe": len(rows), "scored": scored,
                                               "q_scored": q_scored, "purged_stale": purged})
            conn.commit()
        return {"universe": len(rows), "scored": scored, "q_scored": q_scored, "purged_stale": purged}
    finally:
        if own:
            conn.close()


def run_mf_score_nightly(conn=None):
    """cc#520 step_4: nightly MQS recompute -- category averages then scores, both PURE reads of
    already-stored mf_master data (zero external HTTP calls). Distinct from the scraping jobs
    cc#499 disabled: cc#499 turned off fetching NEW data from AMFI/mfapi/Moneycontrol on a timer;
    this recomputes scores FROM whatever data already sits in mf_master, which the founder
    explicitly wants to stay on a real nightly cadence ("the score compute from stored data
    stays"). Safe to schedule unconditionally -- idempotent, no network, sub-second for ~519 rows."""
    own = conn is None
    conn = conn or _conn()
    try:
        # cc#546: ensure schema first so the coverage_pct column self-creates before
        # compute_mf_scores writes it -- makes the recompute self-sufficient regardless of call
        # order (nightly slot, /score_recompute endpoint, or fresh boot).
        with conn.cursor() as cur:
            ensure_tables(cur)
            conn.commit()
        avgs = compute_mf_category_averages(conn)
        scores = compute_mf_scores(conn)
        return {"averages": avgs, "scores": scores}
    finally:
        if own:
            conn.close()


# ── orchestrator + admin trigger ────────────────────────────────────────────────────
def run_v15_wiring(conn=None):
    """cc#491: monthly-cadence orchestrator — AUM + TER + holdings + monthly snapshot + rolling
    prune. AUTO_MODE_GRANT_17JUL.

    SCOPE_RESHAPE_FOUNDER_17JUL (amendment, postdates the original steps 1-5 spec): MQS scoring
    (step_4, compute_mf_scores) and category averages (step_5, compute_mf_category_averages) are
    DEFERRED OUT of automated execution — "do not build them" — so this orchestrator no longer
    calls either. Both functions and their already-computed data (242 scored funds / 11 category
    rows from this task's earlier pass, before the amendment landed) are left in place, untouched,
    for the founder to review or re-arm manually later; they are simply no longer part of the
    automated wiring chain. Each remaining step is independently best-effort (a failure in one
    does not block the others) and ops_log instrumented individually (see MF_AUM_*, MF_TER_*,
    MF_NAV_HISTORY_PRUNED above) plus one final rollup entry.

    cc#533: holdings is NO LONGER part of this chain — run_holdings_curated()'s AMC-file-fanout
    wrote identical holdings to every scheme of an AMC (509/518 mf_holdings schemes ended up
    byte-identical). Holdings now come exclusively from the Moneycontrol per-fund one-shot
    (run_mc_oneshot, cc#500), run on demand via /api/v15/mf/run_mc_oneshot, not on this
    monthly-wiring cadence."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            _ensure_returns_cols(cur)
            conn.commit()
        results = {}
        with conn.cursor() as cur:
            try:
                results["aum"] = fetch_amfi_aum(cur)
            except Exception as e:
                results["aum"] = {"error": str(e)[:200]}
            conn.commit()
        with conn.cursor() as cur:
            try:
                results["expense_ratio"] = fetch_expense_ratio(cur)
            except Exception as e:
                results["expense_ratio"] = {"error": str(e)[:200]}
            conn.commit()
        with conn.cursor() as cur:
            try:
                results["monthly_snapshot"] = {"rows": _snapshot_current_month(cur)}
            except Exception as e:
                results["monthly_snapshot"] = {"error": str(e)[:200]}
            conn.commit()
        try:
            results["prune"] = prune_monthly_snapshots(conn)
        except Exception as e:
            results["prune"] = {"error": str(e)[:200]}
        with conn.cursor() as cur:
            _oplog(cur, "MF_V15_WIRING_DONE", results)
            conn.commit()
        return results
    finally:
        if own:
            conn.close()


@router.post("/api/v15/mf/score_recompute")
def mf_score_recompute():
    """cc#520: synchronous MQS recompute (category averages + scores) -- pure DB compute, no
    external HTTP calls, sub-second for ~519 rows, so this runs directly rather than via the
    flag-arm-and-poll pattern the scraping jobs use. Same function the nightly 01:20 IST scheduler
    slot calls; exposed here so the full re-run can be forced on demand (verification, or after a
    manual data fix) instead of waiting for the nightly slot."""
    return run_mf_score_nightly()


@router.post("/api/v15/mf/wire_all")
def mf_wire_all_arm():
    """cc#491: ARM the one-shot V15 wiring run (AUM/TER/holdings/snapshot/prune), picked up by
    the scheduler within a tick — same flag-gated single-flight pattern as cc#477's returns
    backfill. Same flag/job as /run_monthly below (spec-named alias)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_v15_wiring_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
        conn.commit()
    return {"armed": True, "flag": "mf_v15_wiring_run=pending"}


@router.post("/api/v15/mf/run_monthly")
def mf_run_monthly_arm():
    """cc#491 course-correct (session_log id=4734): the founder-facing name for the monthly
    Railway-side job — AUM + TER + holdings (AMFI-crawled AMC registry) + monthly snapshot +
    rolling-12mo prune, no scoring/category-averages. Same flag/job as /wire_all."""
    return mf_wire_all_arm()


@router.post("/api/v15/mf/run_weekly")
def mf_run_weekly_arm():
    """cc#491 course-correct: ARM the weekly NAV+returns sync (weekly-rolling-12mo window,
    returns from the full mfapi series) on demand — same job as the Saturday 06:30 IST cron,
    fired manually so it runs server-side on Railway instead of from this session."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_weekly_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
        conn.commit()
    return {"armed": True, "flag": "mf_weekly_run=pending"}


@router.get("/api/v15/mf/coverage_report")
def mf_coverage_report():
    """cc#491 course-correct acceptance criterion: per-category coverage over the canonical
    equity universe (519 schemes, category IS NOT NULL AND <> 'Banking & PSU') — how many
    schemes have aum_cr / expense_ratio / returns / holdings populated. Meaningful only after
    a Railway-executed run_monthly/run_weekly (this session's own probes cannot populate it)."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""
            SELECT category, COUNT(*) AS n, COUNT(aum_cr) AS n_aum, COUNT(expense_ratio) AS n_er,
                   COUNT(ret_1y) AS n_ret_1y, COUNT(ret_3y) AS n_ret_3y, COUNT(ret_5y) AS n_ret_5y
            FROM mf_master
            WHERE category IS NOT NULL AND category <> 'Banking & PSU'
            GROUP BY category ORDER BY n DESC""")
        cols = [d[0] for d in cur.description]
        by_category = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute("""SELECT COUNT(*) FROM mf_master
                       WHERE category IS NOT NULL AND category <> 'Banking & PSU'""")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT scheme_code) FROM mf_holdings")
        n_holdings = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT amc) FROM mf_amc_holdings_registry")
        n_amcs = cur.fetchone()[0]
    return {"total_universe": total, "schemes_with_holdings": n_holdings,
            "amcs_in_registry": n_amcs, "by_category": by_category}


@router.get("/api/v15/mf/curated")
def mf_curated():
    """The founder 12-fund curated screener rows (cc#467 screener table)."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT scheme_code, name, amc, category, crisil_rank,
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
        # cc#550: sector-level ratings for every GVM segment this fund touches. gvm_scores.segment
        # shares the sector_ratings taxonomy, so segment exposure joins straight to a sector GVM.
        segs = sorted({h["segment"] for h in holdings if h.get("segment")})
        seg_ratings = {}
        if segs:
            cur.execute("""SELECT segment, ROUND(mcap_weighted_gvm::numeric,2) AS sector_gvm, verdict
                           FROM sector_ratings
                           WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings)
                             AND segment = ANY(%s)""", (segs,))
            seg_ratings = {row[0]: {"sector_gvm": float(row[1]) if row[1] is not None else None,
                                    "verdict": row[2]} for row in cur.fetchall()}
    # portfolio-weighted GVM (where resolved) — a real P0-A signal
    wsum = sum((h["pct_weight"] or 0) for h in holdings if h.get("gvm") is not None)
    pw_gvm = (sum((h["pct_weight"] or 0) * float(h["gvm"]) for h in holdings if h.get("gvm") is not None) / wsum) if wsum else None
    # cc#550: segment exposure = SUM(pct_weight) grouped by the holding's GVM segment, joined to
    # the latest sector rating. Coverage = rated-segment weight / total disclosed weight; the UI
    # applies the same >=60% honesty gate the Q pillar uses before it shows the exposure read.
    total_w = sum((h.get("pct_weight") or 0) for h in holdings)
    seg_expo = {}
    for h in holdings:
        seg, w = h.get("segment"), h.get("pct_weight")
        if not seg or w is None:
            continue
        seg_expo[seg] = seg_expo.get(seg, 0.0) + float(w)
    segments = []
    for seg, expo in seg_expo.items():
        rr = seg_ratings.get(seg)
        segments.append({"segment": seg, "exposure_pct": round(expo, 2),
                         "sector_gvm": rr["sector_gvm"] if rr else None,
                         "sector_verdict": rr["verdict"] if rr else None})
    segments.sort(key=lambda x: x["exposure_pct"], reverse=True)
    rated_w = sum(s["exposure_pct"] for s in segments if s["sector_gvm"] is not None)
    seg_cov = round(rated_w / float(total_w) * 100, 1) if total_w else 0.0
    return {"master": master, "holdings": holdings, "nav": nav,
            "portfolio_weighted_gvm": round(pw_gvm, 2) if pw_gvm is not None else None,
            "resolved_pct": round(wsum, 1) if holdings else None,
            "segments": segments, "segment_coverage_pct": seg_cov}


# ── cc#500: ONE-TIME Moneycontrol full-set fill (AUM/TER/returns/holdings/manager/inception) ──
# No founder DevTools capture exists for Moneycontrol (unlike cc#498's AMFI TER API) and this
# sandbox has no outbound internet to inspect the live site — so step_1 is a pure discovery
# probe (same discipline as _discover_amfi_endpoints / _ter_diagnostic_probe): try the known
# historical URL conventions AND harvest embedded API URLs from the live page itself, log the
# FULL raw evidence to ops_log, and let a live Railway run decide what step_2/3 build on, rather
# than guessing field names blind. mc_discover is a small standalone GET so it can be triggered
# and inspected on demand before resolve_mc_map commits to any one convention at scale.
_MC_TEST_ISIN = "INF179K01XQ0"   # HDFC Mid-Cap Opportunities Fund — real, in mf_master, for probing only
# attempt_3 (10:30 run): getMcMfMapping?searchKey=ISIN CONFIRMED LIVE — exact match, no fuzzy
# name-matching needed at all for step_1 resolution:
#   {"success":1,"data":{"morningstarid":"HDFCMUTF13-2057G","imid":"MHD1161","isin":"INF179K01XQ0",
#    "slugUrl":"hdfc-mid-cap-opportunities-fund-direct-plan/MHD1161"}}
# getStockHoldings turned out to be the REVERSE lookup (stock ISIN -> funds holding it, not
# fund -> its holdings) — the isin=<scheme ISIN> call returned other schemes' holding data, not
# this scheme's portfolio. Chasing the real fund-detail + fund-holdings endpoints next by
# fetching the resolved fund's own detail page and harvesting ITS page-specific bundles (same
# discovery pattern, pointed at a page whose JS actually calls those endpoints).
_MC_TEST_IMID = "MHD1161"
_MC_TEST_SLUG = "hdfc-mid-cap-opportunities-fund-direct-plan/MHD1161"
_MC_SEARCH_CANDIDATES = [
    # attempt_5: getSchemeSnapshot/Performance/BasicDetails/FundDetails/MfOverview all confirmed
    # dead ("Not Found" from the API itself) — dropped. The real overview data turned out to be
    # embedded server-side in the fund-detail page's __NEXT_DATA__ blob (see page_diags), not a
    # separate client-side API call at all. Keeping only the 3 confirmed-live ones as sanity
    # checks; the __NEXT_DATA__ dump below is now the primary target.
    ("mc_mapping_by_isin", "https://api.moneycontrol.com/swiftapi/v1/mutualfunds/getMcMfMapping",
     {"searchKey": "ISIN", "value": _MC_TEST_ISIN, "responseType": "json"}),
    ("mc_investment_by_stock_imid", "https://api.moneycontrol.com/swiftapi/v1/mutualfunds/getInvestmentByStock",
     {"responseType": "json", "deviceType": "W", "page": "1", "pageSize": "50", "imid": _MC_TEST_IMID}),
]
_MC_DISCOVERY_PAGES = [
    f"https://www.moneycontrol.com/mutual-funds/nav/{_MC_TEST_SLUG}",
]
_MC_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9",
           "Referer": "https://www.moneycontrol.com/"}


def _mc_resolve_isin(isin):
    """Stage 1: ISIN -> (imid, slugUrl, diag) via getMcMfMapping (exact match, verified live
    17-Jul). Never raises."""
    import requests
    diag = {"isin": isin}
    try:
        r = requests.get("https://api.moneycontrol.com/swiftapi/v1/mutualfunds/getMcMfMapping",
                          params={"searchKey": "ISIN", "value": isin, "responseType": "json"},
                          headers=_MC_HDR, timeout=20)
        diag["mapping_http"] = r.status_code
        m = r.json() if r.status_code == 200 else {}
        d = m.get("data") or {}
        return d.get("imid"), d.get("slugUrl"), diag
    except Exception as e:
        diag["error"] = f"mapping fetch: {str(e)[:160]}"
        return None, None, diag


def _mc_fetch_by_slug(slug):
    """Stage 2: slugUrl -> parsed __NEXT_DATA__.props.pageProps.data dict (fund-detail page is
    Next.js SSR — overview/about are embedded server-side, no separate client-side API call).
    Returns (data_dict_or_None, diag). Never raises. Skipped entirely once a scheme's slug is
    already cached in mf_mc_map (step_2/3 reuse this directly without re-resolving the ISIN)."""
    import requests
    diag = {"slug": slug}
    try:
        r = requests.get(f"https://www.moneycontrol.com/mutual-funds/nav/{slug}", headers=_MC_HDR, timeout=30)
        diag["http"] = r.status_code
        html = r.text or ""
        diag["html_len"] = len(html)
        mi = html.find("__NEXT_DATA__")
        if mi < 0:
            diag["error"] = "no __NEXT_DATA__ marker"
            return None, diag
        start = html.find(">", mi) + 1
        end = html.find("</script>", start)
        blob = json.loads(html[start:end])
        data = (((blob.get("props") or {}).get("pageProps") or {}).get("data") or {})
        return data, diag
    except Exception as e:
        diag["error"] = f"page fetch/parse: {str(e)[:160]}"
        return None, diag


def _mc_fetch_scheme_page(isin):
    """Two-stage convenience wrapper (discovery probe only — step_2/3 call the stages
    separately since mf_mc_map already caches the slug)."""
    imid, slug, diag = _mc_resolve_isin(isin)
    diag["imid"], diag["slug"] = imid, slug
    if not slug:
        diag.setdefault("error", "no slugUrl in mapping response")
        return None, diag
    data, diag2 = _mc_fetch_by_slug(slug)
    diag.update(diag2)
    return data, diag


# cc#533 (addendum 18-Jul evening): the REAL per-fund holdings API is the ISIN-keyed
# swiftapi/v1/mutualfunds/holdings?isin=... endpoint (captured live from the fund-page popout),
# NOT getInvestmentByStock (a reverse lookup that served generic content -> the 17-Jul poisoning).
# Per founder discipline we do NOT hardcode a single guessed field name: instead we (a) log a
# full raw sample to ops_log on the first fetch of every run (MF_MC_HOLDINGS_SCHEMA_SAMPLE) and
# (b) match each field by case-insensitive key-fragment so a minor key-name variation across
# MC's response never silently drops data. Weight excludes value/change columns explicitly so
# the rupee-value or "1M HLD Chg" column is never mistaken for the NAV weightage.
_MC_HOLD_NAME_FRAGS   = ("companyname", "stockname", "scripname", "securityname", "company",
                          "stock", "scrip", "security", "holdingname", "sname", "name")
_MC_HOLD_WEIGHT_FRAGS = ("weightage", "percenttonav", "peraum", "percentageaum",
                          "holdingpercentage", "pernav", "weight", "percentage", "percent")
_MC_HOLD_ISIN_FRAGS   = ("isin",)
_MC_HOLD_SECTOR_FRAGS = ("sector",)
_MC_HOLD_WEIGHT_EXCL  = ("change", "chg", "diff", "value", "marketvalue", "amount", "cr", "mktval")


def _mc_norm_key(k):
    return re.sub(r"[^a-z0-9]", "", (k or "").lower())


def _mc_pick_field(row, frags, exclude=()):
    """First (key, value) in row whose normalized key contains any of `frags` and none of
    `exclude`. `frags` is tried in priority order so specific names win over generic ones."""
    norm = {k: _mc_norm_key(k) for k in row.keys()}
    for f in frags:
        for k, nk in norm.items():
            if any(x in nk for x in exclude):
                continue
            if f in nk:
                return k, row.get(k)
    return None, None


def _mc_find_holdings_array(obj, depth=0):
    """Recursively locate the first list-of-dicts that looks like holdings (each dict exposes a
    name-ish AND a weight-ish key). Robust to whatever wrapper shape the endpoint returns."""
    if depth > 6:
        return None
    if isinstance(obj, list):
        dict_rows = [x for x in obj if isinstance(x, dict)]
        if dict_rows:
            nk, _ = _mc_pick_field(dict_rows[0], _MC_HOLD_NAME_FRAGS, exclude=_MC_HOLD_SECTOR_FRAGS)
            wk, _ = _mc_pick_field(dict_rows[0], _MC_HOLD_WEIGHT_FRAGS, exclude=_MC_HOLD_WEIGHT_EXCL)
            if nk and wk:
                return dict_rows
        for x in obj:
            got = _mc_find_holdings_array(x, depth + 1)
            if got:
                return got
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            got = _mc_find_holdings_array(v, depth + 1)
            if got:
                return got
    return None


def _mc_fetch_holdings(isin, sample_cb=None):
    """cc#533: per-fund holdings from the ISIN-keyed endpoint. Returns
    (rows[{name,isin,pct_weight,sector}], portfolio_month_or_None, diag). Never raises.
    sample_cb, if given, is invoked once (first fetch of a run) with a raw-schema dict the
    caller logs to ops_log so the real JSON keys are verifiable before trusting the data."""
    import requests
    diag = {"isin": isin}
    try:
        r = requests.get("https://api.moneycontrol.com/swiftapi/v1/mutualfunds/holdings",
                          params={"isin": isin, "deviceType": "W", "responseType": "json"},
                          headers=_MC_HDR, timeout=30)
        diag["http"] = r.status_code
        if r.status_code != 200:
            if sample_cb:
                sample_cb({"isin": isin, "http": r.status_code, "located": False,
                           "raw_head": (r.text or "")[:800]})
            return [], None, diag
        payload = r.json()
        arr = _mc_find_holdings_array(payload)
        if not arr:
            diag["error"] = "no holdings array located in response"
            if sample_cb:
                sample_cb({"isin": isin, "http": r.status_code, "located": False,
                           "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else "not_a_dict",
                           "raw_head": json.dumps(payload)[:1500]})
            return [], None, diag
        first = arr[0]
        nk, _ = _mc_pick_field(first, _MC_HOLD_NAME_FRAGS, exclude=_MC_HOLD_SECTOR_FRAGS)
        wk, _ = _mc_pick_field(first, _MC_HOLD_WEIGHT_FRAGS, exclude=_MC_HOLD_WEIGHT_EXCL)
        ik, _ = _mc_pick_field(first, _MC_HOLD_ISIN_FRAGS)
        sk, _ = _mc_pick_field(first, _MC_HOLD_SECTOR_FRAGS)
        rows = []
        for row in arr:
            if not isinstance(row, dict):
                continue
            rows.append({"name": (row.get(nk) if nk else None),
                         "pct_weight": _mc_num(row.get(wk)) if wk else None,
                         "isin": (row.get(ik) if ik else None),
                         "sector": (row.get(sk) if sk else None)})
        # portfolio month: look for a date-ish top-level value
        month = None
        pdate = None
        data = payload.get("data") if (isinstance(payload, dict) and isinstance(payload.get("data"), dict)) else payload
        if isinstance(data, dict):
            for cand in ("portfolioDate", "asOnDate", "portfolioAsOn", "asOn", "date"):
                if data.get(cand):
                    pdate = data.get(cand); break
        if pdate:
            for fmt in ("%b %Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y", "%d %B %Y"):
                try:
                    month = datetime.strptime(str(pdate), fmt).date().replace(day=1); break
                except Exception:
                    continue
        diag.update({"n_rows": len(rows), "portfolio_date": pdate,
                     "picked": {"name": nk, "weight": wk, "isin": ik, "sector": sk}})
        if sample_cb:
            sample_cb({"isin": isin, "http": r.status_code, "located": True, "n_rows": len(rows),
                       "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else "list",
                       "sample_row": first, "picked": diag["picked"], "portfolio_date": pdate})
        return rows, month, diag
    except Exception as e:
        diag["error"] = str(e)[:160]
        if sample_cb:
            sample_cb({"isin": isin, "located": False, "error": str(e)[:200]})
        return [], None, diag


def _mc_num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def _mc_parse_inception(s):
    try:
        return datetime.strptime(str(s).strip(), "%d %b, %Y").date()
    except Exception:
        return None


def resolve_mc_map(conn=None):
    """cc#500 step_1_url_resolution: exact ISIN -> MC imid/slug for every canonical-universe
    scheme not yet in mf_mc_map. getMcMfMapping?searchKey=ISIN is a verified exact match (no
    fuzzy name-matching needed — every scheme in the 519-universe already carries a real ISIN).
    Logs unresolved ISINs to ops_log, never forces a bad match. Idempotent — a resumed run only
    processes schemes still missing from mf_mc_map."""
    import time as _t
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            cur.execute("""SELECT m.scheme_code, m.isin, m.name FROM mf_master m
                           WHERE m.category IS NOT NULL AND m.category <> 'Banking & PSU'
                             AND m.isin IS NOT NULL
                             AND NOT EXISTS (SELECT 1 FROM mf_mc_map mm WHERE mm.scheme_code = m.scheme_code)
                           ORDER BY m.scheme_code""")
            pending = cur.fetchall()
        resolved = unresolved = 0
        for sc, isin, name in pending:
            imid, slug, diag = _mc_resolve_isin(isin)
            with conn.cursor() as cur:
                if imid and slug:
                    cur.execute("""INSERT INTO mf_mc_map (scheme_code, mc_id, mc_slug, matched_name, match_score, resolved_at)
                                   VALUES (%s,%s,%s,%s,1.0,NOW())
                                   ON CONFLICT (scheme_code) DO UPDATE SET
                                       mc_id=EXCLUDED.mc_id, mc_slug=EXCLUDED.mc_slug,
                                       matched_name=EXCLUDED.matched_name, match_score=1.0, resolved_at=NOW()""",
                                (sc, imid, slug, name))
                    resolved += 1
                else:
                    unresolved += 1
                    _oplog(cur, "MF_MC_MAP_UNRESOLVED", {"scheme_code": sc, "isin": isin, "name": name, "diag": diag})
                conn.commit()
            if (resolved + unresolved) % 25 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "MF_MC_MAP_PROGRESS", {"done": resolved + unresolved, "of": len(pending),
                           "resolved": resolved, "unresolved": unresolved})
                    conn.commit()
            _t.sleep(0.4)
        stats = {"pending": len(pending), "resolved": resolved, "unresolved": unresolved}
        with conn.cursor() as cur:
            _oplog(cur, "MF_MC_MAP_DONE", stats)
            conn.commit()
        return stats
    finally:
        if own:
            conn.close()


def write_mc_overview(cur, scheme_code, data):
    """cc#500 step_3_write_rules: WHERE-NULL fill for aum_cr/expense_ratio/ret_3y/ret_5y
    (curated + AMFI-TER values are NEVER overwritten); manager/inception fill via COALESCE(new,
    existing) — spec calls this a "plain write" since both are 100% NULL today, COALESCE just
    makes a resumed re-fetch that happens to fail not null out an earlier good value. Sanity
    check (spec): reject aum > Rs 500,000cr as an AMC-aggregate, not a scheme-level number (the
    Groww bug class)."""
    ov = data.get("overview") or {}
    ab = data.get("about") or {}
    aum = _mc_num(ov.get("aum"))
    if aum is not None and aum > 500000:
        aum = None
    ter = _mc_num(ov.get("expenseRatio"))
    ret3 = _mc_num(ov.get("cagr_3y"))
    ret5 = _mc_num(ov.get("cagr_5y"))
    mgrs = ab.get("fund_managers_details") or []
    manager = ", ".join(m.get("name") for m in mgrs if m.get("name")) or None
    # "lanch_date" (sic) confirmed live on 2 real schemes — falls back to the correctly-spelled
    # key in case it's inconsistent across funds/backends.
    inception = _mc_parse_inception(ab.get("lanch_date") or ab.get("launch_date"))
    cur.execute("""UPDATE mf_master SET
        aum_cr = COALESCE(aum_cr, %s), expense_ratio = COALESCE(expense_ratio, %s),
        ret_3y = COALESCE(ret_3y, %s), ret_5y = COALESCE(ret_5y, %s),
        manager = COALESCE(%s, manager), inception = COALESCE(%s, inception), updated_at = NOW()
        WHERE scheme_code = %s""",
        (aum, ter, ret3, ret5, manager, inception, scheme_code))
    return {"aum": aum, "ter": ter, "ret3": ret3, "ret5": ret5, "manager": manager,
            "inception": str(inception) if inception else None}


def write_mc_holdings(cur, scheme_code, rows, as_of_month):
    """step_3: upsert into mf_holdings, resolving each holding to an NSE symbol via the
    existing _resolve_nse (same tiered company-name matching used by the AMC-holdings path).
    cc#533: rows are now the normalized {name, pct_weight, isin, sector} dicts produced by the
    ISIN-endpoint _mc_fetch_holdings (was getInvestmentByStock's raw percentToNAV shape)."""
    as_of_month = as_of_month or date.today().replace(day=1)
    resolved = 0
    for h in rows:
        name = h.get("name")
        if not name:
            continue
        nse_sym, method = _resolve_nse(cur, name)
        if nse_sym:
            resolved += 1
        cur.execute("""INSERT INTO mf_holdings
            (scheme_code, as_of_month, isin, company_name, pct_weight, resolved_nse_symbol, resolve_method)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (scheme_code, as_of_month, company_name) DO UPDATE SET
                isin=EXCLUDED.isin, pct_weight=EXCLUDED.pct_weight,
                resolved_nse_symbol=EXCLUDED.resolved_nse_symbol, resolve_method=EXCLUDED.resolve_method""",
            (scheme_code, as_of_month, h.get("isin"), name, h.get("pct_weight"), nse_sym, method))
    return {"rows": len(rows), "resolved_nse": resolved}


def run_mc_oneshot(conn=None):
    """cc#500: ONE-TIME Moneycontrol full-set fill over the 519-scheme canonical equity
    universe. Resumable via app_config mf_mc_oneshot_cursor (same checkpoint pattern as
    cc#477's mf_backfill_progress) — a redeploy mid-run picks back up from the last completed
    scheme_code, not from scratch. Per scheme: resolve (via resolve_mc_map, cached in
    mf_mc_map) -> overview fetch (AUM/TER/ret_3y/5y/manager/inception) -> holdings fetch+NSE
    resolve+upsert -> ret_1y fill via the EXISTING mfapi.in returns pipeline (WHERE ret_1y IS
    NULL only) since Moneycontrol's embedded overview object has no universal 1Y field
    (confirmed live on 2 real schemes incl. a non-top-performer — only cagr_3y/5y/7y/10y are
    always present). step_5_stop_rules: 3 consecutive 403/no-__NEXT_DATA__ (captcha-shaped)
    responses -> stop; >20% unparseable in the first 50 -> stop and report, never fight
    anti-bot or silently continue past a broken selector.

    cc#533 addition: the 17-Jul run of this exact function completed cleanly by every existing
    stop-rule's measure (no stop_event, holdings_filled=517/519) yet wrote a byte-identical
    holdings signature to 509 of those 517 schemes -- MC was returning HTTP 200 + valid,
    well-formed JSON, just with generic/duplicate content (a soft block, not the 403/no-
    __NEXT_DATA__ "hard block" shape the existing consecutive_bad rule detects). Added a rolling
    holdings-signature check: if the last SIG_WINDOW schemes that got holdings written all share
    one MD5(top-5 names) signature, treat it as a new stop_event and abort -- catches the soft-
    block within ~SIG_WINDOW schemes instead of silently re-poisoning all 519."""
    import time as _t
    import hashlib
    SIG_WINDOW = 15
    own = conn is None
    conn = conn or _conn()
    t0 = _t.time()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            _ensure_returns_cols(cur)
            conn.commit()

        map_stats = resolve_mc_map(conn)

        with conn.cursor() as cur:
            cur.execute("""SELECT m.scheme_code, m.amfi_code, m.isin, mm.mc_id, mm.mc_slug, m.ret_1y
                           FROM mf_master m JOIN mf_mc_map mm ON mm.scheme_code = m.scheme_code
                           WHERE m.category IS NOT NULL AND m.category <> 'Banking & PSU'
                           ORDER BY m.scheme_code""")
            universe = cur.fetchall()
            cur.execute("SELECT value FROM app_config WHERE key='mf_mc_oneshot_cursor'")
            r = cur.fetchone()
        done_prefix = (r[0] if r else "") or ""
        pending = [row for row in universe if row[0] > done_prefix]

        # cc#533 addendum: log the raw holdings-response schema for the first few fetches of this
        # run (MF_MC_HOLDINGS_SCHEMA_SAMPLE) so the exact JSON keys + which fields the extractor
        # picked are verifiable before trusting any downstream distinct-signature count -- and so
        # a Direct-plan-ISIN 404 (addendum point 5) surfaces on the first few schemes, not silently.
        _sample_count = [0]
        def _holdings_sample_cb(sample):
            if _sample_count[0] >= 5:
                return
            _sample_count[0] += 1
            try:
                with conn.cursor() as cur:
                    _oplog(cur, "MF_MC_HOLDINGS_SCHEMA_SAMPLE", {"seq": _sample_count[0], **sample})
                    conn.commit()
            except Exception as e:
                log.warning(f"holdings sample log: {e}")

        n_overview = n_holdings = n_ret1y = fails = 0
        consecutive_bad = unparseable_first50 = 0
        stop_event = None
        recent_holdings_sigs = []
        i = -1
        for i, (sc, amfi_code, isin, mc_id, mc_slug, ret_1y) in enumerate(pending):
            try:
                data, diag = _mc_fetch_by_slug(mc_slug)
                bad_response = diag.get("error") == "no __NEXT_DATA__ marker" or diag.get("http") == 403
                if data is None:
                    fails += 1
                    if i < 50:
                        unparseable_first50 += 1
                    consecutive_bad = consecutive_bad + 1 if bad_response else 0
                else:
                    consecutive_bad = 0
                    with conn.cursor() as cur:
                        write_mc_overview(cur, sc, data)
                        n_overview += 1
                        conn.commit()
                    hrows, hmonth, _hdiag = _mc_fetch_holdings(isin, sample_cb=_holdings_sample_cb)
                    if hrows:
                        with conn.cursor() as cur:
                            write_mc_holdings(cur, sc, hrows, hmonth)
                            n_holdings += 1
                            conn.commit()
                        top5 = sorted(h.get("name") or "" for h in hrows)[:5]
                        sig = hashlib.md5(",".join(top5).encode()).hexdigest()
                        recent_holdings_sigs.append(sig)
                        if len(recent_holdings_sigs) > SIG_WINDOW:
                            recent_holdings_sigs.pop(0)
                        if len(recent_holdings_sigs) == SIG_WINDOW and len(set(recent_holdings_sigs)) == 1:
                            stop_event = {"reason": "holdings_signature_collapsed", "at_scheme": sc,
                                          "index": i + 1, "sig_window": SIG_WINDOW,
                                          "note": "last N schemes with holdings all returned an "
                                                  "identical top-5-name signature -- MC is likely "
                                                  "soft-blocking (200 OK, valid JSON, generic/cached "
                                                  "content) rather than hard-blocking"}
                    if ret_1y is None:
                        with conn.cursor() as cur:
                            rres = sync_scheme_nav_and_returns(cur, sc, amfi_code)
                            if rres.get("returns"):
                                n_ret1y += 1
                            conn.commit()
                        _t.sleep(1.0)   # mfapi polite rate-limit, same as run_mf_returns_backfill
            except Exception as e:
                fails += 1
                log.warning(f"mc_oneshot {sc}: {e}")
                try: conn.rollback()
                except Exception: pass
            with conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_oneshot_cursor',%s,NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (sc,))
                conn.commit()
            if (i + 1) % 25 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "MF_MC_ONESHOT_PROGRESS", {"done": i + 1, "of": len(pending),
                           "overview": n_overview, "holdings": n_holdings, "ret1y_filled": n_ret1y,
                           "fails": fails, "elapsed_min": round((_t.time() - t0) / 60, 1)})
                    conn.commit()
            if consecutive_bad >= 3:
                stop_event = {"reason": "3_consecutive_403_or_captcha", "at_scheme": sc, "index": i + 1}
                break
            if i + 1 == 50 and unparseable_first50 / 50.0 > 0.20:
                stop_event = {"reason": "over_20pct_unparseable_first_50", "unparseable": unparseable_first50}
                break
            if stop_event:   # holdings_signature_collapsed, set above
                break
            _t.sleep(1.7)   # spec pacing: 1.5-2s between schemes

        with conn.cursor() as cur:
            snap_rows = _snapshot_current_month(cur)
            conn.commit()
        summary = {"map": map_stats, "universe": len(universe), "processed": i + 1,
                   "overview_filled": n_overview, "holdings_filled": n_holdings, "ret1y_filled": n_ret1y,
                   "fails": fails, "stop_event": stop_event, "monthly_snapshot_rows": snap_rows,
                   "elapsed_min": round((_t.time() - t0) / 60, 1)}
        if not stop_event:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app_config WHERE key='mf_mc_oneshot_cursor'")
                conn.commit()
        with conn.cursor() as cur:
            _oplog(cur, "MF_MC_ONESHOT_DONE", summary)
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


@router.post("/api/v15/mf/run_mc_oneshot")
def mf_run_mc_oneshot_arm():
    """cc#500 step_4_run_mechanics: ARM the one-time Moneycontrol full-set fill. Scheduler
    picks it up single-flight (same pattern as /wire_all). Resumable via app_config
    mf_mc_oneshot_cursor if interrupted mid-run. ONE run only — no recurring schedule (cc#499
    keeps all MF scraping cadence deactivated; this is on-demand only, same as the other manual
    /run_* triggers)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_oneshot_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
        conn.commit()
    return {"armed": True, "flag": "mf_mc_oneshot_run=pending"}


_MC_STRUCTURE_TEST_ISINS = [
    ("INF179K01XQ0", "HDFC Mid-Cap Opportunities Fund (likely top-performer content)"),
    ("INF767K01GW5", "LIC MF Infrastructure Fund (obscure AMC, sectoral — structure-universality check)"),
]


def _discover_mc_search_api(cur):
    """Fetch the MC mutual-fund listing page raw, harvest embedded <script src> bundle URLs,
    fetch a handful, regex-harvest quoted strings that look like a real search/suggest API on
    the moneycontrol.com domain — plus directly probe _MC_SEARCH_CANDIDATES with a real,
    well-known scheme name and log the FULL raw body of each (diagnostic-first, cc#498
    discipline: never assume a field-name convention ahead of live evidence). Best-effort,
    never raises."""
    import requests
    import time
    base = "https://www.moneycontrol.com"
    candidates = []
    page_diags = []
    for page_url in _MC_DISCOVERY_PAGES:
        diag = {"page": page_url, "html_len": 0, "script_tags": 0, "bundles_found": 0, "bundles_fetched_ok": 0}
        try:
            r = requests.get(page_url, headers=_MC_HDR, timeout=30)
            html = r.text or ""
            diag["html_len"] = len(html)
            diag["http"] = r.status_code
            diag["script_tags"] = len(re.findall(r'<script\b', html, re.I))
            # any moneycontrol-family domain (incl. priceapi./api. subdomains) whose path/query
            # looks like an api/search/suggest endpoint — broadened past a single guessed host
            # so the real convention (whatever it is) gets caught by the harvest itself.
            for u in re.findall(r'["\'](https?://[a-zA-Z0-9.\-]*moneycontrol\.com/[a-zA-Z0-9\-/_.]*'
                                 r'(?:api|search|suggest)[a-zA-Z0-9\-/_.]*(?:\?[^"\']*)?)["\']', html, re.I):
                candidates.append(u)
            # this is a server-rendered fund-detail page (176KB, not a JSON-widget listing page)
            # -- check for embedded initial-state JSON before assuming a client-side API call is
            # even needed for the overview fields (AUM/TER/returns/manager/inception).
            for marker in ("__NEXT_DATA__", "__INITIAL_STATE__", "__PRELOADED_STATE__", "__NUXT__"):
                mi = html.find(marker)
                if mi >= 0:
                    span = 7000 if marker == "__NEXT_DATA__" else 300
                    diag.setdefault("embedded_json_markers", []).append(
                        {"marker": marker, "context": html[mi:mi + span]})
            bundles = re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', html, re.I)
            diag["bundles_found"] = len(bundles)
            for b in bundles[:12]:
                b_url = b if b.startswith("http") else (base + b if b.startswith("/") else base + "/" + b)
                try:
                    br = requests.get(b_url, headers=_MC_HDR, timeout=20)
                    body = br.text or ""
                    diag["bundles_fetched_ok"] += 1
                    for u in re.findall(r'["\'](/mcapi/[a-zA-Z0-9\-/_.]*)["\']', body):
                        candidates.append(base + u)
                    for u in re.findall(r'["\'](https?://[a-zA-Z0-9.\-]*moneycontrol\.com/[a-zA-Z0-9\-/_.]*'
                                         r'(?:api|search|suggest)[a-zA-Z0-9\-/_.]*)["\']', body, re.I):
                        candidates.append(u)
                except Exception:
                    continue
        except Exception as e:
            diag["error"] = str(e)[:200]
        page_diags.append(diag)
        time.sleep(0.3)
    seen, uniq = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    _oplog(cur, "MF_MC_DISCOVERY", {"pages": page_diags, "n_candidates": len(uniq), "sample": uniq[:25]})

    probe_results = []
    for label, url, params_tpl in _MC_SEARCH_CANDIDATES:
        params = {k: (v.format(q="HDFC Top 100 Fund") if isinstance(v, str) else v) for k, v in params_tpl.items()}
        try:
            r = requests.get(url, params=params, headers=_MC_HDR, timeout=20)
            ct = (r.headers.get("content-type") or "").lower()
            probe_results.append({"label": label, "url": r.url, "http": r.status_code, "ct": ct[:40]})
            _oplog(cur, "MF_MC_SEARCH_PROBE", {"label": label, "url": r.url, "http": r.status_code,
                   "ct": ct[:40], "body": (r.text or "")[:800]})
        except Exception as e:
            _oplog(cur, "MF_MC_SEARCH_PROBE", {"label": label, "url": url, "error": str(e)[:160]})
        time.sleep(0.5)

    # attempt_6: __NEXT_DATA__ confirmed as the real source for AUM/TER/CAGR/manager/inception
    # (see MF_MC_STRUCTURE_PROBE) — structure-universality check across a top-performer fund AND
    # an obscure sectoral one, via _mc_fetch_scheme_page (the same helper the real scraper uses),
    # logging the ACTUAL parsed overview/about/kbyi sub-objects (not raw text slices) so field
    # presence is verified exactly, not eyeballed from a truncated string.
    for isin, note in _MC_STRUCTURE_TEST_ISINS:
        data, diag2 = _mc_fetch_scheme_page(isin)
        entry = {"isin": isin, "note": note, "diag": diag2}
        if data:
            ov = data.get("overview") or {}
            entry["overview_keys"] = sorted(ov.keys())
            entry["overview_sample"] = {k: ov.get(k) for k in
                ("aum", "expenseRatio", "cagr_3y", "cagr_5y", "cagr_7y", "cagr_10y",
                 "cagr_since_inception", "companyName", "categoryName")}
            entry["kbyi_present"] = bool(data.get("kbyi"))
            ab = data.get("about") or {}
            entry["about_keys"] = sorted(ab.keys())
            entry["about_sample"] = {"lanch_date": ab.get("lanch_date"),
                                      "fund_managers_details": ab.get("fund_managers_details"),
                                      "benchmark": ab.get("benchmark")}
        _oplog(cur, "MF_MC_STRUCTURE_PROBE", entry)
        time.sleep(0.6)

    return {"bundle_candidates": uniq[:20], "page_diags": page_diags, "search_probes": probe_results}


@router.get("/api/v15/mf/mc_discover")
def mf_mc_discover():
    """cc#500 step_1: run the Moneycontrol discovery probe synchronously (one page fetch + a
    few candidate GETs — seconds, not minutes) and return its findings directly. Inspect
    ops_log (MF_MC_DISCOVERY / MF_MC_SEARCH_PROBE) for the full raw evidence."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        res = _discover_mc_search_api(cur)
        conn.commit()
    return {"discovery": res}


@router.post("/api/v15/mf/mc_discover_run")
def mf_mc_discover_run_arm():
    """Same probe as GET /mc_discover, but ARMED for the scheduler to run server-side — the
    dev session itself has no outbound path to scorr.in, so this is how the probe gets
    triggered and its ops_log evidence inspected via run_sql (same pattern as /wire_all)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES ('mf_mc_discover_run','pending',NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value='pending', updated_at=NOW()")
        conn.commit()
    return {"armed": True, "flag": "mf_mc_discover_run=pending"}

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
        cur.execute("""SELECT scheme_code, name, amc, category FROM mf_master
                       WHERE name ILIKE %s OR amc ILIKE %s OR category ILIKE %s
                       ORDER BY (name ILIKE '%%direct%%' AND name ILIKE '%%growth%%') DESC,
                                curated DESC, length(name), name
                       LIMIT %s""",
                    (f"%{q}%", f"%{q}%", f"%{q}%", max(1, min(limit, 25))))
        rows = [{"scheme_code": sc, "name": nm, "amc": amc,
                 "category": _derive_cat(nm, cat), "plan": _derive_plan(nm)}
                for sc, nm, amc, cat in cur.fetchall()]
    return {"count": len(rows), "results": rows}


@router.get("/api/v15/fund/{scheme_code}")
def v15_fund(scheme_code: str):
    """cc#480: thin hero read — meta + returns bindings (ret_* NULL until cc#477 lands,
    rendered as em-dash by the page). No MQS/holdings (those are COMING SOON)."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        try:
            _ensure_returns_cols(cur); conn.commit()
        except Exception:
            conn.rollback()
        cur.execute("""SELECT scheme_code, name, amc, category, expense_ratio, aum_cr,
                              finkhoz_rating, crisil_rank, manager,
                              ret_1w, ret_1m, ret_3m, ret_6m, ret_1y, ret_2y, ret_3y, returns_asof
                       FROM mf_master WHERE scheme_code=%s""", (scheme_code,))
        r = cur.fetchone()
        if not r:
            return {"error": "not found"}
        cols = [d[0] for d in cur.description]
        m = dict(zip(cols, r))
    m["category"] = _derive_cat(m.get("name"), m.get("category"))
    m["plan"] = _derive_plan(m.get("name"))
    for k in ("expense_ratio", "aum_cr", "finkhoz_rating", "ret_1w", "ret_1m", "ret_3m",
              "ret_6m", "ret_1y", "ret_2y", "ret_3y"):
        if m.get(k) is not None:
            m[k] = float(m[k])
    m["returns_asof"] = str(m["returns_asof"]) if m.get("returns_asof") else None
    return {"fund": m}


@router.get("/api/v15/screener")
def v15_screener(category: str = "", sort: str = "1y", limit: int = 40):
    """cc#480: screener rows for a category tab. Matches mf_master.category OR the scheme name
    (real AMFI names carry the category), Direct-Growth only. Sort by 1Y desc (NULLs last) then
    AUM. AUM>5000 filter activates automatically once aum_cr is populated (cc#477)."""
    cat = (category or "").strip()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        try:
            _ensure_returns_cols(cur); conn.commit()
        except Exception:
            conn.rollback()
        where = ["name ILIKE '%%direct%%'", "name ILIKE '%%growth%%'"]
        params = []
        if cat:
            where.append("(category ILIKE %s OR name ILIKE %s)")
            params += [f"%{cat}%", f"%{cat}%"]
        order = ("ret_1y DESC NULLS LAST, aum_cr DESC NULLS LAST, name"
                 if sort == "1y" else "aum_cr DESC NULLS LAST, ret_1y DESC NULLS LAST, name")
        sql = ("SELECT scheme_code, name, amc, category, finkhoz_rating, crisil_rank, "
               "expense_ratio, aum_cr, ret_1y, ret_3y FROM mf_master WHERE "
               + " AND ".join(where) + " ORDER BY " + order + " LIMIT %s")
        params.append(max(1, min(limit, 80)))
        cur.execute(sql, params)
        rows = []
        for sc, nm, amc, c, fr, cr, er, aum, r1y, r3y in cur.fetchall():
            rows.append({"scheme_code": sc, "name": nm, "amc": amc,
                         "category": _derive_cat(nm, c),
                         "finkhoz_rating": float(fr) if fr is not None else None,
                         "crisil_rank": cr, "expense_ratio": float(er) if er is not None else None,
                         "aum_cr": float(aum) if aum is not None else None,
                         "ret_1y": float(r1y) if r1y is not None else None,
                         "ret_3y": float(r3y) if r3y is not None else None})
    return {"category": cat, "count": len(rows), "results": rows}


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


# ── cc#491: wire the remaining V15 data layers over the canonical equity universe ──────
# (AUM sweep fix, expense ratio, holdings orchestration framework, MQS scoring, category
# averages). AUTO_MODE_GRANT_17JUL: claim -> implement all 5 steps -> push -> verify ->
# finalize, no per-step OKs. Sequenced after #493/#495/#496 per the founder's queue.

def _canonical_equity_universe(cur):
    """V15_EQUITY_UNIVERSE_V1 (session_log id=4334, locked 16-Jul, AFTER this task was filed):
    mf_master rows with a non-NULL category, excluding Banking & PSU (a debt category, out of
    scope for the equity-focused V15 MQS). Supersedes the original 635-fund proxy candidate
    set from the pre-amendment spec. Verified live 17-Jul: 519 rows match this filter."""
    cur.execute("""SELECT scheme_code, amfi_code, name, category FROM mf_master
                   WHERE category IS NOT NULL AND category <> 'Banking & PSU'""")
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
    """cc#491 VERIFIED_SOURCE_INTEL_17JUL_CLAUDE_WEB: crawl each AMC's OWN disclosure site
    directly (AMFI's aggregator is dead — see _AMC_DOMAIN_SEED comment). First seeds any AMC not
    already in mf_amc_holdings_registry with its guessed root domain (ON CONFLICT DO NOTHING —
    never clobbers a previously-resolved or founder-corrected row). Then, three stages per AMC:
    (1) fetch the root domain, (2) find an anchor whose link text or href mentions
    portfolio+disclosure, (3) fetch that page and find the actual xlsx/csv/pdf link (first one —
    AMCs generally list the latest month first). Best-effort, never raises — every stage is
    ops_log'd so a Railway-side failure is diagnosable rather than silently returning nothing."""
    import requests
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}
    for amc, domain in _AMC_DOMAIN_SEED.items():
        cur.execute("""INSERT INTO mf_amc_holdings_registry (amc, disclosure_url, source_page, discovered_at)
                       VALUES (%s,%s,'domain_seed',NOW()) ON CONFLICT (amc) DO NOTHING""", (amc, domain))
    found = {}
    for amc, domain in _AMC_DOMAIN_SEED.items():
        try:
            r = requests.get(domain, headers=hdr, timeout=30)
            html = r.text or ""
            disclosure_link = None
            for href, text in _ANCHOR_RE.findall(html):
                clean = re.sub(r"<[^>]+>", "", text).strip().lower()
                hlow = (href or "").lower()
                if ("portfolio" in clean and "disclos" in clean) or \
                   ("portfolio" in hlow and "disclos" in hlow):
                    disclosure_link = href
                    break
            if not disclosure_link:
                continue
            full = disclosure_link if disclosure_link.startswith("http") else (
                domain.rstrip("/") + "/" + disclosure_link.lstrip("/"))
            if _IS_FILE_URL.search(full):
                found[amc] = full
                continue
            r2 = requests.get(full, headers=hdr, timeout=30)
            body2 = r2.text or ""
            # cc#491 attempt_2: prefer a file link whose OWN anchor text/href mentions
            # "portfolio" over just grabbing files[0] — the first-run test picked up an
            # unrelated PDF (a "Ready Reckoner" doc) sitting earlier on the disclosure page
            # than the actual monthly portfolio file.
            f = None
            for href2, text2 in _ANCHOR_RE.findall(body2):
                clean2 = re.sub(r"<[^>]+>", "", text2).strip().lower()
                if _IS_FILE_URL.search(href2 or "") and "portfolio" in (clean2 + " " + (href2 or "").lower()):
                    f = href2
                    break
            if not f:
                files = _FILE_LINK_RE.findall(body2)
                f = files[0] if files else None
            if f:
                if not f.startswith("http"):
                    f = full.rsplit("/", 1)[0] + "/" + f.lstrip("/")
                found[amc] = f
        except Exception as e:
            _oplog(cur, "MF_HOLDINGS_CRAWL_ERROR", {"stage": "amc_site", "amc": amc, "domain": domain,
                                                     "error": str(e)[:160]})
    for amc, url in found.items():
        cur.execute("""INSERT INTO mf_amc_holdings_registry (amc, disclosure_url, source_page, discovered_at)
                       VALUES (%s,%s,%s,NOW())
                       ON CONFLICT (amc) DO UPDATE SET disclosure_url=EXCLUDED.disclosure_url,
                         source_page=EXCLUDED.source_page, discovered_at=NOW()""",
                    (amc, url, _AMC_DOMAIN_SEED.get(amc, "")))
    _oplog(cur, "MF_HOLDINGS_URL_CRAWL", {"amcs_seeded": len(_AMC_DOMAIN_SEED), "amcs_resolved": len(found),
                                           "sample": list(found.items())[:5]})
    return found


def _schemes_for_amc(cur, amc_label):
    """Match canonical-universe schemes to an AMC by name-substring: mf_master.amc is populated
    only for the 11 curated seed funds, while the 519-row AMFI universe carries the AMC only
    inside the scheme name — so holdings coverage across the full universe needs a name match,
    not an amc-column equality."""
    toks = _norm_fund(amc_label)
    key = max(toks, key=len) if toks else None
    if not key:
        return []
    cur.execute("""SELECT scheme_code FROM mf_master WHERE category IS NOT NULL
                   AND category <> 'Banking & PSU' AND name ILIKE %s""", (f"%{key}%",))
    return [row[0] for row in cur.fetchall()]


def fetch_amc_holdings(cur, amc, url, as_of_month=None):
    """Download one AMC's monthly portfolio-disclosure excel, parse via parse_holdings_xlsx(),
    resolve each holding to an NSE symbol via _resolve_nse(), upsert into mf_holdings for every
    canonical-universe scheme belonging to that AMC (broadened from curated-only by cc#491
    course-correct — see _schemes_for_amc). Best-effort — never raises."""
    import requests
    as_of_month = as_of_month or date.today().replace(day=1)
    try:
        r = requests.get(url, headers={"User-Agent": "Scorr-MF/1.0"}, timeout=90)
        r.raise_for_status()
        rows, err = parse_holdings_xlsx(r.content)
    except Exception as e:
        _oplog(cur, "MF_HOLDINGS_ERROR", {"amc": amc, "url": url, "error": str(e)[:200]})
        return {"amc": amc, "error": str(e)[:200]}
    if err:
        _oplog(cur, "MF_HOLDINGS_ERROR", {"amc": amc, "url": url, "parse_error": err})
        return {"amc": amc, "error": err}
    schemes = _schemes_for_amc(cur, amc)
    if not schemes:
        return {"amc": amc, "rows": len(rows), "schemes": 0, "note": "no matching scheme for this AMC"}
    resolved = 0
    for sc in schemes:
        for h in rows:
            nse_sym, method = _resolve_nse(cur, h["company"])
            if nse_sym:
                resolved += 1
            cur.execute("""INSERT INTO mf_holdings
                (scheme_code, as_of_month, isin, company_name, pct_weight, resolved_nse_symbol, resolve_method)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (scheme_code, as_of_month, company_name) DO UPDATE SET
                    isin=EXCLUDED.isin, pct_weight=EXCLUDED.pct_weight,
                    resolved_nse_symbol=EXCLUDED.resolved_nse_symbol, resolve_method=EXCLUDED.resolve_method""",
                (sc, as_of_month, h.get("isin"), h["company"], h.get("pct"), nse_sym, method))
    stats = {"amc": amc, "url": url, "holdings_rows": len(rows), "schemes": len(schemes),
             "resolved_nse": resolved}
    _oplog(cur, "MF_HOLDINGS_BACKFILL", stats)
    return stats


def run_holdings_curated(conn=None):
    """step_3, cc#491 VERIFIED_SOURCE_INTEL_17JUL bugfix: crawl each AMC's own site
    (_crawl_amc_holdings_urls), THEN READ THE mf_amc_holdings_registry TABLE itself (not just
    this run's in-memory crawl result — the prior version ignored anything already persisted
    there, so a previously-discovered or founder/Claude-web-corrected URL was silently never
    used unless THIS exact run's crawl re-found it fresh). Founder AMC_HOLDINGS_URLS overrides
    still win. Rows that never resolved past the raw domain-seed (no real file found yet) are
    filtered out — not fetchable, would just 404/parse-fail. Still returns status=skipped
    honestly if nothing resolved — never fabricates holdings."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            _crawl_amc_holdings_urls(cur)
            conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT amc, disclosure_url FROM mf_amc_holdings_registry "
                        "WHERE disclosure_url IS NOT NULL")
            registry = dict(cur.fetchall())
        registry.update({k: v for k, v in AMC_HOLDINGS_URLS.items() if v})   # founder overrides win
        registry = {k: v for k, v in registry.items() if v and _IS_FILE_URL.search(v)}
        if not registry:
            with conn.cursor() as cur:
                _oplog(cur, "MF_HOLDINGS_SKIPPED",
                       {"note": "no AMC disclosure URL resolved to an actual xlsx/csv/pdf yet — "
                                "registry may hold only unresolved domain seeds"})
                conn.commit()
            return {"status": "skipped", "reason": "no_resolved_file_urls", "results": []}
        results = []
        with conn.cursor() as cur:
            for amc, url in registry.items():
                results.append(fetch_amc_holdings(cur, amc, url))
                conn.commit()
        return {"status": "ok", "amcs": len(registry), "results": results}
    finally:
        if own:
            conn.close()


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
                WHERE category IS NOT NULL AND category <> 'Banking & PSU'
                GROUP BY category
            """)
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
            conn.commit()
            _oplog(cur, "MF_CATEGORY_AVERAGES", {"categories": len(rows)})
            conn.commit()
        return {"categories": len(rows)}
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


def compute_mf_scores(conn=None):
    """step_4: MQS (Mutual Fund Quality Score) — FQS pillar weights per the founder's
    MF_Analysis_Working_Model.xlsx: Quality 35% / Returns 30% / Cost 15% / Size 20%.

    cc#491 honesty note: this environment has no access to the actual Excel model (an
    external deliverable never in this repo) to copy its exact pillar formulas verbatim, so
    this is a reasonable, DOCUMENTED relative-to-category-peer scoring — not a guaranteed
    match to the Excel's precise math. Flagging for founder validation against the working
    model rather than claiming exact fidelity to an artifact never seen, per "do not
    fabricate/guess" — the pillar STRUCTURE (4 pillars, these weights) is followed exactly;
    the per-pillar formula is this session's best-effort interpretation.

    Missing pillars are EXCLUDED and weights renormalized among what IS available, rather
    than fabricating a neutral fill-in for missing data — a fund with no finkhoz_rating gets
    an MQS from R/C/S only (weights renormalized to sum to 1), not a Quality=50 guess.

      Quality (35%): finkhoz_rating (0-10) -> 0-100. NULL (uncurated fund) -> pillar skipped.
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
                           WHERE category IS NOT NULL AND category <> 'Banking & PSU'
                             AND aum_cr IS NOT NULL GROUP BY category""")
            cat_aum_avg = {r[0]: _f2(r[1]) for r in cur.fetchall()}

            cur.execute("""SELECT scheme_code, category, finkhoz_rating, expense_ratio, aum_cr,
                                  ret_1y, ret_3y, ret_5y
                           FROM mf_master
                           WHERE category IS NOT NULL AND category <> 'Banking & PSU'""")
            rows = cur.fetchall()
            scored = 0
            for sc, cat, rating, er, aum, r1y, r3y, r5y in rows:
                ca = cat_avg.get(cat, {})
                pillars, weights = {}, {}

                if rating is not None:
                    pillars["q"] = _clip(float(rating) * 10.0)
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
                cur.execute("""INSERT INTO mf_scores (scheme_code, mqs, q_score, r_score, c_score, s_score, computed_at)
                               VALUES (%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (scheme_code) DO UPDATE SET
                                   mqs=EXCLUDED.mqs, q_score=EXCLUDED.q_score, r_score=EXCLUDED.r_score,
                                   c_score=EXCLUDED.c_score, s_score=EXCLUDED.s_score, computed_at=NOW()""",
                            (sc, round(mqs, 2), pillars.get("q"), pillars.get("r"),
                             pillars.get("c"), pillars.get("s")))
                scored += 1
            conn.commit()
            _oplog(cur, "MF_SCORES_COMPUTED", {"universe": len(rows), "scored": scored})
            conn.commit()
        return {"universe": len(rows), "scored": scored}
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
    MF_HOLDINGS_*, MF_NAV_HISTORY_PRUNED above) plus one final rollup entry."""
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
        try:
            results["holdings"] = run_holdings_curated(conn)
        except Exception as e:
            results["holdings"] = {"error": str(e)[:200]}
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


# ── cc#500: ONE-TIME Moneycontrol full-set fill (AUM/TER/returns/holdings/manager/inception) ──
# No founder DevTools capture exists for Moneycontrol (unlike cc#498's AMFI TER API) and this
# sandbox has no outbound internet to inspect the live site — so step_1 is a pure discovery
# probe (same discipline as _discover_amfi_endpoints / _ter_diagnostic_probe): try the known
# historical URL conventions AND harvest embedded API URLs from the live page itself, log the
# FULL raw evidence to ops_log, and let a live Railway run decide what step_2/3 build on, rather
# than guessing field names blind. mc_discover is a small standalone GET so it can be triggered
# and inspected on demand before resolve_mc_map commits to any one convention at scale.
_MC_SEARCH_CANDIDATES = [
    # historical/best-effort conventions — NOT verified live from this sandbox (no outbound
    # internet here); each is tried and its full raw response logged, same as the AMFI probes.
    ("mc_autosuggest_type3", "https://www.moneycontrol.com/mccode/common/autosuggesion.php",
     {"query": "{q}", "type": "3", "format": "json"}),
    ("mc_autosuggest_type9", "https://www.moneycontrol.com/mccode/common/autosuggesion.php",
     {"query": "{q}", "type": "9", "format": "json"}),
    ("mc_unified_search", "https://www.moneycontrol.com/mcapi/v1/search/search_by_category",
     {"query": "{q}", "type": "mf"}),
]
_MC_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9",
           "Referer": "https://www.moneycontrol.com/"}


def _discover_mc_search_api(cur):
    """Fetch the MC mutual-fund listing page raw, harvest embedded <script src> bundle URLs,
    fetch a handful, regex-harvest quoted strings that look like a real search/suggest API on
    the moneycontrol.com domain — plus directly probe _MC_SEARCH_CANDIDATES with a real,
    well-known scheme name and log the FULL raw body of each (diagnostic-first, cc#498
    discipline: never assume a field-name convention ahead of live evidence). Best-effort,
    never raises."""
    import requests
    page_url = "https://www.moneycontrol.com/mutual-funds/find-fund/equity"
    candidates = []
    diag = {"html_len": 0, "script_tags": 0, "bundles_found": 0, "bundles_fetched_ok": 0}
    try:
        r = requests.get(page_url, headers=_MC_HDR, timeout=30)
        html = r.text or ""
        diag["html_len"] = len(html)
        diag["http"] = r.status_code
        diag["script_tags"] = len(re.findall(r'<script\b', html, re.I))
        base = "https://www.moneycontrol.com"
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
                for u in re.findall(r'["\'](https?://api\.moneycontrol\.com/[a-zA-Z0-9\-/_.]*)["\']', body):
                    candidates.append(u)
                for u in re.findall(r'["\'](/[a-zA-Z0-9\-/_.]*(?:search|suggest)[a-zA-Z0-9\-/_.]*)["\']', body, re.I):
                    candidates.append(base + u)
            except Exception:
                continue
    except Exception as e:
        _oplog(cur, "MF_MC_DISCOVERY_ERROR", {"page": page_url, "error": str(e)[:200]})
        candidates = []
    seen, uniq = set(), []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    _oplog(cur, "MF_MC_DISCOVERY", {"page": page_url, "n_candidates": len(uniq), "diag": diag, "sample": uniq[:20]})

    import time as _t
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
        _t.sleep(0.5)
    return {"bundle_candidates": uniq[:20], "diag": diag, "search_probes": probe_results}


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

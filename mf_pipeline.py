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


# ── cc#477: AUM backfill + monthly/weekly NAV history + returns ────────────────────
# Founder 13-Jul: MF returns 1W/1M/3M/6M/1Y/2Y for funds with AUM > Rs 5,000 cr
# (LOCKED, revised from 10,000). Month-end NAV comparison (+ Friday weekly for 1W).
_AMFI_AAUM_URL = "https://www.amfiindia.com/modules/AverageAUMDetails"
_AMFI_AAUM_PAGE = "https://www.amfiindia.com/research-information/aum-data/average-aum"
AUM_THRESHOLD_CR = 5000.0        # founder-locked 13-Jul (was 10000)
NAV_HISTORY_MONTHS = 25          # trailing month-end rows (>=25 for 2Y funds)
WEEKLY_MONTHS = 14               # trailing weekly (Friday) rows for ret_1w continuity


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


def _probe_aum_sources(cur, today=None):
    """Try the founder's AUM endpoints in order (SPA JSON API -> legacy ASPX -> form POST). Logs
    each probe (status/content-type/len/rows) to ops_log. Returns (parsed[(name,cr)], used_desc)."""
    import requests
    best, used = [], None
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
    proxy candidate set for NAV+returns (founder unblock: never wait on a perfect AUM source)."""
    try:
        import pandas as pd  # noqa: F401
    except Exception as e:
        _oplog(cur, "MF_AUM_ERROR", {"stage": "import pandas", "error": str(e)[:200]})
        return {"error": "pandas unavailable"}
    parsed, used = _probe_aum_sources(cur, today)
    if not parsed:
        _oplog(cur, "MF_AUM_ERROR", {"stage": "all_sources_failed",
                                     "note": "AMFI AUM unavailable — returns run over proxy candidate set"})
        return {"error": "no AAUM rows parsed", "used": used}

    # Map each AAUM scheme -> the Direct-Growth AMFI row in mf_master by token-overlap.
    cur.execute("SELECT scheme_code, name FROM mf_master WHERE source='amfi' "
                "AND name ILIKE '%%direct%%' AND name ILIKE '%%growth%%'")
    universe = [(sc, nm, set(_norm_fund(nm))) for sc, nm in cur.fetchall()]
    matched = 0
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
            cur.execute("UPDATE mf_master SET aum_cr=%s, updated_at=NOW() WHERE scheme_code=%s",
                        (aaum_cr, best[1]))
            matched += 1
    cur.execute("SELECT COUNT(*) FROM mf_master WHERE aum_cr > %s", (AUM_THRESHOLD_CR,))
    qualifying = cur.fetchone()[0]
    stats = {"fy": fy, "quarter": q, "aaum_rows": len(parsed), "matched": matched,
             "match_rate": round(matched / len(parsed), 3) if parsed else 0,
             "qualifying_gt_5000cr": qualifying, "sample": parsed[:3]}
    _oplog(cur, "MF_AUM_BACKFILL", stats)
    return stats


def _month_end_and_weekly(rows):
    """From a full [(date,nav)] history (asc), select the last NAV of each calendar month
    (trailing NAV_HISTORY_MONTHS) tagged 'm', plus the last NAV of each ISO week (trailing
    WEEKLY_MONTHS) tagged 'w'. Returns {date: (nav, kind)} — month-end wins on a tie."""
    if not rows:
        return {}
    rows = sorted(rows)
    latest = rows[-1][0]
    m_cut = date(latest.year - (2 if latest.month <= NAV_HISTORY_MONTHS % 12 else 1), latest.month, 1)  # generous floor
    by_month, by_week = {}, {}
    for d, nav in rows:
        by_month[(d.year, d.month)] = (d, nav)        # last of month (rows asc)
        iso = d.isocalendar()
        by_week[(iso[0], iso[1])] = (d, nav)          # last of ISO week
    out = {}
    w_floor = date(latest.year - 2, latest.month, 1)
    for (d, nav) in by_week.values():
        if d >= w_floor:
            out[d] = (nav, "w")
    for (d, nav) in by_month.values():
        out[d] = (nav, "m")                            # month-end overrides weekly tag on same date
    # keep only trailing ~NAV_HISTORY_MONTHS+ of month rows; weekly already floored to 2y
    return out


def backfill_scheme_nav(cur, scheme_code, amfi_code):
    """PHASE 2 (per scheme): fetch full mfapi history, keep month-end + weekly rows only,
    upsert into mf_nav_history with nav_kind. Returns row count inserted/updated."""
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
    sel = _month_end_and_weekly(rows)
    n = 0
    for nd, (nav, kind) in sel.items():
        cur.execute("""INSERT INTO mf_nav_history (scheme_code, nav_date, nav, nav_kind)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (scheme_code, nav_date)
                       DO UPDATE SET nav=EXCLUDED.nav,
                         nav_kind=CASE WHEN EXCLUDED.nav_kind='m' THEN 'm'
                                       ELSE COALESCE(mf_nav_history.nav_kind, EXCLUDED.nav_kind) END""",
                    (scheme_code, nd, nav, kind))
        n += 1
    return {"scheme_code": scheme_code, "amfi_code": code, "rows": n}


def _pct(cur_nav, past_nav):
    if not past_nav or past_nav <= 0 or not cur_nav:
        return None
    return round((cur_nav / past_nav - 1) * 100, 2)


def _cagr(cur_nav, past_nav, years):
    if not past_nav or past_nav <= 0 or not cur_nav or years <= 0:
        return None
    return round(((cur_nav / past_nav) ** (1.0 / years) - 1) * 100, 2)


def compute_returns_for_scheme(cur, scheme_code):
    """PHASE 3: point-to-point returns from the stored NAV series. Nearest stored NAV on/before
    (latest - horizon) within a tolerance. 2Y as CAGR; others simple pct. NULL if insufficient age."""
    from datetime import timedelta
    cur.execute("SELECT nav_date, nav FROM mf_nav_history WHERE scheme_code=%s AND nav IS NOT NULL "
                "ORDER BY nav_date", (scheme_code,))
    series = [(d, float(n)) for d, n in cur.fetchall()]
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
    n2y = past(730, 25)
    r2y = _cagr(latest_nav, n2y, 2.0)
    cur.execute("""UPDATE mf_master SET ret_1w=%s, ret_1m=%s, ret_3m=%s, ret_6m=%s, ret_1y=%s,
                   ret_2y=%s, returns_asof=%s, updated_at=NOW() WHERE scheme_code=%s""",
                (r1w, r1m, r3m, r6m, r1y, r2y, latest_d, scheme_code))
    return {"ret_1w": r1w, "ret_1m": r1m, "ret_3m": r3m, "ret_6m": r6m, "ret_1y": r1y, "ret_2y": r2y}


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
        # qualifying set: AUM>5000 if AMFI AUM landed, ELSE the proxy candidate set (founder
        # unblock — never wait on a perfect AUM source; NAV+returns via mfapi are independent).
        with conn.cursor() as cur:
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
                    res = backfill_scheme_nav(cur, sc, ac)
                    if not res.get("error"):
                        n_nav += res.get("rows", 0)
                        if compute_returns_for_scheme(cur, sc):
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
    the qualifying set append the newest weekly NAV + recompute returns (month-end rows are added
    when a new month has completed — backfill_scheme_nav re-selects them idempotently)."""
    own = conn is None
    conn = conn or _conn()
    try:
        run_amfi_nav(conn)   # newest daily/Friday NAV into mf_nav_history
        with conn.cursor() as cur:
            _ensure_returns_cols(cur)
            cur.execute("SELECT scheme_code, amfi_code FROM mf_master WHERE aum_cr > %s", (AUM_THRESHOLD_CR,))
            qual = cur.fetchall()
            if not qual:
                qual = _candidate_scheme_set(cur)   # same proxy fallback as the backfill
            conn.commit()
        import time as _t
        n = 0
        for sc, ac in qual:
            try:
                with conn.cursor() as cur:
                    backfill_scheme_nav(cur, sc, ac)      # re-selects month-end + weekly idempotently
                    compute_returns_for_scheme(cur, sc)
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
    """cc#477 phase_4: monthly (3rd calendar day) AUM re-fetch + >5000cr re-qualification."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur); _ensure_returns_cols(cur)
            res = fetch_amfi_aum(cur)
            conn.commit()
        return res
    finally:
        if own:
            conn.close()


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

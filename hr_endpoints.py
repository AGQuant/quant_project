"""
hr_endpoints.py — cc#398 Portfolio Health Report (spec id=2994), MODULE 1: ingest.

Flow: upload/paste holdings (xlsx/csv; best-effort PDF) -> flexible column-map parse -> resolve each
symbol against input_raw (nse_code exact, then company_name fuzzy) -> return an editable grid with
unresolved flags -> confirm/fix -> save to hr_portfolios + hr_holdings. All Scorr-native. P1 = excel/
csv/manual + best-effort PDF (CAS/CAMS is P2). The report engine + /health page wire in later modules
against the founder-approved template scorr_health.html.
"""
import io
import os
import json
import re

import psycopg
from fastapi import APIRouter, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(_DB)


# cc#462: header-detection parser for REAL broker exports (Zerodha Console = 22 preamble rows, table at
# row 23, data offset to col B; Groww/Upstox/Dhan/Fyers/Angel/ICICI/Motilal/Sharekhan/... each preamble +
# column-offset variants). Locate the table by HEADER TEXT, not position: scan the first ~40 rows of EVERY
# sheet for a row carrying a Symbol + Quantity + Average-price column (synonym dictionary below, built from
# the founder's verbatim per-broker headers), then take the rows under it until the first fully-empty row.
# Column order/offset is irrelevant. Synonyms are ordered most-specific-first so "Quantity Available" beats
# "Quantity Discrepant"/"...Long Term" and "Average Price" beats "Previous Closing Price".
_SYN = {
    "symbol": ["stock symbol", "scrip name", "scripname", "scrip code", "scripcode", "instrument name",
               "security name", "company name", "stock name", "symbol", "instrument", "scrip", "ticker",
               "stock", "company", "security", "contract"],
    "qty": ["quantity available", "holding quantity", "free quantity", "current qty", "net quantity",
            "holding qty", "net qty", "free qty", "dp bal", "dp avail", "quantity", "qty", "shares",
            "units", "holding"],
    "avg_price": ["average cost price", "avg. cost price", "avg cost price", "avg trading price",
                  "avg. trading price", "average price", "averageprice", "buy average", "buy avg",
                  "purchase price", "collateral price", "cost price", "hold price", "avg.cost", "avg cost",
                  "buy price", "avg rate", "avg. rate", "average", "avg", "rate", "cost"],
}
_BAD_SYM = {"", "total", "grand total", "subtotal", "sub total", "summary", "nan", "equity",
            "mutual funds", "combined"}
_STOP = {"ltd", "limited", "india", "indian", "the", "co", "company", "corp", "corporation", "and",
         "pvt", "private", "enterprises", "industries", "&"}


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def _pick_col(headers, keys):
    """Index of the first header cell whose lowercased text contains any keyword (earliest keyword wins)."""
    low = [str(h or "").strip().lower() for h in headers]
    for kw in keys:
        for i, h in enumerate(low):
            if kw in h:
                return i
    return None


def _num(r, idx):
    if idx is None or idx >= len(r):
        return None
    v = str(r[idx] or "").replace(",", "").replace("₹", "").replace("Rs", "").replace("rs", "").strip()
    try:
        return float(v)
    except Exception:
        return None


def _map_rows(header, body, si, qi, pi):
    out = []
    for r in body:
        if si is None or si >= len(r):
            continue
        sym = _norm(r[si])
        if not sym or sym.lower() in _BAD_SYM:
            continue
        out.append({"input": sym, "qty": _num(r, qi), "avg_price": _num(r, pi)})
    return out


def _rows_from_table(headers, rows):
    """Map a KNOWN header + rows to holdings (PDF + paste paths where the header is row 0)."""
    si = _pick_col(headers, _SYN["symbol"])
    if si is None:
        return []
    return _map_rows(headers, rows, si, _pick_col(headers, _SYN["qty"]), _pick_col(headers, _SYN["avg_price"]))


def _detect_and_map(grid, scan=40):
    """Scan the first `scan` rows for a header carrying Symbol + Quantity + Average columns; the table is
    the rows under it until the first fully-empty row. Returns (rows_or_None, info_dict_or_missing_reason)."""
    limit = min(len(grid), scan)
    for hi in range(limit):
        row = grid[hi]
        si, qi, pi = _pick_col(row, _SYN["symbol"]), _pick_col(row, _SYN["qty"]), _pick_col(row, _SYN["avg_price"])
        if si is not None and qi is not None and pi is not None:
            body = []
            for r in grid[hi + 1:]:
                if all(str(c).strip() == "" for c in r):
                    break
                body.append(r)
            rows = _map_rows(row, body, si, qi, pi)
            info = {"header_row": hi + 1,
                    "columns": {"symbol": _norm(row[si]), "qty": _norm(row[qi]), "avg_price": _norm(row[pi])}}
            return (rows, info) if rows else (None, f"header found (row {hi + 1}) but no data rows below it")
    seen = {"symbol": False, "qty": False, "avg_price": False}
    for r in grid[:limit]:
        for k in seen:
            if _pick_col(r, _SYN[k]) is not None:
                seen[k] = True
    miss = [k for k, v in seen.items() if not v]
    if not miss:
        return None, "found Symbol/Quantity/Average tokens but never together in one header row"
    return None, "no header row with all of Symbol + Quantity + Average; missing: " + ", ".join(miss)


def _parse_bytes(filename, data):
    """Header-detection parse across ALL sheets (Equity preferred; Mutual Funds captured separately, never
    blocking the equity flow). Returns (rows, warning, diagnostics)."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        r, w = _parse_pdf(data)
        return r, w, {"format": "pdf"}
    try:
        import pandas as pd
    except Exception:
        return [], "spreadsheet parser unavailable on server — paste rows instead", {}
    diag = {"sheets": []}
    try:
        if name.endswith((".xlsx", ".xls")):
            xl = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
            sheets = list(xl.items())
        else:
            df = pd.read_csv(io.BytesIO(data), header=None, dtype=str, on_bad_lines="skip")
            sheets = [("csv", df)]
    except Exception as e:
        return [], f"could not read file: {str(e)[:160]}", diag
    best = None          # (priority, rows, sheet_name)
    mf_rows, mf_sheet = [], None
    for sname, df in sheets:
        df = df.fillna("")
        grid = df.values.tolist()
        rows, info = _detect_and_map(grid)
        sd = {"sheet": str(sname), "scanned_rows": min(len(grid), 40)}
        if rows:
            sd.update({"holdings": len(rows), "header_row": info.get("header_row"), "columns": info.get("columns")})
            low = str(sname).lower()
            if "mutual" in low or low in ("mf", "mutual funds"):
                if len(rows) > len(mf_rows):
                    mf_rows, mf_sheet = rows, str(sname)
            else:
                pri = 2 if "equit" in low else 1
                if best is None or pri > best[0] or (pri == best[0] and len(rows) > len(best[1])):
                    best = (pri, rows, str(sname))
        else:
            sd["missing"] = info
        diag["sheets"].append(sd)
    if best:
        diag["chosen_sheet"] = best[2]
        if mf_rows:
            diag["mutual_funds"] = {"sheet": mf_sheet, "count": len(mf_rows)}   # cc#462 fix_3: stashed, not blocking
        return best[1], None, diag
    if mf_rows:
        diag["chosen_sheet"] = mf_sheet
        return mf_rows, "only a Mutual Funds sheet was detected — equity analytics will be limited", diag
    return [], "no holdings table detected — scanned every sheet for a header with Symbol + Quantity + Average columns", diag


def _parse_pdf(data):
    """Best-effort PDF table extract. Requires pdfplumber; degrades gracefully if unavailable."""
    try:
        import pdfplumber
    except Exception:
        return [], "PDF parsing unavailable on server — please upload Excel/CSV or paste rows."
    try:
        rows_out = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                for tbl in (page.extract_tables() or []):
                    if not tbl or len(tbl) < 2:
                        continue
                    rows_out.extend(_rows_from_table(tbl[0], tbl[1:]))
        return rows_out, (None if rows_out else "No tables found in PDF — paste rows or use Excel/CSV.")
    except Exception as e:
        return [], f"PDF parse error: {str(e)[:160]}"


def _parse_pasted(text):
    """Parse pasted text: comma/tab/space-separated, optional header row."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    delim = "\t" if "\t" in lines[0] else ("," if "," in lines[0] else None)
    split = (lambda ln: ln.split(delim)) if delim else (lambda ln: ln.split())
    grid = [split(ln) for ln in lines]
    # header row present if the first row carries a symbol-token (cc#462: _SYN, not the old _COL_KEYS)
    if _pick_col(grid[0], _SYN["symbol"]) is not None:
        return _rows_from_table(grid[0], grid[1:])
    # no header: assume symbol[, qty[, avg]]
    return _map_rows(["symbol", "qty", "avg_price"], grid, 0, 1, 2)


def _norm_name(s):
    """Normalize a company name to significant tokens (cc#462): strip punctuation + corp-suffix stopwords."""
    s = re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower())
    return [t for t in s.split() if t and t not in _STOP]


def _resolve(cur, holdings):
    """cc#462 tiered resolution against input_raw: (a) exact nse_code (exchange suffix stripped),
    (b) exact normalized company_name, (c) token-overlap fuzzy vs company_names (Jaccard >= 0.6 accepted),
    else (d) unmatched WITH top-3 suggestions for one-click confirmation — never silently guess low
    confidence into a financial report. Each row reports the resolution method for transparency."""
    resolved = []
    for h in holdings:
        raw = h["input"]
        up = re.sub(r"[-.](eq|ns|bo|bse|nse|be)$", "", raw.upper().strip(), flags=re.I).strip()
        sym = cname = method = None
        sugg = []
        # (a) exact nse_code
        cur.execute("SELECT nse_code, company_name FROM input_raw WHERE UPPER(nse_code)=%s LIMIT 1", (up,))
        m = cur.fetchone()
        if m:
            sym, cname, method = m[0], m[1], "nse_code"
        # (b) exact normalized company_name
        if not sym:
            key = re.sub(r"[^a-z0-9]", "", raw.lower())
            if key:
                cur.execute("""SELECT nse_code, company_name FROM input_raw
                               WHERE REGEXP_REPLACE(LOWER(company_name), '[^a-z0-9]', '', 'g') = %s LIMIT 1""",
                            (key,))
                m = cur.fetchone()
                if m:
                    sym, cname, method = m[0], m[1], "company_name"
        # (c) token-overlap fuzzy (pg_trgm not installed — scored in Python)
        if not sym:
            toks = _norm_name(raw)
            if toks:
                longest = max(toks, key=len)
                cur.execute("SELECT nse_code, company_name FROM input_raw WHERE company_name ILIKE %s LIMIT 60",
                            (f"%{longest}%",))
                tset = set(toks)
                scored = []
                for nc, cn in cur.fetchall():
                    cset = set(_norm_name(cn))
                    if not cset:
                        continue
                    jac = len(tset & cset) / float(len(tset | cset))
                    scored.append((jac, nc, cn))
                scored.sort(reverse=True)
                sugg = [{"symbol": s2, "company_name": c2, "score": round(j, 2)} for j, s2, c2 in scored[:3]]
                if scored and scored[0][0] >= 0.6:
                    sym, cname, method = scored[0][1], scored[0][2], "fuzzy"
        resolved.append({"input": raw, "symbol": sym, "company_name": cname, "method": method,
                         "qty": h.get("qty"), "avg_price": h.get("avg_price"),
                         "resolved": bool(sym), "suggestions": ([] if sym else sugg)})
    return resolved


@router.post("/api/health/upload")
async def health_upload(file: Optional[UploadFile] = File(None), text: Optional[str] = Form(None)):
    """Parse an uploaded holdings file (xlsx/csv/pdf) or pasted text, resolve symbols, return a grid.
    On parse failure returns `diagnostics` (which sheets/rows were scanned + what column was missing)."""
    warning = None
    diag = {}
    if file is not None:
        data = await file.read()
        rows, warning, diag = _parse_bytes(file.filename, data)
    elif text:
        rows = _parse_pasted(text)
    else:
        return {"error": "provide a file or pasted text"}
    if not rows:
        return {"error": warning or "no holdings parsed", "rows": [], "warning": warning, "diagnostics": diag}
    with _conn() as conn, conn.cursor() as cur:
        resolved = _resolve(cur, rows)
    return {"count": len(resolved), "resolved": sum(1 for r in resolved if r["resolved"]),
            "unresolved": sum(1 for r in resolved if not r["resolved"]),
            "warning": warning, "diagnostics": diag, "rows": resolved}


class HRHolding(BaseModel):
    symbol: str
    company_name: Optional[str] = None
    qty: Optional[float] = None
    avg_price: Optional[float] = None


class HRSaveReq(BaseModel):
    name: Optional[str] = "My Portfolio"
    source: Optional[str] = "upload"
    holdings: List[HRHolding]


@router.post("/api/health/save")
def health_save(body: HRSaveReq):
    """Persist a confirmed holdings set -> hr_portfolios + hr_holdings; returns the portfolio id."""
    valid = [h for h in body.holdings if h.symbol]
    if not valid:
        return {"error": "no resolved holdings to save"}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO hr_portfolios (name, source, created_at) VALUES (%s, %s, NOW()) RETURNING id",
                    (body.name or "My Portfolio", body.source or "upload"))
        pid = cur.fetchone()[0]
        for h in valid:
            cur.execute("""INSERT INTO hr_holdings (portfolio_id, symbol, company_name, qty, avg_price, resolved, raw_input)
                           VALUES (%s, %s, %s, %s, %s, TRUE, %s::jsonb)""",
                        (pid, h.symbol.upper(), h.company_name, h.qty, h.avg_price, json.dumps(h.dict(), default=str)))
        conn.commit()
    return {"portfolio_id": pid, "saved": len(valid), "name": body.name}


@router.get("/api/health/portfolio/{pid}")
def health_portfolio(pid: int):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, source, created_at FROM hr_portfolios WHERE id=%s", (pid,))
        p = cur.fetchone()
        if not p:
            return {"error": "not found"}
        cur.execute("SELECT symbol, company_name, qty, avg_price FROM hr_holdings WHERE portfolio_id=%s ORDER BY id", (pid,))
        cols = [d[0] for d in cur.description]
        holdings = [dict(zip(cols, r)) for r in cur.fetchall()]
    return {"id": p[0], "name": p[1], "source": p[2], "created_at": str(p[3]), "holdings": holdings}


def _cmp_map(cur, syms):
    """cc#651: symbol -> price, cmp_prices first then latest raw_prices close (mirrors hr_report._load_cmp)."""
    out = {}
    if not syms:
        return out
    cur.execute("SELECT symbol, cmp FROM cmp_prices WHERE symbol = ANY(%s)", (syms,))
    for s, c in cur.fetchall():
        if c is not None:
            out[s] = float(c)
    missing = [s for s in syms if s not in out]
    if missing:
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close FROM raw_prices
                       WHERE symbol = ANY(%s) ORDER BY symbol, price_date DESC""", (missing,))
        for s, c in cur.fetchall():
            if c is not None:
                out[s] = float(c)
    return out


@router.get("/api/health/portfolios")
def health_portfolios():
    """cc#651 part_5/2: list every saved portfolio with a lightweight invested/current/P&L summary.
    Powers the /health saved-portfolios list AND the /adaptive client shelf. CMP = cmp_prices with a
    latest raw_prices.close fallback (a holding with no price falls back to its avg so it never breaks
    the sum)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, source, created_at FROM hr_portfolios ORDER BY id DESC")
        ports = cur.fetchall()
        cur.execute("SELECT portfolio_id, symbol, qty, avg_price FROM hr_holdings")
        rows = cur.fetchall()
        px = _cmp_map(cur, list({r[1] for r in rows if r[1]}))
    agg = {}
    for pid_, sym, qty, avg in rows:
        a = agg.setdefault(pid_, {"n": 0, "inv": 0.0, "cur": 0.0})
        q = float(qty or 0); ap = float(avg or 0)
        a["n"] += 1
        a["inv"] += q * ap
        a["cur"] += q * px.get(sym, ap)
    out = []
    for id_, name, source, created in ports:
        a = agg.get(id_, {"n": 0, "inv": 0.0, "cur": 0.0})
        inv, curv = a["inv"], a["cur"]
        pnl = curv - inv
        out.append({"id": id_, "name": name, "source": source, "created_at": str(created),
                    "n_holdings": a["n"], "invested": round(inv, 2), "current": round(curv, 2),
                    "pnl": round(pnl, 2), "pnl_pct": (round(pnl / inv * 100.0, 2) if inv else None)})
    return {"portfolios": out}


class HRGenReq(BaseModel):
    portfolio_id: Optional[int] = None
    name: Optional[str] = None
    source: Optional[str] = "mcp"
    holdings: Optional[List[HRHolding]] = None
    white_label: Optional[bool] = True


@router.post("/api/health/generate")
def health_generate(body: HRGenReq):
    """cc#651 part_1: single-call report generation for the MCP bridge (Scorr:hr_report_generate).
    Pass {portfolio_id} to (re)generate a saved portfolio, OR {name + holdings[]} to create one. Runs
    the FULL Portfolio Health pipeline server-side (symbol resolution + build_report) and returns
    {portfolio_id, report_url}. Idempotent per portfolio_id — the report renders live on each load, so
    a repeat call with the same id simply refreshes it; passing holdings WITH a portfolio_id replaces
    that portfolio's holdings in place."""
    pid = body.portfolio_id
    with _conn() as conn, conn.cursor() as cur:
        if body.holdings:
            raw = [{"input": h.symbol, "qty": h.qty, "avg_price": h.avg_price}
                   for h in body.holdings if h.symbol]
            resolved = _resolve(cur, raw)
            keep = [r for r in resolved if r["resolved"]]
            if not keep:
                return {"error": "no holdings resolved to a Scorr symbol",
                        "unresolved": [r["input"] for r in resolved]}
            if pid:
                cur.execute("SELECT 1 FROM hr_portfolios WHERE id=%s", (pid,))
                if not cur.fetchone():
                    return {"error": f"portfolio {pid} not found"}
                if body.name:
                    cur.execute("UPDATE hr_portfolios SET name=%s WHERE id=%s", (body.name, pid))
                cur.execute("DELETE FROM hr_holdings WHERE portfolio_id=%s", (pid,))
            else:
                cur.execute("INSERT INTO hr_portfolios (name, source, created_at) VALUES (%s,%s,NOW()) RETURNING id",
                            (body.name or "Client Portfolio", body.source or "mcp"))
                pid = cur.fetchone()[0]
            for r in keep:
                cur.execute("""INSERT INTO hr_holdings (portfolio_id, symbol, company_name, qty, avg_price, resolved, raw_input)
                               VALUES (%s,%s,%s,%s,%s,TRUE,%s::jsonb)""",
                            (pid, r["symbol"].upper(), r["company_name"], r["qty"], r["avg_price"],
                             json.dumps(r, default=str)))
            conn.commit()
        elif not pid:
            return {"error": "provide portfolio_id OR name + holdings[]"}
        else:
            cur.execute("SELECT 1 FROM hr_portfolios WHERE id=%s", (pid,))
            if not cur.fetchone():
                return {"error": f"portfolio {pid} not found"}
        priced = None
        try:
            from hr_report import build_report
            rep = build_report(cur, pid)
            priced = len((rep or {}).get("holdings") or [])
        except Exception as e:
            priced = f"warm-failed: {e}"
    wl = "1" if (body.white_label is not False) else "0"
    return {"portfolio_id": pid, "report_url": f"/health?pid={pid}&wl={wl}", "holdings_priced": priced}

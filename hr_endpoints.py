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
import logging

import psycopg
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter()
log = logging.getLogger("scorr.hr")
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


# ── cc#757: portfolio repair tool (editable holdings, duplicate merge, activate toggle) ────────────
def _symbol_exists(cur, sym):
    """cc#757: a symbol resolves if it exists in gvm_scores or raw_prices. CASH is always resolved."""
    if not sym or sym.upper() == "CASH":
        return True
    cur.execute("SELECT 1 FROM gvm_scores WHERE UPPER(symbol)=%s LIMIT 1", (sym,))
    if cur.fetchone():
        return True
    cur.execute("SELECT 1 FROM raw_prices WHERE UPPER(symbol)=%s LIMIT 1", (sym,))
    return cur.fetchone() is not None


def _recompute_client_index(cur, pid):
    """cc#757: after ANY holdings edit, refresh client_index.holdings_value / total_portfolio / cash for
    the portfolio's client (hr_portfolios.created_by = account_id). CASH (avg=1) is valued at its qty, never
    priced; equity is valued via _cmp_map. Best-effort — a missing client_index row must never block an edit."""
    cur.execute("SELECT created_by FROM hr_portfolios WHERE id=%s", (pid,))
    row = cur.fetchone()
    if not row or not row[0]:
        return
    account_id = row[0]
    cur.execute("SELECT symbol, qty, avg_price FROM hr_holdings WHERE portfolio_id=%s", (pid,))
    hrows = cur.fetchall()
    eq_syms = [r[0] for r in hrows if r[0] and str(r[0]).upper() != "CASH"]
    px = _cmp_map(cur, eq_syms)
    hv = cash = 0.0
    for sym, qty, avg in hrows:
        q = float(qty or 0)
        if str(sym).upper() == "CASH":
            cash += q
        else:
            hv += q * px.get(sym, float(avg or 0))
    try:
        cur.execute("""UPDATE client_index SET holdings_value=%s, total_portfolio=%s, cash=%s
                       WHERE account_id=%s""", (round(hv, 2), round(hv + cash, 2), round(cash, 2), account_id))
    except Exception:
        pass


class HRHoldingsEditReq(BaseModel):
    holdings: List[HRHolding]


@router.post("/api/health/portfolio/{pid}/holdings")
def health_edit_holdings(pid: int, body: HRHoldingsEditReq):
    """cc#757: REPLACE a saved portfolio's holdings (add / edit / delete via a full set). Each symbol is
    resolved against gvm_scores/raw_prices -> hr_holdings.resolved=false when unmatched. Recomputes the
    client_index values after. Fixing Seema Alice's avg prices flows through here."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM hr_portfolios WHERE id=%s", (pid,))
        if not cur.fetchone():
            return {"error": "portfolio not found"}
        cur.execute("DELETE FROM hr_holdings WHERE portfolio_id=%s", (pid,))
        n = unresolved = 0
        for h in body.holdings:
            sym = (h.symbol or "").strip().upper()
            if not sym:
                continue
            res = _symbol_exists(cur, sym)
            if not res:
                unresolved += 1
            cur.execute("""INSERT INTO hr_holdings (portfolio_id, symbol, company_name, qty, avg_price, resolved, raw_input)
                           VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                        (pid, sym, h.company_name, h.qty, h.avg_price, res, json.dumps(h.dict(), default=str)))
            n += 1
        _recompute_client_index(cur, pid)
        conn.commit()
    return {"portfolio_id": pid, "saved": n, "unresolved": unresolved}


@router.get("/api/health/portfolio/{pid}/merge_preview")
def health_merge_preview(pid: int):
    """cc#757: PREVIEW a duplicate-symbol merge (summed qty + weighted-average avg_price) — never mutates.
    Many NSEPAY accounts carry the same symbol twice (e.g. PRICOLLTD). Returns only the symbols that
    actually have >1 row so the UI can show a preview before the founder confirms."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, qty, avg_price FROM hr_holdings WHERE portfolio_id=%s ORDER BY id", (pid,))
        rows = cur.fetchall()
    groups = {}
    for sym, qty, avg in rows:
        g = groups.setdefault((sym or "").upper(), {"rows": 0, "qty": 0.0, "cost": 0.0})
        q = float(qty or 0)
        g["rows"] += 1; g["qty"] += q; g["cost"] += q * float(avg or 0)
    dups = [{"symbol": s, "rows": g["rows"], "merged_qty": round(g["qty"], 4),
             "merged_avg": round(g["cost"] / g["qty"], 4) if g["qty"] else 0}
            for s, g in groups.items() if g["rows"] > 1]
    return {"portfolio_id": pid, "duplicates": dups, "has_duplicates": bool(dups)}


@router.post("/api/health/portfolio/{pid}/merge")
def health_merge(pid: int):
    """cc#757: APPLY the duplicate merge — one row per symbol, qty summed + avg_price weighted-averaged.
    Recomputes client_index. (Only reached after the preview above; the UI never auto-merges silently.)"""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT symbol, company_name, qty, avg_price, resolved FROM hr_holdings WHERE portfolio_id=%s ORDER BY id", (pid,))
        rows = cur.fetchall()
        if not rows:
            return {"error": "no holdings"}
        groups = {}
        for sym, cname, qty, avg, res in rows:
            k = (sym or "").upper()
            g = groups.setdefault(k, {"cname": cname, "qty": 0.0, "cost": 0.0, "res": bool(res)})
            q = float(qty or 0)
            g["qty"] += q; g["cost"] += q * float(avg or 0)
            if cname and not g["cname"]:
                g["cname"] = cname
            g["res"] = g["res"] and bool(res)
        cur.execute("DELETE FROM hr_holdings WHERE portfolio_id=%s", (pid,))
        for k, g in groups.items():
            avg = (g["cost"] / g["qty"]) if g["qty"] else 0
            cur.execute("""INSERT INTO hr_holdings (portfolio_id, symbol, company_name, qty, avg_price, resolved)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (pid, k, g["cname"], round(g["qty"], 4), round(avg, 4), g["res"]))
        _recompute_client_index(cur, pid)
        conn.commit()
    return {"portfolio_id": pid, "symbols": len(groups)}


@router.post("/api/health/portfolio/{pid}/toggle_active")
def health_toggle_active(pid: int):
    """cc#757: flip a portfolio between active and inactive — hr_portfolios.source X <-> X_INACTIVE — and
    sync client_index.active (ALTER TABLE is lock-blocked, so active lives in the source suffix, id=3041)."""
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT created_by, source FROM hr_portfolios WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            return {"error": "not found"}
        account_id, source = row[0], (row[1] or "")
        if source.endswith("_INACTIVE"):
            new_source, new_active = source[:-len("_INACTIVE")], True
        else:
            new_source, new_active = source + "_INACTIVE", False
        cur.execute("UPDATE hr_portfolios SET source=%s WHERE id=%s", (new_source, pid))
        if account_id:
            try:
                cur.execute("UPDATE client_index SET active=%s WHERE account_id=%s", (new_active, account_id))
            except Exception:
                pass
        conn.commit()
    return {"portfolio_id": pid, "source": new_source, "active": new_active}


def _cmp_map(cur, syms):
    """symbol -> price for portfolio valuation (/health shelf + /adaptive client cards).

    cc#811: now the shared resolver. The old body read cmp_prices with NO freshness check at all —
    any row, at any age, counted as "CMP" — and only fell back to raw_prices when a symbol had no
    cmp_prices row whatsoever. So a symbol whose feed died last week valued a live portfolio at last
    week's price and looked no different from a fresh one. The resolver ladder (live 5-min spot tick
    -> <15-min cache -> EOD close) makes the freshest available number the one that gets used, and
    the 15-minute bound means a stale cache entry falls through to EOD instead of masquerading as
    live. Falls back to the original two-step on any resolver error — a portfolio card must render.
    """
    out = {}
    if not syms:
        return out
    # cc#835: the resolver runs inside a SAVEPOINT. The cc#811 version wrapped it in a bare
    # `except Exception: pass` — but a FAILED cur.execute leaves the transaction ABORTED, so the
    # "fallback" line below immediately raised InFailedSqlTransaction, uncaught, and FastAPI
    # returned a plain-text 500. That is what the /adaptive shelf was parsing as JSON
    # ("Unexpected token I, Internal S..."). A savepoint makes the fallback actually reachable.
    try:
        cur.execute("SAVEPOINT cmp_resolver_try")
    except Exception:
        pass
    try:
        import cmp_resolver
        for s, r in cmp_resolver.resolve_cmp_many(cur, syms).items():
            if r.get("cmp") is not None:
                out[s] = float(r["cmp"])
        if out:
            try:
                cur.execute("RELEASE SAVEPOINT cmp_resolver_try")
            except Exception:
                pass
            return out
    except Exception as e:
        log.warning("cc#835 _cmp_map: resolver failed, falling back to cmp_prices/raw_prices: %s", e)
        out = {}
    try:
        cur.execute("ROLLBACK TO SAVEPOINT cmp_resolver_try")
    except Exception:
        pass
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


def _norm_sym(s):
    """cc#835: resolve_cmp_many normalises its keys to UPPER/stripped; the legacy cmp_prices path
    returns them in whatever case the row holds. Callers look holdings up by the raw
    hr_holdings.symbol, so the two key spaces have to be reconciled somewhere — here. Without this a
    lower-cased or space-padded holding silently valued at its AVG PRICE (P&L 0), which looks like a
    flat stock rather than a lookup miss."""
    return (s or "").strip().upper()


@router.get("/api/health/portfolios")
def health_portfolios():
    """cc#651 part_5/2: list every saved portfolio with a lightweight invested/current/P&L summary.
    Powers the /health saved-portfolios list AND the /adaptive client shelf. CMP = cmp_prices with a
    latest raw_prices.close fallback (a holding with no price falls back to its avg so it never breaks
    the sum).

    cc#835: a display shelf must degrade to an error CARD, never to a parse exception. Whatever goes
    wrong below, this returns JSON — {"portfolios": [], "error": "..."} — so the client always has
    something it can render and read."""
    try:
        return _health_portfolios_inner()
    except Exception as e:
        log.exception("cc#835 /api/health/portfolios failed")
        return {"portfolios": [], "error": f"{type(e).__name__}: {str(e)[:300]}"}


@router.get("/api/health/ledger")
def health_ledger(pid: int):
    """cc#1212: the row-level ledger for one portfolio, for the VIEW LEDGER sheet.

    Returns every row, internal included, because the sheet SHOWS internal rows - greyed, with an
    INTERNAL chip - so a reader can see the whole account history and still read a total that
    only counts real money. is_internal rides along per row so the client never has to infer it.

    404 for an unknown portfolio. An empty rows array with null totals for a real portfolio that
    simply has no ledger: that is a 200, not an error, because "this client has no ledger" is an
    answer and the shelf renders it as an em-dash.
    """
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name FROM hr_portfolios WHERE id=%s", (pid,))
        prow = cur.fetchone()
        if not prow:
            raise HTTPException(status_code=404, detail=f"portfolio {pid} not found")
        cur.execute("""SELECT txn_date, segment, voucher_no, txn_type, amount, narration,
                              is_internal
                       FROM hr_ledger WHERE portfolio_id=%s
                       ORDER BY txn_date ASC, id ASC""", (pid,))
        rows = [{"txn_date": str(r[0]), "segment": r[1], "voucher_no": r[2],
                 "txn_type": r[3], "amount": float(r[4] or 0), "narration": r[5],
                 "is_internal": bool(r[6])} for r in cur.fetchall()]

    counted = [r for r in rows if not r["is_internal"]]
    payin = round(sum(r["amount"] for r in counted if r["txn_type"] == "PAYIN"), 2)
    payout = round(sum(r["amount"] for r in counted if r["txn_type"] == "PAYOUT"), 2)
    return {"portfolio_id": pid, "name": prow[1], "rows": rows,
            # None, not 0, when there is nothing to total - same rule as the shelf.
            "payin_total": (payin if rows else None),
            "payout_total": (payout if rows else None),
            "net_payin": (round(payin - payout, 2) if rows else None),
            "counted_rows": len(counted), "internal_rows": len(rows) - len(counted)}


def _parse_loose_date(v):
    """cc#1205: client_index.tracking_date is TEXT, not DATE, and holds values like "15May2025".
    Returns a date, or None when the value is missing or unparseable — never a raw string, so a
    caller can hand the result to SQL and to a formatter without checking which it got."""
    if v is None:
        return None
    if hasattr(v, "year"):            # already a date/datetime
        return v
    txt = str(v).strip()
    if not txt:
        return None
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y", "%d %b %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return _dt.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def _health_portfolios_inner():
    with _conn() as conn, conn.cursor() as cur:
        # cc#1205: alpha_start_date rides along — the START column's first choice.
        cur.execute("SELECT id, name, created_by, source, created_at, alpha_start_date "
                    "FROM hr_portfolios ORDER BY id DESC")
        ports = cur.fetchall()
        cur.execute("SELECT portfolio_id, symbol, qty, avg_price FROM hr_holdings")
        rows = cur.fetchall()
        # cc#756: active = source has no _INACTIVE suffix. CMP is fetched ONLY for ACTIVE portfolios'
        # EQUITY symbols — never for CASH (a pseudo-holding valued at its qty) and never for inactive
        # clients (their card is a collapsed name+cash line, no valuation).
        active_ids = {p[0] for p in ports if not str(p[3] or "").endswith("_INACTIVE")}
        eq_syms = list({r[1] for r in rows if r[1] and str(r[1]).upper() != "CASH" and r[0] in active_ids})
        px = _cmp_map(cur, eq_syms)
        # cc#654: realised gainloss per portfolio -> invested_adjusted so the shelf reconciles with the report
        cur.execute("SELECT portfolio_id, COALESCE(SUM(gainloss),0) FROM hr_realised GROUP BY portfolio_id")
        realised_map = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
        # cc#1013: portfolio-level headline basis. meta.invested_amount, when set, IS the Invested and
        # headline P&L = current_value + cash - invested (% on invested) — the only honest basis when
        # per-holding buy prices are unknown (all avg_price NULL -> cost ~0 -> the +1880.5% bug). See
        # build_report / founder rule locked 11-Aug-2026.
        # ── cc#1212: per-portfolio ledger aggregates, ONE grouped query for the whole shelf ──
        # is_internal rows are DISPLAY-ONLY. They are pre-split history and the Dec-24/Jan-25
        # transfers to AliceBlue, and counting them would double-count money that never left the
        # client. So they are excluded from every total here and rendered grey in the sheet.
        # ledger_rows counts ALL rows, internal included, because that is what the sheet lists and
        # what the VIEW LEDGER button is gating on - a portfolio with only internal rows still has
        # a ledger worth opening, it just has no countable money in it.
        # NULL, never 0, when a portfolio has no rows at all: "no ledger on record" and "a ledger
        # that nets to zero" are different statements and the shelf renders them differently.
        ledger_map = {}
        try:
            cur.execute("""SELECT portfolio_id,
                                  count(*),
                                  SUM(amount) FILTER (WHERE txn_type='PAYIN'  AND NOT is_internal),
                                  SUM(amount) FILTER (WHERE txn_type='PAYOUT' AND NOT is_internal),
                                  MIN(txn_date), MAX(txn_date)
                           FROM hr_ledger GROUP BY portfolio_id""")
            for _r in cur.fetchall():
                _pin = float(_r[2]) if _r[2] is not None else 0.0
                _pout = float(_r[3]) if _r[3] is not None else 0.0
                ledger_map[_r[0]] = {
                    "ledger_rows": int(_r[1] or 0),
                    "payin_total": round(_pin, 2),
                    "payout_total": round(_pout, 2),
                    "net_payin": round(_pin - _pout, 2),
                    "ledger_first_date": (str(_r[4]) if _r[4] else None),
                    "ledger_last_date": (str(_r[5]) if _r[5] else None),
                }
        except Exception:
            ledger_map = {}   # table absent (pre-migration) -> the shelf renders without the figures

        cur.execute("SELECT portfolio_id, invested_amount, cash, tracking_date FROM hr_portfolio_meta")
        _meta_rows = cur.fetchall()
        meta_map = {r[0]: (float(r[1]) if r[1] is not None else None, float(r[2] or 0)) for r in _meta_rows}
        # cc#1205: the START column's second choice, kept separate so the fallback ORDER is visible.
        meta_track = {r[0]: r[3] for r in _meta_rows if r[3] is not None}
        # cc#756: client_index metadata via created_by=account_id. SELECT ONLY non-secret columns —
        # password/totp_key are NEVER read here (cc#758 denylist honoured by hand-picking columns).
        ci = {}
        try:
            cur.execute("""SELECT account_id, name, platform, investment_amount, tracking_date,
                                  return_pct, active FROM client_index""")
            for r in cur.fetchall():
                ci[r[0]] = {"name": r[1], "platform": r[2],
                            "investment_amount": float(r[3]) if r[3] is not None else None,
                            "tracking_date": str(r[4]) if r[4] is not None else None,
                            "return_pct": float(r[5]) if r[5] is not None else None,
                            "active": bool(r[6]) if r[6] is not None else None}
        except Exception:
            ci = {}   # client_index absent (pre-migration) -> cards still render without the enrichment
        # ── cc#1205: START date and ALPHA vs Nifty 50 ────────────────────────────────────────
        # THE NIFTY RETURN IS NOT COMPUTED HERE. hr_report._nifty50_return_since already does it,
        # and the card says to reuse it if it exists — it does, and the PDF alpha the founder
        # already trusts is built from it. A second copy would drift the first time either moved.
        # Imported inside the function so this module keeps working if hr_report is mid-deploy.
        try:
            from hr_report import _nifty50_return_since as _n50_since
        except Exception:
            _n50_since = None
        # One lookup per DISTINCT start date, not per portfolio — twenty clients sharing a
        # tracking date is one query, not twenty.
        _start_of = {}
        for _p in ports:
            _pid, _alpha_start = _p[0], _p[5]
            if _alpha_start is not None:
                _start_of[_pid] = (_alpha_start, "alpha")
            elif meta_track.get(_pid) is not None:
                _start_of[_pid] = (meta_track[_pid], "meta")
            else:
                # THE THIRD FALLBACK IS FREE TEXT, and that is not a hypothetical. The other two
                # columns are DATE; client_index.tracking_date is TEXT and the live table holds
                # "15May2025". Passing that straight into a date comparison, as a plain COALESCE
                # would, either errors or silently matches nothing — and it would have rendered
                # "15May2025" in a column of "15 May 2025". Parsed defensively; anything that does
                # not parse is treated as ABSENT, because an unreadable date is not a date.
                # It never fires today (every client_index row that has one also has an
                # alpha_start_date) — it fires the first time someone adds a client without one.
                _ci_t = _parse_loose_date((ci.get(_p[2]) or {}).get("tracking_date"))
                _start_of[_pid] = ((_ci_t, "client_index") if _ci_t else (None, None))
        _n50_cache = {}
        if _n50_since is not None:
            with _conn() as _c2, _c2.cursor() as _cur2:
                for _d, _ in set(v for v in _start_of.values() if v[0] is not None):
                    try:
                        _n50_cache[_d] = _n50_since(_cur2, _d)
                    except Exception:
                        _n50_cache[_d] = None

    agg = {}
    for pid_, sym, qty, avg in rows:
        a = agg.setdefault(pid_, {"n": 0, "inv": 0.0, "cur": 0.0, "cash": 0.0, "null_avg": False})
        q = float(qty or 0); ap = float(avg or 0)
        if sym and str(sym).upper() == "CASH":
            a["cash"] += q            # cc#756: value = qty (avg_price=1), NO price lookup, out of equity P&L
            continue
        if pid_ not in active_ids:
            continue                  # inactive client: name+cash only, no equity valuation / CMP
        a["n"] += 1
        if avg is None:               # cc#1013: an unknown buy price -> the holdings-cost basis is not honest
            a["null_avg"] = True
        a["inv"] += q * ap
        a["cur"] += q * px.get(_norm_sym(sym), px.get(sym, ap))
    out = []
    # cc#1205: the SELECT gained alpha_start_date, so this unpack gained a slot. Named `_a_start`
    # and unused here on purpose — the start resolution happens once above, in _start_of, rather
    # than being re-derived per row with a different fallback order.
    for id_, name, created_by, source, created, _a_start in ports:
        a = agg.get(id_, {"n": 0, "inv": 0.0, "cur": 0.0, "cash": 0.0, "null_avg": False})
        realised = realised_map.get(id_, 0.0)
        curv = a["cur"]; holdings_cost = a["inv"]
        m_inv, m_cash = meta_map.get(id_, (None, 0.0))
        cash = a["cash"] + m_cash          # CASH pseudo-holdings + portfolio-level meta cash
        # cc#1013 basis: meta invested is the headline when set; else holdings-cost ONLY if every buy
        # price is known; else no honest % (em-dash + needs-invested hint), never a fabricated number.
        if m_inv is not None and m_inv > 0:
            basis = "meta"; invested = m_inv; needs_inv = False
            pnl = curv + cash - invested
            pnl_pct = round(pnl / invested * 100.0, 2)
        elif not a["null_avg"]:
            basis = "holdings_cost"; needs_inv = False
            invested = holdings_cost - realised   # cc#654: holdings cost - realised gainloss
            pnl = curv - invested                 # == realised + unrealised (equity only; CASH excluded)
            pnl_pct = round(pnl / invested * 100.0, 2) if invested else None
        else:
            basis = "unknown_cost"; invested = None; needs_inv = True
            pnl = None; pnl_pct = None
        src = str(source or "")
        category = "PMS" if src.startswith("PMS") else ("NSEPAY" if src.startswith("NSEPAY") else src)
        _sd, _ssrc = _start_of.get(id_, (None, None))
        _npct = _n50_cache.get(_sd) if _sd is not None else None
        _npct = round(_npct, 1) if _npct is not None else None
        out.append({"id": id_, "name": name, "source": source, "created_by": created_by,
                    "category": category, "active": id_ in active_ids, "created_at": str(created),
                    "n_holdings": a["n"],
                    "invested": (round(invested, 2) if invested is not None else None),
                    "current": round(curv, 2),
                    "cash": round(cash, 2), "total_portfolio": round(curv + cash, 2),
                    "pnl": (round(pnl, 2) if pnl is not None else None), "pnl_pct": pnl_pct,
                    "basis": basis, "needs_invested_amount": needs_inv,
                    "realised_pnl": round(realised, 2), "unrealised_pnl": round(curv - holdings_cost, 2),
                    "holdings_cost": round(holdings_cost, 2),
                    # cc#1205: the real tracking date and where it came from, so the column can
                    # show a date instead of an em-dash and a reader can tell WHICH date it is.
                    "start_date": (str(_sd) if _sd is not None else None),
                    "start_source": _ssrc,
                    "nifty_pct": _npct,
                    # alpha is NULL when either side is null — never 0. "we could not measure it"
                    # and "it matched the index exactly" are different statements.
                    "alpha_pct": (round(pnl_pct - _npct, 1)
                                  if (pnl_pct is not None and _npct is not None) else None),
                    "client": ci.get(created_by),
                    # cc#1212: ledger figures ride along so the shelf needs no second call. All
                    # five are None together when there is no ledger - a partly-filled set would
                    # let a card render a payout with no pay-in beside it.
                    **(ledger_map.get(id_) or {"ledger_rows": 0, "payin_total": None,
                                               "payout_total": None, "net_payin": None,
                                               "ledger_first_date": None, "ledger_last_date": None})})
    return {"portfolios": out}


class HRGenReq(BaseModel):
    portfolio_id: Optional[int] = None
    name: Optional[str] = None
    source: Optional[str] = "mcp"
    holdings: Optional[List[HRHolding]] = None
    white_label: Optional[bool] = True
    alpha_start_date: Optional[str] = None   # cc#653: alpha window start (YYYY-MM-DD); null -> earliest holding entry
    invested_amount: Optional[float] = None  # cc#1013: portfolio-level invested (headline basis)
    cash: Optional[float] = None             # cc#1013: portfolio-level cash
    tracking_date: Optional[str] = None      # cc#1013: tracking start (YYYY-MM-DD)


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
        # cc#653: persist the per-portfolio alpha window start (applies to create/update/pid-only paths)
        if body.alpha_start_date and pid:
            cur.execute("UPDATE hr_portfolios SET alpha_start_date=%s WHERE id=%s", (body.alpha_start_date, pid))
            conn.commit()
        # cc#1013: upsert portfolio-level invested/cash/tracking meta so a portfolio can set the honest
        # headline basis at creation. Only supplied fields are written (COALESCE keeps the rest).
        if pid and (body.invested_amount is not None or body.cash is not None or body.tracking_date):
            cur.execute("""INSERT INTO hr_portfolio_meta (portfolio_id, invested_amount, cash, tracking_date, updated_at)
                           VALUES (%s,%s,%s,%s,NOW())
                           ON CONFLICT (portfolio_id) DO UPDATE SET
                             invested_amount = COALESCE(EXCLUDED.invested_amount, hr_portfolio_meta.invested_amount),
                             cash            = COALESCE(EXCLUDED.cash, hr_portfolio_meta.cash),
                             tracking_date   = COALESCE(EXCLUDED.tracking_date, hr_portfolio_meta.tracking_date),
                             updated_at      = NOW()""",
                        (pid, body.invested_amount, body.cash, body.tracking_date))
            conn.commit()
        priced = None
        try:
            from hr_report import build_report
            rep = build_report(cur, pid)
            priced = len((rep or {}).get("holdings") or [])
        except Exception as e:
            priced = f"warm-failed: {e}"
    wl = "1" if (body.white_label is not False) else "0"
    return {"portfolio_id": pid, "report_url": f"/health?pid={pid}&wl={wl}", "holdings_priced": priced}

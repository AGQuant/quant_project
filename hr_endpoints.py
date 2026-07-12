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


# flexible header detection — a column "counts" if its lowercased name contains any keyword
_COL_KEYS = {
    "symbol":    ["symbol", "ticker", "scrip", "nse", "stock", "instrument", "security", "name", "company"],
    "qty":       ["qty", "quantity", "shares", "units", "holding", "no. of", "no of"],
    "avg_price": ["avg", "average", "buy price", "cost", "purchase", "price", "rate", "acq"],
}


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def _pick_col(headers, keys):
    """Return the index of the first header matching any keyword (earliest keyword wins)."""
    low = [str(h or "").strip().lower() for h in headers]
    for kw in keys:
        for i, h in enumerate(low):
            if kw in h:
                return i
    return None


def _rows_from_table(headers, rows):
    """Given a header list + row lists, map to holdings dicts using flexible column detection."""
    si = _pick_col(headers, _COL_KEYS["symbol"])
    qi = _pick_col(headers, _COL_KEYS["qty"])
    pi = _pick_col(headers, _COL_KEYS["avg_price"])
    out = []
    for r in rows:
        if si is None or si >= len(r):
            continue
        sym = _norm(r[si])
        if not sym or sym.lower() in ("total", "grand total", "nan"):
            continue

        def _num(idx):
            if idx is None or idx >= len(r):
                return None
            v = str(r[idx] or "").replace(",", "").replace("₹", "").strip()
            try:
                return float(v)
            except Exception:
                return None
        out.append({"input": sym, "qty": _num(qi), "avg_price": _num(pi)})
    return out


def _parse_bytes(filename, data):
    """Parse xlsx/csv (pandas) or pdf (best-effort). Returns (rows, warning)."""
    name = (filename or "").lower()
    warning = None
    try:
        import pandas as pd
        if name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(data), header=0, dtype=str)
        elif name.endswith(".csv") or (b"," in data[:2000] and not name.endswith(".pdf")):
            df = pd.read_csv(io.BytesIO(data), header=0, dtype=str)
        elif name.endswith(".pdf"):
            return _parse_pdf(data)
        else:
            df = pd.read_csv(io.BytesIO(data), header=0, dtype=str)
        df = df.fillna("")
        headers = list(df.columns)
        rows = df.values.tolist()
        return _rows_from_table(headers, rows), warning
    except Exception as e:
        return [], f"parse error: {str(e)[:200]}"


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
    first = [c.lower() for c in grid[0]]
    has_header = any(any(kw in c for kw in _COL_KEYS["symbol"]) for c in first) and \
        not any(ch.isdigit() for ch in "".join(grid[0][1:2]))
    if has_header:
        return _rows_from_table(grid[0], grid[1:])
    # no header: assume symbol[, qty[, avg]]
    return _rows_from_table(["symbol", "qty", "avg_price"], grid)


def _resolve(cur, holdings):
    """Resolve each raw input to a Scorr symbol via input_raw: nse_code exact, then company_name fuzzy."""
    resolved = []
    for h in holdings:
        raw = h["input"]
        up = raw.upper().strip()
        cur.execute("SELECT nse_code, company_name FROM input_raw WHERE UPPER(nse_code)=%s LIMIT 1", (up,))
        m = cur.fetchone()
        if not m:
            cur.execute("""SELECT nse_code, company_name FROM input_raw
                           WHERE company_name ILIKE %s ORDER BY LENGTH(company_name) LIMIT 1""",
                        (f"%{raw}%",))
            m = cur.fetchone()
        if m:
            resolved.append({"input": raw, "symbol": m[0], "company_name": m[1],
                             "qty": h.get("qty"), "avg_price": h.get("avg_price"), "resolved": True})
        else:
            resolved.append({"input": raw, "symbol": None, "company_name": None,
                             "qty": h.get("qty"), "avg_price": h.get("avg_price"), "resolved": False})
    return resolved


@router.post("/api/health/upload")
async def health_upload(file: Optional[UploadFile] = File(None), text: Optional[str] = Form(None)):
    """Parse an uploaded holdings file (xlsx/csv/pdf) or pasted text, resolve symbols, return a grid."""
    warning = None
    if file is not None:
        data = await file.read()
        rows, warning = _parse_bytes(file.filename, data)
    elif text:
        rows = _parse_pasted(text)
    else:
        return {"error": "provide a file or pasted text"}
    if not rows:
        return {"error": warning or "no holdings parsed", "rows": [], "warning": warning}
    with _conn() as conn, conn.cursor() as cur:
        resolved = _resolve(cur, rows)
    return {"count": len(resolved), "resolved": sum(1 for r in resolved if r["resolved"]),
            "unresolved": sum(1 for r in resolved if not r["resolved"]),
            "warning": warning, "rows": resolved}


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

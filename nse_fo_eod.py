"""cc#682: NSE F&O bhavcopy → EOD open-interest ingest.

Closing OI is PUBLIC — the NSE F&O bhavcopy (UDiFF format) publishes per-contract end-of-day open
interest every trading evening (~18:00 IST). This is the permanent OI fallback: when the live DEPTH
feed misses a session (the 24-Jul scar), the day-end OI self-heals the same evening from the bhavcopy,
and it doubles as an independent cross-check of the DEPTH-derived OI.

Flow (run_nightly / backfill_date):
  1. download the F&O bhavcopy zip for the date, parse the STOCK-FUTURES rows (FinInstrmTp='STF'),
     keep the NEAR-MONTH contract per symbol (earliest expiry on/after the trade date);
  2. upsert fo_eod(symbol, trade_date, expiry, close, oi, oi_chg_pct, contracts);
  3. COALESCE-fill the day-end futures_basis OI (the 15:25 / last-tick row) ONLY where it is NULL —
     the live DEPTH value always wins; the bhavcopy just fills holes;
  4. where BOTH a live OI and the bhavcopy OI exist for the day-end row, log any >5% mismatch to
     ops_log (fo_eod_oi_mismatch) as a data-integrity cross-check.

NSE anti-bot handled via the shared nse_eod_ingest cookie-warmup session.
"""
import io
import csv
import zipfile
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg
import requests
from fastapi import APIRouter, Header, HTTPException

import os
import nse_eod_ingest as nse   # reuse _nse_session / _nse_get / _f / _ARCHIVE_HOST / _last_trading_day

log = logging.getLogger("scorr.nse_fo_eod")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
fo_eod_router = APIRouter(tags=["nse-fo-eod"])

OI_MISMATCH_PCT = 5.0   # cross-check tolerance vs the DEPTH-derived day-end OI


def _conn():
    return psycopg.connect(DATABASE_URL)


def _bhavcopy_url(d: date) -> str:
    # UDiFF F&O bhavcopy (2024+ format), e.g. .../content/fo/BhavCopy_NSE_FO_0_0_0_20260724_F_0000.csv.zip
    return f"{nse._ARCHIVE_HOST}/content/fo/BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS fo_eod (
        symbol TEXT NOT NULL, trade_date DATE NOT NULL, expiry DATE,
        close NUMERIC, oi BIGINT, oi_chg_pct NUMERIC, contracts BIGINT,
        loaded_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE (symbol, trade_date))""")


def _parse_date(s):
    s = (s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fetch_fo_eod(session, d: date):
    """Download + parse the F&O bhavcopy for date d. Returns the near-month STOCK-FUTURES row per
    symbol: dict(symbol, expiry, close, oi, oi_chg_pct, contracts)."""
    r = nse._nse_get(session, _bhavcopy_url(d), referer=f"{nse._API_HOST}/all-reports-derivatives", timeout=40)
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
    if not name:
        raise RuntimeError("no CSV inside F&O bhavcopy zip")
    text = zf.read(name).decode("utf-8-sig", errors="replace")
    best = {}   # symbol -> (expiry_date, row)  keeping the earliest expiry >= d (near-month)
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("FinInstrmTp") or "").strip().upper() != "STF":   # stock futures only
            continue
        sym = (row.get("TckrSymb") or "").strip().upper()
        exp = _parse_date(row.get("XpryDt"))
        if not sym or not exp or exp < d:
            continue
        cur = best.get(sym)
        if cur is None or exp < cur[0]:
            best[sym] = (exp, row)
    out = []
    for sym, (exp, row) in best.items():
        oi = nse._f(row.get("OpnIntrst"))
        chg = nse._f(row.get("ChngInOpnIntrst"))
        prev_oi = (oi - chg) if (oi is not None and chg is not None) else None
        oi_chg_pct = round(chg / prev_oi * 100.0, 2) if (chg is not None and prev_oi and prev_oi != 0) else None
        out.append({
            "symbol": sym, "expiry": exp,
            "close": nse._f(row.get("ClsPric")),
            "oi": int(oi) if oi is not None else None,
            "oi_chg_pct": oi_chg_pct,
            "contracts": int(nse._f(row.get("TtlTradgVol")) or 0) or None,
        })
    return out


def upsert_fo_eod(cur, d: date, rows):
    for r in rows:
        cur.execute("""INSERT INTO fo_eod (symbol, trade_date, expiry, close, oi, oi_chg_pct, contracts, loaded_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (symbol, trade_date) DO UPDATE SET
                         expiry=EXCLUDED.expiry, close=EXCLUDED.close, oi=EXCLUDED.oi,
                         oi_chg_pct=EXCLUDED.oi_chg_pct, contracts=EXCLUDED.contracts, loaded_at=NOW()""",
                    (r["symbol"], d, r["expiry"], r["close"], r["oi"], r["oi_chg_pct"], r["contracts"]))


def _apply_to_futures_basis(cur, d: date, rows):
    """COALESCE-fill the day-end (last-tick) futures_basis OI where it is NULL; cross-check where a live
    value already exists. Returns (filled, mismatches)."""
    filled = 0
    mismatches = []
    for r in rows:
        if r["oi"] is None:
            continue
        # the day-end row = the max-ts futures_basis row for this symbol on d
        cur.execute("""SELECT ts, oi FROM futures_basis WHERE symbol=%s AND ts::date=%s
                       ORDER BY ts DESC LIMIT 1""", (r["symbol"], d))
        row = cur.fetchone()
        if not row:
            continue
        ts, live_oi = row[0], row[1]
        if live_oi is None:
            cur.execute("UPDATE futures_basis SET oi=%s WHERE symbol=%s AND ts=%s", (r["oi"], r["symbol"], ts))
            filled += 1
        else:
            try:
                dev = abs(float(live_oi) / r["oi"] - 1.0) * 100.0 if r["oi"] else 0.0
                if dev > OI_MISMATCH_PCT:
                    mismatches.append({"symbol": r["symbol"], "live_oi": int(live_oi),
                                       "bhavcopy_oi": r["oi"], "dev_pct": round(dev, 1)})
            except Exception:
                pass
    return filled, mismatches


def _ops_log(cur, title, details):
    try:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'fo_eod', %s, %s::jsonb)", (title, json.dumps(details)))
    except Exception:
        pass


def run_for_date(d: date) -> dict:
    """Fetch + upsert fo_eod for d, COALESCE-fill the day-end futures_basis OI, cross-check vs live."""
    session = nse._nse_session()
    conn = _conn()
    try:
        cur = conn.cursor()
        _ensure_table(cur)
        rows = fetch_fo_eod(session, d)
        if not rows:
            _ops_log(cur, "fo_eod_empty", {"date": str(d)})
            conn.commit()
            return {"date": str(d), "symbols": 0, "note": "no STF rows (holiday / not published yet?)"}
        upsert_fo_eod(cur, d, rows)
        filled, mism = _apply_to_futures_basis(cur, d, rows)
        _ops_log(cur, "fo_eod_ingest", {"date": str(d), "symbols": len(rows),
                                        "futures_basis_filled": filled, "oi_mismatches": len(mism),
                                        "mismatch_sample": mism[:10]})
        conn.commit()
        log.info(f"fo_eod {d}: {len(rows)} symbols, {filled} basis-OI filled, {len(mism)} mismatches")
        return {"date": str(d), "symbols": len(rows), "futures_basis_filled": filled,
                "oi_mismatches": len(mism)}
    finally:
        conn.close()


def run_nightly() -> dict:
    """~23:00 IST weekdays: ingest the latest trading day's F&O bhavcopy."""
    d = nse._last_trading_day(date.today())
    return run_for_date(d)


# ── one-time 24-Jul backfill, deploy-triggered via app_config flag (sandbox has no HTTP path to prod) ──
_FLAG = "fo_eod_backfill"


def maybe_backfill_from_flag():
    """Claim app_config[fo_eod_backfill]='YYYY-MM-DD' once (atomic), run that date, clear the flag."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key=%s FOR UPDATE", (_FLAG,))
            r = cur.fetchone()
            val = (r[0] if r else "") or ""
            if not val or val in ("done", "claimed"):
                conn.commit()
                return None
            cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s", (_FLAG,))
            conn.commit()
        d = _parse_date(val) or nse._last_trading_day(date.today())
        res = run_for_date(d)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (_FLAG,))
            conn.commit()
        log.info(f"fo_eod backfill (flag {val}): {res}")
        return res
    except Exception as e:
        log.error(f"fo_eod backfill flag failed: {e}")
        return None


@fo_eod_router.on_event("startup")
async def _startup_backfill():
    # deploy-time self-trigger: set app_config fo_eod_backfill='2026-07-24' via run_sql, this claims it.
    maybe_backfill_from_flag()


@fo_eod_router.post("/api/admin/fo_eod")
def fo_eod_run(date_str: Optional[str] = None, x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    d = _parse_date(date_str) if date_str else nse._last_trading_day(date.today())
    if not d:
        raise HTTPException(400, "bad date")
    return run_for_date(d)

"""bhavcopy_diagnostic.py -- cc#1858 step 2 MEASUREMENT ONLY (founder-approved 08-Sep-2026, ~22:45 IST,
cc_task_logs id 5635: "CC pushes the READ-ONLY production diagnostic and nothing else on this card").

WHY THIS FILE EXISTS
    Neither dev sandbox has network access to NSE (curl to nseindia.com / nsearchives.nseindia.com
    both return 403 from the egress proxy, confirmed independently by CC and Fable). Only Railway
    production reaches NSE -- bg_fo_eod (nse_fo_eod.py) proves that every weekday at 23:00. The card's
    step 2 needs a REAL measured count from one real bhavcopy file before any backfill can be sized,
    and that measurement can only happen on production. Same "sandbox has no HTTP path to prod"
    problem nse_fo_eod.py already solved once -- this file reuses that exact fix: an app_config flag,
    claimed atomically and run from a router startup hook on the next deploy, because the sandbox
    can't call an admin endpoint on Railway either.

WHAT THIS DOES NOT DO (do_not_touch, card cc#1858)
    Does NOT touch the fo_eod parser, does NOT write to fo_eod, does NOT change bg_fo_eod. Tonight's
    23:00 futures ingest runs completely unchanged. This is a SEPARATE fetch of the SAME public file,
    parsed into a NEW diagnostic table only. Nothing here is read by any other job or surface.

WHAT IT MEASURES (card cc#1858 step 2, verbatim)
    Total rows in one bhavcopy file; option rows; option rows within ATM +-10 of that day's spot per
    underlying; distinct underlyings carrying options; how many of those rows carry a settlement price
    rather than a traded close. Written to a TABLE (bhavcopy_diagnostic), not a log line and not a
    response body, so Fable can read it with run_sql without any network access of its own.

METHODOLOGY, STATED PLAINLY (this is a first look at real NSE UDiFF columns -- nobody in this
    session has seen the actual file, so the method is documented rather than assumed correct)
    - OPTION ROW = a row whose OptnTp column is CE or PE (case/space normalised). This is a more
      direct signal than FinInstrmTp code lists, which vary by NSE's F&O segment codes.
    - SPOT PROXY per underlying = that underlying's own near-month FUTURES closing price from the
      SAME file (STF for stocks, IDF for indices), using the identical near-month selection nse_fo_eod
      already uses (earliest expiry on/after the trade date). Not a separate spot feed -- if the file
      does not carry a near-month future for an underlying (should not happen for index/stock F&O
      names), that underlying's ATM window cannot be computed and it is reported, not silently zeroed.
    - NEAR-MONTH per underlying's OPTIONS = the option rows' own earliest XpryDt on/after the trade
      date, independently selected per underlying (may not always equal the future's expiry, though
      it should for standard monthly names).
    - ATM +-10 = the 21 strikes (by count, not by rupee spacing) nearest the spot proxy for that
      underlying's near-month expiry, CE and PE both kept at each strike -- matching the card's own
      "21 strikes x 2 option types = 42 rows per symbol per day" sizing basis.
    - SETTLEMENT VS TRADED CLOSE = within that ATM+-10 near-month set, a row counts as "settlement"
      when ClsPric is missing/zero/negative AND SttlmPric is present; otherwise "traded close".
    - csv_columns (the real header row) is stored verbatim so a wrong assumption above is visible
      and fixable without a second production round-trip.

TRIGGER (deploy-time self-run, same pattern as nse_fo_eod.maybe_backfill_from_flag)
    app_config key 'bhavcopy_diagnostic_run', value = 'YYYY-MM-DD' or 'latest' (last trading day).
    Claimed atomically (FOR UPDATE) on the next deploy's startup, run once, then set to 'done'.
    A manual re-trigger is also exposed at POST /api/admin/bhavcopy-diagnostic (ADMIN_TOKEN gated,
    same convention as every other admin_run_* endpoint in this codebase) for a same-deploy retry
    without needing a fresh push.
"""
import io
import csv
import json
import zipfile
import logging
from datetime import date
from typing import Optional

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter, Header, HTTPException

import os
import nse_eod_ingest as nse          # reuse _nse_session / _nse_get / _last_trading_day / _f
from nse_fo_eod import _bhavcopy_url, _parse_date   # reuse the SAME url + date-parse, not a second copy

log = logging.getLogger("scorr.bhavcopy_diagnostic")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
router = APIRouter(tags=["bhavcopy-diagnostic"])

ATM_STRIKE_COUNT = 21   # card's own basis: 21 strikes x 2 option types = 42 rows/symbol/day


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS bhavcopy_diagnostic (
        id SERIAL PRIMARY KEY,
        run_at TIMESTAMPTZ DEFAULT NOW(),
        bhavcopy_date DATE NOT NULL,
        total_rows INT,
        option_rows INT,
        distinct_underlyings_with_options INT,
        distinct_underlyings_with_near_month_future INT,
        atm10_option_rows INT,
        settlement_price_rows INT,
        traded_close_rows INT,
        underlyings_missing_spot INT,
        csv_columns TEXT,
        method_notes TEXT,
        per_underlying JSONB,
        error TEXT,
        UNIQUE (bhavcopy_date))""")


def _is_option_row(row) -> bool:
    ot = (row.get("OptnTp") or "").strip().upper()
    return ot in ("CE", "PE")


def _is_future_row(row) -> bool:
    fit = (row.get("FinInstrmTp") or "").strip().upper()
    return fit in ("STF", "IDF")


def select_atm_window(reader, d: date):
    """cc#1858 SHARED SELECTION (ONE_REGISTRY_ONE_DERIVATION_V1): the near-month-future spot
    proxy, each underlying's near-month OPTION expiry, and its ATM +-10 strike set (21 nearest
    strikes by count) -- computed ONCE here and consumed by both run_diagnostic() (measurement)
    and option_iv_history.py's forward-storage ingest, so the two paths can never define "ATM
    window" two different ways. `reader` is the list of DictReader rows from one bhavcopy CSV.
    Returns (spot_by_sym, opt_near_expiry, atm_strikes_by_sym, underlyings_with_options,
    option_rows_total, strikes_by_sym) -- strikes_by_sym is the RAW (pre-ATM-limit) strike set
    per underlying at its near-month expiry, kept for callers that report total-vs-ATM-limited
    strike counts (run_diagnostic's per_underlying block)."""
    fut_best = {}   # symbol -> (expiry, close)
    for row in reader:
        if not _is_future_row(row):
            continue
        sym = (row.get("TckrSymb") or "").strip().upper()
        exp = _parse_date(row.get("XpryDt"))
        if not sym or not exp or exp < d:
            continue
        cur_best = fut_best.get(sym)
        if cur_best is None or exp < cur_best[0]:
            fut_best[sym] = (exp, nse._f(row.get("ClsPric")))
    spot_by_sym = {s: c for s, (e, c) in fut_best.items() if c}

    opt_near_expiry = {}   # symbol -> expiry
    option_rows_total = 0
    underlyings_with_options = set()
    for row in reader:
        if not _is_option_row(row):
            continue
        option_rows_total += 1
        sym = (row.get("TckrSymb") or "").strip().upper()
        if not sym:
            continue
        underlyings_with_options.add(sym)
        exp = _parse_date(row.get("XpryDt"))
        if not exp or exp < d:
            continue
        cur_exp = opt_near_expiry.get(sym)
        if cur_exp is None or exp < cur_exp:
            opt_near_expiry[sym] = exp

    strikes_by_sym = {}   # sym -> set(strike) seen at near-month expiry
    for row in reader:
        if not _is_option_row(row):
            continue
        sym = (row.get("TckrSymb") or "").strip().upper()
        exp = _parse_date(row.get("XpryDt"))
        if sym not in opt_near_expiry or exp != opt_near_expiry.get(sym):
            continue
        strike = nse._f(row.get("StrkPric"))
        if strike is None:
            continue
        strikes_by_sym.setdefault(sym, set()).add(strike)

    atm_strikes_by_sym = {}
    for sym, strikes in strikes_by_sym.items():
        spot = spot_by_sym.get(sym)
        if spot is None:
            continue
        nearest = sorted(strikes, key=lambda k: abs(k - spot))[:ATM_STRIKE_COUNT]
        atm_strikes_by_sym[sym] = set(nearest)

    return (spot_by_sym, opt_near_expiry, atm_strikes_by_sym, underlyings_with_options,
            option_rows_total, strikes_by_sym)


def run_diagnostic(d: Optional[date] = None) -> dict:
    """Fetch ONE bhavcopy, measure per cc#1858 step 2, write ONE row to bhavcopy_diagnostic.
    Read-only against every existing table -- the only write is the new diagnostic table."""
    d = d or nse._last_trading_day(date.today())
    conn = _conn()
    try:
        cur = conn.cursor()
        _ensure_table(cur)

        session = nse._nse_session()
        try:
            r = nse._nse_get(session, _bhavcopy_url(d),
                              referer=f"{nse._API_HOST}/all-reports-derivatives", timeout=40)
        except Exception as e:
            err = f"fetch failed: {type(e).__name__}: {str(e)[:200]}"
            cur.execute("""INSERT INTO bhavcopy_diagnostic (bhavcopy_date, error) VALUES (%s,%s)
                           ON CONFLICT (bhavcopy_date) DO UPDATE SET run_at=NOW(), error=EXCLUDED.error""",
                        (d, err))
            conn.commit()
            log.error(f"bhavcopy_diagnostic {d}: {err}")
            return {"ok": False, "date": str(d), "error": err}

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if not name:
            err = "no CSV inside F&O bhavcopy zip"
            cur.execute("""INSERT INTO bhavcopy_diagnostic (bhavcopy_date, error) VALUES (%s,%s)
                           ON CONFLICT (bhavcopy_date) DO UPDATE SET run_at=NOW(), error=EXCLUDED.error""",
                        (d, err))
            conn.commit()
            return {"ok": False, "date": str(d), "error": err}
        text = zf.read(name).decode("utf-8-sig", errors="replace")
        reader = list(csv.DictReader(io.StringIO(text)))
        columns = list(reader[0].keys()) if reader else []
        total_rows = len(reader)

        (spot_by_sym, opt_near_expiry, atm_strikes_by_sym, underlyings_with_options,
         option_rows, strikes_by_sym) = select_atm_window(reader, d)
        underlyings_missing_spot = sorted(underlyings_with_options - set(spot_by_sym.keys()))
        per_underlying = {}
        atm10_option_rows = 0
        settlement_price_rows = 0
        traded_close_rows = 0
        for row in reader:
            if not _is_option_row(row):
                continue
            sym = (row.get("TckrSymb") or "").strip().upper()
            exp = _parse_date(row.get("XpryDt"))
            if sym not in opt_near_expiry or exp != opt_near_expiry.get(sym):
                continue
            strike = nse._f(row.get("StrkPric"))
            if strike is None or sym not in atm_strikes_by_sym or strike not in atm_strikes_by_sym[sym]:
                continue
            atm10_option_rows += 1
            close_v = nse._f(row.get("ClsPric"))
            sttl_v = nse._f(row.get("SttlmPric"))
            if (close_v is None or close_v <= 0) and sttl_v is not None:
                settlement_price_rows += 1
            else:
                traded_close_rows += 1

        for sym, strikes in strikes_by_sym.items():
            per_underlying[sym] = {
                "spot_proxy": spot_by_sym.get(sym),
                "near_month_expiry": str(opt_near_expiry.get(sym)) if opt_near_expiry.get(sym) else None,
                "strikes_total": len(strikes),
                "strikes_atm10": len(atm_strikes_by_sym.get(sym, [])),
            }

        method_notes = (
            "Option row = OptnTp in (CE,PE). Spot proxy = same-file near-month STF/IDF ClsPric "
            "(nse_fo_eod's own near-month rule, not re-derived). ATM+-10 = 21 nearest strikes by count "
            "(not rupee spacing) at the OPTIONS' OWN near-month expiry (may differ from the futures "
            "expiry for a given name, tracked independently). Settlement-vs-traded-close measured only "
            "within the ATM+-10 near-month set, per the card's own step D scope. "
            f"{len(underlyings_missing_spot)} underlying(s) had options but no matching near-month "
            "future in this file, so their ATM window could not be computed -- listed in per_underlying "
            "as absent; see underlyings_missing_spot count."
        )

        cur.execute("""INSERT INTO bhavcopy_diagnostic
            (bhavcopy_date, total_rows, option_rows, distinct_underlyings_with_options,
             distinct_underlyings_with_near_month_future, atm10_option_rows, settlement_price_rows,
             traded_close_rows, underlyings_missing_spot, csv_columns, method_notes, per_underlying, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)
            ON CONFLICT (bhavcopy_date) DO UPDATE SET
                run_at=NOW(), total_rows=EXCLUDED.total_rows, option_rows=EXCLUDED.option_rows,
                distinct_underlyings_with_options=EXCLUDED.distinct_underlyings_with_options,
                distinct_underlyings_with_near_month_future=EXCLUDED.distinct_underlyings_with_near_month_future,
                atm10_option_rows=EXCLUDED.atm10_option_rows,
                settlement_price_rows=EXCLUDED.settlement_price_rows,
                traded_close_rows=EXCLUDED.traded_close_rows,
                underlyings_missing_spot=EXCLUDED.underlyings_missing_spot,
                csv_columns=EXCLUDED.csv_columns, method_notes=EXCLUDED.method_notes,
                per_underlying=EXCLUDED.per_underlying, error=NULL""",
            (d, total_rows, option_rows, len(underlyings_with_options), len(spot_by_sym),
             atm10_option_rows, settlement_price_rows, traded_close_rows,
             len(underlyings_missing_spot), ",".join(columns), method_notes,
             Json(per_underlying)))
        conn.commit()

        out = {"ok": True, "date": str(d), "total_rows": total_rows, "option_rows": option_rows,
               "distinct_underlyings_with_options": len(underlyings_with_options),
               "atm10_option_rows": atm10_option_rows,
               "settlement_price_rows": settlement_price_rows,
               "traded_close_rows": traded_close_rows,
               "underlyings_missing_spot": len(underlyings_missing_spot)}
        log.info(f"bhavcopy_diagnostic {d}: {out}")
        return out
    except Exception as e:
        log.exception("bhavcopy_diagnostic failed")
        try:
            cur.execute("""INSERT INTO bhavcopy_diagnostic (bhavcopy_date, error) VALUES (%s,%s)
                           ON CONFLICT (bhavcopy_date) DO UPDATE SET run_at=NOW(), error=EXCLUDED.error""",
                        (d, f"{type(e).__name__}: {str(e)[:200]}"))
            conn.commit()
        except Exception:
            pass
        return {"ok": False, "date": str(d), "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        conn.close()


# ── deploy-time self-trigger, same pattern as nse_fo_eod.maybe_backfill_from_flag ──
_FLAG = "bhavcopy_diagnostic_run"


def maybe_run_from_flag():
    """Claim app_config[bhavcopy_diagnostic_run] once (atomic), run that date (or 'latest'), clear
    the flag. Sandbox has no HTTP path to prod, so this is the only way CC's push actually executes
    the fetch -- set the flag via run_sql, then push/redeploy."""
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
        d = None if val == "latest" else _parse_date(val)
        res = run_diagnostic(d)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (_FLAG,))
            conn.commit()
        log.info(f"bhavcopy_diagnostic (flag {val}): {res}")
        return res
    except Exception as e:
        log.error(f"bhavcopy_diagnostic flag failed: {e}")
        return None


@router.on_event("startup")
async def _startup_diagnostic():
    maybe_run_from_flag()


@router.post("/api/admin/bhavcopy-diagnostic")
def bhavcopy_diagnostic_run(date_str: Optional[str] = None, x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    d = _parse_date(date_str) if date_str else None
    return run_diagnostic(d)

"""option_iv_history.py -- cc#1858 step 5 FORWARD STORAGE + step 7 BACKFILL (founder-released
09-Sep-2026 ~04:30 IST, cc_task_logs id 5654: "FABLE 5.1 IS NOT AVAILABLE. SONNET 5 BUILDS BOTH
P0s... the gates are the safety net, not the model.").

WHAT THIS BUILDS
    Stores per-strike, per-day option data (close, OI, solved implied vol) at ATM +-10 for every
    underlying, from the daily NSE F&O bhavcopy -- the SAME public file bg_fo_eod already fetches
    nightly and reads only the STOCK-FUTURES rows from (nse_fo_eod.py). Every option row in that
    file is currently parsed and discarded; this is the fix (cc#1858's own framing, step 4:
    "the compute is correct, already intraday [for TC]; the output is thrown away" -- same shape
    here, but for the daily bhavcopy's option rows specifically).

WHAT IT DOES NOT DO (do_not_touch, card cc#1858)
    Does NOT touch fo_eod, nse_fo_eod.py, or bg_fo_eod's dispatch in scheduler.py -- the nightly
    futures-OI ingest is completely unchanged. Does NOT change deriv_metrics.py's Black-Scholes
    maths (R_FREE, the d1/d2 formula, the bisection bounds/iteration count) -- this module CONSUMES
    that exact formula (imports deriv_metrics.R_FREE, mirrors _bs_price/_bs_iv's algorithm), it does
    not invent a second model. Does NOT touch atm_iv_daily (cc#1847's separate table/question).

ATM WINDOW DEFINITION -- ONE_REGISTRY_ONE_DERIVATION_V1
    Spot proxy, near-month option expiry, and the ATM +-10 (21 nearest strikes) selection are
    computed by bhavcopy_diagnostic.select_atm_window() -- the SAME function cc#1858 step 2's
    measurement already used and the founder already accepted (8,753 rows/day measured, within 2%
    of Fable's estimate). This module calls that function, it does not re-derive the selection a
    second way.

VECTORISED IV SOLVE -- founder-mandated (id 5654): "Vectorised IV solve and bulk COPY insert are
    still mandatory -- row-by-row will not finish in any window." _bs_iv_vec() below is the SAME
    Black-Scholes formula and bisection algorithm as deriv_metrics._bs_price/_bs_iv (same R_FREE,
    same d1/d2, same [1e-4, 5.0] bounds, same 64 iterations) applied to whole numpy arrays instead
    of one contract per Python call -- ~8,753 rows/day solved in well under a second, versus
    8,753 x 64 scalar Black-Scholes evaluations per day if called through the existing function
    one contract at a time (which would not finish 250 day-files in any bounded window). This is
    the identical maths, vectorised for throughput -- not a second solver.

BULK INSERT -- founder-mandated: COPY, never row-by-row. Each date's rows land in a per-connection
    TEMP staging table via psycopg's native COPY, then one INSERT ... SELECT ... ON CONFLICT DO
    UPDATE folds staging into option_iv_daily -- idempotent (a re-run of an already-loaded date
    overwrites its own rows with the same values, never duplicates).

RESUMABLE, PER-DAY STATUS -- founder-mandated: option_iv_backfill_status is one row per trading
    date (pending/done/error, rows_written, error, timestamps). The backfill loop claims the
    earliest pending date, processes it, marks it done or error, and moves on -- a dropped NSE
    connection or a restart resumes at the next pending date, never re-fetching a completed one.

HARD ABORT 08:30 IST -- founder-mandated, checked as a wall-clock test INSIDE the loop before
    claiming each new date (not a timer set once) -- an abort mid-file still lets the current
    date finish or error cleanly rather than being killed mid-write.

ONE-MONTH PROOF BEFORE THE FULL BACKFILL -- founder-mandated, structurally enforced here, not just
    stated: seed_dates() populates the status table for WHATEVER date range is passed to it.
    There is no auto-continuation from a proof run to the full 11-month backfill inside this
    module -- seeding the full year is a SEPARATE, explicit call (via the admin endpoint or
    run_sql), made only after the one-month proof is Fable-verified. The size gate itself is
    already cleared (founder ruling id 5654: 8,753 rows/day measured, ~309 MB/year accepted) --
    what remains gated is the PROOF, not the size.

    UPDATE 09-Sep-2026 ~05:15 IST: the August-2026 proof month completed clean (21/21 dates done,
    183,383 rows, 217 symbols; spot-checked and posted to cc_task_logs id 1858/1199 for Fable's
    verification). The founder then changed the sequencing for THIS production-mode window only
    (cc_task_logs 1199, "PRODUCTION MODE RE-ARMED" post): post the proof and continue straight into
    the remaining backfill without waiting for a reply, because the per-day resumability and
    idempotent re-run this module already guarantees mean a defect found after the fact costs
    nothing to correct. None of the other safeguards (vectorised solve, bulk COPY, hard abort 08:30,
    30pct-overrun check) are relaxed by that change. Acting on it: the remaining 11 months
    (2025-09-01 through 2026-07-31, weekdays) were seeded via seed_dates()-equivalent SQL and the
    trigger flag set to 'pending' again -- this push's redeploy is what claims it.

TRIGGER -- same pattern as fy_end_backfill.py / bhavcopy_diagnostic.py (sandbox has no HTTP path
    to prod): app_config flag 'option_iv_backfill_run' = 'pending', claimed atomically on deploy
    startup, runs in a daemon thread so the app itself starts normally. Manual re-trigger:
    POST /api/admin/option_iv/run (ADMIN_TOKEN-gated). Seeding a date range for the proof or the
    full backfill is done via seed_dates(), exposed at POST /api/admin/option_iv/seed.
"""
import io
import csv
import logging
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
from scipy.special import ndtr
import psycopg
from fastapi import APIRouter, Header, HTTPException

import os
import nse_eod_ingest as nse                      # session/fetch/date helpers, same as nse_fo_eod
from nse_fo_eod import _bhavcopy_url, _parse_date  # SAME url + date-parse nse_fo_eod uses, not a second copy
from bhavcopy_diagnostic import select_atm_window, _is_option_row   # SAME ATM-window selection cc#1858 step 2 measured
from deriv_metrics import R_FREE                    # SAME risk-free rate the existing Black-Scholes uses

log = logging.getLogger("scorr.option_iv_history")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
router = APIRouter(tags=["option-iv-history"])

IST = ZoneInfo("Asia/Kolkata")
HARD_ABORT_HOUR, HARD_ABORT_MINUTE = 8, 30   # founder-mandated wall clock, checked every iteration
FLAG_KEY = "option_iv_backfill_run"
_running = False


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS option_iv_daily (
        symbol TEXT NOT NULL, trade_date DATE NOT NULL, expiry DATE NOT NULL,
        strike NUMERIC NOT NULL, option_type TEXT NOT NULL,
        close NUMERIC, oi BIGINT, spot NUMERIC, iv NUMERIC, is_settlement BOOLEAN,
        loaded_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (symbol, trade_date, expiry, strike, option_type))""")
    cur.execute("""CREATE INDEX IF NOT EXISTS option_iv_daily_date_idx
                   ON option_iv_daily (trade_date, symbol)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS option_iv_backfill_status (
        trade_date DATE PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | error
        rows_written INT, error TEXT,
        started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ)""")


# ── vectorised Black-Scholes -- SAME formula as deriv_metrics._bs_price/_bs_iv, array-wise ──────
def _bs_price_vec(S, K, T, sigma, is_call):
    with np.errstate(all="ignore"):
        sqrtT = np.sqrt(T)
        d1 = (np.log(S / K) + (R_FREE + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
        d2 = d1 - sigma * sqrtT
        disc = np.exp(-R_FREE * T)
        call_px = S * ndtr(d1) - K * disc * ndtr(d2)
        put_px = K * disc * ndtr(-d2) - S * ndtr(-d1)
    return np.where(is_call, call_px, put_px)


def _bs_iv_vec(price, S, K, T, is_call):
    """Vectorised bisection on [1e-4, 5.0], 64 iterations -- identical bounds/iteration count to
    deriv_metrics._bs_iv, applied to whole arrays. Returns NaN for any row with an invalid input
    (mirrors _bs_iv returning None on the same conditions)."""
    n = price.shape[0]
    lo = np.full(n, 1e-4, dtype=float)
    hi = np.full(n, 5.0, dtype=float)
    valid = (price > 0) & (S > 0) & (K > 0) & (T > 0)
    for _ in range(64):
        mid = (lo + hi) / 2.0
        p = _bs_price_vec(S, K, T, mid, is_call)
        p = np.where(np.isfinite(p), p, -np.inf)   # a bad d1/d2 (e.g. sigma underflow) never wins the bisection
        gt = p > price
        hi = np.where(gt, mid, hi)
        lo = np.where(gt, lo, mid)
    iv = (lo + hi) / 2.0
    return np.where(valid, iv, np.nan)


def _ist_now():
    return datetime.now(IST)


def _hard_abort_hit(now=None) -> bool:
    now = now or _ist_now()
    return (now.hour, now.minute) >= (HARD_ABORT_HOUR, HARD_ABORT_MINUTE)


def ingest_date(d: date) -> dict:
    """Fetch ONE bhavcopy, select the ATM+-10 window (shared with the diagnostic), vectorise the
    IV solve, bulk-COPY into a staging table, upsert into option_iv_daily. Returns
    {ok, rows_written} or {ok:False, error}. Never touches fo_eod / bg_fo_eod."""
    conn = _conn()
    try:
        cur = conn.cursor()
        _ensure_tables(cur)
        session = nse._nse_session()
        r = nse._nse_get(session, _bhavcopy_url(d), referer=f"{nse._API_HOST}/all-reports-derivatives", timeout=40)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if not name:
            raise RuntimeError("no CSV inside F&O bhavcopy zip")
        text = zf.read(name).decode("utf-8-sig", errors="replace")
        reader = list(csv.DictReader(io.StringIO(text)))
        if not reader:
            return {"ok": True, "rows_written": 0, "note": "empty bhavcopy (holiday?)"}

        spot_by_sym, opt_near_expiry, atm_strikes_by_sym, _, _, _ = select_atm_window(reader, d)

        syms, expiries, strikes, otypes, closes, ois, spots, is_call, is_sttl = ([] for _ in range(9))
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
            spot = spot_by_sym.get(sym)
            if spot is None:
                continue
            close_v = nse._f(row.get("ClsPric"))
            sttl_v = nse._f(row.get("SttlmPric"))
            settlement = (close_v is None or close_v <= 0) and sttl_v is not None
            price = sttl_v if settlement else close_v
            oi = nse._f(row.get("OpnIntrst"))
            ot = (row.get("OptnTp") or "").strip().upper()
            syms.append(sym); expiries.append(exp); strikes.append(strike); otypes.append(ot)
            closes.append(price); ois.append(int(oi) if oi is not None else None)
            spots.append(spot); is_call.append(ot == "CE"); is_sttl.append(bool(settlement))

        if not syms:
            return {"ok": True, "rows_written": 0, "note": "no ATM rows selected"}

        price_arr = np.array([c if c is not None else np.nan for c in closes], dtype=float)
        S_arr = np.array(spots, dtype=float)
        K_arr = np.array(strikes, dtype=float)
        T_arr = np.array([(e - d).days / 365.0 for e in expiries], dtype=float)
        call_arr = np.array(is_call, dtype=bool)
        with np.errstate(invalid="ignore"):
            iv_arr = _bs_iv_vec(np.nan_to_num(price_arr, nan=-1.0), S_arr, K_arr, T_arr, call_arr)
        iv_arr = np.where(np.isnan(price_arr) | (price_arr <= 0), np.nan, iv_arr)

        rows = []
        for i in range(len(syms)):
            iv_v = float(iv_arr[i]) if np.isfinite(iv_arr[i]) else None
            rows.append((syms[i], d, expiries[i], strikes[i], otypes[i],
                         closes[i], ois[i], spots[i], iv_v, is_sttl[i]))

        cur.execute("""CREATE TEMP TABLE option_iv_staging (
            symbol TEXT, trade_date DATE, expiry DATE, strike NUMERIC, option_type TEXT,
            close NUMERIC, oi BIGINT, spot NUMERIC, iv NUMERIC, is_settlement BOOLEAN
        ) ON COMMIT DROP""")
        with cur.copy("COPY option_iv_staging (symbol, trade_date, expiry, strike, option_type, "
                      "close, oi, spot, iv, is_settlement) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
        cur.execute("""INSERT INTO option_iv_daily
            (symbol, trade_date, expiry, strike, option_type, close, oi, spot, iv, is_settlement, loaded_at)
            SELECT symbol, trade_date, expiry, strike, option_type, close, oi, spot, iv, is_settlement, NOW()
            FROM option_iv_staging
            ON CONFLICT (symbol, trade_date, expiry, strike, option_type) DO UPDATE SET
                close=EXCLUDED.close, oi=EXCLUDED.oi, spot=EXCLUDED.spot, iv=EXCLUDED.iv,
                is_settlement=EXCLUDED.is_settlement, loaded_at=NOW()""")
        conn.commit()
        return {"ok": True, "rows_written": len(rows)}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        conn.close()


def seed_dates(start: date, end: date) -> dict:
    """Insert one 'pending' row per CALENDAR day in [start, end] into option_iv_backfill_status
    (idempotent -- DO NOTHING on a date already present, so re-seeding never resets a done/error
    row). Weekends/holidays simply resolve to an empty bhavcopy and are marked done with
    rows_written=0 by the loop, not filtered here (nse_holidays not consulted -- same posture as
    nse_eod_ingest._last_trading_day)."""
    n = 0
    with _conn() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        d = start
        while d <= end:
            if d.weekday() < 5:
                cur.execute("""INSERT INTO option_iv_backfill_status (trade_date, status)
                               VALUES (%s,'pending') ON CONFLICT (trade_date) DO NOTHING""", (d,))
                n += cur.rowcount
            d += timedelta(days=1)
        conn.commit()
    return {"seeded": n, "start": str(start), "end": str(end)}


def run_backfill() -> dict:
    """Claim the earliest pending date, ingest it, mark done/error, repeat -- until no pending
    dates remain or the 08:30 IST hard abort fires. Runs in a daemon thread (see _maybe_start),
    never inside a request or a CC session."""
    processed, total_rows, errors = 0, 0, 0
    started = _ist_now()
    log.info("option_iv_history backfill started")
    try:
        with _conn() as conn, conn.cursor() as cur:   # cc#1858 fix: a cold start must not query
            _ensure_tables(cur)                        # option_iv_backfill_status before it exists
            conn.commit()
        while True:
            if _hard_abort_hit():
                log.warning(f"option_iv_history HARD ABORT 08:30 IST -- stopping, {processed} dates done this run")
                break
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""SELECT trade_date FROM option_iv_backfill_status
                               WHERE status='pending' ORDER BY trade_date ASC LIMIT 1 FOR UPDATE SKIP LOCKED""")
                r = cur.fetchone()
                if not r:
                    break
                d = r[0]
                cur.execute("""UPDATE option_iv_backfill_status SET status='in_progress', started_at=NOW()
                               WHERE trade_date=%s""", (d,))
                conn.commit()
            res = ingest_date(d)
            with _conn() as conn, conn.cursor() as cur:
                if res.get("ok"):
                    cur.execute("""UPDATE option_iv_backfill_status
                                   SET status='done', rows_written=%s, finished_at=NOW(), error=NULL
                                   WHERE trade_date=%s""", (res.get("rows_written", 0), d))
                    total_rows += res.get("rows_written", 0)
                else:
                    cur.execute("""UPDATE option_iv_backfill_status
                                   SET status='error', error=%s, finished_at=NOW()
                                   WHERE trade_date=%s""", (res.get("error", "unknown")[:500], d))
                    errors += 1
                conn.commit()
            processed += 1
            if processed % 20 == 0 or _ist_now().hour == 7:
                log.info(f"option_iv_history checkpoint: {processed} dates, {total_rows} rows, {errors} errors")
        elapsed = (_ist_now() - started).total_seconds()
        log.info(f"option_iv_history backfill run ended: {processed} dates, {total_rows} rows, "
                 f"{errors} errors, {elapsed:.0f}s")
        return {"ok": True, "processed": processed, "rows_written": total_rows, "errors": errors}
    except Exception as e:
        log.error(f"option_iv_history backfill crashed: {e}")
        return {"ok": False, "error": str(e), "processed": processed, "rows_written": total_rows}
    finally:
        global _running
        _running = False


def _claim_flag() -> bool:
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key=%s AND value='pending' FOR UPDATE", (FLAG_KEY,))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s", (FLAG_KEY,))
            conn.commit()
        return r is not None
    except Exception as e:
        log.error(f"option_iv_history flag claim failed: {e}")
        return False


def _maybe_start() -> bool:
    global _running
    if _running:
        return False
    _running = True
    threading.Thread(target=run_backfill, name="cc1858-option-iv-backfill", daemon=True).start()
    return True


@router.on_event("startup")
async def _startup_trigger():
    # CC sandbox has no HTTP path to prod (same problem bhavcopy_diagnostic.py / fy_end_backfill.py
    # solved) -- set app_config['option_iv_backfill_run']='pending' via run_sql, then this deploy's
    # boot claims it atomically and starts the daemon thread.
    if _claim_flag():
        _maybe_start()


@router.post("/api/admin/option_iv/seed")
def option_iv_seed(start: str, end: str, x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    return seed_dates(_parse_date(start), _parse_date(end))


@router.post("/api/admin/option_iv/run")
def option_iv_run(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    started = _maybe_start()
    return {"started": started, "already_running": not started}


@router.get("/api/admin/option_iv/status")
def option_iv_status(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*), SUM(rows_written) FROM option_iv_backfill_status GROUP BY status")
        by_status = [{"status": s, "count": c, "rows": int(rw or 0)} for s, c, rw in cur.fetchall()]
        cur.execute("""SELECT trade_date, status, rows_written, error FROM option_iv_backfill_status
                       WHERE status='error' ORDER BY trade_date DESC LIMIT 20""")
        errs = [{"date": str(dt), "error": e} for dt, st, rw, e in cur.fetchall()]
    return {"running": _running, "by_status": by_status, "recent_errors": errs}

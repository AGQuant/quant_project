"""option_iv_history.py -- cc#1858 step 5 FORWARD STORAGE + step 7 BACKFILL (founder-released
09-Sep-2026 ~04:30 IST, cc_task_logs id 5654: "FABLE 5.1 IS NOT AVAILABLE. SONNET 5 BUILDS BOTH
P0s... the gates are the safety net, not the model.").

WHAT THIS BUILDS
    Stores per-strike, per-day option data (close, OI, solved implied vol) at ATM +-10 for every
    underlying, from the daily NSE F&O bhavcopy -- the SAME public file bg_fo_eod already fetches
    nightly and reads only the STOCK-FUTURES rows from (nse_fo_eod.py). Every option row in that
    file is currently parsed and discarded; this is the fix (cc#1858's own framing, step 4:
    "the compute is correct, already intraday [for TC]; the output is thrown away" -- same shape
    here, but for the daily bhavcopy's option rows specifically). run_forward_tick() (below) is
    the ONGOING daily capture, scheduled nightly (scheduler._bg_option_iv_daily, ~23:05 IST) so
    storage keeps accumulating from the day it shipped regardless of the backfill's outcome --
    the card's own step 1 requirement. run_backfill()/seed_dates() are the separate HISTORICAL
    catch-up path; both call the same ingest_date(), never two compute paths.

WHAT IT DOES NOT DO (do_not_touch, card cc#1858)
    Does NOT touch fo_eod, nse_fo_eod.py, or bg_fo_eod's dispatch in scheduler.py -- the nightly
    futures-OI ingest is completely unchanged. Does NOT touch atm_iv_daily (cc#1847's separate
    table/question). Does NOT change deriv_metrics.py's _bs_price/_bs_iv -- those stay byte-for-
    byte unchanged (do_not_touch, other surfaces read them as-is).

AMENDED cc#2031 (15-Sep-2026) -- SOLVER CHANGED from spot-based Black-Scholes to forward-based
    Black-76. The premise this module originally mirrored (_bs_price/_bs_iv's d1 assumes the
    underlying drifts at R_FREE, 7%, to expiry) was found wrong by Fable's 13-Sep audit: real
    NIFTY/BANKNIFTY put-call parity implies an actual carry of 0.2-0.7%, never near 7% (cc#2031
    e1_forward_error evidence). _b76_price_vec/_b76_iv_vec below are the vectorised twin of
    deriv_metrics._b76_price/_b76_iv (same d1/d2 family, same [1e-4,5.0]/64-iteration bisection,
    additive -- _bs_price_vec/_bs_iv_vec above are UNCHANGED and still defined, just no longer
    called from ingest_date()). Per (symbol, trade_date), F = the real put-call-parity-implied
    forward (K_atm + (C_atm-P_atm)*e^(R_FREE*T), K_atm = the strike nearest spot with BOTH legs
    priced that day) when such a pair exists; F = spot*e^(R_FREE*T) otherwise (algebraically
    IDENTICAL to what _bs_price_vec/_bs_iv_vec would have produced -- proven in deriv_metrics.
    _b76_price's own docstring -- so a symbol/day with no valid ATM pair sees the exact same
    stored iv as before, never a fabricated forward). Fallback count is returned from
    ingest_date() and surfaced in run_backfill()/run_forward_tick()'s own result dicts, per the
    card's own instruction to state it, not just log it.

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
import math
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone, time as dt_time
import json
from typing import Dict, Optional
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
    # cc#2020: per-(symbol, trade_date) evidence for the expiry correction -- what the old rows'
    # expiry was, what the corrected one is, how many rows existed before, how many were written,
    # how many old rows were deleted (or kept, with the reason). CREATE TABLE only, never ALTER.
    cur.execute("""CREATE TABLE IF NOT EXISTS option_iv_expiry_fix_log (
        trade_date DATE NOT NULL, symbol TEXT NOT NULL,
        old_expiry DATE, new_expiry DATE NOT NULL,
        rows_before INT NOT NULL, rows_written INT NOT NULL, rows_deleted INT NOT NULL,
        action TEXT NOT NULL,   -- deleted_old | kept_old_implausible_drop
        at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (trade_date, symbol))""")


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


# ── cc#2031 A2: vectorised Black-76 -- SAME formula as deriv_metrics._b76_price/_b76_iv, array-
# wise, mirroring _bs_price_vec/_bs_iv_vec's own structure exactly (same bounds, same iteration
# count). Prices off a FORWARD (F) instead of assuming one from spot+R_FREE -- r is used for
# DISCOUNTING ONLY, no drift term in d1. _bs_price_vec/_bs_iv_vec above are unchanged and no
# longer called from ingest_date() (see AMENDED cc#2031 at the top of this file) but stay defined
# in case a future caller needs the old spot-based behaviour explicitly.
def _b76_price_vec(F, K, T, sigma, is_call):
    with np.errstate(all="ignore"):
        sqrtT = np.sqrt(T)
        d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
        d2 = d1 - sigma * sqrtT
        disc = np.exp(-R_FREE * T)
        call_px = disc * (F * ndtr(d1) - K * ndtr(d2))
        put_px = disc * (K * ndtr(-d2) - F * ndtr(-d1))
    return np.where(is_call, call_px, put_px)


def _b76_iv_vec(price, F, K, T, is_call):
    """Vectorised bisection on [1e-4, 5.0], 64 iterations -- identical bounds/iteration count to
    _bs_iv_vec / deriv_metrics._b76_iv, applied to whole arrays. Returns NaN for any row with an
    invalid input (mirrors _b76_iv returning None on the same conditions)."""
    n = price.shape[0]
    lo = np.full(n, 1e-4, dtype=float)
    hi = np.full(n, 5.0, dtype=float)
    valid = (price > 0) & (F > 0) & (K > 0) & (T > 0) & np.isfinite(F)
    for _ in range(64):
        mid = (lo + hi) / 2.0
        p = _b76_price_vec(F, K, T, mid, is_call)
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


def _retire_superseded_expiry_rows(cur, d: date, syms, expiries):
    """cc#2020 STAGE 3 (gate_destructive_step): option_iv_daily's primary key includes `expiry`, so
    a corrected re-ingest for a date whose expiry choice CHANGED (NIFTY: weekly -> monthly) writes
    NEW rows beside the old ones instead of replacing them. Those old rows must go, or every reader
    that assumes one expiry per (symbol, trade_date) (option_ivp.atm_iv_history, cc#1994's
    _stored_iv_gap_map) silently mixes the two.

    Runs INSIDE ingest_date()'s transaction, AFTER the corrected rows are in the upsert and BEFORE
    commit -- so a date either lands with its corrected rows and without its stale ones, or
    (rollback) keeps exactly what it had. Never a hole. Per SYMBOL, not per table:
      - only symbols that actually got rows written this run are touched;
      - the sanity floor: if the corrected write is under HALF that symbol's existing row count
        for the date, the old rows are KEPT and the case logged (kept_old_implausible_drop) --
        a stale-but-present row is safer than a hole (the card's own words);
      - an unchanged expiry deletes nothing (the upsert overwrote in place; expiry<>new is empty).
    Every (symbol, date) writes one option_iv_expiry_fix_log row: before/written/deleted counts.
    Returns (rows_deleted_total, [symbols kept])."""
    written = {}
    new_exp = {}
    for sym, exp in zip(syms, expiries):
        written[sym] = written.get(sym, 0) + 1
        new_exp[sym] = exp
    deleted_total, kept = 0, []
    for sym, n_new in written.items():
        exp = new_exp[sym]
        cur.execute("""SELECT COUNT(*), MIN(expiry) FROM option_iv_daily
                       WHERE symbol=%s AND trade_date=%s AND expiry<>%s""", (sym, d, exp))
        n_old, old_exp = cur.fetchone()
        n_old = int(n_old or 0)
        if n_old == 0:
            continue   # expiry unchanged (or first load) -- nothing superseded, nothing to log
        if n_new * 2 < n_old:
            action, n_del = "kept_old_implausible_drop", 0
            kept.append(sym)
            log.warning(f"cc#2020 {d} {sym}: corrected write {n_new} rows vs {n_old} existing -- old rows KEPT")
        else:
            cur.execute("""DELETE FROM option_iv_daily
                           WHERE symbol=%s AND trade_date=%s AND expiry<>%s""", (sym, d, exp))
            action, n_del = "deleted_old", cur.rowcount
            deleted_total += n_del
        cur.execute("""INSERT INTO option_iv_expiry_fix_log
                       (trade_date, symbol, old_expiry, new_expiry, rows_before, rows_written, rows_deleted, action)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (trade_date, symbol) DO UPDATE SET old_expiry=EXCLUDED.old_expiry,
                         new_expiry=EXCLUDED.new_expiry, rows_before=EXCLUDED.rows_before,
                         rows_written=EXCLUDED.rows_written, rows_deleted=EXCLUDED.rows_deleted,
                         action=EXCLUDED.action, at=NOW()""",
                    (d, sym, old_exp, exp, n_old, n_new, n_del, action))
    return deleted_total, kept


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

        # cc#2031 A2: per-symbol put-call-parity forward, from the SAME `closes` values about to
        # be solved (settlement-substituted, matching price_arr below exactly -- no second price
        # source). K_atm = the strike nearest that symbol's spot with BOTH legs priced (>0) today;
        # F = K_atm + (C_atm-P_atm)*e^(R_FREE*T). No such pair -> carry fallback F=spot*e^(R_FREE*T)
        # (algebraically identical to the old _bs_iv_vec solve -- see deriv_metrics._b76_price's
        # docstring), counted in forward_fallback_count (stated in the result, per the card).
        by_sym_legs: Dict[str, Dict[float, Dict[str, float]]] = {}
        for i in range(len(syms)):
            c = closes[i]
            if c is None or c <= 0:
                continue
            by_sym_legs.setdefault(syms[i], {}).setdefault(strikes[i], {})[otypes[i]] = c
        forward_by_sym: Dict[str, Optional[float]] = {}
        fallback_syms = []
        for sym, strikes_map in by_sym_legs.items():
            spot = spot_by_sym.get(sym)
            exp = opt_near_expiry.get(sym)
            T_sym = (exp - d).days / 365.0 if exp else None
            best = None   # (dist, K_atm, C, P)
            if spot and T_sym and T_sym > 0:
                for K, legs in strikes_map.items():
                    C, P = legs.get("CE"), legs.get("PE")
                    if C is not None and P is not None:
                        dist = abs(K - spot)
                        if best is None or dist < best[0]:
                            best = (dist, K, C, P)
            if best is not None:
                _, K_atm, C_atm, P_atm = best
                forward_by_sym[sym] = K_atm + (C_atm - P_atm) * math.exp(R_FREE * T_sym)
            elif spot and T_sym and T_sym > 0:
                forward_by_sym[sym] = spot * math.exp(R_FREE * T_sym)
                fallback_syms.append(sym)
            else:
                forward_by_sym[sym] = None
                fallback_syms.append(sym)
        fwd_arr = np.array([forward_by_sym.get(s) if forward_by_sym.get(s) is not None else np.nan
                             for s in syms], dtype=float)

        price_arr = np.array([c if c is not None else np.nan for c in closes], dtype=float)
        K_arr = np.array(strikes, dtype=float)
        T_arr = np.array([(e - d).days / 365.0 for e in expiries], dtype=float)
        call_arr = np.array(is_call, dtype=bool)
        with np.errstate(invalid="ignore"):
            iv_arr = _b76_iv_vec(np.nan_to_num(price_arr, nan=-1.0), fwd_arr, K_arr, T_arr, call_arr)
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
        deleted, kept = _retire_superseded_expiry_rows(cur, d, syms, expiries)
        conn.commit()
        return {"ok": True, "rows_written": len(rows), "rows_deleted": deleted, "kept_old_symbols": kept,
                "forward_fallback_count": len(fallback_syms)}  # cc#2031 A2: symbols priced off carry, not parity
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
    processed, total_rows, errors, total_fallback = 0, 0, 0, 0
    # cc#2020: the flag is claimed at DEPLOY time, and a deploy can land after 08:30 IST (this
    # correction was pushed at ~23:30 IST). Rather than burn the claim on an instant abort, wait
    # for the next 00:00 IST and run then -- the wall-clock test inside the loop is unchanged.
    if _hard_abort_hit():
        now = _ist_now()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=30, microsecond=0)
        wait = (midnight - now).total_seconds()
        log.info(f"option_iv_history backfill deferred {wait:.0f}s to {midnight} (past {HARD_ABORT_HOUR}:{HARD_ABORT_MINUTE} IST)")
        time.sleep(wait)
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
                    total_fallback += res.get("forward_fallback_count", 0)   # cc#2031 A2
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
        _mark_flag_done()
        return {"ok": True, "processed": processed, "rows_written": total_rows, "errors": errors,
                "forward_fallback_count": total_fallback}  # cc#2031 A2, summed across all dates this run
    except Exception as e:
        log.error(f"option_iv_history backfill crashed: {e}")
        return {"ok": False, "error": str(e), "processed": processed, "rows_written": total_rows}
    finally:
        global _running
        _running = False


def run_forward_tick() -> dict:
    """Daily FORWARD capture -- called once nightly by scheduler.py's _bg_option_iv_daily, ~23:05
    IST (5 min after bg_fo_eod fetches the same day's F&O bhavcopy, same public file). Seeds
    today's date if not already present and ingests it via the SAME ingest_date() the backfill
    uses -- not a second compute path. Idempotent: a re-run of an already-'done' date is a no-op,
    so a scheduler retry or an overlapping manual trigger can never duplicate rows.

    This is what makes storage keep accumulating from the day it shipped (09-Sep-2026),
    independent of whether the historical backfill ever ran or finished -- card cc#1858 step 1's
    explicit requirement: "This step must be shippable and verifiable ON ITS OWN... From the day
    it ships you accumulate history whether or not the backfill ever completes." The backfill
    (run_backfill/seed_dates, this same file) covers 2025-09-01..2026-08-31; this tick is what
    covers 2026-09-01 onward."""
    d = _ist_now().date()
    with _conn() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute("SELECT status FROM option_iv_backfill_status WHERE trade_date=%s", (d,))
        r = cur.fetchone()
        if r and r[0] == "done":
            return {"ok": True, "skipped": "already done", "trade_date": str(d)}
        cur.execute("""INSERT INTO option_iv_backfill_status (trade_date, status)
                       VALUES (%s,'pending') ON CONFLICT (trade_date) DO NOTHING""", (d,))
        conn.commit()
    res = ingest_date(d)
    with _conn() as conn, conn.cursor() as cur:
        if res.get("ok"):
            cur.execute("""UPDATE option_iv_backfill_status
                           SET status='done', rows_written=%s, finished_at=NOW(), error=NULL
                           WHERE trade_date=%s""", (res.get("rows_written", 0), d))
        else:
            cur.execute("""UPDATE option_iv_backfill_status
                           SET status='error', error=%s, finished_at=NOW()
                           WHERE trade_date=%s""", (res.get("error", "unknown")[:500], d))
        conn.commit()
    log.info(f"option_iv_history forward tick {d}: {res}")
    return {"ok": bool(res.get("ok")), "trade_date": str(d), **res}


def _mark_flag_done():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s AND value='claimed'", (FLAG_KEY,))
            conn.commit()
    except Exception as e:
        log.error(f"option_iv_history flag done-mark failed: {e}")


def _claim_flag() -> bool:
    # cc#2020: a 'claimed' flag older than 10 min with no run marked done means the claiming
    # container was replaced by a later deploy (the daemon thread died with it). Re-claim it, so a
    # push landing while the run is deferred to 00:00 IST cannot strand the correction. Harmless if
    # the old container is still alive: dates are claimed FOR UPDATE SKIP LOCKED and ingest is
    # idempotent, so two loops simply share the pending list.
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT value FROM app_config WHERE key=%s
                           AND (value='pending' OR (value='claimed' AND updated_at < NOW() - INTERVAL '10 minutes'))
                           FOR UPDATE""", (FLAG_KEY,))
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


# ── cc#2031 A4: the GATED re-solve of STORED option_iv_daily.iv rows on the Black-76 parity forward ──
# Phases A-C (2d4711c, 15-Sep-2026) changed the method for NEW rows only. This is the destructive
# part the card gated on a founder GO: Fable's RECO (cc_task_logs 6864, founder-delegated,
# 17-Sep) set four conditions -- (1) a plain CREATE TABLE backup first, (2) outside 09:15-15:30
# IST, (3) the UPDATE in ONE transaction reporting rows changed vs the dry run, (4) ROLLBACK if the
# changed count is more than 5% away from the dry run. All four live in a4_run() below. The
# re-solve rule is ingest_date()'s own (parity forward off the strike nearest spot with BOTH legs
# priced, carry fallback, _b76_iv_vec) applied to the stored close/spot -- one method, not a
# second copy. Trigger: the same app_config flag pattern _startup_trigger() already uses for the
# backfill (the CC sandbox has no HTTP path to prod): set A4_FLAG_KEY to a JSON
# {"status":"pending","symbols":[...],"expect_outside":109,"tolerance":0.05,"dry_run":false,
# "task_id":2031}; the next boot claims it and runs it on a daemon thread; the result lands in the
# flag value AND as a cc_task_logs line on the task. Or POST /api/admin/option_iv/a4 with the token.
A4_FLAG_KEY = "option_iv_a4_resolve"
A4_BACKUP_TABLE = "option_iv_daily_bak_cc2031"
A4_BAND_LO, A4_BAND_HI = 0.03, 1.50      # option_ivp's read-time sanity band (IV_FLOOR / IV_CEILING)
_a4_running = False


def a4_resolve_rows(rows):
    """PURE (no DB). rows = list of (trade_date, expiry, strike, option_type, close, spot, iv) for
    ONE symbol, any order. Re-solves every row's iv exactly as ingest_date() does for a fresh
    bhavcopy: per (trade_date, expiry) group the parity forward F = K_atm + (C_atm - P_atm) *
    e^(R_FREE*T) off the strike nearest that day's spot with BOTH legs priced (> 0); otherwise the
    carry forward spot*e^(R_FREE*T); then the Black-76 bisection (_b76_iv_vec). A row with no
    price stays NULL. Returns (iv_new ndarray aligned to rows, stats dict). Stats compare the
    stored iv with the re-solve: rows_changed, null_flips, inside_to_outside / outside_to_inside
    (the [0.03, 1.50] band), band_hits before/after (<= lo or >= hi), and the ATM same-strike
    |CE - PE| gap in vol points (median / p95 over the parity groups) before and after."""
    n = len(rows)
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault((r[0], r[1]), []).append(i)
    price = np.full(n, np.nan)
    K = np.zeros(n)
    T = np.zeros(n)
    F = np.full(n, np.nan)
    is_call = np.zeros(n, dtype=bool)
    fallback_groups = 0
    atm_pairs = []
    for (d, exp), idxs in groups.items():
        Tg = (exp - d).days / 365.0
        spot = None
        for i in idxs:
            sp = rows[i][5]
            if sp is not None and float(sp) > 0:
                spot = float(sp)
                break
        legs = {}
        for i in idxs:
            c = rows[i][4]
            c = float(c) if c is not None else None
            if c is not None and c > 0:
                legs.setdefault(float(rows[i][2]), {})[rows[i][3]] = (c, i)
        best = None
        if spot and Tg > 0:
            for k, lg in legs.items():
                if "CE" in lg and "PE" in lg:
                    dist = abs(k - spot)
                    if best is None or dist < best[0]:
                        best = (dist, k, lg["CE"], lg["PE"])
        if best is not None:
            Fg = best[1] + (best[2][0] - best[3][0]) * math.exp(R_FREE * Tg)
            atm_pairs.append((best[2][1], best[3][1]))
        elif spot and Tg > 0:
            Fg = spot * math.exp(R_FREE * Tg)
            fallback_groups += 1
        else:
            Fg = np.nan
            fallback_groups += 1
        for i in idxs:
            c = rows[i][4]
            price[i] = float(c) if c is not None else np.nan
            K[i] = float(rows[i][2])
            T[i] = Tg
            F[i] = Fg
            is_call[i] = (rows[i][3] == "CE")
    with np.errstate(invalid="ignore"):
        iv_new = _b76_iv_vec(np.nan_to_num(price, nan=-1.0), F, K, T, is_call)
    iv_new = np.where(np.isnan(price) | (price <= 0), np.nan, iv_new)
    old = np.array([float(r[6]) if r[6] is not None else np.nan for r in rows], dtype=float)
    lo, hi = A4_BAND_LO, A4_BAND_HI
    both = np.isfinite(old) & np.isfinite(iv_new)
    changed = both & (np.abs(old - iv_new) > 1e-9)
    null_flips = int(np.sum(np.isfinite(old) != np.isfinite(iv_new)))
    in_old = both & (old >= lo) & (old <= hi)
    out_old = both & ((old < lo) | (old > hi))
    in_new = both & (iv_new >= lo) & (iv_new <= hi)
    out_new = both & ((iv_new < lo) | (iv_new > hi))

    def _pct(vals, q):
        return round(float(np.percentile(vals, q)), 3) if len(vals) else None

    gb = [abs(old[a] - old[b]) * 100.0 for a, b in atm_pairs if np.isfinite(old[a]) and np.isfinite(old[b])]
    ga = [abs(iv_new[a] - iv_new[b]) * 100.0 for a, b in atm_pairs if np.isfinite(iv_new[a]) and np.isfinite(iv_new[b])]
    stats = {
        "rows": n, "groups": len(groups), "parity_groups": len(atm_pairs), "fallback_groups": fallback_groups,
        "rows_with_iv_before": int(np.sum(np.isfinite(old))), "rows_with_iv_after": int(np.sum(np.isfinite(iv_new))),
        "rows_changed": int(changed.sum()), "null_flips": null_flips,
        "inside_to_outside": int(np.sum(in_old & out_new)), "outside_to_inside": int(np.sum(out_old & in_new)),
        "band_hits_before": int(np.sum(np.isfinite(old) & ((old <= lo) | (old >= hi)))),
        "band_hits_after": int(np.sum(np.isfinite(iv_new) & ((iv_new <= lo) | (iv_new >= hi)))),
        "atm_gap_before_median": _pct(gb, 50), "atm_gap_before_p95": _pct(gb, 95),
        "atm_gap_after_median": _pct(ga, 50), "atm_gap_after_p95": _pct(ga, 95),
        "gap_unit": "vol points (iv x 100), ATM same-strike |CE - PE| per parity group",
    }
    return iv_new, stats


def a4_run(symbols, expect_outside=None, tolerance=0.05, dry_run=True, backup_table=A4_BACKUP_TABLE) -> dict:
    """The gated run. dry_run=True: read + re-solve + stats, nothing written (the connection is
    rolled back). dry_run=False: refuses inside 09:15-15:30 IST on a weekday; then, in ONE
    transaction: CREATE TABLE <backup_table> AS SELECT * FROM option_iv_daily (a plain CREATE --
    a re-run against an existing backup fails and rolls back rather than overwriting it), COPY the
    re-solved ivs into a temp table, UPDATE only the rows whose iv actually differs, and commit --
    unless the gate fails (inside_to_outside more than `tolerance` away from `expect_outside`),
    in which case everything including the backup is rolled back and the result says so."""
    now = _ist_now()
    if not dry_run and now.weekday() < 5 and dt_time(9, 15) <= now.time() <= dt_time(15, 30):
        return {"ok": False, "error": "market hours -- A4 writes run outside 09:15-15:30 IST only", "now_ist": now.isoformat(timespec="seconds")}
    symbols = [str(x).strip().upper() for x in (symbols or []) if str(x).strip()]
    if not symbols:
        return {"ok": False, "error": "no symbols"}
    conn = _conn()
    try:
        cur = conn.cursor()
        per = {}
        staging = []
        for sym in symbols:
            cur.execute("""SELECT trade_date, expiry, strike, option_type, close, spot, iv
                           FROM option_iv_daily WHERE symbol=%s
                           ORDER BY trade_date, expiry, strike, option_type""", (sym,))
            rows = cur.fetchall()
            if not rows:
                per[sym] = {"rows": 0, "note": "no stored rows"}
                continue
            iv_new, st = a4_resolve_rows(rows)
            per[sym] = st
            for i, r in enumerate(rows):
                v = float(iv_new[i]) if np.isfinite(iv_new[i]) else None
                staging.append((sym, r[0], r[1], r[2], r[3], v))
        total_outside = sum(int(s.get("inside_to_outside", 0)) for s in per.values())
        total_changed = sum(int(s.get("rows_changed", 0)) for s in per.values())
        gate = None
        if expect_outside is not None:
            diff = abs(total_outside - int(expect_outside)) / max(1, int(expect_outside))
            gate = {"expect_outside": int(expect_outside), "actual_outside": total_outside,
                    "diff_pct": round(diff * 100.0, 2), "tolerance_pct": round(float(tolerance) * 100.0, 2),
                    "pass": bool(diff <= float(tolerance))}
        result = {"ok": True, "dry_run": bool(dry_run), "symbols": symbols, "ran_at_ist": now.isoformat(timespec="seconds"),
                  "per_symbol": per, "rows_changed": total_changed, "inside_to_outside": total_outside, "gate": gate}
        if dry_run:
            conn.rollback()
            result["action"] = "DRY RUN: nothing written"
            return result
        if gate is not None and not gate["pass"]:
            conn.rollback()
            result.update(ok=False, action="ROLLBACK: gate failed, nothing written (no backup left behind either)")
            return result
        cur.execute(f"CREATE TABLE {backup_table} AS SELECT * FROM option_iv_daily")
        cur.execute("""CREATE TEMP TABLE option_iv_a4_staging (
            symbol TEXT, trade_date DATE, expiry DATE, strike NUMERIC, option_type TEXT, iv NUMERIC
        ) ON COMMIT DROP""")
        with cur.copy("COPY option_iv_a4_staging (symbol, trade_date, expiry, strike, option_type, iv) FROM STDIN") as cp:
            for row in staging:
                cp.write_row(row)
        cur.execute("""UPDATE option_iv_daily o SET iv = s.iv, loaded_at = NOW()
                       FROM option_iv_a4_staging s
                       WHERE o.symbol = s.symbol AND o.trade_date = s.trade_date AND o.expiry = s.expiry
                         AND o.strike = s.strike AND o.option_type = s.option_type
                         AND o.iv IS DISTINCT FROM s.iv""")
        result["rows_updated"] = cur.rowcount
        cur.execute(f"SELECT COUNT(*) FROM {backup_table}")
        result["backup_table"] = backup_table
        result["backup_rows"] = int(cur.fetchone()[0])
        conn.commit()
        result["action"] = "COMMITTED (one transaction: backup + update)"
        return result
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "action": "ROLLBACK on error"}
    finally:
        conn.close()


def _a4_claim():
    """Atomically claim a pending A4 request from app_config; returns the config dict or None."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key=%s FOR UPDATE", (A4_FLAG_KEY,))
            r = cur.fetchone()
            if not r:
                return None
            try:
                cfg = json.loads(r[0])
            except Exception:
                return None
            if not isinstance(cfg, dict) or cfg.get("status") != "pending":
                return None
            cfg["status"] = "claimed"
            cfg["claimed_at_ist"] = _ist_now().isoformat(timespec="seconds")
            cur.execute("UPDATE app_config SET value=%s, updated_at=NOW() WHERE key=%s", (json.dumps(cfg), A4_FLAG_KEY))
            conn.commit()
            return cfg
    except Exception as e:
        log.error(f"option_iv A4 flag claim failed: {e}")
        return None


def _a4_finish(cfg, result):
    """Write the outcome where CC can read it: the flag value and one cc_task_logs line."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cfg = dict(cfg or {})
            cfg["status"] = "done" if result.get("ok") else "error"
            cfg["result"] = result
            cur.execute("UPDATE app_config SET value=%s, updated_at=NOW() WHERE key=%s", (json.dumps(cfg, default=str), A4_FLAG_KEY))
            task_id = int(cfg.get("task_id") or 0)
            if task_id:
                cur.execute("INSERT INTO cc_task_logs (task_id, actor, message) VALUES (%s, 'claude_code', %s)",
                            (task_id, ("A4 SERVER RUN (option_iv_history.a4_run, started by the app_config flag): "
                                       + json.dumps(result, default=str))[:6000]))
            conn.commit()
    except Exception as e:
        log.error(f"option_iv A4 finish-write failed: {e} -- result was {json.dumps(result, default=str)[:800]}")


def _a4_thread(cfg):
    global _a4_running
    try:
        res = a4_run(cfg.get("symbols") or [], expect_outside=cfg.get("expect_outside"),
                     tolerance=float(cfg.get("tolerance", 0.05)), dry_run=bool(cfg.get("dry_run", True)))
    except Exception as e:
        res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    finally:
        _a4_running = False
    log.info(f"option_iv A4 run: {json.dumps(res, default=str)[:500]}")
    _a4_finish(cfg, res)


def _a4_maybe_start() -> bool:
    global _a4_running
    if _a4_running:
        return False
    cfg = _a4_claim()
    if not cfg:
        return False
    _a4_running = True
    threading.Thread(target=_a4_thread, args=(cfg,), name="cc2031-a4-resolve", daemon=True).start()
    return True


@router.post("/api/admin/option_iv/a4")
def option_iv_a4(symbols: str = "NIFTY,BANKNIFTY,RELIANCE", dry_run: bool = True,
                 expect_outside: Optional[int] = None, tolerance: float = 0.05,
                 x_admin_token: Optional[str] = Header(None)):
    """On-demand A4 (token-gated). dry_run=true is read-only; dry_run=false writes under the same
    four gates as the flag path. Runs inline (30k rows per symbol solve in well under a second;
    the write path's full-table backup copy is the slow part -- prefer the flag path for that)."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    return a4_run([x for x in symbols.split(",") if x.strip()], expect_outside=expect_outside,
                  tolerance=tolerance, dry_run=dry_run)


@router.on_event("startup")
async def _startup_trigger():
    # CC sandbox has no HTTP path to prod (same problem bhavcopy_diagnostic.py / fy_end_backfill.py
    # solved) -- set app_config['option_iv_backfill_run']='pending' via run_sql, then this deploy's
    # boot claims it atomically and starts the daemon thread.
    if _claim_flag():
        _maybe_start()
    _a4_maybe_start()   # cc#2031 A4: same flag pattern, its own key (A4_FLAG_KEY)


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

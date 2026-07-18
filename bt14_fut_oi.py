"""cc#538: ORB basis/OI RESEARCH backfill — probe-gated, exploratory, DROPPABLE.

Captures stock-futures 5-min OHLC + Open Interest history for the top-200 turnover
names around the ORB backtest window into the scratch table `bt14_fut_oi`, and derives
basis = fut_close - spot_close (spot from bt14_bars). Claude-web later JOINs this onto
bt14_orb_trades (run_label='I_brk025', 375 trades, 19-Jun..10-Jul) to scan whether
basis-sign / oi_chg separate ORB winners from losers, then DROPS the table.

Touches NOTHING live: no futures_basis / option_chain writes. ORB logic unchanged. This
is purely exploratory data to interrogate — ORB fires on spot price action alone.

HARD GATE (Step 0): Fyers History must serve OI (oi_flag=1 => 7th candle column) for an
EXPIRED stock-futures contract. We probe a recent JULY contract first (most likely to
serve), then confirm the EXPIRED JUNE contract. verdict:
  both      -> full backfill; scan on Window-A (June) trades has real basis/OI. Best case.
  july_only -> report; July bars still land, but the June pattern-scan is limited/blocked.
  none      -> abort; basis/OI stay live-forward-only (a valid, useful finding).

Fetch mirrors pcr_backfill._fetch_option_history (oi_flag=1) but cont_flag=0 (explicit
monthly contract — no continuous-series splice across the expiry rollover). Futures
symbols come from fyers_backfill.fyers_fut_symbol (reuse the feed-layer builder; no
hardcoded strings). Per-date active near-month contract respected: June contract for
bars <= June expiry, July contract after (last-Tuesday expiry rule).

Arming (app_config.bt14_fut_oi_run, polled by scheduler._bg_bt14_fut_oi, off-market only):
  'probe'    -> run Step-0 probe, log BT14_FUT_OI_PROBE, set flag 'probe_done'
  'backfill' -> run one resumable chunk (cursor bt14_fut_oi_cursor), stays 'backfill'
                until complete then 'done'; gated on the last probe verdict (!= none)
10 GB working guard: pause if pg_database_size would exceed ~9.5 GB (visibility log always).
"""
import os
import json
import time
from datetime import datetime

import requests
import psycopg2
from pytz import timezone

import pcr_backfill  # HISTORY_URL, _hdr, _load_token, _last_tuesday

_DB = os.environ.get("DATABASE_URL")
_IST = timezone("Asia/Kolkata")
_WORK_LIMIT_BYTES = int(9.5 * 1024 ** 3)  # 10 GB working guard (pause > 9.5 GB)
_PACE_S = 5.0                             # post-market pacing, per request
_TOP_N = 200
_MKT_OPEN, _MKT_CLOSE = (9, 15), (15, 30)

# Contract codes ('YYMMM') and per-contract fetch windows. June contract carries the
# near-month bars up to June expiry; July contract carries post-rollover bars to the
# live-feed start (~13-Jul). cont_flag=0 => each is fetched as its own explicit contract.
_JUN_CODE, _JUL_CODE = "26JUN", "26JUL"
_CONTRACTS = [
    (_JUN_CODE, "2026-05-01", "2026-06-30"),   # June near-month (<= June expiry 30-Jun)
    (_JUL_CODE, "2026-07-01", "2026-07-14"),   # July near-month (post-June-expiry -> live start)
]


def _conn():
    return psycopg2.connect(_DB)


def _fut_symbol(stock, code):
    """Reuse the feed-layer futures-symbol builder (no hardcoded strings)."""
    from fyers_backfill import fyers_fut_symbol
    return fyers_fut_symbol(stock, code)


def _oplog(cur, title, details, category="bt14_fut_oi"):
    try:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)",
                    (category, title, json.dumps(details, default=str)))
    except Exception:
        pass


def _ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bt14_fut_oi (
                symbol     text      NOT NULL,
                ts         timestamp NOT NULL,   -- naive IST, matches bt14_bars.ts
                contract   text,                 -- '26JUN' / '26JUL'
                fut_open   double precision,
                fut_high   double precision,
                fut_low    double precision,
                fut_close  double precision,
                oi         bigint,
                oi_chg     bigint,               -- oi - prev-ts oi (per symbol)
                basis      double precision,     -- fut_close - spot_close (NULL if no spot bar)
                PRIMARY KEY (symbol, ts)
            )""")
        conn.commit()


def _db_size_bytes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        return int(cur.fetchone()[0])


def _fetch_fut_history(token, sym, start, end):
    """5-min futures history WITH OI. Mirrors pcr_backfill._fetch_option_history but
    cont_flag=0 (explicit monthly contract — no continuous-series splice at rollover).
    Returns (candles, raw). Each candle: [ts,o,h,l,c,v,oi] when oi_flag served."""
    r = requests.get(pcr_backfill.HISTORY_URL, params={
        "symbol": sym, "resolution": "5", "date_format": "1",
        "range_from": start, "range_to": end,
        "cont_flag": "0", "oi_flag": "1",
    }, headers=pcr_backfill._hdr(token), timeout=20)
    d = r.json()
    if d.get("s") != "ok":
        return None, d
    return d.get("candles", []), d


def _in_mkt(ts):
    t = (ts.hour, ts.minute)
    return _MKT_OPEN <= t <= _MKT_CLOSE


# ---------------------------------------------------------------- Step 0: PROBE

def probe(token, conn):
    """HARD GATE. July (recent) contract first, then expired June, on RELIANCE.
    Returns {july:{...}, june:{...}, verdict}. verdict in {both, july_only, none}."""
    out = {}
    for tag, code, rng in (("july", _JUL_CODE, ("2026-07-06", "2026-07-08")),
                           ("june", _JUN_CODE, ("2026-06-24", "2026-06-26"))):
        sym = _fut_symbol("RELIANCE", code)
        candles, raw = _fetch_fut_history(token, sym, rng[0], rng[1])
        has_c = bool(candles)
        has_oi = bool(has_c and len(candles[0]) >= 7 and candles[0][6] is not None)
        out[tag] = {
            "symbol": sym, "range": list(rng), "api_s": (raw or {}).get("s"),
            "n_candles": len(candles or []),
            "cols": (len(candles[0]) if has_c else 0),
            "oi_present": has_oi,
            "sample_candle": (candles[0] if has_c else None),
            "api_msg": (raw or {}).get("message") or (raw or {}).get("s"),
        }
        time.sleep(_PACE_S)
    july_oi, june_oi = out["july"]["oi_present"], out["june"]["oi_present"]
    out["verdict"] = "both" if (july_oi and june_oi) else ("july_only" if july_oi else "none")
    with conn.cursor() as cur:
        _oplog(cur, "BT14_FUT_OI_PROBE", out)
        conn.commit()
    return out


def run_probe():
    conn = _conn()
    try:
        _ensure_table(conn)
        token = pcr_backfill._load_token(conn)
        return probe(token, conn)
    finally:
        conn.close()


def _last_probe_verdict(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT details->>'verdict' FROM ops_log
                       WHERE category='bt14_fut_oi' AND title='BT14_FUT_OI_PROBE'
                       ORDER BY id DESC LIMIT 1""")
        r = cur.fetchone()
    return r[0] if r else None


# --------------------------------------------------------------- Step 1: BACKFILL

def _top_symbols(conn, n=_TOP_N):
    with conn.cursor() as cur:
        cur.execute("""SELECT symbol FROM bt14_days
                       GROUP BY symbol ORDER BY SUM(turnover) DESC NULLS LAST LIMIT %s""", (n,))
        return [r[0] for r in cur.fetchall()]


def _write_symbol(conn, token, stock):
    """Fetch both contracts for one stock, upsert its rows. Returns rows written."""
    total = 0
    for code, start, end in _CONTRACTS:
        sym = _fut_symbol(stock, code)
        candles, _raw = _fetch_fut_history(token, sym, start, end)
        time.sleep(_PACE_S)
        if not candles:
            continue
        rows = []
        for c in candles:
            if len(c) < 7:
                continue
            ts = datetime.fromtimestamp(c[0], tz=_IST).replace(tzinfo=None)
            if not _in_mkt(ts):
                continue
            oi = int(c[6]) if c[6] is not None else None
            rows.append((stock, ts, code, float(c[1]), float(c[2]),
                         float(c[3]), float(c[4]), oi))
        if not rows:
            continue
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO bt14_fut_oi
                    (symbol, ts, contract, fut_open, fut_high, fut_low, fut_close, oi)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, ts) DO UPDATE SET
                    contract=EXCLUDED.contract, fut_open=EXCLUDED.fut_open,
                    fut_high=EXCLUDED.fut_high, fut_low=EXCLUDED.fut_low,
                    fut_close=EXCLUDED.fut_close, oi=EXCLUDED.oi
            """, rows)
            conn.commit()
        total += len(rows)
    return total


def _finalize(conn):
    """Set-based derivations after the row load: oi_chg (per-symbol LAG) + basis (join
    bt14_bars spot close on symbol+ts). Both cheap, idempotent, re-runnable."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH d AS (
              SELECT symbol, ts,
                     oi - LAG(oi) OVER (PARTITION BY symbol ORDER BY ts) AS chg
              FROM bt14_fut_oi)
            UPDATE bt14_fut_oi f SET oi_chg = d.chg
            FROM d WHERE f.symbol=d.symbol AND f.ts=d.ts""")
        cur.execute("""
            UPDATE bt14_fut_oi f SET basis = f.fut_close - b.c
            FROM bt14_bars b WHERE b.symbol=f.symbol AND b.ts=f.ts""")
        conn.commit()


def run_backfill(time_budget_s=1800):
    """Resumable chunk. Cursor = index into the top-N symbol list (app_config
    bt14_fut_oi_cursor). Returns {complete, cursor, ...}. Off-market caller only."""
    t0 = time.time()
    conn = _conn()
    try:
        _ensure_table(conn)

        sz = _db_size_bytes(conn)
        if sz > _WORK_LIMIT_BYTES:
            with conn.cursor() as cur:
                _oplog(cur, "BT14_FUT_OI_PAUSE_DISK",
                       {"db_bytes": sz, "limit": _WORK_LIMIT_BYTES})
                conn.commit()
            return {"complete": False, "paused": "disk", "db_bytes": sz}

        verdict = _last_probe_verdict(conn)
        if verdict is None:
            with conn.cursor() as cur:
                _oplog(cur, "BT14_FUT_OI_ABORT",
                       {"reason": "no probe on record — run probe first"})
                conn.commit()
            return {"complete": True, "aborted": "no_probe"}
        if verdict == "none":
            with conn.cursor() as cur:
                _oplog(cur, "BT14_FUT_OI_ABORT",
                       {"reason": "probe verdict=none — Fyers History serves no OI for "
                                  "expired stock-futures; basis/OI stay live-forward-only"})
                conn.commit()
            return {"complete": True, "aborted": "no_oi"}

        token = pcr_backfill._load_token(conn)
        syms = _top_symbols(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='bt14_fut_oi_cursor'")
            r = cur.fetchone()
        cursor = int(r[0]) if (r and r[0] and str(r[0]).isdigit()) else 0

        written = 0
        i = cursor
        while i < len(syms):
            if time.time() - t0 > time_budget_s:
                break
            try:
                written += _write_symbol(conn, token, syms[i])
            except Exception as e:
                with conn.cursor() as cur:
                    _oplog(cur, "BT14_FUT_OI_SYM_ERR", {"symbol": syms[i], "err": str(e)})
                    conn.commit()
            i += 1
            with conn.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) "
                            "VALUES ('bt14_fut_oi_cursor',%s,NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                            (str(i),))
                conn.commit()

        complete = i >= len(syms)
        if complete:
            _finalize(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), "
                            "COUNT(basis), COUNT(oi) FROM bt14_fut_oi")
                cnt = cur.fetchone()
                _oplog(cur, "BT14_FUT_OI_DONE",
                       {"rows": cnt[0], "symbols": cnt[1], "rows_with_basis": cnt[2],
                        "rows_with_oi": cnt[3], "db_bytes": _db_size_bytes(conn),
                        "verdict": verdict, "n_target": len(syms)})
                conn.commit()
        return {"complete": complete, "cursor": i, "n_symbols": len(syms),
                "written_this_run": written, "verdict": verdict, "db_bytes": sz}
    finally:
        conn.close()

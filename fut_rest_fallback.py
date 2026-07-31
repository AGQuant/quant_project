"""
fut_rest_fallback.py — cc#770 (P0) app-side REST futures fallback.

WHY: on 31-Jul the native WS futures leg went silent — the worker subscribes and Fyers ACKs the futures
batches, but streams ZERO futures ticks (equity ticks fine on the SAME socket; clean restarts don't fix
it). Meanwhile the Fyers /data/quotes REST endpoint returns LIVE data for the very same futures
contracts. This module synthesizes 5-min futures bars from those REST snapshots so open-position MTM,
the index book and the signal engines are not blind while the WS leg is dark.

CONTRACT:
  - Bars are written with source='fyers_fut_rest' — NEVER 'fyers_fut'. Live consumers may read the
    fallback source (see the smartgain MTM pass); the native-WS-only replay path is untouched, so
    replay integrity holds (cc#770 spec: "clearly tagged source").
  - APP-SIDE ONLY. Does NOT touch the worker — no re-auth coin-flip on the working equity leg (the exact
    risk the FEED WORKER DEPLOY RULE guards, and the reason this fallback exists instead of a worker bounce).
  - GATED on native-fut-dark: if a fresh native fyers_fut bar exists (worker recovered), this NO-OPs, so
    it self-retires the moment the real feed returns.
  - GENTLE pace: batched quotes, exponential backoff on empty/throttled bodies, priority = open-position
    futures + indices first, then the rest of the active universe if the token isn't being throttled.

Driven by scheduler.py's 5-min market-hours loop.
"""
import logging
import time
from datetime import datetime, timedelta, time as dt_time

import requests

import fyers_endpoints as FE   # reuse _conn / _get_token / _current_expiry / QUOTES_URL / FYERS_CLIENT_ID

log = logging.getLogger("scorr.fut_rest_fallback")

REST_SOURCE      = "fyers_fut_rest"   # distinct from native 'fyers_fut' — replay integrity
NATIVE_DARK_MIN  = 12                 # native fut is "dark" if no fyers_fut bar in the last this-many minutes
BATCH            = 40                 # symbols per /data/quotes call (Fyers accepts comma-separated)
GENTLE_SLEEP     = 0.4                # base pace between batches (s)
MAX_BACKOFF      = 8.0                # cap on the empty-body backoff (s)
INDEX_SYMS       = ["NIFTY", "BANKNIFTY"]


def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _market_hours(n=None):
    n = n or _ist_now()
    return n.weekday() < 5 and dt_time(9, 15) <= n.time() <= dt_time(15, 30)


def _floor_5m(n):
    return n.replace(minute=(n.minute // 5) * 5, second=0, microsecond=0)


def _fut_fyers_symbol(nse_code):
    exp = FE._current_expiry()
    return f"NSE:{nse_code}{exp.strftime('%y')}{exp.strftime('%b').upper()}FUT"


def _native_fut_dark(cur):
    """True when no native WS futures bar has landed in the last NATIVE_DARK_MIN minutes (IST)."""
    cur.execute(f"""SELECT MAX(ts) FROM intraday_prices WHERE source='fyers_fut'
                    AND ts >= (NOW() AT TIME ZONE 'Asia/Kolkata') - INTERVAL '{NATIVE_DARK_MIN} minutes'""")
    r = cur.fetchone()
    return not (r and r[0])


def _target_symbols(cur):
    """Priority-ordered target list: open-position futures + indices FIRST, then the rest of the active
    futures universe. De-duped, upper-cased. Priority names survive even if later batches get throttled."""
    prio, seen, out = [], set(), []
    # indices always first
    for s in INDEX_SYMS:
        if s not in seen:
            seen.add(s); prio.append(s)
    # open SmartGain / client / test futures positions
    for tbl in ("smartgain_holdings", "client_positions", "test_positions"):
        try:
            cur.execute(f"SELECT DISTINCT UPPER(symbol) FROM {tbl} WHERE qty IS NOT NULL AND qty <> 0")
            for (s,) in cur.fetchall():
                if s and s not in seen:
                    seen.add(s); prio.append(s)
        except Exception:
            cur.connection.rollback()   # table may not exist / differ — skip, keep going
    # rest of the active futures universe (headroom coverage)
    try:
        cur.execute("SELECT DISTINCT UPPER(symbol) FROM futures_universe WHERE is_active=TRUE")
        rest = [s for (s,) in cur.fetchall() if s and s not in seen]
    except Exception:
        cur.connection.rollback(); rest = []
    out = prio + sorted(rest)
    return out, len(prio)


def _fetch_batch(fyers_syms, token):
    """One /data/quotes call for a comma-separated batch. Returns {fyers_symbol: value_dict}. Raises on a
    hard HTTP/parse failure so the caller can back off; returns {} on an empty (throttled) body."""
    r = requests.get(FE.QUOTES_URL, params={"symbols": ",".join(fyers_syms)},
                     headers={"Authorization": f"{FE.FYERS_CLIENT_ID}:{token}"}, timeout=10)
    d = r.json()
    if d.get("s") != "ok":
        raise RuntimeError(f"quotes API not ok: {d.get('message', d)}")
    out = {}
    for item in (d.get("d") or []):
        n = item.get("n"); v = item.get("v") or {}
        if n:
            out[n] = v
    return out


def run(conn=None):
    """Fetch REST futures quotes for the target set and write 5-min fyers_fut_rest bars. NO-OP outside
    market hours or when the native WS futures leg is healthy. Returns a summary dict."""
    if not _market_hours():
        return {"skipped": "off-hours"}
    own = conn is None
    conn = conn or FE._conn()
    summary = {"written": 0, "symbols": 0, "batches": 0, "empty_batches": 0, "priority": 0}
    try:
        with conn.cursor() as cur:
            if not _native_fut_dark(cur):
                return {"skipped": "native fut healthy"}
            targets, n_prio = _target_symbols(cur)
            summary["priority"] = n_prio
        # cc#770: the OI/depth REST is already ~50% throttled (empty bodies) and account-level rate
        # limiting is a suspected contributor to the WS-futures outage — so DO NOT hammer all ~208
        # contracts. Fetch the priority set only (open-position futures + indices). Extending to the
        # full universe is deliberately withheld until the throttle clears (spec: "full universe IF
        # headroom" — there is none right now).
        targets = targets[:n_prio] if n_prio else targets
        if not targets:
            return {"skipped": "no targets"}
        token = FE._get_token()
        # NSE:CODE..FUT -> CODE map (to write bars keyed by the base NSE symbol, matching native fut bars)
        fmap = {_fut_fyers_symbol(s): s for s in targets}
        fsyms = list(fmap.keys())
        ts = _floor_5m(_ist_now())
        backoff = GENTLE_SLEEP
        rows = []
        for i in range(0, len(fsyms), BATCH):
            batch = fsyms[i:i + BATCH]
            summary["batches"] += 1
            try:
                got = _fetch_batch(batch, token)
            except Exception as e:
                log.warning(f"fut_rest_fallback batch {i//BATCH}: {e} — backing off {backoff:.1f}s")
                summary["empty_batches"] += 1
                time.sleep(backoff); backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if not got:
                # empty body = throttle signature -> exponential backoff + jitter, halve pace
                summary["empty_batches"] += 1
                jitter = (i % 5) * 0.05
                time.sleep(backoff + jitter); backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            backoff = GENTLE_SLEEP   # recovered — reset pace
            for fsym, v in got.items():
                ltp = v.get("lp") or v.get("ltp")
                if ltp is None:
                    continue
                code = fmap.get(fsym)
                if not code:
                    continue
                ltp = float(ltp)
                o = float(v.get("open_price") or ltp)
                hi = float(v.get("high_price") or ltp)
                lo = float(v.get("low_price") or ltp)
                vol = v.get("volume")
                # 5-min bar keyed to this window; O/H/L default to the day fields, close=LTP (a REST
                # snapshot is a point-in-time — the source tag makes clear it is not a native OHLC bar).
                rows.append((code, ts, o, max(hi, ltp), min(lo, ltp), ltp,
                             int(vol) if vol is not None else None))
            time.sleep(GENTLE_SLEEP)
        if rows:
            with conn.cursor() as cur:
                cur.executemany(f"""
                    INSERT INTO intraday_prices (symbol, ts, open, high, low, close, volume, timeframe, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, '5m', '{REST_SOURCE}')
                    ON CONFLICT (symbol, ts, timeframe, source) DO UPDATE
                        SET close = EXCLUDED.close,
                            high  = GREATEST(intraday_prices.high, EXCLUDED.high),
                            low   = LEAST(intraday_prices.low, EXCLUDED.low),
                            volume = EXCLUDED.volume
                """, rows)
            conn.commit()
            summary["written"] = len(rows)
            summary["symbols"] = len({r[0] for r in rows})
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'alert', 'FUT_REST_FALLBACK', %s::jsonb)""",
                            (__import__("json").dumps(summary),))
            conn.commit()
        except Exception:
            conn.rollback()
        log.info(f"fut_rest_fallback: {summary}")
        return summary
    except Exception as e:
        log.error(f"fut_rest_fallback failed: {e}")
        try: conn.rollback()
        except Exception: pass
        return {"error": str(e)}
    finally:
        if own:
            try: conn.close()
            except Exception: pass

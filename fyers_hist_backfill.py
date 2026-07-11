"""
fyers_hist_backfill.py — cc#377 Phase B (signal-day fetcher) + Phase 0 depth probe
==================================================================================
PURGE-EXEMPT historical 5-min EQUITY bars for backtest replay.

DESIGN (documented per spec): historical bars are written to intraday_prices with a
DEDICATED source='fyers_hist'. purge_old_bars() (fyers_feed.py) explicitly EXEMPTS this
source, so year-old backfilled bars survive the nightly 365d(fyers_eq)/7d(other) purge that
would otherwise delete them within days. Backtest readers MUST query source='fyers_hist'
explicitly (kept separate from the live fyers_eq series so neither pollutes the other).

Reuses the production Fyers REST 5m pattern verbatim (fyers_backfill.fetch_history +
upsert_candles); token via fyers_feed.get_valid_token. Writes are POST-MARKET / on-demand
only — fetch_hist_5m asserts _assert_not_market_hours (canonical rule cc#87); the probe is a
pure read (no writes) and is safe anytime.

Phase B (this file):
  - fetch_hist_5m(symbol, from_date, to_date)  -> chunked (<=100d/req) 5m eq fetch, upsert
    source='fyers_hist', idempotent (ON CONFLICT DO UPDATE), 5s pacing.
  - backfill_signals(pairs, trailing_days=15)  -> batch over (symbol, date) pairs; each window
    = [date, date+trailing_days]. Replays a backtest's entries + N-day exit tracking.
  - probe_5m_depth(symbol='SBIN')              -> Phase 0: one-week windows at ~T-2m/6m/9m/12m,
    per-window candle count logged to session_log (category=data_audit,
    title=FYERS_5M_DEPTH_PROBE). Gates Phase A scope = min(365d, actual depth).

Phase A (full 365d warehouse for all active futures_universe symbols) ships SEPARATELY once
the probe confirms depth (founder decides if depth < 9 months).
"""
import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

log = logging.getLogger("scorr.fyers_hist_backfill")
router = APIRouter(prefix="/api/admin", tags=["fyers_hist_backfill"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

HIST_SOURCE   = "fyers_hist"   # purge-exempt (see purge_old_bars in fyers_feed.py)
CHUNK_DAYS    = 100            # Fyers History API intraday cap per request
SLEEP_BETWEEN = 5             # seconds between REST calls — rate-limit safe (matches fyers_backfill)


def _check_admin(token):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


def _as_date(d):
    return datetime.strptime(d, "%Y-%m-%d").date() if isinstance(d, str) else d


def fetch_hist_5m(symbol, from_date, to_date, conn=None, token=None) -> dict:
    """Fetch 5m EQ candles for [from_date, to_date] for ONE symbol and upsert them PURGE-EXEMPT
    (source='fyers_hist'). Chunks to <=100 days/request, 5s pacing, idempotent (DO UPDATE).
    Post-market/on-demand only (asserts market-hours block)."""
    import fyers_backfill
    import fyers_feed
    fyers_backfill._assert_not_market_hours("fetch_hist_5m")
    from_date, to_date = _as_date(from_date), _as_date(to_date)
    own = conn is None
    if own:
        conn = fyers_feed.get_db()
    try:
        if token is None:
            token = fyers_feed.get_valid_token(conn)
        sym = symbol.upper()
        fsym = fyers_backfill.fyers_eq_symbol(sym)
        total, chunks = 0, 0
        cur_from = from_date
        while cur_from <= to_date:
            cur_to = min(cur_from + timedelta(days=CHUNK_DAYS - 1), to_date)
            rows = fyers_backfill.fetch_history(token, sym, "5", "5m", cur_from, cur_to,
                                                fyers_symbol=fsym, source=HIST_SOURCE, cont_flag="1")
            if rows:
                fyers_backfill.upsert_candles(conn, rows, on_conflict="update")
                total += len(rows)
            chunks += 1
            cur_from = cur_to + timedelta(days=1)
            if cur_from <= to_date:
                time.sleep(SLEEP_BETWEEN)
        log.info(f"fetch_hist_5m {sym} {from_date}->{to_date}: {total} bars, {chunks} chunks")
        return {"symbol": sym, "from": str(from_date), "to": str(to_date),
                "bars": total, "chunks": chunks, "source": HIST_SOURCE}
    finally:
        if own:
            conn.close()


def backfill_signals(pairs, trailing_days=15, conn=None, token=None) -> dict:
    """Batch signal-day fetcher: for each (symbol, signal_date) fetch [date, date+trailing_days]
    of 5m hist bars — so a backtest's entries AND their N-day exit windows replay on real bars.
    pairs = list of {"symbol":..,"date":"YYYY-MM-DD"} (or [symbol, date] tuples)."""
    import fyers_feed
    own = conn is None
    if own:
        conn = fyers_feed.get_db()
    try:
        if token is None:
            token = fyers_feed.get_valid_token(conn)
        out, total = [], 0
        for p in pairs:
            sym = p.get("symbol") if isinstance(p, dict) else (p[0] if len(p) > 0 else None)
            d = p.get("date") if isinstance(p, dict) else (p[1] if len(p) > 1 else None)
            if not sym or not d:
                continue
            d0 = _as_date(d)
            res = fetch_hist_5m(sym, d0, d0 + timedelta(days=trailing_days), conn=conn, token=token)
            out.append(res)
            total += res["bars"]
        return {"pairs": len(out), "bars_written": total, "trailing_days": trailing_days,
                "source": HIST_SOURCE, "detail": out}
    finally:
        if own:
            conn.close()


def probe_5m_depth(symbol="SBIN", conn=None, token=None) -> dict:
    """Phase 0 depth probe (READ-ONLY — writes NOTHING to intraday_prices). Fyers officially
    guarantees only '7+ months' of minute history, so 1 year is not assured. Fetch one-week
    windows at ~T-2m/6m/9m/12m and record candles-per-window; log to session_log
    (data_audit / FYERS_5M_DEPTH_PROBE). Phase A scope = min(365d, deepest window with data)."""
    import fyers_backfill
    import fyers_feed
    own = conn is None
    if own:
        conn = fyers_feed.get_db()
    try:
        if token is None:
            token = fyers_feed.get_valid_token(conn)
        sym = symbol.upper()
        fsym = fyers_backfill.fyers_eq_symbol(sym)
        today = date.today()
        windows = {}
        for label, months in (("T-2m", 2), ("T-6m", 6), ("T-9m", 9), ("T-12m", 12)):
            end = today - timedelta(days=months * 30)
            start = end - timedelta(days=7)
            rows = fyers_backfill.fetch_history(token, sym, "5", "5m", start, end,
                                                fyers_symbol=fsym, source=HIST_SOURCE, cont_flag="1")
            windows[label] = {"from": str(start), "to": str(end), "candles": len(rows),
                              "first_ts": str(rows[0][1]) if rows else None,
                              "last_ts": str(rows[-1][1]) if rows else None}
            time.sleep(SLEEP_BETWEEN)
        deepest = next((lbl for lbl in ("T-12m", "T-9m", "T-6m", "T-2m") if windows[lbl]["candles"] > 0), None)
        result = {"symbol": sym, "windows": windows, "deepest_with_data": deepest,
                  "probed_at": datetime.utcnow().isoformat()}
        try:
            with conn.cursor() as c:
                c.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                             VALUES (CURRENT_DATE, NOW(), 'data_audit', 'FYERS_5M_DEPTH_PROBE', %s::jsonb)""",
                          (json.dumps(result),))
            conn.commit()
        except Exception as e:
            log.warning(f"probe_5m_depth session_log write: {e}")
        log.info(f"probe_5m_depth {sym}: deepest_with_data={deepest} {result['windows']}")
        return result
    finally:
        if own:
            conn.close()


# ── deploy-time self-trigger for the Phase 0 probe ──
# The CC sandbox has no HTTP path to prod, so a DB flag set via MCP run_sql + this startup hook
# runs the probe exactly once (atomic claim). Flag app_config['fyers_5m_probe']='pending' -> SBIN;
# 'pending:SYMBOL' -> that symbol. Mirrors stock_options_backfill's boot trigger.
_PROBE_FLAG = "fyers_5m_probe"


def _claim_probe_flag():
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s AND value LIKE 'pending%%' FOR UPDATE",
                            (_PROBE_FLAG,))
                r = cur.fetchone()
                if r:
                    cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s",
                                (_PROBE_FLAG,))
            conn.commit()
        finally:
            conn.close()
        if not r:
            return None
        val = r[0] or "pending"
        return val.split(":", 1)[1].strip().upper() if ":" in val else "SBIN"
    except Exception as e:
        log.error(f"probe flag claim failed: {e}")
        return None


_HIST_FETCH_FLAG = "hist_fetch_pending"   # value 'SYMBOL:FROM:TO' (dates YYYY-MM-DD) -> one fetch_hist_5m


def _claim_hist_fetch_flag():
    """Atomically consume a one-off 'SYMBOL:FROM:TO' hist-fetch flag (deploy-time self-trigger for
    the Phase B acceptance test / ad-hoc pulls; CC has no HTTP path to prod). 'claimed' has no
    colon so it never re-runs."""
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s AND value LIKE '%%:%%' FOR UPDATE",
                            (_HIST_FETCH_FLAG,))
                r = cur.fetchone()
                if r:
                    cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s",
                                (_HIST_FETCH_FLAG,))
            conn.commit()
        finally:
            conn.close()
        return r[0] if r else None
    except Exception as e:
        log.error(f"hist_fetch flag claim failed: {e}")
        return None


# ── cc#389 Phase A: full trailing-365d 5m EQUITY warehouse for ALL active futures symbols ──
_PHASE_A_FLAG     = "phase_a_run"        # 'pending' -> claimed -> done (boot self-trigger)
_PHASE_A_PROGRESS = "phase_a_progress"   # last completed symbol (resume checkpoint)


def _set_config(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s, NOW())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                    (key, value))
    conn.commit()


def _oplog(conn, category, title, details):
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                        (category, title, json.dumps(details, default=str)))
        conn.commit()
    except Exception as e:
        log.warning(f"ops_log write ({title}): {e}")


def run_phase_a(conn=None, token=None) -> dict:
    """cc#389: full warehouse run. For every active futures_universe symbol (A->Z) fetch trailing
    365d of 5m EQ bars into source='fyers_hist' (purge-exempt), reusing ONE shared conn+token.
    RESUMABLE: app_config['phase_a_progress'] holds the last completed symbol; on start we skip every
    symbol <= saved, so a crash/restart continues and never repeats (upserts make repeats harmless
    anyway). ops_log PHASE_A_PROGRESS every 20 symbols; completion -> session_log
    PHASE_A_WAREHOUSE_COMPLETE with totals + failures. A failed symbol is logged and skipped, never
    aborts the run. Post-market/on-demand only (fetch_hist_5m asserts market-hours block)."""
    import fyers_feed
    own = conn is None
    if own:
        conn = fyers_feed.get_db()
    started = time.time()
    try:
        if token is None:
            token = fyers_feed.get_valid_token(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT UPPER(symbol) FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT value FROM app_config WHERE key=%s", (_PHASE_A_PROGRESS,))
            r = cur.fetchone()
        progress = (r[0] or "") if r else ""
        today = date.today()
        frm = today - timedelta(days=365)
        pending = [s for s in symbols if s > progress] if progress else list(symbols)
        _oplog(conn, "data_infra", "PHASE_A_START",
               {"universe": len(symbols), "resuming_after": progress or None,
                "to_do": len(pending), "from": str(frm), "to": str(today)})
        done = bars_total = failures_ct = 0
        failures = []
        for sym in pending:
            try:
                res = fetch_hist_5m(sym, frm, today, conn=conn, token=token)
                bars_total += res["bars"]
            except Exception as e:
                failures_ct += 1
                failures.append({"symbol": sym, "error": str(e)[:200]})
                log.error(f"run_phase_a {sym} failed: {e}")
            done += 1
            try:
                _set_config(conn, _PHASE_A_PROGRESS, sym)   # checkpoint after EVERY symbol
            except Exception as e:
                log.warning(f"phase_a checkpoint {sym}: {e}")
            if done % 20 == 0:
                _oplog(conn, "data_infra", "PHASE_A_PROGRESS",
                       {"done": done, "of": len(pending), "bars_written": bars_total,
                        "failures": failures_ct, "elapsed_min": round((time.time() - started) / 60, 1),
                        "last_symbol": sym})
        elapsed = round((time.time() - started) / 60, 1)
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts)::date
                           FROM intraday_prices WHERE source=%s""", (HIST_SOURCE,))
            c = cur.fetchone()
        summary = {"symbols_processed": done, "universe": len(symbols),
                   "bars_written_this_run": bars_total, "failures": failures_ct,
                   "failures_list": failures, "elapsed_min": elapsed,
                   "warehouse_total_bars": int(c[0] or 0), "warehouse_symbols": int(c[1] or 0),
                   "warehouse_oldest": str(c[2]) if c[2] else None, "source": HIST_SOURCE}
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'data_audit', 'PHASE_A_WAREHOUSE_COMPLETE', %s::jsonb)""",
                            (json.dumps(summary, default=str),))
            conn.commit()
        except Exception as e:
            log.warning(f"PHASE_A completion log: {e}")
        try:
            _set_config(conn, _PHASE_A_FLAG, "done")
        except Exception:
            pass
        log.info(f"run_phase_a COMPLETE: {summary}")
        return summary
    finally:
        if own:
            conn.close()


def _claim_phase_a_flag():
    """Atomically consume app_config['phase_a_run']='pending' (deploy-time self-trigger; CC has no
    HTTP path to prod). 'claimed'/'done' never re-fire."""
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s AND value='pending' FOR UPDATE",
                            (_PHASE_A_FLAG,))
                r = cur.fetchone()
                if r:
                    cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s",
                                (_PHASE_A_FLAG,))
            conn.commit()
        finally:
            conn.close()
        return bool(r)
    except Exception as e:
        log.error(f"phase_a flag claim failed: {e}")
        return False


@router.on_event("startup")
async def _probe_startup_trigger():
    import threading
    sym = _claim_probe_flag()
    if sym:
        log.info(f"cc#377: 5m-depth-probe flag claimed — probing {sym} in background")
        threading.Thread(target=probe_5m_depth, args=(sym,), name="cc377-probe", daemon=True).start()
    spec = _claim_hist_fetch_flag()
    if spec:
        parts = spec.split(":")
        if len(parts) >= 3:
            fsym, frm, to = parts[0].strip().upper(), parts[1].strip(), parts[2].strip()
            log.info(f"cc#377: hist_fetch flag claimed — {fsym} {frm}..{to} in background")
            threading.Thread(target=fetch_hist_5m, args=(fsym, frm, to), name="cc377-histfetch", daemon=True).start()
    if _claim_phase_a_flag():   # cc#389
        log.info("cc#389: phase_a_run flag claimed — starting full 365d warehouse in background")
        threading.Thread(target=run_phase_a, name="cc389-phasea", daemon=True).start()


# ── admin endpoints (thin wiring; also proxied by the MCP tools in mcp_dispatch.py) ──
class FetchHistReq(BaseModel):
    symbol: str
    from_date: str
    to_date: str


class BackfillSignalsReq(BaseModel):
    pairs: List[dict]
    trailing_days: Optional[int] = 15


@router.post("/fetch_hist_5m")
async def fetch_hist_5m_now(body: FetchHistReq, x_admin_token: Optional[str] = Header(None)):
    """cc#377 Phase B: fetch 5m eq hist for one symbol/window into source='fyers_hist' (purge-exempt)."""
    _check_admin(x_admin_token)
    try:
        return await asyncio.to_thread(fetch_hist_5m, body.symbol, body.from_date, body.to_date)
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/backfill_signals")
async def backfill_signals_now(body: BackfillSignalsReq, x_admin_token: Optional[str] = Header(None)):
    """cc#377 Phase B: batch fetch [date, date+trailing_days] 5m hist for a backtest signal list."""
    _check_admin(x_admin_token)
    try:
        return await asyncio.to_thread(backfill_signals, body.pairs, body.trailing_days or 15)
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/probe_5m_depth")
async def probe_5m_depth_now(symbol: str = "SBIN", x_admin_token: Optional[str] = Header(None)):
    """cc#377 Phase 0: probe Fyers 5m history depth (read-only) -> session_log FYERS_5M_DEPTH_PROBE."""
    _check_admin(x_admin_token)
    try:
        return await asyncio.to_thread(probe_5m_depth, symbol)
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/run_phase_a")
async def run_phase_a_now(x_admin_token: Optional[str] = Header(None)):
    """cc#389: kick the full 365d 5m warehouse run (all active futures) in a background daemon —
    resumable, non-blocking. Watch ops_log PHASE_A_PROGRESS + session_log PHASE_A_WAREHOUSE_COMPLETE."""
    _check_admin(x_admin_token)
    import threading
    threading.Thread(target=run_phase_a, name="cc389-phasea-manual", daemon=True).start()
    return {"status": "started",
            "note": "Phase A warehouse running in background; poll ops_log / session_log for progress."}

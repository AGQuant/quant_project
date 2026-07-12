"""
GVM Deep Backfill (cc#468 / cc#470) — history-only reconstruction.
=================================================================
Reconstruct daily GVM back ~5 YEARS for the FUTURES universe FIRST, then the
TOP-500 stocks by market cap (screener_raw mcap rank). Purpose: V9 pair
discovery/backtest needs GVM as-of PAST dates (pairs filter GVM>=threshold at
formation date) — 44 live days in gvm_history is not enough.

Pillar honesty (cc#470 mandate):
  M (momentum): FULLY reconstructed per trading date from raw_prices, reusing
    momentum_daily's EXACT scoring primitives. Peer trimmed-means are computed
    over the full raw_prices universe (same as the live engine) so backfilled M
    matches live M within rounding on the overlap window.
  G/V: HELD at the current screener_raw snapshot and flagged
    method='backfill_step_partial'. fundamentals_history is raw scraped jsonb
    financials (revenue/profit periods), NOT the derived screener metrics
    (sales_growth_5y, opm_expansion, roce, potential_upside, ...) that the G/V
    scorer consumes — so a point-in-time G/V cannot be honestly reconstructed.
    Per cc#470 the honest fallback is: hold G/V at oldest/only-known and FLAG.
    Honest flagging is mandatory; no fabricated point-in-time fundamentals.

Write model:
  gvm_history ONLY, INSERT ... ON CONFLICT (symbol, score_date) DO NOTHING.
  The 44 LIVE days (30-May-2026 → today) are NEVER touched — the backfill date
  range explicitly ends at 2026-05-29. The live GVM engine/scores are untouched
  (scope_guard: history reconstruction only).

Runtime:
  Resumable overnight, checkpointed in app_config['gvm_backfill_progress']
  (JSON set of completed score_dates). Iterates NEWEST->OLDEST so the most
  recently queried history — and the overlap-sanity window — land first. A
  per-invocation wall-clock budget lets a single night stop gracefully and the
  next night resume. ops_log progress every 20 dates + a final summary.
"""

import os
import json
import time
import logging
from datetime import date, timedelta

import psycopg
import pandas as pd
import numpy as np

import momentum_daily as md
import gvm_nightly as gn

log = logging.getLogger("scorr.gvm_backfill")

DATABASE_URL = os.getenv("DATABASE_URL")

# Backfill horizon + the live window we must never overwrite.
BACKFILL_YEARS = 5
LIVE_WINDOW_START = date(2026, 5, 30)   # gvm_history live rows start here (44 days)
TOP_N_BY_MCAP = 500
METHOD_FLAG = "backfill_step_partial"   # G/V held-constant, fundamentals shallow (cc#470)

PROGRESS_KEY = "gvm_backfill_progress"
UNIVERSE_KEY = "gvm_backfill_universe"
RUN_FLAG_KEY = "gvm_backfill_run"       # 'pending' | 'running' | 'done'


def _conn():
    return psycopg.connect(DATABASE_URL)


# ── schema + config helpers ─────────────────────────────────────────────────
def _ensure_method_col():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE gvm_history ADD COLUMN IF NOT EXISTS method TEXT")
        conn.commit()


def _cfg_get(cur, key):
    cur.execute("SELECT value FROM app_config WHERE key=%s", (key,))
    r = cur.fetchone()
    return r[0] if r else None


def _cfg_set(cur, key, value):
    cur.execute(
        "INSERT INTO app_config (key, value, updated_at) VALUES (%s,%s,NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
        (key, value),
    )


def _ops(cur, title, details):
    cur.execute(
        "INSERT INTO ops_log (session_date, session_ts, category, title, details) "
        "VALUES (CURRENT_DATE, NOW(), 'gvm_backfill', %s, %s)",
        (title, json.dumps(details)),
    )


# ── universe: futures-first, then top-500 by mcap ───────────────────────────
def _build_universe(gv_syms):
    """Ordered symbol list: active futures FIRST (V9 pairs priority), then the
    top-500 stocks by market_cap. Restricted to symbols that have G/V inputs
    (present in the screener/input merge) so every written row is scorable.
    Persisted to app_config so the ordering is stable across resume nights."""
    with _conn() as conn, conn.cursor() as cur:
        cached = _cfg_get(cur, UNIVERSE_KEY)
        if cached:
            try:
                arr = json.loads(cached)
                if isinstance(arr, list) and arr:
                    return [s for s in arr if s in gv_syms]
            except Exception:
                pass

        cur.execute("SELECT DISTINCT symbol FROM futures_universe WHERE is_active=true")
        futures = [str(r[0]).strip() for r in cur.fetchall()]
        cur.execute(
            "SELECT nse_code FROM screener_raw WHERE market_cap IS NOT NULL "
            "ORDER BY market_cap DESC LIMIT %s", (TOP_N_BY_MCAP,)
        )
        top500 = [str(r[0]).strip() for r in cur.fetchall()]

        ordered, seen = [], set()
        for s in futures + top500:
            if s and s in gv_syms and s not in seen:
                ordered.append(s); seen.add(s)

        _cfg_set(cur, UNIVERSE_KEY, json.dumps(ordered))
        conn.commit()
        log.info(f"gvm_backfill universe: {len(futures)} futures + top{TOP_N_BY_MCAP} "
                 f"-> {len(ordered)} scorable symbols")
        return ordered


# ── G/V held-constant (current screener snapshot) ───────────────────────────
def _compute_gv_snapshot():
    """Score G + V once from the current screener_raw snapshot. Held constant
    across every backfilled date and flagged backfill_step_partial. Returns
    {nse_code: (g, v, segment, company_name)}."""
    df = gn._load_merged_df(date.today())
    if df.empty:
        return {}
    peer_avgs = gn._peer_averages(df)
    gv = {}
    for _, row in df.iterrows():
        sym = str(row.get("nse_code", "")).strip()
        if not sym:
            continue
        try:
            sd = gn._stock_dict(row, peer_avgs)
            g = round(float(gn.api_g_score(sd)["score"]), 2)
            v = round(float(gn.api_v_score(sd)["score"]), 2)
        except Exception as e:
            log.warning(f"gv snapshot {sym}: {e}")
            continue
        seg = row.get("gvm_segment", "Unknown")
        cname = row.get("company_name", sym)
        gv[sym] = (g, v, str(seg), cname)
    return gv


# ── trading calendar (NEWEST -> OLDEST, live window excluded) ────────────────
def _trading_dates():
    start = date.today() - timedelta(days=int(BACKFILL_YEARS * 365.25) + 5)
    end = LIVE_WINDOW_START - timedelta(days=1)   # 2026-05-29 — never touch live rows
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT price_date FROM raw_prices "
            "WHERE symbol=%s AND price_date BETWEEN %s AND %s ORDER BY price_date DESC",
            (md.INDEX_SYMBOL, start, end),
        )
        dates = [r[0] for r in cur.fetchall()]
        if not dates:
            # fallback: union across all symbols (NIFTY50 series missing/short)
            cur.execute(
                "SELECT DISTINCT price_date FROM raw_prices "
                "WHERE price_date BETWEEN %s AND %s ORDER BY price_date DESC",
                (start, end),
            )
            dates = [r[0] for r in cur.fetchall()]
    return dates


# ── per-date M scoring (mirrors momentum_daily.compute_momentum exactly) ─────
def _m_scores_for_date(prices, seg_map, target_date):
    """Return {symbol: (m_score, latest_price)} for target_date, computed with
    the SAME peer trimmed-means (over the full universe) as the live engine."""
    raw = md._compute_raw_params(prices, target_date)
    if raw.empty:
        return {}
    peer1m = md._peer_trimmed_mean(raw, "ret_1m", seg_map)
    peer1y = md._peer_trimmed_mean(raw, "ret_1y", seg_map)
    peer3y = md._peer_trimmed_mean(raw, "ret_3y", seg_map)

    FB = 5.0
    out = {}
    for _, r in raw.iterrows():
        seg = seg_map.get(r["symbol"], "Unknown")
        r1m = md.score_two_factor(md.score_ret1m_absolute(r["ret_1m"]),
                                  md.score_relative(r["ret_1m"], peer1m.get(seg)))
        r1y = md.score_two_factor(md.score_return_absolute(r["ret_1y"]),
                                  md.score_relative(r["ret_1y"], peer1y.get(seg)))
        r3y = md.score_two_factor(md.score_return_absolute(r["ret_3y"]),
                                  md.score_relative(r["ret_3y"], peer3y.get(seg)))
        d50 = md.score_dma_absolute(r["dma_50"])
        d200 = md.score_dma_absolute(r["dma_200"])
        r52 = md.score_return_absolute(r["ret_52w_vs_index"])
        rsi = md.score_rsi_month_absolute(r["rsi_month"])
        vt = md.score_vol_trend(r.get("vol_trend"))
        ratings = [r1m, r1y, r3y, d50, d200, r52, rsi, vt]
        filled = [x if x is not None else FB for x in ratings]
        m_score = round(sum(filled) / 8, 2)
        out[str(r["symbol"]).strip()] = (m_score, r["latest_price"])
    return out


# ── main backfill loop ──────────────────────────────────────────────────────
def run_backfill(time_budget_s=10800, max_dates=None):
    """Reconstruct gvm_history for the ordered universe across the 5yr window.
    Resumable + idempotent (ON CONFLICT DO NOTHING). Returns a summary dict."""
    t0 = time.time()
    _ensure_method_col()

    gv = _compute_gv_snapshot()
    if not gv:
        return {"status": "warn", "message": "G/V snapshot empty (screener/input merge)"}
    universe = _build_universe(set(gv.keys()))
    if not universe:
        return {"status": "warn", "message": "empty universe"}
    universe_set = set(universe)

    prices = md._load_prices()
    if prices.empty:
        return {"status": "warn", "message": "raw_prices empty"}
    seg_map = md._segment_map()

    all_dates = _trading_dates()
    with _conn() as conn, conn.cursor() as cur:
        prog_raw = _cfg_get(cur, PROGRESS_KEY)
    done = set(json.loads(prog_raw)) if prog_raw else set()
    remaining = [d for d in all_dates if str(d) not in done]

    ins_sql = (
        "INSERT INTO gvm_history "
        "(symbol, score_date, g_score, v_score, m_score, gvm_score, verdict, segment, method) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (symbol, score_date) DO NOTHING"
    )

    processed = 0
    rows_written = 0
    stopped = "complete"
    for d in remaining:
        if max_dates is not None and processed >= max_dates:
            stopped = "max_dates"; break
        if time.time() - t0 > time_budget_s:
            stopped = "time_budget"; break

        mscores = _m_scores_for_date(prices, seg_map, d)
        batch = []
        for sym in universe_set:
            ms = mscores.get(sym)
            if ms is None:      # symbol has no price history on/before d — skip honestly
                continue
            m = ms[0]
            g, v, seg, _ = gv[sym]
            gvm = round((g + v + m) / 3, 2)
            batch.append((sym, d, g, v, m, gvm, gn._verdict(gvm), seg, METHOD_FLAG))

        if batch:
            with _conn() as conn, conn.cursor() as cur:
                cur.executemany(ins_sql, batch)
                inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                conn.commit()
            rows_written += inserted

        done.add(str(d))
        processed += 1

        if processed % 20 == 0:
            with _conn() as conn, conn.cursor() as cur:
                _cfg_set(cur, PROGRESS_KEY, json.dumps(sorted(done)))
                _ops(cur, "progress", {
                    "processed_this_run": processed, "dates_done_total": len(done),
                    "dates_total": len(all_dates), "rows_written_this_run": rows_written,
                    "last_date": str(d), "universe": len(universe),
                })
                conn.commit()
            log.info(f"gvm_backfill: {len(done)}/{len(all_dates)} dates, "
                     f"{rows_written} rows this run, last={d}")

    complete = len(done) >= len(all_dates)
    with _conn() as conn, conn.cursor() as cur:
        _cfg_set(cur, PROGRESS_KEY, json.dumps(sorted(done)))
        if complete:
            _cfg_set(cur, RUN_FLAG_KEY, "done")
        _ops(cur, "run_end", {
            "stopped": stopped, "complete": complete,
            "processed_this_run": processed, "dates_done_total": len(done),
            "dates_total": len(all_dates), "rows_written_this_run": rows_written,
            "elapsed_s": round(time.time() - t0, 1), "universe": len(universe),
            "method_flag": METHOD_FLAG,
        })
        conn.commit()

    result = {
        "status": "ok", "stopped": stopped, "complete": complete,
        "processed_this_run": processed, "dates_done_total": len(done),
        "dates_total": len(all_dates), "rows_written_this_run": rows_written,
        "universe": len(universe), "elapsed_s": round(time.time() - t0, 1),
    }
    if complete:
        result["sanity"] = sanity_report()
    return result


# ── overlap sanity: backfilled M vs LIVE M on the live window ────────────────
def sanity_report(symbols=None, n_dates=3):
    """Read-only. Recompute M for a few symbols on live-window dates and compare
    to the live gvm_history rows. M must match within rounding (same math, same
    prices). G/V drift is EXPECTED and honest (backfill holds the current
    snapshot; live rows carried the snapshot of their own date)."""
    try:
        prices = md._load_prices()
        seg_map = md._segment_map()
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT score_date FROM gvm_history "
                "WHERE score_date >= %s ORDER BY score_date DESC LIMIT %s",
                (LIVE_WINDOW_START, n_dates),
            )
            dates = [r[0] for r in cur.fetchall()]
            if symbols is None:
                cur.execute(
                    "SELECT DISTINCT symbol FROM gvm_history WHERE score_date=%s "
                    "AND symbol IN (SELECT symbol FROM futures_universe WHERE is_active=true) "
                    "ORDER BY symbol LIMIT 3", (dates[0],) if dates else (LIVE_WINDOW_START,)
                )
                symbols = [r[0] for r in cur.fetchall()]

        checks = []
        for d in dates:
            ms = _m_scores_for_date(prices, seg_map, d)
            with _conn() as conn, conn.cursor() as cur:
                for sym in symbols:
                    cur.execute(
                        "SELECT m_score, gvm_score FROM gvm_history WHERE symbol=%s AND score_date=%s",
                        (sym, d),
                    )
                    row = cur.fetchone()
                    recomputed = ms.get(sym)
                    if row and recomputed is not None:
                        live_m = float(row[0]) if row[0] is not None else None
                        bf_m = float(recomputed[0])
                        checks.append({
                            "symbol": sym, "date": str(d),
                            "live_m": live_m, "backfill_m": bf_m,
                            "m_delta": round(abs(bf_m - live_m), 3) if live_m is not None else None,
                            "match": (live_m is not None and abs(bf_m - live_m) <= 0.3),
                        })
        matched = sum(1 for c in checks if c["match"])
        return {"checks": checks, "matched": matched, "total": len(checks),
                "note": "M compared (must match within rounding); G/V held-constant so drift expected"}
    except Exception as e:
        log.error(f"sanity_report: {e}")
        return {"status": "error", "message": str(e)}

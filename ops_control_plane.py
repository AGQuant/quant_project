"""cc#693: OPS CONTROL PLANE V1 — Phase 1 (spec session_log id=9116, seeded from id=9115).

Three DB masters + one execution spine, all read by a PURE-READER diagnosis:
  * job_runs        — the spine. ONE row per job execution (scheduled or manual): job_key, started_at,
                      finished_at, status (ok/partial/failed), sections_landed jsonb, error. Single truth,
                      replaces fragmented per-table status bookkeeping over time.
  * scrape_registry — every external data-acquisition job (the 13 of SCRAPE_FRAMEWORK_V1 id=9115).
  * schedule_registry — every scheduled job wired in scheduler.py (job_key, cadence, owner, watchdog).

UNIVERSAL FAILURE RULE (founder 26-Jul): any run with status partial/failed ALSO writes an ops_log row
category=scrape_review the same run (job, scope, reason) — no scrape failure is ever silent; log-only,
no proactive nudge. The scheduler CRITICAL-LOGS panel + master diagnosis read those.

This module owns table DDL, the record_run() write path, the registry seeds, and the diagnosis reader.
It does NOT rewrite any job logic — jobs call record_run() at entry/exit. SCRAPE_WORKER_V1 (dedicated
Railway service) is a separate card (~mid-Aug); nothing is moved to a new service here.
"""
import os
import json
import logging
from typing import Optional, List, Dict, Any

import psycopg
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.control_plane")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
control_plane_router = APIRouter(tags=["control_plane"])


def _conn():
    return psycopg.connect(DATABASE_URL)


def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS scrape_registry (
        job_key TEXT PRIMARY KEY, name TEXT, source TEXT, target_table TEXT, freq_desc TEXT,
        freq_window_hours NUMERIC, owner_module TEXT, health_check TEXT,
        enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS schedule_registry (
        job_key TEXT PRIMARY KEY, cadence TEXT, owner_module TEXT, watchdog_rule TEXT,
        enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS job_runs (
        id BIGSERIAL PRIMARY KEY, job_key TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ, status TEXT NOT NULL DEFAULT 'ok', sections_landed JSONB, error TEXT)""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_job_runs_key_started ON job_runs(job_key, started_at DESC)")


# ── the spine: one call per execution ────────────────────────────────────────────────────────────
def record_run(job_key: str, status: str = "ok", sections_landed=None, error: str = None,
               started_at=None, scope: str = None) -> Optional[int]:
    """Write ONE job_runs row for an execution. status in {ok, partial, failed}. On partial/failed also
    write ops_log category=scrape_review (universal failure rule). Best-effort — never raises into the
    caller's job. Pass started_at (a datetime) to record real duration; omitted -> now for both ends."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            cur.execute("""INSERT INTO job_runs (job_key, started_at, finished_at, status, sections_landed, error)
                           VALUES (%s, COALESCE(%s, NOW()), NOW(), %s, %s, %s) RETURNING id""",
                        (job_key, started_at, status,
                         json.dumps(sections_landed) if sections_landed is not None else None,
                         (error or None)))
            run_id = cur.fetchone()[0]
            if status in ("partial", "failed"):
                try:
                    cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                                   VALUES (CURRENT_DATE, NOW(), 'scrape_review', %s, %s::jsonb)""",
                                (f"{job_key} {status}",
                                 json.dumps({"job_key": job_key, "status": status, "scope": scope,
                                             "reason": error, "sections_landed": sections_landed,
                                             "job_run_id": run_id})))
                except Exception as e:
                    log.warning(f"control_plane scrape_review write failed for {job_key}: {e}")
            conn.commit()
            return run_id
    except Exception as e:
        log.warning(f"control_plane record_run failed for {job_key}: {e}")
        return None


# ── seeds ────────────────────────────────────────────────────────────────────────────────────────
# 13 external-acquisition jobs (SCRAPE_FRAMEWORK_V1 id=9115). freq_window_hours = the staleness bound
# master diagnosis uses for coverage (an enabled job with no ok run inside this window = a coverage miss).
SCRAPE_SEED: List[Dict[str, Any]] = [
    ("screener_fundamentals", "Screener fundamentals (quarters/P&L/BS/CF/ratios)", "screener.in company page", "fundamentals_history", "T+1 nightly (reported) + Sat 10:00 retry + 4 season sweeps + quarterly shareholding", 30, "ops_metrics_pipeline+fundamentals_scraper", "quarters section scraped_at vs earnings reported date"),
    ("screener_docs", "Screener Documents/Concalls -> doc_texts", "screener.in Documents", "doc_texts+doc_registry", "T+1 staging + on-demand backfill", 30, "ops_metrics_pipeline", "failed_fetch count + staged=0 alarm"),
    ("screener_earnings_calendar", "Earnings calendar lifecycle", "screener.in", "earnings_calendar", "daily 06:15/05:10 IST", 36, "admin_data._earnings_lifecycle", "last_updated < 36h"),
    ("corporate_actions", "NSE/BSE corporate actions", "NSE/BSE + screener", "corp_actions", "daily (incl weekends) sharded", 30, "nightly ca_sweep", "ca scrape ops_log freshness"),
    ("yahoo_eod", "Yahoo EOD OHLC sync", "Yahoo Finance", "raw_prices", "weekdays 15:35 IST", 30, "scheduler._bg_yahoo_daily_sync", "latest raw_prices price_date = last trading day"),
    ("fo_bhavcopy_oi", "NSE F&O bhavcopy EOD OI", "nsearchives.nseindia.com", "fo_eod+futures_basis", "weekdays 23:00 IST", 30, "nse_fo_eod", "fo_eod rows for last trading day"),
    ("fo_ban_list", "NSE F&O ban list", "NSE", "fo_ban", "weekdays 08:45 IST", 30, "scheduler._bg_fo_ban_fetch", "fo_ban last fetch < 30h on trading day"),
    ("stock_news", "Stock news RSS", "RSS feeds", "news_articles", "daily ~00:30 IST", 30, "scheduler._bg_fetch_stock_news", "news_articles freshness"),
    ("market_news", "Market news RSS (domestic+global)", "RSS feeds", "market_news", "daily 05:20 + market-hours", 30, "scheduler._bg_fetch_market_news", "market_news freshness"),
    ("global_indices", "Global indices EOD/intraday", "Yahoo/global", "global_indices", "daily 05:15 + intraday", 30, "scheduler._bg_fetch_global", "global_indices freshness"),
    ("fyers_feed", "Fyers live 5-min feed (worker)", "Fyers API", "intraday_prices", "5-min market hours (worker truthful-friendship)", 1, "worker/fyers_feed", "latest tick within market session"),
    ("mf_pipeline", "MF/AMC scheme data", "AMFI/MC", "mf_schemes+mf_holdings", "weekly/on-demand (founder call pending)", 200, "mf_pipeline", "mf_schemes freshness"),
    ("mc_ops_metrics", "MoneyControl ops-metrics docs", "moneycontrol", "sector_ops_metrics+doc_texts", "weekly detect (Sat 06:10) + CC drain", 200, "ops_metrics_pipeline", "ops_metrics_t1_queue depth"),
]

# ~scheduler jobs registered from scheduler.py (cadence as wired; watchdog = the freshness/skip rule).
SCHEDULE_SEED: List[Dict[str, Any]] = [
    ("signal_writer", "5-min market hours 09:15-15:30", "v8_signal_writer", "live-signal freshness within session"),
    ("oi_snapshot", "5-min market hours", "scheduler._bg_oi_snapshot", "oi snapshot freshness"),
    ("guardian_tick", "5-min market hours", "feed_guardian", "per-leg detect/repair"),
    ("guardian_offhours", "off-hours cadence", "feed_guardian", "off-hours integrity"),
    ("v14_cycle", "market-hours cycle", "scheduler._bg_v14_cycle", "v14 freshness"),
    ("v8_paper_exit", "market-hours live exit pass", "scheduler._bg_v8_paper_exit", "paper exit pass ran"),
    ("v10_tick", "market-hours tick", "scheduler._bg_v10_tick", "v10 freshness"),
    ("pcr_intraday", "market-hours", "scheduler._bg_pcr_intraday", "pcr intraday freshness"),
    ("tc_lite", "09:30-15:15", "scheduler._bg_tc_lite", "tc lite freshness"),
    ("smartgain_mtm", "market-hours", "scheduler._bg_smartgain_mtm", "smartgain mtm freshness"),
    ("qb_intraday_mark", "market-hours", "scheduler._bg_qb_intraday_mark", "qb mark freshness"),
    ("intraday_scan", "15-min 09:30-15:15", "scheduler._bg_intraday_scan", "intraday_watchlist freshness"),
    ("tc_scanner", "15-min 09:30-15:15", "tc_scanner_endpoints", "tc_scanner_holds freshness"),
    ("gate_rebalance", "weekdays 15:20", "scheduler._bg_gate_rebalance", "over-slot close ran"),
    ("yahoo_daily_sync", "weekdays 15:35", "scheduler._bg_yahoo_daily_sync", "raw_prices last trading day"),
    ("tc_scanner_eod", "weekdays ~15:35", "scheduler._bg_tc_scanner_eod", "eod sweep ran"),
    ("heal_intraday", "weekdays 15:40", "scheduler._bg_heal_intraday", "session gaps healed"),
    ("v8_eod", "daily 15:45", "v8_engine", "eod engine ran"),
    ("adr_pcr", "daily 15:50", "scheduler._bg_adr_pcr", "adr/pcr freshness"),
    ("fetch_global", "daily 05:15", "scheduler._bg_fetch_global", "global indices freshness"),
    ("fetch_market_news", "daily 05:20 + market-hours", "scheduler._bg_fetch_market_news", "market_news freshness"),
    ("fetch_stock_news", "daily ~00:30", "scheduler._bg_fetch_stock_news", "stock news freshness"),
    # cc#847: fetch_position_news retired with the Position News feature.
    ("tag_news", "daily 05:30", "scheduler._bg_tag_news", "news tagged"),
    ("fetch_global_intraday", "market-hours", "scheduler._bg_fetch_global_intraday", "global intraday freshness"),
    ("fu_sync", "Mon 05:40", "scheduler._bg_fu_sync", "futures universe sync"),
    ("earnings_refresh", "daily 05:10", "scheduler._bg_earnings_refresh", "earnings_calendar < 36h"),
    ("fo_ban_fetch", "weekdays 08:45", "scheduler._bg_fo_ban_fetch", "fo_ban < 30h"),
    ("fo_eod", "weekdays 23:00", "nse_fo_eod", "fo_eod last trading day"),
    ("ca_daily_note", "daily 09:00", "scheduler._bg_ca_daily_note", "CA morning note written"),
    ("master_watchdog_note", "daily 09:05", "scheduler._bg_master_watchdog_note", "watchdog note written"),
    ("ca_sweep_daily", "daily (incl weekends)", "scheduler._bg_ca_sweep_daily", "CA sweep ran"),
    ("ca_weekly_scan", "Sun 05:30", "scheduler._bg_ca_weekly_scan", "distortion scan ran"),
    ("weekly_ops_metrics_queue", "Sat 06:10", "scheduler._bg_weekly_ops_metrics_queue", "ops-metrics detect ran"),
    ("premarket_writer_check", "weekdays 09:10", "scheduler._premarket_writer_check", "writer readiness ok"),
    ("t1_refresh", "T+1 nightly ~05:40", "ops_metrics_pipeline.run_t1_refresh", "reported symbols quarters landed"),
    ("saturday_retry", "Sat 10:00", "ops_metrics_pipeline.run_saturday_retry", "failed t1 rows retried"),
]


def seed_registries(cur) -> Dict[str, int]:
    ensure_tables(cur)
    for k, name, source, tgt, freq, win, owner, hc in SCRAPE_SEED:
        cur.execute("""INSERT INTO scrape_registry (job_key, name, source, target_table, freq_desc,
                         freq_window_hours, owner_module, health_check)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (job_key) DO UPDATE SET name=EXCLUDED.name, source=EXCLUDED.source,
                         target_table=EXCLUDED.target_table, freq_desc=EXCLUDED.freq_desc,
                         freq_window_hours=EXCLUDED.freq_window_hours, owner_module=EXCLUDED.owner_module,
                         health_check=EXCLUDED.health_check""",
                    (k, name, source, tgt, freq, win, owner, hc))
    for k, cadence, owner, wd in SCHEDULE_SEED:
        cur.execute("""INSERT INTO schedule_registry (job_key, cadence, owner_module, watchdog_rule)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (job_key) DO UPDATE SET cadence=EXCLUDED.cadence,
                         owner_module=EXCLUDED.owner_module, watchdog_rule=EXCLUDED.watchdog_rule""",
                    (k, cadence, owner, wd))
    return {"scrape_registry": len(SCRAPE_SEED), "schedule_registry": len(SCHEDULE_SEED)}


# ── plane 3: PURE-READER diagnosis (coverage / failures / drift) ─────────────────────────────────
def control_plane_diagnosis(cur) -> Dict[str, Any]:
    ensure_tables(cur)
    # COVERAGE: each enabled scrape job with an ok run inside its freq_window_hours
    cur.execute("""SELECT r.job_key, r.freq_window_hours,
                     (SELECT MAX(started_at) FROM job_runs j WHERE j.job_key=r.job_key AND j.status='ok') AS last_ok
                   FROM scrape_registry r WHERE r.enabled ORDER BY r.job_key""")
    coverage_miss = []
    for jk, win, last_ok in cur.fetchall():
        win = float(win) if win is not None else 30.0
        if last_ok is None:
            coverage_miss.append({"job_key": jk, "last_ok": None, "window_h": win})
        else:
            cur.execute("SELECT EXTRACT(EPOCH FROM (NOW()-%s))/3600.0", (last_ok,))
            age = float(cur.fetchone()[0])
            if age > win:
                coverage_miss.append({"job_key": jk, "last_ok": str(last_ok), "age_h": round(age, 1), "window_h": win})
    # FAILURES: unreviewed scrape_review comments
    cur.execute("""SELECT COUNT(*) FROM ops_log WHERE category='scrape_review'
                   AND COALESCE((details->>'reviewed_at'),'')='' """)
    unreviewed = int(cur.fetchone()[0])
    cur.execute("""SELECT title, details->>'job_key', details->>'reason', session_ts
                   FROM ops_log WHERE category='scrape_review' AND COALESCE((details->>'reviewed_at'),'')=''
                   ORDER BY id DESC LIMIT 20""")
    failures = [{"title": t, "job_key": jk, "reason": rs, "at": str(ts)} for t, jk, rs, ts in cur.fetchall()]
    # DRIFT: job_runs job_keys not in either registry; and registered jobs with zero runs ever
    cur.execute("""SELECT DISTINCT job_key FROM job_runs
                   WHERE job_key NOT IN (SELECT job_key FROM scrape_registry)
                     AND job_key NOT IN (SELECT job_key FROM schedule_registry)""")
    unregistered = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT job_key FROM schedule_registry WHERE enabled
                   AND job_key NOT IN (SELECT DISTINCT job_key FROM job_runs) ORDER BY job_key""")
    silent = [r[0] for r in cur.fetchall()]
    return {"coverage_miss": coverage_miss, "coverage_miss_n": len(coverage_miss),
            "unreviewed_failures": unreviewed, "recent_failures": failures,
            "drift_unregistered": unregistered, "drift_silent_registered": silent}


@control_plane_router.on_event("startup")
async def _seed_on_startup():
    # idempotent (ON CONFLICT) — keeps the registries in sync with the seed lists on every deploy.
    try:
        with _conn() as conn, conn.cursor() as cur:
            seed_registries(cur)
            conn.commit()
    except Exception as e:
        log.warning(f"control_plane startup seed failed: {e}")


@control_plane_router.post("/api/ops/control-plane/seed")
def seed_endpoint(x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    with _conn() as conn, conn.cursor() as cur:
        res = seed_registries(cur)
        conn.commit()
    return {"seeded": res}


@control_plane_router.get("/api/ops/control-plane/diagnosis")
def diagnosis_endpoint():
    with _conn() as conn, conn.cursor() as cur:
        return control_plane_diagnosis(cur)


@control_plane_router.get("/api/ops/control-plane/findings")
def findings_endpoint():
    """cc#693: the Scheduler-tab CRITICAL LOGS / FINDINGS feed — unreviewed scrape_review, recent
    failed/partial runs (48h), and drift — newest first, for the web panel. Founder marks reviewed via
    POST .../review (sets details.reviewed_at)."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT id, title, details->>'job_key', details->>'reason', details->>'scope', session_ts
                       FROM ops_log WHERE category='scrape_review' AND COALESCE((details->>'reviewed_at'),'')=''
                       ORDER BY id DESC LIMIT 100""")
        review = [{"id": i, "title": t, "job_key": jk, "reason": rs, "scope": sc, "at": str(ts)}
                  for i, t, jk, rs, sc, ts in cur.fetchall()]
        cur.execute("""SELECT job_key, status, error, started_at, finished_at FROM job_runs
                       WHERE status IN ('partial','failed') AND started_at > NOW() - INTERVAL '48 hours'
                       ORDER BY started_at DESC LIMIT 100""")
        recent = [{"job_key": jk, "status": st, "error": er, "started_at": str(s), "finished_at": str(f)}
                  for jk, st, er, s, f in cur.fetchall()]
        diag = control_plane_diagnosis(cur)
    return {"unreviewed_review": review, "recent_failed_runs": recent,
            "drift": {"unregistered": diag["drift_unregistered"], "silent": diag["drift_silent_registered"]},
            "coverage_miss": diag["coverage_miss"]}


@control_plane_router.post("/api/ops/control-plane/review")
def mark_reviewed(log_id: int, x_admin_token: Optional[str] = Header(None)):
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "invalid admin token")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE ops_log SET details = jsonb_set(COALESCE(details,'{}'::jsonb),
                         '{reviewed_at}', to_jsonb(NOW()::text)) WHERE id=%s AND category='scrape_review'""", (log_id,))
        conn.commit()
    return {"reviewed": log_id}

"""
engine_watchdog.py — cc#599 ENGINE WATCHDOG (ENGINE_WATCHDOG_V1, session_log 7129).
================================================================================
Daily OUTCOME/data-freshness audit of EVERY scheduled job — catches the "active but doing nothing"
failure the scheduler_master drift audit can't see (e.g. bg_ops_metrics_t1: ticking, 0 rows since
19-Jul). Complements SCHEDULER_MASTER_RULE (that = registration drift; this = outcome drift).

DETECT + WRITE + SUGGEST only — NO auto-remediation in v1 (founder-locked). Gaps land in
watchdog_gaps; Claude-web reads status=open at session start and triggers the fix per
suggested_action. A gap auto-resolves the next time the check comes back healthy.

Two tables (CC owns build + seed; Claude-web owns predicate refinement):
  * watchdog_checks — config registry: one row per health predicate.
  * watchdog_gaps   — findings, UNIQUE(job_name,check_id): a persistent breach bumps last_seen
                      rather than duplicating; auto-resolves when healthy.

Robustness: a DATA check whose query errors (bad table/column in a seed predicate) is logged as
'check_error' and NEVER creates a gap — "can't verify" is not "breached", so a misconfigured seed
can't raise false alarms; Claude-web tunes the predicate. Auto-register backstop: every
scheduler_master job with no watchdog_checks row gets a DEFAULT tick-only check
(notes=AUTO_REGISTERED), so coverage scales with the system by construction (rule 7130).
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter

log = logging.getLogger("scorr.engine_watchdog")
router = APIRouter(tags=["engine_watchdog"])

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist():
    return datetime.now(IST).replace(tzinfo=None)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watchdog_checks (
    check_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    check_type TEXT NOT NULL DEFAULT 'tick',   -- data | tick | both
    output_table TEXT,
    ts_column TEXT,
    scope_filter TEXT,          -- nullable WHERE on output_table (no leading WHERE)
    precondition_sql TEXT,      -- nullable: check only applies when this returns >=1 row
    cadence TEXT,               -- daily_trading | daily | weekly | quarterly_window:Jul,Oct,Jan,Apr | 5min_trading | season
    sla_hours NUMERIC,
    severity TEXT DEFAULT 'medium',
    suggested_action TEXT,
    active BOOLEAN DEFAULT TRUE,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS watchdog_gaps (
    id SERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    check_id TEXT NOT NULL,
    severity TEXT,
    observed TEXT,
    expected TEXT,
    suggested_action TEXT,
    status TEXT DEFAULT 'open',   -- open | acknowledged | fixing | resolved
    first_seen TIMESTAMP DEFAULT NOW(),
    last_seen TIMESTAMP DEFAULT NOW(),
    detected_at TIMESTAMP DEFAULT NOW(),
    resolved_at TIMESTAMP,
    resolution_note TEXT,
    UNIQUE(job_name, check_id)
);
CREATE INDEX IF NOT EXISTS idx_watchdog_gaps_status ON watchdog_gaps(status);
"""

# ── seed: the ~13 data checks (session_log 7129 seed_checks_data). Best-effort predicates; a query
# error is logged, never a false gap — Claude-web refines predicates (their ownership). ────────────
SEED_CHECKS = [
    {"check_id": "ops_metrics_t1_data", "job_name": "bg_ops_metrics_t1", "check_type": "data",
     "output_table": "sector_ops_metrics", "ts_column": "created_at", "scope_filter": None,
     "precondition_sql": "SELECT 1 FROM earnings_calendar WHERE status='reported' AND ex_date >= CURRENT_DATE-3 LIMIT 1",
     "cadence": "daily", "sla_hours": 48, "severity": "high",
     "suggested_action": "sector_ops_metrics stale while companies reported: confirm T+1 stages doc_texts, then run CC extraction (doc_texts -> sector_ops_metrics). App anthropic retired 19-Jul (cc#595)."},
    {"check_id": "ops_metrics_saturday_data", "job_name": "bg_ops_metrics_saturday", "check_type": "data",
     "output_table": "sector_ops_metrics", "ts_column": "created_at", "scope_filter": None,
     "precondition_sql": None, "cadence": "weekly", "sla_hours": 192, "severity": "medium",
     "suggested_action": "weekly ops-metrics retry produced nothing in 8d — check the Saturday queue + CC extraction."},
    {"check_id": "shareholding_quarterly_data", "job_name": "bg_shareholding_quarterly", "check_type": "data",
     "output_table": "fundamentals_history", "ts_column": "scraped_at", "scope_filter": "section='shareholding'",
     "precondition_sql": None, "cadence": "quarterly_window:Jul,Oct,Jan,Apr", "sla_hours": 240, "severity": "medium",
     "suggested_action": "no new shareholding quarter after the last-week window — check bg_shareholding_quarterly + arm run_shareholding_quarterly."},
    {"check_id": "yahoo_eod_data", "job_name": "bg_yahoo_daily_sync", "check_type": "data",
     "output_table": "raw_prices", "ts_column": "price_date", "scope_filter": None,
     "precondition_sql": None, "cadence": "daily_trading", "sla_hours": 30, "severity": "high",
     "suggested_action": "raw_prices missing the latest trading day — re-run yahoo EOD sync + heal_intraday."},
    {"check_id": "gvm_nightly_data", "job_name": "bg_gvm", "check_type": "data",
     "output_table": "gvm_scores", "ts_column": "score_date", "scope_filter": None,
     "precondition_sql": None, "cadence": "daily", "sla_hours": 30, "severity": "high",
     "suggested_action": "gvm_scores stale — re-run the nightly GVM job."},
    {"check_id": "pivots_data", "job_name": "bg_pivots", "check_type": "data",
     "output_table": "v8_paper_pivots", "ts_column": "computed_at", "scope_filter": None,
     "precondition_sql": None, "cadence": "daily_trading", "sla_hours": 30, "severity": "medium",
     "suggested_action": "pivots stale — re-run bg_pivots."},
    {"check_id": "news_polish_data", "job_name": "bg_fetch_stock_news", "check_type": "data",
     "output_table": "polished_news", "ts_column": "polished_at", "scope_filter": None,
     "precondition_sql": None, "cadence": "daily", "sla_hours": 30, "severity": "low",
     "suggested_action": "polished_news stale — check the news fetch + polish chain."},
    {"check_id": "fyers_feed_data", "job_name": "bg_signal_writer", "check_type": "data",
     "output_table": "intraday_prices", "ts_column": "ts",
     "scope_filter": "timeframe='5m' AND source IN ('fyers_eq','fyers_fut')",
     "precondition_sql": None, "cadence": "5min_trading", "sla_hours": 0.5, "severity": "critical",
     "suggested_action": "feed 5-min bars stale during market — check the fyers worker (mint-once / fut-verify) + heal_intraday."},
    {"check_id": "v8_writer_data", "job_name": "bg_signal_writer", "check_type": "data",
     "output_table": "v8_metrics", "ts_column": "score_date", "scope_filter": None,
     "precondition_sql": None, "cadence": "daily_trading", "sla_hours": 30, "severity": "critical",
     "suggested_action": "v8_metrics missing today — the live writer is not producing; check signal_writer_crash ops_log."},
    # cc#618 Section D (1) writer tick freshness — v8_metrics.computed_at stale >10min DURING market
    # hours (the 09:35-death class: rows exist for today but the live writer stalled mid-session).
    # Evaluated every 15min in market hours by run_market_checks (not the daily 08:45 pass).
    {"check_id": "v8_writer_tick", "job_name": "bg_signal_writer", "check_type": "data",
     "output_table": "v8_metrics", "ts_column": "computed_at", "scope_filter": None,
     "precondition_sql": None, "cadence": "market_15min", "sla_hours": 0.17, "severity": "critical",
     "suggested_action": "v8_metrics.computed_at stale >10min during market hours — the live writer stalled mid-session (09:35 death class); check the scheduler _check_watchdog restart + signal_writer_crash ops_log."},
    # cc#618 Section D (3) ops-extraction yield — no new sector_ops_metrics in >24h on a trading day
    # while companies have reported (the doc-fetch/extraction cycle silently produced nothing).
    {"check_id": "ops_extraction_yield", "job_name": "bg_ops_metrics_t1", "check_type": "data",
     "output_table": "sector_ops_metrics", "ts_column": "created_at", "scope_filter": None,
     "precondition_sql": "SELECT 1 FROM earnings_calendar WHERE status='reported' AND ex_date >= CURRENT_DATE-5 LIMIT 1",
     "cadence": "daily_trading", "sla_hours": 24, "severity": "high",
     "suggested_action": "ops-extraction produced no new sector_ops_metrics in >24h while companies reported — check the doc-fetch/extraction cycle (cc#595/596) + the CC extraction batch off doc_texts."},
    {"check_id": "position_news_data", "job_name": "bg_fetch_position_news", "check_type": "data",
     "output_table": "position_news", "ts_column": "fetched_at", "scope_filter": None,
     "precondition_sql": "SELECT 1 FROM (SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL) p LIMIT 1",
     "cadence": "daily", "sla_hours": 30, "severity": "high",
     "suggested_action": "position_news stale while positions are open — the revived 3-slot fetch (07:35/13:35/19:35) stalled; check bg_fetch_position_news + position_news.fetch_and_alert (cc#611; 06-Jul unwiring class)."},
    {"check_id": "fundamentals_t1_data", "job_name": "bg_ops_metrics_t1", "check_type": "data",
     "output_table": "fundamentals_history", "ts_column": "scraped_at", "scope_filter": "section<>'shareholding'",
     "precondition_sql": "SELECT 1 FROM earnings_calendar WHERE status='reported' AND ex_date >= CURRENT_DATE-3 LIMIT 1",
     "cadence": "daily", "sla_hours": 72, "severity": "medium",
     "suggested_action": "fundamentals not re-scraped for reported companies — check the T+1 fundamentals path."},
]

_TICK_TOLERANCE_HOURS = {"daily": 26, "daily_trading": 30, "weekly": 192, "5min_trading": 0.5,
                         "quarterly_window:Jul,Oct,Jan,Apr": 24 * 100, "season": 24 * 100, None: 26}


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _seed(conn):
    with conn.cursor() as cur:
        for c in SEED_CHECKS:
            cur.execute("""INSERT INTO watchdog_checks
                (check_id, job_name, check_type, output_table, ts_column, scope_filter, precondition_sql,
                 cadence, sla_hours, severity, suggested_action, active, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'SEED')
                ON CONFLICT (check_id) DO UPDATE SET
                  job_name=EXCLUDED.job_name, check_type=EXCLUDED.check_type, output_table=EXCLUDED.output_table,
                  ts_column=EXCLUDED.ts_column, scope_filter=EXCLUDED.scope_filter,
                  precondition_sql=EXCLUDED.precondition_sql, cadence=EXCLUDED.cadence,
                  sla_hours=EXCLUDED.sla_hours, severity=EXCLUDED.severity,
                  suggested_action=EXCLUDED.suggested_action""",
                        (c["check_id"], c["job_name"], c["check_type"], c["output_table"], c["ts_column"],
                         c["scope_filter"], c["precondition_sql"], c["cadence"], c["sla_hours"],
                         c["severity"], c["suggested_action"]))
    conn.commit()


def _auto_register_backstop(conn):
    """Every scheduler_master job with no watchdog_checks row gets a DEFAULT tick-only check
    (notes=AUTO_REGISTERED) so nothing is uncovered and future jobs self-cover (rule 7130)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT sm.job_name, sm.cadence_human FROM scheduler_master sm
                       WHERE sm.active IS TRUE
                         AND NOT EXISTS (SELECT 1 FROM watchdog_checks wc WHERE wc.job_name=sm.job_name)""")
        for job, cadence_human in cur.fetchall():
            cur.execute("""INSERT INTO watchdog_checks
                (check_id, job_name, check_type, cadence, sla_hours, severity, suggested_action, active, notes)
                VALUES (%s,%s,'tick','daily',30,'low',%s,TRUE,'AUTO_REGISTERED')
                ON CONFLICT (check_id) DO NOTHING""",
                        (f"{job}_tick", job,
                         "job has not ticked within its cadence — check scheduler_master last_run_at/last_status."))
    conn.commit()


def _is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    try:
        import nse_holidays
        return not nse_holidays.is_holiday(d)
    except Exception:
        return True


def _cadence_due(cadence: str, now: datetime) -> bool:
    """Whether a check should be EVALUATED now (else skip — no gap). Trading-day jobs only on/after a
    trading day; quarterly-window jobs only inside/after their last-week window; season jobs skipped."""
    if not cadence:
        return True
    if cadence == "market_15min":
        # cc#618 Section D: only DURING market hours on a trading day (09:15-15:30 IST).
        mins = now.hour * 60 + now.minute
        return _is_trading_day(now.date()) and (555 <= mins <= 930)
    if cadence in ("daily_trading", "5min_trading"):
        return _is_trading_day(now.date())
    if cadence == "5min_trading":
        return _is_trading_day(now.date())
    if cadence.startswith("quarterly_window"):
        months = {"Jul": 7, "Oct": 10, "Jan": 1, "Apr": 4}
        want = {months[m] for m in cadence.split(":", 1)[1].split(",")} if ":" in cadence else set()
        return now.month in want and now.day >= 26   # only after the last-week filing window
    if cadence == "season":
        return False   # season sweeps are month-locked; tick-check covers liveness
    return True


def _scalar(cur, sql):
    cur.execute(sql)
    r = cur.fetchone()
    return r[0] if r else None


def _eval_data(cur, c, now):
    """Returns (breached, observed, expected) or (None, reason, None) if not applicable/unverifiable."""
    if c.get("precondition_sql"):
        try:
            cur.execute(c["precondition_sql"])
            if cur.fetchone() is None:
                return None, "precondition_not_met", None   # not applicable now
        except Exception as e:
            return None, f"precondition_error:{str(e)[:80]}", None
    tbl, col = c.get("output_table"), c.get("ts_column")
    if not tbl or not col:
        return None, "no_data_predicate", None
    where = f" WHERE {c['scope_filter']}" if c.get("scope_filter") else ""
    try:
        max_ts = _scalar(cur, f"SELECT MAX({col}) FROM {tbl}{where}")
    except Exception as e:
        return None, f"query_error:{str(e)[:80]}", None   # can't verify != breached
    sla = float(c.get("sla_hours") or 30)
    expected = f"MAX({tbl}.{col}) within {sla}h"
    if max_ts is None:
        return True, f"no rows in {tbl}{where}", expected
    # normalize to naive datetime for age
    if isinstance(max_ts, date) and not isinstance(max_ts, datetime):
        max_dt = datetime(max_ts.year, max_ts.month, max_ts.day)
    else:
        max_dt = max_ts.replace(tzinfo=None) if getattr(max_ts, "tzinfo", None) else max_ts
    age_h = (now - max_dt).total_seconds() / 3600.0
    return (age_h > sla), f"latest {max_dt} ({age_h:.1f}h old)", expected


def _eval_tick(cur, c, now):
    try:
        cur.execute("SELECT last_run_at, last_status FROM scheduler_master WHERE job_name=%s", (c["job_name"],))
        r = cur.fetchone()
    except Exception as e:
        return None, f"query_error:{str(e)[:80]}", None
    if not r:
        return None, "not_in_scheduler_master", None
    last_run, last_status = r
    tol = _TICK_TOLERANCE_HOURS.get(c.get("cadence"), 26)
    expected = f"ticked within {tol}h and status not error"
    if last_run is None:
        return True, "never ran (last_run_at NULL)", expected
    last_dt = last_run.replace(tzinfo=None) if getattr(last_run, "tzinfo", None) else last_run
    age_h = (now - last_dt).total_seconds() / 3600.0
    if last_status == "error":
        return True, f"last_status=error at {last_dt}", expected
    if age_h > tol:
        return True, f"last ran {last_dt} ({age_h:.1f}h ago > {tol}h)", expected
    return False, f"ticked {last_dt} ({age_h:.1f}h ago), status={last_status}", expected


def _upsert_gap(cur, c, observed, expected, now):
    cur.execute("""INSERT INTO watchdog_gaps
        (job_name, check_id, severity, observed, expected, suggested_action, status, first_seen,
         last_seen, detected_at)
        VALUES (%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)
        ON CONFLICT (job_name, check_id) DO UPDATE SET
          severity=EXCLUDED.severity, observed=EXCLUDED.observed, expected=EXCLUDED.expected,
          suggested_action=EXCLUDED.suggested_action, last_seen=EXCLUDED.last_seen,
          status=CASE WHEN watchdog_gaps.status='resolved' THEN 'open' ELSE watchdog_gaps.status END,
          resolved_at=CASE WHEN watchdog_gaps.status='resolved' THEN NULL ELSE watchdog_gaps.resolved_at END""",
                (c["job_name"], c["check_id"], c.get("severity") or "medium", observed, expected,
                 c.get("suggested_action"), now, now, now))


def _resolve_gap(cur, c, observed, now):
    cur.execute("""UPDATE watchdog_gaps SET status='resolved', resolved_at=%s, last_seen=%s,
                     resolution_note=%s
                   WHERE job_name=%s AND check_id=%s AND status<>'resolved'""",
                (now, now, f"auto-resolved: {observed}", c["job_name"], c["check_id"]))


_REGISTRY_BASKETS = ("buy_reversal", "sell_reversal", "sell_momentum", "buy_momentum")


def _check_filter_registry_parity(conn, now) -> dict:
    """cc#607 Phase A parity guard: the BASKET_FILTERS registry (v8_signal_writer.py) is the single
    source for each basket's gates. Compare its keys against the keys the live handler actually wrote
    to v8_funnel_counts on the latest score_date — if a handler ever adds/drops a gate without the
    registry (or vice-versa), the funnel/pass-count/i-button would silently drift. Drift -> open a
    watchdog gap (DETECT-only, per cc#599 no-auto-remediation). Healthy/absent -> auto-resolve."""
    checked = drift = 0
    try:
        from v8_signal_writer import basket_funnel_keys
    except Exception as e:
        log.warning(f"registry parity: cannot import registry: {e}")
        return {"checked": 0, "drift": 0, "error": str(e)[:80]}
    with conn.cursor() as cur:
        for basket in _REGISTRY_BASKETS:
            reg_keys = basket_funnel_keys(basket)
            if not reg_keys:
                continue
            cur.execute("""SELECT counts FROM v8_funnel_counts WHERE basket=%s
                           ORDER BY score_date DESC LIMIT 1""", (basket,))
            r = cur.fetchone()
            if not r or not r[0]:
                continue   # no live funnel yet -> can't verify, not drift
            counts = r[0] if isinstance(r[0], dict) else json.loads(r[0])
            live_keys = {k for k in counts.keys() if not k.startswith("_")}
            checked += 1
            c = {"job_name": "bg_signal_writer", "check_id": f"filter_registry_parity_{basket}",
                 "severity": "high",
                 "suggested_action": f"BASKET_FILTERS['{basket}'] keys drifted from the live "
                                     f"v8_funnel_counts keys — reconcile the registry in "
                                     f"v8_signal_writer.py with the handler so funnel/pass-count/"
                                     f"i-button stay single-sourced (cc#607)."}
            if live_keys != reg_keys:
                missing = sorted(reg_keys - live_keys)   # in registry, not written by handler
                extra   = sorted(live_keys - reg_keys)   # written by handler, not in registry
                observed = f"registry={sorted(reg_keys)} live={sorted(live_keys)} missing={missing} extra={extra}"
                _upsert_gap(cur, c, observed, "registry keys == live funnel keys", now)
                drift += 1
            else:
                _resolve_gap(cur, c, f"registry matches live funnel ({len(reg_keys)} gates)", now)
    conn.commit()
    return {"checked": checked, "drift": drift}


def _check_result_analysis_staleness(conn, now) -> dict:
    """cc#618 Section D (4): backstop behind the Section B auto-wire. Count announced tickers whose
    Result Analysis card PREDATES the announcement (last_result_analysis_updated < ex_date) and it has
    been >48h since ex_date — i.e. the daily T+1 auto-regen (+ weekly sweep) missed one. Breach if any."""
    c = {"job_name": "bg_result_corner_verify", "check_id": "result_analysis_staleness",
         "severity": "high",
         "suggested_action": "announced tickers still serve a pre-announcement Result Analysis card >48h "
                             "later — the Section B auto-regen (daily T+1 wire / weekly sweep) missed them; "
                             "regenerate result_analysis for the listed tickers (result_analysis_gen.regenerate)."}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH ann AS (
                    SELECT UPPER(ticker) AS sym, MAX(ex_date) AS ex_date
                    FROM earnings_calendar
                    WHERE status='reported' AND ex_date <= CURRENT_DATE - 2 AND ticker IS NOT NULL
                    GROUP BY UPPER(ticker))
                SELECT COUNT(*), array_agg(a.sym ORDER BY a.sym)
                FROM ann a JOIN input_raw i ON i.nse_code = a.sym
                WHERE i.result_analysis IS NOT NULL
                  AND i.last_result_analysis_updated IS NOT NULL
                  AND i.last_result_analysis_updated < a.ex_date""")
            row = cur.fetchone()
            n = int(row[0] or 0)
            syms = row[1] or []
            if n > 0:
                _upsert_gap(cur, c, f"{n} announced tickers with pre-ex_date analysis >48h: {syms[:20]}",
                            "0 stale announced cards", now)
            else:
                _resolve_gap(cur, c, "no stale announced result-analysis cards", now)
        conn.commit()
        return {"stale": n}
    except Exception as e:
        log.warning(f"result_analysis staleness check: {e}")
        return {"stale": 0, "error": str(e)[:80]}


def run_watchdog(conn) -> dict:
    """Seed + auto-register backstop + evaluate every active check. Breach -> upsert open gap;
    healthy -> auto-resolve. Summary -> ops_log. Returns counts."""
    ensure_tables(conn)
    _seed(conn)
    _auto_register_backstop(conn)
    now = _now_ist()
    evaluated = breached = resolved = skipped = errored = 0
    open_gaps = []
    with conn.cursor() as cur:
        cur.execute("""SELECT check_id, job_name, check_type, output_table, ts_column, scope_filter,
                              precondition_sql, cadence, sla_hours, severity, suggested_action
                       FROM watchdog_checks WHERE active IS TRUE""")
        cols = [d[0] for d in cur.description]
        checks = [dict(zip(cols, r)) for r in cur.fetchall()]

    for c in checks:
        if not _cadence_due(c.get("cadence"), now):
            skipped += 1
            continue
        with conn.cursor() as cur:
            breach = None
            observed = expected = None
            if c["check_type"] in ("data", "both"):
                breach, observed, expected = _eval_data(cur, c, now)
            if (breach in (None, False)) and c["check_type"] in ("tick", "both"):
                tb, tobs, texp = _eval_tick(cur, c, now)
                # tick supplements data: only override when data was inapplicable/healthy
                if tb is not None:
                    breach, observed, expected = tb, (observed or tobs), (expected or texp)
            if breach is None:
                errored += 1   # unverifiable/not-applicable -> never a false gap
                conn.commit()
                continue
            evaluated += 1
            if breach:
                _upsert_gap(cur, c, observed, expected, now)
                breached += 1
                open_gaps.append(f"{c['job_name']}/{c['check_id']}: {observed}")
            else:
                _resolve_gap(cur, c, observed, now)
                resolved += 1
            conn.commit()

    # cc#607 Phase A: basket filter-registry ↔ live-funnel-keys parity guard (drift -> gap).
    try:
        parity = _check_filter_registry_parity(conn, now)
    except Exception as e:
        log.warning(f"registry parity check failed: {e}")
        parity = {"checked": 0, "drift": 0, "error": str(e)[:80]}
    # cc#618 Section D (4): announced-vs-analysis staleness backstop (nightly).
    try:
        ra_stale = _check_result_analysis_staleness(conn, now)
    except Exception as e:
        log.warning(f"result_analysis staleness check failed: {e}")
        ra_stale = {"stale": 0, "error": str(e)[:80]}

    summary = {"evaluated": evaluated, "breached": breached, "auto_resolved": resolved,
               "skipped_not_due": skipped, "unverifiable": errored, "checks_total": len(checks),
               "registry_parity": parity, "result_analysis_staleness": ra_stale,
               "open_sample": open_gaps[:15], "ist": str(now)}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM watchdog_gaps WHERE status='open'")
            summary["open_gaps_total"] = cur.fetchone()[0]
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'engine_watchdog', 'ENGINE_WATCHDOG_RUN', %s::jsonb)""",
                        (json.dumps(summary, default=str),))
        conn.commit()
    except Exception as e:
        log.warning(f"engine_watchdog summary log failed: {e}")
    log.info(f"engine_watchdog: {summary}")
    return summary


def run_watchdog_conn() -> dict:
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        return run_watchdog(conn)
    finally:
        conn.close()


def run_market_checks(conn) -> dict:
    """cc#618 Section D: market-hours-only checks (cadence market_15min: the v8_writer tick freshness).
    Scheduled every 15min during market hours — the daily 08:45 pass skips these (off-market). Same
    gap/alert/auto-resolve mechanics; only logs ops_log on a breach (no 15-min healthy spam)."""
    ensure_tables(conn)
    _seed(conn)
    now = _now_ist()
    evaluated = breached = resolved = 0
    open_gaps = []
    with conn.cursor() as cur:
        cur.execute("""SELECT check_id, job_name, check_type, output_table, ts_column, scope_filter,
                              precondition_sql, cadence, sla_hours, severity, suggested_action
                       FROM watchdog_checks WHERE active IS TRUE AND cadence='market_15min'""")
        cols = [d[0] for d in cur.description]
        checks = [dict(zip(cols, r)) for r in cur.fetchall()]
    for c in checks:
        if not _cadence_due(c.get("cadence"), now):
            continue
        with conn.cursor() as cur:
            breach, observed, expected = _eval_data(cur, c, now)
            if breach is None:
                conn.commit()
                continue
            evaluated += 1
            if breach:
                _upsert_gap(cur, c, observed, expected, now)
                breached += 1
                open_gaps.append(f"{c['job_name']}/{c['check_id']}: {observed}")
            else:
                _resolve_gap(cur, c, observed, now)
                resolved += 1
            conn.commit()
    summary = {"scope": "market_15min", "evaluated": evaluated, "breached": breached,
               "auto_resolved": resolved, "open_sample": open_gaps, "ist": str(now)}
    if breached:
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'engine_watchdog', 'WATCHDOG_MARKET_ALERT', %s::jsonb)""",
                            (json.dumps(summary, default=str),))
            conn.commit()
        except Exception as e:
            log.warning(f"market watchdog log: {e}")
    log.info(f"engine_watchdog market: {summary}")
    return summary


def run_market_checks_conn() -> dict:
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        return run_market_checks(conn)
    finally:
        conn.close()


@router.get("/api/watchdog/gaps")
def get_gaps(status: str = "open"):
    """Open watchdog gaps (Claude-web reads this at session start). status=open|all."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        with conn.cursor() as cur:
            ensure_tables(conn)
            if status == "all":
                cur.execute("""SELECT job_name, check_id, severity, status, observed, expected,
                                      suggested_action, first_seen, last_seen, resolved_at
                               FROM watchdog_gaps ORDER BY (status='open') DESC, severity, last_seen DESC""")
            else:
                cur.execute("""SELECT job_name, check_id, severity, status, observed, expected,
                                      suggested_action, first_seen, last_seen, resolved_at
                               FROM watchdog_gaps WHERE status=%s ORDER BY severity, last_seen DESC""", (status,))
            cols = [d[0] for d in cur.description]
            gaps = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"count": len(gaps), "status_filter": status, "gaps": gaps}
    finally:
        conn.close()

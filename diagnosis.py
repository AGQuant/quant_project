"""
Scorr — System Diagnosis
=========================
GET /api/diagnosis

Single-call full system health check across all subsystems.
Returns traffic-light status (green/yellow/red) per section + issues list.

Sections:
  1. data_feeds     — Fyers intraday, raw_prices, cmp_prices, global indices, ADR/PCR
  2. v8_engine      — metrics, signals, funnel, paper positions, market mood
  3. gvm            — scores, history, sector ratings
  4. quant_basket   — 4 baskets positions, EOD checker
  5. scheduler      — each job vs expected run window (IST)
  6. infrastructure — DB size, table count, version, GitHub

Thresholds:
  green  = all checks pass
  yellow = at least one warning (stale by 1 extra day, minor count mismatch)
  red    = at least one failure (missing data, stale >2 days, critical count wrong)
"""

from fastapi import APIRouter
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Any
import psycopg
import os
import logging

from nse_holidays import is_trading_day

log = logging.getLogger("scorr.diagnosis")
router = APIRouter(prefix="/api", tags=["diagnosis"])

IST = timezone(timedelta(hours=5, minutes=30))

# cc#146: market-hours gate shared by _section_data_feeds (was applying market-hours
# thresholds unconditionally -> false RED on every pre-market/off-hours run).
_GRACE_START      = 555   # 09:15 IST
_GRACE_END        = 565   # 09:25 IST — feed still warming up right after open
_MKT_END          = 930   # 15:30 IST
_CLOSE_BAND_START = 920   # 15:20 IST
_CLOSE_BAND_END   = 930   # 15:30 IST

def _market_state(now: datetime) -> str:
    """'market' | 'grace' | 'off'. NSE holidays (nse_holidays.py) count as off-hours."""
    if not is_trading_day(now.date()):
        return 'off'
    mins = now.hour * 60 + now.minute
    if _GRACE_START <= mins < _GRACE_END:
        return 'grace'
    if _GRACE_END <= mins <= _MKT_END:
        return 'market'
    return 'off'

def _closed_cleanly(ts) -> bool:
    """True if ts's time-of-day falls in the prior session's 15:20-15:30 close band —
    distinguishes a feed that shut down normally from one that died mid-session."""
    if ts is None:
        return False
    mins = ts.hour * 60 + ts.minute
    return _CLOSE_BAND_START <= mins <= _CLOSE_BAND_END

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

def _ist_now() -> datetime:
    return datetime.now(IST)

def _days_old(d) -> int:
    if d is None:
        return 999
    if hasattr(d, 'date'):
        d = d.date()
    return (date.today() - d).days

def _mins_ago(ts) -> int:
    if ts is None:
        return 9999
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return int((_ist_now() - ts).total_seconds() / 60)

def _status(checks: List[Dict]) -> str:
    if any(c['level'] == 'red'    for c in checks): return 'red'
    if any(c['level'] == 'yellow' for c in checks): return 'yellow'
    return 'green'

def _emoji(level: str) -> str:
    return {'green': '🟢', 'yellow': '🟡', 'red': '🔴'}.get(level, '⚪')

def _chk(label: str, value: Any, ok: bool, warn: bool = True, detail: str = '') -> Dict:
    level = 'green' if ok else ('yellow' if warn else 'red')
    return {'label': label, 'value': str(value), 'level': level,
            'emoji': _emoji(level), 'detail': detail}


# ── Section builders ───────────────────────────────────────────────────────────────────────────

def _section_data_feeds(cur) -> Dict:
    checks = []
    now = _ist_now()
    state = _market_state(now)   # cc#146: 'market' | 'grace' | 'off'

    # Fyers intraday
    # FIX (12-Jun-2026): Fyers writes 5-min bars (timeframe='5m'), not '1m'.
    # The old '1m' filter matched zero rows -> false "feed dead" red while the
    # 5m feed was healthy. Source tags are fyers_eq / fyers_fut.
    if state == 'market':
        cur.execute("""
            SELECT COUNT(DISTINCT symbol) as syms, MAX(ts) as latest, COUNT(*) as rows
            FROM intraday_prices WHERE ts::date = CURRENT_DATE AND timeframe='5m'
        """)
        r = cur.fetchone()
        syms, latest_ts, rows = r[0] or 0, r[1], r[2] or 0
        mins = _mins_ago(latest_ts)
        checks.append(_chk('Fyers intraday symbols', syms,
            ok=(syms >= 200), warn=(syms >= 150),
            detail=f'Expected 208-211'))
        checks.append(_chk('Fyers latest tick', f'{mins} min ago',
            ok=(mins <= 10), warn=(mins <= 30),
            detail=str(latest_ts)[:16] if latest_ts else 'no data'))
        checks.append(_chk('Fyers intraday rows today', rows,
            ok=(rows >= 1000), warn=(rows >= 100)))
    elif state == 'grace':
        # 09:15-09:25 IST: feed is still warming up right after open — thin/missing
        # data here is expected, not a failure. YELLOW at worst, never RED.
        cur.execute("""
            SELECT COUNT(DISTINCT symbol) as syms, MAX(ts) as latest, COUNT(*) as rows
            FROM intraday_prices WHERE ts::date = CURRENT_DATE AND timeframe='5m'
        """)
        r = cur.fetchone()
        syms, latest_ts, rows = r[0] or 0, r[1], r[2] or 0
        grace_detail = 'Grace window 09:15-09:25 IST — feed warming up'
        checks.append(_chk('Fyers intraday symbols', syms,
            ok=(syms >= 200), warn=True, detail=grace_detail))
        checks.append(_chk('Fyers latest tick', str(latest_ts)[:16] if latest_ts else 'no data yet',
            ok=(latest_ts is not None), warn=True, detail=grace_detail))
        checks.append(_chk('Fyers intraday rows today', rows,
            ok=(rows >= 1000), warn=True, detail=grace_detail))
    else:
        # Off-hours (pre-market / post-market / weekend / NSE holiday): today's
        # intraday_prices is legitimately empty, so grade the PRIOR trading
        # session's last bar instead. GREEN only if that last bar's time-of-day
        # falls in the 15:20-15:30 close band (a clean shutdown) — a feed that
        # died mid-session (e.g. last bar 14:00) still surfaces, just as YELLOW
        # (not a false RED, since off-hours is never actionable at 3am).
        cur.execute("""
            SELECT COUNT(DISTINCT symbol) as syms, MAX(ts) as latest, COUNT(*) as rows
            FROM intraday_prices
            WHERE ts::date = (SELECT MAX(ts::date) FROM intraday_prices WHERE timeframe='5m')
              AND timeframe='5m'
        """)
        r = cur.fetchone()
        syms, latest_ts, rows = r[0] or 0, r[1], r[2] or 0
        clean = _closed_cleanly(latest_ts)
        off_detail = (f'GREEN — last bar {latest_ts} (prior session close)' if clean
                      else f'off-hours — last bar {latest_ts or "none"} not in 15:20-15:30 close band')
        checks.append(_chk('Fyers intraday symbols', syms,
            ok=(clean and syms >= 200), warn=True, detail=off_detail))
        checks.append(_chk('Fyers latest tick', str(latest_ts)[:16] if latest_ts else 'no data',
            ok=clean, warn=True, detail=off_detail))
        checks.append(_chk('Fyers intraday rows today', rows,
            ok=(clean and rows >= 1000), warn=True, detail=off_detail))

    # raw_prices EOD
    cur.execute("SELECT MAX(price_date), COUNT(DISTINCT symbol) FROM raw_prices")
    r = cur.fetchone()
    rp_date, rp_syms = r[0], r[1] or 0
    rp_days = _days_old(rp_date)
    checks.append(_chk('raw_prices latest date', str(rp_date),
        ok=(rp_days <= 1), warn=(rp_days <= 3),
        detail=f'{rp_syms} symbols'))

    # cmp_prices
    cur.execute("SELECT COUNT(*), MAX(updated_at) FROM cmp_prices")
    r = cur.fetchone()
    cmp_cnt, cmp_ts = r[0] or 0, r[1]
    if state == 'market':
        cmp_mins = _mins_ago(cmp_ts)
        checks.append(_chk('cmp_prices count', cmp_cnt,
            ok=(cmp_cnt >= 200), warn=(cmp_cnt >= 150)))
        checks.append(_chk('cmp_prices freshness', f'{cmp_mins} min ago',
            ok=(cmp_mins <= 15), warn=(cmp_mins <= 60)))
    elif state == 'grace':
        grace_detail = 'Grace window 09:15-09:25 IST — feed warming up'
        checks.append(_chk('cmp_prices count', cmp_cnt,
            ok=(cmp_cnt >= 200), warn=True, detail=grace_detail))
        checks.append(_chk('cmp_prices freshness', str(cmp_ts)[:16] if cmp_ts else 'no data',
            ok=True, warn=True, detail=grace_detail))
    else:
        clean = _closed_cleanly(cmp_ts)
        off_detail = (f'GREEN — last updated {cmp_ts} (prior session close)' if clean
                      else f'off-hours — last updated {cmp_ts or "none"} not in 15:20-15:30 close band')
        checks.append(_chk('cmp_prices count', cmp_cnt,
            ok=(cmp_cnt >= 200), warn=True, detail=off_detail))
        checks.append(_chk('cmp_prices freshness', str(cmp_ts)[:16] if cmp_ts else 'no data',
            ok=clean, warn=True, detail=off_detail))

    # Global indices
    cur.execute("SELECT MAX(quote_date), COUNT(DISTINCT symbol) FROM global_indices")
    r = cur.fetchone()
    gi_date, gi_cnt = r[0], r[1] or 0
    gi_days = _days_old(gi_date)
    checks.append(_chk('Global indices latest', str(gi_date),
        ok=(gi_days <= 1), warn=(gi_days <= 3),
        detail=f'{gi_cnt} symbols'))

    # ADR
    cur.execute("SELECT price_date, adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
    r = cur.fetchone()
    if r:
        adr_date, adr_val = r[0], float(r[1]) if r[1] else None
        adr_days = _days_old(adr_date)
        checks.append(_chk('ADR latest date', str(adr_date),
            ok=(adr_days <= 1), warn=(adr_days <= 3),
            detail=f'ADR={adr_val}'))
    else:
        checks.append(_chk('ADR', 'NO DATA', ok=False, warn=False))

    # PCR
    cur.execute("SELECT MAX(price_date), COUNT(*) FROM pcr_daily")
    r = cur.fetchone()
    pcr_date, pcr_cnt = r[0], r[1] or 0
    pcr_days = _days_old(pcr_date)
    checks.append(_chk('PCR latest date', str(pcr_date),
        ok=(pcr_days <= 1), warn=(pcr_days <= 3),
        detail=f'{pcr_cnt} rows'))

    return {'name': 'Data Feeds', 'checks': checks, 'status': _status(checks)}


def _section_v8_engine(cur) -> Dict:
    checks = []

    # v8_metrics
    cur.execute("SELECT MAX(score_date), COUNT(DISTINCT symbol) FROM v8_metrics")
    r = cur.fetchone()
    vm_date, vm_syms = r[0], r[1] or 0
    vm_days = _days_old(vm_date)
    checks.append(_chk('v8_metrics latest date', str(vm_date),
        ok=(vm_days == 0), warn=(vm_days <= 1),
        detail=f'{vm_syms} symbols'))
    checks.append(_chk('v8_metrics symbol count', vm_syms,
        ok=(vm_syms >= 200), warn=(vm_syms >= 150)))

    # v8_qualified per basket
    cur.execute("""
        SELECT basket, COUNT(*) FROM v8_qualified
        WHERE signal_date = CURRENT_DATE GROUP BY basket
    """)
    basket_signals = {r[0]: r[1] for r in cur.fetchall()}
    for basket in ['buy_reversal', 'buy_momentum', 'sell_reversal', 'sell_momentum']:
        cnt = basket_signals.get(basket, 0)
        checks.append(_chk(f'Signals: {basket}', cnt,
            ok=(cnt >= 0), warn=True,
            detail='0 may be correct in weak market'))

    # v8_funnel_counts
    cur.execute("SELECT MAX(score_date) FROM v8_funnel_counts")
    r = cur.fetchone()
    fc_date = r[0]
    fc_days = _days_old(fc_date)
    checks.append(_chk('v8_funnel_counts latest', str(fc_date),
        ok=(fc_days <= 1), warn=(fc_days <= 2)))

    # Paper positions
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE side='LONG') as longs,
               COUNT(*) FILTER (WHERE side='SHORT') as shorts
        FROM v8_paper_positions WHERE status='OPEN'
    """)
    r = cur.fetchone()
    longs, shorts = r[0] or 0, r[1] or 0
    checks.append(_chk('Paper open positions', f'{longs}L / {shorts}S',
        ok=True, detail=f'Total {longs+shorts}/15 slots'))

    # Paper win rate
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE result='TARGET') as wins, COUNT(*) as total
        FROM v8_paper_trades
    """)
    r = cur.fetchone()
    wins, total = r[0] or 0, r[1] or 0
    wr = round(wins/total*100, 1) if total > 0 else 0
    checks.append(_chk('Paper win rate', f'{wr}% ({wins}W/{total}T)',
        ok=(wr >= 60 or total < 5), warn=(wr >= 40 or total < 5)))

    # Market mood — FIX (12-Jun-2026): the live gate reads adr_intraday (5-min),
    # NOT adr_daily. The old adr_daily read showed a stale EOD value (e.g. 0.235)
    # and false-flagged red while the real gate saw a healthy live ADR. Mirror the
    # gate: adr_intraday primary (universe>=50), adr_daily fallback off-hours.
    cur.execute("""
        SELECT adr, universe_count FROM adr_intraday
        WHERE ts::date = CURRENT_DATE ORDER BY ts DESC LIMIT 1
    """)
    r = cur.fetchone()
    if r and r[0] is not None and (r[1] or 0) >= 50:
        adr = float(r[0]); adr_src = 'adr_intraday (live)'
    else:
        cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
        r = cur.fetchone()
        adr = float(r[0]) if r and r[0] else 0.0
        adr_src = 'adr_daily (EOD fallback)'
    checks.append(_chk('Market mood ADR', adr,
        ok=(adr >= 1.0), warn=(adr >= 0.8),
        detail=f'Gate open if >= 1.0 · src={adr_src}'))

    return {'name': 'V8 Engine', 'checks': checks, 'status': _status(checks)}


def _section_gvm(cur) -> Dict:
    checks = []

    # gvm_scores
    cur.execute("SELECT MAX(score_date), COUNT(*), ROUND(AVG(gvm_score)::numeric,2) FROM gvm_scores")
    r = cur.fetchone()
    gs_date, gs_cnt, gs_avg = r[0], r[1] or 0, r[2]
    gs_days = _days_old(gs_date)
    checks.append(_chk('GVM scores latest date', str(gs_date),
        ok=(gs_days <= 1), warn=(gs_days <= 3)))
    checks.append(_chk('GVM universe size', gs_cnt,
        ok=(gs_cnt >= 1500), warn=(gs_cnt >= 1000),
        detail=f'avg GVM={gs_avg}'))

    # gvm_history
    cur.execute("SELECT MAX(score_date), COUNT(DISTINCT score_date) FROM gvm_history")
    r = cur.fetchone()
    gh_date, gh_snaps = r[0], r[1] or 0
    gh_days = _days_old(gh_date)
    checks.append(_chk('GVM history snapshots', gh_snaps,
        ok=(gh_snaps >= 1), warn=True,
        detail=f'latest={gh_date}'))

    # sector ratings — via gvm_scores segment column
    cur.execute("SELECT COUNT(DISTINCT segment) FROM gvm_scores WHERE segment IS NOT NULL")
    r = cur.fetchone()
    seg_cnt = r[0] or 0
    checks.append(_chk('Sectors rated', seg_cnt,
        ok=(seg_cnt >= 100), warn=(seg_cnt >= 50)))

    return {'name': 'GVM', 'checks': checks, 'status': _status(checks)}


def _section_quant_basket(cur) -> Dict:
    checks = []

    # QB positions per basket
    cur.execute("""
        SELECT basket_name, COUNT(*) FILTER (WHERE status='open') as open_cnt
        FROM quant_paper_positions GROUP BY basket_name
    """)
    qb_pos = {r[0]: r[1] for r in cur.fetchall()}
    expected = {'large_cap': 13, 'mid_cap': 15, 'small_cap': 22, 'alpha_multicap': 17}
    for basket, exp in expected.items():
        cnt = qb_pos.get(basket, 0)
        checks.append(_chk(f'QB {basket}', f'{cnt} positions',
            ok=(cnt >= exp - 2), warn=(cnt >= exp - 5),
            detail=f'Expected ~{exp}'))

    # Total QB positions
    total_qb = sum(qb_pos.values())
    checks.append(_chk('QB total positions', total_qb,
        ok=(total_qb >= 60), warn=(total_qb >= 40)))

    # EOD checker last run — check quant_paper_positions last updated
    cur.execute("SELECT MAX(updated_at) FROM quant_paper_positions WHERE status='open'")
    r = cur.fetchone()
    qb_ts = r[0]
    qb_days = _days_old(qb_ts.date() if qb_ts else None)
    checks.append(_chk('QB EOD checker last run', str(qb_ts)[:16] if qb_ts else 'never',
        ok=(qb_days <= 1), warn=(qb_days <= 2)))

    return {'name': 'Quant Basket', 'checks': checks, 'status': _status(checks)}


def _ops_age_min(cur, title, category=None):
    """(minutes_since_newest, last_ts) for the newest ops_log row matching title (and
    category if given). Age is computed in SQL (NOW() - session_ts) so it is DB-clock
    consistent and immune to the UTC/IST skew that plagues Python-side ts math.
    Returns (None, None) when no such row exists."""
    if category is None:
        cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW()-MAX(session_ts)))/60.0, MAX(session_ts)
                       FROM ops_log WHERE title=%s""", (title,))
    else:
        cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW()-MAX(session_ts)))/60.0, MAX(session_ts)
                       FROM ops_log WHERE title=%s AND category=%s""", (title, category))
    r = cur.fetchone()
    if not r or r[0] is None:
        return None, None
    return float(r[0]), r[1]


def _alert_age_min(cur, title):
    """Minutes since the newest category='alert' ops_log row for title (None if never)."""
    cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW()-MAX(session_ts)))/60.0
                   FROM ops_log WHERE category='alert' AND title=%s""", (title,))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _sched_row(label, age_min, last_ts, expected, green_h=26.0, yellow_h=50.0):
    """Traffic-light for a job whose last successful run is age_min minutes ago
    (green=on-time, yellow=late/recovered, red=missing)."""
    if age_min is None:
        return _chk(label, 'no run recorded', ok=False, warn=False,
                    detail=f'Expected {expected} · no ops_log/proxy row yet')
    disp = f'{int(age_min)} min ago' if age_min < 180 else f'{age_min/60.0:.1f}h ago'
    return _chk(label, disp, ok=(age_min <= green_h*60), warn=(age_min <= yellow_h*60),
                detail=f'Expected {expected} · last {str(last_ts)[:16]}')


def _alert_absence_row(cur, label, alert_title, expected, window_min=1200.0):
    """green when NO alert of alert_title fired within window_min (default ~20h)."""
    age = _alert_age_min(cur, alert_title)
    if age is None or age > window_min:
        return _chk(label, 'OK — no alert', ok=True,
                    detail=f'Expected {expected} · alert-absence ({alert_title})')
    return _chk(label, f'ALERT {int(age)} min ago', ok=False, warn=False,
                detail=f'Expected {expected} · {alert_title} fired')


def _section_scheduler(cur) -> Dict:
    """Every scheduled job vs its expected IST window + last actual successful run
    (ops_log scheduler_health / rich telemetry rows, or a direct table proxy) + traffic
    light. cc#255 rebuild: (a) the live-engine heartbeat now reads v8_metrics computed_at
    age (the same proxy scheduler.py's own watchdog trusts) instead of the dead
    app_config heartbeat key; (b) stale nightly labels fixed to the real 01:00-02:05 chain
    (the old late-evening window was retired on task #31); (c) coverage extended from 6 to
    the full job set, grouped where jobs genuinely share a fate to stay glanceable."""
    checks = []
    now = _ist_now()
    mkt = _market_state(now) == 'market'

    # ── Live market-hours engine — 6 jobs share ONE 5-min dispatch block, so
    #    v8_metrics freshness is a faithful umbrella proxy (avoid 6 duplicate rows).
    cur.execute("SELECT EXTRACT(EPOCH FROM (NOW()-MAX(computed_at)))/60.0, MAX(computed_at) FROM v8_metrics")
    r = cur.fetchone()
    sw_age, sw_ts = (float(r[0]), r[1]) if r and r[0] is not None else (None, None)
    if sw_age is None:
        checks.append(_chk('Live engine (5-min ×6 jobs)', 'no v8_metrics', ok=False, warn=False,
                           detail='signal_writer/paper_exit/v10/pcr/tc_lite/smartgain_mtm'))
    elif mkt:
        checks.append(_chk('Live engine (5-min ×6 jobs)', f'{int(sw_age)} min ago',
            ok=(sw_age <= 10), warn=(sw_age <= 20),
            detail='v8_metrics computed_at age (umbrella for the 6 shared live jobs)'))
    else:
        checks.append(_chk('Live engine (5-min ×6 jobs)', f'{int(sw_age)} min ago (off-hours)',
            ok=True, warn=True, detail='off-hours — last live tick; strict only 09:15-15:30'))

    # ── Nightly chain (01:00-02:05 IST, runs every calendar day) ──────────────────
    cur.execute("SELECT MAX(price_date) FROM raw_prices")
    rp_date = cur.fetchone()[0]
    rp_days = _days_old(rp_date)
    checks.append(_chk('Yahoo EOD (15:35 + 01:00 safety)', str(rp_date),
        ok=(rp_days <= 1), warn=(rp_days <= 3),
        detail='raw_prices freshness — one merged check for both runs'))

    age, ts = _ops_age_min(cur, 'qb_eod', 'scheduler_health')
    if age is None:
        cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW()-MAX(updated_at)))/60.0, MAX(updated_at)
                       FROM quant_paper_positions WHERE status='open'""")
        r = cur.fetchone(); age, ts = (float(r[0]), r[1]) if r and r[0] is not None else (None, None)
    checks.append(_sched_row('QB EOD checker (01:15)', age, ts, 'daily 01:15 IST'))

    age, ts = _ops_age_min(cur, 'gvm_recompute', 'scheduler_health')
    if age is not None:
        checks.append(_sched_row('GVM recompute (01:30)', age, ts, 'daily 01:30 IST'))
    else:
        cur.execute("SELECT MAX(score_date) FROM gvm_scores")
        gd = cur.fetchone()[0]; gdd = _days_old(gd)
        checks.append(_chk('GVM recompute (01:30)', str(gd),
            ok=(gdd <= 1), warn=(gdd <= 3), detail='daily 01:30 IST · gvm_scores date'))

    age, ts = _ops_age_min(cur, 'pivots_build', 'scheduler_health')
    checks.append(_sched_row('Paper pivots build (01:45)', age, ts, 'daily 01:45 IST'))

    age, ts = _ops_age_min(cur, 'cleanup_news', 'scheduler_health')
    if age is None:
        age, ts = _ops_age_min(cur, 'news_retention', 'news_retention')
    checks.append(_sched_row('News retention purge (01:50)', age, ts, 'daily 01:50 IST'))

    age, ts = _ops_age_min(cur, 'v8_paper_exit_eod', 'scheduler_health')
    checks.append(_sched_row('Paper EOD exit fallback (02:00)', age, ts, 'daily 02:00 IST'))

    age, ts = _ops_age_min(cur, 'universe_technicals', 'scheduler_health')
    checks.append(_sched_row('Universe technicals (02:05)', age, ts, 'daily 02:05 IST'))

    # ── Pre-market cluster ────────────────────────────────────────────────────────
    cur.execute("SELECT MAX(quote_date) FROM global_indices")
    gi_date = cur.fetchone()[0]; gi_days = _days_old(gi_date)
    checks.append(_chk('Global indices fetch (06:00)', str(gi_date),
        ok=(gi_days <= 1), warn=(gi_days <= 3), detail='daily 06:00 IST'))

    age, ts = _ops_age_min(cur, 'earnings_refresh', 'info')
    if age is None:
        cur.execute("SELECT EXTRACT(EPOCH FROM (NOW()-MAX(loaded_at)))/60.0, MAX(loaded_at) FROM earnings_calendar")
        r = cur.fetchone(); age, ts = (float(r[0]), r[1]) if r and r[0] is not None else (None, None)
    checks.append(_sched_row('Earnings refresh (06:15)', age, ts, 'weekdays 06:15 IST', yellow_h=74.0))

    age, ts = _ops_age_min(cur, 'fetch_stock_news')
    checks.append(_sched_row('Stock-news fetch (08:30/12:30/16:30)', age, ts,
                             '3× trading day', yellow_h=74.0))

    checks.append(_alert_absence_row(cur, 'Pre-market writer check (09:10)',
                                     'scheduler_stall_9am', 'weekdays 09:10 IST'))
    checks.append(_alert_absence_row(cur, 'Open-bars feed alarm (09:25)',
                                     'feed_silent_at_open', 'trading days 09:25 IST'))

    age, ts = _ops_age_min(cur, 'fu_sync', 'scheduler_health')
    checks.append(_sched_row('Futures universe sync (Mon 08:00)', age, ts,
                             'weekly Mon 08:00 IST', green_h=192.0, yellow_h=384.0))

    # ── Post-close chain ──────────────────────────────────────────────────────────
    age, ts = _ops_age_min(cur, 'gate_rebalance_15_20', 'gate_rebalance')
    checks.append(_sched_row('Gate rebalance (15:20)', age, ts, 'weekdays 15:20 IST', yellow_h=74.0))

    age, ts = _ops_age_min(cur, 'heal_intraday', 'scheduler_health')
    checks.append(_sched_row('Session-gap heal (15:40)', age, ts, 'weekdays 15:40 IST', yellow_h=74.0))

    age, ts = _ops_age_min(cur, 'v8_eod', 'scheduler_health')
    if age is None:
        cur.execute("SELECT MAX(score_date) FROM v8_metrics")
        vd = cur.fetchone()[0]; vdd = _days_old(vd)
        checks.append(_chk('V8 EOD engine (15:45)', str(vd),
            ok=(vdd == 0), warn=(vdd <= 1), detail='weekdays 15:45 IST · v8_metrics date'))
    else:
        checks.append(_sched_row('V8 EOD engine (15:45)', age, ts, 'weekdays 15:45 IST', yellow_h=74.0))

    age, ts = _ops_age_min(cur, 'adr_compute', 'scheduler_health')
    checks.append(_sched_row('ADR/PCR compute (15:50)', age, ts, 'weekdays 15:50 IST', yellow_h=74.0))

    age, ts = _ops_age_min(cur, 'tc_screener_precompute', 'scheduler_health')
    checks.append(_sched_row('TC screener precompute (16:00)', age, ts, 'weekdays 16:00 IST', yellow_h=74.0))

    checks.append(_alert_absence_row(cur, 'Stock-news watchdog (16:00)',
                                     'stock_news_stale', 'trading days 16:00 IST', window_min=1500.0))

    age, ts = _ops_age_min(cur, 'v21_killswitch', 'scheduler_health')
    checks.append(_sched_row('V2.1 kill-switch (16:10)', age, ts, 'weekdays 16:10 IST', yellow_h=74.0))

    # cc#524 QUARTERLY UPDATE FRAMEWORK coverage -- T+1 (daily), Saturday scoped retry
    # (weekly), season-close sweep (quarterly: Sep/Dec/Mar/Jun 1st).
    age, ts = _ops_age_min(cur, 'OPS_METRICS_T1_RUN', 'ops_metrics_pull')
    checks.append(_sched_row('Ops-metrics T+1 refresh (~08:00)', age, ts, 'daily ~08:00 IST'))

    age, ts = _ops_age_min(cur, 'OPS_METRICS_SATURDAY_RETRY', 'ops_metrics_pull')
    checks.append(_sched_row('Ops-metrics Saturday retry (10:00)', age, ts,
                             'Saturdays 10:00 IST', green_h=170.0, yellow_h=200.0))

    age, ts = _ops_age_min(cur, 'OPS_METRICS_SEASON_SWEEP_DONE', 'ops_metrics_pull')
    checks.append(_sched_row('Ops-metrics season sweep', age, ts,
                             'Sep/Dec/Mar/Jun 1st 10:00 IST', green_h=2400.0, yellow_h=3120.0))

    return {'name': 'Scheduler', 'checks': checks, 'status': _status(checks)}


def _section_infrastructure(cur) -> Dict:
    checks = []

    # DB size
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    db_size = cur.fetchone()[0]
    checks.append(_chk('DB size', db_size, ok=True))

    # Table count
    cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'")
    tbl_cnt = cur.fetchone()[0]
    checks.append(_chk('Tables in DB', tbl_cnt,
        ok=(tbl_cnt >= 40), warn=(tbl_cnt >= 30)))

    # futures_universe
    cur.execute("SELECT COUNT(*) FROM futures_universe WHERE is_active=TRUE")
    fut_cnt = cur.fetchone()[0] or 0
    checks.append(_chk('Active futures universe', fut_cnt,
        ok=(fut_cnt >= 200), warn=(fut_cnt >= 150),
        detail='Expected 208-211'))

    # cmp_prices vs futures
    cur.execute("SELECT COUNT(*) FROM cmp_prices")
    cmp_cnt = cur.fetchone()[0] or 0
    checks.append(_chk('CMP coverage vs universe', f'{cmp_cnt}/{fut_cnt}',
        ok=(cmp_cnt >= fut_cnt - 5), warn=(cmp_cnt >= fut_cnt - 20)))

    # session_log count
    cur.execute("SELECT COUNT(*) FROM session_log")
    sl_cnt = cur.fetchone()[0] or 0
    checks.append(_chk('Session log entries', sl_cnt, ok=True))

    return {'name': 'Infrastructure', 'checks': checks, 'status': _status(checks)}


# ── Main endpoint ───────────────────────────────────────────────────────────────────────────

@router.get("/diagnosis")
def run_diagnosis():
    now = _ist_now()
    sections = []
    issues = []
    warnings = []

    try:
        with _conn() as conn, conn.cursor() as cur:
            for builder in [
                _section_data_feeds,
                _section_v8_engine,
                _section_gvm,
                _section_quant_basket,
                _section_scheduler,
                _section_infrastructure,
            ]:
                try:
                    sec = builder(cur)
                    sections.append(sec)
                    for c in sec['checks']:
                        if c['level'] == 'red':
                            issues.append(f"[{sec['name']}] {c['label']}: {c['value']}")
                        elif c['level'] == 'yellow':
                            warnings.append(f"[{sec['name']}] {c['label']}: {c['value']}")
                except Exception as e:
                    conn.rollback()
                    sections.append({
                        'name': builder.__name__.replace('_section_', '').replace('_', ' ').title(),
                        'status': 'red',
                        'checks': [{'label': 'Section error', 'value': str(e),
                                    'level': 'red', 'emoji': '🔴', 'detail': ''}],
                    })
                    issues.append(f"[Section] {builder.__name__}: {str(e)[:100]}")

    except Exception as e:
        return {'status': 'red', 'error': str(e), 'sections': [], 'issues': [str(e)], 'warnings': []}

    # Overall status
    overall = 'red' if issues else ('yellow' if warnings else 'green')

    return {
        'status':        overall,
        'emoji':         _emoji(overall),
        'generated_at':  now.strftime('%Y-%m-%d %H:%M:%S IST'),
        'issues_count':  len(issues),
        'warnings_count':len(warnings),
        'issues':        issues,
        'warnings':      warnings,
        'sections':      sections,
    }

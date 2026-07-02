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


def _section_scheduler(cur) -> Dict:
    """
    Check each scheduled job against expected run window (IST).
    Uses latest data timestamps as proxy for job completion.
    """
    checks = []
    now = _ist_now()
    today = now.date()

    # 07:00 — Global indices fetch
    cur.execute("SELECT MAX(quote_date) FROM global_indices")
    r = cur.fetchone()
    gi_date = r[0]
    gi_ok = gi_date == today or (gi_date == today - timedelta(days=1) and now.hour < 8)
    checks.append(_chk('07:00 Global indices fetch', str(gi_date),
        ok=gi_ok, warn=(gi_date is not None),
        detail='Expected: daily 07:00 IST'))

    # 09:00 — V8 history cache: RETIRED 06-Jun-2026 (v8_live removed, single engine
    # = v8_signal_writer). The v8_history_cache table is no longer written, so the
    # old check was permanently red on a dead artifact. Replaced with the live
    # signal-writer heartbeat (sched_writer_hb) which proves the 5-min engine is
    # firing. FIX (12-Jun-2026).
    cur.execute("SELECT value FROM app_config WHERE key='sched_writer_hb'")
    r = cur.fetchone()
    hb_ts = None
    if r and r[0]:
        try:
            hb_ts = datetime.strptime(r[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=IST)
        except Exception:
            hb_ts = None
    hb_mins = _mins_ago(hb_ts)
    # During market hours expect a tick within ~10 min; off-hours just report last.
    mkt = (now.weekday() < 5 and (now.hour*60+now.minute) >= 555 and (now.hour*60+now.minute) <= 930)
    checks.append(_chk('Live signal writer (5-min)', f'{hb_mins} min ago',
        ok=(not mkt) or (hb_mins <= 10),
        warn=(not mkt) or (hb_mins <= 30),
        detail='heartbeat sched_writer_hb' + ('' if mkt else ' (off-hours)')))

    # 15:45 — V8 EOD engine
    cur.execute("SELECT MAX(score_date) FROM v8_metrics")
    r = cur.fetchone()
    vm_date = r[0]
    # After 15:45 today it should be today; before that yesterday is fine
    if now.hour >= 16:
        vm_ok = vm_date == today
        vm_warn = vm_date == today - timedelta(days=1)
    else:
        vm_ok = vm_date >= today - timedelta(days=1)
        vm_warn = True
    checks.append(_chk('15:45 V8 EOD engine', str(vm_date),
        ok=vm_ok, warn=vm_warn,
        detail='Runs after market close'))

    # 21:00 — Yahoo daily OHLC
    cur.execute("SELECT MAX(price_date) FROM raw_prices")
    r = cur.fetchone()
    rp_date = r[0]
    # After 21:00 today's close should be in; before that yesterday is fine
    if now.hour >= 21:
        rp_ok = rp_date == today
        rp_warn = rp_date == today - timedelta(days=1)
    else:
        rp_ok = rp_date >= today - timedelta(days=1)
        rp_warn = True
    checks.append(_chk('21:00 Yahoo daily OHLC', str(rp_date),
        ok=rp_ok, warn=rp_warn))

    # 21:05 — QB EOD checker
    cur.execute("SELECT MAX(updated_at)::date FROM quant_paper_positions WHERE status='open'")
    r = cur.fetchone()
    qb_date = r[0]
    if now.hour >= 21:
        qb_ok = qb_date == today
        qb_warn = qb_date == today - timedelta(days=1)
    else:
        qb_ok = qb_date >= today - timedelta(days=1)
        qb_warn = True
    checks.append(_chk('21:05 QB EOD checker', str(qb_date),
        ok=qb_ok, warn=qb_warn))

    # 22:00 — GVM recompute
    cur.execute("SELECT MAX(score_date) FROM gvm_scores")
    r = cur.fetchone()
    gvm_date = r[0]
    if now.hour >= 22:
        gvm_ok = gvm_date == today
        gvm_warn = gvm_date == today - timedelta(days=1)
    else:
        gvm_ok = gvm_date >= today - timedelta(days=1)
        gvm_warn = True
    checks.append(_chk('22:00 GVM recompute', str(gvm_date),
        ok=gvm_ok, warn=gvm_warn))

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

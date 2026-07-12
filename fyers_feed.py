"""
Fyers Live Feed - Scorr V8
============================
Standalone Railway WORKER (not in FastAPI). The live intraday source.

Architecture (v6 — 5-MIN SYSTEM, equity + futures + options on single WS):
  1. BACKFILL  - on boot, one-time 7-day history for ALL equity symbols (skip-if-fresh, async).
  2. LIVE WS   - single persistent WebSocket, up to 5000 symbols (Fyers v3 limit).
                 * 211 equity  (NSE:SYMBOL-EQ)   → source='fyers_eq', timeframe='5m'
                 * 209 futures (NSE:SYMBOLMNTHFUT) → source='fyers_fut', timeframe='5m'
                 * ~1040 options (top-50 mcap + NIFTY + BANKNIFTY, ATM±10 CE+PE)
                   → stored in option_chain table, 5-min bars
  3. OI POLL   - futures OI via DEPTH REST every OI_POLL_MINS
                 (quotes API has NO OI — Fyers KB confirmed; depth is the only source).
  4. HEAL GAP  - daily at 18:00 IST: checks equity symbols, fills missing bars.
  5. CMP FLUSH - every 30s during market hours → cmp_prices (IST timestamp).
  6. ATM ROLL  - every 15 min during market hours: recheck ATM per option symbol,
                 re-subscribe if drifted ±2 strikes.
  7. MONTHLY ROLL - on expiry day (last Tuesday): rebuild futures + option symbol lists.
  8. PURGE     - rolling (cc#297): intraday_prices source='fyers_eq' at EQUITY_RETENTION_DAYS
                 (365d, long sim/BT7 history); fyers_fut/legacy AND futures_basis at
                 INTRADAY_FUT_RETENTION_DAYS (7d); option_chain at OPTION_RETENTION_DAYS (7d).
                 Rows older deleted daily. Equity and futures_basis no longer share a constant.

v6.1 (10-Jun-2026):
  * CRITICAL DEADLOCK FIX: flush_all() holds agg.lock while _flush → _compute_basis
    tried to re-acquire it for the last_oi fallback. threading.Lock is NOT
    re-entrant → housekeeping thread froze on the first futures bar flush and
    every WS tick then blocked on the same lock (feed frozen 13:54 IST).
    Fix: agg.lock is now an RLock AND _compute_basis reads last_oi without
    locking (CPython dict .get is GIL-atomic).
  * OPTION SYMBOL MASTER: ladders from Fyers NSE_FO master (actually-listed strikes).
  * INDEX/ETF LTP: NIFTY500, GOLDBEES, SILVERBEES in the 30s quotes poll.
  * OI POLL DEBUG: start/first-response logging + dict/list response handling.

v6.2 (15-Jun-2026):
  * RETENTION SPLIT: intraday_prices + futures_basis extended 7d → 30d to bank
    real 5-min history for the intraday filter optimizer. option_chain stays 7d
    (heaviest churn, not used by the sim) via OPTION_RETENTION_DAYS. purge_old_bars
    now uses two cutoffs.
  * cc#297 (08-Jul-2026): retention DECOUPLED. fyers_eq extended 30d → 365d via its own
    EQUITY_RETENTION_DAYS; futures_basis moved back to the 7d INTRADAY_FUT_RETENTION_DAYS
    window (it had been dragged to 30d by the shared constant). fyers_fut/option_chain/
    global_intraday unchanged at 7d. Equity and futures_basis no longer share a constant.

5-MIN SYSTEM (canonical spec session_log id=167):
  All rolling intraday feeds store at 5-min granularity. NOT a flash/1-min system.
  1-min is deprecated as default (future on-demand only — flip BAR_MINUTES).

TOKEN MODEL (Fyers v3, SEBI framework from 01-Apr-2026):
  Refresh-token flow is DISABLED. ONE 2FA login per TRADING DAY.
  access_token valid the whole trading day, survives restarts.
  Stored in Railway table fyers_tokens (id=1).

  Boot logic (get_valid_token):
    1. --auth-code given  -> bootstrap (mint + store today's token).
    2. else stored access_token created TODAY AND verified live -> reuse it.
    3. else -> AUTO-LOGIN via TOTP (headless) -> store + return. Zero-touch.

USAGE:
  Normal (zero-touch): python fyers_feed.py
  Manual override:     python fyers_feed.py --auth-code <code>
"""

# ops: worker bounce 12-Jul-2026 (weekend, market closed) to resume the stalled cc#389/#390 Phase A
# 5m warehouse backfill (phase_a_run re-armed to 'run'; daemon re-claims on boot). No logic change.

import argparse, bisect, calendar, hashlib, os, sys, json, time, logging, threading, re
from datetime import datetime, timedelta, time as dt_time, date
import pytz, psycopg2, requests
from nse_holidays import is_trading_day   # cc#188: market-hours gate for subscribe_verify

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    '')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL      = 'https://api-t1.fyers.in/api/v3/validate-authcode'
QUOTES_URL        = 'https://api-t1.fyers.in/data/quotes'
DEPTH_URL         = 'https://api-t1.fyers.in/data/depth'
OPTION_MASTER_URL = 'https://public.fyers.in/sym_details/NSE_FO.csv'
IST               = pytz.timezone('Asia/Kolkata')

# cc#297: retention constants DECOUPLED. fyers_eq (equity) gets its OWN 365d constant; every other
# intraday store — fyers_fut, residual legacy bars, AND futures_basis — uses the 7d futures window.
# The old shared RETENTION_DAYS (=30, drove BOTH fyers_eq and futures_basis) is removed so an equity
# bump can never silently drag futures_basis along again.
EQUITY_RETENTION_DAYS = 730   # intraday_prices source='fyers_eq' ONLY (30→365 08-Jul; →730 cc#381 11-Jul: 2yr rolling for replay/sim depth)
HIST_RETENTION_DAYS   = 730   # cc#381: source='fyers_hist' backtest warehouse — 2yr rolling (matches equity; was purge-exempt in cc#377)
INTRADAY_FUT_RETENTION_DAYS = 7   # cc#227: fyers_fut + residual legacy fyers/yahoo intraday bars — AND futures_basis (cc#297)
MARKET_OPEN    = dt_time(9, 15)
MARKET_CLOSE   = dt_time(15, 30)

INDEX_LTP_SYMBOLS = {
    'NIFTY50':    'NSE:NIFTY50-INDEX',
    'BANKNIFTY':  'NSE:NIFTYBANK-INDEX',
    'INDIAVIX':   'NSE:INDIAVIX-INDEX',
    'NIFTY500':   'NSE:NIFTY500-INDEX',
    'GOLDBEES':   'NSE:GOLDBEES-EQ',
    'SILVERBEES': 'NSE:SILVERBEES-EQ',
}

SKIP_SYMBOLS    = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}

# ── Option chain config ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
OPTION_RETENTION_DAYS = 7      # option_chain stays lean (heaviest churn, not used by sim)
ATM_CHECK_MINS        = 15     # re-check ATM every 15 min
ATM_DRIFT_STRIKES     = 2      # re-subscribe if ATM drifts by this many strikes
N_STRIKES             = 30     # cc#320: INDEX options ATM±30 (61 strikes, was ±10/21) — fixes the
                               # PCR window-drift bias where strikes exited the tracked band as spot
                               # moved and silently dropped their OI from the PCR sums. Index only
                               # (NIFTY/BANKNIFTY); STOCK options stay STOCK_N_STRIKES=3. 7d retention
                               # (purge_old_bars) already covers the wider band. Capacity: index
                               # 84->244 contracts, feed total ~4170->~4330 (< ~5000 WS budget).
# cc#189 (founder redesign 04-Jul): options subscribe ONLY when live prices are
# fresh. No boot/REST hydration — a cold-boot/pre-market restart just waits for
# the market + a fresh cmp_prices tick set, then computes ATM from LIVE prices.
OPT_FRESH_MIN_FRAC    = 0.80          # >=80% of option underlyings must have a fresh tick
OPT_FRESH_WINDOW_MIN  = 10           # "fresh" = cmp_prices tick within the last N minutes
OPT_SUB_DEADLINE      = dt_time(9, 30)  # still unsubscribed by this IST time -> CRITICAL alert
OPT_STOCK_SUB_MIN_TIME = dt_time(9, 25)  # cc#241: HARD floor — no stock-option subscribe/write
                                          # before 09:25 IST (10 min for the open to settle so ATM
                                          # anchors on a real print). Index options are NOT gated.
OPT_STOCK_OVERFLOW_FRAC = 0.95           # cc#241: <95% stock underlyings subscribed -> overflow alert
BAR_MINUTES           = 5      # 5-min system: all rolling intraday bars at 5-min granularity
OI_POLL_MINS          = 5      # poll futures OI via DEPTH REST every N min (quotes has NO OI)
CMP_FLUSH_MINS        = 5      # flush cmp_prices every N min (was 30s; throttled 14-Jun-2026)
OI_CALL_SPACING_SEC   = 0.35   # ~170 req/min — under Fyers 200/min data limit

# ── feed heartbeat / health / watchdog (cc_task #84 + #85) ────────────────────
# The WS stream for the 212 stock futures crashed at the 09:15 open on 25-Jun and
# did not auto-reconnect until ~11:25 — a 2h15m data gap that fed stale prices to
# V8 paper, trade-check and the dashboard. These guard the live stream.
HEARTBEAT_STALE_MINS    = 10   # window for "wrote a live bar recently"
HEALTH_LOG_MINS         = 5    # log feed health every N min during market hours
FEED_CRITICAL_SYMBOLS   = 100  # cc_task #85 PART_3: < this writing in 10 min → log.error CRITICAL
WATCHDOG_MIN_SYMBOLS    = 50   # cc_task #85 PART_4: < this sustained → force WS reconnect
WATCHDOG_STALE_MINS     = 15   # consecutive minutes below WATCHDOG_MIN_SYMBOLS before reconnect
TOTAL_FUTURES           = 212  # denominator for the N/212 health log
RECONNECT_COOLDOWN_MINS = 10   # min gap between forced reconnects (anti-thrash)
# cc_task #112: socket-reconnect-only is REJECTED. If N consecutive forced reconnects
# fail to restore coverage, escalate to a HARD process restart (os.execv) so Railway
# relaunches a clean worker that re-subscribes all 212 symbols from scratch. This is
# the auto-restart that was the missing piece in the 4 prior recurrences.
WATCHDOG_MAX_RECONNECTS = 2    # forced socket reconnects before escalating to hard restart
CMP_STALE_GUARD_SECS    = 90   # cc_task #112: only (re)write a cmp_prices row when its tick is
                               # newer than the last flush — no fresh tick => no timestamp update
CMP_SANITY_MAX_DEV      = 0.02 # cc#367: reject a cmp write that deviates >2% from the symbol's most
                               # recent completed equity 5m bar (same session) — a polluted spot tick.
STARTUP_GRACE_MINS      = 10   # suppress the watchdog this long after 09:15 (bars need time to form)
OPEN_RACE_GUARD_SECS    = 60   # hold the first subscription until N s after 09:15 (NSE feed-init race)
WS_SUB_BATCH            = 200  # cc_task #88: subscribe in 200-symbol batches (Fyers silently drops bulk subs at open)
WS_SUB_BATCH_SLEEP_SEC  = 2    # seconds between subscription batches

NIFTY_STEP   = 50
BNIFTY_STEP  = 100
STOCK_STEPS  = {               # FALLBACK only (master-driven ladder is primary)
    'RELIANCE': 20, 'TCS': 50, 'HDFCBANK': 10, 'INFY': 10, 'ICICIBANK': 10,
    'HDFC': 10, 'SBIN': 5, 'BHARTIARTL': 5, 'KOTAKBANK': 20, 'LT': 20,
    'AXISBANK': 5, 'WIPRO': 5, 'MARUTI': 100, 'BAJFINANCE': 50, 'TITAN': 20,
}

INDEX_OPTION_UNDERLYINGS = {
    'NIFTY':     {'fyers_index': 'NSE:NIFTY50-INDEX',   'step': NIFTY_STEP,  'cmp_sym': 'NIFTY50'},
    'BANKNIFTY': {'fyers_index': 'NSE:NIFTYBANK-INDEX', 'step': BNIFTY_STEP, 'cmp_sym': 'BANKNIFTY'},
}

OPTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS option_chain (
    id          SERIAL PRIMARY KEY,
    symbol      TEXT    NOT NULL,
    underlying  TEXT    NOT NULL,
    strike      NUMERIC NOT NULL,
    option_type TEXT    NOT NULL,
    expiry      DATE    NOT NULL,
    ltp         NUMERIC,
    oi          BIGINT,
    volume      BIGINT,
    bid         NUMERIC,
    ask         NUMERIC,
    ts          TIMESTAMP NOT NULL,
    UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_option_chain_ts         ON option_chain(ts DESC);
CREATE INDEX IF NOT EXISTS idx_option_chain_underlying ON option_chain(underlying, ts DESC);
"""

FUTURES_BASIS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS futures_basis (
    id            SERIAL PRIMARY KEY,
    symbol        TEXT    NOT NULL,
    ts            TIMESTAMP NOT NULL,
    spot_close    NUMERIC,
    futures_close NUMERIC,
    basis         NUMERIC,
    basis_pct     NUMERIC,
    oi            BIGINT,
    oi_prev       BIGINT,
    oi_chg        BIGINT,
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_futures_basis_symbol_ts ON futures_basis(symbol, ts DESC);
"""

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')



# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def get_db(): return psycopg2.connect(DATABASE_URL)
def app_id_hash(): return hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()


def last_tuesday(y, m):
    """Last Tuesday of month y/m — NSE expiry since Sep 2025."""
    last_day = calendar.monthrange(y, m)[1]
    d = date(y, m, last_day)
    while d.weekday() != 1:   # 1 = Tuesday
        d = d.replace(day=d.day - 1)
    return d


def current_expiry() -> date:
    """Current active monthly expiry (last Tuesday). Rolls to next month after expiry."""
    today = date.today()
    exp = last_tuesday(today.year, today.month)
    if today > exp:
        if today.month == 12:
            exp = last_tuesday(today.year + 1, 1)
        else:
            exp = last_tuesday(today.year, today.month + 1)
    return exp


def futures_fyers_symbol(nse_code: str, expiry: date = None) -> str:
    """Build Fyers futures symbol e.g. NSE:SBIN26JUNFUT"""
    if expiry is None:
        expiry = current_expiry()
    return f"NSE:{nse_code}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}FUT"


def option_fyers_symbol(underlying: str, strike, opt_type: str, expiry: date = None) -> str:
    """Build Fyers option symbol e.g. NSE:NIFTY26JUN24000CE"""
    if expiry is None:
        expiry = current_expiry()
    strike_str = str(int(strike)) if float(strike) == int(strike) else str(strike)
    return f"NSE:{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike_str}{opt_type}"


def atm_strike(cmp: float, step: int) -> int:
    return int(round(cmp / step) * step)


def auto_step(cmp: float) -> int:
    """Derive option strike step from CMP when not in STOCK_STEPS (fallback only)."""
    if cmp < 100:   return 5
    if cmp < 500:   return 10
    if cmp < 1000:  return 20
    if cmp < 3000:  return 50
    if cmp < 10000: return 100
    return 200


# ── option symbol master (Fyers NSE_FO CSV — actually-listed contracts) ───────────

class OptionMaster:
    """
    Loads the Fyers public NSE_FO symbol master and exposes:
      * valid_symbols  — set of every listed NSE F&O ticker
      * atm_window()   — the actual listed strikes around CMP for an underlying/expiry
    Built at boot + reloaded on monthly roll. If download/parse fails,
    loaded=False and callers fall back to step-guessing (pre-v6 behaviour).
    CSV columns (no header, community-documented):
      8=expiry epoch, 9=symbol ticker, 13=underlying, 15=strike, 16=option type.
    """
    def __init__(self):
        self.valid_symbols = set()
        self.strikes       = {}     # (underlying, expiry_date, opt_type) -> sorted [strike]
        self.loaded        = False

    def load(self):
        self.valid_symbols, self.strikes, self.loaded = set(), {}, False
        try:
            r = requests.get(OPTION_MASTER_URL, timeout=30)
            r.raise_for_status()
            rows = 0
            for line in r.text.splitlines():
                parts = line.split(',')
                if len(parts) < 17:
                    continue
                ticker = parts[9].strip()
                if not ticker.startswith('NSE:'):
                    continue
                self.valid_symbols.add(ticker)
                otype = parts[16].strip().upper()
                if otype in ('CE', 'PE'):
                    try:
                        strike = float(parts[15])
                        und    = parts[13].strip().upper()
                        exp    = datetime.fromtimestamp(int(float(parts[8])), IST).date()
                        self.strikes.setdefault((und, exp, otype), []).append(strike)
                    except Exception:
                        continue
                rows += 1
            for k in self.strikes:
                self.strikes[k] = sorted(set(self.strikes[k]))
            self.loaded = rows > 1000   # sanity: a real master has thousands of rows
            log.info(f"Option master: {rows} contracts, {len(self.strikes)} strike chains, loaded={self.loaded}")
        except Exception as e:
            log.warning(f"Option master load FAILED ({e}) — falling back to step-guessing")
            self.loaded = False

    def atm_window(self, underlying, expiry, opt_type, cmp, n=N_STRIKES):
        """Up to 2n+1 ACTUAL listed strikes centered on CMP. None if chain unknown."""
        chain = self.strikes.get((underlying.upper(), expiry, opt_type))
        if not chain:
            return None
        i  = bisect.bisect_left(chain, cmp)
        lo = max(0, i - n)
        hi = min(len(chain), i + n + 1)
        return chain[lo:hi]

    def is_valid(self, ticker):
        return (not self.loaded) or (ticker in self.valid_symbols)


# ── DB / token ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def load_tokens(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token, refresh_token, access_created, refresh_created "
                    "FROM fyers_tokens WHERE id=1")
        return cur.fetchone()

def save_tokens(conn, access=None, refresh=None, new_refresh=False):
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM fyers_tokens WHERE id=1")
        if cur.fetchone():
            if new_refresh:
                cur.execute("""UPDATE fyers_tokens SET access_token=%s, refresh_token=%s,
                               access_created=%s, refresh_created=%s, updated_at=NOW() WHERE id=1""",
                            (access, refresh, now, now))
            else:
                cur.execute("UPDATE fyers_tokens SET access_token=%s, access_created=%s, updated_at=NOW() WHERE id=1",
                            (access, now))
        else:
            cur.execute("""INSERT INTO fyers_tokens (id,access_token,refresh_token,access_created,refresh_created,updated_at)
                           VALUES (1,%s,%s,%s,%s,NOW())""", (access, refresh, now, now))
    conn.commit()

def bootstrap_from_authcode(conn, auth_code):
    r = requests.post(AUTHCODE_URL, json={'grant_type':'authorization_code',
        'appIdHash':app_id_hash(),'code':auth_code}, timeout=10)
    d = r.json()
    if d.get('code') != 200: raise Exception(f"Auth-code exchange failed: {d}")
    save_tokens(conn, access=d['access_token'], refresh=d.get('refresh_token'), new_refresh=True)
    log.info("Bootstrap OK - access token stored (valid for today)")
    return d['access_token']

def _token_is_live(token):
    try:
        r = requests.get(QUOTES_URL,
                         params={'symbols': 'NSE:NIFTY50-INDEX'},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'},
                         timeout=8)
        return r.json().get('s') == 'ok'
    except Exception as e:
        log.warning(f"Token liveness check failed: {e}")
        return False

def get_valid_token(conn, auth_code=None):
    if auth_code:
        try:
            return bootstrap_from_authcode(conn, auth_code)
        except Exception as e:
            log.warning(f"Auth-code bootstrap failed ({e}); falling through")

    row = load_tokens(conn)
    if row and row[0] and row[2]:
        access_token, access_created = row[0], row[2]
        today = datetime.now(IST).replace(tzinfo=None).date()
        if access_created.date() == today:
            log.info("Stored same-day token found — verifying with Fyers...")
            if _token_is_live(access_token):
                log.info("Token verified live — reusing (restart-safe)")
                return access_token
            log.warning("Stored same-day token REJECTED — re-authing")
        else:
            log.warning(f"Stored token from {access_created.date()} — re-authing")

    try:
        import fyers_autologin
        log.info("Running TOTP auto-login (headless)...")
        token = fyers_autologin.auto_login(conn)
        log.info("TOTP auto-login SUCCESS — fresh token stored")
        return token
    except Exception as e:
        raise SystemExit(
            f"\nAUTO-LOGIN FAILED ({e}).\n"
            "Check env vars: FYERS_TOTP_SECRET, FYERS_PIN, FYERS_SECRET, FYERS_FY_ID.\n"
            "Manual fallback:\n"
            f"  1. https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}"
            "&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n"
            "  2. python fyers_feed.py --auth-code <code>\n")


# ── universe ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _ist_now_str():
    return datetime.now(IST).replace(tzinfo=None).isoformat()


def _ops_log(conn, category, title, details):
    """cc#339: one ops_log row (category=alert/info). Best-effort — never raises into the boot path."""
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                         VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                      (category, title, json.dumps(details)))
        conn.commit()
    except Exception as e:
        log.warning(f"_ops_log({title}) failed: {e}")


def _rest_quote_ok(token):
    """cc#339 fix_2: ONE REST quote self-test (NSE:SBIN-EQ). Returns (ok, detail). An EMPTY body is
    the exact dead-token signature that produced the 09-Jul 'Expecting value: line 1 column 1' spam,
    so it is treated as a hard failure (not a parse exception)."""
    try:
        r = requests.get(QUOTES_URL, params={'symbols': 'NSE:SBIN-EQ'},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=8)
        body = (r.text or '').strip()
        if not body:
            return False, 'EMPTY_BODY'
        return (r.json().get('s') == 'ok'), body[:180]
    except Exception as e:
        return False, f'exc:{e}'


def _boot_auth_selfcheck(conn, token):
    """cc#339 fix_2: BEFORE subscribing, prove the token can actually fetch a REST quote. On failure:
    CRITICAL log + ops_log(feed_token_dead) alert, retry auto-login ONCE, and if still dead exit(1)
    so Railway's restart-loop + the alert make it LOUD instead of silent warning spam. Returns the
    (possibly refreshed) valid token."""
    ok, detail = _rest_quote_ok(token)
    if ok:
        log.info("BOOT AUTH OK — REST quote self-test passed (NSE:SBIN-EQ)")
        _ops_log(conn, 'info', 'feed_boot_ok',
                 {'selftest': 'NSE:SBIN-EQ', 'result': 'ok', 'ist': _ist_now_str()})
        return token
    log.critical(f"TOKEN DEAD — REST returning empty/invalid bodies on boot self-test (detail={detail}). "
                 "Retrying auto-login ONCE...")
    _ops_log(conn, 'alert', 'feed_token_dead',
             {'selftest': 'NSE:SBIN-EQ', 'stage': 'boot', 'detail': str(detail)[:180], 'ist': _ist_now_str()})
    try:
        import fyers_autologin
        token = fyers_autologin.auto_login(conn)
        log.info("Auto-login retry SUCCESS — re-testing REST quote...")
    except Exception as e:
        log.critical(f"Auto-login retry FAILED ({e}) — exit(1) for a loud Railway restart")
        _ops_log(conn, 'alert', 'feed_token_dead',
                 {'stage': 'relogin_exception', 'detail': str(e)[:180], 'ist': _ist_now_str()})
        sys.exit(1)
    ok2, detail2 = _rest_quote_ok(token)
    if ok2:
        log.info("BOOT AUTH OK after re-login — REST quote self-test passed")
        _ops_log(conn, 'info', 'feed_boot_ok',
                 {'selftest': 'NSE:SBIN-EQ', 'result': 'ok_after_relogin', 'ist': _ist_now_str()})
        return token
    log.critical("TOKEN STILL DEAD after re-login — exit(1) so the failure is LOUD (Railway "
                 "restart-loop + feed_token_dead alert) rather than 2900 silent 'Expecting value' warns")
    _ops_log(conn, 'alert', 'feed_token_dead',
             {'selftest': 'NSE:SBIN-EQ', 'stage': 'post_relogin', 'detail': str(detail2)[:180], 'ist': _ist_now_str()})
    sys.exit(1)


def _boot_gap_report(conn):
    """cc#339 fix_3: gap-aware boot. Record the last 5m bar ts in DB + gap minutes so a mid-market
    reboot always self-documents its outage window (ops_log feed_boot_gap). Non-fatal."""
    try:
        with conn.cursor() as c:
            c.execute("""SELECT MAX(ts) FROM intraday_prices
                         WHERE ts::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date AND timeframe='5m'""")
            last = c.fetchone()[0]
        now_ist = datetime.now(IST).replace(tzinfo=None)
        mkt = is_trading_day(now_ist.date()) and MARKET_OPEN <= now_ist.time() <= MARKET_CLOSE
        if last is None:
            log.info("BOOT GAP: no 5m bars yet today (cold / pre-market boot)")
            _ops_log(conn, 'info', 'feed_boot_gap',
                     {'last_bar': None, 'gap_min': None, 'market_open': mkt, 'ist': now_ist.isoformat()})
            return
        gap_min = round((now_ist - last).total_seconds() / 60.0, 1)
        level = 'alert' if (mkt and gap_min > 12) else 'info'   # >12min mid-market = a real outage
        log.info(f"BOOT GAP: last 5m bar {last} — gap {gap_min} min (market_open={mkt})")
        _ops_log(conn, level, 'feed_boot_gap',
                 {'last_bar': str(last), 'gap_min': gap_min, 'market_open': mkt, 'ist': now_ist.isoformat()})
    except Exception as e:
        log.warning(f"_boot_gap_report failed (non-fatal): {e}")


def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE")
        futures = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS)


# cc#162: NIFTY/BANKNIFTY index futures — index futures were never subscribed
# on the live feed (SKIP_SYMBOLS excludes them from get_universe(), which feeds
# BOTH the equity leg -- correctly, no -EQ instrument exists for an index --
# AND the futures leg -- incorrectly, silently dropping real futures contracts
# that should be subscribed). This is a SEPARATE list, added ONLY to the
# futures leg, never the equity leg. Scope is intentionally just these two
# (task cc#162) -- SKIP_SYMBOLS also lists FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX
# but none of those are actually present in futures_universe.
INDEX_FUTURES_UNIVERSE = ('NIFTY', 'BANKNIFTY')

def get_index_futures_universe(conn):
    """Read live from futures_universe (same is_active pattern as get_universe)
    rather than hardcoding a blind subscribe, so an ops-side deactivation of
    either symbol is honored automatically, same as it already is for the 209
    stock futures."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM futures_universe WHERE is_active = TRUE "
            "AND symbol = ANY(%s)", (list(INDEX_FUTURES_UNIVERSE),))
        return sorted(r[0] for r in cur.fetchall())


def get_top50_option_underlyings(conn):
    """Top 50 futures stocks by mcap rank from input_raw."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT fu.symbol
                FROM futures_universe fu
                JOIN input_raw ir ON ir.nse_code = fu.symbol
                WHERE fu.is_active = TRUE
                  AND ir.mcap_rank <= 50
                ORDER BY fu.symbol
            """)
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"get_top50_option_underlyings: {e}")
        return []


STOCK_N_STRIKES = 3   # cc#155: stock options subscribe ATM±3 (index stays N_STRIKES=±10)


def get_all_option_underlyings(conn):
    """cc#155 (STOCK_OPTIONS_CHAIN_SPEC_V1, session_log 1173): ALL active futures-universe
    stock underlyings for stock options, excluding indices/SKIP_SYMBOLS (+NIFTY50). Ordered
    by mcap rank so a pilot/limit takes the most-liquid names first. Distinct from
    get_top50_option_underlyings (hard-capped at 50)."""
    try:
        excl = list(SKIP_SYMBOLS | {'NIFTY50'})
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fu.symbol
                FROM futures_universe fu
                LEFT JOIN input_raw ir ON ir.nse_code = fu.symbol
                WHERE fu.is_active = TRUE
                  AND fu.symbol <> ALL(%s)
                ORDER BY COALESCE(ir.mcap_rank, 999999), fu.symbol
            """, (excl,))
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"get_all_option_underlyings: {e}")
        return []


def _stock_options_config(conn):
    """cc#155: runtime gate for stock options, read from app_config so scope can be scaled or
    killed LIVE (no redeploy) — the phased rollout + kill-switch the spec's HARD 8/10 design
    requires. Defaults DISABLED => index-only feed unchanged (14-Jun lock preserved).
      stock_options_enabled = 'true'|'false'  (default false)
      stock_options_limit   = int underlyings  (default 20 pilot; <=0 = all 209)
      stock_options_n       = ATM± strikes     (default 3)"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_config WHERE key = ANY(%s)",
                        (['stock_options_enabled', 'stock_options_limit', 'stock_options_n'],))
            cfg = {k: v for k, v in cur.fetchall()}
        enabled = str(cfg.get('stock_options_enabled', 'false')).strip().lower() == 'true'
        limit = int(cfg.get('stock_options_limit') or 20)
        n = int(cfg.get('stock_options_n') or STOCK_N_STRIKES)
        return enabled, limit, n
    except Exception as e:
        log.warning(f"_stock_options_config: {e} — defaulting to index-only")
        return False, 20, STOCK_N_STRIKES


def _cmp_fresh_fraction(conn, opt_mgr, kind=None):
    """cc#189: fraction of option underlyings whose cmp_prices row was updated
    within the last OPT_FRESH_WINDOW_MIN minutes. Drives the 'subscribe options
    ONLY when live prices are fresh' gate. cmp_prices.updated_at and NOW() are
    both the DB clock, so the window is timezone-agnostic. cc#241: kind filters to
    index-only / stock-only underlyings so the index gate is never blocked by stock
    cmp freshness (and vice-versa)."""
    if not opt_mgr._underlyings:
        opt_mgr._build_underlyings()
    syms = [u['cmp_sym'] for u in opt_mgr._underlyings if (kind is None or u.get('kind') == kind)]
    if not syms:
        return 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT symbol) FROM cmp_prices "
                "WHERE symbol = ANY(%s) AND updated_at >= NOW() - INTERVAL '"
                + str(int(OPT_FRESH_WINDOW_MIN)) + " minutes'",
                (syms,))
            fresh = cur.fetchone()[0] or 0
    except Exception as e:
        log.warning(f"_cmp_fresh_fraction: {e}")
        return 0.0
    return fresh / len(syms)


def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')

def from_fyers_symbol(fsym):
    if fsym == 'NSE:M&M-EQ': return 'M&M'
    if 'FUT' in fsym:
        inner = fsym.replace('NSE:', '')
        # cc#148: was ^([A-Z&]+)\d{2}[A-Z]{3}FUT$ — only [A-Z&], so digit/hyphen
        # tickers (360ONE, BAJAJ-AUTO, NAM-INDIA) failed to match and the RAW
        # contract name (e.g. "360ONE26JULFUT") leaked into intraday_prices.
        # Non-greedy base group now allows digits/hyphens in the ticker itself.
        m = re.match(r'^([A-Z0-9&-]+?)(\d{2}[A-Z]{3})FUT$', inner)
        if m: return m.group(1)
        return inner
    return fsym.replace('NSE:', '').replace('-EQ', '')


# ── option symbol manager ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class OptionSymbolManager:
    """
    Manages option symbol subscriptions.
    Tracks current ATM per underlying, rebuilds on drift or monthly roll.
    v6: strike ladders come from the Fyers symbol master (actually-listed strikes);
    step-guessing remains only as a fallback when the master fails to load.
    """
    def __init__(self, conn, token=None, master: 'OptionMaster' = None):
        self.conn         = conn
        self.token        = token
        self.master       = master
        self.lock         = threading.Lock()
        self.expiry       = current_expiry()
        self.atm_map      = {}   # underlying -> current ATM strike
        self.sym_map      = {}   # fyers_option_symbol -> (underlying, strike, opt_type, expiry)
        self._underlyings = []

    def _build_underlyings(self):
        # cc#155 (STOCK_OPTIONS_CHAIN_SPEC_V1, session_log 1173): index underlyings are ALWAYS
        # present at ATM±N_STRIKES (±10). Stock underlyings (ATM±stock_n, default ±3) are
        # ADDITIVE and config-gated via app_config (_stock_options_config). Default OFF keeps
        # the 14-Jun INDEX-ONLY lock intact; an operator enables + scales the pilot LIVE (no
        # redeploy) and can kill it instantly. Additive-only: stock options never displace the
        # index/eq/fut subscription (the 06-Jul regression that zeroed index ATM±10).
        out = []
        for name, meta in INDEX_OPTION_UNDERLYINGS.items():
            out.append({'name': name, 'step': meta['step'], 'cmp_sym': meta['cmp_sym'],
                        'n': N_STRIKES, 'kind': 'index'})
        enabled, limit, stock_n = _stock_options_config(self.conn)
        n_stock = 0
        if enabled:
            index_names = set(INDEX_OPTION_UNDERLYINGS)
            stocks = [s for s in get_all_option_underlyings(self.conn) if s not in index_names]
            if limit and limit > 0:
                stocks = stocks[:limit]
            for s in stocks:
                out.append({'name': s, 'step': None, 'cmp_sym': s, 'n': stock_n, 'kind': 'stock'})
            n_stock = len(stocks)
        self._underlyings = out
        log.info(f"OptionSymbolManager: {len(out)} underlyings "
                 f"({len(INDEX_OPTION_UNDERLYINGS)} index"
                 + (f" + {n_stock} stock ATM±{stock_n}, limit={limit}" if enabled
                    else "-only; stock options disabled") + ")")

    def _get_cmp(self, cmp_sym, allow_rest=False):
        # cc#189: the AUTOMATIC subscribe path uses LIVE cmp_prices only (allow_rest
        # defaults False) — the housekeeping gate only calls build_initial once
        # cmp_prices is fresh, so ATM strikes come from live prices. The Fyers REST
        # quotes fallback is RETAINED (founder 04-Jul: keep REST for on-demand
        # fallback) and used only when a caller explicitly passes allow_rest=True.
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT cmp FROM cmp_prices WHERE symbol = %s", (cmp_sym,))
                r = cur.fetchone()
                if r and r[0]:
                    return float(r[0])
        except Exception:
            pass
        if not allow_rest or not self.token:
            return None
        # on-demand REST fallback: pull live CMP straight from the Fyers quotes API
        try:
            meta = INDEX_OPTION_UNDERLYINGS.get(cmp_sym)
            fsym = meta['fyers_index'] if meta else fyers_eq_symbol(cmp_sym)
            resp = requests.get(QUOTES_URL, params={'symbols': fsym},
                                headers={'Authorization': f'{FYERS_CLIENT_ID}:{self.token}'},
                                timeout=5)
            d = resp.json()
            if d.get('s') == 'ok':
                for item in d.get('d', []):
                    lp = item.get('v', {}).get('lp')
                    if lp:
                        # cc#229 (id166 permanent fix): seed cmp_prices with the REST CMP so a
                        # cold-boot EMPTY table gets populated -> the option/ATM build never
                        # silently skips and any-time restart is safe.
                        try:
                            with self.conn.cursor() as _c:
                                _c.execute(
                                    "INSERT INTO cmp_prices (symbol, cmp, updated_at, source) "
                                    "VALUES (%s,%s,NOW(),'fyers_rest') "
                                    "ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, "
                                    "updated_at=EXCLUDED.updated_at, source='fyers_rest'",
                                    (cmp_sym, float(lp)))
                            self.conn.commit()
                        except Exception as _se:
                            try: self.conn.rollback()
                            except Exception: pass
                            log.warning(f"_get_cmp seed cmp_prices {cmp_sym}: {_se}")
                        return float(lp)
        except Exception as e:
            log.warning(f"_get_cmp Fyers REST fallback {cmp_sym}: {e}")
        return None

    def _ladder(self, u, cmp):
        """
        Returns list of (strike, opt_type) for ATM±N.
        Primary: actual listed strikes from the symbol master.
        Fallback: step-based generation (pre-v6).
        """
        pairs = []
        n = u.get('n', N_STRIKES)   # cc#155: per-underlying window (index ±10, stock ±3)
        if self.master and self.master.loaded:
            ce = self.master.atm_window(u['name'], self.expiry, 'CE', cmp, n=n)
            pe = self.master.atm_window(u['name'], self.expiry, 'PE', cmp, n=n)
            if ce or pe:
                for s in (ce or []): pairs.append((s, 'CE'))
                for s in (pe or []): pairs.append((s, 'PE'))
                return pairs
            log.warning(f"_ladder: no master chain for {u['name']} {self.expiry} — step fallback")
        step = u['step'] or auto_step(cmp)
        atm  = atm_strike(cmp, step)
        for i in range(-n, n + 1):
            strike = atm + i * step
            if strike <= 0: continue
            pairs.append((strike, 'CE'))
            pairs.append((strike, 'PE'))
        return pairs

    def build_initial(self, allow_rest=False, kind=None):
        """Returns list of Fyers option symbols to subscribe. cc#189: the automatic
        live-price gate calls this with allow_rest=False (cmp_prices only); an
        on-demand caller may pass allow_rest=True to use the Fyers REST CMP
        fallback (retained per founder 04-Jul). cc#241: kind selects which underlyings
        to build — 'index' (early gate), 'stock' (09:25 gate, MERGED additively into
        sym_map so the index chains are never wiped), or None (full rebuild / monthly roll)."""
        # cc#229 (id166 permanent fix): on a cold boot cmp_prices can be EMPTY (worker
        # restarted pre-open or after downtime). Empty cmp_prices -> zero underlyings resolve
        # a CMP -> zero option subscriptions (the known zombie). Detect empty and force the
        # Fyers REST CMP path so prices are fetched + seeded and subscriptions never skip.
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM cmp_prices")
                if (cur.fetchone()[0] or 0) == 0 and self.token:
                    allow_rest = True
                    log.warning("build_initial: cmp_prices EMPTY (cold boot) — forcing Fyers REST "
                                "CMP fetch to seed prices before subscribe (cc#229/id166)")
        except Exception:
            pass
        self._build_underlyings()
        self.expiry = current_expiry()
        symbols = []
        unders = [u for u in self._underlyings if (kind is None or u.get('kind') == kind)]
        with self.lock:
            if kind is None:                       # full rebuild: wipe + rebuild everything
                self.sym_map = {}
                self.atm_map = {}
                self.built_per_underlying = {}
            elif not hasattr(self, 'built_per_underlying'):
                self.built_per_underlying = {}     # cc#241: kind build MERGES (never wipes index)
            for u in unders:
                cmp = self._get_cmp(u['cmp_sym'], allow_rest=allow_rest)
                if not cmp:
                    self.built_per_underlying[u['name']] = 0
                    log.warning(f"No CMP for {u['cmp_sym']} — skipping options")
                    continue
                step = u['step'] or auto_step(cmp)
                self.atm_map[u['name']] = atm_strike(cmp, step)
                before = len(symbols)
                for strike, otype in self._ladder(u, cmp):
                    fsym = option_fyers_symbol(u['name'], strike, otype, self.expiry)
                    if self.master and not self.master.is_valid(fsym):
                        continue
                    self.sym_map[fsym] = (u['name'], strike, otype, self.expiry)
                    symbols.append(fsym)
                self.built_per_underlying[u['name']] = len(symbols) - before
        log.info(f"OptionSymbolManager: built {len(symbols)} {kind or 'all'} option symbols "
                 f"({'master' if self.master and self.master.loaded else 'step-fallback'})")
        return symbols

    def subscribe_health(self, kind=None):
        """cc#189: (underlyings_total, underlyings_ok, missing_names, contracts) from the last
        build — drives the subscribed-vs-expected alert (an underlying with 0 contracts = a
        miss). cc#241: kind filters to index-only / stock-only (the stock overflow alert reads
        kind='stock')."""
        per = getattr(self, 'built_per_underlying', {})
        names = [u['name'] for u in self._underlyings if (kind is None or u.get('kind') == kind)]
        if not names and kind is None:
            names = list(per.keys())
        total = len(names)
        ok = sum(1 for n in names if per.get(n, 0) > 0)
        missing = sorted(n for n in names if per.get(n, 0) == 0)
        contracts = sum(per.get(n, 0) for n in names)
        return total, ok, missing, contracts

    def index_option_syms(self, syms):
        """cc#155: index-only subset of the given option syms — for OI depth polling. Stock
        options are WS-ONLY, NO REST OI (spec 1173): ~2912 stock depth calls = ~27min, which
        would blow the 5-min bar cadence. Index OI poll (~136 syms) stays unchanged."""
        idx = set(INDEX_OPTION_UNDERLYINGS)
        return [s for s in syms if (self.sym_map.get(s) or ('',))[0] in idx]

    def stock_option_syms(self, syms):
        """cc#375: stock-only subset of the given (subscribed) option syms — for a SEPARATE OI depth
        poll so stock option_chain rows carry OI (the WS strips it; without this poll oi stays NULL
        and the cockpit ATM OI d/d is always '--'). Complements index_option_syms. Bounded by the
        subscribed set (app_config stock_options_limit, pilot default 20 underlyings ~= a few hundred
        syms), which fits the 5-min bar; at full 209-stock scale the caller's separate lock lets a
        long cycle skip gracefully rather than delay the index poll."""
        idx = set(INDEX_OPTION_UNDERLYINGS)
        return [s for s in syms if s in self.sym_map and (self.sym_map.get(s) or ('',))[0] not in idx]

    def check_atm_drift(self):
        """Returns (add_syms, remove_syms) if any ATM has drifted >= ATM_DRIFT_STRIKES."""
        add, remove = [], []
        with self.lock:
            for u in self._underlyings:
                cmp = self._get_cmp(u['cmp_sym'])
                if not cmp: continue
                step    = u['step'] or auto_step(cmp)
                new_atm = atm_strike(cmp, step)
                old_atm = self.atm_map.get(u['name'])
                if old_atm is None: continue
                drift = abs(new_atm - old_atm) // step
                if drift < ATM_DRIFT_STRIKES: continue
                log.info(f"ATM drift {u['name']}: {old_atm} → {new_atm} ({drift} strikes)")
                old_syms = [s for s, v in self.sym_map.items() if v[0] == u['name']]
                for s in old_syms:
                    del self.sym_map[s]; remove.append(s)
                self.atm_map[u['name']] = new_atm
                for strike, otype in self._ladder(u, cmp):
                    fsym = option_fyers_symbol(u['name'], strike, otype, self.expiry)
                    if self.master and not self.master.is_valid(fsym):
                        continue
                    self.sym_map[fsym] = (u['name'], strike, otype, self.expiry)
                    add.append(fsym)
        return add, remove

    def check_monthly_roll(self):
        """Returns True if expiry rolled to next month."""
        new_expiry = current_expiry()
        if new_expiry != self.expiry:
            log.info(f"Monthly roll: {self.expiry} → {new_expiry}")
            self.expiry = new_expiry
            if self.master:
                self.master.load()   # refresh listed contracts for the new series
            return True
        return False

    def lookup(self, fsym):
        with self.lock:
            return self.sym_map.get(fsym)


# ── bar aggregator ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class BarAggregator:
    def __init__(self, conn):
        self.conn     = conn
        self.bars     = {}
        self.last_ltp = {}
        # cc_task #112: per-symbol time of the LAST GENUINE tick + time of the last
        # cmp flush. flush_cmp uses these so a symbol with no fresh tick is never
        # re-stamped — stops the stale-write masking that made a dead feed look healthy.
        self.last_ltp_ts        = {}   # symbol -> datetime of most recent real tick
        self._last_cmp_flush_ts = None # datetime of the last successful cmp flush
        self.last_oi  = {}   # symbol -> latest OI from depth REST poll (futures)
        # RLock (re-entrant): flush_all holds it while _flush → _compute_basis
        # runs; a plain Lock here deadlocked the whole feed (v6.1 fix).
        self.lock     = threading.RLock()

    def _bucket(self, ts):
        # 5-min bucket: round down to nearest 5-min boundary
        return ts.replace(minute=ts.minute - ts.minute % BAR_MINUTES, second=0, microsecond=0)

    def on_tick(self, sym, ltp, vol, ts=None, source='fyers_eq', oi=None):
        ts  = ts or datetime.now(IST).replace(tzinfo=None)
        bkt = self._bucket(ts)
        key = (sym, source)
        with self.lock:
            # cc#367: last_ltp/last_ltp_ts feed cmp_prices (SPOT snapshot) via flush_cmp ONLY.
            # The bar dict is keyed by (sym, source) so eq & fut bars stay separate, but last_ltp
            # was keyed by sym alone — a futures tick (source='fyers_fut') would overwrite the
            # spot LTP with a basis-premium/discount price (3-4% off), and flush_cmp then wrote
            # that fut price into cmp_prices as if it were spot. Only spot ticks may set last_ltp.
            if source != 'fyers_fut':
                self.last_ltp[sym]    = ltp
                self.last_ltp_ts[sym] = ts   # cc_task #112: mark when this genuine tick arrived
            bar = self.bars.get(key)
            if bar is None or bar['ts'] != bkt:
                if bar is not None:
                    self._flush(key, bar)
                self.bars[key] = {'ts': bkt, 'o': ltp, 'h': ltp, 'l': ltp,
                                  'c': ltp, 'v': vol or 0, 'oi': oi, 'source': source}
            else:
                bar['h'] = max(bar['h'], ltp)
                bar['l'] = min(bar['l'], ltp)
                bar['c'] = ltp
                if vol: bar['v'] = vol
                if oi is not None: bar['oi'] = oi

    def _flush(self, key, bar):
        sym, source = key
        try:
            # cc#193: NEVER persist an off-hours bar. Fyers streams phantom ticks
            # on non-trading days and outside 09:15-15:30 (garbage levels — e.g.
            # Sat 04-Jul BANKNIFTY 64,043 while the real Friday close was 58,255).
            # Only bars inside a real trading session are real data. Was: only
            # ts.time() >= MARKET_CLOSE rejected (no trading-day/pre-open guard).
            bt = bar['ts']
            if (not is_trading_day(bt.date())) or bt.time() < MARKET_OPEN or bt.time() >= MARKET_CLOSE:
                return
        except Exception:
            pass
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'5m',%s)
                    ON CONFLICT (symbol,ts,timeframe,source) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume
                """, (sym, bar['ts'], bar['o'], bar['h'], bar['l'], bar['c'],
                      int(bar['v']), source))
            self.conn.commit()
            if source == 'fyers_fut':
                self._compute_basis(sym, bar['ts'], bar['c'], bar.get('oi'))
        except Exception as e:
            log.warning(f"flush {sym} ({source}): {e}")

    def _compute_basis(self, sym, ts, fut_close, oi=None):
        """On futures bar flush: store basis + OI + OI change vs prior bar."""
        try:
            # Fallback: bar carried no OI (tick pre-dated first poll) — read the
            # latest polled value. NO LOCK here: caller may already hold agg.lock
            # (flush_all path) and CPython dict .get is GIL-atomic anyway.
            if oi is None:
                oi = self.last_oi.get(sym)
            # cc#162: NIFTY the futures-contract root symbol differs from
            # NIFTY50, the canonical spot index symbol used everywhere else in
            # this codebase (market_mood, v8, cmp_prices, raw_prices all key
            # off NIFTY50). Without this alias every spot lookup below misses
            # and basis stays permanently NULL. BANKNIFTY needs no alias — its
            # futures root already matches the spot key used system-wide.
            # futures_basis.symbol itself still stores `sym` (the contract
            # identity), only the SPOT lookups use the alias.
            spot_sym = 'NIFTY50' if sym == 'NIFTY' else sym
            with self.conn.cursor() as cur:
                # Spot = nearest non-futures intraday bar for this symbol at/before ts.
                # (exact ts + source='fyers_eq' missed: eq feed is sparse & ts-misaligned;
                #  bulk spot data is source='fyers'. Match nearest, exclude fyers_fut self.)
                cur.execute("""
                    SELECT close FROM intraday_prices
                    WHERE symbol=%s AND ts::date=%s::date AND ts<=%s AND source<>'fyers_fut'
                    ORDER BY ts DESC LIMIT 1
                """, (spot_sym, ts, ts))
                row       = cur.fetchone()
                spot      = float(row[0]) if row else None
                # Fallback: the equity bar for this 5-min bucket may not be flushed
                # yet (eq/fut flush on the same boundary but not simultaneously), so
                # the lookup above can miss → spot None → NULL basis. Fall back to
                # the live CMP (refreshed every CMP_FLUSH_MINS), then prior EOD close.
                if spot is None:
                    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (spot_sym,))
                    r2 = cur.fetchone()
                    spot = float(r2[0]) if r2 and r2[0] is not None else None
                if spot is None:
                    cur.execute("SELECT close FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1", (spot_sym,))
                    r3 = cur.fetchone()
                    spot = float(r3[0]) if r3 and r3[0] is not None else None
                basis     = round(fut_close - spot, 4) if spot is not None else None
                basis_pct = round((fut_close - spot) / spot * 100, 4) if spot else None
                # prior bar OI for this symbol (most recent non-null before this ts)
                oi_prev = None
                if oi is not None:
                    cur.execute("""
                        SELECT oi FROM futures_basis
                        WHERE symbol=%s AND oi IS NOT NULL AND ts < %s
                        ORDER BY ts DESC LIMIT 1
                    """, (sym, ts))
                    pr = cur.fetchone()
                    oi_prev = int(pr[0]) if pr and pr[0] is not None else None
                oi_chg = (int(oi) - oi_prev) if (oi is not None and oi_prev is not None) else None
                cur.execute("""
                    INSERT INTO futures_basis (symbol, ts, spot_close, futures_close, basis, basis_pct, oi, oi_prev, oi_chg)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        spot_close=EXCLUDED.spot_close, futures_close=EXCLUDED.futures_close,
                        basis=EXCLUDED.basis, basis_pct=EXCLUDED.basis_pct,
                        oi=EXCLUDED.oi, oi_prev=EXCLUDED.oi_prev, oi_chg=EXCLUDED.oi_chg
                """, (sym, ts, spot, fut_close, basis, basis_pct,
                      int(oi) if oi is not None else None, oi_prev, oi_chg))
            self.conn.commit()
        except Exception as e:
            log.warning(f"_compute_basis {sym}: {e}")

    def flush_all(self):
        with self.lock:
            for key, bar in list(self.bars.items()):
                self._flush(key, bar)

    def flush_cmp(self):
        """cc_task #112 — STOP STALE-WRITE MASKING (most critical fix).
        Only (re)write a cmp_prices row for a symbol that received a GENUINE tick
        since the last flush. The updated_at stamped is the real tick time, never
        a blanket now(). A symbol with no fresh tick is left untouched so its
        updated_at ages truthfully. If the WS is dead, ZERO rows are written and
        cmp_prices freshness goes stale on its own — so health checks finally see
        the truth instead of a feed that lies while frozen."""
        _ist = datetime.now(IST).replace(tzinfo=None)
        prev = self._last_cmp_flush_ts
        # tolerate the very first flush (prev=None) with a short look-back window so a
        # symbol that ticked just before boot is still written once.
        cutoff = prev if prev is not None else (_ist - timedelta(seconds=CMP_STALE_GUARD_SECS))
        with self.lock:
            rows = [(s, p, self.last_ltp_ts.get(s))
                    for s, p in self.last_ltp.items()
                    if p and self.last_ltp_ts.get(s) is not None
                    and self.last_ltp_ts[s] > cutoff]
        if not rows:
            log.warning("CMP flush SKIPPED — 0 fresh ticks since last flush "
                        "(WS feed likely dead; NOT stamping stale prices)")
            return
        # cc#367: SANITY GUARD (defense-in-depth behind the spot-only last_ltp fix above). A genuine
        # spot tick must sit within CMP_SANITY_MAX_DEV of the symbol's most recent completed equity
        # 5m bar in the SAME session; a larger gap = a polluted tick (fut LTP that slipped through,
        # corrupt post-close tick, symbol mis-map). Reject those writes and record them to ops_log so
        # the eq snapshot can't be corrupted. Best-effort — on any error we write `rows` unchanged.
        try:
            _syms = [r[0] for r in rows]
            _ref = {}
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (symbol) symbol, close FROM intraday_prices
                    WHERE source='fyers_eq' AND timeframe='5m'
                      AND ts::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                      AND symbol = ANY(%s)
                    ORDER BY symbol, ts DESC
                """, (_syms,))
                for _s, _c in cur.fetchall():
                    if _c: _ref[_s] = float(_c)
            _kept, _rejected = [], []
            for r in rows:
                base = _ref.get(r[0])
                if base and base > 0 and abs(float(r[1]) / base - 1.0) > CMP_SANITY_MAX_DEV:
                    _rejected.append((r[0], round(float(r[1]), 2), round(base, 2),
                                      round((float(r[1]) / base - 1.0) * 100, 2)))
                else:
                    _kept.append(r)
            if _rejected:
                _ops_log(self.conn, 'alert', 'cmp_guard_reject',
                         {"n": len(_rejected), "threshold_pct": CMP_SANITY_MAX_DEV * 100,
                          "rejected": [{"symbol": s, "cmp": p, "eq_bar_close": b, "dev_pct": d}
                                       for s, p, b, d in _rejected[:40]]})
                log.warning(f"CMP guard: rejected {len(_rejected)} polluted tick(s) "
                            f">{CMP_SANITY_MAX_DEV*100:.0f}% off eq bar: "
                            + ", ".join(f"{s}({d:+.1f}%)" for s, _, _, d in _rejected[:8]))
            rows = _kept
        except Exception as e:
            log.warning(f"flush_cmp sanity guard skipped (writing rows unchanged): {e}")
        if not rows:
            return
        try:
            with self.conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO cmp_prices (symbol, cmp, updated_at, source)
                    VALUES (%s, %s, %s, 'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET
                        cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='fyers'
                """, rows)
            self.conn.commit()
            self._last_cmp_flush_ts = _ist
            log.info(f"CMP flushed: {len(rows)} fresh symbols")
        except Exception as e:
            log.warning(f"flush_cmp: {e}")


# ── option bar store ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class OptionBarStore:
    """Stores 5-min option ticks into option_chain."""
    def __init__(self, conn, opt_mgr: OptionSymbolManager):
        self.conn    = conn
        self.opt_mgr = opt_mgr
        self.bars    = {}
        self.lock    = threading.RLock()
        self.last_oi = {}   # fyers_option_symbol -> latest OI from DEPTH poll (WS strips OI)

    def _bucket(self, ts):
        # 5-min bucket
        return ts.replace(minute=ts.minute - ts.minute % BAR_MINUTES, second=0, microsecond=0)

    def on_tick(self, fsym, ltp, oi=None, vol=None, bid=None, ask=None, ts=None):
        ts  = ts or datetime.now(IST).replace(tzinfo=None)
        bkt = self._bucket(ts)
        key = (fsym, bkt)
        with self.lock:
            existing = self.bars.get(key)
            if existing is None or existing['bkt'] != bkt:
                if existing is not None:
                    self._flush(fsym, existing)
                self.bars[key] = {'bkt': bkt, 'ltp': ltp, 'oi': oi,
                                  'vol': vol, 'bid': bid, 'ask': ask}
            else:
                existing['ltp'] = ltp
                if oi  is not None: existing['oi']  = oi
                if vol is not None: existing['vol'] = vol
                if bid is not None: existing['bid'] = bid
                if ask is not None: existing['ask'] = ask

    def _flush(self, fsym, bar):
        # cc#193: same off-hours guard as the equity/futures aggregator — never
        # persist an option bar outside a real trading session (phantom weekend
        # ticks are garbage).
        try:
            bt = bar['bkt']
            if (not is_trading_day(bt.date())) or bt.time() < MARKET_OPEN or bt.time() >= MARKET_CLOSE:
                return
        except Exception:
            pass
        meta = self.opt_mgr.lookup(fsym)
        if not meta: return
        underlying, strike, otype, expiry = meta
        # WS strips OI (Fyers SDK pops it) -> fall back to the DEPTH-poll value.
        oi = bar['oi'] if bar.get('oi') is not None else self.last_oi.get(fsym)
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO option_chain
                        (symbol, underlying, strike, option_type, expiry, ltp, oi, volume, bid, ask, ts)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        ltp=EXCLUDED.ltp, oi=EXCLUDED.oi, volume=EXCLUDED.volume,
                        bid=EXCLUDED.bid, ask=EXCLUDED.ask
                """, (fsym, underlying, strike, otype, expiry,
                      bar['ltp'], oi, bar['vol'], bar['bid'], bar['ask'], bar['bkt']))
            self.conn.commit()
        except Exception as e:
            log.warning(f"option_bar flush {fsym}: {e}")

    def flush_all(self):
        with self.lock:
            for (fsym, _), bar in list(self.bars.items()):
                self._flush(fsym, bar)


# ── index LTP ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def update_index_ltp(conn, token, agg=None):
    try:
        r = requests.get(QUOTES_URL, params={'symbols': ','.join(INDEX_LTP_SYMBOLS.values())},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=5)
        d = r.json()
        if d.get('s') != 'ok': return
        _ist = datetime.now(IST).replace(tzinfo=None)
        rows = []
        for item in d.get('d', []):
            lp = item['v'].get('lp', 0)
            if not lp: continue
            for name, fsym in INDEX_LTP_SYMBOLS.items():
                if fsym == item['n']:
                    rows.append((name, lp, _ist))
                    if agg is not None:
                        agg.on_tick(name, float(lp), 0, source='fyers_eq')
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,%s,'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='fyers'""", rows)
            conn.commit()
    except Exception as e:
        log.warning(f"Index LTP: {e}")


# ── purge ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def purge_old_bars(conn):
    now          = datetime.now(IST).replace(tzinfo=None)
    # cc#297: fyers_eq → 365d (long sim/BT7 history); futures_basis → 7d (the INTRADAY_FUT window,
    # NOT the equity constant — decoupled so a future equity bump can't silently drag it along).
    eq_cutoff    = now - timedelta(days=EQUITY_RETENTION_DAYS)             # intraday fyers_eq (730d, 2yr sim history)
    hist_cutoff  = now - timedelta(days=HIST_RETENTION_DAYS)              # cc#381: fyers_hist warehouse (730d, 2yr rolling)
    basis_cutoff = now - timedelta(days=INTRADAY_FUT_RETENTION_DAYS)       # futures_basis (7d, matches fyers_fut)
    fut_cutoff   = now - timedelta(days=INTRADAY_FUT_RETENTION_DAYS)       # intraday fyers_fut + legacy (7d)
    opt_cutoff   = now - timedelta(days=OPTION_RETENTION_DAYS)            # option_chain (7d, leaner)
    try:
        with conn.cursor() as cur:
            # cc#227: SOURCE-AWARE intraday retention. fyers_eq (canonical equity, cc#228) keeps
            # 365d (cc#297) for BT7/sim history; fyers_fut keeps 7d; residual legacy fyers/yahoo keep
            # 7d (shrinking once the cc#228 relabel/dedupe lands). IS DISTINCT FROM handles any NULL.
            # cc#377/381: source='fyers_hist' (backtest warehouse) rolls on its OWN 2yr window (730d,
            # HIST_RETENTION_DAYS) — was purge-exempt in cc#377; cc#381 gives it a cutoff so it rolls
            # instead of growing forever. Still excluded from the 7d "other" rule below.
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source='fyers_eq'", (eq_cutoff,))
            eq_del = cur.rowcount
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source='fyers_hist'", (hist_cutoff,))
            hist_del = cur.rowcount
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source IS DISTINCT FROM 'fyers_eq' AND source IS DISTINCT FROM 'fyers_hist'", (fut_cutoff,))
            other_del = cur.rowcount
            cur.execute("DELETE FROM option_chain WHERE ts < %s", (opt_cutoff,))
            opt_del = cur.rowcount
            cur.execute("DELETE FROM futures_basis WHERE ts < %s", (basis_cutoff,))
            basis_del = cur.rowcount
        conn.commit()
        log.info(f"Purged intraday: fyers_eq={eq_del} (>{EQUITY_RETENTION_DAYS}d), "
                 f"fyers_hist={hist_del} (>{HIST_RETENTION_DAYS}d), "
                 f"fut/legacy={other_del} (>{INTRADAY_FUT_RETENTION_DAYS}d); "
                 f"option_chain={opt_del} (>{OPTION_RETENTION_DAYS}d), "
                 f"futures_basis={basis_del} (>{INTRADAY_FUT_RETENTION_DAYS}d)")
    except Exception as e:
        log.warning(f"purge_old_bars: {e}")


def ensure_schemas(conn):
    with conn.cursor() as cur:
        cur.execute(OPTION_SCHEMA_SQL)
        cur.execute(FUTURES_BASIS_SCHEMA_SQL)
        cur.execute("ALTER TABLE futures_basis ADD COLUMN IF NOT EXISTS oi BIGINT, "
                    "ADD COLUMN IF NOT EXISTS oi_prev BIGINT, ADD COLUMN IF NOT EXISTS oi_chg BIGINT")
        cur.execute("ALTER TABLE cmp_prices ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'fyers'")
    conn.commit()
    log.info("Schemas ready (option_chain, futures_basis)")


# ── futures OI poll (DEPTH REST — quotes API has NO OI) ───────────────────────────────────────────────────────────────────────────────

_OI_POLL_LOCK = threading.Lock()

def poll_futures_oi(token, fut_syms, agg):
    """
    Fyers quotes API has NO OI (KB-confirmed) — depth API is the only source.
    1 symbol/call, rate-limited ~170 req/min. Runs in a background thread.
    Latest OI → agg.last_oi → attached on next futures bar flush → futures_basis.
    Debug: logs the raw response of the FIRST symbol each cycle for diagnosis.
    """
    if not _OI_POLL_LOCK.acquire(blocking=False):
        log.info("OI poll skipped — previous cycle still running")
        return
    try:
        log.info(f"OI poll starting: {len(fut_syms)} futures via depth API")
        headers = {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
        got, first = 0, True
        for fsym in fut_syms:
            try:
                r = requests.get(DEPTH_URL,
                                 params={'symbol': fsym, 'ohlcv_flag': 1},
                                 headers=headers, timeout=8)
                if first:
                    log.info(f"OI poll debug {fsym}: HTTP {r.status_code} body={r.text[:300]}")
                    first = False
                d = r.json()
                if d.get('s') != 'ok':
                    continue
                data_d = d.get('d')
                node = {}
                if isinstance(data_d, dict):
                    node = data_d.get(fsym) or (next(iter(data_d.values())) if data_d else {})
                elif isinstance(data_d, list) and data_d and isinstance(data_d[0], dict):
                    node = data_d[0].get('v', data_d[0])
                oi = node.get('oi') if isinstance(node, dict) else None
                if oi is None:
                    continue
                nse = from_fyers_symbol(fsym)
                agg.last_oi[nse] = int(oi)   # GIL-atomic dict write; no lock needed
                got += 1
            except Exception as e:
                log.warning(f"poll_futures_oi {fsym}: {e}")
            time.sleep(OI_CALL_SPACING_SEC)
        log.info(f"OI poll (depth API): {got}/{len(fut_syms)} futures OI updated")
    finally:
        _OI_POLL_LOCK.release()


_OPT_OI_POLL_LOCK = threading.Lock()
_STOCK_OPT_OI_POLL_LOCK = threading.Lock()   # cc#375: separate lock so a slow stock OI cycle never blocks the index poll

def poll_options_oi(token, opt_syms, opt_store, lock=None, label="index"):
    """
    Option OI via DEPTH REST. The WS feed strips OI (Fyers SDK pops the 'OI' field),
    so depth is the only live source — same pattern as poll_futures_oi.
    Index cycle (~136 NIFTY+BANKNIFTY ATM+/-10 syms ~= 48s) fits inside the 5-min bar.
    cc#375: also called for the SUBSCRIBED stock options (bounded by stock_options_limit)
    on a SEPARATE lock/thread, so their option_chain rows carry OI instead of NULL.
    Latest OI -> opt_store.last_oi[fsym] -> attached on next option bar flush.
    """
    lock = lock or _OPT_OI_POLL_LOCK
    if not lock.acquire(blocking=False):
        log.info(f"Option OI poll ({label}) skipped — previous cycle still running")
        return
    try:
        log.info(f"Option OI poll ({label}) starting: {len(opt_syms)} options via depth API")
        headers = {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
        got = 0
        for fsym in opt_syms:
            try:
                r = requests.get(DEPTH_URL,
                                 params={'symbol': fsym, 'ohlcv_flag': 1},
                                 headers=headers, timeout=8)
                d = r.json()
                if d.get('s') != 'ok':
                    continue
                data_d = d.get('d')
                node = {}
                if isinstance(data_d, dict):
                    node = data_d.get(fsym) or (next(iter(data_d.values())) if data_d else {})
                elif isinstance(data_d, list) and data_d and isinstance(data_d[0], dict):
                    node = data_d[0].get('v', data_d[0])
                oi = node.get('oi') if isinstance(node, dict) else None
                if oi is None:
                    continue
                opt_store.last_oi[fsym] = int(oi)   # GIL-atomic dict write; no lock needed
                got += 1
            except Exception as e:
                log.warning(f"poll_options_oi ({label}) {fsym}: {e}")
            time.sleep(OI_CALL_SPACING_SEC)
        log.info(f"Option OI poll ({label}, depth API): {got}/{len(opt_syms)} option OI updated")
    finally:
        lock.release()


def _batched_subscribe(ws, symbols, action='sub', label=''):
    """cc#151: single batched code path for subscribe/unsubscribe (WS_SUB_BATCH chunks
    + sleep + per-batch log). cc_task #88 batching was applied to on_connect only — the
    monthly-roll path still fired one bulk call (fut+options combined) and Fyers
    silently dropped symbols under that load (1-Jul roll: only 3/212 futures survived).
    This is now the ONLY subscribe/unsubscribe path, used by both on_connect and roll."""
    if not symbols:
        return
    verb = 'Subscribing' if action == 'sub' else 'Unsubscribing'
    tag = f" ({label})" if label else ""
    log.info(f"{verb} {len(symbols)} symbols{tag} in batches of {WS_SUB_BATCH}")
    for i in range(0, len(symbols), WS_SUB_BATCH):
        batch = symbols[i:i + WS_SUB_BATCH]
        if action == 'sub':
            ws.subscribe(symbols=batch, data_type="SymbolUpdate")
        else:
            ws.unsubscribe(symbols=batch)
        log.info(f"{verb} batch {i // WS_SUB_BATCH + 1}: {len(batch)} symbols "
                 f"({min(i + WS_SUB_BATCH, len(symbols))}/{len(symbols)})")
        time.sleep(WS_SUB_BATCH_SLEEP_SEC)


# ── cold-boot CMP seed (cc#352, id166 family) ───────────────────────────────────
CMP_BOOT_STALE_MIN = 20   # cc#352: seed cmp_prices from REST if the freshest row is older than this
QUOTE_BATCH        = 50   # Fyers quotes API cap per request

def _seed_cmp_from_rest(conn, token, equity_symbols):
    """cc#352 (id166 family): on a cold/stale boot, seed cmp_prices SPOT from the Fyers REST
    quotes API for the full equity universe BEFORE the WS subscribes — so the worker is never
    left price-less on a pre-market / crash / overnight boot and the options live-price gate has
    prices immediately (empty cmp_prices was the root of the connected-but-deaf zombie).

    cmp_prices holds SPOT (equity/index) price keyed by the plain NSE symbol — matching every
    existing consumer (_get_cmp, the ATM gate) — so we seed the EQUITY leg (NSE:SYM-EQ), never
    futures/options prices (which would corrupt the spot cache). Additive + fully guarded: 3x
    retry with backoff per batch; on total failure logs ops_log feed_boot_initial_failed and
    falls back to prior behavior. This function NEVER raises — it can only add prices, never
    change the subscribe logic, so it cannot break the feed."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), EXTRACT(EPOCH FROM (NOW() - MAX(updated_at)))/60 FROM cmp_prices")
            n, age_min = cur.fetchone()
        n = n or 0
        if n > 0 and age_min is not None and age_min <= CMP_BOOT_STALE_MIN:
            log.info(f"boot cmp seed: cmp_prices fresh ({n} rows, {age_min:.0f}m old) — skip REST seed")
            return
        log.warning(f"boot cmp seed: cmp_prices empty/stale (rows={n}, age_min={age_min}) — "
                    f"seeding SPOT from Fyers REST before subscribe (cc#352/id166)")
    except Exception as e:
        log.warning(f"boot cmp seed precheck failed (skipping seed): {e}")
        return

    fsyms = [fyers_eq_symbol(s) for s in equity_symbols]   # NSE:SYM-EQ (M&M handled by SPECIAL_SYMBOLS)
    seeded, failed_batches = 0, 0
    for i in range(0, len(fsyms), QUOTE_BATCH):
        batch = fsyms[i:i + QUOTE_BATCH]
        rows = []
        for attempt in range(3):
            try:
                r = requests.get(QUOTES_URL, params={'symbols': ','.join(batch)},
                                 headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=10)
                d = r.json()
                if d.get('s') != 'ok':
                    raise RuntimeError(f"quotes s={d.get('s')} {str(d)[:120]}")
                ist = datetime.now(IST).replace(tzinfo=None)
                for item in d.get('d', []):
                    lp   = (item.get('v') or {}).get('lp')
                    fsym = item.get('n')
                    if not lp or not fsym:
                        continue
                    rows.append((from_fyers_symbol(fsym), float(lp), ist))
                break
            except Exception as e:
                if attempt == 2:
                    failed_batches += 1
                    log.warning(f"boot cmp seed batch {i // QUOTE_BATCH} failed after 3x: {e}")
                else:
                    time.sleep(2 ** attempt)
        if rows:
            try:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,%s,'fyers_boot') "
                        "ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, "
                        "updated_at=EXCLUDED.updated_at, source='fyers_boot'", rows)
                conn.commit()
                seeded += len(rows)
            except Exception as e:
                log.warning(f"boot cmp seed write batch {i // QUOTE_BATCH}: {e}")
    if seeded == 0:
        try:
            _ops_log(conn, 'alert', 'feed_boot_initial_failed',
                     {'reason': 'REST cmp seed returned 0 rows', 'failed_batches': failed_batches,
                      'universe': len(fsyms), 'ist': _ist_now_str()})
        except Exception:
            pass
        log.error("boot cmp seed: 0 rows seeded — feed_boot_initial_failed (falling back to prior behavior)")
    else:
        log.info(f"boot cmp seed: seeded {seeded}/{len(fsyms)} equity SPOT from REST "
                 f"({failed_batches} batch failures) (cc#352)")


# ── main run ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

# ── cc#390: Phase A 12-mo warehouse INSIDE the worker (main app lacks FYERS_TOTP env; worker has it) ──
# The whole loop runs on its OWN daemon thread with its OWN db connection + token, so it can NEVER
# block or corrupt the WS loop's connection. Market-hours safe (pauses to 15:35 if it crosses a live
# session). Reuses cc#389's fetch_hist_5m + progress/log helpers (import only — no duplicated rules).
def _phase_a_market_open():
    now = datetime.now(IST)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _claim_phase_a_worker():
    """Atomic FOR UPDATE claim of app_config phase_a_run='pending' -> 'claimed_worker'."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='phase_a_run' AND value='pending' FOR UPDATE")
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed_worker', updated_at=NOW() WHERE key='phase_a_run'")
        conn.commit()
        return bool(r)
    except Exception as e:
        log.error(f"phase_a worker claim failed: {e}")
        return False
    finally:
        conn.close()


def _worker_run_phase_a():
    """Full trailing-365d 5m EQ warehouse for every active futures symbol, on the worker's token.
    Resumable (app_config phase_a_progress); failures logged + skipped; ops_log every 20; completion
    session_log PHASE_A_WAREHOUSE_COMPLETE. Pauses between symbols if a live session starts."""
    import fyers_hist_backfill as fhb
    conn = get_db()
    started = time.time()
    try:
        token = get_valid_token(conn)                       # worker owns the Fyers secrets
        today = datetime.now(IST).replace(tzinfo=None).date()
        frm = today - timedelta(days=365)
        with conn.cursor() as cur:
            cur.execute("SELECT UPPER(symbol) FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT value FROM app_config WHERE key=%s", (fhb._PHASE_A_PROGRESS,))
            r = cur.fetchone()
        progress = (r[0] or "") if r else ""
        pending = [s for s in symbols if s > progress] if progress else list(symbols)
        fhb._oplog(conn, "data_infra", "PHASE_A_START",
                   {"where": "worker", "universe": len(symbols), "to_do": len(pending),
                    "from": str(frm), "to": str(today), "resuming_after": progress or None})
        done = bars = fails = 0
        failures = []
        for sym in pending:
            # market-hours guard — never fetch during a live session; resume-safe if it crosses over
            while _phase_a_market_open():
                nowt = datetime.now(IST)
                run_at = nowt.replace(hour=15, minute=35, second=0, microsecond=0)
                wait_s = max(60, (run_at - nowt).total_seconds())
                log.info(f"cc#390 Phase A paused for market hours — recheck in {min(wait_s,900)/60:.0f} min")
                time.sleep(min(wait_s, 900))
            try:
                res = fhb.fetch_hist_5m(sym, frm, today, conn=conn, token=token)
                bars += res["bars"]
            except Exception as e:
                fails += 1
                failures.append({"symbol": sym, "error": str(e)[:200]})
                log.error(f"cc#390 Phase A {sym} failed: {e}")
            done += 1
            try:
                fhb._set_config(conn, fhb._PHASE_A_PROGRESS, sym)
            except Exception as e:
                log.warning(f"phase_a checkpoint {sym}: {e}")
            if done % 20 == 0:
                fhb._oplog(conn, "data_infra", "PHASE_A_PROGRESS",
                           {"where": "worker", "done": done, "of": len(pending), "bars": bars,
                            "failures": fails, "elapsed_min": round((time.time() - started) / 60, 1),
                            "last": sym})
            time.sleep(5)                                    # inter-symbol pacing (chunks already 5s-paced)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts)::date FROM intraday_prices WHERE source=%s",
                        (fhb.HIST_SOURCE,))
            c = cur.fetchone()
        summary = {"where": "worker", "symbols_processed": done, "universe": len(symbols),
                   "bars_written_this_run": bars, "failures": fails, "failures_list": failures,
                   "elapsed_min": round((time.time() - started) / 60, 1),
                   "warehouse_total_bars": int(c[0] or 0), "warehouse_symbols": int(c[1] or 0),
                   "warehouse_oldest": str(c[2]) if c[2] else None, "source": fhb.HIST_SOURCE}
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'data_audit', 'PHASE_A_WAREHOUSE_COMPLETE', %s::jsonb)""",
                            (json.dumps(summary, default=str),))
            conn.commit()
        except Exception as e:
            log.warning(f"PHASE_A completion log: {e}")
        try:
            fhb._set_config(conn, fhb._PHASE_A_FLAG, "done")
        except Exception:
            pass
        log.info(f"cc#390 worker Phase A COMPLETE: {summary}")
    except Exception as e:
        log.error(f"cc#390 worker Phase A fatal: {e}")
    finally:
        conn.close()


def _phase_a_worker_daemon():
    """Boot + hourly idle check: when off-market, atomically claim phase_a_run and run the warehouse
    once, then keep idle-checking hourly (so a missed boot claim still fires later).
    cc#390 follow-up: main-app auto-claim removed, so the worker is the sole owner of this flag."""
    while True:
        try:
            if not _phase_a_market_open() and _claim_phase_a_worker():
                log.info("cc#390: phase_a_run claimed on worker — starting 365d warehouse (background)")
                _worker_run_phase_a()
        except Exception as e:
            log.error(f"cc#390 phase_a daemon: {e}")
        time.sleep(3600)


def run(auth_code=None):
    import fyers_backfill
    from fyers_apiv3.FyersWebsocket import data_ws

    conn    = get_db()
    token   = get_valid_token(conn, auth_code)
    token   = _boot_auth_selfcheck(conn, token)   # cc#339 fix_2: prove REST auth BEFORE subscribing
    symbols = get_universe(conn)

    # cc#162: futures leg = stocks + confirmed-active index futures (NIFTY/
    # BANKNIFTY). Equity leg stays `symbols`-only -- no -EQ instrument exists
    # for an index, so it must never be added there.
    index_fut_codes = get_index_futures_universe(conn)
    fut_codes       = symbols + index_fut_codes

    ensure_schemas(conn)
    _boot_gap_report(conn)   # cc#339 fix_3: self-document any outage window at boot
    threading.Thread(target=_phase_a_worker_daemon, name="cc390-phasea", daemon=True).start()   # cc#390
    log.info(f"Universe: {len(symbols)} equity + {len(fut_codes)} futures "
             f"({len(index_fut_codes)} index: {index_fut_codes}) + options")

    # cc#352 (id166 family): seed cmp_prices SPOT from Fyers REST on a cold/stale boot BEFORE the
    # WS subscribes, so the worker is never price-less (the options live-price gate needs a
    # populated cmp_prices, and an empty cache was the root of the connected-but-deaf zombie).
    # Fully guarded — never raises, only adds prices, never alters subscribe logic.
    _seed_cmp_from_rest(conn, token, symbols)

    # Skip-if-fresh: only backfill when today's intraday data is missing.
    # When fresh, skip the ~40-min sequential backfill entirely (instant restart).
    def _intraday_fresh():
        try:
            with conn.cursor() as c:
                # cc_task #88 GAP_1: only count actual market-hours bars (ts::time >= 09:15)
                # so a stale pre-market bar (e.g. 07:15) never makes the boot-backfill skip.
                c.execute("SELECT COUNT(DISTINCT symbol) FROM intraday_prices "
                          "WHERE ts::date = CURRENT_DATE AND timeframe='5m' "
                          "AND ts::time >= '09:15:00'")
                n = c.fetchone()[0] or 0
            return n >= 150   # most of universe already has today's bars
        except Exception:
            return False

    if _intraday_fresh():
        log.info("Boot backfill SKIPPED — today's intraday already fresh (>=150 symbols)")
    else:
        # Defer backfill to a background thread so the live WS connects immediately.
        def _deferred_backfill():
            # cc_task #87: Yahoo/REST backfill must NEVER write during market hours
            # (09:15-15:30 IST) — stale history bars caused a wrong-price paper entry.
            # If this thread wakes inside the live session, hold it until 15:35 IST.
            now = datetime.now(IST)
            if now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE:
                run_at  = now.replace(hour=15, minute=35, second=0, microsecond=0)
                wait_s  = max(0, (run_at - now).total_seconds())
                log.info(f"Deferred backfill held to 15:35 IST (market open) — sleeping {wait_s/60:.0f} min")
                time.sleep(wait_s)
            log.info("Deferred backfill (7-day equity, sequential, background)...")
            try:
                fyers_backfill.backfill_7day(token, conn)
                log.info("Deferred backfill complete")
            except Exception as e:
                log.error(f"Deferred backfill failed (continuing): {e}")
        threading.Thread(target=_deferred_backfill, daemon=True).start()

    expiry = current_expiry()
    log.info(f"Active expiry: {expiry}")

    equity_fyers_syms  = [fyers_eq_symbol(s) for s in symbols]
    futures_fyers_syms = [futures_fyers_symbol(s, expiry) for s in fut_codes]   # cc#162: + index futures

    master = OptionMaster()
    master.load()

    opt_mgr     = OptionSymbolManager(conn, token=token, master=master)
    # cc#189 (founder redesign): options are NOT built/subscribed at boot. They
    # subscribe later — only once the market is open AND cmp_prices is fresh — via
    # the gate in housekeeping(). This eliminates the cold-boot bug where an empty
    # cmp_prices silently produced zero option subscriptions on a pre-market restart.
    option_syms = []

    all_syms    = equity_fyers_syms + futures_fyers_syms
    log.info(f"WS: {len(equity_fyers_syms)} eq + {len(futures_fyers_syms)} fut + "
             f"0 opt (options deferred to live-price gate) = {len(all_syms)} total")

    equity_set  = set(equity_fyers_syms)
    futures_set = set(futures_fyers_syms)

    agg       = BarAggregator(conn)
    opt_store = OptionBarStore(conn, opt_mgr)
    access    = f"{FYERS_CLIENT_ID}:{token}"

    def on_message(msg):
        try:
            fsym = msg.get('symbol', '')
            ltp  = msg.get('ltp')
            vol  = msg.get('vol_traded_today') or msg.get('volume') or 0
            if not fsym or not ltp: return

            if fsym in equity_set:
                agg.on_tick(from_fyers_symbol(fsym), float(ltp), float(vol), source='fyers_eq')
            elif fsym in futures_set:
                # OI not in WS — sourced from depth REST poll (agg.last_oi)
                nse = from_fyers_symbol(fsym)
                agg.on_tick(nse, float(ltp), float(vol),
                            source='fyers_fut', oi=agg.last_oi.get(nse))
            else:
                opt_store.on_tick(fsym, float(ltp),
                                  vol=float(vol),
                                  bid=msg.get('bid'), ask=msg.get('ask'))
        except Exception as e:
            log.warning(f"on_message: {e}")

    def on_connect():
        # cc_task #84 change_3: avoid the NSE-feed-init race at the open. When we
        # connect inside the first OPEN_RACE_GUARD_SECS after 09:15, hold the first
        # subscription until the exchange feed is fully up (prevents the open crash).
        now_t   = datetime.now(IST)
        open_dt = now_t.replace(hour=9, minute=15, second=0, microsecond=0)
        if open_dt <= now_t < open_dt + timedelta(seconds=OPEN_RACE_GUARD_SECS):
            wait_s = OPEN_RACE_GUARD_SECS - (now_t - open_dt).total_seconds()
            if wait_s > 0:
                log.info(f"on_connect: holding subscription {wait_s:.0f}s (NSE open-race guard)")
                time.sleep(wait_s)
        # cc_task #88 GAP_2 / cc#151: subscribe in WS_SUB_BATCH-sized chunks via the
        # shared _batched_subscribe helper — a single bulk subscribe of ~1460 symbols
        # was silently dropped by the Fyers server under open-load (212 futures got
        # zero data on 25-Jun).
        # cc#189: subscribe eq + fut + ANY already-live-gated options (option_syms
        # is empty on a cold boot, populated after the gate fires — so a reconnect
        # re-subscribes the live options too instead of dropping them).
        sub_list = equity_fyers_syms + futures_fyers_syms + list(option_syms)
        log.info(f"WS connected — subscribing {len(sub_list)} symbols ({len(option_syms)} options)")
        _batched_subscribe(fyers_ws, sub_list, action='sub', label='initial')
        threading.Thread(target=_verify_subscribe_survivors, args=('connect',), daemon=True).start()
        fyers_ws.keep_running()

    def on_error(msg):  log.error(f"WS error: {msg}")
    def on_close(msg):  log.warning(f"WS closed: {msg}")

    fyers_ws = data_ws.FyersDataSocket(
        access_token=access, log_path="",
        litemode=False, write_to_file=False, reconnect=True,
        on_connect=on_connect, on_close=on_close,
        on_error=on_error, on_message=on_message,
    )

    # ── feed heartbeat helpers (cc_task #84) ──────────────────────────────────
    def _recent_symbol_count(minutes=HEARTBEAT_STALE_MINS):
        """Distinct symbols whose latest live 5-min bar bucket falls within the last
        `minutes`. Read on the housekeeping thread's own conn (single-thread = safe).
        Returns -1 on DB error so a failed read never triggers a false reconnect."""
        try:
            cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=minutes)
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(DISTINCT symbol) FROM intraday_prices
                    WHERE timeframe='5m' AND source IN ('fyers_eq','fyers_fut')
                      AND ts >= %s
                """, (cutoff,))
                return cur.fetchone()[0] or 0
        except Exception as e:
            log.warning(f"_recent_symbol_count: {e}")
            return -1

    def _heal_gap_bg():
        """change_2: REST-backfill each symbol from its newest stored bar -> now, on a
        FRESH connection (never share the worker conn across threads)."""
        try:
            hc = get_db()
            try:
                fyers_backfill.heal_gap(token, hc, symbols)
            finally:
                hc.close()
        except Exception as e:
            log.error(f"heartbeat heal_gap failed: {e}")

    def _force_reconnect():
        """change_1: drop the socket so the SDK (reconnect=True) re-establishes and
        on_connect re-subscribes the full universe."""
        try:
            log.error("HEARTBEAT: forcing WebSocket reconnect (close_connection)")
            fyers_ws.close_connection()
        except Exception as e:
            log.warning(f"force reconnect: {e}")

    def _log_feed_incident(kind, detail):
        """cc_task #112: record each watchdog action to ops_log (category=alert)
        so every recurrence is visible after the fact. Uses the housekeeping conn.
        cc#156: telemetry categories moved off session_log to ops_log."""
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'alert', %s, %s::jsonb)",
                    (kind, json.dumps({"detail": detail, "ist": datetime.now(IST).isoformat()})))
            conn.commit()
        except Exception as e:
            log.warning(f"_log_feed_incident: {e}")

    def _verify_subscribe_survivors(label):
        """cc#151: after ANY batched (re)subscribe — on_connect boot/reconnect or the
        monthly-roll path — confirm futures are actually ticking and log it to ops_log,
        so every re-subscribe is auditable instead of just assumed. Acceptance:
        >=205/212 futures ticking within 15min; this samples the last 15min window.

        cc#188: only raise the ops_log alert during market hours (09:15-15:30 IST)
        on a trading day — same gate pattern as the ADR fix. A (re)subscribe
        off-hours (e.g. an evening reconnect) naturally shows ~0 ticking because
        the feed is idle; that is NOT an incident, so it must not fire a
        0/212 alert. Off-hours we log at info level only."""
        time.sleep(120)
        try:
            recent = _recent_symbol_count(15)
            now = datetime.now(IST)
            in_market = is_trading_day(now.date()) and MARKET_OPEN <= now.time() <= MARKET_CLOSE
            msg = f"{label}: {recent}/{TOTAL_FUTURES} symbols writing bars"
            if in_market:
                _log_feed_incident("subscribe_verify", msg)
            else:
                log.info(f"Post-{label} verification (off-hours — no alert): {msg}")
            log.info(f"Post-{label} verification: {recent}/{TOTAL_FUTURES} symbols ticking")
        except Exception as e:
            log.warning(f"post-{label} verify failed: {e}")

    def _hard_restart(reason):
        """cc_task #112 — the missing auto-restart. Socket-reconnect failed to revive
        the feed (rejected as a fix on its own), so RE-EXEC the whole process: a clean
        boot re-auths (same-day token reused), rebuilds the WS and re-subscribes all 212
        symbols from scratch. Railway also relaunches the worker if execv ever fails."""
        log.error(f"FEED WATCHDOG: HARD RESTART — {reason}")
        _log_feed_incident("feed_hard_restart", reason)
        try:
            agg.flush_all(); opt_store.flush_all()   # persist whatever bars we hold
        except Exception:
            pass
        try:
            fyers_ws.close_connection()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f"os.execv failed ({e}) — exiting for Railway to relaunch")
            os._exit(1)

    def housekeeping():
        last_atm_check  = None
        last_purge_day  = None
        last_heal_day   = None
        last_roll_check = None
        last_oi_poll    = None
        last_cmp_flush  = None
        last_health_log = None        # cc_task #84
        last_reconnect  = None        # cc_task #84
        watchdog_stale_since = None   # cc_task #85: when coverage first dropped below WATCHDOG_MIN_SYMBOLS
        reconnect_attempts   = 0      # cc_task #112: forced reconnects since coverage last healthy
        opt_subscribed       = False  # cc#189: INDEX options subscribed once live prices went fresh
        opt_stock_subscribed = False  # cc#241: STOCK options subscribed once >=09:25 + cmp fresh
        opt_deadline_alerted = False  # cc#189: fired the 09:30 not-subscribed CRITICAL once (per day)
        opt_gate_day         = None   # cc#189: reset the gate each trading day
        starvation_day       = None   # cc#228: fyers_eq starvation check fired once per trading day

        while True:
            now    = datetime.now(IST)
            today  = now.date()
            now_dt = now.replace(tzinfo=None)
            in_market = (now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE)

            # cc#189: reset the once-per-day 09:30 deadline alert each trading day.
            if opt_gate_day != today:
                opt_gate_day = today
                opt_deadline_alerted = False

            # cc#228: fyers_eq starvation watchdog. fyers_eq (live WS) is now the SOLE equity
            # source (legacy fyers backfill is dormant), and it is new — only 03-Jul is proven
            # full (~15,700 bars/day). If it wrote < 10,000 5m bars by 11:00 IST on a trading
            # day, the equity feed is starving -> fire a one-per-day ops_log alert so the
            # dormant legacy path can be manually re-armed if needed.
            if (starvation_day != today and now.time() >= dt_time(11, 0)
                    and is_trading_day(today)):
                starvation_day = today
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM intraday_prices "
                                    "WHERE source='fyers_eq' AND timeframe='5m' "
                                    "AND ts >= %s AND ts < %s",
                                    (today, today + timedelta(days=1)))
                        eq_bars = cur.fetchone()[0]
                    if eq_bars < 10000:
                        _log_feed_incident("fyers_eq_starvation",
                            f"fyers_eq wrote only {eq_bars} 5m bars by 11:00 IST (<10000; ~15700 "
                            f"expected) — equity feed may be starving. Legacy fyers backfill is "
                            f"dormant (cc#228); re-arm manually (force=True / LEGACY_EQUITY_BACKFILL) "
                            f"if the WS cannot recover.")
                        log.error(f"FYERS_EQ STARVATION: only {eq_bars} 5m bars by 11:00 IST (<10000)")
                    else:
                        log.info(f"fyers_eq starvation check OK: {eq_bars} 5m bars by 11:00 IST")
                except Exception as _sv:
                    log.warning(f"fyers_eq starvation watchdog: {_sv}")

            if in_market:
                update_index_ltp(conn, token, agg)
                agg.flush_all()
                # CMP flush throttled 30s -> 5-min (14-Jun-2026): cmp_prices is a
                # 218-row UPSERT (no growth); sub-minute freshness not needed
                # (ATM drift check is 15-min). flush_all still writes 5-min bars
                # every pass (dedupes by bucket).
                if (last_cmp_flush is None or
                        (now_dt - last_cmp_flush).total_seconds() >= CMP_FLUSH_MINS * 60):
                    agg.flush_cmp()
                    last_cmp_flush = now_dt
                opt_store.flush_all()
                # Index option OI poll added alongside the futures OI poll below.

                # ── cc#189: options subscribe ONLY when live prices are fresh ──
                # Founder redesign: no boot/REST hydration. Once the market is open
                # and >=80% of option underlyings have a cmp_prices tick in the last
                # 10 min, compute ATM strikes from LIVE prices and subscribe. Retries
                # every loop (30s); a CRITICAL alert fires if still unsubscribed by
                # 09:30. On a gap day live prices beat yesterday-close for ATM too.
                if not opt_subscribed:
                    fresh = _cmp_fresh_fraction(conn, opt_mgr, kind='index')
                    if fresh >= OPT_FRESH_MIN_FRAC:
                        try:
                            new_opts = opt_mgr.build_initial(kind='index')   # ATM from LIVE cmp_prices
                            if new_opts:
                                _batched_subscribe(fyers_ws, new_opts, action='sub', label='options-live')
                                option_syms.clear(); option_syms.extend(new_opts)
                                opt_subscribed = True
                                total, ok, missing, contracts = opt_mgr.subscribe_health(kind='index')
                                log.info(f"cc#189 INDEX options subscribed LIVE at {now.time().strftime('%H:%M')}: "
                                         f"{contracts} contracts, {ok}/{total} underlyings (cmp fresh {fresh:.0%})")
                                if total and ok < OPT_FRESH_MIN_FRAC * total:
                                    _log_feed_incident("options_subscribe_critical",
                                        f"CRITICAL: only {ok}/{total} INDEX option underlyings subscribed "
                                        f"({contracts} contracts); missing: {', '.join(missing) or 'none'}")
                            else:
                                log.warning("cc#189 gate: cmp fresh but build produced 0 option symbols")
                        except Exception as e:
                            log.warning(f"cc#189 options live-subscribe failed: {e}")
                    elif now.time() >= OPT_SUB_DEADLINE and not opt_deadline_alerted:
                        _log_feed_incident("options_not_subscribed_0930",
                            f"CRITICAL: options unsubscribed at {now.time().strftime('%H:%M')} — cmp_prices "
                            f"fresh for only {fresh:.0%} of underlyings (need {OPT_FRESH_MIN_FRAC:.0%})")
                        opt_deadline_alerted = True

                # ── cc#241: STOCK options — HARD 09:25 floor, subscribed SEPARATELY from index
                # so index goes early (above) and stocks anchor ATM off the settled 09:25 print.
                # Config-gated (app_config, default OFF); founder flips enabled=true + limit=0 on
                # a watched morning. Additive: never touches the index/eq/fut subscription.
                if (not opt_stock_subscribed) and opt_subscribed and now.time() >= OPT_STOCK_SUB_MIN_TIME:
                    s_enabled, s_limit, _s_n = _stock_options_config(conn)
                    if s_enabled:
                        s_fresh = _cmp_fresh_fraction(conn, opt_mgr, kind='stock')
                        if s_fresh >= OPT_FRESH_MIN_FRAC:
                            try:
                                new_stock = opt_mgr.build_initial(kind='stock')   # ATM off 09:25 print
                                if new_stock:
                                    _batched_subscribe(fyers_ws, new_stock, action='sub', label='stock-options-0925')
                                    option_syms.extend(new_stock)
                                    opt_stock_subscribed = True
                                    s_total, s_ok, s_missing, s_contracts = opt_mgr.subscribe_health(kind='stock')
                                    log.info(f"cc#241 STOCK options subscribed at {now.time().strftime('%H:%M')}: "
                                             f"{s_contracts} contracts, {s_ok}/{s_total} underlyings "
                                             f"(cmp fresh {s_fresh:.0%}, limit={s_limit})")
                                    # decision_3: alert-only if the WS silently dropped subs (<95%).
                                    if s_total and s_ok < OPT_STOCK_OVERFLOW_FRAC * s_total:
                                        _log_feed_incident("stock_options_ws_overflow",
                                            f"stock options: only {s_ok}/{s_total} underlyings subscribed "
                                            f"({s_contracts} contracts, {len(s_missing)} missing) — WS may have "
                                            f"silently dropped subs at scale. Reduce app_config "
                                            f"stock_options_limit LIVE if needed (no redeploy).")
                                else:
                                    log.warning("cc#241 stock gate: enabled + fresh but built 0 stock option symbols")
                            except Exception as e:
                                log.warning(f"cc#241 stock options subscribe failed: {e}")

                # Futures OI poll every OI_POLL_MINS via DEPTH API (quotes has NO OI).
                # Background thread: 208 depth calls ≈ 75s — must not block flushes.
                if (last_oi_poll is None or
                        (now_dt - last_oi_poll).total_seconds() >= OI_POLL_MINS * 60):
                    threading.Thread(target=poll_futures_oi,
                                     args=(token, list(futures_fyers_syms), agg),
                                     daemon=True).start()
                    threading.Thread(target=poll_options_oi,
                                     args=(token, opt_mgr.index_option_syms(list(option_syms)), opt_store),
                                     daemon=True).start()
                    # cc#375: also poll OI for the SUBSCRIBED stock options (separate lock/thread so it
                    # never delays the index poll). Without this their WS bars carry no OI -> option_chain
                    # oi stays NULL and the cockpit ATM OI d/d is always '--'. Only when stock options are
                    # actually subscribed; bounded by app_config stock_options_limit (pilot default 20).
                    if opt_stock_subscribed:
                        threading.Thread(target=poll_options_oi,
                                         args=(token, opt_mgr.stock_option_syms(list(option_syms)), opt_store),
                                         kwargs={"lock": _STOCK_OPT_OI_POLL_LOCK, "label": "stock"},
                                         daemon=True).start()
                    last_oi_poll = now_dt

                # ATM drift check every ATM_CHECK_MINS
                if (last_atm_check is None or
                        (now_dt - last_atm_check).total_seconds() >= ATM_CHECK_MINS * 60):
                    try:
                        add_syms, rem_syms = opt_mgr.check_atm_drift()
                        if add_syms or rem_syms:
                            if rem_syms: fyers_ws.unsubscribe(symbols=rem_syms)
                            if add_syms:
                                fyers_ws.subscribe(symbols=add_syms, data_type="SymbolUpdate")
                                option_syms.extend(add_syms)
                            log.info(f"ATM rebalance: +{len(add_syms)} -{len(rem_syms)}")
                    except Exception as e:
                        log.warning(f"ATM drift check failed: {e}")
                    last_atm_check = now_dt

                # ── feed health + watchdog (cc_task #84 + #85) ────────────────
                # PART_3: every HEALTH_LOG_MINS log how many symbols are writing;
                #   < FEED_CRITICAL_SYMBOLS → log.error (visible in Railway logs).
                # PART_4: if < WATCHDOG_MIN_SYMBOLS for WATCHDOG_STALE_MINS straight,
                #   force a full WS reconnect (close_connection → SDK reconnect) + REST
                #   gap-heal. Suppressed for STARTUP_GRACE_MINS after 09:15 so first
                #   bars can form.
                mins_open = (now_dt - now_dt.replace(hour=9, minute=15, second=0, microsecond=0)).total_seconds() / 60
                if mins_open >= STARTUP_GRACE_MINS:
                    recent = _recent_symbol_count(HEARTBEAT_STALE_MINS)
                    if (last_health_log is None or
                            (now_dt - last_health_log).total_seconds() >= HEALTH_LOG_MINS * 60):
                        if 0 <= recent < FEED_CRITICAL_SYMBOLS:
                            log.error(f"FEED CRITICAL: only {recent}/{TOTAL_FUTURES} symbols writing bars")
                        else:
                            log.info(f"Feed health: {recent} symbols wrote a 5m bar in last {HEARTBEAT_STALE_MINS} min")
                        last_health_log = now_dt
                    # PART_4 watchdog: sustained low coverage -> force reconnect + heal
                    if 0 <= recent < WATCHDOG_MIN_SYMBOLS:
                        if watchdog_stale_since is None:
                            watchdog_stale_since = now_dt
                        stale_mins = (now_dt - watchdog_stale_since).total_seconds() / 60
                        if (stale_mins >= WATCHDOG_STALE_MINS and
                                (last_reconnect is None or
                                 (now_dt - last_reconnect).total_seconds() >= RECONNECT_COOLDOWN_MINS * 60)):
                            # cc_task #112: escalate. The first WATCHDOG_MAX_RECONNECTS actions
                            # try a socket reconnect; if coverage is STILL dead after that, a
                            # reconnect clearly isn't fixing it (4 prior recurrences) -> hard
                            # restart the whole process for a guaranteed clean re-subscribe.
                            if reconnect_attempts >= WATCHDOG_MAX_RECONNECTS:
                                _hard_restart(
                                    f"{recent}/{TOTAL_FUTURES} symbols writing after "
                                    f"{reconnect_attempts} reconnects, {stale_mins:.0f}min gap")
                                # process is being replaced; nothing below runs
                            log.error(f"FEED WATCHDOG: forcing reconnect after {stale_mins:.0f}min gap "
                                      f"(<{WATCHDOG_MIN_SYMBOLS} symbols writing, "
                                      f"attempt {reconnect_attempts + 1}/{WATCHDOG_MAX_RECONNECTS})")
                            _force_reconnect()
                            _log_feed_incident("feed_watchdog_reconnect",
                                               f"{recent}/{TOTAL_FUTURES} writing; {stale_mins:.0f}min gap")
                            reconnect_attempts += 1
                            # cc_task #87: heal_gap must NEVER run during market hours
                            # (09:15-15:30 IST). The force-reconnect restores the live WS
                            # feed immediately; the outage gap is backfilled by the 18:00
                            # IST daily heal_gap (REST writes are post-market only).
                            log.info("Watchdog: live feed reconnect issued; gap-heal deferred to 18:00 IST")
                            last_reconnect = now_dt
                            watchdog_stale_since = now_dt   # restart the window after acting
                    else:
                        watchdog_stale_since = None         # recovered -> reset the timer
                        reconnect_attempts   = 0            # cc_task #112: clear escalation counter

            # Monthly roll — once per day
            if last_roll_check != today:
                try:
                    if opt_mgr.check_monthly_roll():
                        new_expiry   = opt_mgr.expiry
                        new_fut_syms = [futures_fyers_symbol(s, new_expiry) for s in fut_codes]   # cc#162: + index futures
                        new_opt_syms = opt_mgr.build_initial()
                        # cc#151: batched unsub/sub (same helper as on_connect) — the old
                        # single bulk unsubscribe+subscribe silently dropped symbols under
                        # load (1-Jul roll: only 3/212 futures survived).
                        _batched_subscribe(fyers_ws, futures_fyers_syms + option_syms,
                                           action='unsub', label='roll-old')
                        _batched_subscribe(fyers_ws, new_fut_syms + new_opt_syms,
                                           action='sub', label='roll-new')
                        futures_fyers_syms.clear(); futures_fyers_syms.extend(new_fut_syms)
                        futures_set.clear();        futures_set.update(new_fut_syms)
                        option_syms.clear();        option_syms.extend(new_opt_syms)
                        log.info(f"Monthly roll complete: {new_expiry}")
                        threading.Thread(target=_verify_subscribe_survivors, args=('roll',), daemon=True).start()
                except Exception as e:
                    log.warning(f"Monthly roll check failed: {e}")
                last_roll_check = today

            # Daily 18:00 IST — heal equity gaps
            if now.hour == 18 and now.minute < 1 and last_heal_day != today:
                log.info("18:00 IST: Running daily heal_gap for equity")
                try:
                    fyers_backfill.heal_gap(token, conn, symbols)
                    last_heal_day = today
                except Exception as e:
                    log.error(f"Daily heal_gap failed: {e}")

            if last_purge_day != today:
                purge_old_bars(conn)
                last_purge_day = today

            time.sleep(30)

    # cc_task #112 — NEVER DIE SILENT. The watchdog lives inside housekeeping(); if that
    # thread ever raises and dies, the feed loses its only auto-recovery and freezes
    # unnoticed (the failure mode behind the 4 recurrences). Supervise it: any crash or
    # unexpected return is logged and the loop is restarted after a short backoff.
    def _housekeeping_supervised():
        while True:
            try:
                housekeeping()   # normally an infinite loop — should never return
                log.error("housekeeping() returned unexpectedly — restarting in 5s")
            except Exception as e:
                log.error(f"housekeeping THREAD crashed: {e} — restarting in 5s")
                try:
                    _log_feed_incident("housekeeping_crash", str(e))
                except Exception:
                    pass
            time.sleep(5)
    threading.Thread(target=_housekeeping_supervised, daemon=True).start()
    log.info("Connecting WebSocket (live)...")
    fyers_ws.connect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()
    run(auth_code=args.auth_code)

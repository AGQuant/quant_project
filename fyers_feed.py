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
  8. PURGE     - rolling: intraday_prices + futures_basis at RETENTION_DAYS (30d),
                 option_chain at OPTION_RETENTION_DAYS (7d). Rows older deleted daily.

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

import argparse, bisect, calendar, hashlib, os, time, logging, threading, re
from datetime import datetime, timedelta, time as dt_time, date
import pytz, psycopg2, requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    '')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL      = 'https://api-t1.fyers.in/api/v3/validate-authcode'
QUOTES_URL        = 'https://api-t1.fyers.in/data/quotes'
DEPTH_URL         = 'https://api-t1.fyers.in/data/depth'
OPTION_MASTER_URL = 'https://public.fyers.in/sym_details/NSE_FO.csv'
IST               = pytz.timezone('Asia/Kolkata')

RETENTION_DAYS = 30   # intraday_prices + futures_basis (extended 7→30 on 15-Jun-2026 for sim history)
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
N_STRIKES             = 10     # ATM ± 10
BAR_MINUTES           = 5      # 5-min system: all rolling intraday bars at 5-min granularity
OI_POLL_MINS          = 5      # poll futures OI via DEPTH REST every N min (quotes has NO OI)
CMP_FLUSH_MINS        = 5      # flush cmp_prices every N min (was 30s; throttled 14-Jun-2026)
OI_CALL_SPACING_SEC   = 0.35   # ~170 req/min — under Fyers 200/min data limit

# ── feed heartbeat / health (cc_task #84) ─────────────────────────────────────
# The WS stream for the 212 stock futures crashed at the 09:15 open on 25-Jun and
# did not auto-reconnect until ~11:25 — a 2h15m data gap that fed stale prices to
# V8 paper, trade-check and the dashboard. These guard the live stream.
HEARTBEAT_STALE_MINS    = 10   # if 0 symbols wrote a live bar in this window → reconnect
HEALTH_LOG_MINS         = 5    # log feed health every N min during market hours
MIN_HEALTHY_SYMBOLS     = 50   # fewer symbols than this writing bars → alert in Railway logs
RECONNECT_COOLDOWN_MINS = 10   # min gap between forced reconnects (anti-thrash)
STARTUP_GRACE_MINS      = 10   # suppress the heartbeat for this long after 09:15 (bars need time to form)
OPEN_RACE_GUARD_SECS    = 60   # hold the first subscription until N s after 09:15 (NSE feed-init race)

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

def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE")
        futures = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS)


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


def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')

def from_fyers_symbol(fsym):
    if fsym == 'NSE:M&M-EQ': return 'M&M'
    if 'FUT' in fsym:
        inner = fsym.replace('NSE:', '')
        m = re.match(r'^([A-Z&]+)\d{2}[A-Z]{3}FUT$', inner)
        if m: return m.group(1)
        return inner
    return fsym.replace('NSE:', '').replace('-EQ', '')


# ── option symbol manager ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

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
        # INDEX-ONLY (locked 14-Jun-2026): NIFTY + BANKNIFTY options only.
        # Stock options dropped from live WS to save DB + Fyers load; the stock
        # helpers (get_top50_option_underlyings / STOCK_STEPS) are intentionally
        # retained for future on-demand revival.
        out = []
        for name, meta in INDEX_OPTION_UNDERLYINGS.items():
            out.append({'name': name, 'step': meta['step'], 'cmp_sym': meta['cmp_sym']})
        self._underlyings = out
        log.info(f"OptionSymbolManager: {len(out)} underlyings (index-only)")

    def _get_cmp(self, cmp_sym):
        # 1) table first (warm path)
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT cmp FROM cmp_prices WHERE symbol = %s", (cmp_sym,))
                r = cur.fetchone()
                if r and r[0]:
                    return float(r[0])
        except Exception:
            pass
        # 2) cold-boot fallback: pull live CMP straight from Fyers quotes API
        if not self.token:
            return None
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
                        return float(lp)
        except Exception as e:
            log.warning(f"_get_cmp Fyers fallback {cmp_sym}: {e}")
        return None

    def _ladder(self, u, cmp):
        """
        Returns list of (strike, opt_type) for ATM±N.
        Primary: actual listed strikes from the symbol master.
        Fallback: step-based generation (pre-v6).
        """
        pairs = []
        if self.master and self.master.loaded:
            ce = self.master.atm_window(u['name'], self.expiry, 'CE', cmp)
            pe = self.master.atm_window(u['name'], self.expiry, 'PE', cmp)
            if ce or pe:
                for s in (ce or []): pairs.append((s, 'CE'))
                for s in (pe or []): pairs.append((s, 'PE'))
                return pairs
            log.warning(f"_ladder: no master chain for {u['name']} {self.expiry} — step fallback")
        step = u['step'] or auto_step(cmp)
        atm  = atm_strike(cmp, step)
        for i in range(-N_STRIKES, N_STRIKES + 1):
            strike = atm + i * step
            if strike <= 0: continue
            pairs.append((strike, 'CE'))
            pairs.append((strike, 'PE'))
        return pairs

    def build_initial(self):
        """Returns list of Fyers option symbols to subscribe."""
        self._build_underlyings()
        self.expiry = current_expiry()
        symbols = []
        with self.lock:
            self.sym_map = {}
            self.atm_map = {}
            for u in self._underlyings:
                cmp = self._get_cmp(u['cmp_sym'])
                if not cmp:
                    log.warning(f"No CMP for {u['cmp_sym']} — skipping options")
                    continue
                step = u['step'] or auto_step(cmp)
                self.atm_map[u['name']] = atm_strike(cmp, step)
                for strike, otype in self._ladder(u, cmp):
                    fsym = option_fyers_symbol(u['name'], strike, otype, self.expiry)
                    if self.master and not self.master.is_valid(fsym):
                        continue
                    self.sym_map[fsym] = (u['name'], strike, otype, self.expiry)
                    symbols.append(fsym)
        log.info(f"OptionSymbolManager: built {len(symbols)} option symbols "
                 f"({'master' if self.master and self.master.loaded else 'step-fallback'})")
        return symbols

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
            self.last_ltp[sym] = ltp
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
            if bar['ts'].time() >= MARKET_CLOSE:
                return
        except Exception:
            pass
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'5m',%s)
                    ON CONFLICT (symbol,ts,timeframe) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume,source=EXCLUDED.source
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
            with self.conn.cursor() as cur:
                # Spot = nearest non-futures intraday bar for this symbol at/before ts.
                # (exact ts + source='fyers_eq' missed: eq feed is sparse & ts-misaligned;
                #  bulk spot data is source='fyers'. Match nearest, exclude fyers_fut self.)
                cur.execute("""
                    SELECT close FROM intraday_prices
                    WHERE symbol=%s AND ts::date=%s::date AND ts<=%s AND source<>'fyers_fut'
                    ORDER BY ts DESC LIMIT 1
                """, (sym, ts, ts))
                row       = cur.fetchone()
                spot      = float(row[0]) if row else None
                # Fallback: the equity bar for this 5-min bucket may not be flushed
                # yet (eq/fut flush on the same boundary but not simultaneously), so
                # the lookup above can miss → spot None → NULL basis. Fall back to
                # the live CMP (refreshed every CMP_FLUSH_MINS), then prior EOD close.
                if spot is None:
                    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (sym,))
                    r2 = cur.fetchone()
                    spot = float(r2[0]) if r2 and r2[0] is not None else None
                if spot is None:
                    cur.execute("SELECT close FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1", (sym,))
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
        _ist = datetime.now(IST).replace(tzinfo=None)
        with self.lock:
            rows = [(s, p, _ist) for s, p in self.last_ltp.items() if p]
        if not rows: return
        try:
            with self.conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO cmp_prices (symbol, cmp, updated_at, source)
                    VALUES (%s, %s, %s, 'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET
                        cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='fyers'
                """, rows)
            self.conn.commit()
            log.info(f"CMP flushed: {len(rows)} symbols")
        except Exception as e:
            log.warning(f"flush_cmp: {e}")


# ── option bar store ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

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
    cutoff       = now - timedelta(days=RETENTION_DAYS)         # intraday_prices + futures_basis (30d)
    opt_cutoff   = now - timedelta(days=OPTION_RETENTION_DAYS)  # option_chain (7d, leaner)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m'", (cutoff,))
            eq_del = cur.rowcount
            cur.execute("DELETE FROM option_chain WHERE ts < %s", (opt_cutoff,))
            opt_del = cur.rowcount
            cur.execute("DELETE FROM futures_basis WHERE ts < %s", (cutoff,))
            basis_del = cur.rowcount
        conn.commit()
        log.info(f"Purged: intraday={eq_del} (>{RETENTION_DAYS}d), "
                 f"option_chain={opt_del} (>{OPTION_RETENTION_DAYS}d), futures_basis={basis_del} (>{RETENTION_DAYS}d)")
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

def poll_options_oi(token, opt_syms, opt_store):
    """
    Index option OI via DEPTH REST. The WS feed strips OI (Fyers SDK pops the 'OI'
    field), so depth is the only live source — same pattern as poll_futures_oi.
    INDEX-ONLY scope keeps this to ~136 symbols (NIFTY+BANKNIFTY ATM+/-10) so a full
    cycle (~136 * OI_CALL_SPACING_SEC ~= 48s) fits inside the 5-min bar.
    Latest OI -> opt_store.last_oi[fsym] -> attached on next option bar flush.
    """
    if not _OPT_OI_POLL_LOCK.acquire(blocking=False):
        log.info("Option OI poll skipped — previous cycle still running")
        return
    try:
        log.info(f"Option OI poll starting: {len(opt_syms)} index options via depth API")
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
                log.warning(f"poll_options_oi {fsym}: {e}")
            time.sleep(OI_CALL_SPACING_SEC)
        log.info(f"Option OI poll (depth API): {got}/{len(opt_syms)} index option OI updated")
    finally:
        _OPT_OI_POLL_LOCK.release()


# ── main run ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def run(auth_code=None):
    import fyers_backfill
    from fyers_apiv3.FyersWebsocket import data_ws

    conn    = get_db()
    token   = get_valid_token(conn, auth_code)
    symbols = get_universe(conn)

    ensure_schemas(conn)
    log.info(f"Universe: {len(symbols)} equity + {len(symbols)} futures + options")

    # Skip-if-fresh: only backfill when today's intraday data is missing.
    # When fresh, skip the ~40-min sequential backfill entirely (instant restart).
    def _intraday_fresh():
        try:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(DISTINCT symbol) FROM intraday_prices "
                          "WHERE ts::date = CURRENT_DATE AND timeframe='5m'")
                n = c.fetchone()[0] or 0
            return n >= 150   # most of universe already has today's bars
        except Exception:
            return False

    if _intraday_fresh():
        log.info("Boot backfill SKIPPED — today's intraday already fresh (>=150 symbols)")
    else:
        # Defer backfill to a background thread so the live WS connects immediately.
        def _deferred_backfill():
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
    futures_fyers_syms = [futures_fyers_symbol(s, expiry) for s in symbols]

    master = OptionMaster()
    master.load()

    opt_mgr     = OptionSymbolManager(conn, token=token, master=master)
    option_syms = opt_mgr.build_initial()

    all_syms    = equity_fyers_syms + futures_fyers_syms + option_syms
    log.info(f"WS: {len(equity_fyers_syms)} eq + {len(futures_fyers_syms)} fut + {len(option_syms)} opt = {len(all_syms)} total")

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
        log.info(f"WS connected — subscribing {len(all_syms)} symbols")
        fyers_ws.subscribe(symbols=all_syms, data_type="SymbolUpdate")
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

    def housekeeping():
        last_atm_check  = None
        last_purge_day  = None
        last_heal_day   = None
        last_roll_check = None
        last_oi_poll    = None
        last_cmp_flush  = None
        last_health_log = None   # cc_task #84
        last_reconnect  = None   # cc_task #84

        while True:
            now    = datetime.now(IST)
            today  = now.date()
            now_dt = now.replace(tzinfo=None)
            in_market = (now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE)

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

                # Futures OI poll every OI_POLL_MINS via DEPTH API (quotes has NO OI).
                # Background thread: 208 depth calls ≈ 75s — must not block flushes.
                if (last_oi_poll is None or
                        (now_dt - last_oi_poll).total_seconds() >= OI_POLL_MINS * 60):
                    threading.Thread(target=poll_futures_oi,
                                     args=(token, list(futures_fyers_syms), agg),
                                     daemon=True).start()
                    threading.Thread(target=poll_options_oi,
                                     args=(token, list(option_syms), opt_store),
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

                # ── feed heartbeat + health (cc_task #84) ─────────────────────
                # change_4 health log every HEALTH_LOG_MINS; change_1 force a WS
                # reconnect + REST gap-heal when 0 symbols have written a live bar
                # in the last HEARTBEAT_STALE_MINS. Suppressed for STARTUP_GRACE_MINS
                # after the open so the first bars have time to form.
                mins_open = (now_dt - now_dt.replace(hour=9, minute=15, second=0, microsecond=0)).total_seconds() / 60
                if mins_open >= STARTUP_GRACE_MINS:
                    recent = _recent_symbol_count(HEARTBEAT_STALE_MINS)
                    if (last_health_log is None or
                            (now_dt - last_health_log).total_seconds() >= HEALTH_LOG_MINS * 60):
                        if 0 <= recent < MIN_HEALTHY_SYMBOLS:
                            log.warning(f"FEED HEALTH ALERT: only {recent} symbols wrote a 5m bar in the "
                                        f"last {HEARTBEAT_STALE_MINS} min (< {MIN_HEALTHY_SYMBOLS})")
                        else:
                            log.info(f"Feed health: {recent} symbols wrote a 5m bar in last {HEARTBEAT_STALE_MINS} min")
                        last_health_log = now_dt
                    if recent == 0 and (last_reconnect is None or
                            (now_dt - last_reconnect).total_seconds() >= RECONNECT_COOLDOWN_MINS * 60):
                        log.error(f"HEARTBEAT DEAD: 0 symbols wrote a bar in {HEARTBEAT_STALE_MINS} min — "
                                  f"reconnecting WS + REST gap-heal")
                        _force_reconnect()
                        threading.Thread(target=_heal_gap_bg, daemon=True).start()
                        last_reconnect = now_dt

            # Monthly roll — once per day
            if last_roll_check != today:
                try:
                    if opt_mgr.check_monthly_roll():
                        new_expiry   = opt_mgr.expiry
                        new_fut_syms = [futures_fyers_symbol(s, new_expiry) for s in symbols]
                        new_opt_syms = opt_mgr.build_initial()
                        fyers_ws.unsubscribe(symbols=futures_fyers_syms + option_syms)
                        fyers_ws.subscribe(symbols=new_fut_syms + new_opt_syms, data_type="SymbolUpdate")
                        futures_fyers_syms.clear(); futures_fyers_syms.extend(new_fut_syms)
                        futures_set.clear();        futures_set.update(new_fut_syms)
                        option_syms.clear();        option_syms.extend(new_opt_syms)
                        log.info(f"Monthly roll complete: {new_expiry}")
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

    threading.Thread(target=housekeeping, daemon=True).start()
    log.info("Connecting WebSocket (live)...")
    fyers_ws.connect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()
    run(auth_code=args.auth_code)

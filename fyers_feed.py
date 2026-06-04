"""
Fyers Live Feed - Scorr V8
============================
Standalone Railway WORKER (not in FastAPI). The live intraday source.

Architecture (v3 — all-futures 1-min bars):
  1. BACKFILL  - on boot, one-time 7-day 1-min history for ALL 211 symbols.
  2. LIVE WS   - persistent WebSocket for all 208 futures + indices.
                 * ALL 211 symbols: 1-min bars stored in intraday_prices.
                 * flush_cmp() also writes latest LTP to cmp_prices every 30s.
  3. GAP HEAL  - on every WS (re)connect, patch gaps for all symbols.
  4. CMP FLUSH - every 30s during market hours → cmp_prices (IST timestamp).
  5. OPTION CHAIN - SDK optionchain() poll every 5-min → option_chain table.
  6. PURGE     - 7-day rolling: rows older than 7 days deleted daily.

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

import argparse, calendar, hashlib, os, time, logging, threading
from datetime import datetime, timedelta, time as dt_time, date
import pytz, psycopg2, requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    '')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL = 'https://api-t1.fyers.in/api/v3/validate-authcode'
QUOTES_URL   = 'https://api-t1.fyers.in/data/quotes'
IST          = pytz.timezone('Asia/Kolkata')

RETENTION_DAYS = 7

# Market-close guard: no intraday bar at/after this IST time is persisted.
MARKET_CLOSE = dt_time(15, 30)

# Index symbols — stored in intraday_prices AND cmp_prices
INDEX_LTP_SYMBOLS = {
    'NIFTY50':   'NSE:NIFTY50-INDEX',
    'BANKNIFTY': 'NSE:NIFTYBANK-INDEX',
    'INDIAVIX':  'NSE:INDIAVIX-INDEX',
}

SKIP_SYMBOLS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}

# ── Option chain config ───────────────────────────────────────────────────────
OPTION_RETENTION_DAYS = 7
OPTION_POLL_MINS      = 5
N_STRIKES             = 10
NIFTY_STEP            = 50
BNIFTY_STEP           = 100
OPTION_MONTHS         = ['JAN','FEB','MAR','APR','MAY','JUN',
                          'JUL','AUG','SEP','OCT','NOV','DEC']

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')


# ─────────────────────────────────────────────────────────────────────── DB / token

def get_db(): return psycopg2.connect(DATABASE_URL)
def app_id_hash(): return hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()

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
        d = r.json()
        return d.get('s') == 'ok'
    except Exception as e:
        log.warning(f"Token liveness check failed: {e}")
        return False

def get_valid_token(conn, auth_code=None):
    if auth_code:
        try:
            return bootstrap_from_authcode(conn, auth_code)
        except Exception as e:
            log.warning(f"Auth-code bootstrap failed ({e}); falling through to stored/auto-login")

    row = load_tokens(conn)
    if row and row[0] and row[2]:
        access_token, access_created = row[0], row[2]
        today = datetime.now(IST).replace(tzinfo=None).date()
        if access_created.date() == today:
            log.info("Stored same-day token found — verifying with Fyers...")
            if _token_is_live(access_token):
                log.info("Token verified live — reusing (restart-safe)")
                return access_token
            log.warning("Stored same-day token REJECTED by Fyers — re-authing")
        else:
            log.warning(f"Stored token is from {access_created.date()} (previous day) — re-authing")

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


# ─────────────────────────────────────────────────────────────────────── universe

def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        alls = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS), sorted((alls - futures) - SKIP_SYMBOLS)

def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')
def from_fyers_symbol(fsym):
    if fsym == 'NSE:M&M-EQ': return 'M&M'
    return fsym.replace('NSE:', '').replace('-EQ', '')


# ─────────────────────────────────────────────────────────────────────── bar aggregator

class BarAggregator:
    def __init__(self, conn):
        self.conn = conn
        self.bars = {}
        self.last_ltp = {}
        self.lock = threading.Lock()

    def _bucket(self, ts, minutes=1):
        floored = ts.replace(second=0, microsecond=0)
        floored = floored - timedelta(minutes=floored.minute % minutes)
        return floored

    def on_tick(self, sym, ltp, vol, ts=None):
        ts = ts or datetime.now(IST).replace(tzinfo=None)
        bkt = self._bucket(ts, 1)
        key = (sym, '1m')
        with self.lock:
            self.last_ltp[sym] = ltp
            bar = self.bars.get(key)
            if bar is None or bar['ts'] != bkt:
                if bar is not None:
                    self._flush(key, bar)
                self.bars[key] = {'ts': bkt, 'o': ltp, 'h': ltp, 'l': ltp,
                                  'c': ltp, 'v': vol or 0}
            else:
                bar['h'] = max(bar['h'], ltp)
                bar['l'] = min(bar['l'], ltp)
                bar['c'] = ltp
                if vol: bar['v'] = vol

    def _flush(self, key, bar):
        sym, _ = key
        try:
            if bar['ts'].time() >= MARKET_CLOSE:
                return
        except Exception:
            pass
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'1m','fyers')
                    ON CONFLICT (symbol,ts,timeframe) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume,source='fyers'
                """, (sym, bar['ts'], bar['o'], bar['h'], bar['l'], bar['c'], int(bar['v'])))
            self.conn.commit()
        except Exception as e:
            log.warning(f"flush {sym}: {e}")

    def flush_all(self):
        with self.lock:
            for key, bar in list(self.bars.items()):
                self._flush(key, bar)

    def flush_cmp(self):
        _ist = datetime.now(IST).replace(tzinfo=None)
        with self.lock:
            rows = [(s, p, _ist) for s, p in self.last_ltp.items() if p]
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
            log.info(f"CMP (fyers) flushed: {len(rows)} symbols")
        except Exception as e:
            log.warning(f"flush_cmp: {e}")


# ─────────────────────────────────────────────────────────────────────── index LTP

def update_index_ltp(conn, token, agg=None):
    try:
        r = requests.get(QUOTES_URL, params={'symbols': ','.join(INDEX_LTP_SYMBOLS.values())},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=5)
        d = r.json()
        if d.get('s') != 'ok': return
        rows = []
        _ist = datetime.now(IST).replace(tzinfo=None)
        for item in d.get('d', []):
            lp = item['v'].get('lp', 0)
            if not lp: continue
            for name, fsym in INDEX_LTP_SYMBOLS.items():
                if fsym == item['n']:
                    rows.append((name, lp, _ist))
                    if agg is not None:
                        agg.on_tick(name, float(lp), 0)
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,%s,'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='fyers'""", rows)
            conn.commit()
    except Exception as e:
        log.warning(f"Index LTP: {e}")


# ─────────────────────────────────────────────────────────────────────── purge

def purge_old_bars(conn):
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=RETENTION_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND source = 'fyers'", (cutoff,))
            deleted = cur.rowcount
        conn.commit()
        if deleted:
            log.info(f"Purged {deleted} fyers bars older than {RETENTION_DAYS} days")
    except Exception as e:
        log.warning(f"purge_old_bars: {e}")


# ─────────────────────────────────────────────────────────────────────── option chain helpers

def ensure_option_schema(conn):
    with conn.cursor() as cur:
        cur.execute(OPTION_SCHEMA_SQL)
    conn.commit()
    log.info("option_chain schema ready")

def get_monthly_expiry():
    import calendar as cal
    today = datetime.now(IST).date()
    year, month = today.year, today.month

    def last_thursday(y, m):
        last_day = cal.monthrange(y, m)[1]
        d = date(y, m, last_day)
        while d.weekday() != 3:
            d -= timedelta(days=1)
        return d

    exp = last_thursday(year, month)
    if today > exp:
        month = month + 1 if month < 12 else 1
        year  = year if month > 1 else year + 1
        exp   = last_thursday(year, month)
    return exp

def get_atm(conn, underlying):
    step    = NIFTY_STEP if underlying == 'NIFTY' else BNIFTY_STEP
    cmp_sym = 'NIFTY50'  if underlying == 'NIFTY' else 'BANKNIFTY'
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT cmp FROM cmp_prices WHERE symbol = %s", (cmp_sym,))
            r = cur.fetchone()
        if not r: return None
        return int(round(float(r[0]) / step) * step)
    except Exception as e:
        log.warning(f"get_atm {underlying}: {e}")
        return None

def poll_option_chain(conn, token, expiry):
    from fyers_apiv3 import fyersModel
    fy = fyersModel.FyersModel(client_id=FYERS_CLIENT_ID, token=token, is_async=False, log_path="")

    now = datetime.now(IST).replace(tzinfo=None)
    bkt = now.replace(second=0, microsecond=0)
    bkt = bkt - timedelta(minutes=bkt.minute % OPTION_POLL_MINS)

    rows = []
    for underlying, index_sym in (('NIFTY', 'NSE:NIFTY50-INDEX'),
                                  ('BANKNIFTY', 'NSE:NIFTYBANK-INDEX')):
        try:
            resp = fy.optionchain(data={"symbol": index_sym,
                                        "strikecount": N_STRIKES,
                                        "timestamp": ""})
            if not isinstance(resp, dict) or resp.get('s') != 'ok':
                log.warning(f"optionchain {underlying}: {resp.get('message', resp) if isinstance(resp, dict) else resp}")
                continue
            chain = (resp.get('data') or {}).get('optionsChain') or []
            for c in chain:
                otype = c.get('option_type')
                if otype not in ('CE', 'PE'): continue
                fsym   = c.get('symbol')
                strike = c.get('strike_price')
                ltp    = c.get('ltp')
                if fsym is None or strike is None or ltp is None: continue
                rows.append((fsym, underlying, strike, otype, expiry,
                             ltp, c.get('oi'), c.get('volume'), c.get('bid'), c.get('ask'), bkt))
        except Exception as e:
            log.warning(f"optionchain {underlying} error: {e}")

    if not rows:
        log.info("option_chain: 0 rows")
        return

    try:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO option_chain
                  (symbol, underlying, strike, option_type, expiry,
                   ltp, oi, volume, bid, ask, ts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, ts) DO UPDATE SET
                  ltp=EXCLUDED.ltp, oi=EXCLUDED.oi, volume=EXCLUDED.volume,
                  bid=EXCLUDED.bid, ask=EXCLUDED.ask
            """, rows)
        conn.commit()
        log.info(f"option_chain: {len(rows)} rows stored at {bkt}")
    except Exception as e:
        log.warning(f"option_chain store: {e}")

def cleanup_option_chain(conn):
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=OPTION_RETENTION_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM option_chain WHERE ts < %s", (cutoff,))
            deleted = cur.rowcount
        conn.commit()
        if deleted:
            log.info(f"option_chain cleanup: {deleted} rows deleted")
    except Exception as e:
        log.warning(f"option_chain cleanup: {e}")


# ─────────────────────────────────────────────────────────────────────── market helpers

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+29)


# ─────────────────────────────────────────────────────────────────────── main run

def run(auth_code=None):
    import fyers_backfill
    from fyers_apiv3.FyersWebsocket import data_ws

    conn  = get_db()
    token = get_valid_token(conn, auth_code)
    futures, equity = get_universe(conn)
    log.info(f"Universe: {len(futures)} futures — all get 1-min bars (7-day rolling)")

    ensure_option_schema(conn)

    # Boot backfill — all futures + indices
    log.info("Boot backfill (7-day, all symbols)...")
    try:
        fyers_backfill.backfill_7day(token, conn)
    except Exception as e:
        log.error(f"Boot backfill failed (continuing): {e}")

    agg = BarAggregator(conn)
    futures_fyers_syms = [fyers_eq_symbol(s) for s in futures]
    access = f"{FYERS_CLIENT_ID}:{token}"

    def on_message(msg):
        try:
            sym = from_fyers_symbol(msg.get('symbol', ''))
            ltp = msg.get('ltp')
            vol = msg.get('vol_traded_today') or msg.get('volume') or 0
            if sym and ltp:
                agg.on_tick(sym, float(ltp), float(vol))
        except Exception as e:
            log.warning(f"on_message: {e}")

    def on_connect():
        log.info("WS connected - subscribing all futures + index symbols")
        fyers_ws.subscribe(symbols=futures_fyers_syms, data_type="SymbolUpdate")
        fyers_ws.keep_running()

    def on_error(msg):  log.error(f"WS error: {msg}")
    def on_close(msg):  log.warning(f"WS closed: {msg}")

    fyers_ws = data_ws.FyersDataSocket(
        access_token=access, log_path="",
        litemode=False, write_to_file=False, reconnect=True,
        on_connect=on_connect, on_close=on_close,
        on_error=on_error, on_message=on_message,
    )

    def housekeeping():
        last_option_poll = None
        last_cleanup_day = None
        last_purge_day   = None

        while True:
            if is_market_open():
                update_index_ltp(conn, token, agg)
                agg.flush_all()
                agg.flush_cmp()

                now_dt = datetime.now(IST).replace(tzinfo=None)
                if (last_option_poll is None or
                        (now_dt - last_option_poll).total_seconds() >= OPTION_POLL_MINS * 60):
                    expiry = get_monthly_expiry()
                    try:
                        poll_option_chain(conn, token, expiry)
                    except Exception as e:
                        log.warning(f"option_chain poll failed: {e}")
                    last_option_poll = now_dt

            today = datetime.now(IST).date()
            if last_cleanup_day != today:
                cleanup_option_chain(conn)
                last_cleanup_day = today

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

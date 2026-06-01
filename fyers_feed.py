"""
Fyers Live Feed - Scorr V8
============================
Standalone Railway WORKER (not in FastAPI). The live intraday source.

FUTURES-ONLY WebSocket (streaming):
  ~208 futures universe in 1-min bars via WebSocket (under Fyers ~200 cap).

OPTION CHAIN (REST poll, 5-min):
  NIFTY + BANKNIFTY current-month expiry, ATM±10 strikes, CE+PE.
  Polled via REST /data/quotes every 5 min -> option_chain table (7-day retention).
  Uses the same access token. No extra auth needed.

Architecture (3 layers for futures + 1 for options):
  1. BACKFILL  - on boot, one-time 7-day 1-min futures history (fyers_backfill).
  2. LIVE      - persistent WebSocket. Futures ticks aggregated locally into
                 1-min bars -> intraday_prices. Every tick's LTP also ->
                 cmp_prices (source='fyers'), making Fyers the PRIMARY
                 real-time CMP for futures. Near-zero REST calls.
  3. GAP HEAL  - on every WS (re)connect, patch the slice from the newest
                 stored candle -> now, so a drop never leaves a hole.
  4. OPTION CHAIN - REST poll every 5 min. ATM recomputed each cycle from
                 latest cmp_prices. 84 symbols (2 indices × 21 strikes × CE+PE)
                 split into 2 batches of 50 to stay within REST limits.

TOKEN MODEL (Fyers v3, SEBI framework from 01-Apr-2026):
  Refresh-token flow is DISABLED. ONE 2FA login per TRADING DAY.
  access_token valid the whole trading day, survives restarts.
  Stored in Railway table fyers_tokens (id=1).

  Boot logic (get_valid_token):
    1. --auth-code given  -> bootstrap (mint + store today's token).
    2. else stored access_token created TODAY -> reuse it (restart-safe).
    3. else -> AUTO-LOGIN via TOTP (headless) -> store + return. Zero-touch.

USAGE:
  Normal (zero-touch): python fyers_feed.py
  Manual override:     python fyers_feed.py --auth-code <code>
"""

import argparse, calendar, hashlib, os, time, logging, threading
from datetime import datetime, timedelta, time as dt_time, date
import pytz, psycopg2, requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    'YXTIR2MN9V')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '2580')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL = 'https://api-t1.fyers.in/api/v3/validate-authcode'
QUOTES_URL   = 'https://api-t1.fyers.in/data/quotes'
IST          = pytz.timezone('Asia/Kolkata')

RETENTION_DAYS = 7

# Market-close guard: no intraday bar at/after this IST time is persisted.
MARKET_CLOSE = dt_time(15, 30)

SKIP_SYMBOLS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}
INDEX_LTP_SYMBOLS = {
    'NIFTY50':   'NSE:NIFTY50-INDEX',
    'BANKNIFTY': 'NSE:NIFTYBANK-INDEX',
    'INDIAVIX':  'NSE:INDIAVIX-INDEX',
}

# ── Option chain config ────────────────────────────────────────────────────────
OPTION_RETENTION_DAYS = 7
OPTION_POLL_MINS      = 5      # poll every 5 min via REST
N_STRIKES             = 10     # ATM ± 10 strikes each side
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


# ──────────────────────────────────────────────── DB / token

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

def get_valid_token(conn, auth_code=None):
    if auth_code:
        try:
            return bootstrap_from_authcode(conn, auth_code)
        except Exception as e:
            log.warning(f"Auth-code bootstrap failed ({e}); trying stored same-day token")
    row = load_tokens(conn)
    if row and row[0] and row[2]:
        access_token, access_created = row[0], row[2]
        if access_created.date() == datetime.now(IST).replace(tzinfo=None).date():
            log.info("Reusing stored same-day access token (restart-safe)")
            return access_token
        log.warning("Stored access token is from a previous day - auto-login needed")
    try:
        import fyers_autologin
        log.info("No valid token - running TOTP auto-login...")
        return fyers_autologin.auto_login(conn)
    except Exception as e:
        raise SystemExit(
            f"\nAUTO-LOGIN FAILED ({e}).\n"
            "Check env vars: FYERS_TOTP_SECRET, FYERS_PIN, FYERS_SECRET, FYERS_FY_ID.\n"
            "Manual fallback:\n"
            f"  1. https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}"
            "&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n"
            "  2. python fyers_feed.py --auth-code <code>\n")


# ──────────────────────────────────────────────── universe

def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        alls = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS), sorted((alls - futures) - SKIP_SYMBOLS)

def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')
def from_fyers_symbol(fsym):
    if fsym == 'NSE:M&M-EQ': return 'M&M'
    return fsym.replace('NSE:', '').replace('-EQ', '')


# ──────────────────────────────────────────────── bar aggregator

class BarAggregator:
    def __init__(self, conn, futures_set):
        self.conn = conn
        self.futures = futures_set
        self.bars = {}
        self.last_ltp = {}
        self.lock = threading.Lock()

    def _bucket(self, ts, minutes):
        floored = ts.replace(second=0, microsecond=0)
        floored = floored - timedelta(minutes=floored.minute % minutes)
        return floored

    def on_tick(self, sym, ltp, vol, ts=None):
        ts = ts or datetime.now(IST).replace(tzinfo=None)
        tf, mins = ('1m', 1)
        bkt = self._bucket(ts, mins)
        key = (sym, tf)
        with self.lock:
            self.last_ltp[sym] = ltp
            bar = self.bars.get(key)
            if bar is None or bar['ts'] != bkt:
                if bar is not None:
                    self._flush(key, bar)
                self.bars[key] = {'ts': bkt, 'o': ltp, 'h': ltp, 'l': ltp,
                                  'c': ltp, 'v': vol or 0, 'tf': tf}
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
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'fyers')
                    ON CONFLICT (symbol,ts,timeframe) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume,source='fyers'
                """, (sym, bar['ts'], bar['o'], bar['h'], bar['l'], bar['c'], int(bar['v']), bar['tf']))
            self.conn.commit()
        except Exception as e:
            log.warning(f"flush {sym}: {e}")

    def flush_all(self):
        with self.lock:
            for key, bar in list(self.bars.items()):
                self._flush(key, bar)

    def flush_cmp(self):
        with self.lock:
            rows = [(s, p) for s, p in self.last_ltp.items() if p]
        if not rows:
            return
        try:
            with self.conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO cmp_prices (symbol, cmp, updated_at, source)
                    VALUES (%s, %s, NOW(), 'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET
                        cmp=EXCLUDED.cmp, updated_at=NOW(), source='fyers'
                """, rows)
            self.conn.commit()
            log.info(f"CMP (fyers) flushed: {len(rows)} symbols")
        except Exception as e:
            log.warning(f"flush_cmp: {e}")


# ──────────────────────────────────────────────── index LTP

def update_index_ltp(conn, token, agg=None):
    try:
        r = requests.get(QUOTES_URL, params={'symbols': ','.join(INDEX_LTP_SYMBOLS.values())},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=5)
        d = r.json()
        if d.get('s') != 'ok': return
        rows = []
        for item in d.get('d', []):
            lp = item['v'].get('lp', 0)
            if not lp: continue
            for name, fsym in INDEX_LTP_SYMBOLS.items():
                if fsym == item['n']:
                    rows.append((name, lp))
                    if agg is not None:
                        agg.on_tick(name, float(lp), 0)
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,NOW(),'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=NOW(), source='fyers'""", rows)
            conn.commit()
    except Exception as e:
        log.warning(f"Index LTP: {e}")


# ──────────────────────────────────────────────── option chain helpers

def ensure_option_schema(conn):
    with conn.cursor() as cur:
        cur.execute(OPTION_SCHEMA_SQL)
    conn.commit()
    log.info("option_chain schema ready")

def get_monthly_expiry():
    """Last Thursday of current (or next) month."""
    today = datetime.now(IST).date()
    year, month = today.year, today.month

    def last_thursday(y, m):
        last_day = calendar.monthrange(y, m)[1]
        d = date(y, m, last_day)
        while d.weekday() != 3:   # 3 = Thursday
            d -= timedelta(days=1)
        return d

    exp = last_thursday(year, month)
    if today > exp:
        month = month + 1 if month < 12 else 1
        year  = year if month > 1 else year + 1
        exp   = last_thursday(year, month)
    return exp

def get_atm(conn, underlying):
    """Round current CMP to nearest strike interval."""
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

def build_option_symbols(conn, expiry):
    """
    Returns list of tuples: (fyers_symbol, underlying, strike, option_type, expiry)
    NIFTY + BANKNIFTY, ATM±N_STRIKES strikes, CE + PE.
    """
    exp_str = f"{str(expiry.year)[2:]}{OPTION_MONTHS[expiry.month - 1]}"
    symbols = []
    for underlying, step in [('NIFTY', NIFTY_STEP), ('BANKNIFTY', BNIFTY_STEP)]:
        atm = get_atm(conn, underlying)
        if atm is None:
            log.warning(f"ATM unavailable for {underlying} — skipping option chain")
            continue
        for i in range(-N_STRIKES, N_STRIKES + 1):
            strike = int(atm + i * step)
            for otype in ('CE', 'PE'):
                fsym = f"NSE:{underlying}{exp_str}{strike}{otype}"
                symbols.append((fsym, underlying, strike, otype, expiry))
    log.info(f"option symbols: {len(symbols)} ({exp_str} expiry, ATM±{N_STRIKES})")
    return symbols

def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def poll_option_chain(conn, token, expiry):
    """REST poll for all option symbols → option_chain table (5-min bucket)."""
    sym_meta = build_option_symbols(conn, expiry)
    if not sym_meta:
        return

    fyers_syms = [s[0] for s in sym_meta]
    meta_map   = {s[0]: s for s in sym_meta}

    # 5-min bucket timestamp
    now = datetime.now(IST).replace(tzinfo=None)
    bkt = now.replace(second=0, microsecond=0)
    bkt = bkt - timedelta(minutes=bkt.minute % OPTION_POLL_MINS)

    rows = []
    for batch in _chunks(fyers_syms, 50):    # Fyers REST: max ~50/call
        try:
            r = requests.get(
                QUOTES_URL,
                params={'symbols': ','.join(batch)},
                headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'},
                timeout=10,
            )
            d = r.json()
            if d.get('s') != 'ok':
                log.warning(f"option poll batch: {d.get('message','')}")
                continue
            for item in d.get('d', []):
                fsym = item.get('n', '')
                if fsym not in meta_map:
                    continue
                _, underlying, strike, otype, exp = meta_map[fsym]
                v   = item.get('v', {})
                ltp = v.get('lp') or v.get('ltp')
                oi  = v.get('oi') or v.get('open_interest')
                vol = v.get('vol_traded_today') or v.get('volume')
                bid = v.get('bid_price') or v.get('bp')
                ask = v.get('ask_price') or v.get('ap')
                if ltp is None:
                    continue
                rows.append((fsym, underlying, strike, otype, exp,
                              ltp, oi, vol, bid, ask, bkt))
        except Exception as e:
            log.warning(f"option poll batch error: {e}")

    if not rows:
        log.info("option_chain: 0 rows (no LTP data returned)")
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
    """Delete option_chain rows older than OPTION_RETENTION_DAYS."""
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(days=OPTION_RETENTION_DAYS)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM option_chain WHERE ts < %s", (cutoff,))
            deleted = cur.rowcount
        conn.commit()
        if deleted:
            log.info(f"option_chain cleanup: {deleted} rows deleted (>{OPTION_RETENTION_DAYS}d)")
    except Exception as e:
        log.warning(f"option_chain cleanup: {e}")


# ──────────────────────────────────────────────── market helpers

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+29)


# ──────────────────────────────────────────────── main run

def run(auth_code=None):
    import fyers_backfill
    from fyers_apiv3.FyersWebsocket import data_ws

    conn  = get_db()
    token = get_valid_token(conn, auth_code)
    futures, equity = get_universe(conn)
    futures_set = set(futures)
    log.info(f"Universe: {len(futures)} futures (1m WS) | option chain: NIFTY+BNIFTY ATM±{N_STRIKES} REST 5-min")

    # Option chain schema
    ensure_option_schema(conn)

    # ---- Layer 1: boot backfill (futures only) ----
    log.info("Boot backfill (7-day, futures only)...")
    try:
        fyers_backfill.backfill_7day(token, conn)
    except Exception as e:
        log.error(f"Boot backfill failed (continuing): {e}")

    agg = BarAggregator(conn, futures_set)
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
        log.info("WS connected - healing futures gaps then subscribing")
        try:
            fyers_backfill.heal_gap(token, conn, futures, '1', '1m')
        except Exception as e:
            log.error(f"Gap heal failed: {e}")
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

    # ---- Housekeeping thread ----
    def housekeeping():
        last_option_poll = None
        last_cleanup_day = None

        while True:
            if is_market_open():
                # Futures: index LTP + bar flush + CMP (every 30s)
                update_index_ltp(conn, token, agg)
                agg.flush_all()
                agg.flush_cmp()

                # Option chain: REST poll every 5 min
                now_dt = datetime.now(IST).replace(tzinfo=None)
                if (last_option_poll is None or
                        (now_dt - last_option_poll).total_seconds() >= OPTION_POLL_MINS * 60):
                    expiry = get_monthly_expiry()
                    try:
                        poll_option_chain(conn, token, expiry)
                    except Exception as e:
                        log.warning(f"option_chain poll failed: {e}")
                    last_option_poll = now_dt

            # Daily cleanup (once per calendar day, any time)
            today = datetime.now(IST).date()
            if last_cleanup_day != today:
                cleanup_option_chain(conn)
                last_cleanup_day = today

            time.sleep(30)

    threading.Thread(target=housekeeping, daemon=True).start()
    log.info("Connecting WebSocket (live, futures only)...")
    fyers_ws.connect()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()
    run(auth_code=args.auth_code)

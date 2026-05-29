"""
Fyers Live Feed - Scorr V8
============================
Standalone Railway WORKER (not in FastAPI). The live intraday source.

FUTURES-ONLY (29-May-2026): streams the ~208 futures universe in 1-min bars
via WebSocket (well under Fyers' ~200 subscription cap). The 1508 equity
stocks are NOT streamed — their prices come from Yahoo (EOD) and, later, a
free BSE 5-min delayed feed.

Architecture (3 layers, no rate-limit risk):
  1. BACKFILL  - on boot, one-time 7-day 1-min futures history (fyers_backfill).
  2. LIVE      - persistent WebSocket. Futures ticks aggregated locally into
                 1-min bars -> intraday_prices. Every tick's LTP also ->
                 cmp_prices (source='fyers'), making Fyers the PRIMARY
                 real-time CMP for futures. Near-zero REST calls.
  3. GAP HEAL  - on every WS (re)connect, patch the slice from the newest
                 stored candle -> now, so a drop never leaves a hole.

CMP ownership: Fyers primary for futures (real-time, every ~30s flush).
Yahoo (main.py) covers everything else and acts as fallback - it fills
symbols Fyers didn't update (stale > 3 min or not subscribed). If the Fyers
worker is down, all symbols go stale and Yahoo automatically covers the
whole universe.

Indices (NIFTY/BANKNIFTY/INDIAVIX) LTP -> cmp_prices.
Yahoo is NOT used here (EOD raw_prices handled separately by main.py 21:00).

TOKEN MODEL (Fyers v3, SEBI framework from 01-Apr-2026):
  Refresh-token flow is DISABLED (SEBI: continuous refresh sessions banned).
  -> ONE 2FA auth-code bootstrap per TRADING DAY.
  access_token  - valid the whole trading day, survives restarts.
  Stored in Railway table fyers_tokens (id=1).

  Boot logic:
    1. --auth-code given  -> bootstrap (mint + store today's token).
    2. else stored access_token created TODAY -> reuse it (restart-safe).
    3. else -> exit cleanly with login URL (no crash loop; wait for morning code).

USAGE:
  Daily bootstrap: python fyers_feed.py --auth-code <code>
  Restart (same day, no code needed): python fyers_feed.py
"""

import argparse, hashlib, os, time, logging, threading
from datetime import datetime, timedelta
import pytz, psycopg2, requests

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    'YXTIR2MN9V')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '2580')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL = 'https://api-t1.fyers.in/api/v3/validate-authcode'
QUOTES_URL   = 'https://api-t1.fyers.in/data/quotes'
IST          = pytz.timezone('Asia/Kolkata')

RETENTION_DAYS = 7

SKIP_SYMBOLS = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX'}
# Literal & — the Fyers symbol-master (WS validation) expects NSE:M&M-EQ, and
# requests URL-encodes it correctly for REST. %26 fails both paths.
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}
INDEX_LTP_SYMBOLS = {
    'NIFTY':     'NSE:NIFTY50-INDEX',
    'BANKNIFTY': 'NSE:NIFTYBANK-INDEX',
    'INDIAVIX':  'NSE:INDIAVIX-INDEX',
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')


# ---------------------------------------------------------------- DB / token

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
    # SEBI Apr-2026: refresh-token flow is DISABLED. One 2FA auth-code per
    # trading day. Token is valid all day and survives restarts.
    #   1. --auth-code given -> bootstrap (mint + store today's token).
    #   2. else stored access_token created TODAY -> reuse it (restart-safe).
    #   3. else -> exit cleanly with login URL (no crash loop).
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
        log.warning("Stored access token is from a previous day - new 2FA needed")
    raise SystemExit(
        "\nNO VALID TOKEN FOR TODAY (SEBI: daily 2FA, no refresh).\n"
        f"  1. https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}"
        "&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n"
        "  2. python fyers_feed.py --auth-code <code>\n")


# ---------------------------------------------------------------- universe

def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe")
        futures = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT symbol FROM cmp_prices")
        alls = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS), sorted((alls - futures) - SKIP_SYMBOLS)

def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')
def from_fyers_symbol(fsym):
    # 'NSE:SBIN-EQ' -> 'SBIN' ; handle M&M (NSE:M&M-EQ -> M&M)
    if fsym == 'NSE:M&M-EQ': return 'M&M'
    return fsym.replace('NSE:', '').replace('-EQ', '')


# ---------------------------------------------------------------- bar aggregator

class BarAggregator:
    """
    Accumulates ticks into OHLCV bars per (symbol, timeframe) and flushes
    completed bars to intraday_prices. Also tracks latest LTP per symbol and
    flushes it to cmp_prices (source='fyers') - Fyers is the PRIMARY CMP feed.
    Futures-only: every streamed symbol is a future -> 1-min bars.
    """
    def __init__(self, conn, futures_set):
        self.conn = conn
        self.futures = futures_set       # set of raw symbols using 1-min
        self.bars = {}                   # (sym, tf) -> dict bar
        self.last_ltp = {}               # sym -> latest ltp (for cmp_prices, primary)
        self.lock = threading.Lock()

    def _bucket(self, ts, minutes):
        floored = ts.replace(second=0, microsecond=0)
        floored = floored - timedelta(minutes=floored.minute % minutes)
        return floored

    def on_tick(self, sym, ltp, vol, ts=None):
        ts = ts or datetime.now(IST).replace(tzinfo=None)
        # Futures-only stream: all symbols are 1-min. (Fallback to 1m if
        # an unexpected symbol arrives.)
        tf, mins = ('1m', 1)
        bkt = self._bucket(ts, mins)
        key = (sym, tf)
        with self.lock:
            self.last_ltp[sym] = ltp     # newest price for CMP
            bar = self.bars.get(key)
            if bar is None or bar['ts'] != bkt:
                if bar is not None:
                    self._flush(key, bar)        # previous bar complete
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
        """Write latest LTP per symbol to cmp_prices as PRIMARY (source='fyers')."""
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


# ---------------------------------------------------------------- index LTP

def update_index_ltp(conn, token):
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
                if fsym == item['n']: rows.append((name, lp))
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,NOW(),'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=NOW(), source='fyers'""", rows)
            conn.commit()
    except Exception as e:
        log.warning(f"Index LTP: {e}")


# ---------------------------------------------------------------- main run

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5: return False
    mins = now.hour * 60 + now.minute
    return (9*60+15) <= mins <= (15*60+30)

def run(auth_code=None):
    import fyers_backfill
    from fyers_apiv3.FyersWebsocket import data_ws

    conn  = get_db()
    token = get_valid_token(conn, auth_code)
    futures, equity = get_universe(conn)
    futures_set = set(futures)
    log.info(f"Universe: {len(futures)} futures (1m streamed) | {len(equity)} equity (NOT streamed - Yahoo/BSE)")

    # ---- Layer 1: one-time backfill on boot (FUTURES ONLY) ----
    log.info("Boot backfill (7-day, futures only)...")
    try:
        fyers_backfill.backfill_7day(token, conn)
    except Exception as e:
        log.error(f"Boot backfill failed (continuing to live): {e}")

    agg = BarAggregator(conn, futures_set)
    # FUTURES ONLY on the websocket -> stays under Fyers ~200 subscription cap.
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
        # ---- Layer 3: gap heal on every (re)connect (FUTURES ONLY) ----
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

    # ---- Layer 2: live websocket (blocking, auto-reconnect) ----
    # Periodic flush + CMP + index LTP on a side thread.
    # NOTE: No daily token refresh here. SEBI (Apr-2026) disabled the
    # refresh endpoint -> token is bootstrapped once per trading day via
    # --auth-code. A new day requires a fresh 2FA bootstrap (manual restart).
    def housekeeping():
        while True:
            if is_market_open():
                update_index_ltp(conn, token)
                agg.flush_all()   # flush partial bars every cycle (safety)
                agg.flush_cmp()   # PRIMARY CMP - latest LTP per symbol -> cmp_prices
            time.sleep(30)

    threading.Thread(target=housekeeping, daemon=True).start()
    log.info("Connecting WebSocket (live, futures only)...")
    fyers_ws.connect()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()
    run(auth_code=args.auth_code)

"""
Fyers Auto-Login (TOTP) - Scorr V8
====================================
Fully automated daily login. No browser, no manual auth-code.

SEBI (Apr-2026) requires daily 2FA. This script does the whole 2FA chain
headless using the TOTP secret, mints a fresh auth_code, exchanges it for a
day-valid access token, and stores it in Railway table fyers_tokens (id=1).

5-step flow (Fyers v3):
  1. send_login_otp   -> request_key
  2. verify_otp       (TOTP 6-digit via pyotp) -> request_key
  3. verify_pin       (PIN, base64) -> access_token (vagator session)
  4. token            -> auth_code
  5. validate-authcode(appIdHash + auth_code) -> final access_token -> DB

*** ACCOUNT-BLOCK SAFETY ***
Fyers blocks the account after ~5 failed TOTP attempts. A crashing worker that
restarts fast can burn all 5 in seconds. To prevent that:
  - CIRCUIT BREAKER: every attempt stamps fyers_tokens.last_attempt. A new
    auto-login is REFUSED if the last attempt was < ATTEMPT_COOLDOWN secs ago.
    This survives container restarts (state is in the DB, not memory).
  - So even if Railway restart-loops the worker, only ONE TOTP attempt fires
    per cooldown window — the account can never be hammered to a block.

All secrets come from ENV VARS (safe for the public repo):
  FYERS_FY_ID, FYERS_CLIENT_ID, FYERS_SECRET, FYERS_PIN, FYERS_TOTP_SECRET,
  FYERS_REDIRECT_URI (default http://127.0.0.1), DATABASE_URL

USAGE:
  Standalone:  python fyers_autologin.py
  Imported:    from fyers_autologin import auto_login; token = auto_login(conn)
"""

import os, time, base64, hashlib, logging
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
import requests, pyotp, pytz, psycopg2

IST = pytz.timezone('Asia/Kolkata')

FY_ID         = os.environ.get('FYERS_FY_ID', 'XA32319')
CLIENT_ID     = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
SECRET        = os.environ.get('FYERS_SECRET', '')
PIN           = os.environ.get('FYERS_PIN', '')
TOTP_SECRET   = (os.environ.get('FYERS_TOTP_SECRET', '') or '').strip().replace(' ', '')
REDIRECT_URI  = os.environ.get('FYERS_REDIRECT_URI', 'http://127.0.0.1')
DATABASE_URL  = os.environ.get('DATABASE_URL')

# Refuse another auto-login attempt within this many seconds of the last one.
# Protects the Fyers account from a restart loop burning all 5 TOTP tries.
ATTEMPT_COOLDOWN = 90

# cc#645 fix_4: HARD cap on TOTP logins per rolling hour, PERSISTED in the DB so an
# os._exit/restart loop can NEVER bypass it (24-Jul incident: the os._exit restart loop
# re-ran a TOTP login every ~7 min, and each mint that then failed its boot self-test
# triggered another restart — the account-block + Fyers-regime ban vector). At most this
# many real TOTP attempts fire in any 60-min window, across restarts.
MAX_LOGINS_PER_HOUR = 3

# Vagator (login) + API v3 endpoints
BASE_VAGATOR = 'https://api-t2.fyers.in/vagator/v2'
BASE_API     = 'https://api-t1.fyers.in/api/v3'
URL_SEND_OTP   = BASE_VAGATOR + '/send_login_otp_v2'
URL_VERIFY_OTP = BASE_VAGATOR + '/verify_otp'
URL_VERIFY_PIN = BASE_VAGATOR + '/verify_pin_v2'
URL_TOKEN      = BASE_API + '/token'
URL_VALIDATE   = BASE_API + '/validate-authcode'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_autologin')


def _b64(s):
    return base64.b64encode(str(s).encode('ascii')).decode('ascii')

def _app_id_hash():
    return hashlib.sha256(f'{CLIENT_ID}:{SECRET}'.encode()).hexdigest()


# ---------------------------------------------------------------- circuit breaker

def _ensure_attempt_col(conn):
    """Make sure fyers_tokens has a last_attempt column (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE fyers_tokens ADD COLUMN IF NOT EXISTS last_attempt timestamp")
    conn.commit()

def _too_soon(conn):
    """True if an auto-login was attempted within ATTEMPT_COOLDOWN seconds."""
    with conn.cursor() as cur:
        cur.execute("SELECT last_attempt FROM fyers_tokens WHERE id=1")
        r = cur.fetchone()
    if not r or not r[0]:
        return False
    age = (datetime.now(IST).replace(tzinfo=None) - r[0]).total_seconds()
    return age < ATTEMPT_COOLDOWN

def _stamp_attempt(conn):
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM fyers_tokens WHERE id=1")
        if cur.fetchone():
            cur.execute("UPDATE fyers_tokens SET last_attempt=%s WHERE id=1", (now,))
        else:
            cur.execute("INSERT INTO fyers_tokens (id,last_attempt) VALUES (1,%s)", (now,))
    conn.commit()


# ---------------------------------------------------------------- rolling-hour login cap (cc#645 fix_4)

def _ensure_attempts_table(conn):
    """cc#645 fix_4: a tiny append-only table recording every real TOTP attempt, so the
    rolling-hour cap survives os._exit / container restarts (in-memory counters do not)."""
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS fyers_login_attempts (
                           id SERIAL PRIMARY KEY,
                           attempted_at timestamp NOT NULL DEFAULT (NOW() AT TIME ZONE 'Asia/Kolkata')
                       )""")
    conn.commit()

def _hourly_cap_reached(conn):
    """True if MAX_LOGINS_PER_HOUR real TOTP attempts already fired in the last 60 min."""
    cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(hours=1)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM fyers_login_attempts WHERE attempted_at >= %s", (cutoff,))
        n = cur.fetchone()[0] or 0
    return n >= MAX_LOGINS_PER_HOUR, n

def _record_attempt(conn):
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO fyers_login_attempts (attempted_at) VALUES (%s)", (now,))
        # keep the table tiny — drop anything older than 2 days
        cur.execute("DELETE FROM fyers_login_attempts WHERE attempted_at < %s",
                    (now - timedelta(days=2),))
    conn.commit()


# ---------------------------------------------------------------- 2FA chain

def get_auth_code():
    """Run the headless TOTP 2FA chain and return a fresh auth_code."""
    if not (TOTP_SECRET and PIN and SECRET):
        raise SystemExit("Auto-login missing env vars (FYERS_TOTP_SECRET / FYERS_PIN / FYERS_SECRET).")

    # 1. send_login_otp
    r = requests.post(URL_SEND_OTP, json={'fy_id': _b64(FY_ID), 'app_id': '2'}, timeout=10).json()
    if 'request_key' not in r:
        raise Exception(f"send_login_otp failed: {r}")
    req_key = r['request_key']

    # 2. verify_otp — generate the code at a CLEAN window start so it can't
    #    roll over mid-request. Wait until we're early in a 30s slot.
    sec = datetime.now().second % 30
    if sec > 25 or sec < 1:
        time.sleep(31 - sec if sec > 25 else 1)
    otp = pyotp.TOTP(TOTP_SECRET).now()
    r = requests.post(URL_VERIFY_OTP, json={'request_key': req_key, 'otp': otp}, timeout=10).json()
    if 'request_key' not in r:
        raise Exception(f"verify_otp failed: {r}")
    req_key2 = r['request_key']

    # 3. verify_pin
    r = requests.post(URL_VERIFY_PIN, json={
        'request_key': req_key2, 'identity_type': 'pin', 'identifier': _b64(PIN)
    }, timeout=10).json()
    access = r.get('data', {}).get('access_token')
    if not access:
        raise Exception(f"verify_pin failed: {r}")

    # 4. token -> auth_code
    r = requests.post(URL_TOKEN, headers={'Authorization': f'Bearer {access}'}, json={
        'fyers_id': FY_ID,
        'app_id': CLIENT_ID.split('-')[0],
        'redirect_uri': REDIRECT_URI,
        'appType': CLIENT_ID.split('-')[-1],
        'code_challenge': '',
        'state': 'sample',
        'scope': '',
        'nonce': '',
        'response_type': 'code',
        'create_cookie': True,
    }, timeout=10).json()
    url = r.get('Url') or r.get('url')
    if not url:
        raise Exception(f"token (auth_code) failed: {r}")
    auth_code = parse_qs(urlparse(url).query)['auth_code'][0]
    log.info("Auto-login: auth_code obtained via TOTP")
    return auth_code


def exchange_auth_code(auth_code):
    """Exchange auth_code for the final day-valid access token.

    cc#645 fix_2: the refresh_token Fyers returns is DISCARDED — the refresh-token flow is
    discontinued (SEBI Apr-2026 framework), TOTP daily 2FA is the ONLY auth path. Return
    the access token only."""
    r = requests.post(URL_VALIDATE, json={
        'grant_type': 'authorization_code',
        'appIdHash': _app_id_hash(),
        'code': auth_code,
    }, timeout=10).json()
    if r.get('code') != 200 or 'access_token' not in r:
        raise Exception(f"validate-authcode failed: {r}")
    return r['access_token']


def save_token(conn, access):
    """cc#645 fix_2: store the day-valid access token only — no refresh_token is written
    (refresh flow discontinued). The legacy refresh_token column is left untouched."""
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM fyers_tokens WHERE id=1")
        if cur.fetchone():
            cur.execute("UPDATE fyers_tokens SET access_token=%s, access_created=%s, updated_at=NOW() WHERE id=1",
                        (access, now))
        else:
            cur.execute("INSERT INTO fyers_tokens (id,access_token,access_created,updated_at) VALUES (1,%s,%s,NOW())",
                        (access, now))
    conn.commit()


def auto_login(conn=None):
    """Full headless login -> store token -> return access token.

    Circuit-breaker protected: refuses to attempt if another attempt happened
    within ATTEMPT_COOLDOWN seconds (prevents account block from restart loops).

    cc#489 fix_1 (16-Jul incident): ALWAYS opens its own short-lived connection
    for the fyers_tokens writes (breaker stamp + token save) and closes ONLY
    that one. The `conn` param is accepted for backward-compat call sites but
    is never touched — this function must NEVER be able to close a
    caller-supplied connection (root cause of the worker's global DB conn
    dying right after boot auth on 16-Jul)."""
    own_conn = psycopg2.connect(DATABASE_URL)
    try:
        _ensure_attempt_col(own_conn)
        _ensure_attempts_table(own_conn)   # cc#645 fix_4
        if _too_soon(own_conn):
            raise SystemExit(
                f"Auto-login SKIPPED — another attempt < {ATTEMPT_COOLDOWN}s ago "
                "(account-block protection). Wait, then retry.")
        # cc#645 fix_4: HARD rolling-hour cap, persisted so a restart loop can't bypass it.
        capped, n = _hourly_cap_reached(own_conn)
        if capped:
            raise SystemExit(
                f"Auto-login CAPPED — {n} TOTP logins already in the last hour "
                f"(>= {MAX_LOGINS_PER_HOUR} rolling-hour cap, cc#645 fix_4). Backing off, "
                "not attempting (do not exit/restart-loop).")
        _record_attempt(own_conn)  # cc#645 fix_4: count BEFORE trying, so a crash still counts
        _stamp_attempt(own_conn)   # record BEFORE trying, so a crash still counts
        auth_code = get_auth_code()
        access = exchange_auth_code(auth_code)
        save_token(own_conn, access)
        log.info("Auto-login OK - fresh token stored (valid for today)")
        return access
    finally:
        own_conn.close()


def try_relogin(conn):
    """cc#473 item 3: breaker-SAFE wrapper for in-loop self-heal. NEVER raises
    SystemExit (which, uncaught inside the feed loop, turned the 90s cooldown into a
    Railway crash-loop on 13-Jul). Returns a dict:
      {'ok': bool, 'token': str|None, 'skipped': bool, 'error': str|None}
    'skipped'=True => the 90s ATTEMPT_COOLDOWN breaker refused this attempt; the
    caller must BACK OFF 90s and retry, not crash. The breaker still guarantees at
    most one real TOTP attempt per cooldown window (account-block protection)."""
    try:
        tok = auto_login(conn)
        return {'ok': True, 'token': tok, 'skipped': False, 'capped': False, 'error': None}
    except SystemExit as e:
        msg = str(e)
        capped = 'CAPPED' in msg
        skipped = capped or ('SKIPPED' in msg) or ('cooldown' in msg.lower()) or ('block protection' in msg.lower())
        return {'ok': False, 'token': None, 'skipped': skipped, 'capped': capped, 'error': msg}
    except Exception as e:
        return {'ok': False, 'token': None, 'skipped': False, 'capped': False, 'error': str(e)}


if __name__ == '__main__':
    auto_login()

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

All secrets come from ENV VARS (safe for the public repo):
  FYERS_FY_ID         e.g. XA32319   (your login/client ID)
  FYERS_CLIENT_ID     e.g. 1A4STS8ZGD-100  (app id)
  FYERS_SECRET        app secret
  FYERS_PIN           4-digit PIN
  FYERS_TOTP_SECRET   TOTP key from myaccount.fyers.in/ManageAccount
  FYERS_REDIRECT_URI  default http://127.0.0.1
  DATABASE_URL

USAGE:
  Standalone:  python fyers_autologin.py
  Imported:    from fyers_autologin import auto_login; token = auto_login(conn)
"""

import os, time, base64, hashlib, logging
from datetime import datetime
from urllib.parse import parse_qs, urlparse
import requests, pyotp, pytz, psycopg2

IST = pytz.timezone('Asia/Kolkata')

FY_ID         = os.environ.get('FYERS_FY_ID', 'XA32319')
CLIENT_ID     = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
SECRET        = os.environ.get('FYERS_SECRET', '')
PIN           = os.environ.get('FYERS_PIN', '')
TOTP_SECRET   = os.environ.get('FYERS_TOTP_SECRET', '')
REDIRECT_URI  = os.environ.get('FYERS_REDIRECT_URI', 'http://127.0.0.1')
DATABASE_URL  = os.environ.get('DATABASE_URL')

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


def get_auth_code():
    """Run the headless TOTP 2FA chain and return a fresh auth_code."""
    if not (TOTP_SECRET and PIN and SECRET):
        raise SystemExit("Auto-login missing env vars (FYERS_TOTP_SECRET / FYERS_PIN / FYERS_SECRET).")

    # 1. send_login_otp
    r = requests.post(URL_SEND_OTP, json={'fy_id': _b64(FY_ID), 'app_id': '2'}, timeout=10).json()
    if 'request_key' not in r:
        raise Exception(f"send_login_otp failed: {r}")
    req_key = r['request_key']

    # 2. verify_otp (avoid a TOTP rollover at the 30s boundary)
    if datetime.now().second % 30 > 27:
        time.sleep(4)
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
    """Exchange auth_code for the final day-valid access token."""
    r = requests.post(URL_VALIDATE, json={
        'grant_type': 'authorization_code',
        'appIdHash': _app_id_hash(),
        'code': auth_code,
    }, timeout=10).json()
    if r.get('code') != 200 or 'access_token' not in r:
        raise Exception(f"validate-authcode failed: {r}")
    return r['access_token'], r.get('refresh_token')


def save_token(conn, access, refresh):
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM fyers_tokens WHERE id=1")
        if cur.fetchone():
            cur.execute("""UPDATE fyers_tokens SET access_token=%s, refresh_token=%s,
                           access_created=%s, refresh_created=%s, updated_at=NOW() WHERE id=1""",
                        (access, refresh, now, now))
        else:
            cur.execute("""INSERT INTO fyers_tokens (id,access_token,refresh_token,access_created,refresh_created,updated_at)
                           VALUES (1,%s,%s,%s,%s,NOW())""", (access, refresh, now, now))
    conn.commit()


def auto_login(conn=None):
    """Full headless login -> store token -> return access token."""
    own = conn is None
    if own:
        conn = psycopg2.connect(DATABASE_URL)
    try:
        auth_code = get_auth_code()
        access, refresh = exchange_auth_code(auth_code)
        save_token(conn, access, refresh)
        log.info("Auto-login OK - fresh token stored (valid for today)")
        return access
    finally:
        if own:
            conn.close()


if __name__ == '__main__':
    auto_login()

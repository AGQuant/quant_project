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

*** ACCOUNT-BLOCK SAFETY — cc#883, 07-Aug-2026 ***
Fyers blocks the account after ~5 failed attempts. On 07-Aug the old design burned
all five in eleven minutes (ops_log 17106-17120) and the account was BLOCKED at
10:07 IST. Two things were wrong and both are fixed here.

FOUNDER PRINCIPLE (07-Aug): a worker that cannot authenticate goes to SLEEP, not to war.
Never trouble the broker with repeated failures.

  1. THE 90-SECOND COOLDOWN IS NOT A BUDGET. It only spaces attempts out; it never
     stops them. Retrying every 90s with a stale PIN is a hammer with a slow handle.
     So the breaker is now TWO layers:
       a) ATTEMPT_COOLDOWN (90s) — unchanged, still stops a restart loop from firing
          two attempts back to back.
       b) A PERSISTED FAILURE LADDER (fyers_auth_state) — counts CONSECUTIVE real
          failures and escalates the sleep. PIN: 1st failure sleeps 15 minutes, 2nd
          stops automatic relogin for the rest of the day. OTP/TOTP: 15 min, then
          60 min, then stop for the day. A broker-side block (-1021 / user_blocked)
          halts PERMANENTLY until a human clears the flag.
     OUR ceiling is 2 of the broker's 5 attempts. We never try to detect their reset
     window — we simply never get near it.

  2. NO DDL ON THE AUTH PATH. The old code ran
       ALTER TABLE fyers_tokens ADD COLUMN IF NOT EXISTS last_attempt
     on every single login. On 07-Aug that ALTER queued behind an idle-in-transaction
     SELECT on the same table (pids 756588/756589) and every relogin died with
     "canceling statement due to statement timeout" for four minutes. Schema changes
     now happen ONCE, at boot, in migrate_schema(), before any WS or auth thread
     starts. The auth path does plain DML only, on an AUTOCOMMIT connection so it can
     never leave an open transaction sitting on fyers_tokens — that idle reader was
     the other half of the wedge.

A DB failure is NOT a PIN attempt. statement_timeout and lock_timeout are set low
(10s / 5s) on the auth connection; when one fires we log feed_auth_db_wedge with the
blocking pid and return without touching the failure ladder, because nothing ever
reached Fyers.

All secrets come from ENV VARS (safe for the public repo):
  FYERS_FY_ID, FYERS_CLIENT_ID, FYERS_SECRET, FYERS_PIN, FYERS_TOTP_SECRET,
  FYERS_REDIRECT_URI (default http://127.0.0.1), DATABASE_URL

USAGE:
  Standalone:  python fyers_autologin.py
  Imported:    from fyers_autologin import auto_login; token = auto_login(conn)

CLEARING A HALT (human action, deliberately manual):
  UPDATE fyers_auth_state SET blocked=FALSE, halted_for_day=NULL, halted_until=NULL,
         pin_fails=0, otp_fails=0, halt_reason=NULL WHERE id=1;
"""

import os, time, base64, hashlib, json, logging
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
# Protects the Fyers account from a restart loop burning attempts back to back.
ATTEMPT_COOLDOWN = 90

# cc#883 item 1 — the failure ladder. Sleep lengths in seconds.
PIN_SLEEP_FIRST   = 15 * 60          # 1st PIN failure: sleep 15 minutes
PIN_MAX_AUTO      = 2                # 2nd PIN failure: stop for the day. OUR ceiling, not theirs.
OTP_SLEEPS        = [15 * 60, 60 * 60]   # OTP/TOTP: 15 min, then 60 min, then stop for the day
OTP_MAX_AUTO      = 3

# cc#883 items 3+4 — the auth connection is deliberately impatient. A wedge should surface
# as an error in seconds, not as a four-minute stall nobody can see.
AUTH_STATEMENT_TIMEOUT_MS = 10000
AUTH_LOCK_TIMEOUT_MS      = 5000
# The BOOT migration gets the same short lock_timeout: if it cannot get the lock quickly it
# must fail loudly at boot rather than block the worker's startup behind someone else's txn.
MIGRATION_LOCK_TIMEOUT_MS = 5000

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

def _now():
    return datetime.now(IST).replace(tzinfo=None)


class AuthStageError(Exception):
    """A 2FA stage that actually reached Fyers and was refused.

    Carries the stage name and the raw response so the ladder can tell a wrong PIN
    (-1005, our fault, counts) apart from an account block (-1021, stop everything)
    apart from a network blip. A bare Exception could not, which is why the old code
    treated every failure identically and retried all of them the same way."""
    def __init__(self, stage, payload):
        self.stage = stage
        self.payload = payload if isinstance(payload, dict) else {'raw': str(payload)}
        super().__init__(f"{stage} failed: {payload}")

    @property
    def code(self):
        for k in ('code', 's', 'errorCode'):
            v = self.payload.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.lstrip('-').isdigit():
                return int(v)
        return None

    @property
    def blocked(self):
        """True when Fyers says the ACCOUNT is blocked — never retry automatically."""
        if self.code == -1021:
            return True
        if self.payload.get('user_blocked') is True:
            return True
        msg = json.dumps(self.payload).lower()
        return 'user_blocked' in msg or 'account is blocked' in msg


class AuthDbWedge(Exception):
    """A DB timeout or lock failure on the auth path. Nothing reached Fyers, so this
    must NEVER count against the attempt ladder (cc#883 item 4)."""


# ---------------------------------------------------------------- connections

def _connect(statement_timeout_ms, lock_timeout_ms, autocommit):
    conn = psycopg2.connect(
        DATABASE_URL,
        connect_timeout=10,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        options=f"-c statement_timeout={statement_timeout_ms} -c lock_timeout={lock_timeout_ms}",
    )
    conn.autocommit = autocommit
    return conn


def _auth_conn():
    """cc#883 item 3: AUTOCOMMIT. Every read commits the instant it returns, so the auth
    path can never be the idle-in-transaction reader that another statement queues behind.
    That reader is half of what wedged 07-Aug; the ALTER was only the other half."""
    return _connect(AUTH_STATEMENT_TIMEOUT_MS, AUTH_LOCK_TIMEOUT_MS, autocommit=True)


def _is_db_wedge(exc):
    """A statement_timeout / lock_timeout / deadlock, as opposed to a real DB error."""
    code = getattr(exc, 'pgcode', None)
    if code in ('57014', '55P03', '40P01'):     # query_canceled, lock_not_available, deadlock
        return True
    msg = str(exc).lower()
    return ('statement timeout' in msg) or ('lock timeout' in msg) or ('canceling statement' in msg)


def _blocking_pids():
    """Best-effort: who is holding the lock on fyers_tokens right now. Written into the
    feed_auth_db_wedge alert so the next incident starts with the answer instead of the
    ten minutes it took to find pid 756588 by hand on 07-Aug."""
    try:
        c = _connect(5000, 2000, autocommit=True)
        try:
            with c.cursor() as cur:
                cur.execute("""
                    SELECT a.pid, a.state, LEFT(COALESCE(a.query, ''), 120)
                    FROM pg_stat_activity a
                    JOIN pg_locks l ON l.pid = a.pid
                    WHERE l.relation = 'fyers_tokens'::regclass
                      AND a.pid <> pg_backend_pid()
                    ORDER BY a.state_change
                    LIMIT 5
                """)
                return [{'pid': r[0], 'state': r[1], 'query': r[2]} for r in cur.fetchall()]
        finally:
            c.close()
    except Exception as e:
        return [{'error': str(e)[:120]}]


def _alert(event, message, extra=None):
    """One ops_log row per real decision. Always on its own fresh short-lived connection —
    an alert about a wedged connection must not travel over the wedged connection (cc#497)."""
    payload = {'detail': message, 'ist': datetime.now(IST).isoformat()}
    if extra:
        payload.update(extra)
    log.error(f"{event}: {message}")
    c = None
    try:
        c = _connect(10000, 5000, autocommit=True)
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                "VALUES (CURRENT_DATE, NOW(), 'alert', %s, %s::jsonb)",
                (event, json.dumps(payload)))
    except Exception as e:
        log.warning(f"_alert({event}) could not be written: {e}")
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass


# ---------------------------------------------------------------- boot migration

MIGRATION_SQL = [
    "ALTER TABLE fyers_tokens ADD COLUMN IF NOT EXISTS last_attempt timestamp",
    """CREATE TABLE IF NOT EXISTS fyers_auth_state (
           id             SMALLINT PRIMARY KEY,
           pin_fails      INT  NOT NULL DEFAULT 0,
           otp_fails      INT  NOT NULL DEFAULT 0,
           halted_until   TIMESTAMP,
           halted_for_day DATE,
           blocked        BOOLEAN NOT NULL DEFAULT FALSE,
           halt_reason    TEXT,
           updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
       )""",
    "INSERT INTO fyers_auth_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING",
]


def migrate_schema():
    """cc#883 item 3: ALL schema work for the auth path, ONCE, at boot, before any WS or
    auth thread starts. Its own short transaction with an explicit commit and a 5s
    lock_timeout, so it either completes quickly or fails loudly — it can never sit
    holding up a login the way the per-call ALTER did on 07-Aug.

    Returns True on success. A failure is logged and returned as False rather than raised:
    the feed's job is to tick, and a migration that cannot get its lock at boot is a
    problem to alert on, not a reason to refuse to start."""
    conn = None
    try:
        conn = _connect(30000, MIGRATION_LOCK_TIMEOUT_MS, autocommit=False)
        with conn.cursor() as cur:
            for stmt in MIGRATION_SQL:
                cur.execute(stmt)
        conn.commit()
        log.info("cc#883 auth schema migration OK (fyers_tokens.last_attempt, fyers_auth_state)")
        return True
    except Exception as e:
        if conn is not None:
            try: conn.rollback()
            except Exception: pass
        _alert('feed_auth_migration_failed',
               f"boot migration could not complete: {str(e)[:200]}",
               {'blocking': _blocking_pids()})
        return False
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


# ---------------------------------------------------------------- persisted failure ladder

_STATE_COLS = ('pin_fails', 'otp_fails', 'halted_until', 'halted_for_day', 'blocked', 'halt_reason')


def read_state(conn=None):
    """Current ladder state. Never raises on a missing table — a worker booting against a
    database that has not been migrated yet must still be able to log in, not refuse to."""
    own = conn is None
    c = _auth_conn() if own else conn
    try:
        with c.cursor() as cur:
            cur.execute(f"SELECT {', '.join(_STATE_COLS)} FROM fyers_auth_state WHERE id=1")
            r = cur.fetchone()
        if not r:
            return dict(pin_fails=0, otp_fails=0, halted_until=None,
                        halted_for_day=None, blocked=False, halt_reason=None)
        return dict(zip(_STATE_COLS, r))
    except Exception as e:
        if _is_db_wedge(e):
            raise AuthDbWedge(str(e))
        log.warning(f"read_state: {e} — treating the ladder as clear")
        return dict(pin_fails=0, otp_fails=0, halted_until=None,
                    halted_for_day=None, blocked=False, halt_reason=None)
    finally:
        if own:
            try: c.close()
            except Exception: pass


def _write_state(**fields):
    """Plain DML on an autocommit connection. No DDL, no open transaction."""
    if not fields:
        return
    sets = ', '.join(f"{k}=%s" for k in fields)
    vals = list(fields.values())
    c = None
    try:
        c = _auth_conn()
        with c.cursor() as cur:
            cur.execute(f"UPDATE fyers_auth_state SET {sets}, updated_at=NOW() WHERE id=1", vals)
            if cur.rowcount == 0:
                cur.execute("INSERT INTO fyers_auth_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
                cur.execute(f"UPDATE fyers_auth_state SET {sets}, updated_at=NOW() WHERE id=1", vals)
    except Exception as e:
        log.warning(f"_write_state: {e}")
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass


def _clear_ladder():
    _write_state(pin_fails=0, otp_fails=0, halted_until=None,
                 halted_for_day=None, halt_reason=None)


def _gate(state):
    """Return a human reason to REFUSE this attempt, or None to proceed.

    Order matters: a broker-side block outranks everything, then the day halt, then the
    escalating sleep, then the 90-second spacing. Each one is a strictly stronger claim
    than the next, so the message the operator sees is always the most important one."""
    if state.get('blocked'):
        return ("Auto-login HALTED — Fyers reported the account BLOCKED. This does not clear "
                "itself. A human must confirm the account is usable and clear fyers_auth_state. "
                f"Reason on record: {state.get('halt_reason') or 'user_blocked'}")
    day = state.get('halted_for_day')
    if day and day == _now().date():
        return ("Auto-login STOPPED for today after repeated authentication failures "
                f"({state.get('halt_reason') or 'see ops_log feed_auth_pin_halt'}). "
                "Check FYERS_PIN in Railway against the PIN set at Fyers, then clear "
                "fyers_auth_state to resume.")
    until = state.get('halted_until')
    if until and until > _now():
        mins = int((until - _now()).total_seconds() // 60) + 1
        return (f"Auto-login SLEEPING for another ~{mins} min after a failed attempt "
                f"({state.get('halt_reason') or 'escalating backoff'}). Not contacting Fyers.")
    return None


def _record_failure(exc, state):
    """Advance the ladder for a failure that genuinely reached Fyers."""
    if not isinstance(exc, AuthStageError):
        # Network/parse trouble on our side. Space the next try out, but never spend a rung
        # of the ladder on it — the broker did not refuse us.
        _write_state(halted_until=_now() + timedelta(seconds=PIN_SLEEP_FIRST),
                     halt_reason=f"transport failure: {str(exc)[:120]}")
        _alert('feed_auth_transport_backoff',
               f"auth chain failed before the broker answered: {str(exc)[:160]}. "
               f"Sleeping {PIN_SLEEP_FIRST // 60} min.")
        return

    if exc.blocked:
        _write_state(blocked=True,
                     halt_reason=f"{exc.stage}: account blocked by Fyers ({exc.code})")
        _alert('feed_auth_account_blocked',
               f"Fyers reports the ACCOUNT BLOCKED at {exc.stage} (code {exc.code}). All "
               "automatic relogin is stopped permanently until a human clears "
               "fyers_auth_state. The WS keeps running on whatever token exists.",
               {'payload': exc.payload})
        return

    if exc.stage == 'verify_pin':
        fails = int(state.get('pin_fails') or 0) + 1
        if fails >= PIN_MAX_AUTO:
            _write_state(pin_fails=fails, halted_for_day=_now().date(),
                         halt_reason=f"verify_pin failed {fails}x (code {exc.code})")
            _alert('feed_auth_pin_halt',
                   f"verify_pin failed {fails} times (code {exc.code}). STOPPING all automatic "
                   "relogin for today at 2 of the broker's 5 attempts. WHAT TO DO: check "
                   "FYERS_PIN in Railway against the PIN currently set at Fyers, fix it, then "
                   "clear fyers_auth_state. The WS continues on the existing token.",
                   {'payload': exc.payload, 'attempts_used_by_us': fails})
        else:
            _write_state(pin_fails=fails,
                         halted_until=_now() + timedelta(seconds=PIN_SLEEP_FIRST),
                         halt_reason=f"verify_pin failed (code {exc.code})")
            _alert('feed_auth_pin_backoff',
                   f"verify_pin failed (code {exc.code}). Sleeping {PIN_SLEEP_FIRST // 60} min. "
                   f"One more failure stops automatic relogin for the day.",
                   {'payload': exc.payload})
        return

    # send_login_otp / verify_otp / token / validate-authcode
    fails = int(state.get('otp_fails') or 0) + 1
    if fails >= OTP_MAX_AUTO:
        _write_state(otp_fails=fails, halted_for_day=_now().date(),
                     halt_reason=f"{exc.stage} failed {fails}x (code {exc.code})")
        _alert('feed_auth_otp_halt',
               f"{exc.stage} failed {fails} times (code {exc.code}). STOPPING automatic relogin "
               "for today. WHAT TO DO: check FYERS_TOTP_SECRET and the server clock, then clear "
               "fyers_auth_state.",
               {'payload': exc.payload})
    else:
        sleep_s = OTP_SLEEPS[min(fails, len(OTP_SLEEPS)) - 1]
        _write_state(otp_fails=fails, halted_until=_now() + timedelta(seconds=sleep_s),
                     halt_reason=f"{exc.stage} failed (code {exc.code})")
        _alert('feed_auth_otp_backoff',
               f"{exc.stage} failed (code {exc.code}). Sleeping {sleep_s // 60} min "
               f"(failure {fails} of {OTP_MAX_AUTO}).",
               {'payload': exc.payload})


# ---------------------------------------------------------------- 90s spacing breaker

def _too_soon(conn):
    """True if an auto-login was attempted within ATTEMPT_COOLDOWN seconds."""
    with conn.cursor() as cur:
        cur.execute("SELECT last_attempt FROM fyers_tokens WHERE id=1")
        r = cur.fetchone()
    if not r or not r[0]:
        return False
    age = (_now() - r[0]).total_seconds()
    return age < ATTEMPT_COOLDOWN


def _stamp_attempt(conn):
    """One statement, no read-then-write. On an autocommit connection this leaves nothing
    open on fyers_tokens — the table the whole 07-Aug wedge formed around."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO fyers_tokens (id, last_attempt) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET last_attempt = EXCLUDED.last_attempt",
                    (_now(),))


# ---------------------------------------------------------------- 2FA chain

def get_auth_code():
    """Run the headless TOTP 2FA chain and return a fresh auth_code."""
    if not (TOTP_SECRET and PIN and SECRET):
        raise SystemExit("Auto-login missing env vars (FYERS_TOTP_SECRET / FYERS_PIN / FYERS_SECRET).")

    # 1. send_login_otp
    r = requests.post(URL_SEND_OTP, json={'fy_id': _b64(FY_ID), 'app_id': '2'}, timeout=10).json()
    if 'request_key' not in r:
        raise AuthStageError('send_login_otp', r)
    req_key = r['request_key']

    # 2. verify_otp — generate the code at a CLEAN window start so it can't
    #    roll over mid-request. Wait until we're early in a 30s slot.
    sec = datetime.now().second % 30
    if sec > 25 or sec < 1:
        time.sleep(31 - sec if sec > 25 else 1)
    otp = pyotp.TOTP(TOTP_SECRET).now()
    r = requests.post(URL_VERIFY_OTP, json={'request_key': req_key, 'otp': otp}, timeout=10).json()
    if 'request_key' not in r:
        raise AuthStageError('verify_otp', r)
    req_key2 = r['request_key']

    # 3. verify_pin — the stage that burned five attempts on 07-Aug. Its failure is now
    #    typed, so the ladder can tell a wrong PIN from a blocked account.
    r = requests.post(URL_VERIFY_PIN, json={
        'request_key': req_key2, 'identity_type': 'pin', 'identifier': _b64(PIN)
    }, timeout=10).json()
    access = r.get('data', {}).get('access_token')
    if not access:
        raise AuthStageError('verify_pin', r)

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
        raise AuthStageError('token', r)
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
        raise AuthStageError('validate_authcode', r)
    return r['access_token'], r.get('refresh_token')


def save_token(conn, access, refresh):
    now = _now()
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO fyers_tokens
                         (id, access_token, refresh_token, access_created, refresh_created, updated_at)
                       VALUES (1, %s, %s, %s, %s, NOW())
                       ON CONFLICT (id) DO UPDATE SET
                         access_token   = EXCLUDED.access_token,
                         refresh_token  = EXCLUDED.refresh_token,
                         access_created = EXCLUDED.access_created,
                         refresh_created= EXCLUDED.refresh_created,
                         updated_at     = NOW()""",
                    (access, refresh, now, now))
    if not conn.autocommit:
        conn.commit()


def auto_login(conn=None):
    """Full headless login -> store token -> return access token.

    cc#489 fix_1 (16-Jul incident): ALWAYS opens its own short-lived connection for the
    fyers_tokens writes and closes ONLY that one. The `conn` param is accepted for
    backward-compat call sites but is never touched — this function must NEVER be able to
    close a caller-supplied connection (root cause of the worker's global DB conn dying
    right after boot auth on 16-Jul).

    cc#883: no DDL here (see migrate_schema), autocommit so nothing is left open on
    fyers_tokens, and a persisted failure ladder in front of the 90-second spacing breaker."""
    try:
        own_conn = _auth_conn()
    except Exception as e:
        raise SystemExit(f"Auto-login SKIPPED — could not reach the database: {str(e)[:160]}")

    try:
        # --- gates, cheapest and strongest first ------------------------------------
        try:
            state = read_state(own_conn)
        except AuthDbWedge as e:
            _alert('feed_auth_db_wedge',
                   f"could not read fyers_auth_state within {AUTH_STATEMENT_TIMEOUT_MS}ms: "
                   f"{str(e)[:160]}. NOT counted as an attempt — nothing reached Fyers.",
                   {'blocking': _blocking_pids()})
            raise SystemExit("Auto-login SKIPPED — the auth state read timed out (DB wedge). "
                             "No attempt was made.")

        refusal = _gate(state)
        if refusal:
            log.warning(refusal)
            raise SystemExit(refusal)

        try:
            if _too_soon(own_conn):
                raise SystemExit(
                    f"Auto-login SKIPPED — another attempt < {ATTEMPT_COOLDOWN}s ago "
                    "(account-block protection). Wait, then retry.")
            _stamp_attempt(own_conn)   # record BEFORE trying, so a crash still counts
        except SystemExit:
            raise
        except Exception as e:
            if _is_db_wedge(e):
                # cc#883 item 4: a lock or timeout here never reached Fyers, so the ladder is
                # untouched and no attempt is spent. Name the blocker so the next incident
                # starts where 07-Aug ended.
                _alert('feed_auth_db_wedge',
                       f"fyers_tokens breaker read/stamp wedged: {str(e)[:160]}",
                       {'blocking': _blocking_pids()})
                raise SystemExit("Auto-login SKIPPED — fyers_tokens was locked (DB wedge). "
                                 "No attempt was made, no Fyers attempt spent.")
            raise

        # --- the actual chain --------------------------------------------------------
        try:
            auth_code = get_auth_code()
            access, refresh = exchange_auth_code(auth_code)
        except SystemExit:
            raise                      # missing env vars — not a broker failure
        except Exception as e:
            _record_failure(e, state)
            raise

        save_token(own_conn, access, refresh)
        _clear_ladder()                # a success resets every rung
        log.info("Auto-login OK - fresh token stored (valid for today)")
        return access
    finally:
        try: own_conn.close()
        except Exception: pass


def try_relogin(conn):
    """cc#473 item 3: breaker-SAFE wrapper for in-loop self-heal. NEVER raises
    SystemExit (which, uncaught inside the feed loop, turned the 90s cooldown into a
    Railway crash-loop on 13-Jul). Returns a dict:
      {'ok': bool, 'token': str|None, 'skipped': bool, 'error': str|None}
    'skipped'=True => an account-protection gate refused this attempt; the caller must
    BACK OFF and retry later, not crash.

    cc#883: 'skipped' now covers the whole ladder — the 90s spacing breaker, the
    escalating sleep, the day halt and the permanent block. All of them mean the same
    thing to the caller (we did not contact Fyers, do not treat it as a broker failure),
    so they are reported the same way."""
    try:
        tok = auto_login(conn)
        return {'ok': True, 'token': tok, 'skipped': False, 'error': None}
    except SystemExit as e:
        msg = str(e)
        low = msg.lower()
        skipped = any(w in low for w in
                      ('skipped', 'cooldown', 'block protection', 'sleeping',
                       'stopped for today', 'halted', 'db wedge'))
        return {'ok': False, 'token': None, 'skipped': skipped, 'error': msg}
    except Exception as e:
        return {'ok': False, 'token': None, 'skipped': False, 'error': str(e)}


if __name__ == '__main__':
    migrate_schema()
    auto_login()

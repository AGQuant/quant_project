"""
scorr_auth.py — password gate for all HTML pages (AUTH_HARDENING_V2, cc#709).

Password : env SCORR_AUTH_PASSWORD (set in Railway), falling back to _PASSWORD only if the
           env var is missing (a warning is logged on fallback).
Sessions : random per-login token (secrets.token_hex(32)) stored in auth_sessions
           (token PK, created_at, expires_at). _is_authed = token row exists AND not expired.
           /login inserts a 7-day token + lazily prunes expired rows; /logout DELETEs the row
           (real server-side revocation). Old static-hash cookies are invalid after deploy — one
           re-login (acceptable per spec).
Cookie   : scorr_auth (7-day, httponly, path=/, secure, samesite=none) — flags unchanged.
Login    : rate-limited to 5 failed attempts per IP per 10 min (in-memory, single process) -> 429.
Protected: gated by the PROTECTED set (extended in main.py); /authdebug REMOVED (was a live
           unauthenticated token leak = auth bypass).
"""

import logging
import os
import secrets
import time

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger("scorr.auth")
router = APIRouter()

COOKIE_NAME = "scorr_auth"
PROTECTED = {"/", "/dashboard", "/cio", "/cio2", "/ask", "/check", "/sector", "/scanners", "/fpc", "/news", "/holdings", "/filters"}

# Fallback password ONLY if SCORR_AUTH_PASSWORD env var is missing (warned). Do not commit a new value.
_PASSWORD = "556700"
DATABASE_URL = os.getenv("DATABASE_URL")

SESSION_DAYS = 7
RATE_MAX = 5                 # max failed logins ...
RATE_WINDOW = 10 * 60        # ... per IP per 10 minutes
_FAILED = {}                 # {ip: [unix_ts, ...]} — in-memory, single process (spec-approved)
_warned_fallback = False


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ensure_sessions():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL)""")
        conn.commit()


def _js_str(s: str) -> str:
    import json
    return json.dumps(s)


def _clean(s: str) -> str:
    if s is None:
        return ""
    for ch in ("​", "‌", "‍", "﻿", "\xa0"):
        s = s.replace(ch, "")
    return s.strip()


def _password() -> str:
    global _warned_fallback
    env = os.environ.get("SCORR_AUTH_PASSWORD")
    if env:
        return _clean(env)
    if not _warned_fallback:
        log.warning("SCORR_AUTH_PASSWORD not set — falling back to the in-repo _PASSWORD constant. "
                    "Set the env var in Railway (alphanumeric 12+).")
        _warned_fallback = True
    return _PASSWORD


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _FAILED.get(ip, []) if now - t < RATE_WINDOW]
    _FAILED[ip] = hits
    return len(hits) >= RATE_MAX


def _record_fail(ip: str):
    _FAILED.setdefault(ip, []).append(time.time())


def _clear_fails(ip: str):
    _FAILED.pop(ip, None)


def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token:
        return False
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM auth_sessions WHERE token=%s AND expires_at > now()", (token,))
            return cur.fetchone() is not None
    except Exception as e:
        # fail closed — a DB blip redirects to /login rather than granting access
        log.warning(f"auth session check failed: {e}")
        return False


def _new_session_token() -> str:
    token = secrets.token_hex(32)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions WHERE expires_at < now()")   # lazy cleanup
        cur.execute("INSERT INTO auth_sessions (token, expires_at) VALUES (%s, now() + %s::interval)",
                    (token, f"{SESSION_DAYS} days"))
        conn.commit()
    return token


def _revoke_token(token: str):
    if not token:
        return
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM auth_sessions WHERE token=%s", (token,))
            conn.commit()
    except Exception as e:
        log.warning(f"auth session revoke failed: {e}")


def _login_page(error: bool = False, rate_limited: bool = False) -> str:
    if rate_limited:
        err = ('<p style="color:#dd3a4a;font-size:13px;margin-bottom:16px;font-weight:600;">'
               "Too many attempts, wait 10 minutes.</p>")
    elif error:
        err = ('<p style="color:#dd3a4a;font-size:13px;margin-bottom:16px;font-weight:600;">'
               "Incorrect password. Try again.</p>")
    else:
        err = ""
    # cc#709: randomized field name per page load defeats browser autofill; POST reads the first
    # field whose name starts with 'pw_'. autocomplete='new-password' is the strongest no-autofill hint.
    field = "pw_" + secrets.token_hex(3)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>V8 Dashboard · Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0f1623;min-height:100vh;display:flex;align-items:center;
     justify-content:center;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;}}
.card{{background:#1c2536;border:1px solid #2a3548;border-radius:16px;
       padding:44px 48px;width:100%;max-width:380px;text-align:center;
       box-shadow:0 24px 64px rgba(0,0,0,0.5);}}
.logo{{font-size:26px;font-weight:900;letter-spacing:-.01em;color:#fff;margin-bottom:6px;}}
.logo span{{color:#b45309;}}
.sub{{font-size:10.5px;color:#5a6781;letter-spacing:.12em;text-transform:uppercase;margin-bottom:36px;}}
input[type=password]{{width:100%;padding:13px 16px;border-radius:10px;
  border:1.5px solid #2a3548;background:#0f1623;color:#fff;font-size:15px;
  outline:none;margin-bottom:12px;transition:border .18s;}}
input[type=password]:focus{{border-color:#b45309;}}
input[type=password]::placeholder{{color:#3d4f6b;}}
button{{width:100%;padding:13px;border-radius:10px;border:none;
  background:#b45309;color:#fff;font-size:15px;font-weight:700;
  cursor:pointer;transition:background .15s;letter-spacing:.03em;}}
button:hover{{background:#9a4507;}}
.foot{{font-size:10px;color:#3d4f6b;margin-top:28px;letter-spacing:.06em;}}
</style>
</head>
<body>
<div class="card">
  <div class="logo">V8 Dashboard</div>
  <div class="sub">Long-Short Futures Signals</div>
  {err}
  <form method="POST" action="/login" autocomplete="off">
    <input type="password" name="{field}" placeholder="Enter password" autofocus autocomplete="new-password"/>
    <button type="submit">Enter &rarr;</button>
  </form>
  <div class="foot">Authorised access only</div>
</div>
</body>
</html>"""


def _set_auth_cookie(response, token: str):
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DAYS * 24 * 3600, path="/",
        httponly=True, samesite="none", secure=True,
    )


@router.on_event("startup")
async def _startup():
    try:
        _ensure_sessions()
    except Exception as e:
        log.error(f"auth_sessions ensure on startup failed: {e}")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_get(request: Request):
    if _is_authed(request):
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(_login_page())


@router.post("/login", include_in_schema=False)
async def login_post(request: Request):
    ip = _client_ip(request)
    if _rate_limited(ip):
        return HTMLResponse(_login_page(rate_limited=True), status_code=429)
    form = await request.form()
    # cc#709: field name is randomized ('pw_' + hex) — read the first pw_* field.
    raw = ""
    for k, v in form.items():
        if k.startswith("pw_"):
            raw = str(v)
            break
    if not raw:
        raw = str(form.get("password", ""))   # defensive fallback
    password = _clean(raw)
    next_url = str(form.get("next", "/")) or "/"
    if password and password == _password():
        _clear_fails(ip)
        safe_next = next_url if next_url.startswith("/") else "/"
        token = _new_session_token()
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta http-equiv='refresh' content='0;url={safe_next}'>"
            "</head><body style='background:#0f1623'>"
            f"<script>window.location.replace({_js_str(safe_next)});</script>"
            "</body></html>"
        )
        response = HTMLResponse(html, status_code=200)
        _set_auth_cookie(response, token)
        return response
    _record_fail(ip)
    if _rate_limited(ip):
        return HTMLResponse(_login_page(rate_limited=True), status_code=429)
    return HTMLResponse(_login_page(error=True), status_code=401)


@router.get("/logout", include_in_schema=False)
async def logout(request: Request):
    _revoke_token(request.cookies.get(COOKIE_NAME, ""))
    response = RedirectResponse(url="/login", status_code=302)
    response.set_cookie(
        COOKIE_NAME, "",
        max_age=0, expires=0, path="/",
        httponly=True, samesite="none", secure=True,
    )
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

"""
scorr_auth.py — Simple password gate for all HTML pages.

Password stored in Railway env var: SCORR_PASSWORD
Cookie: scorr_auth (7-day, httponly, path=/, secure, samesite=lax)
Protected: /, /dashboard, /cio, /cio2, /ask, /check, /sector
Exempt: /api/*, /mcp, /oauth/*, /.well-known/*, /login, /logout, /status

Fixes (v2):
  - Cookie now secure=True (Railway serves HTTPS; non-secure cookies were
    being dropped on the POST /login -> 302 -> GET / redirect, so login
    appeared to "not work").
  - Password compare strips whitespace on BOTH sides (trailing newline/space
    in the Railway env value no longer locks you out).
  - Logout overwrites the cookie with an expired empty value (delete_cookie
    alone can fail to match when attributes differ) so it always clears.
"""

import os
import hashlib
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

COOKIE_NAME = "scorr_auth"
PROTECTED = {"/", "/dashboard", "/cio", "/cio2", "/ask", "/check", "/sector"}
_SALT = "scorr2026"


def _password() -> str:
    """Configured password, whitespace-stripped."""
    return os.getenv("SCORR_PASSWORD", "").strip()


def _expected_token() -> str:
    return hashlib.sha256(f"{_password()}:{_SALT}".encode()).hexdigest()


def _is_authed(request: Request) -> bool:
    """Return True if request has valid auth cookie (or no password is set)."""
    if not _password():
        return True  # no password configured = open access
    return request.cookies.get(COOKIE_NAME, "") == _expected_token()


def _login_page(error: bool = False) -> str:
    err = (
        '<p style="color:#dd3a4a;font-size:13px;margin-bottom:16px;font-weight:600;">'
        "Incorrect password. Try again.</p>"
        if error
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Scorr · Login</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:#0f1623;min-height:100vh;display:flex;align-items:center;
     justify-content:center;font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',sans-serif;}}
.card{{background:#1c2536;border:1px solid #2a3548;border-radius:16px;
       padding:44px 48px;width:100%;max-width:380px;text-align:center;
       box-shadow:0 24px 64px rgba(0,0,0,0.5);}}
.logo{{font-size:30px;font-weight:900;letter-spacing:-.02em;color:#fff;margin-bottom:4px;}}
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
  <div class="logo">SC<span>O</span>RR</div>
  <div class="sub">Learn More. Score More.</div>
  {err}
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Enter password" autofocus autocomplete="current-password"/>
    <button type="submit">Enter &rarr;</button>
  </form>
  <div class="foot">Your AI Chief Investment Officer</div>
</div>
</body>
</html>"""


def _set_auth_cookie(response):
    response.set_cookie(
        COOKIE_NAME, _expected_token(),
        max_age=7 * 24 * 3600, path="/",
        httponly=True, samesite="lax", secure=True,
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_get(request: Request):
    if _is_authed(request):
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(_login_page())


@router.post("/login", include_in_schema=False)
async def login_post(request: Request):
    form = await request.form()
    password = str(form.get("password", "")).strip()
    correct = _password()
    next_url = str(form.get("next", "/")) or "/"
    if not correct or password == correct:
        response = RedirectResponse(url=next_url, status_code=302)
        _set_auth_cookie(response)
        return response
    return HTMLResponse(_login_page(error=True), status_code=401)


@router.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    # Overwrite with an expired empty cookie (matches set attributes) so it
    # reliably clears even if delete_cookie's attribute match is imperfect.
    response.set_cookie(
        COOKIE_NAME, "",
        max_age=0, expires=0, path="/",
        httponly=True, samesite="lax", secure=True,
    )
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

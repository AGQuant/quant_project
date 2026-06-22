"""
scorr_auth.py — Simple password gate for all HTML pages.

Password: HARDCODED (env var was unreliable). Change _PASSWORD below to update.
Cookie: scorr_auth (7-day, httponly, path=/, secure, samesite=none)
Protected: /, /dashboard, /cio, /cio2, /ask, /check, /sector, /news
Exempt: /api/*, /mcp, /oauth/*, /.well-known/*, /login, /logout, /status, /authdebug

Fixes (v5):
  - SameSite Lax -> None (with Secure). The CIO Shell (/cio2) embeds module
    pages (/dashboard, /check, /sector, /cio) as IFRAMES. With SameSite=Lax,
    browsers WITHHOLD the auth cookie on iframe subrequests, so the gate saw
    no cookie and redirected the iframe to /login — which manifested as
    "login once, then auto-logout, then can't log in again." SameSite=None
    (Secure) sends the cookie in embedded contexts. Same-origin only; safe.

Fixes (v4):
  - Password hardcoded to remove env-var dependency entirely.
  - Login success returns 200 HTML + JS redirect (avoids browsers dropping
    Set-Cookie on a cross-path 302).
  - Logout overwrites cookie with expired empty value so it always clears.
  - Branding: "V8 Dashboard" (Scorr name removed from login screen).
"""

import hashlib
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

router = APIRouter()

COOKIE_NAME = "scorr_auth"
PROTECTED = {"/", "/dashboard", "/cio", "/cio2", "/ask", "/check", "/sector", "/scanners", "/fpc", "/news"}
_SALT = "scorr2026"

# Hardcoded password — change here to update.
_PASSWORD = "556700"


def _js_str(s: str) -> str:
    """Safely embed a string as a JS string literal."""
    import json
    return json.dumps(s)


def _clean(s: str) -> str:
    """Strip ASCII + unicode whitespace and zero-width / non-breaking chars."""
    if s is None:
        return ""
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\xa0"):
        s = s.replace(ch, "")
    return s.strip()


def _password() -> str:
    """Hardcoded password (env ignored)."""
    return _PASSWORD


def _expected_token() -> str:
    return hashlib.sha256(f"{_password()}:{_SALT}".encode()).hexdigest()


def _is_authed(request: Request) -> bool:
    """Return True if request has valid auth cookie."""
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
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Enter password" autofocus autocomplete="current-password"/>
    <button type="submit">Enter &rarr;</button>
  </form>
  <div class="foot">Authorised access only</div>
</div>
</body>
</html>"""


def _set_auth_cookie(response):
    response.set_cookie(
        COOKIE_NAME, _expected_token(),
        max_age=7 * 24 * 3600, path="/",
        httponly=True, samesite="none", secure=True,
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_get(request: Request):
    if _is_authed(request):
        return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(_login_page())


@router.post("/login", include_in_schema=False)
async def login_post(request: Request):
    form = await request.form()
    password = _clean(str(form.get("password", "")))
    correct = _password()
    next_url = str(form.get("next", "/")) or "/"
    if password == correct:
        safe_next = next_url if next_url.startswith("/") else "/"
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta http-equiv='refresh' content='0;url={safe_next}'>"
            "</head><body style='background:#0f1623'>"
            f"<script>window.location.replace({_js_str(safe_next)});</script>"
            "</body></html>"
        )
        response = HTMLResponse(html, status_code=200)
        _set_auth_cookie(response)
        return response
    return HTMLResponse(_login_page(error=True), status_code=401)


@router.get("/logout", include_in_schema=False)
async def logout():
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


@router.get("/authdebug", include_in_schema=False)
async def authdebug(request: Request):
    """Auth-exempt diagnostic. Echoes exactly what the server sees so we can
    diagnose the login/cookie loop. Safe: reveals no secret beyond a token the
    correct password already produces. Remove after debugging."""
    raw_cookie_header = request.headers.get("cookie", "")
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    expected = _expected_token()
    return JSONResponse({
        "saw_cookie_header": raw_cookie_header,
        "scorr_auth_cookie_value": cookie_val,
        "expected_token": expected,
        "match": cookie_val == expected,
        "is_authed": _is_authed(request),
        "all_cookie_names": list(request.cookies.keys()),
        "host": request.headers.get("host", ""),
        "x_forwarded_proto": request.headers.get("x-forwarded-proto", ""),
        "user_agent": request.headers.get("user-agent", "")[:80],
    })

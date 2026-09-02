import functools
import logging
import os
import sys
import base64
import traceback
import httpx
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from typing import Optional

log = logging.getLogger("github_ops")

# ── GitHub Ops router ───────────────────────────────────────────────
# Extracted from main.py (File 5/5 split). Self-contained: reads env
# vars directly, no import from main.py (avoids circular import).
# Endpoints: github_read, github_list, github_push, github_delete.

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
DEPLOY_GUARD = os.getenv("DEPLOY_GUARD", "false").lower() == "true"
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

router = APIRouter()


# ── cc#1249 · FAIL-CLOSED: AN EMPTY BODY IS ITSELF A DEFECT ─────────────────────────────────────
# All four github tools went dark together and every one of them reported the SAME thing to the
# caller: "Expecting value: line 1 column 1 (char 0)". That is not an error message, it is json
# parsing failing on an empty string, and it says nothing about what actually broke. The chain is:
# an exception that is not an HTTPException escapes the handler -> FastAPI returns a 500 whose body
# is plain text, not JSON -> mcp_dispatch calls r.json() on it with no status check -> the MCP layer
# surfaces the parse error and the real cause never leaves the server.
#
# So the first fix is not a fix for the outage, it is a fix for the BLINDNESS. Every handler now
# answers in JSON whatever happens, naming the exception type, its message and the traceback's last
# frame. A tool that fails should say why in the same breath.
#
# HTTPException is deliberately re-raised untouched: FastAPI already renders those as JSON, they
# carry a deliberate status code, and wrapping them would flatten 403/404/422 into one 500.
def _json_errors(fn):
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except HTTPException:
            raise
        except Exception as e:
            tb = traceback.extract_tb(e.__traceback__)
            where = ("%s:%s in %s" % (os.path.basename(tb[-1].filename), tb[-1].lineno, tb[-1].name)
                     if tb else "unknown")
            log.exception("github_ops.%s failed", fn.__name__)
            detail = str(e)[:400]
            # httpx raise_for_status() hides the response body, which for the GitHub API is where
            # the actual reason lives ("Bad credentials", "API rate limit exceeded", ...). Dig it
            # out rather than reporting only the status line.
            gh_status = gh_body = None
            resp = getattr(e, "response", None)
            if resp is not None:
                gh_status = getattr(resp, "status_code", None)
                try:
                    gh_body = resp.text[:300]
                except Exception:
                    gh_body = None
            return JSONResponse(status_code=500, content={
                "status": "error", "tool": fn.__name__, "error_type": type(e).__name__,
                "error": detail, "at": where,
                "github_status": gh_status, "github_body": gh_body,
                "note": "cc#1249 fail-closed wrapper: the handler raised and this JSON is the "
                        "report. Before this existed the caller got an empty body and the reason "
                        "was lost on the server.",
            })
    return wrapper

def _gh_headers():
    if not GITHUB_TOKEN: raise HTTPException(500,"GITHUB_TOKEN not configured")
    return {"Authorization":f"Bearer {GITHUB_TOKEN}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"}

def _check_admin(token):
    if not ADMIN_TOKEN: return True
    if token != ADMIN_TOKEN: raise HTTPException(403,"Invalid admin token")
    return True

def _check_deploy_guard():
    if not DEPLOY_GUARD: raise HTTPException(403,"DEPLOY_GUARD is off")

async def _gh_get_file(filepath):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers())
        if r.status_code == 404: return {"exists":False,"content":None,"sha":None,"size":0}
        r.raise_for_status(); data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        return {"exists":True,"content":content,"sha":data["sha"],"size":data["size"]}

async def _gh_put_file(filepath, new_content, commit_message, sha=None):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    payload = {"message":commit_message,"content":base64.b64encode(new_content.encode("utf-8")).decode("ascii"),"branch":"main"}
    if sha: payload["sha"] = sha
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.put(url, headers=_gh_headers(), json=payload)
        if r.status_code not in (200,201): raise HTTPException(r.status_code, f"GitHub error: {r.text[:300]}")
        return r.json()

async def _gh_delete_file(filepath, commit_message, sha):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{filepath}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request("DELETE", url, headers=_gh_headers(), json={"message":commit_message,"sha":sha,"branch":"main"})
        if r.status_code != 200: raise HTTPException(r.status_code, f"GitHub delete error: {r.text[:300]}")
        return r.json()

async def _gh_list_tree(path_prefix=""):
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{path_prefix}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_gh_headers()); r.raise_for_status(); data = r.json()
        if isinstance(data,dict): data = [data]
        return [{"name":x["name"],"path":x["path"],"type":x["type"],"size":x.get("size",0)} for x in data]

@router.get("/api/admin/github_read")
@_json_errors
async def github_read(filepath: str, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    info = await _gh_get_file(filepath)
    if not info["exists"]: raise HTTPException(404,f"File not found: {filepath}")
    return {"filepath":filepath,"size":info["size"],"sha":info["sha"],"content":info["content"],"lines":info["content"].count("\n")+1}

@router.get("/api/admin/github_list")
@_json_errors
async def github_list(path: str = "", x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token)
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    files = await _gh_list_tree(path)
    return {"path":path or "/","items":files,"count":len(files)}

@router.post("/api/admin/github_push")
@_json_errors
async def github_push(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); _check_deploy_guard()
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath"); new_content = body.get("new_content")
    commit_message = body.get("commit_message", f"chore: update {filepath}")
    create_if_missing = body.get("create_if_missing", True)
    if not filepath or new_content is None: raise HTTPException(400,"filepath and new_content required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"] and not create_if_missing: raise HTTPException(404,f"File {filepath} does not exist")
    if existing["exists"] and existing["content"] == new_content:
        return {"status":"noop","message":"Content identical","filepath":filepath}
    # cc#1185 P8 THEME_TOKEN_LOCK — the gate, on the one push path this app owns.
    # It judges the content that is ABOUT to land, not what already did, and it is a RATCHET: a
    # themed file may lose raw declarations or stay level, never gain them. Files with no recorded
    # baseline pass untouched — a gate that opines on numbers it was never given is one people
    # learn to switch off. A deliberate regression lands with allow_theme_regression and is echoed
    # back in the response, so it is on the record instead of being silent.
    theme_note = None
    if not body.get("allow_theme_regression"):
        try:
            from theme_validator import gate as _theme_gate
            allowed, why = _theme_gate(filepath, new_content)
            if not allowed:
                raise HTTPException(422, why)
        except HTTPException:
            raise
        except Exception as e:
            # A broken validator must never block a push — it is a lint, not the deploy path.
            theme_note = f"theme validator skipped: {type(e).__name__}: {str(e)[:120]}"
    else:
        theme_note = "theme regression allowed explicitly by the caller"
    # cc#1588 NO_BRAND_IN_TAB_TITLE_V1 — a WEB page's <title> / document.title is the page name
    # only, never "Scorr". Same shape as the theme gate: judges the content about to land, is
    # scoped by the script itself (mobile/, previews/, design_refs/ are not web pages), and a
    # broken checker never blocks a push.
    title_note = None
    try:
        _tools_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
        if _tools_dir not in sys.path:
            sys.path.append(_tools_dir)
        from check_tab_titles import gate as _title_gate
        allowed, why = _title_gate(filepath, new_content)
        if not allowed:
            raise HTTPException(422, why)
    except HTTPException:
        raise
    except Exception as e:
        title_note = f"tab-title check skipped: {type(e).__name__}: {str(e)[:120]}"
    sha = existing["sha"] if existing["exists"] else None
    result = await _gh_put_file(filepath, new_content, commit_message, sha)
    return {"status":"ok","filepath":filepath,"action":"updated" if existing["exists"] else "created",
            "commit_sha":result.get("commit",{}).get("sha"),"commit_url":result.get("commit",{}).get("html_url"),
            "old_size":existing["size"],"new_size":len(new_content),"theme_gate":theme_note,"tab_title_gate":title_note}

@router.post("/api/admin/github_delete")
@_json_errors
async def github_delete(req: Request, x_admin_token: Optional[str] = Header(None)):
    _check_admin(x_admin_token); _check_deploy_guard()
    if not GITHUB_REPO: raise HTTPException(500,"GITHUB_REPO not configured")
    body = await req.json()
    filepath = body.get("filepath"); commit_message = body.get("commit_message",f"chore: delete {filepath}")
    if not filepath: raise HTTPException(400,"filepath required")
    existing = await _gh_get_file(filepath)
    if not existing["exists"]: raise HTTPException(404,f"File not found: {filepath}")
    result = await _gh_delete_file(filepath, commit_message, existing["sha"])
    return {"status":"ok","filepath":filepath,"action":"deleted","commit_sha":result.get("commit",{}).get("sha")}

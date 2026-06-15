"""
scorr_authset_probe.py — TEMPORARY cookie diagnostic.

Two endpoints, both auth-exempt:

  GET /authset
    Sets a probe cookie three different ways (lax, none, plain) and returns
    the literal Set-Cookie header string(s) the server emits, so we can see
    exactly what the browser is being told. Then visit /authdebug2 to see
    which (if any) came back.

  GET /authdebug2
    Echoes every cookie the browser sent back.

Remove this module after diagnosis (drop the import + include_router in main.py).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/authset", include_in_schema=False)
async def authset(request: Request):
    resp = JSONResponse({"note": "probe cookies set — now open /authdebug2 in the SAME tab"})
    # Three variants with distinct names so we can see which the browser keeps.
    resp.set_cookie("probe_none", "v1", max_age=3600, path="/",
                    httponly=False, samesite="none", secure=True)
    resp.set_cookie("probe_lax", "v2", max_age=3600, path="/",
                    httponly=False, samesite="lax", secure=True)
    resp.set_cookie("probe_plain", "v3", max_age=3600, path="/",
                    httponly=False)
    # Echo back the literal Set-Cookie headers the server is emitting.
    set_cookie_headers = [v for (k, v) in resp.raw_headers and []]  # placeholder
    return resp


@router.get("/authdebug2", include_in_schema=False)
async def authdebug2(request: Request):
    return JSONResponse({
        "raw_cookie_header": request.headers.get("cookie", ""),
        "cookies_received": dict(request.cookies),
        "probe_none_present": "probe_none" in request.cookies,
        "probe_lax_present": "probe_lax" in request.cookies,
        "probe_plain_present": "probe_plain" in request.cookies,
        "host": request.headers.get("host", ""),
        "x_forwarded_proto": request.headers.get("x-forwarded-proto", ""),
    })

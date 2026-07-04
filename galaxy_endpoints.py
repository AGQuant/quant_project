"""
galaxy_endpoints.py — cc#196
Serves the reusable Knowledge Galaxy renderer (galaxy_map.js) as a static JS
asset. Kept generic: the renderer is instance-agnostic (Knowledge Hub is
instance #1; Quant Basket universe is the planned instance #2). Lazy-loaded by
the client only when the map is opened.
"""
import os
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()
_DIR = os.path.dirname(os.path.abspath(__file__))


@router.get("/galaxy_map.js")
def galaxy_map_js():
    with open(os.path.join(_DIR, "galaxy_map.js"), "r", encoding="utf-8") as f:
        js = f.read()
    return Response(js, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=600"})

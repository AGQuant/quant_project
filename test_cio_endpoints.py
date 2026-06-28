"""
test_cio_endpoints.py -- /test-cio AI CIO testbench (CC_TASK_104).
Mounted in main.py via: app.include_router(test_cio_router)

Side-by-side comparison of two AI CIO architectures over the SAME query:
  Tab 1  Gemini  -- 2-layer: Gemini classifies intent -> native DB query -> format
  Tab 2  Haiku   -- single layer: keyword-prefetched DB context -> Haiku answers

  GET  /test-cio          -- standalone testbench page (no auth, internal tool)
  POST /test-cio/gemini   -- {query} -> {response, time_ms, cost_usd, layer_used, ...}
  POST /test-cio/haiku    -- {query} -> {response, time_ms, cost_usd, tokens_used, ...}

Internal tool. New file only; main.py just include_router()s it.
"""
import os
import re
import json
import time
import asyncio
import httpx
import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

GEMINI_MODEL = "gemini-2.5-flash-lite"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

BASE_URL = os.getenv("RAILWAY_PUBLIC_DOMAIN", "quantproject-production.up.railway.app")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"

# Pricing -- USD per million tokens (from task #104 spec)
GEM_IN, GEM_OUT = 0.1, 0.4
HAI_IN, HAI_OUT = 1.0, 5.0

SYSTEM_PROMPT = ("You are Scorr AI CIO -- an institutional-grade investment research "
                 "assistant for Indian retail investors. Answer based on the data provided. "
                 "Be concise, precise, and analytical. Never fabricate data not in the context.")


# ---------------------------------------------------------------- DB helpers
def _conn():
    return psycopg.connect(DATABASE_URL)


def _q(sql, params=()):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fmt(rows, title):
    if not rows:
        return f"{title}: no matching data found."
    out = [title + ":"]
    for r in rows:
        out.append("  - " + ", ".join(f"{k}={v}" for k, v in r.items() if v is not None))
    return "\n".join(out)


# ---------------------------------------------------------------- native DB intent runners
def _db_gvm(symbol):
    rows = _q("""SELECT symbol, company_name, gvm_score, verdict, g_score, v_score, m_score, segment
                 FROM gvm_scores WHERE UPPER(symbol)=%s ORDER BY score_date DESC LIMIT 1""",
              (symbol.upper(),))
    return _fmt(rows, f"GVM score for {symbol.upper()}")


def _db_sector(segment):
    rows = _q("""SELECT symbol, company_name, gvm_score, verdict FROM gvm_scores
                 WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores) AND segment ILIKE %s
                 ORDER BY gvm_score DESC NULLS LAST LIMIT 10""", (f"%{segment}%",))
    return _fmt(rows, f"Top stocks in segment '{segment}'")


def _db_v8_signals():
    rows = _q("""SELECT q.symbol, q.basket, q.cmp, q.gvm_score, p.pp, p.r1, p.s1, q.signal_ts
                 FROM v8_qualified q
                 LEFT JOIN v8_paper_pivots p ON p.symbol=q.symbol
                   AND p.pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                 WHERE q.signal_date=CURRENT_DATE
                 ORDER BY q.gvm_score DESC NULLS LAST LIMIT 10""")
    return _fmt(rows, "V8 qualified signals today")


def _db_portfolio():
    rows = _q("""SELECT symbol, direction, entry_price, qty, trade_date, v8_basket
                 FROM personal_journal WHERE exit_price IS NULL
                 ORDER BY trade_date DESC LIMIT 20""")
    return _fmt(rows, "Open personal-journal positions")


async def _market_mood():
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE_URL}/api/v8/market_mood")
            return "Market mood:\n" + json.dumps(r.json(), default=str)[:1500]
    except Exception as e:
        return f"Market mood: unavailable ({e})"


# ---------------------------------------------------------------- LLM callers (httpx, async)
async def _gemini(system, user, json_mode=False):
    """Returns (text, prompt_tokens, output_tokens). Raises on hard failure."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 700},
    }
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(url, json=body)
        r.raise_for_status()
        d = r.json()
    text = ""
    cand = (d.get("candidates") or [])
    if cand:
        parts = (cand[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
    um = d.get("usageMetadata") or {}
    return text, um.get("promptTokenCount", 0) or 0, um.get("candidatesTokenCount", 0) or 0


async def _haiku(system, user):
    """Returns (text, input_tokens, output_tokens). Raises on hard failure."""
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": HAIKU_MODEL, "max_tokens": 700, "system": system,
                  "messages": [{"role": "user", "content": user}]},
        )
        r.raise_for_status()
        d = r.json()
    text = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    u = d.get("usage") or {}
    return text, u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0


# ---------------------------------------------------------------- GEMINI panel: intent -> native DB
_CLASSIFY_SYS = (
    "Classify the user's Indian-stock-market question. Return ONLY compact JSON, no prose:\n"
    '{"intent": one of '
    '["get_gvm","get_sector","get_v8_signals","get_market_mood","get_portfolio","fallback"], '
    '"symbol": NSE ticker in UPPERCASE or null, "segment": sector/industry name or null}\n'
    "Rules: a single company question -> get_gvm with its NSE symbol "
    "(e.g. 'HDFC Bank'->HDFCBANK, 'Reliance'->RELIANCE). "
    "A sector/industry question -> get_sector with the segment. "
    "Today's trades/signals -> get_v8_signals. Breadth/mood/strongest-overall -> get_market_mood. "
    "My holdings/portfolio/journal -> get_portfolio. Anything else -> fallback."
)


def _parse_intent(text):
    try:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


@router.post("/test-cio/gemini")
async def test_cio_gemini(req: Request):
    body = await req.json()
    query = (body.get("query") or "").strip()
    t0 = time.time()
    if not GEMINI_KEY:
        return JSONResponse({"response": "GEMINI_API_KEY not configured in Railway env.",
                             "time_ms": 0, "cost_usd": 0, "layer_used": "ERROR"})
    if not query:
        return JSONResponse({"response": "Empty query.", "time_ms": 0, "cost_usd": 0, "layer_used": "ERROR"})
    tin = tout = 0
    try:
        cls_text, pin, pout = await _gemini(_CLASSIFY_SYS, query, json_mode=True)
        tin += pin; tout += pout
        intent_obj = _parse_intent(cls_text)
        intent = intent_obj.get("intent", "fallback")
        symbol = intent_obj.get("symbol")
        segment = intent_obj.get("segment")

        layer = "NATIVE_DB"
        if intent == "get_gvm" and symbol:
            response = await asyncio.to_thread(_db_gvm, symbol)
        elif intent == "get_sector" and segment:
            response = await asyncio.to_thread(_db_sector, segment)
        elif intent == "get_v8_signals":
            response = await asyncio.to_thread(_db_v8_signals)
        elif intent == "get_portfolio":
            response = await asyncio.to_thread(_db_portfolio)
        elif intent == "get_market_mood":
            response = await _market_mood()
        else:
            # fallback -> let Gemini answer from its own knowledge
            response, fin, fout = await _gemini(SYSTEM_PROMPT, query)
            tin += fin; tout += fout
            layer = "GEMINI_LLM"

        cost = tin / 1e6 * GEM_IN + tout / 1e6 * GEM_OUT
        return JSONResponse({"response": response, "time_ms": int((time.time() - t0) * 1000),
                             "cost_usd": round(cost, 6), "layer_used": layer,
                             "intent": intent, "tokens_used": {"input": tin, "output": tout}})
    except httpx.HTTPStatusError as e:
        return JSONResponse({"response": f"Gemini API error {e.response.status_code}: {e.response.text[:300]}",
                             "time_ms": int((time.time() - t0) * 1000), "cost_usd": 0, "layer_used": "ERROR"})
    except Exception as e:
        return JSONResponse({"response": f"Error: {e}", "time_ms": int((time.time() - t0) * 1000),
                             "cost_usd": 0, "layer_used": "ERROR"})


# ---------------------------------------------------------------- HAIKU panel: keyword prefetch -> Haiku
def _haiku_context(query):
    """Keyword-driven DB prefetch -> plain-text context block for Haiku."""
    ql = query.lower()
    blocks = []
    # symbol / company detection -> GVM
    toks = [t for t in re.findall(r"[A-Za-z]{3,}", query)]
    stop = {"the", "what", "show", "today", "todays", "which", "score", "gvm", "stock", "stocks",
            "give", "tell", "about", "for", "and", "with", "sector", "strongest", "signals",
            "market", "mood", "bank"}
    cand = [t for t in toks if t.lower() not in stop]
    if cand:
        try:
            rows = _q("""SELECT symbol, company_name, gvm_score, verdict, g_score, v_score, m_score, segment
                         FROM gvm_scores
                         WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                           AND (UPPER(symbol)=ANY(%s) OR company_name ILIKE ANY(%s))
                         ORDER BY gvm_score DESC NULLS LAST LIMIT 5""",
                      ([t.upper() for t in cand], [f"%{t}%" for t in cand]))
            if rows:
                blocks.append(_fmt(rows, "GVM data"))
        except Exception:
            pass
    if any(k in ql for k in ("v8", "signal", "trade", "qualified", "today")):
        try:
            blocks.append(_db_v8_signals())
        except Exception:
            pass
    if any(k in ql for k in ("sector", "strongest", "rotation", "industry")):
        try:
            rows = _q("""SELECT segment, mcap_weighted_gvm, verdict FROM sector_ratings
                         WHERE score_date=(SELECT MAX(score_date) FROM sector_ratings)
                         ORDER BY mcap_weighted_gvm DESC NULLS LAST LIMIT 10""")
            if rows:
                blocks.append(_fmt(rows, "Sector ratings (mcap-weighted GVM)"))
        except Exception:
            pass
    return "\n\n".join(blocks) if blocks else "No structured data matched this query."


@router.post("/test-cio/haiku")
async def test_cio_haiku(req: Request):
    body = await req.json()
    query = (body.get("query") or "").strip()
    t0 = time.time()
    if not ANTHROPIC_KEY:
        return JSONResponse({"response": "ANTHROPIC_API_KEY not configured.", "time_ms": 0,
                             "cost_usd": 0, "layer_used": "ERROR"})
    if not query:
        return JSONResponse({"response": "Empty query.", "time_ms": 0, "cost_usd": 0, "layer_used": "ERROR"})
    try:
        context = await asyncio.to_thread(_haiku_context, query)
        user = f"DB CONTEXT:\n{context}\n\nUSER QUESTION: {query}"
        response, tin, tout = await _haiku(SYSTEM_PROMPT, user)
        cost = tin / 1e6 * HAI_IN + tout / 1e6 * HAI_OUT
        return JSONResponse({"response": response, "time_ms": int((time.time() - t0) * 1000),
                             "cost_usd": round(cost, 6), "layer_used": "HAIKU_LLM",
                             "tokens_used": {"input": tin, "output": tout}})
    except httpx.HTTPStatusError as e:
        return JSONResponse({"response": f"Anthropic API error {e.response.status_code}: {e.response.text[:300]}",
                             "time_ms": int((time.time() - t0) * 1000), "cost_usd": 0, "layer_used": "ERROR"})
    except Exception as e:
        return JSONResponse({"response": f"Error: {e}", "time_ms": int((time.time() - t0) * 1000),
                             "cost_usd": 0, "layer_used": "ERROR"})


# ---------------------------------------------------------------- testbench page
_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI CIO Testbench - Gemini vs Haiku</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;font-size:14px;padding:18px}
h1{font-size:17px;font-weight:700;margin-bottom:3px}
.sub{font-size:11px;color:#8b949e;margin-bottom:16px}
.bar{display:flex;gap:8px;margin-bottom:10px}
#q{flex:1;background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:11px 13px;font-size:14px;outline:none}
#q:focus{border-color:#2f81f7}
#send{background:#238636;border:none;border-radius:8px;color:#fff;font-weight:700;padding:0 20px;cursor:pointer;font-size:14px}
#send:disabled{opacity:.5;cursor:not-allowed}
.samples{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:18px}
.samp{background:#161b22;border:1px solid #30363d;border-radius:14px;color:#8b949e;font-size:11px;padding:5px 11px;cursor:pointer}
.samp:hover{border-color:#2f81f7;color:#e6edf3}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.cols{grid-template-columns:1fr}}
.panel{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px;min-height:160px}
.plabel{font-size:13px;font-weight:700;margin-bottom:2px}
.parch{font-size:10.5px;color:#8b949e;margin-bottom:10px}
.presp{white-space:pre-wrap;font-size:13px;line-height:1.5;color:#e6edf3;font-family:ui-monospace,Menlo,monospace;background:#0d1117;border:1px solid #21262d;border-radius:7px;padding:11px;min-height:70px;overflow-x:auto}
.meta{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:11px;color:#8b949e}
.meta b{color:#e6edf3;font-weight:700}
.tag{display:inline-block;font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:5px;background:#1f6feb33;color:#58a6ff}
.tag.native{background:#23863633;color:#3fb950}
.tag.err{background:#f8514933;color:#f85149}
.spin{color:#8b949e;font-style:italic}
</style>
</head>
<body>
<h1>AI CIO Testbench</h1>
<div class="sub">Same query, two architectures, side by side. Internal tool - no auth.</div>
<div class="bar">
  <input id="q" placeholder="Ask the AI CIO anything..." autocomplete="off">
  <button id="send" onclick="run()">Send</button>
</div>
<div class="samples" id="samples"></div>
<div class="cols">
  <div class="panel">
    <div class="plabel">Gemini</div>
    <div class="parch">2-layer: intent classify -&gt; native DB query -&gt; format</div>
    <div class="presp" id="g-resp">-</div>
    <div class="meta">
      <span>Time: <b id="g-time">-</b></span>
      <span>Cost: <b id="g-cost">-</b></span>
      <span>Layer: <span class="tag" id="g-layer">-</span></span>
    </div>
  </div>
  <div class="panel">
    <div class="plabel">Haiku</div>
    <div class="parch">Single layer: keyword DB prefetch -&gt; Haiku full round trip</div>
    <div class="presp" id="h-resp">-</div>
    <div class="meta">
      <span>Time: <b id="h-time">-</b></span>
      <span>Cost: <b id="h-cost">-</b></span>
      <span>Layer: <span class="tag" id="h-layer">-</span></span>
    </div>
  </div>
</div>
<script>
var SAMPLES = ["What is HDFC Bank GVM score?", "Show me todays V8 signals", "Which sector is strongest today?"];
var sd = document.getElementById('samples');
SAMPLES.forEach(function(s){
  var b = document.createElement('button'); b.className='samp'; b.textContent=s;
  b.onclick=function(){ document.getElementById('q').value=s; run(); };
  sd.appendChild(b);
});
function fmtCost(v){ return (v==null)?'-':('$'+Number(v).toFixed(6)); }
function setLayer(el, layer){
  el.textContent = layer || '-';
  el.className = 'tag' + (layer==='NATIVE_DB'?' native':(layer==='ERROR'?' err':''));
}
async function fire(side, url, query){
  var R=document.getElementById(side+'-resp'), T=document.getElementById(side+'-time'),
      C=document.getElementById(side+'-cost'), L=document.getElementById(side+'-layer');
  R.innerHTML='<span class="spin">thinking...</span>'; T.textContent='-'; C.textContent='-'; setLayer(L,'-');
  var t0=Date.now();
  try{
    var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:query})});
    var d=await r.json();
    R.textContent = d.response || '(no response)';
    T.textContent = (d.time_ms!=null?d.time_ms:(Date.now()-t0))+'ms';
    C.textContent = fmtCost(d.cost_usd);
    setLayer(L, d.layer_used);
  }catch(e){
    R.textContent='Request failed: '+e.message; setLayer(L,'ERROR');
    T.textContent=(Date.now()-t0)+'ms';
  }
}
function run(){
  var query=document.getElementById('q').value.trim();
  if(!query) return;
  var btn=document.getElementById('send'); btn.disabled=true;
  Promise.all([
    fire('g','/test-cio/gemini',query),
    fire('h','/test-cio/haiku',query)
  ]).finally(function(){ btn.disabled=false; });
}
document.getElementById('q').addEventListener('keydown',function(e){ if(e.key==='Enter') run(); });
</script>
</body>
</html>"""


@router.get("/test-cio", response_class=HTMLResponse)
def test_cio_page():
    return _PAGE

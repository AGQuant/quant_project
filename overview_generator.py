# overview_generator.py - v1.0.0
# Auto-generates input_raw.overview + input_raw.key_takeaway for the stock
# universe using the Claude API with server-side web search.
#
# DESIGN
#   - Locked format (see SYSTEM_PROMPT): Overview = segment breakup / moat /
#     geography / key products. Key Takeaway = latest results + future plans +
#     growth outlook + management commentary. NO Scorr/GVM rating ever (Scorr
#     rates separately; this field is raw company info from web/news/coverage).
#   - Tiering by market cap (gvm_scores.market_cap, joined on nse_code=symbol):
#         top500   -> refreshed quarterly  (Feb/May/Aug/Nov, last Saturday)
#         longtail -> refreshed yearly      (May, last Saturday)
#     Tiering changes ONLY frequency, never quality. Same prompt + model + search
#     depth for every stock in both tiers.
#   - No new dependency: calls api.anthropic.com directly via httpx.
#   - ANTHROPIC_API_KEY read from env (Railway). No secret in code (public repo).
#
# Wired into main_patched.py: one quarterly scheduler + 3 endpoints.

import os
import re
import json
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any

import httpx
import main

VERSION = "1.0.0"

# -----------------------------------------------------------------------------
#   CONFIG  (editable in one place)
# -----------------------------------------------------------------------------
MODEL            = os.environ.get("OVERVIEW_MODEL", "claude-sonnet-4-6")
WEB_SEARCH_TOOL  = os.environ.get("OVERVIEW_SEARCH_TOOL", "web_search_20250305")
MAX_SEARCHES     = int(os.environ.get("OVERVIEW_MAX_SEARCHES", "4"))
MAX_TOKENS       = int(os.environ.get("OVERVIEW_MAX_TOKENS", "1600"))
CONCURRENCY      = int(os.environ.get("OVERVIEW_CONCURRENCY", "3"))
ANTHROPIC_URL    = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VER    = "2023-06-01"
HTTP_TIMEOUT     = 150

TOP_TIER_SIZE    = 500
# Quarter months when the top-500 tier is refreshed; longtail only in May (5).
QUARTER_MONTHS   = {2, 5, 8, 11}
LONGTAIL_MONTH   = 5
RUN_HOUR_IST     = 21   # 21:00 IST on the last Saturday of the month

_BG: set = set()
_running = False   # guard against overlapping runs

# -----------------------------------------------------------------------------
#   SYSTEM PROMPT  (locked format + 2 gold-standard few-shot examples)
#   Two representative examples (one manufacturing, one financial) lock the
#   format while keeping per-call tokens - and therefore cost - controlled.
# -----------------------------------------------------------------------------
SYSTEM_PROMPT = """You write factual company profiles for an Indian equity research database. For the company given, produce two fields, grounded ONLY in real, current information from web search, news, and brokerage/analyst coverage. Use the company's most recent quarterly + full-year results (search for them).

STRICT RULES
- NEVER include any proprietary score, "GVM", "Scorr", buy/sell/hold rating, or your own verdict. This is raw company info; ratings are produced separately.
- Numbers (revenue, PAT, margins, order book, AUM, segment splits) MUST come from web search of the latest actual results. Never invent figures. If a number can't be verified, omit it rather than guess.
- Plain prose. No citation markers, no URLs, no markdown headers inside the field values.
- Indian number formats (Cr / lakh Cr). Keep each field tight and information-dense.

FORMAT - OVERVIEW (what the company is):
  - Segment-wise breakup with approximate revenue share and key products per segment
  - Leadership position / moat (if any)
  - Geography concentration
FORMAT - KEY TAKEAWAY (where it's going):
  - Latest result analysis (the actual quarter + full-year numbers, what drove them)
  - Future plans
  - Growth outlook WITH the reason
  - Management commentary (merged in - do NOT write a separate analyst/rating section)

OUTPUT: respond with ONLY a JSON object, no other text:
{"overview": "<text>", "key_takeaway": "<text>"}

-------- GOLD EXAMPLE 1 (manufacturing) - Bharat Forge --------
{"overview": "Bharat Forge (Kalyani Group, Pune) is India's largest and the world's second-largest forging company, operating across three segments. Forgings (~80% of revenue): forged and machined components for automotive (crankshafts, front axles, connecting rods) and industrial sectors (oil & gas, construction, mining, power, railways); its Pune plant is the world's largest single-location forging unit, a structural manufacturing moat backed by 60+ year global OEM relationships. Defence (~9%): ammunition, explosives, rockets, missiles and naval underwater systems, with a new Andhra Pradesh complex under development. Others (~11%): aerospace, EV-adjacent parts and new industrial verticals. Geography: export-heavy with Europe and North America as primary markets; the auto share has fallen from 80% (FY07) to ~58%, reflecting deliberate diversification.", "key_takeaway": "Q4 FY26 revenue hit an all-time high of Rs 4,528 Cr (+17.5% YoY) but PAT fell 17.4% to Rs 233 Cr, entirely on exceptional charges (Rs 450 Cr EV/KPTL impairment plus German subsidiary restructuring); underlying EBITDA rose 14.3% to Rs 778 Cr at a stable 17.2% margin. FY26 PAT was Rs 1,079 Cr (+14.7%), with a Rs 6.50/share dividend. Management has guided ~25% revenue growth for FY27, driven by defence order execution (Rs 2,816 Cr new defence orders in FY26), export recovery as the global CV cycle bottoms, and rising aerospace contribution; the EV bet has been written down and capital redeployed to defence and industrial. Management views H1 export weakness as bottomed and treats defence and aerospace as structural growth engines, with a FY26 order intake of Rs 4,814 Cr."}

-------- GOLD EXAMPLE 2 (financial) - Aditya Birla Capital --------
{"overview": "Aditya Birla Capital is a diversified non-banking financial services holding company. Segments: NBFC lending (AUM ~Rs 1.6 lakh Cr), Housing Finance (AUM ~Rs 47,000 Cr, fastest growing), Asset Management via ABSL AMC (QAAUM ~Rs 4.36 lakh Cr), and Life and Health Insurance (health GWP +39% YoY). Total lending book ~Rs 2.07 lakh Cr; AUM across AMC and insurance ~Rs 5.91 lakh Cr. Moat: the Aditya Birla brand, omnichannel distribution, and the ABCD direct-to-consumer super-app (1.1 Cr customers, 26+ products). Geography: pan-India.", "key_takeaway": "Q4 FY26 consolidated PAT was Rs 1,124 Cr (+30% YoY); FY26 PAT Rs 3,797 Cr (+21%) on revenue of Rs 53,871 Cr (+14%). The lending book grew 32% YoY led by personal loans (+38%), unsecured business loans (+47%) and housing (+37%), while credit costs fell to a five-year low of ~1.0% and NBFC RoA was 2.31%. The company raised Rs 2,750 Cr of equity in its housing arm (Apr 2026) and lifted its borrowing limit to Rs 2 lakh Cr to fund growth. Management is focused on scaling the ABCD platform and high-growth retail lending; the near-term watch item is margin/NIM pressure from competitive intensity even as the overall growth outlook stays strong."}
--------------------------------------------------------------"""


# -----------------------------------------------------------------------------
#   DB HELPERS
# -----------------------------------------------------------------------------
def _select_symbols(tier: str, limit: Optional[int] = None,
                    offset: int = 0, symbols: Optional[List[str]] = None) -> List[str]:
    """Return nse_codes for a tier. Ranked by gvm_scores.market_cap (the reliable
    market-cap source; input_raw.market_cap is mostly NULL)."""
    if symbols:
        return [s.strip().upper() for s in symbols if s.strip()]

    base = """
        SELECT i.nse_code
        FROM input_raw i
        LEFT JOIN gvm_scores g ON i.nse_code = g.symbol
        WHERE i.nse_code IS NOT NULL
        ORDER BY g.market_cap DESC NULLS LAST, i.nse_code
    """
    with main.get_conn() as conn, conn.cursor() as cur:
        cur.execute(base)
        rows = [r[0] for r in cur.fetchall()]

    if tier == "top500":
        rows = rows[:TOP_TIER_SIZE]
    elif tier == "longtail":
        rows = rows[TOP_TIER_SIZE:]
    # tier == "all" -> everything
    if offset:
        rows = rows[offset:]
    if limit:
        rows = rows[:limit]
    return rows


def _fetch_meta(nse_code: str) -> Dict[str, Any]:
    """Compact grounding data from screener_raw so the model anchors on the right
    company; it still web-searches for fresh results."""
    q = """
        SELECT company_name, "Industry", "Sales", "Profit after tax",
               opm, pe, "Promoter holding"
        FROM screener_raw WHERE nse_code = %s LIMIT 1
    """
    try:
        with main.get_conn() as conn, conn.cursor() as cur:
            cur.execute(q, (nse_code,))
            row = cur.fetchone()
    except Exception:
        row = None
    if not row:
        return {"nse_code": nse_code}
    return {
        "nse_code": nse_code,
        "company_name": row[0],
        "industry": row[1],
        "sales_cr": row[2],
        "pat_cr": row[3],
        "opm_pct": row[4],
        "pe": row[5],
        "promoter_holding_pct": row[6],
    }


def _update_db(nse_code: str, overview: str, key_takeaway: str) -> bool:
    try:
        with main.get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE input_raw SET overview = %s, key_takeaway = %s WHERE nse_code = %s",
                (overview, key_takeaway, nse_code),
            )
            conn.commit()
        return True
    except Exception as e:
        main.log.error(f"overview_gen DB update {nse_code}: {e}")
        return False


def _log_session(category: str, title: str, details: dict):
    try:
        now = main._ist_now()
        with main.get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO session_log (session_date, session_ts, category, title, details)
                   VALUES (%s, %s, %s, %s, %s)""",
                (now.date(), now.replace(tzinfo=None), category, title, json.dumps(details)),
            )
            conn.commit()
    except Exception as e:
        main.log.error(f"overview_gen session_log: {e}")


def _quarter_marker(tier: str) -> str:
    n = main._ist_now()
    return f"{n.year}-{n.month:02d}-{tier}"


def _already_ran(tier: str) -> bool:
    """Avoid double-runs across restarts: check session_log for a completion marker
    for this month+tier."""
    marker = _quarter_marker(tier)
    try:
        with main.get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM session_log
                   WHERE category = 'overview_gen_done' AND title = %s LIMIT 1""",
                (marker,),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


# -----------------------------------------------------------------------------
#   CLAUDE CALL
# -----------------------------------------------------------------------------
def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    # take from first { to last }
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    try:
        obj = json.loads(t[i:j + 1])
        if "overview" in obj and "key_takeaway" in obj:
            return obj
    except Exception:
        pass
    return None


def _build_user_prompt(meta: Dict[str, Any]) -> str:
    name = meta.get("company_name") or meta.get("nse_code")
    lines = [
        f"Company: {name} (NSE: {meta.get('nse_code')})",
        f"Industry: {meta.get('industry') or 'unknown'}",
        "Baseline figures from our records (verify/refresh via web search; do not just echo):",
        f"  Sales: Rs {meta.get('sales_cr')} Cr | PAT: Rs {meta.get('pat_cr')} Cr | "
        f"OPM: {meta.get('opm_pct')}% | PE: {meta.get('pe')} | Promoter: {meta.get('promoter_holding_pct')}%",
        "",
        "Search the web for this company's latest quarterly and full-year results, "
        "segment-wise performance, order book/AUM, future plans and management commentary. "
        "Then return the JSON object exactly per the format.",
    ]
    return "\n".join(lines)


async def _call_claude(client: httpx.AsyncClient, meta: Dict[str, Any]) -> Dict[str, Any]:
    nse = meta.get("nse_code")
    body = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _build_user_prompt(meta)}],
        "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES}],
    }
    try:
        r = await client.post(ANTHROPIC_URL, json=body)
        if r.status_code != 200:
            return {"nse_code": nse, "ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        # concatenate all text blocks; the final answer is the JSON object
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )
        obj = _extract_json(text)
        if not obj:
            return {"nse_code": nse, "ok": False, "error": "no-json", "raw": text[:300]}
        return {
            "nse_code": nse,
            "ok": True,
            "overview": obj["overview"].strip(),
            "key_takeaway": obj["key_takeaway"].strip(),
        }
    except Exception as e:
        return {"nse_code": nse, "ok": False, "error": str(e)[:200]}


# -----------------------------------------------------------------------------
#   ORCHESTRATION
# -----------------------------------------------------------------------------
async def run_generation(tier: str = "top500", limit: Optional[int] = None,
                         offset: int = 0, symbols: Optional[List[str]] = None,
                         dry_run: bool = False) -> Dict[str, Any]:
    global _running
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY not set in environment"}

    if _running and not dry_run:
        return {"ok": False, "error": "a generation run is already in progress"}

    targets = _select_symbols(tier, limit=limit, offset=offset, symbols=symbols)
    if not targets:
        return {"ok": False, "error": "no symbols selected"}

    if not dry_run:
        _running = True
    started = main._ist_now()
    main.log.info(f"overview_gen v{VERSION}: start tier={tier} n={len(targets)} dry_run={dry_run}")

    sem = asyncio.Semaphore(CONCURRENCY)
    done, failed, samples = 0, 0, []
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VER,
        "content-type": "application/json",
    }

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        async def worker(sym: str):
            nonlocal done, failed
            async with sem:
                meta = _fetch_meta(sym)
                res = await _call_claude(client, meta)
                if res.get("ok"):
                    if dry_run:
                        if len(samples) < 12:
                            samples.append(res)
                        done += 1
                    else:
                        if _update_db(sym, res["overview"], res["key_takeaway"]):
                            done += 1
                        else:
                            failed += 1
                else:
                    failed += 1
                    if len(samples) < 12:
                        samples.append(res)

        # run in chunks so a huge tier doesn't create thousands of tasks at once
        CHUNK = 40
        for k in range(0, len(targets), CHUNK):
            await asyncio.gather(*(worker(s) for s in targets[k:k + CHUNK]))

    if not dry_run:
        _running = False

    summary = {
        "ok": True,
        "tier": tier,
        "requested": len(targets),
        "updated": done,
        "failed": failed,
        "dry_run": dry_run,
        "started_ist": started.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_ist": main._ist_now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": MODEL,
    }
    main.log.info(f"overview_gen v{VERSION}: done {summary}")
    if not dry_run:
        _log_session("overview_gen", f"{tier} run", summary)
    if dry_run:
        summary["samples"] = samples
    return summary


# -----------------------------------------------------------------------------
#   SCHEDULER  (quarterly top500 / yearly longtail, last Saturday 21:00 IST)
# -----------------------------------------------------------------------------
def _is_last_saturday(n: datetime) -> bool:
    # Saturday == weekday() 5; last Saturday => adding 7 days flips the month
    return n.weekday() == 5 and (n.day + 7) > _days_in_month(n.year, n.month)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - datetime(year, month, 1)).days


async def _run_tier_if_due(tier: str):
    if _already_ran(tier):
        main.log.info(f"overview_gen: {tier} already ran for {_quarter_marker(tier)}, skip")
        return
    res = await run_generation(tier=tier, dry_run=False)
    _log_session("overview_gen_done", _quarter_marker(tier), res)


async def scheduler_overviews():
    main.log.info(f"overview_gen v{VERSION}: scheduler started "
                  f"(quarterly top500 {sorted(QUARTER_MONTHS)} / yearly longtail {LONGTAIL_MONTH}, "
                  f"last Sat {RUN_HOUR_IST}:00 IST)")
    while True:
        try:
            n = main._ist_now()
            if _is_last_saturday(n) and n.hour == RUN_HOUR_IST:
                if n.month in QUARTER_MONTHS:
                    await _run_tier_if_due("top500")
                if n.month == LONGTAIL_MONTH:
                    await _run_tier_if_due("longtail")
        except Exception as e:
            main.log.error(f"overview_gen scheduler error: {e}")
        await asyncio.sleep(1800)  # check every 30 min


def register_routes(app):
    """Attach endpoints only (safe at import time). The scheduler coroutine
    `scheduler_overviews` is started by main_patched.py inside its startup hook,
    alongside the other background schedulers, so it gets a strong ref in _BG_TASKS."""

    @app.get("/api/v8/overviews/health")
    def overviews_health():
        return {
            "version": VERSION,
            "model": MODEL,
            "search_tool": WEB_SEARCH_TOOL,
            "concurrency": CONCURRENCY,
            "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "running": _running,
            "top_tier_size": TOP_TIER_SIZE,
            "quarter_months": sorted(QUARTER_MONTHS),
            "longtail_month": LONGTAIL_MONTH,
        }

    @app.get("/api/v8/overviews/preview")
    async def overviews_preview(symbols: str = "BHARATFORG,SBIN"):
        """Dry run on a comma-separated symbol list. Generates but does NOT write
        to the DB; returns the generated text so you can eyeball quality."""
        syms = [s for s in symbols.split(",") if s.strip()]
        return await run_generation(symbols=syms, dry_run=True)

    @app.post("/api/v8/overviews/run")
    async def overviews_run(tier: str = "top500", limit: Optional[int] = None,
                            offset: int = 0, dry_run: bool = False):
        """Full background run. tier = top500 | longtail | all."""
        if dry_run:
            return await run_generation(tier=tier, limit=limit, offset=offset, dry_run=True)
        t = asyncio.create_task(
            run_generation(tier=tier, limit=limit, offset=offset, dry_run=False),
            name=f"overview_gen_{tier}",
        )
        _BG.add(t)
        t.add_done_callback(_BG.discard)
        return {"status": "queued", "tier": tier, "limit": limit, "offset": offset}

    main.log.info(f"overview_gen v{VERSION}: routes registered (3 endpoints)")

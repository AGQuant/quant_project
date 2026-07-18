"""
ops_metrics_pipeline.py -- cc#523 OPS METRICS V2 (Tijori-grade).

Per-sector operational KPI registry + quarterly extraction pipeline (investor
presentation + earnings-call transcript, dual-source cross-checked) + a
permanent concall-summary archive. See cc_tasks id=523 for the full founder
spec (three revisions: original 3-part spec -> screener-discovery addendum ->
final REVISION scope).

HONESTY NOTES (read before trusting any output of this pipeline):
  1. The founder's spec assumed `sector_ops_metrics` already existed with a
     working dual-source/confidence/discrepancy schema ("KEPT -- already
     right") and that 12 other sectors already had "current metric names" to
     carry over. Neither is true: grep of the whole repo turned up ZERO
     CREATE TABLE / writer for sector_ops_metrics before this file, and zero
     existing per-sector metric names anywhere. This file builds the schema
     and the 22-sector taxonomy fresh -- the 10 founder-named sectors use his
     exact metric lists; the other 12 are MY OWN rule-based grouping of the
     129 gvm_scores.segment values (see _infer_sector) with a minimal 3-metric
     generic placeholder set, since there was nothing real to "carry over".
     Flag this taxonomy for founder review/rename.
  2. The screener.in Documents/Concalls scraper (_discover_docs) is written
     against my general knowledge of screener.in's page layout, but this
     sandbox's network policy blocks screener.in outright (confirmed via
     curl and WebFetch, both 403 "policy denial" -- same class of block as
     nseindia.com/scorr.in found earlier this session) -- so it could NOT be
     verified against the live page from here. It uses a loose, text/keyword
     based match (not brittle exact class-name selectors) specifically to
     tolerate markup drift, but its first live run on Railway needs watching
     (ops_log category='ops_metrics_pull' records exactly what it finds/misses
     per company).
  3. Because of (2), NONE of this pipeline's actual extraction has run yet in
     this session -- the tables are empty until the arm flag fires on Railway
     (which has real network access, unlike this sandbox). Acceptance items
     that require real extracted data (WIPRO no-data gone, source_quote +
     confidence populated, 8-quarter trend rendering) cannot be verified here;
     they need a live run + founder spot-check after deploy.

STORAGE GUARD (spec, 18-Jul-2026): raw PDFs/transcripts are fetched into
memory, mined for text, and DISCARDED -- never written to Postgres. Only
extracted numeric rows + short (<=300 char) source quotes are persisted.
"""
import os
import re
import io
import json
import time
import logging
import statistics
from datetime import datetime, date, timedelta, timezone

import requests
import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException

from fundamentals_scraper import _fetch, _fetch_soup, UA, THROTTLE, BASE as SCREENER_BASE

log = logging.getLogger("scorr.ops_metrics")
router = APIRouter(tags=["ops_metrics"])

_DB = os.getenv("DATABASE_URL", "")
_IST = timezone(timedelta(hours=5, minutes=30))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

import psycopg


def _conn():
    return psycopg.connect(_DB)


def _oplog(cur, title, details, category="ops_metrics_pull"):
    try:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)",
                    (category, title, json.dumps(details, default=str)))
    except Exception as e:
        log.warning(f"oplog {title}: {e}")


def _check_admin(token):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


def _cfg_get(cur, key):
    cur.execute("SELECT value FROM app_config WHERE key=%s", (key,))
    r = cur.fetchone()
    return r[0] if r else None


def _cfg_set(cur, key, value):
    cur.execute("INSERT INTO app_config (key, value, updated_at) VALUES (%s,%s,NOW()) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()", (key, value))


# ── 1. SCHEMA ────────────────────────────────────────────────────────────────────

def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS sector_kpi_registry (
        id SERIAL PRIMARY KEY, sector TEXT NOT NULL, metric_name TEXT NOT NULL,
        display_name TEXT NOT NULL, unit TEXT,
        direction TEXT NOT NULL DEFAULT 'higher_better',   -- 'higher_better' | 'lower_better'
        tier TEXT NOT NULL DEFAULT 'core',                 -- 'core' | 'extended'
        UNIQUE(sector, metric_name))""")

    cur.execute("""CREATE TABLE IF NOT EXISTS sector_ops_metrics (
        id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, sector TEXT,
        metric_name TEXT NOT NULL, metric_value NUMERIC, unit TEXT, quarter TEXT NOT NULL,
        confidence TEXT,           -- 'high' | 'medium' | 'low' | NULL (no data either pass)
        source_1 TEXT,             -- presentation quote, <=300 chars
        source_1_value NUMERIC,
        source_2 TEXT,             -- transcript quote, <=300 chars
        source_2_value NUMERIC,
        discrepancy_pct NUMERIC,   -- |v1-v2| / max(|v1|,|v2|) * 100, both-present only
        notes TEXT,
        computed_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(symbol, metric_name, quarter))""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_som_symbol_q ON sector_ops_metrics(symbol, quarter)")

    # cc#523 REVISION point 2: latest-only, one row per (symbol, doc_type), overwritten in place.
    cur.execute("""CREATE TABLE IF NOT EXISTS doc_registry (
        symbol TEXT NOT NULL, doc_type TEXT NOT NULL,   -- 'presentation'|'transcript'|'press_release'
        quarter TEXT, url TEXT, source TEXT DEFAULT 'screener',
        discovered_at TIMESTAMPTZ DEFAULT NOW(), extracted_at TIMESTAMPTZ,
        extract_status TEXT DEFAULT 'pending',    -- 'pending'|'ok'|'failed'|'absent'
        PRIMARY KEY(symbol, doc_type))""")

    # cc#523 REVISION point 3: PERMANENT -- quarters accumulate, never purged.
    cur.execute("""CREATE TABLE IF NOT EXISTS concall_summaries (
        symbol TEXT NOT NULL, quarter TEXT NOT NULL, summary TEXT, key_metrics JSONB,
        guidance TEXT, tone TEXT, source_docs TEXT[], computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(symbol, quarter))""")


# ── 2. SECTOR TAXONOMY (see HONESTY NOTE 1) ─────────────────────────────────────

# The founder's 10 explicitly-specced sectors, exact metric lists from the cc#523 spec.
SECTOR_REGISTRY_SEED = {
    "IT": [
        ("cc_rev_growth", "CC Revenue Growth", "%", "higher_better", "core"),
        ("ebit_margin", "EBIT Margin", "%", "higher_better", "core"),
        ("attrition_pct", "Attrition", "%", "lower_better", "core"),
        ("headcount_net_adds", "Headcount Net Adds", "count", "higher_better", "core"),
        ("utilization_pct", "Utilization", "%", "higher_better", "extended"),
        ("tcv_deal_wins", "TCV Deal Wins", "$mn", "higher_better", "extended"),
    ],
    "Banking_NBFC": [
        ("nim", "Net Interest Margin", "%", "higher_better", "core"),
        ("gnpa_pct", "Gross NPA", "%", "lower_better", "core"),
        ("nnpa_pct", "Net NPA", "%", "lower_better", "core"),
        ("casa_pct", "CASA Ratio", "%", "higher_better", "core"),
        ("credit_growth", "Credit Growth", "%", "higher_better", "core"),
        ("deposit_growth", "Deposit Growth", "%", "higher_better", "extended"),
        ("credit_cost", "Credit Cost", "%", "lower_better", "extended"),
        ("pcr", "Provision Coverage Ratio", "%", "higher_better", "extended"),
        ("roa", "Return on Assets", "%", "higher_better", "core"),
    ],
    "Auto": [
        ("volumes_units_by_segment", "Volumes", "units", "higher_better", "core"),
        ("realization_per_unit", "Realization/Unit", "₹", "higher_better", "core"),
        ("ebitda_per_vehicle", "EBITDA/Vehicle", "₹", "higher_better", "core"),
        ("ev_mix_pct", "EV Mix", "%", "higher_better", "extended"),
    ],
    "Cement": [
        ("volumes_mn_t", "Volumes", "mn t", "higher_better", "core"),
        ("realization_per_t", "Realization/Tonne", "₹/t", "higher_better", "core"),
        ("ebitda_per_t", "EBITDA/Tonne", "₹/t", "higher_better", "core"),
        ("capacity_util_pct", "Capacity Utilization", "%", "higher_better", "core"),
        ("power_fuel_cost_per_t", "Power & Fuel Cost/Tonne", "₹/t", "lower_better", "extended"),
    ],
    "Hospitals": [
        ("occupancy_pct", "Occupancy", "%", "higher_better", "core"),
        ("arpob", "ARPOB", "₹", "higher_better", "core"),
        ("alos_days", "ALOS", "days", "lower_better", "extended"),
        ("bed_count_adds", "Bed Count Adds", "count", "higher_better", "extended"),
        ("payor_mix", "Payor Mix", "%", "higher_better", "extended"),
    ],
    "Telecom": [
        ("arpu", "ARPU", "₹", "higher_better", "core"),
        ("subscribers_net_adds", "Subscriber Net Adds", "count", "higher_better", "core"),
        ("data_per_sub_gb", "Data/Sub", "GB", "higher_better", "extended"),
        ("churn_pct", "Churn", "%", "lower_better", "core"),
    ],
    "Hotels": [
        ("occupancy_pct", "Occupancy", "%", "higher_better", "core"),
        ("arr", "ARR", "₹", "higher_better", "core"),
        ("revpar", "RevPAR", "₹", "higher_better", "core"),
        ("rooms_pipeline", "Rooms Pipeline", "count", "higher_better", "extended"),
    ],
    "Retail_QSR": [
        ("sssg_pct", "SSSG", "%", "higher_better", "core"),
        ("store_adds", "Store Adds", "count", "higher_better", "core"),
        ("revenue_per_sqft", "Revenue/Sqft", "₹", "higher_better", "extended"),
        ("gross_margin_pct", "Gross Margin", "%", "higher_better", "core"),
    ],
    "Insurance": [
        ("ape_growth", "APE Growth", "%", "higher_better", "core"),
        ("vnb_margin", "VNB Margin", "%", "higher_better", "core"),
        ("persistency_13m", "13M Persistency", "%", "higher_better", "extended"),
        ("persistency_61m", "61M Persistency", "%", "higher_better", "extended"),
        ("solvency_ratio", "Solvency Ratio", "%", "higher_better", "core"),
    ],
    "Metals_Steel": [
        ("volumes_mn_t", "Volumes", "mn t", "higher_better", "core"),
        ("realization_per_t", "Realization/Tonne", "₹/t", "higher_better", "core"),
        ("ebitda_per_t", "EBITDA/Tonne", "₹/t", "higher_better", "core"),
        ("net_debt_ebitda", "Net Debt/EBITDA", "x", "lower_better", "core"),
        ("capacity_util_pct", "Capacity Utilization", "%", "higher_better", "extended"),
    ],
}

# cc#523 spec: "remaining 12 existing sectors: carry over current metric names into the
# registry (extend later)" -- there ARE no current metric names anywhere in this codebase
# (see HONESTY NOTE 1), so these 12 get a minimal generic core set, same shape as the
# thin "IT has only 3 generic metrics" state the spec itself describes as the problem.
GENERIC_CORE_METRICS = [
    ("revenue_growth_yoy", "Revenue Growth YoY", "%", "higher_better", "core"),
    ("ebitda_margin_pct", "EBITDA Margin", "%", "higher_better", "core"),
    ("pat_growth_yoy", "PAT Growth YoY", "%", "higher_better", "core"),
]
OTHER_SECTORS = [
    "Pharma", "Chemicals", "FMCG", "Realty", "Power_Energy",
    "Capital_Goods_Engineering", "Textiles_Apparel", "Consumer_Durables_Electronics",
    "Paper_Packaging", "Financial_Services_Markets", "Media_Digital_Services", "Diversified_Others",
]
ALL_SECTORS = list(SECTOR_REGISTRY_SEED.keys()) + OTHER_SECTORS   # 22 total


def _infer_sector(segment):
    """Rule-based map from the 129 gvm_scores.segment values -> one of the 22 coarse
    sectors above. Keyword/prefix matching (not a static per-segment dict) so any segment
    added later still lands somewhere sensible instead of being silently dropped -- worst
    case it falls through to the Diversified_Others catch-all. See HONESTY NOTE 1: this
    taxonomy is my construction, not a pre-existing founder-approved mapping."""
    if not segment:
        return "Diversified_Others"
    s = segment
    if s.startswith("IT - "):
        return "IT"
    if any(k in s for k in ("Private Banks", "PSU Bank", "Small Finance Bank", "NBFC",
                             "Housing Finance", "MSME Finance", "Microfinance")):
        return "Banking_NBFC"
    if s.startswith("Auto") or s in ("Tyres", "Bearings & Abrasives", "Castings & Forgings"):
        return "Auto"
    if s.startswith("Cement"):
        return "Cement"
    if s.startswith("Hospitals") or "Diagnostics" in s:
        return "Hospitals"
    if "Telecom" in s:
        return "Telecom"
    if s.startswith("Hotels"):
        return "Hotels"
    if s.startswith("Retail") or "QSR" in s or "Restaurant" in s or s == "Consumer Goods Trading":
        return "Retail_QSR"
    if "Insurance" in s:
        return "Insurance"
    if "Steel" in s or s in ("Aluminium & Non Ferrous", "Mining"):
        return "Metals_Steel"
    if s.startswith("Pharma") or "CDMO" in s:
        return "Pharma"
    if any(k in s for k in ("Chemical", "Fertilizers", "Petrochemicals", "Adhesives",
                             "Paints", "Fluorochemicals")):
        return "Chemicals"
    if s.startswith("FMCG") or any(k in s for k in ("Packaged Foods", "Beverages & Spirits",
                                                      "Edible Oil", "Sugar & Agri")):
        return "FMCG"
    if s.startswith("Realty") or s == "REITs" or "Infrastructure" in s:
        return "Realty"
    if any(k in s for k in ("Power", "Renewable Energy", "Solar", "City Gas",
                             "Oil Services", "Refineries")):
        return "Power_Energy"
    if any(k in s for k in ("Capital Goods", "Engineering", "Electrical", "Electronics",
                             "Pumps", "Defence")):
        return "Capital_Goods_Engineering"
    if s.startswith("Textiles") or any(k in s for k in ("Garments", "Footwear",
                                                          "Synthetic Fibres", "Home Textiles")):
        return "Textiles_Apparel"
    if s.startswith("Consumer Durables") or s == "Consumer Plastics & Others":
        return "Consumer_Durables_Electronics"
    if any(k in s for k in ("Paper", "Packaging", "Building Materials", "Pipes & Tubes")):
        return "Paper_Packaging"
    if any(k in s for k in ("Broking", "Capital Markets", "Exchanges")):
        return "Financial_Services_Markets"
    if any(k in s for k in ("Broadcasting", "Entertainment", "Print Media",
                             "Digital Aggregators", "Internet & Digital")):
        return "Media_Digital_Services"
    return "Diversified_Others"


def seed_registry(conn=None):
    """Idempotent upsert of the 22-sector registry. Also logs the live segment->sector
    distribution so the founder can audit _infer_sector's calls (HONESTY NOTE 1)."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            n = 0
            for sector, rows in SECTOR_REGISTRY_SEED.items():
                for metric_name, display_name, unit, direction, tier in rows:
                    cur.execute("""INSERT INTO sector_kpi_registry
                                   (sector, metric_name, display_name, unit, direction, tier)
                                   VALUES (%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (sector, metric_name) DO UPDATE SET
                                     display_name=EXCLUDED.display_name, unit=EXCLUDED.unit,
                                     direction=EXCLUDED.direction, tier=EXCLUDED.tier""",
                                (sector, metric_name, display_name, unit, direction, tier))
                    n += 1
            for sector in OTHER_SECTORS:
                for metric_name, display_name, unit, direction, tier in GENERIC_CORE_METRICS:
                    cur.execute("""INSERT INTO sector_kpi_registry
                                   (sector, metric_name, display_name, unit, direction, tier)
                                   VALUES (%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (sector, metric_name) DO UPDATE SET
                                     display_name=EXCLUDED.display_name, unit=EXCLUDED.unit,
                                     direction=EXCLUDED.direction, tier=EXCLUDED.tier""",
                                (sector, metric_name, display_name, unit, direction, tier))
                    n += 1

            cur.execute("SELECT DISTINCT segment FROM gvm_scores WHERE segment IS NOT NULL")
            segs = [r[0] for r in cur.fetchall()]
            dist = {}
            for seg in segs:
                sec = _infer_sector(seg)
                dist.setdefault(sec, []).append(seg)
            _oplog(cur, "OPS_METRICS_REGISTRY_SEEDED",
                   {"rows": n, "sectors": len(ALL_SECTORS),
                    "segment_distribution": {k: len(v) for k, v in dist.items()}},
                   category="ops_metrics_pull")
            conn.commit()
        return {"rows": n, "sectors": len(ALL_SECTORS), "segments_mapped": len(segs)}
    finally:
        if own:
            conn.close()


def _registry_for_sector(cur, sector):
    cur.execute("""SELECT metric_name, display_name, unit, direction, tier
                   FROM sector_kpi_registry WHERE sector=%s ORDER BY tier, metric_name""", (sector,))
    return [{"metric_name": r[0], "display_name": r[1], "unit": r[2],
              "direction": r[3], "tier": r[4]} for r in cur.fetchall()]


def _sector_for_symbol(cur, symbol):
    cur.execute("SELECT segment FROM gvm_scores WHERE symbol=%s", (symbol,))
    r = cur.fetchone()
    return _infer_sector(r[0] if r else None)


# ── 3. DOCUMENT DISCOVERY (screener.in) -- see HONESTY NOTE 2 ──────────────────

_QUARTER_HINT_RE = re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*['’]?\s*\d{2,4})\b",
                               re.IGNORECASE)
_DOC_LABELS = {"presentation": ("ppt", "presentation", "investor presentation"),
               "transcript": ("transcript",),
               "press_release": ("press release", "results pdf")}


def _discover_docs(symbol):
    """Best-effort screener.in Documents/Concalls discovery, LATEST filing only per doc_type
    (REVISION point 2). Reuses fundamentals_scraper's exact session (_fetch_soup: same UA,
    same consolidated->standalone fallback, same backoff) -- zero extra HTTP calls beyond
    what the financials scraper already makes for this symbol, since Documents lives on the
    same company page. Loose keyword matching on link text (not brittle exact class/id
    selectors) so it tolerates markup drift -- see HONESTY NOTE 2 for the unverified caveat.
    Returns {"presentation": {"url":.., "quarter":..} | None, "transcript": {...} | None,
             "press_release": {...} | None}."""
    result = {"presentation": None, "transcript": None, "press_release": None}
    soup, _cons = _fetch_soup(symbol)
    if soup is None:
        return result

    anchors = soup.find_all("a", href=True)
    for a in anchors:
        text = a.get_text(" ", strip=True).lower()
        href = a["href"]
        if not text or not href:
            continue
        doc_type = None
        if any(lbl in text for lbl in _DOC_LABELS["transcript"]):
            doc_type = "transcript"
        elif any(lbl in text for lbl in _DOC_LABELS["presentation"]):
            doc_type = "presentation"
        elif any(lbl in text for lbl in _DOC_LABELS["press_release"]):
            doc_type = "press_release"
        if doc_type is None or result[doc_type] is not None:
            continue   # first match per type wins -- screener lists newest concall first
        # quarter hint: look at the anchor's own text plus its parent element's text for a
        # nearby month/year label (screener rows carry the concall date beside the links).
        ctx = text
        if a.parent is not None:
            ctx = a.parent.get_text(" ", strip=True).lower() + " " + ctx
        m = _QUARTER_HINT_RE.search(ctx)
        quarter_hint = m.group(1).strip() if m else None
        url = href if href.startswith("http") else f"https://www.screener.in{href}"
        result[doc_type] = {"url": url, "quarter": quarter_hint}

    return result


def _upsert_doc_registry(cur, symbol, docs):
    inserted = 0
    for doc_type, d in docs.items():
        if d is None:
            continue
        cur.execute("""INSERT INTO doc_registry (symbol, doc_type, quarter, url, source, discovered_at, extract_status)
                       VALUES (%s,%s,%s,%s,'screener',NOW(),'pending')
                       ON CONFLICT (symbol, doc_type) DO UPDATE SET
                         quarter=EXCLUDED.quarter, url=EXCLUDED.url, discovered_at=NOW(),
                         extract_status='pending'""",
                    (symbol, doc_type, d.get("quarter"), d.get("url")))
        inserted += 1
    return inserted


# ── 4. PDF FETCH + TEXT EXTRACTION (storage guard: bytes never persisted) ──────

_MAX_DOC_CHARS = {"presentation": 15000, "transcript": 20000}


def _fetch_pdf_text(url, doc_type):
    """Fetch a PDF into memory, extract text via pdfplumber, return it truncated. The PDF
    bytes are never written anywhere -- this function's return value is plain text only."""
    if not url:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for page in pdf.pages[:60]:   # cap pages read, not just chars, to bound decode time
                t = page.extract_text() or ""
                if t:
                    text_parts.append(t)
        text = "\n".join(text_parts).strip()
        if not text:
            return None
        return text[:_MAX_DOC_CHARS.get(doc_type, 15000)]
    except Exception as e:
        log.warning(f"_fetch_pdf_text failed for {url}: {e}")
        return None


# ── 5. EXTRACTION (anthropic call, two-pass) ────────────────────────────────────

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _anthropic_call(prompt, max_tokens=2000):
    """Direct call to the Anthropic API, same pattern as sector_brief_endpoints.py's
    _generate() (raw httpx, not routed through our own /api/anthropic/chat wrapper, since
    that wrapper's caller-facing schema is a plain string prompt with no document/JSON-mode
    support beyond what we build ourselves here anyway)."""
    if not _ANTHROPIC_KEY:
        return None
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                        headers={"x-api-key": _ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": _ANTHROPIC_MODEL, "max_tokens": max_tokens,
                              "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        log.warning(f"_anthropic_call failed: {e}")
        return None


def _metric_list_block(registry):
    return "\n".join(f'- {m["metric_name"]} ("{m["display_name"]}", unit: {m["unit"] or "n/a"})'
                      for m in registry)


def _presentation_prompt(symbol, sector, registry, text):
    return f"""You are extracting operational KPIs for {symbol} ({sector} sector) from an
investor presentation. For EACH metric below, return the value found in the text, its unit,
and a short verbatim quote (<=300 characters) showing exactly where you found it. If a metric
is NOT clearly and explicitly stated in the text, return null for value/unit/quote for it --
NEVER estimate, infer, or calculate a number that isn't directly stated.

Metrics:
{_metric_list_block(registry)}

Return ONLY valid JSON, no markdown fences, in this exact shape:
{{"quarter": "<quarter label found in the doc, e.g. Q1FY27, or null>",
 "metrics": {{"<metric_name>": {{"value": <number or null>, "unit": "<string or null>", "quote": "<string or null>"}}, ...}}}}

PRESENTATION TEXT:
{text}"""


def _transcript_prompt(symbol, sector, registry, text):
    return f"""You are extracting operational KPIs AND a concall summary for {symbol}
({sector} sector) from an earnings-call transcript.

PART A -- for EACH metric below, return the value stated on the call, its unit, and a short
verbatim quote (<=300 characters). If not stated, return null for all three -- never guess.
Metrics:
{_metric_list_block(registry)}

PART B -- write:
 - summary_bullets: 8-12 bullet-sentence strings covering results drivers, demand commentary,
   margin walk, guidance (explicit numbers verbatim where given), capex/expansion plans, and
   risks management flagged.
 - guidance: a short paragraph of forward guidance numbers/statements (verbatim where
   possible), or the literal string "No explicit guidance given" if none was provided.
 - tone: exactly one word -- confident, cautious, defensive, or mixed.

Return ONLY valid JSON, no markdown fences, in this exact shape:
{{"quarter": "<quarter label found in the doc, e.g. Q1FY27, or null>",
 "metrics": {{"<metric_name>": {{"value": <number or null>, "unit": "<string or null>", "quote": "<string or null>"}}, ...}},
 "summary_bullets": ["...", ...], "guidance": "...", "tone": "confident|cautious|defensive|mixed"}}

TRANSCRIPT TEXT:
{text}"""


def _num(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def run_extraction(symbol, sector, registry, pres_text, trans_text, quarter_hint=None):
    """Two-pass extraction (presentation + transcript) merged into per-metric rows, plus a
    concall summary from the transcript pass (cc#523 REVISION point 3: "one call does both
    jobs"). Returns {"quarter": str|None, "metrics": {name: {...}}, "concall": {...}|None}.
    Never fabricates: a metric with no signal from either pass is still emitted (metric_value
    None) so the row exists as an honest "checked, not found" record."""
    pres = _anthropic_call(_presentation_prompt(symbol, sector, registry, pres_text)) if pres_text else None
    trans = _anthropic_call(_transcript_prompt(symbol, sector, registry, trans_text)) if trans_text else None

    quarter = (pres or {}).get("quarter") or (trans or {}).get("quarter") or quarter_hint or "unspecified"
    pres_m = (pres or {}).get("metrics") or {}
    trans_m = (trans or {}).get("metrics") or {}

    metrics_out = {}
    for m in registry:
        name = m["metric_name"]
        p = pres_m.get(name) or {}
        t = trans_m.get(name) or {}
        v1, v2 = _num(p.get("value")), _num(t.get("value"))
        q1, q2 = (p.get("quote") or None), (t.get("quote") or None)
        if q1:
            q1 = q1[:300]
        if q2:
            q2 = q2[:300]
        value = v1 if v1 is not None else v2
        discrepancy = None
        if v1 is not None and v2 is not None:
            denom = max(abs(v1), abs(v2), 1e-9)
            discrepancy = round(abs(v1 - v2) / denom * 100, 2)
        if v1 is not None and v2 is not None:
            confidence = "high" if (discrepancy is not None and discrepancy <= 5) else "low"
        elif v1 is not None or v2 is not None:
            confidence = "medium"
        else:
            confidence = None
        metrics_out[name] = {
            "value": value, "unit": p.get("unit") or t.get("unit") or m["unit"],
            "source_1": q1, "source_1_value": v1, "source_2": q2, "source_2_value": v2,
            "discrepancy_pct": discrepancy, "confidence": confidence,
        }

    concall = None
    if trans is not None:
        concall = {"summary": "\n".join(f"- {b}" for b in (trans.get("summary_bullets") or [])),
                   "guidance": trans.get("guidance"), "tone": trans.get("tone"),
                   "key_metrics": {k: v["value"] for k, v in metrics_out.items() if v["value"] is not None}}

    return {"quarter": quarter, "metrics": metrics_out, "concall": concall}


def write_extraction(cur, symbol, sector, quarter, metrics, concall, doc_urls):
    for name, m in metrics.items():
        cur.execute("""INSERT INTO sector_ops_metrics
            (symbol, sector, metric_name, metric_value, unit, quarter, confidence,
             source_1, source_1_value, source_2, source_2_value, discrepancy_pct, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (symbol, metric_name, quarter) DO UPDATE SET
              metric_value=EXCLUDED.metric_value, unit=EXCLUDED.unit, confidence=EXCLUDED.confidence,
              source_1=EXCLUDED.source_1, source_1_value=EXCLUDED.source_1_value,
              source_2=EXCLUDED.source_2, source_2_value=EXCLUDED.source_2_value,
              discrepancy_pct=EXCLUDED.discrepancy_pct, computed_at=NOW()""",
                    (symbol, sector, name, m["value"], m["unit"], quarter, m["confidence"],
                     m["source_1"], m["source_1_value"], m["source_2"], m["source_2_value"],
                     m["discrepancy_pct"]))
    if concall is not None and (concall.get("summary") or concall.get("guidance")):
        cur.execute("""INSERT INTO concall_summaries
            (symbol, quarter, summary, key_metrics, guidance, tone, source_docs, computed_at)
            VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,NOW())
            ON CONFLICT (symbol, quarter) DO UPDATE SET
              summary=EXCLUDED.summary, key_metrics=EXCLUDED.key_metrics, guidance=EXCLUDED.guidance,
              tone=EXCLUDED.tone, source_docs=EXCLUDED.source_docs, computed_at=NOW()""",
                    (symbol, quarter, concall.get("summary"), json.dumps(concall.get("key_metrics") or {}),
                     concall.get("guidance"), concall.get("tone"), doc_urls))


def run_company(symbol, conn=None, force=False):
    """One company, one pass: discover docs -> fetch+extract text -> two-pass LLM extraction
    -> write sector_ops_metrics + concall_summaries. Used by both the manual per-symbol admin
    endpoint, the backfill orchestrator loop, and the monthly incremental job.

    Idempotent by design (spec: monthly job must "extract ONLY new/unprocessed docs"): if the
    newly-discovered doc URLs are identical to what's already in doc_registry AND that row's
    extract_status is already 'ok', this is the same filing we've already mined -- skip the
    LLM calls entirely (status='already_current') rather than re-spending API cost and creating
    a no-op row overwrite. Pass force=True to re-extract anyway (used by the manual admin
    endpoint so a symbol can always be re-run on demand)."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            sector = _sector_for_symbol(cur, symbol)
            registry = _registry_for_sector(cur, sector)
            if not registry:
                return {"symbol": symbol, "status": "no_registry", "sector": sector}

            docs = _discover_docs(symbol)

            if not force:
                cur.execute("""SELECT doc_type, url, extract_status FROM doc_registry WHERE symbol=%s""", (symbol,))
                existing = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
                new_urls = {dt: (d.get("url") if d else None) for dt, d in docs.items()}
                unchanged = all(
                    new_urls.get(dt) == existing.get(dt, (None, None))[0]
                    and existing.get(dt, (None, None))[1] in ("ok", "absent")
                    for dt in ("presentation", "transcript")
                    if new_urls.get(dt) is not None or existing.get(dt, (None, None))[0] is not None
                )
                if unchanged and any(existing.get(dt, (None, None))[1] == "ok" for dt in ("presentation", "transcript")):
                    return {"symbol": symbol, "status": "already_current", "sector": sector}

            _upsert_doc_registry(cur, symbol, docs)
            conn.commit()

            pres_url = (docs.get("presentation") or {}).get("url")
            trans_url = (docs.get("transcript") or {}).get("url")
            quarter_hint = (docs.get("transcript") or (docs.get("presentation") or {})).get("quarter")
            pres_text = _fetch_pdf_text(pres_url, "presentation") if pres_url else None
            trans_text = _fetch_pdf_text(trans_url, "transcript") if trans_url else None

            if not pres_text and not trans_text:
                for dt in ("presentation", "transcript"):
                    cur.execute("""UPDATE doc_registry SET extract_status='absent'
                                   WHERE symbol=%s AND doc_type=%s AND url IS NULL""", (symbol, dt))
                conn.commit()
                return {"symbol": symbol, "status": "no_docs", "sector": sector}

            result = run_extraction(symbol, sector, registry, pres_text, trans_text, quarter_hint)
            doc_urls = [u for u in (pres_url, trans_url) if u]
            write_extraction(cur, symbol, sector, result["quarter"], result["metrics"],
                              result["concall"], doc_urls)
            for dt, url in (("presentation", pres_url), ("transcript", trans_url)):
                if url:
                    cur.execute("""UPDATE doc_registry SET extracted_at=NOW(), extract_status='ok'
                                   WHERE symbol=%s AND doc_type=%s""", (symbol, dt))
            conn.commit()
            n_found = sum(1 for m in result["metrics"].values() if m["value"] is not None)
            return {"symbol": symbol, "status": "ok", "sector": sector, "quarter": result["quarter"],
                    "metrics_found": n_found, "metrics_total": len(registry),
                    "has_concall": result["concall"] is not None}
    except Exception as e:
        log.error(f"run_company failed for {symbol}: {e}", exc_info=True)
        return {"symbol": symbol, "status": "error", "error": str(e)[:200]}
    finally:
        if own:
            conn.close()


# ── 6. RESUMABLE 500-COMPANY BACKFILL (mirrors cc#500's mc_oneshot pattern) ────

BACKFILL_N = 500
UNIVERSE_KEY = "ops_metrics_backfill_universe"
CURSOR_KEY = "ops_metrics_backfill_cursor"
RUN_FLAG_KEY = "ops_metrics_backfill_run"   # 'pending' | 'running' | 'done'


def _build_universe(cur):
    """Futures (F&O) symbols first, then top-500 by market_cap, de-duped -- same shape as
    gvm_backfill.py's _build_universe, kept local (not imported) to avoid coupling this
    feature to gvm_backfill's internals. Cached to app_config for stable resume ordering."""
    cached = _cfg_get(cur, UNIVERSE_KEY)
    if cached:
        try:
            arr = json.loads(cached)
            if isinstance(arr, list) and arr:
                return arr
        except Exception:
            pass
    cur.execute("SELECT DISTINCT symbol FROM futures_universe WHERE is_active=true")
    futures = [str(r[0]).strip() for r in cur.fetchall()]
    cur.execute("SELECT nse_code FROM screener_raw WHERE market_cap IS NOT NULL "
                "ORDER BY market_cap DESC LIMIT %s", (BACKFILL_N,))
    top = [str(r[0]).strip() for r in cur.fetchall()]
    ordered, seen = [], set()
    for s in futures + top:
        if s and s not in seen:
            ordered.append(s)
            seen.add(s)
    ordered = ordered[:BACKFILL_N]
    _cfg_set(cur, UNIVERSE_KEY, json.dumps(ordered))
    return ordered


def run_ops_metrics_backfill(conn=None, stop_event=None):
    """Latest-quarter-only backfill over the priority 500-company universe (REVISION point 1).
    Checkpointed like cc#500's mc_oneshot: cursor = last-completed symbol, advanced after every
    single item so a crash mid-run loses at most one company's progress. Polite pacing (THROTTLE
    from fundamentals_scraper, same session/backoff) between companies."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            universe = _build_universe(cur)
            cursor = _cfg_get(cur, CURSOR_KEY)
            start_idx = (universe.index(cursor) + 1) if (cursor and cursor in universe) else 0
            pending = universe[start_idx:]
            conn.commit()

        ok = miss = err = skip = 0
        for i, sym in enumerate(pending):
            if stop_event is not None and stop_event.is_set():
                break
            r = run_company(sym)
            if r["status"] == "ok":
                ok += 1
            elif r["status"] == "already_current":
                skip += 1
            elif r["status"] == "no_docs":
                miss += 1
            elif r["status"] == "error":
                err += 1
            with conn.cursor() as cur:
                _cfg_set(cur, CURSOR_KEY, sym)
                conn.commit()
            if (i + 1) % 25 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "OPS_METRICS_BACKFILL_PROGRESS",
                           {"done": i + 1, "total": len(pending), "ok": ok, "already_current": skip,
                            "no_docs": miss, "errors": err})
                    conn.commit()
            time.sleep(THROTTLE)

        with conn.cursor() as cur:
            summary = {"universe": len(universe), "processed": len(pending), "ok": ok,
                       "already_current": skip, "no_docs": miss, "errors": err,
                       "stopped_early": bool(stop_event and stop_event.is_set())}
            _oplog(cur, "OPS_METRICS_BACKFILL_DONE", summary)
            if not (stop_event and stop_event.is_set()):
                cur.execute("DELETE FROM app_config WHERE key=%s", (CURSOR_KEY,))
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


# ── 7. ENDPOINTS ─────────────────────────────────────────────────────────────────

@router.get("/api/ops_metrics/registry")
def get_registry(sector: str = ""):
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        if sector:
            cur.execute("""SELECT sector, metric_name, display_name, unit, direction, tier
                           FROM sector_kpi_registry WHERE sector=%s ORDER BY tier, metric_name""", (sector,))
        else:
            cur.execute("""SELECT sector, metric_name, display_name, unit, direction, tier
                           FROM sector_kpi_registry ORDER BY sector, tier, metric_name""")
        rows = [{"sector": r[0], "metric_name": r[1], "display_name": r[2], "unit": r[3],
                 "direction": r[4], "tier": r[5]} for r in cur.fetchall()]
    return {"count": len(rows), "sectors": len(ALL_SECTORS), "rows": rows}


@router.get("/api/ops_metrics/company/{symbol}")
def get_company_ops_metrics(symbol: str):
    """Replaces the frontend's raw run_sql passthrough. Per metric: latest value, up to 8
    quarters of trend, segment-peer median/rank/best/worst (>=3 peers required, matching the
    existing OPS_MIN_PEERS convention).

    "Latest"/trend ordering uses computed_at (insertion time), NOT a lexicographic sort of the
    quarter label string -- quarter labels are LLM-returned free text ("Q1FY27", "Mar 2024",
    etc.) and sorting those as strings breaks (e.g. "Q1FY27" < "Q4FY26" lexicographically, which
    is chronologically backwards). The pipeline only ever appends newer quarters going forward
    (backfill = latest quarter only, monthly job appends the next one), so insertion order IS
    chronological order in practice."""
    sym = symbol.strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        sector = _sector_for_symbol(cur, sym)
        cur.execute("SELECT segment FROM gvm_scores WHERE symbol=%s", (sym,))
        r = cur.fetchone()
        segment = r[0] if r else None

        cur.execute("""SELECT som.symbol, som.metric_name, som.metric_value, som.unit, som.quarter,
                              som.confidence, som.source_1, som.source_2, som.discrepancy_pct, som.computed_at
                       FROM sector_ops_metrics som JOIN gvm_scores g ON g.symbol = som.symbol
                       WHERE g.segment = %s
                       ORDER BY som.computed_at ASC""", (segment,))
        rows = cur.fetchall()
        registry = _registry_for_sector(cur, sector)

    reg_by_name = {m["metric_name"]: m for m in registry}
    by_metric = {}
    for sy, name, val, unit, quarter, conf, s1, s2, disc, computed_at in rows:
        by_metric.setdefault(name, []).append({
            "symbol": sy, "value": float(val) if val is not None else None, "unit": unit,
            "quarter": quarter, "confidence": conf, "source_1": s1, "source_2": s2,
            "discrepancy_pct": disc, "computed_at": computed_at,
        })

    metrics_out = []
    latest_quarter_seen = None
    for name, entries in by_metric.items():
        reg = reg_by_name.get(name, {})
        lower_better = reg.get("direction") == "lower_better"
        self_entries = sorted([e for e in entries if e["symbol"] == sym], key=lambda e: e["computed_at"])
        trend = [{"quarter": e["quarter"], "value": e["value"]} for e in self_entries[-8:]]
        latest_q = self_entries[-1]["quarter"] if self_entries else None
        if latest_q:
            latest_quarter_seen = latest_q
        peers = [e for e in entries if e["quarter"] == latest_q and e["value"] is not None] if latest_q else []
        self_latest = next((e for e in peers if e["symbol"] == sym), None)
        peer_n = len(peers)
        peer_median = round(statistics.median([p["value"] for p in peers]), 2) if peer_n else None
        sorted_peers = sorted(peers, key=lambda p: p["value"], reverse=not lower_better)
        rank = (next((i + 1 for i, p in enumerate(sorted_peers) if p["symbol"] == sym), None)
                if self_latest else None)
        metrics_out.append({
            "metric_name": name, "display_name": reg.get("display_name", name),
            "unit": reg.get("unit"), "direction": reg.get("direction", "higher_better"),
            "tier": reg.get("tier", "core"),
            "company": self_latest["value"] if self_latest else None,
            "confidence": self_latest["confidence"] if self_latest else None,
            "source_1": self_latest["source_1"] if self_latest else None,
            "source_2": self_latest["source_2"] if self_latest else None,
            "discrepancy_pct": self_latest["discrepancy_pct"] if self_latest else None,
            "quarter": latest_q, "trend": trend,
            "peer_median": peer_median, "peer_n": peer_n, "rank": rank,
            "best": {"symbol": sorted_peers[0]["symbol"], "value": sorted_peers[0]["value"]} if sorted_peers else None,
            "worst": {"symbol": sorted_peers[-1]["symbol"], "value": sorted_peers[-1]["value"]} if sorted_peers else None,
        })

    n_reported = len({e["symbol"] for entries in by_metric.values() for e in entries
                       if e["quarter"] == latest_quarter_seen}) if latest_quarter_seen else 0
    return {"symbol": sym, "sector": sector, "segment": segment, "metrics": metrics_out,
            "quarter": latest_quarter_seen, "quarters_reported": n_reported}


@router.get("/api/ops_metrics/concall/{symbol}")
def get_concall_summary(symbol: str):
    sym = symbol.strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT quarter, summary, key_metrics, guidance, tone, source_docs, computed_at
                       FROM concall_summaries WHERE symbol=%s ORDER BY quarter DESC LIMIT 1""", (sym,))
        r = cur.fetchone()
    if not r:
        return {"symbol": sym, "found": False}
    return {"symbol": sym, "found": True, "quarter": r[0], "summary": r[1], "key_metrics": r[2],
            "guidance": r[3], "tone": r[4], "source_docs": r[5],
            "computed_at": str(r[6]) if r[6] else None}


@router.post("/api/admin/ops_metrics/seed_registry")
def admin_seed_registry(token: str = ""):
    _check_admin(token)
    return seed_registry()


@router.post("/api/admin/ops_metrics/run_company/{symbol}")
def admin_run_company(symbol: str, token: str = ""):
    _check_admin(token)
    return run_company(symbol.strip().upper(), force=True)


@router.post("/api/admin/ops_metrics/run_backfill")
def admin_arm_backfill(token: str = ""):
    _check_admin(token)
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        _cfg_set(cur, RUN_FLAG_KEY, "pending")
        conn.commit()
    return {"armed": True, "flag": f"{RUN_FLAG_KEY}=pending"}


@router.get("/api/admin/ops_metrics/status")
def admin_status():
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("SELECT COUNT(*) FROM sector_kpi_registry")
        n_registry = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT symbol) FROM doc_registry")
        n_doc_syms = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM doc_registry WHERE url IS NOT NULL")
        n_docs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT symbol) FROM sector_ops_metrics")
        n_metric_syms = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT symbol) FROM concall_summaries")
        n_concalls = cur.fetchone()[0]
        cur.execute("SELECT value FROM app_config WHERE key=%s", (RUN_FLAG_KEY,))
        r = cur.fetchone()
        run_flag = r[0] if r else None
        cur.execute("SELECT value FROM app_config WHERE key=%s", (CURSOR_KEY,))
        r = cur.fetchone()
        cursor = r[0] if r else None
    return {"registry_rows": n_registry, "doc_registry_symbols": n_doc_syms, "docs_found": n_docs,
            "companies_with_metrics": n_metric_syms, "companies_with_concall": n_concalls,
            "backfill_run_flag": run_flag, "backfill_cursor": cursor}

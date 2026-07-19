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

from fundamentals_scraper import (_fetch, _fetch_soup, UA, THROTTLE, BASE as SCREENER_BASE,
                                   fetch_company as _fh_fetch_company, _write_symbol as _fh_write_symbol)

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

    # cc#527: this table already existed live (pre-dates this session, seeded 14-Jun-2026)
    # with a schema that does not match what earlier code assumed -- CREATE TABLE IF NOT
    # EXISTS was a silent no-op against it the whole time. Rewritten here to match the real
    # live columns (id SERIAL PK, created_at/updated_at not computed_at, confidence CHECK
    # requires UPPERCASE, plus company_name/discrepancy_flag/verified_at) so a fresh DB would
    # create something compatible with what write_extraction() below actually writes.
    cur.execute("""CREATE TABLE IF NOT EXISTS sector_ops_metrics (
        id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, sector TEXT,
        metric_name TEXT NOT NULL, metric_value NUMERIC, unit TEXT, quarter TEXT NOT NULL,
        confidence TEXT CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
        source_1 TEXT,             -- presentation quote, <=300 chars
        source_1_value NUMERIC,
        source_2 TEXT,             -- transcript quote, <=300 chars
        source_2_value NUMERIC,
        discrepancy_pct NUMERIC,   -- |v1-v2| / max(|v1|,|v2|) * 100, both-present only
        notes TEXT,
        company_name TEXT, discrepancy_flag BOOLEAN DEFAULT FALSE, verified_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now(),
        UNIQUE(symbol, quarter, metric_name))""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_som_symbol_q ON sector_ops_metrics(symbol, quarter)")

    # cc#524 widened the PK from (symbol, doc_type) to (symbol, doc_type, quarter) so the depth
    # backfill can hold up to 4 quarters of filings per company; the monthly/T+1 single-latest
    # path still upserts one row per doc_type per run, just now keyed by that run's quarter too.
    cur.execute("""CREATE TABLE IF NOT EXISTS doc_registry (
        symbol TEXT NOT NULL, doc_type TEXT NOT NULL,   -- 'presentation'|'transcript'|'press_release'
        quarter TEXT NOT NULL, url TEXT, source TEXT DEFAULT 'screener',
        discovered_at TIMESTAMPTZ DEFAULT NOW(), extracted_at TIMESTAMPTZ,
        extract_status TEXT DEFAULT 'pending',    -- 'pending'|'ok'|'failed'|'absent'
        PRIMARY KEY(symbol, doc_type, quarter))""")

    # cc#523 REVISION point 3: PERMANENT -- quarters accumulate, never purged.
    cur.execute("""CREATE TABLE IF NOT EXISTS concall_summaries (
        symbol TEXT NOT NULL, quarter TEXT NOT NULL, summary TEXT, key_metrics JSONB,
        guidance TEXT, tone TEXT, source_docs TEXT[], computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(symbol, quarter))""")

    # cc#524 analytics layer.
    cur.execute("""CREATE TABLE IF NOT EXISTS sector_ops_trends (
        sector TEXT NOT NULL, metric_name TEXT NOT NULL, quarter TEXT NOT NULL,
        median_value NUMERIC, n_companies INTEGER, computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(sector, metric_name, quarter))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS guidance_tracker (
        id BIGSERIAL PRIMARY KEY, symbol TEXT NOT NULL, quarter_guided TEXT NOT NULL,
        quarter_actual TEXT NOT NULL, item_text TEXT NOT NULL, guided_quote TEXT,
        actual_outcome TEXT, actual_quote TEXT, status TEXT,   -- 'MET'|'MISSED'|'MIXED'
        computed_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(symbol, quarter_guided, item_text))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ops_divergence_flags (
        symbol TEXT NOT NULL, metric_name TEXT NOT NULL, quarter TEXT NOT NULL,
        company_direction TEXT, sector_direction TEXT, streak_quarters INTEGER,
        flag_text TEXT, computed_at TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY(symbol, metric_name, quarter))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ops_metrics_failures (
        symbol TEXT PRIMARY KEY, consecutive_failures INTEGER DEFAULT 0,
        last_failure_at TIMESTAMPTZ, last_error TEXT)""")

    # cc#524 QUARTERLY UPDATE FRAMEWORK: T+1 queue -- one row per (symbol, ex_date) result
    # event, tracking whether that event's T+1 (fundamentals + ops-metrics) run succeeded, so
    # the Saturday sweep knows exactly which result-dated companies still need a retry (scoped,
    # not a blind universe sweep).
    cur.execute("""CREATE TABLE IF NOT EXISTS ops_metrics_t1_queue (
        symbol TEXT NOT NULL, ex_date DATE NOT NULL, queued_at TIMESTAMPTZ DEFAULT NOW(),
        processed_at TIMESTAMPTZ, status TEXT DEFAULT 'pending',   -- 'pending'|'done'|'failed'
        PRIMARY KEY(symbol, ex_date))""")

    # cc#527 PHASE-SPLIT: storage-guard AMENDMENT (founder-approved 18-Jul) -- truncated PLAIN
    # TEXT (never raw PDF bytes) may be persisted here so extraction can run later as pure DB
    # reads, decoupled from the fragile scrape step. Rows are deletable once their extraction
    # is confirmed good, but nothing auto-deletes them -- founder decides retention later.
    cur.execute("""CREATE TABLE IF NOT EXISTS doc_texts (
        symbol TEXT NOT NULL, doc_type TEXT NOT NULL, quarter TEXT NOT NULL,
        url TEXT, text_content TEXT, char_count INTEGER,
        fetched_at TIMESTAMPTZ DEFAULT NOW(),
        extract_status TEXT DEFAULT 'stored',   -- 'stored'|'extracted'|'failed_fetch'
        PRIMARY KEY(symbol, doc_type, quarter))""")


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


def _discover_docs_multi(symbol, max_quarters=4):
    """cc#524: same page/session as _discover_docs, but collects up to max_quarters distinct
    filings per doc_type instead of first-match-wins -- used for the one-time depth backfill
    (spec: "the 3 quarters BEFORE the latest"). Returns {"presentation": [{"url","quarter"},...],
    "transcript": [...], "press_release": [...]}, newest first (screener's own list order)."""
    result = {"presentation": [], "transcript": [], "press_release": []}
    soup, _cons = _fetch_soup(symbol)
    if soup is None:
        return result

    seen_quarters = {"presentation": set(), "transcript": set(), "press_release": set()}
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
        if doc_type is None or len(result[doc_type]) >= max_quarters:
            continue
        ctx = text
        if a.parent is not None:
            ctx = a.parent.get_text(" ", strip=True).lower() + " " + ctx
        m = _QUARTER_HINT_RE.search(ctx)
        quarter_hint = m.group(1).strip() if m else None
        key = quarter_hint or href   # dedupe by quarter label; fall back to URL if no date found
        if key in seen_quarters[doc_type]:
            continue
        seen_quarters[doc_type].add(key)
        url = href if href.startswith("http") else f"https://www.screener.in{href}"
        result[doc_type].append({"url": url, "quarter": quarter_hint})

    return result


def _upsert_doc_registry(cur, symbol, docs):
    """docs: {"presentation": {..}|None, "transcript": {..}|None, ...} (single, from
    _discover_docs) OR {"presentation": [{..},...], ...} (list, from _discover_docs_multi) --
    handles both shapes. Conflict target is (symbol, doc_type, quarter) since cc#524 widened
    the PK to hold multiple quarters per doc_type."""
    inserted = 0
    for doc_type, d in docs.items():
        entries = d if isinstance(d, list) else ([d] if d else [])
        for entry in entries:
            if not entry:
                continue
            cur.execute("""INSERT INTO doc_registry (symbol, doc_type, quarter, url, source, discovered_at, extract_status)
                           VALUES (%s,%s,%s,%s,'screener',NOW(),'pending')
                           ON CONFLICT (symbol, doc_type, quarter) DO UPDATE SET
                             url=EXCLUDED.url, discovered_at=NOW(), extract_status='pending'""",
                        (symbol, doc_type, entry.get("quarter") or "unspecified", entry.get("url")))
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
_warned_no_anthropic_key = False
# cc#524 cost control: HAIKU is the default extraction model (strict JSON schema + mandatory
# source quotes keep it reliable for this structured task); SONNET is the escalation tier, used
# only when Haiku's own result signals trouble (see _should_escalate below) -- never routine.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"


def _anthropic_call(prompt, max_tokens=2000, model=HAIKU_MODEL):
    """Direct call to the Anthropic API, same pattern as sector_brief_endpoints.py's
    _generate() (raw httpx, not routed through our own /api/anthropic/chat wrapper, since
    that wrapper's caller-facing schema is a plain string prompt with no document/JSON-mode
    support beyond what we build ourselves here anyway). Returns (parsed_json_or_None,
    usage_dict) -- usage is {"input_tokens":.., "output_tokens":.., "model":..} or {} on
    failure, so callers can accumulate real token spend (cc#524: "log actual token spend...
    so the number is known, not guessed")."""
    if not _ANTHROPIC_KEY:
        # cc#526 item 2: this used to fail silent -- a missing ANTHROPIC_API_KEY would make
        # every extraction pass quietly return None with no diagnostic trail at all. Now logged
        # once per process (not once per call, which would spam ops_log every company) so a
        # missing key is immediately visible instead of looking identical to "no docs found".
        global _warned_no_anthropic_key
        if not _warned_no_anthropic_key:
            _warned_no_anthropic_key = True
            log.error("ANTHROPIC_API_KEY not set -- ops-metrics extraction will no-op for every company")
            try:
                with _conn() as _c, _c.cursor() as _cur:
                    _oplog(_cur, "OPS_METRICS_NO_API_KEY",
                           {"message": "ANTHROPIC_API_KEY missing -- extraction calls no-op"})
                    _c.commit()
            except Exception:
                pass
        return None, {}
    try:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=60,
                        headers={"x-api-key": _ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": model, "max_tokens": max_tokens,
                              "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        body = r.json()
        usage = body.get("usage") or {}
        usage["model"] = model
        text = body["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip()), usage
    except Exception as e:
        log.warning(f"_anthropic_call failed ({model}): {e}")
        return None, {}


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


def _merge_pass(registry, pres, trans, quarter_hint):
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
    return quarter, metrics_out


_DISCREPANCY_ESCALATE_PCT = 10.0   # cc#524: discrepancy above this triggers a Sonnet re-check


def _should_escalate(metrics_out, pres_text, trans_text):
    """cc#524 cost control: escalate to Sonnet only on (a) any metric confidence LOW,
    (b) any metric discrepancy_pct above the escalate threshold, (c) a doc existed for a pass
    but that pass returned nothing at all (empty extraction from a real document) -- never a
    routine re-run."""
    any_low = any(v["confidence"] == "low" for v in metrics_out.values())
    any_big_gap = any((v["discrepancy_pct"] or 0) > _DISCREPANCY_ESCALATE_PCT for v in metrics_out.values())
    all_empty = all(v["value"] is None for v in metrics_out.values())
    doc_existed = bool(pres_text or trans_text)
    return any_low or any_big_gap or (all_empty and doc_existed)


def run_extraction(symbol, sector, registry, pres_text, trans_text, quarter_hint=None):
    """Two-pass extraction (presentation + transcript) merged into per-metric rows, plus a
    concall summary from the transcript pass (cc#523 REVISION point 3: "one call does both
    jobs"). Default model is Haiku (cc#524 cost control); escalates to Sonnet and re-runs both
    passes when _should_escalate flags trouble. Returns {"quarter":.., "metrics": {name:{...}},
    "concall": {...}|None, "model_used": "haiku"|"sonnet", "usage": [usage_dict, ...]}.
    Never fabricates: a metric with no signal from either pass is still emitted (metric_value
    None) so the row exists as an honest "checked, not found" record."""
    usages = []

    def _call(text, prompt_fn, model):
        if not text:
            return None
        parsed, usage = _anthropic_call(prompt_fn(symbol, sector, registry, text), model=model)
        if usage:
            usages.append(usage)
        return parsed

    pres = _call(pres_text, _presentation_prompt, HAIKU_MODEL)
    trans = _call(trans_text, _transcript_prompt, HAIKU_MODEL)
    quarter, metrics_out = _merge_pass(registry, pres, trans, quarter_hint)
    model_used = "haiku"

    if _should_escalate(metrics_out, pres_text, trans_text):
        pres2 = _call(pres_text, _presentation_prompt, SONNET_MODEL)
        trans2 = _call(trans_text, _transcript_prompt, SONNET_MODEL)
        quarter, metrics_out = _merge_pass(registry, pres2, trans2, quarter_hint)
        trans = trans2   # concall summary comes from whichever pass actually ran last
        model_used = "sonnet"

    concall = None
    if trans is not None:
        concall = {"summary": "\n".join(f"- {b}" for b in (trans.get("summary_bullets") or [])),
                   "guidance": trans.get("guidance"), "tone": trans.get("tone"),
                   "key_metrics": {k: v["value"] for k, v in metrics_out.items() if v["value"] is not None}}

    return {"quarter": quarter, "metrics": metrics_out, "concall": concall,
            "model_used": model_used, "usage": usages}


def write_extraction(cur, symbol, sector, quarter, metrics, concall, doc_urls):
    # cc#527 fix: the live sector_ops_metrics table (pre-existing, see ensure_tables note above)
    # has no computed_at column -- it has created_at/updated_at instead -- and its confidence
    # CHECK constraint requires UPPERCASE 'HIGH'/'MEDIUM'/'LOW'. Every write_extraction() call
    # since cc#523 was failing with "column computed_at does not exist" on this exact INSERT;
    # internal confidence comparisons (_merge_pass/_should_escalate) stay lowercase, only the
    # value written to the DB is upper-cased here.
    for name, m in metrics.items():
        confidence = m["confidence"].upper() if m["confidence"] else None
        cur.execute("""INSERT INTO sector_ops_metrics
            (symbol, sector, metric_name, metric_value, unit, quarter, confidence,
             source_1, source_1_value, source_2, source_2_value, discrepancy_pct, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (symbol, quarter, metric_name) DO UPDATE SET
              metric_value=EXCLUDED.metric_value, unit=EXCLUDED.unit, confidence=EXCLUDED.confidence,
              source_1=EXCLUDED.source_1, source_1_value=EXCLUDED.source_1_value,
              source_2=EXCLUDED.source_2, source_2_value=EXCLUDED.source_2_value,
              discrepancy_pct=EXCLUDED.discrepancy_pct, updated_at=NOW()""",
                    (symbol, sector, name, m["value"], m["unit"], quarter, confidence,
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

            # cc#526 item 2b: discovery gets its own try/except -- see run_company_depth's
            # identical rationale.
            try:
                docs = _discover_docs(symbol)
            except Exception as e:
                err_text = f"discovery failed: {type(e).__name__}: {e}"
                log.warning(f"run_company discovery failed for {symbol}: {err_text}")
                _record_failure(cur, symbol, err_text)
                conn.commit()
                return {"symbol": symbol, "status": "fetch_blocked", "sector": sector, "error": err_text[:200]}
            quarter_hint = (docs.get("transcript") or docs.get("presentation") or {}).get("quarter") or "unspecified"

            if not force:
                cur.execute("""SELECT DISTINCT ON (doc_type) doc_type, url, extract_status
                               FROM doc_registry WHERE symbol=%s ORDER BY doc_type, discovered_at DESC""", (symbol,))
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

            try:
                _upsert_doc_registry(cur, symbol, docs)
                conn.commit()
            except Exception as e:
                err_text = f"doc_registry upsert failed: {type(e).__name__}: {e}"
                log.warning(f"run_company upsert failed for {symbol}: {err_text}")
                _record_failure(cur, symbol, err_text)
                conn.commit()
                return {"symbol": symbol, "status": "fetch_blocked", "sector": sector, "error": err_text[:200]}

            pres_url = (docs.get("presentation") or {}).get("url")
            trans_url = (docs.get("transcript") or {}).get("url")
            pres_text = _fetch_pdf_text(pres_url, "presentation") if pres_url else None
            trans_text = _fetch_pdf_text(trans_url, "transcript") if trans_url else None

            if not pres_text and not trans_text:
                for dt in ("presentation", "transcript"):
                    q = (docs.get(dt) or {}).get("quarter") or quarter_hint
                    if docs.get(dt) is None:
                        cur.execute("""INSERT INTO doc_registry (symbol, doc_type, quarter, url, source, discovered_at, extract_status)
                                       VALUES (%s,%s,%s,NULL,'screener',NOW(),'absent')
                                       ON CONFLICT (symbol, doc_type, quarter) DO UPDATE SET extract_status='absent'""",
                                    (symbol, dt, q))
                conn.commit()
                _record_failure(cur, symbol, "no presentation/transcript text extracted (docs missing or PDF unreadable)")
                conn.commit()
                return {"symbol": symbol, "status": "no_docs", "sector": sector}

            result = run_extraction(symbol, sector, registry, pres_text, trans_text, quarter_hint)
            doc_urls = [u for u in (pres_url, trans_url) if u]
            write_extraction(cur, symbol, sector, result["quarter"], result["metrics"],
                              result["concall"], doc_urls)
            for dt, url in (("presentation", pres_url), ("transcript", trans_url)):
                if url:
                    q = (docs.get(dt) or {}).get("quarter") or quarter_hint
                    cur.execute("""UPDATE doc_registry SET extracted_at=NOW(), extract_status='ok'
                                   WHERE symbol=%s AND doc_type=%s AND quarter=%s""", (symbol, dt, q))
            _log_token_usage(cur, symbol, result["model_used"], result["usage"])
            _clear_failure(cur, symbol)
            post_extraction_chain(cur, symbol, sector)
            conn.commit()
            n_found = sum(1 for m in result["metrics"].values() if m["value"] is not None)
            return {"symbol": symbol, "status": "ok", "sector": sector, "quarter": result["quarter"],
                    "metrics_found": n_found, "metrics_total": len(registry),
                    "has_concall": result["concall"] is not None, "model_used": result["model_used"]}
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        log.error(f"run_company failed for {symbol}: {err_text}", exc_info=True)
        try:
            with conn.cursor() as cur:
                _record_failure(cur, symbol, err_text)
                conn.commit()
        except Exception as e2:
            log.error(f"run_company: _record_failure ALSO failed for {symbol}: {e2}")
        return {"symbol": symbol, "status": "error", "error": err_text[:200]}
    finally:
        if own:
            conn.close()


DEPTH_QUARTERS = 4   # cc#524: first-leg backfill depth (was 1 quarter under cc#523)


def run_company_depth(symbol, conn=None, max_quarters=DEPTH_QUARTERS):
    """cc#524 item 1: same company, but discovers and extracts up to max_quarters filings
    (not just the latest) via _discover_docs_multi, so the store ends up holding e.g. Q2FY26,
    Q3FY26, Q4FY26, Q1FY27 per company as available. Each quarter gets its own doc_registry
    row and its own extraction pass (cost-controlled the same way as run_company: Haiku
    default, Sonnet escalation on trouble). Returns a summary dict with per-quarter results."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            sector = _sector_for_symbol(cur, symbol)
            registry = _registry_for_sector(cur, sector)
            if not registry:
                return {"symbol": symbol, "status": "no_registry", "sector": sector, "quarters": []}

            # cc#526 item 2b: the discovery step (screener fetch + doc_registry upsert) gets its
            # own try/except -- a fetch/parse/DB exception here must never be conflated with a
            # generic "error" that looks the same as an extraction failure. Classified as
            # 'fetch_blocked': the symbol stays resumable (cursor still advances so the backfill
            # doesn't stall, but next run's idempotency check will naturally retry it since no
            # doc_registry row got marked 'ok').
            try:
                docs_multi = _discover_docs_multi(symbol, max_quarters=max_quarters)
                _upsert_doc_registry(cur, symbol, docs_multi)
                conn.commit()
            except Exception as e:
                err_text = f"discovery failed: {type(e).__name__}: {e}"
                log.warning(f"run_company_depth discovery failed for {symbol}: {err_text}")
                _record_failure(cur, symbol, err_text)
                conn.commit()
                return {"symbol": symbol, "status": "fetch_blocked", "sector": sector,
                        "error": err_text[:200], "quarters": []}

            # Pair up presentation/transcript entries by quarter label (best-effort match --
            # screener usually lists both for the same quarter adjacently, but a doc missing
            # one side just extracts from whichever side it has).
            by_quarter = {}
            for dt in ("presentation", "transcript"):
                for entry in docs_multi.get(dt) or []:
                    q = entry.get("quarter") or "unspecified"
                    by_quarter.setdefault(q, {})[dt] = entry
            quarters_done = []
            any_ok = False
            for quarter, entries in list(by_quarter.items())[:max_quarters]:
                pres_url = (entries.get("presentation") or {}).get("url")
                trans_url = (entries.get("transcript") or {}).get("url")
                pres_text = _fetch_pdf_text(pres_url, "presentation") if pres_url else None
                trans_text = _fetch_pdf_text(trans_url, "transcript") if trans_url else None
                if not pres_text and not trans_text:
                    quarters_done.append({"quarter": quarter, "status": "no_docs"})
                    continue
                result = run_extraction(symbol, sector, registry, pres_text, trans_text, quarter)
                doc_urls = [u for u in (pres_url, trans_url) if u]
                write_extraction(cur, symbol, sector, result["quarter"], result["metrics"],
                                  result["concall"], doc_urls)
                for dt, url in (("presentation", pres_url), ("transcript", trans_url)):
                    if url:
                        cur.execute("""UPDATE doc_registry SET extracted_at=NOW(), extract_status='ok'
                                       WHERE symbol=%s AND doc_type=%s AND quarter=%s""", (symbol, dt, quarter))
                _log_token_usage(cur, symbol, result["model_used"], result["usage"])
                conn.commit()
                any_ok = True
                n_found = sum(1 for m in result["metrics"].values() if m["value"] is not None)
                quarters_done.append({"quarter": result["quarter"], "status": "ok",
                                       "metrics_found": n_found, "model_used": result["model_used"]})
                time.sleep(0.5)   # polite pacing between quarters of the same company

            if any_ok:
                _clear_failure(cur, symbol)
                post_extraction_chain(cur, symbol, sector)
            else:
                no_docs_reason = ("no presentation/transcript filings discovered at all" if not by_quarter
                                   else f"discovered {len(by_quarter)} quarter(s) but none had readable text")
                _record_failure(cur, symbol, no_docs_reason)
            conn.commit()
            return {"symbol": symbol, "status": "ok" if any_ok else "no_docs", "sector": sector,
                    "quarters": quarters_done}
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        log.error(f"run_company_depth failed for {symbol}: {err_text}", exc_info=True)
        try:
            with conn.cursor() as cur:
                _record_failure(cur, symbol, err_text)
                conn.commit()
        except Exception as e2:
            log.error(f"run_company_depth: _record_failure ALSO failed for {symbol}: {e2}")
        return {"symbol": symbol, "status": "error", "error": err_text[:200], "quarters": []}
    finally:
        if own:
            conn.close()


def run_company_text_fetch(symbol, conn=None, max_quarters=DEPTH_QUARTERS):
    """cc#527 PHASE-SPLIT: fetch + store TEXT ONLY -- discover docs, fetch each PDF, extract
    plain text, write to doc_texts. NO anthropic_call, NO write_extraction, NO
    post_extraction_chain -- this is the fragile scrape step decoupled from extraction so a
    future extraction pass is a pure DB read with zero re-scraping. Registry-independent (no
    _registry_for_sector gate) since fetching docs has nothing to do with what metrics a
    sector's KPI registry defines. ~3-4x faster per company than run_company_depth (no LLM
    calls in the loop)."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            sector = _sector_for_symbol(cur, symbol)

            try:
                docs_multi = _discover_docs_multi(symbol, max_quarters=max_quarters)
                _upsert_doc_registry(cur, symbol, docs_multi)
                conn.commit()
            except Exception as e:
                err_text = f"discovery failed: {type(e).__name__}: {e}"
                log.warning(f"run_company_text_fetch discovery failed for {symbol}: {err_text}")
                _record_failure(cur, symbol, err_text)
                conn.commit()
                return {"symbol": symbol, "status": "fetch_blocked", "sector": sector,
                        "error": err_text[:200], "quarters": []}

            by_quarter = {}
            for dt in ("presentation", "transcript"):
                for entry in docs_multi.get(dt) or []:
                    q = entry.get("quarter") or "unspecified"
                    by_quarter.setdefault(q, {})[dt] = entry

            quarters_done = []
            any_ok = False
            for quarter, entries in list(by_quarter.items())[:max_quarters]:
                for dt in ("presentation", "transcript"):
                    entry = entries.get(dt)
                    if not entry or not entry.get("url"):
                        continue
                    url = entry["url"]
                    text = _fetch_pdf_text(url, dt)
                    if not text:
                        cur.execute("""INSERT INTO doc_texts (symbol, doc_type, quarter, url, text_content,
                                       char_count, fetched_at, extract_status)
                                       VALUES (%s,%s,%s,%s,NULL,0,NOW(),'failed_fetch')
                                       ON CONFLICT (symbol, doc_type, quarter) DO UPDATE SET
                                         url=EXCLUDED.url, text_content=NULL, char_count=0,
                                         fetched_at=NOW(), extract_status='failed_fetch'""",
                                    (symbol, dt, quarter, url))
                        continue
                    cur.execute("""INSERT INTO doc_texts (symbol, doc_type, quarter, url, text_content,
                                   char_count, fetched_at, extract_status)
                                   VALUES (%s,%s,%s,%s,%s,%s,NOW(),'stored')
                                   ON CONFLICT (symbol, doc_type, quarter) DO UPDATE SET
                                     url=EXCLUDED.url, text_content=EXCLUDED.text_content,
                                     char_count=EXCLUDED.char_count, fetched_at=NOW(), extract_status='stored'""",
                                (symbol, dt, quarter, url, text, len(text)))
                    cur.execute("""UPDATE doc_registry SET extracted_at=NOW(), extract_status='ok'
                                   WHERE symbol=%s AND doc_type=%s AND quarter=%s""", (symbol, dt, quarter))
                    any_ok = True
                conn.commit()
                quarters_done.append({"quarter": quarter, "status": "ok" if any_ok else "no_docs"})
                time.sleep(0.5)   # polite pacing between quarters of the same company

            if any_ok:
                _clear_failure(cur, symbol)
            else:
                no_docs_reason = ("no presentation/transcript filings discovered at all" if not by_quarter
                                   else f"discovered {len(by_quarter)} quarter(s) but no PDF text extracted")
                _record_failure(cur, symbol, no_docs_reason)
            conn.commit()
            return {"symbol": symbol, "status": "ok" if any_ok else "no_docs", "sector": sector,
                    "quarters": quarters_done}
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        log.error(f"run_company_text_fetch failed for {symbol}: {err_text}", exc_info=True)
        try:
            with conn.cursor() as cur:
                _record_failure(cur, symbol, err_text)
                conn.commit()
        except Exception as e2:
            log.error(f"run_company_text_fetch: _record_failure ALSO failed for {symbol}: {e2}")
        return {"symbol": symbol, "status": "error", "error": err_text[:200], "quarters": []}
    finally:
        if own:
            conn.close()


TEXT_FETCH_CURSOR_KEY = "ops_text_fetch_cursor"
TEXT_FETCH_RUN_FLAG_KEY = "ops_text_fetch_run"   # 'pending' | 'running' | 'done'


def run_text_fetch_backfill(conn=None, stop_event=None):
    """cc#527 PHASE-SPLIT phase 1 runner: identical skeleton to run_ops_metrics_backfill --
    SAME universe builder (_build_universe, shared cache/key: it's just "which 500 companies in
    priority order", not run-state) but its OWN cursor/flag app_config keys -- the old
    ops_metrics_backfill_run/cursor stay completely untouched, this is a separate resumable job,
    not a variant of the old one. Fetch-only: no LLM calls in the loop, ~3-4x faster per company."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            universe = _build_universe(cur)
            cursor = _cfg_get(cur, TEXT_FETCH_CURSOR_KEY)
            start_idx = (universe.index(cursor) + 1) if (cursor and cursor in universe) else 0
            pending = universe[start_idx:]
            conn.commit()

        ok = miss = err = blocked = 0
        sample_error = None
        for i, sym in enumerate(pending):
            if stop_event is not None and stop_event.is_set():
                break
            r = run_company_text_fetch(sym)
            if r["status"] == "ok":
                ok += 1
            elif r["status"] == "no_docs":
                miss += 1
            elif r["status"] == "fetch_blocked":
                blocked += 1
                if sample_error is None:
                    sample_error = r.get("error")
            elif r["status"] == "error":
                err += 1
                if sample_error is None:
                    sample_error = r.get("error")
            with conn.cursor() as cur:
                _cfg_set(cur, TEXT_FETCH_CURSOR_KEY, sym)
                conn.commit()
            if (i + 1) % 25 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "OPS_TEXT_FETCH_PROGRESS",
                           {"done": i + 1, "total": len(pending), "ok": ok, "no_docs": miss,
                            "fetch_blocked": blocked, "errors": err, "sample_error": sample_error})
                    conn.commit()
                sample_error = None
            time.sleep(OPS_THROTTLE)

        with conn.cursor() as cur:
            summary = {"universe": len(universe), "processed": len(pending), "ok": ok,
                       "no_docs": miss, "fetch_blocked": blocked, "errors": err,
                       "stopped_early": bool(stop_event and stop_event.is_set())}
            _oplog(cur, "OPS_TEXT_FETCH_DONE", summary)
            if not (stop_event and stop_event.is_set()):
                cur.execute("DELETE FROM app_config WHERE key=%s", (TEXT_FETCH_CURSOR_KEY,))
            conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_total_relation_size('doc_texts')")
            sz = cur.fetchone()[0] or 0
            storage = {"doc_texts_bytes": sz, "doc_texts_mb": round(sz / 1048576, 2)}
            _oplog(cur, "OPS_TEXT_FETCH_STORAGE", storage)
            conn.commit()
        return summary
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


OPS_THROTTLE = 5.0   # cc#526: dedicated, more polite than fundamentals_scraper's THROTTLE (2.5s)
                      # -- this pipeline hits the same screener.in pages fundamentals_scraper
                      # does, PLUS a PDF fetch on top, so it should be gentler, not equally fast.


def run_ops_metrics_backfill(conn=None, stop_event=None):
    """cc#524: first-leg 500-company backfill, now DEPTH_QUARTERS (4) quarters per company
    (was 1 under cc#523) via run_company_depth. Checkpointed like cc#500's mc_oneshot: cursor =
    last-completed symbol, advanced after every single item so a crash mid-run loses at most
    one company's progress. OPS_THROTTLE pacing between companies. Token spend rolled up to
    ops_log every 100 companies.

    cc#526: progress logs now carry a sample_error (first error string seen in that 25-batch)
    so a systematic failure is diagnosable from ops_log alone, without needing a manual repro."""
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

        ok = miss = err = blocked = 0
        sample_error = None
        for i, sym in enumerate(pending):
            if stop_event is not None and stop_event.is_set():
                break
            r = run_company_depth(sym)
            if r["status"] == "ok":
                ok += 1
            elif r["status"] == "no_docs":
                miss += 1
            elif r["status"] == "fetch_blocked":
                blocked += 1
                if sample_error is None:
                    sample_error = r.get("error")
            elif r["status"] == "error":
                err += 1
                if sample_error is None:
                    sample_error = r.get("error")
            with conn.cursor() as cur:
                _cfg_set(cur, CURSOR_KEY, sym)
                conn.commit()
            if (i + 1) % 25 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "OPS_METRICS_BACKFILL_PROGRESS",
                           {"done": i + 1, "total": len(pending), "ok": ok, "no_docs": miss,
                            "fetch_blocked": blocked, "errors": err, "sample_error": sample_error})
                    conn.commit()
                sample_error = None   # fresh sample per batch, not one stale error forever
            if (i + 1) % 100 == 0:
                log_token_rollup(conn)
            time.sleep(OPS_THROTTLE)

        with conn.cursor() as cur:
            summary = {"universe": len(universe), "processed": len(pending), "ok": ok,
                       "no_docs": miss, "fetch_blocked": blocked, "errors": err,
                       "stopped_early": bool(stop_event and stop_event.is_set())}
            _oplog(cur, "OPS_METRICS_BACKFILL_DONE", summary)
            if not (stop_event and stop_event.is_set()):
                cur.execute("DELETE FROM app_config WHERE key=%s", (CURSOR_KEY,))
            conn.commit()
        log_token_rollup(conn)
        measure_storage_delta(conn)
        return summary
    finally:
        if own:
            conn.close()


# ── 6a. PHASE-2 EXTRACTION (cc#541) — LLM extraction sourced ONLY from stored
# doc_texts (cc#527 phase-split fetched them). ZERO screener.in / PDF HTTP here. ─

EXTRACT_CURSOR_KEY = "ops_extraction_cursor"
EXTRACT_RUN_FLAG_KEY = "ops_extraction_run"   # 'pending' | 'running' | 'done'


def _extract_universe(cur):
    """cc#541 phase-2 universe: every symbol with fetched doc_texts (any status), stable
    ORDER BY symbol for resumable cursoring. This tracks the doc_texts corpus (~476 syms),
    NOT _build_universe's top-500 — we extract exactly what phase-1 stored."""
    cur.execute("SELECT DISTINCT symbol FROM doc_texts ORDER BY symbol")
    return [r[0] for r in cur.fetchall()]


def run_company_extract_from_doctexts(symbol, conn=None, force=False):
    """cc#541 PHASE-2: two-pass LLM extraction sourced ONLY from stored doc_texts — ZERO HTTP.
    For each quarter of this symbol that still has 'stored' doc_texts, gather presentation +
    transcript text, run the SAME run_extraction + write_extraction as the live path, then flip
    those doc_texts rows to 'extracted' (the idempotency marker — a re-run skips them). Honest:
    a metric with no signal is still written (value NULL). force=True re-extracts every quarter."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            sector = _sector_for_symbol(cur, symbol)
            registry = _registry_for_sector(cur, sector)
            if not registry:
                _record_failure(cur, symbol, f"no KPI registry for sector={sector}")
                conn.commit()
                return {"symbol": symbol, "status": "no_registry", "sector": sector}
            status_filter = "" if force else "AND extract_status='stored'"
            cur.execute(f"""SELECT quarter, doc_type, url, text_content
                            FROM doc_texts
                            WHERE symbol=%s AND text_content IS NOT NULL {status_filter}""",
                        (symbol,))
            rows = cur.fetchall()
        if not rows:
            return {"symbol": symbol, "status": "nothing_to_extract", "sector": sector}

        by_q = {}
        for quarter, doc_type, url, text in rows:
            q = quarter or "unspecified"
            by_q.setdefault(q, {})[doc_type] = (url, text)

        quarters_done, total_found, errored = [], 0, False
        for quarter, docs in by_q.items():
            pres_url, pres_text = docs.get("presentation", (None, None))
            trans_url, trans_text = docs.get("transcript", (None, None))
            if not pres_text and not trans_text:
                continue
            try:
                result = run_extraction(symbol, sector, registry, pres_text, trans_text, quarter)
            except Exception as e:
                errored = True
                with conn.cursor() as cur:
                    _record_failure(cur, symbol, f"extraction failed q={quarter}: {type(e).__name__}: {e}")
                    conn.commit()
                continue
            doc_urls = [u for u in (pres_url, trans_url) if u]
            # COMMIT the metrics per-quarter FIRST (mirrors run_company_depth). post_extraction_chain
            # runs AFTER the loop in its OWN transaction: if the analytics chain silently aborts the
            # txn (a swallowed internal error), a single shared commit would roll the metrics back too
            # — the cc#541 phase-2 persistence bug (status=ok but zero rows written).
            with conn.cursor() as cur:
                write_extraction(cur, symbol, sector, result["quarter"] or quarter,
                                 result["metrics"], result["concall"], doc_urls)
                cur.execute("""UPDATE doc_texts SET extract_status='extracted'
                               WHERE symbol=%s AND quarter=%s AND extract_status='stored'""",
                            (symbol, quarter))
                _log_token_usage(cur, symbol, result["model_used"], result["usage"])
                conn.commit()
            n = sum(1 for m in result["metrics"].values() if m["value"] is not None)
            total_found += n
            quarters_done.append({"quarter": result["quarter"] or quarter,
                                  "metrics_found": n, "model": result["model_used"]})

        # analytics chain + failure-clear, isolated so a chain error can't discard the committed metrics
        if quarters_done:
            try:
                with conn.cursor() as cur:
                    _clear_failure(cur, symbol)
                    post_extraction_chain(cur, symbol, sector)
                    conn.commit()
            except Exception as _pe:
                try:
                    conn.rollback()
                except Exception:
                    pass
                log.warning(f"post_extraction_chain failed for {symbol} (metrics already committed): {_pe}")
        if not quarters_done and not errored:
            return {"symbol": symbol, "status": "no_text", "sector": sector}
        return {"symbol": symbol, "status": "ok" if quarters_done else "error", "sector": sector,
                "quarters": quarters_done, "metrics_found": total_found}
    except Exception as e:
        err_text = f"{type(e).__name__}: {e}"
        log.error(f"run_company_extract_from_doctexts failed for {symbol}: {err_text}", exc_info=True)
        try:
            with conn.cursor() as cur:
                _record_failure(cur, symbol, err_text)
                conn.commit()
        except Exception:
            pass
        return {"symbol": symbol, "status": "error", "error": err_text[:200]}
    finally:
        if own:
            conn.close()


def run_extraction_backfill(conn=None, stop_event=None, time_budget_s=1200):
    """cc#541 PHASE-2 runner: extract sector_ops_metrics from stored doc_texts, ZERO re-fetch.
    Resumable cursor over the doc_texts symbol universe (ops_extraction_cursor); self-limits to
    time_budget_s per invocation and resumes next tick (LLM extraction over ~476 symbols is
    multi-hour). Returns a summary incl. `complete`; per-run progress to ops_log."""
    own = conn is None
    conn = conn or _conn()
    t0 = time.time()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            universe = _extract_universe(cur)
            cursor = _cfg_get(cur, EXTRACT_CURSOR_KEY)
            start_idx = (universe.index(cursor) + 1) if (cursor and cursor in universe) else 0
            pending = universe[start_idx:]
            conn.commit()

        ok = noreg = notext = err = 0
        sample_error = None
        processed = 0
        for sym in pending:
            if stop_event is not None and stop_event.is_set():
                break
            if time.time() - t0 > time_budget_s:
                break
            r = run_company_extract_from_doctexts(sym)
            st = r["status"]
            if st == "ok":
                ok += 1
            elif st == "no_registry":
                noreg += 1
            elif st in ("no_text", "nothing_to_extract"):
                notext += 1
            elif st == "error":
                err += 1
                if sample_error is None:
                    sample_error = r.get("error")
            with conn.cursor() as cur:
                _cfg_set(cur, EXTRACT_CURSOR_KEY, sym)
                conn.commit()
            processed += 1
            if processed % 20 == 0:
                with conn.cursor() as cur:
                    _oplog(cur, "OPS_EXTRACTION_PROGRESS",
                           {"processed": processed, "remaining": len(pending) - processed,
                            "ok": ok, "no_registry": noreg, "no_text": notext, "errors": err,
                            "sample_error": sample_error})
                    conn.commit()
                log_token_rollup(conn)
                sample_error = None
            time.sleep(1.0)   # gentle between companies (pure LLM+DB, lighter than the 5s scrape throttle)

        complete = processed >= len(pending)
        with conn.cursor() as cur:
            summary = {"universe": len(universe), "processed": processed,
                       "remaining": max(0, len(pending) - processed), "ok": ok,
                       "no_registry": noreg, "no_text": notext, "errors": err,
                       "complete": complete, "sample_error": sample_error}
            _oplog(cur, "OPS_EXTRACTION_DONE" if complete else "OPS_EXTRACTION_PROGRESS", summary)
            if complete:
                cur.execute("DELETE FROM app_config WHERE key=%s", (EXTRACT_CURSOR_KEY,))
            conn.commit()
        log_token_rollup(conn)
        return summary
    finally:
        if own:
            conn.close()


# ── 6b. ANALYTICS LAYER (cc#524) -- all computed from data already sitting in
# sector_ops_metrics/concall_summaries, zero new scraping/data ────────────────

def _record_failure(cur, symbol, error=None):
    """cc#524 failure rule: 2 consecutive failed extractions -> persistent ops_log warning
    (not silent NO DATA forever).

    cc#526 BUG FIX: this never wrote last_error (column existed, was never populated in the
    INSERT), and worse -- callers invoke this from an `except Exception:` handler on the SAME
    connection whose transaction may already be ABORTED by whatever raised. Postgres refuses
    every subsequent statement on an aborted transaction (InFailedSqlTransaction) until a
    ROLLBACK, so the original cc#524 code's own failure-recording call was itself silently
    swallowed by the caller's `except Exception: pass` around it -- explaining why
    ops_metrics_failures had ZERO rows despite 267 real errors in the 18-Jul backfill run.
    Rolling back here first makes this call succeed regardless of the connection's prior state."""
    try:
        cur.connection.rollback()
    except Exception:
        pass
    cur.execute("""INSERT INTO ops_metrics_failures (symbol, consecutive_failures, last_failure_at, last_error)
                   VALUES (%s, 1, NOW(), %s)
                   ON CONFLICT (symbol) DO UPDATE SET
                     consecutive_failures = ops_metrics_failures.consecutive_failures + 1,
                     last_failure_at = NOW(), last_error = EXCLUDED.last_error
                   RETURNING consecutive_failures""", (symbol, (error or "")[:300] if error else None))
    n = cur.fetchone()[0]
    if n >= 2:
        _oplog(cur, "OPS_METRICS_PERSISTENT_FAILURE",
               {"symbol": symbol, "consecutive_failures": n, "last_error": (error or "")[:300] if error else None,
                "message": f"{symbol} has failed ops-metrics extraction {n} runs in a row"})


def _clear_failure(cur, symbol):
    cur.execute("DELETE FROM ops_metrics_failures WHERE symbol=%s", (symbol,))


def _log_token_usage(cur, symbol, model_used, usage_list):
    if not usage_list:
        return
    total_in = sum(u.get("input_tokens", 0) for u in usage_list)
    total_out = sum(u.get("output_tokens", 0) for u in usage_list)
    _oplog(cur, "OPS_METRICS_TOKEN_USAGE",
           {"symbol": symbol, "model_used": model_used, "calls": len(usage_list),
            "input_tokens": total_in, "output_tokens": total_out})


def log_token_rollup(conn=None):
    """cc#524: "log actual token spend per 100 companies to ops_log so the number is known,
    not guessed" -- sums the per-company OPS_METRICS_TOKEN_USAGE rows written since the last
    rollup and writes one summary row. Called by the backfill loop every 100 companies."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), COALESCE(SUM((details->>'input_tokens')::bigint),0),
                                  COALESCE(SUM((details->>'output_tokens')::bigint),0),
                                  COALESCE(SUM(((details->>'model_used')='sonnet')::int),0)
                           FROM ops_log WHERE title='OPS_METRICS_TOKEN_USAGE'
                             AND session_ts > NOW() - INTERVAL '1 day'""")
            n, tin, tout, sonnet_n = cur.fetchone()
            rollup = {"companies": n, "input_tokens": tin, "output_tokens": tout,
                      "sonnet_escalations": sonnet_n}
            _oplog(cur, "OPS_METRICS_TOKEN_ROLLUP", rollup)
            conn.commit()
        return rollup
    finally:
        if own:
            conn.close()


def refresh_sector_trends(cur, sector):
    """cc#524: per sector per metric, median across reporting companies per quarter. Cheap
    SQL-only aggregation over data already in sector_ops_metrics -- no new scraping/LLM calls."""
    cur.execute("""SELECT metric_name, quarter, metric_value FROM sector_ops_metrics
                   WHERE sector=%s AND metric_value IS NOT NULL""", (sector,))
    by_metric_q = {}
    for name, quarter, val in cur.fetchall():
        by_metric_q.setdefault((name, quarter), []).append(float(val))
    for (name, quarter), vals in by_metric_q.items():
        median = round(statistics.median(vals), 4)
        cur.execute("""INSERT INTO sector_ops_trends (sector, metric_name, quarter, median_value, n_companies, computed_at)
                       VALUES (%s,%s,%s,%s,%s,NOW())
                       ON CONFLICT (sector, metric_name, quarter) DO UPDATE SET
                         median_value=EXCLUDED.median_value, n_companies=EXCLUDED.n_companies, computed_at=NOW()""",
                    (sector, name, quarter, median, len(vals)))
    return len(by_metric_q)


_GUIDANCE_PROMPT = """You are comparing a company's forward guidance from one earnings call
against what actually happened the following quarter.

GUIDANCE GIVEN (from the {quarter_guided} call):
{guidance_text}

WHAT ACTUALLY HAPPENED ({quarter_actual}, from that quarter's own concall + reported metrics):
{actual_text}

For EACH distinct guided item (a number, a range, or a specific commitment), determine whether
it was MET, MISSED, or MIXED (partially met) based ONLY on the actual-outcome text above. If the
guidance text contains no specific, checkable items, return an empty items list -- never invent
a guided item that wasn't actually stated.

Return ONLY valid JSON, no markdown fences:
{{"items": [{{"item_text": "<short description of the guided item>",
              "guided_quote": "<short quote of the guidance, <=200 chars>",
              "actual_outcome": "<what actually happened, one sentence>",
              "actual_quote": "<short quote supporting that, <=200 chars>",
              "status": "MET|MISSED|MIXED"}}, ...]}}"""


def refresh_guidance_tracker_for_symbol(cur, symbol):
    """cc#524 item c (the "highest value" analytics piece): quarter N's guidance vs quarter
    N+1's actuals -> a management-credibility record. Needs >=2 accumulated quarters with a
    non-empty guidance string; no-ops otherwise (nothing fabricated when there's nothing to
    compare)."""
    cur.execute("""SELECT quarter, guidance, summary, key_metrics FROM concall_summaries
                   WHERE symbol=%s ORDER BY computed_at ASC""", (symbol,))
    rows = cur.fetchall()
    if len(rows) < 2:
        return 0
    q_guided, guidance_text, _s1, _k1 = rows[-2]
    q_actual, _g2, actual_summary, actual_metrics = rows[-1]
    if not guidance_text or guidance_text.strip().lower() in ("no explicit guidance given", ""):
        return 0
    actual_text = (actual_summary or "") + "\n\nReported metrics: " + json.dumps(actual_metrics or {})
    prompt = _GUIDANCE_PROMPT.format(quarter_guided=q_guided, guidance_text=guidance_text[:2000],
                                      quarter_actual=q_actual, actual_text=actual_text[:4000])
    parsed, usage = _anthropic_call(prompt, max_tokens=1200, model=HAIKU_MODEL)
    if usage:
        _log_token_usage(cur, symbol, "haiku", [usage])
    items = (parsed or {}).get("items") or []
    for it in items:
        cur.execute("""INSERT INTO guidance_tracker
            (symbol, quarter_guided, quarter_actual, item_text, guided_quote, actual_outcome, actual_quote, status, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (symbol, quarter_guided, item_text) DO UPDATE SET
              quarter_actual=EXCLUDED.quarter_actual, guided_quote=EXCLUDED.guided_quote,
              actual_outcome=EXCLUDED.actual_outcome, actual_quote=EXCLUDED.actual_quote,
              status=EXCLUDED.status, computed_at=NOW()""",
                    (symbol, q_guided, q_actual, (it.get("item_text") or "")[:200],
                     (it.get("guided_quote") or "")[:300], (it.get("actual_outcome") or "")[:500],
                     (it.get("actual_quote") or "")[:300], it.get("status")))
    return len(items)


def compute_divergence_flags_for_sector(cur, sector):
    """cc#524 item e: a company's metric moving OPPOSITE its sector median trend for >=2
    consecutive quarters. Pure SQL-derived comparison of already-computed sector_ops_trends
    against each company's own sector_ops_metrics series -- no LLM calls."""
    cur.execute("""SELECT metric_name, quarter, median_value FROM sector_ops_trends
                   WHERE sector=%s ORDER BY metric_name, quarter""", (sector,))
    sector_series = {}
    for name, quarter, med in cur.fetchall():
        sector_series.setdefault(name, []).append((quarter, float(med) if med is not None else None))

    cur.execute("""SELECT symbol, metric_name, quarter, metric_value FROM sector_ops_metrics
                   WHERE sector=%s AND metric_value IS NOT NULL
                   ORDER BY symbol, metric_name, computed_at""", (sector,))
    company_series = {}
    for sym, name, quarter, val in cur.fetchall():
        company_series.setdefault((sym, name), []).append((quarter, float(val)))

    n_flags = 0
    for (sym, name), points in company_series.items():
        sec_pts = {q: v for q, v in sector_series.get(name, [])}
        if len(points) < 3:
            continue
        # direction per consecutive pair, most recent first
        streak = 0
        for i in range(len(points) - 1, 1, -1):
            q_cur, v_cur = points[i]
            q_prev, v_prev = points[i - 1]
            sec_cur, sec_prev = sec_pts.get(q_cur), sec_pts.get(q_prev)
            if sec_cur is None or sec_prev is None:
                break
            co_dir = "up" if v_cur > v_prev else ("down" if v_cur < v_prev else "flat")
            se_dir = "up" if sec_cur > sec_prev else ("down" if sec_cur < sec_prev else "flat")
            if co_dir == "flat" or se_dir == "flat" or co_dir == se_dir:
                break
            streak += 1
            if streak == 1:
                latest_q, latest_co_dir, latest_se_dir = q_cur, co_dir, se_dir
        if streak >= 2:
            label = (name or "").replace("_", " ")
            flag_text = f"{label} {latest_co_dir} vs sector {latest_se_dir} for {streak} consecutive quarters"
            cur.execute("""INSERT INTO ops_divergence_flags
                (symbol, metric_name, quarter, company_direction, sector_direction, streak_quarters, flag_text, computed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (symbol, metric_name, quarter) DO UPDATE SET
                  company_direction=EXCLUDED.company_direction, sector_direction=EXCLUDED.sector_direction,
                  streak_quarters=EXCLUDED.streak_quarters, flag_text=EXCLUDED.flag_text, computed_at=NOW()""",
                        (sym, name, latest_q, latest_co_dir, latest_se_dir, streak, flag_text))
            n_flags += 1
    return n_flags


def post_extraction_chain(cur, symbol, sector):
    """cc#524 spec item 5: run after every extraction, in order -- sector trends refresh ->
    guidance-tracker refresh -> divergence flags recompute. Cheap (SQL aggregation + at most
    one small Haiku call), safe to call after every single company."""
    try:
        refresh_sector_trends(cur, sector)
        refresh_guidance_tracker_for_symbol(cur, symbol)
        compute_divergence_flags_for_sector(cur, sector)
    except Exception as e:
        log.warning(f"post_extraction_chain failed for {symbol}/{sector}: {e}")


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
                              som.confidence, som.source_1, som.source_2, som.discrepancy_pct, som.updated_at
                       FROM sector_ops_metrics som JOIN gvm_scores g ON g.symbol = som.symbol
                       WHERE g.segment = %s
                       ORDER BY som.updated_at ASC""", (segment,))
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

        # cc#524 item a: QoQ/YoY deltas from the accumulated quarters -- each quarter appears
        # at most once per (symbol, metric) (UNIQUE constraint), appended chronologically, so
        # self_entries[-2] is QoQ and self_entries[-5] is YoY (4 quarters back) once they exist.
        def _delta(cur_v, prev_v):
            if cur_v is None or prev_v is None:
                return None, None
            d = round(cur_v - prev_v, 4)
            dp = round((cur_v - prev_v) / abs(prev_v) * 100, 2) if prev_v != 0 else None
            return d, dp
        qoq, qoq_pct = (_delta(self_entries[-1]["value"], self_entries[-2]["value"])
                        if len(self_entries) >= 2 else (None, None))
        yoy, yoy_pct = (_delta(self_entries[-1]["value"], self_entries[-5]["value"])
                        if len(self_entries) >= 5 else (None, None))
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
            "qoq_delta": qoq, "qoq_delta_pct": qoq_pct, "yoy_delta": yoy, "yoy_delta_pct": yoy_pct,
            "peer_median": peer_median, "peer_n": peer_n, "rank": rank,
            "best": {"symbol": sorted_peers[0]["symbol"], "value": sorted_peers[0]["value"]} if sorted_peers else None,
            "worst": {"symbol": sorted_peers[-1]["symbol"], "value": sorted_peers[-1]["value"]} if sorted_peers else None,
        })

    n_reported = len({e["symbol"] for entries in by_metric.values() for e in entries
                       if e["quarter"] == latest_quarter_seen}) if latest_quarter_seen else 0

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT metric_name, quarter, company_direction, sector_direction, streak_quarters, flag_text
                       FROM ops_divergence_flags WHERE symbol=%s ORDER BY computed_at DESC LIMIT 5""", (sym,))
        divergence = [{"metric_name": r[0], "quarter": r[1], "company_direction": r[2],
                       "sector_direction": r[3], "streak_quarters": r[4], "flag_text": r[5]}
                      for r in cur.fetchall()]
        cur.execute("""SELECT status, COUNT(*) FROM guidance_tracker WHERE symbol=%s GROUP BY status""", (sym,))
        gt_counts = {r[0]: r[1] for r in cur.fetchall()}

    return {"symbol": sym, "sector": sector, "segment": segment, "metrics": metrics_out,
            "quarter": latest_quarter_seen, "quarters_reported": n_reported,
            "divergence_flags": divergence,
            "guidance_track_record": gt_counts if gt_counts else None}


@router.get("/api/ops_metrics/guidance/{symbol}")
def get_guidance_tracker(symbol: str):
    sym = symbol.strip().upper()
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT quarter_guided, quarter_actual, item_text, guided_quote,
                              actual_outcome, actual_quote, status, computed_at
                       FROM guidance_tracker WHERE symbol=%s ORDER BY quarter_guided DESC, id ASC""", (sym,))
        rows = [{"quarter_guided": r[0], "quarter_actual": r[1], "item_text": r[2],
                 "guided_quote": r[3], "actual_outcome": r[4], "actual_quote": r[5],
                 "status": r[6], "computed_at": str(r[7]) if r[7] else None} for r in cur.fetchall()]
        met = sum(1 for r in rows if r["status"] == "MET")
    return {"symbol": sym, "count": len(rows), "met": met, "items": rows,
            "track_record": f"{met}/{len(rows)} met" if rows else None}


@router.get("/api/ops_metrics/sector_trend")
def get_sector_trend(sector: str, metric_name: str):
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        cur.execute("""SELECT quarter, median_value, n_companies, computed_at FROM sector_ops_trends
                       WHERE sector=%s AND metric_name=%s ORDER BY computed_at ASC""", (sector, metric_name))
        rows = [{"quarter": r[0], "median_value": float(r[1]) if r[1] is not None else None,
                 "n_companies": r[2]} for r in cur.fetchall()]
    return {"sector": sector, "metric_name": metric_name, "trend": rows}


@router.get("/api/ops_metrics/segment_trends")
def get_segment_trends(segment: str):
    """cc#524 item d ("Surface on the Sector page brief card"): SectorBrief operates on the
    fine-grained gvm_scores.segment (129 values), not this module's coarse 22-sector taxonomy --
    this endpoint does the segment->sector mapping server-side and returns all CORE-tier metric
    trends for that sector in one call, so the UI doesn't need to know about _infer_sector."""
    sector = _infer_sector(segment)
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        registry = [m for m in _registry_for_sector(cur, sector) if m["tier"] == "core"]
        out = []
        for m in registry:
            cur.execute("""SELECT quarter, median_value, n_companies FROM sector_ops_trends
                           WHERE sector=%s AND metric_name=%s ORDER BY computed_at ASC""",
                        (sector, m["metric_name"]))
            trend = [{"quarter": r[0], "median_value": float(r[1]) if r[1] is not None else None,
                      "n_companies": r[2]} for r in cur.fetchall()]
            if trend:
                out.append({"metric_name": m["metric_name"], "display_name": m["display_name"],
                            "unit": m["unit"], "direction": m["direction"], "trend": trend})
    return {"segment": segment, "sector": sector, "metrics": out}


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


@router.post("/api/admin/ops_metrics/run_company_depth/{symbol}")
def admin_run_company_depth(symbol: str, token: str = ""):
    """cc#524: manual trigger for the 4-quarter depth path (vs the single-latest-quarter
    admin_run_company above) -- used to test/seed the acceptance companies directly."""
    _check_admin(token)
    return run_company_depth(symbol.strip().upper())


@router.post("/api/admin/ops_metrics/run_backfill")
def admin_arm_backfill(token: str = ""):
    _check_admin(token)
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        _cfg_set(cur, RUN_FLAG_KEY, "pending")
        conn.commit()
    return {"armed": True, "flag": f"{RUN_FLAG_KEY}=pending"}


@router.post("/api/admin/ops_metrics/run_text_fetch/{symbol}")
def admin_run_text_fetch(symbol: str, token: str = ""):
    """cc#527: manual single-symbol trigger for the fetch-only phase."""
    _check_admin(token)
    return run_company_text_fetch(symbol.strip().upper())


@router.post("/api/admin/ops_metrics/arm_text_fetch")
def admin_arm_text_fetch(token: str = ""):
    _check_admin(token)
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        _cfg_set(cur, TEXT_FETCH_RUN_FLAG_KEY, "pending")
        conn.commit()
    return {"armed": True, "flag": f"{TEXT_FETCH_RUN_FLAG_KEY}=pending"}


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
        cur.execute("SELECT COUNT(DISTINCT symbol) FROM doc_texts WHERE text_content IS NOT NULL")
        n_text_syms = cur.fetchone()[0]
        cur.execute("SELECT value FROM app_config WHERE key=%s", (TEXT_FETCH_RUN_FLAG_KEY,))
        r = cur.fetchone()
        text_fetch_flag = r[0] if r else None
        cur.execute("SELECT value FROM app_config WHERE key=%s", (TEXT_FETCH_CURSOR_KEY,))
        r = cur.fetchone()
        text_fetch_cursor = r[0] if r else None
    return {"registry_rows": n_registry, "doc_registry_symbols": n_doc_syms, "docs_found": n_docs,
            "companies_with_metrics": n_metric_syms, "companies_with_concall": n_concalls,
            "backfill_run_flag": run_flag, "backfill_cursor": cursor,
            "companies_with_stored_text": n_text_syms,
            "text_fetch_run_flag": text_fetch_flag, "text_fetch_cursor": text_fetch_cursor}


_OPS_METRICS_TABLES = ["sector_kpi_registry", "sector_ops_metrics", "doc_registry",
                        "concall_summaries", "sector_ops_trends", "guidance_tracker",
                        "ops_divergence_flags", "ops_metrics_failures", "ops_metrics_t1_queue",
                        "doc_texts"]


def measure_storage_delta(conn=None):
    """cc#524 item f: total on-disk size of every ops-metrics table, logged to ops_log so the
    real number is known rather than the spec's <25MB/yr estimate being taken on faith."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            sizes = {}
            total = 0
            for t in _OPS_METRICS_TABLES:
                cur.execute("SELECT pg_total_relation_size(%s)", (t,))
                r = cur.fetchone()
                sz = r[0] if r and r[0] is not None else 0
                sizes[t] = sz
                total += sz
            result = {"tables": sizes, "total_bytes": total, "total_mb": round(total / 1048576, 2)}
            _oplog(cur, "OPS_METRICS_STORAGE_DELTA", result)
            conn.commit()
        return result
    finally:
        if own:
            conn.close()


@router.get("/api/admin/ops_metrics/storage")
def admin_storage():
    return measure_storage_delta()


# ── 8. QUARTERLY UPDATE FRAMEWORK (cc#524, OPS_METRICS_FRAMEWORK_V1) ───────────
# FINAL CADENCE CONSOLIDATION (the spec's own words: "supersedes any conflicting line above"):
#   1. T+1: one screener visit per reported company does EVERYTHING -- append-only fundamentals
#      re-scrape (Result Analysis card fresh next morning) + doc discovery/extraction (ops
#      metrics + concall summary) in the same visit.
#   2. Saturday 10:00 IST: SCOPED retry -- only companies with an earnings_calendar result date
#      whose T+1 run failed or found docs unpublished. No blind universe sweeps.
#   3. Companies with no stored result date: caught by 4 season-close bulk sweeps (Sep/Dec/Mar/
#      Jun) using a staleness predicate, not scraped speculatively during the season.
# fundamentals_history's own UPSERT (fundamentals_scraper._write_symbol, ON CONFLICT DO UPDATE
# keyed on symbol+section+period_label+consolidated) is ALREADY append-only-safe -- re-scraping
# the full page and upserting is idempotent, so no extra "only newer periods" filter is needed;
# unchanged periods just get re-written with the same values. gvm_page_extras.py (the Result
# Analysis card's data source) reads fundamentals_history live with no cache layer (grepped --
# no lru_cache/@cached anywhere in that file), so no cache-busting step is needed either.

def _t1_refresh_company(cur, symbol):
    """One screener visit, does everything for one company: fundamentals re-scrape (append-only
    upsert) + ops-metrics doc discovery/extraction. Returns True on any success. Note: this
    makes its own screener page fetch for fundamentals (fetch_company) separate from the one
    run_company below makes for Documents/Concalls -- two page visits per company, not one, since
    fundamentals_scraper's _fetch_soup and this module's own doc-discovery walk the same live
    page independently. Acceptable for now (2x THROTTLE cost, not 2x company count); a shared
    single-fetch path is a reasonable follow-up optimization, not a correctness issue."""
    fh_ok = False
    try:
        rows, cons = _fh_fetch_company(symbol)
        if rows:
            _fh_write_symbol(cur.connection, symbol, rows, cons)
            fh_ok = True
    except Exception as e:
        log.warning(f"_t1_refresh_company fundamentals re-scrape failed for {symbol}: {e}")
    r = run_company(symbol, force=True)
    return fh_ok or r.get("status") == "ok"


def run_t1_refresh(conn=None):
    """cc#524 item 1: companies whose earnings_calendar row transitioned to status='reported'
    yesterday (IST) get queued and refreshed T+1. Reads the SAME status='reported' signal
    admin_data.py's _earnings_lifecycle() already flips daily at 06:15 IST -- this just acts on
    it, doesn't duplicate the lifecycle logic."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            cur.execute("""SELECT DISTINCT UPPER(ticker), ex_date FROM earnings_calendar
                           WHERE status='reported' AND ex_date = (CURRENT_DATE - INTERVAL '1 day')::date
                             AND ticker IS NOT NULL""")
            due = cur.fetchall()
            for sym, ex_date in due:
                cur.execute("""INSERT INTO ops_metrics_t1_queue (symbol, ex_date, status)
                               VALUES (%s,%s,'pending') ON CONFLICT (symbol, ex_date) DO NOTHING""",
                            (sym, ex_date))
            conn.commit()
            cur.execute("""SELECT symbol, ex_date FROM ops_metrics_t1_queue WHERE status='pending'""")
            pending = cur.fetchall()

        ok = fail = 0
        for sym, ex_date in pending:
            with conn.cursor() as cur:
                try:
                    success = _t1_refresh_company(cur, sym)
                except Exception as e:
                    log.error(f"run_t1_refresh failed for {sym}: {e}")
                    success = False
                cur.execute("""UPDATE ops_metrics_t1_queue SET status=%s, processed_at=NOW()
                               WHERE symbol=%s AND ex_date=%s""",
                            ("done" if success else "failed", sym, ex_date))
                conn.commit()
            if success:
                ok += 1
            else:
                fail += 1
            time.sleep(THROTTLE)

        with conn.cursor() as cur:
            summary = {"due": len(pending), "ok": ok, "failed": fail}
            _oplog(cur, "OPS_METRICS_T1_RUN", summary)
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


def run_saturday_retry(conn=None):
    """cc#524 item 2: SCOPED retry, Saturday 10:00 IST -- only ops_metrics_t1_queue rows still
    'pending'/'failed' (a result-dated company whose T+1 run didn't complete cleanly). No blind
    universe sweep."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            cur.execute("""SELECT symbol, ex_date FROM ops_metrics_t1_queue
                           WHERE status IN ('pending','failed') ORDER BY ex_date DESC LIMIT 200""")
            scoped = cur.fetchall()

        ok = fail = 0
        for sym, ex_date in scoped:
            with conn.cursor() as cur:
                try:
                    success = _t1_refresh_company(cur, sym)
                except Exception as e:
                    log.error(f"run_saturday_retry failed for {sym}: {e}")
                    success = False
                cur.execute("""UPDATE ops_metrics_t1_queue SET status=%s, processed_at=NOW()
                               WHERE symbol=%s AND ex_date=%s""",
                            ("done" if success else "failed", sym, ex_date))
                conn.commit()
            ok += 1 if success else 0
            fail += 0 if success else 1
            time.sleep(THROTTLE)

        with conn.cursor() as cur:
            summary = {"scope": "calendar-dated failures only", "scoped_count": len(scoped),
                       "ok": ok, "failed": fail}
            _oplog(cur, "OPS_METRICS_SATURDAY_RETRY", summary)
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


_SEASON_STALE_DAYS = 120   # cc#524 item 3: "older than the just-ended reporting season" proxy --
                           # a fixed ~4-month staleness window on the latest stored fundamentals
                           # quarter, rather than hand-encoding 4 fiscal result-window date
                           # ranges (which would need re-verifying every year); reasonable given
                           # results seasons run roughly quarterly.


def run_season_sweep(conn=None, dry_run=False):
    """cc#524 item 3: quarterly bulk sweep (Sep/Dec/Mar/Jun) for companies with NO tracked
    earnings_calendar result date (mostly smallcaps) -- staleness predicate on the latest
    stored fundamentals_history quarter, one paced run over the ops-metrics universe."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            universe = _build_universe(cur)
            cur.execute("""SELECT symbol, MAX(period_end) FROM fundamentals_history
                           GROUP BY symbol""")
            latest_period = {r[0]: r[1] for r in cur.fetchall()}
            cutoff = date.today() - timedelta(days=_SEASON_STALE_DAYS)
            stale = [s for s in universe
                     if latest_period.get(s) is None or latest_period[s] < cutoff]

        if dry_run:
            with conn.cursor() as cur:
                _oplog(cur, "OPS_METRICS_SEASON_SWEEP_DRYRUN", {"stale_count": len(stale), "universe": len(universe)})
                conn.commit()
            return {"dry_run": True, "stale_count": len(stale), "universe": len(universe)}

        ok = fail = 0
        for sym in stale:
            with conn.cursor() as cur:
                try:
                    success = _t1_refresh_company(cur, sym)
                except Exception as e:
                    log.error(f"run_season_sweep failed for {sym}: {e}")
                    success = False
                conn.commit()
            ok += 1 if success else 0
            fail += 0 if success else 1
            time.sleep(THROTTLE)

        with conn.cursor() as cur:
            summary = {"stale_count": len(stale), "universe": len(universe), "ok": ok, "failed": fail}
            _oplog(cur, "OPS_METRICS_SEASON_SWEEP_DONE", summary)
            conn.commit()
        return summary
    finally:
        if own:
            conn.close()


@router.post("/api/admin/ops_metrics/run_t1")
def admin_run_t1(token: str = ""):
    _check_admin(token)
    return run_t1_refresh()


@router.post("/api/admin/ops_metrics/run_saturday_retry")
def admin_run_saturday_retry(token: str = ""):
    _check_admin(token)
    return run_saturday_retry()


@router.post("/api/admin/ops_metrics/run_season_sweep")
def admin_run_season_sweep(token: str = "", dry_run: bool = True):
    _check_admin(token)
    return run_season_sweep(dry_run=dry_run)

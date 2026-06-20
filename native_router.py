"""
Native Query Router — Zero token, pure Railway DB queries.
Column names verified against live DB schema 13-Jun-2026.

ARCHITECTURE:
  Layer 0a:  Trade Check v3.3 (string triggers + regex pattern)
  Layer 0:   Hardcoded commands (dashboard, mood, qb, pcr, health, qualified)
  Layer 0b:  Personal journal (open/closed)
  Layer 0c:  V8 paper book
  Layer 0d:  Daily digest
  Layer 0e:  Top gainers / losers
  Layer 0f:  Server time / market state ("what time"/"market open"/"market hours")
  Layer 0g:  Global indices (NEW 13-Jun-2026)
  Layer 1:   Grammar parser+executor (RANK/FILTER/SCREEN/LOOKUP/HISTORY/SECTOR_VIEW)
  Layer 1.5: Native intent classifier — pure Python, $0
             Order: explicit off-topic → classify → soft off-topic
  Layer 2:   Fallback hint → suggest Claude toggle

  query_log captures every query for native training analytics.

FIXES v10 (13-Jun-2026) — research-driven dictionary expansion (FIX Q):
  Sourced from 18 retail-investor research articles (Zerodha Varsity,
  Tickertape, Motilal Oswal, Bajaj Finserv, Univest, Britannica Money).
  METRIC_MAP +12 new metrics, all column names verified against live
  screener_raw schema (63 cols, 13-Jun-2026):
    pb / p/b / price to book  → "Price to book value"
    peg / peg ratio           → "PEG Ratio"
    roe / return on equity    → "Return on equity"
    eps                       → "EPS"
    eps growth                → "EPS growth 5Years"
    ev ebitda / evebitda      → EVEBITDA
    debt                      → "Debt"  (distinct from d/e → "Debt to equity")
    cfo pat / cash flow       → "Cfo by Pat"  (cash conversion quality)
    profit growth 3y          → profit_growth_3y
    qoq sales                 → qoq_sales_growth
    qoq profit                → qoq_profit_growth
  LOOKUP_TRIGGER_WORDS +16 judgment/research words:
    research, study, multibagger, gem, turnaround, oversold, moat,
    fundamentals, fundamentally, quality, bargain, prospects, outlook,
    valuation, intrinsic, fairvalue.
  Safe: RANK (top/best/highest) checked first; LOOKUP gate is
  "has_lookup_word and not has_metric_thresh" so metric+threshold queries
  still route to FILTER. New words mirrored into exec_lookup stop set and
  _parse_sector stopwords so they don't pollute company/sector matching.

FIXES v9 (13-Jun-2026) — CAPLIPOINT bug + research-verb expansion:
  P. Research verb forms added to LOOKUP_TRIGGER_WORDS: "analyse", "analyze",
     "evaluate", "assess", "review", "rate", "rating", "score", "view".
     Bug: "analyse caplin point" had no trigger word (noun "analysis" was
     present, verb "analyse" was not), fell to RANK, sector resolved to
     "caplin point", exec_rank found 0 rows on segment mismatch, fell through
     to off-topic help card. Now routes to LOOKUP → finds CAPLIPOINT.
     score/rate are SAFE: _parse_operation checks RANK first (top/best/highest)
     and the LOOKUP gate is "has_lookup_word and not has_metric_thresh", so
     "gvm score above 7" still routes to FILTER. Same words added to
     exec_lookup stop set so company-name search strips the verb cleanly.

FIXES v8 (13-Jun-2026) — Level-2 v2 polish:
  O. Variant action verb forms added: "trust", "held", "owning", "owned",
     "picked", "pick", "picks". Catches "can I trust RELIANCE long term",
     "can MARUTI be held for 1 year", "stocks I should be owning".
     Final form of action-verb routing — together with v7 catches all
     natural buy/sell/hold/own/pick question patterns.

FIXES v7 (13-Jun-2026) — Level-2 v2 100-query test:
  N. Action verbs + judgment words added to LOOKUP_TRIGGER_WORDS:
     buy/sell/hold/exit/enter/dump/trim/book/accumulate/recommend/worth/
     overvalued/undervalued/cheap/expensive. Catches "is SBIN a buy",
     "should I buy TCS", "sell my TCS", "hold ITC", "is HDFCBANK overvalued".
     Same words added to exec_lookup stop set + question-prefix words
     (should/would/could/trust/long/term/year/now/today) so company search
     finds the actual stock without verb pollution.
     Tradeoff: "strong buy stocks" now routes to LOOKUP (was RANK with
     verdict filter). Acceptable — RANK path was already returning
     NO_RESULT_MSG for that query in production (sector mismatch).

FIXES v6 (13-Jun-2026) — Level-2 100-query test:
  K. COMPARE op routed to LOOKUP (single-stock for now; multi-stock TODO).
     "TCS vs INFY" / "compare HDFC and ICICI" no longer fall through to
     off-topic redirect — surfaces primary stock instead.
  L. Added "news", "update", "latest", "happened" to LOOKUP_TRIGGER_WORDS.
     "any news on Adani" / "latest on Reliance" now route to LOOKUP.
  M. exec_lookup stop set expanded with compare/news/preposition noise
     (vs/versus/compare/peer/news/update/latest/any/on/to/with/from/at/in)
     so company search finds the actual stock cleanly.

FIXES v5 (13-Jun-2026) — final polish from Level-1 100-query test:
  J. Added "result" and "results" to LOOKUP_TRIGGER_WORDS. Docstring lists
     "result" but the set was missing it. "Bharti Airtel result" now routes
     to LOOKUP correctly.

FIXES v4 (13-Jun-2026) — regressions surfaced from v3 verification:
  H. _parse_metric uses word-boundary for short phrases (<=4 chars) — stops
     "pe" matching inside "recipe" or "horoscope" and ranking PE stocks.
  I. exec_lookup returns canonical NO_RESULT_MSG on no-match — so queries
     like "tell me a joke" fall through to Layer 1.5 off-topic redirect
     instead of returning "No stock found for 'joke'".

FIXES (13-Jun-2026 v3) — from 100-query test:
  A. LOOKUP_TRIGGER_WORDS added → _parse_operation now returns "LOOKUP" for
     "analysis/result/details/profile/takeaway/like" without ranking signal.
     execute_grammar's has_signal now includes "LOOKUP" in the op gate.
  B. resolve_sector() uses re.search(r"\\bALIAS\\b") for single-word aliases —
     prevents "it" matching inside "favorite/ITC/Bharti". Multi-word aliases
     keep substring match (they're unambiguous).
  C. native_intent split into is_off_topic_explicit + is_off_topic. Router
     order: explicit off-topic → intent classify → soft off-topic. Stops
     "whats the weather today" misrouting to v8_qualified on weak "today" score.
  D. Layer 0a accepts "evaluate X long", "check X long" via TRADE_CHECK_PATTERN.
  E. Layer 0g: global indices handler (was hint fallback before).
  F. Layer 0f extended: "what time", "market open", "market closed",
     "current time" all hit server_time handler.
  G. exec_lookup stop set expanded: "analysis", "report", "like", "stocks",
     "key", "top", "by", "my" — strips noise so company-name search finds the
     actual stock instead of false-matching unrelated words.
"""

import os
import re
import asyncio
import time
from datetime import datetime, time as dtime
from typing import Optional, Tuple, Dict, Any
import psycopg

from native_trade_check import native_trade_check
from native_intent import (classify as intent_classify,
                           is_off_topic, is_off_topic_explicit,
                           OFF_TOPIC_REDIRECT)

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── SECTOR ALIAS MAP ───────────────────────────────────────────────────────
SECTOR_ALIASES = {
    "tech": "IT - ", "it": "IT - ", "software": "IT - ",
    "technology": "IT - ", "infotech": "IT - ",
    "bank": "Banks", "banking": "Banks", "banks": "Banks",
    "psu bank": "PSU Banks", "private bank": "Private Banks",
    "small finance bank": "Small Finance Banks",
    "pharma": "Pharma", "drug": "Pharma", "drugs": "Pharma",
    "auto": "Auto", "automobile": "Auto", "automotive": "Auto",
    "fmcg": "FMCG", "consumer goods": "FMCG",
    "realty": "Realty", "real estate": "Realty", "property": "Realty",
    "power": "Power", "energy": "Power",
    "renewable": "Renewable Energy", "solar": "Solar",
    "defence": "Defence", "defense": "Defence",
    "cement": "Cement",
    "steel": "Steel", "metal": "Steel", "aluminium": "Aluminium", "metals": "Steel",
    "chemical": "Chemicals", "chemicals": "Chemicals",
    "specialty chemical": "Specialty Chemicals", "agro chemical": "Agro Chemicals",
    "insurance": "Insurance", "life insurance": "Life Insurance",
    "nbfc": "NBFC", "housing finance": "Housing Finance",
    "microfinance": "Microfinance", "msme": "MSME Finance",
    "infra": "Infrastructure", "infrastructure": "Infrastructure", "epc": "EPC",
    "telecom": "Telecom", "telco": "Telecom",
    "logistics": "Logistics",
    "hospital": "Hospitals", "hospitals": "Hospitals",
    "diagnostic": "Diagnostics", "diagnostics": "Diagnostics",
    "oil": "Oil", "refinery": "Refineries", "mining": "Mining",
    "fertilizer": "Fertilizers", "fertilizers": "Fertilizers",
    "retail": "Retail", "sugar": "Sugar",
    "textile": "Textiles", "textiles": "Textiles",
    "media": "Entertainment", "entertainment": "Entertainment",
    "hotel": "Hotels", "hospitality": "Hotels",
    "jewellery": "Gems", "gems": "Gems",
    "paint": "Paints", "paints": "Paints",
    "tyre": "Tyres", "tyres": "Tyres",
    "packaging": "Packaging", "paper": "Paper",
    "exchange": "Exchanges", "broking": "Broking",
    "shipping": "Shipping",
    "education": "Education",
    "restaurant": "Restaurants", "qsr": "QSR",
    "digital": "Digital", "ecommerce": "Digital",
}


def resolve_sector(raw: Optional[str]) -> Optional[str]:
    """
    Resolve investor shorthand to DB segment keyword.

    FIX B (13-Jun-2026 v3): Single-word aliases use word-boundary match.
    Prevents "it" inside "favorite/ITC/Bharti" → IT sector misfire.
    Multi-word aliases keep substring match (unambiguous).
    """
    if not raw:
        return raw
    rl = raw.lower().strip()
    for alias in sorted(SECTOR_ALIASES.keys(), key=len, reverse=True):
        if " " in alias:
            # Multi-word: substring is safe ("small finance bank" is unique)
            if alias in rl:
                return SECTOR_ALIASES[alias]
        else:
            # Single-word: enforce word boundary
            if re.search(r"\b" + re.escape(alias) + r"\b", rl):
                return SECTOR_ALIASES[alias]
    return raw


# ── GRAMMAR PARSER ───────────────────────────────────────────────────────────

METRIC_MAP = {
    "gvm":              ("g", "gvm_score",              "gvm"),
    "g score":          ("g", "g_score",                "gvm"),
    "growth score":     ("g", "g_score",                "gvm"),
    "v score":          ("g", "v_score",                "gvm"),
    "value score":      ("g", "v_score",                "gvm"),
    "m score":          ("g", "m_score",                "gvm"),
    "momentum score":   ("g", "m_score",                "gvm"),
    "growth":           ("g", "g_score",                "gvm"),
    "value":            ("g", "v_score",                "gvm"),
    "momentum":         ("g", "m_score",                "gvm"),
    "market cap":       ("g", "market_cap",             "gvm"),
    "mcap":             ("g", "market_cap",             "gvm"),
    "opm":              ("s", "opm",                    "screener"),
    "margin":           ("s", "opm",                    "screener"),
    "margins":          ("s", "opm",                    "screener"),
    "roce":             ("s", "roce",                   "screener"),
    "pe":               ("s", "pe",                     "screener"),
    "p/e":              ("s", "pe",                     "screener"),
    "promoter":         ("s", "\"Promoter holding\"",   "screener"),
    "promoter holding": ("s", "\"Promoter holding\"",   "screener"),
    "fii":              ("s", "fii_change",             "screener"),
    "fii change":       ("s", "fii_change",             "screener"),
    "dii":              ("s", "dii_change",             "screener"),
    "sales growth":     ("s", "sales_growth_5y",        "screener"),
    "revenue growth":   ("s", "sales_growth_5y",        "screener"),
    "profit growth":    ("s", "profit_growth_5y",       "screener"),
    "return 1y":        ("s", "return_1y",              "screener"),
    "1y return":        ("s", "return_1y",              "screener"),
    "return 3y":        ("s", "return_3y",              "screener"),
    "3y return":        ("s", "return_3y",              "screener"),
    "rsi":              ("s", "RSI",                    "screener"),
    "dma 50":           ("s", "dma_50",                 "screener"),
    "50 dma":           ("s", "dma_50",                 "screener"),
    "dma 200":          ("s", "dma_200",                "screener"),
    "200 dma":          ("s", "dma_200",                "screener"),
    "dividend":         ("s", "dividend_yield",         "screener"),
    "div yield":        ("s", "dividend_yield",         "screener"),
    "debt equity":      ("s", "\"Debt to equity\"",     "screener"),
    "d/e":              ("s", "\"Debt to equity\"",     "screener"),
    "interest coverage":("s", "interest_coverage",      "screener"),
    "int coverage":     ("s", "interest_coverage",      "screener"),
    # ── FIX Q (v10): research-driven additions, all columns verified vs
    # screener_raw live schema (13-Jun-2026). Longer phrases sort first in
    # _parse_metric, so "price to book" matches before bare "pe" etc.
    "price to book":    ("s", "\"Price to book value\"", "screener"),
    "p/b":              ("s", "\"Price to book value\"", "screener"),
    "pb":               ("s", "\"Price to book value\"", "screener"),
    "peg ratio":        ("s", "\"PEG Ratio\"",          "screener"),
    "peg":              ("s", "\"PEG Ratio\"",          "screener"),
    "return on equity": ("s", "\"Return on equity\"",   "screener"),
    "roe":              ("s", "\"Return on equity\"",   "screener"),
    "eps growth":       ("s", "\"EPS growth 5Years\"",  "screener"),
    "eps":              ("s", "\"EPS\"",                "screener"),
    "ev ebitda":        ("s", "EVEBITDA",               "screener"),
    "evebitda":         ("s", "EVEBITDA",               "screener"),
    "cash flow":        ("s", "\"Cfo by Pat\"",         "screener"),
    "cfo pat":          ("s", "\"Cfo by Pat\"",         "screener"),
    "cfo by pat":       ("s", "\"Cfo by Pat\"",         "screener"),
    "debt":             ("s", "\"Debt\"",               "screener"),
    "profit growth 3y": ("s", "profit_growth_3y",       "screener"),
    "qoq sales":        ("s", "qoq_sales_growth",       "screener"),
    "qoq sales growth": ("s", "qoq_sales_growth",       "screener"),
    "qoq profit":       ("s", "qoq_profit_growth",      "screener"),
    "qoq profit growth":("s", "qoq_profit_growth",      "screener"),
    "2d mom":           ("v", "mom_2d",                 "v8"),
    "2d momentum":      ("v", "mom_2d",                 "v8"),
    "mom 2d":           ("v", "mom_2d",                 "v8"),
    "day change":       ("v", "mom_2d",                 "v8"),
    "week return":      ("v", "week_return",            "v8"),
    "month return":     ("v", "month_return",           "v8"),
}

VM_ALIASES = {"vm", "vm score", "v m", "value momentum", "value and momentum"}

CAP_MAP = {
    "large cap": "large", "largecap": "large", "large": "large",
    "mid cap":   "mid",   "midcap":   "mid",   "mid":   "mid",
    "small cap": "small", "smallcap": "small", "small": "small",
    "micro cap": "micro", "microcap": "micro", "micro": "micro",
}

VERDICT_MAP = {
    "strong buy": "Excellent", "excellent": "Excellent",
    "buy": "Good", "good": "Good",
    "watch": "Average", "average": "Average", "accumulate": "Average",
    "exit": "Weak", "weak": "Weak", "avoid": "Weak",
}

BELOW_WORDS = {"below","under","less","fewer","<","<=","maximum","max","atmost"}

# FIX A (13-Jun-2026 v3): LOOKUP trigger words.
# When any of these appear in the query AND there's no metric+threshold combo,
# _parse_operation returns "LOOKUP" — routing to exec_lookup which finds a
# specific stock by name/symbol rather than ranking a list.
LOOKUP_TRIGGER_WORDS = {
    "about", "overview", "profile", "tell", "details", "info",
    "analysis", "report", "result", "results", "takeaway", "like",
    # FIX L (v6): news-style queries ("any news on X", "latest on X")
    "news", "update", "latest", "happened",
    # FIX N (v7): action verbs + judgment words. Catches "is SBIN a buy",
    # "should I buy TCS", "sell my TCS", "hold ITC", "is HDFCBANK overvalued",
    # "would you recommend INFY". Action verb + stock symbol → LOOKUP.
    "buy", "sell", "hold", "exit", "enter", "dump", "trim", "book",
    "accumulate", "recommend", "worth", "overvalued", "undervalued",
    "cheap", "expensive",
    # FIX O (v8): variant forms — "can I trust RELIANCE", "can MARUTI be held"
    "trust", "held", "owning", "owned", "picked", "pick",
    # FIX P (v9): research verb forms. "analyse caplin point", "evaluate INFY",
    # "review TCS", "rate SBIN", "score of HDFC". Noun "analysis" was present
    # but verb forms were not. score/rate SAFE: RANK checked first + LOOKUP
    # gate is "has_lookup_word and not has_metric_thresh" so metric+threshold
    # queries ("gvm score above 7") still route to FILTER.
    "analyse", "analyze", "evaluate", "assess", "review",
    "rate", "rating", "score", "view",
    # FIX Q (v10): research/judgment vocabulary from 18 retail-investor
    # articles. "is RELIANCE a multibagger", "TCS fundamentals", "INFY moat",
    # "is SBIN a bargain", "ITC prospects", "HDFC quality stock".
    "research", "study", "multibagger", "gem", "turnaround", "oversold",
    "moat", "fundamentals", "fundamentally", "quality", "bargain",
    "prospects", "outlook", "valuation", "intrinsic", "fairvalue",
}


def _parse_count(q: str, default: int = 10) -> int:
    m = re.search(r"top\s+(\d+)|(\d+)\s+stocks?", q)
    if m:
        return max(1, min(int(m.group(1) or m.group(2)), 50))
    return default


def _is_vm(q: str) -> bool:
    return any(alias in q for alias in VM_ALIASES)


def _parse_metric(q: str) -> Optional[Tuple]:
    """
    FIX H (v4): Short metric phrases (<=4 chars: pe, fii, dii, rsi, opm, gvm,
    d/e, mcap, pb, peg, roe, eps) require word-boundary match. Without this,
    "pe" substring-matches inside "recipe" / "horoscope" and the query routes
    to PE-ranked stocks. Multi-word or longer single-word phrases keep
    substring match (safe). Sorted by length desc so "price to book" /
    "eps growth" / "qoq sales growth" match before their short aliases.
    """
    for phrase in sorted(METRIC_MAP.keys(), key=len, reverse=True):
        if " " in phrase or len(phrase) > 4:
            # Multi-word or longer single-word: substring is safe.
            if phrase in q:
                return METRIC_MAP[phrase]
        else:
            # Short single-word (<=4 chars): word-boundary required.
            if re.search(r"\b" + re.escape(phrase) + r"\b", q):
                return METRIC_MAP[phrase]
    return None


def _parse_threshold(q: str) -> Optional[Tuple]:
    m = re.search(
        r"(above|over|more than|greater than|atleast|minimum|min|>=|>|"
        r"below|under|less than|fewer than|maximum|max|atmost|<=|<)\s*(\d+\.?\d*)", q)
    if not m:
        return None
    word, val = m.group(1).lower(), float(m.group(2))
    return ("below" if word in BELOW_WORDS else "above", val)


def _parse_sector(q: str) -> Optional[str]:
    stopwords = {
        "top","best","show","me","give","list","fetch","get","stocks","stock",
        "results","result","by","gvm","score","high","highest","in","of","for",
        "with","and","db","from","rank","ranked","a","an","please","what","whats",
        "are","is","companies","company","names","above","below","over","under",
        "than","greater","less","more","having","where","filter","find","screen",
        "scan","about","overview","profile","tell","details","info","sector",
        "segment","industry","cap","large","mid","small","micro","history",
        "trend","historical","past","last","return","returns","growth","value",
        "momentum","change","latest","current","today","now","good","high","vm",
        # FIX (v3): generic words that previously dragged "it" into sector match
        "time","like","analysis","report","takeaway","key",
        # FIX P (v9): research verbs as sector-noise stopwords
        "analyse","analyze","evaluate","assess","review","rate","rating","view",
        # FIX Q (v10): research/judgment words as sector-noise stopwords
        "research","study","multibagger","gem","turnaround","oversold","moat",
        "fundamentals","fundamentally","quality","bargain","prospects","outlook",
        "valuation","intrinsic","fairvalue",
    }
    cleaned = re.sub(r"top\s+\d+|\d+\s+stocks?", "", q)
    cleaned = re.sub(
        r"(above|below|over|under|greater than|less than|more than|fewer than|>=|<=|>|<)\s*\d+\.?\d*",
        "", cleaned)
    cleaned = re.sub(r"\d+\.?\d*", "", cleaned)
    metric_words = set()
    for k in METRIC_MAP:
        metric_words.update(k.split())
    words = [w.strip(".,?!") for w in cleaned.lower().split()
             if len(w.strip(".,?!")) >= 2
             and w.strip(".,?!") not in stopwords
             and w.strip(".,?!") not in metric_words]
    return " ".join(words).strip() if words else None


def _parse_cap(q: str) -> Optional[str]:
    for phrase in sorted(CAP_MAP.keys(), key=len, reverse=True):
        if phrase in q.lower():
            return CAP_MAP[phrase]
    return None


def _parse_verdict(q: str) -> Optional[str]:
    for phrase in sorted(VERDICT_MAP.keys(), key=len, reverse=True):
        if phrase in q.lower():
            return VERDICT_MAP[phrase]
    return None


def _parse_operation(q: str) -> str:
    """
    FIX A (v3): LOOKUP now reachable. When a trigger word is present and the
    query lacks a metric+threshold combo (which signals FILTER), return LOOKUP.
    LOOKUP must come BEFORE FILTER to keep "RELIANCE analysis" out of FILTER's path.
    """
    # RANK first (explicit "top/best/highest")
    if any(w in q.split() for w in {"top","best","highest","leaders","leading"}):
        return "RANK"
    # FIX K (v6): COMPARE → LOOKUP. Comparison ops have no dedicated executor;
    # routing to LOOKUP at least surfaces the primary stock instead of falling
    # through to off-topic redirect. Multi-stock comparison TODO.
    if any(w in q for w in ["vs ","versus","compare","peer"]):
        return "LOOKUP"
    if any(w in q for w in ["history","trend","historical","over time","past "]):
        return "HISTORY"
    if any(w in q for w in ["sector rating","sector view","sectors","segments"]):
        return "SECTOR_VIEW"
    # LOOKUP: trigger word present, no metric+threshold
    has_lookup_word = any(w in q for w in LOOKUP_TRIGGER_WORDS)
    has_metric_thresh = bool(_parse_metric(q)) and bool(_parse_threshold(q))
    if has_lookup_word and not has_metric_thresh:
        return "LOOKUP"
    if has_metric_thresh:
        return "FILTER"
    if re.search(r"\band\b", q) and _parse_metric(q):
        return "SCREEN"
    return "RANK"


def parse_query(q: str) -> Dict[str, Any]:
    q_lower = q.lower().strip()
    raw_sector = _parse_sector(q_lower)
    return {
        "operation": _parse_operation(q_lower),
        "count":     _parse_count(q_lower),
        "sector":    resolve_sector(raw_sector),
        "cap":       _parse_cap(q_lower),
        "verdict":   _parse_verdict(q_lower),
        "metric":    _parse_metric(q_lower),
        "threshold": _parse_threshold(q_lower),
        "vm":        _is_vm(q_lower),
        "raw":       q_lower,
    }


# ── GRAMMAR EXECUTOR ─────────────────────────────────────────────────────────

def fmt_table(headers: list, rows: list) -> str:
    if not rows:
        return "No data found."
    col_w = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
             for i, h in enumerate(headers)]
    sep  = "| " + " | ".join("-" * w for w in col_w) + " |"
    head = "| " + " | ".join(str(h).ljust(col_w[i]) for i, h in enumerate(headers)) + " |"
    body = "\n".join("| " + " | ".join(str(r[i]).ljust(col_w[i]) for i in range(len(headers))) + " |"
                     for r in rows)
    return f"{head}\n{sep}\n{body}"


def _f(val) -> float:
    return float(val) if val is not None else 0.0


def _build_from(join_type: str, needs_input: bool = False) -> str:
    base = "FROM gvm_scores g"
    if join_type == "screener":
        base += " JOIN screener_raw s ON g.symbol = s.nse_code"
    elif join_type == "v8":
        base += " JOIN v8_metrics v ON g.symbol = v.symbol AND v.score_date = CURRENT_DATE"
    if needs_input:
        base += " JOIN input_raw i ON g.symbol = i.nse_code"
    return base


def _sector_condition(sector: Optional[str]) -> Optional[str]:
    if not sector:
        return None
    if sector.endswith(" ") or sector.endswith("- "):
        return f"g.segment ILIKE '{sector}%'"
    return f"g.segment ILIKE '%{sector}%'"


def exec_rank(cur, slots: Dict) -> str:
    n, sector, cap, verdict = slots["count"], slots["sector"], slots["cap"], slots["verdict"]
    metric, threshold, vm = slots["metric"], slots["threshold"], slots["vm"]

    if vm:
        needs_input = cap is not None
        from_clause = _build_from("gvm", needs_input)
        conditions = ["1=1"]
        sc = _sector_condition(sector)
        if sc: conditions.append(sc)
        if cap: conditions.append(f"i.cap_category = '{cap}'")
        if verdict: conditions.append(f"g.verdict = '{verdict}'")
        where = " AND ".join(conditions)
        cur.execute(f"""
            SELECT g.symbol, g.company_name, g.segment,
                   ROUND(g.gvm_score::numeric,2),
                   ROUND(g.v_score::numeric,2),
                   ROUND(g.m_score::numeric,2)
            {from_clause} WHERE {where}
            ORDER BY (g.v_score + g.m_score) DESC NULLS LAST LIMIT {n}
        """)
        rows = cur.fetchall()
        if not rows: return "No stocks found. Try a broader query or toggle Claude ON."
        title_parts = [f"Top {len(rows)}"]
        if sector: title_parts.append(sector.strip('- ').title())
        if cap:    title_parts.append(cap.title())
        title_parts.append("by V + M Score")
        data = [(r[0], r[1][:22], r[2][:18] if r[2] else "",
                 f"{_f(r[3]):.2f}", f"{_f(r[4]):.2f}", f"{_f(r[5]):.2f}") for r in rows]
        return f"**{' '.join(title_parts)}**\n{fmt_table(['Symbol','Company','Segment','GVM','V Score','M Score'], data)}"

    if metric:
        alias, col, join_type = metric
        order_col = f"{alias}.{col}"
    else:
        alias, col, join_type = "g", "gvm_score", "gvm"
        order_col = "g.gvm_score"

    needs_input = cap is not None
    from_clause = _build_from(join_type, needs_input)
    conditions = ["1=1"]
    sc = _sector_condition(sector)
    if sc: conditions.append(sc)
    if cap: conditions.append(f"i.cap_category = '{cap}'")
    if verdict: conditions.append(f"g.verdict = '{verdict}'")
    if threshold:
        direction, val = threshold
        op = ">=" if direction == "above" else "<="
        conditions.append(f"{order_col} {op} {val}")

    where = " AND ".join(conditions)
    cur.execute(f"""
        SELECT g.symbol, g.company_name, g.segment,
               ROUND(g.gvm_score::numeric,2),
               ROUND({order_col}::numeric,2) as metric_val
        {from_clause} WHERE {where}
        ORDER BY {order_col} DESC NULLS LAST LIMIT {n}
    """)
    rows = cur.fetchall()
    if not rows: return "No stocks found. Try a broader query or toggle Claude ON."

    raw_label = col.replace('"','').replace('_',' ').title()
    metric_label = "2D Mom%" if col == "mom_2d" else raw_label
    title_parts = [f"Top {len(rows)}"]
    if sector:  title_parts.append(sector.strip('- ').title())
    if cap:     title_parts.append(cap.title())
    if verdict: title_parts.append(verdict)
    title_parts.append(f"by {metric_label}")

    data = [(r[0], r[1][:22], r[2][:18] if r[2] else "",
             f"{_f(r[3]):.2f}", f"{_f(r[4]):.2f}") for r in rows]
    return f"**{' '.join(title_parts)}**\n{fmt_table(['Symbol','Company','Segment','GVM',metric_label], data)}"


def exec_filter(cur, slots: Dict) -> str:
    return exec_rank(cur, slots)


def exec_screen(cur, slots: Dict) -> str:
    return exec_rank(cur, slots)


def exec_lookup(cur, slots: Dict) -> str:
    """
    FIX G (v3): expanded stop set so noise words ("analysis", "report",
    "like", "stocks", "key", "top", "by", "my") are stripped before company
    search. Prevents "key takeaway for ITC" from searching "key itc" and
    falsely matching a different "Key..." company.
    """
    raw = slots["raw"]
    stop = {
        "about","overview","profile","tell","what","whats","details","info","me",
        "the","give","show","for","is","a","an","of","gvm","score","result",
        "results","takeaway","and","can","you","please",
        # v3 additions:
        "analysis","report","like","stocks","stock","key","top","best","by",
        "my","mine","i","be","share","shares",
        # v6 additions: compare/news/preposition noise
        "vs","versus","compare","compared","comparison","peer","peers","or",
        "news","update","updates","latest","happened","happens","any",
        "on","to","with","from","at","in",
        # v7 additions (FIX N): action verbs + question-prefix words.
        # Strip these so "is SBIN a buy" searches "sbin" not "is sbin buy".
        "buy","sell","hold","exit","enter","dump","trim","book",
        "accumulate","recommend","worth","overvalued","undervalued",
        "cheap","expensive","should","would","could","trust","long","term",
        "year","years","now","today","tomorrow","next","next year",
        # v8 additions (FIX O): variant action verb forms
        "held","owning","owned","picked","pick","picks",
        # v9 additions (FIX P): research verb forms
        "analyse","analyze","evaluate","assess","review","rate","rating","view",
        # v10 additions (FIX Q): research/judgment vocabulary
        "research","study","multibagger","gem","turnaround","oversold","moat",
        "fundamentals","fundamentally","quality","bargain","prospects","outlook",
        "valuation","intrinsic","fairvalue",
    }
    words = [w.strip(".,?!") for w in raw.lower().split()
             if w.strip(".,?!") not in stop and len(w.strip(".,?!")) >= 2]
    company = " ".join(words).strip()
    if not company:
        return "Specify a company name. E.g. 'overview HDFC bank'"

    r = None
    for search in [company] + sorted(company.split(), key=len, reverse=True):
        if len(search) < 2: continue
        cur.execute("""
            SELECT g.symbol, g.company_name, g.segment,
                   ROUND(g.gvm_score::numeric,2), ROUND(g.g_score::numeric,2),
                   ROUND(g.v_score::numeric,2), ROUND(g.m_score::numeric,2),
                   g.verdict, g.punchline, i.overview, i.key_takeaway, i.result_analysis
            FROM gvm_scores g LEFT JOIN input_raw i ON g.symbol = i.nse_code
            WHERE UPPER(g.symbol) LIKE %s OR LOWER(g.company_name) LIKE %s LIMIT 1
        """, (f"%{search.upper()}%", f"%{search.lower()}%"))
        r = cur.fetchone()
        if r: break

    if not r:
        # FIX I (v4): return canonical NO_RESULT_MSG so the router falls
        # through to Layer 1.5 (off-topic / intent / hint). Previously
        # "tell me a joke" surfaced "No stock found for 'joke'" — now
        # the off-topic check fires correctly via the same fallthrough
        # the RANK path uses.
        return "No stocks found. Try a broader query or toggle Claude ON."

    parts = [f"**{r[1]} ({r[0]}) — {r[2]}**"]
    parts.append(f"GVM: {r[3]} | G: {r[4]} | V: {r[5]} | M: {r[6]} | {r[7]}")
    if r[8]:  parts.append(f"_{r[8]}_")
    if r[9]:  parts.append(f"\n**Overview:** {r[9][:400]}")
    if r[10]: parts.append(f"\n**Key Takeaway:** {r[10][:300]}")
    if r[11]: parts.append(f"\n**Result Analysis:** {r[11][:300]}")
    return "\n".join(parts)


def exec_history(cur, slots: Dict) -> str:
    raw = slots["raw"]
    stop = {"history","trend","historical","over","time","past","last","months",
            "weeks","days","gvm","score","for","of","show","me","the","a","an"}
    words = [w.strip(".,?!") for w in raw.lower().split()
             if w.strip(".,?!") not in stop and len(w.strip(".,?!")) >= 2]
    months = 6
    m = re.search(r"(\d+)\s*month", raw)
    if m: months = int(m.group(1))
    company = " ".join(words).strip()
    if not company:
        return "Specify a stock. E.g. 'GVM history HDFC bank'"

    r = None
    for search in [company] + sorted(company.split(), key=len, reverse=True):
        if len(search) < 2: continue
        cur.execute("""
            SELECT g.symbol, g.company_name FROM gvm_scores g
            WHERE UPPER(g.symbol) LIKE %s OR LOWER(g.company_name) LIKE %s LIMIT 1
        """, (f"%{search.upper()}%", f"%{search.lower()}%"))
        r = cur.fetchone()
        if r: break

    if not r: return f"Stock not found for '{company}'."
    symbol, name = r[0], r[1]
    cur.execute("""
        SELECT score_date, ROUND(gvm_score::numeric,2), ROUND(g_score::numeric,2),
               ROUND(v_score::numeric,2), ROUND(m_score::numeric,2), verdict
        FROM gvm_history WHERE symbol = %s
        ORDER BY score_date DESC LIMIT %s
    """, (symbol, months * 4))
    rows = cur.fetchall()
    if not rows: return f"No history found for {name} ({symbol})."
    data = [(str(r[0]), f"{_f(r[1]):.2f}", f"{_f(r[2]):.2f}",
             f"{_f(r[3]):.2f}", f"{_f(r[4]):.2f}", r[5] or "") for r in rows]
    return (f"**GVM History — {name} ({symbol})**\n"
            f"{fmt_table(['Date','GVM','G','V','M','Verdict'], data)}")


def exec_sector_view(cur, slots: Dict) -> str:
    n = slots["count"]
    cur.execute("""
        SELECT segment, stocks_count,
               ROUND(mcap_weighted_gvm::numeric,2), ROUND(weighted_g::numeric,2),
               ROUND(weighted_v::numeric,2), ROUND(weighted_m::numeric,2),
               verdict, top_stock
        FROM sector_ratings
        WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)
        ORDER BY mcap_weighted_gvm DESC LIMIT %s
    """, (n,))
    rows = cur.fetchall()
    if not rows: return "No sector ratings available."
    data = [(r[0][:25], r[1], f"{_f(r[2]):.2f}", f"{_f(r[3]):.2f}",
             f"{_f(r[4]):.2f}", f"{_f(r[5]):.2f}", r[6] or "", r[7] or "") for r in rows]
    return (f"**Sector Rankings (top {len(rows)} by GVM)**\n"
            f"{fmt_table(['Segment','Stocks','GVM','G','V','M','Verdict','Top Stock'], data)}")


def execute_grammar(query: str) -> Optional[str]:
    slots = parse_query(query)
    op    = slots["operation"]
    # FIX A (v3): LOOKUP added to has_signal so exec_lookup runs when op==LOOKUP.
    has_signal = (
        slots["metric"] or slots["sector"] or slots["cap"] or
        slots["verdict"] or slots["threshold"] or slots["vm"] or
        op in ("HISTORY", "SECTOR_VIEW", "LOOKUP")
    )
    if not has_signal:
        return None
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                if op == "RANK":        return exec_rank(cur, slots)
                if op == "FILTER":      return exec_filter(cur, slots)
                if op == "SCREEN":      return exec_screen(cur, slots)
                if op == "LOOKUP":      return exec_lookup(cur, slots)
                if op == "HISTORY":     return exec_history(cur, slots)
                if op == "SECTOR_VIEW": return exec_sector_view(cur, slots)
                return None
    except Exception as e:
        return f"DB error: {str(e)[:150]}\nToggle Claude ON for full access."


# ── QUERY LOGGER ─────────────────────────────────────────────────────────────

def _log_query(query: str, mode: str, operation: str, metric: str,
               sector: str, resolved: bool, latency_ms: int,
               tokens_in: int = 0, tokens_out: int = 0, cost: float = 0.0):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO query_log
                    (query_raw, mode, operation, metric, sector, resolved,
                     latency_ms, tokens_in, tokens_out, cost_usd)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (query[:500], mode, operation, metric, sector,
                      resolved, latency_ms, tokens_in, tokens_out, cost))
                conn.commit()
    except Exception:
        pass


# ── VIRTUAL DASHBOARD V8 ─────────────────────────────────────────────────────

def _vd_market_gate(cur) -> str:
    cur.execute("""
        SELECT COUNT(CASE WHEN close>open THEN 1 END),
               COUNT(CASE WHEN close<open THEN 1 END),
               ROUND(COUNT(CASE WHEN close>open THEN 1 END)::numeric/
                     NULLIF(COUNT(CASE WHEN close<open THEN 1 END),0),2),
               MAX(ts)
        FROM intraday_prices
        WHERE ts::date=CURRENT_DATE AND source IN ('fyers_eq','fyers')
          AND ts=(SELECT MAX(ts) FROM intraday_prices
                 WHERE ts::date=CURRENT_DATE AND source IN ('fyers_eq','fyers'))
    """)
    r = cur.fetchone()
    live = bool(r and r[0] and r[2])
    if live:
        adv,dec,adr_val,as_of = r[0],r[1],_f(r[2]),r[3]
        tag="LIVE"; time_str=as_of.strftime('%H:%M IST') if as_of else ""
    else:
        cur.execute("SELECT adr,advances,declines,price_date FROM adr_daily ORDER BY price_date DESC LIMIT 1")
        r2=cur.fetchone()
        if not r2: return "**1. Market Gate**\nNo ADR data."
        adr_val,adv,dec=_f(r2[0]),r2[1],r2[2]; tag=f"EOD {r2[3]}"; time_str=""
    gate="OPEN" if adr_val>=1.0 else "CLOSED"
    mood="Bullish" if adr_val>=2 else "Neutral" if adr_val>=0.8 else "Bearish"
    return (f"**1. Market Gate — {tag} {time_str}**\n"
            f"ADR: {adr_val:.2f} | Gate: {gate} | Mood: {mood}\n"
            f"Advances: {adv} | Declines: {dec} | "
            f"Buy slots: {5 if adr_val>=1.0 else 0} | Sell slots: {5 if adr_val<1.0 else 3}")


def _vd_qualified(cur) -> str:
    cur.execute("""
        SELECT basket,COUNT(*),string_agg(symbol,', ' ORDER BY gvm_score DESC)
        FROM v8_qualified WHERE signal_date=CURRENT_DATE GROUP BY basket ORDER BY basket
    """)
    rows=cur.fetchall()
    if not rows: return "**2. V8 Watchlist Today**\nNo candidates today."
    data=[(r[0],r[1],r[2] if r[1]<=5 else f"{r[1]} stocks") for r in rows]
    return f"**2. V8 Watchlist Today** _(passed filters — entry needs pivot + gate open)_\n{fmt_table(['Basket','Count','Symbols'],data)}"


def _vd_paper_summary(cur) -> str:
    cur.execute("""
        SELECT p.basket,COUNT(*),
               SUM(CASE WHEN p.side='LONG' THEN (c.cmp-p.entry_price)*p.qty
                        WHEN p.side='SHORT' THEN (p.entry_price-c.cmp)*p.qty ELSE 0 END)
        FROM v8_paper_positions p LEFT JOIN cmp_prices c ON p.symbol=c.symbol
        WHERE UPPER(p.status)='OPEN' GROUP BY p.basket ORDER BY p.basket
    """)
    rows=cur.fetchall()
    if not rows: return "**3. Paper Positions Summary**\nNo open positions."
    data=[(r[0],r[1],f"{_f(r[2]):+,.0f}") for r in rows]
    total=sum(_f(r[2]) for r in rows)
    return f"**3. Paper Positions Summary**\n{fmt_table(['Basket','Open','Unrealised P&L'],data)}\nTotal Unrealised: {total:+,.0f}"


def _vd_closed_performance(cur) -> str:
    cur.execute("""
        SELECT basket,COUNT(*),
               COUNT(*) FILTER (WHERE result IN ('TARGET','GAP_TARGET_EXIT')),
               SUM(pnl)
        FROM v8_paper_trades GROUP BY basket ORDER BY basket
    """)
    rows=cur.fetchall()
    if not rows: return "**4. Closed Performance**\nNo closed trades."
    data,tc,tw,tr_=[],0,0,0.0
    for basket,closed,wins,realised in rows:
        acc=round(wins/closed*100,1) if closed else 0.0
        data.append((basket,closed,wins,f"{acc}%",f"{_f(realised):+,.0f}"))
        tc+=closed; tw+=wins; tr_+=_f(realised)
    tot_acc=round(tw/tc*100,1) if tc else 0.0
    data.append(("TOTAL",tc,tw,f"{tot_acc}%",f"{tr_:+,.0f}"))
    return f"**4. Closed Performance**\n{fmt_table(['Basket','Closed','Wins','Acc%','Realised P&L'],data)}"


def _vd_open_detail(cur) -> str:
    cur.execute("""
        SELECT p.symbol,p.side,p.basket,p.entry_price,c.cmp,
               CASE WHEN p.side='LONG' THEN (c.cmp-p.entry_price)*p.qty
                    WHEN p.side='SHORT' THEN (p.entry_price-c.cmp)*p.qty ELSE 0 END,
               p.entry_ts
        FROM v8_paper_positions p LEFT JOIN cmp_prices c ON p.symbol=c.symbol
        WHERE UPPER(p.status)='OPEN' ORDER BY 6 DESC NULLS LAST LIMIT 15
    """)
    rows=cur.fetchall()
    if not rows: return "**5. Open Positions Detail**\nNo open positions."
    data=[(r[0],r[1],r[2],f"{_f(r[3]):.1f}",
           f"{_f(r[4]):.1f}" if r[4] else "—",
           f"{_f(r[5]):+,.0f}" if r[5] else "—",
           r[6].strftime('%d-%b %H:%M') if r[6] else "") for r in rows]
    return f"**5. Open Positions Detail**\n{fmt_table(['Symbol','Side','Basket','Entry','CMP','P&L','Entry'],data)}"


def _vd_top_signals(cur) -> str:
    cur.execute("""
        SELECT symbol,basket,gvm_score,mom_2d,week_return FROM v8_qualified
        WHERE signal_date=CURRENT_DATE ORDER BY gvm_score DESC LIMIT 3
    """)
    rows=cur.fetchall()
    if not rows: return "**6. Top 3 Candidates Now**\nNo candidates today."
    data=[(r[0],r[1],f"{_f(r[2]):.2f}",f"{_f(r[3]):+.2f}%",f"{_f(r[4]):+.2f}%") for r in rows]
    return f"**6. Top 3 Candidates Now**\n{fmt_table(['Symbol','Basket','GVM','2D Mom%','Week%'],data)}"


def _virtual_dashboard(cur) -> str:
    ts=datetime.now().strftime('%d-%b-%Y %H:%M IST')
    parts=[f"⚡ **VIRTUAL DASHBOARD V8 — {ts}**"]
    for b in [_vd_market_gate,_vd_qualified,_vd_paper_summary,
              _vd_closed_performance,_vd_open_detail,_vd_top_signals]:
        try:
            parts.append(b(cur))
        except Exception as e:
            try: cur.connection.rollback()
            except: pass
            parts.append(f"**{b.__name__.replace('_vd_','').replace('_',' ').title()}**\n(error: {str(e)[:80]})")
    return "\n\n".join(parts)


# ── FIX D (v3): Trade Check pattern ──────────────────────────────────────────
# Catches "evaluate X long", "check X short", "trade check X long/short", etc.
# Strict pattern: must end with "long" or "short" to avoid false positives.
TRADE_CHECK_PATTERN = re.compile(
    r"^(trade\s+check|evaluate|check)\s+([A-Za-z][A-Za-z0-9\-_]+)\s+(long|short)$",
    re.IGNORECASE
)

# ── FIX F (v3): Layer 0f extended triggers ───────────────────────────────────
TIME_TRIGGERS = ["server time", "ist time", "market hours",
                 "market open", "market closed",
                 "what time", "current time"]


# ── MAIN QUERY HANDLER ───────────────────────────────────────────────────────

def _query_sync(query: str, _depth: int = 0) -> str:
    """
    Main query handler. _depth guards Layer 1.5 recursion.
    """
    q = query.lower().strip()
    t0 = time.time()

    def log(mode, op, metric="", sector="", resolved=True):
        _log_query(query, mode, op, metric, sector, resolved,
                   int((time.time()-t0)*1000))

    # ── LAYER 0a: Trade Check ─────────────────────────────────────────────────
    trade_check_triggered = (
        any(k in q for k in ["trade check", "trade journal", "journal check",
                              "evaluate stock", "trade card"])
        or TRADE_CHECK_PATTERN.match(q)
    )
    if trade_check_triggered:
        result = native_trade_check(query)
        log("native", "trade_check_v33")
        return result

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:

            # ── LAYER 0: Hardcoded commands ───────────────────────────────────

            if "virtual dashboard" in q or "v8 dashboard" in q:
                result=_virtual_dashboard(cur); log("native","virtual_dashboard"); return result

            if any(k in q for k in ["market mood","mood","adr","gate","slots"]):
                cur.execute("""
                    SELECT COUNT(CASE WHEN close>open THEN 1 END),
                           COUNT(CASE WHEN close<open THEN 1 END),
                           COUNT(CASE WHEN close=open THEN 1 END),
                           ROUND(COUNT(CASE WHEN close>open THEN 1 END)::numeric/
                                 NULLIF(COUNT(CASE WHEN close<open THEN 1 END),0),2),
                           MAX(ts)
                    FROM intraday_prices
                    WHERE ts::date=CURRENT_DATE AND source IN ('fyers_eq','fyers')
                      AND ts=(SELECT MAX(ts) FROM intraday_prices
                              WHERE ts::date=CURRENT_DATE AND source IN ('fyers_eq','fyers'))
                """)
                r=cur.fetchone()
                if not r or r[0]==0:
                    cur.execute("SELECT price_date,adr,advances,declines,computed_at FROM adr_daily ORDER BY price_date DESC LIMIT 1")
                    r2=cur.fetchone()
                    if r2:
                        result=(f"**Market Mood — {r2[0]} (EOD)**\n"
                                f"ADR: {_f(r2[1]):.2f} | Advances: {r2[2]} | Declines: {r2[3]}\n"
                                f"Updated: {r2[4].strftime('%d-%b %H:%M IST')}")
                        log("native","market_mood","adr"); return result
                    log("native","market_mood","","",False); return "No market mood data."
                adv,dec,unch,adr,as_of=r[0],r[1],r[2],r[3],r[4]
                adr_val=_f(adr)
                mood="Bullish" if adr_val>=2 else "Neutral" if adr_val>=0.8 else "Bearish"
                time_str=as_of.strftime('%H:%M IST') if as_of else "N/A"
                result=(f"**Market Mood — {datetime.now().strftime('%d-%b')} {time_str} (LIVE)**\n"
                        f"ADR: {adr_val:.2f} | {mood}\n"
                        f"Advances: {adv} | Declines: {dec} | Unchanged: {unch}")
                log("native","market_mood","adr"); return result

            if any(k in q for k in ["qb","quant basket","qb summary"]):
                cur.execute("""
                    SELECT basket_name,COUNT(*),SUM(pnl),SUM(current_value),
                           ROUND(AVG(pnl_pct)::numeric,2)
                    FROM quant_paper_positions WHERE status='open'
                    GROUP BY basket_name ORDER BY basket_name
                """)
                rows=cur.fetchall()
                if rows:
                    data=[(r[0],r[1],f"Rs{_f(r[2]):,.0f}",f"Rs{_f(r[3]):,.0f}",f"{_f(r[4])}%") for r in rows]
                    total=sum(_f(r[2]) for r in rows)
                    result=f"**QB Summary**\n{fmt_table(['Basket','Pos','PnL','Value','Avg%'],data)}\nTotal PnL: Rs {total:,.0f}"
                    log("native","qb_summary"); return result
                log("native","qb_summary","","",False); return "No active QB positions."

            if any(k in q for k in ["pcr","put call"]):
                cur.execute("SELECT underlying,pcr,put_oi,call_oi,computed_at FROM pcr_daily ORDER BY computed_at DESC LIMIT 2")
                rows=cur.fetchall()
                if rows:
                    lines=[f"**PCR — {rows[0][4].strftime('%d-%b %H:%M')}**"]
                    for r in rows: lines.append(f"{r[0]}: PCR {_f(r[1]):.3f} | Put OI {r[2]:,} | Call OI {r[3]:,}")
                    log("native","pcr","pcr"); return "\n".join(lines)
                log("native","pcr","","",False); return "No PCR data."

            if any(k in q for k in ["health","system status"]):
                cur.execute("SELECT COUNT(*) FROM raw_prices WHERE price_date=(SELECT MAX(price_date) FROM raw_prices)")
                rp=cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM gvm_scores"); gvm_cnt=cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM intraday_prices WHERE ts::date=CURRENT_DATE"); intr=cur.fetchone()[0]
                cur.execute("SELECT MAX(price_date) FROM raw_prices"); latest=cur.fetchone()[0]
                result=(f"**System Health — {datetime.now().strftime('%H:%M IST')}**\n"
                        f"raw_prices latest: {latest} ({rp:,} symbols)\n"
                        f"GVM scored: {gvm_cnt:,} stocks | Intraday bars today: {intr:,}\nRailway DB: OK")
                log("native","health"); return result

            if any(k in q for k in ["qualified","watchlist","v8 watchlist","v8 signal","v8 qualified"]):
                cur.execute("""
                    SELECT symbol,basket,gvm_score,cmp,mom_2d,signal_ts
                    FROM v8_qualified WHERE signal_date=CURRENT_DATE ORDER BY gvm_score DESC LIMIT 20
                """)
                rows=cur.fetchall()
                if rows:
                    data=[(r[0],r[1],"LONG" if "buy" in r[1].lower() else "SHORT",
                           f"{_f(r[2]):.1f}",f"{_f(r[3]):.1f}",f"{_f(r[4]):+.2f}%") for r in rows]
                    result=(f"**V8 Watchlist Today ({len(rows)} candidates)**\n"
                            f"_(Entry needs pivot confirmation + gate open)_\n"
                            f"{fmt_table(['Symbol','Basket','Side','GVM','CMP','2D Mom%'],data)}")
                    log("native","v8_watchlist"); return result
                log("native","v8_watchlist","","",False); return "No V8 candidates today."

            # ── LAYER 0b: Personal Journal ────────────────────────────────────
            if any(k in q for k in ["personal journal","my journal","my open trades",
                                    "my closed trades","journal closed","journal open",
                                    "journal pnl","journal stats","my trades"]):
                want_open = "open" in q
                if want_open:
                    cur.execute("""
                        SELECT trade_date, symbol, direction, qty,
                               entry_price, sl, target, holding_days, v8_basket
                        FROM personal_journal WHERE exit_price IS NULL
                        ORDER BY trade_date DESC, id DESC LIMIT 30
                    """)
                    rows = cur.fetchall()
                    if not rows:
                        log("native","journal_open","","",False)
                        return "**My Journal — Open Trades**\nNo open trades. Add via: 'add SBIN 1 qty to journal long'"
                    data = [(str(r[0]),r[1],r[2],r[3],f"{_f(r[4]):.1f}",
                             f"{_f(r[5]):.1f}" if r[5] else "—",
                             f"{_f(r[6]):.1f}" if r[6] else "—",
                             r[7] or "—", r[8] or "—") for r in rows]
                    log("native","journal_open")
                    return (f"**My Journal — Open Trades ({len(rows)})**\n"
                            f"{fmt_table(['Date','Symbol','Side','Qty','Entry','SL','Target','Days','Basket'], data)}")

                cur.execute("""
                    SELECT trade_date, symbol, direction, qty,
                           entry_price, exit_price, pnl, result, holding_days
                    FROM personal_journal WHERE exit_price IS NOT NULL
                    ORDER BY trade_date DESC, id DESC LIMIT 30
                """)
                rows = cur.fetchall()
                if not rows:
                    log("native","journal_closed","","",False)
                    return "**My Journal — Closed Trades**\nNo closed trades yet."
                data = [(str(r[0]),r[1],r[2],r[3],f"{_f(r[4]):.1f}",
                         f"{_f(r[5]):.1f}",f"{_f(r[6]):+,.0f}",
                         r[7] or "—",r[8] or "—") for r in rows]
                wins = sum(1 for r in rows if _f(r[6])>0)
                total_pnl = sum(_f(r[6]) for r in rows)
                acc = round(wins/len(rows)*100,1) if rows else 0.0
                log("native","journal_closed")
                return (f"**My Journal — Closed Trades ({len(rows)})**\n"
                        f"Total P&L: {total_pnl:+,.0f} · Wins: {wins}/{len(rows)} ({acc}%)\n\n"
                        f"{fmt_table(['Date','Symbol','Side','Qty','Entry','Exit','P&L','Result','Days'], data)}")

            # ── LAYER 0c: V8 Paper Book ───────────────────────────────────────
            if (("v8 paper" in q) or ("paper open" in q) or ("paper closed" in q)
                or ("paper book" in q) or ("paper positions" in q)
                or ("paper trades" in q) or ("paper pnl" in q) or ("paper summary" in q)):
                summary = closed = detail = ""
                try: summary = _vd_paper_summary(cur)
                except Exception:
                    try: cur.connection.rollback()
                    except: pass
                try: closed = _vd_closed_performance(cur)
                except Exception:
                    try: cur.connection.rollback()
                    except: pass
                try: detail = _vd_open_detail(cur)
                except Exception:
                    try: cur.connection.rollback()
                    except: pass
                parts = ["**V8 Paper Book**"]
                if summary: parts.append(summary)
                if closed: parts.append(closed)
                if detail: parts.append(detail)
                log("native","v8_paper_book")
                return "\n\n".join(parts)

            # ── LAYER 0d: Daily Digest ────────────────────────────────────────
            if "daily digest" in q or q.strip() == "digest" or "morning brief" in q:
                parts = [f"**Daily Digest — {datetime.now().strftime('%d-%b-%Y %H:%M IST')}**"]
                for fn in [_vd_market_gate, _vd_qualified, _vd_top_signals]:
                    try: parts.append(fn(cur))
                    except Exception:
                        try: cur.connection.rollback()
                        except: pass
                try:
                    cur.execute("""
                        SELECT g.symbol, g.company_name, ROUND(v.day_1d::numeric,2) as dchg
                        FROM v8_metrics v JOIN gvm_scores g ON g.symbol=v.symbol
                        WHERE v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                        ORDER BY v.day_1d DESC NULLS LAST LIMIT 5
                    """)
                    gainers = cur.fetchall()
                    if gainers:
                        d = [(r[0], r[1][:22], f"{_f(r[2]):+.2f}%") for r in gainers]
                        parts.append(f"**Top Gainers**\n{fmt_table(['Symbol','Company','Day%'], d)}")
                except Exception:
                    try: cur.connection.rollback()
                    except: pass
                log("native","daily_digest")
                return "\n\n".join(parts)

            # ── LAYER 0e: Gainers / Losers ────────────────────────────────────
            if "top gainers" in q or "biggest movers" in q or "gainers today" in q:
                cur.execute("""
                    SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2),
                           ROUND(v.day_1d::numeric,2), g.segment
                    FROM v8_metrics v JOIN gvm_scores g ON g.symbol=v.symbol
                    WHERE v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                    ORDER BY v.day_1d DESC NULLS LAST LIMIT 15
                """)
                rows = cur.fetchall()
                if not rows: log("native","gainers","","",False); return "No gainer data."
                d = [(r[0], r[1][:22], r[4][:18] if r[4] else "—",
                      f"{_f(r[2]):.2f}", f"{_f(r[3]):+.2f}%") for r in rows]
                log("native","gainers")
                return f"**Top Gainers Today**\n{fmt_table(['Symbol','Company','Segment','GVM','Day%'], d)}"

            if "top losers" in q or "biggest decliners" in q or "losers today" in q:
                cur.execute("""
                    SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2),
                           ROUND(v.day_1d::numeric,2), g.segment
                    FROM v8_metrics v JOIN gvm_scores g ON g.symbol=v.symbol
                    WHERE v.score_date=(SELECT MAX(score_date) FROM v8_metrics)
                    ORDER BY v.day_1d ASC NULLS LAST LIMIT 15
                """)
                rows = cur.fetchall()
                if not rows: log("native","losers","","",False); return "No loser data."
                d = [(r[0], r[1][:22], r[4][:18] if r[4] else "—",
                      f"{_f(r[2]):.2f}", f"{_f(r[3]):+.2f}%") for r in rows]
                log("native","losers")
                return f"**Top Losers Today**\n{fmt_table(['Symbol','Company','Segment','GVM','Day%'], d)}"

            # ── LAYER 0f: Server Time / Market State (FIX F v3) ───────────
            if any(t in q for t in TIME_TRIGGERS):
                now = datetime.now()
                is_weekday = now.weekday() < 5
                t = now.time()
                mkt_open = dtime(9,15); mkt_close = dtime(15,30)
                state = "OPEN" if (is_weekday and mkt_open <= t <= mkt_close) else "CLOSED"
                log("native","server_time")
                return (f"**Server Time — {now.strftime('%d-%b-%Y %H:%M:%S IST')}**\n"
                        f"NSE: {state} · Hours: Mon-Fri 09:15 – 15:30 IST\n"
                        f"Day: {now.strftime('%A')}")

            # ── LAYER 0g: Global Indices (NEW — FIX E v3) ─────────────────
            if ("global indices" in q or "global markets" in q
                or "world markets" in q or "global market" in q):
                try:
                    cur.execute("""
                        SELECT g.name, g.category, g.price, g.chg_pct
                        FROM global_indices g
                        JOIN (SELECT symbol, MAX(quote_date) AS md
                              FROM global_indices GROUP BY symbol) m
                          ON g.symbol=m.symbol AND g.quote_date=m.md
                        ORDER BY CASE g.category
                            WHEN 'index' THEN 1 WHEN 'volatility' THEN 2
                            WHEN 'commodity' THEN 3 WHEN 'currency' THEN 4
                            ELSE 5 END, g.name
                    """)
                    rows = cur.fetchall()
                    if rows:
                        d = [(r[0][:22], (r[1] or "")[:10],
                              f"{_f(r[2]):.2f}", f"{_f(r[3]):+.2f}%") for r in rows]
                        log("native","global_indices")
                        return f"**Global Indices**\n{fmt_table(['Name','Category','Price','Chg%'], d)}"
                    log("native","global_indices","","",False)
                    return "No global indices data."
                except Exception as e:
                    try: cur.connection.rollback()
                    except: pass
                    log("native","global_indices","","",False)
                    return f"Global indices unavailable: {str(e)[:80]}"

            # ── LAYER 1: Grammar ──────────────────────────────────────────────

            slots = parse_query(q)
            grammar_result = execute_grammar(q)

            NO_RESULT_MSG = "No stocks found. Try a broader query or toggle Claude ON."
            if grammar_result is not None and grammar_result != NO_RESULT_MSG:
                log("grammar", slots["operation"],
                    slots["metric"][1] if slots["metric"] else "",
                    slots["sector"] or "", True)
                return grammar_result

            log("grammar", slots["operation"],
                slots["metric"][1] if slots["metric"] else "",
                slots["sector"] or "", False)

            # ── LAYER 1.5: Native Intent Classifier ───────────────────────
            # FIX C (v3): explicit off-topic → classify → soft off-topic.
            # Previously soft-off-topic ran first and false-flagged
            # "morning briefing please". Reorder also stops "weather today"
            # from matching v8_qualified on weak "today" score.
            if _depth == 0:
                # Priority 1: explicit off-topic word
                if is_off_topic_explicit(query):
                    log("native", "off_topic")
                    return OFF_TOPIC_REDIRECT
                # Priority 2: intent classifier reroute
                intent = intent_classify(query)
                if intent is not None:
                    intent_id, canonical_q, score = intent
                    log("native", f"intent_{intent_id}")
                    return _query_sync(canonical_q, _depth=1)
                # Priority 3: soft off-topic (no domain words)
                if is_off_topic(query):
                    log("native", "off_topic")
                    return OFF_TOPIC_REDIRECT

            return ("⚡ Native — $0. Try:\n"
                    "• 'trade check RELIANCE long' | 'check INFY short' | 'evaluate TCS long'\n"
                    "• 'Virtual Dashboard V8' | 'market mood' | 'QB summary' | 'PCR'\n"
                    "• 'my journal open trades' | 'my journal closed trades'\n"
                    "• 'V8 paper book' | 'top gainers' | 'top losers' | 'daily digest'\n"
                    "• 'top 10 pharma gvm above 7.5' | 'tech stocks vm score'\n"
                    "• 'low pe banks' | 'high roe stocks' | 'peg below 1' | 'best dividend stocks'\n"
                    "• 'sector ratings' | 'GVM history HDFC bank' | 'global indices'\n"
                    "• 'overview Torrent Pharma' | 'RELIANCE analysis' | 'whats SBIN like'\n"
                    "Or toggle Claude ON for free-text queries.")


async def route_native(query: str) -> str:
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query_sync, query)
    except Exception as e:
        return f"DB error: {str(e)[:200]}\nToggle Claude ON for full access."

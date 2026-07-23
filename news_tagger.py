"""
news_tagger.py — cc#207 (Part C: main-feed symbol tagger)
=========================================================
After each market-news polish cycle, tag polished_news rows with the universe
symbols they mention, so company pages populate WITHOUT per-company Google waves.

  mentioned_symbols  = universe symbols whose company name / NSE code appears in the
                       article (word-boundary matched, headline + summary).

Precision guards (a reading room populated with WRONG company news is worse than empty):
  • Word-boundary regex only (so "Titan" never matches "titanium", "ITC" never matches
    "switch").
  • Strip corporate suffixes (Ltd/Limited/Industries/…), then require the CORE name to
    be >= 5 chars and not in a stop set of generic/ambiguous words.
  • An alias map handles the handful of names that are real English words or trade under
    a different popular name.
  • The NSE code is matched as an upper-case token only (word-boundary), never lowercased.

C2 backfill is folded into the scheduled pass: every run tags ALL still-untagged
polished rows within the window, so the first run after deploy backfills 30 days and
subsequent runs handle the trickle. A no-match row is stamped '{}' so it is never
re-scanned.
"""

import os
import re
import logging

import psycopg

log = logging.getLogger("news_tagger")
DATABASE_URL = os.getenv("DATABASE_URL", "")

_SUFFIXES = re.compile(
    r"\b(ltd|limited|industries|industry|corporation|corp|company|co|enterprises|"
    r"holdings|technologies|technology|systems|services|international|india|of india|"
    r"financial|finance|bank|motors|pharma|pharmaceuticals|laboratories|labs|"
    r"and|&)\b", re.I)

# generic / ambiguous cores that must NOT be matched on their own (require nse_code or
# a fuller unique token). Extend as false positives surface during the trial.
_STOP_CORE = {
    "power", "steel", "cement", "energy", "capital", "auto", "infra", "life", "one",
    "gold", "sun", "india", "united", "national", "state", "central", "global",
    "future", "vision", "max", "force", "orient", "century", "network", "media",
    "sona", "page", "route", "info", "care", "trent", "coal", "oil", "gas", "wipro",
}
# explicit overrides: symbol -> list of extra literal names to match (word-boundary)
_ALIAS = {
    "TITAN": ["titan company"],
    "MARUTI": ["maruti suzuki", "maruti"],
    "ITC": [],            # matched via NSE-code token 'ITC' only (bare word too risky)
    "MRF": [],
    "BEL": [],
    "TATASTEEL": ["tata steel"],
    "TATAMOTORS": ["tata motors"],
    "BAJFINANCE": ["bajaj finance"],
    "BAJAJFINSV": ["bajaj finserv"],
    # cc#611 D1: generic/truncated financial names whose derived core is a shared sector phrase
    # (screener_raw truncates 'Life Insurance Corporation of India' -> 'Life Insurance', which then
    # matched EVERY 'X Life Insurance'). Pin to the DISTINCTIVE phrase (or code-only) so HDFC Life /
    # SBI Life / ICICI Pru / Canara HSBC Life never bleed onto LICI, and HDFC AMC never onto HDFCBANK.
    "LICI": ["life insurance corporation"],
    "HDFCLIFE": ["hdfc life"],
    "SBILIFE": ["sbi life"],
    "ICICIPRULI": ["icici prudential", "icici pru life"],
    "HDFCAMC": ["hdfc asset management", "hdfc amc"],
}


def _conn():
    return psycopg.connect(DATABASE_URL)


def _core(name):
    n = (name or "").lower()
    n = re.sub(r"[.,]", " ", n)
    n = _SUFFIXES.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def build_index(conn):
    """Return [(symbol, [compiled_regex, ...])] for every universe symbol with a usable
    name/code signature. Built from screener_raw (company_name + nse_code)."""
    # screener_raw keys on nse_code (the NSE ticker == the symbol company pages query);
    # it carries the full equity universe's company_name. (futures_universe is a subset
    # of the same tickers, so nse_code coverage already spans it.)
    rows = []
    with conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT ON (nse_code) nse_code AS symbol, company_name, nse_code
                       FROM screener_raw WHERE nse_code IS NOT NULL AND nse_code <> ''""")
        rows = cur.fetchall()
    index = []
    for sym, cname, nse in rows:
        pats, literals = [], set()
        # (1) NSE code as an upper-case token (precise) — length>=3, alphanumeric
        code = (nse or sym or "").strip().upper()
        if code and len(code) >= 3 and re.match(r"^[A-Z0-9&]+$", code):
            pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])"))
        # (2) explicit aliases
        for a in _ALIAS.get(sym.upper(), []):
            literals.add(a.lower())
        # (3) name patterns if unambiguous (an _ALIAS entry means "code/alias only").
        if sym.upper() not in _ALIAS:
            # (3a) FULL name minus only the legal suffix — precise, catches spelled-out
            #      names the aggressive core drops (e.g. "State Bank of India" -> SBIN,
            #      which never appears as the ticker in prose).
            full = re.sub(r"\b(ltd|limited)\.?\s*$", "", (cname or "").lower()).strip()
            full = re.sub(r"[.,]", " ", full); full = re.sub(r"\s+", " ", full).strip()
            if " " in full and len(full) >= 8 and full not in _STOP_CORE:
                literals.add(full)
            # (3b) core (short reference like "Reliance" from "Reliance Industries").
            #      Multi-word cores safe at >=5 chars; a bare single word must be >=7.
            core = _core(cname)
            multiword = " " in core
            if core and core not in _STOP_CORE and ((multiword and len(core) >= 5) or (not multiword and len(core) >= 7)):
                literals.add(core)
        for lit in literals:
            pats.append(re.compile(r"\b" + re.escape(lit) + r"\b", re.I))
        if pats:
            index.append((sym.upper(), pats))
    log.info(f"news_tagger index: {len(index)} symbols")
    return index


def _match(text_lower, text_upper, index):
    hits = []
    for sym, pats in index:
        for p in pats:
            # upper-case NSE-code patterns are case-sensitive; alias/core are re.I
            target = text_upper if (p.flags & re.IGNORECASE) == 0 else text_lower
            if p.search(target):
                hits.append(sym)
                break
    return hits


def headline_identity(conn, symbol):
    """cc#210: patterns to decide if a headline is ABOUT `symbol` (PRIMARY) vs merely
    mentions it in the body (MENTIONED). Tuned per-symbol and slightly more lenient than
    the cross-universe tagger: because the page context is a single known symbol, the
    company's leading word (e.g. 'HDFC' from 'HDFC Bank') is a safe PRIMARY signal here
    even though it is too collision-prone for the global tagger."""
    sym = (symbol or "").upper()
    cname = None; nse = sym
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT company_name, nse_code FROM screener_raw WHERE UPPER(nse_code)=%s LIMIT 1", (sym,))
            row = cur.fetchone()
        if row:
            cname, nse = row[0], (row[1] or sym)
    except Exception as e:
        log.warning(f"headline_identity({sym}): {e}")
    pats = []
    code = (nse or sym).strip().upper()
    if code and len(code) >= 3 and re.match(r"^[A-Z0-9&]+$", code):
        pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(code) + r"(?![A-Za-z0-9])"))
    literals = set(a.lower() for a in _ALIAS.get(sym, []))
    if sym not in _ALIAS:
        full = re.sub(r"\b(ltd|limited)\.?\s*$", "", (cname or "").lower()).strip()
        full = re.sub(r"[.,]", " ", full); full = re.sub(r"\s+", " ", full).strip()
        if " " in full and len(full) >= 8 and full not in _STOP_CORE:
            literals.add(full)
        core = _core(cname); multiword = " " in core
        if core and core not in _STOP_CORE and ((multiword and len(core) >= 5) or (not multiword and len(core) >= 7)):
            literals.add(core)
        # lenient leading word (page-scoped): first significant token of the name
        first = (re.sub(r"[.,]", " ", (cname or "")).split() or [""])[0].lower()
        if len(first) >= 3 and first not in _STOP_CORE and re.match(r"^[a-z0-9&]+$", first):
            literals.add(first)
    for lit in literals:
        pats.append(re.compile(r"\b" + re.escape(lit) + r"\b", re.I))
    return pats


def is_primary(pats, headline):
    """True if any identity pattern matches the headline (word-boundary; NSE-code patterns
    are case-sensitive upper, name patterns are case-insensitive)."""
    if not headline or not pats:
        return False
    low, up = headline.lower(), headline.upper()
    for p in pats:
        target = up if (p.flags & re.IGNORECASE) == 0 else low
        if p.search(target):
            return True
    return False


def tag_untagged(conn=None, days=30, max_rows=8000):
    """Tag every still-untagged polished_news row within `days`. First run backfills."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        index = build_index(conn)
        if not index:
            return {"ok": True, "scanned": 0, "tagged": 0, "note": "empty index"}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.id, COALESCE(p.headline_clean, r.headline), COALESCE(p.summary, r.description)
                FROM polished_news p JOIN raw_news r ON r.id = p.raw_news_id
                WHERE p.mentioned_symbols IS NULL
                  AND p.polished_at > NOW() - INTERVAL '%s days'
                ORDER BY p.polished_at DESC
                LIMIT %s
            """ % (int(days), int(max_rows)))
            batch = cur.fetchall()
        scanned = tagged = 0
        with conn.cursor() as cur:
            for pid, headline, summary in batch:
                scanned += 1
                text = ((headline or "") + " . " + (summary or ""))
                syms = _match(text.lower(), text.upper(), index)
                cur.execute("UPDATE polished_news SET mentioned_symbols=%s WHERE id=%s",
                            (syms, pid))     # [] stamps as scanned so it is never rescanned
                if syms:
                    tagged += 1
            conn.commit()
        log.info(f"news_tagger: scanned {scanned}, tagged {tagged}")
        return {"ok": True, "scanned": scanned, "tagged": tagged, "index_symbols": len(index)}
    except Exception as e:
        log.error(f"news_tagger.tag_untagged: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()

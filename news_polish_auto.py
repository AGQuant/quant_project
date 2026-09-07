"""
news_polish_auto.py — cc#1804 NEWS_POLISH_AUTOMATION_V1 (session_log id=40138, founder-set
07-Sep-2026): hourly automated news-short polish, 06:00-17:00 IST inclusive (12 dispatch slots/
day — the hour gate lives in scheduler.py's dispatch loop, not here). Ceiling 4/hour weekdays,
2/hour weekends ("batch is a ceiling not a target" — never pad to reach it).

WHAT THIS DOES NOT TOUCH (do_not_touch, per the card):
  - the existing on-demand chat polish path Fable uses (news_endpoints.py / run_sql) — untouched,
    this module only ADDS a second, automated writer of the same table
  - AI Editorial: excluded from selection entirely (source_type NOT considered here at all is
    moot — AI Editorial has no raw_news source_type; the category this module ever writes is
    Domestic / Global / IPO, never AI Editorial, so EDITORIAL_DATA_GATE (cc#1729) never applies)
  - the cc#242 POSITION_NEWS_PIPELINE_V1 (source_type='company', open-positions-only): this
    module explicitly excludes source_type='company' from its candidate query. "Company" here is
    a SELECTION-PRIORITY TIER only (COMPANY > DOMESTIC > IPO > GLOBAL), not an output category —
    confirmed via `SELECT DISTINCT category FROM polished_news`, which has no such value. A
    company-tier story is still published under category='Domestic'.
  - existing polished_news rows, display ordering, v_polished_articles — read-only w.r.t. those.

COMPANY-TIER DETECTION (raw_news.symbol is populated on <1% of rows — unusable as a router).
Built fresh here: a compiled regex over the ACTIVE futures_universe symbol list + each symbol's
screener_raw company_name, word-boundary matched against headline+description at pickup time.
Deliberately scoped to the liquid futures_universe (~200 names) rather than all of screener_raw
(thousands of rows) — matching against every listed company name would false-positive constantly
on common words. This is a v1 heuristic; the card asks for the hit rate to be reported after a
week of real runs (see the ops_log rows this module writes), not for a perfect router on day one.

NEVER FABRICATE: the LLM prompt is explicit that every fact in full_summary must come from the
source row, and mentioned_symbols is capped to what the pickup-time match (or the model, told not
to invent one) actually found — a hallucinated symbol on a story that isn't about it is a worse
failure than an empty array.
"""
import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("scorr.news_polish_auto")

IST = timezone(timedelta(hours=5, minutes=30))
MODEL = "claude-sonnet-4-6"          # same default as anthropic_endpoints.py / native_trade_check.py
CANDIDATE_POOL = 400                 # rows pulled before tiering; ceiling picks off the top of it

# IPO-tier keyword heuristic — raw_news carries no source_type='ipo' (confirmed: distinct
# source_type values are company/domestic/editorial/global only), so this is content-matched,
# same approach news_fetcher.py/news_endpoints.py already use for their own IPO detection.
_IPO_RE = re.compile(
    r"\bipo\b|initial public offering|grey market premium|\bgmp\b|listing (?:gain|debut)s?|"
    r"anchor investors?|allotment status|subscription status|bourses? debut|market debut|"
    r"draft red herring|\bdrhp\b|price band|\bqib\b portion|retail portion", re.I)

CATEGORY_FOR_TIER = {"company": "Domestic", "domestic": "Domestic", "ipo": "IPO", "global": "Global"}


def _weekday_ceiling(now_ist: datetime) -> int:
    return 2 if now_ist.weekday() >= 5 else 4          # Sat/Sun = 5/6


def _company_matcher(conn):
    """One compiled regex + a lowercase-term->symbol map, built fresh each run (cheap: ~200
    active futures_universe rows). Longest term first so 'Reliance Industries' matches whole,
    not the 'Reliance' inside a longer unrelated word boundary case."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT fu.symbol, sr.company_name
            FROM futures_universe fu
            LEFT JOIN screener_raw sr ON sr.nse_code = fu.symbol
            WHERE fu.is_active = true
        """)
        rows = cur.fetchall()
    term_symbol = {}
    for sym, cname in rows:
        if sym and len(sym) >= 3:
            term_symbol[sym.lower()] = sym
        if cname:
            cname = cname.strip()
            if len(cname) >= 4:
                term_symbol[cname.lower()] = sym
    if not term_symbol:
        return None, {}
    terms_sorted = sorted(term_symbol.keys(), key=len, reverse=True)
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms_sorted) + r")\b", re.I)
    return pattern, term_symbol


def _company_tier(pattern, term_symbol, headline, description):
    if pattern is None:
        return None
    text = f"{headline or ''} {description or ''}"
    m = pattern.search(text)
    if not m:
        return None
    return term_symbol.get(m.group(1).lower())


def _select_candidates(conn):
    """Reuses news_endpoints.py's own quality/suppression/reco clauses so this module can never
    drift from the manual-polish candidate definition it's meant to complement, not compete with.
    source_type restricted to domestic/global only — 'company' (position-locked pipeline) and
    'editorial' (AI Editorial, excluded entirely) are never candidates here."""
    import news_endpoints as ne
    reco_clause = ne._reco_exclude_clause(conn)
    quality_clause = ne._QUALITY_CLAUSE.replace("%", "%%")   # escaped: LIMIT %s below is a real param
    sql = f"""
        SELECT r.id AS raw_id, r.source_type, r.symbol, r.headline, r.description,
               r.source_name, r.published_at
        FROM raw_news r
        WHERE NOT EXISTS (SELECT 1 FROM polished_news p WHERE p.raw_news_id = r.id)
          AND r.canonical_id IS NULL
          AND r.source_type IN ('domestic', 'global')
        {quality_clause}
        {ne._SUPPRESSED_CLAUSE}
        {reco_clause}
        ORDER BY r.relevance_score DESC NULLS LAST, r.fetched_at DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (CANDIDATE_POOL,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _tier_ordered(conn, rows):
    """COMPANY > DOMESTIC > IPO > GLOBAL, per NEWS_POLISH_AUTOMATION_V1. A company match wins
    over an IPO-keyword match (a company-specific IPO update is still primarily about the
    company); global-source rows never get promoted into ipo/domestic even on an IPO keyword hit
    (a global IPO story stays GLOBAL — the tier is about audience relevance, not just keywords)."""
    pattern, term_symbol = _company_matcher(conn)
    buckets = {"company": [], "domestic": [], "ipo": [], "global": []}
    for r in rows:
        sym = _company_tier(pattern, term_symbol, r["headline"], r["description"])
        if sym:
            r["_matched_symbol"] = sym
            buckets["company"].append(r)
            continue
        text = f"{r['headline'] or ''} {r['description'] or ''}"
        if r["source_type"] == "domestic" and _IPO_RE.search(text):
            buckets["ipo"].append(r)
        elif r["source_type"] == "global":
            buckets["global"].append(r)
        else:
            buckets["domestic"].append(r)
    ordered = []
    for tier in ("company", "domestic", "ipo", "global"):
        for r in buckets[tier]:
            r["_tier"] = tier
            ordered.append(r)
    return ordered


def _polish_one(client, row):
    symbol_hint = row.get("_matched_symbol")
    if symbol_hint:
        hint = (f"Pickup-time matching found this story mentions {symbol_hint} (NSE symbol). "
                f"Include it in mentioned_symbols only if it is genuinely central to the story.\n")
    else:
        hint = ("No specific listed company was pre-matched for this story. Leave "
                "mentioned_symbols empty unless the source text explicitly names a listed "
                "company you are confident is NSE-listed.\n")
    prompt = (
        "You are a financial news editor writing a short, plain-English polish of a raw wire "
        "story for Indian retail investors. Use ONLY facts present in the SOURCE below — never "
        "invent a number, a quote, or a company detail that is not there. If the source itself "
        "is thin, write a shorter honest summary rather than padding it with invented detail.\n\n"
        f"SOURCE HEADLINE: {row.get('headline') or ''}\n"
        f"SOURCE BODY: {row.get('description') or ''}\n\n"
        f"{hint}"
        "Return ONLY a JSON object with these exact keys, nothing else:\n"
        '  "headline_clean": tightened plain-English headline (max 120 chars)\n'
        '  "summary": one short teaser sentence (max 160 chars)\n'
        '  "full_summary": 3 to 5 substantive sentences with the real figures/facts from the '
        "source, plain English, no jargon, never fabricated\n"
        '  "sentiment": one of Positive, Negative, Neutral\n'
        '  "impact": one of High, Medium, Low\n'
        '  "mentioned_symbols": a JSON array of NSE symbols genuinely central to the story '
        "(usually empty or one symbol)\n"
        "Never mention Scorr. No markdown fencing, no preamble — the JSON object only."
    )
    msg = client.messages.create(model=MODEL, max_tokens=600,
                                  messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.M).strip()
    return json.loads(txt)


def _insert_polished(conn, row, parsed):
    category = CATEGORY_FOR_TIER[row["_tier"]]
    symbols = parsed.get("mentioned_symbols") or []
    symbols = [s.strip().upper() for s in symbols if isinstance(s, str) and s.strip()][:5]
    with conn.cursor() as cur:
        # published_time == polished_at, POLISH_TIMESTAMP_RULE_V2 (session_log 39131)
        cur.execute("""
            INSERT INTO polished_news
                (raw_news_id, headline_clean, summary, category, sentiment, impact,
                 mentioned_symbols, polished_at, full_summary, source, published_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, NOW())
        """, (row["raw_id"], parsed.get("headline_clean") or row.get("headline"),
              parsed.get("summary"), category, parsed.get("sentiment"), parsed.get("impact"),
              symbols, parsed.get("full_summary"), row.get("source_name")))
    conn.commit()


def run(conn) -> dict:
    """Entry point called by scheduler.py's _bg_news_polish_auto(). Returns a plain result dict —
    the caller (not this module) translates it into the _Skip/_Empty/ok vocabulary, keeping that
    vocabulary centralized in scheduler.py where every other job's version of it already lives."""
    now = datetime.now(IST)
    ceiling = _weekday_ceiling(now)
    rows = _select_candidates(conn)
    ordered = _tier_ordered(conn, rows)
    eligible = len(ordered)

    # Floor rule (NEWS_POLISH_AUTOMATION_V1): <2 eligible = skip, not an error. Thin supply is
    # routine at the 06:00 slot before the morning fetch has landed much, or on a quiet news day.
    if eligible < 2:
        return {"status": "skip_thin_supply", "eligible": eligible}

    tier_counts_eligible = {}
    for r in ordered:
        tier_counts_eligible[r["_tier"]] = tier_counts_eligible.get(r["_tier"], 0) + 1

    selected = ordered[:ceiling]     # ceiling is a CAP, never a target — never pad below it

    client = None
    init_error = None
    try:
        import anthropic
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if key:
            client = anthropic.Anthropic(api_key=key)
        else:
            init_error = "ANTHROPIC_API_KEY not set"
    except Exception as e:
        init_error = f"{type(e).__name__}: {e}"

    if client is None:
        return {"status": "empty", "eligible": eligible, "selected": len(selected),
                "detail": init_error or "anthropic client unavailable"}

    inserted = 0
    tier_counts_inserted = {}
    errors = []
    for row in selected:
        try:
            parsed = _polish_one(client, row)
            if not (parsed.get("full_summary") or "").strip():
                errors.append(f"raw_id={row['raw_id']}: model returned empty full_summary, skipped")
                continue
            _insert_polished(conn, row, parsed)
            inserted += 1
            tier_counts_inserted[row["_tier"]] = tier_counts_inserted.get(row["_tier"], 0) + 1
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            errors.append(f"raw_id={row.get('raw_id')}: {type(e).__name__}: {str(e)[:200]}")

    result = {
        "status": "ok" if inserted else "empty",
        "eligible": eligible, "eligible_by_tier": tier_counts_eligible,
        "ceiling": ceiling, "selected": len(selected),
        "inserted": inserted, "inserted_by_tier": tier_counts_inserted,
        "errors": errors[:10],
    }
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'news_polish_auto', 'hourly_run', %s::jsonb)""",
                        (json.dumps(result, default=str),))
        conn.commit()
    except Exception as e:
        log.warning(f"news_polish_auto ops_log write failed: {e}")
    return result

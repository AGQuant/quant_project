"""
news_identity.py — cc#847: the news IDENTITY helpers, lifted out of position_news.py.

WHY THIS FILE EXISTS. cc#847 retires Position News — tab, fetcher, endpoints, scheduler job. But
`position_news.py` was never only a fetcher: `stock_views_funnel.fetch_universe_reco_news` (cc#787,
a LIVE two-slot/day job over the active futures universe) imports three helpers from it —
`_name_map`, `_split_title` and `_target_is_primary`. Deleting the module outright would have taken
that job down with it, so the shared parts move here and the retired parts go.

Nothing here touches the `position_news` TABLE. These are pure identity/attribution helpers over
gvm_scores + the news_tagger index:

  _name_map          symbol -> company_name, for a richer search query
  _split_title       'Headline - Publisher' -> (headline, publisher)
  _target_is_primary the cc#611 D1 whole-universe specificity match — the guard that stops
                     'HDFC Life Insurance' being filed under LICI or HDFCBANK

The cc#611 D1 doctrine is preserved verbatim; this is a MOVE, not a rewrite. The junk-title /
junk-source patterns travel with it because they belong to the same identity layer.
"""

import re
import logging

import news_fetcher as nf

log = logging.getLogger("scorr.news_identity")

# cc#611 D2: quote/price-page garbage — NOT news. Broker "share price / stock price / live NSE"
# landing pages (Upstox, Groww, 5paisa, Angel One, Tickertape, …) leak into a Google News company
# query; drop them by headline pattern OR source so only genuine articles survive.
_JUNK_TITLE = re.compile(
    r'(share\s*price|stock\s*price|price\s*(?:&|and)\s*chart|live\s*(?:nse|bse)\b|'
    r'share\s*price\s*(?:live|today|target)|stock\s*quote|/\s*chart|price\s*target\b)', re.I)
_JUNK_SOURCE = re.compile(
    r'(upstox|groww|angel\s*one|angelone|5paisa|tickertape|moneyworks4me|trendlyne|'
    r'stockedge|equitymaster\s*quote|screener\.in)', re.I)

_NSE_TAG = re.compile(r'\((?:NSE|BSE)\s*:\s*([A-Z0-9&]+)\)', re.I)


def _match_len_code(pats, low, up):
    """Best matched-literal length across a symbol's identity patterns + whether an NSE-code
    (case-sensitive, non-IGNORECASE) pattern hit (a code hit is a definitive attribution)."""
    best, code_hit = 0, False
    for p in pats:
        t = up if (p.flags & re.IGNORECASE) == 0 else low
        m = p.search(t)
        if m:
            L = m.end() - m.start()
            if L > best:
                best = L
            if (p.flags & re.IGNORECASE) == 0:
                code_hit = True
    return best, code_hit


def _target_is_primary(index, target, text):
    """cc#611 D1: the article is ABOUT `target` iff — across the WHOLE-universe news_tagger index —
    target's best name/code match is the MOST SPECIFIC (longest literal). So 'HDFC Life Insurance'
    resolves to HDFCLIFE ('hdfc life insurance', 19) over LICI ('life insurance', 14) and is dropped
    for LICI; 'HDFC Asset Management' never touches HDFCBANK (whose only literals are 'HDFCBANK' /
    'hdfc bank'). An exact NSE-code hit is definitive. A headline that explicitly tags a DIFFERENT
    listed code (NSE:XXX) and NOT the target is rejected outright. This replaces the single-symbol
    is_primary(), whose lenient leading-word ('HDFC','Life') is the cross-company-bleed root cause."""
    tgt = target.upper()
    tags = {m.group(1).upper() for m in _NSE_TAG.finditer(text)}
    if tags and tgt not in tags:
        return False
    tpats = index.get(tgt)
    if not tpats:
        return False
    low, up = text.lower(), text.upper()
    tbest, tcode = _match_len_code(tpats, low, up)
    if tbest == 0:
        return False
    if tcode:
        return True
    for sym, pats in index.items():
        if sym == tgt:
            continue
        b, _c = _match_len_code(pats, low, up)
        if b > tbest:      # a different company matched more specifically -> not about target
            return False
    return True


def _name_map(conn, symbols):
    """symbol -> company_name (for a richer Google query); falls back to the symbol."""
    names = {}
    if not symbols:
        return names
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, company_name FROM gvm_scores
                           WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                             AND symbol = ANY(%s) AND company_name IS NOT NULL""", (list(symbols),))
            for sym, nm in cur.fetchall():
                names[sym] = nm
    except Exception as e:
        log.warning(f"_name_map: {e}")
    return names


def _split_title(title):
    """Google News titles are 'Headline - Publisher'. Split off the publisher for the
    source badge; leave the headline clean. Returns (headline, source_name)."""
    t = nf._clean(title or "")
    if " - " in t:
        head, _, src = t.rpartition(" - ")
        if head and len(src) <= 60:
            return head.strip(), src.strip()
    return t, "Google News"

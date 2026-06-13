"""
Native Intent Classifier — Layer 1.5 of native_router.

Pure Python. $0. Offline. No LLM. No external API.
Fuzzy matches arbitrary user queries to one of N canonical Scorr intents,
each of which maps to a query string that an existing Layer 0 handler catches.

Algorithm:
  1. Off-topic guard: explicit OFF_TOPIC_WORDS hit → polite redirect
  2. Score every intent's keyword-bag against query words using:
     - exact substring match  (+10)
     - exact word match       (+3)
     - prefix/suffix stem     (+1 each, for words ≥4 chars)
     - Levenshtein ≤1         (+2, for typos)
  3. Best score ≥ 4.0 → return canonical query for Layer 0 reroute
  4. Else → None (caller shows help hint)

Used by native_router._query_sync as the final fallback before the hint.
"""

import re
from typing import Optional, Tuple, List

# ── Canonical Intent Library ─────────────────────────────────────────────────
# (intent_id, canonical_query, keywords)
# Each canonical_query MUST hit an existing Layer 0/0a-0f or Layer 1 handler
# directly when re-routed. Verified 13-Jun-2026 against native_router.py.

INTENTS: List[Tuple[str, str, List[str]]] = [
    # V8 Signals ─────────────────────────────────────────────────────────────
    ("v8_dashboard",   "virtual dashboard v8",
     ["v8","dashboard","virtual","signals","live","cockpit"]),
    ("v8_mood",        "market mood",
     ["mood","gate","adr","nifty","slot","sentiment","bullish","bearish"]),
    ("v8_qualified",   "qualified now",
     ["qualified","candidates","watchlist","signals","today","picks"]),
    ("v8_paper",       "v8 paper book",
     ["paper","book","positions","open","closed","pnl","unrealised","trades"]),

    # GVM Engine ─────────────────────────────────────────────────────────────
    ("gvm_top",        "top 10 stocks by gvm",
     ["top","gvm","best","quality","rank","ranking","leaders"]),
    ("gvm_strong",     "gvm above 8",
     ["strong","strongbuy","accumulate","conviction"]),
    ("gvm_sector",     "sector ratings",
     ["sector","sectors","ratings","rotation","industry","segment","segments"]),

    # Trade Check (routes via Layer 0a) ──────────────────────────────────────
    # NOTE: trade check needs a SYMBOL — handled by frontend prompt before query.

    # Quant Basket ───────────────────────────────────────────────────────────
    ("qb_summary",     "qb summary",
     ["qb","quant","basket","baskets","equity","portfolio","pf","holdings"]),

    # Daily Digest ───────────────────────────────────────────────────────────
    ("daily_digest",   "daily digest",
     ["digest","daily","morning","brief","briefing","summary","overview","recap"]),
    ("top_gainers",    "top gainers today",
     ["gainers","gainer","winners","movers","rising","rallying","up","green"]),
    ("top_losers",     "top losers today",
     ["losers","loser","decliners","falling","dropping","down","red"]),

    # Market Intelligence ────────────────────────────────────────────────────
    ("server_time",    "server time",
     ["time","clock","ist","now","when","hours","market","open"]),
    ("pcr",            "pcr",
     ["pcr","put","call","ratio","options","oi"]),

    # Personal Journal ───────────────────────────────────────────────────────
    ("journal_open",   "my personal journal open trades",
     ["my","journal","personal","open","current","running","live"]),
    ("journal_closed", "my personal journal closed trades and pnl",
     ["my","journal","personal","closed","exit","exits","pnl","performance","stats"]),

    # System ─────────────────────────────────────────────────────────────────
    ("system_health",  "system health",
     ["health","system","status","check","working","ok","feeds","data"]),
]

# ── Off-topic detection ──────────────────────────────────────────────────────
# Explicit words that strongly indicate non-investing query.
# Keeping the list focused — only obvious non-financial domains.

OFF_TOPIC_WORDS = {
    # Sports
    "cricket","football","soccer","hockey","tennis","kabaddi","badminton",
    "ipl","worldcup","fifa","olympics","commonwealth","asiacup",
    # Entertainment
    "movie","movies","film","films","actor","actress","song","songs",
    "music","album","band","singer","bollywood","hollywood","netflix",
    "youtube","tiktok","instagram","facebook",
    # Food / lifestyle
    "recipe","recipes","cooking","kitchen","biryani","pizza","burger",
    # Weather / news non-market
    "weather","temperature","forecast","rainfall","earthquake","cyclone",
    # Other off-topic
    "joke","jokes","poem","poems","story","novel","fiction",
    "celebrity","gossip","scandal","dating","relationship","marriage",
    "horoscope","astrology","zodiac",
}

# ── Domain keywords ──────────────────────────────────────────────────────────
# At least one of these in the query strongly suggests on-topic.

DOMAIN_WORDS = {
    # Generic finance
    "stock","stocks","share","shares","trade","trades","trading",
    "invest","investing","investment","investor","market","markets",
    "price","prices","buy","sell","long","short","pnl","profit","loss",
    "gain","gainer","loser","return","returns","growth","value","momentum",
    "score","rank","top","best","high","low",
    # Indian indices / exchanges
    "nifty","sensex","banknifty","nse","bse","sebi","mcx",
    # Sectors
    "bank","banks","banking","pharma","auto","it","tech","technology",
    "fmcg","cement","steel","power","metal","metals","energy","oil","gas",
    "consumer","retail","textile","sugar","paper","mining","fertilizer",
    "chemical","chemicals","telecom","media","defence","defense","shipping",
    "logistics","hotel","hospitality","realty","property","infra","epc",
    "renewable","solar","healthcare","hospital","insurance","nbfc","amc",
    # Caps / classifications
    "largecap","midcap","smallcap","microcap","large","mid","small","micro",
    # Scorr products
    "scorr","max","aicio","cio","gvm","v8","qb","v10","v9","claude",
    # Technical
    "rsi","pe","roce","opm","margin","dma","ema","macd","fibonacci","fib",
    "pivot","support","resistance","breakout","reversal","consolidation",
    "dividend","yield","fii","dii","promoter","institutional","mcap",
    "earnings","result","results","quarter","quarterly","annual","report",
    # Trade-flow
    "check","review","journal","personal","my","mine","open","closed",
    "entry","exit","target","stoploss","sl","slot","slots","basket",
    "watchlist","qualified","candidate","candidates","signal","signals",
    "alert","alerts","mood","gate","adr","pcr","puts","calls","oi",
    "futures","options","fno","intraday","swing","positional",
    "paper","portfolio","position","positions","trade","book",
    # Time
    "now","today","week","weekly","month","monthly","yearly","daily",
    "digest","brief","time","clock","ist",
    # Misc
    "company","companies","corporate","equity","equities","fundamental",
    "technical","alpha","beta","strategy","backtest","posted","reported",
    "good","strong","weak","outperform","underperform","beat","miss",
    "find","show","list","get","fetch","display","view","tell",
    "what","whats","which","how","hows","why","when","where",
    "global","dow","nasdaq","sp500","gold","silver","crude","commodity",
    "commodities","currency","usdinr","rupee","dollar","yen","euro",
}

OFF_TOPIC_REDIRECT = (
    "⚡ **Scorr is built for Indian equity research and trading.**\n\n"
    "I can help with:\n"
    "• **Stocks** — GVM scores, sector ranks, peer compare\n"
    "• **V8 signals** — market mood, qualified, paper book\n"
    "• **Your portfolio** — journal, P&L, open positions\n"
    "• **Trade Check** — type 'trade check INFY long'\n\n"
    "_Try: 'top 10 pharma'  ·  'market mood'  ·  'my journal'  ·  'daily digest'_"
)


# ── Levenshtein (capped, early-exit) ─────────────────────────────────────────
def _lev(a: str, b: str, cap: int = 2) -> int:
    """Edit distance with early exit at cap+1."""
    if a == b: return 0
    if abs(len(a) - len(b)) > cap: return cap + 1
    if len(a) < len(b): a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(cur[-1] + 1, prev[j+1] + 1, prev[j] + (ca != cb)))
        prev = cur
        if min(prev) > cap: return cap + 1
    return prev[-1]


# ── Off-topic check ──────────────────────────────────────────────────────────
def is_off_topic(query: str) -> bool:
    """True if query has off-topic words AND no domain words."""
    if not query: return False
    words = set(re.findall(r"[a-z]+", query.lower()))
    if not words: return False
    has_off_topic = bool(words & OFF_TOPIC_WORDS)
    has_domain = bool(words & DOMAIN_WORDS)
    # Strong off-topic word with NO domain word → off-topic
    if has_off_topic and not has_domain:
        return True
    # No off-topic word, no domain word, query is non-trivial → probably off-topic
    if not has_off_topic and not has_domain and len(words) >= 2:
        return True
    return False


# ── Intent scoring ───────────────────────────────────────────────────────────
def _score_intent(query_lower: str, query_words: List[str],
                  kws: List[str], canonical: str) -> float:
    bag_text = (" ".join(kws) + " " + canonical).lower()
    bag_words = set(re.findall(r"[a-z]+", bag_text))
    score = 0.0

    # Exact substring of the full query
    if query_lower in bag_text:
        score += 10.0

    # Per-word scoring
    for w in query_words:
        if len(w) < 2: continue
        if w in bag_words:
            score += 3.0
        elif len(w) >= 4:
            stem_pre = w[:4]
            stem_suf = w[-4:]
            for bw in bag_words:
                if bw.startswith(stem_pre) or bw.endswith(stem_suf):
                    score += 1.0
                    break
            # Levenshtein for typo tolerance
            for kw in kws:
                if abs(len(w) - len(kw)) <= 2 and _lev(w, kw, cap=1) <= 1:
                    score += 2.0
                    break

    return score


def classify(query: str, min_score: float = 4.0) -> Optional[Tuple[str, str, float]]:
    """
    Returns (intent_id, canonical_query, score) if best score >= min_score, else None.
    """
    if not query: return None
    ql = query.lower().strip()
    words = re.findall(r"[a-z]+", ql)
    if not words: return None

    best_id, best_q, best_s = None, None, 0.0
    for intent_id, canonical, kws in INTENTS:
        s = _score_intent(ql, words, kws, canonical)
        if s > best_s:
            best_id, best_q, best_s = intent_id, canonical, s

    if best_s >= min_score:
        return (best_id, best_q, best_s)
    return None

"""custom_screener_app.py -- cc#2158 (SCREENERS_APP_V3 push 3/5): the Custom Screener backend, EOD, button-only.

THE SIMPLEST POSSIBLE SCREENER. The user taps buttons, never types a number. Ten filters, at most three
buttons each. Buttons inside one filter are OR; filters combine with AND; a filter nobody touched is OFF.
Every button maps to ONE locked threshold on ONE end-of-day table, and the thresholds are the bands the app
already prints (GVM verdict bands, Investment Score bands), so a button never disagrees with a badge shown
elsewhere in the app. NOT the TC score -- founder ruling 17-Sep-2026: "TC has no relevance here, it is not
the trading scanner." Investment Score = investment_check_v2_scores.band (cc#2134, EOD), read as persisted.

REGISTRY SOURCE OF TRUTH: cc#2158 spec `filter_registry_locked` == cc#1199 log 6913. A threshold here is a
founder decision, not a tuning knob.

BASIS. EOD only, computed on request. Universe = gvm_scores at MAX(score_date) (registry-derived, never a
hardcoded list). ONE scored CTE: gvm_scores LEFT JOIN mcap_rank_daily / sector_ratings /
investment_check_v2_scores / universe_technicals, each at ITS OWN latest date (all five dates come back in
`as_of`). NULL never passes a filter that is on (cc#1822): a name with no technicals row cannot pass a
technicals button, and /run says how many were dropped for that reason (`excluded_no_technicals`, present
only when a technicals-backed filter is on).

NO WRITE, NO TABLE, NO SCHEDULER ROW: this is an on-demand EOD query, so ENGINE_LIVENESS_RULE does not apply.
Nothing here reads a live price or the trading scanner.

Endpoints (both behind the app guard, both JSON-safe):
  GET /api/mobile/custom_screener/meta  -> the registry (key, label, buttons, rule_text) + a live `alone` count
                                           per button + as-of dates + technicals_missing. ONE query: 30 filtered
                                           counts in a single pass over the scored CTE.
  GET /api/mobile/custom_screener/run   -> ?size=large,mid&gvm=good&momentum=strong&invest=high (repeated or
                                           comma values, any subset of the 10 keys) -> {count, rows[<=300],
                                           as_of, applied, excluded_no_technicals?, universe}. ONE query.
                                           Unknown key or button -> {error, known}. Nothing selected -> the
                                           universe count, rows=[], note 'pick at least one filter'.
Row shape: symbol, company_name, segment, cap, gvm, verdict, g, v, m, invest_score (/10), invest_band, price
(EOD close = gvm_scores.price at the GVM score date; labelled in as_of.price_basis), year_return, w52.
Sort: invest_score DESC NULLS LAST, gvm DESC, symbol. Rows capped at 300; `count` is always the full count.
"""
import json
import logging
import time

from fastapi import APIRouter, Request

from mobile_endpoints import _conn, _guard, _json_safe

log = logging.getLogger("scorr.custom_screener")
router = APIRouter()

ROW_CAP = 300
BASIS_LINE = ("EOD only, computed on request from the persisted score tables at each table's latest date; "
              "no write, no table, no scheduler row (ENGINE_LIVENESS_RULE does not apply). Not the TC score.")

# ── THE LOCKED REGISTRY ───────────────────────────────────────────────────────────────────────────────────
# One entry per filter. buttons = (button_key, label, SQL predicate over the `scored` CTE below). Predicates
# are code constants -- no request text ever reaches the SQL -- and every one of them is false on NULL.
REGISTRY = [
    {"key": "size", "label": "Size", "tech": False,
     "source": "mcap_rank_daily at MAX(rank_date), cap_category; 'small' = small + micro",
     "rule_text": "Large = top 100 by market cap, Mid = next 150, Small & micro = the rest",
     "buttons": [("large", "Large", "cap = 'large'"),
                 ("mid", "Mid", "cap = 'mid'"),
                 ("small", "Small & micro", "cap IN ('small', 'micro')")]},
    {"key": "sector", "label": "Sector rating", "tech": False,
     "source": "sector_ratings at MAX(score_date) joined on gvm_scores.segment, verdict",
     "rule_text": "the segment's mcap-weighted GVM verdict, same as the Sector Intel page",
     "buttons": [("good", "Good", "sector_verdict IN ('Good', 'Excellent')"),
                 ("average", "Average", "sector_verdict = 'Average'"),
                 ("weak", "Weak", "sector_verdict = 'Weak'")]},
    {"key": "gvm", "label": "GVM rating", "tech": False,
     "source": "gvm_scores.verdict; Good = Good + Excellent",
     "rule_text": "Good 7+, Average 6-6.99, Weak under 6",
     "buttons": [("good", "Good", "verdict IN ('Good', 'Excellent')"),
                 ("average", "Average", "verdict = 'Average'"),
                 ("weak", "Weak", "verdict = 'Weak'")]},
    {"key": "growth", "label": "Growth", "tech": False,
     "source": "gvm_scores.g_score",
     "rule_text": "G score: Strong 7+, Steady 6-6.99, Slow under 6",
     "buttons": [("strong", "Strong", "g_score >= 7"),
                 ("steady", "Steady", "g_score >= 6 AND g_score < 7"),
                 ("slow", "Slow", "g_score < 6")]},
    {"key": "valuation", "label": "Valuation", "tech": False,
     "source": "gvm_scores.v_score",
     "rule_text": "V score: Reasonable 7+, Fair 6-6.99, Expensive under 6 (a high V means the price is reasonable for the quality)",
     "buttons": [("reasonable", "Reasonable", "v_score >= 7"),
                 ("fair", "Fair", "v_score >= 6 AND v_score < 7"),
                 ("expensive", "Expensive", "v_score < 6")]},
    {"key": "momentum", "label": "Momentum", "tech": False,
     "source": "gvm_scores.m_score",
     "rule_text": "M score: Strong 7+, Neutral 6-6.99, Weak under 6",
     "buttons": [("strong", "Strong", "m_score >= 7"),
                 ("neutral", "Neutral", "m_score >= 6 AND m_score < 7"),
                 ("weak", "Weak", "m_score < 6")]},
    {"key": "invest", "label": "Investment score", "tech": False,
     "source": "investment_check_v2_scores at MAX(score_date), band column (never a retyped threshold)",
     "rule_text": "Investment Score /10, end of day: High 6.5+, Medium 5-6.49, Low under 5",
     "buttons": [("high", "High", "invest_band IN ('STRONG_BUY', 'ACCUMULATE')"),
                 ("medium", "Medium", "invest_band = 'WATCH'"),
                 ("low", "Low", "invest_band = 'AVOID'")]},
    {"key": "trend", "label": "Trend", "tech": True,
     "source": "universe_technicals at MAX(score_date): dma_50 / dma_200 = % distance from the average",
     "rule_text": "price vs its 50-day and 200-day averages",
     "buttons": [("up", "Uptrend", "dma_50 >= 0 AND dma_200 >= 0"),
                 ("mixed", "Mixed", "dma_50 IS NOT NULL AND dma_200 IS NOT NULL AND NOT (dma_50 >= 0 AND dma_200 >= 0) AND NOT (dma_50 < 0 AND dma_200 < 0)"),
                 ("down", "Downtrend", "dma_50 < 0 AND dma_200 < 0")]},
    {"key": "range52", "label": "52-week range", "tech": True,
     "source": "universe_technicals.week_index_52 (0 = the 52-week low, 100 = the high)",
     "rule_text": "where the price sits between its 52-week low (0) and high (100)",
     "buttons": [("high", "Near high", "week_index_52 >= 75"),
                 ("middle", "Middle", "week_index_52 > 25 AND week_index_52 < 75"),
                 ("low", "Near low", "week_index_52 <= 25")]},
    {"key": "ret1y", "label": "1-year return", "tech": True,
     "source": "universe_technicals.year_return (%)",
     "rule_text": "Up = +10% or better, Down = -10% or worse",
     "buttons": [("up", "Up", "year_return >= 10"),
                 ("flat", "Flat", "year_return > -10 AND year_return < 10"),
                 ("down", "Down", "year_return <= -10")]},
]
KNOWN = {f["key"]: [b[0] for b in f["buttons"]] for f in REGISTRY}
_BY_KEY = {f["key"]: f for f in REGISTRY}

# ── THE ONE SCORED CTE ────────────────────────────────────────────────────────────────────────────────────
# Same shape as qb_universe_builder's scored universe (copied, not imported): gvm_scores at its latest
# score_date, every other table joined at ITS OWN latest date. has_tech marks a technicals row for the day.
_SCORED_CTE = """
WITH latest AS (SELECT MAX(score_date) AS d FROM gvm_scores),
     mr_d AS (SELECT MAX(rank_date) AS d FROM mcap_rank_daily),
     sr_d AS (SELECT MAX(score_date) AS d FROM sector_ratings),
     ic_d AS (SELECT MAX(score_date) AS d FROM investment_check_v2_scores),
     ut_d AS (SELECT MAX(score_date) AS d FROM universe_technicals),
     scored AS (
       SELECT g.symbol, g.company_name, g.segment, g.gvm_score, g.verdict, g.g_score, g.v_score, g.m_score, g.price,
              mr.cap_category AS cap, sr.verdict AS sector_verdict,
              ic.score10 AS invest_score, ic.band AS invest_band,
              ut.dma_50, ut.dma_200, ut.week_index_52, ut.year_return,
              (ut.symbol IS NOT NULL) AS has_tech
         FROM gvm_scores g
         LEFT JOIN mcap_rank_daily mr ON mr.symbol = g.symbol AND mr.rank_date = (SELECT d FROM mr_d)
         LEFT JOIN sector_ratings sr ON sr.segment = g.segment AND sr.score_date = (SELECT d FROM sr_d)
         LEFT JOIN investment_check_v2_scores ic ON ic.symbol = g.symbol AND ic.score_date = (SELECT d FROM ic_d)
         LEFT JOIN universe_technicals ut ON ut.symbol = g.symbol AND ut.score_date = (SELECT d FROM ut_d)
        WHERE g.score_date = (SELECT d FROM latest))
"""
_DATES_SQL = ("(SELECT d FROM latest) AS gvm_date, (SELECT d FROM mr_d) AS rank_date, (SELECT d FROM sr_d) AS sector_date, "
              "(SELECT d FROM ic_d) AS invest_date, (SELECT d FROM ut_d) AS technicals_date")


def meta_sql() -> str:
    counts = ", ".join("COUNT(*) FILTER (WHERE %s) AS c_%s_%s" % (b[2], f["key"], b[0]) for f in REGISTRY for b in f["buttons"])
    return (_SCORED_CTE + "SELECT COUNT(*) AS universe, COUNT(*) FILTER (WHERE NOT has_tech) AS technicals_missing, "
            + counts + ", " + _DATES_SQL + " FROM scored")


class _Params:
    """The subset of Starlette's QueryParams the parser uses, so tests can pass a plain dict of lists."""
    def __init__(self, d):
        self.d = {k: (v if isinstance(v, (list, tuple)) else [v]) for k, v in d.items()}
    def keys(self):
        return self.d.keys()
    def getlist(self, k):
        return self.d.get(k, [])


def parse_selection(params):
    """Query string -> {filter_key: [button_keys]} in registry order. Repeated keys and comma lists both work.
    Returns (selection, None) or (None, error_dict)."""
    if not hasattr(params, "getlist"):
        params = _Params(params)
    picked = {}
    for key in params.keys():
        if key not in KNOWN:
            return None, {"error": "unknown filter '%s'" % key, "known": KNOWN}
        vals = []
        for raw in params.getlist(key):
            for tok in str(raw).split(","):
                tok = tok.strip().lower()
                if not tok:
                    continue
                if tok not in KNOWN[key]:
                    return None, {"error": "unknown button '%s' for '%s'" % (tok, key), "known": KNOWN}
                if tok not in vals:
                    vals.append(tok)
        if vals:
            picked[key] = vals
    return {f["key"]: picked[f["key"]] for f in REGISTRY if f["key"] in picked}, None


def where_parts(selection):
    """-> (non_technicals_where, technicals_where, technicals_on). OR inside a filter, AND across filters."""
    non_tech, tech = [], []
    for f in REGISTRY:
        chosen = selection.get(f["key"]) if selection else None
        if not chosen:
            continue
        preds = [b[2] for b in f["buttons"] if b[0] in chosen]
        clause = "(" + " OR ".join("(%s)" % p for p in preds) + ")"
        (tech if f["tech"] else non_tech).append(clause)
    return (" AND ".join(non_tech) or "TRUE", " AND ".join(tech) or "TRUE", bool(tech))


def applied(selection):
    out = []
    for key, chosen in (selection or {}).items():
        f = _BY_KEY[key]
        out.append({"key": key, "label": f["label"], "buttons_chosen": list(chosen),
                    "buttons_label": [b[1] for b in f["buttons"] if b[0] in chosen], "rule_text": f["rule_text"]})
    return out


def run_sql(selection, limit: int = ROW_CAP) -> str:
    """ONE statement: universe, full match count, names dropped for a missing technicals row, the five dates,
    and the first `limit` rows as JSON. Nothing selected -> LIMIT 0 (the count still comes back)."""
    non_tech, tech, _ = where_parts(selection)
    rows_limit = int(limit) if selection else 0
    return (_SCORED_CTE
            + ", base AS (SELECT * FROM scored WHERE " + non_tech + ")"
            + ", matched AS (SELECT * FROM base WHERE " + tech + ")\n"
            "SELECT (SELECT COUNT(*) FROM scored) AS universe, (SELECT COUNT(*) FROM matched) AS total,\n"
            "       (SELECT COUNT(*) FROM base WHERE NOT has_tech) AS excluded_no_technicals, " + _DATES_SQL + ",\n"
            "       (SELECT COALESCE(json_agg(m), '[]'::json) FROM (\n"
            "            SELECT symbol, company_name, segment, cap, ROUND(gvm_score::numeric, 2) AS gvm, verdict,\n"
            "                   ROUND(g_score::numeric, 2) AS g, ROUND(v_score::numeric, 2) AS v, ROUND(m_score::numeric, 2) AS m,\n"
            "                   ROUND(invest_score::numeric, 2) AS invest_score, invest_band, price,\n"
            "                   ROUND(year_return::numeric, 2) AS year_return, ROUND(week_index_52::numeric, 1) AS w52\n"
            "              FROM matched ORDER BY invest_score DESC NULLS LAST, gvm_score DESC, symbol LIMIT %d) m) AS rows" % rows_limit)


def _as_of(row):
    return {"gvm": str(row["gvm_date"]) if row.get("gvm_date") else None,
            "invest": str(row["invest_date"]) if row.get("invest_date") else None,
            "technicals": str(row["technicals_date"]) if row.get("technicals_date") else None,
            "rank": str(row["rank_date"]) if row.get("rank_date") else None,
            "sector": str(row["sector_date"]) if row.get("sector_date") else None,
            "price_basis": "EOD close: gvm_scores.price at the GVM score date"}


def _one(cur):
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, cur.fetchone()))


@router.get("/api/mobile/custom_screener/meta")
@_json_safe
def custom_screener_meta(request: Request):
    g = _guard(request)
    if g:
        return g
    t0 = time.time()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(meta_sql())
        row = _one(cur)
    filters = []
    for f in REGISTRY:
        filters.append({"key": f["key"], "label": f["label"], "rule_text": f["rule_text"], "source": f["source"],
                        "technicals": f["tech"],
                        "buttons": [{"key": b[0], "label": b[1], "alone": int(row.get("c_%s_%s" % (f["key"], b[0])) or 0)}
                                    for b in f["buttons"]]})
    return {"filters": filters, "universe": int(row["universe"] or 0),
            "technicals_missing": int(row["technicals_missing"] or 0), "as_of": _as_of(row),
            "query_ms": int((time.time() - t0) * 1000), "basis": BASIS_LINE}


@router.get("/api/mobile/custom_screener/run")
@_json_safe
def custom_screener_run(request: Request):
    g = _guard(request)
    if g:
        return g
    selection, err = parse_selection(request.query_params)
    if err:
        return err
    _, _, tech_on = where_parts(selection)
    t0 = time.time()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(run_sql(selection))
        row = _one(cur)
    rows = row.get("rows") or []
    if isinstance(rows, str):
        rows = json.loads(rows)
    out = {"count": int(row["total"] or 0), "rows": rows, "universe": int(row["universe"] or 0),
           "as_of": _as_of(row), "applied": applied(selection), "row_cap": ROW_CAP,
           "query_ms": int((time.time() - t0) * 1000), "basis": BASIS_LINE}
    if not selection:
        out["count"] = int(row["universe"] or 0)
        out["rows"] = []
        out["note"] = "pick at least one filter"
    if tech_on:
        out["excluded_no_technicals"] = int(row["excluded_no_technicals"] or 0)
    return out

from fastapi import APIRouter
import psycopg
import os
import re   # cc#827: cap-qualifier stripping for the segment merge layer

router = APIRouter()

def get_conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))

COMPOSITE_SQL = """
WITH base AS (
    SELECT sr.segment, sr.mcap_weighted_gvm AS gvm, sr.weighted_g AS g,
           sr.weighted_v AS v, sr.weighted_m AS m,
           sr.stocks_count, sr.total_mcap, sr.verdict, sr.top_stock, sr.top_stock_gvm,
           sr.score_date::text AS score_date
    FROM sector_ratings sr
    WHERE sr.score_date = (SELECT MAX(score_date) FROM sector_ratings)
),
gvm_delta AS (
    SELECT h_new.segment,
           ROUND(AVG(h_new.gvm_score - h_old.gvm_score)::numeric, 3) AS gvm_change
    FROM gvm_history h_new
    JOIN gvm_history h_old ON h_new.symbol = h_old.symbol
    WHERE h_new.score_date = (SELECT MAX(score_date) FROM gvm_history)
      AND h_old.score_date = (SELECT MIN(score_date) FROM gvm_history)
    GROUP BY h_new.segment
),
screener_agg AS (
    SELECT g.segment,
           ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY (NULLIF(s.fii_change::numeric, 0) + NULLIF(s.dii_change::numeric, 0))
           )::numeric, 2) AS inst_change,
           ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (
               ORDER BY s.qoq_profit_growth::numeric
           )::numeric, 1) AS qoq_profit,
           ROUND(AVG(CASE WHEN s.pe::numeric > 0
               THEN (s.historical_pe::numeric - s.pe::numeric) / s.pe::numeric * 100
               END)::numeric, 1) AS annual_upside
    FROM screener_raw s
    JOIN gvm_scores g ON g.symbol = s.nse_code
    WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
    AND s.qoq_profit_growth IS NOT NULL
    GROUP BY g.segment
),
combined AS (
    SELECT b.segment, b.gvm, b.g, b.v, b.m, b.stocks_count, b.total_mcap,
           b.verdict, b.top_stock, b.top_stock_gvm, b.score_date,
           gd.gvm_change, sa.inst_change, sa.qoq_profit, sa.annual_upside
    FROM base b
    LEFT JOIN gvm_delta gd ON gd.segment = b.segment
    LEFT JOIN screener_agg sa ON sa.segment = b.segment
),
-- cc#827: the five PERCENT_RANK columns that fed composite_score are REMOVED. A repo grep
-- confirmed the sector page was their only consumer (computed here, rendered in
-- scorr_sector.html, referenced nowhere else), so nothing downstream loses a field.
-- Founder ruling: "too many analyses make things paralysis" — GVM answers segment quality.
ranked AS (
    SELECT * FROM combined
),
picks AS (
    SELECT t.segment,
           json_agg(json_build_object(
               'symbol',  t.symbol,
               'gvm',     ROUND(t.gvm_score::numeric, 2),
               -- cc#811: the top-pick day move is the LIVE/last 5-min spot tick against the close
               -- before that tick's own session, falling back to EOD only for a symbol with no
               -- intraday feed at all. Was: latest raw_prices close vs the one before it, which
               -- reads YESTERDAY between 15:30 and the nightly Yahoo refresh, and all day for any
               -- symbol whose EOD row had not landed yet.
               'day_ret', ROUND(((
                     COALESCE(
                       (SELECT i.close FROM intraday_prices i WHERE i.symbol = t.symbol
                          AND i.source IN ('fyers_eq','fyers_ext','fyers_hist') AND i.timeframe='5m'
                          AND i.close IS NOT NULL ORDER BY i.ts DESC LIMIT 1),
                       (SELECT close FROM raw_prices WHERE symbol = t.symbol AND close IS NOT NULL
                          ORDER BY price_date DESC LIMIT 1))
                   - (SELECT close FROM raw_prices WHERE symbol = t.symbol AND close IS NOT NULL
                        AND price_date < COALESCE(
                              (SELECT i.ts::date FROM intraday_prices i WHERE i.symbol = t.symbol
                                 AND i.source IN ('fyers_eq','fyers_ext','fyers_hist') AND i.timeframe='5m'
                                 AND i.close IS NOT NULL ORDER BY i.ts DESC LIMIT 1),
                              (SELECT MAX(price_date) FROM raw_prices WHERE symbol = t.symbol))
                      ORDER BY price_date DESC LIMIT 1)
                 ) / NULLIF((SELECT close FROM raw_prices WHERE symbol = t.symbol AND close IS NOT NULL
                        AND price_date < COALESCE(
                              (SELECT i.ts::date FROM intraday_prices i WHERE i.symbol = t.symbol
                                 AND i.source IN ('fyers_eq','fyers_ext','fyers_hist') AND i.timeframe='5m'
                                 AND i.close IS NOT NULL ORDER BY i.ts DESC LIMIT 1),
                              (SELECT MAX(price_date) FROM raw_prices WHERE symbol = t.symbol))
                      ORDER BY price_date DESC LIMIT 1), 0) * 100)::numeric, 2)
           ) ORDER BY t.rn) AS top_stocks
    FROM (
        SELECT segment, symbol, gvm_score,
               ROW_NUMBER() OVER (PARTITION BY segment ORDER BY gvm_score DESC NULLS LAST, symbol) AS rn
        FROM gvm_scores
        WHERE score_date = (SELECT MAX(score_date) FROM gvm_scores)
          AND segment IS NOT NULL
    ) t
    WHERE t.rn <= 2
    GROUP BY t.segment
)
SELECT
    ranked.segment, ranked.score_date,
    ROUND(ranked.gvm::numeric, 2)           AS gvm,
    ROUND(ranked.g::numeric, 2)             AS g_score,
    ROUND(ranked.v::numeric, 2)             AS v_score,
    ROUND(ranked.m::numeric, 2)             AS m_score,
    ranked.stocks_count, ranked.verdict, ranked.top_stock, ranked.top_stock_gvm,
    ROUND(ranked.total_mcap::numeric, 1)    AS total_mcap,
    ROUND(ranked.gvm_change::numeric, 3)    AS gvm_change,
    ranked.inst_change,
    ranked.qoq_profit,
    ROUND(ranked.annual_upside::numeric, 1) AS annual_upside,
    COALESCE(pk.top_stocks, '[]'::json)     AS top_stocks
FROM ranked
LEFT JOIN picks pk ON pk.segment = ranked.segment
ORDER BY ranked.gvm DESC NULLS LAST
"""

# cc#827 MERGE LAYER. A one-stock segment is noise, not a sector: "Iron and Steel - Smallcap (1)"
# tells you about one company while occupying a row that reads like an industry.
#
# DISPLAY-TIME ONLY. gvm_scores.segment is untouched — it feeds peers, the R card and the screeners,
# and rewriting the canonical segment would ripple through all of them. This maps segment ->
# display_segment from LIVE membership counts, so it self-adjusts as the universe moves instead of
# freezing today's answer into a constant.
#
# Rule: a segment with fewer than MIN_MEMBERS merges UP.
#   (a) strip the cap qualifier and fold into its base FAMILY, if the family reaches MIN_MEMBERS;
#   (b) otherwise into a single catch-all, "Others - Diversified".
# The whole family collapses to the base name, not just the small member — otherwise "Textiles" and
# "Textiles - Large" would sit as separate rows describing the same industry.
MIN_MEMBERS = 5
OTHERS = "Others - Diversified"
_CAP_SUFFIX = re.compile(r"\s*[-–—]?\s*(smallcap|midcap|largecap|small\s*cap|mid\s*cap|large\s*cap|"
                         r"large|small|mid|heavy)\s*$", re.I)


def _family(seg: str) -> str:
    """Base family name: the segment minus any trailing cap qualifier."""
    return _CAP_SUFFIX.sub("", (seg or "").strip()).strip(" -–—") or (seg or "").strip()


def build_display_map(counts: dict) -> dict:
    """{segment: member_count} -> {segment: display_segment}. Pure, so it is testable without a DB."""
    fam_total = {}
    for seg, n in counts.items():
        fam_total[_family(seg)] = fam_total.get(_family(seg), 0) + n
    out = {}
    for seg, n in counts.items():
        if n >= MIN_MEMBERS:
            fam = _family(seg)
            # A healthy segment still collapses into its family when a sibling had to merge in —
            # keeping "Textiles - Large" separate from a merged "Textiles" would defeat the merge.
            out[seg] = fam if (fam != seg and fam_total.get(fam, 0) >= MIN_MEMBERS
                               and any(c < MIN_MEMBERS for s, c in counts.items()
                                       if _family(s) == fam)) else seg
        else:
            fam = _family(seg)
            out[seg] = fam if fam_total.get(fam, 0) >= MIN_MEMBERS else OTHERS
    return out


def _wavg(pairs):
    """Market-cap weighted average, cc#810 doctrine. None when no weight is available — never 0."""
    num = den = 0.0
    for val, w in pairs:
        if val is None or w is None or w <= 0:
            continue
        num += float(val) * float(w); den += float(w)
    return round(num / den, 2) if den > 0 else None


def _verdict_from_gvm(g):
    """cc#827: verdict re-keyed off GVM, since the Score it used to follow is gone.
    Bands are the platform's existing GVM bands (CLAUDE.md / gvm_nightly._verdict):
      >= 8.0 Excellent · 7.0-8.0 Good · 6.0-7.0 Average · < 6.0 Weak."""
    if g is None:
        return None
    g = float(g)
    return "Excellent" if g >= 8 else "Good" if g >= 7 else "Average" if g >= 6 else "Weak"


def _merge_rows(rows):
    """Collapse raw segment rows into display segments, recomputing every aggregate over the merged
    membership (cc#810: mcap-weighted, not a mean of means)."""
    counts = {r["segment"]: int(r.get("stocks_count") or 0) for r in rows}
    dmap = build_display_map(counts)
    groups = {}
    for r in rows:
        groups.setdefault(dmap[r["segment"]], []).append(r)

    merged = []
    for disp, grp in groups.items():
        if len(grp) == 1 and grp[0]["segment"] == disp:
            r = dict(grp[0])
            r["display_segment"] = disp
            r["absorbed"] = []
            r["verdict"] = _verdict_from_gvm(r.get("gvm"))
            merged.append(r)
            continue
        mc = [(g.get("total_mcap") or 0) for g in grp]
        gvm = _wavg([(g.get("gvm"), w) for g, w in zip(grp, mc)])
        row = {
            "segment": disp, "display_segment": disp,
            "score_date": grp[0].get("score_date"),
            "gvm": gvm,
            "g_score": _wavg([(g.get("g_score"), w) for g, w in zip(grp, mc)]),
            "v_score": _wavg([(g.get("v_score"), w) for g, w in zip(grp, mc)]),
            "m_score": _wavg([(g.get("m_score"), w) for g, w in zip(grp, mc)]),
            "gvm_change": _wavg([(g.get("gvm_change"), w) for g, w in zip(grp, mc)]),
            "annual_upside": _wavg([(g.get("annual_upside"), w) for g, w in zip(grp, mc)]),
            "inst_change": _wavg([(g.get("inst_change"), w) for g, w in zip(grp, mc)]),
            "qoq_profit": _wavg([(g.get("qoq_profit"), w) for g, w in zip(grp, mc)]),
            "stocks_count": sum(int(g.get("stocks_count") or 0) for g in grp),
            "total_mcap": round(sum(float(g.get("total_mcap") or 0) for g in grp), 1),
            # UNIVERSE_DENOMINATOR_RULE: a merged row must say what it absorbed, or its member count
            # is an unexplained number.
            "absorbed": sorted(g["segment"] for g in grp),
        }
        row["verdict"] = _verdict_from_gvm(gvm)
        best = max(grp, key=lambda g: (g.get("top_stock_gvm") is not None, g.get("top_stock_gvm") or -1))
        row["top_stock"] = best.get("top_stock")
        row["top_stock_gvm"] = best.get("top_stock_gvm")
        picks = []
        for g in grp:
            tp = g.get("top_stocks") or []
            if isinstance(tp, list):
                picks.extend(tp)
        picks.sort(key=lambda p: (p.get("gvm") is None, -(p.get("gvm") or 0)))
        row["top_stocks"] = picks[:2]
        merged.append(row)
    # cc#827 part_2: ranked by GVM. The composite Score is gone, so GVM is the ordering AND the
    # rating — "too many analyses make things paralysis" (founder).
    merged.sort(key=lambda r: (r.get("gvm") is None, -(r.get("gvm") or 0)))
    return merged


# cc#1700 SEGMENT_SIZE_CLASS_V1 (session_log 38956, founder 04-Sep "top 30 by avg mcap large,
# next 30 mid, remaining small"): band the MERGED (display) segments by average member mcap.
def _add_size_class(rows):
    """avg_mcap = total_mcap / stocks_count on the already-merged row fields — cc#834's own "full
    membership" basis (result_corner.py), reused not reinvented: _merge_rows already sums
    total_mcap and stocks_count across every raw segment a display segment absorbed, so this is
    the SAME aggregation the merge already computed, not a second query. Rank 1-30 LARGE, 31-60
    MID, rest SMALL. A row with no computable average (stocks_count 0 or total_mcap None) gets
    size_class/size_rank None and sorts last — never guessed. mcap_n and mcap_total both read
    stocks_count: sector_ratings carries no separate "members with a market cap" count at the row
    level to split them (checked; result_corner.py's own split works from a per-stock universe
    dict this endpoint does not have), so both UNIVERSE_DENOMINATOR_RULE numbers are equal today
    rather than one of them being invented."""
    for r in rows:
        sc = r.get("stocks_count") or 0
        tm = r.get("total_mcap")
        r["avg_mcap"] = round(tm / sc) if (sc and tm is not None) else None
        r["mcap_n"] = sc
        r["mcap_total"] = sc
    order = sorted(range(len(rows)),
                    key=lambda i: (rows[i]["avg_mcap"] is None, -(rows[i]["avg_mcap"] or 0)))
    rank = 1
    for i in order:
        if rows[i]["avg_mcap"] is None:
            rows[i]["size_rank"] = None
            rows[i]["size_class"] = None
            continue
        rows[i]["size_rank"] = rank
        rows[i]["size_class"] = "LARGE" if rank <= 30 else "MID" if rank <= 60 else "SMALL"
        rank += 1
    return rows


@router.get("/api/sector/rotation")
def sector_rotation():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(COMPOSITE_SQL)
            cols = [d[0] for d in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]
            rows = _merge_rows(raw)
            rows = _add_size_class(rows)
            # cc#827: composite_score is no longer computed at all — see the SQL above.
            # Hot/cold now key off GVM, matching the platform's own bands rather than a percentile
            # rank of five signals that no other surface used.
            cold = [r for r in reversed(rows) if r.get("gvm") is not None and float(r["gvm"]) < 6.0]
            return {
                "score_date": rows[0]["score_date"] if rows else None,
                "total_segments": len(rows),
                "raw_segments": len(raw),
                "merged_away": len(raw) - len(rows),
                "min_members": MIN_MEMBERS,
                "top5": rows[:5],
                "bottom5": cold,   # key kept for HTML compatibility
                "all": rows,
            }
    except Exception as e:
        return {"error": str(e)}

from fastapi import APIRouter
import psycopg
import os

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
ranked AS (
    SELECT *,
           PERCENT_RANK() OVER (ORDER BY gvm)          AS r_gvm,
           PERCENT_RANK() OVER (ORDER BY gvm_change)   AS r_gvm_change,
           PERCENT_RANK() OVER (ORDER BY inst_change)  AS r_inst,
           PERCENT_RANK() OVER (ORDER BY qoq_profit)   AS r_profit,
           PERCENT_RANK() OVER (ORDER BY annual_upside) AS r_upside
    FROM combined
),
picks AS (
    SELECT t.segment,
           json_agg(json_build_object(
               'symbol',  t.symbol,
               'gvm',     ROUND(t.gvm_score::numeric, 2),
               'day_ret', ROUND(((
                     (SELECT close FROM raw_prices WHERE symbol = t.symbol ORDER BY price_date DESC LIMIT 1)
                   - (SELECT close FROM raw_prices WHERE symbol = t.symbol
                        AND price_date < (SELECT MAX(price_date) FROM raw_prices WHERE symbol = t.symbol)
                      ORDER BY price_date DESC LIMIT 1)
                 ) / NULLIF((SELECT close FROM raw_prices WHERE symbol = t.symbol
                        AND price_date < (SELECT MAX(price_date) FROM raw_prices WHERE symbol = t.symbol)
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
    ROUND(((ranked.r_gvm + ranked.r_gvm_change + ranked.r_inst + ranked.r_profit + ranked.r_upside) / 5 * 10)::numeric, 2) AS composite_score,
    COALESCE(pk.top_stocks, '[]'::json)     AS top_stocks
FROM ranked
LEFT JOIN picks pk ON pk.segment = ranked.segment
ORDER BY composite_score DESC
"""

@router.get("/api/sector/rotation")
def sector_rotation():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(COMPOSITE_SQL)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            top5   = rows[:5]
            # Cold = all sectors with composite score < 4.0, worst first
            cold   = [r for r in reversed(rows) if float(r["composite_score"]) < 4.0]
            return {
                "score_date": rows[0]["score_date"] if rows else None,
                "total_segments": len(rows),
                "top5": top5,
                "bottom5": cold,   # key kept for HTML compatibility
                "all": rows,
            }
    except Exception as e:
        return {"error": str(e)}

"""
Investment Check v1.0
Medium-to-long term equity holding filter.
12 rules, single tier, GVM-native.
Spec: session_log category=spec_locked title=investment_check_v1

Verdict bands:
  STRONG BUY  : 10-12
  ACCUMULATE  : 8-9
  WATCH       : 6-7
  AVOID       : <6

Universe counts (as of 15-Jun-2026):
  Strong Buy  : 84  (Large:50, Mid:23, Small:11)
  Accumulate  : 197 (Large:70, Mid:55, Small:72)
  Buy Zone    : 281 of 1761
"""

from fastapi import APIRouter, Query
from typing import Optional
import traceback

router = APIRouter()

# ── Verdict bands ──────────────────────────────────────────────
def get_verdict(score: int) -> dict:
    if score >= 10:
        return {"verdict": "STRONG BUY", "emoji": "🟢", "band": "10-12"}
    elif score >= 8:
        return {"verdict": "ACCUMULATE", "emoji": "🟡", "band": "8-9"}
    elif score >= 6:
        return {"verdict": "WATCH",      "emoji": "🟠", "band": "6-7"}
    else:
        return {"verdict": "AVOID",      "emoji": "🔴", "band": "<6"}

def get_cap_category(market_cap: Optional[float]) -> str:
    if not market_cap:
        return "Unknown"
    if market_cap >= 20000:
        return "Large"
    elif market_cap >= 5000:
        return "Mid"
    return "Small"

BFSI_KEYWORDS = [
    "bank", "nbfc", "insurance", "amc", "finance",
    "capital market", "housing finance"
]

def is_bfsi(segment: str) -> bool:
    seg = (segment or "").lower()
    return any(k in seg for k in BFSI_KEYWORDS)


# ── Single symbol check ────────────────────────────────────────
@router.get("/api/investment-check")
async def investment_check(symbol: str = Query(..., description="NSE symbol")):
    from db import get_conn
    conn = get_conn()
    try:
        symbol = symbol.upper().strip()
        cur = conn.cursor()

        # GVM scores
        cur.execute("""
            SELECT gvm_score, g_score, v_score, m_score, segment, verdict
            FROM gvm_scores WHERE symbol = %s
        """, (symbol,))
        gvm = cur.fetchone()
        if not gvm:
            return {"error": f"Symbol {symbol} not found in GVM universe"}
        gvm_score, g_score, v_score, m_score, segment, gvm_verdict = gvm

        # Screener raw
        cur.execute("""
            SELECT sales_growth_5y, profit_growth_5y, opm, roce,
                   qoq_sales_growth, qoq_profit_growth, market_cap, company_name
            FROM screener_raw WHERE nse_code = %s
        """, (symbol,))
        scr = cur.fetchone()
        if not scr:
            return {"error": f"Symbol {symbol} not found in screener_raw"}
        sales_5y, profit_5y, opm, roce, qoq_sales, qoq_profit, market_cap, company_name = scr

        # Peer averages for segment
        cur.execute("""
            SELECT AVG(s.sales_growth_5y), AVG(s.opm)
            FROM gvm_scores g
            JOIN screener_raw s ON g.symbol = s.nse_code
            WHERE g.segment = %s
        """, (segment,))
        peer = cur.fetchone()
        peer_sales_5y = float(peer[0] or 0)
        peer_opm      = float(peer[1] or 0)

        # v8_metrics latest
        cur.execute("""
            SELECT dma_200, rsi_month, year_return
            FROM v8_metrics WHERE symbol = %s
            ORDER BY computed_at DESC LIMIT 1
        """, (symbol,))
        v8 = cur.fetchone()
        dma_200     = v8[0] if v8 else None
        rsi_month   = v8[1] if v8 else None
        year_return = v8[2] if v8 else None

        # Volume accumulation (20-day)
        cur.execute("""
            WITH lagged AS (
                SELECT close, volume,
                    LAG(close) OVER (ORDER BY price_date) as prev_close
                FROM raw_prices
                WHERE symbol = %s
                  AND price_date >= CURRENT_DATE - INTERVAL '25 days'
            )
            SELECT
                AVG(CASE WHEN close > prev_close THEN volume END) as avg_up,
                AVG(CASE WHEN close < prev_close THEN volume END) as avg_down
            FROM lagged WHERE prev_close IS NOT NULL
        """, (symbol,))
        vol = cur.fetchone()
        avg_up_vol   = float(vol[0] or 0)
        avg_down_vol = float(vol[1] or 0)

        # ── Score rules ────────────────────────────────────────
        bfsi = is_bfsi(segment)
        cap  = get_cap_category(market_cap)

        rules = [
            {
                "rule": "R1", "name": "GVM Score",
                "condition": "≥ 7.0",
                "value": round(float(gvm_score), 2) if gvm_score else None,
                "pass": bool(gvm_score and gvm_score >= 7.0)
            },
            {
                "rule": "R2", "name": "Value Score (V)",
                "condition": "≥ 6.0",
                "value": round(float(v_score), 2) if v_score else None,
                "pass": bool(v_score and v_score >= 6.0)
            },
            {
                "rule": "R3", "name": "Momentum Score (M)",
                "condition": "≥ 6.0",
                "value": round(float(m_score), 2) if m_score else None,
                "pass": bool(m_score and m_score >= 6.0)
            },
            {
                "rule": "R4", "name": "Sales 5Y CAGR vs Peer",
                "condition": "> peer avg",
                "value": round(float(sales_5y), 1) if sales_5y else None,
                "peer": round(peer_sales_5y, 1),
                "pass": bool(sales_5y and float(sales_5y) > peer_sales_5y)
            },
            {
                "rule": "R5", "name": "Profit 5Y CAGR",
                "condition": "> 10%",
                "value": round(float(profit_5y), 1) if profit_5y else None,
                "pass": bool(profit_5y and float(profit_5y) > 10)
            },
            {
                "rule": "R6", "name": "OPM vs Peer",
                "condition": "> peer avg",
                "value": round(float(opm), 1) if opm else None,
                "peer": round(peer_opm, 1),
                "pass": bool(opm and float(opm) > peer_opm)
            },
            {
                "rule": "R7", "name": "ROCE",
                "condition": "≥ 15% (BFSI exempt)",
                "value": round(float(roce), 1) if roce else None,
                "pass": bfsi or bool(roce and float(roce) >= 15),
                "note": "BFSI exempt" if bfsi else None
            },
            {
                "rule": "R8", "name": "Last Result",
                "condition": "QoQ Sales > 0 AND QoQ PAT > 0",
                "value": f"Sales {round(float(qoq_sales),1) if qoq_sales else 'N/A'}% | PAT {round(float(qoq_profit),1) if qoq_profit else 'N/A'}%",
                "pass": bool(qoq_sales and qoq_profit and float(qoq_sales) > 0 and float(qoq_profit) > 0)
            },
            {
                "rule": "R9", "name": "Volume Accumulation",
                "condition": "Up-day vol > Down-day vol (20d)",
                "value": f"Up {round(avg_up_vol/1e5,1)}L | Down {round(avg_down_vol/1e5,1)}L",
                "pass": bool(avg_up_vol and avg_up_vol > avg_down_vol)
            },
            {
                "rule": "R10", "name": "DMA200",
                "condition": "> -5%",
                "value": round(float(dma_200), 2) if dma_200 is not None else None,
                "pass": bool(dma_200 is not None and float(dma_200) > -5)
            },
            {
                "rule": "R11", "name": "RSI Monthly",
                "condition": "35–75",
                "value": round(float(rsi_month), 1) if rsi_month else None,
                "pass": bool(rsi_month and 35 <= float(rsi_month) <= 75)
            },
            {
                "rule": "R12", "name": "1Y Return",
                "condition": "> 0%",
                "value": round(float(year_return), 1) if year_return else None,
                "pass": bool(year_return and float(year_return) > 0)
            },
        ]

        score = sum(1 for r in rules if r["pass"])
        verdict_info = get_verdict(score)

        return {
            "symbol": symbol,
            "company": company_name,
            "segment": segment,
            "cap_category": cap,
            "market_cap_cr": round(float(market_cap), 0) if market_cap else None,
            "gvm_verdict": gvm_verdict,
            "score": score,
            "max_score": 12,
            "verdict": verdict_info["verdict"],
            "emoji": verdict_info["emoji"],
            "band": verdict_info["band"],
            "rules": rules,
            "passes": [r["name"] for r in rules if r["pass"]],
            "fails":  [r["name"] for r in rules if not r["pass"]],
        }

    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}
    finally:
        conn.close()


# ── Screener — top N by score ──────────────────────────────────
@router.get("/api/investment-check/screener")
async def investment_screener(
    verdict: Optional[str] = Query(None, description="STRONG BUY | ACCUMULATE | WATCH | AVOID"),
    cap: Optional[str]     = Query(None, description="Large | Mid | Small"),
    limit: int             = Query(50, le=200)
):
    from db import get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            WITH peer_avgs AS (
                SELECT g.segment, AVG(s.sales_growth_5y) as avg_sales_5y, AVG(s.opm) as avg_opm
                FROM gvm_scores g JOIN screener_raw s ON g.symbol = s.nse_code GROUP BY g.segment
            ),
            bfsi_segs AS (
                SELECT DISTINCT segment FROM gvm_scores
                WHERE segment ILIKE ANY(ARRAY['%%bank%%','%%nbfc%%','%%insurance%%',
                    '%%amc%%','%%finance%%','%%capital market%%','%%housing finance%%'])
            ),
            latest_m AS (
                SELECT DISTINCT ON (symbol) symbol, dma_200, rsi_month, year_return
                FROM v8_metrics ORDER BY symbol, computed_at DESC
            ),
            raw_lag AS (
                SELECT symbol, close, volume,
                    LAG(close) OVER (PARTITION BY symbol ORDER BY price_date) as prev_close
                FROM raw_prices WHERE price_date >= CURRENT_DATE - INTERVAL '25 days'
            ),
            vol_acc AS (
                SELECT symbol,
                    AVG(CASE WHEN close > prev_close THEN volume END) as avg_up,
                    AVG(CASE WHEN close < prev_close THEN volume END) as avg_dn
                FROM raw_lag WHERE prev_close IS NOT NULL GROUP BY symbol
            )
            SELECT
                g.symbol, s.company_name, g.segment,
                ROUND(g.gvm_score::numeric,1) as gvm,
                ROUND(g.g_score::numeric,1) as g,
                ROUND(g.v_score::numeric,1) as v,
                ROUND(g.m_score::numeric,1) as m,
                s.market_cap,
                CASE
                    WHEN s.market_cap >= 20000 THEN 'Large'
                    WHEN s.market_cap >= 5000  THEN 'Mid'
                    ELSE 'Small'
                END as cap,
                (CASE WHEN g.gvm_score>=7 THEN 1 ELSE 0 END +
                 CASE WHEN g.v_score>=6 THEN 1 ELSE 0 END +
                 CASE WHEN g.m_score>=6 THEN 1 ELSE 0 END +
                 CASE WHEN COALESCE(s.sales_growth_5y,0) > COALESCE(pa.avg_sales_5y,0) THEN 1 ELSE 0 END +
                 CASE WHEN COALESCE(s.profit_growth_5y,0) > 10 THEN 1 ELSE 0 END +
                 CASE WHEN COALESCE(s.opm,0) > COALESCE(pa.avg_opm,0) THEN 1 ELSE 0 END +
                 CASE WHEN (g.segment IN (SELECT segment FROM bfsi_segs) OR COALESCE(s.roce,0)>=15) THEN 1 ELSE 0 END +
                 CASE WHEN COALESCE(s.qoq_sales_growth,0)>0 AND COALESCE(s.qoq_profit_growth,0)>0 THEN 1 ELSE 0 END +
                 CASE WHEN va.avg_up > va.avg_dn THEN 1 ELSE 0 END +
                 CASE WHEN m2.dma_200 > -5 THEN 1 ELSE 0 END +
                 CASE WHEN m2.rsi_month BETWEEN 35 AND 75 THEN 1 ELSE 0 END +
                 CASE WHEN COALESCE(m2.year_return,0) > 0 THEN 1 ELSE 0 END
                ) as score
            FROM gvm_scores g
            JOIN screener_raw s ON g.symbol = s.nse_code
            JOIN peer_avgs pa ON g.segment = pa.segment
            LEFT JOIN vol_acc va ON g.symbol = va.symbol
            LEFT JOIN latest_m m2 ON g.symbol = m2.symbol
        """)
        rows = cur.fetchall()
        cols = ["symbol", "company", "segment", "gvm", "g", "v", "m",
                "market_cap", "cap", "score"]

        results = []
        for r in rows:
            row = dict(zip(cols, r))
            sc = row["score"]
            if sc >= 10:   row["verdict"] = "STRONG BUY"
            elif sc >= 8:  row["verdict"] = "ACCUMULATE"
            elif sc >= 6:  row["verdict"] = "WATCH"
            else:          row["verdict"] = "AVOID"

            if verdict and row["verdict"] != verdict.upper():
                continue
            if cap and row["cap"] != cap.capitalize():
                continue
            results.append(row)

        results.sort(key=lambda x: (-x["score"], -(float(x["gvm"] or 0))))
        total = len(results)
        results = results[:limit]

        return {
            "total_matched": total,
            "returned": len(results),
            "filters": {"verdict": verdict, "cap": cap},
            "stocks": results
        }

    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}
    finally:
        conn.close()


# ── Summary stats ──────────────────────────────────────────────
@router.get("/api/investment-check/summary")
async def investment_summary():
    from db import get_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            WITH peer_avgs AS (
                SELECT g.segment, AVG(s.sales_growth_5y) as avg_sales_5y, AVG(s.opm) as avg_opm
                FROM gvm_scores g JOIN screener_raw s ON g.symbol = s.nse_code GROUP BY g.segment
            ),
            bfsi_segs AS (
                SELECT DISTINCT segment FROM gvm_scores
                WHERE segment ILIKE ANY(ARRAY['%%bank%%','%%nbfc%%','%%insurance%%',
                    '%%amc%%','%%finance%%','%%capital market%%','%%housing finance%%'])
            ),
            latest_m AS (
                SELECT DISTINCT ON (symbol) symbol, dma_200, rsi_month, year_return
                FROM v8_metrics ORDER BY symbol, computed_at DESC
            ),
            raw_lag AS (
                SELECT symbol, close, volume,
                    LAG(close) OVER (PARTITION BY symbol ORDER BY price_date) as prev_close
                FROM raw_prices WHERE price_date >= CURRENT_DATE - INTERVAL '25 days'
            ),
            vol_acc AS (
                SELECT symbol,
                    AVG(CASE WHEN close > prev_close THEN volume END) as avg_up,
                    AVG(CASE WHEN close < prev_close THEN volume END) as avg_dn
                FROM raw_lag WHERE prev_close IS NOT NULL GROUP BY symbol
            ),
            scored AS (
                SELECT g.symbol,
                    CASE WHEN s.market_cap >= 20000 THEN 'Large'
                         WHEN s.market_cap >= 5000  THEN 'Mid'
                         ELSE 'Small' END as cap,
                    (CASE WHEN g.gvm_score>=7 THEN 1 ELSE 0 END +
                     CASE WHEN g.v_score>=6 THEN 1 ELSE 0 END +
                     CASE WHEN g.m_score>=6 THEN 1 ELSE 0 END +
                     CASE WHEN COALESCE(s.sales_growth_5y,0) > COALESCE(pa.avg_sales_5y,0) THEN 1 ELSE 0 END +
                     CASE WHEN COALESCE(s.profit_growth_5y,0) > 10 THEN 1 ELSE 0 END +
                     CASE WHEN COALESCE(s.opm,0) > COALESCE(pa.avg_opm,0) THEN 1 ELSE 0 END +
                     CASE WHEN (g.segment IN (SELECT segment FROM bfsi_segs) OR COALESCE(s.roce,0)>=15) THEN 1 ELSE 0 END +
                     CASE WHEN COALESCE(s.qoq_sales_growth,0)>0 AND COALESCE(s.qoq_profit_growth,0)>0 THEN 1 ELSE 0 END +
                     CASE WHEN va.avg_up > va.avg_dn THEN 1 ELSE 0 END +
                     CASE WHEN m2.dma_200 > -5 THEN 1 ELSE 0 END +
                     CASE WHEN m2.rsi_month BETWEEN 35 AND 75 THEN 1 ELSE 0 END +
                     CASE WHEN COALESCE(m2.year_return,0) > 0 THEN 1 ELSE 0 END
                    ) as score
                FROM gvm_scores g
                JOIN screener_raw s ON g.symbol = s.nse_code
                JOIN peer_avgs pa ON g.segment = pa.segment
                LEFT JOIN vol_acc va ON g.symbol = va.symbol
                LEFT JOIN latest_m m2 ON g.symbol = m2.symbol
            )
            SELECT
                COUNT(CASE WHEN score>=10 THEN 1 END) as strong_buy,
                COUNT(CASE WHEN score BETWEEN 8 AND 9 THEN 1 END) as accumulate,
                COUNT(CASE WHEN score BETWEEN 6 AND 7 THEN 1 END) as watch,
                COUNT(CASE WHEN score<6 THEN 1 END) as avoid,
                COUNT(CASE WHEN score>=8 THEN 1 END) as buy_zone,
                COUNT(*) as universe,
                COUNT(CASE WHEN score>=10 AND cap='Large' THEN 1 END) as sb_large,
                COUNT(CASE WHEN score>=10 AND cap='Mid'   THEN 1 END) as sb_mid,
                COUNT(CASE WHEN score>=10 AND cap='Small' THEN 1 END) as sb_small,
                COUNT(CASE WHEN score BETWEEN 8 AND 9 AND cap='Large' THEN 1 END) as acc_large,
                COUNT(CASE WHEN score BETWEEN 8 AND 9 AND cap='Mid'   THEN 1 END) as acc_mid,
                COUNT(CASE WHEN score BETWEEN 8 AND 9 AND cap='Small' THEN 1 END) as acc_small
            FROM scored
        """)
        r = cur.fetchone()
        cols = ["strong_buy","accumulate","watch","avoid","buy_zone","universe",
                "sb_large","sb_mid","sb_small","acc_large","acc_mid","acc_small"]
        d = dict(zip(cols, r))
        return {
            "universe": d["universe"],
            "buy_zone": d["buy_zone"],
            "bands": {
                "strong_buy": {"total": d["strong_buy"], "large": d["sb_large"], "mid": d["sb_mid"], "small": d["sb_small"]},
                "accumulate": {"total": d["accumulate"], "large": d["acc_large"], "mid": d["acc_mid"], "small": d["acc_small"]},
                "watch":  d["watch"],
                "avoid":  d["avoid"],
            },
            "version": "v1.0",
            "spec": "session_log category=spec_locked title=investment_check_v1"
        }
    except Exception as e:
        return {"error": str(e), "trace": traceback.format_exc()}
    finally:
        conn.close()

"""
Native Query Router — Zero token, pure Railway DB queries.
Column names verified against live DB schema 10-Jun-2026.
Rule updates: SHORT side GVM not applicable, sector/RSI/fib rules corrected.
"""

import os
import asyncio
from datetime import datetime
import psycopg

DATABASE_URL = os.getenv("DATABASE_URL", "")


def fmt_table(headers: list, rows: list) -> str:
    if not rows:
        return "No data found."
    col_w = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) for i, h in enumerate(headers)]
    sep  = "| " + " | ".join("-" * w for w in col_w) + " |"
    head = "| " + " | ".join(str(h).ljust(col_w[i]) for i, h in enumerate(headers)) + " |"
    body = "\n".join("| " + " | ".join(str(r[i]).ljust(col_w[i]) for i in range(len(headers))) + " |" for r in rows)
    return f"{head}\n{sep}\n{body}"


def extract_company(text: str) -> str:
    remove = {"hi", "can", "you", "fetch", "get", "show", "me", "the",
              "overview", "takeaway", "key", "gvm", "score", "please",
              "and", "for", "data", "info", "details", "is", "what", "whats"}
    words = [w.strip(".,?!") for w in text.lower().split() if w.strip(".,?!") not in remove]
    return " ".join(words).strip()


def _query_sync(query: str) -> str:
    q = query.lower().strip()

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:

            # 1. Market mood — LIVE ADR from intraday_prices
            if any(k in q for k in ["market mood", "mood", "adr", "gate", "slots"]):
                cur.execute("""
                    SELECT
                        COUNT(CASE WHEN close > open THEN 1 END) as advances,
                        COUNT(CASE WHEN close < open THEN 1 END) as declines,
                        COUNT(CASE WHEN close = open THEN 1 END) as unchanged,
                        ROUND(COUNT(CASE WHEN close > open THEN 1 END)::numeric /
                              NULLIF(COUNT(CASE WHEN close < open THEN 1 END), 0), 2) as live_adr,
                        MAX(ts) as as_of
                    FROM intraday_prices
                    WHERE ts::date = CURRENT_DATE
                      AND source IN ('fyers_eq', 'fyers')
                      AND ts = (SELECT MAX(ts) FROM intraday_prices
                                WHERE ts::date = CURRENT_DATE
                                AND source IN ('fyers_eq', 'fyers'))
                """)
                r = cur.fetchone()
                if not r or r[0] == 0:
                    cur.execute("SELECT price_date, adr, advances, declines, computed_at FROM adr_daily ORDER BY price_date DESC LIMIT 1")
                    r2 = cur.fetchone()
                    if r2:
                        return (f"**Market Mood — {r2[0]} (EOD)**\n"
                                f"ADR: {r2[1]:.2f} | Advances: {r2[2]} | Declines: {r2[3]}\n"
                                f"Updated: {r2[4].strftime('%d-%b %H:%M IST')}")
                    return "No market mood data."
                adv, dec, unch, adr, as_of = r[0], r[1], r[2], r[3], r[4]
                adr_val = float(adr) if adr else 0
                mood = "Bullish" if adr_val >= 2 else "Neutral" if adr_val >= 0.8 else "Bearish"
                time_str = as_of.strftime('%H:%M IST') if as_of else "N/A"
                return (f"**Market Mood — {datetime.now().strftime('%d-%b')} {time_str} (LIVE)**\n"
                        f"ADR: {adr_val:.2f} | {mood}\n"
                        f"Advances: {adv} | Declines: {dec} | Unchanged: {unch}")

            # 2. V8 signals — side derived from basket name
            if any(k in q for k in ["v8", "signal", "qualified", "v8 dashboard"]):
                cur.execute("""
                    SELECT symbol, basket, gvm_score, cmp, day_change, signal_ts
                    FROM v8_qualified
                    WHERE signal_date = CURRENT_DATE
                    ORDER BY gvm_score DESC LIMIT 15
                """)
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1],
                             "LONG" if "buy" in r[1].lower() else "SHORT",
                             f"{r[2]:.1f}", f"{r[3]:.1f}",
                             f"{r[4]:+.2f}%") for r in rows]
                    return f"**V8 Qualified Today ({len(rows)})**\n{fmt_table(['Symbol','Basket','Side','GVM','CMP','Day%'], data)}"
                return "No V8 signals today."

            # 3. QB summary — quant_paper_positions (correct table)
            if any(k in q for k in ["qb", "quant basket", "portfolio", "qb summary"]):
                cur.execute("""
                    SELECT basket_name,
                           COUNT(*) as positions,
                           SUM(pnl) as total_pnl,
                           SUM(current_value) as market_value,
                           ROUND(AVG(pnl_pct)::numeric, 2) as avg_pnl_pct
                    FROM quant_paper_positions
                    WHERE status = 'active'
                    GROUP BY basket_name
                    ORDER BY basket_name
                """)
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1], f"Rs{r[2]:,.0f}", f"Rs{r[3]:,.0f}", f"{r[4]}%") for r in rows]
                    total = sum(r[2] for r in rows)
                    return (f"**QB Summary**\n"
                            f"{fmt_table(['Basket','Pos','PnL','Value','Avg%'], data)}\n"
                            f"Total PnL: Rs {total:,.0f}")
                return "No active QB positions."

            # 4. Paper positions — v8_paper_positions (no pnl/current_price cols)
            if any(k in q for k in ["paper", "open position", "position", "p&l", "pnl"]):
                cur.execute("""
                    SELECT symbol, side, basket, entry_price, target, stop_loss,
                           qty, entry_ts
                    FROM v8_paper_positions
                    WHERE status = 'open'
                    ORDER BY entry_ts DESC LIMIT 10
                """)
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1], r[2],
                             f"{r[3]:.1f}", f"{r[4]:.1f}", f"{r[5]:.1f}",
                             r[6]) for r in rows]
                    return (f"**Open Paper Positions ({len(rows)})**\n"
                            f"{fmt_table(['Symbol','Side','Basket','Entry','Target','SL','Qty'], data)}")
                return "No open paper positions."

            # 5. Top GVM
            if any(k in q for k in ["top gvm", "top stocks", "strong buy"]):
                cur.execute("""
                    SELECT symbol, company_name, gvm_score, verdict, segment
                    FROM gvm_scores WHERE gvm_score >= 8
                    ORDER BY gvm_score DESC LIMIT 10
                """)
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1][:20], f"{r[2]:.2f}", r[3], r[4][:15] if r[4] else '') for r in rows]
                    return f"**Top GVM Stocks (>=8)**\n{fmt_table(['Symbol','Company','GVM','Verdict','Segment'], data)}"
                return "No Strong Buy stocks."

            # 6. Health
            if any(k in q for k in ["health", "status", "system"]):
                cur.execute("SELECT COUNT(*) FROM raw_prices WHERE price_date=(SELECT MAX(price_date) FROM raw_prices)")
                rp = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM gvm_scores")
                gvm = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM intraday_prices WHERE ts::date=CURRENT_DATE")
                intr = cur.fetchone()[0]
                cur.execute("SELECT MAX(price_date) FROM raw_prices")
                latest = cur.fetchone()[0]
                return (f"**System Health — {datetime.now().strftime('%H:%M IST')}**\n"
                        f"raw_prices latest: {latest} ({rp:,} symbols)\n"
                        f"GVM scored: {gvm:,} stocks\n"
                        f"Intraday bars today: {intr:,}\n"
                        f"Railway DB: OK")

            # 7. PCR
            if any(k in q for k in ["pcr", "put call"]):
                cur.execute("""
                    SELECT underlying, pcr, put_oi, call_oi, computed_at
                    FROM pcr_daily ORDER BY computed_at DESC LIMIT 2
                """)
                rows = cur.fetchall()
                if rows:
                    lines = [f"**PCR — {rows[0][4].strftime('%d-%b %H:%M')}**"]
                    for r in rows:
                        lines.append(f"{r[0]}: PCR {r[1]:.3f} | Put OI {r[2]:,} | Call OI {r[3]:,}")
                    return "\n".join(lines)
                return "No PCR data."

            # 8. GVM lookup
            if any(k in q for k in ["gvm", "score"]):
                company = extract_company(q)
                if company:
                    cur.execute("""
                        SELECT symbol, company_name, gvm_score, g_score, v_score, m_score, verdict, segment
                        FROM gvm_scores
                        WHERE UPPER(symbol) LIKE %s OR LOWER(company_name) LIKE %s LIMIT 1
                    """, (f"%{company.upper()}%", f"%{company.lower()}%"))
                    r = cur.fetchone()
                    if r:
                        return (f"**{r[1]} ({r[0]})**\n"
                                f"GVM: {r[2]:.2f} | G: {r[3]:.2f} | V: {r[4]:.2f} | M: {r[5]:.2f}\n"
                                f"Verdict: {r[6]} | Segment: {r[7]}")
                return "Specify stock. E.g. 'GVM SBIN'"

            # 9. Overview / takeaway
            if any(k in q for k in ["overview", "takeaway", "result", "about"]):
                company = extract_company(q)
                if company:
                    cur.execute("""
                        SELECT i.nse_code, i.company_name, i.overview, i.key_takeaway,
                               i.result_analysis, g.gvm_score, g.verdict
                        FROM input_raw i
                        LEFT JOIN gvm_scores g ON i.nse_code = g.symbol
                        WHERE UPPER(i.nse_code) LIKE %s OR LOWER(i.company_name) LIKE %s LIMIT 1
                    """, (f"%{company.upper()}%", f"%{company.lower()}%"))
                    r = cur.fetchone()
                    if r:
                        parts = [f"**{r[1]} ({r[0]})**"]
                        if r[5]: parts.append(f"GVM: {r[5]:.2f} | {r[6]}")
                        if r[2]: parts.append(f"\nOverview:\n{r[2][:500]}")
                        if r[3]: parts.append(f"\nKey Takeaway:\n{r[3][:400]}")
                        if r[4]: parts.append(f"\nResult:\n{r[4][:300]}")
                        return "\n".join(parts)
                return "No content found. Toggle Claude ON for fuzzy search."

            # 10. Generic stock fallback
            company = extract_company(q)
            if company and len(company) >= 3:
                cur.execute("""
                    SELECT g.symbol, g.company_name, g.gvm_score, g.verdict, g.segment,
                           i.key_takeaway
                    FROM gvm_scores g
                    LEFT JOIN input_raw i ON g.symbol = i.nse_code
                    WHERE UPPER(g.symbol) LIKE %s OR LOWER(g.company_name) LIKE %s LIMIT 1
                """, (f"%{company.upper()}%", f"%{company.lower()}%"))
                r = cur.fetchone()
                if r:
                    reply = f"**{r[1]} ({r[0]})**\nGVM: {r[2]:.2f} | {r[3]} | {r[4]}"
                    if r[5]: reply += f"\n\nKey Takeaway:\n{r[5][:400]}"
                    return reply

            return ("⚡ Native — $0. Try:\n"
                    "• 'market mood' | 'V8 dashboard' | 'open positions'\n"
                    "• 'QB summary' | 'top GVM stocks' | 'health' | 'PCR'\n"
                    "• 'overview Bharat Forge' | 'GVM SBIN'\n"
                    "Or toggle Claude ON for free-text.")


async def route_native(query: str) -> str:
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query_sync, query)
    except Exception as e:
        return f"DB error: {str(e)[:200]}\nToggle Claude ON for full access."

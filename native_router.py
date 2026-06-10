"""
Native Query Router — Zero token, pure Railway DB queries.
Uses psycopg3 sync in thread executor (compatible with FastAPI async).
Called when claude_on=False in scorr_chat_endpoint.py
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
    """Synchronous DB query — runs in thread executor."""
    q = query.lower().strip()

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:

            # 1. Market mood
            if any(k in q for k in ["market mood", "mood", "adr", "gate", "slots"]):
                cur.execute("SELECT date, adr, nifty_day_pct, nifty_week_pct, nifty_month_pct, mood, buy_slots, sell_slots FROM adr_daily ORDER BY date DESC LIMIT 1")
                r = cur.fetchone()
                if r:
                    return (f"**Market Mood — {r[0]}**\nMood: {r[5]} | ADR: {r[1]:.2f}\n"
                            f"Nifty D/W/M: {r[2]:.2f}% / {r[3]:.2f}% / {r[4]:.2f}%\nSlots -> Buy: {r[6]} | Sell: {r[7]}")
                return "No market mood data."

            # 2. V8 signals
            if any(k in q for k in ["v8", "signal", "qualified"]):
                cur.execute("SELECT symbol, basket, side, gvm_score, signal_ts FROM v8_signals WHERE DATE(signal_ts) = CURRENT_DATE ORDER BY gvm_score DESC LIMIT 10")
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1], r[2], f"{r[3]:.1f}", str(r[4])[-8:-3]) for r in rows]
                    return f"**V8 Signals Today ({len(rows)})**\n{fmt_table(['Symbol','Basket','Side','GVM','Time'], data)}"
                return "No V8 signals today."

            # 3. QB summary
            if any(k in q for k in ["qb", "quant basket", "portfolio"]):
                cur.execute("SELECT basket_name, COUNT(*), SUM(unrealised_pnl), SUM(current_value) FROM qb_positions GROUP BY basket_name ORDER BY basket_name")
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1], f"{r[2]:,.0f}", f"{r[3]:,.0f}") for r in rows]
                    total = sum(r[2] for r in rows)
                    return f"**QB Summary**\n{fmt_table(['Basket','Pos','PnL (Rs)','Value (Rs)'], data)}\nTotal PnL: Rs {total:,.0f}"
                return "No QB positions."

            # 4. Paper positions
            if any(k in q for k in ["paper", "open position", "position", "p&l", "pnl"]):
                cur.execute("SELECT symbol, side, entry_price, current_price, pnl, pnl_pct FROM v8_paper_positions WHERE status='open' ORDER BY entry_ts DESC LIMIT 10")
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1], f"{r[2]:.1f}", f"{r[3]:.1f}", f"Rs{r[4]:,.0f}", f"{r[5]:.2f}%") for r in rows]
                    total = sum(r[4] for r in rows)
                    return f"**Open Paper Positions ({len(rows)})**\n{fmt_table(['Symbol','Side','Entry','CMP','PnL','PnL%'], data)}\nTotal PnL: Rs {total:,.0f}"
                return "No open paper positions."

            # 5. Top GVM
            if any(k in q for k in ["top gvm", "top stocks", "strong buy"]):
                cur.execute("SELECT symbol, company_name, gvm_score, verdict, sector FROM gvm_scores WHERE gvm_score >= 8 ORDER BY gvm_score DESC LIMIT 10")
                rows = cur.fetchall()
                if rows:
                    data = [(r[0], r[1][:20], f"{r[2]:.2f}", r[3], r[4][:15]) for r in rows]
                    return f"**Top GVM Stocks (>=8)**\n{fmt_table(['Symbol','Company','GVM','Verdict','Sector'], data)}"
                return "No Strong Buy stocks."

            # 6. Health
            if any(k in q for k in ["health", "status", "system"]):
                cur.execute("SELECT COUNT(*) FROM raw_prices WHERE date=(SELECT MAX(date) FROM raw_prices)")
                rp = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM gvm_scores")
                gvm = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM intraday_prices WHERE ts::date=CURRENT_DATE")
                intr = cur.fetchone()[0]
                return (f"**System Health — {datetime.now().strftime('%H:%M IST')}**\n"
                        f"raw_prices: {rp:,} | GVM: {gvm:,} | Intraday today: {intr:,}\nRailway DB: OK")

            # 7. PCR
            if any(k in q for k in ["pcr", "put call"]):
                cur.execute("SELECT underlying, pcr_total, pcr_atm, computed_at FROM pcr_daily ORDER BY computed_at DESC LIMIT 2")
                rows = cur.fetchall()
                if rows:
                    lines = [f"**PCR — {rows[0][3].strftime('%d-%b %H:%M')}**"]
                    for r in rows:
                        lines.append(f"{r[0]}: Total {r[1]:.3f} | ATM {r[2]:.3f}")
                    return "\n".join(lines)
                return "No PCR data."

            # 8. GVM lookup
            if any(k in q for k in ["gvm", "score"]):
                company = extract_company(q)
                if company:
                    cur.execute("SELECT symbol, company_name, gvm_score, g_score, v_score, m_score, verdict, sector FROM gvm_scores WHERE UPPER(symbol) LIKE %s OR LOWER(company_name) LIKE %s LIMIT 1",
                                (f"%{company.upper()}%", f"%{company.lower()}%"))
                    r = cur.fetchone()
                    if r:
                        return f"**{r[1]} ({r[0]})**\nGVM: {r[2]:.2f} | G: {r[3]:.2f} | V: {r[4]:.2f} | M: {r[5]:.2f}\nVerdict: {r[6]} | Sector: {r[7]}"
                return "Specify stock. E.g. 'GVM SBIN'"

            # 9. Overview / takeaway
            if any(k in q for k in ["overview", "takeaway", "result", "about"]):
                company = extract_company(q)
                if company:
                    cur.execute("""
                        SELECT i.symbol, i.company_name, i.overview, i.key_takeaway, i.result_analysis, g.gvm_score, g.verdict
                        FROM input_raw i LEFT JOIN gvm_scores g ON i.symbol = g.symbol
                        WHERE UPPER(i.symbol) LIKE %s OR LOWER(i.company_name) LIKE %s LIMIT 1
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
                    SELECT g.symbol, g.company_name, g.gvm_score, g.verdict, g.sector, i.key_takeaway
                    FROM gvm_scores g LEFT JOIN input_raw i ON g.symbol = i.symbol
                    WHERE UPPER(g.symbol) LIKE %s OR LOWER(g.company_name) LIKE %s LIMIT 1
                """, (f"%{company.upper()}%", f"%{company.lower()}%"))
                r = cur.fetchone()
                if r:
                    reply = f"**{r[1]} ({r[0]})**\nGVM: {r[2]:.2f} | {r[3]} | {r[4]}"
                    if r[5]: reply += f"\n\nKey Takeaway:\n{r[5][:400]}"
                    return reply

            return ("⚡ Native — $0. Try:\n"
                    "• 'market mood' | 'V8 signals' | 'QB summary'\n"
                    "• 'open positions' | 'top GVM stocks' | 'health'\n"
                    "• 'overview Bharat Forge' | 'GVM SBIN' | 'PCR'\n"
                    "Or toggle Claude ON for free-text.")


async def route_native(query: str) -> str:
    """Async wrapper — runs sync DB query in thread executor."""
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query_sync, query)
    except Exception as e:
        return f"DB error: {str(e)[:200]}\nToggle Claude ON for full access."

"""
Native Query Router — Zero token, pure Railway DB queries.
Pattern matches user query → runs SQL → returns formatted card.
Called when claude_on=False in scorr_chat_endpoint.py
"""

import psycopg
import os
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_conn():
    return psycopg.connect(DATABASE_URL)

def fmt_table(headers: list, rows: list) -> str:
    """Format rows as markdown table."""
    if not rows:
        return "No data found."
    col_w = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) for i, h in enumerate(headers)]
    sep  = "| " + " | ".join("-" * w for w in col_w) + " |"
    head = "| " + " | ".join(str(h).ljust(col_w[i]) for i, h in enumerate(headers)) + " |"
    body = "\n".join("| " + " | ".join(str(r[i]).ljust(col_w[i]) for i in range(len(headers))) + " |" for r in rows)
    return f"{head}\n{sep}\n{body}"


def extract_symbol(text: str) -> str | None:
    """Try to extract a stock name/symbol from text."""
    stop = {"hi", "can", "you", "fetch", "get", "show", "me", "the", "a", "an",
            "for", "of", "and", "overview", "takeaway", "key", "gvm", "score",
            "data", "info", "details", "please", "now", "today", "is", "what",
            "whats", "forge", "bharat"}
    words = [w.strip(".,?!") for w in text.lower().split()]
    candidates = [w.upper() for w in words if w not in stop and len(w) >= 3]
    return candidates[0] if candidates else None


def extract_company(text: str) -> str:
    """Extract likely company name for LIKE search."""
    # Remove common command words, return remainder
    remove = {"hi", "can", "you", "fetch", "get", "show", "me", "the",
              "overview", "takeaway", "key", "gvm", "score", "please",
              "and", "for", "data", "info", "details"}
    words = [w.strip(".,?!") for w in text.lower().split() if w.strip(".,?!") not in remove]
    return " ".join(words).strip()


async def route_native(query: str) -> str:
    """
    Pattern match query → DB query → formatted reply. Zero tokens.
    """
    q = query.lower().strip()
    conn = None

    try:
        conn = await get_conn()

        # ── 1. Market mood ─────────────────────────────────────────
        if any(k in q for k in ["market mood", "mood", "adr", "gate", "slots"]):
            rows = await conn.fetch("""
                SELECT date, adr, nifty_day_pct, nifty_week_pct, nifty_month_pct,
                       mood, buy_slots, sell_slots
                FROM adr_daily ORDER BY date DESC LIMIT 1
            """)
            if rows:
                r = rows[0]
                return (f"**Market Mood — {r['date']}**\n"
                        f"Mood: {r['mood']} | ADR: {r['adr']:.2f}\n"
                        f"Nifty D/W/M: {r['nifty_day_pct']:.2f}% / {r['nifty_week_pct']:.2f}% / {r['nifty_month_pct']:.2f}%\n"
                        f"Slots → Buy: {r['buy_slots']} | Sell: {r['sell_slots']}")
            return "No market mood data available."

        # ── 2. V8 signals / qualified ──────────────────────────────
        if any(k in q for k in ["v8", "signal", "qualified now", "qualified"]):
            rows = await conn.fetch("""
                SELECT symbol, basket, side, gvm_score, signal_ts
                FROM v8_signals
                WHERE DATE(signal_ts) = CURRENT_DATE
                ORDER BY gvm_score DESC LIMIT 10
            """)
            if rows:
                headers = ["Symbol", "Basket", "Side", "GVM", "Time"]
                data = [(r['symbol'], r['basket'], r['side'],
                         f"{r['gvm_score']:.1f}", str(r['signal_ts'])[-8:-3]) for r in rows]
                return f"**V8 Signals Today ({len(rows)})**\n{fmt_table(headers, data)}"
            return "No V8 signals today."

        # ── 3. QB summary ──────────────────────────────────────────
        if any(k in q for k in ["qb", "quant basket", "portfolio", "basket summary"]):
            rows = await conn.fetch("""
                SELECT basket_name,
                       COUNT(*) as positions,
                       SUM(unrealised_pnl) as total_pnl,
                       SUM(current_value) as market_value
                FROM qb_positions
                GROUP BY basket_name ORDER BY basket_name
            """)
            if rows:
                headers = ["Basket", "Pos", "PnL (Rs)", "Value (Rs)"]
                data = [(r['basket_name'], r['positions'],
                         f"{r['total_pnl']:,.0f}", f"{r['market_value']:,.0f}") for r in rows]
                total_pnl = sum(r['total_pnl'] for r in rows)
                return f"**QB Summary**\n{fmt_table(headers, data)}\nTotal PnL: Rs {total_pnl:,.0f}"
            return "No QB positions found."

        # ── 4. Paper positions ─────────────────────────────────────
        if any(k in q for k in ["paper", "open position", "position", "p&l", "pnl"]):
            rows = await conn.fetch("""
                SELECT symbol, side, entry_price, current_price, pnl, pnl_pct, entry_ts
                FROM v8_paper_positions
                WHERE status = 'open'
                ORDER BY entry_ts DESC LIMIT 10
            """)
            if rows:
                headers = ["Symbol", "Side", "Entry", "CMP", "PnL", "PnL%"]
                data = [(r['symbol'], r['side'], f"{r['entry_price']:.1f}",
                         f"{r['current_price']:.1f}", f"Rs{r['pnl']:,.0f}",
                         f"{r['pnl_pct']:.2f}%") for r in rows]
                total = sum(r['pnl'] for r in rows)
                return f"**Open Paper Positions ({len(rows)})**\n{fmt_table(headers, data)}\nTotal PnL: Rs {total:,.0f}"
            return "No open paper positions."

        # ── 5. Top GVM stocks ──────────────────────────────────────
        if any(k in q for k in ["top gvm", "top stocks", "best stocks", "strong buy"]):
            rows = await conn.fetch("""
                SELECT symbol, company_name, gvm_score, verdict, sector
                FROM gvm_scores WHERE gvm_score >= 8
                ORDER BY gvm_score DESC LIMIT 10
            """)
            if rows:
                headers = ["Symbol", "Company", "GVM", "Verdict", "Sector"]
                data = [(r['symbol'], r['company_name'][:20], f"{r['gvm_score']:.2f}",
                         r['verdict'], r['sector'][:15]) for r in rows]
                return f"**Top GVM Stocks (>=8)**\n{fmt_table(headers, data)}"
            return "No Strong Buy stocks found."

        # ── 6. Health check ────────────────────────────────────────
        if any(k in q for k in ["health", "status", "system"]):
            rp   = await conn.fetchval("SELECT COUNT(*) FROM raw_prices WHERE date=(SELECT MAX(date) FROM raw_prices)")
            gvm  = await conn.fetchval("SELECT COUNT(*) FROM gvm_scores")
            intr = await conn.fetchval("SELECT COUNT(*) FROM intraday_prices WHERE ts::date=CURRENT_DATE")
            now  = datetime.now().strftime("%H:%M IST")
            return (f"**System Health — {now}**\n"
                    f"raw_prices (latest date): {rp:,} symbols\n"
                    f"GVM scored: {gvm:,} stocks\n"
                    f"Intraday bars today: {intr:,}\n"
                    f"Railway DB: OK")

        # ── 7. PCR ─────────────────────────────────────────────────
        if any(k in q for k in ["pcr", "put call", "options ratio"]):
            rows = await conn.fetch("""
                SELECT underlying, pcr_total, pcr_atm, computed_at
                FROM pcr_daily ORDER BY computed_at DESC LIMIT 2
            """)
            if rows:
                lines = [f"**PCR — {rows[0]['computed_at'].strftime('%d-%b %H:%M')}**"]
                for r in rows:
                    lines.append(f"{r['underlying']}: Total {r['pcr_total']:.3f} | ATM {r['pcr_atm']:.3f}")
                return "\n".join(lines)
            return "No PCR data available."

        # ── 8. GVM lookup (explicit) ───────────────────────────────
        if any(k in q for k in ["gvm", "score", "quality"]):
            company = extract_company(q)
            if company:
                rows = await conn.fetch("""
                    SELECT symbol, company_name, gvm_score, g_score, v_score, m_score, verdict, sector
                    FROM gvm_scores
                    WHERE UPPER(symbol) LIKE $1 OR LOWER(company_name) LIKE $2
                    LIMIT 3
                """, f"%{company.upper()}%", f"%{company.lower()}%")
                if rows:
                    r = rows[0]
                    return (f"**{r['company_name']} ({r['symbol']})**\n"
                            f"GVM: {r['gvm_score']:.2f} | G: {r['g_score']:.2f} | V: {r['v_score']:.2f} | M: {r['m_score']:.2f}\n"
                            f"Verdict: {r['verdict']} | Sector: {r['sector']}")
            return "Specify a stock name. E.g. 'GVM SBIN'"

        # ── 9. Overview / takeaway / result (stock query) ──────────
        if any(k in q for k in ["overview", "takeaway", "key takeaway", "result", "about"]):
            company = extract_company(q)
            if company:
                rows = await conn.fetch("""
                    SELECT i.symbol, i.company_name, i.overview, i.key_takeaway, i.result_analysis,
                           g.gvm_score, g.verdict
                    FROM input_raw i
                    LEFT JOIN gvm_scores g ON i.symbol = g.symbol
                    WHERE UPPER(i.symbol) LIKE $1 OR LOWER(i.company_name) LIKE $2
                    LIMIT 1
                """, f"%{company.upper()}%", f"%{company.lower()}%")
                if rows:
                    r = rows[0]
                    parts = [f"**{r['company_name']} ({r['symbol']})**"]
                    if r['gvm_score']:
                        parts.append(f"GVM: {r['gvm_score']:.2f} | {r['verdict']}")
                    if r['overview']:
                        parts.append(f"\nOverview:\n{r['overview'][:500]}")
                    if r['key_takeaway']:
                        parts.append(f"\nKey Takeaway:\n{r['key_takeaway'][:400]}")
                    if r['result_analysis']:
                        parts.append(f"\nResult Analysis:\n{r['result_analysis'][:300]}")
                    return "\n".join(parts)
                return f"No content found for '{company}'. Toggle Claude ON for fuzzy search."

        # ── 10. Generic stock lookup fallback ──────────────────────
        company = extract_company(q)
        if company and len(company) >= 3:
            rows = await conn.fetch("""
                SELECT g.symbol, g.company_name, g.gvm_score, g.verdict, g.sector,
                       i.key_takeaway
                FROM gvm_scores g
                LEFT JOIN input_raw i ON g.symbol = i.symbol
                WHERE UPPER(g.symbol) LIKE $1 OR LOWER(g.company_name) LIKE $2
                LIMIT 1
            """, f"%{company.upper()}%", f"%{company.lower()}%")
            if rows:
                r = rows[0]
                reply = (f"**{r['company_name']} ({r['symbol']})**\n"
                         f"GVM: {r['gvm_score']:.2f} | {r['verdict']} | {r['sector']}")
                if r['key_takeaway']:
                    reply += f"\n\nKey Takeaway:\n{r['key_takeaway'][:400]}"
                return reply

        # ── Fallback help ──────────────────────────────────────────
        return ("⚡ Native mode — $0 cost. Try:\n"
                "• 'market mood' — ADR + Nifty gate\n"
                "• 'V8 signals' — today's qualified stocks\n"
                "• 'QB summary' — basket PnL\n"
                "• 'open positions' — paper trades\n"
                "• 'top GVM stocks' — Strong Buy list\n"
                "• 'health' — system status\n"
                "• 'overview Bharat Forge' — stock content\n"
                "• 'GVM SBIN' — GVM score\n"
                "Or toggle Claude ON for free-text queries.")

    except Exception as e:
        return f"DB error: {str(e)[:200]}\nToggle Claude ON for full access."
    finally:
        if conn:
            await conn.close()

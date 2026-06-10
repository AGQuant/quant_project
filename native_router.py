"""
Native Query Router — Zero token, pure Railway DB queries.
Column names verified against live DB schema 10-Jun-2026.
Rule updates: SHORT side GVM not applicable, sector/RSI/fib rules corrected.

10-Jun-2026 additions:
  - Virtual Dashboard V8: consolidated 6-table view (market gate, qualified,
    paper summary, closed performance, open positions detail, top 3 signals).
    Live CMP/P&L computed by JOIN to cmp_prices (live Fyers stored in DB).
  - Sector ranking: "top N <sector>" parses N (fallback 10), ranks by GVM,
    flags which have result_analysis.
"""

import os
import re
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
    remove = {
        # question/request words
        "hi", "can", "you", "fetch", "get", "show", "me", "the", "give", "tell",
        "explain", "provide", "find", "search", "look", "up", "check", "pull",
        # content type words
        "overview", "takeaway", "key", "gvm", "score", "result", "analysis",
        "details", "info", "information", "data", "summary", "report",
        # filler words
        "please", "and", "for", "on", "a", "an", "of", "in", "at", "to", "is",
        "what", "whats", "about", "regarding", "related", "some", "any",
    }
    words = [w.strip(".,?!") for w in text.lower().split() if w.strip(".,?!") not in remove]
    return " ".join(words).strip()


def _lookup_company(cur, company: str):
    """
    Try exact phrase match first, then fall back to each word individually.
    Returns first matching row from input_raw joined with gvm_scores.
    """
    # Pass 1: full phrase
    cur.execute("""
        SELECT i.nse_code, i.company_name, i.overview, i.key_takeaway,
               i.result_analysis, g.gvm_score, g.verdict
        FROM input_raw i
        LEFT JOIN gvm_scores g ON i.nse_code = g.symbol
        WHERE UPPER(i.nse_code) LIKE %s OR LOWER(i.company_name) LIKE %s LIMIT 1
    """, (f"%{company.upper()}%", f"%{company.lower()}%"))
    r = cur.fetchone()
    if r:
        return r

    # Pass 2: try each word individually (longest first)
    words = sorted(company.split(), key=len, reverse=True)
    for word in words:
        if len(word) < 3:
            continue
        cur.execute("""
            SELECT i.nse_code, i.company_name, i.overview, i.key_takeaway,
                   i.result_analysis, g.gvm_score, g.verdict
            FROM input_raw i
            LEFT JOIN gvm_scores g ON i.nse_code = g.symbol
            WHERE UPPER(i.nse_code) LIKE %s OR LOWER(i.company_name) LIKE %s LIMIT 1
        """, (f"%{word.upper()}%", f"%{word.lower()}%"))
        r = cur.fetchone()
        if r:
            return r

    return None


def _lookup_gvm(cur, company: str):
    """Same two-pass lookup but against gvm_scores."""
    cur.execute("""
        SELECT symbol, company_name, gvm_score, g_score, v_score, m_score, verdict, segment
        FROM gvm_scores
        WHERE UPPER(symbol) LIKE %s OR LOWER(company_name) LIKE %s LIMIT 1
    """, (f"%{company.upper()}%", f"%{company.lower()}%"))
    r = cur.fetchone()
    if r:
        return r

    words = sorted(company.split(), key=len, reverse=True)
    for word in words:
        if len(word) < 3:
            continue
        cur.execute("""
            SELECT symbol, company_name, gvm_score, g_score, v_score, m_score, verdict, segment
            FROM gvm_scores
            WHERE UPPER(symbol) LIKE %s OR LOWER(company_name) LIKE %s LIMIT 1
        """, (f"%{word.upper()}%", f"%{word.lower()}%"))
        r = cur.fetchone()
        if r:
            return r

    return None


# ── Sector words to strip when extracting the sector term ─────────────────────
_SECTOR_STOPWORDS = {
    "top", "best", "show", "me", "the", "give", "list", "fetch", "get", "stocks",
    "stock", "results", "result", "by", "gvm", "score", "high", "highest", "in",
    "of", "for", "with", "and", "db", "from", "rank", "ranked", "ranking", "a",
    "an", "please", "what", "whats", "are", "is", "companies", "company", "names",
}


def _parse_top_n(q: str, fallback: int = 10) -> int:
    """Parse a number from 'top 10', 'top 5', etc. Fallback if none."""
    m = re.search(r"top\s+(\d+)", q)
    if m:
        n = int(m.group(1))
        return max(1, min(n, 50))  # clamp 1..50
    return fallback


def _extract_sector(q: str) -> str:
    """Strip command/filler words, leaving the sector term."""
    cleaned = re.sub(r"top\s+\d+", "", q)  # remove 'top N'
    words = [w.strip(".,?!") for w in cleaned.lower().split()
             if w.strip(".,?!") not in _SECTOR_STOPWORDS]
    return " ".join(words).strip()


def _sector_ranking(cur, q: str) -> str:
    """
    'top N <sector>' → rank by GVM desc within segment ILIKE %sector%.
    If 'result(s)' present in query, restrict to rows with result_analysis.
    """
    n = _parse_top_n(q, fallback=10)
    sector = _extract_sector(q)
    if not sector or len(sector) < 3:
        return ("Specify a sector. E.g. 'top 10 pharma' | 'top 5 banks' | "
                "'top auto stocks'")

    want_results = "result" in q

    if want_results:
        cur.execute("""
            SELECT g.symbol, g.company_name, g.gvm_score, g.segment,
                   i.result_analysis IS NOT NULL AS has_result
            FROM gvm_scores g
            LEFT JOIN input_raw i ON g.symbol = i.nse_code
            WHERE g.segment ILIKE %s AND i.result_analysis IS NOT NULL
            ORDER BY g.gvm_score DESC
            LIMIT %s
        """, (f"%{sector}%", n))
    else:
        cur.execute("""
            SELECT g.symbol, g.company_name, g.gvm_score, g.segment,
                   i.result_analysis IS NOT NULL AS has_result
            FROM gvm_scores g
            LEFT JOIN input_raw i ON g.symbol = i.nse_code
            WHERE g.segment ILIKE %s
            ORDER BY g.gvm_score DESC
            LIMIT %s
        """, (f"%{sector}%", n))

    rows = cur.fetchall()

    # Retry with singular form if plural returned nothing (e.g. 'banks' -> 'bank')
    if not rows and sector.endswith("s") and len(sector) > 4:
        singular = sector[:-1]
        base_sql = """
            SELECT g.symbol, g.company_name, g.gvm_score, g.segment,
                   i.result_analysis IS NOT NULL AS has_result
            FROM gvm_scores g
            LEFT JOIN input_raw i ON g.symbol = i.nse_code
            WHERE g.segment ILIKE %s {extra}
            ORDER BY g.gvm_score DESC
            LIMIT %s
        """
        extra = "AND i.result_analysis IS NOT NULL" if want_results else ""
        cur.execute(base_sql.format(extra=extra), (f"%{singular}%", n))
        rows = cur.fetchall()
        if rows:
            sector = singular

    if not rows:
        return f"No stocks found for sector '{sector}'. Try a broader term."

    data = [(r[0], r[1][:22], f"{r[2]:.2f}",
             (r[3][:20] if r[3] else ""), ("✓" if r[4] else "—")) for r in rows]
    title_extra = " with results" if want_results else ""
    return (f"**Top {len(rows)} {sector.title()}{title_extra} — by GVM**\n"
            f"{fmt_table(['Symbol','Company','GVM','Segment','Result'], data)}")


# ── Virtual Dashboard V8 — consolidated 6-table view ──────────────────────────

def _vd_market_gate(cur) -> str:
    """Table 1: Market Gate — ADR breadth + gate status."""
    # LIVE ADR from intraday; fall back to EOD adr_daily
    cur.execute("""
        SELECT
            COUNT(CASE WHEN close > open THEN 1 END) as adv,
            COUNT(CASE WHEN close < open THEN 1 END) as dec,
            ROUND(COUNT(CASE WHEN close > open THEN 1 END)::numeric /
                  NULLIF(COUNT(CASE WHEN close < open THEN 1 END), 0), 2) as live_adr,
            MAX(ts) as as_of
        FROM intraday_prices
        WHERE ts::date = CURRENT_DATE
          AND source IN ('fyers_eq', 'fyers')
          AND ts = (SELECT MAX(ts) FROM intraday_prices
                    WHERE ts::date = CURRENT_DATE AND source IN ('fyers_eq', 'fyers'))
    """)
    r = cur.fetchone()
    live = bool(r and r[0] and r[2])
    if live:
        adv, dec, adr_val, as_of = r[0], r[1], float(r[2]), r[3]
        tag = "LIVE"
        time_str = as_of.strftime('%H:%M IST') if as_of else ""
    else:
        cur.execute("SELECT adr, advances, declines, price_date FROM adr_daily ORDER BY price_date DESC LIMIT 1")
        r2 = cur.fetchone()
        if not r2:
            return "**1. Market Gate**\nNo ADR data."
        adr_val, adv, dec = float(r2[0]) if r2[0] else 0.0, r2[1], r2[2]
        tag = f"EOD {r2[3]}"
        time_str = ""

    gate = "OPEN" if adr_val >= 1.0 else "CLOSED"
    mood = "Bullish" if adr_val >= 2 else "Neutral" if adr_val >= 0.8 else "Bearish"
    buy_slots = 5 if adr_val >= 1.0 else 0
    sell_slots = 5 if adr_val < 1.0 else 3
    return (f"**1. Market Gate — {tag} {time_str}**\n"
            f"ADR: {adr_val:.2f} | Gate: {gate} | Mood: {mood}\n"
            f"Advances: {adv} | Declines: {dec} | Buy slots: {buy_slots} | Sell slots: {sell_slots}")


def _vd_qualified(cur) -> str:
    """Table 2: Qualified Today — per basket, symbols if <=5."""
    cur.execute("""
        SELECT basket, COUNT(*),
               string_agg(symbol, ', ' ORDER BY gvm_score DESC) as syms
        FROM v8_qualified
        WHERE signal_date = CURRENT_DATE
        GROUP BY basket ORDER BY basket
    """)
    rows = cur.fetchall()
    if not rows:
        return "**2. Qualified Today**\nNo signals today."
    data = []
    for basket, cnt, syms in rows:
        show = syms if cnt <= 5 else f"{cnt} stocks"
        data.append((basket, cnt, show))
    return f"**2. Qualified Today**\n{fmt_table(['Basket','Count','Symbols'], data)}"


def _vd_paper_summary(cur) -> str:
    """Table 3: Paper Positions Summary — open count + unrealised P&L per basket."""
    cur.execute("""
        SELECT p.basket,
               COUNT(*) as open_cnt,
               SUM(CASE WHEN p.side = 'LONG'  THEN (c.cmp - p.entry_price) * p.qty
                        WHEN p.side = 'SHORT' THEN (p.entry_price - c.cmp) * p.qty
                        ELSE 0 END) as unrealised
        FROM v8_paper_positions p
        LEFT JOIN cmp_prices c ON p.symbol = c.symbol
        WHERE p.status = 'open'
        GROUP BY p.basket ORDER BY p.basket
    """)
    rows = cur.fetchall()
    if not rows:
        return "**3. Paper Positions Summary**\nNo open positions."
    data = [(r[0], r[1], f"{(r[2] or 0):+,.0f}") for r in rows]
    total = sum((r[2] or 0) for r in rows)
    out = fmt_table(['Basket', 'Open', 'Unrealised P&L'], data)
    return f"**3. Paper Positions Summary**\n{out}\nTotal Unrealised: {total:+,.0f}"


def _vd_closed_performance(cur) -> str:
    """Table 4: Closed Performance — wins/accuracy/realised P&L per basket."""
    cur.execute("""
        SELECT basket,
               COUNT(*) as closed,
               COUNT(*) FILTER (WHERE result = 'TARGET') as wins,
               SUM(pnl) as realised
        FROM v8_paper_trades
        GROUP BY basket ORDER BY basket
    """)
    rows = cur.fetchall()
    if not rows:
        return "**4. Closed Performance**\nNo closed trades."
    data, tot_closed, tot_wins, tot_real = [], 0, 0, 0.0
    for basket, closed, wins, realised in rows:
        acc = round(wins / closed * 100, 1) if closed else 0.0
        data.append((basket, closed, wins, f"{acc}%", f"{(realised or 0):+,.0f}"))
        tot_closed += closed; tot_wins += wins; tot_real += (realised or 0)
    tot_acc = round(tot_wins / tot_closed * 100, 1) if tot_closed else 0.0
    data.append(("TOTAL", tot_closed, tot_wins, f"{tot_acc}%", f"{tot_real:+,.0f}"))
    return f"**4. Closed Performance**\n{fmt_table(['Basket','Closed','Wins','Acc%','Realised P&L'], data)}"


def _vd_open_detail(cur) -> str:
    """Table 5: Open Positions Detail — live CMP/P&L, sorted P&L desc."""
    cur.execute("""
        SELECT p.symbol, p.side, p.basket, p.entry_price, c.cmp,
               CASE WHEN p.side = 'LONG'  THEN (c.cmp - p.entry_price) * p.qty
                    WHEN p.side = 'SHORT' THEN (p.entry_price - c.cmp) * p.qty
                    ELSE 0 END as pnl,
               p.entry_ts
        FROM v8_paper_positions p
        LEFT JOIN cmp_prices c ON p.symbol = c.symbol
        WHERE p.status = 'open'
        ORDER BY pnl DESC NULLS LAST
        LIMIT 15
    """)
    rows = cur.fetchall()
    if not rows:
        return "**5. Open Positions Detail**\nNo open positions."
    data = []
    for sym, side, basket, entry, cmp, pnl, ts in rows:
        cmp_s = f"{cmp:.1f}" if cmp is not None else "—"
        pnl_s = f"{pnl:+,.0f}" if pnl is not None else "—"
        t = ts.strftime('%d-%b %H:%M') if ts else ""
        data.append((sym, side, basket, f"{entry:.1f}", cmp_s, pnl_s, t))
    return f"**5. Open Positions Detail**\n{fmt_table(['Symbol','Side','Basket','Entry','CMP','P&L','Entry'], data)}"


def _vd_top_signals(cur) -> str:
    """Table 6: Top 3 Signals Now — best by GVM across all baskets today."""
    cur.execute("""
        SELECT symbol, basket, gvm_score, day_change, week_return
        FROM v8_qualified
        WHERE signal_date = CURRENT_DATE
        ORDER BY gvm_score DESC LIMIT 3
    """)
    rows = cur.fetchall()
    if not rows:
        return "**6. Top 3 Signals Now**\nNo signals today."
    data = [(r[0], r[1], f"{r[2]:.2f}",
             f"{(r[3] or 0):+.2f}%", f"{(r[4] or 0):+.2f}%") for r in rows]
    return f"**6. Top 3 Signals Now**\n{fmt_table(['Symbol','Basket','GVM','Day%','Week%'], data)}"


def _virtual_dashboard(cur) -> str:
    """Consolidated Virtual Dashboard V8 — all 6 tables."""
    ts = datetime.now().strftime('%d-%b-%Y %H:%M IST')
    parts = [
        f"⚡ **VIRTUAL DASHBOARD V8 — {ts}**",
        _vd_market_gate(cur),
        _vd_qualified(cur),
        _vd_paper_summary(cur),
        _vd_closed_performance(cur),
        _vd_open_detail(cur),
        _vd_top_signals(cur),
    ]
    return "\n\n".join(parts)


def _query_sync(query: str) -> str:
    q = query.lower().strip()

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:

            # 0. Virtual Dashboard V8 — consolidated view (MUST be before generic v8)
            if "virtual dashboard" in q or "v8 dashboard" in q:
                return _virtual_dashboard(cur)

            # 0b. Sector ranking — "top N <sector>" (before market mood / top gvm)
            if re.search(r"\btop\b", q) and not any(
                k in q for k in ["top gvm", "top stocks", "top qualified"]
            ):
                sector_try = _extract_sector(q)
                if sector_try and len(sector_try) >= 3:
                    return _sector_ranking(cur, q)

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

            # 8. Overview / takeaway — uses two-pass fuzzy lookup
            if any(k in q for k in ["overview", "takeaway", "result", "about"]):
                company = extract_company(q)
                if company:
                    r = _lookup_company(cur, company)
                    if r:
                        parts = [f"**{r[1]} ({r[0]})**"]
                        if r[5]: parts.append(f"GVM: {r[5]:.2f} | {r[6]}")
                        if r[2]: parts.append(f"\nOverview:\n{r[2][:500]}")
                        if r[3]: parts.append(f"\nKey Takeaway:\n{r[3][:400]}")
                        if r[4]: parts.append(f"\nResult:\n{r[4][:300]}")
                        return "\n".join(parts)
                return "No content found. Toggle Claude ON for fuzzy search."

            # 9. GVM lookup — uses two-pass fuzzy lookup
            if any(k in q for k in ["gvm", "score"]):
                company = extract_company(q)
                if company:
                    r = _lookup_gvm(cur, company)
                    if r:
                        return (f"**{r[1]} ({r[0]})**\n"
                                f"GVM: {r[2]:.2f} | G: {r[3]:.2f} | V: {r[4]:.2f} | M: {r[5]:.2f}\n"
                                f"Verdict: {r[6]} | Segment: {r[7]}")
                return "Specify stock. E.g. 'GVM SBIN'"

            # 10. Generic stock fallback — uses two-pass fuzzy lookup
            company = extract_company(q)
            if company and len(company) >= 3:
                r = _lookup_company(cur, company)
                if r:
                    reply = f"**{r[1]} ({r[0]})**"
                    if r[5]: reply += f"\nGVM: {r[5]:.2f} | {r[6]}"
                    if r[3]: reply += f"\n\nKey Takeaway:\n{r[3][:400]}"
                    return reply

            return ("⚡ Native — $0. Try:\n"
                    "• 'Virtual Dashboard V8' | 'market mood' | 'open positions'\n"
                    "• 'QB summary' | 'top GVM stocks' | 'health' | 'PCR'\n"
                    "• 'top 10 pharma' | 'overview Bharat Forge' | 'GVM SBIN'\n"
                    "Or toggle Claude ON for free-text.")


async def route_native(query: str) -> str:
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _query_sync, query)
    except Exception as e:
        return f"DB error: {str(e)[:200]}\nToggle Claude ON for full access."

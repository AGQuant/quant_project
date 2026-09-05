"""
max_native_cards.py — cc#836 PHASE B: a native card template for every remaining IVR leaf.

Phase A shipped the tree and wired the 42 leaves that mapped to existing intents. The other 41 were
seeded and visible but rendered an honest "building this week" state. This is that build — the
intent_gap_list from MAX_IVR_TREE_V2 (session_log id=13822), stage 2 of the founder's 3-stage plan.

THE INVARIANTS THESE CARDS HOLD (13823 / 851):
  * $0. Every card is a DB read. No card may call a paid model, now or by later edit — which is why
    they all live behind ONE dispatch here rather than each leaf growing its own path.
  * LOCKED v1 FORMAT. Title, body, and a footer stating the DATA BASIS and the as-of date. A card
    that cannot say what it read and when is not a card, it is a claim.
  * NEVER FABRICATE. A card with no data says so and says why. "--" beats an invented zero — the
    cc#828 lesson, and the reason the empty branches below are as carefully worded as the full ones.
  * RA-SAFE BY CONSTRUCTION. These render DATA. No card recommends, targets, or advises; tip-shaped
    questions route here precisely so they get a GVM/Trade-Check answer instead of advice prose.
  * SYMBOL-FIRST TABLES. The IVR front end appends the shared C·A·R·D row strip to any table whose
    first cell is a symbol (cc#836 phase A / cc#789), so putting the symbol first is what makes every
    answer one tap from the flagship deep-dive. That is a layout contract, not a style preference.
"""

import os
import logging
from typing import Optional, Dict, Any, List

import psycopg2
from fastapi import APIRouter

# cc#1426: screener_raw.segment_pe ("Industry PE") is a dead column — falls back to Scorr's own
# segment peer-avg PE, honestly labelled. See segment_pe_fallback.py.
from segment_pe_fallback import segment_pe_map, lookup as _pe_lookup

log = logging.getLogger("scorr.max_cards")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg2.connect(_DB)


def _tbl(headers: List[str], rows: List[tuple]) -> str:
    """Markdown pipe table — the same shape native_router.fmt_table emits, so a Max answer looks
    identical whether it came from the old grammar or from here."""
    if not rows:
        return ""
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join("—" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def _n(v, nd=1, suffix=""):
    """One-decimal contract (cc#823). None stays None so _tbl prints an em dash, never a zero."""
    if v is None:
        return None
    try:
        return f"{float(v):,.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return None


def _sgn(v, nd=1):
    if v is None:
        return None
    try:
        f = float(v)
        return f"{'+' if f >= 0 else ''}{f:,.{nd}f}%"
    except (TypeError, ValueError):
        return None


def _card(title: str, body: str, basis: str, asof=None) -> Dict[str, Any]:
    foot = f"_Native · $0 · {basis}"
    if asof:
        foot += f" · as of {asof}"
    foot += "_"
    return {"title": title, "body": (body or "").rstrip() + "\n\n" + foot, "basis": basis,
            "as_of": (str(asof) if asof else None)}


def _empty(title: str, why: str, basis: str) -> Dict[str, Any]:
    return _card(title, f"**No data to show.**\n\n{why}", basis)


def _need(param: str) -> Dict[str, Any]:
    return {"error": f"missing required parameter: {param}"}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# ANALYZE — per-symbol
# ══════════════════════════════════════════════════════════════════════════════════════════════

def why_moving(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    cur.execute("""SELECT price_date, open, close, volume,
                          (SELECT AVG(volume) FROM (SELECT volume FROM raw_prices r2
                             WHERE r2.symbol=r.symbol AND r2.price_date < r.price_date
                             ORDER BY r2.price_date DESC LIMIT 21) x) AS avg21
                   FROM raw_prices r WHERE symbol=%s AND close>0
                   ORDER BY price_date DESC LIMIT 1""", (sym,))
    row = cur.fetchone()
    if not row:
        return _empty(f"Why is {sym} moving?", f"No price history for {sym} in raw_prices.", "raw_prices")
    d, o, c, vol, avg21 = row
    day_pct = ((float(c) / float(o) - 1) * 100) if o else None
    rvol = (float(vol) / float(avg21)) if (vol and avg21) else None
    body = _tbl(["Metric", "Value"], [
        ("Close", _n(c, 2)),
        ("Day move", _sgn(day_pct)),
        ("Volume", _n(vol, 0)),
        ("RVOL (vs 21d avg)", _n(rvol, 2, "x")),
    ])
    # A move without volume behind it is noise wearing a candle — say so rather than leave the
    # reader to infer it.
    if rvol is not None:
        body += ("\n\n**Participation:** " + ("elevated — the move carries real volume."
                 if rvol >= 1.5 else "ordinary — this move is not backed by unusual volume."))
    try:
        from news_endpoints import polished_company_filter
        s, prm = polished_company_filter()
        cur.execute(f"""SELECT headline, display_time::date FROM v_polished_articles
                        WHERE (symbol=%s OR mentioned_symbols @> ARRAY[%s]::text[]) {s}
                        ORDER BY display_time DESC LIMIT 3""", [sym, sym] + prm)
        news = cur.fetchall()
        if news:
            body += "\n\n**Polished coverage**\n" + "\n".join(f"- {h} _({dt})_" for h, dt in news)
        else:
            body += "\n\n**Polished coverage:** none in the window — the move has no news attached here."
    except Exception as e:
        log.warning("why_moving news leg failed for %s: %s", sym, e)
    return _card(f"Why is {sym} moving?", body, "raw_prices + polished_news", d)


def levels(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    cur.execute("""SELECT pp, r1, r2, s1, s2, week_index_52, score_date
                   FROM universe_technicals WHERE symbol=%s
                   ORDER BY score_date DESC LIMIT 1""", (sym,))
    ut = cur.fetchone()
    cur.execute("""SELECT MAX(high), MIN(low), MAX(price_date) FROM raw_prices
                   WHERE symbol=%s AND price_date >= CURRENT_DATE - 365""", (sym,))
    hi, lo, last_d = cur.fetchone() or (None, None, None)
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND close>0
                   ORDER BY price_date DESC LIMIT 1""", (sym,))
    cr = cur.fetchone()
    cmp_v = float(cr[0]) if cr else None
    if not ut and cmp_v is None:
        return _empty(f"{sym} · Levels", f"No technicals or price history for {sym}.", "universe_technicals + raw_prices")
    rows = [("CMP", _n(cmp_v, 2))]
    if ut:
        pp, r1, r2, s1, s2, wk52, sd = ut
        rows += [("R2", _n(r2, 2)), ("R1", _n(r1, 2)), ("Pivot", _n(pp, 2)),
                 ("S1", _n(s1, 2)), ("S2", _n(s2, 2)), ("52W range position", _n(wk52, 1, "%"))]
    else:
        sd = last_d
    if hi and cmp_v:
        rows.append(("52W high", f"{float(hi):,.2f}  ({_sgn((cmp_v/float(hi)-1)*100)} away)"))
    if lo and cmp_v:
        rows.append(("52W low", f"{float(lo):,.2f}  ({_sgn((cmp_v/float(lo)-1)*100)} away)"))
    return _card(f"{sym} · Levels", _tbl(["Level", "Value"], rows),
                 "universe_technicals pivots + raw_prices 52W", sd)


def fin_snapshot(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    try:
        import gvm_company_report as gcr
        q_sales = gcr._quarter_vals(cur, sym, ["Sales", "Revenue"])
        q_np = gcr._quarter_vals(cur, sym, ["Net Profit"])
        ttm_s, ttm_p = gcr._ttm_growth(q_sales), gcr._ttm_growth(q_np)
    except Exception as e:
        log.warning("fin_snapshot TTM leg failed for %s: %s", sym, e)
        ttm_s = ttm_p = None
    cur.execute("""SELECT s.pe, s.opm, s.roce, s."Return on equity", s.segment_pe, g.segment, g.gvm_score
                   FROM gvm_scores g LEFT JOIN screener_raw s ON s.nse_code=g.symbol
                   WHERE g.symbol=%s AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)""", (sym,))
    r = cur.fetchone()
    if not r and ttm_s is None:
        return _empty(f"{sym} · Financials", f"{sym} is not in gvm_scores and has no quarterly series.",
                      "fundamentals_history + screener_raw")
    pe, opm, roce, roe, seg_pe, seg, gvm = (r or (None,) * 7)
    # cc#1426: screener_raw.segment_pe is a dead column (always None) — fall back to Scorr's own
    # segment peer-avg PE, and relabel the row honestly so it is never read as Screener's Industry PE.
    seg_pe_label = "Segment PE"
    if seg_pe is None:
        seg_pe, basis = _pe_lookup(segment_pe_map(cur), seg)
        if basis:
            seg_pe_label = f"Segment PE · {basis}"   # e.g. "Segment PE · peer avg PE (n=13)"
    rows = [
        ("TTM sales growth", _sgn(ttm_s)),
        ("TTM profit growth", _sgn(ttm_p)),
        ("Operating margin", _n(opm, 1, "%")),
        ("ROCE", _n(roce, 1, "%")),
        ("ROE", _n(roe, 1, "%")),
        ("PE", _n(pe, 1)),
        (f"{seg_pe_label} ({seg or '—'})", _n(seg_pe, 1)),
        ("GVM", _n(gvm, 2)),
    ]
    body = _tbl(["Metric", "Value"], rows)
    if ttm_s is None or ttm_p is None:
        # cc#832 basis, restated here so the dash is explained where it is seen.
        body += ("\n\n_TTM growth needs 8 clean quarters (latest 4 vs the 4 before). Where it shows "
                 "—, this symbol does not have them; no annual figure is substituted._")
    return _card(f"{sym} · Financials snapshot", body,
                 "fundamentals_history quarters (TTM, cc#832 basis) + screener_raw")


def shareholding(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    cur.execute("""SELECT period_label, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section='shareholding'
                   ORDER BY period_end DESC LIMIT 8""", (sym,))
    rows = cur.fetchall()
    if not rows:
        return _empty(f"{sym} · Shareholding", f"No shareholding series stored for {sym}.",
                      "fundamentals_history")
    rows = list(reversed(rows))
    keys = ["Promoters", "FIIs", "DIIs", "Public"]
    out = []
    for k in keys:
        vals = [(m or {}).get(k) for _, m in rows]
        if all(v is None for v in vals):
            continue
        out.append(tuple([k] + [v if v is not None else None for v in vals]))
    if not out:
        return _empty(f"{sym} · Shareholding", "The stored series carries none of the standard holder rows.",
                      "fundamentals_history")
    return _card(f"{sym} · Shareholding trend",
                 _tbl(["Holder"] + [lbl for lbl, _ in rows], out),
                 "fundamentals_history shareholding", rows[-1][0])


def dividend(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    cur.execute("""SELECT dividend_yield FROM screener_raw WHERE nse_code=%s LIMIT 1""", (sym,))
    dy = cur.fetchone()
    cur.execute("""SELECT period_label, metrics->>'Dividend Payout %%' FROM fundamentals_history
                   WHERE symbol=%s AND section='profit-loss' AND period_type='annual'
                   ORDER BY period_end DESC LIMIT 6""", (sym,))
    pay = [r for r in cur.fetchall() if r[1] is not None]
    if not dy and not pay:
        return _empty(f"{sym} · Dividend", f"No dividend yield or payout history stored for {sym}.",
                      "screener_raw + fundamentals_history")
    body = _tbl(["Metric", "Value"], [("Dividend yield", _n(dy[0] if dy else None, 2, "%"))])
    if pay:
        body += "\n\n**Payout ratio history**\n" + _tbl(["Year", "Payout"], list(reversed(pay)))
    return _card(f"{sym} · Dividend & yield", body, "screener_raw + fundamentals_history payout")


def polished_news_symbol(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    from news_endpoints import polished_company_filter
    s, prm = polished_company_filter()
    cur.execute(f"""SELECT headline, category, source_name, display_time::date
                    FROM v_polished_articles
                    WHERE (symbol=%s OR mentioned_symbols @> ARRAY[%s]::text[]) {s}
                    ORDER BY display_time DESC LIMIT 8""", [sym, sym] + prm)
    rows = cur.fetchall()
    if not rows:
        # cc#802/830 rule: never fall back to raw. A shorter card beats a link-out.
        return _empty(f"{sym} · Polished news", "No polished coverage yet for this symbol.",
                      "polished_news (cc#830 scope)")
    return _card(f"{sym} · Polished news",
                 _tbl(["Headline", "Category", "Source", "Date"], rows),
                 "polished_news, cc#830 scope (Domestic / AI Editorial / Stock Views)")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# SIGNALS
# ══════════════════════════════════════════════════════════════════════════════════════════════

def paper_pivots(cur, p):
    cur.execute("""SELECT symbol, pp, r1, r2, s1, s2, pivot_date FROM v8_paper_pivots
                   WHERE pivot_date=(SELECT MAX(pivot_date) FROM v8_paper_pivots)
                   ORDER BY symbol LIMIT 40""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Pivot levels", "No pivot rows built yet.", "v8_paper_pivots")
    d = rows[0][6]
    return _card("Pivot levels · V8 paper universe",
                 _tbl(["Symbol", "Pivot", "R1", "R2", "S1", "S2"],
                      [(r[0], _n(r[1], 2), _n(r[2], 2), _n(r[3], 2), _n(r[4], 2), _n(r[5], 2)) for r in rows]),
                 "v8_paper_pivots", d)


def oi_buildup(cur, p):
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, spot_close, basis_pct, oi, oi_chg, ts
                   FROM futures_basis WHERE oi IS NOT NULL AND oi_chg IS NOT NULL
                   ORDER BY symbol, ts DESC""")
    rows = cur.fetchall()
    if not rows:
        return _empty("OI buildup", "No futures basis / OI rows available.", "futures_basis")
    asof = max(r[5] for r in rows)
    graded = []
    for sym, spot, bp, oi, oichg, ts in rows:
        if oichg is None or bp is None:
            continue
        # Price direction proxied by the basis sign (cc#819: same source, correctly anchored).
        up = float(bp) >= 0
        oi_up = float(oichg) > 0
        label = ("Long buildup" if (up and oi_up) else "Short buildup" if (not up and oi_up)
                 else "Short covering" if up else "Long unwinding")
        graded.append((sym, label, _n(oi, 0), _sgn(float(oichg) / (float(oi) - float(oichg)) * 100)
                       if (oi and float(oi) != float(oichg)) else None, _n(bp, 2, "%")))
    graded.sort(key=lambda x: x[1])
    body = _tbl(["Symbol", "Buildup", "OI", "OI change", "Basis"], graded[:30])
    body += ("\n\n_Buildup is price direction paired with open-interest direction: price up + OI up = "
             "long buildup; price down + OI up = short buildup; price up + OI down = short covering; "
             "price down + OI down = long unwinding._")
    return _card("OI long / short buildup", body, "futures_basis (cc#819 anchor)", asof)


def rvol_spikes(cur, p):
    cur.execute("""
        WITH latest AS (SELECT MAX(price_date) d FROM raw_prices),
        t AS (SELECT r.symbol, r.volume, r.close, r.open,
                     AVG(r2.volume) AS avg21
              FROM raw_prices r
              JOIN LATERAL (SELECT volume FROM raw_prices x WHERE x.symbol=r.symbol
                            AND x.price_date < r.price_date ORDER BY x.price_date DESC LIMIT 21) r2 ON true
              WHERE r.price_date=(SELECT d FROM latest) AND r.volume>0
              GROUP BY r.symbol, r.volume, r.close, r.open)
        SELECT t.symbol, ROUND((t.volume/NULLIF(t.avg21,0))::numeric,2) rvol,
               ROUND(((t.close/NULLIF(t.open,0)-1)*100)::numeric,2) day_pct,
               (SELECT d FROM latest)
        FROM t JOIN gvm_scores g ON g.symbol=t.symbol
                             AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
        WHERE (t.volume/NULLIF(t.avg21,0)) >= 2.0
        ORDER BY rvol DESC LIMIT 25""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Volume spikes (RVOL)", "No symbol traded at 2x its 21-day average volume in the last session.",
                      "raw_prices")
    return _card("Volume spikes · RVOL ≥ 2x",
                 _tbl(["Symbol", "RVOL", "Day %"], [(r[0], f"{r[1]}x", _sgn(r[2])) for r in rows]),
                 "raw_prices, latest vs 21-day average volume", rows[0][3])


def breakouts_today(cur, p):
    cur.execute("""SELECT ut.symbol, ROUND(ut.week_index_52::numeric,1), ROUND(g.gvm_score::numeric,2),
                          ROUND(ut.year_return::numeric,1), ut.score_date
                   FROM universe_technicals ut
                   JOIN gvm_scores g ON g.symbol=ut.symbol
                                    AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                   WHERE ut.score_date=(SELECT MAX(score_date) FROM universe_technicals)
                     AND ut.week_index_52 >= 98
                   ORDER BY ut.week_index_52 DESC, g.gvm_score DESC LIMIT 25""")
    rows = cur.fetchall()
    if not rows:
        return _empty("52W-high breakouts", "Nothing is sitting in the top 2% of its 52-week range.",
                      "universe_technicals")
    return _card("52W-high breakouts",
                 _tbl(["Symbol", "52W position", "GVM", "1Y return"],
                      [(r[0], f"{r[1]}%", r[2], _sgn(r[3])) for r in rows]),
                 "universe_technicals week_index_52 ≥ 98", rows[0][4])


def v10_signal(cur, p):
    cur.execute("""SELECT symbol, side, entry_price, stop, target, status, entry_ts::date
                   FROM v10_positions ORDER BY entry_ts DESC LIMIT 20""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Index Intel (V10)", "No V10 positions recorded yet.", "v10_positions")
    return _card("Index Intel · V10",
                 _tbl(["Symbol", "Side", "Entry", "Stop", "Target", "Status", "Date"],
                      [(r[0], r[1], _n(r[2], 2), _n(r[3], 2), _n(r[4], 2), r[5], r[6]) for r in rows]),
                 "v10_positions")


def v9_pairs(cur, p):
    cur.execute("""SELECT segment, long_symbol, short_symbol, ROUND(gvm_gap::numeric,2),
                          ROUND(COALESCE(spread_pnl,0)::numeric,0), status, rebalance_date
                   FROM v9_paper_positions ORDER BY rebalance_date DESC, segment LIMIT 20""")
    rows = cur.fetchall()
    if not rows:
        # cc#840: the honest state while the book has not opened.
        return _empty("Pairs (V9 Brahmastra)",
                      "No pair has been opened yet. Entries are taken at the EOD close on the first "
                      "trading day of a month, and a month where nothing clears the GVM-gap filter is "
                      "a valid cash month.", "v9_paper_positions")
    return _card("Pairs · V9 Brahmastra",
                 _tbl(["Segment", "Long", "Short", "GVM gap", "Spread P&L", "Status"],
                      [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]),
                 "v9_paper_positions", rows[0][6])


def tc_scan(cur, p):
    cur.execute("""SELECT symbol, direction, entry_price, cmp, pp, r1, s1, signal_date
                   FROM tc_intraday_signals
                   WHERE signal_date=(SELECT MAX(signal_date) FROM tc_intraday_signals)
                   ORDER BY symbol LIMIT 30""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Intraday scan (Trade Check)", "No intraday signals in the latest session.",
                      "tc_intraday_signals")
    return _card("Intraday scan · Trade Check",
                 _tbl(["Symbol", "Direction", "Entry", "CMP", "Pivot", "R1", "S1"],
                      [(r[0], r[1], _n(r[2], 2), _n(r[3], 2), _n(r[4], 2), _n(r[5], 2), _n(r[6], 2)) for r in rows]),
                 "tc_intraday_signals", rows[0][7])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# MARKET
# ══════════════════════════════════════════════════════════════════════════════════════════════

def index_levels(cur, p):
    out, asof = [], None
    for idx in ("NIFTY50", "BANKNIFTY"):
        cur.execute("""SELECT high, low, close, price_date FROM raw_prices
                       WHERE symbol=%s AND close>0 ORDER BY price_date DESC LIMIT 5""", (idx,))
        rows = cur.fetchall()
        if not rows:
            continue
        h = max(float(r[0]) for r in rows if r[0] is not None)
        lo = min(float(r[1]) for r in rows if r[1] is not None)
        c = float(rows[0][2])
        asof = asof or rows[0][3]
        pp = (h + lo + c) / 3
        out.append((idx, _n(c, 1), _n(pp, 1), _n(2 * pp - lo, 1), _n(pp + (h - lo), 1),
                    _n(2 * pp - h, 1), _n(pp - (h - lo), 1)))
    if not out:
        return _empty("Index support & resistance", "No index history available.", "raw_prices")
    body = _tbl(["Index", "Close", "Pivot", "R1", "R2", "S1", "S2"], out)
    body += "\n\n_Rolling 5-session pivots: PP = (H+L+C)/3 over the window. Arithmetic, not opinion._"
    return _card("Nifty / BankNifty · support & resistance", body, "raw_prices, rolling 5 sessions", asof)


def fii_dii_positioning(cur, p):
    cur.execute("""SELECT d, category, buy_cr, sell_cr, net_cr FROM fii_dii_daily
                   WHERE d >= (SELECT MAX(d) FROM fii_dii_daily) - 5 ORDER BY d DESC, category""")
    rows = cur.fetchall()
    if not rows:
        return _empty("FII / DII flows", "No flow rows stored.", "fii_dii_daily")
    return _card("FII / DII flows · last 5 sessions",
                 _tbl(["Date", "Category", "Buy (Cr)", "Sell (Cr)", "Net (Cr)"],
                      [(r[0], r[1], _n(r[2], 0), _n(r[3], 0), _n(r[4], 0)) for r in rows]),
                 "fii_dii_daily", rows[0][0])


def global_indices_card(cur, p):
    cur.execute("""SELECT category, name, price, chg_pct, quote_date FROM global_indices
                   WHERE quote_date=(SELECT MAX(quote_date) FROM global_indices)
                   ORDER BY category, name""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Global · commodities · currency", "No global quotes stored.", "global_indices")
    return _card("Global · commodities · currency",
                 _tbl(["Category", "Instrument", "Price", "Change"],
                      [(r[0], r[1], _n(r[2], 2), _sgn(r[3])) for r in rows]),
                 "global_indices", rows[0][4])


def results_today(cur, p):
    cur.execute("""SELECT UPPER(ticker), company_name, ex_date FROM earnings_calendar
                   WHERE ex_date = CURRENT_DATE AND verified <> 'false' ORDER BY company_name""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Results today", "No company is scheduled to report today.", "earnings_calendar")
    return _card("Results today", _tbl(["Symbol", "Company"], [(r[0], r[1]) for r in rows]),
                 "earnings_calendar", rows[0][2])


def results_week(cur, p):
    cur.execute("""SELECT UPPER(ticker), company_name, ex_date FROM earnings_calendar
                   WHERE ex_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 7 AND verified <> 'false'
                   ORDER BY ex_date, company_name LIMIT 60""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Results this week", "Nothing scheduled in the next 7 days.", "earnings_calendar")
    return _card("Results · next 7 days",
                 _tbl(["Symbol", "Company", "Date"], rows), "earnings_calendar")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# SECTORS
# ══════════════════════════════════════════════════════════════════════════════════════════════

def sector_segment(cur, p):
    seg = (p.get("seg") or "").strip()
    if not seg:
        return _need("seg")
    cur.execute("""SELECT symbol, company_name, ROUND(gvm_score::numeric,2),
                          ROUND(g_score::numeric,2), ROUND(v_score::numeric,2),
                          ROUND(m_score::numeric,2), verdict, score_date
                   FROM gvm_scores WHERE segment ILIKE %s
                     AND score_date=(SELECT MAX(score_date) FROM gvm_scores)
                   ORDER BY gvm_score DESC NULLS LAST LIMIT 30""", (f"%{seg}%",))
    rows = cur.fetchall()
    if not rows:
        return _empty(f"{seg} · members", f"No GVM-scored members matched '{seg}'.", "gvm_scores")
    return _card(f"{seg} · members by GVM",
                 _tbl(["Symbol", "Company", "GVM", "G", "V", "M", "Verdict"],
                      [(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]),
                 "gvm_scores latest score_date", rows[0][7])


def sector_brief(cur, p):
    seg = (p.get("seg") or "").strip()
    if not seg:
        return _need("seg")
    cur.execute("""SELECT segment, what_is_it, growth_drivers, business_model, key_risks, generated_at::date
                   FROM sector_briefs WHERE segment ILIKE %s ORDER BY generated_at DESC LIMIT 1""",
                (f"%{seg}%",))
    r = cur.fetchone()
    if not r:
        return _empty(f"{seg} · brief", f"No stored brief for '{seg}'.", "sector_briefs")
    parts = [("What it is", r[1]), ("Growth drivers", r[2]),
             ("Business model", r[3]), ("Key risks", r[4])]
    body = "\n\n".join(f"**{k}**\n{v}" for k, v in parts if v)
    return _card(f"{r[0]} · segment brief", body or "Brief row exists but carries no sections.",
                 "sector_briefs", r[5])


def segment_move(cur, p):
    """cc#836 phase B: the cc#810 mcap-weighted segment move, reused rather than recomputed —
    segment_change.py is the single source for this number across the platform."""
    seg = (p.get("seg") or "").strip()
    import segment_change
    if seg:
        info = segment_change.segment_change_for(cur, seg)
        if not info:
            return _empty(f"{seg} · segment move", f"No segment matched '{seg}'.", "segment_change (cc#810)")
        body = _tbl(["Window", "Move"], [("Day", _sgn(info.get("day"))),
                                         ("Week", _sgn(info.get("week"))),
                                         ("Month", _sgn(info.get("month")))])
        body += "\n\n_" + segment_change.segment_label(info) + "_"
        return _card(f"{seg} · segment move", body, "segment_change, mcap-weighted over FULL membership")
    all_seg = segment_change.segment_changes(cur)
    if not all_seg:
        return _empty("Segment move", "No segment moves computed.", "segment_change (cc#810)")
    rows = sorted(((k, v) for k, v in all_seg.items() if v.get("day") is not None),
                  key=lambda kv: kv[1]["day"], reverse=True)
    top = [(k, _sgn(v.get("day")), _sgn(v.get("week")), _sgn(v.get("month")),
            v.get("members_total")) for k, v in rows[:15]]
    return _card("Segment move · today",
                 _tbl(["Segment", "Day", "Week", "Month", "Members"], top)
                 + "\n\n_Mcap-weighted across the FULL segment membership (cc#810), so the number is "
                   "not swung by whichever members happen to be priced._",
                 "segment_change, mcap-weighted")


def sector_rotation(cur, p):
    cur.execute("""SELECT segment, ROUND(mcap_weighted_gvm::numeric,2), stocks_count, verdict
                   FROM sector_ratings ORDER BY mcap_weighted_gvm DESC NULLS LAST LIMIT 15""")
    top = cur.fetchall()
    if not top:
        return _empty("Sector rotation", "No sector ratings computed.", "sector_ratings")
    cur.execute("""SELECT segment, ROUND(mcap_weighted_gvm::numeric,2), stocks_count, verdict
                   FROM sector_ratings ORDER BY mcap_weighted_gvm ASC NULLS LAST LIMIT 8""")
    bottom = cur.fetchall()
    body = ("**Leading**\n" + _tbl(["Segment", "GVM (mcap-wtd)", "Members", "Verdict"], top)
            + "\n\n**Lagging**\n" + _tbl(["Segment", "GVM (mcap-wtd)", "Members", "Verdict"], bottom))
    body += ("\n\n_Ranked on mcap-weighted GVM, so a segment's read is driven by the businesses that "
             "actually carry its weight rather than by a simple average across sizes._")
    return _card("Sector rotation", body, "sector_ratings")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# SCREENS — all read STORED results or screener_raw. Never a live V13 engine run (rule id=3069).
# ══════════════════════════════════════════════════════════════════════════════════════════════

def screen_members(cur, p):
    sid = p.get("screen_id")
    if not sid:
        cur.execute("""SELECT screen_id, screen_name, COUNT(*), MAX(last_seen)
                       FROM v13_screen_results GROUP BY 1,2 ORDER BY 2""")
        rows = cur.fetchall()
        if not rows:
            return _empty("Screens", "No stored screen results yet — the EOD job has not run.",
                          "v13_screen_results")
        return _card("Predefined screens",
                     _tbl(["Screen", "Name", "Members", "Last run"], rows)
                     + "\n\n_Tap a screen from the menu, or ask for it by name._",
                     "v13_screen_results (stored EOD membership, never a live engine run)")
    cur.execute("""SELECT r.symbol, g.company_name, ROUND(g.gvm_score::numeric,2), r.first_seen,
                          r.screen_name, r.last_seen
                   FROM v13_screen_results r
                   LEFT JOIN gvm_scores g ON g.symbol=r.symbol
                                         AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                   WHERE r.screen_id=%s ORDER BY r.rank NULLS LAST, r.symbol LIMIT 40""", (str(sid),))
    rows = cur.fetchall()
    if not rows:
        return _empty(f"Screen {sid}", "No stored members for this screen.", "v13_screen_results")
    return _card(f"{rows[0][4]} · members",
                 _tbl(["Symbol", "Company", "GVM", "Added on"], [(r[0], r[1], r[2], r[3]) for r in rows]),
                 "v13_screen_results (stored EOD membership)", rows[0][5])


def _screener_filter(cur, title, where, order, basis, empty_why, extra_col=None, extra_label=None):
    sel = f", {extra_col}" if extra_col else ""
    cur.execute(f"""SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2){sel}
                    FROM gvm_scores g JOIN screener_raw s ON s.nse_code=g.symbol
                    WHERE g.score_date=(SELECT MAX(score_date) FROM gvm_scores) AND {where}
                    ORDER BY {order} LIMIT 25""")
    rows = cur.fetchall()
    if not rows:
        return _empty(title, empty_why, basis)
    headers = ["Symbol", "Company", "GVM"] + ([extra_label] if extra_label else [])
    return _card(title, _tbl(headers, rows), basis)


def filter_dividend(cur, p):
    return _screener_filter(
        cur, "High dividend yield",
        "s.dividend_yield IS NOT NULL AND s.dividend_yield >= 2 AND g.gvm_score >= 6",
        "s.dividend_yield DESC", "screener_raw dividend_yield ≥ 2% with GVM ≥ 6",
        "No GVM-6+ company currently yields 2% or more in screener_raw.",
        'ROUND(s.dividend_yield::numeric,2)', "Yield %")


def filter_debtfree(cur, p):
    return _screener_filter(
        cur, "Debt-free quality",
        's."Debt to equity" IS NOT NULL AND s."Debt to equity" <= 0.1 AND g.gvm_score >= 7',
        "g.gvm_score DESC", "screener_raw D/E ≤ 0.1 with GVM ≥ 7",
        "No GVM-7+ company currently shows D/E at or below 0.1.",
        'ROUND(s."Debt to equity"::numeric,2)', "D/E")


def filter_near52w(cur, p):
    cur.execute("""SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2),
                          ROUND(ut.week_index_52::numeric,1), ut.score_date
                   FROM universe_technicals ut
                   JOIN gvm_scores g ON g.symbol=ut.symbol
                                    AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                   WHERE ut.score_date=(SELECT MAX(score_date) FROM universe_technicals)
                     AND ut.week_index_52 BETWEEN 90 AND 100 AND g.gvm_score >= 6.5
                   ORDER BY ut.week_index_52 DESC LIMIT 25""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Near 52W high", "Nothing with GVM ≥ 6.5 is in the top decile of its 52-week range.",
                      "universe_technicals")
    return _card("Near 52W high",
                 _tbl(["Symbol", "Company", "GVM", "52W position"],
                      [(r[0], r[1], r[2], f"{r[3]}%") for r in rows]),
                 "universe_technicals week_index_52 90-100, GVM ≥ 6.5", rows[0][4])


def filter_oversold(cur, p):
    cur.execute("""SELECT g.symbol, g.company_name, ROUND(g.gvm_score::numeric,2),
                          ROUND(ut.rsi_month::numeric,1), ROUND(ut.week_index_52::numeric,1), ut.score_date
                   FROM universe_technicals ut
                   JOIN gvm_scores g ON g.symbol=ut.symbol
                                    AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                   WHERE ut.score_date=(SELECT MAX(score_date) FROM universe_technicals)
                     AND ut.rsi_month IS NOT NULL AND ut.rsi_month <= 40 AND g.gvm_score >= 7
                   ORDER BY ut.rsi_month ASC LIMIT 25""")
    rows = cur.fetchall()
    if not rows:
        # The spec flagged this leaf as RSI-dependent. It reads the NATIVE rsi_month, so the cc#828
        # screener blanking does not touch it — an empty result here is a real market state.
        return _empty("Oversold bounce candidates",
                      "No GVM-7+ company is at or below 40 monthly RSI. RSI is read from "
                      "universe_technicals (native), so this is a market state, not missing data.",
                      "universe_technicals rsi_month")
    return _card("Oversold bounce candidates",
                 _tbl(["Symbol", "Company", "GVM", "Monthly RSI", "52W position"],
                      [(r[0], r[1], r[2], r[3], f"{r[4]}%") for r in rows]),
                 "universe_technicals rsi_month ≤ 40, GVM ≥ 7 (native, not screener CSV)", rows[0][5])


def gvm_top_smallcap(cur, p):
    cur.execute("""SELECT symbol, company_name, ROUND(gvm_score::numeric,2), ROUND(market_cap::numeric,0),
                          verdict, score_date
                   FROM gvm_scores
                   WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                     AND market_cap BETWEEN 500 AND 20000 AND gvm_score >= 7
                   ORDER BY gvm_score DESC LIMIT 25""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Small caps by GVM", "No small cap currently scores 7 or above.", "gvm_scores")
    body = _tbl(["Symbol", "Company", "GVM", "MCap (Cr)", "Verdict"],
                [(r[0], r[1], r[2], _n(r[3], 0), r[4]) for r in rows])
    body += ("\n\n_This is the honest answer to a penny-stock or multibagger question: quality-gated "
             "small caps, ranked by GVM. Scorr does not publish tips._")
    return _card("Small caps by GVM", body, "gvm_scores, mcap 500-20,000 Cr and GVM ≥ 7", rows[0][5])


# ══════════════════════════════════════════════════════════════════════════════════════════════
# RESULTS SEASON — reuse result_corner, never a second aggregation (the cc#802 lesson).
# ══════════════════════════════════════════════════════════════════════════════════════════════

_SEASON_CACHE = {"payload": None}


def _season_payload(cur=None):
    """result_corner.result_corner_v2() owns the season maths — the ONE payload that already drives
    all three Result Corner sections. Calling it is deliberate: re-deriving season medians here
    would be a second aggregation of the same numbers, which is exactly the divergence cc#802 was
    created to stop. It opens its own connection, so `cur` is unused and kept only for signature
    symmetry with every other card."""
    try:
        import result_corner
        return result_corner.result_corner_v2()
    except Exception as e:
        log.warning("result_corner_v2 unavailable: %s", e)
        return None


def result_season(cur, p):
    d = _season_payload(cur)
    if not d or not d.get("summary"):
        return _empty("Results season", "The Result Corner payload is not available right now.",
                      "result_corner")
    s = d["summary"]
    rows = [(k.replace("_", " ").title(), v) for k, v in s.items()
            if isinstance(v, (int, float, str)) and not isinstance(v, bool)]
    return _card(f"Results season · {d.get('season') or ''}".strip(),
                 _tbl(["Metric", "Value"], rows), "result_corner season summary")


def result_sector_table(cur, p):
    d = _season_payload(cur)
    secs = (d or {}).get("sectors") or []
    if not secs:
        return _empty("Results · sector table", "No sector rows in the Result Corner payload.",
                      "result_corner")
    rows = [(x.get("sector"), _n(x.get("avg_mcap"), 0), f"{x.get('reported')}/{x.get('total')}",
             _sgn(x.get("sales_yoy")), _sgn(x.get("pat_yoy")),
             "tiny base" if x.get("tiny_base") else "") for x in secs[:20]]
    body = _tbl(["Sector", "Avg MCap (Cr)", "Reported", "Sales YoY", "PAT YoY", "Flag"], rows)
    body += "\n\n_Ordered by average sector market cap (cc#834), so heavyweight sectors lead._"
    return _card("Results · sector table", body, "result_corner sectors, avg-mcap ordered")


def result_beats(cur, p):
    d = _season_payload(cur)
    cos = (d or {}).get("companies") or []
    same = [c for c in cos if c.get("pat_yoy") is not None]
    if not same:
        return _empty("Beats & misses", "No reported company carries a usable YoY base yet.",
                      "result_corner")
    same.sort(key=lambda c: c.get("pat_yoy") or 0, reverse=True)
    beats = [(c.get("symbol"), c.get("company"), _sgn(c.get("sales_yoy")), _sgn(c.get("pat_yoy")))
             for c in same[:10]]
    misses = [(c.get("symbol"), c.get("company"), _sgn(c.get("sales_yoy")), _sgn(c.get("pat_yoy")))
              for c in same[-10:]][::-1]
    body = ("**Strongest PAT growth**\n" + _tbl(["Symbol", "Company", "Sales YoY", "PAT YoY"], beats)
            + "\n\n**Weakest**\n" + _tbl(["Symbol", "Company", "Sales YoY", "PAT YoY"], misses))
    return _card("Beats & misses", body, "result_corner companies, same-quarter reporters")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# MUTUAL FUNDS — consumes the cc#837 endpoint's query shape, never a second fund search.
# ══════════════════════════════════════════════════════════════════════════════════════════════

_MF_CATS = {
    "mf_large": "Large Cap Fund", "mf_mid": "Mid Cap Fund", "mf_small": "Small Cap Fund",
    "mf_flexi": "Flexi Cap Fund", "mf_elss": "ELSS",
}


def _mf_top(cur, cats: Optional[List[str]], title: str, n=12):
    if cats:
        cur.execute("""SELECT m.name, m.amc, m.category, ROUND(s.mqs::numeric,1)
                       FROM mf_master m JOIN mf_scores s ON s.scheme_code=m.scheme_code
                       WHERE m.category = ANY(%s)
                         AND m.name ILIKE '%%direct%%' AND m.name ILIKE '%%growth%%'
                       ORDER BY s.mqs DESC LIMIT %s""", (cats, n))
    else:
        cur.execute("""SELECT m.name, m.amc, m.category, ROUND(s.mqs::numeric,1)
                       FROM mf_master m JOIN mf_scores s ON s.scheme_code=m.scheme_code
                       WHERE m.name ILIKE '%%direct%%' AND m.name ILIKE '%%growth%%'
                       ORDER BY s.mqs DESC LIMIT %s""", (n,))
    rows = cur.fetchall()
    if not rows:
        return _empty(title, "No scored fund matched.", "mf_master + mf_scores")
    body = _tbl(["Fund", "AMC", "Category", "MQS"],
                [(r[0], r[1], r[2], _n((float(r[3]) / 10) if r[3] is not None else None, 1)) for r in rows])
    body += ("\n\n_MQS is shown on the /10 scale. It scores a fund by looking THROUGH to the GVM of "
             "what it actually holds, weighted by portfolio weight — not by past NAV alone._")
    return _card(title, body, "mf_scores (MQS), Direct-Growth plans")


def mf_top_mqs(cur, p):
    return _mf_top(cur, None, "Top funds by MQS")


def mf_category(cur, p):
    node = p.get("node") or ""
    cat = _MF_CATS.get(node) or p.get("cat")
    if not cat:
        return _need("cat")
    return _mf_top(cur, [cat], f"{cat} · top by MQS")


def mf_screener(cur, p):
    cur.execute("""SELECT m.category, COUNT(*), ROUND(AVG(s.mqs)::numeric,1), ROUND(MAX(s.mqs)::numeric,1)
                   FROM mf_master m JOIN mf_scores s ON s.scheme_code=m.scheme_code
                   WHERE m.name ILIKE '%%direct%%' AND m.name ILIKE '%%growth%%'
                   GROUP BY 1 ORDER BY AVG(s.mqs) DESC""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Curated MF screener", "No scored funds available.", "mf_master + mf_scores")
    return _card("Curated MF screener · categories by average MQS",
                 _tbl(["Category", "Scored funds", "Avg MQS", "Best MQS"],
                      [(r[0], r[1], _n(float(r[2]) / 10, 1), _n(float(r[3]) / 10, 1)) for r in rows])
                 + "\n\n_Pick a category from the menu for its ranked funds._",
                 "mf_scores grouped by category")


def mf_fund(cur, p):
    q = (p.get("q") or p.get("fund") or "").strip()
    if not q:
        return _need("q")
    cur.execute("""SELECT m.name, m.amc, m.category, ROUND(s.mqs::numeric,1),
                          ROUND(s.q_score::numeric,1), ROUND(s.r_score::numeric,1),
                          ROUND(s.c_score::numeric,1), ROUND(s.s_score::numeric,1), s.basis_label
                   FROM mf_master m LEFT JOIN mf_scores s ON s.scheme_code=m.scheme_code
                   WHERE m.name ILIKE %s ORDER BY s.mqs DESC NULLS LAST, length(m.name) LIMIT 1""",
                (f"%{q}%",))
    r = cur.fetchone()
    if not r:
        return _empty("Fund lookup", f"No fund matched '{q}'.", "mf_master")
    if r[3] is None:
        return _card(r[0], f"**{r[1]} · {r[2]}**\n\nThis fund is in the universe but is **not yet "
                           f"scored** — no MQS is shown rather than a placeholder.", "mf_master")
    body = _tbl(["Metric", "Value"], [
        ("AMC", r[1]), ("Category", r[2]),
        ("MQS", _n(float(r[3]) / 10, 1)),
        ("Q · quality of holdings", _n(r[4], 1)), ("R · returns", _n(r[5], 1)),
        ("C · consistency", _n(r[6], 1)), ("S · size/cost", _n(r[7], 1)),
        ("Basis", r[8]),
    ])
    return _card(r[0], body, "mf_master + mf_scores")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# PORTFOLIO
# ══════════════════════════════════════════════════════════════════════════════════════════════

def qb_positions(cur, p):
    cur.execute("""SELECT basket_name, symbol, ROUND(entry_price::numeric,2), qty,
                          ROUND(current_price::numeric,2), ROUND(pnl::numeric,0), ROUND(pnl_pct::numeric,1)
                   FROM quant_paper_positions WHERE status='open'
                   ORDER BY basket_name, pnl_pct DESC NULLS LAST LIMIT 60""")
    rows = cur.fetchall()
    if not rows:
        return _empty("QB positions", "No open positions across the quant baskets.",
                      "quant_paper_positions")
    return _card("Quant Basket · open positions",
                 _tbl(["Symbol", "Basket", "Entry", "Qty", "CMP", "P&L", "P&L %"],
                      [(r[1], r[0], r[2], r[3], r[4], r[5], _sgn(r[6])) for r in rows]),
                 "quant_paper_positions (open)")


def hr_reports_list(cur, p):
    cur.execute("""SELECT p.name, p.source, COUNT(h.id), p.created_at::date
                   FROM hr_portfolios p LEFT JOIN hr_holdings h ON h.portfolio_id=p.id
                   GROUP BY p.id, p.name, p.source, p.created_at ORDER BY p.created_at DESC LIMIT 30""")
    rows = cur.fetchall()
    if not rows:
        return _empty("Saved reports", "No saved portfolios yet.", "hr_portfolios")
    return _card("Saved portfolio reports",
                 _tbl(["Client", "Source", "Holdings", "Created"], rows)
                 + "\n\n_Open the Adaptive Dashboard for the full shelf._", "hr_portfolios")


def exit_check_composite(cur, p):
    sym = (p.get("sym") or "").upper()
    if not sym:
        return _need("sym")
    cur.execute("""SELECT basket_name, ROUND(entry_price::numeric,2), qty,
                          ROUND(current_price::numeric,2), ROUND(pnl_pct::numeric,1), stop_loss_price
                   FROM quant_paper_positions WHERE symbol=%s AND status='open'""", (sym,))
    held = cur.fetchall()
    cur.execute("""SELECT ROUND(gvm_score::numeric,2), ROUND(m_score::numeric,2), verdict, score_date
                   FROM gvm_scores WHERE symbol=%s
                     AND score_date=(SELECT MAX(score_date) FROM gvm_scores)""", (sym,))
    g = cur.fetchone()
    if not held and not g:
        return _empty(f"{sym} · exit check", f"{sym} is not held in any basket and is not GVM-scored.",
                      "quant_paper_positions + gvm_scores")
    body = ""
    if held:
        body += "**Held in**\n" + _tbl(["Basket", "Entry", "Qty", "CMP", "P&L %", "Stop"],
                                       [(h[0], h[1], h[2], h[3], _sgn(h[4]), _n(h[5], 2)) for h in held])
    else:
        body += "_Not currently held in any quant basket._"
    if g:
        body += "\n\n**Current standing**\n" + _tbl(["Metric", "Value"],
                                                    [("GVM", g[0]), ("M (momentum)", g[1]), ("Verdict", g[2])])
    body += ("\n\n_This card shows position and standing. It does not issue an exit instruction — "
             "the basket's own HS1/HS2 and quality rules govern exits._")
    return _card(f"{sym} · exit check", body, "quant_paper_positions + gvm_scores",
                 g[3] if g else None)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# DISPATCH
# ══════════════════════════════════════════════════════════════════════════════════════════════

CARDS = {
    "why_moving": why_moving, "levels": levels, "fin_snapshot": fin_snapshot,
    "shareholding": shareholding, "dividend": dividend, "polished_news_symbol": polished_news_symbol,
    "paper_pivots": paper_pivots, "oi_buildup": oi_buildup, "rvol_spikes": rvol_spikes,
    "breakouts_today": breakouts_today, "v10_signal": v10_signal, "v9_pairs": v9_pairs,
    "tc_scan": tc_scan,
    "index_levels": index_levels, "fii_dii_positioning": fii_dii_positioning,
    "global_indices_card": global_indices_card, "results_today": results_today,
    "results_week": results_week,
    "sector_segment": sector_segment, "sector_brief": sector_brief, "sector_rotation": sector_rotation,
    "segment_move": segment_move,
    "screen_members": screen_members, "filter_dividend": filter_dividend,
    "filter_debtfree": filter_debtfree, "filter_near52w": filter_near52w,
    "filter_oversold": filter_oversold, "gvm_top_smallcap": gvm_top_smallcap,
    "result_season": result_season, "result_sector_table": result_sector_table,
    "result_beats": result_beats,
    "mf_screener": mf_screener, "mf_top_mqs": mf_top_mqs, "mf_fund": mf_fund,
    "mf_category": mf_category,
    "qb_positions": qb_positions, "hr_reports_list": hr_reports_list,
    "exit_check_composite": exit_check_composite,
}


@router.get("/api/max/card/{intent}")
def max_card(intent: str, sym: str = "", seg: str = "", screen_id: str = "",
             cat: str = "", q: str = "", node: str = ""):
    """cc#836 phase B: ONE dispatch for every native IVR card. $0 by construction — nothing in
    CARDS may call a model, and there is no second path a leaf could take to reach one."""
    fn = CARDS.get(intent)
    if not fn:
        return {"error": f"unknown card intent: {intent}", "available": sorted(CARDS.keys())}
    params = {"sym": sym, "seg": seg, "screen_id": screen_id, "cat": cat, "q": q, "node": node}
    try:
        with _conn() as conn, conn.cursor() as cur:
            out = fn(cur, params)
        return out
    except Exception as e:
        log.exception("max_card %s failed", intent)
        # Always JSON with a readable message (cc#835 doctrine) — a card must degrade to a
        # sentence, never to a parse error on the page.
        return {"title": intent.replace("_", " ").title(),
                "body": f"**This card could not be built right now.**\n\n`{type(e).__name__}: {str(e)[:200]}`",
                "error": f"{type(e).__name__}: {str(e)[:200]}"}

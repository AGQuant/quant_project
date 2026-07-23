"""
result_analysis_gen.py — cc#602 result_analysis regeneration (data-driven, per reported quarter).
================================================================================
The Result Analysis card (input_raw.result_analysis + last_result_analysis_updated, read by
results_endpoints.py) is a data-driven quarter card:

    Q1 FY27 · <Company>

    <emoji> Sales   +x% QoQ  +y% YoY  (Sector s%)
    <emoji> PAT     +x% QoQ  +y% YoY  (Sector s%)
    <emoji> Margins  m% vs l% LY
    <emoji> PE       p x vs q x sector

    <verdict>
    <revenue narrative>
    <margin narrative>

It had frozen at the 05-Jun Q4FY26 batch (no regenerator existed in the app — it was a one-off).
This module makes it self-refreshing: after each T+1 fundamentals re-scrape (and on the startup
one-shot when the latest card is older than the newest reported quarter), every announced company
whose fundamentals_history now carries the just-reported quarter gets a freshly-computed card with
last_result_analysis_updated=NOW(). Numbers are computed from fundamentals_history (Sales / Net
Profit / OPM %) + gvm_scores (pe_raw, pe_peer) — never invented; a company without the new quarter
in fundamentals_history is skipped (regenerates on a later cycle once its scrape lands).
"""
import logging
from datetime import date, datetime

from fastapi import APIRouter, Header, HTTPException
from typing import Optional

log = logging.getLogger("scorr.result_analysis_gen")
router = APIRouter(prefix="/api/admin/result_analysis", tags=["result_analysis_gen"])

_G, _R, _Y = "\U0001F7E2", "\U0001F534", "\U0001F7E1"   # green / red / yellow


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _pct(cur_v, prev_v):
    if cur_v is None or prev_v in (None, 0):
        return None
    return round((cur_v - prev_v) / abs(prev_v) * 100.0, 1)


def _fq_label(period_end: date) -> Optional[str]:
    """Jun->Q1, Sep->Q2, Dec->Q3, Mar->Q4; fiscal year = calendar year+1 for Apr-Dec, else same."""
    if not period_end:
        return None
    m, y = period_end.month, period_end.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(m)
    if q is None:
        return None
    fy = (y + 1) if m >= 4 else y
    return f"Q{q} FY{str(fy)[-2:]}"


def _sign(v, plus=True):
    if v is None:
        return "n/a"
    return f"{'+' if v >= 0 and plus else ''}{v}%"


def _emoji_vs(v, sector, higher_good=True):
    """green if clearly beats sector, red if clearly lags, yellow if broadly inline / no sector ref."""
    if v is None:
        return _Y
    if sector is None:
        return _G if (v > 0) == higher_good else _R
    diff = v - sector
    band = max(2.0, abs(sector) * 0.15)
    if abs(diff) <= band:
        return _Y
    return _G if (diff > 0) == higher_good else _R


def build_card(cur, symbol: str, min_quarter_end: date = None) -> Optional[str]:
    """Compute the result-analysis card for one symbol from fundamentals_history + gvm_scores.
    Returns the card text, or None when the symbol has no quarter >= min_quarter_end (so it is
    skipped until its scrape lands). Sector figures = segment peer averages over the latest quarter."""
    cur.execute("""SELECT period_end, metrics FROM fundamentals_history
                   WHERE symbol=%s AND section='quarters' AND period_type='quarter' AND period_end IS NOT NULL
                   ORDER BY period_end DESC LIMIT 6""", (symbol,))
    rows = cur.fetchall()
    if not rows:
        return None
    latest_end = rows[0][0]
    if min_quarter_end and latest_end < min_quarter_end:
        return None
    def m(i, key):
        return _num((rows[i][1] or {}).get(key)) if i < len(rows) else None
    sales, sales_p, sales_y = m(0, "Sales"), m(1, "Sales"), m(4, "Sales")
    pat, pat_p, pat_y = m(0, "Net Profit"), m(1, "Net Profit"), m(4, "Net Profit")
    opm, opm_ly = m(0, "OPM %"), m(4, "OPM %")
    s_qoq, s_yoy = _pct(sales, sales_p), _pct(sales, sales_y)
    p_qoq, p_yoy = _pct(pat, pat_p), _pct(pat, pat_y)

    cur.execute("SELECT segment, pe_raw, pe_peer, verdict FROM gvm_scores WHERE symbol=%s "
                "ORDER BY score_date DESC LIMIT 1", (symbol,))
    g = cur.fetchone()
    segment, pe_raw, pe_peer, verdict = (g[0], _num(g[1]), _num(g[2]), g[3]) if g else (None, None, None, None)

    # sector sales/PAT YoY = median of same-segment names' latest-quarter YoY (computed, not invented)
    sec_sales = sec_pat = None
    if segment:
        cur.execute("""WITH q AS (
            SELECT fh.symbol, fh.metrics, fh.period_end,
                   ROW_NUMBER() OVER (PARTITION BY fh.symbol ORDER BY fh.period_end DESC) rn
            FROM fundamentals_history fh JOIN gvm_scores gs ON gs.symbol=fh.symbol
            WHERE fh.section='quarters' AND gs.segment=%s
              AND gs.score_date=(SELECT MAX(score_date) FROM gvm_scores))
            SELECT symbol, (SELECT metrics FROM q q2 WHERE q2.symbol=q.symbol AND q2.rn=1) latest,
                            (SELECT metrics FROM q q4 WHERE q4.symbol=q.symbol AND q4.rn=5) yoy
            FROM q WHERE rn=1""", (segment,))
        ss, ps = [], []
        for _sym, latest, yoy in cur.fetchall():
            sv = _pct(_num((latest or {}).get("Sales")), _num((yoy or {}).get("Sales")))
            pv = _pct(_num((latest or {}).get("Net Profit")), _num((yoy or {}).get("Net Profit")))
            if sv is not None:
                ss.append(sv)
            if pv is not None:
                ps.append(pv)
        if ss:
            sec_sales = round(sorted(ss)[len(ss) // 2], 1)
        if ps:
            sec_pat = round(sorted(ps)[len(ps) // 2], 1)

    cur.execute("SELECT company_name FROM screener_raw WHERE UPPER(nse_code)=%s LIMIT 1", (symbol,))
    r = cur.fetchone()
    company = (r[0] if r else None) or symbol
    qlabel = _fq_label(latest_end) or "Latest"

    # emoji per line
    e_sales = _emoji_vs(s_yoy if s_yoy is not None else s_qoq, sec_sales, True)
    e_pat = _emoji_vs(p_yoy if p_yoy is not None else p_qoq, sec_pat, True)
    e_marg = _G if (opm is not None and opm_ly is not None and opm >= opm_ly) else (_Y if opm is not None else _Y)
    e_pe = _Y if (pe_raw is None or pe_peer is None) else (_G if pe_raw <= pe_peer else _R)

    # narrative (templated from the pattern)
    if s_yoy is not None and sec_sales is not None and s_yoy >= sec_sales:
        rev_line = "Revenue outpaced the sector; topline momentum ahead of peers."
    elif s_yoy is not None and sec_sales is not None and s_yoy < sec_sales - 2:
        rev_line = "Revenue lagged as the sector outpaced; topline momentum behind peers."
    else:
        rev_line = "Revenue broadly inline with the sector; no significant divergence."
    if opm is not None and opm_ly is not None:
        d = round(opm - opm_ly, 1)
        marg_line = (f"Margin expanded {abs(d)}pp YoY; operating leverage or mix improvement." if d > 0
                     else (f"Margin compressed {abs(d)}pp YoY; cost or mix pressure." if d < 0
                           else "Margin broadly stable YoY; no significant cost surprises."))
    else:
        marg_line = "Margin data not available for this quarter."
    strong = sum(1 for x in (s_yoy, p_yoy) if x is not None and x > 0)
    if strong == 2 and (sec_sales is None or (s_yoy or 0) >= sec_sales):
        verdict_line = "Strong quarter with growth ahead of the sector."
    elif strong == 0:
        verdict_line = "Soft quarter; growth below trend."
    else:
        verdict_line = "Mixed quarter; selective outperformance."

    def line(emoji, label, a, b, blabel, sector):
        secpart = f"  (Sector {_sign(sector)})" if sector is not None else ""
        return f"{emoji} {label:<7} {_sign(a):>7} QoQ  {_sign(b):>7} {blabel}{secpart}"

    parts = [
        f"{qlabel} · {company}", "",
        line(e_sales, "Sales", s_qoq, s_yoy, "YoY", sec_sales),
        line(e_pat, "PAT", p_qoq, p_yoy, "YoY", sec_pat),
        f"{e_marg} Margins  {_sign(opm, plus=False) if opm is not None else 'n/a'} vs {_sign(opm_ly, plus=False) if opm_ly is not None else 'n/a'} LY",
        f"{e_pe} PE       {(str(pe_raw)+'x') if pe_raw is not None else 'n/a'} vs {(str(pe_peer)+'x') if pe_peer is not None else 'n/a'} sector",
        "", verdict_line, rev_line, marg_line,
    ]
    return "\n".join(parts)


def _announced_symbols(cur, since: date):
    cur.execute("""SELECT DISTINCT UPPER(ticker) FROM earnings_calendar
                   WHERE status='reported' AND ex_date >= %s AND ticker IS NOT NULL""", (since,))
    return [r[0] for r in cur.fetchall()]


def regenerate(conn, since: date = None, min_quarter_end: date = None) -> dict:
    """Regenerate result_analysis for every announced company whose fundamentals_history now carries
    a quarter >= min_quarter_end. Idempotent; only writes when a card was built."""
    since = since or date(2026, 6, 25)
    min_quarter_end = min_quarter_end or date(2026, 6, 1)   # Q1FY27 (Jun-2026) or newer
    written = skipped = 0
    with conn.cursor() as cur:
        syms = _announced_symbols(cur, since)
    for sym in syms:
        with conn.cursor() as cur:
            try:
                card = build_card(cur, sym, min_quarter_end=min_quarter_end)
            except Exception as e:
                log.warning(f"result_analysis build failed for {sym}: {e}")
                card = None
            if card:
                cur.execute("""UPDATE input_raw SET result_analysis=%s, last_result_analysis_updated=CURRENT_DATE
                               WHERE UPPER(nse_code)=%s""", (card, sym))
                written += cur.rowcount
            else:
                skipped += 1
            conn.commit()
    summary = {"announced": len(syms), "regenerated": written, "skipped_no_new_quarter": skipped,
               "min_quarter_end": str(min_quarter_end)}
    try:
        import json
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), 'result_analysis', 'RESULT_ANALYSIS_REGEN', %s::jsonb)""",
                        (json.dumps(summary),))
        conn.commit()
    except Exception:
        pass
    log.info(f"result_analysis regenerate: {summary}")
    return summary


def regenerate_conn() -> dict:
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        return regenerate(conn)
    finally:
        conn.close()


@router.post("/regenerate")
def regenerate_now(x_admin_token: Optional[str] = Header(None)):
    import os
    if not os.getenv("ADMIN_TOKEN") or x_admin_token != os.getenv("ADMIN_TOKEN"):
        raise HTTPException(status_code=401, detail="admin token required")
    return regenerate_conn()


@router.on_event("startup")
async def _startup_regen():
    """One-shot on boot: if the newest result_analysis is older than the newest reported quarter's
    typical filing, regenerate. Cheap + idempotent; ensures the card can never silently freeze again."""
    import threading

    def _go():
        try:
            import fyers_feed
            conn = fyers_feed.get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT MAX(last_result_analysis_updated) FROM input_raw")
                    latest = cur.fetchone()[0]
                # regenerate if the batch predates the Q1FY27 season (last refresh <= 05-Jun)
                if latest is None or latest <= date(2026, 6, 30):
                    log.info("cc#602: result_analysis stale on boot — regenerating Q1FY27")
                    regenerate(conn)
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"cc#602 startup regen skipped: {e}")

    threading.Thread(target=_go, name="cc602-result-analysis-regen", daemon=True).start()

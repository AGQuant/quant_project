"""
results_endpoints.py — cc#572 (spec id=6438): Results "R" card backend.

GET /api/results/card?symbol=X

Branch logic:
  - earnings_calendar row with ex_date <= today -> ANNOUNCED. Return input_raw.result_analysis if
    present (with last_result_analysis_updated); else ANNOUNCED_NO_ANALYSIS (never invent figures).
  - ex_date > today -> UPCOMING; no earnings row -> DATE_TBD. Serve the cached FY27 outlook from
    input_raw.fy27_outlook if present; else set outlook_pending so the card shows a "due September"
    note. cc#609: app-side generation is RETIRED (Anthropic key depleted 20-Jul; the FY27 outlook
    batch is CC-authored, Max-subscription, and DEFERRED to the Sep-2026 review) — NO model is ever
    called here and there is no `generate` path anymore.

Storage: input_raw.fy27_outlook + last_fy27_outlook_updated (same convention as result_analysis;
main.py registers fy27_outlook in _ALLOWED_CONTENT_FIELDS + _FIELD_TO_TS_COL for manual override).
"""
import os
import logging
from datetime import date
from typing import Optional

import psycopg2
from fastapi import APIRouter

log = logging.getLogger("results_card")
router = APIRouter()


def _fq_label(period_end):
    """cc#609: 'Q1 FY27'-style label for a quarter period-end (Jun->Q1, Sep->Q2, Dec->Q3, Mar->Q4)."""
    if not period_end:
        return None
    m, y = period_end.month, period_end.year
    q = {6: 1, 9: 2, 12: 3, 3: 4}.get(m)
    if q is None:
        return None
    fy = (y + 1) if m >= 4 else y
    return f"Q{q} FY{str(fy)[-2:]}"


def _expected_quarter(today=None):
    """cc#620: the LATEST quarter companies are currently reporting = the most recent COMPLETED
    fiscal quarter-end (Mar/Jun/Sep/Dec) on or before today. A structured result card is 'fresh'
    only if its label matches this (never show a stale-quarter card)."""
    d = today or date.today()
    ends = [date(d.year, 3, 31), date(d.year, 6, 30), date(d.year, 9, 30),
            date(d.year, 12, 31), date(d.year - 1, 12, 31)]
    prev = max(e for e in ends if e <= d)
    return _fq_label(prev)


def _card_quarter(text):
    """cc#620: parse the leading 'Qn FYyy' quarter label from a result_analysis card (first line)."""
    import re
    m = re.match(r"\s*(Q[1-4]\s+FY\d{2})", text or "")
    return m.group(1).strip() if m else None


def _symbol_news(cur, sym):
    """cc#620 fallback tiers: the most relevant position_news item for a symbol — PREFER one with a
    polished_news match (url_hash -> raw_news -> polished_news, Intel-tab quality), else the newest
    raw one-liner. Returns None if the symbol has no news."""
    cur.execute("""
        SELECT pn.headline, COALESCE(pol.full_summary, pn.summary) AS summary,
               pn.source_name, pn.url,
               (pol.full_summary IS NOT NULL) AS intel,
               (COALESCE(pol.full_summary, pn.summary) IS NOT NULL) AS has_summary
        FROM position_news pn
        LEFT JOIN raw_news rn ON rn.url_hash = pn.url_hash
        LEFT JOIN polished_news pol ON pol.raw_news_id = rn.id
        WHERE pn.symbol = %s
        ORDER BY (pol.full_summary IS NOT NULL) DESC,
                 COALESCE(pn.published_at, pn.fetched_at) DESC NULLS LAST, pn.id DESC
        LIMIT 1""", (sym,))
    r = cur.fetchone()
    if not r:
        return None
    return {"headline": r[0], "summary": r[1], "source": r[2], "url": r[3],
            "polished": bool(r[4]), "has_summary": bool(r[5])}


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _ensure_cols(cur):
    # idempotent column self-create (run_sql ALTER is blocked by MAINTENANCE_LOCK_RULE; app-side).
    cur.execute("ALTER TABLE input_raw ADD COLUMN IF NOT EXISTS fy27_outlook TEXT")
    cur.execute("ALTER TABLE input_raw ADD COLUMN IF NOT EXISTS last_fy27_outlook_updated TIMESTAMP")


def _f(v):
    return round(float(v), 2) if v is not None else None


def _gvm_ctx(cur, sym):
    """Latest GVM/G/V/M + verdict + 180d GVM delta, from gvm_history (complete universe)."""
    cur.execute("""SELECT gvm_score, g_score, v_score, m_score, verdict
                   FROM gvm_history WHERE symbol=%s ORDER BY score_date DESC LIMIT 1""", (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    cur.execute("""SELECT gvm_score FROM gvm_history WHERE symbol=%s
                   AND score_date BETWEEN CURRENT_DATE-200 AND CURRENT_DATE-180
                   ORDER BY score_date DESC LIMIT 1""", (sym,))
    d180 = cur.fetchone()
    dgvm = (float(r[0]) - float(d180[0])) if (r[0] is not None and d180 and d180[0] is not None) else None
    return {"gvm": _f(r[0]), "g": _f(r[1]), "v": _f(r[2]), "m": _f(r[3]), "verdict": r[4],
            "dgvm_180": round(dgvm, 2) if dgvm is not None else None}


def _peer_comparison(cur, sym, segment):
    """cc#590: latest QoQ sales & profit vs the TOP-3-by-GVM segment peers (self-excluded, non-null
    metric, <3 -> full-segment avg fallback). IDENTICAL basis to Investment Check v3.0 F3. Zero-token."""
    if not segment:
        return None
    cur.execute("""SELECT g.gvm_score, s.qoq_sales_growth, s.qoq_profit_growth
                   FROM gvm_scores g JOIN screener_raw s ON g.symbol = s.nse_code
                   WHERE g.segment=%s AND g.symbol<>%s""", (segment, sym))
    peers = [(_flt(r[0]), _flt(r[1]), _flt(r[2])) for r in cur.fetchall()]

    def _top3(idx):
        cand = [(p[0], p[idx]) for p in peers if p[idx] is not None and p[0] is not None]
        if not cand:
            return None, 0
        cand.sort(key=lambda x: -x[0])
        use = cand[:3] if len(cand) >= 3 else cand
        return sum(v for _, v in use) / len(use), len(use)

    peer_s, n_s = _top3(1)
    peer_p, n_p = _top3(2)
    cur.execute('SELECT qoq_sales_growth, qoq_profit_growth FROM screener_raw WHERE nse_code=%s', (sym,))
    sr = cur.fetchone()
    st_s = _flt(sr[0]) if sr else None
    st_p = _flt(sr[1]) if sr else None
    if st_s is None and st_p is None and peer_s is None and peer_p is None:
        return None
    # cc#609: label which quarter the QoQ figures reflect — the stock's latest reported quarter in
    # fundamentals_history (same vintage the screener QoQ is computed from). One line above the rows.
    cur.execute("""SELECT MAX(period_end) FROM fundamentals_history
                   WHERE symbol=%s AND section='quarters' AND period_type='quarter'""", (sym,))
    qq = cur.fetchone()
    quarter = _fq_label(qq[0]) if qq and qq[0] else None
    return {
        "peer_basis": "top-3 by GVM in segment (self-excluded)",
        "segment": segment,
        "quarter": quarter,
        "peer_count": max(n_s, n_p),
        "fallback": (n_s < 3 or n_p < 3),
        "sales": {"stock": _f(st_s), "peer": _f(peer_s),
                  "beat": (st_s is not None and peer_s is not None and st_s > peer_s)},
        "profit": {"stock": _f(st_p), "peer": _f(peer_p),
                   "beat": (st_p is not None and peer_p is not None and st_p > peer_p)},
    }


def _flt(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _fundamentals(cur, sym):
    cur.execute('''SELECT "Operating profit growth", roce, opm, "Debt to equity", "Return on equity"
                   FROM screener_raw WHERE nse_code=%s LIMIT 1''', (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"opg": _f(r[0]), "roce": _f(r[1]), "opm": _f(r[2]), "de": _f(r[3]), "roe": _f(r[4])}


@router.get("/api/results/card")
async def results_card(symbol: str, generate: bool = False):
    # cc#609: `generate` is retained for backward-compat with any cached client URL but is IGNORED —
    # app-side FY27-outlook generation is retired (dead Anthropic path removed). Cards serve the
    # cached input_raw.fy27_outlook only; when none exists the card shows a "due September" note.
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol is required"}
    with _conn() as conn, conn.cursor() as cur:
        try:
            _ensure_cols(cur)
            conn.commit()
        except Exception:
            conn.rollback()

        cur.execute("SELECT verdict, segment FROM gvm_scores WHERE symbol=%s ORDER BY score_date DESC LIMIT 1", (sym,))
        vr = cur.fetchone()
        gvm_verdict = vr[0] if vr else None
        segment = vr[1] if vr else None
        peer_comparison = _peer_comparison(cur, sym, segment)  # cc#590: top-3-by-GVM QoQ peer block

        def _with_peer(d):
            d["peer_comparison"] = peer_comparison
            return d

        cur.execute("SELECT ex_date FROM earnings_calendar WHERE UPPER(ticker)=%s ORDER BY ex_date DESC LIMIT 1", (sym,))
        er = cur.fetchone()
        today = date.today()

        # cc#620 RESULT_CARD_CONSISTENCY_V1 — one strict content priority chain (used identically by
        # the R button and the Position News tab):
        #   TIER 1 structured : input_raw.result_analysis, shown ONLY if reported AND its quarter label
        #                       == the expected latest quarter (never a stale-quarter card — the
        #                       cc#618 Section B freshness flag: never show a quarter the data lacks).
        #   TIER 2 polished   : the matched polished_news item (url_hash -> raw_news -> polished_news).
        #   TIER 3 raw        : the RSS/Google feed one-liner with a RAW chip.
        expected_q = _expected_quarter(today)
        news = _symbol_news(cur, sym)

        def _news_payload(base):
            if news:
                base.update({"news_summary": news["summary"], "news_headline": news["headline"],
                             "news_source": news["source"], "news_url": news["url"],
                             "news_polished": news["polished"]})
            return _with_peer(base)

        # Branch A: announced
        if er and er[0] is not None and er[0] <= today:
            cur.execute("SELECT result_analysis, last_result_analysis_updated FROM input_raw WHERE nse_code=%s", (sym,))
            ra = cur.fetchone()
            card = ra[0] if ra else None
            card_q = _card_quarter(card) if card else None
            # TIER 1: structured card only when its quarter matches the expected latest quarter.
            if card and card_q and card_q == expected_q:
                return _with_peer({"symbol": sym, "status": "announced", "tier": "structured",
                        "ex_date": str(er[0]), "result_analysis": card, "card_quarter": card_q,
                        "expected_quarter": expected_q,
                        "generated_at": str(ra[1]) if ra[1] else None, "gvm_verdict": gvm_verdict})
            # TIER 2/3: no fresh structured card -> matched polished, else raw one-liner.
            if news and news["has_summary"]:
                return _news_payload({"symbol": sym, "status": "announced", "ex_date": str(er[0]),
                        "tier": "polished" if news["polished"] else "raw",
                        "expected_quarter": expected_q, "card_quarter": card_q, "gvm_verdict": gvm_verdict})
            return _news_payload({"symbol": sym, "status": "announced_no_analysis", "tier": "raw" if news else "pending",
                    "ex_date": str(er[0]), "expected_quarter": expected_q,
                    "card_quarter": card_q, "gvm_verdict": gvm_verdict})

        # Branch B (upcoming) / C (date_tbd): FY27 outlook — cached-only (cc#609: no generation).
        status = "upcoming" if (er and er[0] is not None) else "date_tbd"
        ed = str(er[0]) if (er and er[0] is not None) else None

        cur.execute("SELECT fy27_outlook, last_fy27_outlook_updated FROM input_raw WHERE nse_code=%s", (sym,))
        fo = cur.fetchone()
        cached, cached_ts = (fo[0], fo[1]) if fo else (None, None)
        # cc#620: not-yet-reported — no structured result. Carry the news fallback (tier 2/3) alongside
        # the expected-date / FY27-outlook state so the shared card renders one coherent surface.
        return _news_payload({"symbol": sym, "status": status, "ex_date": ed,
                "tier": ("polished" if (news and news["polished"]) else ("raw" if news else "outlook")),
                "expected_quarter": expected_q,
                "fy27_outlook": cached if cached else None,
                "generated_at": str(cached_ts) if cached_ts else None,
                # cc#609: no cached outlook -> the card shows a "FY27 outlook due September" note.
                "outlook_pending": (not cached),
                "gvm_verdict": gvm_verdict})

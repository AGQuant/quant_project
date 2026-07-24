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


def _fy27_growth(cur, sym):
    """cc#623: FY27 estimated growth % from input_raw.fy27_growth (Sonnet on Trendlyne consensus,
    ~1536/2008 populated). Numeric or None. NEVER touches the retired empty fy27_outlook column."""
    cur.execute("SELECT fy27_growth FROM input_raw WHERE nse_code=%s", (sym,))
    r = cur.fetchone()
    return _f(r[0]) if (r and r[0] is not None) else None


def _raw_news(cur, sym, hours=48):
    """cc#623 / cc#625 fix_2: RAW news section — the last-48h position_news headlines for this exact
    symbol, date-sorted desc. The 48h cutoff is driven by the ARTICLE's published_at ONLY — never
    COALESCE with fetched_at, which let a 13-day-stale article (published 10-Jul, fetched today) slip
    through on ingest drift. published_at is 100% populated for the recent window; a null-published
    row cannot prove freshness and is correctly excluded from the 48h section."""
    cur.execute("""
        SELECT headline, source_name, url, published_at
        FROM position_news
        WHERE symbol = %s AND published_at >= NOW() - make_interval(hours => %s)
        ORDER BY published_at DESC, id DESC
        LIMIT 12""", (sym, hours))
    return [{"headline": r[0], "source": r[1], "url": r[2],
             "published_at": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]


def _polished_by_symbol(cur, sym, days=30):
    """cc#623 POLISHED architecture: REPLACES the cc#619 per-item url_hash lookup (2% hit rate) with a
    symbol-section query — polished_news where the symbol is in mentioned_symbols. cc#625 fix_2: the
    1-month window is driven by the ARTICLE's published_time (not polished_at, which is when WE polished
    it and drifts — an old article polished today would masquerade as fresh). Date-sorted desc."""
    cur.execute("""
        SELECT headline_clean, COALESCE(full_summary, summary) AS summary, source, published_time
        FROM polished_news
        WHERE %s = ANY(mentioned_symbols) AND published_time >= NOW() - make_interval(days => %s)
        ORDER BY published_time DESC, id DESC
        LIMIT 15""", (sym, days))
    return [{"headline": r[0], "summary": r[1], "source": r[2],
             "published_time": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]


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
    metric, <3 -> full-segment avg fallback). IDENTICAL basis to Investment Check v3.0 F3. Zero-token.
    cc#625 fix_3(d): SAME-QUARTER rule — a peer whose latest reported quarter != the subject's quarter
    must not fold a stale-quarter QoQ into the comparison. Restrict the pool to same-quarter reporters
    when >=3 exist; else fall back to the full pool and FLAG the mismatch."""
    if not segment:
        return None
    # subject's latest reported quarter — the vintage the whole comparison is locked to.
    cur.execute("""SELECT MAX(period_end) FROM fundamentals_history
                   WHERE symbol=%s AND section='quarters' AND period_type='quarter'""", (sym,))
    qq = cur.fetchone()
    subj_q = qq[0] if qq and qq[0] else None
    quarter = _fq_label(subj_q) if subj_q else None
    # peers = gvm + screener QoQ + each peer's OWN latest reported quarter (fundamentals_history)
    cur.execute("""SELECT g.gvm_score, s.qoq_sales_growth, s.qoq_profit_growth, fh.latest_q
                   FROM gvm_scores g JOIN screener_raw s ON g.symbol = s.nse_code
                   LEFT JOIN (SELECT symbol, MAX(period_end) latest_q FROM fundamentals_history
                              WHERE section='quarters' AND period_type='quarter' GROUP BY symbol) fh
                          ON fh.symbol = g.symbol
                   WHERE g.segment=%s AND g.symbol<>%s""", (segment, sym))
    rows = [(_flt(r[0]), _flt(r[1]), _flt(r[2]), r[3]) for r in cur.fetchall()]
    same = [p for p in rows if subj_q is not None and p[3] == subj_q]
    if len(same) >= 3:
        peers, quarter_mismatch = same, False
    else:
        peers = rows   # fallback: full pool, flagged
        quarter_mismatch = (subj_q is not None and len(same) < len(rows))

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
    return {
        "peer_basis": "top-3 by GVM in segment (self-excluded, same quarter)",
        "segment": segment,
        "quarter": quarter,
        "peer_count": max(n_s, n_p),
        "same_quarter_peers": len(same),
        "quarter_mismatch": quarter_mismatch,   # True -> pool includes off-quarter peers (flagged in UI)
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

        cur.execute("SELECT ex_date, status FROM earnings_calendar WHERE UPPER(ticker)=%s ORDER BY ex_date DESC LIMIT 1", (sym,))
        er = cur.fetchone()
        today = date.today()
        # cc#648 part_1: earnings_calendar.status='reported' is the AUTHORITATIVE "results are out" signal
        # (an ex_date can pass without the company having reported — reschedules). The structured card is
        # gated on status='reported'; the announced branch still fires on a passed ex_date so a
        # reported-but-not-yet-flagged name shows the pending body rather than an "upcoming" one.
        ex_dt = er[0] if er else None
        reported = bool(er) and (er[1] or "").strip().lower() == "reported"
        announced = reported or (ex_dt is not None and ex_dt <= today)

        # cc#623 POSITION_NEWS_CARD_V2 — two-branch flow, both surfaces (R button + Position News tab)
        # sharing the unified renderer. Sections common to BOTH branches, computed once:
        #   fy27_growth  : input_raw.fy27_growth (FY27 Est. Growth row; hidden when null)
        #   raw_news     : last-48h position_news for this symbol (RAW chip)
        #   polished_news: last-30d polished_news via mentioned_symbols (symbol-section query — REPLACES
        #                  the cc#619 per-item url_hash lookup that hit ~2%)
        # Quarter labels reuse _fq_label / the card's own leading label (cc#618-B doctrine: never label a
        # quarter the data does not contain; the cc#622-A downgrade guard protects stored cards).
        expected_q = _expected_quarter(today)
        fy27 = _fy27_growth(cur, sym)
        raw_news = _raw_news(cur, sym, hours=48)
        pol_news = _polished_by_symbol(cur, sym, days=30)

        def _sections(base):
            base.update({"fy27_growth": fy27, "raw_news": raw_news, "polished_news": pol_news})
            return _with_peer(base)

        cur.execute("SELECT result_analysis, last_result_analysis_updated FROM input_raw WHERE nse_code=%s", (sym,))
        ra = cur.fetchone()
        card = ra[0] if (ra and ra[0]) else None
        card_q = _card_quarter(card) if card else None
        card_ts = str(ra[1]) if (ra and ra[1]) else None

        # Branch A: ANNOUNCED — strict priority chain on the structured card.
        if announced:
            # TIER 1 structured: the stored card's quarter == the expected latest quarter. A
            # current-quarter result_analysis card existing IS proof the company reported this quarter,
            # so the quarter-match is the authoritative guard (a stale-quarter card — e.g. a name that
            # is 'reported' but whose new card hasn't been generated — correctly falls through to
            # pending). We do NOT additionally require status='reported' here: the earnings_calendar
            # status lags reality (e.g. INFY reported Q1 FY27 but its row still reads 'upcoming'), so
            # gating on it would hide a valid fresh card. status='reported' is instead used above as a
            # first-class 'announced' trigger. Renderer order: result analysis -> FY27 -> RAW 48h -> POLISH 1mo.
            if card and card_q and card_q == expected_q:
                return _sections({"symbol": sym, "status": "announced", "tier": "structured",
                        "ex_date": str(ex_dt) if ex_dt else None, "result_analysis": card, "card_quarter": card_q,
                        "expected_quarter": expected_q, "generated_at": card_ts, "gvm_verdict": gvm_verdict})
            # Announced but no fresh current-quarter card -> pending body; FY27 + news sections still show.
            return _sections({"symbol": sym, "status": "announced_no_analysis", "tier": "pending",
                    "ex_date": str(ex_dt) if ex_dt else None, "expected_quarter": expected_q,
                    "card_quarter": card_q, "gvm_verdict": gvm_verdict})

        # Branch B: NOT ANNOUNCED (upcoming / date_tbd). cc#623 state-machine tweak — RENDER the existing
        # prior-quarter card EXPLICITLY as "LAST RESULT · <its own quarter>" (no freshness gate; it is
        # avowedly the last result), instead of suppressing it. Order: expected date + FY27 -> LAST
        # RESULT -> RAW 48h -> POLISH 1mo. fy27_outlook (retired/empty) is no longer read.
        status = "upcoming" if (ex_dt is not None) else "date_tbd"
        ed = str(ex_dt) if (ex_dt is not None) else None
        return _sections({"symbol": sym, "status": status, "tier": "last_result", "ex_date": ed,
                "expected_quarter": expected_q,
                "last_result_analysis": card, "last_card_quarter": card_q,
                "generated_at": card_ts, "gvm_verdict": gvm_verdict})

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
from fastapi.responses import JSONResponse   # cc#1192: /api/results/peers returns real 4xx codes

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


# cc#796 EXPECTATIONS. Screener's CSV carries an EXPECTED quarterly sales/profit per company. It is a
# mechanical trend projection, NOT analyst consensus — Claude web measured the season at a median
# deviation of -16% (155 beats vs 320 misses), which is what a run-rate extrapolation looks like, not
# what a broker forecast looks like. So the surface must never call it "analyst estimates" or "street
# expectations": it is "Screener projected run-rate", labelled "vs est." on the card.
#
# The columns are NOT in screener_raw yet (63 columns, none expected-*). Adding them needs an
# ALTER TABLE, which MAINTENANCE_LOCK_RULE blocks on the run_sql path — see the cc#796 task result for
# the exact migration. This probes for them so the code is safe to deploy before OR after that lands:
# absent columns simply return None, and the spec's own rule ("no expected value -> omit the line
# entirely, no empty state") makes that the correct rendering rather than a degraded one.
_EXPECTED_COLS = None      # tri-state cache: None = not probed


def _expected_cols(cur):
    global _EXPECTED_COLS
    if _EXPECTED_COLS is None:
        try:
            # cc#1191 BUG FIX. This probed for expected_qtr_profit, which DOES NOT EXIST in
            # screener_raw. The real column is expected_quarterly_net_profit — 1,688 of 1,866 rows
            # filled, right there beside expected_qtr_sales at 1,693. So the probe returned only
            # the sales column, got.get("expected_qtr_profit") was always None, and _expectations
            # has never once returned a profit side on any symbol. Not a data gap: a typo in a
            # column name, silently degrading to half a feature because the lookup used .get().
            # expected_qtr_profit stays in the IN list as a FALLBACK ALIAS, per the card — if some
            # environment does carry that name, it still resolves, and the alias costs one string.
            cur.execute("""SELECT column_name FROM information_schema.columns
                           WHERE table_name='screener_raw'
                             AND column_name IN ('expected_qtr_sales',
                                                 'expected_quarterly_net_profit',
                                                 'expected_qtr_profit')""")
            _EXPECTED_COLS = {r[0] for r in cur.fetchall()}
        except Exception:
            _EXPECTED_COLS = set()
    return _EXPECTED_COLS


def _expectations(cur, sym, actual_sales=None, actual_profit=None):
    """cc#796: {sales:{expected,actual,dev_pct,tag}, profit:{...}} for whichever side is derivable.
    Returns None when nothing is — the caller omits the line rather than rendering an empty state.

    Bands are founder-set: BEAT > +2%, IN-LINE within +/-2%, MISS < -2%. Deviation is reported
    alongside the tag, never the tag alone, because a 2.1% beat and a 60% beat are not the same
    statement and the binary hides that."""
    cols = _expected_cols(cur)
    if not cols:
        return None
    try:
        sel = ", ".join(f'"{c}"' for c in sorted(cols))
        cur.execute(f"SELECT {sel} FROM screener_raw WHERE UPPER(nse_code)=UPPER(%s)", (sym,))
        r = cur.fetchone()
    except Exception as e:
        log.warning(f"_expectations {sym}: {e}")
        return None
    if not r:
        return None
    got = dict(zip(sorted(cols), r))

    def side(exp, act):
        exp, act = _f(exp), _f(act)
        if exp is None or act is None or exp == 0:
            return None
        dev = (act - exp) / abs(exp) * 100.0
        tag = "BEAT" if dev > 2 else ("MISS" if dev < -2 else "IN-LINE")
        return {"expected": round(exp, 2), "actual": round(act, 2),
                "dev_pct": round(dev, 1), "tag": tag}

    out = {}
    s = side(got.get("expected_qtr_sales"), actual_sales)
    # cc#1191: real name first, historical alias second. Written as an explicit two-step rather
    # than a single .get() so that which column actually answered is readable at a glance.
    _exp_pat = got.get("expected_quarterly_net_profit")
    if _exp_pat is None:
        _exp_pat = got.get("expected_qtr_profit")
    p = side(_exp_pat, actual_profit)
    if s:
        out["sales"] = s
    if p:
        out["profit"] = p
    return out or None


# ── cc#1268 · RESULT DOT V3 (RESULT_DOT_RULE_V3, session_log 29790) ──────────────────────────────
# Supersedes V2 (29672) and the cc#1238 V1 compute. Basis = the company's OWN pre-result estimate,
# resolved as the latest fundamentals_history quarterly snapshot with scraped_at < the result date,
# carrying BOTH expected_qtr_sales and expected_quarterly_net_profit. NEVER live screener_raw for
# an already-reported quarter — its expected fields roll forward after the result posts, which was
# V2 Check B's actual defect (it read screener_raw current-value, drifting the moment the result
# landed). ONE function, consumed by both the digest deck rows and the result sheet, so the two
# surfaces cannot compute two different colours for the same symbol.
#
# THE STRUCTURAL FINDING THIS FUNCTION SHIPS WITH (measured 24-Aug, re-verified before this push):
# fundamentals_history holds ZERO rows carrying either expected_* field — the scrape has only ever
# written ACTUALS (Sales, Net Profit, OPM, ...) per quarter, never a captured pre-result estimate.
# screener_raw_bak_cc804 (the one candidate historical fallback) carries neither column either. So
# no valid pre-result snapshot survives ANYWHERE for Q1FY27, and this function returns dotless for
# every symbol until a forward-only snapshot job (its own card, not yet filed) has captured at
# least one pre-result reading across a future season. That is the honest V3 contract working as
# specced — "NULL/no-dot when no valid pre-result estimate" — not a bug in this code.
def _result_dot_v3(cur, sym, result_date, actual_sales=None, actual_profit=None):
    """{dot, basis:'own_estimate', est_sales, est_pat, snapshot_date} or {dot: None} when no
    pre-result snapshot exists. green = actual_sales>=est_sales AND actual_pat>=est_pat;
    amber = exactly one; red = neither; dotless when either actual or the snapshot is missing."""
    if not sym or not result_date:
        return {"dot": None, "basis": None}
    try:
        cur.execute("""
            SELECT metrics, scraped_at FROM fundamentals_history
            WHERE UPPER(symbol)=UPPER(%s) AND section='quarters' AND period_type='quarter'
              AND scraped_at < %s
              AND metrics ? 'expected_qtr_sales' AND metrics ? 'expected_quarterly_net_profit'
            ORDER BY scraped_at DESC LIMIT 1
        """, (sym, result_date))
        row = cur.fetchone()
    except Exception as e:
        log.warning(f"_result_dot_v3 {sym}: {e}")
        return {"dot": None, "basis": None}
    if not row:
        return {"dot": None, "basis": None}
    metrics, snap_ts = row
    est_sales = _f(str(metrics.get("expected_qtr_sales") or "").replace(",", ""))
    est_pat = _f(str(metrics.get("expected_quarterly_net_profit") or "").replace(",", ""))
    if est_sales is None or est_pat is None or actual_sales is None or actual_profit is None:
        return {"dot": None, "basis": None, "est_sales": est_sales, "est_pat": est_pat,
                "snapshot_date": str(snap_ts.date()) if snap_ts else None}
    sales_ok = actual_sales >= est_sales
    pat_ok = actual_profit >= est_pat
    dot = "green" if (sales_ok and pat_ok) else ("amber" if (sales_ok or pat_ok) else "red")
    return {"dot": dot, "basis": "own_estimate", "est_sales": est_sales, "est_pat": est_pat,
            "snapshot_date": str(snap_ts.date()) if snap_ts else None}


# ── cc#797 BASIC POLISH L1 (design BASIC_POLISH_L1_CARD_DESIGN_V1, session_log 13551) ────────────
# Block 1 is ABSOLUTES FIRST: Sales / PAT with QoQ and YoY, margin latest vs last year, PE vs industry.
#
# SOURCE DEVIATION, STATED: the design says these come from the CSV's latest/preceding/preceding-year
# columns. Those columns do not exist in screener_raw (63 columns, checked) and would need the same
# blocked migration as cc#796. fundamentals_history carries the identical quarterly series for 331
# symbols after the cc#790 scrape, so L1 is built from there instead — same numbers, available today,
# and no dependency on a migration. The CSV can replace this source later without changing the card.
#
# BFSI handling is derived from the DATA rather than a hardcoded name list, which cannot go stale:
#   has 'OPM %'              -> non-BFSI, show the OPM pair
#   has 'Financing Margin %' -> Bank/NBFC, relabel the row "Financing Margin"
#   neither                  -> Insurer, hide the margin row entirely
def _l1_quarter(cur, sym, segment=None):
    try:
        cur.execute("""SELECT period_end, period_label, metrics FROM fundamentals_history
                       WHERE UPPER(symbol)=UPPER(%s) AND section='quarters' AND period_type='quarter'
                       ORDER BY period_end DESC LIMIT 5""", (sym,))
        rows = cur.fetchall()
    except Exception as e:
        log.warning(f"_l1_quarter {sym}: {e}")
        return None
    if not rows:
        return None

    def num(m, *keys):
        for k in keys:
            v = m.get(k)
            if v not in (None, "", "-"):
                n = _f(str(v).replace(",", "").replace("%", ""))
                if n is not None:
                    return n
        return None

    cur_pe, cur_lbl, cur_m = rows[0][0], rows[0][1], (rows[0][2] or {})
    prev_m = (rows[1][2] or {}) if len(rows) > 1 else {}
    # year-ago = same quarter-end one year back; None when the series does not reach it
    yr_m = {}
    for pe, _lbl, m in rows:
        if pe and cur_pe and pe.year == cur_pe.year - 1 and pe.month == cur_pe.month:
            yr_m = m or {}
            break

    def pct(now, was):
        if now is None or was is None or was == 0:
            return None
        return round((now - was) / abs(was) * 100.0, 1)

    sales_k = ("Sales", "Revenue")
    s_now, s_prev, s_yr = num(cur_m, *sales_k), num(prev_m, *sales_k), num(yr_m, *sales_k)
    p_now, p_prev, p_yr = num(cur_m, "Net Profit"), num(prev_m, "Net Profit"), num(yr_m, "Net Profit")

    pe_self = pe_ind = None
    is_insurer = False
    try:
        cur.execute("""SELECT pe, industry_group FROM screener_raw
                       WHERE UPPER(nse_code)=UPPER(%s)""", (sym,))
        r = cur.fetchone()
        if r:
            pe_self = _f(r[0])
            is_insurer = (str(r[1] or "").strip().lower() == "insurance")
    except Exception:
        pass

    # cc#1311: screener_raw.segment_pe used to feed "industry" PE and is dead — 0 of 1870 rows have
    # ever carried it, so this line always rendered as an em-dash. Founder: derive it from GVM segment
    # peers instead. Reusing the EXACT top-3-by-GVM-desc, self-excluded peer set _peer_comparison and
    # _peer_results already use for Sales/PAT — one peer basis for the whole card, not a second one
    # invented just for this row. "Industry" here means those three peers' own PE, averaged; a peer
    # with no PE (loss-making, or not in screener_raw) is dropped rather than counted as 0.
    try:
        if segment:
            cur.execute("""SELECT g.symbol FROM gvm_scores g
                           WHERE g.segment=%s AND g.symbol<>%s AND g.gvm_score IS NOT NULL
                             AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                           ORDER BY g.gvm_score DESC LIMIT 3""", (segment, sym))
            peer_syms = [row[0] for row in cur.fetchall()]
            if peer_syms:
                cur.execute("""SELECT pe FROM screener_raw WHERE UPPER(nse_code) = ANY(%s)""",
                            ([s.upper() for s in peer_syms],))
                peer_pes = [_f(row[0]) for row in cur.fetchall()]
                peer_pes = [p for p in peer_pes if p is not None]
                if peer_pes:
                    pe_ind = round(sum(peer_pes) / len(peer_pes), 1)
    except Exception as e:
        log.warning(f"_l1_quarter industry_pe {sym}: {e}")

    # Insurers must be checked EXPLICITLY, not inferred from the metric keys. Screener publishes an
    # "OPM %" for them (verified: SBILIFE, CANHLIFE, ICICIPRULI, GODIGIT, STARHEALTH, NIACL all carry
    # it), so a keys-only rule would have shown them a Margins row the design says to hide — and an
    # operating margin on a life insurer is not a meaningful number to put in front of a reader.
    # industry_group='Insurance' separates all six cleanly from banks and non-BFSI.
    if is_insurer:
        m_label, m_now, m_yr = None, None, None      # row hidden entirely, never shown empty
    elif "OPM %" in cur_m:
        m_label, m_now, m_yr = "Margins", num(cur_m, "OPM %"), num(yr_m, "OPM %")
    elif "Financing Margin %" in cur_m:
        m_label, m_now, m_yr = "Financing Margin", num(cur_m, "Financing Margin %"), num(yr_m, "Financing Margin %")
    else:
        m_label, m_now, m_yr = None, None, None

    return {
        # cc#801 fix_6: ONE quarter naming card-wide. period_label is "Jun 2026", which rendered as
        # "QUARTER · JUN 2026" while every other block said "Q1 FY27". _fq_label is the canonical
        # converter already used by the peer block, so both now read the same.
        "quarter_label": (_fq_label(cur_pe) or cur_lbl), "quarter_end": str(cur_pe) if cur_pe else None,
        "sales": {"value": s_now, "qoq": pct(s_now, s_prev), "yoy": pct(s_now, s_yr)},
        "pat": {"value": p_now, "qoq": pct(p_now, p_prev), "yoy": pct(p_now, p_yr)},
        "margin": ({"label": m_label, "now": m_now, "ly": m_yr,
                    "pp": (round(m_now - m_yr, 1) if (m_now is not None and m_yr is not None) else None)}
                   if m_label else None),
        "valuation": {"pe": pe_self, "industry_pe": pe_ind},
    }


# cc#797 AUTO-VERDICT: one deterministic line, no LLM. 12 combinations = PAT YoY sign (2) x vs-est
# band (3) x margin direction (2). The point is that the same inputs always produce the same sentence,
# so the card cannot drift between renders or cost anything to generate. When a dimension is unknown
# the sentence simply omits that clause rather than guessing a direction.
def _auto_verdict(l1, expectations):
    if not l1:
        return None
    pat = (l1.get("pat") or {})
    yoy = pat.get("yoy")
    if yoy is None:
        return None
    mg = l1.get("margin") or {}
    pp = mg.get("pp")
    band = None
    exp = expectations or {}
    if exp.get("profit"):
        band = exp["profit"].get("tag")
    elif exp.get("sales"):
        band = exp["sales"].get("tag")

    grew = yoy >= 0
    head = ("Profit grew %.0f%%" % abs(yoy)) if grew else ("Profit fell %.0f%%" % abs(yoy))
    if band == "BEAT":
        mid = " and beat the projected run-rate"
    elif band == "MISS":
        mid = " but missed the projected run-rate" if grew else " and missed the projected run-rate"
    elif band == "IN-LINE":
        mid = " and landed on the projected run-rate"
    else:
        mid = ""
    if pp is None:
        tail = "."
    elif pp > 0.05:
        tail = "; margins did the work." if grew else "; margins held up even so."
    elif pp < -0.05:
        tail = "; margins gave ground." if grew else "; margins went with it."
    else:
        tail = "; margins were flat."
    return head + mid + tail


def _fy27_growth(cur, sym):
    """cc#623: FY27 estimated growth % from input_raw.fy27_growth (Sonnet on Trendlyne consensus,
    ~1536/2008 populated). Numeric or None. NEVER touches the retired empty fy27_outlook column."""
    cur.execute("SELECT fy27_growth FROM input_raw WHERE nse_code=%s", (sym,))
    r = cur.fetchone()
    return _f(r[0]) if (r and r[0] is not None) else None


def _raw_news(cur, sym, hours=168):
    """cc#623 / cc#625 / cc#650: RAW headlines section — position_news headlines for this exact symbol,
    date-sorted desc, over the last 7 days (cc#650 widened from 48h; headlines-only list — no summaries).
    The cutoff is driven by the ARTICLE's published_at ONLY — never COALESCE with fetched_at, which let
    a stale article (published earlier, fetched today) slip through on ingest drift. published_at is
    100% populated for the recent window; a null-published row cannot prove freshness and is correctly
    excluded. Capped at the latest 10 so the card stays scannable."""
    cur.execute("""
        SELECT headline, source_name, url, published_at
        FROM position_news
        WHERE symbol = %s AND published_at >= NOW() - make_interval(hours => %s)
        ORDER BY published_at DESC, id DESC
        LIMIT 10""", (sym, hours))
    return [{"headline": r[0], "source": r[1], "url": r[2],
             "published_at": r[3].isoformat() if r[3] else None} for r in cur.fetchall()]


def _polished_by_symbol(cur, sym, days=30):
    """cc#623 POLISHED architecture: REPLACES the cc#619 per-item url_hash lookup (2% hit rate) with a
    symbol-section query — polished_news where the symbol is in mentioned_symbols. cc#625 fix_2: the
    1-month window is driven by the ARTICLE's published_time (not polished_at, which is when WE polished
    it and drifts — an old article polished today would masquerade as fresh). Date-sorted desc."""
    # cc#802 CATEGORY SCOPE (founder correction supersedes the earlier amendment): company news lives
    # under Domestic, so shorts are DOMESTIC ONLY — Global and IPO are deliberately NOT pulled into a
    # company card. AI Editorial long-form and Stock Views ride the same stream, tagged, so the reader
    # gets one merged feed newest-first rather than three sections to reconcile.
    # cc#830: the category list (and now the wire-stub + reco-shape guards) moved to news_endpoints
    # so the R card and the GVM page Latest News block read from ONE definition. The inline
    # `category IN (...)` that used to sit here was the copy that would have drifted.
    from news_endpoints import polished_company_filter
    _scope_sql, _scope_params = polished_company_filter(headline_col="headline_clean")
    # cc#1309: the result-sheet COMPANY NEWS block is narrower than the general company filter —
    # founder wants Domestic + AI Editorial only here, Stock Views excluded. Overriding the
    # category list AFTER the call rather than editing polished_company_filter/
    # POLISHED_COMPANY_CATEGORIES, which is shared with the GVM page's Latest News block and other
    # callers this card has no business narrowing.
    _scope_params[0] = ["Domestic", "AI Editorial"]
    cur.execute(f"""
        SELECT headline_clean, COALESCE(full_summary, summary) AS summary, source, published_time,
               category, summary AS short_summary
        FROM polished_news
        WHERE %s = ANY(mentioned_symbols) AND published_time >= NOW() - make_interval(days => %s)
        {_scope_sql}
        ORDER BY published_time DESC, id DESC
        LIMIT 15""", [sym, days] + _scope_params)
    # cc#1309: `source` is dropped from the returned shape entirely — it carries whatever outlet
    # ingest recorded (an outside wire name, or an internal tag like analyst_desk, inconsistently),
    # and the founder's ruling is that this card never names an outside agency. `category` is the
    # one field this card is allowed to show, and it is already constrained to Domestic/AI
    # Editorial by the query above, so nothing here can leak a source name by omission.
    return [{"headline": r[0], "summary": r[1], "published_time": r[3].isoformat() if r[3] else None,
             "category": r[4], "short_summary": r[5]} for r in cur.fetchall()]


def _conn():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


_COLS_READY = False


def _ensure_cols(cur):
    """cc#857 fix 3: STARTUP ONLY — this must never run on a request path again.

    It fired 2x ALTER TABLE input_raw on EVERY card load. Even as a no-op, Postgres takes an
    ACCESS EXCLUSIVE lock on input_raw for each one, so concurrent card loads serialised behind
    each other and behind anything else touching that table. It is also the wrong reading of
    MAINTENANCE_LOCK_RULE (cc#351) — DDL is console/propose-first, and firing it per page view is
    the exact opposite of that.

    The module-level guard makes it a no-op after the first call, and ensure_startup() below runs
    that first call once at boot.
    """
    global _COLS_READY
    if _COLS_READY:
        return
    cur.execute("ALTER TABLE input_raw ADD COLUMN IF NOT EXISTS fy27_outlook TEXT")
    cur.execute("ALTER TABLE input_raw ADD COLUMN IF NOT EXISTS last_fy27_outlook_updated TIMESTAMP")
    _COLS_READY = True


@router.on_event("startup")
def ensure_startup():
    """cc#857: run the one-time DDL at boot, off every user request path."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_cols(cur)
            conn.commit()
        log.info("cc#857: input_raw columns ensured at startup (not per request)")
    except Exception as e:
        log.warning("cc#857 startup column ensure failed (non-fatal): %s", e)


# ── cc#857 fix 5: PEER BLOCK CACHE ────────────────────────────────────────────────────────────
# _peer_comparison and _peer_results depend ONLY on (segment, score_date) and change once a day,
# yet every symbol in a segment recomputed the identical result. 'IT - Small' has 33 members, so
# that was 33 identical scans per day minimum. Keyed by (fn, segment, date); the date component
# makes it self-expiring, so no TTL sweep is needed and a new score_date can never serve stale
# peers. In-process is sufficient for v1 (single Railway service); if this ever runs multi-replica
# each replica simply warms its own copy.
_PEER_CACHE: dict = {}


def _cached(kind, segment, day, build):
    if not segment:
        return build()
    key = (kind, segment, str(day))
    if key in _PEER_CACHE:
        return _PEER_CACHE[key]
    val = build()
    if len(_PEER_CACHE) > 400:      # bounded: ~200 segments x 2 kinds, cleared on the day roll
        _PEER_CACHE.clear()
    _PEER_CACHE[key] = val
    return val


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
    # cc#801 fix_1 — ONE PROFIT DEFINITION CARD-WIDE. This used to read the subject's own growth from
    # screener_raw.qoq_profit_growth while the header read fundamentals_history "Net Profit", so the
    # same card showed PAT YoY +3.3% up top and 5.79% in the peer row (TORNTPHARM). Screener's profit
    # line is not the post-minority Net Profit the EPS is struck on, so the two could never agree.
    #
    # Both the subject AND every peer are now computed from fundamentals_history "Net Profit" and
    # Sales|Revenue — same table, same line, same YoY arithmetic as the L1 block. The screener
    # qoq_* columns are no longer read here at all; that second source is dead.
    # cc#857 fix 1+2 — SAME OUTPUT, ONE PASS. Two defects were compounding here:
    #   (a) the correlated `WHERE c.period_end = (SELECT MAX(period_end) FROM q z WHERE z.sym=c.sym)`
    #       re-ran a SubPlan once per quarter row (8,888 times), and
    #   (b) the CTE materialised EVERY symbol's full quarter history and then discarded ~99% of it,
    #       so the nested loop removed 6,132,032 rows by join filter.
    # Now the segment is joined FIRST (so `q` holds ~100 rows instead of 8,888) and the latest
    # quarter comes from MAX(period_end) OVER (PARTITION BY sym) in the same scan.
    # Measured on segment 'IT - Small': 3,769 ms -> 18 ms, rows-removed 6,132,032 -> 94, and the
    # result set is byte-identical (33 rows, zero symmetric difference vs the old query).
    # Restricting `q` to segment symbols is safe: `p` only ever joins on p.sym = c.sym, and c.sym
    # is always a segment symbol, so no year-ago row that could have matched is excluded.
    cur.execute("""
        WITH seg AS (
            SELECT UPPER(symbol) AS sym, gvm_score
            FROM gvm_scores WHERE segment=%s AND symbol<>%s),
        q AS (
            SELECT UPPER(f.symbol) AS sym, f.period_end,
                   NULLIF(replace(COALESCE(f.metrics->>'Sales', f.metrics->>'Revenue'),',',''),'')::numeric AS rev,
                   NULLIF(replace(f.metrics->>'Net Profit',',',''),'')::numeric AS pat
            FROM fundamentals_history f
            JOIN seg ON seg.sym = UPPER(f.symbol)
            WHERE f.section='quarters' AND f.period_type='quarter'),
        ranked AS (
            SELECT sym, period_end, rev, pat,
                   MAX(period_end) OVER (PARTITION BY sym) AS max_pe
            FROM q),
        yoy AS (
            SELECT c.sym, c.period_end AS latest_q,
                   CASE WHEN p.rev > 0 THEN (c.rev - p.rev) / p.rev * 100 END AS s_yoy,
                   CASE WHEN p.pat > 0 THEN (c.pat - p.pat) / p.pat * 100 END AS p_yoy
            FROM ranked c
            LEFT JOIN q p ON p.sym = c.sym
                         AND p.period_end = (c.period_end - INTERVAL '1 year')::date
            WHERE c.period_end = c.max_pe)
        SELECT seg.gvm_score, y.s_yoy, y.p_yoy, y.latest_q
        FROM seg LEFT JOIN yoy y ON y.sym = seg.sym""", (segment, sym))
    rows = [(_flt(r[0]), _flt(r[1]), _flt(r[2]), r[3]) for r in cur.fetchall()]
    same = [p for p in rows if subj_q is not None and p[3] == subj_q]
    # cc#697 bug_1: SAME-QUARTER ONLY — never blend quarters. Rank the same-quarter reporters by GVM and
    # take the top 3 (like cc#687); 1-2 -> compare vs those with an honest count; 0 -> caller shows the
    # sector line only (peer figures come back null). The old <3 -> full-pool fallback is removed.
    peers = same

    def _top3(idx):
        cand = [(p[0], p[idx]) for p in peers if p[idx] is not None and p[0] is not None]
        if not cand:
            return None, 0
        cand.sort(key=lambda x: -x[0])
        use = cand[:3]
        return sum(v for _, v in use) / len(use), len(use)

    peer_s, n_s = _top3(1)
    peer_p, n_p = _top3(2)
    # cc#801 fix_1: the subject's own figures come from the SAME source as the peers and as the L1
    # header — fundamentals_history, Net Profit and Sales|Revenue — not from screener_raw.
    cur.execute("""
        WITH q AS (
            SELECT period_end,
                   NULLIF(replace(COALESCE(metrics->>'Sales', metrics->>'Revenue'),',',''),'')::numeric AS rev,
                   NULLIF(replace(metrics->>'Net Profit',',',''),'')::numeric AS pat
            FROM fundamentals_history
            WHERE UPPER(symbol)=UPPER(%s) AND section='quarters' AND period_type='quarter')
        SELECT CASE WHEN p.rev > 0 THEN (c.rev - p.rev) / p.rev * 100 END,
               CASE WHEN p.pat > 0 THEN (c.pat - p.pat) / p.pat * 100 END
        FROM q c LEFT JOIN q p ON p.period_end = (c.period_end - INTERVAL '1 year')::date
        WHERE c.period_end = (SELECT MAX(period_end) FROM q)""", (sym,))
    # cc#857: this one is already single-symbol (WHERE UPPER(symbol)=UPPER(%s)), so its subquery
    # scans a handful of rows, not 8,888 — it is NOT the correlated-per-row pattern that made the
    # peer query slow, and rewriting it would add noise without measurable gain. Left as-is
    # deliberately, having checked rather than assumed.
    sr = cur.fetchone()
    st_s = _flt(sr[0]) if sr else None
    st_p = _flt(sr[1]) if sr else None
    if st_s is None and st_p is None and peer_s is None and peer_p is None:
        return None
    return {
        "peer_basis": "top-3 same-quarter reporters by GVM in segment (self-excluded)",
        "segment": segment,
        "quarter": quarter,
        # cc#697 bug_2: screener_raw qoq_sales_growth / qoq_profit_growth are Screener-export LATEST-Q vs
        # year-ago-Q figures (YoY semantics), NOT sequential QoQ. Label as YoY in the UI.
        "growth_basis": "YoY",
        "peer_count": max(n_s, n_p),
        "same_quarter_peers": len(same),
        "total_peers": len(rows),
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


def _completed_quarter_end(today=None):
    """cc#766: the most recent COMPLETED fiscal quarter-end (Mar/Jun/Sep/Dec) on or before today — the
    quarter peers are currently reporting. Used as the validated-reported window start for the peer table."""
    d = today or date.today()
    ends = [date(d.year, 3, 31), date(d.year, 6, 30), date(d.year, 9, 30),
            date(d.year, 12, 31), date(d.year - 1, 12, 31)]
    return max(e for e in ends if e <= d)


def _result_day_move(cur, psym, rep_date):
    """cc#766: peer stock move % on its result day = raw_prices close on rep_date vs the prior trading
    close. None when either bar is missing (result day was a no-trade holiday, or pre-listing history)."""
    cur.execute("SELECT close FROM raw_prices WHERE symbol=%s AND price_date=%s", (psym, rep_date))
    c = cur.fetchone()
    if not c or c[0] is None:
        return None
    cur.execute("""SELECT close FROM raw_prices WHERE symbol=%s AND price_date < %s AND close IS NOT NULL
                   ORDER BY price_date DESC LIMIT 1""", (psym, rep_date))
    p = cur.fetchone()
    if not p or p[0] is None:
        return None
    try:
        return round((float(c[0]) - float(p[0])) / float(p[0]) * 100.0, 2)
    except Exception:
        return None


def _peer_results(cur, sym, segment, today=None, include_self=False):
    """cc#766: pre-results peer context for the R-card. Uses the SAME top-3-by-GVM same-segment peer set
    as the MISS/BEAT block (self-excluded). For each peer with a VALIDATED reported result THIS quarter
    (earnings_calendar status='reported' AND verified<>'false', ex_date >= the last completed quarter-end
    — the cc#765 gate: a news lead sits at 'upcoming' and is never surfaced as reported) it returns the
    result date, Sales/PAT YoY + margin vs LY (screener_raw export) and the stock's move on result day
    (raw_prices close vs prior close). Unreported peers come back greyed with their expected date. Returns
    None (button hidden) when no peer has reported. Ordered GVM desc. Zero new scrape."""
    if not segment:
        return None
    d = today or date.today()
    q_start = _completed_quarter_end(d)
    quarter = _fq_label(q_start)
    cur.execute("""SELECT g.symbol, g.gvm_score FROM gvm_scores g
                   WHERE g.segment=%s AND g.symbol<>%s AND g.gvm_score IS NOT NULL
                     AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                   ORDER BY g.gvm_score DESC LIMIT 3""", (segment, sym))
    peers = cur.fetchall()
    if not peers:
        return None
    # cc#1192: the SUBJECT joins the pool when asked, and only then. The top-3 selection above is
    # untouched — self is still excluded from it, so which three peers appear never changes. The
    # subject is APPENDED, flagged is_self, and scored through the identical arithmetic below, so
    # /api/results/peers can show "you versus your three best peers" without a second code path
    # that could drift from this one.
    self_gvm = None
    if include_self:
        cur.execute("""SELECT g.gvm_score FROM gvm_scores g
                       WHERE g.symbol=%s AND g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                       LIMIT 1""", (sym,))
        _sg = cur.fetchone()
        self_gvm = _sg[0] if _sg else None
        peers = list(peers) + [(sym, self_gvm)]
    # cc#857 fix 6: was 3 peers x 3 queries = 9 round trips. Now TWO queries keyed by symbol,
    # fetched once and looked up in the loop. Same rows, same gates (status='reported',
    # verified<>'false', ex_date >= completed quarter end — the cc#765 news-lead gate is preserved
    # verbatim); DISTINCT ON reproduces the per-symbol `ORDER BY ex_date DESC LIMIT 1`.
    psyms = [p[0] for p in peers]
    cur.execute("""SELECT DISTINCT ON (UPPER(ticker)) UPPER(ticker), ex_date
                   FROM earnings_calendar
                   WHERE UPPER(ticker) = ANY(%s) AND status='reported'
                     AND verified<>'false' AND ex_date >= %s
                   ORDER BY UPPER(ticker), ex_date DESC""", (psyms, q_start))
    rep_map = {r[0]: r[1] for r in cur.fetchall()}
    # cc#1192 (RESULT_PEER_SOURCE_RULE_V1, session_log 29006): screener_raw still supplies the
    # company NAME and the two OPM columns, and nothing else. qoq_sales_growth and
    # qoq_profit_growth are GONE from this function — they were the last result surface still
    # reading them, and they disagree with every other result surface on the card.
    #
    # MEASURED, not asserted: on the top-mcap analysed name in each of the nine largest segments,
    # sales YoY is identical on both sources 9 times out of 9, and PAT YoY differs on 4 of 9.
    # CONTROLPR is the clearest: this quarter's PAT is 3.92 on both, but the year-ago figure is
    # 8.57 in the filed quarterly history and 6.47 in the CSV, giving -54.3% against -39.4%. The
    # filing is the number the company reported; the CSV line is not the post-minority Net Profit
    # the rest of this card is struck on. cc#801 already killed this second source inside
    # _peer_comparison for exactly this reason; _peer_results was the leftover.
    cur.execute("""SELECT DISTINCT ON (UPPER(nse_code)) UPPER(nse_code), company_name,
                          opm_latest_q, opm_prev_year_q
                   FROM screener_raw WHERE UPPER(nse_code) = ANY(%s)
                   ORDER BY UPPER(nse_code)""", (psyms,))
    scr_map = {r[0]: r[1:] for r in cur.fetchall()}

    # YoY from fundamentals_history, the SAME CTE shape _peer_comparison uses — latest period_end
    # per symbol, year-ago row at exactly period_end minus one year, Sales or Revenue, Net Profit.
    # Not a re-derivation: the shape is copied so the two peer blocks on one card cannot disagree.
    cur.execute("""
        WITH q AS (
            SELECT UPPER(f.symbol) AS sym, f.period_end,
                   NULLIF(replace(COALESCE(f.metrics->>'Sales', f.metrics->>'Revenue'),',',''),'')::numeric AS rev,
                   NULLIF(replace(f.metrics->>'Net Profit',',',''),'')::numeric AS pat
            FROM fundamentals_history f
            WHERE f.section='quarters' AND f.period_type='quarter'
              AND UPPER(f.symbol) = ANY(%s)),
        ranked AS (
            SELECT sym, period_end, rev, pat,
                   MAX(period_end) OVER (PARTITION BY sym) AS max_pe
            FROM q)
        SELECT c.sym,
               CASE WHEN p.rev > 0 THEN (c.rev - p.rev) / p.rev * 100 END AS s_yoy,
               CASE WHEN p.pat > 0 THEN (c.pat - p.pat) / p.pat * 100 END AS p_yoy
        FROM ranked c
        LEFT JOIN q p ON p.sym = c.sym
                     AND p.period_end = (c.period_end - INTERVAL '1 year')::date
        WHERE c.period_end = c.max_pe""", (psyms,))
    yoy_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    out, n_reported = [], 0
    for psym, gvm in peers:
        rep_date = rep_map.get(psym)
        sr = scr_map.get(psym)
        name = (sr[0] if sr and sr[0] else None) or psym
        rec = {"symbol": psym, "name": name, "gvm": _f(gvm), "reported": rep_date is not None}
        if include_self and psym == sym:
            rec["is_self"] = True
        if rep_date is not None:
            n_reported += 1
            _yy = yoy_map.get(psym) or (None, None)
            rec.update({
                "result_date": str(rep_date),
                # cc#1192: sales_yoy and pat_yoy come from fundamentals_history now. The two OPM
                # columns still come from screener_raw and their indices shifted by two when the
                # qoq pair left the SELECT — indices are why this is spelled out rather than nudged.
                "sales_yoy": _f(_yy[0]),
                "pat_yoy": _f(_yy[1]),
                "margin": _f(_flt(sr[1])) if sr else None,
                "margin_ly": _f(_flt(sr[2])) if sr else None,
                "move_pct": _result_day_move(cur, psym, rep_date),
            })
        else:
            # unreported -> greyed with the peer's own next expected earnings_calendar date
            cur.execute("""SELECT ex_date FROM earnings_calendar
                           WHERE UPPER(ticker)=%s AND ex_date >= %s ORDER BY ex_date ASC LIMIT 1""", (psym, d))
            er = cur.fetchone()
            rec["expected_date"] = str(er[0]) if (er and er[0]) else None
        out.append(rec)
    if n_reported == 0:
        return None
    return {"quarter": quarter, "segment": segment, "n_reported": n_reported, "peers": out}


def _fundamentals(cur, sym):
    cur.execute('''SELECT "Operating profit growth", roce, opm, "Debt to equity", "Return on equity"
                   FROM screener_raw WHERE nse_code=%s LIMIT 1''', (sym,))
    r = cur.fetchone()
    if not r:
        return {}
    return {"opg": _f(r[0]), "roce": _f(r[1]), "opm": _f(r[2]), "de": _f(r[3]), "roe": _f(r[4])}


@router.get("/api/results/card/context")
def results_card_context(symbol: str):
    """cc#858: the four HEAVY blocks, fetched in parallel after the core card has already painted.

    Deliberately independent of the core: a slow peer block must never hold back news and vice
    versa, and if this whole call fails the core card stays fully usable — the client shows a quiet
    "could not load" line per block rather than a blank card or an endless shimmer.

    Each block is individually try/except-ed for the same reason. One failing block degrades to
    null and names itself in `errors`; it does not take the other three down with it.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol is required"}
    out = {"symbol": sym, "peer_comparison": None, "peer_results": None,
           "raw_news": None, "polished_news": None, "errors": {}}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT segment FROM gvm_scores WHERE symbol=%s
                           ORDER BY score_date DESC LIMIT 1""", (sym,))
            r = cur.fetchone()
            segment = r[0] if r else None
            out["segment"] = segment
            today = date.today()
            for key, build in (
                ("peer_comparison", lambda: _cached("cmp", segment, today,
                                                    lambda: _peer_comparison(cur, sym, segment))),
                ("peer_results",    lambda: _cached("res", segment, today,
                                                    lambda: _peer_results(cur, sym, segment, today))),
                ("raw_news",        lambda: _raw_news(cur, sym, hours=168)),
                ("polished_news",   lambda: _polished_by_symbol(cur, sym, days=30)),
            ):
                try:
                    out[key] = build()
                except Exception as e:
                    out["errors"][key] = f"{type(e).__name__}: {str(e)[:120]}"
                    log.warning("cc#858 context block %s failed for %s: %s", key, sym, e)
                    try:
                        conn.rollback()   # un-poison the txn so the remaining blocks still run
                    except Exception:
                        pass
    except Exception as e:
        log.exception("cc#858 card context failed")
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ── cc#1191 scope 2: the whole segment's results in one payload ──────────────────────────────
# ONE SQL. The card says no per-row loops and it is not a style note: 130 segments exist and the
# largest carry 20+ members, so a per-symbol round trip would be the cc#857 mistake again (3 peers
# x 3 queries became 9 trips there; here it would be 23 x 4).
#
# THE TWO ESTIMATE SOURCES ARE NEVER BLENDED, per estimate_sources_locked on the card:
#   vs_est   is screener_raw.expected_quarterly_net_profit ONLY - a per-QUARTER absolute, compared
#            against the fundamentals_history actual for the SAME quarter. Labelled "vs Screener
#            run-rate" so nobody reads it as a broker consensus.
#   fy27_est is input_raw.fy27_growth ONLY - Trendlyne consensus, the same field the R card's FY27
#            row already reads, 1,536 of 2,008 filled.
# Never input_raw.fy27_revenue_est (0 rows), never fy27_outlook (retired at cc#609), never the
# screener qoq_* columns (killed for result surfaces at cc#1192, one card ago).
_SEGMENT_RESULTS_SQL = """
    WITH seg AS (
        SELECT UPPER(g.symbol) AS sym, g.gvm_score, g.company_name
        FROM gvm_scores g
        WHERE g.segment = %s AND g.score_date = (SELECT MAX(score_date) FROM gvm_scores)),
    rep AS (
        SELECT DISTINCT ON (UPPER(ticker)) UPPER(ticker) AS sym, ex_date
        FROM earnings_calendar
        WHERE UPPER(ticker) IN (SELECT sym FROM seg) AND status = 'reported'
          AND verified <> 'false' AND ex_date >= %s
        ORDER BY UPPER(ticker), ex_date DESC),
    nxt AS (
        SELECT DISTINCT ON (UPPER(ticker)) UPPER(ticker) AS sym, ex_date
        FROM earnings_calendar
        WHERE UPPER(ticker) IN (SELECT sym FROM seg) AND ex_date >= %s
        ORDER BY UPPER(ticker), ex_date ASC),
    q AS (
        SELECT UPPER(f.symbol) AS sym, f.period_end,
               NULLIF(replace(COALESCE(f.metrics->>'Sales', f.metrics->>'Revenue'),',',''),'')::numeric AS rev,
               NULLIF(replace(f.metrics->>'Net Profit',',',''),'')::numeric AS pat
        FROM fundamentals_history f
        WHERE f.section = 'quarters' AND f.period_type = 'quarter'
          AND UPPER(f.symbol) IN (SELECT sym FROM seg)),
    ranked AS (
        SELECT sym, period_end, rev, pat,
               MAX(period_end) OVER (PARTITION BY sym) AS max_pe FROM q),
    yoy AS (
        SELECT c.sym, c.pat AS act_pat,
               CASE WHEN p.rev > 0 THEN (c.rev - p.rev) / p.rev * 100 END AS sales_yoy,
               CASE WHEN p.pat > 0 THEN (c.pat - p.pat) / p.pat * 100 END AS pat_yoy
        FROM ranked c
        LEFT JOIN q p ON p.sym = c.sym
                     AND p.period_end = (c.period_end - INTERVAL '1 year')::date
        WHERE c.period_end = c.max_pe)
    SELECT s.sym, COALESCE(s.company_name, s.sym) AS company, s.gvm_score,
           r.ex_date AS result_date, n.ex_date AS expected_date,
           y.sales_yoy, y.pat_yoy, y.act_pat,
           sr.expected_quarterly_net_profit AS exp_pat,
           ir.fy27_growth
    FROM seg s
    LEFT JOIN rep r  ON r.sym  = s.sym
    LEFT JOIN nxt n  ON n.sym  = s.sym
    LEFT JOIN yoy y  ON y.sym  = s.sym
    LEFT JOIN screener_raw sr ON UPPER(sr.nse_code) = s.sym
    LEFT JOIN input_raw    ir ON UPPER(ir.nse_code) = s.sym
    ORDER BY (r.ex_date IS NOT NULL) DESC, r.ex_date DESC NULLS LAST, s.gvm_score DESC NULLS LAST
"""


def _segment_results(cur, segment, today=None):
    """Every member of a segment, reported ones first, with the numbers the popout table shows."""
    d = today or date.today()
    q_start = _completed_quarter_end(d)
    cur.execute(_SEGMENT_RESULTS_SQL, (segment, q_start, d))
    rows, n_reported = [], 0
    for (sym, company, gvm, rdate, edate, s_yoy, p_yoy, act_pat, exp_pat, fy27) in cur.fetchall():
        reported = rdate is not None
        n_reported += 1 if reported else 0
        rec = {"symbol": sym, "company": company, "gvm": _f(gvm), "reported": reported,
               "sales_yoy": _f(s_yoy), "pat_yoy": _f(p_yoy), "fy27_est": _f(fy27)}
        if reported:
            rec["result_date"] = str(rdate)
            # vs_est: the SAME BEAT/IN-LINE/MISS bands _expectations uses (+/-2%), not a second
            # set of thresholds. A card and its segment table disagreeing on what counts as a
            # beat would be the drift cc#1192 just finished removing from this file.
            _e, _a = _f(exp_pat), _f(act_pat)
            if _e not in (None, 0) and _a is not None:
                dev = (_a - _e) / abs(_e) * 100.0
                rec["vs_est"] = {"side": "profit", "dev_pct": round(dev, 1),
                                 "tag": "BEAT" if dev > 2 else ("MISS" if dev < -2 else "IN-LINE")}
            else:
                rec["vs_est"] = None
        else:
            # An unreported member is greyed with its own next expected date. Absent is absent —
            # no date is invented when the calendar has none.
            rec["expected_date"] = str(edate) if edate else None
        rows.append(rec)
    return {"segment": segment, "quarter": _fq_label(q_start),
            "n_reported": n_reported, "n_total": len(rows), "rows": rows}


@router.get("/api/results/segment/{segment}")
def results_segment(segment: str):
    """cc#1191 scope 2 — SEGMENT RESULTS, the table behind the R card's segment chip."""
    seg = (segment or "").strip()
    if not seg:
        return JSONResponse({"error": "segment required"}, status_code=400)
    with _conn() as conn, conn.cursor() as cur:
        out = _cached("segresults", seg, date.today(), lambda: _segment_results(cur, seg))
    if not out["rows"]:
        return JSONResponse({"error": "unknown segment", "segment": seg}, status_code=404)
    return out


@router.get("/api/results/peers/{symbol}")
def results_peers(symbol: str):
    """cc#1192 scope 2 — the peer result block on its own, WITH the subject in it.

    The same _peer_results the R card calls, so there is no second copy of the arithmetic to drift.
    include_self appends the subject flagged is_self, which is what makes this useful on its own:
    a reader gets "you against your three best-scored peers" from one call, all four rows struck
    on the identical fundamentals_history basis.

    THE CACHE KEY CARRIES THE SYMBOL, and that is not cosmetic. _cached keys on (kind, segment,
    day). Every symbol in a segment shares a segment, so caching this under a bare kind would
    serve the FIRST caller's self-row to every other name in that segment — a card confidently
    showing another company's numbers as your own. The kind is namespaced per symbol and per
    include_self for that reason.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return JSONResponse({"error": "symbol required"}, status_code=400)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT segment FROM gvm_scores WHERE UPPER(symbol)=%s
                       AND score_date=(SELECT MAX(score_date) FROM gvm_scores) LIMIT 1""", (sym,))
        r = cur.fetchone()
        segment = r[0] if r else None
        if not segment:
            # A 404 with a reason, not an empty 200. An unsegmented name has no peer set at all,
            # and a caller must be able to tell that apart from "the segment has nobody reported".
            return JSONResponse({"error": "no segment for symbol", "symbol": sym}, status_code=404)
        out = _cached("peerres_self:%s" % sym, segment, date.today(),
                      lambda: _peer_results(cur, sym, segment, include_self=True))
    if out is None:
        return JSONResponse({"error": "no peer has reported this quarter",
                             "symbol": sym, "segment": segment}, status_code=404)
    return out


@router.get("/api/results/card")
def results_card(symbol: str, generate: bool = False, full: int = 0):
    """cc#858: returns the FAST CORE by default; ?full=1 returns the original combined payload.

    The four heavy blocks (peer_comparison, peer_results, raw_news, polished_news) moved to
    GET /api/results/card/context, which the client fetches in parallel right after the core lands.
    Everything the core returns is a single-row lookup and is already quick, so the card can paint
    immediately instead of waiting on segment-wide peer work.

    ?full=1 is kept so nothing that already calls this URL breaks — it returns the identical
    original payload. Callers checked: pwa_endpoints.py (ScorrRCard, the shared R-card builder),
    scorr_card_strip.js, scorr_result_corner.html, v8_dashboard.html. None passes `full`, so all of
    them get the fast core and then the context call; the combined shape stays available for any
    caller found later.
    """
    # cc#609: `generate` is retained for backward-compat with any cached client URL but is IGNORED —
    # app-side FY27-outlook generation is retired (dead Anthropic path removed). Cards serve the
    # cached input_raw.fy27_outlook only; when none exists the card shows a "due September" note.
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "symbol is required"}
    with _conn() as conn, conn.cursor() as cur:
        # cc#857 fix 3: the _ensure_cols() call that used to sit HERE is GONE. It fired 2x ALTER
        # TABLE input_raw on every single card load, taking an ACCESS EXCLUSIVE lock each time and
        # serialising concurrent card loads against each other and against anything else touching
        # input_raw. It now runs exactly once, at startup, via ensure_startup(). No DDL executes
        # on this request path — and, with the guard, none can.

        cur.execute("SELECT verdict, segment FROM gvm_scores WHERE symbol=%s ORDER BY score_date DESC LIMIT 1", (sym,))
        vr = cur.fetchone()
        gvm_verdict = vr[0] if vr else None
        segment = vr[1] if vr else None
        # cc#857 fix 5: both blocks depend only on (segment, date), so every symbol in a segment
        # was recomputing an identical result. Cached per segment per day.
        # NOTE the cache key deliberately EXCLUDES `sym`. Both functions self-exclude the subject
        # via `g.symbol <> %s`, so a cached block technically contains one peer the subject would
        # have excluded from its own view — checked, and it does not matter: the block is a
        # top-3-by-GVM SEGMENT summary, and the subject is not rendered inside its own peer table.
        _today = date.today()
        # cc#858: skipped entirely on the fast path. cc#857 made them fast, but "fast" is still
        # segment-wide work the header does not need in order to paint.
        _want_ctx = bool(full)
        peer_comparison = _cached("cmp", segment, _today,
                                  lambda: _peer_comparison(cur, sym, segment)) if _want_ctx else None
        peer_results = _cached("res", segment, _today,
                               lambda: _peer_results(cur, sym, segment, _today)) if _want_ctx else None

        def _with_peer(d):
            d["peer_comparison"] = peer_comparison
            d["peer_results"] = peer_results
            # cc#1191 scope 3: the segment travels on the CORE payload so the header chip can be
            # drawn on the card's first paint. It is added HERE, in the one wrapper every return
            # path in this function passes through, rather than on each of the five branches —
            # a field added to four of five branches is a chip that vanishes on the fifth, and
            # nobody would find that until a name in that state was opened.
            # Null for an unsegmented name, which scope 5 renders as no chip at all.
            d["segment"] = segment
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
        #   raw_news     : last-7-day position_news headlines for this symbol (RAW chip; cc#650)
        #   polished_news: last-30d polished_news via mentioned_symbols (symbol-section query — REPLACES
        #                  the cc#619 per-item url_hash lookup that hit ~2%)
        # Quarter labels reuse _fq_label / the card's own leading label (cc#618-B doctrine: never label a
        # quarter the data does not contain; the cc#622-A downgrade guard protects stored cards).
        expected_q = _expected_quarter(today)
        fy27 = _fy27_growth(cur, sym)
        # cc#796: reported quarter actuals, from the SAME basis that carries the latest quarter —
        # a symbol can hold both a stale consolidated series and a current standalone one (cc#793),
        # and comparing an expectation against an abandoned basis would be worse than showing nothing.
        _act_sales = _act_pat = None
        try:
            cur.execute("""SELECT metrics FROM fundamentals_history
                           WHERE UPPER(symbol)=UPPER(%s) AND section='quarters' AND period_type='quarter'
                           ORDER BY period_end DESC LIMIT 1""", (sym,))
            _m = cur.fetchone()
            if _m and _m[0]:
                _mm = _m[0]
                _act_sales = _f(str(_mm.get("Sales") or _mm.get("Revenue") or "").replace(",", ""))
                _act_pat = _f(str(_mm.get("Net Profit") or "").replace(",", ""))
        except Exception as e:
            log.warning(f"cc#796 actuals {sym}: {e}")
        # cc#1427: a Basic/CSV-only name (no fundamentals_history quarters scraped yet — the
        # result_analysis_gen.py "(limited review)" case) has NO stale-vs-current basis to worry
        # about (cc#796's concern above): there is exactly ONE basis, the CSV, or none at all. Fall
        # back to the same screener_raw absolutes result_corner.py's CSV layer already reads, so
        # vs-est (block_2) renders for these names instead of silently degrading to "omitted".
        if _act_sales is None and _act_pat is None:
            try:
                cur.execute("""SELECT sales_latest_quarter, profit_after_tax_latest_quarter
                               FROM screener_raw WHERE UPPER(nse_code)=UPPER(%s) LIMIT 1""", (sym,))
                _cr = cur.fetchone()
                if _cr:
                    _act_sales = _f(_cr[0])
                    _act_pat = _f(_cr[1])
            except Exception as e:
                log.warning(f"cc#1427 CSV actuals fallback {sym}: {e}")
        expectations = _expectations(cur, sym, _act_sales, _act_pat)
        l1 = _l1_quarter(cur, sym, segment)              # cc#797 block 1; cc#1311: segment for industry PE
        auto_verdict = _auto_verdict(l1, expectations)  # cc#797 deterministic verdict line
        # cc#1268 RESULT_DOT_RULE_V3: own-estimate dot, struck against the SAME actuals block
        # above and gated on ex_dt (the result date the snapshot must predate). Dotless when no
        # pre-result snapshot survives — see _result_dot_v3's docstring for why that is currently
        # every symbol, not a bug in this call.
        result_dot = _result_dot_v3(cur, sym, ex_dt, _act_sales, _act_pat)
        # cc#858: news also moves to the context call. cc#847 note preserved — _raw_news still
        # reads position_news and that dependency is unchanged, it simply runs on the other endpoint.
        raw_news = _raw_news(cur, sym, hours=168) if _want_ctx else None   # cc#650: 7-day window
        pol_news = _polished_by_symbol(cur, sym, days=30) if _want_ctx else None

        def _sections(base):
            base.update({"fy27_growth": fy27, "raw_news": raw_news, "polished_news": pol_news,
                         "expectations": expectations,    # cc#796: None -> card omits the line
                         "l1": l1, "auto_verdict": auto_verdict,   # cc#797
                         "result_dot": result_dot})   # cc#1268: {dot, basis, est_sales, est_pat, snapshot_date}
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


# ══ cc#784: RESULT ANALYSIS V2 — editorial long-form, generated by the "polish results" batches ═══
# Landing table result_analysis_v2 (symbol, quarter, analysis_text, sections, polished_at). Content is
# AUTHORED (POLISH_RESULTS_FRAMEWORK_V1 id=13354 + RESULT_ANALYSIS_V2_SAMPLE id=13353), never
# auto-generated here — result_analysis_gen's auto-regen is retired for THIS surface. These endpoints
# only read what a polish batch has written, so the card can never invent an analysis.

_RA2_MONTH = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "june": 6,
              "jul": 7, "july": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def normalize_doc_quarter(label):
    """cc#785 hard rule: doc_texts.quarter is free text ('jul 2026', "june' 2026", 'july 29',
    'unspecified'). Normalize to QxFYxx using the founder's own mapping in POLISH_RESULTS_FRAMEWORK_V1
    ("Q1FY27 = quarter hints jun/jul/aug 2026 or FY27") — i.e. a filing published 1-2 months after a
    quarter closes reports THAT quarter:
        jun/jul/aug Y -> Q1 FY(Y+1)   sep/oct/nov Y -> Q2 FY(Y+1)
        dec/jan/feb   -> Q3           mar/apr/may Y -> Q4 FY(Y)
    Returns None when the label carries no usable month+year — the caller must then skip rather than
    guess a quarter (a wrong quarter label is worse than no row)."""
    import re as _re
    if not label:
        return None
    s = str(label).strip().lower().replace("'", " ")
    m = _re.search(r"^(q[1-4])\s*fy\s*(\d{2})$", s.replace(" ", ""))
    if m:
        return f"{m.group(1).upper()}FY{m.group(2)}"
    mon = _re.search(r"([a-z]{3,5})", s)
    yr = _re.search(r"(20\d{2})", s)
    if not mon or not yr:
        return None
    mm = _RA2_MONTH.get(mon.group(1)[:4]) or _RA2_MONTH.get(mon.group(1)[:3])
    if not mm:
        return None
    y = int(yr.group(1))
    if mm in (6, 7, 8):
        return f"Q1FY{str(y + 1)[-2:]}"
    if mm in (9, 10, 11):
        return f"Q2FY{str(y + 1)[-2:]}"
    if mm == 12:
        return f"Q3FY{str(y + 1)[-2:]}"
    if mm in (1, 2):
        return f"Q3FY{str(y)[-2:]}"
    return f"Q4FY{str(y)[-2:]}"      # mar/apr/may -> Q4 of the FY just ended


@router.get("/api/results/v2/{symbol}")
def result_analysis_v2(symbol: str, quarter: str = ""):
    """Long-form Result Analysis for one symbol (latest polished quarter unless one is named).
    Returns has_analysis=False when no polish batch has covered it — the box then keeps the existing
    4-line metric header and simply shows no long-form body. Never fabricates."""
    sym = (symbol or "").strip().upper()
    try:
        with _conn() as conn, conn.cursor() as cur:
            if quarter:
                cur.execute("""SELECT quarter, analysis_text, sections, char_count, polished_at
                               FROM result_analysis_v2 WHERE UPPER(symbol)=%s AND quarter=%s""", (sym, quarter))
            else:
                cur.execute("""SELECT quarter, analysis_text, sections, char_count, polished_at
                               FROM result_analysis_v2 WHERE UPPER(symbol)=%s
                               ORDER BY polished_at DESC LIMIT 1""", (sym,))
            r = cur.fetchone()
        if not r:
            return {"symbol": sym, "has_analysis": False}
        return {"symbol": sym, "has_analysis": True, "quarter": r[0], "analysis": r[1],
                "sections": r[2], "char_count": r[3],
                "polished_at": r[4].isoformat() if r[4] else None,
                "basis": "Scorr editorial · sourced from filings, concall and reported financials"}
    except Exception as e:
        return {"symbol": sym, "has_analysis": False, "error": str(e)[:200]}


@router.get("/api/results/v2")
def result_analysis_v2_list(limit: int = 60, quarter: str = ""):
    """cc#784 Results navbar page: latest analysed results, newest first."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            q = """SELECT v.symbol, v.quarter, v.char_count, v.polished_at,
                          LEFT(v.analysis_text, 220) AS teaser,
                          (SELECT company_name FROM input_raw i WHERE UPPER(i.nse_code)=UPPER(v.symbol) LIMIT 1),
                          (SELECT MAX(e.ex_date) FROM earnings_calendar e
                             WHERE UPPER(e.ticker)=UPPER(v.symbol) AND e.status='reported' AND e.verified<>'false')
                   FROM result_analysis_v2 v"""
            params = []
            if quarter:
                q += " WHERE v.quarter=%s"
                params.append(quarter)
            q += " ORDER BY v.polished_at DESC LIMIT %s"
            params.append(max(1, min(limit, 200)))
            cur.execute(q, params)
            rows = [{"symbol": r[0], "quarter": r[1], "char_count": r[2],
                     "polished_at": r[3].isoformat() if r[3] else None,
                     "teaser": (r[4] or "").strip(), "company": r[5], "result_date": str(r[6]) if r[6] else None}
                    for r in cur.fetchall()]
            cur.execute("SELECT count(*) FROM result_analysis_v2")
            total = cur.fetchone()[0]
        return {"count": len(rows), "total_polished": total, "results": rows}
    except Exception as e:
        return {"count": 0, "total_polished": 0, "results": [], "error": str(e)[:200]}


@router.get("/api/results/v2/queue/status")
def result_analysis_v2_queue():
    """cc#785 MANDATORY opening report for 'polish results': the four numbers, computed live —
    Scrape Universe | Data Available (running-quarter docs stored) | Polished | Unpolished."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT count(DISTINCT symbol) FROM doc_texts
                           WHERE extract_status='stored' AND char_count>0""")
            universe = cur.fetchone()[0]
            cur.execute("""SELECT DISTINCT UPPER(symbol), quarter FROM doc_texts
                           WHERE extract_status='stored' AND char_count>0 AND quarter IS NOT NULL""")
            avail = {s for s, q in cur.fetchall() if normalize_doc_quarter(q) == "Q1FY27"}
            cur.execute("SELECT UPPER(symbol) FROM result_analysis_v2 WHERE quarter='Q1FY27'")
            done = {r[0] for r in cur.fetchall()}
        return {"scrape_universe": universe, "data_available": len(avail),
                "polished": len(done), "unpolished": len(avail - done), "quarter": "Q1FY27"}
    except Exception as e:
        return {"error": str(e)[:200]}

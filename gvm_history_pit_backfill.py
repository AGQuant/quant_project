"""
gvm_history_pit_backfill.py -- cc#2092 (P0): honest POINT-IN-TIME G/V/M history backfill.

Diagnosis this fixes (cc#2090/cc#2091/cc#2092): gvm_history's old method=backfill_step_partial rows
froze TODAY's G and V onto every past date (proof: TANLA read G 6.07 / V 7.50 identically on five
dates two years apart). cc#2091 deleted every one of those rows -- gone, not archived, so M (which
WAS genuinely honest, price-derived) is gone too and must be rebuilt here as well
(AMENDMENT_14SEP_M_SCORE_MUST_ALSO_BE_REBUILT, spec cc_tasks.id=2092).

THE_ONE_HARD_RULE (spec, verbatim): import and call gvm_engine.api_g_score / api_v_score /
api_m_score DIRECTLY. This module builds their INPUTS from real historical data and lets the
engine's own BLANK_SCORE=5.0 rule fire on None -- it never re-implements banding, peer-relative or
blank logic. No second GVM scorer.

WHERE EACH G/V/M INPUT REALLY COMES FROM (state the method, per the card's own instruction --
this list IS that statement):
  - roce, opm: DIRECTLY read from fundamentals_history's own annual figures (ratios."ROCE %",
    profit-loss."OPM %") -- the same published ratio Screener shows, just for a past year instead
    of today. No construction, no approximation.
  - sales_growth_5y/3y, profit_growth_5y/3y: gvm_nightly.py's _load_merged_df does NOT compute
    these -- they flow through untouched from screener_raw, i.e. they are SCRAPED DIRECTLY from
    Screener.in's own (undocumented, proprietary) calculation, never derived by this codebase.
    Screener has no historical archive of its own past snapshots, so there is nothing in this
    codebase to "replicate" for a past date -- this module instead computes a standard CAGR from
    fundamentals_history's own raw Sales/Net Profit figures ((val[t]/val[t-N])**(1/N)-1)*100.
    Calibrated against real current data: sales CAGR matched screener_raw's live sales_growth_5y/3y
    EXACTLY on both symbols checked (RELIANCE, TCS, both timeframes). Profit CAGR did NOT match
    screener_raw's live profit_growth figure as closely (off by 2-4 points on the same two symbols)
    -- Screener's own profit-growth methodology likely adjusts for exceptional items this module
    cannot see. Both are real, standard, honestly-computed CAGRs from trusted raw figures; profit
    growth specifically is flagged here as an approximation of a proprietary third-party figure,
    not a bug to chase further inside this already-large card.
  - qoq_sales_growth, qoq_profit_growth, opm_expansion: gvm_nightly._load_merged_df DOES compute
    these itself (the one place live logic exists to replicate) -- same formulas here, applied to
    fundamentals_history's quarterly rows instead of screener_raw's current snapshot. opm_expansion
    keeps the live formula's exact (latest_q - prior_year_q) * 100 shape, oddly large-magnitude as
    that is, because matching the live system's own construction outranks this module's opinion of
    it -- not a place to quietly "fix" a value while rebuilding history.
  - inst_holding_abs, inst_holding_change: FIIs + DIIs from fundamentals_history's quarterly
    shareholding section, same formula as gvm_nightly._load_merged_df (fillna(0) sum; change vs
    the prior quarterly shareholding row).
  - fixed_asset_growth: not a scraped field this codebase controls either -- 3-year CAGR of Fixed
    Assets from the annual balance-sheet section (standard, stated construction).
  - interest_coverage: EBIT/Interest = (Profit before tax + Interest) / Interest from the annual
    profit-loss section -- the standard textbook definition, computed because the raw components
    exist even though the ratio itself is not stored anywhere historically. BFSI segments SKIP
    (gvm_engine.score_interest_coverage's own is_bfsi branch), matching live behaviour exactly.
  - dividend_yield: not stored historically; derived as (Dividend Payout % x EPS) / point-in-time
    price x 100 from the annual profit-loss section -- a standard, stated approximation.
  - pe: Price (point-in-time, raw_prices) / EPS (annual or TTM, whichever is the as-known basis) --
    the standard definition, using a real point-in-time price so this one is NOT an approximation.
  - historical_pe: Screener's own "10-year historical PE" cannot be reconstructed as Screener
    computes it (no per-day PE archive exists anywhere). Built instead as the plain average of this
    module's OWN computed `pe` across up to 10 PRIOR annual periods (strictly before the current
    one -- no look-ahead), however many are actually available. A stated construction, not a
    reproduction of Screener's specific method.
  - segment_pe: point-in-time peer MEDIAN of this module's own computed `pe`, same peer machinery
    as every other peer_* field (see below) -- mirrors cc#506's live "segment_pe = live segment
    median pe" design exactly, just historically.
  - potential_upside: ALWAYS None here, per the spec's own explicit instruction. fy27_growth
    (input_raw) is a forward-looking analyst estimate with no historical record anywhere --
    using today's value for a past date WOULD BE the exact look-ahead bias this whole card exists
    to remove. gvm_engine.score_potential_upside's own blank rule fires (BLANK_SCORE=5.0), honestly.
  - gvm_segment, is_bfsi: today's segment (input_raw has no historical segment membership anywhere)
    -- a stated, honest simplification, explicitly sanctioned by the spec's own words ("Peer
    GROUPING uses today's gvm_segment as a stated, honest simplification since no historical
    segment membership exists anywhere").
  - M (price, dma_50, dma_200, return_1y, return_3y, return_52w_vs_index): 100% from raw_prices,
    point-in-time, nothing scraped or passed through. dma_50/200 are plain trailing simple moving
    averages of daily close (N most recent trading days as of the date, not calendar days).
    return_1y/3y are simple point-to-point returns (NOT CAGR) -- calibrated directly against a real
    momentum_scores row (RELIANCE, 13-Sep-2026): hand-computed -9.09%/-9.44% actual for 1y (small
    gap is a date-anchor rounding difference, not a formula difference) confirms simple-return, not
    CAGR (CAGR would have given a value an order of magnitude smaller). return_52w_vs_index =
    stock's own return_1y minus NIFTY50's return_1y over the same window -- confirmed the same way.
    M can only start where raw_prices' own dense daily coverage starts (varies by symbol, commonly
    ~2021 -- some symbols carry only one snapshot row per year before that); a period whose M
    inputs are not yet available honestly gets None, which gvm_engine's own blank rule neutral-
    scores -- never padded backward, per the spec's own natural-start-dates rule.

AS-KNOWN-ON-DATE: prefer a 'reported' earnings_calendar row for that symbol within a
[-30d, +150d] window of the annual period_end (a genuine announcement-date match); else
period_end + 60 days, a stated conservative lag (annual results for a March year-end typically
land in April-May; 60 days lands after nearly all of them without waiting for the rare late filer).
Every write records which basis was used.

WRITE SAFETY: gvm_history has ONE unique constraint, (symbol, score_date) -- it does not include
method. The live nightly process (method IS NULL) already owns every date from its own start
forward; overwriting one of those rows would corrupt a genuine, already-correct live score. This
module computes that boundary itself at runtime (MIN(score_date) WHERE method IS NULL) and NEVER
writes a row on or after it -- and uses ON CONFLICT DO NOTHING (not recompute_gvm's DO UPDATE) as
a second, independent guard: a collision is skipped and counted, never silently overwritten.

EXECUTION: this session has no direct database connection (no DATABASE_URL locally) and this is a
genuine bulk data job (thousands of gvm_engine calls across the top-750, each needing point-in-time
peer averaging) -- not something to run row-by-row over an MCP SQL tool. Wired the same way
v12_backtest.py's own boot self-test works: gated behind app_config['gvm_pit_backfill']='run',
triggered on the next app startup (this push's own deploy), progress/result polled back out of
app_config['gvm_pit_backfill_result'] -- an established, already-shipped pattern in this codebase,
not a new mechanism invented for this card.
"""
import os
import json
import time
import bisect
import logging
import statistics
import threading
from datetime import date, timedelta

import psycopg
from fastapi import APIRouter

from gvm_engine import api_g_score, api_v_score, api_m_score
from gvm_nightly import BFSI_SEGMENTS
from scrape_universe import universe_symbols

router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")
log = logging.getLogger("scorr.gvm_pit_backfill")

METHOD_TAG = "backfill_pit_v2"
ANNUAL_LOOKBACK_YEARS = (5, 3)   # sales/profit growth windows
FA_LOOKBACK_YEARS = 3            # fixed_asset_growth window
HIST_PE_MAX_PRIOR_PERIODS = 10
QTR_REPORT_LAG_DAYS = 30         # conservative "surely public by" buffer for quarterly figures
ANNUAL_REPORT_LAG_DAYS = 60      # fallback as-known lag when no earnings_calendar match


def _conn():
    return psycopg.connect(_DB)


# ── small parsing helpers (fundamentals_history.metrics values are Screener-format strings) ──

def _num(v):
    """'1,123,055' / '55.22' / None -> float or None. Never raises."""
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        if s in ("", "-", "NA", "N/A"):
            return None
        return float(s)
    except Exception:
        return None


def _pct(v):
    """Screener percent strings ('16%') and plain numbers both parse to the same 16.0 convention
    gvm_nightly's own screener_raw columns already use (confirmed: RELIANCE opm=16.34, not 0.1634)."""
    return _num(v)


# ── point-in-time price series: close + trailing-window DMA/return helpers ──────────────────

class _PxSeries:
    """Per-symbol sorted (dates, closes). as_of (<=) bisect; dma(n) / ret(days) both point-in-time
    (only look at data with date <= the query date -- never the still-open future)."""
    def __init__(self):
        self.map = {}

    def add(self, sym, rows):
        self.map[sym] = ([r[0] for r in rows], [r[1] for r in rows])

    def _idx(self, sym, d):
        pair = self.map.get(sym)
        if not pair or not pair[0]:
            return None
        ds, _ = pair
        i = bisect.bisect_right(ds, d) - 1
        return i if i >= 0 else None

    def as_of(self, sym, d):
        i = self._idx(sym, d)
        if i is None:
            return None
        return self.map[sym][1][i]

    def dma(self, sym, d, n):
        i = self._idx(sym, d)
        if i is None or i + 1 < n:
            return None
        cs = self.map[sym][1]
        window = cs[i + 1 - n:i + 1]
        return sum(window) / n

    def ret(self, sym, d, days):
        now = self.as_of(sym, d)
        then = self.as_of(sym, d - timedelta(days=days))
        if now is None or then in (None, 0):
            return None
        return (now / then - 1.0) * 100.0


def _load_price_series(cur, symbols):
    s = _PxSeries()
    if not symbols:
        return s
    cur.execute("""SELECT symbol, price_date, close FROM raw_prices
                   WHERE symbol = ANY(%s) AND close IS NOT NULL
                   ORDER BY symbol, price_date""", (list(symbols) + ["NIFTY50"],))
    cur_sym, buf = None, []
    for sym, d, c in cur.fetchall():
        if sym != cur_sym:
            if cur_sym is not None:
                s.add(cur_sym, buf)
            cur_sym, buf = sym, []
        buf.append((d, float(c)))
    if cur_sym is not None:
        s.add(cur_sym, buf)
    return s


# ── bulk fundamentals load ────────────────────────────────────────────────────────────────────

def _load_fundamentals(cur, symbols):
    """symbol -> section -> sorted list of (period_end, metrics dict). Annual TTM rows
    (period_end IS NULL) are excluded -- TTM is "as of today", not a fixed historical point."""
    if not symbols:
        return {}
    cur.execute("""SELECT symbol, section, period_end, metrics FROM fundamentals_history
                   WHERE symbol = ANY(%s) AND period_end IS NOT NULL
                   ORDER BY symbol, section, period_end""", (list(symbols),))
    out = {}
    for sym, section, pend, metrics in cur.fetchall():
        out.setdefault(sym, {}).setdefault(section, []).append((pend, metrics))
    return out


def _load_earnings_dates(cur, symbols):
    """symbol -> sorted list of reported/verified-or-estimated announcement dates. Sparse (this
    table is a recent lead/tracking feed, not a historical archive) -- most annual periods will
    fall through to the stated +60d lag, by design, not by bug."""
    if not symbols:
        return {}
    cur.execute("""SELECT ticker, ex_date FROM earnings_calendar
                   WHERE ticker = ANY(%s) AND status = 'reported' AND ex_date IS NOT NULL
                   ORDER BY ticker, ex_date""", (list(symbols),))
    out = {}
    for tkr, exd in cur.fetchall():
        out.setdefault(tkr, []).append(exd)
    return out


def _as_known_date(period_end, earnings_dates):
    """Prefer a real reported earnings_calendar date within [-30d, +150d] of period_end; else
    period_end + 60 days. Returns (date, basis_str)."""
    if earnings_dates:
        lo, hi = period_end - timedelta(days=30), period_end + timedelta(days=150)
        cands = [d for d in earnings_dates if lo <= d <= hi]
        if cands:
            return min(cands, key=lambda d: abs((d - period_end).days)), "earnings_calendar"
    return period_end + timedelta(days=ANNUAL_REPORT_LAG_DAYS), "period_end+60d"


def _series_lookup(rows, period_end):
    """rows: sorted [(period_end, metrics)]. Exact period_end match (annual figures are keyed by
    fiscal year-end, not a range) -> metrics dict, or {} if absent."""
    for pe, m in rows:
        if pe == period_end:
            return m or {}
    return {}


def _nearest_annual_on_or_before(rows, target_end, years_back):
    """rows: sorted [(period_end, metrics)]. The annual row whose period_end is (years_back years
    before target_end), tolerating +/-45 days for fiscal-year-end drift. None if absent."""
    want = date(target_end.year - years_back, target_end.month, 1)
    best, best_gap = None, None
    for pe, m in rows:
        gap = abs((pe - want).days)
        if gap <= 45 and (best_gap is None or gap < best_gap):
            best, best_gap = m, gap
    return best


def _latest_quarter_on_or_before(rows, cutoff):
    """rows: sorted [(period_end, metrics)] for section='quarters'. Latest quarter whose
    period_end <= cutoff (already lag-adjusted by the caller)."""
    out = None
    for pe, m in rows:
        if pe <= cutoff:
            out = m
        else:
            break
    return out


def _quarter_same_q_prior_year(rows, q_period_end):
    """The quarter exactly one year before q_period_end (+/-20 days for month-end drift)."""
    if q_period_end is None:
        return None
    want = date(q_period_end.year - 1, q_period_end.month, 1)
    for pe, m in rows:
        if abs((pe - want).days) <= 20:
            return m
    return None


# ── per-symbol raw G+V extraction for one annual period ──────────────────────────────────────

def _cagr_pct(now, then, years):
    """(now/then)**(1/years) is only real-valued when the ratio is >= 0 -- a negative ratio (the
    NOW figure went negative, e.g. a loss year's Net Profit) raises it to a fractional power and
    Python returns a COMPLEX number, not an exception. That complex value would silently flow into
    gvm_engine and, worse, crash statistics.median() the moment it is sorted against a real float
    from another peer ('<' not supported between complex and float) -- caught for real on the
    first live run (14-Sep-2026), not by reasoning alone. A sign flip has no honest real-valued
    CAGR here, so it returns None -- the engine's own blank rule scores it neutrally, which is
    correct: a growth rate through a loss year is not a number this formula can honestly produce."""
    if now is None or then is None or then <= 0:
        return None
    ratio = now / then
    if ratio < 0:
        return None
    return (ratio ** (1.0 / years) - 1.0) * 100.0


def _extract_gv_raw(fh_sym, period_end, as_known_date, px, symbol):
    """Every G+V input except peer_*/segment/is_bfsi (added by the caller once peers are known).
    Missing/uncomputable -> None, honestly (gvm_engine's own BLANK_SCORE fires on None)."""
    pl_rows = fh_sym.get("profit-loss", [])
    bs_rows = fh_sym.get("balance-sheet", [])
    ra_rows = fh_sym.get("ratios", [])
    sh_rows = fh_sym.get("shareholding", [])
    qt_rows = fh_sym.get("quarters", [])

    pl = _series_lookup(pl_rows, period_end)
    bs = _series_lookup(bs_rows, period_end)
    ra = _series_lookup(ra_rows, period_end)

    sales_now, np_now, eps_now = _num(pl.get("Sales")), _num(pl.get("Net Profit")), _num(pl.get("EPS in Rs"))
    interest_now, pbt_now = _num(pl.get("Interest")), _num(pl.get("Profit before tax"))
    payout_now = _num(pl.get("Dividend Payout %"))
    fa_now = _num(bs.get("Fixed Assets"))
    roce = _num(ra.get("ROCE %"))
    opm = _pct(pl.get("OPM %"))

    sales5 = _nearest_annual_on_or_before(pl_rows, period_end, 5)
    sales3 = _nearest_annual_on_or_before(pl_rows, period_end, 3)
    fa3 = _nearest_annual_on_or_before(bs_rows, period_end, FA_LOOKBACK_YEARS)
    sales_growth_5y = _cagr_pct(sales_now, _num((sales5 or {}).get("Sales")), 5)
    sales_growth_3y = _cagr_pct(sales_now, _num((sales3 or {}).get("Sales")), 3)
    np5, np3 = _num((sales5 or {}).get("Net Profit")), _num((sales3 or {}).get("Net Profit"))
    profit_growth_5y = _cagr_pct(np_now, np5, 5) if (np5 or 0) > 0 else None
    profit_growth_3y = _cagr_pct(np_now, np3, 3) if (np3 or 0) > 0 else None
    fixed_asset_growth = _cagr_pct(fa_now, _num((fa3 or {}).get("Fixed Assets")), FA_LOOKBACK_YEARS)

    # Standard textbook definition: EBIT / Interest = (Profit before tax + Interest) / Interest.
    interest_coverage = None
    if interest_now and interest_now != 0 and pbt_now is not None:
        interest_coverage = (pbt_now + interest_now) / interest_now

    # quarterly-derived: latest quarter known by (as_known_date - lag), vs the same quarter a year
    # earlier. A conservative extra lag (on top of the annual as-known date already being used as
    # the outer cutoff) since quarterly results usually land faster than annual ones.
    q_cutoff = as_known_date - timedelta(days=QTR_REPORT_LAG_DAYS)
    q_now = _latest_quarter_on_or_before(qt_rows, q_cutoff)
    qoq_sales_growth = qoq_profit_growth = opm_expansion = None
    q_now_pend = None
    if q_now is not None:
        for pe, m in qt_rows:
            if m is q_now:
                q_now_pend = pe
                break
        q_prior = _quarter_same_q_prior_year(qt_rows, q_now_pend)
        if q_prior is not None:
            s_now, s_prior = _num(q_now.get("Sales")), _num(q_prior.get("Sales"))
            if s_prior not in (None, 0):
                qoq_sales_growth = (s_now / s_prior - 1.0) * 100.0 if s_now is not None else None
            p_now, p_prior = _num(q_now.get("Net Profit")), _num(q_prior.get("Net Profit"))
            if p_prior is not None and p_prior > 0 and p_now is not None:
                qoq_profit_growth = (p_now / p_prior - 1.0) * 100.0
            opm_now_q, opm_prior_q = _pct(q_now.get("OPM %")), _pct(q_prior.get("OPM %"))
            if opm_now_q is not None and opm_prior_q is not None:
                # cc#2092: replicate gvm_nightly._load_merged_df's live formula EXACTLY (including
                # its x100 shape) -- matching the live construction outranks this module's opinion
                # of it. Not a place to quietly "fix" a value while rebuilding history.
                opm_expansion = (opm_now_q - opm_prior_q) * 100.0

    # shareholding (quarterly): inst holding as-of, and change vs the prior shareholding quarter.
    inst_holding_abs = inst_holding_change = None
    sh_asof, sh_prior = None, None
    for pe, m in sh_rows:
        if pe <= as_known_date:
            sh_prior = sh_asof
            sh_asof = m
        else:
            break
    if sh_asof is not None:
        fii, dii = _pct(sh_asof.get("FIIs")), _pct(sh_asof.get("DIIs"))
        if fii is not None or dii is not None:
            inst_holding_abs = (fii or 0) + (dii or 0)
        if sh_prior is not None:
            fii_p, dii_p = _pct(sh_prior.get("FIIs")), _pct(sh_prior.get("DIIs"))
            if (fii is not None or dii is not None) and (fii_p is not None or dii_p is not None):
                inst_holding_change = ((fii or 0) + (dii or 0)) - ((fii_p or 0) + (dii_p or 0))

    # PE: point-in-time price / EPS (annual EPS as the trailing basis for this period).
    price_now = px.as_of(symbol, as_known_date)
    pe_val = (price_now / eps_now) if (price_now and eps_now and eps_now > 0) else None

    # dividend_yield: (Payout% x EPS) / price x 100 -- a stated approximation (no per-share
    # dividend amount stored anywhere historically).
    dividend_yield = None
    if payout_now is not None and eps_now is not None and price_now:
        div_per_share = (payout_now / 100.0) * eps_now
        if div_per_share >= 0:
            dividend_yield = (div_per_share / price_now) * 100.0

    return {
        "sales_growth_5y": sales_growth_5y, "sales_growth_3y": sales_growth_3y,
        "profit_growth_5y": profit_growth_5y, "profit_growth_3y": profit_growth_3y,
        "qoq_sales_growth": qoq_sales_growth, "qoq_profit_growth": qoq_profit_growth,
        "opm": opm, "opm_expansion": opm_expansion,
        "fixed_asset_growth": fixed_asset_growth,
        "inst_holding_abs": inst_holding_abs, "inst_holding_change": inst_holding_change,
        "roce": roce, "interest_coverage": interest_coverage,
        "dividend_yield": dividend_yield, "pe": pe_val,
        "potential_upside": None,   # cc#2092 explicit instruction -- fy27_growth has no history
    }


def _extract_m_raw(symbol, as_known_date, px):
    """price/dma/return_1y/return_3y point-in-time from raw_prices, plus return_52w_vs_index
    (stock's own return_1y minus NIFTY50's return_1y over the same window)."""
    ret_1y = px.ret(symbol, as_known_date, 365)
    idx_ret_1y = px.ret("NIFTY50", as_known_date, 365)
    return {
        "price": px.as_of(symbol, as_known_date),
        "dma_50": px.dma(symbol, as_known_date, 50),
        "dma_200": px.dma(symbol, as_known_date, 200),
        "return_1y": ret_1y,
        "return_3y": px.ret(symbol, as_known_date, 365 * 3),
        "return_52w_vs_index": (ret_1y - idx_ret_1y) if (ret_1y is not None and idx_ret_1y is not None) else None,
    }


def _median(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 4) if v else None


def run_backfill():
    """The full point-in-time backfill. Writes progress + a final result summary to app_config
    (gated trigger, see module docstring) since this is a long bulk job run server-side, not
    something polled synchronously over a request."""
    t0 = time.time()
    result = {"status": "running", "started_at": str(date.today())}
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO app_config(key,value,updated_at) VALUES('gvm_pit_backfill_result',%s,NOW()) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                    (json.dumps(result),))
        conn.commit()

        universe = sorted(universe_symbols(cur))
        cur.execute("SELECT MIN(score_date) FROM gvm_history WHERE method IS NULL")
        row = cur.fetchone()
        live_boundary = row[0] if row and row[0] else date.today()

        cur.execute("SELECT nse_code, gvm_segment FROM input_raw WHERE nse_code = ANY(%s)", (universe,))
        segment_of = {r[0]: (r[1] or "Unknown").strip() or "Unknown" for r in cur.fetchall()}
        for s in universe:
            segment_of.setdefault(s, "Unknown")

        fh = _load_fundamentals(cur, universe)
        earn = _load_earnings_dates(cur, universe)
        px = _load_price_series(cur, universe)

    # pass 1: every symbol's own annual grid -> ONE merged raw G+V+M dict per as_known_date (no
    # peers yet -- M is included here, not computed separately later, so peer lookups for
    # return_1y/return_3y/return_52w_vs_index can bisect the SAME per-symbol series as every G/V
    # peer field, via the one _peer_val_as_of helper below).
    by_symbol = {}          # symbol -> sorted [(as_known_date, period_end, raw_dict, basis)]
    skipped_live_boundary = 0
    for sym in universe:
        fh_sym = fh.get(sym, {})
        pl_rows = fh_sym.get("profit-loss", [])
        if not pl_rows:
            continue
        earn_dates = earn.get(sym, [])
        entries = []
        for pend, _m in pl_rows:
            akd, basis = _as_known_date(pend, earn_dates)
            if akd >= live_boundary:
                skipped_live_boundary += 1
                continue
            raw = _extract_gv_raw(fh_sym, pend, akd, px, sym)
            raw.update(_extract_m_raw(sym, akd, px))
            entries.append((akd, pend, raw, basis))
        entries.sort(key=lambda t: t[0])
        if entries:
            by_symbol[sym] = entries

    # pass 2: historical_pe (avg of own PE over up to 10 STRICTLY PRIOR periods -- no look-ahead)
    for sym, entries in by_symbol.items():
        for i, (akd, pend, raw, basis) in enumerate(entries):
            prior_pes = [e[2]["pe"] for e in entries[max(0, i - HIST_PE_MAX_PRIOR_PERIODS):i] if e[2]["pe"]]
            raw["historical_pe"] = (sum(prior_pes) / len(prior_pes)) if prior_pes else None

    symbols_by_segment = {}
    for sym in by_symbol:
        symbols_by_segment.setdefault(segment_of.get(sym, "Unknown"), []).append(sym)

    def _peer_val_as_of(peer, d, key):
        ents = by_symbol.get(peer)
        if not ents:
            return None
        i = bisect.bisect_right([e[0] for e in ents], d) - 1
        return ents[i][2].get(key) if i >= 0 else None

    # every peer_* field gvm_engine.py's api_g_score/api_v_score/api_m_score actually reads.
    # inst_holding_abs deliberately excluded -- score_inst_holding_abs takes only the stock's own
    # value, gvm_engine never reads a peer for it. potential_upside deliberately excluded -- always
    # None (see module docstring), its peer is therefore always None too, no need to compute it.
    peer_keys = ["sales_growth_5y", "sales_growth_3y", "profit_growth_5y", "profit_growth_3y",
                 "qoq_sales_growth", "qoq_profit_growth", "opm", "opm_expansion",
                 "fixed_asset_growth", "inst_holding_change", "roce", "interest_coverage",
                 "dividend_yield", "pe", "return_1y", "return_3y", "return_52w_vs_index"]

    write_rows = []          # (symbol, score_date, g, v, m, gvm, verdict, segment)
    per_symbol_report = {}
    blank5_total = blank5_by_symbol = 0
    n_rows = 0

    for sym, entries in by_symbol.items():
        seg = segment_of.get(sym, "Unknown")
        peers = [p for p in symbols_by_segment.get(seg, []) if p != sym]
        is_bfsi = seg in BFSI_SEGMENTS
        first_date = last_date = None
        bases_used = set()
        sym_blank5 = 0

        for akd, pend, raw, basis in entries:
            bases_used.add(basis)
            peer_avg = {k: _median([_peer_val_as_of(p, akd, k) for p in peers]) for k in peer_keys}

            sd = {
                "name": sym, "is_bfsi": is_bfsi,
                "sales_growth_5y": raw["sales_growth_5y"], "peer_sales_growth_5y": peer_avg["sales_growth_5y"],
                "sales_growth_3y": raw["sales_growth_3y"], "peer_sales_growth_3y": peer_avg["sales_growth_3y"],
                "profit_growth_5y": raw["profit_growth_5y"], "peer_profit_growth_5y": peer_avg["profit_growth_5y"],
                "profit_growth_3y": raw["profit_growth_3y"], "peer_profit_growth_3y": peer_avg["profit_growth_3y"],
                "qoq_sales_growth": raw["qoq_sales_growth"], "peer_qoq_sales_growth": peer_avg["qoq_sales_growth"],
                "qoq_profit_growth": raw["qoq_profit_growth"], "peer_qoq_profit_growth": peer_avg["qoq_profit_growth"],
                "opm": raw["opm"], "peer_opm": peer_avg["opm"],
                "opm_expansion": raw["opm_expansion"], "peer_opm_expansion": peer_avg["opm_expansion"],
                "fixed_asset_growth": raw["fixed_asset_growth"], "peer_fixed_asset_growth": peer_avg["fixed_asset_growth"],
                "inst_holding_abs": raw["inst_holding_abs"],
                "inst_holding_change": raw["inst_holding_change"], "peer_inst_holding_change": peer_avg["inst_holding_change"],
                "roce": raw["roce"], "peer_roce": peer_avg["roce"],
                "interest_coverage": raw["interest_coverage"], "peer_interest_coverage": peer_avg["interest_coverage"],
                "dividend_yield": raw["dividend_yield"], "peer_dividend_yield": peer_avg["dividend_yield"],
                "pe": raw["pe"], "historical_pe": raw["historical_pe"], "segment_pe": peer_avg["pe"],
                "potential_upside": None, "peer_potential_upside": None,
                "price": raw["price"] or 0,
                "return_1y": raw["return_1y"], "peer_return_1y": peer_avg["return_1y"],
                "return_3y": raw["return_3y"], "peer_return_3y": peer_avg["return_3y"],
                "dma_50": raw["dma_50"], "dma_200": raw["dma_200"],
                "return_52w_vs_index": raw["return_52w_vs_index"],
                "peer_return_52w_vs_index": peer_avg["return_52w_vs_index"],
            }

            gres, vres, mres = api_g_score(sd), api_v_score(sd), api_m_score(sd)
            g, v, m = gres["score"], vres["score"], mres["score"]
            gvm = round((g + v + m) / 3, 2)
            verdict = "Excellent" if gvm >= 8 else "Good" if gvm >= 7 else "Average" if gvm >= 6 else "Weak"

            for bd in (gres["breakdown"], vres["breakdown"], mres["breakdown"]):
                for score_val in bd.values():
                    if score_val == 5.0:
                        blank5_total += 1
                        sym_blank5 += 1

            write_rows.append((sym, akd, g, v, m, gvm, verdict, seg))
            n_rows += 1
            first_date = first_date or akd
            last_date = akd

        per_symbol_report[sym] = {"first_date": str(first_date), "last_date": str(last_date),
                                   "n_periods": len(entries), "basis_used": sorted(bases_used),
                                   "blank5_count": sym_blank5}
        blank5_by_symbol += 1 if sym_blank5 else 0

    with _conn() as conn, conn.cursor() as cur:
        written = skipped_conflict = 0
        for r in write_rows:
            cur.execute("""INSERT INTO gvm_history (symbol, score_date, g_score, v_score, m_score,
                           gvm_score, verdict, segment, method)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (symbol, score_date) DO NOTHING""",
                        (*r, METHOD_TAG))
            if cur.rowcount:
                written += 1
            else:
                skipped_conflict += 1
        conn.commit()

        worst20 = sorted(per_symbol_report.items(), key=lambda kv: -kv[1]["blank5_count"])[:20]
        result = {
            "status": "done",
            "runtime_s": round(time.time() - t0, 1),
            "universe_size": len(universe),
            "symbols_with_rows": len(by_symbol),
            "symbols_without_fundamentals": len(universe) - len(by_symbol),
            "rows_written": written,
            "rows_skipped_conflict": skipped_conflict,
            "rows_skipped_live_boundary": skipped_live_boundary,
            "live_boundary_date": str(live_boundary),
            "blank5_fallback_total": blank5_total,
            "symbols_with_any_blank5": blank5_by_symbol,
            "worst20_blank5": [{"symbol": s, **v} for s, v in worst20],
            "method": METHOD_TAG,
        }
        cur.execute("INSERT INTO app_config(key,value,updated_at) VALUES('gvm_pit_backfill_result',%s,NOW()) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                    (json.dumps(result, default=str)[:100000],))
        cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key='gvm_pit_backfill'")
        conn.commit()
    return result


def _run_backfill_bg():
    try:
        run_backfill()
    except Exception as e:
        log.exception("gvm_pit_backfill failed")
        try:
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO app_config(key,value,updated_at) VALUES('gvm_pit_backfill_result',%s,NOW()) "
                            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                            (json.dumps({"status": "error", "error": str(e)[:2000]}),))
                cur.execute("UPDATE app_config SET value='error', updated_at=NOW() WHERE key='gvm_pit_backfill'")
                conn.commit()
        except Exception:
            pass


@router.on_event("startup")
def _gvm_pit_backfill_trigger():
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='gvm_pit_backfill'")
            row = cur.fetchone()
        if row and str(row[0]).strip() == "run":
            threading.Thread(target=_run_backfill_bg, name="gvm-pit-backfill", daemon=True).start()
    except Exception:
        pass

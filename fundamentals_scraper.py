"""
fundamentals_scraper.py — cc#361 GVM_HISTORY_BACKFILL_V1, PHASE 1 (SCRAPE) ONLY.
================================================================================
Founder-ordered one-time scrape of Screener.in LOGGED-OUT PUBLIC company pages to reconstruct
5yr point-in-time fundamentals for the whole ~1,766-stock universe. Raw capture only — G/V field
selection + backward-GVM compute (Phases 2-4) are GATED and live elsewhere; this file NEVER computes
GVM and NEVER writes gvm_scores/gvm_history.

DESIGN (per spec id=361):
  * Public pages only (www.screener.in/company/<CODE>/[consolidated/]) — never any account session.
  * Consolidated preferred when the page carries consolidated financials, else standalone; flag stored.
  * Store EVERYTHING raw: one row per (symbol, section, period) with a jsonb of every metric Screener
    exposes in that table — quarters / profit-loss / balance-sheet / cash-flow / ratios. Field
    selection happens at compute time (tomorrow), so nothing is dropped now.
  * Throttle 2-3 s/page, exponential backoff on 429/403, checkpoint-resume per symbol (a re-run skips
    already-ok symbols; writes are idempotent upserts so repeats are harmless).
  * Runs as a background daemon ON RAILWAY (boot-flag trigger / admin route) — NOT inside a CC session.

Trigger (app_config key 'fundamentals_scrape'):
    'test'    -> scrape only the 3 founder spot-check symbols (RELIANCE, KPITTECH, HDFCBANK), then stop
                 (lets the parser be verified before the full run).
    'run'/'pending' -> full universe (screener_raw.nse_code), resumable in ~590-symbol stages.
Status: GET /api/admin/fundamentals_scrape_status.  Manual kick: POST /api/admin/run_fundamentals_scrape.

DEPTH NOTE (verified 11-Jul on RELIANCE/HDFCBANK/KPITTECH test scrape): logged-out public pages
expose ~13 trailing quarters + ~12 annual years — NOT the ~40 quarters the spec anticipated. Annual
history (12y) is ample for the annual-stepped G/V components; quarterly QoQ inputs are capped at
~3.25y. Phase 2 compute should lean on annual G/V + daily-M and treat QoQ as short-history.
"""

import logging
import os
import time
from datetime import datetime, date, timedelta

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException
from typing import Optional

log = logging.getLogger("scorr.fundamentals_scraper")
router = APIRouter(prefix="/api/admin", tags=["fundamentals_scraper"])
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

BASE = "https://www.screener.in/company/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0 Safari/537.36")
THROTTLE = 2.5          # seconds between company pages (spec: 2-3 s)
STAGE_SIZE = 590        # ~1766 / 3 -> per-stage summary boundary
SPOT_CHECK = ["RELIANCE", "KPITTECH", "HDFCBANK"]
SCRAPE_FLAG = "fundamentals_scrape"
SCRAPE_1B_FLAG = "fundamentals_scrape_1b"   # cc#391 shareholding phase

# cc#741: scrape-universe enforcement — every fundamentals/shareholding fetch is hard-gated at the
# single network choke point (_fetch_soup) so no enqueue/retry path can scrape beyond the universe.
from scrape_universe import in_scrape_universe, log_universe_skip, universe_symbols

# screener section id -> period_type stored in fundamentals_history
SECTIONS = {"quarters": "quarter", "profit-loss": "annual", "balance-sheet": "annual",
            "cash-flow": "annual", "ratios": "annual"}
_MONTHS = {"jan": 31, "feb": 28, "mar": 31, "apr": 30, "may": 31, "jun": 30,
           "jul": 31, "aug": 31, "sep": 30, "oct": 31, "nov": 30, "dec": 31}


def _check_admin(token):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


# ── schema (idempotent) ──────────────────────────────────────────────────────────

def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fundamentals_history (
                id            BIGSERIAL PRIMARY KEY,
                symbol        TEXT NOT NULL,
                consolidated  BOOLEAN,
                section       TEXT NOT NULL,
                period_type   TEXT,
                period_label  TEXT NOT NULL,
                period_end    DATE,
                metrics       JSONB,
                scraped_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (symbol, section, period_label, consolidated)
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_fh_symbol ON fundamentals_history(symbol)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fundamentals_scrape_status (
                symbol       TEXT PRIMARY KEY,
                status       TEXT,          -- ok / failed / skipped
                rows_written INTEGER,
                consolidated BOOLEAN,
                error        TEXT,
                scraped_at   TIMESTAMPTZ DEFAULT NOW()
            )""")
        # cc#391: independent Phase-1B (shareholding) progress on the SAME status table
        cur.execute("ALTER TABLE fundamentals_scrape_status ADD COLUMN IF NOT EXISTS phase1b_status TEXT")
        cur.execute("ALTER TABLE fundamentals_scrape_status ADD COLUMN IF NOT EXISTS phase1b_rows INTEGER")
        cur.execute("ALTER TABLE fundamentals_scrape_status ADD COLUMN IF NOT EXISTS phase1b_scraped_at TIMESTAMPTZ")
    conn.commit()


# ── parsing ──────────────────────────────────────────────────────────────────────

def _period_end(label):
    """'Mar 2024' -> 2024-03-31; 'TTM'/unknown -> None."""
    try:
        parts = label.strip().split()
        if len(parts) != 2:
            return None
        mon = parts[0][:3].lower()
        yr = int(parts[1])
        if mon not in _MONTHS:
            return None
        day = 29 if (mon == "feb" and yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0)) else _MONTHS[mon]
        return date(yr, list(_MONTHS).index(mon) + 1, day)
    except Exception:
        return None


def _parse_section(soup, section_id, ptype):
    """Extract one Screener data-table into per-period metric dicts. Returns list of period rows."""
    sec = soup.find("section", id=section_id)
    if not sec:
        return []
    table = sec.find("table", class_="data-table")
    if not table or not table.find("thead") or not table.find("tbody"):
        return []
    heads = [th.get_text(strip=True) for th in table.find("thead").find_all("th")]
    periods = heads[1:]                       # first header cell is the row-label column (empty)
    if not periods:
        return []
    per = {p: {} for p in periods}
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = tds[0].get_text(" ", strip=True).rstrip("+").strip()
        if not label:
            continue
        for i, td in enumerate(tds[1:]):
            if i >= len(periods):
                break
            val = td.get_text(strip=True)
            if val != "":
                per[periods[i]][label] = val
    rows = []
    for p, metrics in per.items():
        if metrics:
            rows.append({"section": section_id, "period_type": ptype, "period_label": p,
                         "period_end": _period_end(p), "metrics": metrics})
    return rows


def _fetch(url):
    """GET with browser UA + exponential backoff on 429/403. Returns Response or None."""
    delay = 5
    for attempt in range(4):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                             timeout=25)
        except Exception as e:
            log.warning(f"fetch {url} attempt {attempt}: {e}")
            time.sleep(delay); delay *= 2; continue
        if r.status_code in (429, 403):
            log.warning(f"fetch {url} -> {r.status_code}, backoff {delay}s")
            time.sleep(delay); delay *= 2; continue
        return r
    return None


def _fetch_soup(symbol):
    """Fetch a company page (consolidated preferred, else standalone) and return (soup, cons) or
    (None, None). A valid page carries a profit-loss data-table.

    cc#741: HARD scrape-universe gate at the SINGLE network choke point. fetch_company,
    fetch_shareholding and the cc#694 single-visit path all fetch through here, so an out-of-universe
    symbol can NEVER hit the network regardless of which enqueue/retry path reached it (this is what
    plugs the daily 8-19-symbol T+1 leak — the earlier per-path gates logged skips but some path still
    fetched). Out-of-universe -> (None, None), audit-logged once via log_universe_skip. Fail-OPEN on a
    DB error (proceed to fetch) so a transient hiccup never blocks a legitimate universe fetch."""
    sym = symbol.strip().upper()
    try:
        import fyers_feed
        _c = fyers_feed.get_db()
        try:
            with _c.cursor() as _cur:
                if not in_scrape_universe(_cur, sym):
                    log_universe_skip(_cur, sym, None, "fundamentals_scraper._fetch_soup")
                    _c.commit()
                    return None, None
        finally:
            _c.close()
    except Exception as _ge:
        log.warning(f"cc#741 universe gate check failed for {sym} (fail-open): {_ge}")
    # cc#793 STANDALONE FALLBACK. The old test was "does this page have a profit-loss section",
    # which the consolidated view passes even when it is a FROZEN RELIC. Colgate stopped consolidated
    # reporting in 2010, so screener.in/company/COLPAL/consolidated/ still serves a real page — with
    # annuals ending Mar 2010 and an EMPTY quarterly table — and the old check accepted it and never
    # looked at standalone, where the current data actually lives. Confirmed live by Claude web; the
    # parser was correct all along, it was being handed the wrong view. Same class: BAYERCROP (2006),
    # TATAELXSI (2015), 3MINDIA (2024).
    #
    # The view is now chosen on QUALITY, not mere existence: a view carrying quarterly rows beats one
    # that does not, and among equals the fresher annual history wins. The remembered preference is
    # tried first so a symbol already known to need standalone costs ONE fetch, not two.
    order = [(True, f"{BASE}{sym}/consolidated/"), (False, f"{BASE}{sym}/")]
    pref = _get_view_pref(sym)
    if pref == "standalone":
        order.reverse()
    # "Has SOME quarterly rows" is not good enough as a stop condition. BAYERCROP's consolidated view
    # carries exactly 2 quarter rows, both from 2006, and TATAELXSI's stops at 2015 — an abandoned
    # view can be stale without being empty. So a view is only taken WITHOUT comparison when it is
    # actually FRESH; otherwise both views are fetched and the one with the newer latest quarter wins.
    _fresh_cut = str(_last_quarter_end(date.today()) - timedelta(days=200))
    cands = []
    for cons, path in order:
        r = _fetch(path)
        if r is None or r.status_code != 200 or "data-table" not in r.text:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        if not soup.find("section", id="profit-loss"):
            continue
        q = _page_quality(soup)
        cands.append((soup, cons, q))
        if q["newest_quarter"] and q["newest_quarter"] >= _fresh_cut:
            break        # fresh view — no need to spend a second fetch
    if not cands:
        return None, None
    # newest quarter is the decisive signal; fall back to newest annual, then to row count
    best = max(cands, key=lambda c: (c[2]["newest_quarter"], c[2]["newest_annual"], c[2]["quarter_rows"]))
    soup, cons, q = best
    if len(cands) > 1 or pref:
        want = "consolidated" if cons else "standalone"
        if want != (pref or "consolidated"):
            _set_view_pref(sym, want, f"chosen on freshness: latest quarter "
                                      f"{q['newest_quarter'] or 'none'}, annual {q['newest_annual'] or 'none'} (cc#793)")
    if not q["newest_quarter"]:
        log.warning(f"cc#793 {sym}: no view carried quarterly rows (using "
                    f"{'consolidated' if cons else 'standalone'})")
    return soup, cons


def _page_quality(soup):
    """cc#793: how usable is this view? quarter_rows is the decisive signal — a view whose quarterly
    table is empty cannot produce a running quarter no matter how well it parses."""
    q = _parse_section(soup, "quarters", "quarter")
    ann = _parse_section(soup, "profit-loss", "annual")
    def _newest(rows):
        out = ""
        for r in rows:
            pe = r.get("period_end")
            if pe and str(pe) > out:
                out = str(pe)
        return out
    return {"quarter_rows": len(q), "annual_rows": len(ann),
            "newest_quarter": _newest(q), "newest_annual": _newest(ann)}


def _ensure_view_pref_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS fundamentals_view_pref (
        symbol text PRIMARY KEY, view text NOT NULL, reason text,
        decided_at timestamptz DEFAULT now())""")


def _get_view_pref(symbol):
    """Remembered per-symbol view ('consolidated' | 'standalone'), or None."""
    try:
        import fyers_feed
        c = fyers_feed.get_db()
        try:
            with c.cursor() as cur:
                _ensure_view_pref_table(cur)
                cur.execute("SELECT view FROM fundamentals_view_pref WHERE symbol=%s", (symbol,))
                r = cur.fetchone()
            c.commit()
            return r[0] if r else None
        finally:
            c.close()
    except Exception as e:
        log.warning(f"_get_view_pref {symbol}: {e}")
        return None


def _set_view_pref(symbol, view, reason=""):
    try:
        import fyers_feed
        c = fyers_feed.get_db()
        try:
            with c.cursor() as cur:
                _ensure_view_pref_table(cur)
                cur.execute("""INSERT INTO fundamentals_view_pref (symbol, view, reason, decided_at)
                               VALUES (%s,%s,%s,NOW())
                               ON CONFLICT (symbol) DO UPDATE SET view=EXCLUDED.view,
                                 reason=EXCLUDED.reason, decided_at=NOW()""", (symbol, view, reason[:400]))
            c.commit()
            log.info(f"cc#793 {symbol}: fetch view -> {view} ({reason})")
        finally:
            c.close()
    except Exception as e:
        log.warning(f"_set_view_pref {symbol}: {e}")


def fetch_company(symbol):
    """Financials sections (quarters/profit-loss/balance-sheet/cash-flow/ratios).
    Returns (rows, consolidated_flag) or ([], None)."""
    soup, cons = _fetch_soup(symbol)
    if soup is None:
        return [], None
    rows = []
    for sid, ptype in SECTIONS.items():
        rows.extend(_parse_section(soup, sid, ptype))
    return (rows, cons) if rows else ([], None)


# cc#694: all 6 sections (5 financials + shareholding) — parsed from ONE already-fetched soup so the T+1
# single-visit path needs just one company-page fetch instead of one per section-group.
ALL_SECTIONS = dict(SECTIONS)
ALL_SECTIONS["shareholding"] = "quarter"


def parse_all_sections(soup):
    """cc#694: parse every section (quarters/profit-loss/balance-sheet/cash-flow/ratios/shareholding)
    from a pre-fetched soup. Returns the combined row list (same shape as fetch_company's rows)."""
    rows = []
    for sid, ptype in ALL_SECTIONS.items():
        rows.extend(_parse_section(soup, sid, ptype))
    return rows


def fetch_shareholding(symbol):
    """cc#391 Phase 1B: the shareholding quarterly table only (Promoters/FIIs/DIIs/Government/Public/
    No. of Shareholders per quarter). Returns (rows, consolidated_flag, page_ok). cc#597: page_ok
    distinguishes a fetch/page failure (False -> status 'failed', retry) from a page that loaded but
    genuinely carries no shareholding section (True with empty rows -> status 'empty', never retried)."""
    soup, cons = _fetch_soup(symbol)
    if soup is None:
        return [], None, False
    rows = _parse_section(soup, "shareholding", "quarter")
    return (rows, cons, True)


# ── writing + status ─────────────────────────────────────────────────────────────

def _write_symbol(conn, symbol, rows, cons):
    import json
    with conn.cursor() as cur:
        for row in rows:
            cur.execute("""
                INSERT INTO fundamentals_history
                    (symbol, consolidated, section, period_type, period_label, period_end, metrics, scraped_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,NOW())
                ON CONFLICT (symbol, section, period_label, consolidated)
                DO UPDATE SET metrics=EXCLUDED.metrics, period_end=EXCLUDED.period_end, scraped_at=NOW()
            """, (symbol, cons, row["section"], row["period_type"], row["period_label"],
                  row["period_end"], json.dumps(row["metrics"])))
    conn.commit()


def _grade_scrape(rows, cons):
    """cc#791: grade a SUCCESSFUL parse instead of stamping a flat 'ok'.

    'ok' used to mean only "some rows were written". It did not mean the quarterly table was
    parsed, and it did not mean the data was recent — so a scrape could write junk and record a
    success, which nothing alerts on and the resume logic treats as done. Two silent-success
    classes were found on 02-Aug during the cc#790 run:

      no quarters   AUBANK wrote 16 rows and COLPAL 20, both with ZERO section='quarters' rows.
                    A symbol with no quarters row can NEVER enter Basic Polish, and nothing said so.
      stale annual  COLPAL's annual sections parsed as Mar 2006..Mar 2010 and stopped — five
                    ancient periods — while its shareholding section came back current to Jun 2026.
                    Compare APLAPOLLO, which parses Mar 2015..Mar 2026 + TTM correctly.

    Returns the (status, rows_written, cons, error) tuple for _set_status. Both new statuses still
    mean "rows were written" — they are graded successes, not failures — but they are queryable and
    alertable, and the cc#790 outcome-based resume filter will retry them rather than treating a
    hollow success as done.
    """
    n = len(rows)
    qrows = [r for r in rows if r.get("section") == "quarters"]
    if not qrows:
        return ("ok_no_quarters", n, cons,
                "parsed and wrote rows but ZERO section=quarters rows — this symbol can never "
                "enter Basic Polish; check whether the quarters data-table is present on the page "
                "and simply unmatched, or genuinely absent upstream (cc#791)")
    ann = [r.get("period_end") for r in rows
           if r.get("period_type") == "annual" and r.get("period_end")]
    if ann:
        newest = max(ann)
        if (date.today() - newest).days > 730:
            return ("ok_thin", n, cons,
                    f"annual sections stop at {newest} (>2y stale) while the parse reported success "
                    "— the annual table was almost certainly truncated to its oldest columns (cc#791)")
    return ("ok", n, cons, None)


def _set_status(conn, symbol, status, rows_written=0, cons=None, error=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fundamentals_scrape_status (symbol, status, rows_written, consolidated, error, scraped_at)
            VALUES (%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (symbol) DO UPDATE SET status=EXCLUDED.status, rows_written=EXCLUDED.rows_written,
                consolidated=EXCLUDED.consolidated, error=EXCLUDED.error, scraped_at=NOW()
        """, (symbol, status, rows_written, cons, (error or "")[:400]))
    conn.commit()


def _slog(conn, category, title, details):
    import json
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                           VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                        (category, title, json.dumps(details, default=str)))
        conn.commit()
    except Exception as e:
        log.warning(f"session_log write ({title}): {e}")


# ── runner ───────────────────────────────────────────────────────────────────────

def run_scrape(mode="run", symbols=None) -> dict:
    """Phase-1 scrape. mode='test' -> 3 spot-check symbols only; else full universe (resumable).
    Per symbol: fetch + parse + upsert + status; throttle THROTTLE s; failures logged and skipped
    (never abort). Stage summary (scraped/failed/skipped) every STAGE_SIZE symbols. On completion
    writes session_log GVM_HISTORY_SCRAPE_COMPLETE and STOPS — no compute (Phases 2-4 gated).

    cc#790 TARGETED RE-SCRAPE. Pass `symbols` (a list) to scrape exactly those, BYPASSING the
    "already ok" resume filter.

    Why this had to exist: the resume filter is `status='ok'`, which is set ONCE per symbol and
    never expires. A symbol scraped successfully in a prior season is therefore skipped forever and
    can never pick up a new quarter. Measured 02-Aug: of 149 announced companies with no Q1FY27 row
    in fundamentals_history, 102 were marked ok on a single date (11-Jul-2026) — before most Q1FY27
    results were even announced (the season ran 07-Jul to 01-Aug). Re-running the full scrape could
    not have fixed them; it would have skipped all 102. Only the 47 with no status row would have
    moved. The resume filter is correct for crash-resume WITHIN a season and wrong ACROSS seasons.

    Targeted mode also skips the universe pre-filter, so an explicitly-named symbol is always
    honoured — the caller has already decided it is in scope.
    """
    import fyers_feed
    conn = fyers_feed.get_db()
    started = time.time()
    targeted = bool(symbols)
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            if targeted:
                seen, symbols = set(), [str(s).strip().upper() for s in symbols if s and str(s).strip()]
                symbols = [s for s in symbols if not (s in seen or seen.add(s))]
                already = set()          # explicit targets are never skipped
                # cc#790: _fetch_soup carries its OWN hard scrape-universe gate (cc#741, HARD_BOUNDARY
                # id=10260) at the network choke point. An out-of-universe symbol returns (None, None)
                # there, which surfaced here as "no financial tables parsed" — a deliberate POLICY SKIP
                # wearing a data-failure label. Measured 02-Aug: 81 of a 122-symbol targeted run were
                # recorded as parse failures when nothing was ever fetched.
                # Bypassing the enqueue filter therefore does NOT bypass the boundary, and it must not:
                # that boundary is a locked founder decision. So classify honestly up front instead —
                # these are marked 'skipped_out_of_universe' and never attempted, so the run's failure
                # count means what it says.
                try:
                    _univ = universe_symbols(cur)
                    _out = [s for s in symbols if s not in _univ]
                    symbols = [s for s in symbols if s in _univ]
                    for _s in _out:
                        _set_status(conn, _s, "skipped_out_of_universe", 0, None,
                                    "outside the scrape universe (cc#741 HARD_BOUNDARY id=10260) — "
                                    "never fetched; widen the universe to include it")
                    if _out:
                        log.info(f"run_scrape targeted: {len(_out)} symbol(s) outside the scrape "
                                 f"universe — classified, not attempted")
                except Exception as e:
                    log.warning(f"run_scrape targeted universe pre-classify failed: {e}")
            elif mode == "test":
                symbols = list(SPOT_CHECK)
                already = set()
            else:
                cur.execute("SELECT DISTINCT UPPER(nse_code) FROM screener_raw "
                            "WHERE nse_code IS NOT NULL AND nse_code<>'' ORDER BY 1")
                symbols = [r[0] for r in cur.fetchall()]
                _univ = universe_symbols(cur)   # cc#741: enqueue-side pre-filter to the scrape universe
                symbols = [s for s in symbols if s in _univ]
                # cc#790 SELF-HEAL: the resume set used to be "status='ok'", a flag written once and
                # never expired — so a symbol scraped in a prior season was skipped FOREVER and could
                # never pick up a new quarter. On 02-Aug that had frozen 102 announced companies at
                # their 11-Jul scrape with no Q1FY27 row, and no full run could ever have fixed them.
                #
                # Resume now tests the OUTCOME we actually care about — does this symbol already hold
                # the most recently completed quarter — instead of a proxy flag. A symbol that is
                # genuinely up to date is still skipped (crash-resume within a season keeps working),
                # but every symbol missing the running quarter is picked up automatically on the next
                # scheduled run. No manual trigger, no stale flag.
                _qend = _last_quarter_end(date.today())
                cur.execute("""SELECT s.symbol FROM fundamentals_scrape_status s
                               WHERE s.status='ok' AND EXISTS (
                                 SELECT 1 FROM fundamentals_history f
                                 WHERE UPPER(f.symbol)=UPPER(s.symbol) AND f.section='quarters'
                                   AND f.period_type='quarter' AND f.period_end=%s)""", (_qend,))
                already = {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in already]
        _slog(conn, "backfill", "GVM_HISTORY_SCRAPE_START",
              {"mode": ("targeted" if targeted else mode), "targeted": targeted,
               "universe": len(symbols), "already_ok": len(already), "to_do": len(todo)})
        ok = failed = skipped = rows_total = 0
        failures = []
        for i, sym in enumerate(todo, 1):
            try:
                rows, cons = fetch_company(sym)
                if rows:
                    _write_symbol(conn, sym, rows, cons)
                    _set_status(conn, sym, *_grade_scrape(rows, cons))
                    ok += 1; rows_total += len(rows)
                else:
                    _set_status(conn, sym, "failed", 0, None, "no financial tables parsed")
                    failed += 1; failures.append(sym)
            except Exception as e:
                try:
                    _set_status(conn, sym, "failed", 0, None, str(e))
                except Exception:
                    pass
                failed += 1; failures.append(sym)
                log.error(f"scrape {sym} failed: {e}")
            if i % STAGE_SIZE == 0:
                _slog(conn, "backfill", "GVM_HISTORY_SCRAPE_STAGE",
                      {"mode": mode, "done": i, "of": len(todo), "ok": ok, "failed": failed,
                       "rows": rows_total, "elapsed_min": round((time.time()-started)/60, 1),
                       "last": sym})
            time.sleep(THROTTLE)
        # coverage: symbols with < 8 quarters flagged (spec)
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*), COALESCE(SUM(rows_written),0) FROM fundamentals_scrape_status WHERE status='ok'""")
            tot = cur.fetchone()
            cur.execute("""SELECT COUNT(*) FROM (
                SELECT symbol FROM fundamentals_history WHERE section='quarters'
                GROUP BY symbol HAVING COUNT(*) < 8) z""")
            thin = cur.fetchone()
        # cc#791 POST-SCRAPE ASSERTION. A run used to report ok/failed counts only, so a scrape that
        # "succeeded" while landing zero running-quarter rows looked healthy. Check the thing the
        # pipeline actually exists to produce: for every symbol this run touched that the earnings
        # calendar says REPORTED this season, did the running quarter actually land? Anything that
        # did not is named here and raised as an alert, so a hollow success can never pass silently
        # again. On 02-Aug a 122-symbol run produced 13 ok and exactly ONE running-quarter row.
        missed = []
        try:
            _qend = _last_quarter_end(date.today())
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.symbol FROM fundamentals_scrape_status s
                    WHERE s.symbol = ANY(%s)
                      AND EXISTS (SELECT 1 FROM earnings_calendar e
                                  WHERE UPPER(e.ticker)=UPPER(s.symbol) AND e.status='reported'
                                    AND e.ex_date >= %s)
                      AND NOT EXISTS (SELECT 1 FROM fundamentals_history f
                                      WHERE UPPER(f.symbol)=UPPER(s.symbol) AND f.section='quarters'
                                        AND f.period_type='quarter' AND f.period_end=%s)
                    ORDER BY 1""", (todo, _qend - timedelta(days=60), _qend))
                missed = [r[0] for r in cur.fetchall()]
        except Exception as e:
            log.warning(f"post-scrape running-quarter assertion failed: {e}")
        summary = {"mode": ("targeted" if targeted else mode), "ok": ok, "failed": failed,
                   "skipped_already": len(already),
                   "rows_written_this_run": rows_total, "elapsed_min": round((time.time()-started)/60, 1),
                   "symbols_ok_total": int(tot[0] or 0), "rows_total": int(tot[1] or 0),
                   "symbols_under_8_quarters": int(thin[0] or 0), "failures_sample": failures[:50],
                   "announced_missing_running_quarter": len(missed),
                   "announced_missing_sample": missed[:50], "running_quarter_end": str(_last_quarter_end(date.today()))}
        _slog(conn, "data_audit", "GVM_HISTORY_SCRAPE_COMPLETE", summary)
        if missed:
            log.warning(f"cc#791: {len(missed)} announced symbol(s) still have no running-quarter row "
                        f"after this run: {', '.join(missed[:20])}")
            _slog(conn, "alert", "SCRAPE_ANNOUNCED_MISSING_RUNNING_QUARTER",
                  {"count": len(missed), "sample": missed[:50],
                   "running_quarter_end": str(_last_quarter_end(date.today())),
                   "note": "these companies have REPORTED per earnings_calendar but the scrape did not "
                           "land their running-quarter row — they cannot enter Basic Polish (cc#791)"})
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (SCRAPE_FLAG,))
            conn.commit()
        except Exception:
            pass
        log.info(f"run_scrape COMPLETE: {summary}")
        return summary
    finally:
        conn.close()


def _set_status_1b(conn, symbol, status, rows_written=0):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fundamentals_scrape_status (symbol, phase1b_status, phase1b_rows, phase1b_scraped_at)
            VALUES (%s,%s,%s,NOW())
            ON CONFLICT (symbol) DO UPDATE SET phase1b_status=EXCLUDED.phase1b_status,
                phase1b_rows=EXCLUDED.phase1b_rows, phase1b_scraped_at=NOW()
        """, (symbol, status, rows_written))
    conn.commit()


def run_shareholding_scrape() -> dict:
    """cc#391 Phase 1B: scrape ONLY the shareholding quarterly table for the full universe into
    fundamentals_history (section='shareholding'), tracked independently via phase1b_status. Same
    politeness pacing + checkpoint-resume (skip phase1b_status='ok'). Completion -> session_log
    PHASE_1B_SHAREHOLDING_COMPLETE. Never computes GVM (Phases 2-4 gated)."""
    import fyers_feed
    conn = fyers_feed.get_db()
    started = time.time()
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT UPPER(nse_code) FROM screener_raw "
                        "WHERE nse_code IS NOT NULL AND nse_code<>'' ORDER BY 1")
            symbols = [r[0] for r in cur.fetchall()]
            _univ = universe_symbols(cur)   # cc#741: enqueue-side pre-filter to the scrape universe
            symbols = [s for s in symbols if s in _univ]
            # cc#597: skip 'ok' AND 'empty' (a page that loaded but genuinely has no shareholding
            # section — delisted/new/illiquid). Only NULL + 'failed' are retried.
            cur.execute("SELECT symbol FROM fundamentals_scrape_status WHERE phase1b_status IN ('ok','empty')")
            already = {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in already]
        _slog(conn, "backfill", "PHASE_1B_SHAREHOLDING_START",
              {"universe": len(symbols), "already_done": len(already), "to_do": len(todo)})
        ok = failed = empty = rows_total = 0
        failures = []
        for i, sym in enumerate(todo, 1):
            try:
                rows, cons, page_ok = fetch_shareholding(sym)
                if rows:
                    _write_symbol(conn, sym, rows, cons)
                    _set_status_1b(conn, sym, "ok", len(rows))
                    ok += 1; rows_total += len(rows)
                elif page_ok:
                    # cc#597: page loaded, no shareholding section -> genuinely empty, never retry
                    _set_status_1b(conn, sym, "empty", 0)
                    empty += 1
                else:
                    _set_status_1b(conn, sym, "failed", 0)
                    failed += 1; failures.append(sym)
            except Exception as e:
                try:
                    _set_status_1b(conn, sym, "failed", 0)
                except Exception:
                    pass
                failed += 1; failures.append(sym)
                log.error(f"shareholding scrape {sym} failed: {e}")
            if i % STAGE_SIZE == 0:
                _slog(conn, "backfill", "PHASE_1B_SHAREHOLDING_STAGE",
                      {"done": i, "of": len(todo), "ok": ok, "failed": failed, "empty": empty,
                       "rows": rows_total, "elapsed_min": round((time.time()-started)/60, 1), "last": sym})
            time.sleep(THROTTLE)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM fundamentals_scrape_status WHERE phase1b_status='ok'")
            tot = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM fundamentals_scrape_status WHERE phase1b_status='empty'")
            emp_tot = cur.fetchone()
            cur.execute("SELECT COUNT(DISTINCT symbol) FROM fundamentals_history WHERE section='shareholding'")
            covered = cur.fetchone()
            cur.execute("""SELECT COUNT(*) FROM (SELECT symbol FROM fundamentals_history
                           WHERE section='shareholding' GROUP BY symbol HAVING COUNT(*)>=12) z""")
            full12 = cur.fetchone()
        summary = {"ok": ok, "failed": failed, "empty": empty, "skipped_already": len(already),
                   "rows_written_this_run": rows_total, "symbols_ok_total": int(tot[0] or 0),
                   "symbols_empty_total": int(emp_tot[0] or 0),
                   "symbols_with_shareholding": int(covered[0] or 0),
                   "symbols_full_12q": int(full12[0] or 0),
                   "elapsed_min": round((time.time()-started)/60, 1), "failures_sample": failures[:50]}
        _slog(conn, "data_audit", "PHASE_1B_SHAREHOLDING_COMPLETE", summary)
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key=%s", (SCRAPE_1B_FLAG,))
            conn.commit()
        except Exception:
            pass
        log.info(f"run_shareholding_scrape COMPLETE: {summary}")
        return summary
    finally:
        conn.close()


def _last_quarter_end(today: date) -> date:
    """cc#598: the most recently COMPLETED calendar quarter-end on or before `today`. Aligns with
    SHAREHOLDING_SCRAPE_CADENCE_V1 quarter_map — a run in Jul->Jun30 (Q1), Oct->Sep30 (Q2),
    Jan->prev Dec31 (Q3), Apr->Mar31 (Q4)."""
    y, m = today.year, today.month
    if m >= 10:
        return date(y, 9, 30)     # Q3 (Jul-Sep) closed
    if m >= 7:
        return date(y, 6, 30)     # Q2 (Apr-Jun) closed
    if m >= 4:
        return date(y, 3, 31)     # Q1 (Jan-Mar) closed
    return date(y - 1, 12, 31)    # Jan-Mar -> prior Q4 (Oct-Dec) closed


def run_shareholding_quarterly(target_end: date = None, today: date = None) -> dict:
    """cc#598 (SHAREHOLDING_SCRAPE_CADENCE_V1, session_log id=7121): rolling-forward quarterly
    refresh of the shareholding table. Unlike the cc#391 backfill (which skips every symbol whose
    phase1b_status='ok', so it never re-scrapes), this appends the JUST-CLOSED quarter for the
    full GVM universe — it skips only the symbols that ALREADY carry a shareholding row with
    period_end >= the target quarter-end. That makes a daily retry inside the last-week window
    cheap (re-scrapes only symbols still missing the new quarter) and self-idling once the universe
    is captured. Append-only via _write_symbol's ON CONFLICT (rolling 12Q; consumers read last 12).
    Same politeness pacing (THROTTLE) + phase1b_status tracking as cc#391. Completion ->
    session_log data_audit SHAREHOLDING_QUARTERLY_RUN. Never computes GVM."""
    import fyers_feed
    conn = fyers_feed.get_db()
    started = time.time()
    today = today or date.today()
    target_end = target_end or _last_quarter_end(today)
    try:
        ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT UPPER(nse_code) FROM screener_raw "
                        "WHERE nse_code IS NOT NULL AND nse_code<>'' ORDER BY 1")
            symbols = [r[0] for r in cur.fetchall()]
            _univ = universe_symbols(cur)   # cc#741: enqueue-side pre-filter to the scrape universe
            symbols = [s for s in symbols if s in _univ]
            cur.execute("""SELECT DISTINCT symbol FROM fundamentals_history
                           WHERE section='shareholding' AND period_end >= %s""", (target_end,))
            already = {r[0] for r in cur.fetchall()}
            # cc#597: also skip genuinely-empty symbols (delisted/new/illiquid, no section) so a
            # quarterly pass never re-scrapes them forever.
            cur.execute("SELECT symbol FROM fundamentals_scrape_status WHERE phase1b_status='empty'")
            already |= {r[0] for r in cur.fetchall()}
        todo = [s for s in symbols if s not in already]
        _slog(conn, "backfill", "SHAREHOLDING_QUARTERLY_START",
              {"target_quarter_end": str(target_end), "universe": len(symbols),
               "already_have_quarter": len(already), "to_do": len(todo)})
        ok = failed = empty = rows_total = got_quarter = 0
        failures = []
        for i, sym in enumerate(todo, 1):
            try:
                rows, cons, page_ok = fetch_shareholding(sym)
                if rows:
                    _write_symbol(conn, sym, rows, cons)
                    _set_status_1b(conn, sym, "ok", len(rows))
                    ok += 1; rows_total += len(rows)
                    if any((r.get("period_end") is not None and r["period_end"] >= target_end)
                           for r in rows):
                        got_quarter += 1
                elif page_ok:
                    _set_status_1b(conn, sym, "empty", 0)   # cc#597: genuinely no section, never retry
                    empty += 1
                else:
                    _set_status_1b(conn, sym, "failed", 0)
                    failed += 1; failures.append(sym)
            except Exception as e:
                try:
                    _set_status_1b(conn, sym, "failed", 0)
                except Exception:
                    pass
                failed += 1; failures.append(sym)
                log.error(f"shareholding quarterly {sym} failed: {e}")
            if i % STAGE_SIZE == 0:
                _slog(conn, "backfill", "SHAREHOLDING_QUARTERLY_STAGE",
                      {"target_quarter_end": str(target_end), "done": i, "of": len(todo),
                       "ok": ok, "failed": failed, "empty": empty, "got_quarter": got_quarter,
                       "rows": rows_total, "elapsed_min": round((time.time()-started)/60, 1), "last": sym})
            time.sleep(THROTTLE)
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(DISTINCT symbol) FROM fundamentals_history
                           WHERE section='shareholding' AND period_end >= %s""", (target_end,))
            covered = cur.fetchone()
        summary = {"target_quarter_end": str(target_end), "ok": ok, "failed": failed, "empty": empty,
                   "skipped_already_have_quarter": len(already),
                   "new_quarter_captured_this_run": got_quarter,
                   "universe_with_target_quarter": int((covered or [0])[0] or 0),
                   "rows_written_this_run": rows_total,
                   "elapsed_min": round((time.time()-started)/60, 1),
                   "complete_for_universe": len(todo) == 0,
                   "failures_sample": failures[:50]}
        _slog(conn, "data_audit", "SHAREHOLDING_QUARTERLY_RUN", summary)
        log.info(f"run_shareholding_quarterly COMPLETE: {summary}")
        return summary
    finally:
        conn.close()


def _claim_scrape_flag():
    """Consume app_config['fundamentals_scrape']; return the mode or None.
    Marks 'claimed:<value>' so it never re-fires on the next boot.

    Accepted values:
      'test'                  -> 3 spot-check symbols
      'run' | 'pending'       -> full universe (resumable)
      'run:SYM1,SYM2,...'     -> cc#790 TARGETED re-scrape of exactly those symbols

    The targeted form exists because this flag is the ONLY trigger that survives a restart, and a
    season refresh often needs a bounded, explicitly-scoped run rather than the whole universe.
    Returns either a mode string or a ('targeted', [symbols]) tuple.
    """
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s FOR UPDATE", (SCRAPE_FLAG,))
                r = cur.fetchone()
                raw = (r[0] or "").strip() if r else ""
                val = raw.lower()
                syms = []
                if val.startswith("run:"):
                    syms = [s.strip().upper() for s in raw.split(":", 1)[1].split(",") if s.strip()]
                    if not syms:
                        conn.commit(); return None
                elif val not in ("test", "run", "pending"):
                    conn.commit(); return None
                cur.execute("UPDATE app_config SET value=%s, updated_at=NOW() WHERE key=%s",
                            (f"claimed:{raw}"[:2000], SCRAPE_FLAG))
            conn.commit()
        finally:
            conn.close()
        if syms:
            return ("targeted", syms)
        return "test" if val == "test" else "run"
    except Exception as e:
        log.error(f"scrape flag claim failed: {e}")
        return None


def _claim_1b_flag():
    """Consume app_config['fundamentals_scrape_1b'] in ('run','pending') -> claimed. Returns True once."""
    import fyers_feed
    try:
        conn = fyers_feed.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key=%s FOR UPDATE", (SCRAPE_1B_FLAG,))
                r = cur.fetchone()
                val = (r[0] or "").strip().lower() if r else ""
                if val not in ("run", "pending"):
                    conn.commit(); return False
                cur.execute("UPDATE app_config SET value='claimed', updated_at=NOW() WHERE key=%s", (SCRAPE_1B_FLAG,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        log.error(f"scrape_1b flag claim failed: {e}")
        return False


@router.on_event("startup")
async def _scrape_startup_trigger():
    import threading
    mode = _claim_scrape_flag()
    if isinstance(mode, tuple) and mode[0] == "targeted":
        _syms = mode[1]
        log.info(f"cc#790: fundamentals_scrape flag claimed (targeted, {len(_syms)} symbols) — starting in background")
        threading.Thread(target=run_scrape, kwargs={"symbols": _syms},
                         name="cc790-scrape-flag-targeted", daemon=True).start()
    elif mode:
        log.info(f"cc#361: fundamentals_scrape flag claimed (mode={mode}) — starting in background")
        threading.Thread(target=run_scrape, args=(mode,), name="cc361-scrape", daemon=True).start()
    # cc#741 (founder 28-Jul, id=10093): the Phase 1B full-universe shareholding backfill ambition is
    # RETIRED — everything beyond the ~613-symbol scrape universe is cancelled (nothing outside it feeds
    # the rating engine). The startup auto-trigger is disabled so an armed flag can NEVER relaunch the
    # full-universe queue. The manual /run_shareholding_scrape endpoint remains (now hard-gated at
    # _fetch_soup to the universe) for a deliberate, universe-scoped refresh if ever needed.
    # if _claim_1b_flag():   # cc#391 — RETIRED cc#741
    #     log.info("cc#391: fundamentals_scrape_1b flag claimed — starting shareholding scrape in background")
    #     threading.Thread(target=run_shareholding_scrape, name="cc391-scrape1b", daemon=True).start()


@router.get("/fundamentals_scrape_status")
async def fundamentals_scrape_status():
    """cc#361 status: per-status counts + rows + a rough ETA for the remaining universe."""
    import fyers_feed
    conn = fyers_feed.get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.fundamentals_scrape_status')")
            if cur.fetchone()[0] is None:
                return {"status": "not_started", "note": "tables not yet created"}
            cur.execute("SELECT status, COUNT(*) FROM fundamentals_scrape_status GROUP BY status")
            counts = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT COALESCE(SUM(rows_written),0) FROM fundamentals_scrape_status WHERE status='ok'")
            rows = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT phase1b_status, COUNT(*) FROM fundamentals_scrape_status WHERE phase1b_status IS NOT NULL GROUP BY phase1b_status")
            phase1b = {r[0]: int(r[1]) for r in cur.fetchall()}
            cur.execute("SELECT COUNT(DISTINCT UPPER(nse_code)) FROM screener_raw WHERE nse_code IS NOT NULL AND nse_code<>''")
            universe = int(cur.fetchone()[0] or 0)
        done = sum(counts.values())
        remaining = max(0, universe - counts.get("ok", 0))
        return {"universe": universe, "counts": counts, "rows_written": rows,
                "phase1b": phase1b, "remaining_est": remaining,
                "eta_min_est": round(remaining * THROTTLE / 60, 1),
                "at": datetime.utcnow().isoformat()}
    finally:
        conn.close()


@router.post("/run_fundamentals_scrape")
async def run_fundamentals_scrape(mode: str = "run", symbols: Optional[str] = None,
                                  x_admin_token: Optional[str] = Header(None)):
    """cc#361 Phase 1: kick the Screener scrape in a background daemon. mode='test' (3 spot-check
    symbols) or 'run' (full universe, resumable). Phases 2-4 (compute) remain gated.

    cc#790: `symbols` (comma-separated) runs a TARGETED re-scrape of exactly those, bypassing the
    'already ok' resume filter. Needed because that filter never expires, so a symbol scraped in a
    prior season is skipped forever and can never pick up a new quarter — the cause of the 02-Aug
    gap where 102 announced companies stayed frozen at their 11-Jul scrape."""
    _check_admin(x_admin_token)
    import threading
    syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    if syms:
        threading.Thread(target=run_scrape, kwargs={"symbols": syms},
                         name="cc790-scrape-targeted", daemon=True).start()
        return {"status": "started", "mode": "targeted", "symbols": len(syms),
                "note": "Targeted re-scrape running; poll /api/admin/fundamentals_scrape_status."}
    m = "test" if (mode or "").lower() == "test" else "run"
    threading.Thread(target=run_scrape, args=(m,), name="cc361-scrape-manual", daemon=True).start()
    return {"status": "started", "mode": m,
            "note": "Scrape running in background; poll /api/admin/fundamentals_scrape_status."}


@router.post("/run_shareholding_scrape")
async def run_shareholding_scrape_now(x_admin_token: Optional[str] = Header(None)):
    """cc#391 Phase 1B: kick the shareholding scrape (Promoters/FIIs/DIIs/... per quarter) in a
    background daemon — resumable, independent of Phase 1. Phases 2-4 remain gated."""
    _check_admin(x_admin_token)
    import threading
    threading.Thread(target=run_shareholding_scrape, name="cc391-scrape1b-manual", daemon=True).start()
    return {"status": "started",
            "note": "Shareholding scrape running; poll /api/admin/fundamentals_scrape_status (phase1b_* fields)."}


@router.post("/run_shareholding_quarterly")
async def run_shareholding_quarterly_now(x_admin_token: Optional[str] = Header(None)):
    """cc#598: force a shareholding QUARTERLY refresh out-of-band (the scheduler runs it automatically
    in the last week of Jul/Oct/Jan/Apr). Targets the just-closed quarter; skips symbols that already
    carry it, so a forced run is safe/idempotent. Background daemon; poll fundamentals_scrape_status."""
    _check_admin(x_admin_token)
    import threading
    threading.Thread(target=run_shareholding_quarterly, name="cc598-shareholding-q-manual", daemon=True).start()
    return {"status": "started", "target_quarter_end": str(_last_quarter_end(date.today())),
            "note": "Shareholding quarterly refresh running; poll /api/admin/fundamentals_scrape_status (phase1b_* fields)."}

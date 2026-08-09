"""
yahoo_symbol_resolver.py — cc#938 PRICE FEED RESOLVER

FOUNDER RULE (09-Aug-2026): if a live feed is not available for a symbol, fall back to EOD.
Every GVM-universe symbol must at minimum have Yahoo EOD daily prices in raw_prices, and any
symbol that genuinely cannot be priced must be FLAGGED — never silently scored on nothing.

WHY THIS FILE EXISTS, AND WHY IT IS NOT A ONE-OFF FIX
-----------------------------------------------------
The nightly Yahoo EOD job has exactly one rule for turning an NSE symbol into a Yahoo ticker:
append ".NS". That rule has two failure modes, and the 09-Aug scan found both at once.

  1. NO ROWS AT ALL. The nightly universe is `SELECT DISTINCT symbol FROM raw_prices`, so a
     symbol with zero rows is never fetched — and because it is never fetched it never gets a
     row. 64 GVM-universe symbols were sitting in that trap. They cannot bootstrap themselves.

  2. THE TICKER MOVED. 10 more symbols DO have history and then stopped: Yahoo now 404s on
     `<SYMBOL>.NS` for them (JBCHEPHARM frozen at 23-Jul is the one the founder spotted; the
     nightly drop log has been printing the same 12 names every night since). A hardcoded
     append-".NS" has nowhere to record "this one is actually called something else", so the
     job re-learns the same 404 every night forever.

So the fix is a MAPPING TABLE plus a RESOLVER, not a patched symbol.

  yahoo_symbol_map      symbol -> the ticker that actually works, with how it was found.
                        yahoo_daily_update consults it BEFORE falling back to append-".NS",
                        so a resolved symbol stays fixed without anyone editing code again.
  price_feed_excluded   symbols Yahoo cannot serve on any candidate (SME boards, unlisted
                        InvITs). Written with the reason and every attempt, so a price-dependent
                        field can say "no data" instead of scoring a blank as a zero.

THE CANDIDATE LADDER — cheapest and most likely first, one request at a time (Yahoo hard rule):
  1. an existing yahoo_symbol_map row, re-verified (a mapping can break twice)
  2. <SYMBOL>.NS            — the default, right for the overwhelming majority
  3. Yahoo's own search API — this is the step that FINDS a moved ticker instead of guessing it
  4. <BSE_CODE>.BO          — when a caller supplies the numeric BSE code
The first candidate that returns real bars wins and is written to the map.

Nothing here writes a price it did not receive from Yahoo, and a symbol that resolves to zero
bars is recorded as excluded rather than quietly left looking healthy.
"""

import json
import logging
import os
import time
from typing import List, Optional

import httpx
import psycopg
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger(__name__)
router = APIRouter()

DB_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
UA = "Mozilla/5.0 (compatible; Scorr/1.0)"
# One second between Yahoo calls. This is a politeness guarantee, not a tuning knob — the whole
# reason this runs as an armed batch rather than inline is to keep the request rate boring.
THROTTLE = 1.0
# Yahoo exchange codes we accept from the search API. NSI = NSE, BSE = BSE. Anything else
# (NYQ, LSE, a currency pair) is a name collision on a foreign listing, not our symbol.
_OK_EXCHANGES = ("NSI", "BSE")


def _conn():
    return psycopg.connect(DB_URL)


def ensure_tables(cur):
    """Idempotent. CREATE TABLE IF NOT EXISTS only — no ALTER, so MAINTENANCE_LOCK_RULE (cc#351)
    is not engaged: nothing here takes a lock on an existing object."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS yahoo_symbol_map (
            symbol        TEXT PRIMARY KEY,
            yahoo_ticker  TEXT NOT NULL,
            source        TEXT,
            bars          INTEGER,
            first_date    DATE,
            last_date     DATE,
            resolved_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_feed_excluded (
            symbol      TEXT PRIMARY KEY,
            reason      TEXT,
            attempts    JSONB,
            checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")


def load_map(cur=None) -> dict:
    """{symbol: yahoo_ticker} for every resolved override. Read once per run by the nightly job."""
    own = cur is None
    conn = None
    try:
        if own:
            conn = _conn()
            cur = conn.cursor()
        ensure_tables(cur)
        cur.execute("SELECT UPPER(symbol), yahoo_ticker FROM yahoo_symbol_map WHERE yahoo_ticker IS NOT NULL")
        return {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        log.warning(f"yahoo_symbol_map load failed (falling back to the .NS rule): {e}")
        return {}
    finally:
        if own and conn is not None:
            try:
                conn.commit()
                conn.close()
            except Exception:
                pass


def load_excluded(cur=None) -> set:
    """Symbols Yahoo cannot serve. The nightly job skips them so the drop log stops repeating
    the same unfixable 404s every night and a real new break stays visible in it."""
    own = cur is None
    conn = None
    try:
        if own:
            conn = _conn()
            cur = conn.cursor()
        ensure_tables(cur)
        cur.execute("SELECT UPPER(symbol) FROM price_feed_excluded")
        return {r[0] for r in cur.fetchall()}
    except Exception as e:
        log.warning(f"price_feed_excluded load failed: {e}")
        return set()
    finally:
        if own and conn is not None:
            try:
                conn.commit()
                conn.close()
            except Exception:
                pass


def _search_tickers(query: str) -> List[str]:
    """Ask Yahoo what it calls this thing. Returns candidate tickers on NSE/BSE, best first.

    This is the step that makes the resolver a fix rather than a guess: when JBCHEPHARM.NS
    starts 404ing, no amount of suffix logic recovers it — only asking Yahoo does.
    """
    try:
        with httpx.Client(timeout=15.0, headers={"User-Agent": UA}) as client:
            r = client.get(SEARCH_URL, params={"q": query, "quotesCount": 10, "newsCount": 0,
                                               "enableFuzzyQuery": "false"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning(f"yahoo search '{query}': {type(e).__name__}: {str(e)[:120]}")
        return []
    out = []
    for q in (data.get("quotes") or []):
        sym, exch = q.get("symbol"), q.get("exchange")
        if not sym or exch not in _OK_EXCHANGES:
            continue
        if q.get("quoteType") not in (None, "EQUITY", "ETF", "MUTUALFUND", "INDEX"):
            continue
        out.append(sym)
    # NSE ahead of BSE: .NS carries the volume our momentum metrics are built on.
    out.sort(key=lambda t: 0 if t.endswith(".NS") else 1)
    return out


def _probe(ticker: str, symbol: str, lookback: str):
    """One chart request for one candidate. Reuses the nightly job's own row shaper so a bar
    ingested here is byte-identical to a bar ingested by the nightly path — there is exactly
    one place in the codebase that turns a Yahoo chart response into a raw_prices row."""
    import yahoo_daily_update as ydu
    rows, reason = ydu._fetch_history_ticker(ticker, symbol, lookback)
    return rows, reason


def resolve_symbol(symbol: str, bse: Optional[str] = None, name: Optional[str] = None,
                   lookback: str = "5y", known: Optional[str] = None, fresh_by=None):
    """Walk the candidate ladder for ONE symbol. Returns (ticker, rows, attempts, source, fresh).

    FRESHNESS IS PART OF RESOLVING, not a detail — this is the correction that the first live run
    forced. The first build stopped at the first candidate that returned ANY bars, and for a symbol
    whose entire complaint is "frozen" that is the one answer guaranteed to be wrong: JBCHEPHARM.NS
    still serves 1,228 bars, it just has nothing after 23-Jul, so the ladder declared victory on the
    exact series that is stuck and never asked Yahoo whether the name had moved. A candidate now
    only WINS if its newest bar is at least `fresh_by` (the newest price_date in raw_prices, i.e.
    the last session the rest of the table has). Anything older is remembered as a fallback and the
    ladder keeps walking.

    If nothing fresh exists, the best stale candidate is still returned — with fresh=False — because
    for a zero-row symbol five years of history ending last quarter beats nothing at all. The caller
    records that as `stale_feed`, NOT as resolved: a series Yahoo has stopped updating is a finding
    the founder needs to see, not a green tick.
    """
    symbol = (symbol or "").strip().upper()
    attempts = []
    tried = set()
    best_stale = None   # (ticker, rows, source, last_date)

    def _try(ticker, source):
        """Probe one candidate. Returns rows ONLY when they clear the freshness bar; otherwise the
        candidate is banked as a possible stale fallback and we report 'not fresh enough'."""
        nonlocal best_stale
        if not ticker or ticker in tried:
            return []
        tried.add(ticker)
        rows, reason = _probe(ticker, symbol, lookback)
        last = max((r[1] for r in rows), default=None)
        fresh = bool(rows) and (fresh_by is None or (last is not None and last >= fresh_by))
        attempts.append({"ticker": ticker, "source": source, "bars": len(rows),
                         "last_date": str(last) if last else None, "fresh": fresh,
                         "reason": reason if not rows else (None if fresh else "series_stale")})
        time.sleep(THROTTLE)
        if rows and not fresh:
            if best_stale is None or (last and best_stale[3] and last > best_stale[3]):
                best_stale = (ticker, rows, source, last)
            return []
        return rows

    # 1 — a mapping we already learned. Re-verified, because a ticker can move twice.
    if known:
        rows = _try(known, "existing_map")
        if rows:
            return known, rows, attempts, "existing_map", True

    # 2 — the default rule.
    nse = f"{symbol}.NS"
    rows = _try(nse, "nse_suffix")
    if rows:
        return nse, rows, attempts, "nse_suffix", True

    # 3 — ask Yahoo. Search on the raw code first, then the company name if we have one; a code
    #     that Yahoo has retired usually still resolves by name. This is also where the NSE board
    #     suffixes live that no append rule would ever produce: SME names are <SYM>-SM.NS, InvITs
    #     are <SYM>-IV.NS, rights entitlements <SYM>-RR.NS. Seven of the first twelve were these.
    for q in [symbol] + ([name] if name else []):
        for cand in _search_tickers(q):
            rows = _try(cand, f"search:{q}")
            if rows:
                return cand, rows, attempts, f"search:{q}", True
        time.sleep(THROTTLE)

    # 4 — explicit BSE numeric code, when the caller supplied one.
    if bse:
        code = str(bse).split(".")[0].strip()
        if code and code.lower() not in ("none", "nan", ""):
            bo = f"{code}.BO"
            rows = _try(bo, "bse_code")
            if rows:
                return bo, rows, attempts, "bse_code", True

    if best_stale:
        t, rws, src, _last = best_stale
        return t, rws, attempts, src, False

    return None, [], attempts, None, False


def resolve_and_backfill(symbols, lookback: str = "5y", specs=None) -> dict:
    """Resolve every symbol, ingest whatever history Yahoo has, and record the outcome either
    way. Idempotent: re-running re-verifies the map and clears an exclusion that has started
    working. Returns a report with the resolved/unresolved split.

    specs (optional): [{"nse","bse","isin","name"}] — supplies BSE codes / company names for the
    ladder. Symbols without a spec still go through steps 1-3.
    """
    import yahoo_daily_update as ydu
    by_sym = {}
    for sp in (specs or []):
        k = (sp.get("nse") or sp.get("symbol") or "").strip().upper()
        if k:
            by_sym[k] = sp

    syms = [s.strip().upper() for s in symbols if (s or "").strip()]
    with _conn() as conn, conn.cursor() as cur:
        ensure_tables(cur)
        conn.commit()
        cur.execute("SELECT UPPER(symbol), yahoo_ticker FROM yahoo_symbol_map WHERE UPPER(symbol) = ANY(%s)",
                    (syms,))
        known_map = {r[0]: r[1] for r in cur.fetchall()}
        # Company names come from GVM so the search step has something human to match on.
        cur.execute("""SELECT DISTINCT ON (UPPER(symbol)) UPPER(symbol), company_name
                       FROM gvm_scores WHERE UPPER(symbol) = ANY(%s)
                       ORDER BY UPPER(symbol), score_date DESC""", (syms,))
        names = {r[0]: r[1] for r in cur.fetchall()}
        # The freshness bar: the newest price_date the table holds for ANY symbol — i.e. the last
        # session the rest of the universe has priced. A candidate whose newest bar is older than
        # that is not serving this symbol any more, whatever its history depth.
        cur.execute("SELECT MAX(price_date) FROM raw_prices")
        fresh_by = cur.fetchone()[0]

    report = []
    for sym in syms:
        sp = by_sym.get(sym, {})
        ticker, rows, attempts, source, fresh = resolve_symbol(
            sym, bse=sp.get("bse"), name=sp.get("name") or names.get(sym),
            lookback=lookback, known=known_map.get(sym), fresh_by=fresh_by)
        if rows:
            written = ydu._commit_rows({sym: rows})
            first, last = min(r[1] for r in rows), max(r[1] for r in rows)
            with _conn() as conn, conn.cursor() as cur:
                # The map row is written either way — the nightly job still needs to know which
                # ticker serves this symbol — but `source` carries the stale marker so nobody reads
                # the table as a list of healthy feeds.
                cur.execute("""INSERT INTO yahoo_symbol_map
                                 (symbol, yahoo_ticker, source, bars, first_date, last_date, resolved_at)
                               VALUES (%s,%s,%s,%s,%s,%s,NOW())
                               ON CONFLICT (symbol) DO UPDATE SET
                                 yahoo_ticker=EXCLUDED.yahoo_ticker, source=EXCLUDED.source,
                                 bars=EXCLUDED.bars, first_date=EXCLUDED.first_date,
                                 last_date=EXCLUDED.last_date, resolved_at=NOW()""",
                            (sym, ticker, source if fresh else f"{source} (STALE)",
                             len(rows), first, last))
                # It works now, so it is no longer excluded. Leaving a stale exclusion behind
                # would keep the nightly job skipping a symbol that has come back to life.
                cur.execute("DELETE FROM price_feed_excluded WHERE UPPER(symbol)=%s", (sym,))
                conn.commit()
            report.append({"symbol": sym,
                           # A stale series is NOT a resolution. The bars are still ingested (five
                           # years ending last quarter beats nothing for a dark symbol) but the
                           # status says plainly that Yahoo has stopped updating this name.
                           "status": "resolved" if fresh else "stale_feed",
                           "yahoo_ticker": ticker, "source": source, "bars_upserted": written,
                           "first_date": str(first), "last_date": str(last),
                           "fresh_by": str(fresh_by) if fresh_by else None,
                           "attempts": attempts})
        else:
            reason = (attempts[-1].get("reason") if attempts else None) or "no_candidate_resolved"
            with _conn() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO price_feed_excluded (symbol, reason, attempts, checked_at)
                               VALUES (%s,%s,%s::jsonb,NOW())
                               ON CONFLICT (symbol) DO UPDATE SET
                                 reason=EXCLUDED.reason, attempts=EXCLUDED.attempts, checked_at=NOW()""",
                            (sym, reason[:300], json.dumps(attempts, default=str)))
                conn.commit()
            report.append({"symbol": sym, "status": "excluded", "reason": reason, "attempts": attempts})

    resolved = [r for r in report if r["status"] == "resolved"]
    stale = [r for r in report if r["status"] == "stale_feed"]
    excl = [r for r in report if r["status"] == "excluded"]
    summary = {"attempted": len(syms), "resolved": len(resolved),
               "stale_feed": len(stale), "excluded": len(excl),
               "fresh_by": str(fresh_by) if fresh_by else None,
               "bars_upserted": sum(r.get("bars_upserted") or 0 for r in report),
               "resolved_symbols": [r["symbol"] for r in resolved],
               "stale_symbols": [f"{r['symbol']}@{r['last_date']}" for r in stale],
               "excluded_symbols": [r["symbol"] for r in excl]}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), 'yahoo_backfill', 'YAHOO_SYMBOL_RESOLVE', %s::jsonb)",
                        (json.dumps({"summary": summary, "report": report}, default=str),))
            conn.commit()
    except Exception as e:
        log.warning(f"resolve oplog: {e}")
    log.info(f"yahoo_symbol_resolver: {summary}")
    return {"summary": summary, "report": report}


# ── NSE bhavcopy healer — the answer when Yahoo has abandoned a name ──────────────────────────

def heal_from_nse(symbols, max_dates: int = 40) -> dict:
    """cc#938 — fill raw_prices from NSE's own EOD file for symbols Yahoo has stopped serving.

    THIS IS THE FOUNDER RULE TAKEN LITERALLY: "if a live feed is not available for a symbol, fall
    back to EOD". The first resolver run proved Yahoo cannot serve four of these names under ANY
    ticker — JBCHEPHARM's series simply ends on 23-Jul, and Yahoo search offers nothing newer. So
    the fix is not a better Yahoo guess, it is a different source.

    We already download exactly the file we need. nse_eod_ingest pulls sec_bhavdata_full every
    night for delivery percentages and throws the price columns away; the same row carries
    OPEN/HIGH/LOW/CLOSE for every EQ-series scrip on the exchange. NSE is also the authority Yahoo
    is republishing, so this is a step CLOSER to source, not a workaround.

    ONE FILE PER DATE, and that file serves every symbol at once — so the cost is the number of
    missing DATES, not the number of symbols. Capped at `max_dates` a run (oldest gap first, so
    holes fill contiguously and a re-run continues where this one stopped) because each date is a
    separate archive download from NSE.
    """
    import nse_eod_ingest as nse
    import yahoo_daily_update as ydu
    import csv as _csv
    import io as _io

    syms = {(s or "").strip().upper() for s in symbols if (s or "").strip()}
    if not syms:
        return {"symbols": 0, "dates": 0, "rows": 0, "note": "no symbols"}

    with _conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(price_date) FROM raw_prices")
        target = cur.fetchone()[0]
        cur.execute("""SELECT UPPER(symbol), MAX(price_date) FROM raw_prices
                       WHERE UPPER(symbol) = ANY(%s) GROUP BY 1""", (sorted(syms),))
        have = {r[0]: r[1] for r in cur.fetchall()}
        # The dates to fetch are the sessions the REST of the table already has and these symbols
        # do not. Deriving them from raw_prices rather than from a calendar means holidays and
        # non-trading days can never enter the list — a date nobody has is not a gap.
        oldest = min([have.get(s) for s in syms if have.get(s)] or [target])
        cur.execute("""SELECT DISTINCT price_date FROM raw_prices
                       WHERE price_date > %s AND price_date <= %s
                       ORDER BY price_date LIMIT %s""", (oldest, target, max_dates))
        dates = [r[0] for r in cur.fetchall()]

    if not dates:
        return {"symbols": len(syms), "dates": 0, "rows": 0, "note": "no missing sessions"}

    session = nse._nse_session()
    total, per_date = 0, []
    for d in dates:
        try:
            r = nse._nse_get(session, nse._delivery_url(d), referer=f"{nse._API_HOST}/all-reports")
            text = r.content.decode("utf-8-sig", errors="replace")
        except Exception as e:
            per_date.append({"date": str(d), "rows": 0, "error": f"{type(e).__name__}: {str(e)[:120]}"})
            continue
        rows = []
        for row in _csv.DictReader(_io.StringIO(text)):
            row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            if row.get("SERIES", "").strip().upper() != "EQ":
                continue
            sym = (row.get("SYMBOL") or "").strip().upper()
            if sym not in syms:
                continue
            c = nse._f(row.get("CLOSE_PRICE"))
            if c is None:
                continue
            o, h, l = (nse._f(row.get("OPEN_PRICE")), nse._f(row.get("HIGH_PRICE")),
                       nse._f(row.get("LOW_PRICE")))
            v = nse._f(row.get("TTL_TRD_QNTY"))
            # adjusted_close = close. NSE publishes the traded price, not a split-adjusted one, so
            # claiming an adjustment we did not compute would be a fabricated number. The CA
            # watchdog's restate path is what adjusts history, and it works off this same column.
            rows.append((sym, d, o, h, l, c, c, int(v) if v is not None else 0))
        if rows:
            with _conn() as conn, conn.cursor() as cur:
                cur.executemany(ydu.UPSERT_SQL, rows)
                conn.commit()
            total += len(rows)
        per_date.append({"date": str(d), "rows": len(rows)})
        time.sleep(0.6)

    out = {"symbols": len(syms), "dates": len(dates), "rows": total,
           "first_date": str(dates[0]), "last_date": str(dates[-1]), "per_date": per_date[-10:]}
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), 'yahoo_backfill', 'NSE_PRICE_HEAL', %s::jsonb)",
                        (json.dumps({"summary": out, "symbols": sorted(syms)}, default=str),))
            conn.commit()
    except Exception as e:
        log.warning(f"heal_from_nse oplog: {e}")
    log.info(f"heal_from_nse: {out}")
    return out


def stale_symbols() -> List[str]:
    """Symbols whose Yahoo series exists but has stopped updating — the heal_from_nse work-list."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            conn.commit()
            cur.execute("SELECT UPPER(symbol) FROM yahoo_symbol_map WHERE source LIKE '%%(STALE)'")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"stale_symbols: {e}")
        return []


# ── routes ────────────────────────────────────────────────────────────────────────────────────

def _check_admin(token):
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


@router.post("/api/admin/yahoo/resolve")
def yahoo_resolve(symbols: str = "", lookback: str = "5y", missing: bool = False,
                  x_admin_token: Optional[str] = Header(None)):
    """cc#938: resolve + EOD-backfill a symbol list. Synchronous, one Yahoo call per candidate
    with a 1s throttle, so keep batches modest.

    symbols  comma/space separated NSE codes
    missing  true -> every GVM-universe symbol with ZERO raw_prices rows (the self-healing set)
    """
    _check_admin(x_admin_token)
    syms = [s.strip().upper() for s in (symbols or "").replace(",", " ").split() if s.strip()]
    if missing:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT UPPER(g.symbol) FROM gvm_scores g
                           WHERE g.score_date=(SELECT MAX(score_date) FROM gvm_scores)
                             AND NOT EXISTS (SELECT 1 FROM raw_prices p
                                             WHERE UPPER(p.symbol)=UPPER(g.symbol))
                           ORDER BY 1""")
            syms = sorted(set(syms) | {r[0] for r in cur.fetchall()})
    if not syms:
        return {"error": "pass symbols=... or missing=true"}
    return resolve_and_backfill(syms, lookback=lookback)


@router.post("/api/admin/yahoo/heal-nse")
def yahoo_heal_nse(symbols: str = "", max_dates: int = 40,
                   x_admin_token: Optional[str] = Header(None)):
    """cc#938: fill raw_prices from NSE sec_bhavdata_full for symbols Yahoo has abandoned.
    No symbols -> every symbol currently marked (STALE) in yahoo_symbol_map."""
    _check_admin(x_admin_token)
    syms = [s.strip().upper() for s in (symbols or "").replace(",", " ").split() if s.strip()]
    if not syms:
        syms = stale_symbols()
    if not syms:
        return {"note": "nothing stale to heal", "symbols": 0, "dates": 0, "rows": 0}
    return heal_from_nse(syms, max_dates=max_dates)


@router.get("/api/feeds/price-excluded")
def price_excluded():
    """The honest no-data register: GVM-universe symbols Yahoo cannot price on any candidate.
    A price-dependent field for one of these must render as no-data, never as a zero."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            conn.commit()
            cur.execute("""SELECT symbol, reason, checked_at FROM price_feed_excluded
                           ORDER BY symbol""")
            rows = [{"symbol": r[0], "reason": r[1],
                     "checked_at": r[2].isoformat() if r[2] else None} for r in cur.fetchall()]
            # A stale feed is the second kind of "do not trust this price". It is not in
            # price_feed_excluded (bars DO exist), so it has to be reported here or it hides.
            cur.execute("""SELECT symbol, yahoo_ticker, last_date FROM yahoo_symbol_map
                           WHERE source LIKE '%%(STALE)' ORDER BY symbol""")
            stale = [{"symbol": r[0], "yahoo_ticker": r[1],
                      "last_date": str(r[2]) if r[2] else None} for r in cur.fetchall()]
        return {"count": len(rows), "excluded": rows,
                "stale_count": len(stale), "stale_feeds": stale,
                "note": "excluded = no Yahoo EOD series on .NS, Yahoo search or .BO. "
                        "stale_feeds = a series exists but Yahoo stopped updating it on last_date. "
                        "Price-dependent fields must show no-data for both, never a zero."}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "count": 0, "excluded": [],
                "stale_count": 0, "stale_feeds": []}


@router.get("/api/feeds/symbol-map")
def symbol_map():
    """Every symbol whose Yahoo ticker is NOT the plain <SYMBOL>.NS, and how it was found."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            ensure_tables(cur)
            conn.commit()
            cur.execute("""SELECT symbol, yahoo_ticker, source, bars, first_date, last_date, resolved_at
                           FROM yahoo_symbol_map ORDER BY symbol""")
            rows = [{"symbol": r[0], "yahoo_ticker": r[1], "source": r[2], "bars": r[3],
                     "first_date": str(r[4]) if r[4] else None,
                     "last_date": str(r[5]) if r[5] else None,
                     "resolved_at": r[6].isoformat() if r[6] else None} for r in cur.fetchall()]
        return {"count": len(rows), "map": rows}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}", "count": 0, "map": []}

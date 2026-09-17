"""screeners_endpoints.py — cc#824 PREDEFINED SCREENERS API.

Read-only. The page renders what the nightly job stored; there is no run endpoint here, deliberately.
A Run button is what the founder explicitly removed: a screen should be a standing view of the
market, already computed, not a thing the reader has to trigger and wait for. Adding a run path here
later would also breach rule id=3069, which limits preset execution to the approved EOD batch.

  GET /api/screeners            -> the tab strip: every global preset + its stored member count
  GET /api/screeners/{id}       -> one screen's stored members, with live CMP/day% overlaid

CMP is resolved through cmp_resolver (cc#811), so the price a reader sees here is the same number
the GVM card and the Trade Check card show for that symbol at that moment — live tick during the
session, last tick after close, EOD only for symbols with no feed at all.
"""
import logging
import os

from fastapi import APIRouter, HTTPException
import psycopg

log = logging.getLogger("scorr.screeners")
router = APIRouter(tags=["screeners"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


# Column labels for the screen-relevant fields, so the page does not have to carry a second
# vocabulary. Anything not listed falls back to the raw key, which keeps a founder-added filter
# rendering sensibly without a code change.
FIELD_LABELS = {
    "gvm_score": "GVM", "g_score": "G", "v_score": "V", "m_score": "M",
    "market_cap": "MCap (Cr)", "roce": "ROCE %", "pe": "PE",
    "return_1y": "1Y %", "return_3y": "3Y %", "return_52w_vs_index": "vs Index",
    "week_return": "Wk %", "month_return": "Mo %", "year_return": "1Y %",
    "week_index_52": "52W Idx", "month_index": "Mo Idx",
    "vol_ratio_21": "Vol x21", "vol_ratio": "Vol x", "mom_2d": "2D Mom %",
    "dma_20": "vs 20DMA %", "dma_50": "vs 50DMA %", "dma_200": "vs 200DMA %",
    "rsi_month": "RSI (M)", "rsi_weekly": "RSI (W)", "daily_rsi": "RSI (D)",
    "day_1d": "Day %", "sector_week": "Sec Wk %", "sector_month": "Sec Mo %",
}

# cc#823 number contract, carried to this page: a field is either a LEVEL (integer) or a
# GROWTH/CHANGE (one decimal). Listed here rather than inferred, because the distinction is
# editorial — "52W Idx 96" is a position, "1Y % +18.4" is a rate.
GROWTH_FIELDS = {"return_1y", "return_3y", "return_52w_vs_index", "week_return", "month_return",
                 "year_return", "mom_2d", "day_1d", "sector_week", "sector_month",
                 "dma_20", "dma_50", "dma_200"}


@router.get("/api/screeners")
def screeners_list():
    """Tab strip: every global preset with its stored member count and last run date."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            # cc#1519: source discriminates the two tab rows — 'scorr' (algorithmic, real filters)
            # vs 'finz' (static Finkhoz imports, filters={} by construction, cc#1517/1518). Derived
            # from filters because the planned `source` COLUMN is an ALTER TABLE, which the
            # MAINTENANCE_LOCK_RULE holds for a weekend Railway-console run — when that lands,
            # this CASE becomes COALESCE(p.source, ...) with identical output. The page renders
            # the scorr block first, then finz, alphabetical within each.
            cur.execute("""
                SELECT p.id, p.name, p.sort_key, p.sort_dir,
                       COALESCE(r.n, 0) AS members, r.last_seen,
                       CASE WHEN p.filters = '{}'::jsonb THEN 'finz' ELSE 'scorr' END AS source
                FROM v13_presets p
                LEFT JOIN (SELECT screen_id, COUNT(*) AS n, MAX(last_seen) AS last_seen
                           FROM v13_screen_results GROUP BY screen_id) r ON r.screen_id = p.id
                WHERE COALESCE(p.scope,'global')='global'
                ORDER BY CASE WHEN p.filters = '{}'::jsonb THEN 1 ELSE 0 END, p.name
            """)
            rows = [{"id": r[0], "name": r[1], "sort_key": r[2], "sort_dir": r[3],
                     "members": int(r[4] or 0), "last_run": str(r[5]) if r[5] else None,
                     "source": r[6]}
                    for r in cur.fetchall()]
            # cc#1677: `newest` — up to 3 symbols with the most recent first_seen per screen, for
            # the Model Portfolio pane's Screeners table (its own row without a per-screen detail
            # fetch: read-only, one round trip, no new formula — the same rows screener_detail()
            # already returns, just the newest slice of them). ROW_NUMBER over first_seen DESC so
            # the LIMIT is per screen_id, not a single global top-3.
            cur.execute("""
                SELECT screen_id, symbol FROM (
                    SELECT screen_id, symbol,
                           ROW_NUMBER() OVER (PARTITION BY screen_id ORDER BY first_seen DESC, symbol) AS rn
                    FROM v13_screen_results
                ) t WHERE rn <= 3
                ORDER BY screen_id, rn
            """)
            newest_map = {}
            for sid, sym in cur.fetchall():
                newest_map.setdefault(sid, []).append(sym)
            for r in rows:
                r["newest"] = newest_map.get(r["id"], [])
        # never_run is the honest state for a fresh deploy: the tab exists, the table is empty, and
        # the page says why rather than rendering an ambiguous blank.
        return {"status": "ok", "screens": rows,
                "never_run": all(x["last_run"] is None for x in rows) if rows else True}
    except Exception as e:
        raise HTTPException(500, f"screeners_list failed: {e}")


@router.get("/api/screeners/{screen_id}")
def screener_detail(screen_id: int):
    """One screen's stored members + live CMP/day% + the fields it actually screened on."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, filters, sort_key FROM v13_presets WHERE id=%s", (screen_id,))
            p = cur.fetchone()
            if not p:
                raise HTTPException(404, f"screen {screen_id} not found")
            _id, name, filters, sort_key = p
            cur.execute("""SELECT symbol, first_seen, last_seen, snapshot, rank
                           FROM v13_screen_results WHERE screen_id=%s
                           ORDER BY rank NULLS LAST, symbol""", (screen_id,))
            members = cur.fetchall()

            # Column set = the preset's OWN filter keys, in the preset's order. A screen shows the
            # numbers it screened on; that is what makes the table readable without a legend.
            keys = [k for k in (filters or {}).keys()]
            cols = [{"key": k, "label": FIELD_LABELS.get(k, k),
                     "fmt": "growth" if k in GROWTH_FIELDS else "level"} for k in keys]

            rows = []
            for sym, first_seen, last_seen, snap, rank in members:
                rows.append({"symbol": sym, "rank": rank, "first_seen": str(first_seen),
                             "last_seen": str(last_seen), "metrics": snap or {}})

            # cc#811: one batch resolver pass for the whole table — per-row resolution would be four
            # queries per symbol and this page can carry hundreds of rows.
            if rows:
                try:
                    import cmp_resolver
                    res = cmp_resolver.resolve_cmp_many(cur, [r["symbol"] for r in rows])
                    for r in rows:
                        q = res.get(r["symbol"].upper()) or {}
                        r["cmp"] = q.get("cmp")
                        r["day_pct"] = q.get("day_pct")
                        r["live"] = bool(q.get("live"))
                except Exception as e:
                    log.warning(f"screener_detail cmp overlay: {e}")

            # cc#2133: WEEK% / MONTH% for the screen's own rows -- a per-request calc over this small
            # row set (a screen is a few dozen symbols), NOT a precompute. Same price source the
            # page's other numbers rest on (raw_prices closes), anchored on each symbol's OWN last
            # traded date exactly the way invest_check_v2._ret_over anchors: base = the last close on
            # or before (last_date - 7 / 30 calendar days). A symbol without a close that far back
            # gets null, which the page renders as '--' -- never a number made up for missing
            # history. v8_metrics' week_return/month_return is NOT used: it covers the ~209 F&O names
            # only, and this population is the full Screeners universe.
            if rows:
                try:
                    cur.execute("""
                        WITH syms AS (SELECT unnest(%s::text[]) AS symbol),
                        last AS (SELECT r.symbol, MAX(r.price_date) AS d0 FROM raw_prices r
                                 JOIN syms s ON s.symbol = r.symbol WHERE r.close > 0 GROUP BY r.symbol),
                        c0 AS (SELECT r.symbol, l.d0, r.close AS c0 FROM raw_prices r
                               JOIN last l ON l.symbol = r.symbol AND r.price_date = l.d0),
                        w AS (SELECT DISTINCT ON (r.symbol) r.symbol, r.close AS cw FROM raw_prices r
                              JOIN last l ON l.symbol = r.symbol
                              WHERE r.close > 0 AND r.price_date <= l.d0 - INTERVAL '7 days'
                              ORDER BY r.symbol, r.price_date DESC),
                        m AS (SELECT DISTINCT ON (r.symbol) r.symbol, r.close AS cm FROM raw_prices r
                              JOIN last l ON l.symbol = r.symbol
                              WHERE r.close > 0 AND r.price_date <= l.d0 - INTERVAL '30 days'
                              ORDER BY r.symbol, r.price_date DESC)
                        SELECT c0.symbol, c0.d0,
                               ROUND(((c0.c0 / NULLIF(w.cw, 0)) - 1) * 100, 2) AS week_pct,
                               ROUND(((c0.c0 / NULLIF(m.cm, 0)) - 1) * 100, 2) AS month_pct
                        FROM c0 LEFT JOIN w ON w.symbol = c0.symbol LEFT JOIN m ON m.symbol = c0.symbol
                    """, ([r["symbol"].upper() for r in rows],))
                    ret = {sym: (d0, wk, mo) for sym, d0, wk, mo in cur.fetchall()}
                    for r in rows:
                        d0, wk, mo = ret.get(r["symbol"].upper(), (None, None, None))
                        r["week_pct"] = float(wk) if wk is not None else None
                        r["month_pct"] = float(mo) if mo is not None else None
                        r["ret_as_of"] = str(d0) if d0 else None
                except Exception as e:
                    conn.rollback()
                    log.warning(f"screener_detail week/month overlay: {e}")

            # cc#2134: Investment Score overlay -- score10 + band from investment_check_v2_scores at
            # its LATEST score_date, joined by symbol. A symbol with no row (first day, or outside
            # the GVM universe) gets null, which the page renders as '--'. Never a computed value.
            inv_date = None
            if rows:
                try:
                    cur.execute("SELECT MAX(score_date) FROM investment_check_v2_scores")
                    inv_date = cur.fetchone()[0]
                    if inv_date:
                        cur.execute("""SELECT symbol, score10, band FROM investment_check_v2_scores
                                       WHERE score_date=%s AND symbol = ANY(%s)""",
                                    (inv_date, [r["symbol"].upper() for r in rows]))
                        inv = {s: (sc, b) for s, sc, b in cur.fetchall()}
                        for r in rows:
                            sc, b = inv.get(r["symbol"].upper(), (None, None))
                            r["inv_score"] = float(sc) if sc is not None else None
                            r["inv_band"] = b
                except Exception as e:
                    conn.rollback()
                    log.warning(f"screener_detail inv score overlay: {e}")
        return {"status": "ok", "id": _id, "name": name, "sort_key": sort_key,
                "columns": cols, "rows": rows, "members": len(rows),
                "inv_score_date": str(inv_date) if inv_date else None}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"screener_detail failed: {e}")

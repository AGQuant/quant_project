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
        return {"status": "ok", "id": _id, "name": name, "sort_key": sort_key,
                "columns": cols, "rows": rows, "members": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"screener_detail failed: {e}")

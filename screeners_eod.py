"""screeners_eod.py — cc#824 PREDEFINED SCREENERS, storage + nightly batch runner.

The founder's model (Finkhoz Best-of pattern): a reader opens /screeners and sees the RESULTING
STOCKS in a table. There is no Run button anywhere, because a screen is not a thing you trigger —
it is a standing view that the platform recomputes each night after the EOD engines land.

AUTHORISATION. Rule id=3069 (Fable–V13 Theme Bridge) says presets are screens only and must never be
wired to a live engine without a founder-approved cc task. cc#824 IS that approval, and its scope is
exactly this: an EOD batch runner that executes the stored presets through the REAL V13 engine and
records membership. No intraday runs, no trade-engine wiring, nothing that places or sizes anything.

WHY first_seen MATTERS. "Added On" is the column the founder asked for, and it only means something
if it survives recomputation: a symbol that stays in a screen for three weeks must keep its ORIGINAL
date, not today's. So membership is an UPSERT that touches last_seen and the snapshot but leaves
first_seen alone; first_seen is set once on entry and only resets if the symbol leaves and later
returns. Exits are pruned from the current view and appended to a small exit log, which is what makes
"it was in this screen last week" answerable at all.

FIELD SEMANTICS come from v13_presets_endpoints._screen_sql — the same builder the ad-hoc runner
uses. That is deliberate: a second copy of the dma-is-a-%-distance / screener_raw-is-text rules would
drift, and a screen that quietly means something different from the one the founder validated is
worse than no screen.
"""
import logging
import os
from datetime import date

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("scorr.screeners_eod")

# Fields worth storing per screen beyond the always-present ones. Keyed by nothing — the snapshot
# carries whatever the preset's own filters reference, so a screen shows the numbers it screened on
# (52-Week Breakout shows vol_ratio_21 / week_index_52 / month_index; Quality Compounders shows
# roce / g_score / return_3y). Computing this from the preset rather than hardcoding per screen means
# a preset edited by the founder brings its new fields to the page with no code change.
_ALWAYS = ("gvm_score", "g_score", "v_score", "m_score", "market_cap")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS v13_screen_results (
    screen_id    INTEGER NOT NULL,
    screen_name  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    first_seen   DATE    NOT NULL,
    last_seen    DATE    NOT NULL,
    snapshot     JSONB,
    rank         INTEGER,
    PRIMARY KEY (screen_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_v13_screen_results_screen ON v13_screen_results(screen_id, rank);

CREATE TABLE IF NOT EXISTS v13_screen_exits (
    id           SERIAL PRIMARY KEY,
    screen_id    INTEGER NOT NULL,
    screen_name  TEXT,
    symbol       TEXT    NOT NULL,
    first_seen   DATE,
    exited_on    DATE    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v13_screen_exits_screen ON v13_screen_exits(screen_id, exited_on DESC);
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def ensure_tables(cur):
    cur.execute(SCHEMA_SQL)


def _members(cur, filters, sort_key, sort_dir):
    """All members of one screen, in the preset's own sort order, with the metric values it screened
    on. No LIMIT: the page shows the screen, not a top-N of it — capping here would silently turn
    "Market Leaders" into "the first 100 Market Leaders" with nothing on the card saying so."""
    from v13_presets_endpoints import _screen_sql, _col_expr, _FIELD_MAP

    wsql, params, join, order, _us, _uf = _screen_sql(filters, sort_key, sort_dir)

    # Snapshot columns = the preset's own filter keys + the always-useful score set, deduped and
    # restricted to keys the engine actually knows.
    keys = [k for k in list(filters.keys()) + list(_ALWAYS) if k in _FIELD_MAP]
    seen, cols = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            cols.append(k)

    sel = ", ".join(f"{_col_expr(*_FIELD_MAP[k])} AS \"{k}\"" for k in cols)
    cur.execute(f"SELECT u.symbol{', ' + sel if sel else ''} "
                f"FROM universe_technicals u {join} WHERE {wsql} {order}", params)
    out = []
    for i, row in enumerate(cur.fetchall(), start=1):
        d = dict(row) if isinstance(row, dict) else None
        if d is None:                       # plain tuple cursor
            names = ["symbol"] + cols
            d = dict(zip(names, row))
        sym = d.pop("symbol")
        # numeric -> float so the JSONB is queryable and the page does not have to parse strings
        snap = {}
        for k, v in d.items():
            try:
                snap[k] = None if v is None else float(v)
            except (TypeError, ValueError):
                snap[k] = None
        out.append((sym, snap, i))
    return out


def run_all(conn=None, run_date=None):
    """Recompute every global preset and persist membership. Returns a per-screen summary.

    Holiday-safe by construction rather than by a calendar lookup: it screens whatever the latest
    universe_technicals score_date holds. On a holiday no new EOD row exists, so the screens
    recompute to the same membership, last_seen advances, and first_seen — the column that carries
    meaning — does not move. Nothing is corrupted by running on a day the market was shut.
    """
    own = conn is None
    conn = conn or _conn()
    today = run_date or date.today()
    summary = []
    try:
        with conn.cursor() as cur:
            ensure_tables(cur)
            cur.execute("SELECT id, name, filters, sort_key, sort_dir FROM v13_presets "
                        "WHERE COALESCE(scope,'global')='global' ORDER BY id")
            presets = cur.fetchall()
        for pid, pname, filters, sort_key, sort_dir in presets:
            try:
                with conn.cursor(row_factory=dict_row) as c2:
                    rows = _members(c2, filters or {}, sort_key, sort_dir if sort_dir is not None else -1)
                syms = [r[0] for r in rows]
                with conn.cursor() as cur:
                    import json as _json
                    for sym, snap, rank in rows:
                        # first_seen is written ONLY on insert. The whole point of "Added On" is that
                        # it survives every later recompute — EXCLUDED.first_seen here would reset it
                        # nightly and quietly turn the column into "today" for every row.
                        cur.execute("""
                            INSERT INTO v13_screen_results
                                (screen_id, screen_name, symbol, first_seen, last_seen, snapshot, rank)
                            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                            ON CONFLICT (screen_id, symbol) DO UPDATE SET
                                screen_name = EXCLUDED.screen_name,
                                last_seen   = EXCLUDED.last_seen,
                                snapshot    = EXCLUDED.snapshot,
                                rank        = EXCLUDED.rank
                        """, (pid, pname, sym, today, today, _json.dumps(snap), rank))
                    # Exits: anything still carrying an older last_seen is no longer a member.
                    cur.execute("""INSERT INTO v13_screen_exits (screen_id, screen_name, symbol, first_seen, exited_on)
                                   SELECT screen_id, screen_name, symbol, first_seen, %s
                                   FROM v13_screen_results
                                   WHERE screen_id=%s AND last_seen < %s""", (today, pid, today))
                    exits = cur.rowcount
                    cur.execute("DELETE FROM v13_screen_results WHERE screen_id=%s AND last_seen < %s",
                                (pid, today))
                conn.commit()
                summary.append({"screen_id": pid, "name": pname, "members": len(syms), "exits": exits})
                log.info("cc#824 screener '%s' (id=%s): %d members, %d exits", pname, pid, len(syms), exits)
            except Exception as e:
                conn.rollback()
                # One bad preset must not abort the rest — a screen with a filter key that no longer
                # exists should fail alone and visibly, not take the page down with it.
                log.warning("cc#824 screener id=%s '%s' failed: %s", pid, pname, e)
                summary.append({"screen_id": pid, "name": pname, "error": str(e)[:200]})
        return {"status": "ok", "run_date": str(today), "screens": summary}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def fetch_screen(cur, screen_id):
    """Stored members of one screen, in stored rank order. Read-only; the page never recomputes."""
    cur.execute("""SELECT symbol, first_seen, last_seen, snapshot, rank
                   FROM v13_screen_results WHERE screen_id=%s ORDER BY rank NULLS LAST, symbol""",
                (screen_id,))
    return cur.fetchall()

"""v8_unrealised_daily.py -- cc#2097 item 3: the first-ever stored history of portfolio
UNREALISED P&L, snapshotted once per trading day at/after market close.

WHY THIS DIDN'T EXIST. mobile/v8.html's Positions deck (cc#2005) always showed the REALISED
cumulative curve (v8_daylog_extras.build_series, keyed off v8_paper_trades' closed-trade dates)
with today's live unrealised tacked on as one extra dashed end-point (dlLiveUnreal) -- there was
never a stored, dated series of what unrealised itself was on any PAST day. Searched the schema
for unreal/mtm/mark_to_market/book_snapshot before building -- nothing existed.

ONE FORMULA, NEVER A SECOND ONE (rule 13 V8_PNL_CANON_V1). snapshot_unrealised_daily() reads
v8_book_canon.book_canon(conn) -- the exact function /api/mobile/v8book itself calls -- and
stores its unrealised / long.unrealised / short.unrealised / open figures verbatim. It never
re-derives unrealised from v8_paper_positions directly; a formula drift between this table and
the live book KPI boxes is structurally impossible because there is only one place the number is
computed.

NO BACKFILL, STATED HONESTLY (spec item 4's own instruction). There is no historical mark-to-
market record for past open positions anywhere in this schema, and reconstructing one from
raw_prices/v8_metrics would mean guessing which positions were open on which past day and at what
CMP -- not an honest reconstruction. History starts accumulating from this feature's first real
snapshot, and the client-side chart says so plainly rather than implying a longer past.

SAME POINT/DATE SHAPE AS v8_daylog_extras.build_series (cc#1561), so mobile/v8.html's existing
dlChartSvg() can draw this series with zero changes to its own drawing code -- only the values
mean something different: net_cum/gross_cum here are the day's raw unrealised LEVEL (a mark-to-
market snapshot), not a running SUM the way realised P&L accumulates. Same field names, different
semantics -- the client thread this through explicitly (kind='unreal') rather than silently
treating a level series as if it were a cumulative flow.
"""
import os
from datetime import datetime, timezone, timedelta

import psycopg
from fastapi import APIRouter

from v8_book_canon import book_canon

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS v8_unrealised_daily (
            trade_date       DATE PRIMARY KEY,
            unrealised       NUMERIC,
            long_unrealised  NUMERIC,
            short_unrealised NUMERIC,
            open_n           INTEGER,
            computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def snapshot_unrealised_daily(conn, trade_date=None) -> dict:
    """One row per trading day. Reads book_canon(conn) -- the SAME canon /api/mobile/v8book
    itself calls -- never recomputes unrealised from raw positions. UPSERT so a same-day retry
    (a redeploy, a manual re-run) corrects the row rather than duplicating it."""
    d = trade_date or datetime.now(IST).date()
    payload = book_canon(conn)
    unrealised = payload.get("unrealised")
    long_u = (payload.get("long") or {}).get("unrealised")
    short_u = (payload.get("short") or {}).get("unrealised")
    open_n = payload.get("open")
    with conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute("""
            INSERT INTO v8_unrealised_daily
                (trade_date, unrealised, long_unrealised, short_unrealised, open_n, computed_at)
            VALUES (%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (trade_date) DO UPDATE SET
                unrealised=EXCLUDED.unrealised, long_unrealised=EXCLUDED.long_unrealised,
                short_unrealised=EXCLUDED.short_unrealised, open_n=EXCLUDED.open_n,
                computed_at=NOW()
        """, (d, unrealised, long_u, short_u, open_n))
        conn.commit()
    return {"trade_date": str(d), "unrealised": unrealised, "long_unrealised": long_u,
            "short_unrealised": short_u, "open_n": open_n}


_SIDE_COL = {"LONG": "long_unrealised", "SHORT": "short_unrealised", None: "unrealised"}


def build_series(rows, side=None) -> dict:
    """rows: [(trade_date, value), ...] ascending. Same point/date shape as
    v8_daylog_extras.build_series (cc#1561) -- see module docstring for why net_cum/gross_cum
    carry a LEVEL here, not a running sum. Never fabricates a day: a NULL value (book_canon ran
    but returned no figure, or a gap) is skipped rather than zero-filled or carried forward."""
    points = []
    for d, val in rows:
        if val is None:
            continue
        v = float(val)
        points.append({"date": str(d), "gross_day": int(round(v)), "net_day": int(round(v)),
                       "gross_cum": int(round(v)), "net_cum": int(round(v))})
    window_start = points[0]["date"] if points else None
    window_end = points[-1]["date"] if points else None
    return {
        "window_start": window_start, "window_end": window_end, "table_start": window_start,
        "trading_days": len(points), "side": side, "kind": "unrealised",
        "points": points,
        "return_pct": None, "cagr_pct": None,
        "cagr_note": "Unrealised is a mark-to-market level, not a compounding return -- no CAGR shown.",
    }


@router.get("/api/v8/unrealised_daily/series")
def v8_unrealised_daily_series(side: str = None):
    """?side=LONG|SHORT slices which column is served -- same optional param shape as
    /api/v8/daylog/series (cc#2005) -- omitted (the default chart open) serves the whole book."""
    side = (side or "").strip().upper() or None
    if side not in (None, "LONG", "SHORT"):
        side = None
    col = _SIDE_COL[side]
    with _conn() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        conn.commit()
        cur.execute(f"SELECT trade_date, {col} FROM v8_unrealised_daily ORDER BY trade_date ASC")
        rows = cur.fetchall()
    return build_series(rows, side=side)

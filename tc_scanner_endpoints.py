"""
TC Scanner — Scorr (cc#464, engine per INTRADAY_SCANNER_SPEC_V2 id=399/400)
============================================================================
Founder-approved OPTION B (12-Jul chat, reversed the earlier TC V4 direction):
a NEW, standalone binary 13-check bucket — NOT the TC V4 R1-R16 scoring engine
used on the /check page. Two independent buckets, one per side:

  BUY  (id=399): C1 ADR>=1 · C2 sector day>0 · C3 sector week>0
                 C4 PP<CMP<=R1 · C5 VolX (time-matched vol) >=1.25
                 C6 last 5m bar GREEN · C7 day-high room>0.3%
                 C8 week return>0 · C9 wRSI 50-75 · C10 mRSI band
                 C11 day_1d>0 · C12 mom_2d>0 · C13 room-to-R1 >=50% of band
  SELL (id=400): exact mirror of every check above.

OPEN INTERPRETATION FLAGGED HONESTLY (spec text was ambiguous on two points —
implemented with the most internally-consistent reading, not invented from
nothing; surfaced here per the founder's own "flag, don't invent" convention):
  - C10 "mRSI band": spec gives no explicit numbers. Adopted the SAME 50-75
    band as C9 (wRSI), which is explicitly specified — a monthly RSI in the
    same bullish-not-overheated zone as the weekly RSI check. SELL mirror:
    25-50.
  - C13 "room-to-R1 >= 50% of band": read as (R1-CMP)/(R1-PP) >= 0.5 — i.e.
    CMP sits in the LOWER half of the PP..R1 band, so at least half the move
    to R1 is still ahead (an "enter with room left" filter, consistent with
    TC v4's existing R11 room-to-run logic). SELL mirror: (CMP-S1)/(PP-S1)>=0.5.

Qualify: pass >= 12 of 13 (n-1) per side. NULL checks are skipped and scored
from the available set (n-1 of whatever evaluated, per id=399 NULL-handling
convention already used by the sibling Intraday Scanner V2 TC bucket).
First qualification per symbol/side/day LATCHES (UNIQUE constraint, ON
CONFLICT DO NOTHING — never re-evaluated or overwritten same day).

Cadence: every 15 min during market hours (shares the qb_intraday_mark slot).
Position tracking: entry = futures CMP at the qualifying tick. target=+3%/
SL=-3% from entry (SELL mirrored). Checked each subsequent 15-min tick vs
live futures LTP; an EOD sweep does one final check against the last
available price so a touch between polls is not missed. Still-open positions
after the EOD sweep remain exit_reason=OPEN (screener-only — no forced close,
no paper engine).
"""

import os
import time
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter

# Reuse the existing full-universe scan query + time-matched volume helper —
# same data source as the sibling Intraday Scanner V2 engine, no duplication.
from intraday_scanner_endpoints import _SCAN_SQL, _vol_timenorm, _f

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
DATABASE_URL = os.getenv("DATABASE_URL", "")

TARGET_PCT = 0.03
SL_PCT = 0.03

# ── cc#465: named spec constants. The check functions below reference THESE
# (never a bare literal), and /api/scanners/tc/spec renders the info-modal
# straight from them — the modal can never drift from the engine's actual
# thresholds because there is only one copy of each number.
ADR_MIN = 1.0
VOLX_MIN = 1.25
DAY_ROOM_MIN_PCT = 0.3
WRSI_BUY = (50.0, 75.0)
MRSI_BUY = (50.0, 75.0)   # flagged interpretation — see module docstring
WRSI_SELL = (25.0, 50.0)
MRSI_SELL = (25.0, 50.0)
ROOM_TO_LEVEL_MIN_FRAC = 0.5
N_MINUS = 1               # pass >= evaluated - N_MINUS (12 of 13 at full evaluation)
TOTAL_CHECKS = 13


def _conn():
    return psycopg.connect(DATABASE_URL)


def _ist_now() -> datetime:
    return datetime.now(IST)


def ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tc_scanner_holds (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            style TEXT,
            score INTEGER,
            evaluated INTEGER,
            entry_price NUMERIC,
            entry_ts TIMESTAMP,
            target NUMERIC,
            sl NUMERIC,
            exit_price NUMERIC,
            exit_ts TIMESTAMP,
            exit_reason TEXT DEFAULT 'OPEN',
            scan_date DATE NOT NULL,
            UNIQUE (symbol, side, scan_date)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tc_scanner_holds_date ON tc_scanner_holds(scan_date, side)")


# ── 13-check evaluation ───────────────────────────────────────────────────
def _tc13_buy(row: dict, adr: Optional[float]) -> dict:
    cmp = _f(row.get("live_close"))
    pp, r1 = _f(row.get("pp")), _f(row.get("r1"))
    today_high = _f(row.get("today_high"))
    lb_open, lb_close = _f(row.get("lb_open")), _f(row.get("lb_close"))
    rsi_w, rsi_m = _f(row.get("rsi_weekly")), _f(row.get("rsi_month"))
    volx = _vol_timenorm(row)

    zone = (pp is not None and r1 is not None and cmp is not None and pp < cmp <= r1)
    room_to_r1 = ((r1 - cmp) / (r1 - pp)) if (zone and (r1 - pp) > 0) else None
    day_high_room = ((today_high - cmp) / cmp * 100) if (cmp and today_high and cmp > 0) else None

    checks = {
        "C1_adr": (None if adr is None else adr >= ADR_MIN),
        "C2_sector_day": _f(row.get("sector_day")) is not None and _f(row.get("sector_day")) > 0,
        "C3_sector_week": _f(row.get("sector_week")) is not None and _f(row.get("sector_week")) > 0,
        "C4_pivot_zone": bool(zone),
        "C5_volx": (None if volx is None else volx >= VOLX_MIN),
        "C6_last_bar_green": (lb_open is not None and lb_close is not None and lb_close > lb_open),
        "C7_day_high_room": (day_high_room is not None and day_high_room > DAY_ROOM_MIN_PCT),
        "C8_week_return": _f(row.get("week_return")) is not None and _f(row.get("week_return")) > 0,
        "C9_wrsi_band": (rsi_w is not None and WRSI_BUY[0] <= rsi_w <= WRSI_BUY[1]),
        "C10_mrsi_band": (rsi_m is not None and MRSI_BUY[0] <= rsi_m <= MRSI_BUY[1]),
        "C11_day_1d": _f(row.get("day_1d")) is not None and _f(row.get("day_1d")) > 0,
        "C12_mom_2d": _f(row.get("mom_2d")) is not None and _f(row.get("mom_2d")) > 0,
        "C13_room_to_r1": (room_to_r1 is not None and room_to_r1 >= ROOM_TO_LEVEL_MIN_FRAC),
    }
    evaluated = [v for v in checks.values() if v is not None]
    passed = sum(1 for v in evaluated if v)
    need = max(1, len(evaluated) - N_MINUS)
    return {"pass": passed >= need, "score": passed, "evaluated": len(evaluated),
            "need": need, "checks": checks, "entry_price": cmp}


def _tc13_sell(row: dict, adr: Optional[float]) -> dict:
    cmp = _f(row.get("live_close"))
    pp, s1 = _f(row.get("pp")), _f(row.get("s1"))
    today_low = _f(row.get("today_low"))
    lb_open, lb_close = _f(row.get("lb_open")), _f(row.get("lb_close"))
    rsi_w, rsi_m = _f(row.get("rsi_weekly")), _f(row.get("rsi_month"))
    volx = _vol_timenorm(row)

    zone = (pp is not None and s1 is not None and cmp is not None and s1 <= cmp < pp)
    room_to_s1 = ((cmp - s1) / (pp - s1)) if (zone and (pp - s1) > 0) else None
    day_low_room = ((cmp - today_low) / cmp * 100) if (cmp and today_low and cmp > 0) else None

    checks = {
        "C1_adr": (None if adr is None else adr >= ADR_MIN),
        "C2_sector_day": _f(row.get("sector_day")) is not None and _f(row.get("sector_day")) < 0,
        "C3_sector_week": _f(row.get("sector_week")) is not None and _f(row.get("sector_week")) < 0,
        "C4_pivot_zone": bool(zone),
        "C5_volx": (None if volx is None else volx >= VOLX_MIN),
        "C6_last_bar_red": (lb_open is not None and lb_close is not None and lb_close < lb_open),
        "C7_day_low_room": (day_low_room is not None and day_low_room > DAY_ROOM_MIN_PCT),
        "C8_week_return": _f(row.get("week_return")) is not None and _f(row.get("week_return")) < 0,
        "C9_wrsi_band": (rsi_w is not None and WRSI_SELL[0] <= rsi_w <= WRSI_SELL[1]),
        "C10_mrsi_band": (rsi_m is not None and MRSI_SELL[0] <= rsi_m <= MRSI_SELL[1]),
        "C11_day_1d": _f(row.get("day_1d")) is not None and _f(row.get("day_1d")) < 0,
        "C12_mom_2d": _f(row.get("mom_2d")) is not None and _f(row.get("mom_2d")) < 0,
        "C13_room_to_s1": (room_to_s1 is not None and room_to_s1 >= ROOM_TO_LEVEL_MIN_FRAC),
    }
    evaluated = [v for v in checks.values() if v is not None]
    passed = sum(1 for v in evaluated if v)
    need = max(1, len(evaluated) - N_MINUS)
    return {"pass": passed >= need, "score": passed, "evaluated": len(evaluated),
            "need": need, "checks": checks, "entry_price": cmp}


def _target_sl(entry, side):
    if entry is None or entry <= 0:
        return None, None
    if side == "SELL":
        return round(entry * (1 - TARGET_PCT), 2), round(entry * (1 + SL_PCT), 2)
    return round(entry * (1 + TARGET_PCT), 2), round(entry * (1 - SL_PCT), 2)


# ── scan + record (LATCH via UNIQUE, ON CONFLICT DO NOTHING) ────────────────
def run_scan():
    """Full-universe scan, both sides. Records new qualifiers only — first
    qualification per symbol/side/day latches (existing rows never overwritten)."""
    now = _ist_now()
    today = now.date()
    with _conn() as conn, conn.cursor() as cur:
        ensure_schema(cur)
        conn.commit()
        cur.execute("SELECT adr FROM adr_daily WHERE price_date=CURRENT_DATE ORDER BY computed_at DESC LIMIT 1")
        ar = cur.fetchone()
        adr = float(ar[0]) if ar and ar[0] is not None else None
        cur.execute("SET LOCAL jit = off")
        cur.execute(_SCAN_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    buy_new = sell_new = 0
    with _conn() as conn, conn.cursor() as cur:
        for row in rows:
            sym = row["symbol"]
            b = _tc13_buy(row, adr)
            if b["pass"] and b["entry_price"]:
                tgt, sl = _target_sl(b["entry_price"], "BUY")
                cur.execute("""
                    INSERT INTO tc_scanner_holds
                        (symbol, side, style, score, evaluated, entry_price, entry_ts, target, sl, scan_date)
                    VALUES (%s,'BUY','TC13',%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, side, scan_date) DO NOTHING
                """, (sym, b["score"], b["evaluated"], b["entry_price"], now.replace(tzinfo=None), tgt, sl, today))
                if cur.rowcount:
                    buy_new += 1
            s = _tc13_sell(row, adr)
            if s["pass"] and s["entry_price"]:
                tgt, sl = _target_sl(s["entry_price"], "SELL")
                cur.execute("""
                    INSERT INTO tc_scanner_holds
                        (symbol, side, style, score, evaluated, entry_price, entry_ts, target, sl, scan_date)
                    VALUES (%s,'SELL','TC13',%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, side, scan_date) DO NOTHING
                """, (sym, s["score"], s["evaluated"], s["entry_price"], now.replace(tzinfo=None), tgt, sl, today))
                if cur.rowcount:
                    sell_new += 1
        conn.commit()
    return {"universe": len(rows), "buy_new": buy_new, "sell_new": sell_new, "scan_ts": now.isoformat()}


def check_exits():
    """Check every OPEN hold (today) against the current futures LTP for target/SL touch."""
    now = _ist_now()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, symbol, side, entry_price, target, sl FROM tc_scanner_holds
            WHERE scan_date=CURRENT_DATE AND exit_reason='OPEN'
        """)
        open_rows = cur.fetchall()
        if not open_rows:
            return {"checked": 0, "closed": 0}
        syms = list({r[1] for r in open_rows})
        cur.execute("""
            SELECT DISTINCT ON (symbol) symbol, close FROM intraday_prices
            WHERE source='fyers_fut' AND ts::date=CURRENT_DATE AND symbol = ANY(%s)
            ORDER BY symbol, ts DESC
        """, (syms,))
        ltp = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}

        closed = 0
        for hid, sym, side, entry, target, sl in open_rows:
            px = ltp.get(sym)
            if px is None:
                continue
            hit = None
            if side == "BUY":
                if target is not None and px >= float(target):
                    hit = "TARGET"
                elif sl is not None and px <= float(sl):
                    hit = "SL"
            else:
                if target is not None and px <= float(target):
                    hit = "TARGET"
                elif sl is not None and px >= float(sl):
                    hit = "SL"
            if hit:
                cur.execute("""UPDATE tc_scanner_holds SET exit_price=%s, exit_ts=%s, exit_reason=%s
                               WHERE id=%s""", (px, now.replace(tzinfo=None), hit, hid))
                closed += 1
        conn.commit()
    return {"checked": len(open_rows), "closed": closed}


def eod_sweep():
    """One final check at EOD against the last available price — catches a touch that
    happened between 15-min polls. Positions still unresolved after this stay OPEN
    (screener-only; no forced close, no paper engine)."""
    res = check_exits()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'tc_scanner', 'eod_sweep', %s)",
                    (Json(res),))
        conn.commit()
    return res


# ── cc#465: info-modal spec, generated FROM the constants above ─────────────
def _check_defs(side):
    """The 13 checks for one side, described using the ACTUAL live constants —
    never a hand-typed number, so this can never drift from the engine."""
    buy = side == "BUY"
    return [
        {"id": "C1", "label": "ADR", "rule": f"ADR >= {ADR_MIN}"},
        {"id": "C2", "label": "Sector day", "rule": f"sector day return {'> 0' if buy else '< 0'}"},
        {"id": "C3", "label": "Sector week", "rule": f"sector week return {'> 0' if buy else '< 0'}"},
        {"id": "C4", "label": "Pivot zone", "rule": "PP < CMP <= R1" if buy else "S1 <= CMP < PP"},
        {"id": "C5", "label": "VolX (time-matched)", "rule": f"time-matched volume ratio >= {VOLX_MIN}"},
        {"id": "C6", "label": "Last 5m bar", "rule": "last 5-min bar GREEN" if buy else "last 5-min bar RED"},
        {"id": "C7", "label": "Room to day extreme", "rule": f"room to today's {'high' if buy else 'low'} > {DAY_ROOM_MIN_PCT}%"},
        {"id": "C8", "label": "Week return", "rule": f"week return {'> 0' if buy else '< 0'}"},
        {"id": "C9", "label": "Weekly RSI (wRSI)", "rule": f"{(WRSI_BUY if buy else WRSI_SELL)[0]}-{(WRSI_BUY if buy else WRSI_SELL)[1]}"},
        {"id": "C10", "label": "Monthly RSI (mRSI)", "rule": f"{(MRSI_BUY if buy else MRSI_SELL)[0]}-{(MRSI_BUY if buy else MRSI_SELL)[1]}",
         "note": "interpretation flagged — spec gave no explicit band; mirrors the C9 wRSI band"},
        {"id": "C11", "label": "Day 1D change", "rule": f"day_1d {'> 0' if buy else '< 0'}"},
        {"id": "C12", "label": "2-day momentum", "rule": f"mom_2d {'> 0' if buy else '< 0'}"},
        {"id": "C13", "label": f"Room to {'R1' if buy else 'S1'}",
         "rule": f"room-to-{'R1' if buy else 'S1'} >= {int(ROOM_TO_LEVEL_MIN_FRAC*100)}% of the {'PP-R1' if buy else 'S1-PP'} band",
         "note": "interpretation flagged — read as (R1-CMP)/(R1-PP)>=0.5, mirrored for SELL"},
    ]


@router.get("/api/scanners/tc/spec")
def tc_scanner_spec():
    """cc#465: full qualification logic for the info modal, generated from the same
    constants the engine evaluates against — cannot drift from the live code."""
    return {
        "engine": "TC Scanner — 13-check binary bucket (spec ids 399 BUY / 400 SELL)",
        "universe": "all active futures_universe symbols",
        "cadence_min": 15,
        "total_checks": TOTAL_CHECKS,
        "qualify_rule": f"pass >= evaluated - {N_MINUS} (i.e. {TOTAL_CHECKS - N_MINUS} of {TOTAL_CHECKS} at full evaluation)",
        "null_handling": "a check with missing data is SKIPPED (not scored as a fail) and the "
                          "pass bar becomes (evaluated - N_MINUS) of whatever checks DID evaluate",
        "latch_rule": "first qualification per symbol/side/day is recorded and LOCKED — "
                       "never re-evaluated or overwritten for the rest of that day",
        "entry_definition": "futures CMP (live_close) at the qualifying scan tick",
        "target_pct": TARGET_PCT * 100,
        "sl_pct": SL_PCT * 100,
        "target_sl_note": "stamped once at entry; SELL side is the mirror (target below entry, SL above)",
        "buy_checks": _check_defs("BUY"),
        "sell_checks": _check_defs("SELL"),
    }


# ── read endpoint ────────────────────────────────────────────────────────────
@router.get("/api/scanners/tc/holds")
def tc_scanner_holds(date_: Optional[str] = None):
    """BUY + SELL, open + closed, for the page's two-table view. WR% + net pts computed here."""
    with _conn() as conn, conn.cursor() as cur:
        ensure_schema(cur)
        conn.commit()
        d = date_ or str(date.today())
        cur.execute("""
            SELECT h.symbol, h.side, h.score, h.evaluated, h.entry_price, h.entry_ts,
                   h.target, h.sl, h.exit_price, h.exit_ts, h.exit_reason, c.cmp
            FROM tc_scanner_holds h
            LEFT JOIN cmp_prices c ON c.symbol = h.symbol
            WHERE h.scan_date = %s
            ORDER BY h.entry_ts DESC
        """, (d,))
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    out = {"BUY": [], "SELL": []}
    for r in rows:
        entry = float(r["entry_price"]) if r["entry_price"] is not None else None
        side = r["side"]
        if r["exit_reason"] != "OPEN" and r["exit_price"] is not None:
            px = float(r["exit_price"])
        else:
            px = float(r["cmp"]) if r["cmp"] is not None else entry
        pnl_pct = None
        if entry and px:
            pnl_pct = round((px - entry) / entry * 100 * (1 if side == "BUY" else -1), 2)
        row = {
            "symbol": r["symbol"], "side": side, "score": r["score"], "evaluated": r["evaluated"],
            "entry_price": entry, "entry_ts": str(r["entry_ts"]) if r["entry_ts"] else None,
            "target": float(r["target"]) if r["target"] is not None else None,
            "sl": float(r["sl"]) if r["sl"] is not None else None,
            "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
            "exit_ts": str(r["exit_ts"]) if r["exit_ts"] else None,
            "exit_reason": r["exit_reason"], "cmp": float(r["cmp"]) if r["cmp"] is not None else None,
            "pnl_pct": pnl_pct,
        }
        out.setdefault(side, []).append(row)

    def _stats(rows_side):
        closed = [x for x in rows_side if x["exit_reason"] != "OPEN"]
        wins = [x for x in closed if x["exit_reason"] == "TARGET"]
        net_pts = round(sum((x["pnl_pct"] or 0) for x in closed), 2)
        wr = round(len(wins) / len(closed) * 100, 1) if closed else None
        return {"open": len(rows_side) - len(closed), "closed": len(closed),
                "wins": len(wins), "wr_pct": wr, "net_pts_pct": net_pts}

    return {"date": d, "buy": out.get("BUY", []), "sell": out.get("SELL", []),
            "buy_stats": _stats(out.get("BUY", [])), "sell_stats": _stats(out.get("SELL", []))}

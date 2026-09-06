"""
TC Scanner — Scorr. The book engine behind /api/scanners/tc/holds (tc_scanner_holds).
=======================================================================================

V2 — cc#1746, session_log 39467 TC_SCANNER_ENTRY_EXIT_V2 (founder-stated 06-Sep-2026,
SELL-mirrors-BUY amendment the same day). Live from Monday 07-Sep-2026 on a book Fable
cleared that Sunday (410 rows archived to tc_scanner_holds_archive_20260906).

  ENTRY  Every 5 minutes in market hours the full active futures universe is scored on
         the FOUR score100 buckets by the shared scorer (tc_v4_dual.score_card via
         tc_v4_scan._load_bulk — the same numbers the Trade Check page and the scanner
         header show). A bucket ENTERS when its score100 >= its bar in
         tc_scanner_config.TC_SCANNER_CONFIG["score_thresholds"]:
             BUY-REV 80 · BUY-MOM 85 · SELL-REV 80 · SELL-MOM 85
         No sector/RSI gate and no per-bucket cap on the book (39467 names the score bar
         only; the 29447 gates and caps stay DISPLAY marks on the scanner page).
         One entry per symbol/side/day (UNIQUE latch); no entry while a position is
         OPEN on that side. If both buckets of a side clear their bar on the same
         tick the higher score100 is the entry. Entry price = futures CMP at the tick.
  EXIT   +3% target / -3% stop from entry (SELL mirrored), checked on every fyers_fut
         5-min bar's high/low inside the 7-day life. Stop before target inside a bar.
         A bar that OPENS beyond the level fills at its open (gap fill) — the recorded
         P&L keeps the slippage. Still open at entry_ts + 7 calendar days -> closed at
         the latest futures close, exit_reason TIME.
  BOOK   Screener-only: no position sizing, no paper engine. P&L is percent.
  Rows in the archive with style 'TC13' and score <= 13 are from the engine below.

HISTORY — the cc#464 engine this file ran from 13-Jul to 04-Sep-2026 (retired 06-Sep):
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
Position tracking: entry = futures CMP at the qualifying tick. target=+2%/
SL=-2% from entry (SELL mirrored). Checked each subsequent 15-min tick vs
live futures LTP; an EOD sweep does one final check against the last
available price so a touch between polls is not missed. Still-open positions
after the EOD sweep remain exit_reason=OPEN (screener-only — no forced close,
no paper engine).

24-Aug-2026: TARGET_PCT/SL_PCT revised from 3% to 2% (founder request, chat
session same date) to match the TC_SCANNER_CONFIG exit convention
(tc_scanner_config.py: target_pct 2 / stop_pct -2) used by the sibling TC v4
scanner, so the two "TC scanner" surfaces quote the same exit band.

06-Sep-2026, cc#1746 — TC_SCANNER_ENTRY_EXIT_V2 (session_log 39467, founder-stated):
  - Bracket back to +3% target / -3% stop from entry (the 24-Aug 2% band is
    superseded; closed rows keep whatever band they were entered on — history).
  - Stop is evaluated BEFORE target inside a bar: a bar that touches both
    levels closes as SL (it used to be left OPEN as "ambiguous").
  - Gap fills are recorded at the gap: when the touching bar OPENS beyond the
    level, exit_price = that bar's open, not the level. exit_reason stays
    SL / TARGET so the slippage is visible in the book, never clamped to 3%.
  - 7-calendar-day TIME STOP (HOLD_DAYS): a position still open at
    entry_ts + 7 days closes at the live futures price on that tick,
    exit_reason = TIME. Bracket touches are only honoured inside the 7 days.
  - No new entry on a symbol/side while a position is OPEN on that side
    (carried from 29136); the same-day latch is unchanged.
  - Cadence: every 5 minutes in market hours (was 15).
  - The 13-check entry test is RETIRED (Fable amendment 06-Sep: SELL mirrors
    BUY, book cleared). Entries now come from the four score100 buckets —
    see the V2 block at the top. The check functions are gone from this file.
"""

import os
import time
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Json
from fastapi import APIRouter

# cc#1746: ONE copy of every number. The entry bars and the exit band live in
# tc_scanner_config.TC_SCANNER_CONFIG (39467); this module derives its constants
# from there so the scanner page, the wall's engine-rules sheet and the book can
# never quote different figures.
from tc_scanner_config import TC_SCANNER_CONFIG, BUCKETS, side_of

router = APIRouter()
IST = ZoneInfo("Asia/Kolkata")
DATABASE_URL = os.getenv("DATABASE_URL", "")

ENTRY_THRESHOLDS = dict(TC_SCANNER_CONFIG["score_thresholds"])   # {"BUY-REV": 80, ...}
_EXIT = TC_SCANNER_CONFIG["exit"]
TARGET_PCT = float(_EXIT["target_pct"]) / 100.0        # +3% target (39467)
SL_PCT = abs(float(_EXIT["stop_pct"])) / 100.0         # -3% stop (39467)
HOLD_DAYS = int(_EXIT["hold_days"])                    # 7 calendar days, then TIME (39467)
CADENCE_MIN = 5                                        # scheduler._bg_tc_scanner, m % 5 == 0
SCORE_MAX = 100                                        # score100 scale; stored in `evaluated`
assert set(ENTRY_THRESHOLDS) == set(BUCKETS), "every bucket needs an entry bar"
# cc#1685: named once, read by BOTH check_exits()'s own return dict and /api/scanners/tc/spec's
# info-modal payload — same discipline as the constants above, one copy of the fact.
EXIT_BASIS = ("fyers_fut 5m bar high/low inside the %d-day life; stop before target in a bar; "
              "fill = the level, or the bar open when it gaps through (cc#1599 / cc#1746); "
              "TIME stop at %d days on the latest futures close" % (HOLD_DAYS, HOLD_DAYS))


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


# ── 13-check evaluation ──────────────────────────────────────────────────
def _target_sl(entry, side):
    if entry is None or entry <= 0:
        return None, None
    if side == "SELL":
        return round(entry * (1 - TARGET_PCT), 2), round(entry * (1 + SL_PCT), 2)
    return round(entry * (1 + TARGET_PCT), 2), round(entry * (1 - SL_PCT), 2)


# cc#1746: one statement for both sides. The same-day latch is the UNIQUE + ON CONFLICT; the
# "no entry while a position is OPEN on that side" rule (39467, carried from 29136) is the NOT
# EXISTS — under the old engine BEL BUY was opened on 17-Aug AND again on 18-Aug while the first
# was open. `style` = the bucket label (BUY-REV / BUY-MOM / SELL-REV / SELL-MOM), `score` = the
# score100 at entry (rounded), `evaluated` = SCORE_MAX so a row reads "82 / 100".
_INSERT_HOLD_SQL = """
    INSERT INTO tc_scanner_holds
        (symbol, side, style, score, evaluated, entry_price, entry_ts, target, sl, scan_date)
    SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    WHERE NOT EXISTS (SELECT 1 FROM tc_scanner_holds
                      WHERE symbol = %s AND side = %s AND exit_reason = 'OPEN')
    ON CONFLICT (symbol, side, scan_date) DO NOTHING
"""


def _score_universe():
    """Score every active futures symbol on all four buckets with the SHARED scorer — the same
    _load_bulk + score_card path tc_v4_scan.scan() runs for the scanner page, so the book and the
    page cannot disagree about a score. Returns ({symbol: {bucket: score100}}, {symbol: cmp},
    universe_count). A bucket the registry has not weighted has score100 None and never enters."""
    from tc_v4_scan import _load_bulk
    from tc_v4_dual import score_card, STYLES
    with _conn() as conn, conn.cursor() as cur:
        D, ctx = _load_bulk(cur)
    scores, cmps = {}, {}
    for sym, d in D.items():
        if not d.get("daily") or d.get("cmp") is None:
            continue
        cmps[sym] = float(d["cmp"])
        per = {}
        for side in ("BUY", "SELL"):
            for st in STYLES:
                c = score_card(d, st, side)
                per[c["label"]] = c.get("score100")
        scores[sym] = per
    return scores, cmps, int(ctx.get("count") or len(D))


def pick_entries(bucket_scores, thresholds=None):
    """Pure: {bucket: score100} for ONE symbol -> {side: (bucket, score100)} of the buckets that
    clear their bar, one per side (the higher score100 when both buckets of a side clear).
    39467: 'touches 80' is >=. None (unweighted) never enters."""
    th = thresholds or ENTRY_THRESHOLDS
    out = {}
    for bucket, sc in (bucket_scores or {}).items():
        side = side_of(bucket)
        if side is None or sc is None or bucket not in th:
            continue
        if float(sc) >= float(th[bucket]):
            cur = out.get(side)
            if cur is None or float(sc) > cur[1]:
                out[side] = (bucket, float(sc))
    return out


# ── scan + record (LATCH via UNIQUE, ON CONFLICT DO NOTHING) ──────────────
def run_scan():
    """Full-universe scan, both sides, on the V2 entry rule (39467). Records new entries only —
    first qualification per symbol/side/day latches, and a side that is already OPEN is skipped."""
    now = _ist_now()
    today = now.date()
    scores, cmps, universe = _score_universe()
    buy_new = sell_new = 0
    entries = []
    with _conn() as conn, conn.cursor() as cur:
        ensure_schema(cur)
        for sym in sorted(scores):
            picks = pick_entries(scores[sym])
            if not picks:
                continue
            px = cmps.get(sym)
            if not px or px <= 0:
                continue
            for side, (bucket, sc) in picks.items():
                tgt, sl = _target_sl(px, side)
                cur.execute(_INSERT_HOLD_SQL, (sym, side, bucket, int(round(sc)), SCORE_MAX, px,
                                               now.replace(tzinfo=None), tgt, sl, today, sym, side))
                if cur.rowcount:
                    entries.append({"symbol": sym, "side": side, "bucket": bucket, "score100": sc, "entry": px})
                    if side == "BUY":
                        buy_new += 1
                    else:
                        sell_new += 1
        conn.commit()
    return {"universe": universe, "scored": len(scores), "buy_new": buy_new, "sell_new": sell_new,
            "entries": entries, "thresholds": ENTRY_THRESHOLDS, "scan_ts": now.isoformat()}


def check_exits():
    """Close every OPEN hold on a bracket touch inside its 7-day life, then time-stop the rest.

    cc#1529 (P0): the SELECT used to add scan_date=CURRENT_DATE. scan_date is the ENTRY day,
    fixed at insert — so the moment the calendar rolled past it, the row was excluded from this
    check forever and could never close, however far price breached (founder report 31-Aug:
    zero multi-day closes ever; every historical TARGET/SL exit was same-day). Every OPEN row
    is in scope, whatever day it was entered.

    cc#1599 (P1): the check used to read ONE price per symbol — the latest fyers_fut close at
    poll time — and compare it to target / sl. A touch inside the gap between polls, or a
    high/low that reversed before the sampled bar closed, was never seen. Now every fyers_fut
    5m bar since entry_ts is examined on HIGH / LOW, in one query:
      BUY : target hit when bar.high >= target ; SL hit when bar.low  <= sl
      SELL: target hit when bar.low  <= target ; SL hit when bar.high >= sl
    The earlier touch wins. A row with no futures bars since entry has nothing to compare and
    stays OPEN (this check never prices a futures hold off the cash series). Re-running is
    idempotent: an already-closed row is not OPEN and is not selected.

    cc#1746 (TC_SCANNER_ENTRY_EXIT_V2, session_log 39467):
      - Only bars up to entry_ts + HOLD_DAYS are examined for a bracket touch; the position's
        life ends there.
      - STOP BEFORE TARGET inside a bar: when both levels are first touched in the same bar the
        exit is SL. (cc#1599 left such a row OPEN as "ambiguous"; 39467 orders it.)
      - GAP FILL: when the touching bar OPENS beyond the level, exit_price = that bar's open,
        never the level — the slippage stays visible (the 09:15 open-stops that averaged -6.50%
        must read -6.50%, not -3%). exit_reason stays SL / TARGET.
      - TIME STOP: a row still OPEN once now >= entry_ts + HOLD_DAYS closes at the latest
        fyers_fut 5m close on this tick, exit_reason = TIME, exit_ts = the tick. A row past its
        time with no futures price at all stays OPEN and is reported (time_no_price)."""
    now = _ist_now().replace(tzinfo=None)
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH o AS (
                SELECT id, symbol, side, entry_ts, target, sl,
                       entry_ts + make_interval(days => %s) AS life_end
                FROM tc_scanner_holds
                WHERE exit_reason='OPEN'
            ),
            b AS (
                SELECT o.id,
                       MIN(p.ts) FILTER (WHERE (o.side='BUY'  AND p.high >= o.target)
                                            OR (o.side='SELL' AND p.low  <= o.target)) AS tgt_ts,
                       MIN(p.ts) FILTER (WHERE (o.side='BUY'  AND p.low  <= o.sl)
                                            OR (o.side='SELL' AND p.high >= o.sl))     AS sl_ts,
                       COUNT(p.id) AS bars
                FROM o
                LEFT JOIN intraday_prices p
                       ON p.symbol = o.symbol AND p.source = 'fyers_fut' AND p.timeframe = '5m'
                      AND p.ts > o.entry_ts AND p.ts <= o.life_end
                GROUP BY o.id
            )
            SELECT o.id, o.symbol, o.side, o.target, o.sl, o.life_end,
                   b.tgt_ts, b.sl_ts, b.bars,
                   (SELECT p.open FROM intraday_prices p
                     WHERE p.symbol = o.symbol AND p.source = 'fyers_fut' AND p.timeframe = '5m'
                       AND p.ts = b.tgt_ts LIMIT 1) AS tgt_open,
                   (SELECT p.open FROM intraday_prices p
                     WHERE p.symbol = o.symbol AND p.source = 'fyers_fut' AND p.timeframe = '5m'
                       AND p.ts = b.sl_ts LIMIT 1) AS sl_open,
                   (SELECT p.close FROM intraday_prices p
                     WHERE p.symbol = o.symbol AND p.source = 'fyers_fut' AND p.timeframe = '5m'
                       AND p.ts <= %s ORDER BY p.ts DESC LIMIT 1) AS last_px
            FROM o JOIN b ON b.id = o.id
        """, (HOLD_DAYS, now))
        rows = cur.fetchall()
        if not rows:
            return {"checked": 0, "closed": 0, "timed_out": 0}
        closed, timed, no_bars, time_no_price = 0, 0, [], []
        for (hid, sym, side, target, sl, life_end, tgt_ts, sl_ts, bars,
             tgt_open, sl_open, last_px) in rows:
            hit = None
            if sl_ts is not None and (tgt_ts is None or sl_ts <= tgt_ts):
                # stop first — including the same-bar case (39467: stop before target in a bar)
                hit, ts, level, bar_open = "SL", sl_ts, float(sl), sl_open
            elif tgt_ts is not None:
                hit, ts, level, bar_open = "TARGET", tgt_ts, float(target), tgt_open
            if hit:
                px = _fill_price(side, hit, level, bar_open)
                cur.execute("""UPDATE tc_scanner_holds SET exit_price=%s, exit_ts=%s, exit_reason=%s
                               WHERE id=%s AND exit_reason='OPEN'""", (px, ts, hit, hid))
                closed += cur.rowcount
                continue
            if now >= life_end:
                if last_px is None:
                    time_no_price.append(sym)
                    continue
                cur.execute("""UPDATE tc_scanner_holds SET exit_price=%s, exit_ts=%s, exit_reason='TIME'
                               WHERE id=%s AND exit_reason='OPEN'""", (float(last_px), now, hid))
                timed += cur.rowcount
                continue
            if not bars:
                no_bars.append(sym)
        conn.commit()
    out = {"checked": len(rows), "closed": closed, "timed_out": timed, "basis": EXIT_BASIS}
    if no_bars:
        out["no_bars_open"] = sorted(set(no_bars))
    if time_no_price:
        out["time_no_price"] = sorted(set(time_no_price))
    return out


def _fill_price(side, hit, level, bar_open):
    """cc#1746 gap fill: the touching bar's OPEN when it already sits beyond the level, else the
    level itself. BUY SL / SELL TARGET are breached downward, BUY TARGET / SELL SL upward."""
    if bar_open is None:
        return level
    o = float(bar_open)
    downward = (side == "BUY" and hit == "SL") or (side == "SELL" and hit == "TARGET")
    if downward:
        return o if o < level else level
    return o if o > level else level


def eod_sweep():
    """One final check at EOD. cc#1599: the same bar-based check_exits — every 5m bar of the
    session is examined on high/low, so a touch between polls is closed with the bar's ts.
    Positions still inside their 7-day life after this stay OPEN (cc#1746: the time stop, not
    this sweep, is what ends a position that never touched a level)."""
    res = check_exits()
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'tc_scanner', 'eod_sweep', %s)",
                    (Json(res),))
        conn.commit()
    return res


# ── info-sheet spec, generated FROM the constants above (cc#465 discipline, V2 content) ──────
@router.get("/api/scanners/tc/spec")
def tc_scanner_spec():
    """The engine's own description of itself, built from the constants it runs on — the
    dashboard's (i) sheet renders this and types no number of its own."""
    return {
        "engine": "TC Scanner V2 — four score100 buckets (BUY-REV / BUY-MOM / SELL-REV / SELL-MOM)",
        "version": TC_SCANNER_CONFIG["version"],
        "source": "session_log 39467 TC_SCANNER_ENTRY_EXIT_V2 (cc#1746)",
        "universe": "all active futures_universe symbols",
        "cadence_min": CADENCE_MIN,
        "scorer": "tc_v4_dual.score_card over tc_v4_scan._load_bulk — the same scores the Trade Check page and the scanner header show",
        "entry_rules": [{"bucket": b, "threshold": ENTRY_THRESHOLDS[b],
                         "rule": "%s enters when score100 >= %s" % (b, ENTRY_THRESHOLDS[b])} for b in BUCKETS],
        "entry_guard": "one entry per symbol/side/day; no entry while a position is OPEN on that side; "
                       "when both buckets of a side clear their bar on one tick the higher score100 enters",
        "gates_and_caps": "none on the book — the sector/RSI gates and the 5-per-bucket cap (29447) are display marks on the scanner page only",
        "entry_definition": "futures CMP (last fyers_fut 5-min close) at the qualifying tick",
        "target_pct": TARGET_PCT * 100,
        "sl_pct": SL_PCT * 100,
        "target_sl_note": "stamped once at entry; SELL side is the mirror (target below entry, SL above)",
        "hold_days": HOLD_DAYS,
        "time_stop": "still open at entry + %d calendar days -> closed at the live futures price, exit_reason TIME" % HOLD_DAYS,
        "bar_order": "stop is evaluated before target inside a bar",
        "gap_fill": "a bar that opens beyond the level fills at its open; the recorded P&L keeps the slippage",
        # cc#1685: the exit mechanism, same string check_exits() itself reports — one copy.
        "exit_basis": EXIT_BASIS,
    }


# ── read endpoint ──────────────────────────────────────────────────────
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
        # cc#1763 (TC_SCANNER_LOT_SIZING_V1, session_log 39570): ONE LOT per signal. The lot is
        # futures_universe.lot_size (READ ONLY; tc_scanner_holds stores no quantity). Rupees are
        # DISPLAY ONLY — "what one lot would have made" — never a gate, cap, ranking key or
        # threshold, and the exit rule (check_exits: bar high/low vs the target / sl PRICE levels
        # _target_sl derives from TARGET_PCT / SL_PCT) takes no rupee input and is untouched.
        cur.execute("SELECT symbol, lot_size FROM futures_universe WHERE lot_size IS NOT NULL")
        lots = {r[0]: int(r[1]) for r in cur.fetchall()}

    def _rs(entry, mark, side, lot):
        """One-lot rupees, sign-aware. None when entry, mark or lot is missing — never 0.00 off a
        substituted price, never 1 share standing in for a lot (cc#1763 item 5)."""
        if entry is None or mark is None or lot is None:
            return None
        return round((mark - entry) * lot * (1 if side == "BUY" else -1), 2)

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
        # cc#1763: the rupee mark is the exit price on a closed row, cmp_prices on an open one —
        # and NOTHING when there is no cmp (the percent above keeps its older entry fallback).
        mark = (float(r["exit_price"]) if (r["exit_reason"] != "OPEN" and r["exit_price"] is not None)
                else (float(r["cmp"]) if r["cmp"] is not None else None))
        row = {
            "symbol": r["symbol"], "side": side, "score": r["score"], "evaluated": r["evaluated"],
            "entry_price": entry, "entry_ts": str(r["entry_ts"]) if r["entry_ts"] else None,
            "target": float(r["target"]) if r["target"] is not None else None,
            "sl": float(r["sl"]) if r["sl"] is not None else None,
            "exit_price": float(r["exit_price"]) if r["exit_price"] is not None else None,
            "exit_ts": str(r["exit_ts"]) if r["exit_ts"] else None,
            "exit_reason": r["exit_reason"], "cmp": float(r["cmp"]) if r["cmp"] is not None else None,
            "pnl_pct": pnl_pct,
            "lot_size": lots.get(r["symbol"]), "pnl_rs": _rs(entry, mark, side, lots.get(r["symbol"])),
        }
        out.setdefault(side, []).append(row)

    def _stats(rows_side):
        closed = [x for x in rows_side if x["exit_reason"] != "OPEN"]
        # cc#1599 P3: a reconciled exit carries its evidence basis in exit_reason
        # ("TARGET (cash-daily)" / "SL (cash-daily)"), so wins are classified by prefix.
        wins = [x for x in closed if str(x["exit_reason"] or "").startswith("TARGET")]
        net_pts = round(sum((x["pnl_pct"] or 0) for x in closed), 2)
        wr = round(len(wins) / len(closed) * 100, 1) if closed else None
        # cc#1763 C5: WR / net-percent stay the statistics' basis; the rupee total rides beside
        # them at one lot each, summed only over rows that have one.
        rs_rows = [x for x in closed if x.get("pnl_rs") is not None]
        return {"open": len(rows_side) - len(closed), "closed": len(closed),
                "wins": len(wins), "wr_pct": wr, "net_pts_pct": net_pts,
                "net_rs_one_lot": round(sum(x["pnl_rs"] for x in rs_rows), 2), "rs_rows": len(rs_rows)}

    # cc#1599 scope 4/5: the Closed Book is keyed on the EXIT date, not the entry date. A hold
    # entered on 16-Jul and closed on 02-Sep belongs to 02-Sep's Closed Book (and to 16-Jul's
    # Open Book history). The keys above stay exactly as they were (scan_date = entry day), so
    # the Open Book and every older reader are untouched; the tab reads the two new keys.
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT h.symbol, h.side, h.score, h.evaluated, h.entry_price, h.entry_ts,
                   h.target, h.sl, h.exit_price, h.exit_ts, h.exit_reason, h.scan_date
            FROM tc_scanner_holds h
            WHERE h.exit_reason <> 'OPEN' AND h.exit_ts::date = %s
            ORDER BY h.exit_ts DESC
        """, (d,))
        ccols = [c[0] for c in cur.description]
        crows = [dict(zip(ccols, r)) for r in cur.fetchall()]
    by_exit = {"BUY": [], "SELL": []}
    for r in crows:
        entry = float(r["entry_price"]) if r["entry_price"] is not None else None
        px = float(r["exit_price"]) if r["exit_price"] is not None else None
        pnl_pct = None
        if entry and px:
            pnl_pct = round((px - entry) / entry * 100 * (1 if r["side"] == "BUY" else -1), 2)
        by_exit.setdefault(r["side"], []).append({
            "symbol": r["symbol"], "side": r["side"], "score": r["score"], "evaluated": r["evaluated"],
            "entry_price": entry, "entry_ts": str(r["entry_ts"]) if r["entry_ts"] else None,
            "scan_date": str(r["scan_date"]) if r["scan_date"] else None,
            "target": float(r["target"]) if r["target"] is not None else None,
            "sl": float(r["sl"]) if r["sl"] is not None else None,
            "exit_price": px, "exit_ts": str(r["exit_ts"]) if r["exit_ts"] else None,
            "exit_reason": r["exit_reason"], "pnl_pct": pnl_pct,
            "lot_size": lots.get(r["symbol"]), "pnl_rs": _rs(entry, px, r["side"], lots.get(r["symbol"])),   # cc#1763
        })

    # cc#1744 (founder 06-Sep "In TC scanner no open and closed book?"): the OPEN BOOK is NOT a
    # per-date set. The keys above filter on scan_date (= entry day), so the Open Book read
    # empty on every non-trading day and on any date the founder browsed to, while 19 holds
    # sat open with real money at risk — the date control reads 06-Sep (a Sunday) and none of
    # the open rows were ENTERED that day. An open position has no exit date and belongs to
    # no single day: `open_all` is every exit_reason = OPEN row as of now, newest entry first,
    # with NO date predicate. The scan_date-keyed `buy`/`sell` keys are untouched (older
    # readers, and the Closed Book's cc#1599 exit-date keys are a separate query). CMP here is
    # cmp_prices or NOTHING — when a symbol has no price, cmp and pnl_pct are None, never the
    # entry price substituted to render a false 0.00 (item 4).
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT h.symbol, h.side, h.score, h.evaluated, h.entry_price, h.entry_ts,
                   h.target, h.sl, h.scan_date, c.cmp
            FROM tc_scanner_holds h
            LEFT JOIN cmp_prices c ON c.symbol = h.symbol
            WHERE h.exit_reason = 'OPEN'
            ORDER BY h.entry_ts DESC
        """)
        ocols = [c[0] for c in cur.description]
        orows = [dict(zip(ocols, r)) for r in cur.fetchall()]
        cur.execute("SELECT MAX(exit_ts)::date FROM tc_scanner_holds WHERE exit_reason <> 'OPEN' AND exit_ts IS NOT NULL")
        _lc = cur.fetchone()
        last_closure = str(_lc[0]) if (_lc and _lc[0]) else None
    open_all = {"BUY": [], "SELL": []}
    for r in orows:
        entry = float(r["entry_price"]) if r["entry_price"] is not None else None
        cmp_px = float(r["cmp"]) if r["cmp"] is not None else None
        pnl_pct = None
        if entry and cmp_px is not None:
            pnl_pct = round((cmp_px - entry) / entry * 100 * (1 if r["side"] == "BUY" else -1), 2)
        open_all.setdefault(r["side"], []).append({
            "symbol": r["symbol"], "side": r["side"], "score": r["score"], "evaluated": r["evaluated"],
            "entry_price": entry, "entry_ts": str(r["entry_ts"]) if r["entry_ts"] else None,
            "scan_date": str(r["scan_date"]) if r["scan_date"] else None,
            "target": float(r["target"]) if r["target"] is not None else None,
            "sl": float(r["sl"]) if r["sl"] is not None else None,
            "exit_price": None, "exit_ts": None, "exit_reason": "OPEN",
            "cmp": cmp_px, "pnl_pct": pnl_pct,
            "lot_size": lots.get(r["symbol"]), "pnl_rs": _rs(entry, cmp_px, r["side"], lots.get(r["symbol"])),   # cc#1763
        })
    n_open_all = len(open_all.get("BUY", [])) + len(open_all.get("SELL", []))

    # cc#1763 C3: the two BOOK TOTALS at one lot each, stated separately and never summed into one
    # figure — Open Book = UNREALISED (cmp_prices), Closed Book = REALISED (exit price). Rows
    # without a lot or a CMP are counted, not summed. The tab shows both books whole (no side
    # filter), so these describe exactly the rows it renders.
    def _book(rows_list, kind):
        priced = [x for x in rows_list if x.get("pnl_rs") is not None]
        return {"n": len(rows_list), "priced": len(priced),
                "rows_without_lot": sum(1 for x in rows_list if x.get("lot_size") is None),
                "rows_without_cmp": (sum(1 for x in rows_list if x.get("cmp") is None) if kind == "unrealised" else 0),
                kind + "_rs_one_lot": round(sum(x["pnl_rs"] for x in priced), 2),
                "basis": "%d %s position%s, one lot each (futures_universe.lot_size) — the paper book if every signal were taken; %s" % (
                    len(rows_list), "open" if kind == "unrealised" else "closed", "" if len(rows_list) == 1 else "s",
                    "unrealised vs cmp_prices" if kind == "unrealised" else "realised at exit price")}
    open_all_book = _book(open_all.get("BUY", []) + open_all.get("SELL", []), "unrealised")
    closed_by_exit_book = _book(by_exit.get("BUY", []) + by_exit.get("SELL", []), "realised")

    return {"date": d, "buy": out.get("BUY", []), "sell": out.get("SELL", []),
            "buy_stats": _stats(out.get("BUY", [])), "sell_stats": _stats(out.get("SELL", [])),
            # cc#1599: closed on this EXIT date, whatever day they were entered.
            "closed_by_exit": {"buy": by_exit.get("BUY", []), "sell": by_exit.get("SELL", [])},
            "closed_by_exit_stats": {"buy": _stats(by_exit.get("BUY", [])), "sell": _stats(by_exit.get("SELL", []))},
            "closed_basis": "exit_ts::date = date (cc#1599); open book keyed on scan_date (entry)",
            # cc#1744: the Open Book proper — every open hold as of now, no date predicate.
            "open_all": {"buy": open_all.get("BUY", []), "sell": open_all.get("SELL", [])},
            "open_all_count": n_open_all,
            "open_basis": "exit_reason = 'OPEN' as of now, no date filter (cc#1744); cmp from cmp_prices or blank",
            "last_closure_date": last_closure,
            # cc#1763: the one-lot book totals + the rule, stated where the rows are served.
            "open_all_book": open_all_book,
            "closed_by_exit_book": closed_by_exit_book,
            "lot_rule": "TC_SCANNER_LOT_SIZING_V1 (session_log 39570): ONE LOT per signal, lot_size from futures_universe (read only). pnl_rs = (mark - entry) x lot_size, sign-aware; mark = exit price when closed, cmp_prices when open; None when lot or mark is missing. DISPLAY ONLY — win rate and net percent stay on pnl_pct; the engine's exits (check_exits) compare bars to target / sl price levels and read no rupee.",
            "as_of": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}

"""
scanners_app_mobile.py — TC Scanner trades + Investment Scanner app pages. Fable single-mode build
10-Sep-2026 (founder: "TC scanner and investment scanner trades"). Reads the web tabs' OWN functions;
nothing re-derived:
  GET /api/mobile/tcscan?date=YYYY-MM-DD → tc_scanner_endpoints.tc_scanner_holds(): open book (as of now),
                                            closed book on that exit date, per-side stats, one-lot rupees,
                                            plus the engine spec and a since-start record (one small SQL here,
                                            over the same tc_scanner_holds table, stated as such)
  GET /api/mobile/invscan?track=momentum → inv_scanner_endpoints.board() + inv_scanner_rules.get_state():
                                            ranked board with gate state, open/closed positions with P&L,
                                            the rule text from the engine's own constants
Pages: mobile/tcscan.html (/m/tcscan) and mobile/invscan.html (/m/invscan).
"""
import os
import logging

from fastapi import APIRouter, Request
import psycopg

from mobile_endpoints import _guard, _json_safe
from tc_scanner_endpoints import tc_scanner_holds, tc_scanner_spec
from inv_scanner_endpoints import board as inv_board, META as INV_META
from inv_scanner_rules import get_state as inv_state, get_signals as inv_signals

log = logging.getLogger("scorr.mobile.scanners")
router = APIRouter()


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _fl(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _record(cur):
    """Since-start record over tc_scanner_holds — the same table the web holds endpoint reads,
    all closed rows, no date filter. WR counts TARGET exits as wins (the web's own rule).
    cc#2176: the ALL row (both sides together) comes from the SAME query via GROUP BY ROLLUP —
    the database adds the two sides up over the same closed rows; nothing is summed a second time
    in Python, so BUY + SELL and ALL can never drift apart."""
    # cc#2130 item 1: the since-start rupee total at ONE LOT per signal -- the same formula
    # tc_scanner_endpoints._rs applies per row ((mark - entry) x lot_size, sign-aware, lot from
    # futures_universe, rows without a lot counted not summed), aggregated over the same closed rows
    # net_pts already sums. Not a new number, the existing per-row number added up. DISPLAY ONLY.
    cur.execute("""
        SELECT CASE WHEN GROUPING(h.side) = 1 THEN 'ALL' ELSE h.side END AS side, COUNT(*) AS n,
               SUM(CASE WHEN h.exit_reason LIKE 'TARGET%%' THEN 1 ELSE 0 END) AS wins,
               ROUND(SUM(((h.exit_price - h.entry_price) / NULLIF(h.entry_price,0)) * 100 * CASE WHEN h.side='BUY' THEN 1 ELSE -1 END)::numeric, 2) AS net_pts,
               MIN(h.entry_ts)::date AS since, MAX(h.exit_ts)::date AS last,
               COUNT(f.lot_size) AS rs_rows,
               ROUND(SUM((h.exit_price - h.entry_price) * f.lot_size * CASE WHEN h.side='BUY' THEN 1 ELSE -1 END)::numeric, 2) AS net_rs
        FROM tc_scanner_holds h
        LEFT JOIN futures_universe f ON f.symbol = h.symbol AND f.lot_size IS NOT NULL
        WHERE h.exit_reason <> 'OPEN' AND h.exit_price IS NOT NULL AND h.entry_price IS NOT NULL
        GROUP BY ROLLUP (h.side)""")
    out = {}
    for side, n, wins, net, since, last, rs_rows, net_rs in cur.fetchall():
        out[side] = {"closed": int(n or 0), "wins": int(wins or 0), "wr_pct": round(int(wins or 0) / int(n) * 100, 1) if n else None,
                     "net_pts_pct": _fl(net), "since": str(since) if since else None, "last": str(last) if last else None,
                     "net_rs_one_lot": _fl(net_rs), "rs_rows": int(rs_rows or 0)}
    return out


def _by_side(oa):
    """cc#2176: per-side split of the OPEN book, from the SAME per-row pnl_rs / pnl_pct that
    tc_scanner_holds() already computed ((mark - entry) x lot_size, sign-aware; None without a lot
    or a cmp). open_all_book carries no per-side split, so this sums the very rows it sums, per
    side, with the same rule: rows without a priced pnl are COUNTED, not summed (the one-lot rule
    the page prints). A side with nothing open gets count 0 and None figures, never 0.00."""
    out = {}
    for side in ("BUY", "SELL"):
        rows_ = oa.get(side.lower()) or []
        priced = [r for r in rows_ if r.get("pnl_rs") is not None]
        pts = [r for r in rows_ if r.get("pnl_pct") is not None]
        out[side] = {
            "count": len(rows_), "priced": len(priced),
            "rows_without_lot": sum(1 for r in rows_ if r.get("lot_size") is None),
            "rows_without_cmp": sum(1 for r in rows_ if r.get("cmp") is None),
            "unrealised_rs": round(sum(float(r["pnl_rs"]) for r in priced), 2) if priced else None,
            "unrealised_pts_pct": round(sum(float(r["pnl_pct"]) for r in pts), 2) if pts else None,
        }
    return out


@router.get("/api/mobile/tcscan")
@_json_safe
def mobile_tcscan(request: Request, date: str = ""):
    g = _guard(request)
    if g:
        return g
    h = tc_scanner_holds(date or None)
    if not isinstance(h, dict):
        return {"error": "holds unavailable"}
    spec = tc_scanner_spec()
    with _conn() as conn, conn.cursor() as cur:
        record = _record(cur)
        cur.execute("SELECT DISTINCT exit_ts::date FROM tc_scanner_holds WHERE exit_reason <> 'OPEN' AND exit_ts IS NOT NULL ORDER BY 1 DESC LIMIT 12")
        exit_days = [str(r[0]) for r in cur.fetchall()]

    def rows(lst):
        return [{"symbol": r.get("symbol"), "side": r.get("side"), "score": _fl(r.get("score")),
                 "entry": _fl(r.get("entry_price")), "entry_ts": r.get("entry_ts"), "cmp": _fl(r.get("cmp")),
                 "target": _fl(r.get("target")), "sl": _fl(r.get("sl")), "exit": _fl(r.get("exit_price")),
                 "exit_ts": r.get("exit_ts"), "exit_reason": r.get("exit_reason"), "pnl_pct": _fl(r.get("pnl_pct")),
                 "lot": r.get("lot_size"), "pnl_rs": _fl(r.get("pnl_rs"))} for r in (lst or [])]

    oa = h.get("open_all") or {}
    cb = h.get("closed_by_exit") or {}
    return {"date": h.get("date"), "as_of": h.get("as_of"), "last_closure": h.get("last_closure_date"), "exit_days": exit_days,
            "open": {"buy": rows(oa.get("buy")), "sell": rows(oa.get("sell")), "count": h.get("open_all_count"),
                     "book": h.get("open_all_book"),
                     # cc#2176: the hero rail's Long / Short cards. Same rows, same per-row rupee, split by side.
                     "by_side": _by_side(oa),
                     "by_side_basis": "sum of the same per-row pnl_rs (one lot each, cmp_prices mark) over the open rows of that side; rows without a lot or a cmp are counted, not summed; BUY + SELL = open.book.unrealised_rs_one_lot"},
            "closed": {"buy": rows(cb.get("buy")), "sell": rows(cb.get("sell")),
                       "stats": h.get("closed_by_exit_stats"), "book": h.get("closed_by_exit_book")},
            "record": record,
            "spec": {"target_pct": spec.get("target_pct"), "sl_pct": spec.get("sl_pct"), "hold_days": spec.get("hold_days"),
                     "cadence_min": spec.get("cadence_min"), "universe": spec.get("universe"),
                     "entry_rules": spec.get("entry_rules"), "exit_basis": spec.get("exit_basis"), "version": spec.get("version")},
            "lot_rule": h.get("lot_rule"),
            "note": "Paper book: one lot per signal at the qualifying tick. Rupees are display only; win rate and net points are the record."}


@router.get("/api/mobile/invscan")
@_json_safe
def mobile_invscan(request: Request, track: str = "momentum"):
    g = _guard(request)
    if g:
        return g
    track = "reversal" if track == "reversal" else "momentum"
    b = inv_board(track=track, limit=100)
    st = inv_state()
    sig = inv_signals(limit=30)
    rows = []
    for r in (b.get("rows") or []) if isinstance(b, dict) else []:
        gates = r.get("gates") or {}
        rows.append({"symbol": r.get("symbol"), "score": _fl(r.get("score")), "band": r.get("band"),
                     "mom": _fl(r.get("mom_score")), "rev": _fl(r.get("rev_score")),
                     "gvm": _fl(r.get("gvm")), "g": _fl(r.get("g")), "v": _fl(r.get("v")), "m": _fl(r.get("m")),
                     "tags": r.get("tags") or [], "gates_n": r.get("gates_pass_n"), "state": r.get("gates_state"),
                     "gates": {k: {"value": _fl((gates.get(k) or {}).get("value")), "pass": bool((gates.get(k) or {}).get("pass"))} for k in gates},
                     "wk52": _fl(r.get("wk52")), "month_return": _fl(r.get("month_return")),
                     "position": r.get("state")})
    pos_open, pos_closed = [], []
    for r in (st.get("rows") or []) if isinstance(st, dict) else []:
        row = {"symbol": r.get("symbol"), "entered": r.get("entered_at"), "track": r.get("entry_track"),
               "entry_score": _fl(r.get("entry_score")), "entry": _fl(r.get("entry_price")), "cmp": _fl(r.get("cmp")),
               "day_pct": _fl(r.get("day_pnl_pct")), "net_pct": _fl(r.get("net_pnl_pct")),
               "exited": r.get("exited_at"), "exit_reason": r.get("exit_reason"), "exit": _fl(r.get("exit_price"))}
        (pos_open if r.get("status") == "open" else pos_closed).append(row)
    pos_closed.sort(key=lambda x: x.get("exited") or "", reverse=True)
    closed_with = [p for p in pos_closed if p["net_pct"] is not None]
    wins = [p for p in closed_with if p["net_pct"] > 0]
    record = {"closed": len(pos_closed), "wins": len(wins),
              "wr_pct": round(len(wins) / len(closed_with) * 100, 1) if closed_with else None,
              "avg_net_pct": round(sum(p["net_pct"] for p in closed_with) / len(closed_with), 2) if closed_with else None}
    open_with = [p for p in pos_open if p["net_pct"] is not None]
    meta = b.get("meta") if isinstance(b, dict) and b.get("meta") else INV_META
    return {"run_date": b.get("run_date") if isinstance(b, dict) else None, "track": track,
            "board": rows, "enters": [r for r in rows if r["state"] in ("ENTERS", "ENTERED")],
            "positions": {"open": pos_open, "closed": pos_closed[:20], "closed_total": len(pos_closed),
                          "open_avg_net_pct": round(sum(p["net_pct"] for p in open_with) / len(open_with), 2) if open_with else None},
            "record": record,
            "rule": {"what": meta.get("what_it_is"), "universe": meta.get("universe"), "entry": (meta.get("entry") or {}).get("rule"),
                     "gates": (meta.get("entry") or {}).get("gates"), "exit": (meta.get("exit") or {}).get("rule"),
                     "bands": (meta.get("bands") or {}).get("rule"), "honesty": meta.get("honesty"),
                     "gate_labels": meta.get("gate_labels")},
            "signals": [{"event": s.get("event"), "ts": s.get("ts"), "symbol": s.get("symbol"), "track": s.get("track"), "score": _fl(s.get("score"))}
                        for s in (sig.get("rows") or [])] if isinstance(sig, dict) else []}

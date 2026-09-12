"""option_chain_grid.py — cc#2004 OPTION_CHAIN_GRID_V1 (founder direction 11-Sep 20:52-21:15 IST,
strike range + full-row colours revised 12-Sep ~08:15 IST — the same ruling cc#2003 and cc#2006
build on top of: "cc#2003 embeds THIS card component in a Home popup — one implementation, one
data path, shared strike range").

ONE BUILDER (card item 10), not a second data path: this module does NOT recompute ltp, iv, fair
value or the cheap/fair/expensive tag — it calls deriv_metrics.strike_chain() (cc#1576/cc#1859's
own pipeline, already ATM±10 for both stocks and indices, already tag-annotated) and adds ONLY
what that pipeline does not carry: per-strike OI and the max-pain / call-wall / put-wall flags,
sourced from oi_structure.py / max_pain.py (cc#1155/cc#1875's own composer) — the SAME table both
already read, never a third.

INDEX-ONLY, stated plainly rather than silently patched over: `option_chain` (the table oi_structure
and max_pain both read) carries NO stock strikes at all — confirmed by oi_structure.py's own
docstring and by direct schema/data inspection before this file was written. A stock's grid
therefore carries ltp/iv/fair/tag on every row (from strike_chain(), unaffected) and no OI, no
wall highlight, no max-pain highlight on any row — `oi_available` on the returned payload states
this explicitly so a caller renders "not available for this symbol", never a fabricated zero.

The index/stock decision reuses deriv_metrics._INDEX_OPT_ROOT — the EXACT map strike_chain()
itself already uses to choose its own option_chain-vs-Fyers path — never oi_structure.
norm_underlying(), which silently falls back to "NIFTY" for any name it does not recognise (a
real trap: passing a stock symbol to it would silently score that stock against NIFTY's chain).

NOT available anywhere in this pipeline, stated here rather than invented for the tap-to-detail
panel (card item 7's 7-field list): OI CHANGE (option_chain stores a level per tick, not a delta;
computing "change" needs a baseline tick the card does not specify — shipping a guessed baseline
would be worse than omitting the field), and BID/ASK for BOTH legs. For the STOCK path,
deriv_metrics._batch_quotes extracts only `lp`/`ltp` from the Fyers quote response, confirmed by
reading it — no stock bid/ask is captured anywhere. For the INDEX path, `option_chain` DOES carry
`bid`/`ask` columns and this file's own query below does select them — but checked directly
against the live table before writing this note: 0 of 59,369 rows in `option_chain` have ever had
either populated (`SELECT COUNT(bid), COUNT(ask) FROM option_chain` — both zero). The columns
exist; the feed that fills this table has never written to them. The merge below still copies
whatever is there (harmless, and it starts surfacing real values for free the day that feed is
extended) but a caller must not read this module's current output as proof bid/ask is live —
today every row's bid/ask is None, chain-wide, for both stocks and index alike.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

import deriv_metrics
import max_pain as max_pain_mod
import oi_structure as oi_structure_mod

log = logging.getLogger("scorr.option_chain_grid")
router = APIRouter()


def _conn():
    return deriv_metrics._conn()


def _oi_and_bidask_map(cur, root: str) -> Dict[float, Dict[str, Any]]:
    """Per-strike OI + bid/ask for the SAME latest tick / nearest expiry oi_structure and
    strike_chain's own index path both read — max_pain_mod.LATEST_CHAIN_SQL's own expiry/tick
    subqueries, extended with the bid/ask columns it does not itself select (that query is used
    unmodified elsewhere for the max-pain payout curve, which only ever needed OI — adding columns
    to its own SELECT here, not editing the shared constant, so nothing that already imports
    LATEST_CHAIN_SQL sees a different query shape)."""
    cur.execute("""
        WITH exp AS (
            SELECT MIN(expiry) AS e FROM option_chain
            WHERE underlying = %(u)s AND expiry >= CURRENT_DATE
        ),
        mts AS (
            SELECT MAX(ts) AS t FROM option_chain
            WHERE underlying = %(u)s AND expiry = (SELECT e FROM exp)
        )
        SELECT strike, option_type, oi, bid, ask
        FROM option_chain
        WHERE underlying = %(u)s
          AND expiry = (SELECT e FROM exp)
          AND ts = (SELECT t FROM mts)
    """, {"u": root})
    out: Dict[float, Dict[str, Any]] = {}
    for strike, ot, oi, bid, ask in cur.fetchall():
        s = float(strike)
        cell = out.setdefault(s, {})
        leg = "ce" if (ot or "").upper() == "CE" else "pe"
        cell[leg] = {"oi": int(oi) if oi is not None else None,
                     "bid": float(bid) if bid is not None else None,
                     "ask": float(ask) if ask is not None else None}
    return out


def merge_oi_into_rows(rows: List[Dict[str, Any]], oi_map: Dict[float, Dict[str, Any]],
                        max_pain_strike: Optional[float], call_wall: Optional[float],
                        put_wall: Optional[float]) -> None:
    """PURE, no DB access — the actual merge, factored out so it can be unit-tested with synthetic
    data independent of the two live composers above. Mutates `rows` (strike_chain()'s own list)
    in place: adds ce/pe oi+bid+ask where oi_map has that strike, and the three boolean row flags.
    A strike with no oi_map entry at all (should not happen for an index inside the ±10 window,
    since strike_chain() and this file read the same expiry/tick — stated as a genuine 'no data'
    case if it ever does, never a fabricated zero) gets oi=None on both legs, no flags set."""
    for row in rows:
        s = row["strike"]
        cell = oi_map.get(s)
        for leg in ("ce", "pe"):
            extra = (cell or {}).get(leg) or {"oi": None, "bid": None, "ask": None}
            row[leg]["oi"] = extra["oi"]
            row[leg]["bid"] = extra["bid"]
            row[leg]["ask"] = extra["ask"]
        row["is_max_pain"] = (max_pain_strike is not None and s == max_pain_strike)
        row["is_call_wall"] = (call_wall is not None and s == call_wall)
        row["is_put_wall"] = (put_wall is not None and s == put_wall)


LEGEND = [
    {"key": "cheap", "colour": "green", "label": "Cheap — trading below fair value"},
    {"key": "fair", "colour": "grey", "label": "Fair — trading close to fair value"},
    {"key": "expensive", "colour": "coral", "label": "Expensive — trading above fair value"},
    {"key": "max_pain", "colour": "gold", "label": "Max pain — the strike where option writers lose the least"},
    {"key": "call_wall", "colour": "coral", "label": "Call wall — the strike with the heaviest call open interest"},
    {"key": "put_wall", "colour": "teal", "label": "Put wall — the strike with the heaviest put open interest"},
]


def build_chain_grid(symbol: str) -> Dict[str, Any]:
    """The one function both the D-button chain and (via cc#2003/cc#2006) the Home popup call.
    Returns strike_chain()'s own payload shape, plus `oi_available`, `legend`, and — only when
    oi_available — `max_pain`/`call_wall`/`put_wall` (strike-level, for a caller that wants them
    without walking every row) and the per-row oi/bid/ask/is_max_pain/is_call_wall/is_put_wall
    fields this module adds. do_not_touch (card item 11): never routes through Index Intel; this
    is purely a data composer, callable from wherever a chain is already rendered today."""
    sym = (symbol or "").strip().upper()
    payload = deriv_metrics.strike_chain(sym)   # raises HTTPException on its own errors — not caught here
    payload["legend"] = LEGEND
    root = deriv_metrics._INDEX_OPT_ROOT.get(sym)   # the SAME map strike_chain() itself used above
    payload["is_index"] = bool(root)
    if not root:
        payload["oi_available"] = False
        return payload
    try:
        with _conn() as conn, conn.cursor() as cur:
            oi_map = _oi_and_bidask_map(cur, root)
        oi_data = oi_structure_mod.oi_structure(root)
    except Exception as e:
        # An index whose OI leg is down should not take the whole grid down with it — strike_chain()'s
        # own rows (ltp/iv/tag) already succeeded above and are still worth returning.
        log.warning("option_chain_grid: OI merge failed for %s (%s), returning ltp/iv/tag only: %s", sym, root, e)
        payload["oi_available"] = False
        payload["oi_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return payload
    if oi_data.get("status") != "ok":
        payload["oi_available"] = False
        payload["oi_note"] = oi_data.get("note")
        return payload
    max_pain_strike = oi_data.get("max_pain")
    call_wall = oi_data.get("call_wall")
    put_wall = oi_data.get("put_wall")
    merge_oi_into_rows(payload["strikes"], oi_map, max_pain_strike, call_wall, put_wall)
    payload["oi_available"] = True
    payload["max_pain"] = max_pain_strike
    payload["call_wall"] = call_wall
    payload["put_wall"] = put_wall
    payload["oi_asof"] = oi_data.get("as_of")
    payload["pcr"] = oi_data.get("pcr")
    return payload


@router.get("/api/deriv/chain-grid/{symbol}")
def chain_grid_endpoint(symbol: str):
    """New, additive route — does not change strike_chain()'s own /api/deriv/strike-chain/{symbol}
    response shape for any existing caller. Serves the D-button chain's NSE-grid rendering
    (cc#2004) and, once cc#2003/cc#2006 wire it in, the Home popup — one endpoint, one builder."""
    try:
        return build_chain_grid(symbol)
    except HTTPException:
        raise
    except Exception as e:
        log.error("chain_grid_endpoint %s: %s", symbol, e, exc_info=True)
        raise HTTPException(500, f"chain_grid failed: {e}")

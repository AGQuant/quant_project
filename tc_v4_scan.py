"""
Trade Check v4 DUAL-STYLE — batch scanner (cc#387, canonical spec session_log id=2926).

Scans the whole active futures universe (~212) in ONE mostly-vectorized pass and scores all FOUR
cards per stock (BUY/SELL x MOMENTUM/REVERSAL) using the SAME rulebook as the single-symbol engine.

SHARED-MODULE PROOF: this file imports `_derive`, `_gates`, `score_card`, `_verdict` from tc_v4_dual
and calls them unchanged. The bulk loader fills the exact same `d` dict fields the single-symbol
loader fills, so scanner score == /api/trade-check/v4/dual score for the same stock, by construction.
Only R6 (time-adjusted intraday volume) and the Nifty D/W/M read reuse the shared per-call helpers;
every heavy series (daily OHLC, v8 tick, pivots, basis, session bars, peers) is pulled set-based.

Route: GET /api/trade-check/v4/scan?side=ALL&verdict=ALL&segment=  -> ranked list, best card each.
Boot self-test: sets/reads app_config so the deployed engine can be verified from a SQL console.
"""

import os
import json
from datetime import datetime, timedelta

import psycopg
from fastapi import APIRouter

from nifty_dwm import live_nifty_dwm
from r6_volume import volume_ratio
from tc_v4_dual import (_f, _r, _derive, _gates, score_card, _verdict,
                        STYLES, _ist, SPEC_REF, VERSION)

router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")


def _bucket(rows, key_idx):
    out = {}
    for r in rows:
        out.setdefault(r[key_idx], []).append(r)
    return out


def _load_bulk(cur):
    """One set-based pass for every active futures symbol. Returns {symbol: d} ready for scoring."""
    cur.execute("SELECT UPPER(symbol) FROM futures_universe WHERE is_active=TRUE")
    syms = sorted({r[0] for r in cur.fetchall()})
    if not syms:
        return {}, {}
    D = {s: {"symbol": s, "is_future": True} for s in syms}

    # market-wide reads (once)
    nday, nwk, nmo, nsrc = live_nifty_dwm(cur)
    cur.execute("SELECT adr FROM adr_daily ORDER BY price_date DESC LIMIT 1")
    a = cur.fetchone()
    adr = _f(a[0]) if a else None
    for s in syms:
        D[s].update({"nifty_day": nday, "nifty_wk": nwk, "nifty_mo": nmo, "adr": adr})

    # daily OHLCV — last ~240 calendar days, bucketed, keep last 160 ascending
    cur.execute("""SELECT symbol, price_date, open, high, low, close, volume
                   FROM raw_prices WHERE symbol = ANY(%s) AND price_date >= CURRENT_DATE - 240
                   ORDER BY symbol, price_date""", (syms,))
    by = _bucket(cur.fetchall(), 0)
    for s in syms:
        rows = [{"price_date": r[1], "open": _f(r[2]), "high": _f(r[3]), "low": _f(r[4]),
                 "close": _f(r[5]), "volume": _f(r[6])} for r in by.get(s, [])]
        D[s]["daily"] = rows[-160:]

    # v8_metrics latest tick
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, dma_20, dma_50, dma_200, daily_rsi, rsi_month,
                          rsi_weekly, week_return, month_return, mom_2d, week_index_52,
                          sector_week, sector_month, day_1d
                   FROM v8_metrics WHERE symbol = ANY(%s)
                     AND score_date = (SELECT MAX(score_date) FROM v8_metrics)
                   ORDER BY symbol""", (syms,))
    vk = ["dma_20", "dma_50", "dma_200", "daily_rsi", "rsi_month", "rsi_weekly",
          "week_return", "month_return", "mom_2d", "week_index_52", "sector_week", "sector_month", "day_1d"]
    v8map = {r[0]: {vk[i]: _f(r[i + 1]) for i in range(len(vk))} for r in cur.fetchall()}
    for s in syms:
        D[s]["v8"] = v8map.get(s, {k: None for k in vk})

    # gvm score + segment
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, gvm_score, segment FROM gvm_scores
                   WHERE symbol = ANY(%s) ORDER BY symbol, score_date DESC""", (syms,))
    for r in cur.fetchall():
        D[r[0]]["gvm_score"] = _f(r[1]); D[r[0]]["segment"] = r[2]
    for s in syms:
        D[s].setdefault("gvm_score", None); D[s].setdefault("segment", None)

    # peers: segment totals minus self (same numbers as the single-symbol direct query)
    cur.execute("""
        SELECT g.segment,
               COUNT(*) FILTER (WHERE v.day_1d > 1),  COUNT(*) FILTER (WHERE v.day_1d > 0),
               COUNT(*) FILTER (WHERE v.day_1d < -1), COUNT(*) FILTER (WHERE v.day_1d < -0.5),
               COUNT(*) FILTER (WHERE v.day_1d < 0),  COUNT(*)
        FROM gvm_scores g JOIN v8_metrics v ON v.symbol = g.symbol
        WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
          AND v.score_date = (SELECT MAX(score_date) FROM v8_metrics)
          AND g.segment IS NOT NULL
        GROUP BY g.segment""")
    seg = {r[0]: {"up1": int(r[1] or 0), "up": int(r[2] or 0), "dn1": int(r[3] or 0),
                  "dn05": int(r[4] or 0), "dn": int(r[5] or 0), "n": int(r[6] or 0)} for r in cur.fetchall()}
    for s in syms:
        d = D[s]; sg = seg.get(d.get("segment"))
        day = (d.get("v8") or {}).get("day_1d")
        if not sg or day is None:
            d.update({"peers_up1": 0, "peers_up": 0, "peers_dn1": 0, "peers_dn05": 0, "peers_dn": 0,
                      "peer_count": (sg["n"] - 1) if sg else 0})
        else:
            d.update({
                "peers_up1": sg["up1"] - (1 if day > 1 else 0),
                "peers_up":  sg["up"] - (1 if day > 0 else 0),
                "peers_dn1": sg["dn1"] - (1 if day < -1 else 0),
                "peers_dn05": sg["dn05"] - (1 if day < -0.5 else 0),
                "peers_dn":  sg["dn"] - (1 if day < 0 else 0),
                "peer_count": sg["n"] - 1})

    # pivots
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, pp, r1, s1, r2, s2 FROM v8_paper_pivots
                   WHERE symbol = ANY(%s) ORDER BY symbol, pivot_date DESC""", (syms,))
    pmap = {r[0]: {"pp": _f(r[1]), "r1": _f(r[2]), "s1": _f(r[3]), "r2": _f(r[4]), "s2": _f(r[5])}
            for r in cur.fetchall()}
    for s in syms:
        D[s]["pivots"] = pmap.get(s, {"pp": None, "r1": None, "s1": None, "r2": None, "s2": None})

    # latest-session 5-min bars (global latest session — same as single loader on any trading day)
    cur.execute("""SELECT symbol, open, high, low, close, volume FROM intraday_prices
                   WHERE symbol = ANY(%s) AND source='fyers_eq' AND timeframe='5m'
                     AND ts::date = (SELECT MAX(ts::date) FROM intraday_prices
                                     WHERE source='fyers_eq' AND timeframe='5m')
                   ORDER BY symbol, ts""", (syms,))
    bby = _bucket(cur.fetchall(), 0)
    for s in syms:
        D[s]["bars"] = [{"open": _f(r[1]), "high": _f(r[2]), "low": _f(r[3]),
                         "close": _f(r[4]), "volume": _f(r[5])} for r in bby.get(s, [])]

    # cmp fallback map (cmp_prices)
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, cmp FROM cmp_prices
                   WHERE symbol = ANY(%s) ORDER BY symbol, updated_at DESC""", (syms,))
    cmpmap = {r[0]: _f(r[1]) for r in cur.fetchall()}
    for s in syms:
        d = D[s]
        cv = d["bars"][-1]["close"] if d["bars"] else None
        if cv is None:
            cv = cmpmap.get(s)
        if cv is None and d["daily"]:
            cv = d["daily"][-1]["close"]
        d["cmp"] = cv

    # basis last 3 per symbol
    cur.execute("""SELECT symbol, basis_pct, oi_chg FROM futures_basis
                   WHERE symbol = ANY(%s) AND ts >= NOW() - INTERVAL '5 days'
                   ORDER BY symbol, ts DESC""", (syms,))
    fby = _bucket(cur.fetchall(), 0)
    for s in syms:
        D[s]["basis"] = [{"basis_pct": _f(r[1]), "oi_chg": _f(r[2])} for r in fby.get(s, [])[:3]]

    # events blackout set
    cur.execute("""SELECT UPPER(ticker) FROM earnings_calendar
                   WHERE ex_date IN (CURRENT_DATE, CURRENT_DATE + 1)""")
    black = {r[0] for r in cur.fetchall()}
    for s in syms:
        D[s]["event_blackout"] = s in black

    # R6 — time-adjusted intraday volume ratio (shared helper, per symbol, for exact parity)
    for s in syms:
        try:
            D[s]["vol_ratio_today"] = volume_ratio(cur, s)["ratio"]
        except Exception:
            D[s]["vol_ratio_today"] = None

    for s in syms:
        _derive(D[s])

    # cc#400 engineering: session anchor — the last trading session this scan's data reflects
    # (off-hours/weekend safe; loaders already read MAX(date), so R3/R9 use the last live session).
    cur.execute("SELECT MAX(score_date) FROM v8_metrics")
    v8_asof = cur.fetchone()[0]
    cur.execute("""SELECT MAX(ts::date) FROM intraday_prices
                   WHERE source='fyers_eq' AND timeframe='5m'""")
    bars_asof = cur.fetchone()[0]
    return D, {"nifty": {"day": nday, "wk": nwk, "mo": nmo, "src": nsrc}, "adr": adr,
               "count": len(syms), "as_of": str(v8_asof) if v8_asof else None,
               "session_bars_as_of": str(bars_asof) if bars_asof else None}


def scan(side="ALL", verdict="ALL", segment=None, limit=250):
    side = (side or "ALL").upper()
    verdict = (verdict or "ALL").upper()
    sides = ["BUY", "SELL"] if side == "ALL" else [side]
    t0 = datetime.utcnow()
    with psycopg.connect(_DB) as conn, conn.cursor() as cur:
        D, ctx = _load_bulk(cur)

    results = []
    for sym, d in D.items():
        if not d.get("daily") or d.get("cmp") is None:
            continue
        if segment and (d.get("segment") or "") != segment:
            continue
        cards = []
        for s in sides:
            ok, _g = _gates(d, s)
            if not ok:
                continue
            for st in STYLES:
                cards.append(score_card(d, st, s))
        if not cards:
            continue
        best = max(cards, key=lambda c: c["score"])
        if verdict != "ALL" and best["verdict"] != verdict:
            continue
        results.append({
            "symbol": sym, "cmp": _r(d["cmp"]), "segment": d.get("segment"),
            "best_label": best["label"], "best_score": best["score"], "verdict": best["verdict"],
            "scores": {c["label"]: c["score"] for c in cards},
        })
    results.sort(key=lambda x: x["best_score"], reverse=True)
    runtime = round((datetime.utcnow() - t0).total_seconds(), 2)
    return {"count": len(results), "runtime_s": runtime, "universe": ctx.get("count", 0),
            "side": side, "verdict": verdict, "segment": segment,
            "computed_at": _ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "as_of": ctx.get("as_of"), "session_bars_as_of": ctx.get("session_bars_as_of"),
            "spec_ref": SPEC_REF, "version": VERSION, "results": results[:limit]}


@router.get("/api/trade-check/v4/scan")
def v4_scan(side: str = "ALL", verdict: str = "ALL", segment: str = None, limit: int = 250):
    try:
        return scan(side, verdict, segment, limit)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# ── boot self-test (one-shot, gated by app_config) — lets a SQL console verify the deployed engine
@router.on_event("startup")
def _v4_selftest():
    try:
        with psycopg.connect(_DB) as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='v4dual_selftest'")
            row = cur.fetchone()
            if not row or str(row[0]).strip() != 'run':
                return
            from tc_v4_dual import trade_check_v4_dual
            probe = {}
            for s in ("RELIANCE", "TCS", "HDFCBANK", "TATASTEEL", "SUNPHARMA"):
                r = trade_check_v4_dual(s, "ALL")
                probe[s] = {"best_label": r.get("best_label"), "best_score": r.get("best_score"),
                            "verdict": r.get("best_verdict"), "error": r.get("error")}
            sc = scan("ALL", "ALL", None, 250)
            # parity check: scanner best_score vs single-symbol best_score per probe symbol
            sc_map = {x["symbol"]: x["best_score"] for x in sc.get("results", [])}
            for s in probe:
                probe[s]["scan_score"] = sc_map.get(s)
                probe[s]["match"] = (probe[s]["best_score"] == sc_map.get(s))
            # cc#400: SELL verdict distribution + score ceiling (best SELL card per symbol that clears gates)
            scs = scan("SELL", "ALL", None, 250)
            sdist = {"STRONG": 0, "VALID": 0, "REJECT": 0}
            for x in scs.get("results", []):
                sdist[x["verdict"]] = sdist.get(x["verdict"], 0) + 1
            sell_ceiling = max((x["best_score"] for x in scs.get("results", [])), default=None)
            out = {"probe": probe, "scan_count": sc.get("count"), "scan_runtime_s": sc.get("runtime_s"),
                   "universe": sc.get("universe"), "as_of": sc.get("as_of"),
                   "sell_dist": sdist, "sell_scored": scs.get("count"), "sell_ceiling": sell_ceiling,
                   "at": _ist().strftime("%Y-%m-%d %H:%M:%S IST")}
            cur.execute("""INSERT INTO app_config(key, value, updated_at)
                           VALUES('v4dual_selftest_result', %s, NOW())
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                        (json.dumps(out, default=str)[:60000],))
            cur.execute("UPDATE app_config SET value='done', updated_at=NOW() WHERE key='v4dual_selftest'")
            conn.commit()
    except Exception:
        pass

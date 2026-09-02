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
from tc_v4_dual import (_f, _r, _derive, score_card, _verdict,
                        STYLES, _ist, SPEC_REF, VERSION,
                        _sector_aggs, _nifty_ret63,   # cc#586: R18/R19 sector + nifty-RS shared helpers
                        _segment_peer_rows, _peer_counts)   # cc#717 part_3: shared R3 peer helpers (parity)

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

    # gvm score + segment (+ v_score for R17 [cc#583 parity fix], m_score for R18)
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, gvm_score, segment, v_score, m_score FROM gvm_scores
                   WHERE symbol = ANY(%s) ORDER BY symbol, score_date DESC""", (syms,))
    for r in cur.fetchall():
        D[r[0]]["gvm_score"] = _f(r[1]); D[r[0]]["segment"] = r[2]
        D[r[0]]["v_score"] = _f(r[3]); D[r[0]]["m_score"] = _f(r[4])
    for s in syms:
        D[s].setdefault("gvm_score", None); D[s].setdefault("segment", None)
        D[s].setdefault("v_score", None); D[s].setdefault("m_score", None)

    # cc#584/585/586: R19 nifty-RS (once) + R18 sector-M / R19 sector-RS (per distinct segment, cached
    # via the SAME shared helper as the single loader -> identical numbers) + R20 ΔGVM180 (set-based).
    nret63 = _nifty_ret63(cur)
    _seg_cache = {}
    for s in syms:
        seg = D[s].get("segment")
        if seg not in _seg_cache:
            _seg_cache[seg] = _sector_aggs(cur, seg)
        sa = _seg_cache[seg]
        D[s]["sector_m"] = sa["sector_m"]
        D[s]["sector_ret63"] = sa["sector_ret63"]
        D[s]["sector_n_ret"] = sa["n_ret"]
        D[s]["nifty_ret63"] = nret63
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, gvm_score FROM gvm_history
                   WHERE symbol = ANY(%s) AND gvm_score IS NOT NULL AND score_date <= CURRENT_DATE - 180
                   ORDER BY symbol, score_date DESC""", (syms,))
    gh180 = {r[0]: _f(r[1]) for r in cur.fetchall()}
    for s in syms:
        D[s]["gvm180"] = gh180.get(s)
    # cc#936 / 18078 — m_score at the SAME 180-day anchor, for the SELL R18 delta-M rule. Deliberately
    # a SEPARATE query from the gvm one above rather than one two-column read: the m_score series can
    # have a NULL where gvm_score does not, and DISTINCT ON must pick the newest row that has the
    # column being asked for. Same table, same anchor, same at-or-before rule as _m180 in the single
    # loader, so the scanner and /check resolve the identical snapshot. ΔM itself is computed once, in
    # the shared _derive — neither loader does that arithmetic.
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, m_score FROM gvm_history
                   WHERE symbol = ANY(%s) AND m_score IS NOT NULL AND score_date <= CURRENT_DATE - 180
                   ORDER BY symbol, score_date DESC""", (syms,))
    mh180 = {r[0]: _f(r[1]) for r in cur.fetchall()}
    for s in syms:
        D[s]["m180"] = mh180.get(s)

    # peers: cc#717 part_3 — same shared helpers as the single-symbol loader (gvm_scores top-10-mcap
    # peers + live bulk day%, NOT the old gvm_scores⋈v8_metrics INNER JOIN that zeroed cash peers).
    # Segment rows fetched once per distinct segment (cached), then top-10-excl-self counted per symbol
    # -> byte-identical R3 to the single card (SHARED-MODULE CONTRACT preserved).
    _peer_seg_cache = {}
    for s in syms:
        d = D[s]; segn = d.get("segment")
        if not segn:
            d.update({"peers_up1": 0, "peers_up": 0, "peers_dn1": 0, "peers_dn05": 0, "peers_dn": 0, "peer_count": 0})
            continue
        if segn not in _peer_seg_cache:
            _peer_seg_cache[segn] = _segment_peer_rows(cur, segn)
        d.update(_peer_counts(_peer_seg_cache[segn], s))

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

    # events blackout set (cc#451: 3-day earnings lookahead + capture the imminent result date so the
    # scanner's G2 gate matches the single-symbol /check evaluation exactly)
    cur.execute("""SELECT UPPER(ticker), MIN(ex_date) FROM earnings_calendar
                   WHERE ex_date BETWEEN CURRENT_DATE AND CURRENT_DATE + 2 GROUP BY UPPER(ticker)""")
    black = {r[0]: r[1] for r in cur.fetchall()}
    for s in syms:
        D[s]["event_blackout"] = s in black
        D[s]["event_date"] = black[s].isoformat() if (s in black and black[s]) else None

    # cc#935 / 18064 — R24 Delivery confirm inputs, set-based. Byte-identical window to the single
    # loader (_load_one): 3 most recent sessions vs the symbol's own trailing UP-TO-21, ranked per
    # symbol, NULL deliv_pct excluded before ranking. Same four fields, so R24 cannot disagree
    # between the scanner and /check.
    cur.execute("""SELECT s,
                          avg(deliv_pct) FILTER (WHERE rn <= 3),  count(*) FILTER (WHERE rn <= 3),
                          avg(deliv_pct) FILTER (WHERE rn <= 21), count(*) FILTER (WHERE rn <= 21)
                   FROM (SELECT UPPER(symbol) s, deliv_pct,
                                row_number() OVER (PARTITION BY UPPER(symbol) ORDER BY d DESC) rn
                         FROM delivery_eod
                         WHERE UPPER(symbol) = ANY(%s) AND deliv_pct IS NOT NULL) t
                   GROUP BY s""", (syms,))
    dlv = {r[0]: (_f(r[1]), int(r[2] or 0), _f(r[3]), int(r[4] or 0)) for r in cur.fetchall()}
    for s in syms:
        a3, n3, a21, n21 = dlv.get(s, (None, 0, None, 0))
        D[s].update({"deliv_3d": a3, "deliv_n3": n3, "deliv_21d": a21, "deliv_n21": n21})

    # cc#1441: LEGACY T-factor read for the LOCKED 18062 dual-rulebook vol tests (exact parity
    # with tc_v4_dual). R6/R7 themselves moved to r6_read (canon V2); ruling pending on these.
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

    # cc#405: V8 basket membership for the latest signal_date (display-only join, isolation id=244)
    cur.execute("""SELECT UPPER(symbol), array_agg(DISTINCT basket)
                   FROM v8_qualified WHERE signal_date = (SELECT MAX(signal_date) FROM v8_qualified)
                   GROUP BY UPPER(symbol)""")
    v8_baskets = {r[0]: r[1] for r in cur.fetchall()}

    return D, {"nifty": {"day": nday, "wk": nwk, "mo": nmo, "src": nsrc}, "adr": adr,
               "count": len(syms), "as_of": str(v8_asof) if v8_asof else None,
               "session_bars_as_of": str(bars_asof) if bars_asof else None,
               "v8_baskets": v8_baskets}


def _segment_day_map(cur):
    """cc#455: mcap-weighted DAY change per segment — SUM(day_1d*mcap)/SUM(mcap) anchored to the last
    v8_metrics session (same derive as /segment_day, cc#429/#432). Returns {segment: day_pct} for the
    scanner's Sector Day% column; off-market it serves the last session's finals (cc#424 convention)."""
    try:
        cur.execute("""
            WITH mem AS (
                SELECT g.segment, m.day_1d::numeric AS day_1d, g.market_cap::numeric AS mcap
                FROM v8_metrics m JOIN gvm_scores g ON g.symbol = m.symbol
                WHERE m.score_date = (SELECT MAX(score_date) FROM v8_metrics)
                  AND g.segment IS NOT NULL AND m.day_1d IS NOT NULL
                  AND g.market_cap IS NOT NULL AND g.market_cap > 0)
            SELECT segment, ROUND(SUM(day_1d*mcap)/NULLIF(SUM(mcap),0), 2) FROM mem GROUP BY segment
        """)
        return {r[0]: _f(r[1]) for r in cur.fetchall()}
    except Exception:
        return {}


def scan(side="ALL", verdict="ALL", segment=None, limit=250, progress=None):
    side = (side or "ALL").upper()
    verdict = (verdict or "ALL").upper()
    sides = ["BUY", "SELL"] if side == "ALL" else [side]
    t0 = datetime.utcnow()
    with psycopg.connect(_DB) as conn, conn.cursor() as cur:
        D, ctx = _load_bulk(cur)
        seg_day = _segment_day_map(cur)   # cc#455: mcap-weighted segment day% for the Sector Day% column

    results = []
    # cc#1593 (founder 20:40 amendment): the scan reports its own progress. `progress(scored, total,
    # symbol)` is called before each symbol is scored and once more at the end with symbol=None, so
    # a caller can draw a ring that fills from real work, never from a timer. The eligible list is
    # fixed up front so `total` cannot move while the ring is filling. A failing callback is
    # swallowed: progress is a window onto the scan, not a part of it.
    todo = [(sym, d) for sym, d in D.items()
            if d.get("daily") and d.get("cmp") is not None
            and not (segment and (d.get("segment") or "") != segment)]
    n_total = len(todo)

    def _tick(i, sym):
        if progress is None:
            return
        try:
            progress(i, n_total, sym)
        except Exception:
            pass

    for i, (sym, d) in enumerate(todo):
        _tick(i, sym)
        # cc#677: ZERO-VETO — score every side/style; the verdict is score bands alone (no gate filter).
        cards = []
        for s in sides:
            for st in STYLES:
                cards.append(score_card(d, st, s))
        if not cards:
            continue
        # cc#1033 (TC_BEST_OF_FOUR_V1, session_log 22353): best is the highest score/max PERCENTAGE,
        # identical to _compute_result in tc_v4_dual.py. This is the SHARED-MODULE CONTRACT the
        # amendment names: the scanner picks its best the same way the single-symbol check does, or
        # the two disagree about the same stock and the parity probe below stops meaning anything.
        # The cards themselves are still scored by the locked rulebook — only the CHOICE changes.
        best = max(cards, key=lambda c: (c["score"] / c["max"]) if c.get("max") else 0)
        if verdict != "ALL" and best["verdict"] != verdict:
            continue
        # cc#405: failed rule ids for the best card (0-scored only; 0.5 partials excluded) + V8 basket
        failed = [{"rule": r["rule"], "label": r["label"]} for r in best.get("rules", []) if r["credit"] == 0]
        results.append({
            "symbol": sym, "cmp": _r(d["cmp"]), "segment": d.get("segment"),
            "day_chg": _r((d.get("v8") or {}).get("day_1d")),   # cc#413: day change % (last session, cc#373 convention)
            "sector_day": seg_day.get(d.get("segment")),   # cc#455: segment mcap-weighted day % (last-tick)
            "gvm": _r(d.get("gvm_score")),                  # cc#455: for the top-10 tie-break
            "best_label": best["label"], "best_score": best["score"], "verdict": best["verdict"],
            # cc#1222: score100 has ridden on every card since cc#1209 (score10 x 10, same
            # numerator and denominator) — the scan payload simply was not forwarding it. The
            # scanner annotations are all expressed on the /100 scale, so it is forwarded here
            # rather than recomputed anywhere downstream.
            "best_score100": best.get("score100"),
            # cc#1593: the two flags a /100 surface needs beside score100, both already on the
            # card since cc#1172 — forwarded, not recomputed. verdict10 is None when the bucket is
            # unweighted, and a surface must print "unweighted" then rather than band a number the
            # registry did not calibrate.
            "best_verdict10": best.get("verdict10"),
            "best_score10_weighted": bool(best.get("score10_weighted")),
            # cc#1033: the ratio the choice was made on, mirroring best_pct in the dual payload.
            "best_pct": (round(best["score"] / best["max"], 4) if best.get("max") else None),
            "scores": {c["label"]: c["score"] for c in cards},
            "failed_rules": failed,
            "v8_basket": ctx.get("v8_baskets", {}).get(sym, []),
        })
    _tick(n_total, None)
    results.sort(key=lambda x: x["best_score"], reverse=True)
    # ── cc#1222 TC_SCANNER_GATED_CONFIG_V1.1 — MARK, DO NOT CUT (session_log 29448) ─────────────
    # OBSERVATION MODE: every row that was scored still ships. annotate() adds the bucket bar, the
    # three side gates, the rank inside the bucket today and a strict would_qualify flag; it cannot
    # remove a row because it returns marks for the list it was handed. The gates read the SAME
    # v8_metrics tick the scoring read — _load_bulk already pulled sector_week, sector_month and
    # rsi_month into d["v8"] — so the strip costs no extra query and cannot disagree with the score
    # about which session it is describing.
    #
    # Annotated BEFORE the limit slice on purpose: rank-in-bucket and the would-qualify count are
    # statements about the whole day's scan, and ranking a truncated list would renumber the tail
    # and quietly shrink the count in the header.
    tc_summary = None
    try:
        from tc_scanner_config import annotate as _tc_annotate, TC_SCANNER_CONFIG
        tc_summary = _tc_annotate(results, {s: (d.get("v8") or {}) for s, d in D.items()})
        tc_summary["config_version"] = TC_SCANNER_CONFIG["version"]
    except Exception as e:
        # A broken annotator must not take the scan down with it — the scores are the product here
        # and the marks are a layer on top. Named, never silently dropped.
        tc_summary = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    runtime = round((datetime.utcnow() - t0).total_seconds(), 2)
    return {"count": len(results), "runtime_s": runtime, "universe": ctx.get("count", 0),
            "side": side, "verdict": verdict, "segment": segment, "tc_config": tc_summary,
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
            from tc_resolver import get_primary_styles   # cc#1549: resolver, not a hardcoded tc_v4_dual import
            probe = {}
            for s in ("RELIANCE", "TCS", "HDFCBANK", "TATASTEEL", "SUNPHARMA"):
                r = get_primary_styles()(s, "ALL")
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

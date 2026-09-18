"""v8_live_tc.py -- cc#2214: the TC /100 for EVERY qualified row on the four /v8 bucket tabs,
resolved LIVE through tc_resolver.get_primary_styles() -- the canonical style-level scorer -- and
never joined from a book-scoped table.

WHY. The bucket tabs' "Qualified today, entry gated" table took its TC /100 from the tcScores map
the page loads from /api/trade-check/position-stars-v2 (tc_position_stars_v2, DISTINCT ON
symbol|side over ALL history). That table is written only for OPEN positions, so a qualified name
that was never in the book (INFY, KPITTECH on 18-Sep) has no row and rendered a bare "--", while a
name that LEFT the book kept rendering its last stored star (TATAELXSI 81.9 from 08-Sep, HCLTECH
55.5 from 15-Sep) as if it were today's. Both are the same defect: the surface was asking the
wrong source. The qualified table is candidate-scoped, so its score must be computed for the
candidate, now.

ONE SCORER, ONE PICK. The score is the best of the four style cards on the cc#1033 locked ratio
(score / max), chosen by the scorer's own best_card through tc_resolver.get_primary_best_card() --
the same selection the position-star batch makes, so an in-book name resolves to the same number
the tick table holds at the same moment (no scoring drift). score100 = score10 x 10 (TC_SCORE_100_V1,
29138) straight off the card; nothing is re-derived here. No versioned tc_* import anywhere in this
file (cc#738 / cc#1549 guard).

HONEST FAILURE. A symbol the scorer cannot resolve gets tc_error (the reason, never a bare null),
so the page can draw an explicit affordance instead of a "--" that is indistinguishable from
"no signal". Each symbol is scored once per _TTL_S across the four basket calls a page load makes
(the raw scorer result is cached, the side-dependent fields are computed per call); failures are
never cached.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

log = logging.getLogger("v8_live_tc")
IST = timezone(timedelta(hours=5, minutes=30))

_WORKERS = 6          # the scorer opens its own short DB connection per call; 6 in flight is plenty
_TTL_S = 60.0         # one page load fetches four baskets; a symbol is scored once for all of them
_CACHE = {}           # symbol -> (monotonic ts, raw scorer result)
_LOCK = threading.Lock()

ENGINE = "tc_resolver.get_primary_styles, best of four on the cc#1033 ratio"


def side_for(basket) -> str:
    """The direction a bucket tab trades: sell_* baskets are SELL, everything else BUY."""
    return "SELL" if str(basket or "").lower().startswith("sell") else "BUY"


def fields_from_result(res, want_side, best_card) -> dict:
    """Pure: the tc_* display fields from ONE scorer result. Never raises, never fabricates.

    tc_score      best card's score100 (any direction) -- the column value
    tc_bucket     that card's label (BUY-REV / BUY-MOM / SELL-REV / SELL-MOM) -- the capsule
    tc_band       that card's verdict10 (None while the bucket is uncalibrated, exactly as scored)
    tc_opposes    True when the best card is against the tab's own side
    tc_side_*     the best card ON the tab's side, for the hover
    tc_error      the reason when there is no honest number to show
    """
    if not isinstance(res, dict) or res.get("error"):
        err = (res.get("error") if isinstance(res, dict) else None) or "trade check returned no result"
        return {"tc_score": None, "tc_error": str(err)[:160]}
    cards = res.get("cards") or []
    best = best_card(cards)
    if best is None:
        return {"tc_score": None, "tc_error": "no card scored"}
    label = best.get("label")
    if best.get("score100") is None:
        return {"tc_score": None, "tc_bucket": label,
                "tc_error": f"{label or 'best card'}: scored {best.get('score')}/{best.get('max')} but carries no /100"}
    own = best_card(cards, want_side) or {}
    return {
        "tc_score": best.get("score100"),
        "tc_bucket": label,
        "tc_band": best.get("verdict10"),
        "tc_opposes": (not str(label).upper().startswith(want_side)) if label else None,
        "tc_side_bucket": own.get("label"),
        "tc_side_score": own.get("score100"),
        "tc_weighted": bool(best.get("score10_weighted")),
        "tc_error": None,
    }


def clear_cache():
    with _LOCK:
        _CACHE.clear()


def resolve_many(symbols, want_side, scorer=None, best_card=None, workers=_WORKERS, ttl=_TTL_S) -> dict:
    """symbol -> tc_* fields for every distinct symbol, scored through the resolver, in parallel.
    `scorer` / `best_card` default to the resolver's; tests pass fakes. A scorer exception becomes
    that symbol's tc_error and never touches the others."""
    if scorer is None or best_card is None:
        from tc_resolver import get_primary_styles, get_primary_best_card   # cc#738: resolver only
        scorer = scorer or get_primary_styles()
        best_card = best_card or get_primary_best_card()
    syms = list(dict.fromkeys(s for s in symbols if s))
    raw, todo = {}, []
    now = time.monotonic()
    with _LOCK:
        for s in syms:
            hit = _CACHE.get(s)
            if hit and (now - hit[0]) < ttl:
                raw[s] = hit[1]
            else:
                todo.append(s)

    def one(sym):
        try:
            return scorer(sym, "ALL")
        except Exception as e:            # one bad symbol must not lose the table
            return {"error": f"{type(e).__name__}: {str(e)[:120]}"}

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as ex:
            results = list(ex.map(one, todo))
        stamp = time.monotonic()
        with _LOCK:
            for sym, res in zip(todo, results):
                raw[sym] = res
                if isinstance(res, dict) and not res.get("error"):
                    _CACHE[sym] = (stamp, res)     # failures are never cached: the next call retries
    return {s: fields_from_result(raw.get(s), want_side, best_card) for s in syms}


def attach_live_tc(rows, basket, **kw) -> dict:
    """Mutate every row dict in `rows` with its tc_* fields and return a summary for the payload."""
    if not rows:
        return {"resolved": 0, "failed": 0, "engine": ENGINE}
    want = side_for(basket)
    got = resolve_many([r.get("symbol") for r in rows], want, **kw)
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    ok = failed = 0
    for r in rows:
        f = got.get(r.get("symbol")) or {"tc_score": None, "tc_error": "not resolved"}
        r.update(f)
        r["tc_source"] = "live"
        r["tc_ts"] = stamp
        if f.get("tc_error"):
            failed += 1
        else:
            ok += 1
    if failed:
        log.warning("v8_live_tc %s: %d of %d rows did not resolve", basket, failed, len(rows))
    return {"resolved": ok, "failed": failed, "engine": ENGINE, "as_of": stamp}

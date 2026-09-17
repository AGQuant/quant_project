"""stock_view_chart.py -- cc#2139 STOCK VIEWS CHART CARD, the server half.

A Stock View can carry ONE attachment (cc#2140, polished_news_images): a chart image drawn by
Claude/Fable at publish time plus the setup it shows (symbol, side, entry, target, SL). This module
turns that row into the `chart` field BOTH news payloads ship -- /api/news/polished (web /intel)
and /api/mobile/intel (app) -- so the two pages render from ONE shape and ONE formula:

    unrealised_pct = (entry - cmp) / entry * 100   for a SELL
                   = (cmp - entry) / entry * 100   for a BUY

CMP is resolved live through cmp_resolver.resolve_cmp_many -- the same number every other surface
shows for the symbol at that moment -- and is never stored. cmp_live says whether it is a session
tick or the last close, and the page labels the cell accordingly (never a stale number dressed as
live). Nothing here recomputes a level: entry / target / SL are the row's own numbers.

overlay(cur, rows, id_key) sets row["chart"] = dict | None on every row and returns how many rows
got one. It raises on a DB error like any query -- the caller wraps it and rolls back (both callers
do), so a hosting problem never blanks the news page. A CMP failure is caught here and degrades to
cmp None: the chart and the levels still show, the two live cells read '--'.

`chart` shape (both payloads, identical):
    image_id, url, content_type, width_px, height_px,
    symbol, side ('BUY'|'SELL'|None), setup_label, as_of,
    entry, target, sl, cmp_at_publish,          -- the row's numbers, floats or None
    cmp, cmp_live, cmp_source, cmp_ts,          -- live
    unrealised_pct, has_levels
"""
import logging

log = logging.getLogger("scorr.stock_view_chart")


def unrealised_pct(side, entry, cmp_v):
    """ONE formula. SELL earns when price falls below entry; BUY when it rises above it."""
    if entry is None or cmp_v is None or float(entry) <= 0 or float(cmp_v) <= 0:
        return None   # a zero or negative price is a missing price, never a -100% read
    if side == "SELL":
        return round((float(entry) - float(cmp_v)) / float(entry) * 100.0, 2)
    if side == "BUY":
        return round((float(cmp_v) - float(entry)) / float(entry) * 100.0, 2)
    return None


def overlay(cur, rows, id_key="id"):
    from image_assets_endpoints import attachments   # local import: keeps startup order trivial
    for r in rows:
        r["chart"] = None
    ids = [r.get(id_key) for r in rows if r.get(id_key) is not None]
    if not ids:
        return 0
    att = attachments(cur, ids)
    if not att:
        return 0
    syms = sorted({(a.get("symbol") or "").upper() for a in att.values() if a.get("symbol")})
    quotes = {}
    if syms:
        try:
            import cmp_resolver
            quotes = cmp_resolver.resolve_cmp_many(cur, syms) or {}
        except Exception as e:   # the card still renders; the live cells read '--'
            log.warning(f"stock_view_chart cmp resolve failed: {e}")
            quotes = {}
    n = 0
    for r in rows:
        rid = r.get(id_key)
        a = att.get(int(rid)) if rid is not None else None
        if not a:
            continue
        q = quotes.get((a.get("symbol") or "").upper()) or {}
        cmp_v = q.get("cmp")
        cmp_v = float(cmp_v) if cmp_v is not None else None
        has_levels = a.get("entry") is not None and a.get("side") in ("BUY", "SELL")
        c = dict(a)
        c.update({
            "cmp": cmp_v,
            "cmp_live": bool(q.get("live")),
            "cmp_source": q.get("source"),
            "cmp_ts": str(q.get("ts")) if q.get("ts") else None,
            "unrealised_pct": unrealised_pct(a.get("side"), a.get("entry"), cmp_v) if has_levels else None,
            "has_levels": has_levels,
        })
        r["chart"] = c
        n += 1
    return n

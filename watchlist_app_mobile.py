"""
watchlist_app_mobile.py — cc#2196 WATCHLIST V1 (founder 17-Sep-2026 15:17 IST). Replaces mobile_watchlist_stub.py
(cc#1897, the honest-empty page: "no watchlist store exists" — it did not, until this card).

STORE (item 1, CREATE-only, idempotent at first use — the cc#2095 custom_alerts pattern; no ALTER, no DROP):
  user_watchlists       (id, user_id, name, position, created_at)          max 5 per user — guarded in the
                                                                             API under a per-user advisory
                                                                             lock (a CHECK cannot count rows)
  user_watchlist_items  (watchlist_id, symbol, added_at, added_from, gvm_at_add, price_at_add)
                                                                             unique (watchlist_id, symbol);
                                                                             the two _at_add columns are
                                                                             captured at insert so "what
                                                                             changed since you added it" is
                                                                             a plain diff later.
USER IDENTITY — data-source gate answered (Role Split: CC greps and answers): auth_sessions carries NO user
id (columns: token, created_at, expires_at — one app password, per-login tokens; scorr_auth.py). There is no
per-user identity anywhere in the app today, so every authenticated session is the one app user and the
store is keyed on USER_ID = "founder". The column is real so the day a login carries a user id, nothing here
changes shape. Stated on the card, not assumed.

RATING + PRICE (items 2, 4): gvm_history latest row per symbol (score, verdict, segment, score_date) — the
same read hr_report._load_gvm makes; CMP from cmp_prices (latest raw_prices close as the fallback, the same
order hr_report._load_cmp uses); day % = cmp vs prev_close.prev_session_close_many, the anchor and formula
get_v8_live_metrics and custom_alerts already use. Nothing re-derived; company names from input_raw.

API (all _guard-protected, mobile router):
  GET    /api/mobile/watchlists                       lists + counts + symbols + avg day % + top mover
  POST   /api/mobile/watchlists            {name}     create (max 5; name 1-40 chars; unique per user)
  PATCH  /api/mobile/watchlists/{id}       {name}     rename
  DELETE /api/mobile/watchlists/{id}                  delete the list and its items
  GET    /api/mobile/watchlists/{id}/items            rows with rating + CMP + day % + added-on, day % desc
  POST   /api/mobile/watchlists/{id}/items {symbol, surface}   add (captures gvm_at_add / price_at_add)
  DELETE /api/mobile/watchlists/{id}/items/{symbol}   remove
Pages: /m/mywatchlist (the My Scorr tile's route) and /m/watchlist (the card's name; same page).
"""
import logging
import re
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from mobile_endpoints import _conn, _guard, _json_safe, _page

log = logging.getLogger("scorr.mobile.watchlist")
router = APIRouter()

USER_ID = "founder"          # see USER IDENTITY in the module docstring
MAX_LISTS = 5
NAME_MAX = 40
SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&._\-]{0,19}$")
IST = timezone(timedelta(hours=5, minutes=30))
MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

DDL = (
    """CREATE TABLE IF NOT EXISTS user_watchlists (
           id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
           position INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_user_watchlists_user_name ON user_watchlists (user_id, lower(name))""",
    """CREATE TABLE IF NOT EXISTS user_watchlist_items (
           watchlist_id BIGINT NOT NULL REFERENCES user_watchlists(id) ON DELETE CASCADE,
           symbol TEXT NOT NULL, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), added_from TEXT,
           gvm_at_add NUMERIC, price_at_add NUMERIC, PRIMARY KEY (watchlist_id, symbol))""",
    """CREATE INDEX IF NOT EXISTS ix_user_watchlist_items_symbol ON user_watchlist_items (symbol)""",
)


def _ensure(cur):
    for stmt in DDL:
        cur.execute(stmt)


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _r(v, d=2):
    v = _f(v)
    return None if v is None else round(v, d)


def ist(ts):
    """'17 Sep 26' and the ISO stamp for a timestamptz; (None, None) when absent."""
    if ts is None:
        return None, None
    d = ts if isinstance(ts, datetime) else None
    if d is None:
        try:
            d = datetime.fromisoformat(str(ts).replace("T", " "))
        except ValueError:
            return None, None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    i = d.astimezone(IST)
    return f"{i.day} {MON[i.month - 1]} {i:%y}", i.isoformat()


def validate_name(name):
    """(clean name, error). 1-40 chars after trimming, no control characters."""
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    if not s:
        return None, "Give the watchlist a name."
    if len(s) > NAME_MAX:
        return None, f"Keep the name under {NAME_MAX} characters."
    return s, None


def validate_symbol(symbol):
    s = str(symbol or "").strip().upper()
    if not SYMBOL_RE.match(s):
        return None, "That does not look like an NSE symbol."
    return s, None


def shape_lists(lists, items, day):
    """Pure. lists = [(id, name, position, created_at)], items = [(watchlist_id, symbol)],
    day = {symbol: day_pct}. Adds count, symbols, avg day % over the symbols that have one, top mover."""
    by = {}
    for wid, sym in items or []:
        by.setdefault(wid, []).append(sym)
    out = []
    for wid, name, pos, created in lists or []:
        syms = by.get(wid, [])
        moves = [(s, day.get(s)) for s in syms if day.get(s) is not None]
        avg = round(sum(m for _, m in moves) / len(moves), 2) if moves else None
        top = max(moves, key=lambda x: x[1]) if moves else None
        out.append({"id": wid, "name": name, "position": pos, "created_at": ist(created)[1], "count": len(syms),
                    "symbols": syms, "avg_day_pct": avg, "priced": len(moves),
                    "top_mover": ({"symbol": top[0], "day_pct": top[1]} if top else None)})
    return out


def shape_items(rows, prices, prev, ratings, names):
    """Pure. rows = [(symbol, added_at, added_from, gvm_at_add, price_at_add)]; prices = {sym: (cmp, as_of)};
    prev = {sym: prev_close}; ratings = {sym: {gvm, verdict, segment, score_date}}; names = {sym: company}.
    Sorted by day % desc, unpriced last. Nulls stay nulls."""
    from prev_close import day_pct
    out = []
    for sym, added_at, added_from, gvm0, px0 in rows or []:
        cmp_, as_of = (prices or {}).get(sym, (None, None))
        r = (ratings or {}).get(sym) or {}
        dp = day_pct(cmp_, (prev or {}).get(sym))
        gvm0, px0, gvm_now = _f(gvm0), _f(px0), _f(r.get("gvm"))
        since = round((cmp_ / px0 - 1) * 100.0, 2) if (cmp_ is not None and px0) else None
        out.append({"symbol": sym, "company_name": (names or {}).get(sym), "segment": r.get("segment"),
                    "gvm": _r(gvm_now), "verdict": r.get("verdict"), "score_date": r.get("score_date"),
                    "cmp": _r(cmp_), "cmp_as_of": as_of, "day_pct": dp,
                    "added_on": ist(added_at)[0], "added_at": ist(added_at)[1], "added_from": added_from,
                    "gvm_at_add": _r(gvm0), "price_at_add": _r(px0), "since_add_pct": since,
                    "gvm_delta": (round(gvm_now - gvm0, 2) if (gvm_now is not None and gvm0 is not None) else None)})
    out.sort(key=lambda x: (x["day_pct"] is None, -(x["day_pct"] or 0)))
    return out


# ── reads ─────────────────────────────────────────────────────────────────────────────────────────
def _prices(cur, syms):
    """{sym: (cmp, as_of)} — cmp_prices first, latest raw_prices close for anything missing."""
    out = {}
    if not syms:
        return out
    cur.execute("SELECT symbol, cmp, updated_at FROM cmp_prices WHERE symbol = ANY(%s)", (syms,))
    for s, c, u in cur.fetchall():
        if c is not None:
            out[s] = (_f(c), str(u) if u else None)
    missing = [s for s in syms if s not in out]
    if missing:
        cur.execute("""SELECT DISTINCT ON (symbol) symbol, close, price_date FROM raw_prices
                       WHERE symbol = ANY(%s) ORDER BY symbol, price_date DESC""", (missing,))
        for s, c, d in cur.fetchall():
            if c is not None:
                out[s] = (_f(c), str(d))
    return out


def _prev(cur, syms):
    if not syms:
        return {}
    try:
        from prev_close import prev_session_close_many
        return {s: v[0] for s, v in prev_session_close_many(cur, syms).items()}
    except Exception as e:   # a prev-close hiccup must not blank the list — day % goes null, honestly
        log.warning("watchlist prev close failed: %s", e)
        return {}


def _ratings(cur, syms):
    if not syms:
        return {}
    cur.execute("""SELECT DISTINCT ON (symbol) symbol, gvm_score, verdict, segment, score_date FROM gvm_history
                   WHERE symbol = ANY(%s) ORDER BY symbol, score_date DESC""", (syms,))
    return {s: {"gvm": _f(g), "verdict": v, "segment": seg, "score_date": str(d) if d else None} for s, g, v, seg, d in cur.fetchall()}


def _names(cur, syms):
    if not syms:
        return {}
    cur.execute("SELECT nse_code, company_name FROM input_raw WHERE nse_code = ANY(%s)", (syms,))
    return {s: n for s, n in cur.fetchall()}


def capture_at_add(cur, symbol):
    """(gvm_at_add, price_at_add) at the moment of the add — latest gvm_history score and the live CMP
    (cmp_prices, else latest close). Either may be None; a null is stored as null, never 0."""
    cur.execute("SELECT gvm_score FROM gvm_history WHERE symbol=%s ORDER BY score_date DESC LIMIT 1", (symbol,))
    g = cur.fetchone()
    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
    p = cur.fetchone()
    px = _f(p[0]) if p else None
    if px is None:
        cur.execute("SELECT close FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1", (symbol,))
        p2 = cur.fetchone()
        px = _f(p2[0]) if p2 else None
    return (_f(g[0]) if g else None), px


def _own(cur, wid):
    cur.execute("SELECT id, name, position, created_at FROM user_watchlists WHERE id=%s AND user_id=%s", (wid, USER_ID))
    return cur.fetchone()


def _err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


# ── endpoints ─────────────────────────────────────────────────────────────────────────────────────
@router.get("/api/mobile/watchlists")
@_json_safe
def watchlists(request: Request):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        conn.commit()
        cur.execute("SELECT id, name, position, created_at FROM user_watchlists WHERE user_id=%s ORDER BY position, id", (USER_ID,))
        lists = cur.fetchall()
        ids = [l[0] for l in lists]
        items = []
        if ids:
            cur.execute("SELECT watchlist_id, symbol FROM user_watchlist_items WHERE watchlist_id = ANY(%s) ORDER BY added_at DESC", (ids,))
            items = cur.fetchall()
        syms = sorted({s for _, s in items})
        prices = _prices(cur, syms)
        prev = _prev(cur, syms)
    from prev_close import day_pct
    day = {s: day_pct(prices.get(s, (None, None))[0], prev.get(s)) for s in syms}
    out = shape_lists(lists, items, day)
    return {"lists": out, "count": len(out), "max": MAX_LISTS, "can_create": len(out) < MAX_LISTS,
            "symbols": sorted({s for _, s in items}), "user_id": USER_ID}


@router.post("/api/mobile/watchlists")
async def watchlist_create(request: Request):
    g = _guard(request)
    if g:
        return g
    body = await request.json()
    name, err = validate_name((body or {}).get("name"))
    if err:
        return _err(err)
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (USER_ID,))   # the max-5 guard is race-free
        cur.execute("SELECT count(*), COALESCE(max(position), 0) FROM user_watchlists WHERE user_id=%s", (USER_ID,))
        n, maxpos = cur.fetchone()
        if int(n) >= MAX_LISTS:
            conn.rollback()
            return _err(f"You already have {MAX_LISTS} watchlists — the most allowed. Delete one to add another.", 409)
        cur.execute("SELECT 1 FROM user_watchlists WHERE user_id=%s AND lower(name)=lower(%s)", (USER_ID, name))
        if cur.fetchone():
            conn.rollback()
            return _err(f"You already have a watchlist called {name}.", 409)
        cur.execute("INSERT INTO user_watchlists (user_id, name, position) VALUES (%s, %s, %s) RETURNING id, created_at",
                    (USER_ID, name, int(maxpos) + 1))
        wid, created = cur.fetchone()
        conn.commit()
    return {"status": "ok", "list": {"id": wid, "name": name, "position": int(maxpos) + 1, "count": 0, "symbols": [],
                                     "created_at": ist(created)[1]}}


@router.patch("/api/mobile/watchlists/{wid}")
async def watchlist_rename(wid: int, request: Request):
    g = _guard(request)
    if g:
        return g
    body = await request.json()
    name, err = validate_name((body or {}).get("name"))
    if err:
        return _err(err)
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        if not _own(cur, wid):
            return _err("No such watchlist.", 404)
        cur.execute("SELECT 1 FROM user_watchlists WHERE user_id=%s AND lower(name)=lower(%s) AND id<>%s", (USER_ID, name, wid))
        if cur.fetchone():
            return _err(f"You already have a watchlist called {name}.", 409)
        cur.execute("UPDATE user_watchlists SET name=%s WHERE id=%s", (name, wid))
        conn.commit()
    return {"status": "ok", "id": wid, "name": name}


@router.delete("/api/mobile/watchlists/{wid}")
def watchlist_delete(wid: int, request: Request):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        if not _own(cur, wid):
            return _err("No such watchlist.", 404)
        cur.execute("DELETE FROM user_watchlist_items WHERE watchlist_id=%s", (wid,))
        removed = cur.rowcount
        cur.execute("DELETE FROM user_watchlists WHERE id=%s", (wid,))
        conn.commit()
    return {"status": "ok", "id": wid, "items_removed": removed}


@router.get("/api/mobile/watchlists/{wid}/items")
@_json_safe
def watchlist_items(wid: int, request: Request):
    g = _guard(request)
    if g:
        return g
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        conn.commit()
        own = _own(cur, wid)
        if not own:
            return {"error": "No such watchlist."}
        cur.execute("""SELECT symbol, added_at, added_from, gvm_at_add, price_at_add FROM user_watchlist_items
                       WHERE watchlist_id=%s ORDER BY added_at DESC""", (wid,))
        rows = cur.fetchall()
        syms = [r[0] for r in rows]
        prices, prev, ratings, names = _prices(cur, syms), _prev(cur, syms), _ratings(cur, syms), _names(cur, syms)
    out = shape_items(rows, prices, prev, ratings, names)
    return {"id": own[0], "name": own[1], "rows": out, "count": len(out),
            "priced": sum(1 for r in out if r["day_pct"] is not None),
            "basis": "rating = latest gvm_history row; CMP = cmp_prices (latest close when absent); day % vs the previous session close; since-add % vs the price captured when you added it"}


@router.post("/api/mobile/watchlists/{wid}/items")
async def watchlist_add(wid: int, request: Request):
    g = _guard(request)
    if g:
        return g
    body = await request.json() or {}
    sym, err = validate_symbol(body.get("symbol"))
    if err:
        return _err(err)
    surface = str(body.get("surface") or "")[:40] or None
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        own = _own(cur, wid)
        if not own:
            return _err("No such watchlist.", 404)
        gvm0, px0 = capture_at_add(cur, sym)
        cur.execute("""INSERT INTO user_watchlist_items (watchlist_id, symbol, added_from, gvm_at_add, price_at_add)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (watchlist_id, symbol) DO NOTHING RETURNING added_at""",
                    (wid, sym, surface, gvm0, px0))
        row = cur.fetchone()
        cur.execute("SELECT count(*) FROM user_watchlist_items WHERE watchlist_id=%s", (wid,))
        n = cur.fetchone()[0]
        conn.commit()
    return {"status": "ok", "added": bool(row), "already": not bool(row), "symbol": sym, "list": {"id": own[0], "name": own[1], "count": int(n)},
            "gvm_at_add": _r(gvm0), "price_at_add": _r(px0), "added_at": ist(row[0])[1] if row else None}


@router.delete("/api/mobile/watchlists/{wid}/items/{symbol}")
def watchlist_remove(wid: int, symbol: str, request: Request):
    g = _guard(request)
    if g:
        return g
    sym, err = validate_symbol(symbol)
    if err:
        return _err(err)
    with _conn() as conn, conn.cursor() as cur:
        _ensure(cur)
        if not _own(cur, wid):
            return _err("No such watchlist.", 404)
        cur.execute("DELETE FROM user_watchlist_items WHERE watchlist_id=%s AND symbol=%s", (wid, sym))
        gone = cur.rowcount
        conn.commit()
    return {"status": "ok", "removed": bool(gone), "symbol": sym, "id": wid}


@router.get("/m/mywatchlist", response_class=HTMLResponse)
def m_mywatchlist():
    """cc#2196: My Watchlist — the My Scorr tile's page (cc#1897 route kept)."""
    return _page("mywatchlist")


@router.get("/m/watchlist", response_class=HTMLResponse)
def m_watchlist():
    """cc#2196: the card's own name for the page — the same page."""
    return _page("mywatchlist")

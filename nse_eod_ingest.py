"""
nse_eod_ingest.py -- cc#517 NSE EOD INGEST SUITE (founder-approved 18-Jul-2026).

ONE nightly job fetching four NSE public EOD datasets -- published archive files / a lightweight
JSON API, one GET each, no auth, no scraping of rendered pages:
  A. Delivery (sec_bhavdata_full CSV)          -> delivery_eod
  B. FII/DII cash provisional (fiidiiTradeReact)-> fii_dii_daily
  C. Participant-wise OI (fao_participant_oi)   -> participant_oi_daily
  D. F&O ban list (fo_secban CSV)               -> fo_ban

Shared _nse_session()/_nse_get() do cookie warmup (NSE's archive/API hosts want a prior homepage
hit + a Referer) with standard browser headers. run_nightly() is idempotent (upserts) -- the
scheduler calls it at ~18:30 IST and retries at 19:30/20:30 on trading days; each dataset is
independently try/except'd so one publication delay never blocks the others, and a dataset simply
SKIPS (not fails/crashes) when NSE hasn't published yet. Retention 365d on all four tables
(research assets, purged nightly after a successful run).

NETWORK NOTE (read before debugging a "no data" report): this dev sandbox's outbound proxy policy
blocks nseindia.com / nsearchives.nseindia.com outright (confirmed via curl -- CONNECT 403, policy
denial, not a code/URL problem) -- so none of the four fetch paths below could be live-verified
end-to-end from this environment. They follow NSE's well-documented public archive URL/CSV
conventions and are written defensively (retries via re-invocation, graceful skip, column lookups
by NAME not position so minor header drift doesn't crash the parse) -- but the founder should watch
the first live run's ops_log line + row counts and report back if a URL or column name has moved,
so it can be patched quickly. Production (Railway) has normal outbound network access; this is a
sandbox-only limitation.
"""
import csv
import io
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

import psycopg
import requests
from fastapi import APIRouter, Header, HTTPException

log = logging.getLogger("scorr.nse_eod")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
nse_eod_router = APIRouter(tags=["nse-eod"])

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_API_HOST = "https://www.nseindia.com"
_ARCHIVE_HOST = "https://nsearchives.nseindia.com"
_RETENTION_DAYS = 365


def _conn():
    return psycopg.connect(DATABASE_URL)


def _f(x):
    try:
        if x is None:
            return None
        s = str(x).replace(",", "").strip()
        return None if s in ("", "-") else float(s)
    except (TypeError, ValueError):
        return None


def _nse_session() -> requests.Session:
    """Cookie warmup: NSE's archive/API hosts reject a bare GET from a fresh client with no
    session cookies -- one homepage hit first, like a real browser landing before downloading."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        s.get(_API_HOST, timeout=10)
    except Exception as e:
        log.warning(f"nse_session warmup failed (continuing anyway): {e}")
    return s


def _nse_get(session: requests.Session, url: str, referer: str = None, timeout: int = 20) -> requests.Response:
    r = session.get(url, headers={"Referer": referer or _API_HOST}, timeout=timeout)
    r.raise_for_status()
    return r


def _last_trading_day(d: date = None) -> date:
    """Most recent weekday on/before d (skip Sat/Sun; NSE holiday calendar not consulted here --
    a genuine holiday just yields an expected skip/no-publish for that date, handled gracefully)."""
    d = d or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ── A. Delivery (sec_bhavdata_full) ───────────────────────────────────────────────
def _ensure_delivery_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS delivery_eod (
        symbol TEXT NOT NULL, d DATE NOT NULL,
        traded_qty BIGINT, deliv_qty BIGINT, deliv_pct NUMERIC,
        PRIMARY KEY (symbol, d))""")


def _delivery_url(d: date) -> str:
    return f"{_ARCHIVE_HOST}/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"


def fetch_delivery(session, d: date):
    r = _nse_get(session, _delivery_url(d), referer=f"{_API_HOST}/all-reports")
    text = r.content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        row = {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if row.get("SERIES", "").strip().upper() != "EQ":
            continue
        sym = (row.get("SYMBOL") or "").strip()
        if not sym:
            continue
        tq = _f(row.get("TTL_TRD_QNTY"))
        dq = _f(row.get("DELIV_QTY"))
        dp = _f(row.get("DELIV_PER"))
        rows.append((sym, d, int(tq) if tq is not None else None,
                     int(dq) if dq is not None else None, dp))
    return rows


def upsert_delivery(cur, rows):
    for sym, d, tq, dq, dp in rows:
        cur.execute("""INSERT INTO delivery_eod (symbol, d, traded_qty, deliv_qty, deliv_pct)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (symbol, d) DO UPDATE SET
                         traded_qty=EXCLUDED.traded_qty, deliv_qty=EXCLUDED.deliv_qty,
                         deliv_pct=EXCLUDED.deliv_pct""", (sym, d, tq, dq, dp))


def delivery_21d_avg(cur, sym: str, before: date):
    """Own-symbol 21-session average deliv_pct EXCLUDING `before` -- the cockpit compares latest
    vs its OWN history, never an absolute band (founder-locked convention)."""
    cur.execute("""SELECT deliv_pct FROM delivery_eod WHERE symbol=%s AND d < %s
                   AND deliv_pct IS NOT NULL ORDER BY d DESC LIMIT 21""", (sym, before))
    vals = [float(r[0]) for r in cur.fetchall()]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 1), len(vals)


# ── B. FII/DII cash provisional ───────────────────────────────────────────────────
def _ensure_fii_dii_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS fii_dii_daily (
        d DATE NOT NULL, category TEXT NOT NULL,
        buy_cr NUMERIC, sell_cr NUMERIC, net_cr NUMERIC,
        PRIMARY KEY (d, category))""")


_FII_DII_URL = f"{_API_HOST}/api/fiidiiTradeReact"


def fetch_fii_dii(session):
    """Source = NSE's fiidiiTradeReact (primary, same figure NSE's own FII/DII report page shows).
    NSDL/BSE provisional considered as a fallback per spec but not implemented this pass -- NSE's
    own API is the most directly attributable source and a same-format JSON reduces parse risk;
    if it proves unstable in production, add an NSDL fallback behind the same fetch_fii_dii() call
    site so nothing downstream needs to change."""
    r = _nse_get(session, _FII_DII_URL, referer=f"{_API_HOST}/reports-fii-dii-trading-activity")
    data = r.json()
    rows = []
    for row in (data or []):
        cat_raw = (row.get("category") or "").upper()
        category = "FII" if ("FII" in cat_raw or "FPI" in cat_raw) else ("DII" if "DII" in cat_raw else None)
        if not category:
            continue
        d_raw = row.get("date")
        try:
            d = datetime.strptime(d_raw, "%d-%b-%Y").date()
        except (TypeError, ValueError):
            continue
        buy, sell, net = _f(row.get("buyValue")), _f(row.get("sellValue")), _f(row.get("netValue"))
        rows.append((d, category, buy, sell, net))
    return rows


def upsert_fii_dii(cur, rows):
    for d, cat, buy, sell, net in rows:
        cur.execute("""INSERT INTO fii_dii_daily (d, category, buy_cr, sell_cr, net_cr)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (d, category) DO UPDATE SET
                         buy_cr=EXCLUDED.buy_cr, sell_cr=EXCLUDED.sell_cr, net_cr=EXCLUDED.net_cr""",
                    (d, cat, buy, sell, net))


def fii_dii_streak_and_5d(cur, category: str):
    """cc#517 Part B surfacing: consecutive same-sign net days (streak) + 5d cumulative net, for
    the Daily Digest line ("FII net -1,240 Cr (3rd straight sell day, 5d net -4,820 Cr)")."""
    cur.execute("""SELECT d, net_cr FROM fii_dii_daily WHERE category=%s AND net_cr IS NOT NULL
                   ORDER BY d DESC LIMIT 10""", (category,))
    rows = [(r[0], float(r[1])) for r in cur.fetchall()]
    if not rows:
        return None
    latest_net = rows[0][1]
    streak = 0
    sign = 1 if latest_net >= 0 else -1
    for _, net in rows:
        if (net >= 0) == (sign >= 0):
            streak += 1
        else:
            break
    five_d = round(sum(n for _, n in rows[:5]), 0)
    return {"latest_net": round(latest_net, 0), "streak": streak,
            "sign": "buy" if sign > 0 else "sell", "net_5d": five_d, "latest_date": str(rows[0][0])}


# ── C. Participant-wise OI ────────────────────────────────────────────────────────
def _ensure_participant_oi_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS participant_oi_daily (
        d DATE NOT NULL, participant TEXT NOT NULL, instrument TEXT NOT NULL,
        long_contracts BIGINT, short_contracts BIGINT,
        PRIMARY KEY (d, participant, instrument))""")


def _participant_oi_url(d: date) -> str:
    return f"{_ARCHIVE_HOST}/content/nsccl/fao_participant_oi_{d.strftime('%d%m%Y')}.csv"


# NSE's published column name -> (our instrument key, side). Instruments as published: index
# futures, index call/put long/short, stock futures, stock options.
_PARTICIPANT_COLS = [
    ("Future Index Long", "index_futures", "long"), ("Future Index Short", "index_futures", "short"),
    ("Option Index Call Long", "index_call", "long"), ("Option Index Put Long", "index_put", "long"),
    ("Option Index Call Short", "index_call", "short"), ("Option Index Put Short", "index_put", "short"),
    ("Future Stock Long", "stock_futures", "long"), ("Future Stock Short", "stock_futures", "short"),
    ("Option Stock Call Long", "stock_call", "long"), ("Option Stock Put Long", "stock_put", "long"),
    ("Option Stock Call Short", "stock_call", "short"), ("Option Stock Put Short", "stock_put", "short"),
]


def fetch_participant_oi(session, d: date):
    r = _nse_get(session, _participant_oi_url(d), referer=f"{_API_HOST}/all-reports-derivatives")
    text = r.content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    # NSE's fao_participant_oi.csv carries a title line before the real header -- find it by name.
    header_idx = next((i for i, ln in enumerate(lines) if ln.strip().startswith("Client Type")), None)
    if header_idx is None:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    acc = {}   # (participant, instrument) -> [long, short]
    for row in reader:
        row = {(k or "").strip(): v for k, v in row.items()}
        participant = (row.get("Client Type") or "").strip()
        if not participant:
            continue
        for col, instrument, side in _PARTICIPANT_COLS:
            val = _f(row.get(col))
            if val is None:
                continue
            key = (participant, instrument)
            longc, shortc = acc.get(key, (None, None))
            if side == "long":
                longc = int(val)
            else:
                shortc = int(val)
            acc[key] = (longc, shortc)
    return [(d, p, i, l, s) for (p, i), (l, s) in acc.items()]


def upsert_participant_oi(cur, rows):
    for d, p, i, l, s in rows:
        cur.execute("""INSERT INTO participant_oi_daily (d, participant, instrument, long_contracts, short_contracts)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (d, participant, instrument) DO UPDATE SET
                         long_contracts=EXCLUDED.long_contracts, short_contracts=EXCLUDED.short_contracts""",
                    (d, p, i, l, s))


def fii_index_futures_positioning(cur):
    """cc#517 Part C surfacing: the classic "FII long-short ratio" read + d/d delta, plus the
    Client (retail) mirror for the divergence line."""
    cur.execute("""SELECT DISTINCT d FROM participant_oi_daily ORDER BY d DESC LIMIT 2""")
    days = [r[0] for r in cur.fetchall()]
    if not days:
        return None

    def _row(d, participant):
        cur.execute("""SELECT long_contracts, short_contracts FROM participant_oi_daily
                       WHERE d=%s AND participant=%s AND instrument='index_futures'""", (d, participant))
        r = cur.fetchone()
        return (int(r[0] or 0), int(r[1] or 0)) if r else (None, None)

    fii_today = _row(days[0], "FII")
    fii_prev = _row(days[1], "FII") if len(days) > 1 else (None, None)
    client_today = _row(days[0], "Client")
    out = {"d": str(days[0])}
    if fii_today[0] is not None:
        l, s = fii_today
        total = l + s
        out["fii"] = {"long": l, "short": s, "net": l - s,
                       "long_pct": round(l / total * 100.0, 0) if total else None}
        if fii_prev[0] is not None:
            out["fii"]["short_delta"] = fii_today[1] - fii_prev[1]
    if client_today[0] is not None:
        l, s = client_today
        out["client"] = {"long": l, "short": s, "net": l - s}
    return out if "fii" in out else None


# ── D. F&O ban list ────────────────────────────────────────────────────────────────
def _ensure_fo_ban_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS fo_ban (d DATE NOT NULL, symbol TEXT NOT NULL,
        PRIMARY KEY (d, symbol))""")


_FO_BAN_URL = f"{_ARCHIVE_HOST}/content/fo/fo_secban.csv"


def fetch_fo_ban(session, d: date):
    r = _nse_get(session, _FO_BAN_URL, referer=f"{_API_HOST}/all-reports-derivatives")
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if not row or len(row) < 2:
            continue
        sym = row[1].strip().upper()
        if sym and sym not in ("SYMBOL",):
            rows.append((d, sym))
    return rows


def upsert_fo_ban(cur, d: date, rows):
    """Ban list is a full daily snapshot (today's complete list, not incremental) -- replace
    today's rows rather than accumulate duplicates across retries."""
    cur.execute("DELETE FROM fo_ban WHERE d=%s", (d,))
    for _, sym in rows:
        cur.execute("INSERT INTO fo_ban (d, symbol) VALUES (%s,%s) ON CONFLICT DO NOTHING", (d, sym))


def is_banned_today(cur, symbol: str) -> bool:
    cur.execute("SELECT 1 FROM fo_ban WHERE d=(SELECT MAX(d) FROM fo_ban) AND symbol=%s", (symbol.upper(),))
    return cur.fetchone() is not None


# ── retention + orchestrator ───────────────────────────────────────────────────────
def _purge_old(cur):
    cutoff = date.today() - timedelta(days=_RETENTION_DAYS)
    for tbl in ("delivery_eod", "fii_dii_daily", "participant_oi_daily", "fo_ban"):
        cur.execute(f"DELETE FROM {tbl} WHERE d < %s", (cutoff,))


def run_nightly() -> dict:
    """cc#517: idempotent (upserts) -- safe for the scheduler to call at 18:30/19:30/20:30 IST on
    trading days. Each dataset is independently try/except'd: a publication delay or format change
    in one never blocks the other three. Returns a per-dataset result dict for the ops_log line."""
    today = _last_trading_day()
    session = _nse_session()
    out = {"date": str(today)}
    with _conn() as conn:
        with conn.cursor() as cur:
            _ensure_delivery_table(cur)
            _ensure_fii_dii_table(cur)
            _ensure_participant_oi_table(cur)
            _ensure_fo_ban_table(cur)
        conn.commit()

        try:
            with conn.cursor() as cur:
                rows = fetch_delivery(session, today)
                if rows:
                    upsert_delivery(cur, rows)
                    conn.commit()
            out["delivery"] = len(rows)
        except Exception as e:
            log.warning(f"nse_eod delivery skipped/failed for {today}: {e}")
            out["delivery"] = f"skip: {e}"

        try:
            with conn.cursor() as cur:
                rows = fetch_fii_dii(session)
                if rows:
                    upsert_fii_dii(cur, rows)
                    conn.commit()
            out["fii_dii"] = len(rows)
        except Exception as e:
            log.warning(f"nse_eod fii_dii skipped/failed: {e}")
            out["fii_dii"] = f"skip: {e}"

        try:
            with conn.cursor() as cur:
                rows = fetch_participant_oi(session, today)
                if rows:
                    upsert_participant_oi(cur, rows)
                    conn.commit()
            out["participant_oi"] = len(rows)
        except Exception as e:
            log.warning(f"nse_eod participant_oi skipped/failed for {today}: {e}")
            out["participant_oi"] = f"skip: {e}"

        try:
            with conn.cursor() as cur:
                rows = fetch_fo_ban(session, today)
                upsert_fo_ban(cur, today, rows)   # a genuinely-empty ban list is a valid daily snapshot
                conn.commit()
            out["fo_ban"] = len(rows)
        except Exception as e:
            log.warning(f"nse_eod fo_ban skipped/failed for {today}: {e}")
            out["fo_ban"] = f"skip: {e}"

        try:
            with conn.cursor() as cur:
                _purge_old(cur)
            conn.commit()
        except Exception as e:
            log.warning(f"nse_eod purge failed: {e}")

    log.info(f"nse_eod_ingest: {out}")
    return out


def _check_admin(token):
    if not ADMIN_TOKEN:
        return True
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")
    return True


@nse_eod_router.post("/api/nse-eod/run")
def nse_eod_run(x_admin_token: Optional[str] = Header(None)):
    """Manual trigger (admin-token gated) -- runs the full ingest synchronously and returns the
    per-dataset row-count/skip result. Lets the founder force a run to verify a fetch path without
    waiting for the scheduled 18:30/19:30/20:30 IST slots."""
    _check_admin(x_admin_token)
    try:
        return run_nightly()
    except Exception as e:
        raise HTTPException(500, f"nse_eod_run failed: {e}")


@nse_eod_router.get("/api/nse-eod/status")
def nse_eod_status():
    """Row counts + latest date per table, for a quick health check without touching the network."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            out = {}
            for tbl in ("delivery_eod", "fii_dii_daily", "participant_oi_daily", "fo_ban"):
                cur.execute(f"SELECT COUNT(*), MAX(d) FROM {tbl}")
                r = cur.fetchone()
                out[tbl] = {"rows": int(r[0] or 0), "latest_date": str(r[1]) if r[1] else None}
            return out
    except Exception as e:
        raise HTTPException(500, f"nse_eod_status failed: {e}")

"""
position_news.py — cc#207 (Position News trial)
================================================
Google News for the symbols the book actually holds — the UNION of currently open
V8 paper positions and current SmartGain (MHK40) net holdings — kept in a QUARANTINE
table (position_news), never mixed into the main Domestic/Global/IPO/AI feed.

Design decisions (per the cc#207 active spec — supersedes cc#186/cc#206 500-co waves):
  • Separate table, not raw_news: raw_news.url_hash is GLOBALLY unique, so a company
    URL already present as a domestic row would be silently dropped (the original
    0-insert killer). A dedicated table dedups ONLY within itself (UNIQUE(symbol,
    url_hash)) so cross-type collisions can never quarantine a position headline.
  • NO quality gate, NO polish — raw material; the founder is the quality judge.
  • Full funnel telemetry every fetch (symbols_count/parsed/dup_skipped/inserted/
    http_429/http_other) into ops_log — no silent stage, ever.
  • 7-day retention — trial data, not archive.

Politeness (sequential, browser UA, 2-4s jitter, Retry-After, 429-abort) is reused
verbatim from news_fetcher so we stay under Google News' datacenter-IP limit.
"""

import os
import time
import random
import logging
from datetime import datetime, timedelta, date

import psycopg
import news_fetcher as nf

log = logging.getLogger("position_news")

DATABASE_URL = os.getenv("DATABASE_URL", "")
RETENTION_DAYS = 7                    # trial data self-deletes at 7 days
PER_SYMBOL_MAX = 6                    # cap entries stored per symbol per fetch
SG_ACCOUNT = "MHK40"


def _conn():
    return psycopg.connect(DATABASE_URL)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS position_news (
                id BIGSERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                origin VARCHAR(6),                 -- V8 | SG | V8+SG (optional badge)
                headline TEXT NOT NULL,
                source_name VARCHAR(80),
                url TEXT,
                url_hash VARCHAR(64),
                published_at TIMESTAMPTZ,
                fetched_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (symbol, url_hash)          -- dedup WITHIN company_pos only
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posnews_symbol_pub ON position_news(symbol, published_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_posnews_fetched    ON position_news(fetched_at DESC)")
        # cc#611 PART C: polished 2-3 sentence data-rich summary (CC-batch on Max; NO Anthropic API).
        # headline (above) is already the clean headline (publisher split off in _split_title). When
        # `summary` is NULL the card renders the raw headline with an "unpolished" marker — never blank.
        cur.execute("ALTER TABLE position_news ADD COLUMN IF NOT EXISTS summary TEXT")
        cur.execute("ALTER TABLE position_news ADD COLUMN IF NOT EXISTS polished_at TIMESTAMPTZ")
    conn.commit()


# ── symbol set: open V8 positions ∪ SmartGain net holdings ──────────────────────
def _v8_open_symbols(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL")
        return [r[0] for r in cur.fetchall()]


def _smartgain_open_symbols(conn):
    """Net FIFO positions with qty != 0 for the MHK40 book. Reuses the authoritative
    replay from smartgain_daily_m2m (cc#185) so the net matches the M2M card exactly."""
    try:
        import smartgain_daily_m2m as sg
        with conn.cursor() as cur:
            res = sg._replay_full(cur, SG_ACCOUNT, sg._ist_today())
        books = res[3]  # (per_day, tdays, closed, BOOKS, opening, inception, baseline, notes)
        return [s for s, lots in books.items() if lots]
    except Exception as e:
        log.warning(f"_smartgain_open_symbols: {e}")
        return []


def _name_map(conn, symbols):
    """symbol -> company_name (for a richer Google query); falls back to the symbol."""
    names = {}
    if not symbols:
        return names
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT symbol, company_name FROM gvm_scores
                           WHERE score_date=(SELECT MAX(score_date) FROM gvm_scores)
                             AND symbol = ANY(%s) AND company_name IS NOT NULL""", (list(symbols),))
            for sym, nm in cur.fetchall():
                names[sym] = nm
    except Exception as e:
        log.warning(f"_name_map: {e}")
    return names


def position_symbols(conn):
    """UNION of open V8 + SmartGain symbols → {symbol: origin}. origin is V8 / SG / V8+SG."""
    v8 = set(_v8_open_symbols(conn))
    sg = set(_smartgain_open_symbols(conn))
    out = {}
    for s in sorted(v8 | sg):
        if s in v8 and s in sg:
            out[s] = "V8+SG"
        elif s in v8:
            out[s] = "V8"
        else:
            out[s] = "SG"
    return out


def _split_title(title):
    """Google News titles are 'Headline - Publisher'. Split off the publisher for the
    source badge; leave the headline clean. Returns (headline, source_name)."""
    t = nf._clean(title or "")
    if " - " in t:
        head, _, src = t.rpartition(" - ")
        if head and len(src) <= 60:
            return head.strip(), src.strip()
    return t, "Google News"


# ── fetch ───────────────────────────────────────────────────────────────────────
def fetch_position_news(conn=None):
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        symbols = position_symbols(conn)
        if not symbols:
            stats = {"symbols_count": 0, "parsed": 0, "dup_skipped": 0, "inserted": 0,
                     "http_429": 0, "http_other": 0, "empty": 0, "aborted": False, "note": "no open positions"}
            nf._write_ops_log(conn, "news_fetch", "fetch_position_news", stats)
            return {"ok": True, **stats}

        names = _name_map(conn, list(symbols.keys()))
        parsed = dup_skipped = inserted = http_429 = http_other = empty = 0
        consec_429 = 0
        aborted = False

        for sym, origin in symbols.items():
            query = names.get(sym) or sym
            entries, status, bozo, retry_after = nf._fetch_feed(nf._google_news_url(query), agent=nf.BROWSER_UA)
            if status == 429:
                http_429 += 1
                consec_429 += 1
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), nf.RETRY_AFTER_CAP))
                    except (TypeError, ValueError):
                        pass
                if consec_429 >= nf.MAX_CONSECUTIVE_429:
                    aborted = True
                    log.error(f"fetch_position_news: IP flagged — aborting after {consec_429} consecutive 429s")
                    break
            else:
                consec_429 = 0
                if status is not None and status >= 400:
                    http_other += 1
                elif not entries:
                    empty += 1
                else:
                    p, d, i = _store(conn, sym, origin, entries)
                    parsed += p; dup_skipped += d; inserted += i
            time.sleep(random.uniform(nf.COMPANY_SLEEP_MIN, nf.COMPANY_SLEEP_MAX))

        stats = {"symbols_count": len(symbols), "parsed": parsed, "dup_skipped": dup_skipped,
                 "inserted": inserted, "http_429": http_429, "http_other": http_other,
                 "empty": empty, "aborted": aborted}
        nf._write_ops_log(conn, "news_fetch", "fetch_position_news", stats)
        log.info(f"fetch_position_news: {stats}")
        return {"ok": True, **stats}
    except Exception as e:
        log.error(f"fetch_position_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()


def _store(conn, symbol, origin, entries):
    """Insert up to PER_SYMBOL_MAX entries for one symbol. Dedup within (symbol,url_hash)
    only. NO quality gate. Returns (parsed, dup_skipped, inserted)."""
    parsed = dup_skipped = inserted = 0
    seen = set()
    with conn.cursor() as cur:
        for e in entries[:PER_SYMBOL_MAX]:
            link = (e.get("link") or "").strip()
            headline, source = _split_title(e.get("title") or "")
            if not link or not headline:
                continue
            h = nf._md5(link)
            if h in seen:
                continue
            seen.add(h)
            parsed += 1
            cur.execute("""
                INSERT INTO position_news (symbol, origin, headline, source_name, url, url_hash, published_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (symbol, url_hash) DO NOTHING
            """, (symbol, origin, headline[:2000], source[:80], link, h, nf._published_at(e)))
            if cur.rowcount:
                inserted += 1
            else:
                dup_skipped += 1
    conn.commit()
    return parsed, dup_skipped, inserted


def fetch_and_alert(conn=None):
    """cc#611 PART A: run the fetch and raise an ops_log alert when the pipeline is effectively dead
    — open positions exist but the fetch produced nothing AND no position_news row landed in >24h
    (the silent 06-Jul stall was invisible before). This is what the scheduler slots call."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        res = fetch_position_news(conn)
        try:
            from datetime import datetime, timezone
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(fetched_at) FROM position_news")
                last = cur.fetchone()[0]
                cur.execute("""SELECT COUNT(*) FROM (
                    SELECT symbol FROM v8_paper_positions WHERE status='OPEN' AND symbol IS NOT NULL
                    UNION SELECT symbol FROM smartgain_holdings WHERE symbol IS NOT NULL) p""")
                open_n = cur.fetchone()[0]
            stale = last is None or (datetime.now(timezone.utc) - last).total_seconds() > 24 * 3600
            if open_n > 0 and ((not res.get("ok")) or (res.get("inserted", 0) == 0 and stale)):
                nf._write_ops_log(conn, "alert", "position_news_stale",
                                  {"open_positions": open_n, "last_fetched": str(last),
                                   "fetch_result": res,
                                   "note": "position_news fetch produced nothing while positions are open"})
                log.error(f"cc#611 ALERT position_news_stale: open={open_n} last={last} res={res}")
        except Exception as e:
            log.warning(f"position_news alert check: {e}")
        return res
    finally:
        if own:
            conn.close()


def backfill_if_stale(hours=18):
    """cc#611 startup one-shot: run an immediate fetch when the newest position_news row is older
    than `hours`, so a deploy landing after a stall self-heals the open-position feed. Server-side
    (Railway has egress); best-effort."""
    conn = _conn()
    try:
        ensure_schema(conn)
        from datetime import datetime, timezone
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(fetched_at) FROM position_news")
            last = cur.fetchone()[0]
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > hours * 3600:
            log.info("cc#611: position_news stale -> startup backfill fetch")
            return fetch_and_alert(conn)
        return {"ok": True, "skipped": "fresh", "last_fetched": str(last)}
    finally:
        conn.close()


def purge_position_news(conn=None, days=RETENTION_DAYS):
    """7-day retention (called from the 01:50 retention scheduler)."""
    own = conn is None
    if own:
        conn = _conn()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM position_news WHERE fetched_at < NOW() - INTERVAL '%s days'" % int(days))
            deleted = cur.rowcount
        conn.commit()
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        log.error(f"purge_position_news: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        if own:
            conn.close()

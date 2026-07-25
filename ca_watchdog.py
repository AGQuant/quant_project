"""
ca_watchdog.py — cc#658 CA_WATCHDOG_V1 + daily ops notes.

Builds on cc#657 (yahoo_daily_update.detect_cliff_symbols / restate_symbol_history). Responsibilities:
  - weekly_distortion_scan(): full 5y cliff sweep cross-checked vs corporate_actions. A cliff WITH a
    matching CA (ex_date +/-3d) is an unadjusted-feed artefact -> auto-restate (cc#657). A cliff WITHOUT
    a CA is a genuine-crash flag (review-only, NEVER auto-touched).
  - forward_heal(): symbols with a CA ex_date == today get a same-evening 5y adjusted restate.
  - ca_daily_note(): every-morning data-integrity note (ops_log category=data_integrity).
  - master_watchdog_note(): every-morning consolidated ops note across all watchdogs (category=watchdog_daily).

Notes are rule-generated, terse, red-flags-first, and written EVERY day (one-line ALL CLEAR when clean).
Raw-price integrity itself is held by cc#657 even before corporate_actions is populated; this layer adds
classification (artefact vs genuine), forward healing, and the daily ops surface.
"""
import os
import json
import logging

import psycopg

log = logging.getLogger("ca_watchdog")
DB_URL = os.getenv("DATABASE_URL", "")

_INDEX_EXCLUDE = ("NIFTY50", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCAPNIFTY")
_CLIFF_THRESH = -0.33
_CA_MATCH_DAYS = 3


def _oplog(category, title, details):
    try:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                        "VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)",
                        (category, title, json.dumps(details, default=str)))
            conn.commit()
    except Exception as e:
        log.warning(f"ca_watchdog _oplog {title}: {e}")


def _cliff_events(cur, months, thresh=_CLIFF_THRESH):
    """(symbol, cliff_date, drop_pct) for every unadjusted single-day cliff in the window."""
    cur.execute("""
        WITH d AS (SELECT symbol, price_date, close,
                     LAG(close) OVER (PARTITION BY symbol ORDER BY price_date) prev
                   FROM raw_prices WHERE price_date >= CURRENT_DATE - make_interval(months => %s))
        SELECT symbol, price_date, round(((close/prev - 1) * 100)::numeric, 1)
        FROM d
        WHERE prev IS NOT NULL AND prev > 0 AND (close/prev - 1) < %s
          AND symbol <> ALL(%s)
        ORDER BY price_date DESC""", (months, thresh, list(_INDEX_EXCLUDE)))
    return cur.fetchall()


def _matching_ca(cur, symbol, cliff_date, window_days=_CA_MATCH_DAYS):
    """The corporate_actions row whose ex_date is within +/-window_days of the cliff, else None."""
    cur.execute("""SELECT action_type, ex_date, ratio_text FROM corporate_actions
                   WHERE symbol=%s AND ex_date BETWEEN (%s::date - %s) AND (%s::date + %s)
                   ORDER BY ABS(ex_date - %s::date) LIMIT 1""",
                (symbol, cliff_date, window_days, cliff_date, window_days, cliff_date))
    return cur.fetchone()


def weekly_distortion_scan(auto_restate=True):
    """cc#658 part_2: 5y cliff sweep x corporate_actions. Artefact (cliff+CA) -> auto-restate (cc#657);
    genuine crash (cliff, no CA) -> flag only. ops_log(data_integrity, WEEKLY_DISTORTION_SCAN)."""
    from yahoo_daily_update import restate_symbol_history
    artefacts = {}   # symbol -> matched CA detail
    genuine = []
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        events = _cliff_events(cur, months=60)   # 5y
        n_events = len(events)
        for sym, cdate, drop in events:
            ca = _matching_ca(cur, sym, cdate)
            if ca and sym not in artefacts:
                artefacts[sym] = {"action": ca[0], "ex_date": str(ca[1]), "ratio": ca[2],
                                  "cliff_date": str(cdate), "drop_pct": float(drop)}
            elif not ca:
                genuine.append({"symbol": sym, "cliff_date": str(cdate), "drop_pct": float(drop)})
    restated = []
    if auto_restate and artefacts:
        restated = restate_symbol_history(sorted(artefacts.keys()), lookback="5y")
    summary = {"cliff_events": n_events, "artefact_symbols": sorted(artefacts.keys()),
               "artefacts": artefacts,
               "genuine_crash_flags": genuine,
               "restated": [{"symbol": r.get("symbol"), "status": r.get("status"),
                             "residual_cliffs": r.get("residual_cliffs")} for r in restated]}
    _oplog("data_integrity", "WEEKLY_DISTORTION_SCAN", summary)
    log.info(f"weekly_distortion_scan: {n_events} cliffs, {len(artefacts)} artefacts restated, "
             f"{len(genuine)} genuine-crash flags")
    return summary


def forward_heal():
    """cc#658 part_3: any CA ex_date == today -> same-evening 5y adjusted restate (runs after the Yahoo
    nightly run, before GVM). ops_log(data_integrity, CA_FORWARD_HEAL)."""
    from yahoo_daily_update import restate_symbol_history
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM corporate_actions WHERE ex_date = CURRENT_DATE ORDER BY symbol")
        syms = [r[0] for r in cur.fetchall()]
    if not syms:
        return {"symbols": [], "note": "no CA ex-dates today"}
    rep = restate_symbol_history(syms, lookback="5y")
    _oplog("data_integrity", "CA_FORWARD_HEAL", {"symbols": syms, "restated": rep})
    log.info(f"forward_heal: restated {len(syms)} ex-date symbols {syms}")
    return {"symbols": syms, "restated": rep}


def ca_daily_note():
    """cc#658 part_4: every-morning CA integrity note -> ops_log(data_integrity, CA_DAILY_NOTE).
    Upcoming ex-dates (3d), overnight restates (today), genuine-crash flags (recent cliffs w/o a CA),
    coverage. Written even when all-clear."""
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("""SELECT symbol, action_type, ex_date, ratio_text FROM corporate_actions
                       WHERE ex_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + 3) ORDER BY ex_date, symbol""")
        upcoming = [{"symbol": r[0], "action": r[1], "ex_date": str(r[2]), "ratio": r[3]} for r in cur.fetchall()]
        cur.execute("""SELECT details FROM ops_log WHERE category='yahoo_ca_restate'
                       AND session_date=CURRENT_DATE ORDER BY session_ts DESC LIMIT 1""")
        row = cur.fetchone()
        restated = row[0] if (row and row[0]) else []
        events = _cliff_events(cur, months=1)   # last ~month
        genuine = []
        for sym, cdate, drop in events:
            if not _matching_ca(cur, sym, cdate):
                genuine.append({"symbol": sym, "cliff_date": str(cdate), "drop_pct": float(drop)})
        cur.execute("SELECT COUNT(*) FROM corporate_actions")
        ca_rows = cur.fetchone()[0]
    n_restated = len([r for r in (restated or []) if isinstance(r, dict) and r.get("status") == "restated"])
    all_clear = not upcoming and not genuine and n_restated == 0
    note = {"all_clear": all_clear,
            "headline": ("ALL CLEAR" if all_clear else
                         f"{len(upcoming)} upcoming ex-date(s) · {n_restated} restated overnight · "
                         f"{len(genuine)} genuine-crash flag(s)"),
            "upcoming_ex_dates": upcoming,
            "restated_overnight": restated,
            "genuine_crash_flags": genuine,
            "corporate_actions_rows": ca_rows}
    _oplog("data_integrity", "CA_DAILY_NOTE", note)
    return note


def master_watchdog_note():
    """cc#658 part_5: consolidated morning ops note -> ops_log(watchdog_daily, MASTER_WATCHDOG_NOTE).
    One terse, red-flags-first summary across every watchdog. Each source is guarded so the note always
    writes, even if one probe fails."""
    red = []
    lines = {}

    # (a) signal-writer / scheduler engine health
    try:
        import scheduler
        hs = scheduler.health_state() if hasattr(scheduler, "health_state") else {}
        lines["engine"] = {"fail_streak": hs.get("fail_streak"),
                           "watchdog_restarts": hs.get("watchdog_restarts"),
                           "last_writer_ok": str(hs.get("last_signal_writer_ok"))}
        if (hs.get("fail_streak") or 0) > 0 or (hs.get("watchdog_restarts") or 0) > 0:
            red.append(f"engine {hs.get('fail_streak')} fails / {hs.get('watchdog_restarts')} restarts")
    except Exception as e:
        lines["engine"] = f"err:{str(e)[:60]}"

    # (b) scheduler diagnosis traffic light
    try:
        import diagnosis
        diag = diagnosis.run_diagnosis()
        lines["diagnosis"] = {"status": diag.get("status"), "issues": diag.get("issues_count"),
                              "warnings": diag.get("warnings_count")}
        if diag.get("status") == "red":
            red.append(f"diagnosis RED ({diag.get('issues_count')} issues)")
    except Exception as e:
        lines["diagnosis"] = f"err:{str(e)[:60]}"

    # (c) yahoo drops, (e) DB size, feed freshness, CA headline — one DB pass
    try:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("SELECT title FROM ops_log WHERE category='yahoo_eod_drops' ORDER BY session_ts DESC LIMIT 1")
            r = cur.fetchone(); lines["yahoo_eod"] = r[0] if r else "no run recorded"
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            lines["db_size"] = cur.fetchone()[0]
            cur.execute("SELECT MAX(price_date)::text FROM raw_prices")
            lines["raw_prices_latest"] = cur.fetchone()[0]
            cur.execute("""SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts)))/60.0 FROM intraday_prices""")
            _im = cur.fetchone()[0]
            lines["intraday_age_min"] = round(float(_im), 1) if _im is not None else None
            cur.execute("""SELECT details->>'headline' FROM ops_log
                           WHERE category='data_integrity' AND title='CA_DAILY_NOTE'
                           AND session_date=CURRENT_DATE ORDER BY session_ts DESC LIMIT 1""")
            r = cur.fetchone(); lines["ca"] = r[0] if (r and r[0]) else "no CA note yet"
            if lines["ca"] not in ("ALL CLEAR", "no CA note yet"):
                red.append(f"CA: {lines['ca']}")
    except Exception as e:
        lines["db_probe"] = f"err:{str(e)[:80]}"

    note = {"status": "RED" if red else "OK",
            "headline": ("ALL CLEAR" if not red else " · ".join(red[:6])),
            "red_flags": red, "detail": lines}
    _oplog("watchdog_daily", "MASTER_WATCHDOG_NOTE", note)
    return note


def data_integrity_status():
    """cc#658 part_6: compact status for the /health card + V8 dashboard strip. Reads today's CA note +
    master note headlines; red when any unresolved anomaly (genuine-crash flag or master red flag)."""
    out = {"ca_headline": None, "master_headline": None, "genuine_flags": 0, "red": False}
    try:
        with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
            cur.execute("""SELECT details FROM ops_log WHERE category='data_integrity' AND title='CA_DAILY_NOTE'
                           ORDER BY session_ts DESC LIMIT 1""")
            r = cur.fetchone()
            if r and r[0]:
                d = r[0] if isinstance(r[0], dict) else json.loads(r[0])
                out["ca_headline"] = d.get("headline")
                out["genuine_flags"] = len(d.get("genuine_crash_flags") or [])
            cur.execute("""SELECT details FROM ops_log WHERE category='watchdog_daily' AND title='MASTER_WATCHDOG_NOTE'
                           ORDER BY session_ts DESC LIMIT 1""")
            r = cur.fetchone()
            if r and r[0]:
                d = r[0] if isinstance(r[0], dict) else json.loads(r[0])
                out["master_headline"] = d.get("headline")
                out["red"] = bool(d.get("red_flags"))
    except Exception as e:
        out["error"] = str(e)[:120]
    out["red"] = out["red"] or out["genuine_flags"] > 0
    return out

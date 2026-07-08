"""
bt7_harness.py — cc#218 (BT7 = Backtest-7Day parity harness)
============================================================
A PERMANENT bar-by-bar 5-min live simulator. It walks 09:15->15:30 for a target day
and drives the REAL v8_signal_writer + v8_paper functions against a frozen clock
(sim_ts, cc#218 S1), writing ONLY to the `harness` shadow schema. Zero-diff parity
certification for any live-path refactor (cc#217 Phase 2/3).

Sandbox (RULING_A, DB-enforced — not a code assert):
  • runs under SET ROLE bt7_sim (SELECT-only on public, ALL on harness) with
    search_path = harness, public. A stray live write => permission error, loudly.
  • unqualified table names resolve to the same-named harness shadow (writes + the
    materialized inputs); reads with no shadow fall through to public.

Materialization (RULING_A_ADDENDUM_D8) — each run pre-loads its inputs into harness so the
run is byte-reproducible forever, even after the rolling intraday window churns:
  • target-day 5-min bars           -> harness.intraday_prices
  • prior-day EOD v8_metrics baseline-> harness.v8_metrics  (score_date < target)
  • target-day pivots               -> harness.v8_paper_pivots
  From golden_*_YYYYMMDD when archived (03-Jul), else from public (dates still in-window).

NO reimplementation (RULE 2): every qual/entry/exit comes from the real functions.
RULE 1/6: v8_intra_backtest.py (EOD system) and v8_paper_replay.py are never touched.
"""

import os
import logging
import threading
from datetime import datetime, date, time, timedelta, timezone

import psycopg

log = logging.getLogger("scorr.bt7")
DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

# cc#220 single-run advisory-lock key (session-scoped: auto-released when the walk's
# connection closes/dies, so a zombie can never permanently wedge the harness).
_LOCK_KEY = 7220218

# tables the harness truncates to a clean slate before each run (write shadows)
_SCRATCH = ["v8_qualified", "v8_paper_positions", "v8_paper_trades", "v8_paper_missed",
            "v8_funnel_counts", "adr_intraday", "app_config", "ops_log", "v8_metrics",
            "intraday_prices", "v8_paper_pivots"]
_RESULT = ["bt7_qualified", "bt7_positions", "bt7_trades", "bt7_missed"]
_RESULT_SRC = {"bt7_qualified": "v8_qualified", "bt7_positions": "v8_paper_positions",
               "bt7_trades": "v8_paper_trades", "bt7_missed": "v8_paper_missed"}


def _conn():
    # cc#220 self-cleaning: cap any single statement at 90s and auto-kill a connection left
    # idle-in-transaction for >120s (mirrors scheduler._conn). An abandoned run — e.g. an MCP
    # request that timed out mid-walk in the OLD sync design — self-destructs instead of
    # zombieing idle-in-transaction on locks and deadlocking the next run's TRUNCATE. Healthy
    # runs never trip these: no single statement is that slow, and the walk commits per tick
    # (never idle inside an open txn between ticks).
    opts = "-c statement_timeout=90000 -c idle_in_transaction_session_timeout=120000"
    return psycopg.connect(DATABASE_URL, options=opts)


def _mark_running(conn, label, target_date):
    """Write/reset the run row to status='running' BEFORE the walk starts, so a poller sees
    the run the instant bt7_run returns. Runs as the app superuser (before SET ROLE)."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO harness.bt7_runs
                       (run_label, target_date, ticks, source, status, error_detail, ran_at)
                       VALUES (%s,%s,0,'bt7','running',NULL,NOW())
                       ON CONFLICT (run_label) DO UPDATE SET
                         target_date=EXCLUDED.target_date, ticks=0, source='bt7',
                         status='running', error_detail=NULL, ran_at=NOW()""",
                    (label, target_date))
    conn.commit()


def _progress(conn, label, ticks):
    """Publish walk progress to harness.bt7_runs (best-effort; runs under SET ROLE bt7_sim,
    which owns the harness tables). A tick has fully completed+committed before this is
    called, so a rollback-on-failure here can never lose driven-path work."""
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE harness.bt7_runs SET ticks=%s, ran_at=NOW() WHERE run_label=%s",
                        (ticks, label))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _ensure_bt7_runs_cols(conn):
    """Idempotently guarantee the status/error_detail columns exist (a bt7_runs created
    before the cc#218 hotfix would lack them). Runs as the app superuser/owner."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE harness.bt7_runs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ok'")
        cur.execute("ALTER TABLE harness.bt7_runs ADD COLUMN IF NOT EXISTS error_detail TEXT")
    conn.commit()


def _write_error(conn, label, target_date, ticks, detail):
    """Persist a run's true first exception into harness.bt7_runs. Railway stdout is
    invisible to the ops desk; the DB is not — so the next failure names itself."""
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO harness.bt7_runs
                           (run_label, target_date, ticks, source, status, error_detail, ran_at)
                           VALUES (%s,%s,%s,%s,'error',%s,NOW())
                           ON CONFLICT (run_label) DO UPDATE SET
                             target_date=EXCLUDED.target_date, ticks=EXCLUDED.ticks,
                             source=EXCLUDED.source, status='error',
                             error_detail=EXCLUDED.error_detail, ran_at=NOW()""",
                        (label, target_date, ticks, "bt7", detail))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.error(f"_write_error '{label}': {e}")


def _regclass(cur, qualified_name):
    cur.execute("SELECT to_regclass(%s)", (qualified_name,))
    return cur.fetchone()[0] is not None


def _cols(cur, schema, table):
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""",
                (schema, table))
    return [r[0] for r in cur.fetchall()]


def _copy_intersect(cur, dst_schema, dst_table, src_qualified, where=""):
    """INSERT INTO dst SELECT <shared cols> FROM src — column-intersection safe."""
    if "." in src_qualified:
        src_schema, src_table = src_qualified.split(".", 1)
    else:
        src_schema, src_table = "public", src_qualified
    dcols = _cols(cur, dst_schema, dst_table)
    scols = set(_cols(cur, src_schema, src_table))
    shared = [c for c in dcols if c in scols]
    if not shared:
        return 0
    collist = ", ".join('"%s"' % c for c in shared)
    cur.execute(f'INSERT INTO {dst_schema}."{dst_table}" ({collist}) '
                f'SELECT {collist} FROM {src_schema}."{src_table}" {where}')
    return cur.rowcount


def _materialize(conn, target_date):
    """Clean the shadow schema and pre-load this run's point-in-time inputs. Runs as the
    app superuser (before SET ROLE) — reads public/golden, writes harness."""
    ymd = target_date.strftime("%Y%m%d")
    src = {"bars": None, "metrics": None, "pivots": None}
    with conn.cursor() as cur:
        for t in _SCRATCH:
            cur.execute(f"TRUNCATE harness.{t}")
        # (1) target-day bars
        gb = f"golden_bars_{ymd}"
        if _regclass(cur, "public." + gb):
            n = _copy_intersect(cur, "harness", "intraday_prices", "public." + gb)
            src["bars"] = f"{gb} ({n})"
        else:
            n = _copy_intersect(cur, "harness", "intraday_prices", "public.intraday_prices",
                                where=f"WHERE ts::date <= DATE '{target_date}'")
            src["bars"] = f"public.intraday_prices<= {target_date} ({n})"
        # (2) prior-day EOD v8_metrics baseline (score_date < target)
        gm = f"golden_v8_metrics_{ymd}"
        if _regclass(cur, "public." + gm):
            n = _copy_intersect(cur, "harness", "v8_metrics", "public." + gm)
            src["metrics"] = f"{gm} ({n})"
        else:
            n = _copy_intersect(cur, "harness", "v8_metrics", "public.v8_metrics",
                                where=f"WHERE score_date < DATE '{target_date}'")
            src["metrics"] = f"public.v8_metrics< {target_date} ({n})"
        # (3) target-day pivots
        gp = f"golden_pivots_{ymd}"
        if _regclass(cur, "public." + gp):
            n = _copy_intersect(cur, "harness", "v8_paper_pivots", "public." + gp)
            src["pivots"] = f"{gp} ({n})"
        else:
            n = _copy_intersect(cur, "harness", "v8_paper_pivots", "public.v8_paper_pivots",
                                where=f"WHERE pivot_date = DATE '{target_date}'")
            src["pivots"] = f"public.v8_paper_pivots={target_date} ({n})"
    conn.commit()
    return src


def _archive(conn, label, target_date, ticks, src):
    """Snapshot the run's shadow outputs into labeled result tables + a bt7_runs row."""
    with conn.cursor() as cur:
        for r in _RESULT:
            cur.execute(f"DELETE FROM harness.{r} WHERE run_label=%s", (label,))
            srct = _RESULT_SRC[r]
            cols = _cols(cur, "harness", srct)
            collist = ", ".join('"%s"' % c for c in cols)
            cur.execute(f'INSERT INTO harness.{r} (run_label, {collist}) '
                        f'SELECT %s, {collist} FROM harness."{srct}"', (label,))
        cur.execute("SELECT COUNT(*) FROM harness.bt7_qualified WHERE run_label=%s", (label,))
        quals = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM harness.bt7_positions WHERE run_label=%s", (label,))
        entries = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE result='GATE_EXIT') "
                    "FROM harness.bt7_trades WHERE run_label=%s", (label,))
        exits, gate_exits = cur.fetchone()
        from psycopg.types.json import Json
        cur.execute("""INSERT INTO harness.bt7_runs
                       (run_label, target_date, ticks, quals, entries, exits, gate_exits, source, notes,
                        status, error_detail)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ok',NULL)
                       ON CONFLICT (run_label) DO UPDATE SET
                         target_date=EXCLUDED.target_date, ticks=EXCLUDED.ticks, quals=EXCLUDED.quals,
                         entries=EXCLUDED.entries, exits=EXCLUDED.exits, gate_exits=EXCLUDED.gate_exits,
                         ran_at=NOW(), source=EXCLUDED.source, notes=EXCLUDED.notes,
                         status='ok', error_detail=NULL""",
                    (label, target_date, ticks, quals, entries, exits, gate_exits, "bt7", Json(src)))
    conn.commit()
    return {"quals": quals, "entries": entries, "exits": exits, "gate_exits": gate_exits}


def run_bt7(target_date, label, mode="parity"):
    """cc#220 ASYNC entry. The 09:15->15:30 walk takes ~2min — far longer than an MCP/HTTP
    request tolerates — so instead of blocking (the old zombie/deadlock cycle), this:

      1. grabs the single-run advisory lock (a 2nd concurrent run returns {busy:true},
         never deadlocks on TRUNCATE),
      2. marks the run 'running' so a poller sees it immediately,
      3. hands the LOCKED connection to a daemon walker thread, and returns at once.
    Poll harness.bt7_runs (or bt7_status) for status running -> ok/error + the summary.

    cc#324: mode = 'parity' (default; apply V2.1 exactly as live, hourly incl — recent-day
    replay to prove sim==live) or 'backtest' (V2.1 = week_index_52 conditions ONLY, hourly_pct/
    fall_from_day_high policy-skipped — historical sweeps where 5yr hourly data does not exist).
    The mode is recorded in harness.bt7_runs.notes.v21_mode."""
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    mode = "backtest" if str(mode).lower().strip() == "backtest" else "parity"   # cc#324
    conn = _conn()
    # single-run lock on THIS connection (held for the whole walk, auto-released on close/death)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
            got = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return {"ok": False, "label": label, "error": f"lock: {e!r}"}
    if not got:
        conn.close()
        return {"ok": True, "busy": True, "label": label,
                "msg": "another bt7 run is already walking — try again after it finishes"}
    try:
        _ensure_bt7_runs_cols(conn)
        _mark_running(conn, label, target_date)
    except Exception as e:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return {"ok": False, "label": label, "error": f"pre-walk: {e!r}"}
    threading.Thread(target=_walk, args=(conn, target_date, label, mode),
                     name=f"bt7-{label}", daemon=True).start()
    return {"ok": True, "started": True, "label": label, "date": str(target_date),
            "v21_mode": mode,
            "msg": "walk started in background — poll harness.bt7_runs / bt7_status"}


def _walk(conn, target_date, label, mode="parity"):
    """The actual walk, on the locked connection handed over by run_bt7. Owns `conn` for its
    whole life and releases the advisory lock + closes it in finally. Terminal status lands
    in harness.bt7_runs via _archive (ok) / _write_error (error). cc#324: mode ('parity'|
    'backtest') is threaded into the writer's V2.1 application and recorded in the run notes."""
    ticks = 0
    v21_backtest = (mode == "backtest")   # cc#324
    detail = None   # repr() of the first exception — surfaced to harness.bt7_runs on failure
    try:
        src = _materialize(conn, target_date)
        import v8_signal_writer, v8_paper
        with conn.cursor() as cur:
            cur.execute("SET search_path TO harness, public")
            cur.execute("SET ROLE bt7_sim")       # session-level: persists across the driven commits
        conn.commit()
        t = datetime.combine(target_date, time(9, 15))
        end = datetime.combine(target_date, time(15, 30))
        while t <= end:
            try:
                v8_signal_writer.run_live_signal_writer(conn, sim_ts=t, v21_backtest=v21_backtest)   # entries (mirrors live order)
                v8_paper.run_paper_exits(conn, target_date=target_date, mode="live", sim_ts=t)  # exits
            except Exception as e:
                conn.rollback()
                detail = f"tick {t.isoformat()}: {e!r}"
                log.error(f"bt7 tick {t}: {e}")
                raise
            ticks += 1
            if ticks % 10 == 0:
                _progress(conn, label, ticks)
            t += timedelta(minutes=5)
        with conn.cursor() as cur:
            cur.execute("RESET ROLE")
        conn.commit()
        if isinstance(src, dict):
            src["v21_mode"] = mode   # cc#324: record which V2.1 mode ran, in bt7_runs.notes
        summ = _archive(conn, label, target_date, ticks, src)
        log.info(f"bt7 run '{label}' {target_date} (v21_mode={mode}): ticks={ticks} {summ}")
    except Exception as e:
        # RESET ROLE back to the app superuser, then record the true first exception into
        # harness.bt7_runs (status='error') — the ops desk reads the DB, not Railway stdout.
        if detail is None:
            detail = repr(e)
        try:
            with conn.cursor() as cur:
                cur.execute("RESET ROLE")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        _write_error(conn, label, target_date, ticks, detail)
        log.error(f"_walk '{label}': {e}")
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
            conn.commit()
        except Exception:
            pass
        conn.close()


def bt7_status(label):
    """cc#220: one-shot poll of a run's row (status running/ok/error + summary counts)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT run_label, target_date, ticks, quals, entries, exits,
                                  gate_exits, status, error_detail, ran_at, source
                           FROM harness.bt7_runs WHERE run_label=%s""", (label,))
            r = cur.fetchone()
        if not r:
            return {"ok": True, "found": False, "label": label}
        cols = ["run_label", "target_date", "ticks", "quals", "entries", "exits",
                "gate_exits", "status", "error_detail", "ran_at", "source"]
        return {"ok": True, "found": True,
                **{c: (str(v) if v is not None else None) for c, v in zip(cols, r)}}
    except Exception as e:
        return {"ok": False, "label": label, "error": str(e)}
    finally:
        conn.close()


# ── diff ─────────────────────────────────────────────────────────────────────────
# key columns compared — the parity contract. cc#218 D6 fix: v8_qualified has NO `side`
# column (side is a pure function of basket), so the qualified diff keys on (symbol, basket)
# and DERIVES side from BASKET_META for display. v8_paper_trades DOES carry a real `side`
# column, so the trade key uses it directly.
_TRADE_KEY = ["symbol", "side", "basket", "result"]


def _side_for(basket):
    """BUY/SELL for a basket via BASKET_META — v8_qualified stores no side column."""
    try:
        from v8_endpoints import BASKET_META
        return (BASKET_META.get(basket) or {}).get("side", "?")
    except Exception:
        return "?"


def _rows(cur, table, label, keycols):
    cur.execute(f"SELECT {', '.join(keycols)} FROM harness.{table} WHERE run_label=%s "
                f"ORDER BY {', '.join(keycols)}", (label,))
    return [tuple(str(x) for x in r) for r in cur.fetchall()]


def _qual_rows(cur, table, label):
    """(symbol, side, basket) for a harness bt7_qualified run — side derived from basket."""
    cur.execute(f"SELECT symbol, basket FROM harness.{table} WHERE run_label=%s "
                f"ORDER BY symbol, basket", (label,))
    return set((str(sym), _side_for(bk), str(bk)) for sym, bk in cur.fetchall())


def bt7_diff(label_a, label_b):
    """Zero-diff report between two runs on quals + trades (symbol/side/basket[/result]).
    Special label 'golden_YYYYMMDD' compares against the archived golden_qualified_YYYYMMDD."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            out = {"label_a": label_a, "label_b": label_b, "zero_diff": True, "sections": {}}
            # qualified — key on (symbol, basket), side DERIVED from basket (no `side` col)
            a = _qual_rows(cur, "bt7_qualified", label_a)
            if label_b.startswith("golden_"):
                ymd = label_b.split("_", 1)[1]
                gt = f"golden_qualified_{ymd}"
                if _regclass(cur, "public." + gt):
                    cur.execute(f'SELECT symbol, basket FROM public."{gt}" ORDER BY symbol, basket')
                    b = set((str(sym), _side_for(bk), str(bk)) for sym, bk in cur.fetchall())
                else:
                    return {"ok": False, "error": f"{gt} not found"}
            else:
                b = _qual_rows(cur, "bt7_qualified", label_b)
            only_a = sorted(a - b); only_b = sorted(b - a)
            out["sections"]["qualified"] = {"count_a": len(a), "count_b": len(b),
                                            "only_in_a": only_a, "only_in_b": only_b,
                                            "match": not only_a and not only_b}
            if only_a or only_b:
                out["zero_diff"] = False
            # trades (skip vs golden-qualified which has no trades)
            if not label_b.startswith("golden_"):
                ta = set(_rows(cur, "bt7_trades", label_a, _TRADE_KEY))
                tb = set(_rows(cur, "bt7_trades", label_b, _TRADE_KEY))
                oa = sorted(ta - tb); ob = sorted(tb - ta)
                out["sections"]["trades"] = {"count_a": len(ta), "count_b": len(tb),
                                             "only_in_a": oa, "only_in_b": ob,
                                             "match": not oa and not ob}
                if oa or ob:
                    out["zero_diff"] = False
            out["ok"] = True
            return out
    except Exception as e:
        log.error(f"bt7_diff: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()

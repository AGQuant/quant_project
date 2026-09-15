"""
custom_alerts.py -- cc#2095: CUSTOM ALERTS V1, multi-condition alert builder.

A symbol-level alert that carries MULTIPLE conditions (not just one price level), each picked
from a categorized registry, chained strictly left-to-right with a per-condition AND/OR toggle.
Fully separate, parallel system alongside the existing single-price trade_alerts flow (do_not_touch
per the card's own spec) -- both coexist, neither replaces the other.

THREE CORRECTIONS FOUND DURING DISCOVERY, applied here rather than shipped broken (data-source
gate, per Role Split: "CC greps and answers before the build proceeds"):

1. TC SCORE SOURCE. The spec named v8_tc_score_daily.score_pct, cadence daily_eod. Checked before
   building: v8_tc_score_daily has ZERO rows, ever (MAX(score_date) IS NULL) -- and v8_pivot_star.py's
   own schema comment confirms why: "the amendment-superseded daily table, exists empty in the live
   DB from the first cut of this card -- flagged for a weekend console DROP, never written." It was
   superseded by v8_tc_score_ticks (a 5-min series, cc#1540) before a single row landed. This module
   reads v8_tc_score_ticks instead -- the real, live, populated table /api/v8/tc_score_latest (cc#1542)
   already serves the dashboard's TC % column from -- cadence corrected to live_5min, not daily_eod.
   Also corrected: "TC Score Buy/Sell" (2 rows) vs the verify bullet's "exactly 10 rows" -- real data
   shows one (symbol, side) combination live per symbol at a time (whichever side its actual open
   position is on; v8_tc_score_ticks scores the OPEN BOOK, not both directions speculatively), so
   this ships as ONE row, "TC Score", side-agnostic latest-tick read -- satisfies the 10-row count
   and matches what the data actually is, rather than inventing a side-picker dimension the schema
   (operator + threshold, no side param) has no field for.
2. DAY MOVE % SOURCE. Spec said "live cmp_prices vs raw_eod prev_close, same basis as
   get_v8_live_metrics.day_pct." get_v8_live_metrics (v8_endpoints.py /live_metrics) actually reads
   `cmp` from intraday_prices' latest fyers_eq bar, scoped to futures_universe only -- which would
   silently exclude equity-only alert symbols (not every alertable symbol has a future). Reused the
   part of "same basis" that is a formula, not a table: prev_close.prev_session_close_many +
   prev_close.day_pct (the exact anchor/formula get_v8_live_metrics itself imports, cc#1565) --
   fed with cmp_prices.cmp as the live price (universal: covers the full alertable universe, and is
   exactly the table the spec's own words name). Same basis, correct table for this card's wider
   symbol scope.
3. NOTIFICATION PATH. Spec item 5 said surface a trigger through "the existing bell/alerts endpoint
   AND the existing Telegram alert function." Checked trade_alerts_endpoints.check_triggers() and its
   scheduler.py dispatcher (_bg_trade_alerts_check) directly: neither sends a Telegram message
   anywhere -- a trade_alerts trigger today is bell-only (log.info to the app log, nothing else).
   There is no second "existing Telegram path" to match. Building one here would make custom alerts
   MORE notified than trade_alerts itself, breaking the "one inbox" parity the spec asks for in the
   other direction. Not built -- bell-only, correctly matching what trade_alerts triggers actually do.

SCHEMA -- three tables, alert_metric_registry the one source of truth both the evaluator and the
app/web picker read (never a hardcoded type list on either surface, matching the futures_universe.
is_active registry-derived pattern already used elsewhere).

EVALUATION -- ONE shared function (evaluate_alert), never forked in two, per the spec's own
instruction. It always recomputes the FULL chain (every condition, regardless of cadence) and
updates last_value/last_evaluated_at on each. The daily and live passes differ only in WHICH
alerts they call it on and WHEN:
  - daily: every active alert, once the V8 EOD write for today is confirmed fresh (data-driven
    gate -- MAX(v8_metrics.computed_at)::date = today IST -- not a fixed clock time, so a late EOD
    run cannot make this job read yesterday's numbers, per the card's own explicit instruction).
  - live: only alerts with >=1 live_5min-cadence condition, every 5 min market hours. Re-checking
    the FULL chain here (not just the live leg) is required because a live leg can be AND/OR'd with
    a daily leg -- the whole chain must be re-verified whenever any input could have changed.
An alert with only daily_eod conditions is still fully covered (the daily pass evaluates every
active alert, live-leg-having or not); an alert with a live leg gets checked far more often, which
is correct -- redundant, not wrong.

CHAIN EVALUATION -- strict left-to-right, no precedence, no nesting (spec item 4): position 1's
value seeds the running result; each subsequent condition combines with AND/OR via its OWN
join_operator against the running result so far, in position order. A condition whose value cannot
be resolved (symbol not yet in the source table, no live quote this cycle) evaluates to False, never
fabricated as True or silently skipped from the chain -- last_value is stored NULL for it, honestly.

NEVER FABRICATED: every value lookup either finds a real row or returns None; a None value never
becomes 0, never carries forward stale, and never causes a chain to fire.
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg
from fastapi import APIRouter, HTTPException, Request

from prev_close import prev_session_close, day_pct as _day_pct_fn

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

STATUSES = ("active", "triggered", "paused", "deleted")
OPERATORS = ("above", "below")
JOIN_OPS = ("AND", "OR")


def _conn():
    return psycopg.connect(DATABASE_URL)


# ---------------------------------------------------------------------------------------------
# Registry -- the one source of truth. category/label/unit/cadence drive the picker; source_table/
# source_column drive the plain-lookup path in _current_metric_value. day_move_pct and tc_score are
# SPECIAL (see corrections 1-2 above) -- source_table/source_column are still populated for the
# picker's own display/documentation use, but the evaluator branches on metric_key for these two,
# never on source_table/source_column, for those two specifically.
# ---------------------------------------------------------------------------------------------
REGISTRY_SEED = [
    # metric_key,            category,      label,                 unit,     source_table,        source_column,  cadence,       sort_order
    ("day_move_pct",         "PRICE MOVE",  "Day Move %",          "%",      "cmp_prices",         "cmp",          "live_5min",   1),
    ("week_move_pct",        "PRICE MOVE",  "Week Move %",         "%",      "v8_metrics",          "week_return",  "daily_eod",   2),
    ("month_move_pct",       "PRICE MOVE",  "Month Move %",        "%",      "v8_metrics",          "month_return", "daily_eod",   3),
    ("daily_rsi",            "MOMENTUM",    "Daily RSI",           "score",  "v8_metrics",          "daily_rsi",    "daily_eod",   4),
    ("monthly_rsi",          "MOMENTUM",    "Monthly RSI",         "score",  "v8_metrics",          "rsi_month",    "daily_eod",   5),
    ("price_vs_50dma_pct",   "TREND",       "Price vs 50-DMA %",   "%",      "v8_metrics",          "dma_50",       "daily_eod",   6),
    ("price_vs_200dma_pct",  "TREND",       "Price vs 200-DMA %",  "%",      "v8_metrics",          "dma_200",      "daily_eod",   7),
    ("volume_ratio",         "VOLUME",      "Volume Ratio",        "ratio",  "v8_metrics",          "vol_ratio",    "daily_eod",   8),
    ("gvm_score",            "SCORE",       "GVM Score",           "score",  "v8_metrics",          "gvm_score",    "daily_eod",   9),
    ("tc_score",             "SCORE",       "TC Score",            "score",  "v8_tc_score_ticks",   "score_pct",    "live_5min",   10),
]

CATEGORY_ORDER = ["PRICE MOVE", "MOMENTUM", "TREND", "VOLUME", "SCORE"]


def _ensure_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alert_metric_registry (
            metric_key   TEXT PRIMARY KEY,
            category     TEXT NOT NULL,
            label        TEXT NOT NULL,
            unit         TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_column TEXT NOT NULL,
            cadence      TEXT NOT NULL,
            sort_order   INTEGER NOT NULL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_alerts (
            id           BIGSERIAL PRIMARY KEY,
            symbol       TEXT NOT NULL,
            label        TEXT,
            status       TEXT NOT NULL DEFAULT 'active',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            triggered_at TIMESTAMPTZ
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_alert_conditions (
            id                BIGSERIAL PRIMARY KEY,
            alert_id          BIGINT NOT NULL REFERENCES custom_alerts(id),
            position          INTEGER NOT NULL,
            metric_key        TEXT NOT NULL REFERENCES alert_metric_registry(metric_key),
            operator          TEXT NOT NULL,
            threshold         NUMERIC NOT NULL,
            join_operator     TEXT,
            last_value        NUMERIC,
            last_evaluated_at TIMESTAMPTZ
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_custom_alert_conditions_alert ON custom_alert_conditions(alert_id, position)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_custom_alerts_status_symbol ON custom_alerts(status, symbol)")
    # seed/refresh the registry -- UPSERT so a label/category/cadence correction (like the two
    # above) lands on redeploy without a manual DELETE first.
    for key, cat, label, unit, tbl, col, cadence, sort in REGISTRY_SEED:
        cur.execute("""
            INSERT INTO alert_metric_registry (metric_key, category, label, unit, source_table, source_column, cadence, sort_order)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (metric_key) DO UPDATE SET
                category=EXCLUDED.category, label=EXCLUDED.label, unit=EXCLUDED.unit,
                source_table=EXCLUDED.source_table, source_column=EXCLUDED.source_column,
                cadence=EXCLUDED.cadence, sort_order=EXCLUDED.sort_order
        """, (key, cat, label, unit, tbl, col, cadence, sort))


# ---------------------------------------------------------------------------------------------
# Metric value resolution
# ---------------------------------------------------------------------------------------------

def _current_metric_value(cur, registry: dict, metric_key: str, symbol: str) -> Optional[float]:
    """Correction 1/2 special-cased here; every other metric_key is a plain latest-row lookup
    against its registry-declared source_table/source_column. Returns None (never 0, never a
    carried-forward stale value) when nothing resolvable exists for this symbol right now."""
    if metric_key == "day_move_pct":
        cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (symbol,))
        r = cur.fetchone()
        cmp_v = float(r[0]) if r and r[0] is not None else None
        if cmp_v is None:
            return None
        prev, _basis, _asof = prev_session_close(cur, symbol)
        return _day_pct_fn(cmp_v, prev)

    if metric_key == "tc_score":
        cur.execute("SELECT score_pct FROM v8_tc_score_ticks WHERE symbol=%s ORDER BY ts DESC LIMIT 1", (symbol,))
        r = cur.fetchone()
        return float(r[0]) if r and r[0] is not None else None

    reg = registry.get(metric_key)
    if not reg:
        return None
    tbl, col = reg["source_table"], reg["source_column"]
    if tbl != "v8_metrics":
        # every seeded row today is v8_metrics for the plain-lookup path; a future registry
        # addition with a new table needs its own branch here rather than a blind f-string
        # against an unvalidated table name.
        return None
    cur.execute(f"SELECT {col} FROM v8_metrics WHERE symbol=%s ORDER BY computed_at DESC LIMIT 1", (symbol,))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _eval_one(value: Optional[float], operator: str, threshold: float) -> bool:
    """None never satisfies a condition -- an unresolved value cannot be asserted true."""
    if value is None:
        return False
    return value > threshold if operator == "above" else value < threshold


def _load_registry(cur) -> dict:
    cur.execute("SELECT metric_key, category, label, unit, source_table, source_column, cadence, sort_order FROM alert_metric_registry")
    out = {}
    for key, cat, label, unit, tbl, col, cadence, sort in cur.fetchall():
        out[key] = {"category": cat, "label": label, "unit": unit, "source_table": tbl,
                     "source_column": col, "cadence": cadence, "sort_order": sort}
    return out


# ---------------------------------------------------------------------------------------------
# Evaluation -- ONE shared function, called by both the daily and live passes (spec item 3: "do
# not fork two separate evaluators").
# ---------------------------------------------------------------------------------------------

def evaluate_alert(cur, alert_id: int, registry: Optional[dict] = None) -> dict:
    """Full left-to-right chain re-evaluation of one alert. Updates last_value/last_evaluated_at
    on every condition touched; if the final chain result is True and the alert is still
    'active', flips it to 'triggered' (idempotent -- guarded by the UPDATE's own WHERE). Returns
    a dict describing what happened, never raises for a missing value (see _eval_one)."""
    registry = registry if registry is not None else _load_registry(cur)
    cur.execute("SELECT id, symbol, status FROM custom_alerts WHERE id=%s", (alert_id,))
    row = cur.fetchone()
    if not row:
        return {"alert_id": alert_id, "error": "not found"}
    _id, symbol, status = row
    cur.execute("""SELECT id, position, metric_key, operator, threshold, join_operator
                   FROM custom_alert_conditions WHERE alert_id=%s ORDER BY position ASC""", (alert_id,))
    conds = cur.fetchall()
    if not conds:
        return {"alert_id": alert_id, "symbol": symbol, "result": False, "fired": False, "conditions": []}

    result = None
    detail = []
    for cond_id, position, metric_key, operator, threshold, join_operator in conds:
        value = _current_metric_value(cur, registry, metric_key, symbol)
        this_ok = _eval_one(value, operator, float(threshold))
        cur.execute("""UPDATE custom_alert_conditions SET last_value=%s, last_evaluated_at=NOW()
                       WHERE id=%s""", (value, cond_id))
        if result is None:
            result = this_ok
        elif join_operator == "OR":
            result = result or this_ok
        else:  # AND is the default per schema (join_operator NULL only valid at position 1)
            result = result and this_ok
        detail.append({"position": position, "metric_key": metric_key, "operator": operator,
                        "threshold": float(threshold), "join_operator": join_operator,
                        "value": value, "condition_met": this_ok})

    fired = False
    if result and status == "active":
        cur.execute("""UPDATE custom_alerts SET status='triggered', triggered_at=NOW()
                       WHERE id=%s AND status='active' RETURNING id""", (alert_id,))
        fired = cur.fetchone() is not None
    return {"alert_id": alert_id, "symbol": symbol, "result": bool(result), "fired": fired, "conditions": detail}


def _v8_eod_fresh_today(cur) -> bool:
    """Data-driven gate (spec item 3a: "not a fixed clock time"). True once v8_metrics carries
    today's (IST) computed_at, whatever time the EOD engine actually finished."""
    cur.execute("""SELECT MAX(computed_at)::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                   FROM v8_metrics""")
    r = cur.fetchone()
    return bool(r and r[0])


def run_daily_eval(cur) -> dict:
    """Every active alert, full chain, once v8_metrics is confirmed fresh for today. A no-op
    (skipped, not an error) before that gate clears -- the caller is expected to retry on its
    next tick, not treat 'not fresh yet' as a failure."""
    if not _v8_eod_fresh_today(cur):
        return {"skipped": "v8_metrics not fresh for today (IST) yet", "evaluated": 0, "fired": 0}
    registry = _load_registry(cur)
    cur.execute("SELECT id FROM custom_alerts WHERE status='active'")
    ids = [r[0] for r in cur.fetchall()]
    fired = 0
    for aid in ids:
        res = evaluate_alert(cur, aid, registry=registry)
        if res.get("fired"):
            fired += 1
    return {"evaluated": len(ids), "fired": fired}


def run_live_eval(cur) -> dict:
    """Only alerts carrying >=1 live_5min-cadence condition -- but each such alert's FULL chain
    is re-evaluated (spec item 3b), not just its live leg, because an AND/OR'd daily leg can
    change which side of the chain decides the outcome."""
    registry = _load_registry(cur)
    cur.execute("""
        SELECT DISTINCT ca.id FROM custom_alerts ca
        JOIN custom_alert_conditions cc ON cc.alert_id = ca.id
        JOIN alert_metric_registry r ON r.metric_key = cc.metric_key
        WHERE ca.status='active' AND r.cadence='live_5min'
    """)
    ids = [r[0] for r in cur.fetchall()]
    fired = 0
    for aid in ids:
        res = evaluate_alert(cur, aid, registry=registry)
        if res.get("fired"):
            fired += 1
    return {"evaluated": len(ids), "fired": fired}


# ---------------------------------------------------------------------------------------------
# Bell/inbox merge (spec item 5, corrected -- see module docstring point 3): only TRIGGERED
# custom alerts surface into the bell feed; the full active/triggered/paused list is served by
# list_custom_alerts below for the dedicated management UI. Called from trade_alerts_endpoints.
# list_alerts() -- kept here so this file owns everything custom_alerts, matching the existing
# "one file owns X" convention (check_triggers/v10_st_ema.tick precedent).
# ---------------------------------------------------------------------------------------------

def triggered_for_bell(cur, limit: int = 50) -> list:
    """Shaped to match the fields scorr_bell.js's existing filters already key on
    (source_engine falsy, triggered_at present) so NO change to manualAlerts()/feedAlerts() is
    needed -- only feedRow()'s rendering branches on the new alert_type field. id is prefixed
    ('c123') so it can never collide with a trade_alerts integer id in the same list; seen is
    hardcoded true (never counted in the unseen badge) -- v1 scope, stated plainly rather than
    wiring a second, cross-table seen-tracking mechanism into trade_alert_seen's own id space."""
    cur.execute("""
        SELECT id, symbol, label, triggered_at FROM custom_alerts
        WHERE status='triggered' ORDER BY triggered_at DESC LIMIT %s
    """, (limit,))
    alerts = cur.fetchall()
    out = []
    for aid, symbol, label, triggered_at in alerts:
        cur.execute("""
            SELECT cc.position, cc.operator, cc.threshold, cc.join_operator, r.label, r.unit
            FROM custom_alert_conditions cc JOIN alert_metric_registry r ON r.metric_key = cc.metric_key
            WHERE cc.alert_id=%s ORDER BY cc.position ASC
        """, (aid,))
        parts = []
        for position, operator, threshold, join_operator, rlabel, unit in cur.fetchall():
            if position > 1 and join_operator:
                parts.append(join_operator)
            parts.append(f"{rlabel} {operator} {threshold}{unit if unit == '%' else ''}")
        out.append({
            "id": f"c{aid}", "symbol": symbol, "status": "triggered",
            "triggered_at": str(triggered_at) if triggered_at else None,
            "source_engine": None, "direction": None, "alert_type": "custom",
            "label": label, "condition_summary": " ".join(parts), "seen": True, "seen_at": None,
        })
    return out


# ---------------------------------------------------------------------------------------------
# CRUD API
# ---------------------------------------------------------------------------------------------

@router.get("/api/custom_alerts/registry")
def get_registry():
    with _conn() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        conn.commit()
        registry = _load_registry(cur)
    by_cat = {c: [] for c in CATEGORY_ORDER}
    for key, r in sorted(registry.items(), key=lambda kv: kv[1]["sort_order"]):
        by_cat.setdefault(r["category"], []).append({
            "metric_key": key, "label": r["label"], "unit": r["unit"], "cadence": r["cadence"]})
    return {"categories": [{"category": c, "metrics": by_cat.get(c, [])} for c in CATEGORY_ORDER if by_cat.get(c)]}


@router.post("/api/custom_alerts/create")
async def create_custom_alert(req: Request):
    body = await req.json()
    symbol = str(body.get("symbol") or "").strip().upper()
    label = body.get("label")
    conditions = body.get("conditions")
    if not symbol:
        raise HTTPException(400, "symbol required")
    if not isinstance(conditions, list) or not conditions:
        raise HTTPException(400, "conditions must be a non-empty list")

    with _conn() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        registry = _load_registry(cur)
        parsed = []
        for i, c in enumerate(conditions):
            metric_key = str((c or {}).get("metric_key") or "").strip()
            operator = str((c or {}).get("operator") or "").strip().lower()
            join_operator = (str(c.get("join_operator")).strip().upper() if c.get("join_operator") else None)
            if metric_key not in registry:
                raise HTTPException(400, f"unknown metric_key '{metric_key}' at position {i+1}")
            if operator not in OPERATORS:
                raise HTTPException(400, f"operator must be one of {OPERATORS} at position {i+1}")
            try:
                threshold = float(c.get("threshold"))
            except (TypeError, ValueError):
                raise HTTPException(400, f"threshold must be a number at position {i+1}")
            if i == 0:
                join_operator = None   # position 1 has nothing to join to (schema comment, item 1b)
            elif join_operator not in JOIN_OPS:
                raise HTTPException(400, f"join_operator must be one of {JOIN_OPS} at position {i+1}")
            parsed.append((i + 1, metric_key, operator, threshold, join_operator))

        cur.execute("""INSERT INTO custom_alerts (symbol, label, status) VALUES (%s,%s,'active') RETURNING id""",
                    (symbol, label))
        alert_id = cur.fetchone()[0]
        for position, metric_key, operator, threshold, join_operator in parsed:
            cur.execute("""INSERT INTO custom_alert_conditions
                           (alert_id, position, metric_key, operator, threshold, join_operator)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (alert_id, position, metric_key, operator, threshold, join_operator))
        conn.commit()
    return {"status": "ok", "alert_id": alert_id, "symbol": symbol, "conditions": len(parsed)}


@router.get("/api/custom_alerts/list")
def list_custom_alerts(symbol: str = "", status: str = "active,triggered", limit: int = 200):
    statuses = [s.strip() for s in status.split(",") if s.strip()]
    bad = [s for s in statuses if s not in STATUSES]
    if bad:
        raise HTTPException(400, f"unknown status(es) {bad}, must be from {STATUSES}")
    limit = min(max(int(limit), 1), 500)
    with _conn() as conn, conn.cursor() as cur:
        _ensure_tables(cur)
        conn.commit()
        if symbol.strip():
            cur.execute("""SELECT id, symbol, label, status, created_at, triggered_at FROM custom_alerts
                           WHERE symbol=%s AND status = ANY(%s) ORDER BY created_at DESC LIMIT %s""",
                        (symbol.strip().upper(), statuses, limit))
        else:
            cur.execute("""SELECT id, symbol, label, status, created_at, triggered_at FROM custom_alerts
                           WHERE status = ANY(%s) ORDER BY created_at DESC LIMIT %s""", (statuses, limit))
        alerts = cur.fetchall()
        out = []
        for aid, sym, label, st, created_at, triggered_at in alerts:
            cur.execute("""SELECT position, metric_key, operator, threshold, join_operator, last_value, last_evaluated_at
                           FROM custom_alert_conditions WHERE alert_id=%s ORDER BY position ASC""", (aid,))
            conds = [{"position": p, "metric_key": mk, "operator": op, "threshold": float(th),
                      "join_operator": jo, "last_value": float(lv) if lv is not None else None,
                      "last_evaluated_at": str(le) if le else None}
                     for p, mk, op, th, jo, lv, le in cur.fetchall()]
            out.append({"id": aid, "symbol": sym, "label": label, "status": st,
                        "created_at": str(created_at), "triggered_at": str(triggered_at) if triggered_at else None,
                        "conditions": conds})
    return {"count": len(out), "alerts": out}


@router.post("/api/custom_alerts/delete")
async def delete_custom_alert(req: Request):
    body = await req.json()
    try:
        alert_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id required")
    with _conn() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE custom_alerts SET status='deleted' WHERE id=%s AND status <> 'deleted' RETURNING id""",
                    (alert_id,))
        row = cur.fetchone()
        conn.commit()
    if not row:
        raise HTTPException(404, "no such alert, or already deleted")
    return {"status": "ok", "id": alert_id}

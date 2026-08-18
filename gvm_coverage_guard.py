"""
gvm_coverage_guard.py — cc#1095 Sprint 5 · P1 · the silent-blank guard
=======================================================================
For every SCORED GVM metric, measure how much of the universe its RESOLVED source actually
covers, and report it. Read-only: this module writes nothing to gvm_scores and changes no score.

WHY IT EXISTS. cc#1094 was not one bad column, it was a class. screener_raw."Operating profit
growth" is populated for 0 of 1816 rows, so OPM Expansion rendered a dash with rank #-/0 on EVERY
company page, dropped out of its pillar under the cc#828 part_3 exclusion rule, and Trackrecord
quietly averaged 7 of 8 metrics across the whole platform. Nothing anywhere said so. A founder
screenshot found it, not the system — and a defect that only a screenshot can find will be found
again the same way.

WHY THE EXISTING GUARD CANNOT CATCH IT. cc#828 part_1 alerts when a column that WAS above 90%
populated vanishes on a clean-replace. A column that was NEVER populated, or that arrives empty
every single week, never trips it. It watches for a fall, and this class never falls — it starts
at zero and stays there.

THE METRIC LIST IS DERIVED, NEVER COPIED. It is built from gvm_company_report.PARAMS and _M_EXTRA
at import. A hardcoded list would be correct on the day it was written and wrong the first time a
row is added to PARAMS — and the failure would be silent, which is precisely the shape of defect
this file exists to end. If the guard cannot see a metric, it cannot report that the metric is
blind.

RESOLUTION MIRRORS THE REPORT, because measuring a different source than the page reads would
produce a number nobody could act on:
  * a source in _COMPUTED_COLS is measured through its SQL expression, not as a bare column
  * a source with a _NATIVE_FALLBACK entry is measured primary-first, then the universe_technicals
    equivalent, and `fallback_used` records whether the fallback was doing the carrying
  * the 5 _M_EXTRA metrics come from momentum_scores, as they do in the report
The universe is the SCORED universe — gvm_scores at its latest score_date — not every row in
screener_raw. A metric is only blind for companies that are actually being scored.

Run standalone:  python3 gvm_coverage_guard.py
"""

import logging
import os

log = logging.getLogger("scorr.gvm.coverage")

# Under 50% of the scored universe: the metric is effectively blind and its pillar is quietly
# short. Between 50% and 90%: it works, but a material slice of the universe is being scored on a
# smaller denominator than the page implies. Both thresholds are Fable's, from the cc#1095 card.
ALERT_PCT = 50.0
WARN_PCT = 90.0


def _conn():
    import psycopg
    return psycopg.connect(os.getenv("DATABASE_URL"))


def metric_sources():
    """Every scored metric and the source it actually resolves through.

    Derived from gvm_company_report at call time — PARAMS, _M_EXTRA, _COMPUTED_COLS and
    _NATIVE_FALLBACK are read, never restated. Returns a list of dicts, one per metric.
    """
    import gvm_company_report as R

    out = []
    for key, label, group, source, _hib, _prefix, unit in R.PARAMS:
        out.append({
            "metric": key, "label": label, "group": group, "unit": unit,
            "source": source,
            "table": "screener_raw",
            "expr": R._COMPUTED_COLS.get(source),          # None => plain column
            "fallback": R._NATIVE_FALLBACK.get(source),    # None => no native equivalent
        })
    for key, label, unit, col, _hib in R._M_EXTRA:
        out.append({
            "metric": key, "label": label, "group": "Technicals", "unit": unit,
            "source": col, "table": "momentum_scores", "expr": None, "fallback": None,
        })
    return out


def _scored_universe(cur):
    cur.execute("SELECT COUNT(*) FROM gvm_scores WHERE score_date = "
                "(SELECT MAX(score_date) FROM gvm_scores)")
    return int(cur.fetchone()[0])


def _screener_fill(cur, m):
    """(primary_filled, resolved_filled) for a screener_raw-sourced metric, over the scored set.

    resolved_filled counts a symbol once if EITHER the primary expression or the native fallback
    yields a value — which is what the page will actually be able to show.
    """
    prim = m["expr"] or ('s."%s"' % m["source"])
    if m["fallback"]:
        resolved = 'COALESCE(%s, u."%s")' % (prim, m["fallback"])
        join = ('LEFT JOIN universe_technicals u ON u.symbol = g.symbol '
                'AND u.score_date = (SELECT MAX(score_date) FROM universe_technicals)')
    else:
        resolved, join = prim, ""
    cur.execute(f"""
        SELECT COUNT({prim}) AS primary_filled, COUNT({resolved}) AS resolved_filled
        FROM gvm_scores g
        JOIN screener_raw s ON s.nse_code = g.symbol
        {join}
        WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
    """)
    r = cur.fetchone()
    return int(r[0]), int(r[1])


def _momentum_fill(cur, m):
    cur.execute(f"""
        SELECT COUNT(mo."{m['source']}")
        FROM gvm_scores g
        LEFT JOIN momentum_scores mo ON mo.symbol = g.symbol
             AND mo.score_date = (SELECT MAX(score_date) FROM momentum_scores)
        WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
    """)
    n = int(cur.fetchone()[0])
    return n, n


def coverage(conn=None):
    """One row per scored metric. Pure read — no writes anywhere."""
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            universe = _scored_universe(cur)
            rows = []
            for m in metric_sources():
                try:
                    if m["table"] == "momentum_scores":
                        primary, resolved = _momentum_fill(cur, m)
                    else:
                        primary, resolved = _screener_fill(cur, m)
                except Exception as e:
                    # A metric whose source column does not exist is itself a finding, and a
                    # louder one than a thin column. It must not take the whole sweep down.
                    log.warning("coverage: %s unreadable (%s)", m["metric"], e)
                    rows.append(dict(m, primary_filled=None, resolved_filled=None,
                                     universe=universe, pct=None, fallback_used=None,
                                     error=str(e)[:120]))
                    continue
                pct = round(resolved * 100.0 / universe, 1) if universe else None
                rows.append(dict(
                    m, primary_filled=primary, resolved_filled=resolved, universe=universe,
                    pct=pct,
                    # True only when the fallback is actually carrying the metric, not merely
                    # configured. A fallback that exists and is never needed is not a risk.
                    fallback_used=bool(m["fallback"]) and resolved > primary,
                    error=None,
                ))
        return {"universe": universe, "metrics": rows,
                "alert": [r for r in rows if r["pct"] is not None and r["pct"] < ALERT_PCT],
                "warn": [r for r in rows if r["pct"] is not None
                         and ALERT_PCT <= r["pct"] < WARN_PCT],
                "unreadable": [r for r in rows if r["error"]]}
    finally:
        if own:
            conn.close()


def _fmt(rep):
    w = max(len(r["metric"]) for r in rep["metrics"])
    out = ["scored universe: %d symbols" % rep["universe"], ""]
    out.append("%-*s  %-26s %8s %8s %7s  %s" % (w, "METRIC", "SOURCE", "PRIMARY", "RESOLVED",
                                                "PCT", "FALLBACK"))
    for r in sorted(rep["metrics"], key=lambda x: (x["pct"] is None, x["pct"])):
        out.append("%-*s  %-26s %8s %8s %6s%%  %s%s" % (
            w, r["metric"], (r["source"] or "")[:26],
            "-" if r["primary_filled"] is None else r["primary_filled"],
            "-" if r["resolved_filled"] is None else r["resolved_filled"],
            "-" if r["pct"] is None else r["pct"],
            "YES" if r["fallback_used"] else "",
            "   ERROR: " + r["error"] if r["error"] else ""))
    out.append("")
    out.append("under %g%%: %d   %g-%g%%: %d   unreadable: %d" % (
        ALERT_PCT, len(rep["alert"]), ALERT_PCT, WARN_PCT, len(rep["warn"]),
        len(rep["unreadable"])))
    return "\n".join(out)


if __name__ == "__main__":
    print(_fmt(coverage()))

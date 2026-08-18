"""
protocol_one.py — cc#1079 · TEAM NODE N1 · SENTINEL
====================================================
Protocol One as a scheduler job: a twice-daily platform health 1-pager written to session_log.

FORMAT is session_log 24166 (the hand-run first edition): six domains — ENGINE, FEEDS, SCHEDULER,
SCRAPE, REPORTS_CC, VERDICT — each a short prose line carrying its own GREEN / AMBER / RED.

WHAT THIS NODE DOES AND DOES NOT DO. It writes ONE session_log row. No alerting, no app card, no
write to any other table — those are node N1.2 and later. Read-only everywhere except that one
INSERT, which is the card's own boundary and worth keeping literal: a health reporter that starts
repairing things stops being a reporter.

THE ONE PIECE OF REAL ENGINEERING HERE IS GATE-AWARE STALENESS, and it is why v1 cried wolf.
Protocol One v1 measured job health by AGE, and age is meaningless against a gated cadence: at
Monday noon a Friday-afternoon trading-day job is 74 hours old and perfectly healthy, while a
plain daily 09:00 job at 27 hours has missed a run. v1 flagged four gated jobs as stale
(bg_mf_monthly_mc, the two result_radar jobs, the weekend-gated V8 EOD) and buried the one real
amber underneath them.

So this asks a different question: WHEN WAS THIS JOB LAST DUE? It parses the hour/minute out of
`cadence_human`, applies whatever gate that cadence carries (weekday, trading-day via
nse_holidays, day-of-month, month-of-quarter, day-of-week), walks back to the most recent instant
the job SHOULD have fired, and compares last_run_at against that. A job is late only if it missed
a slot that actually came round. That flags bg_ca_daily_note — which is the amber the founder
wanted found — and clears every gated job the same pass.

Cadences this cannot parse (event-triggered hooks, in-process while-loops, startup audits) are
reported as UNSCHEDULED rather than guessed at. Saying "I cannot judge this one" is the honest
output; inventing a threshold for it is how v1 got here.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("scorr.protocol_one")

IST = timezone(timedelta(hours=5, minutes=30))

# ── domain thresholds, all in minutes ───────────────────────────────────────────────────────
FEED_FRESH_MIN = 10        # intraday 5-min bars: two cycles of slack
V8_FRESH_MIN = 10          # the signal writer ticks every 5 min
PCR_FRESH_MIN = 20         # pcr_intraday runs less often than the price feed
NEWS_WINDOW_H = 6


def _now_ist():
    """Naive IST — the platform's canonical clock and the convention every timestamp column in
    this database already uses (cc#855 item 8)."""
    return datetime.now(IST).replace(tzinfo=None)


# ── cc#1079 first-run fix: the two columns that are NOT naive IST ─────────────────────────────
# The module's clock is naive IST because that is what this database's `ts` columns hold. TWO of
# the columns this reporter reads are the exception — they are `timestamp with time zone`:
#
#   scheduler_master.last_run_at   (compared against a naive due-slot -> TypeError)
#   raw_news.fetched_at            (compared against a naive bound -> silently off by 5:30)
#
# The first one is why this node had never written a row. psycopg hands back an AWARE datetime,
# _scheduler_health compared it to the naive slot from last_due(), and Python refuses to order
# aware against naive. It raised on the first job row with a last_run_at — i.e. immediately — and
# _bg_protocol_one's own `except` swallowed it, so the scheduler recorded status 'ok' for a run
# that produced nothing. Verified on the first real dispatch, 15:00 IST 17-Aug: the job fired,
# stamped ok, and session_log stayed empty.
#
# The second is quieter and worse in its way: no exception, just a news window measured from the
# wrong instant whenever the DB session TZ is not IST. It is fixed here rather than left as a
# rounding error nobody would ever notice.
def _naive_ist(dt):
    """Any datetime -> naive IST. Aware values are converted, naive ones are trusted as IST."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)


def _aware_ist(dt):
    """Naive-IST -> aware, for binding against a `timestamp with time zone` column."""
    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=IST)


def _mkt_open(now=None):
    """Is the NSE session running right now? Feed staleness only means something during it."""
    now = now or _now_ist()
    if now.weekday() >= 5:
        return False
    try:
        import nse_holidays
        if not nse_holidays.is_trading_day(now.date()):
            return False
    except Exception:
        pass
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 930          # 09:15 - 15:30 IST


# ══════════════════════════════════════════════════════════════════════════════════════════════
# GATE-AWARE STALENESS
# ══════════════════════════════════════════════════════════════════════════════════════════════
_RE_H = re.compile(r"h\s*==\s*(\d{1,2})")
_RE_M = re.compile(r"m\s*==\s*(\d{1,2})")
_RE_WEEKDAY_LT5 = re.compile(r"weekday\(\)\s*<\s*5")
_RE_WEEKDAY_EQ = re.compile(r"weekday\(\)\s*==\s*(\d)")
_RE_TRADING = re.compile(r"_is_trading_day")
_RE_DAY_EQ = re.compile(r"day\s*==\s*(\d{1,2})")
_RE_DAY_GE = re.compile(r"day\s*>=\s*(\d{1,2})")
_RE_MONTH_IN = re.compile(r"month\s+in\s*\(([\d,\s]+)\)")
_RE_EVERY_MIN = re.compile(r"m\s*%\s*(\d+)|every\s*~?\s*(\d+)\s*min")


def _day_allowed(d, cad):
    """Does the cadence's gate permit this calendar day?"""
    if _RE_WEEKDAY_LT5.search(cad) and d.weekday() >= 5:
        return False
    mw = _RE_WEEKDAY_EQ.search(cad)
    if mw and d.weekday() != int(mw.group(1)):
        return False
    mm = _RE_MONTH_IN.search(cad)
    if mm and d.month not in {int(x) for x in mm.group(1).split(",") if x.strip()}:
        return False
    md = _RE_DAY_EQ.search(cad)
    if md and d.day != int(md.group(1)):
        return False
    mg = _RE_DAY_GE.search(cad)
    if mg and d.day != int(mg.group(1)):
        # ANCHOR, not a window. `day >= 25` reads as seven eligible days, and taking the LAST of
        # them made bg_shareholding_quarterly look late by six days when it had run on the 30th.
        # These jobs self-guard after their first fire, so the DUE day is the anchor — day 25 —
        # and any run on or after it is on time. Caught by the slot unit-test, not in production.
        return False
    if _RE_TRADING.search(cad):
        try:
            import nse_holidays
            if not nse_holidays.is_trading_day(d):
                return False
        except Exception:
            pass
    return True


def last_due(cadence_human, now=None, lookback_days=120):
    """The most recent instant this cadence should have fired at, or None if unparseable.

    Walks back day by day from today, taking the first day the gate allows whose slot has already
    passed. Bounded at 120 days so a quarterly job still resolves while nothing can loop forever.
    """
    if not cadence_human:
        return None
    cad = cadence_human.strip()
    if _RE_EVERY_MIN.search(cad) and not _RE_H.search(cad):
        return None                                  # sub-hourly loops are judged by age, not slots
    mh, mm_ = _RE_H.search(cad), _RE_M.search(cad)
    day_gated = bool(_RE_DAY_EQ.search(cad) or _RE_DAY_GE.search(cad) or _RE_MONTH_IN.search(cad))
    if not mh and not day_gated:
        return None                                  # event-triggered / startup / prose-only
    # A day- or month-gated cadence with no stated hour (e.g. "now.day == 11") still has a real
    # due DAY; anchoring it at midnight judges it properly instead of dropping it as unschedulable.
    hh = int(mh.group(1)) if mh else 0
    mi = int(mm_.group(1)) if mm_ else 0
    now = now or _now_ist()
    for back in range(lookback_days):
        d = (now - timedelta(days=back)).date()
        if not _day_allowed(d, cad):
            continue
        slot = datetime(d.year, d.month, d.day, hh, mi)
        if slot <= now:
            return slot
    return None


def _scheduler_health(cur, now):
    """-> (line, colour, detail dict). Late jobs only, judged against their own last due slot."""
    cur.execute("""
        SELECT job_name, cadence_human, active, last_run_at, last_status, last_error
        FROM scheduler_master WHERE active
    """)
    rows = cur.fetchall()
    late, errored, unscheduled = [], [], 0
    # 10 minutes of grace: a job spawned at its slot writes last_run_at when it FINISHES.
    grace = timedelta(minutes=10)
    for name, cad, _active, last_run, status, err in rows:
        # last_run_at is timestamptz; every other clock in this module is naive IST. Normalise
        # HERE, at the boundary, so the comparison below cannot raise and cannot drift.
        last_run = _naive_ist(last_run)
        if status == "error":
            errored.append((name, (err or "")[:80]))
        due = last_due(cad, now)
        if due is None:
            unscheduled += 1
            continue
        # cc#1093 P2 — A JOB INSIDE ITS OWN DUE MINUTE IS NOT LATE. Protocol One ran at 15:40:00.4
        # and flagged bg_heal_intraday, whose slot is 15:40: last_due had just returned today
        # 15:40, four tenths of a second earlier, so the job was being judged for missing a slot it
        # had not yet been offered. It is due NOW, not overdue.
        #
        # The existing `grace` does the opposite job — it forgives a run that FINISHED slightly
        # before its due time — so it could never cover this. This is the other end of the same
        # window and it is exactly the card's wording: grace is its own due minute.
        #
        # Deliberately the minute and not more. A wider window (say the full 10 minutes) would also
        # hide a job that genuinely missed its slot for nine of them, and that is a judgement about
        # how long a slot may quietly slip — a decision, not a bug fix. Logged for Fable instead.
        if due.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
            continue
        if last_run is None or last_run < due - grace:
            late.append((name, str(due), str(last_run) if last_run else "never"))
    colour = "RED" if errored else ("AMBER" if late else "GREEN")
    bits = ["%d active jobs, %d in error" % (len(rows), len(errored))]
    if errored:
        bits.append("ERRORS: " + "; ".join("%s (%s)" % e for e in errored[:4]))
    if late:
        bits.append("LATE vs own last-due slot: " +
                    "; ".join("%s (due %s, last %s)" % l for l in late[:6]))
    else:
        bits.append("no job late against its own gated schedule")
    bits.append("%d unscheduled (event/loop/startup) — not judged" % unscheduled)
    return " · ".join(bits) + " — " + colour, colour, {
        "active": len(rows), "errored": errored, "late": late, "unscheduled": unscheduled,
    }


def _feeds(cur, now):
    out, colours = [], []
    open_now = _mkt_open(now)

    cur.execute("""SELECT max(ts), count(DISTINCT symbol) FROM intraday_prices
                   WHERE timeframe='5m' AND ts >= %s""", (now - timedelta(minutes=10),))
    r = cur.fetchone() or (None, 0)
    if r[0] is None:
        c = "AMBER" if open_now else "GREEN"
        out.append("fyers 5m: no bar in 10 min" + ("" if open_now else " (market closed — expected)"))
    else:
        age = (now - r[0]).total_seconds() / 60
        c = "GREEN" if age <= FEED_FRESH_MIN else ("AMBER" if open_now else "GREEN")
        out.append("fyers 5m: last bar %s, %d symbols/10m" % (r[0].strftime("%H:%M"), r[1]))
    colours.append(c)

    cur.execute("""SELECT max(ts) FROM global_intraday WHERE symbol='INDIAVIX'""")
    v = (cur.fetchone() or [None])[0]
    if v:
        out.append("India VIX 5m: %s" % v.strftime("%H:%M"))
        colours.append("GREEN" if (now - v).total_seconds() / 60 <= 15 or not open_now else "AMBER")
    else:
        out.append("India VIX 5m: no bars")
        colours.append("AMBER")

    # cc#1057: a flat PCR is the failure this check exists for — equal OI on every row reads as a
    # working feed until you look at the variance. Freshness alone would have missed it.
    cur.execute("""SELECT max(ts), count(*), count(DISTINCT pcr_atm5)
                   FROM pcr_intraday WHERE ts::date = %s""", (now.date(),))
    p = cur.fetchone() or (None, 0, 0)
    if p[0] is None:
        out.append("PCR 5m: no rows today")
        colours.append("AMBER" if open_now else "GREEN")
    else:
        flat = (p[2] or 0) <= 1 and (p[1] or 0) > 2
        out.append("PCR 5m: %s, %d rows, %d distinct values%s"
                   % (p[0].strftime("%H:%M"), p[1], p[2], " — FLAT, cc#1057 regression" if flat else ""))
        colours.append("RED" if flat else "GREEN")

    # fetched_at is timestamptz — bind an AWARE bound or Postgres reads the naive value in the
    # session timezone and the window silently slides by the IST offset.
    cur.execute("""SELECT count(*) FROM raw_news WHERE fetched_at >= %s""",
                (_aware_ist(now - timedelta(hours=NEWS_WINDOW_H)),))
    n = (cur.fetchone() or [0])[0]
    out.append("news: %d raw/%dh" % (n, NEWS_WINDOW_H))
    colours.append("GREEN" if n > 0 else "AMBER")

    worst = "RED" if "RED" in colours else ("AMBER" if "AMBER" in colours else "GREEN")
    return " · ".join(out) + " — " + worst, worst


def _engine(cur, now):
    cur.execute("""SELECT max(computed_at) FROM v8_metrics
                   WHERE score_date = (SELECT max(score_date) FROM v8_metrics)""")
    last = (cur.fetchone() or [None])[0]
    if last is None:
        return "V8 signal writer: no metrics rows — RED", "RED"
    cur.execute("""SELECT count(DISTINCT symbol) FROM v8_metrics
                   WHERE score_date = (SELECT max(score_date) FROM v8_metrics)
                     AND computed_at >= %s""", (last - timedelta(minutes=4),))
    syms = (cur.fetchone() or [0])[0]
    cur.execute("""SELECT count(DISTINCT symbol) FROM v8_metrics
                   WHERE score_date = (SELECT max(score_date) FROM v8_metrics)""")
    total = (cur.fetchone() or [0])[0]
    age = (now - last).total_seconds() / 60
    colour = "GREEN" if (age <= V8_FRESH_MIN or not _mkt_open(now)) else "AMBER"
    return ("V8 signal writer: last tick %s, %d/%d symbols/cycle%s — %s"
            % (last.strftime("%H:%M"), syms, total,
               "" if _mkt_open(now) else " (market closed)", colour)), colour


def _scrape(cur, now):
    try:
        cur.execute("SELECT count(*) FROM ops_metrics_t1_queue WHERE processed_at IS NULL")
        q = (cur.fetchone() or [0])[0]
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass
        cur.execute("SELECT count(*) FROM ops_metrics_t1_queue")
        q = (cur.fetchone() or [0])[0]
    # Ops-metrics is RETIRED (founder 09-Aug, session_log 18213). The count is reported for
    # visibility only — nothing here drains it, and a non-zero queue is not an amber.
    return "ops_metrics queue: %d rows (feature RETIRED 18213 — reported, never drained) — GREEN" % q, "GREEN"


def _reports_cc(cur):
    cur.execute("""SELECT status, count(*) FROM cc_tasks
                   WHERE status IN ('pending','in_progress','blocked') GROUP BY status""")
    counts = {r[0]: r[1] for r in cur.fetchall()}
    p, ip, b = counts.get("pending", 0), counts.get("in_progress", 0), counts.get("blocked", 0)
    colour = "AMBER" if ip > 2 else "GREEN"
    return ("cc_tasks: %d pending, %d in_progress, %d blocked — %s" % (p, ip, b, colour)), colour


def build_protocol_one(conn, run_label=None):
    """Build the 1-pager and write it as ONE session_log row. Returns (session_log_id, payload)."""
    now = _now_ist()
    run_label = run_label or ("AM" if now.hour < 12 else "PM")
    with conn.cursor() as cur:
        engine_line, c_eng = _engine(cur, now)
        feeds_line, c_feed = _feeds(cur, now)
        sched_line, c_sched, sched_detail = _scheduler_health(cur, now)
        scrape_line, c_scrape = _scrape(cur, now)
        cc_line, c_cc = _reports_cc(cur)

        colours = [c_eng, c_feed, c_sched, c_scrape, c_cc]
        worst = "RED" if "RED" in colours else ("AMBER" if "AMBER" in colours else "GREEN")
        ambers = [d for d, c in zip(("ENGINE", "FEEDS", "SCHEDULER", "SCRAPE", "REPORTS_CC"), colours)
                  if c in ("AMBER", "RED")]
        verdict = ("%s%s — written by SENTINEL (cc#1079 node N1), no human in the loop."
                   % (worst, (" · attention: " + ", ".join(ambers)) if ambers else ""))

        payload = {
            "ENGINE": engine_line,
            "FEEDS": feeds_line,
            "SCHEDULER": sched_line,
            "SCRAPE": scrape_line,
            "REPORTS_CC": cc_line,
            "VERDICT": verdict,
            "_meta": {
                "run": run_label,
                "generated_at_ist": now.isoformat(timespec="seconds"),
                "node": "N1 SENTINEL",
                "staleness": "gate-aware: each job judged against its OWN last due slot, not age",
                "late_jobs": sched_detail["late"],
                "unscheduled_jobs": sched_detail["unscheduled"],
            },
        }
        title = "PROTOCOL_ONE — platform health 1-pager (%s %s, automated)" % (
            now.strftime("%d-%b-%Y"), run_label)
        cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                       VALUES (%s, %s, 'protocol_one', %s, %s::jsonb) RETURNING id""",
                    (now.date(), now, title, __import__("json").dumps(payload)))
        new_id = cur.fetchone()[0]
    conn.commit()
    log.info("protocol_one: wrote session_log id=%s verdict=%s", new_id, worst)
    return new_id, payload

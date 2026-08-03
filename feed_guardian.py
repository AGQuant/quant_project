"""feed_guardian.py — cc#660 FEED_GUARDIAN_V1 (founder-approved 25-Jul-2026)

Single owner of feed liveness. Consolidates FIVE overlapping legacy watchdogs into ONE
detect->repair module (the 24-Jul incident proof: 5 watchdogs ALL detected a dead futures
leg, NONE repaired it — it stayed dead 6h after the alerts fired):

  - feed_staleness_watch      (cc#475): per-source stale-bar Telegram
  - feed_incident_relay       (cc#501 item_4): relay worker-written incident ops_log rows
  - open_bars_alarm           (cc#229): feed-silent-at-open
  - oi_feed_health            (cc#515): universe-wide OI-staleness tripwire
  - writer_no_intraday_bars reaction (per-leg staleness)

Two ticks (wired in scheduler.py):
  guardian_tick()          — every 5 min, MARKET HOURS: per-leg DETECT -> REPAIR
  offhours_liveness_tick() — every 15 min, ALL DAYS: worker heartbeat liveness

Founder directive (alert-fatigue fix, 24-Jul: 10+ alerts nobody could act on):
DETECT -> REPAIR per leg; **Telegram ONLY when a repair escalates/fails**. A stale leg (while
another leg still flows) runs a BOUNDED ladder the worker polls via app_config: resubscribe x2
-> restart x2 -> STAND DOWN silent for the day (cc#661). Telegram fires on the restart escalation
and on the single stand-down transition — never on routine resubscribes, never after stand-down.

Repair is executed WORKER-SIDE (worker/fyers_feed.py polls feed_guardian_cmd) — the guardian is
the brain, the worker is the hands. Guardian state lives in app_config (JSON) so escalation
counters survive the app auto-deploy that fires ~90s after every push (an in-process dict would
reset the counter on every deploy — the exact blindness that hid past incidents).

DEV_MODE_PUSH_RULE_V2: the app-side guardian deploys anytime. The worker-side hooks (heartbeat +
resub poll) live in worker/** and deploy OUTSIDE market hours only (cc#416).
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone, time as dt_time

import psycopg
from psycopg.types.json import Json

log = logging.getLogger("scorr.feed_guardian")
DATABASE_URL = os.getenv("DATABASE_URL", "")
IST = timezone(timedelta(hours=5, minutes=30))

# ── config block: ALL thresholds in one place ────────────────────────────────
STALE_MIN            = 10     # a leg whose newest bar is older than this (while another leg flows) is actionable
OPEN_SILENCE_BARS    = 400    # <this many 5m bars since 09:15 by 09:25 = feed silent at open (cc#229 floor)
OI_DEGRADE_PCT       = 20.0   # >this% of active futures with stale OI = feed-wide OI gap (cc#515)
HEARTBEAT_STALE_MIN  = 45     # worker heartbeat older than this off-hours = worker dead (item_3)
# cc#661 BOUNDED REPAIR LADDER: per leg (and "_all") the guardian escalates
#   resubscribe x RESUB_ATTEMPTS -> restart x RESTART_ATTEMPTS -> STAND DOWN (silent for the day).
# This caps repair actions at RESUB_ATTEMPTS+RESTART_ATTEMPTS per leg per trading day, killing the
# cc#660 unbounded hammer (a fundamentally broken leg previously got a RESTART command EVERY 5-min
# tick, ~70/session). On exhaustion the leg is marked failed_for_day and the guardian goes silent on
# it (no commands, no Telegram) until a fresh day, a manual reset, or the leg self-recovers.
RESUB_ATTEMPTS       = 2      # forced resubscribes before escalating to restart
RESTART_ATTEMPTS     = 2      # worker restarts before standing down
ALERT_THROTTLE_MIN   = 30     # min minutes between repeat Telegram alerts for the same key
LEGS                 = ("fyers_eq", "fyers_fut")

# worker incidents that mean a repair ESCALATED/FAILED -> Telegram-worthy. A routine
# feed_watchdog_reconnect (repair SUCCEEDED) is NOT relayed to Telegram (alert-fatigue fix);
# it is still recorded to ops_log by the worker and surfaced in the daily summary.
_ESCALATION_TITLES = ("feed_watchdog_exit", "housekeeping_db_dead_exit",
                      "feed_hard_restart", "feed_fut", "feed_boot_gap")
_RELAY_TITLES = ("feed_watchdog_reconnect", "feed_watchdog_exit", "housekeeping_db_dead_exit",
                 "feed_hard_restart", "feed_fut", "feed_boot_gap")

_STATE_KEY   = "feed_guardian_state"           # app_config JSON — persists across app redeploy
_CMD_KEY     = "feed_guardian_cmd"             # app_config JSON — worker polls this
_CMD_ACK_KEY = "feed_guardian_cmd_ack"         # app_config JSON — worker writes outcome here
_RESET_KEY   = "feed_guardian_reset"           # cc#661 manual-reset flag (Claude web flips via run_sql)
_HB_KEY_LEGACY = "worker_heartbeat_ts"         # fallback if worker_heartbeat table absent


# ── connection / IST helpers ─────────────────────────────────────────────────
def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)


def _conn():
    return psycopg.connect(DATABASE_URL, connect_timeout=10,
                           keepalives=1, keepalives_idle=30,
                           keepalives_interval=10, keepalives_count=3,
                           options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=30000")


def _telegram(msg: str):
    """Best-effort Telegram — never raises into the guardian."""
    try:
        import v10_st_ema
        v10_st_ema.telegram_alert(msg)
    except Exception as e:
        log.warning(f"feed_guardian telegram failed: {e}")


def _oplog(cur, title, details, category="feed_guardian"):
    try:
        cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                       VALUES (CURRENT_DATE, NOW(), %s, %s, %s)""",
                    (category, title, Json(details)))
    except Exception as e:
        log.warning(f"feed_guardian _oplog({title}) failed: {e}")


# ── app_config state (JSON) ──────────────────────────────────────────────────
def _cfg_get(cur, key):
    cur.execute("""CREATE TABLE IF NOT EXISTS app_config (
        key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("SELECT value FROM app_config WHERE key=%s", (key,))
    r = cur.fetchone()
    return r[0] if r else None


def _cfg_set(cur, key, value):
    cur.execute("""INSERT INTO app_config (key, value, updated_at) VALUES (%s,%s,NOW())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                (key, value))


def _load_state(cur):
    raw = _cfg_get(cur, _STATE_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_state(cur, state):
    _cfg_set(cur, _STATE_KEY, json.dumps(state))


def _throttled(state, key, now):
    """True if we alerted on `key` within ALERT_THROTTLE_MIN — suppress a repeat."""
    last = (state.get("alerts") or {}).get(key)
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return False
    return (now - last_dt).total_seconds() < ALERT_THROTTLE_MIN * 60


def _mark_alert(state, key, now):
    state.setdefault("alerts", {})[key] = now.isoformat()


# ── cc#844: THE HEARTBEAT AGE CONTRACT ────────────────────────────────────────────────────────────
# worker_heartbeat.last_beat is a NAIVE `TIMESTAMP` written by the worker with Postgres NOW(), so it
# stores UTC. This module works in IST (_ist_now). Subtracting one from the other — which is exactly
# what both call sites did — adds a permanent, invented 5h30m to every reading.
#
# The damage was not theoretical. On 03-Aug the guardian reported the worker heartbeat as "336 min
# old" while it was genuinely 6 minutes old, fired WORKER SILENT to Telegram, and pushed the master
# watchdog permanently RED. A DB audit then read that alert alongside other UTC-stored timestamps
# (v8_metrics.computed_at, ops_log.session_ts — both naive UTC too) as if they were IST, and
# concluded the whole app had been hung for 5.5 hours during live trading. It had not: ops_log had
# 102/154/96/65/75 rows in the 09:30-14:30 IST hours and v8_metrics was 1 minute fresh. A P0 was
# raised and a founder's buy-shortlist was withheld on the strength of a subtraction error.
#
# So the age is now computed BY POSTGRES, against the DB clock, with the stored basis stated
# explicitly. No Python-side timezone assumption can re-enter, and it needs no data migration.
def _heartbeat_age_min(cur):
    """Minutes since the worker's last heartbeat, or None if it has never written one."""
    cur.execute("""SELECT ROUND((EXTRACT(EPOCH FROM (NOW() - last_beat AT TIME ZONE 'UTC')) / 60.0)::numeric, 1)
                   FROM worker_heartbeat WHERE id = 1""")
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


def _ensure_heartbeat_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS worker_heartbeat (
        id INTEGER PRIMARY KEY DEFAULT 1,
        last_beat TIMESTAMP,
        boot_ts   TIMESTAMP,
        note      TEXT,
        CONSTRAINT worker_heartbeat_singleton CHECK (id = 1))""")


# ── the command channel to the worker ────────────────────────────────────────
def _issue_cmd(cur, action, leg, reason):
    """Write a resubscribe/restart command the worker polls (feed_guardian_cmd). A monotonic
    nonce makes the worker act at most once per command. Returns the nonce."""
    prev = _cfg_get(cur, _CMD_KEY)
    nonce = 1
    if prev:
        try:
            nonce = int(json.loads(prev).get("nonce", 0)) + 1
        except Exception:
            nonce = 1
    cmd = {"action": action, "leg": leg, "nonce": nonce, "reason": reason,
           "issued_ist": _ist_now().isoformat()}
    _cfg_set(cur, _CMD_KEY, json.dumps(cmd))
    _oplog(cur, "guardian_repair", {"action": action, "leg": leg, "nonce": nonce, "reason": reason})
    log.error(f"feed_guardian: issued {action} for {leg} (nonce={nonce}) — {reason}")
    return nonce


def _cmd_ack(cur):
    raw = _cfg_get(cur, _CMD_ACK_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ── per-leg freshness read ───────────────────────────────────────────────────
def _leg_ages(cur, now):
    """Age (minutes) of the newest 5m bar per source, TODAY only. None if no bar yet today."""
    ages = {}
    for src in LEGS:
        cur.execute("SELECT MAX(ts) FROM intraday_prices WHERE source=%s AND ts::date=%s",
                    (src, now.date()))
        last = cur.fetchone()[0]
        ages[src] = None if last is None else (now - last).total_seconds() / 60.0
    return ages


def _bars_since_open(cur, now):
    open_ts = now.replace(hour=9, minute=15, second=0, microsecond=0)
    cur.execute("SELECT COUNT(*) FROM intraday_prices WHERE ts >= %s AND timeframe='5m'", (open_ts,))
    return cur.fetchone()[0]


# ── incident relay (Telegram ONLY for escalations) ───────────────────────────
def _relay_incidents(cur, state, now):
    """Relay NEW worker-written ops_log(category='alert') incident rows. Telegram only for
    ESCALATION titles (a repair that failed/escalated); routine reconnects are recorded to
    ops_log by the worker but NOT Telegrammed (alert-fatigue fix). High-water-mark in state so
    an app redeploy never replays. First-ever run seeds the mark silently."""
    last_id = state.get("relay_last_id")
    if last_id is None:
        cur.execute("SELECT COALESCE(MAX(id),0) FROM ops_log")
        state["relay_last_id"] = cur.fetchone()[0]
        return
    last_id = int(last_id)
    cur.execute("""SELECT id, title, details FROM ops_log
                   WHERE category='alert' AND title = ANY(%s) AND id > %s
                   ORDER BY id ASC LIMIT 50""", (list(_RELAY_TITLES), last_id))
    rows = cur.fetchall()
    max_id = last_id
    for rid, title, details in rows:
        max_id = max(max_id, rid)
        d = details if isinstance(details, dict) else {}
        if title == "feed_boot_gap":
            gap = d.get("gap_min")
            if not (d.get("market_open") and gap is not None and float(gap) > 15):
                continue   # only >15min-at-market-open boot gaps escalate
        if title in _ESCALATION_TITLES:
            detail = d.get("detail") or d.get("note") or d.get("event") or ""
            _telegram(f"FEED GUARDIAN — worker incident '{title}': {detail} (repair escalated).")
    if max_id != last_id:
        state["relay_last_id"] = max_id


# ── cc#661 bounded repair ladder + resets ────────────────────────────────────
def _apply_resets(cur, state, now):
    """cc#661 reset conditions, run at the top of every tick:
    (a) MANUAL — app_config['feed_guardian_reset'] truthy -> clear ALL leg counters + failed_for_day,
        delete the key, ops_log guardian_manual_reset (Claude web flips it after human intervention).
    (b) AUTOMATIC daily — on the first tick of a new trading date, wipe the ladder so every leg starts
        the day with a fresh budget (failed_for_day is date-stamped, so a new day frees a stood-down leg)."""
    legs = state.setdefault("legs", {})
    today = now.date().isoformat()

    raw = _cfg_get(cur, _RESET_KEY)
    if raw is not None and str(raw).strip().lower() not in ("", "0", "false", "no", "off"):
        for st in legs.values():
            if isinstance(st, dict):
                st["attempts"] = 0; st["repairing"] = False
                st["failed_for_day"] = None; st["stood_down_alerted"] = False
        cur.execute("DELETE FROM app_config WHERE key=%s", (_RESET_KEY,))
        state["budget_day"] = today
        _oplog(cur, "guardian_manual_reset", {"cleared_legs": list(legs.keys())})
        log.error("feed_guardian: MANUAL RESET — repair ladder cleared, budget refreshed")
        return

    if state.get("budget_day") != today:
        for st in legs.values():
            if isinstance(st, dict):
                st["attempts"] = 0; st["repairing"] = False
                st["failed_for_day"] = None; st["stood_down_alerted"] = False
        state["budget_day"] = today


def _leg_stood_down(legs, key, now):
    st = legs.get(key)
    return bool(st and st.get("failed_for_day") == now.date().isoformat())


def _ladder_step(cur, state, legs, key, leg_for_cmd, now, reason, summary):
    """Advance the bounded repair ladder for `key` ('_all' or a leg name):
    resubscribe x RESUB_ATTEMPTS -> restart x RESTART_ATTEMPTS -> STAND DOWN (silent).
    Returns the action taken. Telegram fires ONLY on restart escalation and on the single
    stand-down transition — never on the routine resubscribe steps (alert-fatigue rule)."""
    today = now.date().isoformat()
    st = legs.setdefault(key, {"attempts": 0, "repairing": False,
                               "failed_for_day": None, "stood_down_alerted": False})
    # already exhausted today -> stay SILENT: no command, no Telegram, just mark the state
    if st.get("failed_for_day") == today:
        st["repairing"] = True
        summary["actions"].append(f"{key}_standing_down")
        return "standing_down"

    attempts = int(st.get("attempts", 0)) + 1
    st["attempts"] = attempts
    st["repairing"] = True

    if attempts <= RESUB_ATTEMPTS:
        _issue_cmd(cur, "resubscribe", leg_for_cmd,
                   f"{reason} — resubscribe {attempts}/{RESUB_ATTEMPTS}")
        summary["actions"].append(f"{key}_resubscribe")
        return "resubscribe"
    elif attempts <= RESUB_ATTEMPTS + RESTART_ATTEMPTS:
        rnum = attempts - RESUB_ATTEMPTS
        _issue_cmd(cur, "restart", leg_for_cmd,
                   f"{reason} — restart {rnum}/{RESTART_ATTEMPTS} after {RESUB_ATTEMPTS} resubscribes")
        summary["actions"].append(f"{key}_restart")
        if not _throttled(state, f"{key}_restart", now):
            _telegram(f"FEED REPAIR ESCALATING — {key} still down ({reason}); worker restart "
                      f"{rnum}/{RESTART_ATTEMPTS} issued.")
            _mark_alert(state, f"{key}_restart", now)
        return "restart"
    else:
        # ladder exhausted -> STAND DOWN for the day (no command issued)
        st["failed_for_day"] = today
        summary["actions"].append(f"{key}_stand_down")
        if not st.get("stood_down_alerted"):
            st["stood_down_alerted"] = True
            _telegram(f"REPAIR EXHAUSTED — {key} — {RESUB_ATTEMPTS} resubscribes + {RESTART_ATTEMPTS} "
                      f"restarts failed, standing down for the day, manual intervention needed.")
            _oplog(cur, "guardian_stand_down", {"leg": key, "reason": reason, "attempts": attempts})
            log.critical(f"feed_guardian: {key} REPAIR EXHAUSTED — standing down for {today}")
        return "stand_down"


def _ladder_clear(cur, legs, key, now, summary):
    """Leg is flowing again -> clear its ladder. If it was mid-repair or STOOD DOWN, record a
    guardian_recovery (after_standdown flag) so the recovery is visible; never Telegram."""
    st = legs.get(key)
    if not st:
        return
    was_repairing = bool(st.get("repairing"))
    was_stood_down = (st.get("failed_for_day") == now.date().isoformat())
    if was_repairing or was_stood_down:
        _oplog(cur, "guardian_recovery", {"leg": key, "after_standdown": was_stood_down})
        log.info(f"feed_guardian: {key} recovered (after_standdown={was_stood_down})")
    st["attempts"] = 0
    st["repairing"] = False
    st["failed_for_day"] = None
    st["stood_down_alerted"] = False


# ── the 5-min market-hours tick ──────────────────────────────────────────────
def guardian_tick():
    """Every 5 min during market hours: per-leg DETECT -> REPAIR. Returns a summary dict.

    Logic:
      - open-silence at 09:20-09:30: <OPEN_SILENCE_BARS bars since open -> full resubscribe + alert.
      - per leg: stale >STALE_MIN while ANOTHER leg is fresh -> bounded ladder (resubscribe x2 ->
        restart x2 -> STAND DOWN silent for the day). Recovered -> clear the ladder (ops_log recovery).
      - ALL legs stale -> full-feed silence -> same bounded ladder on "_all".
      - stood-down legs stay VISIBLE via guardian_summary red_flags but issue no commands/Telegrams.
      - relay worker incidents (Telegram only for escalations).
      - OI-degradation tripwire (ops_log; surfaced in the daily summary)."""
    now = _ist_now()
    summary = {"ts": now.isoformat(), "actions": [], "legs": {}}
    try:
        with _conn() as conn, conn.cursor() as cur:
            state = _load_state(cur)
            _apply_resets(cur, state, now)          # cc#661: manual + daily budget reset first
            legs = state.setdefault("legs", {})
            ages = _leg_ages(cur, now)
            summary["legs"] = {k: (round(v, 1) if v is not None else None) for k, v in ages.items()}

            fresh = {s: (a is not None and a <= STALE_MIN) for s, a in ages.items()}
            any_fresh = any(fresh.values())
            all_stale = not any_fresh

            # (1) feed-silent-at-open (cc#229) — only in the 09:20-09:30 window
            if dt_time(9, 20) <= now.time() <= dt_time(9, 30):
                nbars = _bars_since_open(cur, now)
                if nbars < OPEN_SILENCE_BARS:
                    n = _issue_cmd(cur, "resubscribe", "all",
                                   f"feed silent at open: {nbars} 5m bars since 09:15 (<{OPEN_SILENCE_BARS})")
                    summary["actions"].append("open_silence_resubscribe")
                    if not _throttled(state, "open_silence", now):
                        _telegram(f"FEED SILENT AT OPEN — {nbars} bars since 09:15. Guardian forced a "
                                  f"full resubscribe (nonce {n}). Check truthful-friendship.")
                        _mark_alert(state, "open_silence", now)

            # (2) per-leg detect -> REPAIR via the cc#661 bounded ladder (resubscribe x2 ->
            #     restart x2 -> STAND DOWN silent). A stood-down leg issues NO command and NO
            #     Telegram until a new day, a manual reset, or self-recovery.
            if all_stale and any(a is not None for a in ages.values()):
                # full-feed silence — run the ladder on "_all"; per-leg ladders stay untouched
                # (they clear naturally once one leg returns). One first-detection heads-up only.
                action = _ladder_step(cur, state, legs, "_all", "all", now,
                                      f"ALL legs stale ({summary['legs']})", summary)
                if action == "resubscribe" and not _throttled(state, "all_stale", now):
                    _telegram(f"FEED FULL SILENCE — all legs stale ({summary['legs']}). "
                              f"Guardian attempting recovery; truthful-friendship likely down.")
                    _mark_alert(state, "all_stale", now)
            else:
                _ladder_clear(cur, legs, "_all", now, summary)
                for src in LEGS:
                    stale = (ages[src] is not None and ages[src] > STALE_MIN)
                    other_fresh = any(fresh[o] for o in LEGS if o != src)
                    if stale and other_fresh:
                        _ladder_step(cur, state, legs, src, src, now,
                                     f"{src} stale {ages[src]:.0f}min while other leg fresh", summary)
                    else:
                        _ladder_clear(cur, legs, src, now, summary)

            # (3) incident relay (Telegram only for escalations)
            try:
                _relay_incidents(cur, state, now)
            except Exception as e:
                log.warning(f"feed_guardian relay: {e}")

            # (4) incident relay done above; OI-degradation runs post-close via eod_oi_check().
            _save_state(cur, state)
            _oplog(cur, "guardian_tick", summary)
            conn.commit()
    except Exception as e:
        log.error(f"feed_guardian.guardian_tick: {e}")
        summary["error"] = str(e)[:200]
    return summary


# ── the 15-min all-days off-hours liveness tick ──────────────────────────────
def offhours_liveness_tick():
    """Every 15 min, ALL days: independent app-side check that the always-on worker is alive.
    A weekend worker death (Sat 25-Jul: dead 01:06-14:00, 13h unnoticed) becomes a <=45-min
    Telegram. Runs in the APP, so it survives the worker dying outright (the worker cannot
    self-report its own death — the would-not-self-report-safe check).

    Bootstrap safety: if NO heartbeat has ever been written (worker not yet running the cc#660
    hook), skip silently — never false-alarm before the feature is live. Also relays worker
    incidents off-hours (they can happen any hour the always-on worker runs)."""
    now = _ist_now()
    out = {"ts": now.isoformat(), "heartbeat_age_min": None, "alerted": False}
    try:
        with _conn() as conn, conn.cursor() as cur:
            _ensure_heartbeat_table(cur)
            state = _load_state(cur)
            _apply_resets(cur, state, now)   # cc#661: honour a manual reset flipped off-hours (e.g. pre-Monday)
            cur.execute("SELECT last_beat FROM worker_heartbeat WHERE id=1")
            r = cur.fetchone()
            last_beat = r[0] if r else None
            if last_beat is None:
                raw = _cfg_get(cur, _HB_KEY_LEGACY)   # fallback channel
                if raw:
                    try:
                        last_beat = datetime.fromisoformat(raw)
                    except Exception:
                        last_beat = None
            if last_beat is None:
                out["note"] = "no heartbeat yet — worker cc#660 hook not live; skipping (safe bootstrap)"
            else:
                # cc#844: age from the DB clock, never (IST now - UTC stored). See the contract above.
                age = _heartbeat_age_min(cur)
                if age is None:
                    out["note"] = "heartbeat row present but unreadable — skipping"
                    return out
                out["heartbeat_age_min"] = round(age, 1)
                if age > HEARTBEAT_STALE_MIN:
                    if not _throttled(state, "worker_silent", now):
                        _telegram(f"WORKER SILENT — feed worker heartbeat {age:.0f} min old "
                                  f"(>{HEARTBEAT_STALE_MIN}). truthful-friendship appears DOWN. "
                                  f"Last beat {(last_beat + timedelta(hours=5, minutes=30)).strftime('%d-%b %H:%M')} IST.")
                        _mark_alert(state, "worker_silent", now)
                        out["alerted"] = True
                    _oplog(cur, "worker_silent", {"age_min": round(age, 1),
                                                  "last_beat": str(last_beat)}, category="alert")
            # off-hours incident relay too
            try:
                _relay_incidents(cur, state, now)
            except Exception as e:
                log.warning(f"feed_guardian offhours relay: {e}")
            _save_state(cur, state)
            conn.commit()
    except Exception as e:
        log.error(f"feed_guardian.offhours_liveness_tick: {e}")
        out["error"] = str(e)[:200]
    return out


# ── EOD OI-degradation tripwire (cc#515 parity, once post-close) ─────────────
def eod_oi_check():
    """cc#515: universe-wide futures OI-staleness tripwire. Runs once post-close (16:15). >20%
    of active futures with OI stale beyond 2 sessions -> ONE oi_feed_degraded ops_log(alert)
    (feed-wide gap, not isolated symbols). ops_log only — no Telegram (informational; surfaced
    in the 16:15 daily feed log + master watchdog note)."""
    out = {"pct": None}
    try:
        import deriv_metrics
        with _conn() as conn, conn.cursor() as cur:
            res = deriv_metrics.check_oi_feed_degradation(conn)
            out.update(res)
            if res.get("pct", 0.0) > OI_DEGRADE_PCT:
                _oplog(cur, "oi_feed_degraded",
                       {"stale": res.get("stale"), "checked": res.get("checked"),
                        "pct": res.get("pct"), "cutoff_session": res.get("cutoff_session")},
                       category="alert")
                conn.commit()
        log.info(f"feed_guardian.eod_oi_check: {out}")
    except Exception as e:
        log.error(f"feed_guardian.eod_oi_check: {e}")
        out["error"] = str(e)[:160]
    return out


# ── summary for MASTER_WATCHDOG_NOTE + feed_daily_log ────────────────────────
def guardian_summary():
    """Compact feed-liveness snapshot for the master watchdog note (cc#658) + the 16:15 daily
    feed log (cc#495). Read-only. Never raises."""
    out = {"legs": {}, "worker_heartbeat_age_min": None, "oi_degrade_pct": None,
           "open_repairs": [], "last_cmd": None, "red_flags": []}
    try:
        with _conn() as conn, conn.cursor() as cur:
            now = _ist_now()
            ages = _leg_ages(cur, now)
            out["legs"] = {k: (round(v, 1) if v is not None else None) for k, v in ages.items()}
            for src, a in ages.items():
                if a is not None and a > STALE_MIN and now.time() >= dt_time(9, 25) \
                        and now.time() <= dt_time(15, 35) and now.weekday() < 5:
                    out["red_flags"].append(f"{src} stale {a:.0f}min")
            _ensure_heartbeat_table(cur)
            # cc#844: same contract — DB-clock age, not (IST now - UTC stored). This red_flag is what
            # drove the master watchdog permanently RED with a phantom "heartbeat 330min old".
            age = _heartbeat_age_min(cur)
            if age is not None:
                out["worker_heartbeat_age_min"] = round(age, 1)
                if age > HEARTBEAT_STALE_MIN:
                    out["red_flags"].append(f"worker heartbeat {age:.0f}min old")
            state = _load_state(cur)
            today = now.date().isoformat()
            for leg, st in (state.get("legs") or {}).items():
                if not isinstance(st, dict):
                    continue
                if st.get("failed_for_day") == today:
                    # cc#661: stood-down state must stay VISIBLE (just not noisy)
                    out["open_repairs"].append({"leg": leg, "attempts": st.get("attempts"),
                                                "stood_down": True})
                    out["red_flags"].append(f"{leg} REPAIR-EXHAUSTED (stood down)")
                elif st.get("attempts"):
                    out["open_repairs"].append({"leg": leg, "attempts": st.get("attempts")})
            raw = _cfg_get(cur, _CMD_KEY)
            if raw:
                try:
                    out["last_cmd"] = json.loads(raw)
                except Exception:
                    pass
            # latest OI tripwire reading today (if the tick recorded one)
            cur.execute("""SELECT details->>'pct' FROM ops_log
                           WHERE category='alert' AND title='oi_feed_degraded'
                             AND session_date=CURRENT_DATE ORDER BY session_ts DESC LIMIT 1""")
            r = cur.fetchone()
            if r and r[0] is not None:
                out["oi_degrade_pct"] = float(r[0])
                out["red_flags"].append(f"OI degraded {r[0]}%")
    except Exception as e:
        out["error"] = str(e)[:160]
    return out

# Broker Feed Integration — Learnings Manual
**Source: 10 documented Fyers feed incidents, 06-Jul to 14-Aug 2026. Written for whoever integrates the next broker.**

This is not a Fyers-specific war story. Every incident below is a general failure class that will recur with any broker's WebSocket/REST feed unless designed against explicitly. Read this before writing the next `worker/<broker>_feed.py`.

---

## The incidents, in order

| # | Date | Symptom | Root cause | Class |
|---|---|---|---|---|
| 1 | 06-Jul | Feed dead 09:15–10:05; then CC's own deploys prolonged it | Pre-market restart hit an empty-`cmp_prices` trap → subscribed to nothing; then 4 market-hours deploys each re-triggered the same trap | **Boot-order / deploy-timing** |
| 2 | 13→15-Jul | 3 consecutive cold-boot failures | Auth circuit-breaker (`SystemExit` on 90s cooldown) wasn't caught by the boot path's `except Exception` → uncaught exit → Railway fast-restart → landed back inside the same cooldown → crash-loop | **Exception-type mismatch (livelock)** |
| 3 | 14-Jul | Futures+options died at 14:00, equity kept flowing, watchdog never fired | Health check pooled `eq+fut` into one count; equity alone cleared the threshold, masking the derivatives-only death | **Aggregated health metric hides partial failure** |
| 4 | 15-Jul | Incident 2's "fix" recurred identically | The fix was root-caused correctly but **never actually committed** — the commit SHA didn't exist in any branch. Task was marked done anyway. | **Unverified deploy claim** |
| 5 | 16-Jul | Zero bars 09:15–09:43, process alive throughout | A fix passed the worker's global DB connection into the auth path, which closed it at end-of-boot; every subsequent write silently failed with no crash, no traceback | **Resource-ownership bug (silent, not crash-shaped)** |
| 6 | 17-Jul | Daily zombie boot, watchdog silent again | Pre-market WS subscriptions don't survive to open (broker-side drop, no TCP close) — SDK looks connected, never reconnects. Separately: the watchdog's own DB connection had died, so it silently skipped every check all morning. | **Broker-side subscription expiry + single-point-of-failure control path** |
| 7 | 20-Jul (AM) | Dead 09:15–10:15 | Incident 6's timing fix (09:14 forced reconnect) shipped — but reconnects on an **expired token** without re-verifying it first. Auto-relogin itself failed silently (dead DB conn, same shape as #5). | **Fixed timing, not credential validity** |
| 8 | 20-Jul (mid-day) | Froze again 10:55–12:29, 94 min | A config default (`limit=0` meaning "no limit" instead of "use the 20-pilot default") caused a reconnect to request 3,574 symbols instead of ~650 → broker force-closed the whole batch | **Unvalidated config value crossing a hard platform limit** |
| 9 | 05-Aug 15:28 → 06-Aug 13:47, **22 h** | Watchdog rung 1 forced a reconnect two minutes before close; the process then went permanently silent — no reconnect, no crash, no restart, no log line for 22 hours. Zero bars for the entire 06-Aug session. | The recovery action completed (its ops_log row exists at 15:28:02), then the housekeeping loop blocked forever on a DB call. `get_db()` is `psycopg2.connect(DATABASE_URL)` with **no `connect_timeout`, no TCP keepalives, no `statement_timeout`** — so a silently-dropped socket makes the next `execute()` wait for a reply that never comes. **No exception is raised**, so `_mark_db_error` never fired, `consecutive_db_failures` never incremented, its `os._exit(1)` was unreachable, and `_housekeeping_supervised`'s `except Exception` never caught anything. Rung 2 was additionally unreachable: rung 1 fired at 15:28 and the next health check is `HEALTH_LOG_MINS`=5 later, past the close. | **Recovery action as the fatal step; hang not crash; supervisor shares the fate** |
| 10 | 14-Aug 10:02 → close | 909 of 2,119 extended-equity symbols froze mid-session; core equity (212) and futures (208) flowed normally all day; nothing detected it — found by a human mid-afternoon | The extended leg (`fyers_ext`, staged in under cc#809) was **excluded from every health surface**: `_verify_subscribe_survivors` probed only the legacy legs, and the watchdog floor-checked only `eq`+`fut`. The trigger was a **partial subscribe-dead socket**: a batch re-subscribe at 10:02 was ACKNOWLEDGED by the broker but honoured only for the legacy legs — the 909 ext symbols never resumed ticking, with no error on either side. Recovery: post-close backfill restored 909/909 via the history API; the code fix (cc#1017, sha ab1ea67) adds ext to the probe + watchdog, a bounded rebuild-then-retreat ladder, and keeps ext failure out of the core kill path. | **Staged leg invisible to health checks + partial subscribe-dead (ACK ≠ honoured)** |

---

## The five failure classes, generalized

### 1. Boot-order traps
A restart that happens *before* the market opens, or before a dependency table is populated, can silently subscribe to nothing and look identical to a healthy idle process. **Design rule:** never let "the process started" imply "the subscription succeeded." Verify the subscription count against the expected universe size immediately after every connect, and alarm on a mismatch — not just on "process not running."

### 2. Exception-type mismatches create livelocks, not crashes
`except Exception` does not catch `SystemExit`, `KeyboardInterrupt`, or other `BaseException` subclasses. A circuit breaker that raises `SystemExit` to signal "back off" will propagate straight through a handler written for `Exception`, killing the process. If the platform auto-restarts fast enough, the new process can land back inside the same cooldown window the old one triggered — an infinite crash-loop that looks like random multi-minute silences from the outside, because the actual outage length is a function of restart-timing luck, not a fixed bug duration. **Design rule:** any deliberate "wait and skip" signal must be a typed, caught return value or a custom exception subclass of `Exception` — never a bare `SystemExit` on a path anything else might wrap.

### 3. Aggregated health metrics hide partial failure
If your feed has multiple legs (equity / futures / options), a single pooled "symbols reporting" count can stay above threshold even when one entire leg is dead, as long as another leg is healthy. **Design rule:** health-check every leg independently. A watchdog that can't see a partial failure will never trigger recovery for it, no matter how good the recovery logic is.

### 4. Never trust "done" — verify the artifact, not the claim
A task can be root-caused perfectly and still not fix anything, because the described commit was never actually pushed, or was pushed to a branch nobody deploys. This happened twice in this incident history (#4 directly, #7 nearly). **Design rule:** after any status flips to "done," independently confirm the commit SHA exists in the deployed branch and that the specific code path changed. Re-verify against live data after deploy (did the symptom actually stop?), not against the task's self-report.

### 5. Config values need validated ranges, not just types
`limit=0` meaning "no limit" is a reasonable convention in isolation, but it silently produces a 10x-larger request than intended the moment someone (or some default) sets it to 0 instead of leaving it unset. **Design rule:** any config value that scales a subscription/request size should have an enforced ceiling checked *before* the request is built, independent of what the value nominally means — log and clamp, don't just trust the semantics.

---

## Cross-cutting patterns worth internalizing

**A fix for "when" is not a fix for "whether."** Incident 7 is the clearest example: the 09:14 forced-reconnect timing fix (from incident 6) was correct and necessary, but it silently assumed the token being reconnected with was still valid. Timing fixes and validity fixes are separate concerns — shipping one does not retire the need for the other.

**Silent failure is worse than a crash.** Incidents 3, 5, and 6's watchdog-blindness component are all variations of the same thing: a component keeps running, looks alive, and simply stops doing its job with no error, no log line, no alert. A crash gets you a Railway restart for free. A silent no-op gets you nothing until someone manually inspects the database. **Prefer loud failure over quiet degradation** — a component that can't do its job should exit or alarm, not idle.

**Resilience added to data paths doesn't cover control paths.** Incident 6's second root cause: DB-connection resilience had been added to the bar-writing paths but not to the watchdog's own connection — so the watchdog silently died while the thing it was supposed to watch kept failing. Any time you harden one path against a failure mode, ask whether the *supervisor* of that path has the same vulnerability.

**Root-cause the failure-start time from data, not from the alarm time.** Incident 5's false lead: the alarm fired at a time that pointed at the wrong code path, because grace windows and detection delays mean the alarm timestamp is not the failure timestamp. Always find the first missing/wrong data point directly, then work backward.

**A hang is not a crash, and only one of them gets you a free restart.** (Incident 9.) Every guard in the worker was written to catch an *exception*: `_mark_db_error` flags psycopg error classes, `consecutive_db_failures >= 3` exits, `_housekeeping_supervised` wraps the loop in `except Exception`. None of them can see a call that simply never returns. A blocking socket read with no timeout produces no exception, no traceback and no log line — it produces silence, and silence is the one thing a supervisor built on `except` cannot detect. **Every blocking call needs a deadline, not just an exception handler.** For DB connections that means `connect_timeout` + TCP keepalives + `statement_timeout` at the point of connect; for a recovery sequence it means a timer that converts "did not finish in N seconds" into a nonzero exit.

**The recovery action can be the fatal step.** (Incident 9, and incident 1 in a different shape.) The watchdog detected correctly and acted correctly, and the act of recovering is what killed the process. When auditing a recovery path, do not only ask "will this fix the problem" — ask "what happens if this step itself hangs or throws." A remedy with no deadline is a new single point of failure, positioned exactly where the system is already unhealthy.

**An escalation ladder needs enough clock left to climb.** (Incident 9.) Rung 1 fired at 15:28 and rung 2 could only evaluate 5 minutes later — after the session ended. The ladder was correct in structure and unreachable in practice. Any staged escalation gated on market hours must either fit inside the remaining window or carry its final rung unconditionally.

**A staged leg joins the health check the day it is staged, or it is dark by construction.** (Incident 10.) The extended-equity rollout added a third leg to the feed while both the post-reconnect probe and the watchdog kept enumerating the original two. A health check that names its legs in a hardcoded list goes blind to every leg added after it was written — and a staged rollout is precisely when a new leg is most likely to fail. **Design rule:** leg enumeration in health checks must be derived from the same registry that drives subscription, never a parallel hand-maintained list. (The registry-derived doctrine of ENGINE_LIVENESS_RULE, applied to feeds.)

**An ACKed subscribe is not an honoured subscribe.** (Incident 10.) The broker acknowledged the 10:02 batch re-subscribe and then delivered ticks for only two of three legs — no error, no close, no retransmit. A partial subscribe-dead socket does not recover in place: re-subscribing on the same connection re-ACKs and re-fails identically. **Design rule:** verify per-leg tick flow after every (re)subscribe, and when one leg is dead while others flow, REBUILD the connection (fresh connect + full re-subscribe) rather than re-subscribing in place — bounded attempts on ONE counter shared across every path that can trigger the rebuild, then stage-retreat and alarm CRITICAL.

**A new leg's failure must not detonate the core.** (Incident 10's fix, by design.) The ext-leg recovery deliberately never enters the core `os._exit` ladder: killing the worker to save a staged 909-symbol extension would drop the 208 futures and the main equity leg that fund the actual book. **Design rule:** blast-radius isolation — each leg's escalation ladder ends at its own stage-retreat, and only the core legs may terminate the process.

**One timezone basis per table, stated in writing.** (03-Aug, fixed for good 15-Aug.) A heartbeat row that stored UTC in one column and IST in its neighbour produced a phantom "330 minutes stale" that fired a false WORKER SILENT, drove a watchdog permanently red, and once withheld a founder shortlist during live trading — all from a subtraction error. Any naive timestamp is a claim about a basis; if the basis is not written next to the column, every future reader will guess, and half will guess wrong. **Design rule:** every naive-timestamp column carries a stated basis (IST, per RAILWAY_MEMORY_RULES), writers and readers change together — writer first — and ages are computed by the database against its own clock converted to that basis, never by subtracting in application code.

**A single mechanism, correctly connected, usually beats a new mechanism.** Repeatedly in this history (incidents 6 and its diagnosis in particular), the fix was not "build a new safety system" — it was "the safe wrapper already exists, but two call sites bypass it and call the raw unsafe function instead." Before adding a new watchdog or breaker, check whether an existing one just isn't wired to the path that needs it.

---

## A pre-integration checklist for the next broker

Before going live with a new broker's feed, verify each of these explicitly — every one maps to an incident above:

- [ ] **Subscription verification**: after every connect/reconnect, assert the subscribed count matches the expected universe size. Never assume "connected" implies "subscribed to everything."
- [ ] **Exception typing**: audit every `except Exception` on the boot/reconnect path — confirm no intentional control-flow signal (breaker skip, rate-limit backoff) is raised as `SystemExit` or another `BaseException` that will slip through.
- [ ] **Per-leg health checks**: if the feed has multiple instrument classes (equity/futures/options/etc.), health-check each independently. Never pool counts across legs for a go/no-go decision.
- [ ] **Token lifecycle**: know exactly when the broker's tokens expire (daily? on demand? on IP change?) and verify token validity *before* every subscribe call, not just at boot. A reconnect on an old token is not a reconnect.
- [ ] **Deploy verification**: after any fix ships, confirm the commit SHA is live in the deployed branch/environment, and confirm the original symptom actually stopped in live data — not from a task status flag.
- [ ] **Config ceiling checks**: any config value that scales a request (symbol count, strike count, poll frequency) needs a hard ceiling check at the point of use, independent of the value's nominal semantics (watch for `0`/`-1`/`null` "meaning" unlimited).
- [ ] **Control-path resilience**: whatever hardening you apply to data-write paths (fresh connections, retry-once-then-exit), apply the identical hardening to the supervisor/watchdog path that's meant to catch failures in the data path.
- [ ] **Loud-failure default**: any component that silently can't do its job (dead connection, empty response, missing dependency) should exit or alarm by default, not skip-and-continue indefinitely. Escalate after N consecutive silent skips.
- [ ] **Completion deadline on every forced-reconnect path** *(added after incident 9)*: any recovery sequence — close, reconnect, re-subscribe — needs its own hard deadline that converts a hang into a nonzero exit. Not a try/except: a timer. A crash gets a free restart; a hang gets 22 hours of silence. Ask of every recovery step, "what if this never returns?"
- [ ] **No untimed blocking calls anywhere in a supervisor loop** *(added after incident 9)*: every DB connection used by a watchdog needs `connect_timeout`, TCP keepalives and a `statement_timeout` set at connect time. An `except` block cannot catch a call that never returns, so exception-based health machinery is blind to it by construction.
- [ ] **Out-of-process dead-man alarm** *(added after incident 9)*: at least one alarm must live outside the supervised process and depend on nothing it owns — "market open and newest bar older than N minutes → page a human." Every in-process alarm on 06-Aug was downstream of something that was also dead.
- [ ] **Null means dead, not unknown** *(added after incident 9)*: during market hours, "no data at all" is the most severe state, not an absence of evidence. Audit every health check for a `value is not None` guard that quietly excludes the total-failure case from action.
- [ ] **Registry-derived leg enumeration in every health surface** *(added after incident 10)*: the probe and the watchdog list their legs from the same source that drives subscription. Adding a leg to the feed without it appearing in health checks in the same deploy is a defect, not a follow-up.
- [ ] **Per-leg tick verification after every (re)subscribe; rebuild, not re-subscribe, on partial death** *(added after incident 10)*: an ACK is not delivery. If one leg is dead while others flow, tear the connection down and rebuild — bounded attempts on one shared counter, then stage-retreat + CRITICAL alarm. Re-subscribing in place on the same socket re-fails silently.
- [ ] **Blast-radius isolation per leg** *(added after incident 10)*: only core legs may reach the process-kill rung. A staged or secondary leg's ladder ends at its own retreat; it must never take the healthy legs down with it.
- [ ] **Pre-market subscription assumption**: test explicitly whether the broker honors subscriptions made before market open, or drops them silently server-side. If dropped, the connect/subscribe sequence needs to happen *after* the open, not before it — or needs an explicit re-verify-and-resubscribe step timed to the open.

---

*Compiled 20-Jul-2026 from session_log ids 1525, 1536, 4017, 4282, 4333, 4792, 6419. Incident 9 added 06-Aug. Incident 10 + the staged-leg, partial-subscribe, blast-radius and timezone-basis patterns added 15-Aug-2026 (cc#1017 sha ab1ea67; cc#1022). Update this file when the next incident teaches something new — the pattern only holds if it's kept current.*

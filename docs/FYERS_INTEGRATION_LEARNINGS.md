# Broker Feed Integration — Learnings Manual
**Source: 8 documented Fyers feed incidents, 06-Jul to 20-Jul 2026. Written for whoever integrates the next broker.**

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
- [ ] **Pre-market subscription assumption**: test explicitly whether the broker honors subscriptions made before market open, or drops them silently server-side. If dropped, the connect/subscribe sequence needs to happen *after* the open, not before it — or needs an explicit re-verify-and-resubscribe step timed to the open.

---

*Compiled 20-Jul-2026 from session_log ids 1525, 1536, 4017, 4282, 4333, 4792, 6419. Update this file when the next incident teaches something new — the pattern only holds if it's kept current.*

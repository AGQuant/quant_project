"""V8 TIMING RULES V1 — session_log 27321, founder-ruled 20-Aug-2026 ~09:05 IST.

Three timing/exit rules, additive. They govern WHEN an entry may fire and HOW an exit may
release — never WHAT qualifies. No signal spec is touched by this file: V4-N5I, V7-B and the buy
specs decide qualification exactly as they did before, and nothing here reads a filter, a
threshold or a price.

Every function is PURE: same inputs, same answer, no DB, no clock unless one is handed in, no
state. That is deliberate. These three decisions sit on the live trading path, and a pure function
is one you can assert against in a test or a REPL without standing up an engine.

  rule 1  entries_allowed(now_ist)         no new entries before 09:30 IST
  rule 2  short_gate_exit_blocked(side)    GATE_EXIT is disabled for SHORT, unconditionally
  rule 3  sell_slot_bonus(fail_count)      +1 sell slot when the mood check fails 2, 3 or 4

Evidence base (27321): Fable simulation on the closed book 10-19 Aug, 38 trades. Baseline
+20,560 against ~+129,870 combined. The per-rule evidence is quoted in each docstring below so a
reader here never has to go and find the ruling to know why the rule exists.
"""

from datetime import time as _dt_time

__all__ = ["ENTRY_CUTOFF_IST", "entries_allowed", "short_gate_exit_blocked", "sell_slot_bonus"]

# 09:30 IST. The market opens 09:15 and the writer's first tick is 09:20, so this blocks exactly
# the 09:20 and 09:25 entry ticks and lets 09:30 through.
ENTRY_CUTOFF_IST = _dt_time(9, 30, 0)


def entries_allowed(now_ist) -> bool:
    """Rule 1 — NO NEW ENTRIES BEFORE 09:30 IST (session_log 27321).

    CONSULTED ON ENTRY PATHS ONLY. Exits, stop monitoring, target checks and gap handling run
    every tick from 09:20 exactly as before — a position already open must be watched from the
    first tick of the day, and gating that would be a new risk, not a new rule. If you are reading
    this from an exit path, that is a bug: this function has no business there.

    Evidence: 12 entries taken between 09:15 and 09:29 in the 10-19 Aug sample netted -84,715.

    `now_ist` is a datetime or a time, ALREADY IN IST. It is passed in rather than read here so
    the rule can be tested at any moment of the day without touching a clock, and so the caller's
    existing IST handling stays the single source of what "now" means. A caller that hands in a
    naive UTC datetime gets a wrong answer, which is why every call site passes the same
    ist_now() the writer already uses.
    """
    t = now_ist.time() if hasattr(now_ist, "time") else now_ist
    return t >= ENTRY_CUTOFF_IST


def short_gate_exit_blocked(side) -> bool:
    """Rule 2 — GATE_EXIT is DISABLED for SHORT positions, COMPLETELY (session_log 27321).

    Unconditional, with no regime dependency: the founder widened this from the regime-conditional
    version in the proposal. A short releases ONLY via TARGET, SL, the GAP variants, or its own
    basket-spec exits. LONG positions keep gate exits exactly as today — that path must stay
    byte-identical.

    Evidence: all 5 gate-exited shorts in the sample did better held. 3 reached target, 0 hit SL.

    Side matching is deliberately loose about case and whitespace because the string arrives from
    several places (basket registry, position row, signal payload) and a rule that silently fails
    to match "Short" would quietly re-enable the exact behaviour it exists to stop. Anything that
    is not recognisably a short returns False, so an unknown side keeps today's behaviour rather
    than inheriting a block nobody asked for.
    """
    return str(side or "").strip().upper() in ("SHORT", "SELL", "S")


def sell_slot_bonus(mood_fail_count) -> int:
    """Rule 3 — SELL SLOTS +1 when the daily mood check fails 2, 3 or 4 of 4 (session_log 27321).

    Fail 0 or 1 -> base slots. Fail 2, 3 or 4 -> base + 1. BUY SLOTS ARE UNCHANGED; this function
    is consulted on the sell side only.

    Bounded on purpose: only 2, 3 and 4 earn the bonus. A None or an out-of-range count returns 0,
    so a missing mood computation can never invent a slot — the failure mode is the base book, not
    a wider one.
    """
    try:
        n = int(mood_fail_count)
    except (TypeError, ValueError):
        return 0
    return 1 if n in (2, 3, 4) else 0

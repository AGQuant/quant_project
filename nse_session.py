"""
nse_session.py — the SINGLE source of truth for the NSE session window (IST).

cc#748: the TC outcome sim (and anything else needing session-relative times) derives
its entry cadence and force-close time from the two constants below, so NOTHING hardcodes
a session time independently. When the NSE session window changes (e.g. the 03-Aug-2026
timing change), edit MARKET_OPEN / MARKET_CLOSE HERE — once — and every derived time follows.

Context isolation id=244: this module carries session *timing* only. No V8 / TC scoring logic.
"""

# (hour, minute) IST. THE single place to edit on any NSE session-timing change.
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


def _hm(t):
    """(h, m) -> minutes since midnight."""
    return t[0] * 60 + t[1]


def entry_tick_times():
    """cc#748 TC-sim entry cadence: hourly ':30' marks from the open through the close, plus the
    close tick itself. Derived from MARKET_OPEN/MARKET_CLOSE so it follows any session-window change
    automatically. With the current 09:15-15:30 window this yields 09:30, 10:30, 11:30, 12:30, 13:30,
    14:30, 15:30 (the 15:30 tick == close, where 5-day TIME force-closes also fire)."""
    o, c = _hm(MARKET_OPEN), _hm(MARKET_CLOSE)
    ticks = []
    for hh in range(MARKET_OPEN[0], MARKET_CLOSE[0] + 1):
        cand = hh * 60 + 30                      # ':30' of each session hour
        if o <= cand <= c:
            ticks.append((hh, 30))
    close = (MARKET_CLOSE[0], MARKET_CLOSE[1])    # always include the close tick
    if close not in ticks:
        ticks.append(close)
    return sorted(set(ticks))


def is_entry_tick(h, m):
    """True if (h, m) IST is one of the sim's hourly entry/exit evaluation marks."""
    return (h, m) in entry_tick_times()


def force_close_time():
    """The close tick at which a position that has reached its 5-trading-day cap is TIME-closed."""
    return MARKET_CLOSE

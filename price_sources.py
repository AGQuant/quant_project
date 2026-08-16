"""
price_sources.py — cc#1053 INDEX_SYMBOL_CONVENTION_V1, the source registry
==========================================================================
ONE symbol in `intraday_prices` can carry MORE THAN ONE INSTRUMENT. That is not an
accident and it is not a bug — the table's unique key is

    uq_intraday_sym_ts_tf_src  UNIQUE (symbol, ts, timeframe, source)

so `source` is part of the row's identity. Verified 14-Aug-2026: RELIANCE 72 fyers_eq
bars + 74 fyers_fut bars, HDFCBANK 72 + 74, TCS 72 + 74, BANKNIFTY 72 + 74. All 209
stock futures already store the cash leg and the futures leg under one name.

THE RULE: a reader that wants the CASH price MUST filter the source. A reader that
does `WHERE symbol = 'X' ORDER BY ts DESC LIMIT 1` gets whichever leg wrote last, and
on 14-Aug that was the FUTURES leg for 208 of 209 symbols.

WHY THIS FILE EXISTS — the sets were being retyped at each call site and drifting.
Seven sites carried `source <> 'fyers_fut'`, which silently lets `fyers_fut_rest`
through (cc#770 added that source after cc#367 wrote the filter). One list, imported,
cannot drift.

THE NIFTY EXCEPTION (deliberate, documented, NOT fixed here — see the P3 card):
Nifty is the one name that does NOT follow the rule. Its cash leg is stored under
`NIFTY50` and its futures leg under `NIFTY` — two different symbols. Bank Nifty
stores both legs under `BANKNIFTY`, like every stock. The split happened because
cc#489 step_6 renamed the Nifty cash row to NIFTY50 when `NIFTY` was already the
futures root in `futures_universe`; Bank Nifty never needed a second name. Unifying
Nifty needs a V10 regression pass and a worker deploy, so it stays as-is and is
written down instead of being half-changed.

Usage — bind the tuple, never inline the strings:

    from price_sources import FUT_SOURCES
    cur.execute("SELECT close FROM intraday_prices "
                "WHERE symbol=%s AND COALESCE(source,'') <> ALL(%s) "
                "ORDER BY ts DESC LIMIT 1", (sym, list(FUT_SOURCES)))
"""

# The DERIVATIVES leg. Anything tagged with one of these is a FUTURES print and is a
# different instrument from the cash symbol it shares a name with. Census 16-Aug-2026
# over the whole table: these are the only two futures sources that have ever written.
FUT_SOURCES = ("fyers_fut", "fyers_fut_rest")

# The CASH leg. `auction` / `fyers_eq_auction` are the 15:15-15:35 auction prints —
# still the cash instrument, so they belong here (cc#855 excludes them separately when
# a caller needs the CONTINUOUS basis; that is a different question from spot-vs-fut).
# `yahoo` and `fyers` currently hold zero rows but their writers still exist
# (yahoo_index_backfill.py, the legacy main.py loader), so they stay listed.
SPOT_SOURCES = ("fyers_eq", "fyers_ext", "fyers_hist",
                "fyers_eq_auction", "auction", "yahoo", "fyers")

# The INDEX cash feed specifically — worker/fyers_feed.INDEX_LTP_SYMBOLS rides the 30s
# quotes poll and writes only these. feed_health uses it to grade the index feed: a
# futures bar must never stand in for a dead index quote poll.
INDEX_SPOT_SOURCES = ("fyers_eq", "fyers_eq_auction", "auction")


def not_fut():
    """The bind value for `COALESCE(source,'') <> ALL(%s)`. A list, because psycopg
    adapts a Python list to a Postgres array and a tuple to a row constructor."""
    return list(FUT_SOURCES)


def spot_only():
    """The bind value for `source = ANY(%s)` when a caller wants an explicit allow-list
    rather than a futures exclusion. Prefer not_fut() — an exclusion stays correct when
    a new cash source is added, an allow-list silently drops it."""
    return list(SPOT_SOURCES)

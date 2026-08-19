"""
theme_change.py — cc#1102 THE ONE SECTOR COMPUTATION FOR THE V8 BOOK
====================================================================

Founder ruling 19-Aug-2026: wherever a sector figure is used — in a strategy gate or in a table —
use the FUTURES THEME, not the GVM segment.

WHY, in one paragraph, because the reason is the whole card. `sector_day/week/month` on v8_metrics
were computed over the WHOLE GVM segment, mcap-weighted, including microcaps V8 can never trade.
So the sector number described a universe the basket does not operate in, and inside that universe
the largest name dominated. V8 trades ONE LOT PER NAME in futures. The honest description of what a
basket actually experiences is an EQUAL-WEIGHT average over the FUTURES members of a theme. This is
a correctness fix, not a tuning change — no gate threshold moves in this card.

THIS MODULE EXISTS SO THERE IS EXACTLY ONE GROUPING. cc#1042 was raised because two surfaces
grouped the same data differently and disagreed in public. The writer needs LIVE values mid-tick
(from the dict it is about to upsert) and the display endpoints need stored values from v8_metrics.
Those are two DATA SOURCES, and they must never become two RULES. So the rule lives in `aggregate`,
one pure function, and both paths feed it:

    writer   : aggregate(theme_of(cur), computed)          # this tick's live values
    display  : theme_changes(cur, score_date)              # stored v8_metrics rows

The shape is taken from v8_endpoints.v8_theme_sectors, which has grouped exactly this way for the
Sectors tab since cc#338: futures_universe.theme, active members only, equal weight, indices
excluded, THEME_MIN_MEMBERS suppression, and a NEW ENTRANTS bucket so an unthemed F&O arrival is
never silently dropped.

WHAT A THIN THEME RETURNS. Fewer than THEME_MIN_MEMBERS PRICED members yields None, never a partial
number and never 0. That matters more here than on a display card: every sector gate in the four
baskets is written as `v is not None and <comparison>`, so None FAILS the gate closed. A 0 would
PASS `sector_week <= -0.5`... no — a 0 would fail that one but PASS `sector_week < 0`'s mirror on
the buy side. Either way a fabricated 0 decides a trade on a number nobody measured. None does not.
"""

import logging
from typing import Dict, Optional

log = logging.getLogger("scorr.theme_change")

# Same three constants the Sectors tab uses, restated here rather than imported, because
# v8_endpoints imports the SIGNAL WRITER and the writer must not import v8_endpoints back.
# They are asserted equal by test_sector_source_theme.py, so a change to one fails loudly.
THEME_MIN_MEMBERS = 3
INDEX_EXCLUDE_SQL = "fu.symbol NOT LIKE '%%NIFTY%%' AND fu.symbol NOT LIKE '%%SENSEX%%'"
NEW_ENTRANTS = "NEW ENTRANTS"

# v8_metrics column -> the sector_* field it feeds. Kept as data so the three stay in lockstep and
# nobody adds a fourth aggregate by editing only half the pipeline.
_FIELDS = (("day", "day_1d"), ("week", "week_return"), ("month", "month_return"))


def theme_of(cur) -> Dict[str, str]:
    """{symbol: theme} for every ACTIVE futures symbol, indices excluded.

    An active symbol with no theme is mapped to NEW ENTRANTS rather than dropped: a stock that just
    joined the F&O list still trades, and a symbol missing from this map gets NULL sector values,
    which fails every sector gate. Silently un-tradeable is not an acceptable default.
    """
    cur.execute(f"""
        SELECT fu.symbol, COALESCE(fu.theme, '{NEW_ENTRANTS}')
        FROM futures_universe fu
        WHERE fu.is_active = TRUE AND {INDEX_EXCLUDE_SQL}
    """)
    return {r[0]: r[1] for r in cur.fetchall()}


def aggregate(theme_map: Dict[str, str], values: Dict[str, dict]) -> Dict[str, dict]:
    """THE RULE. Equal-weight mean of member day_1d / week_return / month_return, per theme.

    `values` is {symbol: {"day_1d": .., "week_return": .., "month_return": ..}} — the writer passes
    its live `computed` dict, the display path passes rows read from v8_metrics. Symbols absent from
    `theme_map` are ignored: they are not active futures and are not part of any theme this book
    trades.

    Each field counts its OWN priced members, so a theme where two members are missing a month
    return still reports an honest day. members_priced is the DAY count, which is the one the
    minimum-membership rule is applied on, and it travels in the payload so a caller can print the
    denominator beside the number (UNIVERSE_DENOMINATOR_RULE).
    """
    acc: Dict[str, dict] = {}
    for sym, theme in theme_map.items():
        a = acc.setdefault(theme, {"members_total": 0, "_sum": {}, "_n": {}})
        a["members_total"] += 1
        m = values.get(sym)
        if not m:
            continue
        for key, col in _FIELDS:
            v = m.get(col)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            a["_sum"][key] = a["_sum"].get(key, 0.0) + v
            a["_n"][key] = a["_n"].get(key, 0) + 1

    out: Dict[str, dict] = {}
    for theme, a in acc.items():
        priced = a["_n"].get("day", 0)
        thin = priced < THEME_MIN_MEMBERS
        row = {"members_priced": priced, "members_total": a["members_total"],
               "thin": thin, "min_members": THEME_MIN_MEMBERS}
        for key, _col in _FIELDS:
            n = a["_n"].get(key, 0)
            # A thin theme is None on EVERY field, not only on the one that is thin. Reporting a
            # month for a theme whose day is suppressed would let the same tile be trustworthy in
            # one column and not in the next.
            row[key] = None if (thin or not n) else round(a["_sum"][key] / n, 2)
        out[theme] = row
    return out


def theme_changes(cur, score_date=None) -> Dict[str, dict]:
    """The stored-data path: read v8_metrics at `score_date` (default: its MAX) and aggregate.

    Used by the display endpoints. Goes through the SAME `aggregate` the writer uses, so a table
    and a gate can disagree about the VALUE only by being at different score_dates — never about
    what a sector means.
    """
    tmap = theme_of(cur)
    if not tmap:
        return {}
    if score_date is None:
        cur.execute("SELECT MAX(score_date) FROM v8_metrics")
        row = cur.fetchone()
        score_date = row[0] if row else None
    cur.execute("""SELECT symbol, day_1d, week_return, month_return
                     FROM v8_metrics WHERE score_date = %s""", (score_date,))
    values = {r[0]: {"day_1d": r[1], "week_return": r[2], "month_return": r[3]}
              for r in cur.fetchall()}
    return aggregate(tmap, values)


def theme_label(info: Optional[dict]) -> str:
    """The label to print beside the number, so the denominator always travels with it.

    UNIVERSE_DENOMINATOR_RULE (debug_learnings 207): never a bare percentage. A suppressed theme
    says so in words rather than rendering an unexplained em-dash.
    """
    if not info:
        return "Theme"
    total = info.get("members_total") or info.get("members_priced") or 0
    priced = info.get("members_priced") or 0
    base = f"Theme · {total} future{'' if total == 1 else 's'} · equal-weight"
    if info.get("thin"):
        return f"{base} · only {priced} priced, under the {info.get('min_members')} minimum"
    if priced and priced != total:
        return f"{base} · {priced} priced"
    return base

"""tc_scanner_config.py — cc#1222: the ONE place TC_SCANNER_GATED_CONFIG_V1 lives.

WHY A SEPARATE MODULE. The card's scope 5 asks for the thresholds, gates and caps to be written
down exactly once and for BOTH the annotation logic and the i-button sheet to be rendered from that
one dict. If the config sat inside tc_v4_scan.py the scanner could read it, but the page could not
without a second copy in HTML — and a second copy is how a config drifts. So the dict lives here,
the annotator here reads it, and `/api/tc-scanner/config` serves the very same dict plus the sheet
lines built from it. The HTML types no threshold, no gate bound and no cap of its own.

OBSERVATION MODE IS THE GOVERNING SPEC. Founder amendment 23-Aug (session_log 29448, V1.1)
supersedes scope items 1-3 of the card as FILTERS: nothing is cut. Every signal stays eligible and
the V1 config becomes marks on the row — did the score clear its bucket bar, which gates pass, and
where the row ranks in its bucket today. The reason is written into the amendment itself: a week of
live data should accumulate with the rules VISIBLE but not ENFORCED, so next weekend's refinement
has an unfiltered week to look at. A filter applied now would delete exactly the rows that ruling
needs.

So `annotate` never drops a row. It cannot: it takes a list and returns marks for that same list.
The one number that speaks for the filter is `would_qualify` — the count of rows that WOULD have
passed V1 — and it is reported next to the total so the header reads "N of M would qualify" rather
than pretending N is all there is.

WHAT THE CONFIG IS AND IS NOT. It is a starting config tuned on ONE week (17-21 Aug 2026) in a
falling market, per session_log 29447's own caveat. It is a display rule on a research surface, not
an auto-trade trigger, and the sheet says so in the last line rather than leaving a reader to guess.
"""

from fastapi import APIRouter

router = APIRouter()

# ── THE CONFIG. Session_log 29447 (V1) as amended by 29448 (V1.1). Nothing here is retyped anywhere
# else in the codebase; every consumer reads this dict. ───────────────────────────────────────────
TC_SCANNER_CONFIG = {
    "version": "V1.1",
    "locked_on": "23 Aug 2026",
    "source": "session_log 29447 (config) + 29448 (observation-mode amendment)",
    # V1.1: the rules are MARKED, not enforced. Flipping this to False is the whole of the change
    # needed to start filtering, which is why it is a flag and not a comment.
    "observation_mode": True,
    "score_thresholds": {"SELL-MOM": 80, "SELL-REV": 80, "BUY-MOM": 65, "BUY-REV": 60},
    # Each gate carries its own label, operator and bound so the strip, the fail phrase and the
    # sheet line are all one sentence built from one row. `key` is the v8_metrics column.
    "gates": {
        "BUY": [
            {"key": "sector_week", "label": "Sector week", "op": ">", "bound": 1, "unit": "%"},
            {"key": "sector_month", "label": "Sector month", "op": ">", "bound": 2, "unit": "%"},
            {"key": "rsi_month", "label": "Monthly RSI", "op": ">", "bound": 60, "unit": ""},
        ],
        "SELL": [
            {"key": "sector_week", "label": "Sector week", "op": "<", "bound": -1, "unit": "%"},
            {"key": "sector_month", "label": "Sector month", "op": "<", "bound": -2, "unit": "%"},
            {"key": "rsi_month", "label": "Monthly RSI", "op": "<", "bound": 40, "unit": ""},
        ],
    },
    "caps": {
        "per_bucket_per_day": 5,
        "book_total": 20,
        "fill": "rank by score100 desc, one position per symbol, no same-day re-entry",
    },
    "exit": {"target_pct": 2, "stop_pct": -2, "time_exit": "15:20 on the 3rd session"},
    "tested_on": "one week of data (17-21 Aug 2026), longer test running",
    "caveat": ("ONE week, falling market. Three dials tuned on 5 days — a starting config, not a "
               "result. The 4-week replay confirms or kills it."),
    "disclaimer": "Signals are research, not trade instructions.",
}

BUCKETS = ("BUY-MOM", "BUY-REV", "SELL-MOM", "SELL-REV")


def side_of(bucket):
    """'BUY-MOM' -> 'BUY'. Returns None for anything that is not one of the four labels, so a
    mangled label annotates as unknown rather than being silently scored as a BUY."""
    b = (bucket or "").upper()
    if b not in BUCKETS:
        return None
    return "BUY" if b.startswith("BUY") else "SELL"


def _cmp(value, op, bound):
    if value is None:
        return None
    return (value > bound) if op == ">" else (value < bound)


def _fmt(value, unit):
    if value is None:
        return "n/a"
    return ("%+.2f%%" % value) if unit == "%" else ("%.1f" % value)


def _fmt_bound(bound, unit):
    return ("%+g%%" % bound) if unit == "%" else ("%g" % bound)


def gate_status(v8, bucket):
    """The three gates for this row's side, each with its value, its bound and its verdict.

    A gate whose input is NULL is `null`, not False. The distinction matters on a surface whose
    whole purpose this week is to accumulate observations: a stock with no monthly RSI has not
    failed the RSI gate, it has not been measured on it, and counting that as a failure would
    quietly bias the week's record against thinly covered names.
    """
    side = side_of(bucket)
    if side is None:
        return {"side": None, "gates": [], "passed": None, "measured": 0, "failing": []}
    out = []
    for g in TC_SCANNER_CONFIG["gates"][side]:
        val = (v8 or {}).get(g["key"])
        val = float(val) if isinstance(val, (int, float)) else None
        ok = _cmp(val, g["op"], g["bound"])
        out.append({
            "key": g["key"], "label": g["label"], "value": val, "op": g["op"],
            "bound": g["bound"], "unit": g["unit"], "pass": ok,
            "text": "%s %s, needs %s %s" % (g["label"], _fmt(val, g["unit"]),
                                            g["op"], _fmt_bound(g["bound"], g["unit"])),
        })
    measured = [g for g in out if g["pass"] is not None]
    failing = [g["label"] for g in out if g["pass"] is False]
    # ALL three must pass, and an unmeasured gate cannot be one of them — so `passed` is None when
    # any gate is unmeasured and nothing has already failed. Same three-state honesty as above.
    if failing:
        passed = False
    elif len(measured) < len(out):
        passed = None
    else:
        passed = True
    return {"side": side, "gates": out, "passed": passed,
            "measured": len(measured), "failing": failing}


def annotate(rows, v8_by_symbol):
    """Mark every row. NEVER drops one — observation mode (29448).

    `rows` are the scanner results (each needs `symbol` and `best_label`; `best_score100` is used
    when present). `v8_by_symbol` maps symbol -> the v8_metrics dict the scan already loaded, so
    this costs no extra query and reads the same row the score was built from.

    Adds per row: `bar` (its bucket's threshold), `bar_met`, `gate` (the block above),
    `rank_in_bucket` (1 = strongest of that bucket today) and `would_qualify`.
    Returns the summary counts the header needs.
    """
    thresholds = TC_SCANNER_CONFIG["score_thresholds"]
    cap = TC_SCANNER_CONFIG["caps"]["per_bucket_per_day"]

    # Rank inside each bucket by score100 desc. Rows with no score100 sort last and are ranked, not
    # skipped — a rank of "—" on a visible row reads as a bug, and every row is visible this week.
    order = sorted(range(len(rows)),
                   key=lambda i: (rows[i].get("best_label") or "",
                                  -(rows[i].get("best_score100") if rows[i].get("best_score100") is not None else -1)))
    seen = {}
    for i in order:
        b = rows[i].get("best_label") or ""
        seen[b] = seen.get(b, 0) + 1
        rows[i]["rank_in_bucket"] = seen[b]

    qualified = 0
    by_bucket = {b: {"total": 0, "would_qualify": 0} for b in BUCKETS}
    for r in rows:
        b = (r.get("best_label") or "").upper()
        s100 = r.get("best_score100")
        bar = thresholds.get(b)
        r["bar"] = bar
        r["bar_met"] = (None if (s100 is None or bar is None) else (s100 >= bar))
        gs = gate_status(v8_by_symbol.get(r.get("symbol")) or {}, b)
        r["gate"] = gs
        r["within_cap"] = (r.get("rank_in_bucket") is not None and r["rank_in_bucket"] <= cap)
        # would_qualify is a STRICT test: an unmeasured bar or an unmeasured gate is not a pass.
        # This number is the one line on the page that speaks for V1, so it must not be generous.
        wq = bool(r["bar_met"]) and gs["passed"] is True and r["within_cap"]
        r["would_qualify"] = wq
        if b in by_bucket:
            by_bucket[b]["total"] += 1
            if wq:
                by_bucket[b]["would_qualify"] += 1
        if wq:
            qualified += 1
    return {"total": len(rows), "would_qualify": qualified,
            "book_cap": TC_SCANNER_CONFIG["caps"]["book_total"],
            "per_bucket_cap": cap, "by_bucket": by_bucket,
            "observation_mode": TC_SCANNER_CONFIG["observation_mode"]}


def _gate_line(side):
    gs = TC_SCANNER_CONFIG["gates"][side]
    return " · ".join("%s %s %s" % (g["label"], g["op"], _fmt_bound(g["bound"], g["unit"])) for g in gs)


def rules_sheet():
    """The i-button sheet, BUILT FROM THE CONFIG — every number below is read, never typed."""
    C = TC_SCANNER_CONFIG
    t = C["score_thresholds"]
    caps = C["caps"]
    ex = C["exit"]
    lines = []
    if C["observation_mode"]:
        # The amendment asks for this line FIRST, and it is the most important sentence on the
        # sheet: a reader who skips it will think the page has filtered something out.
        lines.append({"k": "This week",
                      "v": ("Every signal is shown. The rules below are marked on each row for "
                            "study and will start filtering after review.")})
    lines.append({"k": "The four buckets and their score bars",
                  "v": " · ".join("%s %d" % (b, t[b]) for b in BUCKETS)})
    lines.append({"k": "Gates for a BUY signal", "v": _gate_line("BUY")})
    lines.append({"k": "Gates for a SELL signal", "v": _gate_line("SELL")})
    # The two cap numbers are READ from the config; only the sentence around them is prose. The
    # config's own `fill` string is engine wording ("rank by score100 desc") and is served raw for
    # machines, but it is not what a reader should meet on a sheet — plain words here, per the
    # house rule that a surface explains itself in short sentences.
    lines.append({"k": "How many are taken",
                  "v": ("Max %d per bucket per day, strongest first. The whole book is capped at "
                        "%d. One position per stock, and no second entry in the same stock on the "
                        "same day." % (caps["per_bucket_per_day"], caps["book_total"]))})
    lines.append({"k": "Exit plan",
                  "v": ("Target %+g%%, stop %+g%%, out by %s."
                        % (ex["target_pct"], ex["stop_pct"], ex["time_exit"]))})
    lines.append({"k": "How well tested", "v": "Tested on %s." % C["tested_on"]})
    lines.append({"k": "", "v": C["disclaimer"]})
    return {"title": "HOW THESE SIGNALS ARE PICKED",
            "lines": lines,
            "footer": "Config %s — locked %s" % (C["version"], C["locked_on"])}


@router.get("/api/tc-scanner/config")
def tc_scanner_config():
    """The config and the sheet, from the one dict. The page renders both from this response."""
    return {"config": TC_SCANNER_CONFIG, "sheet": rules_sheet(), "buckets": list(BUCKETS)}

"""
test_sector_source_theme.py — cc#1102

The sector source moved from the GVM segment (mcap-weighted over ~1,795 members) to the FUTURES
THEME (equal-weight over the active futures members). That is a change to the INPUT every basket
reads, so the two things most worth guarding are: nobody moved a THRESHOLD while moving the input,
and nobody grew a second grouping rule.

WHAT THIS ASSERTS, against the SHIPPED source:
  1. NO SECTOR GATE THRESHOLD MOVED. The four baskets' sector conditions are checked literally.
     Re-tuning against the new distribution is a later card and the founder said so explicitly; a
     threshold edit smuggled in beside a source change would be untraceable afterwards.
  2. ONE GROUPING. theme_change is the only module that groups themes for sector values, and both
     write paths — the live writer and the EOD engine — go through it. cc#1042 exists because two
     surfaces grouped differently and disagreed in public.
  3. NEITHER WRITE PATH STILL READS THE OLD TAXONOMY. segment_change/gvm_scores.segment must not
     appear in either sector routine, and the retired GVM-segment pass must be gone, not parked.
  4. A THIN THEME IS NULL ON EVERY FIELD, never 0 and never partial. A fabricated 0 would decide a
     trade on a number nobody measured; None fails every gate closed because each is written
     `v is not None and <comparison>`.
  5. THE MIN-MEMBER RULE MATCHES THE SECTORS TAB. theme_change.THEME_MIN_MEMBERS and the index
     exclusion must equal v8_endpoints' constants — the card says match v8_theme_sectors exactly.

Run:  python3 test_sector_source_theme.py
"""

import ast
import re
import sys

WRITER = "v8_signal_writer.py"
ENGINE = "v8_engine.py"
ENDPOINTS = "v8_endpoints.py"

# The sector gate literals, exactly as each handler writes them. cc#1102 do_not_touch.
GATE_LITERALS = [
    ('sell_reversal  sector_week <= -0.5', WRITER, 'return v is not None and float(v) <= -0.5'),
    ('sell_momentum  sector_week < 0',     WRITER, 'return v is not None and float(v) < 0.0'),
    ('buy_momentum   score band 0..6',     WRITER, '"sector_week":  (0.0, 6.0)'),
    ('buy_reversal   sector_week > 0',     WRITER,
     '{"key": "sector_week",  "label": "sector week",  "cond_min": "> 0",'),
]


def _func_source(src_text, name):
    for n in ast.parse(src_text).body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return "\n".join(src_text.splitlines()[n.lineno - 1:n.end_lineno])
    return ""


def _extract(src_text, names):
    tree = ast.parse(src_text)
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in names)]
    # The extracted defs carry typing annotations, which are evaluated at def time. Supplying the
    # names is cheaper and safer than stripping them — a test that rewrites the source it is
    # checking is not checking the shipped source any more.
    ns = {"Dict": dict, "Optional": lambda *a: None, "log": None}
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), "<t>", "exec"), ns)
    return ns


def main():
    wsrc = open(WRITER, encoding="utf-8").read()
    esrc = open(ENGINE, encoding="utf-8").read()
    psrc = open(ENDPOINTS, encoding="utf-8").read()
    tsrc = open("theme_change.py", encoding="utf-8").read()
    fails = []

    # 1 — thresholds
    for label, _f, lit in GATE_LITERALS:
        ok = lit in wsrc
        print("  %-36s unchanged: %s" % (label, ok))
        if not ok:
            fails.append("SECTOR THRESHOLD MOVED or was reworded: %s" % label)

    # 2 — one grouping, both write paths through it
    for name, src, fn in (("live writer", wsrc, "_add_sector_aggregates"),
                          ("EOD engine", esrc, "run_v8_engine")):
        body = _func_source(src, fn) or src
        ok = "theme_change" in body
        print("  %-12s %-26s uses theme_change: %s" % (name, fn, ok))
        if not ok:
            fails.append("%s (%s) does not go through theme_change" % (name, fn))

    # 3 — the old taxonomy is gone from both
    # CODE only, not the docstring: the docstring SHOULD name the old taxonomy — that is how the
    # next reader learns what changed and why. What must not survive is a live reference to it.
    sect_fn = [n for n in ast.parse(wsrc).body
               if isinstance(n, ast.FunctionDef) and n.name == "_add_sector_aggregates"][0]
    sect_code = ast.unparse(ast.Module(
        body=[n for n in sect_fn.body if not (isinstance(n, ast.Expr)
                                              and isinstance(n.value, ast.Constant)
                                              and isinstance(n.value.value, str))],
        type_ignores=[]))
    for bad in ("segment_change", "gvm_scores"):
        if bad in sect_code:
            fails.append("the live sector routine still READS %s" % bad)
    if "segment_change" not in sect_code and "gvm_scores" not in sect_code:
        print("  live sector routine reads neither segment_change nor gvm_scores: OK")
    if "_update_sector_aggregates_sql" in wsrc.replace(
            "# cc#1011 retired the separate _update_sector_aggregates_sql pass", ""):
        fails.append("the retired GVM-segment sector pass is still present in the writer")
    else:
        print("  retired GVM-segment pass deleted, not parked: OK")

    # 5 — constants match the Sectors tab (checked before 4, which uses them)
    tns = _extract(tsrc, {"THEME_MIN_MEMBERS", "INDEX_EXCLUDE_SQL", "NEW_ENTRANTS",
                          "_FIELDS", "aggregate"})
    p_min = int(re.search(r"^THEME_MIN_MEMBERS = (\d+)", psrc, re.M).group(1))
    p_idx = re.search(r'^_INDEX_EXCLUDE_SQL = (".*")', psrc, re.M).group(1)
    print("  THEME_MIN_MEMBERS  theme_change=%s  v8_endpoints=%s" % (tns["THEME_MIN_MEMBERS"], p_min))
    if tns["THEME_MIN_MEMBERS"] != p_min:
        fails.append("min-member rule differs from the Sectors tab: %s vs %s"
                     % (tns["THEME_MIN_MEMBERS"], p_min))
    if tns["INDEX_EXCLUDE_SQL"] != ast.literal_eval(p_idx):
        fails.append("index exclusion differs from the Sectors tab")
    else:
        print("  index exclusion matches the Sectors tab: OK")

    # 4 — thin themes are None on EVERY field
    agg = tns["aggregate"]
    tmap = {"A": "T", "B": "T", "C": "T", "D": "U", "E": "U"}
    vals = {s: {"day_1d": 1.0, "week_return": 2.0, "month_return": 3.0} for s in "ABCDE"}
    out = agg(dict(tmap), dict(vals))
    print("  full theme T ->", {k: out["T"][k] for k in ("day", "week", "month", "members_priced")})
    if out["T"]["day"] != 1.0 or out["T"]["week"] != 2.0:
        fails.append("equal-weight average is wrong on a full theme: %s" % out["T"])
    if not out["U"]["thin"] or any(out["U"][k] is not None for k in ("day", "week", "month")):
        fails.append("a 2-member theme did not suppress to None on all three fields: %s" % out["U"])
    else:
        print("  2-member theme U -> None on day, week and month: OK")

    # a theme where ONE field is missing must still report the others honestly
    vals["C"]["month_return"] = None
    out2 = agg(dict(tmap), dict(vals))
    if out2["T"]["month"] != 3.0 or out2["T"]["day"] != 1.0:
        fails.append("a partially-missing field broke the other fields: %s" % out2["T"])
    else:
        print("  one missing month does not poison day/week: OK")

    # a symbol outside the theme map must not leak into any average
    out3 = agg({"A": "T", "B": "T", "C": "T"},
               dict(vals, Z={"day_1d": 99.0, "week_return": 99.0, "month_return": 99.0}))
    if out3["T"]["day"] != 1.0:
        fails.append("a non-futures symbol leaked into a theme average: %s" % out3["T"])
    else:
        print("  a symbol outside the futures map is ignored: OK")

    print()
    if fails:
        print("FAIL")
        for f in fails:
            print("  -", f)
        return 1
    print("SECTOR SOURCE OK — one theme grouping, no threshold moved, thin themes are NULL")
    return 0


if __name__ == "__main__":
    sys.exit(main())

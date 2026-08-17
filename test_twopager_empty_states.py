"""
test_twopager_empty_states.py — cc#1085 R6-P5 verify, as a test rather than a claim.

THE RULE: no invented prose, ever. A short, honest page 2 is correct output; padding it to fill
the sheet is not. P5 names two cases:

  1. overview NULL or under 100 chars (37 of the 1,791 scored symbols) -> print
     "Business profile not available for this company." and DROP the moat/risk block entirely.
  2. no screener_raw row -> OMIT Financial profile and Latest quarter rather than printing a grid
     of dashes.

WHY THIS IS A TEST AND NOT A PASTED RENDER. A pasted render proves the behaviour on the day it was
pasted. The failure this guards against is somebody later "helpfully" filling the empty case with a
template sentence, which is exactly the fabrication the rule exists to stop — and that change would
sail past a paste in an old log. So the two cases are executed.

WHAT THE TEST HONESTLY CANNOT CLAIM. Case 2 does not occur in production today: zero of the 1,791
scored symbols lack a screener_raw row (every one of them joins). So case 2 is exercised by forcing
the fetch to return None. That is stated here rather than left to look like a live observation.
Case 1 uses AHLWEST, a REAL symbol whose input_raw.overview is genuinely empty, with its REAL
screener_raw values — including three columns (Return on equity, Debt to equity, Price to book)
that are genuinely NULL, so the partial-row path is covered by live data too.

Run: python3 test_twopager_empty_states.py   (stubs psycopg/fastapi so it runs without deps)
"""

import re
import sys
import types


def _stub_deps():
    """gvm_twopager imports psycopg and fastapi. Neither is needed to render markup, and stubbing
    them lets this test run in any checkout rather than only where the app's deps are installed."""
    for name, attrs in [
        ("psycopg", {"connect": lambda *a, **k: None}),
        ("fastapi", {"APIRouter": lambda **k: types.SimpleNamespace(
            get=lambda *a, **k: (lambda f: f)),
            "HTTPException": type("HTTPException", (Exception,), {})}),
        ("fastapi.responses", {"HTMLResponse": object}),
    ]:
        try:
            __import__(name)
        except ImportError:
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[name] = m


_stub_deps()
import gvm_twopager as T   # noqa: E402

# AHLWEST as it actually sits in screener_raw on 2026-08-16. Three NULLs are real, not invented.
AHLWEST = dict(zip(T._SCREENER_COLS, [
    443.81, 90.28, 44.49, 19.66, None, None, 3.27, None, 0, 72.11, 0, 0.57,
    7.1, 7.7, 7.82, 27.07, 35.95, 20.58, 97.71, 14.28, 89.28, 8.08, 40.32, 38.03, "Q1FY27"]))

REP = {"symbol": "AHLWEST", "company_name": "Asian Hotels (West) Ltd", "score_date": "2026-08-16"}

NOT_AVAILABLE = "Business profile not available for this company."


class _Cur:
    """Two fetches only: input_raw.overview, then the screener_raw row."""

    def __init__(self, overview, screener):
        self.ov, self.sc, self.mode = overview, screener, None

    def execute(self, q, p=None):
        self.mode = "ov" if "input_raw" in q else "sc"

    def fetchone(self):
        if self.mode == "ov":
            return None if self.ov is None else (self.ov,)
        return tuple(self.sc[c] for c in T._SCREENER_COLS) if self.sc else None


def _has_moat_block(html):
    return "Why the position is hard to attack" in html or "What would break it" in html


def main() -> int:
    ok = True
    results = []

    # ── Case 1: real symbol, genuinely empty overview, real (partly NULL) screener row ──────────
    a = T.build_page2(_Cur("", AHLWEST), REP)
    checks = [
        ("prints the not-available line", NOT_AVAILABLE in a),
        ("moat/risk block dropped", not _has_moat_block(a)),
        ("Financial profile KEPT (the row exists)", "Financial profile" in a),
        ("Latest quarter KEPT (the row exists)", "Latest quarter" in a),
        ("genuinely NULL columns show an em-dash, not a zero",
         a.count("<td>&mdash;</td>") == 3),
        ("no fabricated 0.00 for the NULL columns",
         "Return on equity</td><td>0.00%" not in a),
    ]
    results.append(("CASE 1 - overview empty (AHLWEST, real)", a, checks))

    # A NULL overview (not merely short) must take the same path as an empty string.
    b = T.build_page2(_Cur(None, AHLWEST), REP)
    results.append(("CASE 1b - overview NULL (no input_raw row)", b, [
        ("prints the not-available line", NOT_AVAILABLE in b),
        ("moat/risk block dropped", not _has_moat_block(b)),
    ]))

    # A 99-char overview is on the wrong side of the threshold and must not render as a profile.
    short = "x" * 99
    c = T.build_page2(_Cur(short, AHLWEST), REP)
    results.append(("CASE 1c - overview 99 chars (just under the threshold)", c, [
        ("prints the not-available line", NOT_AVAILABLE in c),
        ("does not print the stub text as a profile", short not in c),
    ]))

    # ── Case 2: no screener_raw row. FORCED — zero of 1,791 scored symbols hit this today. ──────
    d = T.build_page2(_Cur("", None), REP)
    results.append(("CASE 2 - no screener_raw row (FORCED, 0 of 1,791 in production)", d, [
        ("Financial profile omitted", "Financial profile" not in d),
        ("Latest quarter omitted", "Latest quarter" not in d),
        ("no grid of dashes left behind", "&mdash;</td>" not in d),
        ("page is short and honest, not padded", len(d) < 900),
        ("masthead and foot still present",
         "COMPANY BACKGROUND" in d and "not investment advice" in d),
    ]))

    for title, html, checks in results:
        print("\n%s" % title)
        print("  rendered %d chars" % len(html))
        for name, passed in checks:
            print("    %-52s %s" % (name, "OK" if passed else "FAIL"))
            ok = ok and passed

    # A RATCHET, not a proof. build_page2 is allowed exactly two originated sentences: the
    # not-available line and the ref's fixed footer. Anything else added later fails here and has
    # to be argued for, which is the point — the failure mode this rule guards against is somebody
    # filling an empty case with a plausible template sentence months from now.
    # Scoped honestly: this covers build_page2's OWN prose. The conditional one-liners in
    # _financials_block / _quarter_block ("Profit has compounded faster than sales over both
    # windows") are statements of the arithmetic just printed above them, not company commentary,
    # and they live outside this span.
    ALLOWED = {
        NOT_AVAILABLE,
        "Figures from company filings via the fundamentals pipeline, as of %s. ",
        "Research only &mdash; not investment advice.",
    }
    src = open(T.__file__).read()
    body = re.search(r"def build_page2\(cur, rep\)(.*?)\n@router", src, re.S)
    code = re.sub(r'"""(?:.|\n)*?"""', "", body.group(1)) if body else ""
    sentences = re.findall(r'"([A-Z][^"]{25,})"', code)
    invented = [s for s in sentences if s not in ALLOWED]
    print("\n  build_page2 originated prose: %s" % (sentences or "none"))
    print("    %-52s %s" % ("no sentence beyond the two allowed",
                            "OK" if not invented else "FAIL %s" % invented))
    ok = ok and not invented

    print("\nEMPTY STATES OK" if ok else "\nEMPTY STATES FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

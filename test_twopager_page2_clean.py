"""
test_twopager_page2_clean.py — cc#1085 R6-P4 verify, as a test rather than a claim.

THE RULE IT PROTECTS (founder-locked, report §C P4): page 2 of the GVM 2-Pager carries no rating,
no score, no pillar value, and the string GVM must not appear. Page 2 is company background only.

WHY THIS IS A FILE AND NOT A ONE-OFF GREP. P4 asks for the grep output pasted into the room once.
But the thing being promised is a property of every future render of every symbol, and a promise
kept by one paste rots the first time somebody adds a field to page 2. So the grep lives here and
runs on demand.

WHY WORD BOUNDARIES. P4 words the check as a case-insensitive grep for `gvm`, `rating`, `score`.
Taken as bare substrings that check FAILS ON THE REF ITSELF: "rating" sits inside "operating", and
the ref's own page 2 prints "Operating margin" twice — a field P4 explicitly requires. `\brating\b`
is the test that means what the rule means, so that is what runs, and the substring case is
reported separately so the difference is visible rather than assumed.

WHAT IT DOES NOT POLICE. Nine of 1,791 scored companies describe THEMSELVES using these words —
ICRA, CRISIL and CARERATING are credit rating agencies; TFCILTD, LICHSGFIN, RECLTD, UGROCAP,
TATACAP and ZEEL carry "rating"/"score" in their business text. That prose is the company's own
description, passed through from input_raw.overview. Redacting it would make the page wrong about
what the company does. The ban is on THIS MODULE emitting an assessment, so the test targets the
module's own template and labels. Zero of 1,791 overviews contain "GVM".

Run: python3 test_twopager_page2_clean.py   (no DB, no deps — it reads one file)
"""

import re
import sys
from pathlib import Path

MOD = Path(__file__).resolve().parent / "gvm_twopager.py"

# The module's page-2 surface: the template, plus every function that emits page-2 markup. If a
# later change adds a page-2 builder it must be listed here, or it is not covered.
PAGE2_SPANS = [
    r'PAGE2_TMPL = _Tmpl\(r"""(.*?)"""\)',
    r"def _financials_block\(sc\):(.*?)\ndef ",
    r"def _quarter_block\(sc\):(.*?)\ndef ",
    r"def build_page2\(cur, rep\)(.*?)\n@router",
]

BANNED = ["gvm", "rating", "ratings", "score", "scores", "pillar"]


def main() -> int:
    src = MOD.read_text(encoding="utf-8")
    ok = True

    chunks = []
    for pat in PAGE2_SPANS:
        m = re.search(pat, src, re.S)
        if not m:
            print("  MISSING page-2 span: %s" % pat)
            ok = False
            continue
        chunks.append(m.group(1))
    body = "\n".join(chunks)

    # Strip comments and docstrings: they EXPLAIN the ban, so they naturally contain the words.
    # What the rule governs is emitted markup, not the reasoning next to it.
    body_code = re.sub(r'"""(?:.|\n)*?"""', "", body)
    body_code = "\n".join(l.split("#", 1)[0] for l in body_code.splitlines())

    print("page-2 surface scanned: %d chars (%d after stripping comments/docstrings)"
          % (len(body), len(body_code)))
    for word in BANNED:
        hits = re.findall(r"\b%s\b" % word, body_code, re.I)
        status = "OK" if not hits else "FAIL (%d)" % len(hits)
        print("  \\b%-9s\\b  %s" % (word, status))
        if hits:
            ok = False
            for line in body_code.splitlines():
                if re.search(r"\b%s\b" % word, line, re.I):
                    print("      %s" % line.strip()[:110])

    # The substring form, reported so the word-boundary choice is visible and not a quiet weakening.
    sub_hits = {w: len(re.findall(w, body_code, re.I)) for w in ("rating", "score", "gvm")}
    print("  substring form (for comparison, NOT the pass condition): %s" % sub_hits)
    if sub_hits["rating"]:
        which = sorted({m.group(0) for m in re.finditer(r"\w*rating\w*", body_code, re.I)})
        print("      substring 'rating' appears only inside: %s" % ", ".join(which))

    # Page 2 must not read anything score-bearing off the report payload.
    m = re.search(r"def build_page2\(cur, rep\)(.*?)\n@router", src, re.S)
    if m:
        for field in ('rep.get("scores")', 'rep.get("verdict")', 'rep.get("benchmark")',
                      'rep["scores"]', 'rep["verdict"]'):
            bad = field in m.group(1)
            print("  build_page2 does not read %-22s %s" % (field, "OK" if not bad else "FAIL"))
            ok = ok and not bad

    print("\nPAGE 2 CLEAN" if ok else "\nPAGE 2 NOT CLEAN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

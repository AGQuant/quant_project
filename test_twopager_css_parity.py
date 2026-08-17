"""
test_twopager_css_parity.py — cc#1085 R6-P2 verify, as a test rather than a claim.

P2 asks that the served CSS block be byte-identical to the ref's. That is exactly the kind of
promise that rots: someone reformats the module, a linter reflows a long line, and the sheet
quietly grows a third page months later with nothing to point at. So the check is executable.

Run: python3 test_twopager_css_parity.py   (no pytest, no DB, no deps — it reads two files)

WHY BYTE-IDENTITY IS THE RIGHT BAR HERE and not "visually equivalent": report §C says the ref is
tuned, not decorative — every font size, padding and column width was adjusted until the content
landed on exactly two A4 pages with no orphan third. A rounded padding is not a cosmetic
difference in a document whose entire contract is its page count.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF = ROOT / "design_refs" / "scorr_gvm_2pager_R1.html"
MOD = ROOT / "gvm_twopager.py"


def ref_css() -> str:
    return re.search(r"<style>(.*?)</style>", REF.read_text(encoding="utf-8"), re.S).group(1)


def module_css() -> str:
    src = MOD.read_text(encoding="utf-8")
    m = re.search(r'REF_CSS = r"""(.*?)"""', src, re.S)
    if not m:
        raise AssertionError("REF_CSS block not found in gvm_twopager.py")
    return m.group(1)


def main() -> int:
    a, b = ref_css(), module_css()
    ok = a == b
    print("ref  CSS: %d bytes" % len(a))
    print("mod  CSS: %d bytes" % len(b))
    if ok:
        print("PARITY OK — served CSS is byte-identical to design_refs/scorr_gvm_2pager_R1.html")
    else:
        print("PARITY FAILED — the served CSS has drifted from the ref.")
        ra, rb = a.splitlines(), b.splitlines()
        for i in range(max(len(ra), len(rb))):
            x = ra[i] if i < len(ra) else "<missing>"
            y = rb[i] if i < len(rb) else "<missing>"
            if x != y:
                print("  line %d\n    ref: %r\n    mod: %r" % (i + 1, x, y))

    # The @page rule and the two-page structure are the load-bearing bits; name them explicitly
    # so a failure says WHICH property broke rather than just "not equal".
    checks = [
        ("@page A4 rule present", "@page { size: A4;" in b),
        ("page-break-after on .page", ".page { page-break-after: always; }" in b),
        ("last page does not break", ".page:last-child { page-break-after: auto; }" in b),
    ]
    body = re.search(r'PAGE_BODY = r"""(.*?)"""', MOD.read_text(encoding="utf-8"), re.S)
    checks.append(("PAGE_BODY ported", bool(body)))
    if body:
        checks.append(("exactly two .page divs", body.group(1).count('<div class="page">') == 2))

    for name, passed in checks:
        print("  %-28s %s" % (name, "OK" if passed else "FAIL"))
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

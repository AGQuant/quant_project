"""cc#1588 NO_BRAND_IN_TAB_TITLE_V1 (session_log 36423) — no "Scorr" in a WEB browser tab title.

The rule: a web page's tab title is the page name only. The brand lives in the wordmark, the PWA
manifest and the app-title metas, never in <title> or document.title. This check has two callers:

  * CLI:  python tools/check_tab_titles.py [paths...]   — scans the web pages (default: every
          *.html in the repo root) and exits 1 on the first offender. Prints one line per hit.
  * Gate: gate(filepath, content) -> (allowed, why) — same signature as theme_validator.gate, run
          by /api/admin/github_push on the content ABOUT to land. Import errors or crashes never
          block a push (github_ops.py catches them) — this is a lint, not the deploy path.

SCOPE IS WEB ONLY (founder scope_correction 02-Sep-2026 12:11 IST). mobile/**, previews/** and
design_refs/** are out of scope on purpose: mobile carries the app-title metas, previews and refs
are not served as product pages. Non-HTML files are never judged.
"""
import os
import re
import sys

# Directory prefixes that are NOT web browser pages. Kept as a tuple so the gate and the CLI agree.
EXCLUDED_PREFIXES = ("mobile/", "previews/", "design_refs/", "tools/", "reports/", "docs/")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DOC_TITLE_RE = re.compile(r"document\.title\s*=\s*(['\"`])(.*?)\1", re.I | re.S)
_BRAND_RE = re.compile(r"scorr", re.I)


def in_scope(filepath: str) -> bool:
    p = (filepath or "").lstrip("./").replace("\\", "/")
    if not p.lower().endswith(".html"):
        return False
    return not p.startswith(EXCLUDED_PREFIXES)


def find_offenders(content: str):
    """Return [(kind, line_no, text)] for every <title> or document.title carrying the brand."""
    hits = []
    for m in _TITLE_RE.finditer(content):
        text = m.group(1).strip()
        if _BRAND_RE.search(text):
            hits.append(("<title>", content.count("\n", 0, m.start()) + 1, text))
    for m in _DOC_TITLE_RE.finditer(content):
        text = m.group(2).strip()
        if _BRAND_RE.search(text):
            hits.append(("document.title", content.count("\n", 0, m.start()) + 1, text))
    return hits


def gate(filepath: str, content: str):
    """(allowed, why). Out-of-scope files always pass; in-scope files fail on the first brand hit."""
    if not in_scope(filepath):
        return True, None
    hits = find_offenders(content or "")
    if not hits:
        return True, None
    kind, line, text = hits[0]
    return False, (f"NO_BRAND_IN_TAB_TITLE_V1 (cc#1588): {filepath}:{line} {kind} is '{text}'. "
                   f"Web tab titles carry the page name only, never 'Scorr'. {len(hits)} hit(s).")


def _default_paths(root: str):
    out = []
    for name in sorted(os.listdir(root)):
        if name.lower().endswith(".html") and os.path.isfile(os.path.join(root, name)):
            out.append(name)
    return out


def main(argv):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = argv[1:] or _default_paths(root)
    bad = 0
    checked = 0
    for rel in paths:
        if not in_scope(rel):
            continue
        full = rel if os.path.isabs(rel) else os.path.join(root, rel)
        try:
            with open(full, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as e:
            print(f"SKIP {rel}: {e}")
            continue
        checked += 1
        for kind, line, text in find_offenders(content):
            bad += 1
            print(f"FAIL {rel}:{line} {kind} = {text!r}")
    print(f"checked {checked} web page(s), {bad} offending title(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

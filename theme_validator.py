"""theme_validator.py — cc#1185 P8: the check that stops the token layer rotting again.

TWO CHECKS, and they answer two different failure modes the sprint uncovered.

(A) SET COMPLETENESS. A theme set that is missing a key does not render blank — it falls through
    to whatever the cascade has underneath, which is usually ANOTHER theme's value. That is the
    worst kind of bug because it looks fine on the theme you are testing. So the contract is the
    UNION of the keys the three sets declare, and any set missing one of them is named.
    The legacy sets are expected to fail this and the card says so: "Run it on GOLD NIGHT + both
    legacy hidden sets; legacy sets may fail — report, do not fix them." Reported, not fixed.

(B) RAW PRIMITIVES. Every hex, rgb() or length that a themed surface writes directly instead of
    reading from the token layer. P&L green/red and pass/fail are EXEMPT — the card's own
    invariant makes them semantic, identical across themes — so they are allowlisted rather than
    counted, and the allowlist decision is made on the SELECTOR, never on the hue.

WHY IT IS A RATCHET AND NOT A ZERO-THRESHOLD GATE. The repoint is not finished: roughly 1,250
declarations still sit on surfaces that cannot be repointed until the schema is linked site-wide.
A gate that demanded zero today would fail every push, which in practice means someone turns it
off, and a gate that is off is worse than no gate. So each file carries a BASELINE count and the
rule is simple: a file may go DOWN or stay level, never up. The counts can only shrink, and the
day one reaches zero it is pinned there by its own baseline.

HOW IT ACTUALLY BLOCKS. github_push is the one push path this app owns, so the check runs there on
the incoming content before the commit. A regression is refused with the count and the file named.
`allow_theme_regression: true` in the request body is the deliberate escape hatch, and it is echoed
back in the response so an intentional regression is on the record rather than silent.
"""

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
THEMES_FILE = os.path.join(HERE, "scorr_themes.css")
BASELINE_FILE = os.path.join(HERE, "reports", "theme_baseline_v1.json")

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
FUNC = re.compile(r"\b(rgba?|hsla?)\s*\([^)]*\)")
LEN = re.compile(r"(?<![\w#.])-?\d*\.?\d+(px|rem|em)\b")
VAR = re.compile(r"var\([^)]*\)")

# The families a theme can move. width/height/clip-path are layout, not theme — the P3 schema does
# not key them and the validator must not count them, or the two disagree about the same file.
FAMILY = re.compile(
    r"^(color|background|background-color|border(-(top|right|bottom|left))?-color|fill|stroke|"
    r"font|font-size|font-family|letter-spacing|"
    r"border(-(top|bottom)-(left|right))?-radius|"
    r"border|border-(top|right|bottom|left)|border(-(top|right|bottom|left))?-(width|style)|outline|"
    r"box-shadow|text-shadow|"
    r"(margin|padding)(-(top|right|bottom|left))?|gap|row-gap|column-gap)$")

# SEMANTIC: exempt by the card's invariant. Decided on the SELECTOR, never the colour — the cyan
# eyebrow and the volt accent are chrome that happens to be bright, and they are exactly what a
# theme has to be able to move.
SEMANTIC_SEL = re.compile(
    r"(^|[.#\s:\[])(ok|bad|win|loss|lose|up|dn|down|pos|neg|bull|bear|gain|pnl|profit|"
    r"green|red|pass|fail|w|l|n|mu|good|weak|strong|alert|danger|warn|vixmid|"
    r"[a-z-]*(-|_)(ok|bad|win|loss|up|down|pos|neg))($|[\s.,:>+~\[{])", re.I)
COLOUR_PROP = re.compile(r"^(color|background|background-color|border(-(top|right|bottom|left))?-color|fill|stroke)$")


def mask_comments(css):
    """Blank /* ... */ keeping length and newlines, so offsets and line numbers survive. Masked and
    not stripped for the same reason P1 did it: a hex inside a comment is not a live primitive, but
    the lines after it still have to point at the right place."""
    out = list(css)
    for m in re.finditer(r"/\*.*?\*/", css, re.S):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def page_styles(html):
    """The <style> bodies of an HTML page, found on HTML-COMMENT-MASKED text.

    The masking is not caution for its own sake. mobile/gvm.html line 14 says "the preview's inline
    <style>" inside a prose comment; matched naively that opens a block covering the whole head.
    That is the cc#821 failure, and it cost a full false-positive verification run during P5.
    """
    masked = re.sub(r"<!--.*?-->", lambda m: " " * len(m.group(0)), html, flags=re.S)
    return "\n".join(masked[m.start(1):m.end(1)]
                     for m in re.finditer(r"<style[^>]*>(.*?)</style>", masked, re.S))


def declarations(css):
    """(selector, prop, value, is_custom) for every declaration. Braces, not a parser: at-rules keep
    their block, so @media and @keyframes bodies are walked as ordinary rules — which is right,
    because a colour inside @media is still a primitive on the page."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sel = " ".join(m.group(1).split())
        for decl in m.group(2).split(";"):
            if ":" not in decl:
                continue
            prop, _, val = decl.partition(":")
            prop, val = prop.strip().lower(), val.strip()
            if prop and val:
                out.append((sel, prop, val, prop.startswith("--")))
    return out


def count_raw(css, detail=False):
    """How many declarations in this stylesheet still write a literal the token layer could own."""
    n = 0
    rows = []
    for sel, prop, val, custom in declarations(mask_comments(css)):
        if custom or not FAMILY.match(prop):
            continue
        if COLOUR_PROP.match(prop) and SEMANTIC_SEL.search(sel):
            continue                                    # semantic — exempt, not debt
        bare = VAR.sub("", val)
        if HEX.search(bare) or FUNC.search(bare) or LEN.search(bare):
            n += 1
            if detail:
                rows.append({"sel": sel[:60], "prop": prop, "val": val[:50]})
    return (n, rows) if detail else n


def count_file(path, content=None):
    """Raw count for a repo path. `content` lets the push gate score a file that is not on disk
    yet — the whole point of a gate is to judge what is ABOUT to land, not what already did."""
    if content is None:
        with open(os.path.join(HERE, path), encoding="utf-8") as fh:
            content = fh.read()
    return count_raw(extract_css(path, content))


def extract_css(path, content):
    """The CSS inside a file, whatever kind of file it is.

    Two of the app's biggest sheets are not .css files at all — pwa_endpoints.MOBILE_CSS and
    mobile_endpoints.MOBILE_CSS are triple-quoted strings inside modules, served under the /static/
    URL namespace. Counting a .py file as raw CSS would score its Python; ignoring it would leave
    909 declarations unmeasured. So the rule is explicit: from a module, take the bodies of the
    `*_CSS = \"\"\"...\"\"\"` constants and nothing else.
    """
    if path.endswith(".html"):
        return page_styles(content)
    if path.endswith(".py"):
        return "\n".join(m.group(2) for m in
                         re.finditer(r'([A-Z][A-Z0-9_]*_CSS)\s*=\s*"""(.*?)"""', content, re.S))
    return content


def theme_sets(css=None):
    """{theme: {key: value}} for every body[data-theme=...] block in the token file."""
    if css is None:
        with open(THEMES_FILE, encoding="utf-8") as fh:
            css = fh.read()
    css = mask_comments(css)
    out = {}
    for m in re.finditer(r'body\[data-theme="([\w-]+)"\]\s*\{([^{}]*)\}', css, re.S):
        out[m.group(1)] = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;\n}]+)", m.group(2)))
    return out


def schema_keys(css=None):
    """The :root schema — the structural keys every theme inherits."""
    if css is None:
        with open(THEMES_FILE, encoding="utf-8") as fh:
            css = fh.read()
    m = re.search(r":root\s*\{(.*?)\n\}", mask_comments(css), re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;\n}]+)", m.group(1))) if m else {}


def check_sets():
    """(A) Every theme set must fill every key the CONTRACT names. The contract is the union of what
    the sets declare, because a key one theme needs is a key all of them need — otherwise the ones
    without it fall through to a neighbour's value and look correct on the theme you are testing."""
    sets = theme_sets()
    contract = sorted({k for keys in sets.values() for k in keys})
    report = {}
    for name, keys in sorted(sets.items()):
        missing = [k for k in contract if k not in keys]
        report[name] = {"declared": len(keys), "missing": missing}
    return {"contract_size": len(contract), "contract": contract, "sets": report,
            "complete": [n for n, r in report.items() if not r["missing"]],
            "incomplete": [n for n, r in report.items() if r["missing"]]}


def load_baseline():
    try:
        with open(BASELINE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def check_file(path, content=None):
    """(B) as a RATCHET. A file may go down or stay level, never up. A file with no baseline is not
    a failure — it is unmeasured, and saying so is more useful than inventing a threshold for it."""
    base = load_baseline().get("files", {})
    now = count_file(path, content)
    if path not in base:
        return {"path": path, "now": now, "baseline": None, "ok": True,
                "note": "no baseline for this file — recorded as unmeasured, not as a pass"}
    was = base[path]
    return {"path": path, "now": now, "baseline": was, "ok": now <= was,
            "delta": now - was,
            "note": ("clean" if now == 0 else
                     "improved by %d" % (was - now) if now < was else
                     "level" if now == was else
                     "REGRESSION: %d new raw declarations" % (now - was))}


def validate(paths=None):
    """The whole report. Used by the MCP tool, the endpoint and the push gate."""
    base = load_baseline()
    paths = paths or sorted(base.get("files", {}))
    files = [check_file(p) for p in paths]
    sets = check_sets()
    return {"sets": sets, "files": files,
            "raw_total": sum(f["now"] for f in files),
            "baseline_total": base.get("total"),
            "baseline_sha": base.get("sha"),
            "regressions": [f for f in files if not f["ok"]],
            "ok": not any(not f["ok"] for f in files)}


def gate(path, content):
    """The push hook. Returns (allowed, message). Only themed surfaces with a baseline are judged;
    everything else passes untouched, because a gate that opines on files it was never given a
    number for is a gate people learn to ignore."""
    if not (path.endswith(".css") or path.endswith(".html")):
        return True, None
    r = check_file(path, content)
    if r["baseline"] is None or r["ok"]:
        return True, None
    d = r["delta"]
    return False, ("THEME_TOKEN_LOCK: %s would add %d raw declaration%s (%d -> %d). "
                   "Repoint %s to the token layer in scorr_themes.css, or resend with "
                   "allow_theme_regression: true to land it deliberately."
                   % (path, d, "" if d == 1 else "s", r["baseline"], r["now"],
                      "it" if d == 1 else "them"))

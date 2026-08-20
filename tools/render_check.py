#!/usr/bin/env python3
"""cc#1133 · CC EYES — render a repo template in headless Chromium and fail on layout defects.

WHY THIS EXISTS
    CC pushes UI changes blind. There is no route to scorr.in from the CC container and the real
    webfonts do not load here, so until now "verified" meant reading CSS and reasoning about it.
    That reasoning failed three times in a row on ONE element — the digest pivot ladder — across
    cc#1111 (b254a91), cc#1124 (474f95b) and cc#1127 (ff57d6f). Each fix was argued correct and
    each shipped a defect the founder photographed the next morning. Reasoning about layout is not
    a substitute for laying it out.

WHAT IT CHECKS
    a) OVERLAP      no two text-bearing elements whose painted boxes intersect
    b) CLIPPING     no element cut off by a scroll container that offers no way to scroll to it
    c) COLLAPSE     no zero-width or zero-height cell inside a grid or flex row
    d) OVERFLOW     no horizontal document overflow

    Every failure names the element selectors involved, so the output points at the fix.

THE FONT PROBLEM, AND WHY THIS IS RUN AT A TEXT SCALE
    Google Fonts is unreachable here, so a page declaring 'JetBrains Mono' silently falls through
    to whatever the container has. That fallback is NARROWER than the real face, which is exactly
    how cc#1124's measurement said "fits" while the founder's screen said otherwise. Two defences:
    a real local monospace is substituted for the webfont families, and every check also runs at an
    inflated text scale, because Android's text-scaling accessibility setting is the input that
    turned "just fits" into "overlaps" on his phone. --scale controls it; the default sweep is
    1.0 and 2.0.

KNOWN LIMITATION — READ THIS BEFORE TRUSTING A PASS
    This renders the REPO TEMPLATE, not the live site. It does not cover pwa.js injection, the
    _MOBILE_HEAD asset chain, live payload shapes, or the real webfont metrics. A PASS here means
    the markup and CSS in the file lay out correctly with the data it was given. It is a floor,
    not a guarantee. THE FOUNDER'S PHONE REMAINS THE FINAL GATE.

USAGE
    python3 tools/render_check.py scorr_digest_mobile.html
    python3 tools/render_check.py mobile/home.html --mock tools/mock/home.json
    python3 tools/render_check.py <file> --html-string "<div>…</div>"   # check a fragment
    python3 tools/render_check.py <file> --widths 360,412 --scale 1,2 --shots /tmp/shots

    Exit code 0 = every check passed at every width and scale. Nonzero = at least one failed.

CONTAINER SETUP (documented per the card so a fresh container can repeat it)
    python3 -m pip install --break-system-packages playwright
    # Do NOT run `playwright install` — this container ships Chromium already. Point at it:
    #   PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
    #   executable_path=/opt/pw-browsers/chromium-1194/chrome-linux/chrome
    # The Python package's expected build revision differs from the preinstalled one, which is why
    # the explicit executable_path below is required rather than optional.
"""

import argparse
import json
import os
import re
import sys

CHROME = os.environ.get(
    "CC_CHROMIUM",
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
)

# Substituted for every webfont family the templates declare. Without this the fallback is a
# PROPORTIONAL face, and a monospace column that fits proportionally can overlap in real mono.
FONT_SHIM = """
  @font-face{font-family:'JetBrains Mono';src:local('DejaVu Sans Mono'),local('Liberation Mono');}
  @font-face{font-family:'IBM Plex Mono';src:local('DejaVu Sans Mono'),local('Liberation Mono');}
  @font-face{font-family:'Space Grotesk';src:local('DejaVu Sans'),local('Liberation Sans');}
  @font-face{font-family:'Archivo Black';src:local('DejaVu Sans Bold'),local('Liberation Sans Bold');}
  @font-face{font-family:'Sora';src:local('DejaVu Sans'),local('Liberation Sans');}
"""

# The page under test is a template: its data comes from fetch() calls this harness cannot serve.
# Rather than let every section render an error state, unfetched requests resolve to the mock
# payload when one is supplied and to an empty object otherwise. Nothing is invented — a section
# with no mock simply renders its own empty state, which is itself worth checking.
NET_STUB = """
(function(){
  var MOCK = window.__CC_MOCK__ || {};
  function pick(url){
    for (var k in MOCK) if (String(url).indexOf(k) > -1) return MOCK[k];
    return {};
  }
  window.fetch = function(url){
    return Promise.resolve({ ok:true, status:200,
      json:function(){ return Promise.resolve(pick(url)); },
      text:function(){ return Promise.resolve(JSON.stringify(pick(url))); } });
  };
})();
"""

# Runs in the page. Returns a list of findings; an empty list is a pass.
# Emulates the device text-scaling accessibility setting. THE FIRST VERSION OF THIS WAS INERT and
# it is worth recording why, because it made the checker pass a template known to be broken:
# -webkit-text-size-adjust is a hint that governs mobile AUTO-sizing, and headless desktop Chromium
# simply ignores it, so "scale 2" changed no font at all. There is no CSS way to multiply every
# already-declared font-size, so the multiplication is done explicitly, after the page has painted,
# by walking the DOM and writing the scaled size inline. That is what an OS text-scale setting
# actually does to a page, and it is the input that turned the pivot ladder from "just fits" into
# glyph soup on the founder's phone.
TEXT_SCALE = r"""
(scale) => {
  if (!scale || Math.abs(scale - 1) < 1e-6) return 0;
  let n = 0;
  /* Read every size FIRST, then write. Writing as we walk would let a child inherit an
     already-scaled parent size and compound the factor down the tree. */
  const els = [...document.querySelectorAll('body *')];
  const sizes = els.map(el => parseFloat(getComputedStyle(el).fontSize) || 0);
  els.forEach((el, i) => {
    if (sizes[i] > 0) { el.style.fontSize = (sizes[i] * scale) + 'px'; n++; }
  });
  return n;
}
"""

PROBE = r"""
() => {
  const out = [];
  const sel = (el) => {
    if (!el) return '?';
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const c = (el.className && el.className.baseVal !== undefined)
      ? el.className.baseVal : (el.className || '');
    if (typeof c === 'string' && c.trim()) s += '.' + c.trim().split(/\s+/).slice(0, 3).join('.');
    return s;
  };
  /* A closed overlay is not a layout defect. /m/home carries full-screen popups as
     .tpo{display:none} and their children are legitimately 0x0 until opened — the first version
     reported all three of them as collapsed cells, which is a tool that cries wolf, and a tool
     that cries wolf gets ignored. Anything inside a hidden ancestor is out of scope for every
     check: it is not on screen, so it cannot overlap, clip or collapse anything a reader sees. */
  const hiddenByAncestor = (el) => {
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return true;
    }
    return false;
  };
  const vis = (el) => {
    if (hiddenByAncestor(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  /* A LEAF that actually paints text. Ancestors are skipped: a container legitimately contains
     its children's boxes, and flagging that would drown the real finding in noise. */
  const leaves = [...document.body.querySelectorAll('*')].filter(el => {
    if (/^(script|style|svg|path|line|rect|circle|polyline|br|head|meta|link)$/i.test(el.tagName)) return false;
    if (!vis(el)) return false;
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) return false;
    return true;
  });

  /* (a) OVERLAP — measured on the TEXT, not the element box. A box may overlap harmlessly
     (a badge sitting on a card); two runs of glyphs sharing pixels is the defect.

     THE TEXT BOX MUST BE CLIPPED FIRST, and getting this wrong made the checker cry wolf on a fix
     that was actually correct. A Range's client rect is the TEXT LAYOUT box: it reports where the
     glyphs would be, ignoring any ancestor with overflow:hidden that is painting over them. The
     first version compared raw range rects and so reported the cc#1127 ladder as overlapping at 2x
     text scale, when overflow:hidden on the cell means nothing outside the cell is ever painted.
     Each text box is now intersected with the client box of every clipping ancestor, so what is
     compared is what a reader can actually see. A box clipped to nothing drops out entirely. */
  const clipped = (el) => {
    const rg = document.createRange();
    rg.selectNodeContents(el);
    let r = rg.getBoundingClientRect();
    let box = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
    /* Starts at EL ITSELF, not its parent: an element carrying overflow:hidden clips its own
       contents, and that is precisely the cc#1127 guarantee — the cell clips the value inside it.
       Walking from the parent missed that and left the cell reporting text it never paints. */
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.overflow === 'visible' && cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const pr = p.getBoundingClientRect();
      if (cs.overflowX !== 'visible') {
        box.left = Math.max(box.left, pr.left);
        box.right = Math.min(box.right, pr.right);
      }
      if (cs.overflowY !== 'visible') {
        box.top = Math.max(box.top, pr.top);
        box.bottom = Math.min(box.bottom, pr.bottom);
      }
    }
    return { width: box.right - box.left, height: box.bottom - box.top,
             left: box.left, top: box.top, right: box.right, bottom: box.bottom };
  };
  const boxes = leaves.map(el => ({ el, r: clipped(el) }))
                      .filter(b => b.r.width > 1 && b.r.height > 1);

  const isAncestor = (a, b) => a.contains(b) || b.contains(a);
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const A = boxes[i], B = boxes[j];
      if (isAncestor(A.el, B.el)) continue;
      const a = A.r, b = B.r;
      const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (ox > 1 && oy > 1) {
        out.push({ check: 'overlap', detail: sel(A.el) + '  <->  ' + sel(B.el),
                   extra: Math.round(ox) + 'x' + Math.round(oy) + 'px of shared text area' });
      }
    }
  }

  /* (c) COLLAPSE — a grid or flex child with no width or no height. This is the shape the
     "column-width collapse" theory predicted; checking it means never guessing at it again. */
  for (const el of document.body.querySelectorAll('*')) {
    const p = el.parentElement;
    if (!p) continue;
    const pd = getComputedStyle(p).display;
    if (!/grid|flex/.test(pd)) continue;
    if (hiddenByAncestor(el)) continue;
    const r = el.getBoundingClientRect();
    if ((r.width < 0.5 || r.height < 0.5) && (el.textContent || '').trim()) {
      out.push({ check: 'collapse', detail: sel(el) + ' in ' + sel(p),
                 extra: Math.round(r.width) + 'x' + Math.round(r.height) + 'px but holds text' });
    }
  }

  /* (b) CLIPPING — content wider than its scroll container is FINE when the container can
     actually scroll. It is a defect when overflow is hidden/visible, because then the excess is
     either cut off silently or painted over whatever sits next to it. */
  for (const el of document.body.querySelectorAll('*')) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    const over = cs.overflowX;
    const excess = el.scrollWidth - el.clientWidth;
    if (excess > 2 && !/auto|scroll/.test(over)) {
      const kids = [...el.children].filter(k => (k.textContent || '').trim());
      if (kids.length) {
        out.push({ check: 'clipped', detail: sel(el),
                   extra: excess + 'px of content past the edge, overflow-x:' + over
                          + ' (no way to scroll to it)' });
      }
    }
  }

  /* (d) HORIZONTAL DOCUMENT OVERFLOW — the page itself must never scroll sideways. */
  const de = document.documentElement;
  if (de.scrollWidth - de.clientWidth > 1) {
    let widest = null, w = 0;
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.right > w) { w = r.right; widest = el; }
    }
    out.push({ check: 'doc-overflow',
               detail: 'document scrolls horizontally by '
                       + (de.scrollWidth - de.clientWidth) + 'px',
               extra: 'widest element ' + sel(widest) + ' reaches x=' + Math.round(w) });
  }
  return out;
}
"""


def build_page(path, html_string, mock, scale):
    """Wrap the template so it renders standalone: font shim, network stub, text scale."""
    if html_string is not None:
        body = html_string
        head = ""
    else:
        raw = open(path, encoding="utf-8").read()
        # Drop external stylesheet/script tags: they 404 here and their absence is not the defect
        # under test. Page-local <style> and <script> — where the layout actually lives — stay.
        raw = re.sub(r'<link[^>]+rel=["\']stylesheet["\'][^>]*>', "", raw, flags=re.I)
        raw = re.sub(r'<script[^>]+\bsrc=[^>]*>\s*</script>', "", raw, flags=re.I)
        head, body = raw, ""
    shim = "<style>%s</style>" % FONT_SHIM
    if head:
        if "</head>" in head:
            return head.replace("</head>", shim + "</head>", 1)
        return shim + head
    return ("<!doctype html><html><head><meta name='viewport' "
            "content='width=device-width,initial-scale=1'>" + shim + "</head><body>"
            + body + "</body></html>")


def run(path, widths, scales, shots_dir, mock, html_string, settle_ms):
    from playwright.sync_api import sync_playwright

    html = None
    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        for scale in scales:
            html = build_page(path, html_string, mock, scale)
            for w in widths:
                page = browser.new_page(viewport={"width": w, "height": 900})
                if mock:
                    page.add_init_script("window.__CC_MOCK__ = %s;" % json.dumps(mock))
                page.add_init_script(NET_STUB)
                page.set_content(html, wait_until="load")
                page.wait_for_timeout(settle_ms)
                # Scaled AFTER the page's own JS has painted, so dynamically built markup is
                # covered too — the pivot ladder and the news rows are both built by script.
                scaled = page.evaluate(TEXT_SCALE, scale)
                if scale > 1 and not scaled:
                    print("  WARN text scale %gx affected no element — check is weaker than it looks"
                          % scale)
                page.wait_for_timeout(60)
                found = page.evaluate(PROBE)
                tag = "%dpx@%sx" % (w, ("%g" % scale))
                if shots_dir:
                    os.makedirs(shots_dir, exist_ok=True)
                    name = "%s_%s.png" % (
                        re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(path or "fragment")), tag)
                    page.screenshot(path=os.path.join(shots_dir, name), full_page=True)
                    shot = name
                else:
                    shot = "(not saved)"
                if found:
                    # One line per DISTINCT finding; the same pair repeated at two scales is one
                    # defect reported twice, and saying so is more useful than a raw count.
                    print("  FAIL %-12s %s" % (tag, shot))
                    seen = set()
                    for f in found:
                        key = (f["check"], f["detail"])
                        if key in seen:
                            continue
                        seen.add(key)
                        print("       [%s] %s" % (f["check"], f["detail"]))
                        print("              %s" % f["extra"])
                        failures.append((tag, f["check"], f["detail"]))
                else:
                    print("  pass %-12s %s" % (tag, shot))
                page.close()
        browser.close()
    return failures


def main():
    ap = argparse.ArgumentParser(description="cc#1133 CC EYES — headless render check")
    ap.add_argument("template", help="path to the HTML template under test")
    ap.add_argument("--widths", default="360,412",
                    help="comma-separated CSS widths (default the founder's two devices)")
    ap.add_argument("--scale", default="1,2",
                    help="comma-separated text scales; 2 emulates Android accessibility scaling")
    ap.add_argument("--mock", help="JSON file: {url-substring: payload} served to fetch()")
    ap.add_argument("--html-string", help="check this markup instead of the file's body")
    ap.add_argument("--shots", default="", help="directory for screenshots (gitignored)")
    ap.add_argument("--settle", type=int, default=400, help="ms to wait after load")
    a = ap.parse_args()

    mock = json.load(open(a.mock, encoding="utf-8")) if a.mock else None
    widths = [int(x) for x in a.widths.split(",") if x.strip()]
    scales = [float(x) for x in a.scale.split(",") if x.strip()]

    print("CC EYES · %s" % (a.template if not a.html_string else "(markup fragment)"))
    print("  widths %s · scales %s · repo template only, NOT the live site" % (widths, scales))
    fails = run(a.template, widths, scales, a.shots or None, mock, a.html_string, a.settle)
    print()
    if fails:
        print("RENDER CHECK FAILED — %d finding(s)." % len(fails))
        print("The founder's phone is still the final gate; this is the floor, not the ceiling.")
        return 1
    print("RENDER CHECK PASSED at every width and scale.")
    print("Repo template only — pwa.js injection, the live asset chain and real webfont metrics")
    print("are NOT covered. The founder's phone remains the final gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

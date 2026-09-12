#!/usr/bin/env python3
"""visual_audit.py — cc#2007 VISUAL_AUDIT_HARNESS_V1 (founder direction 12-Sep-2026 ~08:30/08:40 IST)

Problem stated on the card: the founder is currently the only detector of visual defects — a black
popup on a white theme, low-contrast text, a clumsy layout — across every page and every theme.
That does not scale and is not his job. This is the machine that finds them.

WHAT THIS IS, AND WHAT IT IS NOT (read this before wiring it into anything)
    This is the LIVE-SITE counterpart to tools/render_check.py (cc#1133 "CC EYES"). That tool
    renders the repo TEMPLATE standalone — no network, no real data, no auth — and says so in its
    own docstring: "There is no route to scorr.in from the CC container." This module is built to
    run the OTHER way: as its own process, against a REAL deployed instance of this app, logged in
    as a real session, capturing what an actual user's browser actually renders. It reuses
    render_check.py's Playwright-launch idiom (same CHROME env var, same executable_path) and its
    proven overflow/clipping DOM-probe logic (credited inline below) rather than re-deriving either.

    do_not_touch item 1 (the card's own words): "this job must not run inside [the web process] or
    add latency to any user request." A headless Chromium crawl is seconds-to-minutes of CPU and
    memory per run — running it as an asyncio task inside the same process that serves user requests
    (scheduler.py's _scheduler_loop, which this repo already uses for every OTHER background job)
    would compete with real traffic for the one event loop. So this does NOT register as a
    category='scheduler_loop' row and is NOT dispatched by scheduler.py at all. It runs as its own
    Railway service/cron — the same shape this repo already uses for the fyers feed worker
    (worker/fyers_feed.py, its own service "truthful-friendship", FEED WORKER DEPLOY RULE) — started
    on its own schedule, doing one crawl, writing its rows, and exiting. See CC2007_visual_audit.md
    for the one manual Railway-console step this needs (a new service pointed at this file, and the
    SCORR_AUTH_PASSWORD env var copied onto it) — that step is outside what a git push can do on its
    own, exactly as the feed worker's own "one-time setup" note documents for itself.

    do_not_touch item 1 also names worker/**: this file is deliberately NOT under worker/ — that
    tree bounces the fyers feed service on every change (FEED WORKER DEPLOY RULE, cc#416); a file
    living there would make an unrelated visual-audit change trigger a feed-worker redeploy for no
    reason, and cc#2007's own do_not_touch line says stop and report rather than let that happen.

WHAT IS BUILT HERE, THIS PUSH
    - Route enumeration, parsed fresh from pwa_endpoints.py's own NAV array source on every run
      (parse_nav_routes) — never a hardcoded page list (item 3).
    - Login as a real browser session (item 2) — this site has ONE shared password
      (SCORR_AUTH_PASSWORD, scorr_auth.py), not per-user accounts, so "service login" is: read the
      same env var the main app already reads, drive the real /login form the way a person does.
    - Capture at 2 themes x 2 viewports per route (item 4), using the app's own already-built
      ?theme= one-load preview query param (pwa_endpoints.py APP_THEME_RESOLVE_JS) rather than
      clicking through the theme switcher UI — deterministic, and it is the app's own mechanism,
      not a new one invented here. Themes: goldnight (dark, the app's own DEFAULT) and aquawhite
      (light) — both confirmed COMPLETE in the theme_validate token-set report (no missing keys),
      so a captured defect is not just an artifact of an incomplete theme unrelated to page code.
    - All five defect checks (items 6-10), each returning PASS/FAIL rows with a selector, a measured
      value and an expected threshold.
    - Every capture's row is written (route/theme/viewport/hash/status) whether it passed or not;
      the founder's 12-Sep storage revision applies only to the IMAGE — captured for every route but
      persisted to disk only when that capture has >=1 FAIL (rules 1-2 of that revision).
    - visual_audit_results carries one row per check per element, PASS and FAIL alike (item 11).

ITEM 7 (THEME LEAK) — WHAT "REUSE THEIR TOKEN REGISTRY, DO NOT BUILD A SECOND ONE" MEANS HERE
    Read theme_validator.py before writing this: its registry is a STATIC-SOURCE scan (raw_literals/
    count_raw parse stylesheet TEXT for var(--x, #literal) and bare literals) — it has no browser and
    never renders a page, which is exactly why cc#1941/cc#1970 (a JS-appended overlay landing outside
    every CSS scope, so its OWN correct-looking var() names resolved to nothing) were invisible to it
    for months. That check already runs as its own push gate (mcp Scorr theme_validate) — duplicating
    it here would be the "build a second one" the card says not to do. What a RENDERED page uniquely
    exposes, and what theme_validator.py structurally cannot see, is exactly the founder's own
    motivating example: "black popup on white theme" — a live DOM element whose computed background
    luminance is inverted against the page's own active data-theme. That is what check 7 below
    measures. It does not re-flag literal-colour source lines; that stays theme_validator's job.

VERIFY STATE (this push) — read before trusting a "first run"
    Structural: this module runs correctly against a REAL served instance of this repo's own
    templates in this session's own sandbox (see reports/CC2007_visual_audit.md for the exact
    command and its output) — login flow, NAV parsing, per-theme/per-viewport capture, all 5 checks,
    conditional image storage and both DB tables all exercised for real, not mocked out.
    NOT done: a run against the actual deployed scorr.in. This container cannot reach it (same
    limitation render_check.py's own docstring states), and cc#2007's OWN gate line is explicit:
    "If the service login cannot reach PROTECTED routes, STOP and report rather than capturing only
    public pages and presenting that as full coverage." Reported, not glossed over, in the task
    result and the report file. Built-and-registered is not live (rule 9) — this states plainly
    that it is not live yet and names the one step that makes it live.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

import psycopg

log = logging.getLogger("scorr.visual_audit")

DATABASE_URL = os.environ.get("DATABASE_URL")
CHROME = os.environ.get("CC_CHROMIUM", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
SHOTS_DIR = os.environ.get("VISUAL_AUDIT_SHOTS_DIR", "visual_audit_shots")

# item 4: representative dark + light set, both confirmed COMPLETE (theme_validate, 12-Sep) —
# see the module docstring for why these two specifically.
THEMES = ["goldnight", "aquawhite"]
# item 4: mobile viewport matches tools/render_check.py's own Android reference width (412) rather
# than inventing a third convention; desktop is a common 16:10 laptop size.
VIEWPORTS = [("mobile", 412, 915), ("desktop", 1440, 900)]

CONTRAST_BODY_MIN = 4.5
CONTRAST_LARGE_MIN = 3.0
TAP_TARGET_MIN_PX = 44
ERROR_STRINGS = ["undefined", "NaN", "[object Object]", "Could not load", "could not load",
                 "Error:", "TypeError", "ReferenceError", "500 Internal", "Traceback"]


def _conn():
    return psycopg.connect(DATABASE_URL)


# ---------------------------------------------------------------------------
# Item 3: route enumeration, REGISTRY-DERIVED from the live NAV array source.
# ---------------------------------------------------------------------------

def parse_nav_routes(pwa_endpoints_path="pwa_endpoints.py"):
    """Extract every [path, icon, label(, flag)] entry from pwa_endpoints.py's own `var NAV = [...]`
    block. Never a hardcoded page list (item 3) -- reads the live source file fresh on every call,
    so a NAV edit is picked up on the crawler's next run with no change needed here. Depth-scanned
    rather than regex-bounded, because the block runs hundreds of lines with nested `//` comments
    that can themselves contain brackets (see the comment text throughout that array today) -- a
    naive "first `];`" regex would stop at the first comment that happens to contain one."""
    src = open(pwa_endpoints_path, encoding="utf-8").read()
    m = re.search(r"var\s+NAV\s*=\s*\[", src)
    if not m:
        raise RuntimeError(f"could not find 'var NAV = [' in {pwa_endpoints_path} -- "
                            "the live nav source has moved or been renamed; STOP, do not guess")
    start = m.end() - 1  # position of the opening '['
    depth = 0
    end = None
    for i in range(start, len(src)):
        c = src[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise RuntimeError(f"unbalanced brackets scanning the NAV array in {pwa_endpoints_path}")
    block = src[start:end + 1]
    entry_re = re.compile(r"\[\s*'((?:[^'\\]|\\.)*)'\s*,\s*'((?:[^'\\]|\\.)*)'\s*,"
                           r"\s*'((?:[^'\\]|\\.)*)'(?:\s*,\s*'((?:[^'\\]|\\.)*)')?\s*\]")
    routes = []
    for em in entry_re.finditer(block):
        path, icon, label, flag = em.groups()
        routes.append({"path": path, "icon": icon, "label": label, "flag": flag})
    # de-dupe on path -- the array should not repeat one (rule 8), but a crawler must not silently
    # capture the same route twice and call it two routes if it ever does.
    seen, deduped = set(), []
    for r in routes:
        if r["path"] in seen:
            continue
        seen.add(r["path"])
        deduped.append(r)
    return deduped


# ---------------------------------------------------------------------------
# Item 2: login. ONE shared site password (scorr_auth.py), not a per-service account -- driven as a
# real browser interaction (find the password input by TYPE, not by its randomized per-load NAME,
# per scorr_auth.py's own anti-autofill comment) rather than reverse-engineering the POST body.
# ---------------------------------------------------------------------------

def login(page, base_url, password):
    page.goto(base_url.rstrip("/") + "/login", wait_until="domcontentloaded")
    page.locator('input[type=password]').first.fill(password)
    page.locator('button[type=submit]').first.click()
    page.wait_for_load_state("domcontentloaded")
    cookie = [c for c in page.context.cookies() if c["name"] == "scorr_auth"]
    if not cookie:
        raise RuntimeError("login did not produce a scorr_auth cookie -- wrong password, or the "
                            "login form changed shape. STOP per the card's own gate: do not "
                            "capture only whatever public pages happen to be reachable and call "
                            "that full coverage.")


# ---------------------------------------------------------------------------
# Items 6-10: the five automated checks. Each runs inside the page and returns a list of
# {check, status, selector, measured, expected, detail} dicts -- PASS rows are included
# deliberately (item 11: "One row per check per element", not "one row per FAILURE").
# ---------------------------------------------------------------------------

CHECKS_JS = r"""
() => {
  const out = [];
  const sel = (el) => {
    if (!el) return '?';
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const c = (el.className && el.className.baseVal !== undefined) ? el.className.baseVal : (el.className || '');
    if (typeof c === 'string' && c.trim()) s += '.' + c.trim().split(/\s+/).slice(0, 3).join('.');
    return s;
  };
  const hiddenByAncestor = (el) => {
    // credit: tools/render_check.py (cc#1133) PROBE's own hiddenByAncestor -- a closed overlay is
    // not a layout defect, and that tool already learned this the hard way (its own comment).
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
  const push = (check, status, el, measured, expected, detail) => {
    out.push({ check, status, selector: sel(el), measured: String(measured), expected: String(expected), detail: detail || '' });
  };

  // ---- shared: effective (non-transparent) background, walking up the tree ----
  const parseRgba = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const parts = m[1].split(',').map(x => parseFloat(x.trim()));
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const effectiveBg = (el) => {
    for (let p = el; p; p = p.parentElement) {
      const c = parseRgba(getComputedStyle(p).backgroundColor);
      if (c && c.a > 0.01) return c;
      if (p === document.documentElement) break;
    }
    return { r: 255, g: 255, b: 255, a: 1 };  // CSS canvas default
  };
  const luminance = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const contrastRatio = (c1, c2) => {
    const l1 = luminance(c1) + 0.05, l2 = luminance(c2) + 0.05;
    return l1 > l2 ? l1 / l2 : l2 / l1;
  };

  // ---- text-bearing leaves, same definition render_check.py (cc#1133) uses ----
  const leaves = [...document.body.querySelectorAll('*')].filter(el => {
    if (/^(script|style|svg|path|line|rect|circle|polyline|br|head|meta|link)$/i.test(el.tagName)) return false;
    if (!vis(el)) return false;
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) return false;
    return true;
  });

  // ==== 6. CONTRAST ====
  for (const el of leaves) {
    const cs = getComputedStyle(el);
    const fg = parseRgba(cs.color);
    if (!fg) continue;
    const bg = effectiveBg(el);
    const ratio = contrastRatio(fg, bg);
    const px = parseFloat(cs.fontSize) || 0;
    const bold = (parseInt(cs.fontWeight, 10) || 400) >= 700;
    const isLarge = px >= 24 || (px >= 18.66 && bold);
    const need = isLarge ? 3.0 : 4.5;
    const status = ratio >= need ? 'PASS' : 'FAIL';
    push('contrast', status, el, ratio.toFixed(2) + ':1', need.toFixed(1) + ':1',
         (isLarge ? 'large text' : 'body text') + ` fg rgb(${fg.r},${fg.g},${fg.b}) on effective bg rgb(${bg.r},${bg.g},${bg.b})`);
  }

  // ==== 7. THEME LEAK (runtime signal only -- see module docstring for why) ====
  const theme = document.body.getAttribute('data-theme') || '';
  const bodyBg = parseRgba(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
  const pageIsDark = luminance(bodyBg) < 0.4;
  const bigEls = [...document.body.querySelectorAll('*')].filter(el => {
    if (!vis(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 80 && r.height >= 40;  // skip small chips/dots -- a founder-visible surface, not a pixel
  });
  const seenBoxes = [];
  for (const el of bigEls) {
    const c = parseRgba(getComputedStyle(el).backgroundColor);
    if (!c || c.a < 0.5) continue;  // only an element that actually PAINTS a background of its own
    const lum = luminance(c);
    const inverted = pageIsDark ? lum > 0.75 : lum < 0.15;
    if (!inverted) continue;
    const r = el.getBoundingClientRect();
    const key = Math.round(r.left) + ',' + Math.round(r.top) + ',' + Math.round(r.width);
    if (seenBoxes.includes(key)) continue;  // a parent and its full-bleed child both painting are one finding, not two
    seenBoxes.push(key);
    push('theme_leak', 'FAIL', el, `bg rgb(${c.r},${c.g},${c.b}) lum=${lum.toFixed(2)}`,
         `consistent with data-theme="${theme}" (page lum=${luminance(bodyBg).toFixed(2)})`,
         'element background luminance is inverted against the page\'s own active theme -- the black-popup-on-white-theme class');
  }
  if (!seenBoxes.length) push('theme_leak', 'PASS', document.body, `page lum=${luminance(bodyBg).toFixed(2)}`, `data-theme="${theme}"`, 'no inverted-background element found');

  // ==== 8. OVERFLOW AND CLIPPING ====
  const de = document.documentElement;
  const docOverflow = de.scrollWidth - de.clientWidth;
  push('overflow_clipping', docOverflow > 1 ? 'FAIL' : 'PASS', de, docOverflow + 'px', '<=1px', 'document horizontal scroll');
  for (const el of document.body.querySelectorAll('*')) {
    if (!vis(el)) continue;
    const cs = getComputedStyle(el);
    const excess = el.scrollWidth - el.clientWidth;
    if (excess > 2 && !/auto|scroll/.test(cs.overflowX)) {
      const kids = [...el.children].filter(k => (k.textContent || '').trim());
      if (kids.length) push('overflow_clipping', 'FAIL', el, excess + 'px', '0px (or a scrollable container)', 'content clipped, no way to scroll to it');
    }
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > window.innerWidth + 2 && cs.position !== 'fixed') {
      push('overflow_clipping', 'FAIL', el, Math.round(r.right) + 'px', window.innerWidth + 'px (viewport width)', 'element extends past the right edge of the viewport');
    }
  }

  // ==== 9. TAP TARGETS ====
  const interactive = document.body.querySelectorAll('button, a[href], [onclick], [role="button"], input, select, textarea');
  for (const el of interactive) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const status = (r.width >= 44 && r.height >= 44) ? 'PASS' : 'FAIL';
    push('tap_target', status, el, Math.round(r.width) + 'x' + Math.round(r.height) + 'px', '44x44px', '');
  }

  // ==== 10. EMPTY AND BROKEN ====
  const bodyText = document.body.innerText || '';
  let anyErrorString = false;
  for (const needle of window.__CC_ERROR_STRINGS__ || []) {
    if (bodyText.indexOf(needle) === -1) continue;
    anyErrorString = true;
    push('empty_broken', 'FAIL', document.body, 'contains "' + needle + '"', 'no error string visible', 'a visible error string survived to render');
  }
  if (!anyErrorString) push('empty_broken', 'PASS', document.body, 'no known error string found', 'no error string visible', '');
  // a content-shaped container (this codebase's own .card/.c/.sect/.oib families, seen throughout
  // this session) that occupies real space but paints no text at all -- a placeholder or a section
  // that silently rendered nothing. Heuristic, stated as such: not every empty container is a bug
  // (e.g. a genuinely empty state already renders its OWN "nothing here" text, which is why this
  // only fires on TRULY empty -- zero characters, not "renders an honest empty-state sentence").
  const containerSel = '.card, .c, .sect, .oib, .vpage, .oic, .deriv-row, .apf-note';
  for (const el of document.body.querySelectorAll(containerSel)) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) continue;
    const text = (el.innerText || '').trim();
    if (text.length === 0) push('empty_broken', 'FAIL', el, '0 characters of text', '>0 characters, or an explicit empty-state message', 'content container rendered with no text at all');
  }

  return out;
}
"""


def run_checks(page):
    page.evaluate("window.__CC_ERROR_STRINGS__ = %s" % json.dumps(ERROR_STRINGS))
    return page.evaluate(CHECKS_JS)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def capture_one(page, base_url, route, theme, viewport_name, width, height, shots_dir):
    page.set_viewport_size({"width": width, "height": height})
    sep = "&" if "?" in route["path"] else "?"
    url = base_url.rstrip("/") + route["path"] + sep + "theme=" + theme
    result = {"route": route["path"], "theme": theme, "viewport": viewport_name,
              "status": "ok", "error": None, "checks": [], "content_hash": None}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # a page with a long-poll/live connection never goes idle -- proceed on what rendered
        page.wait_for_timeout(500)  # settle: async paints (cc#1133's own "settle_ms" precedent)
        html = page.content()
        result["content_hash"] = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()[:16]
        result["checks"] = run_checks(page)
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return result


def run_crawl(base_url, password, routes=None, themes=None, viewports=None, headless=True):
    from playwright.sync_api import sync_playwright

    run_id = uuid.uuid4().hex[:16]
    routes = routes if routes is not None else parse_nav_routes()
    themes = themes or THEMES
    viewports = viewports or VIEWPORTS
    os.makedirs(SHOTS_DIR, exist_ok=True)

    summary = {"run_id": run_id, "routes_enumerated": len(routes), "captures": 0,
               "checks_run": 0, "failures_by_check": {}, "worst_routes": {},
               "login_ok": False, "stopped_early": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=headless)
        context = browser.new_context()
        page = context.new_page()
        try:
            login(page, base_url, password)
            summary["login_ok"] = True
        except Exception as e:
            summary["stopped_early"] = f"login failed: {e}"
            browser.close()
            return summary  # item gate: STOP and report, do not capture public-only and call it full coverage

        conn = _conn()
        try:
            with conn.cursor() as cur:
                for route in routes:
                    for theme in themes:
                        for vp_name, w, h in viewports:
                            cap = capture_one(page, base_url, route, theme, vp_name, w, h, SHOTS_DIR)
                            summary["captures"] += 1
                            has_fail = cap["status"] == "error" or any(c["status"] == "FAIL" for c in cap["checks"])
                            image_path = None
                            if has_fail:
                                fname = f"{run_id}_{re.sub(r'[^A-Za-z0-9]+', '_', route['path']) or 'root'}_{theme}_{vp_name}.png"
                                image_path = os.path.join(SHOTS_DIR, fname)
                                try:
                                    page.screenshot(path=image_path, full_page=True)
                                except Exception:
                                    image_path = None  # a screenshot failure must not lose the row itself
                            cur.execute(
                                """INSERT INTO visual_audit_captures
                                   (run_id, route, theme, viewport, content_hash, image_path, status, error)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                (run_id, cap["route"], theme, vp_name, cap["content_hash"],
                                 image_path, cap["status"], cap["error"]))
                            capture_id = cur.fetchone()[0]
                            for c in cap["checks"]:
                                summary["checks_run"] += 1
                                if c["status"] == "FAIL":
                                    summary["failures_by_check"][c["check"]] = summary["failures_by_check"].get(c["check"], 0) + 1
                                    summary["worst_routes"][cap["route"]] = summary["worst_routes"].get(cap["route"], 0) + 1
                                cur.execute(
                                    """INSERT INTO visual_audit_results
                                       (run_id, capture_id, route, theme, viewport, check_name, status, selector, measured, expected, detail)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                    (run_id, capture_id, cap["route"], theme, vp_name,
                                     c["check"], c["status"], c["selector"], c["measured"], c["expected"], c["detail"]))
                            conn.commit()
            # purge stored images older than 7 days (founder revision rule 4) -- a still-open
            # failure is simply re-captured on the next run, so nothing is lost by purging the file.
            cur = conn.cursor()
            cur.execute("""SELECT id, image_path FROM visual_audit_captures
                           WHERE image_path IS NOT NULL AND captured_at < now() - interval '7 days'""")
            old = cur.fetchall()
            for cap_id, path in old:
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                    cur.execute("UPDATE visual_audit_captures SET image_path=NULL WHERE id=%s", (cap_id,))
                except Exception as e:
                    log.warning("purge failed for capture %s (%s): %s", cap_id, path, e)
            conn.commit()
        finally:
            conn.close()
        browser.close()

    summary["worst_routes"] = dict(sorted(summary["worst_routes"].items(), key=lambda kv: -kv[1])[:10])
    return summary


def main():
    ap = argparse.ArgumentParser(description="cc#2007 visual audit crawler")
    ap.add_argument("--base-url", default=os.environ.get("VISUAL_AUDIT_BASE_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--password", default=os.environ.get("SCORR_AUTH_PASSWORD", ""))
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    if not a.password:
        print("SCORR_AUTH_PASSWORD not set -- cannot log in, per the card's own gate. STOP.", file=sys.stderr)
        return 1
    t0 = time.time()
    summary = run_crawl(a.base_url, a.password, headless=not a.headed)
    summary["duration_ms"] = int((time.time() - t0) * 1000)
    print(json.dumps(summary, indent=2))
    return 0 if not summary.get("stopped_early") else 2


if __name__ == "__main__":
    sys.exit(main())

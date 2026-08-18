"""
pwa_endpoints.py — Scorr PWA (installable mobile app) routes.
Mounted in main.py via: app.include_router(pwa_router)

Serves everything the PWA needs from the app itself (no StaticFiles mount, no
committed binary assets):
  GET /app                    -> 302 redirect to / (canonical home; cc#100)
  GET /pwa.js                 -> client bootstrap: manifest link, SW register,
                                 mobile bottom-nav, install prompt
  GET /service_worker.js      -> service worker (served from ROOT so its scope is
                                 the whole origin — offline /app fallback works)
  GET /static/manifest.json   -> web app manifest
  GET /static/icon-192.png    -> app icon (generated at runtime, pure stdlib PNG)
  GET /static/icon-512.png    -> app icon (512)

The bottom-nav + manifest link + SW registration are injected into the existing
Tier-2 pages by the auth_gate middleware in main.py (it already buffers PROTECTED
HTML to add the logout button) — so no existing page file is edited.
"""
import zlib
import struct
from fastapi import APIRouter
from fastapi.responses import Response, JSONResponse, HTMLResponse, RedirectResponse

router = APIRouter(tags=["pwa"])

# ── icon: blue (#2563eb) rounded-feel square with a white "S" (pure stdlib PNG) ──
_S_GLYPH = [
    "11111",
    "10000",
    "10000",
    "11111",
    "00001",
    "00001",
    "11111",
]
_BLUE = (37, 99, 235)
_WHITE = (255, 255, 255)
_ICON_CACHE: dict = {}


def _make_icon(size: int) -> bytes:
    pad = int(size * 0.22)
    inner = size - 2 * pad
    cols, rows = len(_S_GLYPH[0]), len(_S_GLYPH)
    cw, ch = inner / cols, inner / rows
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 (none)
        for x in range(size):
            r, g, b = _BLUE
            if pad <= x < pad + inner and pad <= y < pad + inner:
                gx, gy = int((x - pad) / cw), int((y - pad) / ch)
                if 0 <= gy < rows and 0 <= gx < cols and _S_GLYPH[gy][gx] == "1":
                    r, g, b = _WHITE
            raw += bytes((r, g, b))

    def _chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(bytes(raw), 9)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _icon(size: int) -> bytes:
    if size not in _ICON_CACHE:
        _ICON_CACHE[size] = _make_icon(size)
    return _ICON_CACHE[size]


# ── web app manifest ──
MANIFEST = {
    "name": "Scorr — Invest Like an Institution",
    "short_name": "Scorr",
    "description": "AI-powered market intelligence. GVM scores, V8 signals, Trade Check.",
    # cc#886 item 3 — DELIBERATELY STILL "/". The card offers start_url as an option alongside the
    # redirect. It is not taken, and the reason matters: this manifest is shared by phone AND
    # desktop installs, so pointing start_url at /m/home would open the retail phone app on a
    # desktop install — which item 5 forbids ("desktop untouched"). "/" plus the UA-gated redirect
    # in main.py sends phones to /m/home and leaves desktop exactly where it was. The redirect is
    # also the half that reaches ALREADY-INSTALLED WebAPKs, whose manifest is frozen at install
    # time, so it does the work start_url could not have done anyway.
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "theme_color": "#F4F7FE",    # cc#348: LIGHT is the default now (founder 09-Jul)
    "background_color": "#F4F7FE",
    "icons": [
        {"src": "/static/icon-192.png", "type": "image/png", "sizes": "192x192"},
        {"src": "/static/icon-512.png", "type": "image/png", "sizes": "512x512"},
        {"src": "/static/icon-512.png", "type": "image/png", "sizes": "512x512", "purpose": "maskable"},
    ],
}

# ── service worker (cache shell for offline /app; API always network-only) ──
# !! RULE (cc#178): ANY change to PWA_JS or SW_JS content REQUIRES bumping CACHE
#    (scorr-pwa-vN -> vN+1) in the SAME commit. The activate handler deletes every
#    cache != CACHE, so a bump is what forces installed clients (phone + desktop)
#    to drop stale shell assets on their next visit. Skipping the bump = installed
#    clients serve the old pwa.js/nav forever (root cause: #177 changed the nav
#    label to V13 but did not bump, so v2 clients never saw it).
SW_JS = """
const CACHE = 'scorr-pwa-v13';  // cc#464: /intraday nav label renamed Intraday->TC Scanner (id=2987 same-slot; cc#178 bump)
const SHELL = ['/', '/pwa.js', '/static/manifest.json',
               '/static/icon-192.png', '/static/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Live data is never cached — always go to network.
  if (url.pathname.startsWith('/api/')) return;
  // Navigations: network-first, fall back to cached /app shell when offline.
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/')));
    return;
  }
  // cc#178: /pwa.js is NETWORK-FIRST (not cache-first). It changes often — nav
  // labels/items (e.g. Filters -> V13) — and must propagate on the next normal
  // page load, not only after a cache bump. On a successful (ok) fetch we refresh
  // the cached copy so the offline fallback stays current; a non-ok response is
  // never cached (avoids pinning a bad-deploy 5xx). Offline -> serve the cache.
  if (url.pathname === '/pwa.js') {
    e.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          // waitUntil keeps the SW alive until the refresh write lands; the catch
          // swallows QuotaExceeded/put rejections so they never surface as
          // unhandled rejections (cc#178 review).
          e.waitUntil(caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {}));
        }
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }
  // Other static shell assets (icons, manifest): cache-first.
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(req).then((r) => r || fetch(req)));
  }
});
"""

# ── client bootstrap injected into every Tier-2 page (and the mobile home) ──
PWA_JS = """
(function () {
  if (window.__scorrPwa) return; window.__scorrPwa = true;
  // cc#174: embedded contexts render NO nav/chrome of their own — same signals
  // as main.py _is_embedded (iframe OR ?embed=1); only the parent shows a nav.
  var inIframe = (window.self !== window.top)
    || new URLSearchParams(location.search).get('embed') === '1';

  // 1) manifest link + theme-color (idempotent)
  if (!document.querySelector('link[rel="manifest"]')) {
    var l = document.createElement('link');
    l.rel = 'manifest'; l.href = '/static/manifest.json';
    document.head.appendChild(l);
  }
  if (!document.querySelector('meta[name="theme-color"]')) {
    var m = document.createElement('meta');
    m.name = 'theme-color'; m.content = '#F4F7FE';   // cc#348: light default; applyTheme swaps on dark
    document.head.appendChild(m);
  }

  // 2) register the service worker (root scope)
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service_worker.js').catch(function () {});
    });
  }

  // cc#336 P10: dev-only overflow detector. ?uxdebug=1 outlines every element wider
  // than the viewport in red and console.lists the offenders — makes any future mobile
  // regression one-tap visible. No-op unless the flag is present.
  if (new URLSearchParams(location.search).get('uxdebug') === '1') {
    window.addEventListener('load', function () {
      var vw = document.documentElement.clientWidth, bad = [];
      [].forEach.call(document.querySelectorAll('*'), function (el) {
        var r = el.getBoundingClientRect();
        if (r.width > vw + 1 || r.right > vw + 1) {
          el.style.outline = '2px solid red';
          bad.push((el.tagName + (el.id ? '#' + el.id : '') +
            (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/).join('.') : '')) +
            ' ' + Math.round(r.width) + 'px');
        }
      });
      if (bad.length) { console.warn('[uxdebug] ' + bad.length + ' element(s) exceed viewport ' + vw + 'px:'); bad.forEach(function (b) { console.warn('  ' + b); }); }
      else console.log('[uxdebug] clean — no horizontal overflow at ' + vw + 'px');
    });
  }

  if (inIframe) return;  // no mobile chrome inside the desktop shell iframe

  // 3) shared styles (mobile-only bottom nav + install banner)
  if (!document.getElementById('pwa-style')) {
    var css = ''
      + '.pwa-mnav{display:none}'
      + '@media(max-width:767px){'
      // cc#328: reserve bottom-nav height + iPhone home-indicator safe area
      + '  body{padding-bottom:calc(62px + env(safe-area-inset-bottom,0px))!important}'
      + '  .pwa-mnav{display:flex;position:fixed;bottom:0;left:0;right:0;'
      + '    height:calc(56px + env(safe-area-inset-bottom,0px));'
      + '    padding-bottom:env(safe-area-inset-bottom,0px);'
      + '    background:rgba(13,20,40,.92);-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);'   // cc#344: dark terminal nav
      + '    border-top:1px solid rgba(148,166,210,.24);z-index:9998}'
      + '  .pwa-mn{flex:1;display:flex;flex-direction:column;align-items:center;'
      + '    justify-content:center;gap:2px;font-size:10px;font-weight:700;'
      + '    color:var(--muted, #8C99BD);text-decoration:none;background:none;border:none;'
      + '    font-family:inherit;cursor:pointer;min-height:44px}'
      + '  .pwa-mn .ic{font-size:20px;line-height:1}'
      + '  .pwa-mn.active{color:#37D3E8}'
      // cc#328: "More" bottom-sheet — all remaining destinations, 2-col 44px rows
      + '  .pwa-sheet-ov{display:none;position:fixed;inset:0;z-index:9999;'
      + '    background:rgba(15,22,35,.45)}'
      + '  .pwa-sheet-ov.open{display:block}'
      + '  .pwa-sheet{position:fixed;left:0;right:0;bottom:0;z-index:10000;background:var(--panel, #121A33);'
      + '    border-radius:16px 16px 0 0;padding:10px 12px calc(14px + env(safe-area-inset-bottom,0px));'
      + '    transform:translateY(100%);transition:transform .22s ease;'
      + '    box-shadow:0 -4px 20px rgba(20,35,70,.18)}'
      + '  .pwa-sheet-ov.open .pwa-sheet{transform:translateY(0)}'
      + '  .pwa-sheet h4{margin:6px 4px 10px;font-size:13px;color:var(--chalk, #E9EEFB);font-weight:700}'
      + '  .pwa-sheet-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}'
      + '  .pwa-sheet-grid a{display:flex;align-items:center;gap:9px;min-height:44px;'
      + '    padding:0 12px;border:1px solid rgba(148,166,210,.14);border-radius:10px;text-decoration:none;'
      + '    color:var(--chalk, #E9EEFB);font-size:13px;font-weight:600;background:var(--panel-hi, #182241)}'
      + '  .pwa-sheet-grid a.active{border-color:#37D3E8;color:#37D3E8;background:rgba(55,211,232,.14)}'
      + '  .pwa-sheet-grid a .ic{font-size:17px;line-height:1}'
      // cc#345: theme toggle row (sun/moon)
      + '  .pwa-theme-row{display:flex;align-items:center;gap:9px;width:100%;min-height:44px;'
      + '    padding:0 12px;border:1px solid rgba(148,166,210,.14);border-radius:10px;'
      + '    background:var(--panel-hi, #182241);color:var(--chalk, #E9EEFB);font-size:13px;font-weight:600;cursor:pointer;'
      + '    font-family:inherit;text-align:left}'
      + '  .pwa-theme-row .ic{font-size:17px;line-height:1}'
      + '  .pwa-install{display:flex;position:fixed;left:0;right:0;bottom:56px;'
      + '    height:48px;align-items:center;gap:10px;padding:0 14px;z-index:9999;'
      + '    background:var(--pulse, #2563eb);color:#fff;font-size:12px;font-weight:600}'
      + '  .pwa-install button{margin-left:auto;background:#fff;color:var(--pulse, #2563eb);'
      + '    border:none;border-radius:7px;padding:6px 12px;font-size:11px;'
      + '    font-weight:700;font-family:inherit}'
      + '  .pwa-install .x{background:transparent;color:#fff;font-size:16px;'
      + '    padding:4px 6px;margin-left:4px}'
      + '}'
      // cc#365: the More-sheet overlay was only hidden inside the max-width:767px block, so on
      // desktop it had no display rule and rendered as raw flow content ("All destinations" /
      // links / "Switch to Dark") at the page bottom. Hide it on desktop too (it is only ever
      // opened via the mobile bottom-nav "More" button, which is itself desktop-hidden).
      + '@media(min-width:768px){.pwa-mnav,.pwa-install,.pwa-sheet-ov{display:none!important}}';
    var st = document.createElement('style');
    st.id = 'pwa-style'; st.textContent = css;
    document.head.appendChild(st);
  }

  // cc#328: SINGLE nav source — bottom nav (4 primary + More sheet) AND desktop
  // top-nav both build from THIS array. Pages ship only an empty #scorr-nav placeholder.
  // cc#396/#397 (nav-complete rule id=2987): single nav source for desktop top-nav + mobile
  // bottom-nav "More" sheet. V12 now -> /v12 (Basket Builder, was the /screener collision); V13 -> /v13
  // direct (was /filters redirect); old screener renamed "Screener" -> /screener; every orphan route
  // (TC Scan, Intraday, Structure, Performance, Ask) added so no page is typed-URL-only.
  var NAV = [
    ['/', '\\u2302', 'Home'],
    ['/dashboard', '\\u26a1', 'V8'],
    ['/cio2?model=gvm', '\\u25c9', 'GVM'],
    ['/sector', '\\u2297', 'Sector'],
    ['/check', '\\u2713', 'Check'],
    ['/result-corner', '\\u2263', 'Results'],
    // cc#824 (founder 02-Aug): Screeners — the 8 predefined screens as a standing destination.
    // ONE entry, added ONCE. This array renders in two form factors (desktop top bar and the mobile
    // "More" sheet), so a single entry appears in both automatically — a second entry would double
    // it, not extend it. Rebased on cc#822's trim: Digest / TC Scanner / Holdings stay removed.
    ['/screeners', '\\u2637', 'Screeners'],
    // cc#846 (founder 04-Aug): Daily Digest V3 is now a REAL PAGE (/digest), not the V8 tab it was.
    // cc#822 removed 'Digest' from this array on the explicit grounds that it was "a V8 tab, not a
    // page" — that rationale no longer holds, and rule 8 (NAV-COMPLETE, id=2987) requires a page to
    // carry a nav entry. Re-added on that basis, as ONE entry. If the founder still wants it out of
    // the bar, deleting this line is the whole change; the /digest route stays reachable either way.
    // cc#853 (founder 04-Aug): Digest REMOVED from the main nav. It now lives in the V8 tab row,
    // immediately after V6 BT (v8_dashboard.html). Exactly ONE Digest entry app-wide — this line
    // is deliberately not an entry. cc#846 had re-added it here; the founder has since placed it.
    ['/news', '\\ud83d\\udcf0', 'Intel'],
    // cc#822 (founder 02-Aug): TC Scanner and Digest REMOVED from the nav. Both are V8 tabs, not
    // pages — they stay live and reachable via the V8 tab bar and as deep links (/dashboard#tcscan,
    // /dashboard#digest). Nav removal only; no route changed, nothing retired.
    ['/v13', '\\u25a4', 'V13'],
    ['/health', '\\u2695', 'Health'],
    ['/adaptive', '\\u25f0', 'Adaptive Dashboard'],
    ['/cio', '\\u2299', 'Max'],
    ['/fpc', '\\u25e7', 'FPC'],
    ['/quant-basket', '\\u25eb', 'QB'],
    // cc#822: Holdings REMOVED from the nav. The /holdings route is UNCHANGED and still loads by
    // typed URL — this is the one entry here that is a real page rather than a tab, so it is the
    // one that becomes typed-URL-only. Recorded as such in main.py NAV_REGISTRY (rule id=2987).
    ['/v9', '\\u25c8', 'V9 \\u00b7 Pairs'],
    ['/v15', '\\u25c9', 'V15 \\u00b7 MF'],
    ['/scheduler-master', '\\u2699', 'Scheduler'],
    // cc#1086 (founder 17-Aug): the Fable Room — /room, the read-only window onto cc_task_logs.
    // ONE entry, added ONCE: this array renders as both the desktop top bar and the mobile More
    // sheet, so a single line reaches both. NAV-COMPLETE (rule 2987) requires it — the room is a
    // real page, not a tab. Mirrored in main.py NAV_REGISTRY.
    ['/room', '\\u25a6', 'Fable Room'],
    // cc#995 (founder 10-Aug): Previews REMOVED from the nav — no longer relevant. The /preview
    // route and preview_endpoints.py stay untouched (de-listed only, reachable by typed URL; this
    // is also the cc#867 rollback the note there described as "deleting this one line").
    // cc#874: promoted mobile screens. ONE source for the live nav (rule 2987) — desktop
    // top-nav and the mobile More sheet both build from this array. These are JS comments,
    // not Python: this array lives INSIDE the pwa.js string, and a '#' here is a syntax
    // error that kills the whole injected bundle site-wide (the cc#853 class).
    ['/m/intel', '\\u25a4', 'Intel (mobile)', 'm'],
    ['/m/positions', '\\u25e7', 'Open Book (mobile)', 'm'],
    ['/m/qb', '\\u25f0', 'Baskets (mobile)', 'm'],
    ['/m/gvm', '\\u25c8', 'GVM (mobile)', 'm'],
    ['/m/v8', '\\u26a1', 'V8 (mobile)', 'm'],
    ['/m/check', '\\u2713', 'Trade Check (mobile)', 'm'],
    ['/m/home', '\\u2302', 'Home (mobile)', 'm'],
    ['/m/digest', '\\u25a5', 'Daily Digest (mobile)', 'm'],
    // APP_QA_R4 P11: /m/v10 joins the nav as a first-class app page. /m/digest was already
    // here. Suffix kept as "(mobile)" to match every other /m/ entry in this array — the
    // package names the label "Index Intel", and that is the label the APP shows; this array
    // is the DESKTOP navbar, where the suffix is what distinguishes it from the web page.
    ['/m/v10', '\\u25d0', 'Index Intel (mobile)', 'm'],
    ['/m/results', '\\u25f7', 'Results (mobile)', 'm']
    // cc#995 (founder 10-Aug): Models (mobile) REMOVED from the nav. The /m/models route and the
    // ScorrModels overlay stay (de-listed only, reachable by typed URL).
  ];
  var p = location.pathname, qs = location.search;
  function isActive(route) {
    var base = route.split('?')[0];
    if (route.indexOf('model=gvm') > -1) return p === '/cio2' && qs.indexOf('model=gvm') > -1;
    if (base === '/') return p === '/';
    return p === base || p.indexOf(base + '/') === 0;   // prefix match: sub-views highlight
  }
  function navByPath(pp) { for (var i = 0; i < NAV.length; i++) { if (NAV[i][0] === pp) return NAV[i]; } return null; }

  // cc#882 — ONE array, TWO form factors. Optional 4th element: 'm' = mobile-only,
  // 'd' = desktop-only, absent = both. Backwards compatible, so every existing 3-element entry
  // keeps rendering in both places untouched.
  // Why this exists: cc#874 added its promoted /m/* retail screens as plain entries, and this
  // array renders in BOTH the desktop top bar and the mobile More sheet — so "Intel (mobile)"
  // landed on the professional web nav. Under 16915 (web = professional, app = retail subset) a
  // retail screen must never sit on the professional bar. The flag FILTERS RENDERING; it never
  // forks the array, so rule 2987's single source is preserved.
  function forDesktop(it) { return it[3] !== 'm'; }
  function forMobile(it)  { return it[3] !== 'd'; }

  // 4) mobile bottom nav — 4 primary slots + More (opens all-destinations sheet)
  // cc#886: THE SECOND HALF OF THE ROOT CAUSE. cc#874 wired eleven /m/* screens; this bar still
  // pointed at the OLD web pages (/, /dashboard, /cio2, /check), so even a founder who reached a
  // new screen was one tap from the old product. Repointed to the framework four (15913); slot 5
  // is the More sheet, and Models sits INSIDE the app's own five-slot bar on every /m/* template.
  // The sheet is kept rather than replaced by a Models button, because it is what guarantees
  // nothing is stranded (rule 2987) on the legacy pages this bar renders on.
  var PRIMARY = ['/m/home', '/m/gvm', '/m/check', '/m/intel'];
  if (!document.getElementById('pwa-mobile-nav')) {
    var nav = document.createElement('div');
    nav.className = 'pwa-mnav'; nav.id = 'pwa-mobile-nav';
    var mhtml = '';
    // cc#886: the NAV labels carry a "(mobile)" suffix so the More sheet can tell the app screen
    // apart from its web twin. A bottom-bar slot is ~60px wide and has no room for it — and the
    // founder does not need to be told he is on mobile while holding the phone. One label, two
    // renderings; the array is still the single source (rule 2987).
    function shortLabel(s) { return String(s).replace(/\\s*\\(mobile\\)\\s*$/i, ''); }
    PRIMARY.forEach(function (pp) {
      var it = navByPath(pp); if (!it) return;
      var active = isActive(it[0]) ? ' active' : '';
      mhtml += '<a class="pwa-mn' + active + '" href="' + it[0] + '">'
        + '<span class="ic">' + it[1] + '</span><span>' + shortLabel(it[2]) + '</span></a>';
    });
    var inSheet = NAV.some(function (it) { return PRIMARY.indexOf(it[0]) === -1 && isActive(it[0]); });
    mhtml += '<button type="button" class="pwa-mn' + (inSheet ? ' active' : '') + '" id="pwa-more-btn">'
      + '<span class="ic">\\u2261</span><span>More</span></button>';
    nav.innerHTML = mhtml;
    document.body.appendChild(nav);

    // one-time sheet: every remaining destination + Logout
    var ov = document.createElement('div');
    ov.className = 'pwa-sheet-ov'; ov.id = 'pwa-sheet-ov';
    var rows = NAV.filter(function (it) { return PRIMARY.indexOf(it[0]) === -1 && forMobile(it); })   // cc#882
      .map(function (it) {
        return '<a class="' + (isActive(it[0]) ? 'active' : '') + '" href="' + it[0] + '">'
          + '<span class="ic">' + it[1] + '</span>' + it[2] + '</a>';
      }).join('');
    rows += '<a href="/logout"><span class="ic">\\u23cf</span>Logout</a>';
    ov.innerHTML = '<div class="pwa-sheet"><h4>All destinations</h4>'
      + '<div class="pwa-sheet-grid">' + rows + '</div>'
      + '<h4 style="margin-top:14px">Settings</h4>'
      + '<button id="pwa-theme-toggle" class="pwa-theme-row" type="button">'
      + '<span class="ic" id="pwa-theme-ic"></span><span id="pwa-theme-lbl"></span></button>'
      + '</div>';
    document.body.appendChild(ov);
    document.getElementById('pwa-more-btn').addEventListener('click', function () { ov.classList.add('open'); });
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.classList.remove('open'); });

    // cc#345: theme toggle — dark default (brand identity), user pick persisted forever.
    function curTheme() { try { return localStorage.getItem('scorr_theme') || 'light'; } catch (e) { return 'light'; } }
    function syncThemeBtn() {
      var t = curTheme(), ic = document.getElementById('pwa-theme-ic'), lbl = document.getElementById('pwa-theme-lbl');
      if (ic) ic.innerHTML = (t === 'light') ? '\\u263e' : '\\u2600';         // moon (->dark) / sun (->light)
      if (lbl) lbl.textContent = (t === 'light') ? 'Switch to Dark' : 'Switch to Light';
    }
    function applyTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      try { localStorage.setItem('scorr_theme', t); } catch (e) {}
      var m = document.querySelector('meta[name="theme-color"]');
      if (m) m.content = (t === 'light') ? '#F4F7FE' : '#0A0F1E';
      syncThemeBtn();
    }
    document.getElementById('pwa-theme-toggle').addEventListener('click', function () {
      // cc#348: reload so EVERY page (CSS-var, React GVM, hardcoded) renders in the new theme.
      var t = curTheme() === 'light' ? 'dark' : 'light';
      try { localStorage.setItem('scorr_theme', t); } catch (e) {}
      location.reload();
    });
    applyTheme(curTheme());   // sync meta + button to the theme the head script already applied
  }

  // ── cc#860 MODEL LAUNCHER — ONE shared component, both surfaces ─────────────────────────────
  // Built once here and injected by pwa.js, which runs on every injected page. Desktop opens it
  // from a header button; mobile opens it from the nav. Same DOM, same fetch, same renderer —
  // duplicating it per surface is a spec violation under 9016 pattern 2 / framework core rule 8.
  //
  // The badge is computed SERVER-SIDE (model_launcher.badge_for) and rendered here verbatim. No
  // liveness logic lives in JS, so the launcher and any other consumer of /api/models/status can
  // never disagree about whether a model is running.
  (function () {
    if (document.getElementById('scorr-ml-ov')) return;

    var ov = document.createElement('div');
    ov.className = 'scorr-ml-ov'; ov.id = 'scorr-ml-ov';
    ov.innerHTML = '<div class="scorr-ml" role="dialog" aria-modal="true" aria-label="Models">'
      + '<div class="scorr-ml-hd"><h4>Models</h4>'
      + '<span class="scorr-ml-sub" id="scorr-ml-sub"></span>'
      + '<button class="scorr-ml-x" id="scorr-ml-x" aria-label="Close">×</button></div>'
      + '<div class="scorr-ml-grid" id="scorr-ml-grid">'
      + '<div class="scorr-ml-skel"></div><div class="scorr-ml-skel"></div>'
      + '<div class="scorr-ml-skel"></div><div class="scorr-ml-skel"></div></div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function (e) { if (e.target === ov) closeML(); });
    document.getElementById('scorr-ml-x').addEventListener('click', closeML);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeML(); });

    function closeML() { ov.classList.remove('open'); }

    function esc(t) {
      return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; });
    }

    function card(m) {
      // STALE differs from LIVE by SHAPE, not opacity (card item 9 / framework core rule 10):
      // LIVE is a filled pulsing dot, STALE a hollow ring, OFF a flat bar. A colour-blind or
      // glancing user must never read a dead engine as a running one.
      var st = m.state, cls = 'ml-' + st.toLowerCase();
      var glyph = st === 'LIVE' ? '<span class="ml-dot"></span>'
                : st === 'STALE' ? '<span class="ml-ring"></span>'
                : '<span class="ml-bar"></span>';
      // STALE always states its own age — "stale" without a number is not actionable.
      var sub = (st === 'STALE') ? esc(m.state_reason || m.last_run_human)
              : (st === 'OFF')   ? esc(m.state_reason || 'not running')
              : esc(m.last_run_human || '');
      return '<a class="scorr-ml-card ' + cls + '" href="' + esc(m.route) + '">'
        + '<div class="ml-top"><span class="ml-name">' + esc(m.display_name) + '</span>'
        + '<span class="ml-badge ' + cls + '">' + glyph + st + '</span></div>'
        + (m.description ? '<div class="ml-desc">' + esc(m.description) + '</div>' : '')
        + '<div class="ml-foot">' + sub + '</div></a>';
    }

    function load() {
      fetch('/api/models/status', {cache: 'no-store'})
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var g = document.getElementById('scorr-ml-grid');
          var ms = (d && d.models) || [];
          if (!ms.length) {
            g.innerHTML = '<div class="scorr-ml-empty">'
              + esc((d && d.error) || 'No models registered.') + '</div>';
            return;
          }
          g.innerHTML = ms.map(card).join('');
          var c = d.counts || {};
          var sub = document.getElementById('scorr-ml-sub');
          if (sub) sub.textContent = (c.LIVE || 0) + ' live'
            + ((c.STALE || 0) ? ' · ' + c.STALE + ' stale' : '')
            + ((c.OFF || 0) ? ' · ' + c.OFF + ' off' : '')
            + (d.in_session ? '' : ' · market closed');
        })
        .catch(function () {
          document.getElementById('scorr-ml-grid').innerHTML =
            '<div class="scorr-ml-empty">Could not load model status.</div>';
        });
    }

    // Exposed so any surface can open it — this IS the "one button" the card asks for.
    window.ScorrModels = { open: function () { ov.classList.add('open'); load(); }, close: closeML };
  })();

  // 6) canonical desktop top-nav — single source of truth (cc_task #80, spec 637; nav registry
  //    updated cc#396/#397): normalize #scorr-nav on every injected page from the NAV array above,
  //    active-by-path. Removes per-page nav drift. Scrolls horizontally; hidden on mobile (bottom nav).
  if (!document.getElementById('scorr-cnav-style')) {
    var ncss = ''
      // cc#348: desktop top-nav is theme-aware and self-contained (dark base + light override),
      // so it never disagrees with the page it sits on.
      // cc#438: compact single-row nav that fits ONE screen width (no horizontal scroll needed on
      // standard desktops). Tighter gap/padding/font + smaller icons; items may shrink slightly so
      // the full set fits. overflow-x kept (scrollbar hidden) only as a safety net on very narrow
      // desktop widths; on normal widths the compacted row fits with room to spare.
      + '.scorr-cnav{display:flex;align-items:center;gap:0;height:44px;background:var(--panel, #121A33);'
      + '  border-bottom:1px solid rgba(148,166,210,.14);padding:0 8px;overflow-x:auto;scrollbar-width:none;'
      + '  position:sticky;top:0;z-index:40;box-shadow:0 1px 4px rgba(3,7,20,.4)}'
      + '.scorr-cnav::-webkit-scrollbar{display:none}'
      + '.scorr-cnav a{display:flex;align-items:center;gap:4px;padding:0 7px;height:44px;min-width:0;'
      + '  text-decoration:none;white-space:nowrap;flex-shrink:1;color:var(--muted, #8C99BD);font-size:10.5px;'
      + '  font-weight:600;border-bottom:2px solid transparent;transition:.12s}'
      + '.scorr-cnav a:hover{color:var(--chalk, #E9EEFB)}'
      + '.scorr-cnav a.active{border-bottom-color:var(--pulse, #4D7CFE);color:var(--pulse, #4D7CFE)}'
      + '.scorr-cnav a .ic{font-size:11px;flex-shrink:0}'
      + '.scorr-cnav .sep{width:1px;height:18px;background:rgba(148,166,210,.14);flex-shrink:0;margin:0 2px}'
      + ':root[data-theme="light"] .scorr-cnav{background:#FFFFFF;border-bottom-color:rgba(20,35,80,.1);box-shadow:0 1px 4px rgba(20,35,70,.06)}'
      + ':root[data-theme="light"] .scorr-cnav a{color:#5B6B94}'
      + ':root[data-theme="light"] .scorr-cnav a:hover{color:#0E1630}'
      + ':root[data-theme="light"] .scorr-cnav a.active{border-bottom-color:#3D6BEC;color:#3D6BEC}'
      + ':root[data-theme="light"] .scorr-cnav .sep{background:rgba(20,35,80,.1)}'
      + '@media(max-width:767px){.scorr-cnav{display:none!important}}';
    var nst = document.createElement('style');
    nst.id = 'scorr-cnav-style'; nst.textContent = ncss;
    document.head.appendChild(nst);
  }
  (function () {
    // cc#328: same NAV + isActive as the bottom nav (single source, defined above)
    var host = document.getElementById('scorr-nav');
    if (!host) {
      host = document.createElement('nav'); host.id = 'scorr-nav';
      document.body.insertBefore(host, document.body.firstChild);
    }
    host.className = 'scorr-cnav';
    host.innerHTML = NAV.filter(forDesktop).map(function (it, i) {   // cc#882: no retail /m/* on the professional bar
      var sep = i ? '<span class="sep"></span>' : '';
      var act = isActive(it[0]);
      return sep + '<a' + (act ? ' class="active"' : '') + ' href="' + it[0] + '">'
        + '<span class="ic">' + it[1] + '</span>' + it[2] + '</a>';
    }).join('')
    // cc#860 item 6: launcher button, header RIGHT. Deliberately appended AFTER the NAV map and
    // NOT added to the NAV array — item 7 requires the desktop top-nav itself to be unchanged, and
    // a NAV entry would also duplicate it into the mobile More sheet.
    + '';
    // cc#995 (founder 10-Aug): the far-right "Models" launcher button is REMOVED. It was appended
    // HERE by pwa.js, OUTSIDE the NAV array (the cc#860 append above) \u2014 so THIS was the web "Models"
    // the founder pointed at, not a NAV-array entry. The ScorrModels overlay, /api/models/status and
    // the /m/models route all stay untouched; only the nav button is gone.
  })();

  // 5) install prompt
  if (localStorage.getItem('scorr_pwa_dismissed')) return;

  function banner(html, onAdd) {
    if (document.getElementById('pwa-install-banner')) return;
    var b = document.createElement('div');
    b.className = 'pwa-install'; b.id = 'pwa-install-banner';
    b.innerHTML = '<span>' + html + '</span>';
    var add = document.createElement('button'); add.textContent = 'Add to Home Screen';
    var x = document.createElement('button'); x.className = 'x'; x.innerHTML = '\\u00d7';
    if (onAdd) b.appendChild(add); b.appendChild(x);
    add.onclick = function () { if (onAdd) onAdd(); };
    x.onclick = function () {
      localStorage.setItem('scorr_pwa_dismissed', '1');
      if (b.parentNode) b.parentNode.removeChild(b);
    };
    document.body.appendChild(b);
  }

  var deferred = null;
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault(); deferred = e;
    setTimeout(function () {
      banner('Add Scorr to your home screen for instant access', function () {
        if (!deferred) return;
        deferred.prompt();
        deferred.userChoice.then(function () {
          localStorage.setItem('scorr_pwa_dismissed', '1');
          var el = document.getElementById('pwa-install-banner');
          if (el && el.parentNode) el.parentNode.removeChild(el);
          deferred = null;
        });
      });
    }, 3000);
  });

  // iOS Safari has no beforeinstallprompt — show a one-time hint instead.
  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
  var standalone = ('standalone' in navigator) && navigator.standalone;
  if (isIOS && !standalone) {
    setTimeout(function () {
      banner('Install Scorr: tap Share \\u2191 then "Add to Home Screen"', null);
    }, 3000);
  }
})();
"""

# ── nav hide/show toggle (cc_task #118) — shared across all pages ──
# Persists collapse state in localStorage (key: scorr_nav_hidden). Collapse is
# driven by an attribute on <html> + head CSS rather than a class on the nav,
# so it survives pwa.js rebuilding #scorr-nav (innerHTML + className overwrite).
# A toggle "Hide" pill sits at the right of the nav; when hidden, a thin 20px
# sticky strip with a "Show" pill stays at the top so the nav is always
# recoverable. The button is re-added via MutationObserver if a rebuild wipes it.
NAV_TOGGLE_JS = """
(function () {
  if (window.__scorrNavToggle) return; window.__scorrNavToggle = true;
  // cc#311: model-nav is HIDDEN BY DEFAULT on every browser open / login. "Show" is remembered
  // only for the CURRENT tab session (sessionStorage) — so a fresh browser or new tab always
  // starts hidden and the user reveals the nav via the button when needed; the choice still
  // persists across in-session page navigations and reloads. The old localStorage flag (which
  // wrongly kept the nav shown across browser restarts) is retired on load.
  var SKEY = 'scorr_nav_shown';
  var HID = 'data-scorr-nav-hidden';
  var root = document.documentElement;
  try { localStorage.removeItem('scorr_nav_hidden'); } catch (e) {}

  function isHidden() {
    try { return sessionStorage.getItem(SKEY) !== '1'; } catch (e) { return true; }
  }

  function injectStyle() {
    if (document.getElementById('scorr-navtoggle-style')) return;
    var css = ''
      + '#scorr-nav, .model-nav{transition:max-height .2s ease, opacity .2s ease}'
      + 'html[' + HID + '] #scorr-nav, html[' + HID + '] .model-nav{'
      + '  max-height:0!important;opacity:0!important;overflow:hidden!important;'
      + '  border:none!important}'
      + '#scorr-nav-strip{position:sticky;top:0;z-index:20;display:none;height:20px;'
      + '  align-items:center;justify-content:flex-end;padding:0 12px;background:var(--pulse, #2563eb)}'
      + 'html[' + HID + '] #scorr-nav-strip{display:flex}'
      + '.scorr-nav-btn{font-size:11px;padding:4px 10px;border-radius:10px;'
      + '  background:rgba(255,255,255,0.15);color:inherit;cursor:pointer;border:none;'
      + '  margin-left:auto;flex-shrink:0}'
      + '#scorr-nav-strip .scorr-nav-btn{color:#fff}'
      // cc#126: toggle button is fixed top-right (NOT inline in the nav) so it stays
      // visible no matter how wide the nav grows or whether the nav is hidden.
      // cc#433: stacked below the Logout (top:64) + theme (top:102) pills, all now BELOW the
      // 46px sticky navbar so none overlap the nav tabs; semi-transparent idle -> solid on hover.
      + '#scorr-nav-toggle-btn{position:fixed;top:140px;right:14px;z-index:9999;'
      + '  background:var(--pulse, #2563eb);color:#fff;font-size:11px;font-weight:600;'
      + '  padding:4px 12px;border-radius:12px;border:none;cursor:pointer;margin:0;'
      + '  box-shadow:0 1px 4px rgba(0,0,0,0.25);opacity:.45;transition:opacity .15s}'
      + '#scorr-nav-toggle-btn:hover{opacity:1}'
      // cc#328: the top-nav is display:none at <=767px (bottom nav is used there),
      // so the Show/Hide pill + reveal strip are dead controls on mobile — kill them.
      + '@media(max-width:767px){#scorr-nav-toggle-btn,#scorr-nav-strip{display:none!important}}';
    var st = document.createElement('style');
    st.id = 'scorr-navtoggle-style'; st.textContent = css;
    (document.head || root).appendChild(st);
  }

  injectStyle();
  if (isHidden()) root.setAttribute(HID, '');   // apply immediately — no flash

  function findNav() {
    return document.getElementById('scorr-nav') || document.querySelector('.model-nav');
  }

  function sync() {
    var b = document.getElementById('scorr-nav-toggle-btn');
    if (b) b.textContent = isHidden() ? 'Show' : 'Hide';
  }

  function setHidden(h) {
    try { if (h) sessionStorage.removeItem(SKEY); else sessionStorage.setItem(SKEY, '1'); } catch (e) {}
    if (h) root.setAttribute(HID, ''); else root.removeAttribute(HID);
    sync();
  }

  function build() {
    var nav = findNav();
    if (!nav) return;
    if (!document.getElementById('scorr-nav-toggle-btn')) {
      var btn = document.createElement('button');
      btn.id = 'scorr-nav-toggle-btn'; btn.className = 'scorr-nav-btn';
      btn.type = 'button';
      btn.addEventListener('click', function () { setHidden(!isHidden()); });
      // cc#126: append to body (fixed-positioned) so a wide/hidden nav can't bury it.
      document.body.appendChild(btn);
    }
    if (!document.getElementById('scorr-nav-strip')) {
      var strip = document.createElement('div');
      strip.id = 'scorr-nav-strip';
      var sb = document.createElement('button');
      sb.className = 'scorr-nav-btn'; sb.type = 'button'; sb.textContent = 'Show';
      sb.addEventListener('click', function () { setHidden(false); });
      strip.appendChild(sb);
      nav.parentNode.insertBefore(strip, nav);
    }
    sync();
  }

  function boot() {
    build();
    var nav = findNav();
    if (nav && window.MutationObserver) {
      // pwa.js may rebuild #scorr-nav (innerHTML); re-add the toggle if wiped.
      var mo = new MutationObserver(function () {
        if (!document.getElementById('scorr-nav-toggle-btn')) build();
      });
      mo.observe(nav, {childList: true});
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
"""

_NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}
_CACHE_1D = {"Cache-Control": "public, max-age=86400"}   # cc#712: static assets cache 1 day

# ── cc#327 MOBILE_UX_REDEFINE_V1 P1/10 — shared mobile design system ──────────
# Served at /static/mobile.css and injected site-wide via the auth_gate middleware
# in main.py (same path as the PWA bootstrap, so no protected page is missed).
# Every later mobile-UX task (cc#328-336) consumes these tokens + utilities.
MOBILE_CSS = """/* ==========================================================================
   MOBILE_UX_STANDARD_V1  —  cc#327 (program MOBILE_UX_REDEFINE_V1)
   Single shared mobile design system. Do NOT re-declare these per page.
   --------------------------------------------------------------------------
   BREAKPOINTS (use EVERYWHERE — the ad-hoc 560px query is retired):
       <=480px  phone
       <=767px  large phone / small tablet  (primary mobile target)
       >=768px  desktop  (all mobile rules OFF)
   TAP TARGETS : every interactive control >= 44x44px at <=767px
   TYPE FLOOR  : no text < 11px; body >= 13px; ALL inputs/selects >= 16px
                 (16px is what stops iOS from auto-zooming on focus)
   SAFE AREAS  : body reserves bottom-nav height (56px) + env(safe-area-inset-*)
   FONT        : one canonical stack site-wide — Sora + system fallback
   UTILITIES   : .hscroll  .hscroll-fade  .sticky-col  .tap44  .stack-480
   TABLE       : .mtable pattern (cc#330 P4) — .mtable-wrap edge fades + sticky first
                 column + data-pri 1|2|3 column priority; mobile_tables.js wires the
                 fade toggling and row-tap expand-all-fields
   DRAWER      : mobile filter panels collapse behind a 44px summary bar (cc#335 P9)
   OVERFLOW    : append ?uxdebug=1 to any URL to outline elements wider than the
                 viewport in red + console-list them (cc#336 P10 guardrail)
   ------------------------------------------------------------------------------
   LOCKED CONTRACT (MOBILE_UX_STANDARD_V1, cc#336 final): every future UI task MUST
   satisfy the above — breakpoints, 44px tap targets, 11px/16px type floors,
   safe-area insets, the .mtable table pattern, and zero horizontal body overflow.
   ========================================================================== */
:root{
  --mux-bp-phone:480px;
  --mux-bp-mobile:767px;
  --mux-font:'Sora',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mux-nav-h:56px;
  --mux-tap:44px;
}

/* canonical font site-wide */
body{font-family:var(--mux-font);}

/* ---- utility classes (available at every width) ---- */
.tap44{min-width:var(--mux-tap);min-height:var(--mux-tap);}
.hscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:thin;}
.hscroll::-webkit-scrollbar{height:5px;}
.hscroll::-webkit-scrollbar-thumb{background:rgba(120,130,150,.4);border-radius:3px;}
/* right-edge fade hints there is more content to scroll to */
.hscroll-fade{position:relative;}
.hscroll-fade::after{content:'';position:absolute;top:0;right:0;bottom:0;width:26px;
  pointer-events:none;background:linear-gradient(to right,rgba(255,255,255,0),rgba(255,255,255,.92));}
.sticky-col{position:sticky;left:0;z-index:2;background:inherit;
  box-shadow:2px 0 5px -3px rgba(0,0,0,.25);}

/* ---- mobile rules (<=767px) ---- */
@media(max-width:767px){
  html{-webkit-text-size-adjust:100%;}
  body{font-size:13px;
    padding-bottom:calc(var(--mux-nav-h) + env(safe-area-inset-bottom,0px));}
  /* inputs never below 16px -> kills iOS focus auto-zoom (.fnum was 11px) */
  input,select,textarea,.fnum{font-size:16px !important;}
  /* tap targets — interactive controls reach 44px */
  button,a.btn,.btn,.chip,.tab,.toggle,select,
  th[onclick],[role=button],.week-nav button,.book-toggle{min-height:var(--mux-tap);}
  button,a.btn,.btn,.chip{min-width:var(--mux-tap);}
  input[type=checkbox],input[type=radio]{min-height:auto;}
  /* sticky top elements respect the notch */
  .sticky-top,.topbar,header.sticky{padding-top:env(safe-area-inset-top,0px);}
}
@media(max-width:480px){
  .stack-480{display:grid !important;grid-template-columns:1fr !important;gap:8px;}
}

/* ==========================================================================
   cc#330 P4 — shared mobile TABLE pattern (.mtable). Pages adopt by markup:
     <div class="tw mtable-wrap"><table class="mtable"> ... </table></div>
     mark each <th>/<td> with data-pri 1|2|3 (col 1 = identity, never hidden).
   mobile_tables.js wires: edge-fade affordances, row-tap expand-all-fields.
   ========================================================================== */
.mtable-wrap{position:relative;overflow-x:auto;-webkit-overflow-scrolling:touch;}
.mtable-wrap::-webkit-scrollbar{height:5px}
.mtable-wrap::-webkit-scrollbar-thumb{background:rgba(120,130,150,.4);border-radius:3px}
/* left/right edge fades — JS toggles .can-l/.can-r when scrollable that way */
.mtable-wrap::before,.mtable-wrap::after{content:'';position:absolute;top:0;bottom:0;width:22px;
  pointer-events:none;z-index:5;opacity:0;transition:opacity .15s}
.mtable-wrap::before{left:0;background:linear-gradient(to right,rgba(255,255,255,.96),rgba(255,255,255,0))}
.mtable-wrap::after{right:0;background:linear-gradient(to left,rgba(255,255,255,.96),rgba(255,255,255,0))}
.mtable-wrap.can-l::before{opacity:1}
.mtable-wrap.can-r::after{opacity:1}
/* first column sticky so the row keeps its identity while scrolling */
.mtable th:first-child,.mtable td:first-child{position:sticky;left:0;z-index:2;
  background:var(--mtable-bg,#fff);box-shadow:2px 0 5px -3px rgba(0,0,0,.25)}
.mtable thead th{position:sticky;top:0;z-index:3;background:var(--mtable-bg,#fff)}
.mtable thead th:first-child{z-index:4}
/* JS-injected expand row: every field as label:value */
.mtable-detail>td{background:#f6f8fb;padding:10px 12px}
.mtable-detail dl{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:0;font-size:12.5px}
.mtable-detail dt{color:var(--muted, #5a6781);font-weight:600;white-space:nowrap}
.mtable-detail dd{margin:0;font-weight:700;text-align:right}
.mtable .mchev{display:none;color:var(--muted, #94a3b8);transition:transform .15s}
@media(max-width:767px){
  .mtable th,.mtable td{white-space:nowrap}
  .mtable thead th{height:44px;font-size:12px;vertical-align:middle}
  .mtable thead th .arrow{font-size:12px}
  .mtable tr.mrow-exp{cursor:pointer}
  .mtable .mchev{display:inline-block;margin-left:5px}
  .mtable tr.mrow-exp.open .mchev{transform:rotate(90deg)}
}
@media(max-width:480px){ .mtable [data-pri="3"]{display:none} }
@media(max-width:390px){ .mtable [data-pri="2"]{display:none} }

/* ==========================================================================
   DARK TERMINAL DESIGN SYSTEM  —  cc#344 (SCORR_FULL_APP_REDESIGN, ref R2)
   The shared visual layer held back in cc#327. Dark institutional terminal:
   navy ink, Sora display + IBM Plex Mono numerals, glow-edge state segments,
   capacity bars, LIVE/STALE feed chip, pulsing CMP dots. Extracted verbatim
   from design_refs/scorr_v8_R2.html so every page re-skin consumes ONE source.
   SCOPED to `.dt` (a re-skinned page adds class="dt" to <body>) so the pages
   not yet converted keep their current theme — zero global bleed.
   ========================================================================== */
.dt{
  --ink:#0A0F1E; --surface:#121A33; --surface2:#182241; --well:#0D1428;
  --line:rgba(148,166,210,.14); --line2:rgba(148,166,210,.24);
  --text:#E9EEFB; --mut:#8C99BD; --dim:#5E6B8F;
  --bull:#2FD48B; --bull-soft:rgba(47,212,139,.14);
  --bear:#FF5C6C; --bear-soft:rgba(255,92,108,.13);
  --amber:#F5B94A; --amber-soft:rgba(245,185,74,.14);
  --blue:#4D7CFE; --cyan:#37D3E8;
  --disp:'Sora',system-ui,sans-serif; --mono:'IBM Plex Mono',ui-monospace,monospace;
  --r:18px;
  background:var(--ink); color:var(--text); font-family:var(--disp);
  -webkit-font-smoothing:antialiased;
}
.dt .num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
/* ── cc#859 Part A · SHARED MOBILE SECTION CARD ─────────────────────────────────────────────
   Structure only. The design ref (R2, cc#861) governs the finish; these rules implement the
   grammar already written in MOBILE_APP_FRAMEWORK_V1 (15913) and cc#859 scope. Dark per 15913.
   Scoped to <=767px so DESKTOP IS UNTOUCHED — cc#859 V6 requires zero desktop change, and the
   existing nav CSS already uses this 767/768 split, so there is no 1px gap between them. */
@media(max-width:767px){
  .smc-stack{display:flex;flex-direction:column;gap:8px}   /* core rule 5: 8px between targets */
  .smc{background:var(--panel, #0E1526);border:1px solid var(--edge, #1E2A44);border-radius:10px;overflow:hidden;
    position:relative}
  /* core rule 9 — THE STATE RAIL. 3px left edge; reading the rails down the stack tells you the
     book without opening anything. Live cards breathe. */
  .smc:before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:#334155}
  .smc-state-live:before{background:#34D399;animation:smcBreathe 2.4s ease-in-out infinite}
  .smc-state-stale:before{background:var(--amber, #FBBF24)}
  .smc-state-off:before{background:#475569}
  @keyframes smcBreathe{0%,100%{opacity:1}50%{opacity:.45}}

  /* core rule 4 header contract + core rule 5 tap targets: 44px min, full-width tappable */
  .smc-hd{display:flex;align-items:center;gap:9px;width:100%;min-height:44px;padding:11px 13px;
    background:none;border:0;text-align:left;cursor:pointer;color:inherit;font:inherit}
  .smc-primary .smc-hd{cursor:default;padding-bottom:4px}
  .smc-title{font:600 10.5px Sora,sans-serif;letter-spacing:.14em;text-transform:uppercase;
    color:var(--muted, #64748B);flex:none}
  /* numerals: IBM Plex Mono, >=13px on mobile (cc#859 item 6) */
  .smc-val{margin-left:auto;font:600 13px 'IBM Plex Mono',monospace;color:#E6EDF7;
    font-variant-numeric:tabular-nums;white-space:nowrap}
  .smc-primary .smc-val{font-size:30px;font-weight:600;letter-spacing:-.01em}
  .smc-chev{flex:none;color:var(--muted, #64748B);font-size:17px;line-height:1;transition:transform .16s;
    min-width:22px;text-align:right}
  .smc.open .smc-chev{transform:rotate(90deg)}

  /* collapse: PRIMARY is always expanded and has no chevron (cc#859 item 3) */
  .smc-body{display:none;padding:0 13px 13px}
  .smc.open .smc-body,.smc-primary .smc-body{display:block}

  /* core rule 10 — LIVE/STALE/OFF differ by SHAPE, not opacity */
  .smc-chip{display:inline-flex;align-items:center;gap:5px;flex:none;
    font:700 8.5px 'IBM Plex Mono',monospace;letter-spacing:.12em;padding:3px 6px;
    border-radius:4px;border:1px solid}
  .smc-chip.smc-live{color:#34D399;border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.12)}
  .smc-chip.smc-stale{color:var(--amber, #FBBF24);border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.12)}
  .smc-chip.smc-off{color:var(--muted, #94A3B8);border-color:var(--edge, #334155);background:transparent}
  .smc-dot{width:5px;height:5px;border-radius:50%;background:currentColor}
  .smc-ring{width:5px;height:5px;border-radius:50%;border:1.5px solid currentColor;background:transparent}
  .smc-bar{width:6px;height:2px;background:currentColor;border-radius:1px}

  /* core rule 7 — skeleton sized to final height, no layout jump */
  .smc-skel{border-radius:7px;background:var(--panel-hi, #131C31);animation:smcBreathe 1.4s ease-in-out infinite}

  /* core rule 1 — NO horizontal tab row renders below the breakpoint. The mobile surfaces replace
     them with .smc-stack; this is the backstop so a page that has not been converted yet cannot
     leak a wrapped tab row onto a phone. */
  .smc-mobile-host .tabs{display:none!important}
}

/* ── cc#860 MODEL LAUNCHER — dark, assembled from 9016; no new visual language ────────────── */
.scorr-ml-btn{margin-left:auto;min-height:44px;padding:8px 14px;border-radius:8px;cursor:pointer;
  border:1px solid var(--line2,rgba(148,166,210,.3));background:transparent;
  color:var(--txt,#e6edf7);font:700 12px Sora,sans-serif;letter-spacing:.02em}
.scorr-ml-btn:hover{background:rgba(148,166,210,.12)}
.scorr-ml-ov{position:fixed;inset:0;z-index:95;display:none;background:rgba(5,9,18,.72)}
.scorr-ml-ov.open{display:block}
.scorr-ml{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:min(560px,94vw);
  max-height:86vh;overflow-y:auto;background:var(--panel, #0E1526);border:1px solid var(--edge, #2A3A5C);border-radius:12px}
.scorr-ml-hd{display:flex;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid var(--edge, #1E2A44)}
.scorr-ml-hd h4{margin:0;font:800 15px Sora,sans-serif;color:#E6EDF7}
.scorr-ml-sub{font:500 11px 'IBM Plex Mono',monospace;color:var(--muted, #64748B)}
.scorr-ml-x{margin-left:auto;min-width:44px;min-height:44px;background:none;border:none;
  color:var(--muted, #64748B);font-size:22px;cursor:pointer}
.scorr-ml-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px}
@media(max-width:600px){.scorr-ml-grid{grid-template-columns:1fr}}
.scorr-ml-card{display:block;min-height:44px;padding:12px 14px;border-radius:9px;
  background:var(--panel-hi, #131C31);border:1px solid var(--edge, #1E2A44);text-decoration:none;position:relative;overflow:hidden}
.scorr-ml-card:hover{border-color:var(--edge, #2A3A5C)}
/* the state rail — 3px coloured left edge, framework signature element */
.scorr-ml-card:before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}
.scorr-ml-card.ml-live:before{background:#34D399}
.scorr-ml-card.ml-stale:before{background:var(--amber, #FBBF24)}
.scorr-ml-card.ml-off:before{background:#475569}
.ml-top{display:flex;align-items:center;gap:8px}
.ml-name{font:700 13.5px Sora,sans-serif;color:#E6EDF7}
.ml-desc{font-size:11.5px;color:var(--muted, #9DAEC8);margin-top:3px;line-height:1.4}
.ml-foot{font:500 10.5px 'IBM Plex Mono',monospace;color:var(--muted, #64748B);margin-top:6px}
.ml-badge{margin-left:auto;display:inline-flex;align-items:center;gap:5px;
  font:700 9px 'IBM Plex Mono',monospace;letter-spacing:.12em;padding:3px 7px;border-radius:4px;border:1px solid}
.ml-badge.ml-live{color:#34D399;border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.12)}
.ml-badge.ml-stale{color:var(--amber, #FBBF24);border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.12)}
.ml-badge.ml-off{color:var(--muted, #94A3B8);border-color:var(--edge, #334155);background:transparent}
/* SHAPE, not opacity: filled pulsing dot / hollow ring / flat bar (item 9) */
.ml-dot{width:6px;height:6px;border-radius:50%;background:#34D399;animation:mlPulse 2s infinite}
.ml-ring{width:6px;height:6px;border-radius:50%;border:1.5px solid var(--amber, #FBBF24);background:transparent}
.ml-bar{width:7px;height:2px;background:#94A3B8;border-radius:1px}
@keyframes mlPulse{0%,100%{opacity:1}50%{opacity:.3}}
.scorr-ml-skel{height:78px;border-radius:9px;background:var(--panel-hi, #131C31);
  animation:mlPulse 1.4s ease-in-out infinite}
.scorr-ml-empty{grid-column:1/-1;padding:20px;text-align:center;font-size:12px;color:var(--muted, #64748B)}

/* header + LIVE/STALE feed chip */
.dt .hdr{display:flex;align-items:center;justify-content:space-between;padding:14px 2px 12px;position:sticky;top:0;background:linear-gradient(var(--ink) 82%,transparent);z-index:30}
.dt .brand{font-weight:800;font-size:17px}.dt .brand b{color:var(--blue)}
.dt .hdr-right{display:flex;align-items:center;gap:8px}
.dt .chip{display:inline-flex;align-items:center;gap:6px;height:30px;padding:0 11px;border-radius:15px;border:1px solid var(--line);background:var(--surface);font-size:11px;font-weight:600;color:var(--mut)}
.dt .chip .dot{width:7px;height:7px;border-radius:50%;background:var(--amber);box-shadow:0 0 8px var(--amber)}
.dt .chip.live{color:var(--bull);border-color:rgba(47,212,139,.35)}
.dt .chip.live .dot{background:var(--bull);box-shadow:0 0 8px var(--bull);animation:dtPulse 1.6s ease-in-out infinite}
.dt .chip.stale{color:var(--bear);border-color:rgba(255,92,108,.35)}
.dt .chip.stale .dot{background:var(--bear);box-shadow:0 0 8px var(--bear)}
.dt .clock{font-family:var(--mono);font-size:11px;color:var(--dim)}
@keyframes dtPulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes dtRise{to{opacity:1;transform:none}}
/* gate card + glow-edge state segments */
.dt .gate{background:linear-gradient(168deg,var(--surface) 0%,#0F1730 100%);border:1px solid var(--line);border-radius:var(--r);padding:18px 16px 16px;box-shadow:0 18px 44px rgba(3,7,20,.5)}
.dt .eyebrow{display:flex;justify-content:space-between;font-size:10px;font-weight:700;letter-spacing:.14em;color:var(--dim);text-transform:uppercase}
.dt .mood-row{display:flex;align-items:baseline;gap:12px;margin:10px 0 14px}
.dt .mood{font-size:34px;font-weight:800;letter-spacing:-.02em}
.dt .mood-count{font-family:var(--mono);font-size:12px;font-weight:600;padding:4px 9px;border-radius:9px}
.dt .segs{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}
.dt .seg{border-radius:12px;padding:9px 8px 8px;background:var(--well);border:1px solid var(--line);position:relative;overflow:hidden}
.dt .seg::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px}
.dt .seg.pass::before{background:var(--bull);box-shadow:0 0 10px var(--bull)}
.dt .seg.fail::before{background:var(--bear);box-shadow:0 0 10px var(--bear)}
.dt .seg .k{font-size:9.5px;font-weight:700;letter-spacing:.1em;color:var(--dim);text-transform:uppercase}
.dt .seg .v{font-family:var(--mono);font-size:14px;font-weight:700;margin-top:3px}
.dt .seg.pass .v{color:var(--bull)}.dt .seg.fail .v{color:var(--bear)}
.dt .seg .s{font-size:9px;font-weight:700;margin-top:2px;color:var(--dim)}
/* instrument tiles w/ vs-prev-close delta */
.dt .instr{display:flex;gap:7px;margin-top:12px}
.dt .ins{flex:1;background:var(--surface2);border:1px solid var(--line);border-radius:11px;padding:8px 10px;min-height:52px}
.dt .ins .top{display:flex;justify-content:space-between;align-items:center}
.dt .ins .k{font-size:9.5px;font-weight:700;letter-spacing:.08em;color:var(--dim);text-transform:uppercase}
.dt .ins .v{font-family:var(--mono);font-size:14px;font-weight:700;margin-top:2px}
.dt .ins .d{font-family:var(--mono);font-size:10px;font-weight:700;margin-top:1px}
.dt .ins .d.neg{color:var(--bear)}.dt .ins .d.pos{color:var(--bull)}
.dt .ins .d small{color:var(--dim);font-weight:600;font-size:9px;letter-spacing:.02em}
.dt .ins .spark{width:26px;height:14px;flex-shrink:0;opacity:.9}
/* slot capacity bars */
.dt .slots{margin-top:14px;padding-top:13px;border-top:1px solid var(--line);display:grid;gap:9px}
.dt .slot{display:grid;grid-template-columns:44px 1fr 56px;align-items:center;gap:10px}
.dt .slot .k{font-size:10px;font-weight:700;letter-spacing:.08em;color:var(--mut);text-transform:uppercase}
.dt .slot .bar{display:block;height:8px;border-radius:4px;background:var(--well);border:1px solid var(--line);overflow:hidden}
.dt .slot .fill{display:block;height:100%;border-radius:4px}
.dt .slot .n{font-family:var(--mono);font-size:12px;font-weight:700;text-align:right}
.dt .f-buy{background:linear-gradient(90deg,var(--volt, #1E9E68),var(--bull))}
.dt .f-sell{background:linear-gradient(90deg,var(--heat, #C2404E),var(--bear))}
.dt .f-so{background:linear-gradient(90deg,var(--pulse, #8A5BD6),#B08CFF)}
.dt .f-s1b{background:linear-gradient(90deg,var(--aqua, #2789C9),var(--cyan))}
.dt .mini{display:grid;grid-template-columns:1fr 1fr;gap:9px}
/* pill tabs */
.dt .tabs{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;margin:18px -14px 0;padding:2px 14px 6px;-webkit-mask:linear-gradient(90deg,transparent,#000 18px,#000 calc(100% - 26px),transparent)}
.dt .tabs::-webkit-scrollbar{display:none}
.dt .tab{flex-shrink:0;height:38px;padding:0 15px;border-radius:19px;border:1px solid var(--line);background:var(--surface);color:var(--mut);font-size:12.5px;font-weight:700;display:flex;align-items:center;gap:7px;cursor:pointer}
.dt .tab.on{background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:0 6px 18px rgba(77,124,254,.35)}
.dt .tab .b{font-family:var(--mono);font-size:10px;font-weight:700;background:rgba(255,255,255,.14);border-radius:8px;padding:2px 6px}
.dt .tab:not(.on) .b{background:var(--well);color:var(--dim)}
/* cards + position rows + KPI duo */
.dt .card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:14px 15px;margin-top:14px}
.dt .card .h{display:flex;justify-content:space-between;align-items:center;font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--dim);text-transform:uppercase;margin-bottom:10px}
.dt .prow{display:grid;grid-template-columns:1.2fr .9fr .7fr .9fr;gap:6px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.dt .prow:first-of-type{border-top:none}
.dt .prow .sym{font-weight:700;font-size:13px}
.dt .prow .bk{font-size:9px;color:var(--dim);font-weight:600;margin-top:1px}
.dt .prow .num{font-size:12.5px;font-weight:700;text-align:right}
.dt .lab{display:grid;grid-template-columns:1.2fr .9fr .7fr .9fr;gap:6px;font-size:9px;font-weight:700;letter-spacing:.08em;color:var(--dim);text-transform:uppercase;padding-bottom:5px}
.dt .lab span:not(:first-child){text-align:right}
.dt .pos{color:var(--bull)}.dt .neg{color:var(--bear)}
.dt .kpis{display:grid;grid-template-columns:1fr 1.35fr;gap:10px;margin-top:14px}
.dt .kpi{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:14px 15px 13px}
.dt .kpi .k{font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--dim);text-transform:uppercase}
.dt .kpi .v{font-family:var(--mono);font-size:26px;font-weight:700;margin-top:7px;letter-spacing:-.02em}
.dt .kpi .sub{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:4px}
/* pivot rail w/ pulsing CMP dot (Room Left) */
.dt .rail-wrap{margin-top:6px;padding:18px 6px 6px}
.dt .rail{position:relative;height:6px;border-radius:3px;background:linear-gradient(90deg,rgba(255,92,108,.55),rgba(255,92,108,.18) 40%,rgba(47,212,139,.18) 60%,rgba(47,212,139,.55))}
.dt .rail .tick{position:absolute;top:-5px;width:2px;height:16px;background:var(--dim);border-radius:1px}
.dt .rail .tick.major{background:var(--mut)}
.dt .rail .tlab{position:absolute;top:14px;transform:translateX(-50%);text-align:center}
.dt .rail .tlab .n1{font-size:9px;font-weight:700;color:var(--dim);letter-spacing:.04em}
.dt .rail .tlab .n2{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--mut)}
.dt .cmp-dot{position:absolute;top:50%;transform:translate(-50%,-50%);width:13px;height:13px;border-radius:50%;background:var(--bull);box-shadow:0 0 0 3px rgba(47,212,139,.25),0 0 14px var(--bull);animation:dtPulse 1.4s ease-in-out infinite}
.dt .cmp-tag{position:absolute;top:-26px;transform:translateX(-50%);font-family:var(--mono);font-size:11px;font-weight:700;color:var(--bull);white-space:nowrap}
.dt .verdict{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--amber);background:var(--amber-soft);border:1px solid rgba(245,185,74,.3);padding:3px 8px;border-radius:8px}
/* dark bottom nav */
.dt .bnav{position:fixed;left:0;right:0;bottom:0;max-width:430px;margin:0 auto;height:calc(60px + env(safe-area-inset-bottom));padding:0 8px env(safe-area-inset-bottom);display:flex;background:rgba(13,20,40,.92);backdrop-filter:blur(18px);border-top:1px solid var(--line2)}
.dt .bn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;color:var(--dim);font-size:10px;font-weight:700;text-decoration:none}
.dt .bn svg{width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
.dt .bn.on{color:var(--cyan)}
@media (prefers-reduced-motion:reduce){.dt .chip.live .dot,.dt .cmp-dot,.dt .seg{animation:none;opacity:1;transform:none}}

/* ==========================================================================
   LIGHT THEME  —  cc#345. DARK is the default identity (each page's own :root
   holds the dark tokens). Setting data-theme="light" on <html> overrides every
   page's token names here — this rule out-specifies a bare :root (0,2,0 vs 0,1,0),
   so it wins regardless of load order. rgba-tint chips adapt to the surface on
   their own; only opaque token names need remapping. Light values from Fable.
   ========================================================================== */
:root[data-theme="light"]{
  --bg:#F4F7FE; --bg2:#EDF1FA; --bg3:#E7ECF8;
  --panel:#FFFFFF; --panel2:#EDF1FA; --panel3:#E7ECF8;
  --card:#FFFFFF; --card2:#EDF1FA;
  /* cc#345 NOTE: --ink is a TEXT token on the live pages (screener), NOT the .dt bg meaning
     Fable listed (#F4F7FE). Protecting the live consumer -> dark text; .dt (unused) gets its
     own bg from --surface/--bg. */
  --ink:#0E1630; --surface:#FFFFFF; --surface2:#EDF1FA; --well:#E7ECF8;
  --line:rgba(20,35,80,.10); --line2:rgba(20,35,80,.18);
  --border:rgba(20,35,80,.10); --border2:rgba(20,35,80,.18);
  --txt:#0E1630; --text:#0E1630; --text2:#5B6B94; --text3:#8A97BC;
  --mut:#5B6B94; --dim:#8A97BC; --grey:#5B6B94; --tag-bg:#EDF1FA; --tag-fg:#5B6B94;
  --grn:#0FA968; --green:#0FA968; --bull:#0FA968;
  --grn-d:rgba(15,169,104,.12); --grn-b:rgba(15,169,104,.35);
  --red:#E0405A; --bear:#E0405A;
  --red-d:rgba(224,64,90,.10); --red-b:rgba(224,64,90,.35);
  --blu:#3D6BEC; --blue:#3D6BEC; --accent:#3D6BEC; --blu-d:rgba(61,107,236,.12);
  --cyan:#0FA8C4; --cyan-d:rgba(15,168,196,.12);
  --amber:#C98A12; --gold:#C98A12; --warn:#C98A12;
  --amber-d:rgba(201,138,18,.12); --amber-b:rgba(201,138,18,.35);
  --violet:#7C3AED; --purp:#7C3AED; --purp-d:rgba(124,58,237,.10); --purp-b:rgba(124,58,237,.35);
  --shadow:0 1px 4px rgba(20,35,80,.08); --shadow-md:0 4px 16px rgba(20,35,80,.10);
}
/* glow is a dark-native effect — on light keep the solid 3px edge, drop the bloom */
:root[data-theme="light"] .seg.pass::before{box-shadow:none}
:root[data-theme="light"] .seg.fail::before{box-shadow:none}
:root[data-theme="light"] .cmp-dot,:root[data-theme="light"] .room-cmp-dot{box-shadow:0 0 0 2px rgba(15,169,104,.22)}
:root[data-theme="light"] .chip.live .dot{box-shadow:0 0 4px rgba(15,169,104,.5)}
/* dark translucent bottom nav -> light translucent on light theme */
:root[data-theme="light"] .dt .bnav{background:rgba(255,255,255,.9);border-top-color:rgba(20,35,80,.14)}
"""

# cc#330 P4 — shared table helper: edge-fade affordances + row-tap expand.
# Served at /mobile_tables.js and injected site-wide alongside mobile.css.
MOBILE_TABLES_JS = """
(function () {
  if (window.__scorrMTables) return; window.__scorrMTables = true;

  function initWrap(w) {
    if (w.__mwrap) return; w.__mwrap = true;
    function upd() {
      var sl = w.scrollLeft, max = w.scrollWidth - w.clientWidth;
      w.classList.toggle('can-l', sl > 1);
      w.classList.toggle('can-r', sl < max - 1);
    }
    w.addEventListener('scroll', upd, {passive: true});
    window.addEventListener('resize', upd);
    upd();
  }

  function initTable(t) {
    var heads = [].map.call(t.querySelectorAll('thead th'), function (th) {
      var c = th.cloneNode(true); var a = c.querySelector('.arrow'); if (a) a.remove();
      return c.textContent.trim();
    });
    var body = t.tBodies[0]; if (!body) return;
    [].forEach.call(body.rows, function (row) {
      if (row.classList.contains('mtable-detail') || row.__mwired) return;
      row.__mwired = true;
      var f = row.cells[0];
      if (f && !f.querySelector('.mchev')) {
        var c = document.createElement('span'); c.className = 'mchev'; c.innerHTML = '\\u203a';
        f.appendChild(c);
      }
      row.classList.add('mrow-exp');
      row.addEventListener('click', function (e) {
        if (e.target.closest('a,button,input,select,[onclick]')) return;
        toggle(row);
      });
    });
    function toggle(row) {
      if (row.classList.contains('open')) {
        row.classList.remove('open');
        if (row._detail && row._detail.parentNode) row._detail.parentNode.removeChild(row._detail);
        row._detail = null; return;
      }
      row.classList.add('open');
      var tr = document.createElement('tr'); tr.className = 'mtable-detail';
      var td = document.createElement('td'); td.colSpan = row.cells.length;
      var dl = '<dl>';
      [].forEach.call(row.cells, function (cell, i) {
        var label = heads[i] || ''; if (!label) return;
        dl += '<dt>' + label + '</dt><dd>' + cell.textContent.trim() + '</dd>';
      });
      dl += '</dl>'; td.innerHTML = dl; tr.appendChild(td);
      row.parentNode.insertBefore(tr, row.nextSibling);
      row._detail = tr;
    }
  }

  function scan() {
    [].forEach.call(document.querySelectorAll('.mtable-wrap'), initWrap);
    [].forEach.call(document.querySelectorAll('table.mtable'), initTable);
  }

  var pending = null;
  function scanSoon() { if (pending) return; pending = setTimeout(function () { pending = null; scan(); }, 150); }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan);
  else scan();
  if (window.MutationObserver) {
    new MutationObserver(scanSoon).observe(document.body, {childList: true, subtree: true});
  }
})();
"""


@router.get("/app")
def pwa_home():
    # cc#100: canonical home unified to "/" (scorr_home.html). /app kept as a
    # redirect so existing PWA installs / bookmarks land on the single home route.
    return RedirectResponse(url="/", status_code=302)


@router.get("/pwa.js")
def pwa_js():
    return Response(PWA_JS, media_type="application/javascript", headers=_NOCACHE)


@router.get("/nav_toggle.js")
def pwa_nav_toggle_js():
    return Response(NAV_TOGGLE_JS, media_type="application/javascript", headers=_CACHE_1D)


# cc#792: the deploy build stamp. Railway exposes the commit SHA; fall back to APP_VERSION, then to a
# process-start token so a bare local run still gets a unique value rather than a constant.
import os as _os_build, time as _time_build
BUILD_ID = (_os_build.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:8]
            or _os_build.getenv("APP_VERSION", "")
            or str(int(_time_build.time())))


@router.get("/service_worker.js")
def pwa_service_worker():
    """cc#792: the SW cache name now carries the build stamp, so it bumps on EVERY deploy.

    This retires the cc#178 rule that a human must remember to bump scorr-pwa-vN in the same commit
    as any PWA_JS/SW_JS change. That rule had already failed twice — cc#177 shipped a nav rename
    without a bump and installed clients served the old nav forever, and the founder saw yesterday's
    scorr_chart_card.js hours after cc#779 deployed. A convention that must be remembered on every
    commit is not a safeguard; deriving it from the deploy is. The activate handler already deletes
    every cache != CACHE, so a changing name is exactly what forces installed clients to drop stale
    shell assets on their next visit."""
    h = dict(_NOCACHE); h["Service-Worker-Allowed"] = "/"
    js = SW_JS.replace("scorr-pwa-v13", "scorr-pwa-" + BUILD_ID)
    return Response(js, media_type="application/javascript", headers=h)


@router.get("/static/manifest.json")
@router.get("/manifest.json")
def pwa_manifest():
    return JSONResponse(MANIFEST)


@router.get("/static/icon-192.png")
def pwa_icon_192():
    return Response(_icon(192), media_type="image/png",
                   headers={"Cache-Control": "public, max-age=604800"})


@router.get("/static/icon-512.png")
def pwa_icon_512():
    return Response(_icon(512), media_type="image/png",
                   headers={"Cache-Control": "public, max-age=604800"})


@router.get("/static/mobile.css")
def pwa_mobile_css():
    # cc#327: served no-cache during the MOBILE_UX_REDEFINE_V1 program so later
    # tasks' edits propagate immediately; not in the SW SHELL (avoids stale cache).
    return Response(MOBILE_CSS, media_type="text/css", headers=_CACHE_1D)


@router.get("/mobile_tables.js")
def pwa_mobile_tables_js():
    # cc#330 P4: shared table helper, injected site-wide alongside mobile.css.
    return Response(MOBILE_TABLES_JS, media_type="application/javascript", headers=_CACHE_1D)


# cc#573 (spec id=6438): ONE shared Results "R" pill + card component, injected site-wide via
# _MOBILE_HEAD. A delegated click opens the modal and fetches /api/results/card?symbol=X
# (built to cc#572's response contract). Degrades gracefully if that backend isn't deployed yet.
#
# cc#798: DO NOT add R/V pills to new surfaces. Next to a symbol in a table, use the shared
# C·A·R·D strip instead — window.ScorrCardRow(sym) — which gives Chart / Analysis / Result /
# Cockpit with one call and the same availability rules everywhere. pillV is GONE: the V button
# opened the Volume/Energy card, which is a strict SUBSET of what the strip's A opens (the
# Analysis modal covers GVM, sector rank, volume, delivery and trajectory), so V folded into A
# rather than being dropped — no function was lost. `pill` survives only for the documented raw
# <span class="rcard-pill" data-sym="SYM">R</span> pattern; it is deprecated, and every page call
# site was migrated to ScorrCardRow in cc#798.
RESULTS_CARD_JS = """
(function(){
  if (window.__scorrRCard) return; window.__scorrRCard = true;
  var css = ''
    + '.rcard-pill{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;'
    + 'margin-left:6px;border-radius:4px;font:700 10px/1 Sora,system-ui,sans-serif;cursor:pointer;'
    + 'color:var(--blu,#4D7CFE);background:rgba(77,124,254,.12);border:1px solid rgba(77,124,254,.35);'
    + 'vertical-align:middle;user-select:none;flex:none}'
    + '.rcard-pill:hover{background:rgba(77,124,254,.22)}'
    + '.rcard-ov{position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.45);display:none;'   /* cc#679: unified backdrop */
    + 'align-items:center;justify-content:center;padding:16px}'
    + '.rcard-ov.on{display:flex}'
    + '.rcard{background:var(--card,#fff);color:var(--txt,#101828);border:1px solid var(--line,rgba(148,166,210,.2));'
    + 'border-radius:16px;max-width:620px;width:100%;max-height:88vh;overflow:auto;'   /* cc#679: unified 620/88vh/16 modal footprint */
    + 'box-shadow:0 24px 64px rgba(0,0,0,.22);padding:18px}'
    + '.rcard-hd{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:8px}'
    + '.rcard-sym{font:800 16px/1.1 Sora,sans-serif}'
    + '.rcard-x{border:none;background:none;color:var(--dim,#8892a6);font-size:22px;cursor:pointer;line-height:1}'
    + '.rcard-status{display:inline-flex;align-items:center;gap:6px;font:700 10px/1 Sora,sans-serif;'
    + 'text-transform:uppercase;letter-spacing:.06em;padding:4px 9px;border-radius:6px;margin-top:6px}'
    + '.rcard-st-a{color:var(--volt, #0f9d58);background:rgba(47,212,139,.14);border:1px solid rgba(47,212,139,.4)}'
    + '.rcard-st-u{color:var(--amber, #c98a12);background:rgba(245,185,74,.16);border:1px solid rgba(245,185,74,.45)}'
    + '.rcard-st-t{color:var(--mut,#667085);background:rgba(148,166,210,.14);border:1px solid rgba(148,166,210,.3)}'
    + '.rcard-body{font-size:13px;line-height:1.55;white-space:pre-wrap;margin-top:6px}'
    /* cc#676: SectionSeparator (UI pattern #12) — thin top rule + generous space + uppercase muted
       letter-spaced label, then ~11px before content. Every R-card section label is this component. */
    + '.rcard-lbl{font:700 10.5px/1.3 Sora,sans-serif;text-transform:uppercase;letter-spacing:.1em;color:var(--mut,#667085);margin:18px 0 11px;padding-top:13px;border-top:1px solid var(--line,rgba(148,166,210,.2))}'
    + '.rcard-lbl:first-child{margin-top:2px;padding-top:0;border-top:none}'
    + '.rcard-note{font-size:11px;color:var(--dim,#8892a6);margin-top:8px}'
    /* cc#788 LEVEL 1 — FY27 estimate benchmarked against the actual, one tabular line + HIT/MISSED */
    + '.rcard-fy27{display:flex;align-items:center;gap:18px;flex-wrap:wrap}'
    + '.rcard-fy27-c{display:flex;flex-direction:column;gap:2px}'
    + '.rcard-fy27-k{font:600 9.5px/1 Sora,sans-serif;text-transform:uppercase;letter-spacing:.07em;color:var(--mut,#667085)}'
    + '.rcard-fy27-v{font:800 15px/1.2 Sora,sans-serif;font-variant-numeric:tabular-nums}'
    + '.rcard-hit,.rcard-missed{font:700 10px/1 Sora,sans-serif;text-transform:uppercase;letter-spacing:.06em;'
    + 'padding:4px 9px;border-radius:6px;margin-left:auto}'
    + '.rcard-hit{color:var(--volt, #0f9d58);background:rgba(47,212,139,.14);border:1px solid rgba(47,212,139,.4)}'
    + '.rcard-missed{color:var(--heat, #d0433b);background:rgba(208,67,59,.12);border:1px solid rgba(208,67,59,.35)}'
    /* cc#796 — vs est. rows: actual, expected, deviation %, banded tag. IN-LINE is deliberately grey,
       not green: matching a mechanical run-rate projection is not an achievement. */
    + '.rcard-inline{font:700 10px/1 Sora,sans-serif;text-transform:uppercase;letter-spacing:.06em;'
    + 'padding:4px 9px;border-radius:6px;color:var(--mut,#667085);background:rgba(148,166,210,.14);'
    + 'border:1px solid rgba(148,166,210,.3)}'
    + '.rcard-exp{display:flex;flex-direction:column;gap:7px}'
    + '.rcard-exp-row{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:12.5px}'
    + '.rcard-exp-l{font-weight:700;min-width:42px}'
    + '.rcard-exp-v{font-variant-numeric:tabular-nums;font-weight:700}'
    + '.rcard-exp-e{color:var(--dim,#8892a6);font-variant-numeric:tabular-nums}'
    + '.rcard-exp-d{font-weight:800;font-variant-numeric:tabular-nums;margin-left:auto}'
    /* cc#802 — category tags on the merged news stream. AI Editorial is the long-form worth expanding;
       Stock Views is a distinct editorial product. Domestic shorts carry no tag (they are the default). */
    + '.rcard-cat{display:inline-block;font:700 8.5px/1 Sora,sans-serif;text-transform:uppercase;'
    + 'letter-spacing:.07em;padding:3px 6px;border-radius:4px;margin-left:7px;vertical-align:middle}'
    + '.rcard-cat-ai{color:var(--pulse, #7a5af8);background:rgba(122,90,248,.12);border:1px solid rgba(122,90,248,.32)}'
    + '.rcard-cat-sv{color:var(--volt, #0f9d58);background:rgba(47,212,139,.14);border:1px solid rgba(47,212,139,.38)}'
    /* cc#797 L1 block — absolutes first, deltas trailing, traffic light on the delta only */
    + '.rcard-av{font:600 13px/1.5 Sora,sans-serif;margin:0 0 10px;color:var(--txt,#101828)}'
    + '.rcard-l1row{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:7px;font-size:12.5px}'
    + '.rcard-l1k{font-weight:700;min-width:74px;color:var(--mut,#667085);text-transform:uppercase;'
    + 'font-size:9.5px;letter-spacing:.07em}'
    + '.rcard-l1v{font:800 14px/1.2 Sora,sans-serif;font-variant-numeric:tabular-nums}'
    + '.rcard-l1d{font-variant-numeric:tabular-nums;color:var(--dim,#8892a6)}'
    /* cc#794 — coverage level tag beside the status chip. DETAILED is filled violet so it reads as
       an earned state; BASIC is a grey outline so it reads as a default, not a failure. */
    + '.rcard-lvl{display:inline-flex;align-items:center;font:700 9.5px/1 Sora,sans-serif;'
    + 'text-transform:uppercase;letter-spacing:.07em;padding:4px 8px;border-radius:6px;margin:6px 0 0 6px}'
    + '.rcard-lvl-d{color:#fff;background:var(--pulse, #7a5af8);border:1px solid var(--pulse, #7a5af8)}'
    + '.rcard-lvl-b{color:var(--mut,#667085);background:transparent;border:1px solid var(--line,rgba(148,166,210,.35))}'
    /* cc#788 LEVEL 2 — View Detailed gate above the FY27 section */
    + '.rcard-detwrap{margin-top:14px}'
    /* cc#858 — shimmer placeholders + the quiet failure line. Sized in _ctxSkeleton() to the real
       block heights so the card does not jump when content lands. */
    + '@keyframes rcardShimmer{0%{background-position:-380px 0}100%{background-position:380px 0}}'
    + '.rcard-skel{border-radius:6px;margin:8px 0;background-size:760px 100%;'
    + 'background:linear-gradient(90deg,rgba(148,163,184,.06) 0px,rgba(148,163,184,.16) 190px,rgba(148,163,184,.06) 380px);'
    + 'animation:rcardShimmer 1.25s linear infinite}'
    + '.rcard-ctx-fail{font-size:11.5px;color:var(--mut,#8a94a6);padding:8px 2px;font-style:italic}'
    + '.rcard-detbtn{display:inline-flex;align-items:center;gap:6px;min-height:38px;border:1px solid var(--line,rgba(148,166,210,.28));'
    + 'background:transparent;color:var(--txt,#101828);font:700 12px Sora,sans-serif;border-radius:8px;padding:8px 14px;cursor:pointer}'
    + '.rcard-detbtn:hover{background:rgba(148,166,210,.1)}'
    + '.rcard-detcx{color:var(--mut,#667085);font-size:10px}'
    + '.rcard-btn{margin-top:12px;border:1px solid var(--blu,#4D7CFE);background:rgba(77,124,254,.1);'
    + 'color:var(--blu,#4D7CFE);font:700 12px Sora,sans-serif;border-radius:8px;padding:8px 14px;cursor:pointer}'
    + '.rcard-btn:disabled{opacity:.6;cursor:progress}'
    + '.rcard-foot{margin-top:14px;padding-top:10px;border-top:1px solid var(--line,rgba(148,166,210,.2));'
    + 'font-size:10.5px;color:var(--dim,#8892a6);display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}'
    + '.rcard-gvm{font:700 10px/1 Sora;color:var(--mut,#667085);margin-left:8px}'
    + '.rcard-peer{margin-top:4px;display:flex;flex-direction:column;gap:5px}'
    + '.rcard-peer-row{display:flex;align-items:center;gap:8px;font-size:12px}'
    + '.rcard-peer-l{flex:1;color:var(--mut,#667085)}'
    + '.rcard-peer-v{font:700 12px/1 Sora;font-variant-numeric:tabular-nums;min-width:52px;text-align:right}'
    + '.rcard-peer-p{color:var(--dim,#8892a6);font-variant-numeric:tabular-nums;min-width:64px;text-align:right}'
    + '.rcard-beat{font:700 9px/1 Sora;color:var(--volt, #0f9d58);background:rgba(47,212,139,.14);border:1px solid rgba(47,212,139,.4);border-radius:4px;padding:2px 5px}'
    + '.rcard-miss{font:700 9px/1 Sora;color:var(--mut,#667085);background:rgba(148,166,210,.14);border:1px solid rgba(148,166,210,.3);border-radius:4px;padding:2px 5px}'
    // cc#766: pre-results peer table — button below the Expected result date, inline expandable table.
    + '.rcard-peerres{margin-top:8px}'
    + '.rcard-peerres-btn{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:6px 11px;font:700 12px/1 Sora,sans-serif;color:var(--accent,#2f6df4);background:rgba(47,109,244,.08);border:1px solid rgba(47,109,244,.28);border-radius:7px;cursor:pointer;-webkit-tap-highlight-color:transparent}'
    + '.rcard-peerres-btn:hover{background:rgba(47,109,244,.16)}'
    + '.rcard-peerres-tbl{margin-top:8px;overflow-x:auto}'
    + '.rcard-pr-table{width:100%;border-collapse:collapse;font-size:11.5px;font-variant-numeric:tabular-nums}'
    + '.rcard-pr-table th{font:700 9.5px/1.2 Sora,sans-serif;text-transform:uppercase;letter-spacing:.03em;color:var(--mut,#667085);text-align:left;padding:4px 8px 5px;border-bottom:1px solid var(--line,rgba(148,166,210,.2));white-space:nowrap}'
    + '.rcard-pr-table td{padding:6px 8px;border-bottom:1px solid var(--line,rgba(148,166,210,.12));white-space:nowrap;color:var(--txt,#1c2536)}'
    + '.rcard-pr-nm{font-weight:700;max-width:150px;overflow:hidden;text-overflow:ellipsis}'
    + '.rcard-pr-un td{color:var(--dim,#8892a6);font-style:italic}'
    // cc#579: V (volume/energy) pill + card — sibling of R, same .rcard-* scaffold, green accent
    + '.vcard-pill{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;margin-left:3px;font:800 9px/1 Sora,sans-serif;color:var(--volt, #0f9d58);background:rgba(47,212,139,.14);border:1px solid rgba(47,212,139,.4);border-radius:4px;cursor:pointer;vertical-align:middle;user-select:none}'
    + '.vcard-pill:hover{background:rgba(47,212,139,.24)}'
    + '.vcard-hero{display:flex;align-items:baseline;gap:8px;margin-top:2px}'
    + '.vcard-big{font:800 26px/1 Sora,sans-serif;font-variant-numeric:tabular-nums}'
    + '.vcard-sub{font-size:11.5px;color:var(--dim,#8892a6)}'
    + '.vcard-band{margin-top:5px;font:700 11px/1.3 Sora,sans-serif;padding:5px 9px;border-radius:6px;display:inline-block}'
    + '.vcard-bull{color:var(--volt, #0f9d58);background:rgba(47,212,139,.14);border:1px solid rgba(47,212,139,.4)}'
    + '.vcard-warn{color:var(--amber, #c98a12);background:rgba(245,185,74,.16);border:1px solid rgba(245,185,74,.45)}'
    + '.vcard-neu{color:var(--mut,#667085);background:rgba(148,166,210,.14);border:1px solid rgba(148,166,210,.3)}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  var ov = document.createElement('div'); ov.className = 'rcard-ov';
  ov.innerHTML = '<div class="rcard" role="dialog" aria-modal="true"></div>';
  document.body.appendChild(ov);
  var box = ov.querySelector('.rcard');
  function close(){ ov.classList.remove('on'); }
  ov.addEventListener('click', function(e){ if (e.target === ov) close(); });
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape') close(); });

  // cc#650: delegated "View more" toggle for collapsed polished-news items — one document listener
  // covers BOTH surfaces (R modal + Position News tab inline) since neither re-wires per-item.
  document.addEventListener('click', function(e){
    var t = e.target && e.target.closest && e.target.closest('.rcard-viewmore');
    if (!t) return;
    e.preventDefault();
    var it = t.closest('.rcard-pol-item'); if (!it) return;
    var full = it.querySelector('.rcard-pol-full'), brief = it.querySelector('.rcard-pol-brief');
    var open = !it.classList.contains('rcard-open');
    it.classList.toggle('rcard-open', open);
    if (full)  full.style.display  = open ? 'block' : 'none';
    if (brief) brief.style.display = open ? 'none' : 'block';
    t.textContent = open ? 'View less' : 'View more';
  });
  document.addEventListener('keydown', function(e){
    if ((e.key === 'Enter' || e.key === ' ') && e.target && e.target.classList && e.target.classList.contains('rcard-viewmore')){ e.preventDefault(); e.target.click(); }
  });

  // cc#784: delegated expand/collapse for the V2 long-form (one listener covers the R modal and every
  // inline/collapsible card, same pattern as the polished-news view-more).
  document.addEventListener('click', function(e){
    var t = e.target && e.target.closest && e.target.closest('.rcard-v2-more');
    if (!t) return;
    e.preventDefault(); e.stopPropagation();
    var box = document.getElementById(t.getAttribute('data-t'));
    if (!box) return;
    var open = box.style.maxHeight !== 'none';
    box.style.maxHeight = open ? 'none' : '132px';
    t.textContent = open ? 'Show less' : 'Read full analysis';
  });
  document.addEventListener('keydown', function(e){
    if ((e.key === 'Enter' || e.key === ' ') && e.target && e.target.classList && e.target.classList.contains('rcard-v2-more')){ e.preventDefault(); e.target.click(); }
  });

  // cc#788: delegated toggle for the Level-2 "View Detailed" button. One listener covers the R modal
  // and every inline/collapsible card; per-card state lives in the DOM, same pattern as cc#766.
  document.addEventListener('click', function(e){
    var b = e.target && e.target.closest && e.target.closest('.rcard-detbtn');
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    var box = document.getElementById(b.getAttribute('data-t')); if (!box) return;
    var open = box.style.display === 'none';
    box.style.display = open ? 'block' : 'none';
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
    var cx = b.querySelector('.rcard-detcx'); if (cx) cx.innerHTML = open ? '&#9662;' : '&#9656;';
  });
  document.addEventListener('keydown', function(e){
    if ((e.key === 'Enter' || e.key === ' ') && e.target && e.target.classList && e.target.classList.contains('rcard-detbtn')){ e.preventDefault(); e.target.click(); }
  });

  // cc#784: fetch the card payload and the V2 analysis together, so the renderer sees both at once
  // and never has to re-paint. A missing/failed V2 read degrades to has_analysis:false (template card).
  // cc#858: the CORE now returns without the four heavy blocks, and the context call fetches them
  // in parallel. The core + V2 pair still resolve together (that pairing is cc#784's no-re-paint
  // guarantee and is preserved); context arrives separately and paints into the already-visible card.
  var CTX_TIMEOUT_MS = 8000;

  function _fetchCard(sym, extra){
    return Promise.all([
      fetch('/api/results/card?symbol='+encodeURIComponent(sym)+(extra||''), {cache:'no-store'})
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }),
      fetch('/api/results/v2/'+encodeURIComponent(sym), {cache:'no-store'})
        .then(function(r){ return r.ok ? r.json() : {has_analysis:false}; })
        .catch(function(){ return {has_analysis:false}; })
    ]).then(function(res){ var d = res[0] || {}; d.v2 = res[1]; return d; });
  }

  // cc#858: heavy blocks, fetched after the card is already on screen. A client-side timeout is
  // mandatory here — without one a hung request leaves the shimmer animating forever, which reads
  // as "still loading" and is exactly the failure state the card forbids.
  function _fetchCtx(sym){
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var t = setTimeout(function(){ try{ ctl && ctl.abort(); }catch(e){} }, CTX_TIMEOUT_MS);
    return fetch('/api/results/card/context?symbol='+encodeURIComponent(sym),
                 {cache:'no-store', signal: ctl ? ctl.signal : undefined})
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .catch(function(e){ return {__failed:true, reason:String((e&&e.message)||e)}; })
      .then(function(v){ clearTimeout(t); return v; });
  }

  // Shimmer placeholders. Heights are set to the real block heights so nothing JUMPS when content
  // lands — a card that reflows after paint feels slower than one that waited, which would defeat
  // the point of the split.
  function _skel(h, label){
    return '<div class="rcard-skel" data-skel="'+esc(label)+'" style="height:'+h+'px"></div>';
  }
  function _ctxSkeleton(){
    return '<div class="rcard-ctx">'
         +   _skel(96,  'peers')
         +   _skel(120, 'peer-results')
         +   _skel(84,  'news')
         + '</div>';
  }
  // Each block paints INDEPENDENTLY as it resolves; a failed block shows a quiet line and never
  // blocks its siblings.
  function _ctxFail(msg){
    return '<div class="rcard-ctx-fail">Could not load this section'+(msg?' \u00b7 '+esc(msg):'')+'</div>';
  }

  // cc#858: paint the heavy blocks into an ALREADY-VISIBLE card.
  // The core render and the hydrated render use the SAME builder (cardHtml) with the same `d`, so
  // the final DOM is byte-identical to the pre-split card — the only difference is WHEN the four
  // context keys are populated. Everything above the context region renders from identical fields
  // both times, so it does not move when the blocks land.
  // If the context call fails, `d` keeps its null blocks and cardHtml simply hides those sections
  // (they were already hidden-when-empty), then a quiet failure line is appended. The core card
  // stays fully usable — never a blank card, never an endless shimmer.
  function _hydrate(renderFn, sym, d){
    _fetchCtx(sym).then(function(ctx){
      if (!ctx || ctx.__failed){
        try { renderFn(d, {ctxFailed: (ctx && ctx.reason) || 'timeout'}); } catch(e){}
        return;
      }
      d.peer_comparison = ctx.peer_comparison;
      d.peer_results    = ctx.peer_results;
      d.raw_news        = ctx.raw_news;
      d.polished_news   = ctx.polished_news;
      d.__ctx_errors    = ctx.errors || {};
      try { renderFn(d, {}); } catch(e){}
    });
  }

  // cc#766: delegated toggle for the "Peer Results (N reported)" button — one listener covers both the
  // R modal and every inline/collapsible card; toggles the sibling table + flips the caret. Per-card
  // state lives in the DOM (each card owns its own .rcard-peerres wrapper).
  document.addEventListener('click', function(e){
    var b = e.target && e.target.closest && e.target.closest('.rcard-peerres-btn');
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    var wrap = b.closest('.rcard-peerres'); if (!wrap) return;
    var tbl = wrap.querySelector('.rcard-peerres-tbl'); if (!tbl) return;
    var open = tbl.style.display === 'none';
    tbl.style.display = open ? 'block' : 'none';
    b.setAttribute('aria-expanded', open ? 'true' : 'false');
    var cx = b.querySelector('.rcard-pr-cx'); if (cx) cx.innerHTML = open ? '&#9662;' : '&#9656;';
  });

  function esc(s){ return String(s==null?'':s).replace(/[&<>\"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c];}); }
  function fmtDate(s){ if(!s) return 'TBD'; var d=new Date(String(s).replace(' ','T')); if(isNaN(d.getTime())) return esc(s);
    var M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']; return d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear(); }

  // cc#697: compact peer line MERGED into the Result Analysis block (no separate bottom section, zero
  // duplication). SAME-QUARTER peers only; YoY basis (screener qoq_* = latest-Q vs year-ago-Q). A metric
  // with no same-quarter peer figure is skipped -> the top block's Sector line stands alone.
  function peerHtml(pc){
    if(!pc) return '';
    function row(lbl, o){
      if(!o || o.peer==null) return '';
      var st=(_grow(o.stock)||'--');   // cc#823: peer YoY growth -> one decimal
      var beat=o.beat, cls=beat?'rcard-beat':'rcard-miss', tag=beat?'BEAT':'MISS';
      return '<div class=\"rcard-peer-row\"><span class=\"rcard-peer-l\">'+lbl+' YoY</span>'
        +'<span class=\"rcard-peer-v\">'+esc(st)+'</span>'
        +'<span class=\"rcard-peer-p\">Top-'+(pc.peer_count||0)+' peers '+esc(_grow(o.peer)||'--')+'</span>'
        +'<span class=\"'+cls+'\">'+tag+'</span></div>';
    }
    var body=row('Sales',pc.sales)+row('PAT',pc.profit);
    if(!body) return '';
    // cc#801 fix_5: two counts appear on this card — the header's full-segment count (which INCLUDES
    // the subject) and this peer-pool count (which excludes it), so they legitimately differ by one.
    // Both derive from the same reported-detection rule (latest fundamentals_history quarter ==
    // subject's quarter); only the population differs. Say which population this one is, so a reader
    // seeing "7/12" above and "6/11" here knows it is scope, not a contradiction.
    var cnt=(pc.same_quarter_peers!=null?pc.same_quarter_peers:0)+' of '+(pc.total_peers!=null?pc.total_peers:'?');
    return '<div class=\"rcard-peer\">'+body+'</div>'
      +'<div class=\"rcard-note\">vs top-'+(pc.peer_count||0)+' same-quarter peers by GVM in '+esc(pc.segment||'segment')
      +' &middot; '+cnt+' segment peers (excluding this company) have reported '+esc(pc.quarter||'')
      +'. The sector figure in the header counts the full segment including this company.</div>';
  }

  // cc#766: pre-results peer table. Button "Peer Results (N reported)" below the Expected result date;
  // tap -> inline table of the top-3 same-segment GVM peers (Peer | Result date | Sales YoY | PAT YoY |
  // Margin vs LY | move % on result day). Only reported peers carry figures (cc#765 validated gate);
  // unreported peers render greyed with their expected date. Collapsed by default, per-card state via a
  // delegated toggle (below). pr.peers is already GVM-desc from the backend.
  // cc#823: routed through the ONE contract rather than its own formatter — this is exactly the
  // kind of near-duplicate that drifts. Growth -> one decimal.
  function _pct(v){ return _grow(v) || '--'; }
  function peerResultsHtml(pr){
    if(!pr || !pr.n_reported) return '';
    var rows='';
    for(var i=0;i<pr.peers.length;i++){ var p=pr.peers[i];
      if(p.reported){
        var mv=p.move_pct, mvs=(_grow(mv)||'--');   // cc#823: result-day move is a change -> one decimal
        var mvc=mv==null?'var(--mut,#667085)':(mv>=0?'#0f9d58':'#d0433b');
        // cc#823: margin LEVELS are absolutes -> integer.
        var marg=(p.margin!=null&&p.margin_ly!=null)?(esc(_lvl(p.margin,'%')+' vs '+_lvl(p.margin_ly,'%'))):'--';
        rows+='<tr><td class=\"rcard-pr-nm\" title=\"'+esc(p.name)+'\">'+esc(p.name)+'</td>'
          +'<td>'+fmtDate(p.result_date)+'</td>'
          +'<td style=\"text-align:right\">'+_pct(p.sales_yoy)+'</td>'
          +'<td style=\"text-align:right\">'+_pct(p.pat_yoy)+'</td>'
          +'<td style=\"text-align:right\">'+marg+'</td>'
          +'<td style=\"text-align:right;color:'+mvc+'\">'+mvs+'</td></tr>';
      } else {
        rows+='<tr class=\"rcard-pr-un\"><td class=\"rcard-pr-nm\" title=\"'+esc(p.name)+'\">'+esc(p.name)+'</td>'
          +'<td colspan=\"5\">Not reported'+(p.expected_date?' &middot; expected '+fmtDate(p.expected_date):'')+'</td></tr>';
      }
    }
    var btn='<button type=\"button\" class=\"rcard-peerres-btn\" data-n=\"'+esc(pr.n_reported)+'\" aria-expanded=\"false\">'
      +'Peer Results ('+esc(pr.n_reported)+' reported) <span class=\"rcard-pr-cx\">&#9656;</span></button>';
    var tbl='<div class=\"rcard-peerres-tbl\" style=\"display:none\"><table class=\"rcard-pr-table\"><thead><tr>'
      +'<th>Peer</th><th>Result date</th><th style=\"text-align:right\">Sales YoY</th>'
      +'<th style=\"text-align:right\">PAT YoY</th><th style=\"text-align:right\">Margin vs LY</th>'
      +'<th style=\"text-align:right\">Move %</th></tr></thead><tbody>'+rows+'</tbody></table>'
      +'<div class=\"rcard-note\">Top-3 by GVM in '+esc(pr.segment||'segment')+' &middot; '+esc(pr.quarter||'')
      +' &middot; YoY (screener export); move = close vs prior close on result day.</div></div>';
    return '<div class=\"rcard-peerres\">'+btn+tbl+'</div>';
  }

  // cc#784: RESULT ANALYSIS V2 — the editorial long-form written by the "polish results" batches.
  // Keep the template card's 4-line metric header (Sales / PAT / Margins / PE) and drop only its
  // 3 narrative template lines, which the long-form replaces. When no V2 row exists, the template
  // card renders unchanged — this never blanks an existing surface.
  // cc#801 fix_4: the 3 narrative template lines ("Strong quarter with growth ahead of the sector",
  // "Revenue outpaced...", "Margin expanded...") are now ALWAYS dropped, not only when a V2 row
  // exists. The cc#797 auto-verdict replaces them by design, and until now BOTH rendered — the old
  // lines above the peer rows and the new verdict inside the QUARTER block, saying the same thing
  // twice in different words. Only the 4 traffic-light metric lines survive here.
  function _metricHeader(txt, v2){
    if (!txt) return '';
    var lines = String(txt).split('\\n');
    var out = [], metrics = 0;
    for (var i = 0; i < lines.length; i++){
      var L = lines[i];
      out.push(L);
      if (/^[\\u{1F7E2}\\u{1F534}\\u{1F7E1}]/u.test(L.trim())) metrics++;
      if (metrics >= 4) break;                        // header ends after the 4 metric lines
    }
    return out.join('\\n');
  }
  function v2Html(v2){
    if (!v2 || !v2.has_analysis || !v2.analysis) return '';
    var id = 'rcv2_' + Math.abs(String(v2.symbol||'x').split('').reduce(function(a,c){return a*31+c.charCodeAt(0)|0;},7));
    var paras = String(v2.analysis).split(/\\n\\s*\\n/).filter(function(p){ return p.trim(); });
    var body = paras.map(function(p){
      return '<p style=\"margin:0 0 9px\">'+esc(p.trim())+'</p>';
    }).join('');
    return '<div class=\"rcard-lbl\">Scorr analysis'+(v2.quarter?' &middot; '+esc(v2.quarter):'')+'</div>'
      + '<div id=\"'+id+'\" class=\"rcard-body rcard-v2\" style=\"max-height:132px;overflow:hidden;position:relative\">'+body+'</div>'
      + '<span class=\"rcard-v2-more\" data-t=\"'+id+'\" role=\"button\" tabindex=\"0\" style=\"display:inline-flex;align-items:center;min-height:38px;font-size:12px;font-weight:700;color:var(--accent,#2f6df4);cursor:pointer\">Read full analysis</span>'
      + (v2.polished_at ? '<div class=\"rcard-note\">'+esc(v2.basis||'Scorr editorial')+' &middot; '+fmtDate(v2.polished_at)+'</div>' : '');
  }

  // cc#623: FY27 Est. Growth — from input_raw.fy27_growth (Sonnet on Trendlyne consensus).
  // cc#788 LEVEL 1: BENCHMARKED. The bare "+13%" said nothing on its own; it is now shown against
  // the actual so the reader can see whether the company is tracking to consensus. `actual` is the
  // reported PAT YoY for the quarter, which the card already has from peer_comparison.profit.stock.
  //
  // HONEST LABELLING: the estimate is a FULL-YEAR consensus and the actual is ONE QUARTER, so this
  // is a directional read, not a like-for-like comparison. The note says so on the card rather than
  // letting a green HIT imply the full year is already banked.
  // Hidden when the estimate is null. Degrades to the plain estimate when no actual exists yet.
  // cc#801 fix_2: FY27 renders STANDALONE — number and label only, NO HIT/MISSED tag.
  // Benchmarking a FULL-YEAR estimate against a SINGLE quarter's actual is not a valid comparison,
  // and a green HIT on that basis reads as "the year is banked" when one quarter says no such thing.
  // I flagged this when the tag shipped in cc#788 and put the caveat in a note; the founder's ruling
  // is that a caveat under a wrong comparison is still a wrong comparison. Tag removed, not softened.
  // Quarterly hit/miss now lives ONLY in the vs-est block (cc#796), which compares like with like.
  function fy27Html(g){
    if (g == null) return '';
    var s = esc(_grow(g) || '--');   // cc#823: growth -> exactly one decimal
    var col = g>=0 ? '#0f9d58' : '#d0433b';
    return '<div class=\"rcard-lbl\">FY27 Est. Growth</div>'
      + '<div style=\"font:800 15px/1.2 Sora,sans-serif;color:'+col+';font-variant-numeric:tabular-nums\">'+s+'</div>'
      + '<div class=\"rcard-note\">Full-year FY27 PAT growth estimate. Quarterly hit/miss is in the vs est. block above.</div>';
  }

  // cc#797 BASIC POLISH L1 — block 1, ABSOLUTES FIRST. The number comes before its deltas because the
  // absolute is the fact and the percentages are commentary on it; a card that leads with "+18% YoY"
  // makes the reader hunt for what actually happened.
  // Traffic light is on the DELTA, not the absolute: green above zero, red below, grey at zero.
  // BFSI is handled by the payload (backend derives it from which metric keys the filing carries),
  // so a bank shows "Financing Margin" and an insurer shows no margin row at all rather than an
  // empty one.
  function _sign(v){ return v==null ? 'var(--mut,#667085)' : (v>0 ? '#0f9d58' : (v<0 ? '#d0433b' : 'var(--mut,#667085)')); }

  // cc#823 NUMBER FORMAT CONTRACT — ONE place, used by every numeral on this card, so the metric
  // header text and the rendered blocks cannot drift into different precisions.
  //   _absN  absolutes  -> integer, Indian digit grouping, no decimals  (4,921 / 87 / 34)
  //   _grow  growth/change -> exactly ONE decimal, signed               (+54.8% / -3.0%)
  // Rounding, never truncation: Math.round and toFixed(1) both round, so 87.33 -> 87 and
  // 54.85 -> 54.9 rather than 54.8. The founder's rule is that an absolute reads as a quantity and
  // a growth figure reads as a rate; two decimals on either is noise on a card this dense.
  function _absN(v){
    if (v==null || v==='' || isNaN(Number(v))) return null;
    // en-IN gives the 4,921 / 12,34,567 lakh-crore grouping the founder asked for.
    try { return Math.round(Number(v)).toLocaleString('en-IN'); }
    catch(e){ return String(Math.round(Number(v))); }
  }
  function _grow(v){
    if (v==null || v==='' || isNaN(Number(v))) return null;
    var n = Number(v);
    return (n>=0?'+':'') + n.toFixed(1) + '%';
  }
  function _pc(v){ var s=_grow(v); return s==null ? '--' : s; }   // growth cells (kept name, new contract)
  function _lvl(v, unit){ var s=_absN(v); return s==null ? '--' : (s + (unit||'')); }   // levels: %, x, plain
  function l1Html(l1, verdict){
    if (!l1) return '';
    var h = '<div class=\"rcard-lbl\">Quarter'+(l1.quarter_label?' &middot; '+esc(l1.quarter_label):'')+'</div>';
    if (verdict) h += '<div class=\"rcard-av\">'+esc(verdict)+'</div>';
    // cc#823 rule_1: YoY LEADS, QoQ is the trailing secondary. A single quarter against the prior
    // quarter is seasonal noise for most of this universe; against the same quarter last year it is
    // a business read. The order on the row is the editorial claim, so YoY goes first.
    // cc#823 rule_2/3: the Rs value is an absolute -> integer with Indian grouping (Rs4,921 Cr).
    function moneyRow(lbl, o){
      if (!o || o.value==null) return '';
      var val = _absN(o.value);
      return '<div class=\"rcard-l1row\"><span class=\"rcard-l1k\">'+lbl+'</span>'
        + '<span class=\"rcard-l1v\">&#8377;'+esc(val==null?o.value:val)+' Cr</span>'
        + '<span class=\"rcard-l1d\" style=\"color:'+_sign(o.yoy)+'\">YoY '+_pc(o.yoy)+'</span>'
        + '<span class=\"rcard-l1d\" style=\"color:'+_sign(o.qoq)+'\">QoQ '+_pc(o.qoq)+'</span></div>';
    }
    h += moneyRow('Sales', l1.sales) + moneyRow('PAT', l1.pat);
    var m = l1.margin;
    if (m && m.now!=null){
      // cc#823: margin LEVELS are absolutes -> integer (34% vs 32% LY). The pp DELTA is a change
      // -> one decimal, same rule as growth.
      h += '<div class=\"rcard-l1row\"><span class=\"rcard-l1k\">'+esc(m.label)+'</span>'
        + '<span class=\"rcard-l1v\">'+esc(_lvl(m.now,'%'))+'</span>'
        + '<span class=\"rcard-l1d\">vs LY '+esc(_lvl(m.ly,'%'))+'</span>'
        + '<span class=\"rcard-l1d\" style=\"color:'+_sign(m.pp)+'\">'
        + (m.pp==null?'--':((m.pp>=0?'+':'')+esc(Number(m.pp).toFixed(1))+'pp'))+'</span></div>';
    }
    var v = l1.valuation || {};
    if (v.pe!=null || v.industry_pe!=null){
      // cc#823: NOT pre-rounded — _grow owns the precision. Rounding to an integer here and then
      // formatting to one decimal would print a fabricated ".0" on a number that was never measured
      // to that precision.
      var prem = (v.pe!=null && v.industry_pe) ? ((v.pe/v.industry_pe-1)*100) : null;
      // cc#823: PE and industry PE are LEVELS -> integer (87x vs 34x, not 87.33x vs 33.54x).
      // The premium is a change vs the sector -> one decimal.
      h += '<div class=\"rcard-l1row\"><span class=\"rcard-l1k\">Valuation</span>'
        + '<span class=\"rcard-l1v\">PE '+esc(_lvl(v.pe,'x'))+'</span>'
        + '<span class=\"rcard-l1d\">industry '+esc(_lvl(v.industry_pe,'x'))+'</span>'
        + (prem==null ? '' : '<span class=\"rcard-l1d\" style=\"color:'+_sign(-prem)+'\">'
             + esc(_grow(prem))+' vs industry</span>')
        + '</div>';
    }
    return h;
  }

  // cc#796 EXPECTATIONS. Screener's CSV carries an expected quarterly sales/profit. It is a MECHANICAL
  // TREND PROJECTION, not analyst consensus — the season measured a median deviation of -16%
  // (155 beats vs 320 misses), which is what a run-rate extrapolation looks like, not a broker forecast.
  // So this NEVER says "analyst estimates" or "street expectations": the label is "vs est." and the
  // tooltip says exactly what it is.
  //
  // The deviation % is shown NEXT TO the tag, never the tag alone — a 2.1% beat and a 60% beat are not
  // the same statement, and the binary hides that. Bands are founder-set: BEAT > +2, IN-LINE +/-2,
  // MISS < -2. No expected value (about 5% of the top 500, ~14% of the full CSV) => the whole line is
  // omitted, never an empty state.
  function expHtml(e){
    if (!e || (!e.sales && !e.profit)) return '';
    function row(lbl, o){
      if (!o) return '';
      var cls = o.tag==='BEAT' ? 'rcard-hit' : (o.tag==='MISS' ? 'rcard-missed' : 'rcard-inline');
      // cc#823: actual and expected are ABSOLUTES -> integer + Indian grouping; the deviation is a
      // change -> one decimal. The BEAT/IN-LINE/MISS bands still key off the raw value, so trimming
      // display precision cannot move a row across a band boundary.
      var dev = _grow(o.dev_pct) || '--';
      var col = o.dev_pct>2 ? '#0f9d58' : (o.dev_pct<-2 ? '#d0433b' : 'var(--mut,#667085)');
      var _a = _absN(o.actual), _e = _absN(o.expected);
      return '<div class=\"rcard-exp-row\"><span class=\"rcard-exp-l\">'+lbl+'</span>'
        + '<span class=\"rcard-exp-v\">'+esc(_a==null?o.actual:_a)+'</span>'
        + '<span class=\"rcard-exp-e\">vs est. '+esc(_e==null?o.expected:_e)+'</span>'
        + '<span class=\"rcard-exp-d\" style=\"color:'+col+'\">'+dev+'</span>'
        + '<span class=\"'+cls+'\">'+esc(o.tag)+'</span></div>';
    }
    var body = row('Sales', e.sales) + row('PAT', e.profit);
    if (!body) return '';
    return '<div class=\"rcard-lbl\" title=\"Screener projected run-rate\">vs est.</div>'
      + '<div class=\"rcard-exp\">'+body+'</div>'
      + '<div class=\"rcard-note\">vs Screener projected run-rate &mdash; a mechanical projection from '
      + 'reported trend, not a broker forecast. Bands: beat above +2%, in-line within 2%, miss below -2%.</div>';
  }

  // cc#788 LEVEL 2: the long-form editorial moves out of the inline Result Analysis block and behind
  // this button, which sits ABOVE the FY27 section. Renders ONLY when result_analysis_v2 actually has
  // a row for THIS symbol and THIS quarter — no row means no button and the card is pure Level 1,
  // never an empty section.
  //
  // Quarter labels differ by source: the card carries "Q1 FY27" (results_endpoints._card_quarter)
  // while result_analysis_v2.quarter is "Q1FY27". Compare on a whitespace-stripped uppercase form or
  // the gate would never open. When the card has no quarter at all, fall back to has_analysis rather
  // than hiding content we do have.
  // cc#801 fix_2: _actualPat is GONE. Its only caller was the FY27 HIT/MISSED tag, and that tag is
  // removed because a full-year estimate cannot be scored against one quarter. Leaving a dead helper
  // behind would invite someone to wire the invalid comparison back up.
  function _qkey(q){ return String(q==null?'':q).replace(/\\s+/g,'').toUpperCase(); }
  function v2Matches(v2, cardQuarter){
    if (!v2 || !v2.has_analysis || !v2.analysis) return false;
    var a = _qkey(v2.quarter), b = _qkey(cardQuarter);
    return (!a || !b) ? true : (a === b);
  }
  // cc#794: coverage level at a glance. DETAILED (violet, filled) when a result_analysis_v2 row exists
  // for this symbol AND quarter; BASIC (grey, outline) otherwise.
  // It shares v2Matches with the View Detailed button DELIBERATELY — one predicate, two uses, so the
  // tag and the button can never disagree. A card claiming DETAILED with no button to open, or a
  // button under a BASIC tag, is exactly the drift this task exists to prevent.
  function levelTag(d){
    var on = v2Matches(d && d.v2, d && d.card_quarter);
    return '<span class=\"' + (on ? 'rcard-lvl rcard-lvl-d' : 'rcard-lvl rcard-lvl-b') + '\" title=\"'
      + (on ? 'Level 2 — full Scorr analysis written for this quarter'
            : 'Level 1 — reported numbers, peers and estimate comparison') + '\">'
      + (on ? 'DETAILED' : 'BASIC') + '</span>';
  }

  function viewDetailedHtml(v2, cardQuarter){
    if (!v2Matches(v2, cardQuarter)) return '';
    var id = 'rcdet_' + Math.abs(String(v2.symbol||'x').split('').reduce(function(a,c){return a*31+c.charCodeAt(0)|0;},7));
    return '<div class=\"rcard-detwrap\">'
      + '<button type=\"button\" class=\"rcard-detbtn\" data-t=\"'+id+'\" aria-expanded=\"false\">'
      +   'View Detailed <span class=\"rcard-detcx\">&#9656;</span></button>'
      + '<div id=\"'+id+'\" class=\"rcard-det\" style=\"display:none\">'+v2Html(v2)+'</div>'
      + '</div>';
  }
  // cc#623/cc#650: RAW headlines (last 7 days) — headline + source + date only, date desc; no
  // summaries; hidden when empty. Capped server-side at the latest 10 so the card stays scannable.
  // cc#802: rawNewsHtml DELETED — the card must not render external links, and leaving a
  // working renderer behind is an invitation to re-add the section. Raw news remains
  // available to the editorial desk and funnel 2; it just never reaches this surface.
  // cc#623: POLISHED news (last 1 month, via polished_news.mentioned_symbols) — headline + summary +
  // source; date desc; hidden when empty. Intel-tab quality.
  function polNewsHtml(items){
    if (!items || !items.length) return '';
    // cc#650: POLISHED news is the LAST section and each item renders BRIEF by default — headline +
    // first ~160 chars of summary + a >=44px "View more" affordance that expands THAT item inline to
    // the full summary (delegated click handler below). AI Editorial items are collapsed too: their
    // full body NEVER renders expanded by default. cc#625 fix_1: rule + gap above the header.
    // cc#802: ONE merged stream, newest first — Domestic shorts, AI Editorial long-form and Stock
    // Views together, each tagged, rather than three sections the reader has to reconcile. Backend
    // already excludes Global and IPO (market-wide, not company news). No item carries a link.
    var h = '<div class=\"rcard-lbl\">News &middot; last 30 days</div>';   // cc#676: shared SectionSeparator
    for (var i=0;i<items.length;i++){ var it=items[i];
      var sum = it.summary || '';
      var over = sum.length > 160;
      var brief = over ? (esc(sum.slice(0,160).replace(/\\s+\\S*$/,'')) + '&hellip;') : esc(sum);
      var cat = it.category || '';
      // AI Editorial is the 2000+ char long-form: tag it so the reader knows the body is worth the
      // expand, and so it is distinguishable from a wire short at a glance.
      var tag = cat === 'AI Editorial' ? '<span class=\"rcard-cat rcard-cat-ai\">AI EDITORIAL</span>'
              : cat === 'Stock Views'  ? '<span class=\"rcard-cat rcard-cat-sv\">STOCK VIEW</span>' : '';
      h += '<div class=\"rcard-pol-item\" style=\"margin-bottom:11px\">'
        + '<div style=\"font-size:12.5px;font-weight:700;color:var(--txt,#1c2536);line-height:1.4\">'+esc(it.headline||'')+tag+'</div>';
      if (sum){
        h += '<div class=\"rcard-pol-brief rcard-body\" style=\"margin-top:3px\">'+brief+'</div>';
        if (over){
          h += '<div class=\"rcard-pol-full rcard-body\" style=\"margin-top:3px;display:none\">'+esc(sum)+'</div>'
            + '<span class=\"rcard-viewmore\" role=\"button\" tabindex=\"0\" style=\"display:inline-flex;align-items:center;min-height:44px;padding:0 2px;font-size:12px;font-weight:700;color:var(--accent,#2f6df4);cursor:pointer;-webkit-tap-highlight-color:transparent\">View more</span>';
        }
      }
      h += '<div class=\"rcard-note\" style=\"margin-top:2px\">'+esc(it.source||'')+(it.published_time?' &middot; '+fmtDate(it.published_time):'')+'</div></div>';
    }
    return h;
  }

  var _cur = null;
  // cc#620/623: ONE shared card builder — R-button modal AND Position News tab (inline). opts.close=false
  // omits the modal close button. Two-branch flow (cc#623 POSITION_NEWS_CARD_V2).
  function cardHtml(d, sym, opts){
    opts = opts || {};
    var status = (d && d.status) || 'date_tbd', pill, cls;
    if (status === 'announced' || status === 'announced_no_analysis'){ pill='Announced'; cls='rcard-st-a'; }
    else if (status === 'upcoming'){ pill='Result due ' + fmtDate(d && d.ex_date); cls='rcard-st-u'; }
    else { pill='Date TBD'; cls='rcard-st-t'; }
    /* cc#753: opts.header===false skips this leading header block (the collapsed Position-News row
       already shows symbol + GVM + status + date, so the expanded body must not repeat them). */
    var h = (opts.header===false) ? '' : ('<div class=\"rcard-hd\"><div><div class=\"rcard-sym\">'+esc(sym)
      + (d && d.gvm_verdict ? '<span class=\"rcard-gvm\">GVM: '+esc(d.gvm_verdict)+'</span>' : '')
      + '</div><span class=\"rcard-status '+cls+'\">'+esc(pill)+'</span>'
      + levelTag(d)   /* cc#794: BASIC / DETAILED coverage tag, next to the status chip */
      + '</div>'
      + (opts.close!==false && window.ScorrCardStripHtml ? window.ScorrCardStripHtml(sym,'R') : '')   /* cc#675: host C·A·R·D strip */
      + (opts.close===false ? '' : '<button class=\"rcard-x\" aria-label=\"Close\">&times;</button>')+'</div>');
    var announced = (status === 'announced' || status === 'announced_no_analysis');

    if (announced){
      // BRANCH A order: date -> Result Analysis (current quarter) -> FY27 -> RAW 48h -> POLISH 1mo.
      h += '<div class=\"rcard-lbl\">Result date</div><div style=\"font-size:13px\">'+fmtDate(d.ex_date)
        + (d && d.card_quarter ? ' &middot; '+esc(d.card_quarter) : '')+'</div>';
      if (d && d.result_analysis){
        // cc#784: when a polish batch has written a long-form V2 analysis for this symbol, keep the
        // 4-line metric header (Sales/PAT/Margins/PE) from the template card and render the editorial
        // body BELOW it, collapsed by default. When no V2 row exists the card is byte-identical to
        // before — the surface degrades to the template rather than showing an empty section.
        // cc#788 LEVEL 1: metric header + peer BEAT/MISS rows, exactly as they rendered before.
        // The long-form editorial no longer sits inline — it moved behind the Level-2 button below.
        h += '<div class=\"rcard-lbl\">Result analysis'+(d.card_quarter?' &middot; '+esc(d.card_quarter):'')+'</div>'
          + '<div class=\"rcard-body\">'+esc(_metricHeader(d.result_analysis, d.v2))+'</div>'
          + peerHtml(d && d.peer_comparison);   // cc#697: peer line merged INTO the Result Analysis block
      } else {
        h += '<div class=\"rcard-body\" style=\"color:var(--mut,#667085)\">Result analysis pending for the current quarter.</div>';
      }
      // cc#788: LEVEL 2 gate sits ABOVE the FY27 section; absent when there is no V2 row for this quarter.
      h += l1Html(d && d.l1, d && d.auto_verdict);   // cc#797 block 1: absolutes-first + deterministic verdict
      h += expHtml(d && d.expectations);   // cc#796: reported quarter vs Screener run-rate; omitted when absent
      h += viewDetailedHtml(d && d.v2, d && d.card_quarter);
      h += fy27Html(d && d.fy27_growth);   // cc#801 fix_2: standalone, no HIT/MISS
    } else {
      // BRANCH B order: expected date + FY27 -> LAST RESULT (prior quarter, explicit label) -> RAW -> POLISH.
      h += '<div class=\"rcard-lbl\">'+(status==='upcoming'?'Expected result':'Result date')+'</div>'
        + '<div style=\"font-size:13px\">'+(status==='upcoming'?fmtDate(d.ex_date):'TBD')+'</div>';
      h += peerResultsHtml(d && d.peer_results);   // cc#766: reported-peer table under the expected date
      // cc#788: pre-result there is no reported quarter to benchmark, so fy27Html degrades to the
      // plain estimate. The Level-2 gate is not offered here — Branch B's analysis is the PRIOR
      // quarter, and result_analysis_v2 is keyed to the current one.
      h += fy27Html(d && d.fy27_growth);   // cc#801 fix_2: standalone, no HIT/MISS
      if (d && d.last_result_analysis){
        h += '<div class=\"rcard-lbl\">Last result'+(d.last_card_quarter?' &middot; '+esc(d.last_card_quarter):'')+'</div>'
          + '<div class=\"rcard-body\">'+esc(d.last_result_analysis)+'</div>'
          + peerHtml(d && d.peer_comparison);   // cc#697: peer line merged into the (last) result block
      }
    }
    // cc#650 order: Result/FY27 (peer line now merged INTO the result block above per cc#697) -> RAW
    // headlines (7 days, headlines only) -> POLISHED news LAST (collapsed per item).
    // cc#802: the RAW headlines section is REMOVED from the card. Its items linked out to
    // GuruFocus/scanx and carried duplicates, so it leaked users off-site from a Scorr surface
    // and showed the same story twice. Raw stays internal fuel only (funnel 2, editorial desk).
    // There is deliberately NO fallback to raw when polished is empty — an empty section is
    // hidden entirely, because a link-out is worse than a shorter card.
    h += polNewsHtml(d && d.polished_news);  // cc#650: POLISHED news LAST, collapsed + view-more
    var foot = [];
    if (d && d.generated_at) foot.push('Generated '+fmtDate(d.generated_at));
    if (d && d.model) foot.push(esc(d.model));
    if (foot.length) h += '<div class=\"rcard-foot\"><span>'+foot.join('</span><span>')+'</span></div>';
    return h;
  }
  function render(d, generating){
    box.innerHTML = cardHtml(d, _cur, {close:true});
    var x = box.querySelector('.rcard-x'); if (x) x.addEventListener('click', close);
    var gb = box.querySelector('[data-gen]'); if (gb) gb.addEventListener('click', function(){ load(_cur, true); });
  }
  // cc#620: inline (non-modal) shared card for the Position News tab — same builder, same chain.
  function renderInline(container, sym){
    if (!container) return;
    container.innerHTML = '<div class=\"rcard-body\" style=\"color:var(--mut,#667085)\">Loading '+esc(sym)+'&hellip;</div>';
    _fetchCard(sym)
      .then(function(d){
        // cc#858: paint the core immediately, with shimmer where the heavy blocks will land.
        var paint = function(dd, o){
          container.innerHTML = cardHtml(dd, sym, {close:false})
            + ((o && o.ctxFailed) ? _ctxFail(o.ctxFailed) : '');
        };
        container.innerHTML = cardHtml(d, sym, {close:false}) + _ctxSkeleton();
        _hydrate(paint, sym, d);
      })
      .catch(function(){ container.innerHTML='<div class=\"rcard-body\" style=\"color:var(--mut,#667085)\">Result card unavailable.</div>'; });
  }
  // cc#753: COLLAPSED-by-default Position-News row. Renders a compact clickable header (symbol +
  // GVM tag + result-status chip + date) and the full shared card body hidden underneath; clicking the
  // header expands it IN PLACE (same builder, header:false so nothing repeats). opts.expanded seeds the
  // open state; opts.onToggle(open) lets the host remember expansion for the session.
  function _statusChip(d){
    var status=(d&&d.status)||'date_tbd';
    if(status==='announced'||status==='announced_no_analysis') return {txt:'ANNOUNCED', cls:'rcard-st-a'};
    if(status==='upcoming') return {txt:'RESULT DUE', cls:'rcard-st-u'};
    return {txt:'DATE TBD', cls:'rcard-st-t'};
  }
  function renderCollapsible(container, sym, opts){
    if(!container) return; opts=opts||{};
    container.innerHTML='<div style=\"padding:10px 12px;color:var(--mut,#667085);font-size:12px\">Loading '+esc(sym)+'&hellip;</div>';
    _fetchCard(sym)
      .then(function(d){
        var sc=_statusChip(d);
        var dateStr=(d&&d.ex_date)?fmtDate(d.ex_date):'';
        var gvm=(d&&d.gvm_verdict)?esc(d.gvm_verdict):'';
        var head='<div class=\"rcol-head\" role=\"button\" tabindex=\"0\" style=\"display:flex;align-items:center;gap:8px;cursor:pointer;padding:10px 12px;user-select:none\">'
          + '<span style=\"font-weight:800;font-size:13px\">'+esc(sym)+'</span>'
          + (gvm?'<span class=\"rcard-gvm\">GVM: '+gvm+'</span>':'')
          + '<span class=\"rcard-status '+sc.cls+'\">'+sc.txt+'</span>'
          + (dateStr?'<span style=\"color:var(--mut,#667085);font-size:11.5px;white-space:nowrap\">'+esc(dateStr)+'</span>':'')
          + '<span class=\"rcol-caret\" style=\"margin-left:auto;color:var(--mut,#667085);font-size:11px\">&#9656;</span></div>';
        var body='<div class=\"rcol-body\" style=\"display:none;padding:0 12px 12px\">'+cardHtml(d, sym, {close:false, header:false})+'</div>';
        container.innerHTML=head+body;
        var hd=container.querySelector('.rcol-head'), bd=container.querySelector('.rcol-body'), ca=container.querySelector('.rcol-caret');
        function setOpen(o){ bd.style.display=o?'block':'none'; ca.innerHTML=o?'&#9662;':'&#9656;'; container.setAttribute('data-open',o?'1':'0'); if(opts.onToggle) try{opts.onToggle(o);}catch(e){} }
        hd.addEventListener('click', function(){ setOpen(bd.style.display==='none'); });
        hd.addEventListener('keydown', function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); setOpen(bd.style.display==='none'); } });
        bd.style.display=opts.expanded?'block':'none'; ca.innerHTML=opts.expanded?'&#9662;':'&#9656;'; container.setAttribute('data-open',opts.expanded?'1':'0');
      })
      .catch(function(){ container.innerHTML='<div class=\"rcol-head\" style=\"padding:10px 12px;display:flex;gap:8px;align-items:center\"><span style=\"font-weight:800;font-size:13px\">'+esc(sym)+'</span><span class=\"rcard-status rcard-st-t\">unavailable</span></div>'; });
  }
  function renderMsg(msg){
    box.innerHTML = '<div class=\"rcard-hd\"><div class=\"rcard-sym\">'+esc(_cur)+'</div>'
      + '<button class=\"rcard-x\">&times;</button></div>'
      + '<div class=\"rcard-body\" style=\"color:var(--mut,#667085)\">'+esc(msg)+'</div>';
    box.querySelector('.rcard-x').addEventListener('click', close);
  }
  function load(sym, gen){
    _cur = sym;
    if (gen) render({status:'upcoming', ex_date:null}, true);
    _fetchCard(sym, gen?'&generate=true':'')
      .then(function(d){
        render(d, false);                       // core paints NOW
        _hydrate(function(dd){ render(dd, false); }, sym, d);   // heavy blocks stream in
      })
      .catch(function(e){ renderMsg('Results card unavailable ('+e.message+'). The backend (cc#572) may still be deploying.'); });
  }
  function open(sym){ if(!sym) return; _cur=sym; box.innerHTML='<div class=\"rcard-body\">Loading '+esc(sym)+'...</div>'; ov.classList.add('on'); load(sym, false); }

  // cc#579: V (volume/energy) card — reuses the same overlay/box. Reads the EXISTING
  // /api/deriv-metrics/{symbol} (zero new backend): energy.volx + recent3d_vol_ratio + meanings.
  function renderV(d){
    var sym=_cur, E=(d&&d.energy)||{}, M=(d&&d.meanings)||{};
    var vx=E.volx, r3=E.recent3d_vol_ratio;
    var h='<div class=\"rcard-hd\"><div><div class=\"rcard-sym\">'+esc(sym)
      + '<span class=\"rcard-gvm\">Volume / Energy</span></div></div>'
      + '<button class=\"rcard-x\" aria-label=\"Close\">&times;</button></div>';
    if (vx==null && r3==null){
      h += '<div class=\"rcard-body\" style=\"color:var(--mut,#667085)\">No volume data for this symbol.</div>';
    } else {
      var vb = vx==null?['vcard-neu','—']:(vx>=1.3?['vcard-bull', esc(M.volx||'energy confirming')]
              :(vx<0.8?['vcard-warn', esc(M.volx||'quiet — move not energy-backed')]:['vcard-neu','neutral']));
      h += '<div class=\"rcard-lbl\">VolX · relative volume</div>'
        + '<div class=\"vcard-hero\"><span class=\"vcard-big\">'+(vx==null?'—':esc(vx)+'×')+'</span>'
        + (E.volx_pct!=null?'<span class=\"vcard-sub\">'+(E.volx_pct>=0?'+':'')+esc(E.volx_pct)+'% vs typical</span>':'')+'</div>'
        + '<div class=\"vcard-band '+vb[0]+'\">'+vb[1]+'</div>';
      var rb = r3==null?['vcard-neu','—']:(r3>=1?['vcard-bull', esc(M.recent3d_vol_ratio||'participation rising')]
              :['vcard-warn', esc(M.recent3d_vol_ratio||'participation drying up')]);
      h += '<div class=\"rcard-lbl\">Recent participation · 3D / 21D</div>'
        + '<div class=\"vcard-hero\"><span class=\"vcard-big\">'+(r3==null?'—':esc(r3)+'×')+'</span></div>'
        + '<div class=\"vcard-band '+rb[0]+'\">'+rb[1]+'</div>';
      if (E.volx_asof) h += '<div class=\"rcard-note\">VolX anchored to the '+esc(E.volx_asof)+' session.</div>';
    }
    box.innerHTML = h;
    box.querySelector('.rcard-x').addEventListener('click', close);
  }
  function loadV(sym){
    _cur=sym;
    fetch('/api/deriv-metrics/'+encodeURIComponent(sym))
      .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
      .then(function(d){ renderV(d); })
      .catch(function(e){ renderMsg('Volume card unavailable ('+e.message+').'); });
  }
  function openV(sym){ if(!sym) return; _cur=sym; box.innerHTML='<div class=\"rcard-body\">Loading '+esc(sym)+'...</div>'; ov.classList.add('on'); loadV(sym); }

  document.addEventListener('click', function(e){
    if (!e.target.closest) return;
    var rp = e.target.closest('.rcard-pill');
    if (rp && rp.getAttribute('data-sym')){ e.preventDefault(); e.stopPropagation(); open(rp.getAttribute('data-sym')); return; }
    // cc#798: the .vcard-pill handler is RETIRED along with pillV — no surface emits a V pill any
    // more. openV itself stays on the API because the Volume/Energy view is still reachable
    // programmatically; it is the legacy PILL that is gone, not the capability.
  });
  window.ScorrRCard = { open: open, openV: openV, renderInline: renderInline, renderCollapsible: renderCollapsible, close: close,
    // cc#798 DEPRECATED — use window.ScorrCardRow(sym) for the shared C·A·R·D strip. Kept only for
    // the documented raw-span pattern; every page call site was migrated.
    pill: function(sym){ return sym ? '<span class=\"rcard-pill\" data-sym=\"'+esc(sym)+'\" title=\"Results / Scorr View\">R</span>' : ''; } };
})();
"""


@router.get("/results_card.js")
def pwa_results_card_js():
    # cc#573: shared Results "R" pill + card modal, injected site-wide alongside mobile.css.
    return Response(RESULTS_CARD_JS, media_type="application/javascript", headers=_CACHE_1D)


# cc#706: shared V8-type price chart card, source-of-truth lives in scorr_chart_card.js (repo root),
# read once at import and served here so it can be injected site-wide (main.py _INJECT).
import os as _os
try:
    with open(_os.path.join(_os.path.dirname(__file__), "scorr_chart_card.js"), "r", encoding="utf-8") as _scc_f:
        SCORR_CHART_CARD_JS = _scc_f.read()
except Exception:
    SCORR_CHART_CARD_JS = "/* scorr_chart_card.js unavailable */"


@router.get("/scorr_chart_card.js")
def pwa_scorr_chart_card_js():
    return Response(SCORR_CHART_CARD_JS, media_type="application/javascript", headers=_CACHE_1D)


# cc#789: shared C·A·R·D quick-nav strip — SINGLE SOURCE. Was defined inside v8_dashboard.html with a
# divergent inline copy on the GVM page (the A-button drift the founder reported). Source of truth is
# scorr_card_strip.js (repo root), read once at import and injected site-wide (main.py _MOBILE_HEAD),
# same pattern as scorr_chart_card.js.
try:
    with open(_os.path.join(_os.path.dirname(__file__), "scorr_card_strip.js"), "r", encoding="utf-8") as _scs_f:
        SCORR_CARD_STRIP_JS = _scs_f.read()
except Exception:
    SCORR_CARD_STRIP_JS = "/* scorr_card_strip.js unavailable */"


@router.get("/scorr_card_strip.js")
def pwa_scorr_card_strip_js():
    return Response(SCORR_CARD_STRIP_JS, media_type="application/javascript", headers=_CACHE_1D)


# cc#805: the A (Analysis) and D (Derivative Cockpit) cards, extracted out of v8_dashboard.html so the
# C·A·R·D strip can LOCK all four letters to shared components instead of deep-linking A and D back to
# /dashboard. Three files, and the ORDER MATTERS (main.py _MOBILE_HEAD injects them in this order):
#   scorr_card_common.js   the primitives both cards shared with the dashboard page itself
#                          (num/sign/getJSON/newsEsc + the volume tiles, heat cells and sparkline).
#                          It had to be extracted FIRST — num alone had 139 call sites on the
#                          dashboard, so anything else would have meant duplicating it.
#   scorr_analysis_card.js window.ScorrAnalysisCard.open(sym)
#   scorr_cockpit_card.js  window.ScorrCockpitCard.open(sym, side, qty, entry, cmp)
# Same serve pattern as scorr_chart_card.js: source of truth is the repo-root .js file, read once at
# import, served with the shared 1-day cache headers (main.py stamps the URL with the build id).
def _read_root_js(name):
    try:
        with open(_os.path.join(_os.path.dirname(__file__), name), "r", encoding="utf-8") as _f:
            return _f.read()
    except Exception:
        return "/* %s unavailable */" % name


SCORR_CARD_COMMON_JS = _read_root_js("scorr_card_common.js")
# cc#859 Part A: the shared mobile section card — imported by cc#862 and cc#863, never redefined.
SCORR_MOBILE_CARDS_JS = _read_root_js("scorr_mobile_cards.js")
SCORR_ANALYSIS_CARD_JS = _read_root_js("scorr_analysis_card.js")
SCORR_COCKPIT_CARD_JS = _read_root_js("scorr_cockpit_card.js")
# cc#1021: the V8 dashboard's Ladder View row renderer. Served the same way as the card files —
# repo-root .js read once at import — so the dashboard HTML stays a page, not a JS library.
V8_LADDER_V2_JS = _read_root_js("v8_ladder_v2.js")
# cc#1054: the ONE index-tape renderer. Both the V8 Index Intel pane and the standalone V10
# dashboard load this file — a second copy of the SVG builder is exactly what cc#1034 warned
# about, so there is only ever this one.
INDEX_TAPE_CARD_JS = _read_root_js("index_tape_card.js")
# cc#1061: the crosshair-scrub layer the index tape and the PCR trend both attach to, and the
# PCR trend card itself. Both are in the main.py build-id stamp list — cc#1060 was exactly the
# failure of shipping a hardcoded tag for a file that was not stamped.
SCRUB_LAYER_JS = _read_root_js("scrub_layer.js")
PCR_TREND_CARD_JS = _read_root_js("pcr_trend_card.js")


@router.get("/index_tape_card.js")
def pwa_index_tape_card_js():
    return Response(INDEX_TAPE_CARD_JS, media_type="application/javascript", headers=_CACHE_1D)


# cc#1064: the Telemetry Drop token layer. One file, injected on every page (main.py
# _MOBILE_HEAD), build-id stamped there so a deploy busts it — the cc#1060 rule.
try:
    with open(_os.path.join(_os.path.dirname(__file__), "scorr_theme_r5.css"), "r", encoding="utf-8") as _r5f:
        SCORR_THEME_R5_CSS = _r5f.read()
except Exception:
    SCORR_THEME_R5_CSS = ""


@router.get("/static/scorr_theme_r5.css")
def pwa_scorr_theme_r5_css():
    return Response(SCORR_THEME_R5_CSS, media_type="text/css", headers=_CACHE_1D)


# APP_QA_R4 P1: the three-theme token layer (dark / goldday / goldnight), byte-faithful to
# design_refs/scorr_home_themes_R1.html @ 80da8c6. Served the same way scorr_theme_r5.css is —
# file at repo root, read once at import, exposed under the /static/ URL namespace. P1 ships the
# asset only; no page links it until P2.
try:
    with open(_os.path.join(_os.path.dirname(__file__), "scorr_themes.css"), "r", encoding="utf-8") as _thf:
        SCORR_THEMES_CSS = _thf.read()
except Exception:
    SCORR_THEMES_CSS = ""


@router.get("/static/scorr_themes.css")
def pwa_scorr_themes_css():
    return Response(SCORR_THEMES_CSS, media_type="text/css", headers=_CACHE_1D)


# APP_QA_R4 P3/P4/P5: the shared app shell (header + 5-slot nav), byte-faithful to
# design_refs/scorr_appshell_R1.html @ a20ae0d. Shared rather than copied into three pages —
# three hand-maintained copies is the drift cc#789 exists to prevent.
try:
    with open(_os.path.join(_os.path.dirname(__file__), "scorr_appshell.css"), "r", encoding="utf-8") as _asf:
        SCORR_APPSHELL_CSS = _asf.read()
except Exception:
    SCORR_APPSHELL_CSS = ""


@router.get("/static/scorr_appshell.css")
def pwa_scorr_appshell_css():
    return Response(SCORR_APPSHELL_CSS, media_type="text/css", headers=_CACHE_1D)


@router.get("/scrub_layer.js")
def pwa_scrub_layer_js():
    return Response(SCRUB_LAYER_JS, media_type="application/javascript", headers=_CACHE_1D)


@router.get("/pcr_trend_card.js")
def pwa_pcr_trend_card_js():
    return Response(PCR_TREND_CARD_JS, media_type="application/javascript", headers=_CACHE_1D)


@router.get("/scorr_card_common.js")
def pwa_scorr_card_common_js():
    return Response(SCORR_CARD_COMMON_JS, media_type="application/javascript", headers=_CACHE_1D)


@router.get("/scorr_mobile_cards.js")
def pwa_scorr_mobile_cards_js():
    return Response(SCORR_MOBILE_CARDS_JS, media_type="application/javascript", headers=_CACHE_1D)


@router.get("/v8_ladder_v2.js")
def pwa_v8_ladder_v2_js():
    return Response(V8_LADDER_V2_JS, media_type="application/javascript", headers=_CACHE_1D)


@router.get("/scorr_analysis_card.js")
def pwa_scorr_analysis_card_js():
    return Response(SCORR_ANALYSIS_CARD_JS, media_type="application/javascript", headers=_CACHE_1D)


@router.get("/scorr_cockpit_card.js")
def pwa_scorr_cockpit_card_js():
    return Response(SCORR_COCKPIT_CARD_JS, media_type="application/javascript", headers=_CACHE_1D)

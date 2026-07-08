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
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "theme_color": "#2563eb",
    "background_color": "#f6f8fb",
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
const CACHE = 'scorr-pwa-v4';   // cc#328: pwa.js nav rebuilt (bottom nav + More sheet)
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
    m.name = 'theme-color'; m.content = '#2563eb';
    document.head.appendChild(m);
  }

  // 2) register the service worker (root scope)
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/service_worker.js').catch(function () {});
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
      + '    background:#fff;border-top:1px solid #e4e9f1;z-index:9998;'
      + '    box-shadow:0 -1px 6px rgba(20,35,70,.06)}'
      + '  .pwa-mn{flex:1;display:flex;flex-direction:column;align-items:center;'
      + '    justify-content:center;gap:2px;font-size:10px;font-weight:600;'
      + '    color:#5a6781;text-decoration:none;background:none;border:none;'
      + '    font-family:inherit;cursor:pointer;min-height:44px}'
      + '  .pwa-mn .ic{font-size:20px;line-height:1}'
      + '  .pwa-mn.active{color:#2563eb}'
      // cc#328: "More" bottom-sheet — all remaining destinations, 2-col 44px rows
      + '  .pwa-sheet-ov{display:none;position:fixed;inset:0;z-index:9999;'
      + '    background:rgba(15,22,35,.45)}'
      + '  .pwa-sheet-ov.open{display:block}'
      + '  .pwa-sheet{position:fixed;left:0;right:0;bottom:0;z-index:10000;background:#fff;'
      + '    border-radius:16px 16px 0 0;padding:10px 12px calc(14px + env(safe-area-inset-bottom,0px));'
      + '    transform:translateY(100%);transition:transform .22s ease;'
      + '    box-shadow:0 -4px 20px rgba(20,35,70,.18)}'
      + '  .pwa-sheet-ov.open .pwa-sheet{transform:translateY(0)}'
      + '  .pwa-sheet h4{margin:6px 4px 10px;font-size:13px;color:#1c2536;font-weight:700}'
      + '  .pwa-sheet-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}'
      + '  .pwa-sheet-grid a{display:flex;align-items:center;gap:9px;min-height:44px;'
      + '    padding:0 12px;border:1px solid #e4e9f1;border-radius:10px;text-decoration:none;'
      + '    color:#1c2536;font-size:13px;font-weight:600}'
      + '  .pwa-sheet-grid a.active{border-color:#2563eb;color:#2563eb;background:#eff6ff}'
      + '  .pwa-sheet-grid a .ic{font-size:17px;line-height:1}'
      + '  .pwa-install{display:flex;position:fixed;left:0;right:0;bottom:56px;'
      + '    height:48px;align-items:center;gap:10px;padding:0 14px;z-index:9999;'
      + '    background:#2563eb;color:#fff;font-size:12px;font-weight:600}'
      + '  .pwa-install button{margin-left:auto;background:#fff;color:#2563eb;'
      + '    border:none;border-radius:7px;padding:6px 12px;font-size:11px;'
      + '    font-weight:700;font-family:inherit}'
      + '  .pwa-install .x{background:transparent;color:#fff;font-size:16px;'
      + '    padding:4px 6px;margin-left:4px}'
      + '}'
      + '@media(min-width:768px){.pwa-mnav,.pwa-install{display:none!important}}';
    var st = document.createElement('style');
    st.id = 'pwa-style'; st.textContent = css;
    document.head.appendChild(st);
  }

  // cc#328: SINGLE nav source — bottom nav (4 primary + More sheet) AND desktop
  // top-nav both build from THIS array. Pages ship only an empty #scorr-nav placeholder.
  var NAV = [
    ['/', '\\u2302', 'Home'],
    ['/dashboard', '\\u26a1', 'V8'],
    ['/cio2?model=gvm', '\\u25c9', 'GVM'],
    ['/sector', '\\u2297', 'Sector'],
    ['/check', '\\u2713', 'Check'],
    ['/news', '\\ud83d\\udcf0', 'Intel'],
    ['/scanners', '\\u229e', 'Scanners'],
    ['/filters', '\\u25bd', 'V13'],
    ['/screener', '\\u229f', 'V12'],
    ['/cio', '\\u2299', 'Max'],
    ['/fpc', '\\u25e7', 'FPC'],
    ['/quant-basket', '\\u25eb', 'QB'],
    ['/holdings', '\\u25c6', 'Holdings'],
    ['/v10', '\\u25b3', 'V10']
  ];
  var p = location.pathname, qs = location.search;
  function isActive(route) {
    var base = route.split('?')[0];
    if (route.indexOf('model=gvm') > -1) return p === '/cio2' && qs.indexOf('model=gvm') > -1;
    if (base === '/') return p === '/';
    return p === base || p.indexOf(base + '/') === 0;   // prefix match: sub-views highlight
  }
  function navByPath(pp) { for (var i = 0; i < NAV.length; i++) { if (NAV[i][0] === pp) return NAV[i]; } return null; }

  // 4) mobile bottom nav — 4 primary slots + More (opens all-destinations sheet)
  var PRIMARY = ['/', '/dashboard', '/cio2?model=gvm', '/check'];
  if (!document.getElementById('pwa-mobile-nav')) {
    var nav = document.createElement('div');
    nav.className = 'pwa-mnav'; nav.id = 'pwa-mobile-nav';
    var mhtml = '';
    PRIMARY.forEach(function (pp) {
      var it = navByPath(pp); if (!it) return;
      var active = isActive(it[0]) ? ' active' : '';
      mhtml += '<a class="pwa-mn' + active + '" href="' + it[0] + '">'
        + '<span class="ic">' + it[1] + '</span><span>' + it[2] + '</span></a>';
    });
    var inSheet = NAV.some(function (it) { return PRIMARY.indexOf(it[0]) === -1 && isActive(it[0]); });
    mhtml += '<button type="button" class="pwa-mn' + (inSheet ? ' active' : '') + '" id="pwa-more-btn">'
      + '<span class="ic">\\u2261</span><span>More</span></button>';
    nav.innerHTML = mhtml;
    document.body.appendChild(nav);

    // one-time sheet: every remaining destination + Logout
    var ov = document.createElement('div');
    ov.className = 'pwa-sheet-ov'; ov.id = 'pwa-sheet-ov';
    var rows = NAV.filter(function (it) { return PRIMARY.indexOf(it[0]) === -1; })
      .map(function (it) {
        return '<a class="' + (isActive(it[0]) ? 'active' : '') + '" href="' + it[0] + '">'
          + '<span class="ic">' + it[1] + '</span>' + it[2] + '</a>';
      }).join('');
    rows += '<a href="/logout"><span class="ic">\\u23cf</span>Logout</a>';
    ov.innerHTML = '<div class="pwa-sheet"><h4>All destinations</h4>'
      + '<div class="pwa-sheet-grid">' + rows + '</div></div>';
    document.body.appendChild(ov);
    document.getElementById('pwa-more-btn').addEventListener('click', function () { ov.classList.add('open'); });
    ov.addEventListener('click', function (e) { if (e.target === ov) ov.classList.remove('open'); });
  }

  // 6) canonical desktop top-nav — single source of truth (cc_task #80, spec 637):
  //    normalize #scorr-nav on every injected page to the same 12 items, Intel label,
  //    active-by-path. Removes per-page nav drift. Hidden on mobile (bottom nav used).
  if (!document.getElementById('scorr-cnav-style')) {
    var ncss = ''
      + '.scorr-cnav{display:flex;align-items:center;gap:2px;height:46px;background:#fff;'
      + '  border-bottom:1px solid #e4e9f1;padding:0 16px;overflow-x:auto;scrollbar-width:none;'
      + '  position:sticky;top:0;z-index:40;box-shadow:0 1px 4px rgba(20,35,70,.06)}'
      + '.scorr-cnav::-webkit-scrollbar{display:none}'
      + '.scorr-cnav a{display:flex;align-items:center;gap:5px;padding:0 11px;height:46px;'
      + '  text-decoration:none;white-space:nowrap;flex-shrink:0;color:#5a6781;font-size:11.5px;'
      + '  font-weight:600;border-bottom:2px solid transparent;transition:.12s}'
      + '.scorr-cnav a:hover{color:#1c2536}'
      + '.scorr-cnav a.active{border-bottom-color:#2563eb;color:#2563eb}'
      + '.scorr-cnav a .ic{font-size:13px}'
      + '.scorr-cnav .sep{width:1px;height:20px;background:#e4e9f1;flex-shrink:0;margin:0 4px}'
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
    host.innerHTML = NAV.map(function (it, i) {
      var sep = i ? '<span class="sep"></span>' : '';
      var act = isActive(it[0]);
      return sep + '<a' + (act ? ' class="active"' : '') + ' href="' + it[0] + '">'
        + '<span class="ic">' + it[1] + '</span>' + it[2] + '</a>';
    }).join('');
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
      + '  align-items:center;justify-content:flex-end;padding:0 12px;background:#2563eb}'
      + 'html[' + HID + '] #scorr-nav-strip{display:flex}'
      + '.scorr-nav-btn{font-size:11px;padding:4px 10px;border-radius:10px;'
      + '  background:rgba(255,255,255,0.15);color:inherit;cursor:pointer;border:none;'
      + '  margin-left:auto;flex-shrink:0}'
      + '#scorr-nav-strip .scorr-nav-btn{color:#fff}'
      // cc#126: toggle button is fixed top-right (NOT inline in the nav) so it stays
      // visible no matter how wide the nav grows or whether the nav is hidden.
      + '#scorr-nav-toggle-btn{position:fixed;top:7px;right:10px;z-index:9999;'
      + '  background:#2563eb;color:#fff;font-size:11px;font-weight:600;'
      + '  padding:4px 12px;border-radius:12px;border:none;cursor:pointer;margin:0;'
      + '  box-shadow:0 1px 4px rgba(0,0,0,0.25);opacity:.92}'
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
.mtable-detail dt{color:#5a6781;font-weight:600;white-space:nowrap}
.mtable-detail dd{margin:0;font-weight:700;text-align:right}
.mtable .mchev{display:none;color:#94a3b8;transition:transform .15s}
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
    return Response(NAV_TOGGLE_JS, media_type="application/javascript", headers=_NOCACHE)


@router.get("/service_worker.js")
def pwa_service_worker():
    h = dict(_NOCACHE); h["Service-Worker-Allowed"] = "/"
    return Response(SW_JS, media_type="application/javascript", headers=h)


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
    return Response(MOBILE_CSS, media_type="text/css", headers=_NOCACHE)


@router.get("/mobile_tables.js")
def pwa_mobile_tables_js():
    # cc#330 P4: shared table helper, injected site-wide alongside mobile.css.
    return Response(MOBILE_TABLES_JS, media_type="application/javascript", headers=_NOCACHE)

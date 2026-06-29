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
SW_JS = """
const CACHE = 'scorr-pwa-v2';
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
  // Static shell assets: cache-first.
  if (SHELL.includes(url.pathname)) {
    e.respondWith(caches.match(req).then((r) => r || fetch(req)));
  }
});
"""

# ── client bootstrap injected into every Tier-2 page (and the mobile home) ──
PWA_JS = """
(function () {
  if (window.__scorrPwa) return; window.__scorrPwa = true;
  var inIframe = (window.self !== window.top);

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
      + '  body{padding-bottom:62px!important}'
      + '  .pwa-mnav{display:flex;position:fixed;bottom:0;left:0;right:0;height:56px;'
      + '    background:#fff;border-top:1px solid #e4e9f1;z-index:9998;'
      + '    box-shadow:0 -1px 6px rgba(20,35,70,.06)}'
      + '  .pwa-mn{flex:1;display:flex;flex-direction:column;align-items:center;'
      + '    justify-content:center;gap:2px;font-size:9px;font-weight:600;'
      + '    color:#5a6781;text-decoration:none}'
      + '  .pwa-mn .ic{font-size:18px;line-height:1}'
      + '  .pwa-mn.active{color:#2563eb}'
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

  // 4) bottom nav (active item by pathname)
  if (!document.getElementById('pwa-mobile-nav')) {
    var path = location.pathname;
    var items = [
      ['/', '\\u2302', 'Home'],
      ['/cio2?model=gvm', '\\u25ce', 'GVM'],
      ['/check', '\\u2713', 'Check'],
      ['/news', '\\u2605', 'Intel'],
      ['/v10', '\\u25b3', 'V10']
    ];
    var nav = document.createElement('div');
    nav.className = 'pwa-mnav'; nav.id = 'pwa-mobile-nav';
    nav.innerHTML = items.map(function (it) {
      var base = it[0].split('?')[0];
      var active = (path === base) ? ' active' : '';
      return '<a class="pwa-mn' + active + '" href="' + it[0] + '">'
        + '<span class="ic">' + it[1] + '</span><span>' + it[2] + '</span></a>';
    }).join('');
    document.body.appendChild(nav);
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
    var NAV = [
      ['/', '\\u2302', 'Home'],
      ['/dashboard', '\\u26a1', 'V8'],
      ['/cio2?model=gvm', '\\u25c9', 'GVM'],
      ['/sector', '\\u2297', 'Sector'],
      ['/check', '\\u2713', 'Check'],
      ['/news', '\\ud83d\\udcf0', 'Intel'],
      ['/scanners', '\\u229e', 'Scanners'],
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
      return p === base || p.indexOf(base + '/') === 0;
    }
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
  var KEY = 'scorr_nav_hidden';
  var HID = 'data-scorr-nav-hidden';
  var root = document.documentElement;

  function isHidden() { return localStorage.getItem(KEY) === 'true'; }

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
      + '#scorr-nav-strip .scorr-nav-btn{color:#fff}';
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
    localStorage.setItem(KEY, h ? 'true' : 'false');
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
      nav.appendChild(btn);
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

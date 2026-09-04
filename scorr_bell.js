/* scorr_bell.js — cc#1634 · THE ONE BELL. The app pages used to print "Market open/closed · HH:MM"
   plus a feed / book pill in the top-right corner; that fact now lives in each page's own hero
   footer, and the corner shows this bell instead. Badge = the number of MANUAL alerts still open
   (pending or triggered, engine-less). Tap = a popover listing every custom alert; a row opens the
   full Alerts page, the footer offers the existing New Alert flow. One file, mounted wherever a
   page puts <span data-scorr-bell></span> — the app pages (via _MOBILE_HEAD) AND, as of cc#1696,
   the web top nav (pwa.js appends the mount right before the theme pill). Plain words, page
   tokens only, nothing derived here beyond the SAME phrasing the web Alerts table already uses.

   cc#1696 (founder 04-Sep: "All custom alert should display on bell click in app and same bell
   display on web nav also"): rewired from the narrower /api/alerts/pending_manual (pending-only)
   to /api/alerts/list?status=all — the ONE alerts endpoint both the app and web Alerts pages read
   (trade_alerts_web.html, mobile/alerts.html) — filtered here to source_engine-less rows, the
   SAME "manual" convention cc#1693's web table uses. PENDING/TRIGGERED render open with their
   trigger text; APPROVED/DISMISSED collapse under "History (n)" so a long-lived list never
   crowds out what still needs a decision. Badge counts pending+triggered only — a decided alert
   is not "waiting". Engine alerts are filtered OUT entirely (scope item 5): they live on the
   Alerts page / Wall of Trades, and the sheet footer says so with a link. */
(function(){
  'use strict';
  var URL = '/api/alerts/list?status=all&limit=200', ALERTS = '/m/alerts', WEB_ALERTS = '/alerts', REFRESH_MS = 300000;
  var state = { alerts: null, timer: null, open: false, historyOpen: false };
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function num(n){ return typeof n === 'number' && isFinite(n); }
  function px(v){ if(!num(v)) return '—'; return '₹' + (Math.abs(v) >= 1000 ? v.toLocaleString('en-IN', {maximumFractionDigits: 0}) : v.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})); }
  // Alerts URL: the app popover always deep-links to the app Alerts screen; on desktop width
  // (where this bell now also mounts, cc#1696) the web Alerts page is the more useful landing —
  // matched at open time via matchMedia, not hardcoded to one shell.
  function alertsUrl(){ try { return window.matchMedia('(min-width:768px)').matches ? WEB_ALERTS : ALERTS; } catch(e){ return ALERTS; } }
  var BELL = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>';
  function mounts(){ return Array.prototype.slice.call(document.querySelectorAll('[data-scorr-bell]')); }
  // cc#1693's own IST conversion, ported verbatim (API timestamps are UTC "YYYY-MM-DD HH:MM:SS+00:00").
  function istStamp(ts){
    if(!ts) return '';
    var d = new Date(String(ts).replace(' ', 'T'));
    if(isNaN(d)) return String(ts).slice(0, 16);
    var i = new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000);
    var mo = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][i.getMonth()];
    var hh = ('0' + i.getHours()).slice(-2), mm = ('0' + i.getMinutes()).slice(-2);
    return i.getDate() + ' ' + mo + ' · ' + hh + ':' + mm;
  }
  function manualAlerts(){
    var all = (state.alerts || []).filter(function(a){ return !a.source_engine; });
    return {
      pending: all.filter(function(a){ return a.status === 'pending'; }),
      triggered: all.filter(function(a){ return a.status === 'triggered'; }),
      history: all.filter(function(a){ return a.status === 'approved' || a.status === 'dismissed'; }),
      total: all.length,
    };
  }
  function badgeCount(){ var m = manualAlerts(); return m.pending.length + m.triggered.length; }
  function paint(){
    var n = state.alerts ? badgeCount() : 0;
    mounts().forEach(function(m){
      if(!m.querySelector('button')){
        m.innerHTML = '<button type="button" aria-label="Custom alerts" style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--line,var(--edge,#2A2A31));background:var(--well,transparent);color:var(--chalk,var(--muted,#F5F2EA));cursor:pointer;padding:0">'
          + BELL + '<span data-bell-badge style="display:none;position:absolute;top:-5px;right:-5px;min-width:17px;height:17px;padding:0 4px;border-radius:9px;background:var(--gold,var(--pulse,#D4AF37));color:var(--well,#131316);font:800 10px/17px ui-monospace,monospace;text-align:center"></span></button>';
        m.querySelector('button').addEventListener('click', toggle);
      }
      var b = m.querySelector('[data-bell-badge]');
      if(b){ b.textContent = n > 99 ? '99+' : String(n); b.style.display = n > 0 ? 'block' : 'none'; }
      m.querySelector('button').setAttribute('aria-label', n ? n + ' custom alert' + (n === 1 ? '' : 's') + ' waiting' : 'No custom alerts waiting');
    });
  }
  function load(){
    return fetch(URL, {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(d){
      state.alerts = (d && d.alerts) || [];
      paint(); if(state.open) render();
    }).catch(function(){ if(state.alerts === null){ state.alerts = []; state.loadError = true; paint(); if(state.open) render(); } });
  }
  // cc#1696: trigger/live-price phrasing matches trade_alerts_web.html's rowHtml() exactly (the
  // "waits for cross X (live Y)" line), so a founder reading either surface never sees two
  // different sentences describing the same alert.
  function openRow(a){
    var dir = String(a.direction || '').toUpperCase(), sell = dir === 'SELL' || dir === 'SHORT';
    var cond = a.trigger_condition === 'ABOVE' ? '≥' : '≤';
    var when = a.status === 'triggered' ? ('triggered ' + istStamp(a.triggered_at))
      : (num(a.cmp) ? ('waits for cross ' + cond + ' ' + px(a.trigger_price) + ' (live ' + px(a.cmp) + (a.cmp_live === false ? ', last close' : '') + ')')
                    : 'waits for cross ' + cond + ' ' + px(a.trigger_price) + ' (no live price right now)');
    return '<a href="' + alertsUrl() + '" style="display:block;padding:10px 12px;border-bottom:1px solid var(--line,var(--edge,#2A2A31));text-decoration:none;color:inherit">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><b style="font-size:14px">' + esc(a.symbol) + '</b>'
      + '<span style="font:700 10px/1 ui-monospace,monospace;letter-spacing:1px;padding:4px 7px;border-radius:6px;color:' + (sell ? 'var(--down,var(--red,#FF5C6C))' : 'var(--up,var(--grn,#2FD48B))') + ';border:1px solid currentColor">' + esc(dir || '—') + (a.status === 'triggered' ? ' · TRIGGERED' : '') + '</span></div>'
      + '<div style="margin-top:4px;font:12px/1.4 ui-monospace,monospace;color:var(--chalk,var(--muted,#F5F2EA))">' + esc(when) + '</div></a>';
  }
  function historyRow(a){
    var dir = String(a.direction || '').toUpperCase(), sell = dir === 'SELL' || dir === 'SHORT';
    var decided = a.status === 'approved'
      ? ('approved' + (a.approved_price != null ? ' @ ' + px(a.approved_price) : '') + (a.approved_at ? ' · ' + istStamp(a.approved_at) : ''))
      : 'dismissed';
    return '<a href="' + alertsUrl() + '" style="display:block;padding:8px 12px;border-bottom:1px solid var(--line,var(--edge,#2A2A31));text-decoration:none;color:inherit;opacity:.72">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><b style="font-size:13px">' + esc(a.symbol) + '</b>'
      + '<span style="font:700 9.5px/1 ui-monospace,monospace;color:var(--mute,var(--muted,#8A8A93))">' + esc(dir) + '</span></div>'
      + '<div style="margin-top:3px;font:11px/1.3 ui-monospace,monospace;color:var(--mute,var(--muted,#8A8A93))">' + esc(decided) + '</div></a>';
  }
  function render(){
    var box = document.getElementById('scorr-bell-box'); if(!box) return;
    var m = manualAlerts(), pend = m.pending.length;
    var head = '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 12px 10px;border-bottom:1px solid var(--line,var(--edge,#2A2A31))"><div><div style="font:800 13px/1.2 inherit;color:var(--chalk,var(--muted,#F5F2EA))">Custom alerts</div><div style="margin-top:3px;font:10px/1.2 ui-monospace,monospace;letter-spacing:1.2px;color:var(--mute,var(--muted,#8A8A93));text-transform:uppercase">' + (pend ? pend + ' pending' : (m.total ? 'none pending' : 'none set')) + '</div></div>'
      + '<button type="button" data-bell-close aria-label="Close" style="width:32px;height:32px;border-radius:8px;border:1px solid var(--line,var(--edge,#2A2A31));background:transparent;color:var(--chalk,var(--muted,#F5F2EA));font-size:16px;cursor:pointer">×</button></div>';
    var openList = m.pending.concat(m.triggered);
    var body;
    if(!m.total){
      body = '<div style="padding:18px 12px;text-align:center;color:var(--mute,var(--muted,#8A8A93));font-size:13px">' + (state.loadError ? 'Could not load alerts right now.' : 'No custom alerts set.') + '<div style="margin-top:10px"><a href="' + alertsUrl() + '#new" style="display:inline-block;padding:9px 14px;border-radius:10px;background:var(--gold,var(--pulse,#D4AF37));color:var(--well,#131316);font:800 12px/1 ui-monospace,monospace;letter-spacing:1px;text-decoration:none">+ NEW ALERT</a></div></div>';
    } else {
      body = (openList.length ? openList.map(openRow).join('')
        : '<div style="padding:14px 12px;text-align:center;color:var(--mute,var(--muted,#8A8A93));font-size:12.5px">Nothing waiting right now.</div>');
      if(m.history.length){
        body += '<button type="button" data-bell-history style="display:block;width:100%;text-align:left;padding:9px 12px;border:0;border-top:1px solid var(--line,var(--edge,#2A2A31));border-bottom:1px solid var(--line,var(--edge,#2A2A31));background:var(--well,transparent);color:var(--mute,var(--muted,#8A8A93));font:700 10.5px/1 ui-monospace,monospace;letter-spacing:.6px;cursor:pointer">'
          + (state.historyOpen ? '−' : '+') + ' HISTORY (' + m.history.length + ')</button>';
        if(state.historyOpen) body += m.history.map(historyRow).join('');
      }
    }
    var foot = '<div style="padding:10px 12px;display:flex;flex-direction:column;gap:6px;font:11px/1 ui-monospace,monospace">'
      + '<div style="display:flex;justify-content:space-between;align-items:center"><a href="' + alertsUrl() + '#new" style="color:var(--mute,var(--muted,#8A8A93));text-decoration:none">+ New alert</a><a href="' + alertsUrl() + '" style="color:var(--gold,var(--pulse,#D4AF37));text-decoration:none;font-weight:800">Open Alerts →</a></div>'
      // cc#1696 scope 5: engine alerts are filtered out of this sheet entirely; say where they live.
      + '<div style="color:var(--mute,var(--muted,#8A8A93));font-size:10px">Engine signals are on the <a href="' + alertsUrl() + '" style="color:inherit;text-decoration:underline">Alerts page</a>.</div></div>';
    box.innerHTML = head + '<div style="max-height:min(60vh,420px);overflow-y:auto">' + body + '</div>' + foot;
    var hb = box.querySelector('[data-bell-history]');
    if(hb) hb.addEventListener('click', function(){ state.historyOpen = !state.historyOpen; render(); });
  }
  function close(){ var ov = document.getElementById('scorr-bell-ov'); if(ov) ov.remove(); state.open = false; }
  // cc#1696 scope 4: ONE component, two anchor modes. The app mounts the bell top-right of a
  // narrow viewport (a de-facto bottom-of-header sheet already, since it spans nearly the full
  // width there); the web top nav mounts it inside a much wider bar, so the SAME right-anchored
  // popover reads as a proper corner popover there without any separate markup — anchoring off
  // the mount's own position (not a hardcoded top/right) is what makes one node correct in both.
  function open(){
    close();
    var mount = mounts()[0];
    var anchor = { top: 56, right: 12 };
    if(mount){
      var r = mount.getBoundingClientRect();
      anchor = { top: Math.round(r.bottom + 8), right: Math.max(12, Math.round(window.innerWidth - r.right)) };
    }
    var ov = document.createElement('div'); ov.id = 'scorr-bell-ov';
    ov.style.cssText = 'position:fixed;inset:0;z-index:9000;background:rgba(4,8,18,.55)';
    ov.innerHTML = '<div id="scorr-bell-box" role="dialog" aria-modal="true" aria-label="Custom alerts" style="position:absolute;top:' + anchor.top + 'px;right:' + anchor.right + 'px;width:min(380px,calc(100vw - 24px));background:var(--panel,#17171B);color:var(--chalk,var(--muted,#F5F2EA));border:1px solid var(--line,var(--edge,#2A2A31));border-radius:14px;box-shadow:0 14px 40px rgba(0,0,0,.55);overflow:hidden"></div>';
    ov.addEventListener('click', function(e){ if(e.target === ov || e.target.closest('[data-bell-close]')) close(); });
    document.body.appendChild(ov); state.open = true; render();
    load();
  }
  function toggle(e){ if(e) e.preventDefault(); if(state.open) close(); else open(); }
  document.addEventListener('keydown', function(e){ if(e.key === 'Escape') close(); });
  function boot(){
    if(!mounts().length) return;
    paint(); load();
    if(!state.timer) state.timer = setInterval(function(){ if(document.visibilityState === 'visible') load(); }, REFRESH_MS);
    document.addEventListener('visibilitychange', function(){ if(document.visibilityState === 'visible') load(); });
  }
  window.ScorrBell = { mount: boot, refresh: load, open: open, close: close };
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();

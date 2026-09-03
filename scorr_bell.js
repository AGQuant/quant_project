/* scorr_bell.js — cc#1634 · THE ONE BELL. The app pages used to print "Market open/closed · HH:MM"
   plus a feed / book pill in the top-right corner; that fact now lives in each page's own hero
   footer, and the corner shows this bell instead. Badge = the number of MANUAL price alerts still
   waiting (trade_alerts status pending with no engine origin) from /api/alerts/pending_manual; no
   badge at zero. Tap = a body-level popover listing each one (symbol, direction, trigger, set
   date, distance from the live CMP); a row opens the Alerts page scrolled to that idea, the footer
   opens Alerts, the empty state offers the existing New Alert flow. One file, mounted wherever a
   page puts <span data-scorr-bell></span>. Plain words, page tokens only, nothing derived here. */
(function(){
  'use strict';
  var URL = '/api/alerts/pending_manual', ALERTS = '/m/alerts', REFRESH_MS = 300000;
  var state = { data: null, timer: null, open: false };
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function num(n){ return typeof n === 'number' && isFinite(n); }
  function px(v){ if(!num(v)) return '—'; return '₹' + (Math.abs(v) >= 1000 ? v.toLocaleString('en-IN', {maximumFractionDigits: 0}) : v.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})); }
  var BELL = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/></svg>';
  function mounts(){ return Array.prototype.slice.call(document.querySelectorAll('[data-scorr-bell]')); }
  function paint(){
    var n = state.data ? (state.data.count || 0) : 0;
    mounts().forEach(function(m){
      if(!m.querySelector('button')){
        m.innerHTML = '<button type="button" aria-label="Manual alerts" style="position:relative;display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border-radius:10px;border:1px solid var(--line,#2A2A31);background:var(--well,transparent);color:var(--chalk,#F5F2EA);cursor:pointer;padding:0">'
          + BELL + '<span data-bell-badge style="display:none;position:absolute;top:-5px;right:-5px;min-width:17px;height:17px;padding:0 4px;border-radius:9px;background:var(--gold,#D4AF37);color:var(--well,#131316);font:800 10px/17px ui-monospace,monospace;text-align:center"></span></button>';
        m.querySelector('button').addEventListener('click', toggle);
      }
      var b = m.querySelector('[data-bell-badge]');
      if(b){ b.textContent = n > 99 ? '99+' : String(n); b.style.display = n > 0 ? 'block' : 'none'; }
      m.querySelector('button').setAttribute('aria-label', n ? n + ' manual alert' + (n === 1 ? '' : 's') + ' waiting' : 'No manual alerts');
    });
  }
  function load(){
    return fetch(URL, {cache: 'no-store'}).then(function(r){ return r.json(); }).then(function(d){ state.data = d || {count: 0, alerts: []}; paint(); if(state.open) render(); }).catch(function(){ if(!state.data){ state.data = {count: 0, alerts: [], error: 'unreachable'}; paint(); } });
  }
  function row(a){
    var dir = String(a.direction || '').toUpperCase(), sell = dir === 'SELL' || dir === 'SHORT';
    var cond = a.trigger_condition === 'ABOVE' ? '≥' : a.trigger_condition === 'BELOW' ? '≤' : (a.trigger_condition || '');
    var dist = num(a.distance_pct) ? (Math.abs(a.distance_pct).toFixed(1) + '% ' + (a.distance_pct > 0 ? 'above' : 'below') + ' CMP ' + px(a.cmp)) : (num(a.cmp) ? 'CMP ' + px(a.cmp) : 'no live price');
    return '<a href="' + ALERTS + '#ia-' + encodeURIComponent(a.id) + '" style="display:block;padding:10px 12px;border-bottom:1px solid var(--line,#2A2A31);text-decoration:none;color:inherit">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><b style="font-size:14px">' + esc(a.symbol) + '</b>'
      + '<span style="font:700 10px/1 ui-monospace,monospace;letter-spacing:1px;padding:4px 7px;border-radius:6px;color:' + (sell ? 'var(--down,#FF5C6C)' : 'var(--up,#2FD48B)') + ';border:1px solid currentColor">' + esc(dir || '—') + '</span></div>'
      + '<div style="margin-top:4px;font:12px/1.4 ui-monospace,monospace;color:var(--chalk,#F5F2EA)">triggers ' + esc(cond) + ' ' + px(a.trigger_price) + '</div>'
      + '<div style="margin-top:2px;font:11px/1.4 ui-monospace,monospace;color:var(--mute,#8A8A93)">' + esc(dist) + (a.created_at_ist ? ' · set ' + esc(a.created_at_ist) : '') + '</div></a>';
  }
  function render(){
    var box = document.getElementById('scorr-bell-box'); if(!box) return;
    var d = state.data || {}, list = d.alerts || [], n = list.length;
    var head = '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 12px 10px;border-bottom:1px solid var(--line,#2A2A31)"><div><div style="font:800 13px/1.2 inherit;color:var(--chalk,#F5F2EA)">Manual alerts</div><div style="margin-top:3px;font:10px/1.2 ui-monospace,monospace;letter-spacing:1.2px;color:var(--mute,#8A8A93);text-transform:uppercase">' + (n ? n + ' waiting for price' : 'none waiting') + (d.as_of_ist ? ' · ' + esc(d.as_of_ist) : '') + '</div></div>'
      + '<button type="button" data-bell-close aria-label="Close" style="width:32px;height:32px;border-radius:8px;border:1px solid var(--line,#2A2A31);background:transparent;color:var(--chalk,#F5F2EA);font-size:16px;cursor:pointer">×</button></div>';
    var body = n ? list.map(row).join('')
      : '<div style="padding:18px 12px;text-align:center;color:var(--mute,#8A8A93);font-size:13px">' + (d.error ? 'Could not load alerts right now.' : 'No manual alerts set.') + '<div style="margin-top:10px"><a href="' + ALERTS + '#new" style="display:inline-block;padding:9px 14px;border-radius:10px;background:var(--gold,#D4AF37);color:var(--well,#131316);font:800 12px/1 ui-monospace,monospace;letter-spacing:1px;text-decoration:none">+ NEW ALERT</a></div></div>';
    var foot = '<div style="padding:10px 12px;display:flex;justify-content:space-between;align-items:center;font:11px/1 ui-monospace,monospace"><a href="' + ALERTS + '#new" style="color:var(--mute,#8A8A93);text-decoration:none">+ New alert</a><a href="' + ALERTS + '" style="color:var(--gold,#D4AF37);text-decoration:none;font-weight:800">Open Alerts →</a></div>';
    box.innerHTML = head + '<div style="max-height:min(60vh,420px);overflow-y:auto">' + body + '</div>' + foot;
  }
  function close(){ var ov = document.getElementById('scorr-bell-ov'); if(ov) ov.remove(); state.open = false; }
  function open(){
    close();
    var ov = document.createElement('div'); ov.id = 'scorr-bell-ov';
    ov.style.cssText = 'position:fixed;inset:0;z-index:9000;background:rgba(4,8,18,.55)';
    ov.innerHTML = '<div id="scorr-bell-box" role="dialog" aria-modal="true" aria-label="Manual alerts" style="position:absolute;top:56px;right:12px;width:min(360px,calc(100vw - 24px));background:var(--panel,#17171B);color:var(--chalk,#F5F2EA);border:1px solid var(--line,#2A2A31);border-radius:14px;box-shadow:0 14px 40px rgba(0,0,0,.55);overflow:hidden"></div>';
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

/* scorr_bell.js — cc#1634 · THE ONE BELL, now the ALERT CENTER (cc#2030, founder voice
   13-Sep-2026, no prior session_log entry — recorded here as the first spec). Three parts:
   1) TRIGGERED FEED (top, default view) — the most recently triggered manual alerts, newest
      first, notification-feed style; top 4-5 with a "View more" revealing the rest.
   2) SET ALERT (bottom-left) — opens window.ScorrAlertCreate (cc#1831), the SAME create flow
      cc#2029 just relocated off Wall of Trades — mounted here now, not rebuilt.
   3) VIEW ALERTS (bottom-right) — toggles the panel to the set-but-not-yet-triggered (pending)
      alerts; the same button flips back to the feed.
   One file, mounted wherever a page puts <span data-scorr-bell></span> — every /m/* app page
   (main.py _MOBILE_HEAD) AND the web top nav (pwa.js, cc#1696). Plain words, page tokens only.

   ISOLATION RULE — founder correction, CRITICAL: this panel is PURELY user-level. Every row shown
   is a manual alert the founder personally created via Set Alert — NEVER a Wall of Trades row, an
   engine signal, or an approval of any kind. Verified against the real schema before touching this
   file (not assumed): `trade_alerts.kind` does NOT distinguish manual vs engine — both an
   approve_signal (engine) row and a manual alert insert use kind='entry'; there is no kind='manual'
   value anywhere in the system. The real, verified discriminator, unchanged since cc#1696, is
   `source_engine IS NULL` — that is what every filter below still keys on. Checked against real
   production rows at the time of this rewrite: zero leakage exists today (the one live pending
   manual alert is the only source_engine-less row in the table) — this rewrite changes the LAYOUT,
   not the filter, because the filter was already correct.

   cc#1717 (founder 05-Sep, unchanged by this card): the badge is the UNSEEN count — manual
   pending/triggered alerts nobody has opened the sheet on yet. Seen state lives SERVER-SIDE
   (trade_alert_seen via POST /api/alerts/seen, read back as `seen` on each /api/alerts/list row).
   markSeen()/badgeCount()/paint() are UNTOUCHED by cc#2030 — that mechanism is orthogonal to the
   layout the founder asked to change. Same file ships to /m/* and the web nav — no fork. */
(function(){
  'use strict';
  var URL = '/api/alerts/list?status=all&limit=200', SEEN_URL = '/api/alerts/seen', ALERTS = '/m/alerts', WEB_ALERTS = '/alerts', REFRESH_MS = 300000;
  var FEED_SHOWN = 5;   // founder: "top rows (4-5)" -- 5 chosen, "View more" reveals the rest
  var state = { alerts: null, timer: null, open: false, view: 'feed', feedExpanded: false };
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function num(n){ return typeof n === 'number' && isFinite(n); }
  function px(v){ if(!num(v)) return '—'; return '₹' + (Math.abs(v) >= 1000 ? v.toLocaleString('en-IN', {maximumFractionDigits: 0}) : v.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})); }
  // Alerts URL: a feed/pending row deep-links to the app Alerts screen; on desktop width (where
  // this bell now also mounts, cc#1696) the web Alerts page is the more useful landing — matched
  // at open time via matchMedia, not hardcoded to one shell. Unchanged by cc#2030.
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
  // UNCHANGED by cc#2030 (badge/seen mechanism, orthogonal to the layout redesign): manual =
  // source_engine IS NULL, the one verified-correct discriminator (see the isolation-rule note
  // above — `kind` is not it).
  function manualAlerts(){
    var all = (state.alerts || []).filter(function(a){ return !a.source_engine; });
    return {
      pending: all.filter(function(a){ return a.status === 'pending'; }),
      triggered: all.filter(function(a){ return a.status === 'triggered'; }),
      history: all.filter(function(a){ return a.status === 'approved' || a.status === 'dismissed'; }),
      total: all.length,
    };
  }
  // cc#2030: the TRIGGERED FEED — every manual alert that has actually fired, regardless of what
  // was decided afterwards (still awaiting a decision, approved, or dismissed), newest-triggered-
  // first. Deliberately keyed on triggered_at IS NOT NULL (a real, reliable column) rather than
  // "anything not pending" — a dismissal carries no timestamp of its own on this table (confirmed
  // by reading dismiss_alert()), so triggered_at is the one honest sort key across all three
  // outcomes. A manual alert approved directly from pending (the founder's own pre-trigger
  // override, cc#1586) never sets triggered_at and so does not appear in this feed — it was never
  // "triggered", by construction, and inventing a fallback timestamp for it would be a guess.
  function feedAlerts(){
    return (state.alerts || [])
      .filter(function(a){ return !a.source_engine && a.triggered_at; })
      .sort(function(a, b){ return String(b.triggered_at).localeCompare(String(a.triggered_at)); });
  }
  // cc#1717: badge = UNSEEN waiting alerts (a row the sheet has already been opened on is not news).
  function badgeCount(){ var m = manualAlerts(); return m.pending.concat(m.triggered).filter(function(a){ return !a.seen; }).length; }
  // cc#1717: one POST per sheet open with every waiting id rendered; local rows flip to seen at
  // once so the badge clears now, then the next load() re-reads the server's own flags.
  function markSeen(){
    var m = manualAlerts(), ids = m.pending.concat(m.triggered).filter(function(a){ return !a.seen; }).map(function(a){ return a.id; });
    if(!ids.length) return Promise.resolve();
    (state.alerts || []).forEach(function(a){ if(ids.indexOf(a.id) !== -1) a.seen = true; });
    paint();
    return fetch(SEEN_URL, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ids: ids})})
      .then(function(r){ return r.ok ? r.json() : null; }).catch(function(){ return null; });
  }
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
  // cc#2030: a feed row shows WHAT HAPPENED since it triggered — still waiting on a decision,
  // approved, or dismissed — since the feed spans all three outcomes (see feedAlerts() above).
  function feedRow(a){
    // cc#2095: a Custom Alert row has no single direction/price -- it renders its condition
    // chain instead of the direction pill. Everything else (link, layout, opacity rule) stays
    // identical so the two row types read as one feed, not a bolt-on.
    if(a.alert_type === 'custom'){
      return '<a href="' + alertsUrl() + '" style="display:block;padding:10px 12px;border-bottom:1px solid var(--line,var(--edge,#2A2A31));text-decoration:none;color:inherit">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><b style="font-size:14px">' + esc(a.symbol) + '</b>'
        + '<span style="font:700 10px/1 ui-monospace,monospace;letter-spacing:1px;padding:4px 7px;border-radius:6px;color:var(--gold,var(--pulse,#D4AF37));border:1px solid currentColor">CUSTOM</span></div>'
        + '<div style="margin-top:4px;font:12px/1.4 ui-monospace,monospace;color:var(--chalk,var(--muted,#F5F2EA))">' + esc(a.condition_summary || 'condition met') + ' — triggered ' + istStamp(a.triggered_at) + '</div></a>';
    }
    var dir = String(a.direction || '').toUpperCase(), sell = dir === 'SELL' || dir === 'SHORT';
    var outcome = a.status === 'approved'
        ? ('approved' + (a.approved_price != null ? ' @ ' + px(a.approved_price) : '') + (a.approved_at ? ' · ' + istStamp(a.approved_at) : ''))
      : a.status === 'dismissed' ? 'dismissed'
      : 'triggered ' + istStamp(a.triggered_at) + ' — awaiting a decision';
    return '<a href="' + alertsUrl() + '" style="display:block;padding:10px 12px;border-bottom:1px solid var(--line,var(--edge,#2A2A31));text-decoration:none;color:inherit' + (a.status !== 'pending' && a.status !== 'triggered' ? ';opacity:.72' : '') + '">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><b style="font-size:14px">' + esc(a.symbol) + '</b>'
      + '<span style="font:700 10px/1 ui-monospace,monospace;letter-spacing:1px;padding:4px 7px;border-radius:6px;color:' + (sell ? 'var(--down,var(--red,#FF5C6C))' : 'var(--up,var(--grn,#2FD48B))') + ';border:1px solid currentColor">' + esc(dir || '—') + '</span></div>'
      + '<div style="margin-top:4px;font:12px/1.4 ui-monospace,monospace;color:var(--chalk,var(--muted,#F5F2EA))">' + esc(outcome) + '</div></a>';
  }
  // cc#2030: View Alerts — the set-but-not-yet-triggered alerts ONLY (status==='pending'). A
  // triggered row, decided or not, belongs in the feed above, never here — trigger_condition/
  // trigger_price/live cmp phrasing matches trade_alerts_web.html's rowHtml() exactly, same as
  // the pre-cc#2030 openRow() this replaces.
  function pendingRow(a){
    var dir = String(a.direction || '').toUpperCase(), sell = dir === 'SELL' || dir === 'SHORT';
    var cond = a.trigger_condition === 'ABOVE' ? '≥' : '≤';
    var when = num(a.cmp) ? ('waits for cross ' + cond + ' ' + px(a.trigger_price) + ' (live ' + px(a.cmp) + (a.cmp_live === false ? ', last close' : '') + ')')
                          : 'waits for cross ' + cond + ' ' + px(a.trigger_price) + ' (no live price right now)';
    return '<a href="' + alertsUrl() + '" style="display:block;padding:10px 12px;border-bottom:1px solid var(--line,var(--edge,#2A2A31));text-decoration:none;color:inherit">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><b style="font-size:14px">' + esc(a.symbol) + '</b>'
      + '<span style="font:700 10px/1 ui-monospace,monospace;letter-spacing:1px;padding:4px 7px;border-radius:6px;color:' + (sell ? 'var(--down,var(--red,#FF5C6C))' : 'var(--up,var(--grn,#2FD48B))') + ';border:1px solid currentColor">' + esc(dir || '—') + '</span></div>'
      + '<div style="margin-top:4px;font:12px/1.4 ui-monospace,monospace;color:var(--chalk,var(--muted,#F5F2EA))">' + esc(when) + '</div></a>';
  }
  // cc#2030: Set Alert — mounts window.ScorrAlertCreate (cc#1831), the SAME flow cc#2029 just
  // relocated off Wall of Trades, verbatim, not rebuilt. Closes this popover first (one focused
  // task at a time, not two stacked overlays); on a successful create, load() refreshes the bell's
  // own data so the new alert is reflected next time it is opened.
  function openCreate(){
    close();
    if(window.ScorrAlertCreate) window.ScorrAlertCreate.open(load);
    // ScorrAlertCreate ships as a plain, non-deferred <script> on every page this bell mounts on
    // (main.py _MOBILE_HEAD, pwa.js's web-nav injection) and does not touch the DOM until .open()
    // is called, so by the time a person can click this button the module has long since loaded —
    // this guard exists only so a genuinely missing script fails silently, never a raw
    // ReferenceError thrown at the founder.
  }
  // cc#2095: same mount pattern as openCreate() above -- window.ScorrCustomAlertCreate ships as
  // its own plain <script> alongside ScorrAlertCreate, same guard reasoning.
  function openCustomCreate(){
    close();
    if(window.ScorrCustomAlertCreate) window.ScorrCustomAlertCreate.open(load);
  }
  function render(){
    var box = document.getElementById('scorr-bell-box'); if(!box) return;
    var m = manualAlerts(), feed = feedAlerts(), pendCount = m.pending.length;
    var head = '<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 12px 10px;border-bottom:1px solid var(--line,var(--edge,#2A2A31))"><div><div style="font:800 13px/1.2 inherit;color:var(--chalk,var(--muted,#F5F2EA))">' + (state.view === 'pending' ? 'Pending alerts' : 'Custom alerts') + '</div><div style="margin-top:3px;font:10px/1.2 ui-monospace,monospace;letter-spacing:1.2px;color:var(--mute,var(--muted,#8A8A93));text-transform:uppercase">'
      + (state.view === 'pending' ? (pendCount ? pendCount + ' set, waiting to cross' : 'none set')
                                  : (feed.length ? feed.length + ' triggered' : (state.loadError ? 'could not load' : 'none triggered yet')) + ' · ' + pendCount + ' pending')
      + '</div></div>'
      + '<button type="button" data-bell-close aria-label="Close" style="width:32px;height:32px;border-radius:8px;border:1px solid var(--line,var(--edge,#2A2A31));background:transparent;color:var(--chalk,var(--muted,#F5F2EA));font-size:16px;cursor:pointer">×</button></div>';
    var body;
    if(state.view === 'pending'){
      body = m.pending.length ? m.pending.map(pendingRow).join('')
        : '<div style="padding:18px 12px;text-align:center;color:var(--mute,var(--muted,#8A8A93));font-size:13px">No alerts set right now.</div>';
    } else {
      var shown = state.feedExpanded ? feed : feed.slice(0, FEED_SHOWN);
      body = shown.length ? shown.map(feedRow).join('')
        : '<div style="padding:18px 12px;text-align:center;color:var(--mute,var(--muted,#8A8A93));font-size:13px">' + (state.loadError ? 'Could not load alerts right now.' : 'No alerts have triggered yet.') + '</div>';
      if(!state.feedExpanded && feed.length > FEED_SHOWN){
        body += '<button type="button" data-bell-more style="display:block;width:100%;text-align:center;padding:9px 12px;border:0;border-top:1px solid var(--line,var(--edge,#2A2A31));background:var(--well,transparent);color:var(--mute,var(--muted,#8A8A93));font:700 10.5px/1 ui-monospace,monospace;letter-spacing:.6px;cursor:pointer">− VIEW MORE (' + (feed.length - FEED_SHOWN) + ' EARLIER)</button>';
      }
    }
    var foot = '<div style="padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px">'
      + '<button type="button" data-bell-set style="padding:11px;border-radius:10px;font:800 11.5px/1 ui-monospace,monospace;letter-spacing:.5px;cursor:pointer;border:1px solid var(--gold,var(--pulse,#D4AF37));background:var(--gold,var(--pulse,#D4AF37));color:var(--well,#131316)">＋ SET ALERT</button>'
      + '<button type="button" data-bell-view style="padding:11px;border-radius:10px;font:800 11.5px/1 ui-monospace,monospace;letter-spacing:.5px;cursor:pointer;border:1px solid var(--line,var(--edge,#2A2A31));background:transparent;color:var(--chalk,var(--muted,#F5F2EA))">'
      + (state.view === 'pending' ? '← RECENT' : 'VIEW ALERTS' + (pendCount ? ' (' + pendCount + ')' : '')) + '</button>'
      + '<button type="button" data-bell-custom style="grid-column:1 / -1;padding:11px;border-radius:10px;font:800 11.5px/1 ui-monospace,monospace;letter-spacing:.5px;cursor:pointer;border:1px solid var(--line,var(--edge,#2A2A31));background:transparent;color:var(--chalk,var(--muted,#F5F2EA))">＋ CUSTOM ALERT (multi-condition)</button></div>';
    box.innerHTML = head + '<div style="max-height:min(60vh,420px);overflow-y:auto">' + body + '</div>' + foot;
    var moreBtn = box.querySelector('[data-bell-more]');
    if(moreBtn) moreBtn.addEventListener('click', function(){ state.feedExpanded = true; render(); });
    box.querySelector('[data-bell-view]').addEventListener('click', function(){ state.view = state.view === 'pending' ? 'feed' : 'pending'; state.feedExpanded = false; render(); });
    box.querySelector('[data-bell-set]').addEventListener('click', openCreate);
    box.querySelector('[data-bell-custom]').addEventListener('click', openCustomCreate);
  }
  function close(){ var ov = document.getElementById('scorr-bell-ov'); if(ov) ov.remove(); state.open = false; }
  // cc#1696 scope 4: ONE component, two anchor modes. The app mounts the bell top-right of a
  // narrow viewport (a de-facto bottom-of-header sheet already, since it spans nearly the full
  // width there); the web top nav mounts it inside a much wider bar, so the SAME right-anchored
  // popover reads as a proper corner popover there without any separate markup — anchoring off
  // the mount's own position (not a hardcoded top/right) is what makes one node correct in both.
  // Unchanged by cc#2030.
  function open(){
    close();
    state.view = 'feed'; state.feedExpanded = false;   // cc#2030: always land on the default view
    var mount = mounts()[0];
    var anchor = { top: 56, right: 12 };
    if(mount){
      var r = mount.getBoundingClientRect();
      anchor = { top: Math.round(r.bottom + 8), right: Math.max(12, Math.round(window.innerWidth - r.right)) };
    }
    var ov = document.createElement('div'); ov.id = 'scorr-bell-ov';
    ov.style.cssText = 'position:fixed;inset:0;z-index:9000;background:rgba(4,8,18,.55)';
    ov.innerHTML = '<div id="scorr-bell-box" role="dialog" aria-modal="true" aria-label="Custom alerts" style="position:absolute;top:' + anchor.top + 'px;right:' + anchor.right + 'px;width:min(380px,calc(100vw - 24px));background:var(--panel,#17171B);color:var(--chalk,var(--muted,#F5F2EA));border:1px solid var(--line,var(--edge,#2A2A31));border-radius:14px;box-shadow:0 14px 40px rgba(0,0,0,.55);overflow:hidden"></div>';
    // cc#2025: a click on an anchor INSIDE the sheet (a feed/pending row) must close this popover
    // too — same reasoning as before cc#2030, now applying to the redesigned rows. The Set Alert /
    // View Alerts buttons are <button>, not <a>, and have their OWN explicit handlers (openCreate
    // closes first itself; View Alerts deliberately does NOT close, it switches the view in place)
    // so this generic anchor-close rule does not fire for them.
    ov.addEventListener('click', function(e){ if(e.target === ov || e.target.closest('[data-bell-close]') || e.target.closest('#scorr-bell-box a')) close(); });
    document.body.appendChild(ov); state.open = true; render();
    // cc#1717: mark what is on screen as seen FIRST, then refresh — the GET must read the flags
    // the POST just wrote, never race ahead of it and flip the badge back to 1.
    (state.alerts ? markSeen() : Promise.resolve()).then(load);
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

/* scorr_watchlist_add.js -- cc#2196 WATCHLIST V1 (founder 17-Sep-2026 15:17 IST): the ONE "+" control that
 * puts a stock on a user watchlist, mounted on every mobile stock card that has an action row.
 *
 * ONE COMPONENT, THREE MOUNTS:
 *   1. The shared C·A·R·D strip (scorr_card_strip.js) renders it on the app after the four letters, so every
 *      surface that shows the strip has it with no page code: V8 signal rows and TC Scanner rows (the
 *      tap-reveal strip, scorr_position_row.js), the GVM company view, and the symbol card sheet that opens
 *      on any tapped symbol app-wide (scorr_card_common.js).
 *   2. window.ScorrWatchlistAdd.button(sym, from) -- the same button as an HTML string, for rows with no
 *      strip: Investment Scanner rows, Screener rows, Results movers, the Sector companies table.
 *   3. window.ScorrWatchlistAdd.open(sym, from) -- the sheet itself, for anything else.
 * Tap + -> a bottom sheet listing the user's watchlists (with a "New watchlist" row while under 5) -> tap a list
 * -> POST /api/mobile/watchlists/{id}/items -> toast. The button shows a tick once the symbol sits in any list
 * (one GET /api/mobile/watchlists per page, refreshed after every add; a MutationObserver paints buttons that
 * render later). Site-wide script via main.py _MOBILE_HEAD (same pattern as scorr_card_strip.js); it never
 * touches the DOM until a + is tapped, apart from painting ticks. Page tokens only, with the web token names
 * as fallbacks -- no colour literals. */
(function () {
  if (window.ScorrWatchlistAdd) return;
  var API = '/api/mobile/watchlists', MAX = 5;
  var cache = null, loading = null, ov = null, busy = false;
  var CSS = ''
    + '.scorr-wl-add{position:relative}'
    + '.scorr-wl-add.scorr-wl-in{color:var(--win,var(--grn));border-color:var(--win,var(--grn))}'
    + '.scorr-wl-btn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;border:1px solid var(--edge,var(--line2));'
    + 'background:var(--hi,var(--surface2));color:var(--brand,var(--blu));font:800 17px/1 Sora,system-ui,sans-serif;cursor:pointer;padding:0;flex:none;vertical-align:middle}'
    + '.scorr-wl-btn.scorr-wl-sm{width:26px;height:26px;border-radius:6px;font-size:14px}'
    + '#scorrWlOv{position:fixed;inset:0;z-index:100500;background:color-mix(in srgb, var(--field,var(--ink)) 70%, transparent);display:flex;align-items:flex-end;justify-content:center}'
    + '#scorrWlOv .wl-sh{width:100%;max-width:480px;background:var(--panel);border:1px solid var(--edge,var(--line2));border-radius:20px 20px 0 0;padding:12px 16px calc(16px + env(safe-area-inset-bottom,0px));'
    + 'font-family:Sora,system-ui,sans-serif;color:var(--ink,var(--txt));box-sizing:border-box;max-height:80vh;overflow-y:auto}'
    + '#scorrWlOv .wl-hd{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:4px 0 10px}'
    + '#scorrWlOv .wl-t{font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:var(--muted,var(--mut))}#scorrWlOv .wl-t b{display:block;font-size:16px;letter-spacing:0;text-transform:none;color:var(--ink,var(--txt));margin-top:2px}'
    + '#scorrWlOv .wl-x{width:36px;height:36px;border-radius:10px;border:1px solid var(--edge,var(--line2));background:transparent;color:var(--ink,var(--txt));font-size:18px;cursor:pointer;flex:none}'
    + '#scorrWlOv .wl-row{display:flex;align-items:center;justify-content:space-between;gap:10px;width:100%;text-align:left;padding:13px 12px;margin-top:8px;border:1px solid var(--edge,var(--line2));background:var(--hi,var(--surface2));color:var(--ink,var(--txt));border-radius:12px;font:700 13px/1.3 Sora,system-ui,sans-serif;cursor:pointer;min-height:48px;box-sizing:border-box}'
    + '#scorrWlOv .wl-row small{display:block;font-size:10.5px;font-weight:600;color:var(--muted,var(--mut));margin-top:2px}'
    + '#scorrWlOv .wl-row .wl-tick{color:var(--win,var(--grn));font-weight:800;flex:none}#scorrWlOv .wl-row .wl-go{color:var(--muted,var(--mut));flex:none}'
    + '#scorrWlOv .wl-row.wl-new{background:transparent;border-style:dashed;color:var(--brand,var(--blu))}'
    + '#scorrWlOv .wl-form{display:flex;gap:8px;margin-top:8px}#scorrWlOv .wl-form input{flex:1;min-width:0;min-height:44px;border-radius:12px;border:1px solid var(--edge,var(--line2));background:var(--panel);color:var(--ink,var(--txt));font:600 14px Sora,system-ui,sans-serif;padding:0 12px}'
    + '#scorrWlOv .wl-form button{min-height:44px;padding:0 14px;border-radius:12px;border:none;background:var(--brand,var(--blu));color:var(--field,var(--ink));font:800 13px Sora,system-ui,sans-serif;cursor:pointer}'
    + '#scorrWlOv .wl-msg{font-size:11px;color:var(--muted,var(--mut));margin-top:8px;line-height:1.5;min-height:14px}#scorrWlOv .wl-msg.wl-err{color:var(--loss,var(--red))}'
    + '#scorrWlOv .wl-foot{font-size:10.5px;color:var(--muted,var(--mut));margin-top:10px;line-height:1.5}#scorrWlOv .wl-foot a{color:var(--brand,var(--blu));font-weight:700;text-decoration:none}'
    + '.scorr-wl-toast{position:fixed;left:50%;bottom:88px;transform:translateX(-50%);z-index:100600;background:var(--ink,var(--txt));color:var(--field,var(--surface2));font:700 12.5px Sora,system-ui,sans-serif;padding:10px 14px;border-radius:12px;max-width:calc(100vw - 32px);text-align:center}';
  function css() { if (document.getElementById('scorr-wl-style')) return; var st = document.createElement('style'); st.id = 'scorr-wl-style'; st.appendChild(document.createTextNode(CSS)); (document.head || document.documentElement).appendChild(st); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ft() { return window.fetchWithTimeout || fetch; }
  function load(force) {
    if (cache && !force) return Promise.resolve(cache);
    if (loading && !force) return loading;
    loading = ft()(API, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) { cache = (d && d.lists) ? d : { lists: [], symbols: [], can_create: true, max: MAX }; paint(); return cache; })
      .catch(function () { cache = cache || { lists: [], symbols: [], can_create: true, max: MAX, error: true }; return cache; }).then(function (c) { loading = null; return c; });
    return loading;
  }
  function inAny(sym) { return !!(cache && (cache.symbols || []).indexOf(sym) > -1); }
  function listsOf(sym) { return (cache ? cache.lists || [] : []).filter(function (l) { return (l.symbols || []).indexOf(sym) > -1; }).map(function (l) { return l.name; }); }
  function paint() {
    if (!cache) return;
    var els = document.querySelectorAll('[data-wl-add]');
    for (var i = 0; i < els.length; i++) { var s = els[i].getAttribute('data-wl-add'); var on = inAny(s); els[i].classList.toggle('scorr-wl-in', on); els[i].textContent = on ? '✓' : '＋'; els[i].setAttribute('title', on ? ('On: ' + listsOf(s).join(', ')) : 'Add to a watchlist'); }
  }
  function button(sym, from, small) {
    var s = esc(String(sym || '').toUpperCase()); if (!s) return '';
    return '<button type="button" class="scorr-wl-btn scorr-wl-add' + (small ? ' scorr-wl-sm' : '') + (inAny(s) ? ' scorr-wl-in' : '') + '" data-wl-add="' + s + '" data-wl-from="' + esc(from || '') + '" data-scorr-skip aria-label="Add ' + s + ' to a watchlist" title="Add to a watchlist">' + (inAny(s) ? '✓' : '＋') + '</button>';
  }
  function toast(m) { var t = document.createElement('div'); t.className = 'scorr-wl-toast'; t.textContent = m; document.body.appendChild(t); setTimeout(function () { t.remove(); }, 2200); }
  function close() { if (ov) { ov.remove(); ov = null; } }
  function render(sym, from) {
    var c = cache || { lists: [], can_create: true, max: MAX };
    var rows = (c.lists || []).map(function (l) {
      var has = (l.symbols || []).indexOf(sym) > -1;
      return '<button type="button" class="wl-row" data-wl-list="' + l.id + '"><span>' + esc(l.name) + '<small>' + (l.count || 0) + ' name' + (l.count === 1 ? '' : 's') + (has ? ' · already here' : '') + '</small></span>' + (has ? '<span class="wl-tick">✓</span>' : '<span class="wl-go">›</span>') + '</button>';
    }).join('');
    var canNew = (c.lists || []).length < (c.max || MAX);
    ov.querySelector('.wl-bd').innerHTML = (rows || '<div class="wl-msg">No watchlists yet -- make the first one below.</div>')
      + (canNew ? '<button type="button" class="wl-row wl-new" data-wl-new><span>＋ New watchlist<small>' + (c.lists || []).length + ' of ' + (c.max || MAX) + ' used</small></span></button><div class="wl-form" id="wlForm" style="display:none"><input id="wlName" maxlength="40" placeholder="Name it -- Trading, Investment, Sell…" autocomplete="off"><button type="button" id="wlMake">Create</button></div>'
                : '<div class="wl-msg">' + (c.max || MAX) + ' of ' + (c.max || MAX) + ' watchlists used -- delete one on My Watchlist to make room.</div>')
      + '<div class="wl-msg" id="wlMsg"' + (c.error ? ' class="wl-msg wl-err"' : '') + '>' + (c.error ? 'Could not load your watchlists right now.' : '') + '</div>'
      + '<div class="wl-foot"><a href="/m/mywatchlist">Open My Watchlist ›</a></div>';
    var bd = ov.querySelector('.wl-bd');
    Array.prototype.forEach.call(bd.querySelectorAll('[data-wl-list]'), function (b) { b.addEventListener('click', function () { add(sym, from, Number(b.getAttribute('data-wl-list')), b); }); });
    var nb = bd.querySelector('[data-wl-new]');
    if (nb) nb.addEventListener('click', function () { var f = document.getElementById('wlForm'); f.style.display = f.style.display === 'none' ? 'flex' : 'none'; if (f.style.display === 'flex') { try { document.getElementById('wlName').focus(); } catch (e) {} } });
    var mk = document.getElementById('wlMake');
    if (mk) mk.addEventListener('click', function () { create(sym, from); });
    var inp = document.getElementById('wlName');
    if (inp) inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); create(sym, from); } });
  }
  function msg(t, err) { var m = document.getElementById('wlMsg'); if (m) { m.textContent = t || ''; m.className = 'wl-msg' + (err ? ' wl-err' : ''); } }
  function add(sym, from, wid, btn) {
    if (busy) return; busy = true; if (btn) btn.disabled = true; msg('Adding ' + sym + '…');
    ft()(API + '/' + wid + '/items', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: sym, surface: from || '' }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        busy = false; if (!x.ok || (x.d && x.d.error)) { if (btn) btn.disabled = false; msg((x.d && x.d.error) || 'Could not add.', true); return; }
        var name = (x.d.list && x.d.list.name) || 'the list';
        close(); toast(x.d.already ? (sym + ' is already on ' + name + '.') : (sym + ' added to ' + name + '.'));
        load(true);
      }).catch(function () { busy = false; if (btn) btn.disabled = false; msg('Could not add -- check the connection.', true); });
  }
  function create(sym, from) {
    var inp = document.getElementById('wlName'); var name = (inp && inp.value || '').trim();
    if (!name) { msg('Give the watchlist a name.', true); return; }
    if (busy) return; busy = true; msg('Creating ' + name + '…');
    ft()(API, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        busy = false; if (!x.ok || (x.d && x.d.error)) { msg((x.d && x.d.error) || 'Could not create.', true); return; }
        return load(true).then(function () { add(sym, from, x.d.list.id, null); });
      }).catch(function () { busy = false; msg('Could not create -- check the connection.', true); });
  }
  function open(sym, from) {
    sym = String(sym || '').toUpperCase(); if (!sym) return; css(); close();
    ov = document.createElement('div'); ov.id = 'scorrWlOv'; ov.setAttribute('data-scorr-skip', '1');
    ov.innerHTML = '<div class="wl-sh" role="dialog" aria-modal="true" aria-label="Add to a watchlist"><div class="wl-hd"><div class="wl-t">Add to a watchlist<b>' + esc(sym) + '</b></div><button type="button" class="wl-x" aria-label="Close">×</button></div><div class="wl-bd"><div class="wl-msg">Loading your watchlists…</div></div></div>';
    ov.addEventListener('click', function (e) { if (e.target === ov) close(); });
    ov.querySelector('.wl-x').addEventListener('click', close);
    document.body.appendChild(ov);
    load(false).then(function () { if (ov) render(sym, from); });
  }
  /* the tap: capture phase, so a + sitting inside a row LINK (Investment Scanner, Results movers) opens the sheet
     instead of following the link, and a + inside the C·A·R·D strip never reaches the strip's own dispatch. */
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('[data-wl-add]') : null; if (!b) return;
    e.preventDefault(); e.stopPropagation();
    open(b.getAttribute('data-wl-add'), b.getAttribute('data-wl-from') || '');
  }, true);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  var tick = null;
  function arm() {
    css(); if (document.querySelector('[data-wl-add]')) load(false);
    try { new MutationObserver(function () { if (tick) return; tick = setTimeout(function () { tick = null; if (!cache) { if (document.querySelector('[data-wl-add]')) load(false); } else paint(); }, 120); }).observe(document.body, { childList: true, subtree: true }); } catch (err) {}
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', arm); else arm();
  window.ScorrWatchlistAdd = { button: button, open: open, close: close, refresh: function () { return load(true); }, lists: function () { return cache; } };
})();

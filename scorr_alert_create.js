/* scorr_alert_create.js -- cc#1831 SHARED "+ New alert" (manual/custom price-trigger alert
 * creation), site-wide desktop card.
 * ============================================================================================
 * Founder ruling (cc#1831, session_log 40507 part_3), verbatim: "Create custom alert should also
 * available in WOT instead of alert tab both on web and app."
 *
 * WHY THIS FILE EXISTS. ALERTS_PURE_DISPLAY_V1 (session_log 40507) turns the web Alerts page
 * into a pure Realised/Unrealised display of APPROVED trades (cc#1816). A freshly-created manual
 * alert starts in a waiting/pending state -- it has nowhere left to render on a page that only
 * shows approved, decided rows. So alert CREATION (an action) moves to Wall of Trades, which is
 * where every other pending/approval surface in this product already lives or is moving to
 * (cc#1815's QB rebalance relocation, same destination, different reason -- see cc#1831's own
 * spec for why the two are not merged into one mechanism).
 *
 * This is the SAME form the app (mobile/alerts.html, cc#1507) and the desktop Alerts page
 * (trade_alerts_web.html, cc#1536) already proved out -- lifted here VERBATIM (fields,
 * client-side validation, the /api/gvm/search autocomplete, the POST /api/alerts/create body)
 * rather than rebuilt, and now shared, so trade_alerts_web.html and trade_wall_web.html can never
 * grow two hand-copied modals that drift apart the moment one gets edited (the exact class of bug
 * this codebase spent cc#1828/cc#1830 chasing). Established precedent for this extraction shape:
 * cc#805's scorr_analysis_card.js -- self-owned shell, own scoped CSS, own element ids, published
 * as one small window.Scorr* API.
 *
 * API:  window.ScorrAlertCreate.open(onCreated)  -- onCreated is an OPTIONAL callback, invoked
 *                                                    once after a successful create, so a host
 *                                                    page that renders its own alert/trade list
 *                                                    can refresh it. Omit it on a page that
 *                                                    doesn't need to react (the create still
 *                                                    lands either way -- /api/alerts/create is
 *                                                    the single source of truth).
 *       window.ScorrAlertCreate.close()
 *
 * DEPENDENCIES -- scorr_card_common.js loading first is preferred (its fetchWithTimeout wrapper
 * guarantees the create/search calls settle instead of hanging forever, cc#869), but this file
 * degrades to a bare fetch() if it is somehow the only script on a page, rather than going dark.
 *
 * BACKEND UNTOUCHED. Still POSTs to /api/alerts/create; trade_alerts_endpoints.py still validates
 * and decides. This file only moves WHERE the button and form live, never how an alert is judged.
 */
(function () {
  if (window.ScorrAlertCreate) return;

  var CSS = ''
    + '#scorrAlertOv{display:none;position:fixed;inset:0;background:rgba(10,15,30,.55);z-index:11500;'
    + 'align-items:center;justify-content:center;padding:20px}'
    + '#scorrAlertOv.open{display:flex}'
    + '#scorrAlertOv .sac-box{background:var(--panel,#0e1016);border:1px solid var(--line,#2a2a31);'
    + 'border-radius:12px;max-width:460px;width:100%;padding:18px;box-sizing:border-box;'
    + "font-family:'Sora',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--txt,#e9e9ee)}"
    + '#scorrAlertOv .sac-box *{box-sizing:border-box}'
    + '#scorrAlertOv h2{font-size:14px;margin:0 0 4px}'
    + '#scorrAlertOv .sac-s{font-size:11px;color:var(--mut,#8a8a93);margin-bottom:12px;line-height:1.45}'
    + '#scorrAlertOv .sac-lb{font-size:10px;font-weight:700;letter-spacing:1px;color:var(--mut,#8a8a93);'
    + 'text-transform:uppercase;margin:12px 0 6px}'
    + '#scorrAlertOv .sac-in{width:100%;min-height:40px;border-radius:8px;border:1px solid var(--line2,#33333c);'
    + 'background:var(--card2,#17181f);color:var(--txt,#e9e9ee);font-family:inherit;font-size:13px;padding:9px 11px}'
    + '#scorrAlertOv .sac-res{border:1px solid var(--line,#2a2a31);border-radius:8px;margin-top:6px;'
    + 'overflow:hidden;max-height:220px;overflow-y:auto}'
    + '#scorrAlertOv .sac-ri{display:flex;justify-content:space-between;gap:10px;align-items:baseline;'
    + 'padding:9px 11px;border-bottom:1px solid var(--line2,#33333c);cursor:pointer}'
    + '#scorrAlertOv .sac-ri:hover{background:var(--card2,#17181f)}'
    + '#scorrAlertOv .sac-ri:last-child{border-bottom:none}'
    + '#scorrAlertOv .sac-ri b{font-size:12.5px}'
    + '#scorrAlertOv .sac-ri span{font-size:10.5px;color:var(--mut,#8a8a93);overflow:hidden;'
    + 'text-overflow:ellipsis;white-space:nowrap}'
    + '#scorrAlertOv .sac-seg{display:inline-flex;border:1px solid var(--line2,#33333c);border-radius:8px;overflow:hidden}'
    + '#scorrAlertOv .sac-seg button{font:700 10.5px ui-monospace,monospace;padding:6px 12px;border:none;'
    + 'background:transparent;color:var(--mut,#8a8a93);cursor:pointer;min-height:34px}'
    + '#scorrAlertOv .sac-seg button.on{background:var(--blu,#4d7cfe);color:#fff}'
    + '#scorrAlertOv .sac-segrow{display:flex;gap:12px;flex-wrap:wrap}'
    + '#scorrAlertOv .sac-err{margin-top:10px;font-size:11.5px;color:var(--red,#ff5c6c);line-height:1.5}'
    + '#scorrAlertOv .sac-go{width:100%;min-height:42px;margin-top:16px;border-radius:10px;'
    + 'border:1px solid var(--blu,#4d7cfe);background:var(--blu,#4d7cfe);color:#fff;font-weight:800;'
    + 'font-size:13px;font-family:inherit;cursor:pointer}'
    + '#scorrAlertOv .sac-go[disabled]{opacity:.5;cursor:default}'
    + '#scorrAlertOv .sac-cancel{margin-top:10px;width:100%;padding:8px 16px;border-radius:9px;'
    + 'border:1px solid var(--line,#2a2a31);background:var(--panel,#0e1016);color:var(--txt,#e9e9ee);'
    + 'font-size:12.5px;font-weight:700;cursor:pointer;min-height:38px}'
    + '#scorrAlertOv .sac-cancel:hover{border-color:var(--blu,#4d7cfe)}';

  function _injectStyle() {
    if (document.getElementById('scorr-alert-create-style')) return;
    try {
      var st = document.createElement('style');
      st.id = 'scorr-alert-create-style';
      st.appendChild(document.createTextNode(CSS));
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  var HTML = ''
    + '<div class="sac-box">'
    + '<h2>New alert</h2>'
    + '<div class="sac-s">The server validates the symbol and stays the decider -- an unresolvable '
    + 'name is rejected with the real reason.</div>'
    + '<div class="sac-lb">Symbol</div>'
    + '<input class="sac-in" id="sacQ" type="search" placeholder="Search any stock -- RELIANCE, TCS, LODHA..." autocomplete="off">'
    + '<div id="sacRes"></div>'
    + '<div class="sac-segrow">'
    + '<div><div class="sac-lb">Direction</div>'
    + '<span class="sac-seg" id="sacDir">'
    + '<button type="button" class="on" data-v="BUY">BUY</button>'
    + '<button type="button" data-v="SELL">SELL</button>'
    + '</span></div>'
    + '<div><div class="sac-lb">Trigger</div>'
    + '<span class="sac-seg" id="sacCond">'
    + '<button type="button" class="on" data-v="ABOVE">crosses ABOVE</button>'
    + '<button type="button" data-v="BELOW">crosses BELOW</button>'
    + '</span></div>'
    + '</div>'
    + '<div class="sac-lb">Trigger price</div>'
    + '<input class="sac-in" id="sacPx" type="number" inputmode="decimal" step="0.05" min="0" placeholder="0.00">'
    + '<div class="sac-lb">Notes (optional)</div>'
    + '<input class="sac-in" id="sacNotes" type="text" maxlength="200" placeholder="why this level...">'
    + '<div class="sac-err" id="sacErr" style="display:none"></div>'
    + '<button class="sac-go" id="sacGo">Create alert</button>'
    + '<button class="sac-cancel" id="sacCancel">Cancel</button>'
    + '</div>';

  var FORM = { sym: null, dir: 'BUY', cond: 'ABOVE', busy: false };
  var _t = null, _ov = null, _onCreated = null;

  function _ft() {
    /* cc#878's timeout wrapper when it is available (both current host pages load
       scorr_card_common.js ahead of this file); a bare fetch as a last-resort fallback so this
       card still works if it is ever dropped onto a page that doesn't. */
    return (window.ScorrCardCommon && window.ScorrCardCommon.fetchWithTimeout)
        || window.fetchWithTimeout
        || fetch;
  }

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function _pick(k, v) {
    FORM[k === 'dir' ? 'dir' : 'cond'] = v;
    var host = document.getElementById(k === 'dir' ? 'sacDir' : 'sacCond');
    if (host) Array.prototype.forEach.call(host.querySelectorAll('button'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-v') === v);
    });
  }

  function _search(q) {
    var box = document.getElementById('sacRes');
    if (!box) return;
    if (!q) { box.innerHTML = ''; return; }
    _ft()('/api/gvm/search?q=' + encodeURIComponent(q) + '&limit=8')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var res = d.results || [];
        box.innerHTML = res.length
          ? '<div class="sac-res">' + res.map(function (x) {
              return '<div class="sac-ri" data-sym="' + _esc(x.symbol) + '">' +
                '<b>' + _esc(x.symbol) + '</b><span>' + _esc(x.company_name || '') + '</span></div>';
            }).join('') + '</div>'
          : '<div class="sac-res"><div class="sac-ri"><span>No match -- the server still decides, '
            + 'you can submit the typed symbol.</span></div></div>';
        Array.prototype.forEach.call(box.querySelectorAll('.sac-ri[data-sym]'), function (el) {
          el.addEventListener('click', function () { _sym(el.getAttribute('data-sym')); });
        });
      })
      .catch(function () { /* search is a convenience; the create call validates for real */ });
  }

  function _sym(s) {
    FORM.sym = s;
    document.getElementById('sacQ').value = s;
    document.getElementById('sacRes').innerHTML = '';
  }

  function _submit() {
    if (FORM.busy) return;
    var err = document.getElementById('sacErr'), go = document.getElementById('sacGo');
    var sym = FORM.sym || (document.getElementById('sacQ').value || '').trim().toUpperCase();
    var px = parseFloat(document.getElementById('sacPx').value);
    function fail(msg) { err.style.display = ''; err.textContent = msg; }
    err.style.display = 'none';
    if (!sym) return fail('Pick or type a symbol first.');
    if (!(px > 0)) return fail('Trigger price must be a positive number.');
    FORM.busy = true; go.disabled = true; go.textContent = 'Creating...';
    _ft()('/api/alerts/create', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, direction: FORM.dir, trigger_condition: FORM.cond,
                               trigger_price: px,
                               notes: (document.getElementById('sacNotes').value || '').trim() || null }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) throw new Error((x.d && x.d.detail) || 'create failed');
        close();
        if (typeof _onCreated === 'function') { try { _onCreated(); } catch (e) {} }
      })
      .catch(function (e) {
        FORM.busy = false; go.disabled = false; go.textContent = 'Create alert';
        /* the SERVER's reason verbatim -- an honest rejection, never a generic error */
        fail(e.message);
      });
  }

  function _build() {
    if (_ov) return;
    _injectStyle();
    _ov = document.createElement('div');
    _ov.id = 'scorrAlertOv';
    _ov.innerHTML = HTML;
    _ov.addEventListener('click', function (e) { if (e.target === _ov) close(); });
    document.body.appendChild(_ov);
    document.getElementById('sacGo').addEventListener('click', _submit);
    document.getElementById('sacCancel').addEventListener('click', close);
    Array.prototype.forEach.call(document.querySelectorAll('#sacDir button'), function (b) {
      b.addEventListener('click', function () { _pick('dir', b.getAttribute('data-v')); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('#sacCond button'), function (b) {
      b.addEventListener('click', function () { _pick('cond', b.getAttribute('data-v')); });
    });
    document.getElementById('sacQ').addEventListener('input', function (e) {
      clearTimeout(_t);
      FORM.sym = null;   /* typing invalidates the previous pick */
      var v = e.target.value.trim();
      _t = setTimeout(function () { _search(v); }, 300);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && _ov.classList.contains('open')) close();
    });
  }

  function open(onCreated) {
    _build();
    _onCreated = (typeof onCreated === 'function') ? onCreated : null;
    FORM = { sym: null, dir: 'BUY', cond: 'ABOVE', busy: false };
    document.getElementById('sacQ').value = '';
    document.getElementById('sacPx').value = '';
    document.getElementById('sacNotes').value = '';
    document.getElementById('sacRes').innerHTML = '';
    var err = document.getElementById('sacErr'); err.style.display = 'none';
    var go = document.getElementById('sacGo'); go.disabled = false; go.textContent = 'Create alert';
    _pick('dir', 'BUY'); _pick('cond', 'ABOVE');
    _ov.classList.add('open');
    document.getElementById('sacQ').focus();
  }

  function close() {
    if (_ov) _ov.classList.remove('open');
  }

  window.ScorrAlertCreate = { open: open, close: close };
})();

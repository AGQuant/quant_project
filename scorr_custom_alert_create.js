/* scorr_custom_alert_create.js -- cc#2095 CUSTOM ALERTS V1: the "+" categorized picker + chained
 * condition builder, shared shell for app AND web (same interaction model on both, per the card's
 * own item 7 -- ONE module, mounted from scorr_bell.js's new "+ CUSTOM ALERT" button, never forked).
 *
 * Architecture lifted VERBATIM from scorr_alert_create.js (cc#1831) -- same self-owned overlay
 * shell, own scoped CSS, own element ids, published as one small window.Scorr* API -- so this file
 * reads as a sibling of that one, not a divergent one-off.
 *
 * Condition rows: '+' adds a row (symbol picked once, per-row: category -> metric, operator,
 * threshold). Every row AFTER the first shows a small AND/OR toggle pill on its right edge
 * (default AND, tap to flip) that joins it to the row above -- matches item 7 exactly. The
 * registry (categories + metrics) is read from GET /api/custom_alerts/registry, never
 * hardcoded here -- a metric added there shows up in this picker with no client change.
 *
 * API:  window.ScorrCustomAlertCreate.open(onCreated)  -- same optional-callback contract as
 *                                                          ScorrAlertCreate.
 *       window.ScorrCustomAlertCreate.close()
 *
 * BACKEND: POSTs to /api/custom_alerts/create (custom_alerts.py). This file only builds the
 * request; the server validates every metric_key/operator and decides.
 */
(function () {
  if (window.ScorrCustomAlertCreate) return;

  var CSS = ''
    + '#scorrCacOv{display:none;position:fixed;inset:0;background:rgba(10,15,30,.55);z-index:11500;'
    + 'align-items:center;justify-content:center;padding:20px}'
    + '#scorrCacOv.open{display:flex}'
    + '#scorrCacOv .cac-box{background:var(--panel,#0e1016);border:1px solid var(--line,#2a2a31);'
    + 'border-radius:12px;max-width:480px;width:100%;padding:18px;box-sizing:border-box;'
    + "font-family:'Sora',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--txt,#e9e9ee);"
    + 'max-height:88vh;overflow-y:auto}'
    + '#scorrCacOv .cac-box *{box-sizing:border-box}'
    + '#scorrCacOv h2{font-size:14px;margin:0 0 4px}'
    + '#scorrCacOv .cac-s{font-size:11px;color:var(--mut,#8a8a93);margin-bottom:12px;line-height:1.45}'
    + '#scorrCacOv .cac-lb{font-size:10px;font-weight:700;letter-spacing:1px;color:var(--mut,#8a8a93);'
    + 'text-transform:uppercase;margin:12px 0 6px}'
    + '#scorrCacOv .cac-in{width:100%;min-height:40px;border-radius:8px;border:1px solid var(--line2,#33333c);'
    + 'background:var(--card2,#17181f);color:var(--txt,#e9e9ee);font-family:inherit;font-size:13px;padding:9px 11px}'
    + '#scorrCacOv .cac-res{border:1px solid var(--line,#2a2a31);border-radius:8px;margin-top:6px;'
    + 'overflow:hidden;max-height:180px;overflow-y:auto}'
    + '#scorrCacOv .cac-ri{display:flex;justify-content:space-between;gap:10px;align-items:baseline;'
    + 'padding:9px 11px;border-bottom:1px solid var(--line2,#33333c);cursor:pointer}'
    + '#scorrCacOv .cac-ri:hover{background:var(--card2,#17181f)}'
    + '#scorrCacOv .cac-ri:last-child{border-bottom:none}'
    + '#scorrCacOv .cac-ri b{font-size:12.5px}'
    + '#scorrCacOv .cac-ri span{font-size:10.5px;color:var(--mut,#8a8a93)}'
    + '#scorrCacOv .cac-cond{border:1px solid var(--line2,#33333c);border-radius:10px;padding:10px;margin-top:10px;position:relative}'
    + '#scorrCacOv .cac-join{position:absolute;top:-12px;right:10px;display:flex;border:1px solid var(--blu,#4d7cfe);'
    + 'border-radius:8px;overflow:hidden;background:var(--panel,#0e1016)}'
    + '#scorrCacOv .cac-join button{font:800 9.5px ui-monospace,monospace;padding:4px 8px;border:none;'
    + 'background:transparent;color:var(--mut,#8a8a93);cursor:pointer}'
    + '#scorrCacOv .cac-join button.on{background:var(--blu,#4d7cfe);color:#fff}'
    + '#scorrCacOv .cac-metricbtn{width:100%;text-align:left;padding:9px 11px;border-radius:8px;'
    + 'border:1px solid var(--line2,#33333c);background:var(--card2,#17181f);color:var(--txt,#e9e9ee);'
    + 'font-size:12.5px;cursor:pointer}'
    + '#scorrCacOv .cac-metricbtn.picked{border-color:var(--blu,#4d7cfe);color:#fff}'
    + '#scorrCacOv .cac-row2{display:flex;gap:8px;margin-top:8px}'
    + '#scorrCacOv .cac-row2 .cac-seg{display:inline-flex;border:1px solid var(--line2,#33333c);border-radius:8px;overflow:hidden}'
    + '#scorrCacOv .cac-seg button{font:700 10.5px ui-monospace,monospace;padding:6px 10px;border:none;'
    + 'background:transparent;color:var(--mut,#8a8a93);cursor:pointer;min-height:34px}'
    + '#scorrCacOv .cac-seg button.on{background:var(--blu,#4d7cfe);color:#fff}'
    + '#scorrCacOv .cac-row2 input{flex:1;min-width:0}'
    + '#scorrCacOv .cac-rm{margin-top:8px;font-size:10.5px;color:var(--red,#ff5c6c);background:none;'
    + 'border:none;cursor:pointer;padding:2px 0}'
    + '#scorrCacOv .cac-add{width:100%;margin-top:10px;padding:9px;border-radius:9px;border:1px dashed var(--line2,#33333c);'
    + 'background:transparent;color:var(--mut,#8a8a93);font-size:12px;cursor:pointer}'
    + '#scorrCacOv .cac-err{margin-top:10px;font-size:11.5px;color:var(--red,#ff5c6c);line-height:1.5}'
    + '#scorrCacOv .cac-go{width:100%;min-height:42px;margin-top:16px;border-radius:10px;'
    + 'border:1px solid var(--blu,#4d7cfe);background:var(--blu,#4d7cfe);color:#fff;font-weight:800;'
    + 'font-size:13px;font-family:inherit;cursor:pointer}'
    + '#scorrCacOv .cac-go[disabled]{opacity:.5;cursor:default}'
    + '#scorrCacOv .cac-cancel{margin-top:10px;width:100%;padding:8px 16px;border-radius:9px;'
    + 'border:1px solid var(--line,#2a2a31);background:var(--panel,#0e1016);color:var(--txt,#e9e9ee);'
    + 'font-size:12.5px;font-weight:700;cursor:pointer;min-height:38px}';

  function _injectStyle() {
    if (document.getElementById('scorr-cac-style')) return;
    try {
      var st = document.createElement('style');
      st.id = 'scorr-cac-style';
      st.appendChild(document.createTextNode(CSS));
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function _ft() {
    return (window.ScorrCardCommon && window.ScorrCardCommon.fetchWithTimeout)
        || window.fetchWithTimeout
        || fetch;
  }

  var REGISTRY = null;   // [{category, metrics:[{metric_key,label,unit,cadence}]}], loaded once per open()
  var COND_ID = 0;
  var STATE = { sym: null, conds: [], busy: false, pickerFor: null };
  var _ov = null, _onCreated = null;

  function _loadRegistry() {
    if (REGISTRY) return Promise.resolve(REGISTRY);
    return _ft()('/api/custom_alerts/registry').then(function (r) { return r.json(); })
      .then(function (d) { REGISTRY = d.categories || []; return REGISTRY; })
      .catch(function () { REGISTRY = []; return REGISTRY; });
  }

  function _metricLabel(key) {
    for (var i = 0; i < (REGISTRY || []).length; i++) {
      for (var j = 0; j < REGISTRY[i].metrics.length; j++) {
        if (REGISTRY[i].metrics[j].metric_key === key) return REGISTRY[i].metrics[j];
      }
    }
    return null;
  }

  function _newCond() {
    COND_ID += 1;
    return { id: COND_ID, metric_key: null, operator: 'above', threshold: '', join_operator: 'AND' };
  }

  function _condHtml(c, idx) {
    var m = c.metric_key ? _metricLabel(c.metric_key) : null;
    var joinPill = idx === 0 ? '' :
      '<span class="cac-join" data-cid="' + c.id + '">'
      + '<button type="button" data-jv="AND" class="' + (c.join_operator === 'AND' ? 'on' : '') + '">AND</button>'
      + '<button type="button" data-jv="OR" class="' + (c.join_operator === 'OR' ? 'on' : '') + '">OR</button>'
      + '</span>';
    return '<div class="cac-cond" data-cid="' + c.id + '">' + joinPill
      + '<div class="cac-lb">Condition ' + (idx + 1) + '</div>'
      + '<button type="button" class="cac-metricbtn' + (m ? ' picked' : '') + '" data-pick="' + c.id + '">'
      + (m ? _esc(m.label) + ' (' + _esc(m.unit) + ')' : 'Pick a metric…') + '</button>'
      + '<div id="cacPicker' + c.id + '"></div>'
      + '<div class="cac-row2">'
      + '<span class="cac-seg" data-opseg="' + c.id + '">'
      + '<button type="button" data-v="above" class="' + (c.operator === 'above' ? 'on' : '') + '">above</button>'
      + '<button type="button" data-v="below" class="' + (c.operator === 'below' ? 'on' : '') + '">below</button>'
      + '</span>'
      + '<input class="cac-in" type="number" step="any" placeholder="threshold" data-th="' + c.id + '" value="' + _esc(c.threshold) + '">'
      + '</div>'
      + (idx > 0 ? '<button type="button" class="cac-rm" data-rm="' + c.id + '">remove this condition</button>' : '')
      + '</div>';
  }

  function _renderConds() {
    var host = document.getElementById('cacConds');
    if (!host) return;
    host.innerHTML = STATE.conds.map(_condHtml).join('');
    STATE.conds.forEach(function (c, idx) {
      var box = document.querySelector('.cac-cond[data-cid="' + c.id + '"]');
      if (!box) return;
      var pickBtn = box.querySelector('[data-pick]');
      if (pickBtn) pickBtn.addEventListener('click', function () { _togglePicker(c.id); });
      var seg = box.querySelector('[data-opseg]');
      if (seg) Array.prototype.forEach.call(seg.querySelectorAll('button'), function (b) {
        b.addEventListener('click', function () {
          c.operator = b.getAttribute('data-v');
          Array.prototype.forEach.call(seg.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === b); });
        });
      });
      var th = box.querySelector('[data-th]');
      if (th) th.addEventListener('input', function () { c.threshold = th.value; });
      var join = box.querySelector('.cac-join');
      if (join) Array.prototype.forEach.call(join.querySelectorAll('button'), function (b) {
        b.addEventListener('click', function () {
          c.join_operator = b.getAttribute('data-jv');
          Array.prototype.forEach.call(join.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === b); });
        });
      });
      var rm = box.querySelector('[data-rm]');
      if (rm) rm.addEventListener('click', function () {
        STATE.conds = STATE.conds.filter(function (x) { return x.id !== c.id; });
        _renderConds();
      });
    });
  }

  function _togglePicker(cid) {
    var host = document.getElementById('cacPicker' + cid);
    if (!host) return;
    if (host.innerHTML) { host.innerHTML = ''; return; }
    _loadRegistry().then(function (cats) {
      host.innerHTML = '<div class="cac-res">' + cats.map(function (cat) {
        return '<div class="cac-ri" style="cursor:default;background:rgba(255,255,255,.03)"><b>' + _esc(cat.category) + '</b></div>'
          + cat.metrics.map(function (m) {
              return '<div class="cac-ri" data-mk="' + _esc(m.metric_key) + '"><b>' + _esc(m.label) + '</b><span>' + _esc(m.unit) + '</span></div>';
            }).join('');
      }).join('') + '</div>';
      Array.prototype.forEach.call(host.querySelectorAll('[data-mk]'), function (el) {
        el.addEventListener('click', function () {
          var c = STATE.conds.filter(function (x) { return x.id === cid; })[0];
          if (c) c.metric_key = el.getAttribute('data-mk');
          _renderConds();
        });
      });
    });
  }

  var HTML = ''
    + '<div class="cac-box">'
    + '<h2>New custom alert</h2>'
    + '<div class="cac-s">Chain multiple conditions on one symbol -- each joins the one above it '
    + 'with AND or OR, left to right. The server re-checks the whole chain, never just the newest leg.</div>'
    + '<div class="cac-lb">Symbol</div>'
    + '<input class="cac-in" id="cacQ" type="search" placeholder="Search any stock -- RELIANCE, TCS, LODHA..." autocomplete="off">'
    + '<div id="cacSymRes"></div>'
    + '<div class="cac-lb">Label (optional)</div>'
    + '<input class="cac-in" id="cacLabel" type="text" maxlength="120" placeholder="e.g. breakout watch">'
    + '<div id="cacConds"></div>'
    + '<button type="button" class="cac-add" id="cacAdd">+ add condition</button>'
    + '<div class="cac-err" id="cacErr" style="display:none"></div>'
    + '<button class="cac-go" id="cacGo">Create custom alert</button>'
    + '<button class="cac-cancel" id="cacCancel">Cancel</button>'
    + '</div>';

  function _searchSym(q) {
    var box = document.getElementById('cacSymRes');
    if (!box) return;
    if (!q) { box.innerHTML = ''; return; }
    _ft()('/api/gvm/search?q=' + encodeURIComponent(q) + '&limit=8')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var res = d.results || [];
        box.innerHTML = res.length
          ? '<div class="cac-res">' + res.map(function (x) {
              return '<div class="cac-ri" data-sym="' + _esc(x.symbol) + '">'
                + '<b>' + _esc(x.symbol) + '</b><span>' + _esc(x.company_name || '') + '</span></div>';
            }).join('') + '</div>'
          : '<div class="cac-res"><div class="cac-ri"><span>No match -- the server still decides.</span></div></div>';
        Array.prototype.forEach.call(box.querySelectorAll('.cac-ri[data-sym]'), function (el) {
          el.addEventListener('click', function () {
            STATE.sym = el.getAttribute('data-sym');
            document.getElementById('cacQ').value = STATE.sym;
            box.innerHTML = '';
          });
        });
      })
      .catch(function () {});
  }

  function _submit() {
    if (STATE.busy) return;
    var err = document.getElementById('cacErr'), go = document.getElementById('cacGo');
    var sym = STATE.sym || (document.getElementById('cacQ').value || '').trim().toUpperCase();
    function fail(msg) { err.style.display = ''; err.textContent = msg; }
    err.style.display = 'none';
    if (!sym) return fail('Pick or type a symbol first.');
    if (!STATE.conds.length) return fail('Add at least one condition.');
    var conds = [];
    for (var i = 0; i < STATE.conds.length; i++) {
      var c = STATE.conds[i];
      if (!c.metric_key) return fail('Condition ' + (i + 1) + ': pick a metric.');
      var th = parseFloat(c.threshold);
      if (isNaN(th)) return fail('Condition ' + (i + 1) + ': enter a threshold.');
      conds.push({ metric_key: c.metric_key, operator: c.operator, threshold: th,
                   join_operator: i === 0 ? null : c.join_operator });
    }
    STATE.busy = true; go.disabled = true; go.textContent = 'Creating…';
    _ft()('/api/custom_alerts/create', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: sym, label: (document.getElementById('cacLabel').value || '').trim() || null,
                               conditions: conds }) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (x) {
        if (!x.ok) throw new Error((x.d && x.d.detail) || 'create failed');
        close();
        if (typeof _onCreated === 'function') { try { _onCreated(); } catch (e) {} }
      })
      .catch(function (e) {
        STATE.busy = false; go.disabled = false; go.textContent = 'Create custom alert';
        fail(e.message);
      });
  }

  var _t = null;

  function _build() {
    if (_ov) return;
    _injectStyle();
    _ov = document.createElement('div');
    _ov.id = 'scorrCacOv';
    _ov.innerHTML = HTML;
    _ov.addEventListener('click', function (e) { if (e.target === _ov) close(); });
    document.body.appendChild(_ov);
    document.getElementById('cacGo').addEventListener('click', _submit);
    document.getElementById('cacCancel').addEventListener('click', close);
    document.getElementById('cacAdd').addEventListener('click', function () {
      STATE.conds.push(_newCond());
      _renderConds();
    });
    document.getElementById('cacQ').addEventListener('input', function (e) {
      clearTimeout(_t);
      STATE.sym = null;
      var v = e.target.value.trim();
      _t = setTimeout(function () { _searchSym(v); }, 300);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && _ov.classList.contains('open')) close();
    });
  }

  function open(onCreated) {
    _build();
    _onCreated = (typeof onCreated === 'function') ? onCreated : null;
    STATE = { sym: null, conds: [_newCond()], busy: false, pickerFor: null };
    document.getElementById('cacQ').value = '';
    document.getElementById('cacLabel').value = '';
    document.getElementById('cacSymRes').innerHTML = '';
    var err = document.getElementById('cacErr'); err.style.display = 'none';
    var go = document.getElementById('cacGo'); go.disabled = false; go.textContent = 'Create custom alert';
    _renderConds();
    _loadRegistry();
    _ov.classList.add('open');
    document.getElementById('cacQ').focus();
  }

  function close() {
    if (_ov) _ov.classList.remove('open');
  }

  window.ScorrCustomAlertCreate = { open: open, close: close };
})();

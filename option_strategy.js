/* static/option_strategy.js — cc#2038 OPT sprint 3/5 (shared web+app logic; cc#2039 loads this
 * SAME file rather than duplicating it, per session_log 45192's own dom_ids_shared_web_and_app —
 * one implementation of the behaviour list, wired to the fixed DOM ids every shell provides).
 *
 * Talks only to the five /api/options/* endpoints (cc#2037); computes nothing itself beyond pure
 * display formatting -- no payoff math lives here, ONE_REGISTRY_ONE_DERIVATION_V1.
 */
(function () {
  'use strict';

  var state = {
    underlying: 'NIFTY', expiry: null, lotSize: null, strikeStep: 50, spot: null, atm: null,
    chainRows: [], legs: [], tab: 'build', view: 'bullish', sub: null, templates: [],
    lastResult: null,
  };

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  async function getJSON(url) {
    var r = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(url + ' -> HTTP ' + r.status);
    return r.json();
  }
  async function postJSON(url, body) {
    var r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) { var t = await r.text(); throw new Error(url + ' -> HTTP ' + r.status + ' ' + t.slice(0, 200)); }
    return r.json();
  }
  function fmtNum(v, d) {
    if (v == null) return '—';
    d = d == null ? 0 : d;
    return Number(v).toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d });
  }
  function fmtRupee(v, d) { return v == null ? '—' : ('₹' + fmtNum(v, d)); }
  function fmtTs(s) { return s ? String(s).replace('T', ' ').slice(0, 16) : null; }
  function near(a, b) { return a != null && b != null && Math.abs(a - b) < 0.01; }

  // ── meta + chain ──────────────────────────────────────────────────────────────────────────
  async function loadMeta() {
    var m = await getJSON('/api/options/meta?underlying=' + encodeURIComponent(state.underlying));
    state.lotSize = m.lot_size; state.strikeStep = m.strike_step; state.spot = m.spot.value; state.atm = m.atm.strike;
    var expSel = $('#optExpiry');
    if (expSel) {
      var keep = state.expiry && m.expiries.indexOf(state.expiry) !== -1 ? state.expiry : (m.expiries[0] || null);
      expSel.innerHTML = m.expiries.map(function (e) { return '<option value="' + e + '">' + e + '</option>'; }).join('');
      state.expiry = keep;
      if (state.expiry) expSel.value = state.expiry;
    }
    var spotEl = $('#optSpot'); if (spotEl) spotEl.textContent = fmtNum(state.spot, 2);
    var asofEl = $('#optSpotAsof');
    if (asofEl) asofEl.textContent = m.spot.as_of ? ('as of ' + fmtTs(m.spot.as_of)) : 'no live spot';
    var atmEl = $('#optAtm'); if (atmEl) atmEl.textContent = fmtNum(state.atm, 0);
    var lotEl = $('#optLot'); if (lotEl) lotEl.textContent = state.lotSize != null ? state.lotSize : '—';
    await loadChain();
  }

  async function loadChain() {
    var stale = $('#optStale');
    if (!state.expiry) { state.chainRows = []; if (stale) stale.hidden = true; return; }
    var c = await getJSON('/api/options/chain?underlying=' + encodeURIComponent(state.underlying) + '&expiry=' + encodeURIComponent(state.expiry));
    state.chainRows = c.rows || [];
    if (stale) {
      if (c.stale) { stale.hidden = false; stale.textContent = 'Stale chain — last tick ' + fmtTs(c.as_of); }
      else stale.hidden = true;
    }
  }

  function chainPremium(strike, kind) {
    for (var i = 0; i < state.chainRows.length; i++) {
      if (state.chainRows[i].strike === strike) return kind === 'CE' ? state.chainRows[i].ce_ltp : state.chainRows[i].pe_ltp;
    }
    return null;
  }

  // ── legs (Custom Builder) ─────────────────────────────────────────────────────────────────
  function strikeOptionsHtml(selected) {
    if (!state.chainRows.length) return selected != null ? ('<option value="' + selected + '" selected>' + fmtNum(selected, 0) + '</option>') : '';
    return state.chainRows.map(function (r) {
      return '<option value="' + r.strike + '"' + (r.strike === selected ? ' selected' : '') + '>' + fmtNum(r.strike, 0) + '</option>';
    }).join('');
  }

  // cc#2039: a shell that ships a #optStrikeSheet bottom sheet (the app) gets a tappable button
  // that opens it; a shell without one (the web page) keeps the plain native <select> already
  // verified in cc#2038 -- same function, same file, branching on what the PAGE provides rather
  // than forking the file per shell.
  function hasStrikeSheet() { return !!document.getElementById('optStrikeSheet'); }

  function legRowHtml(l, i) {
    var sideCls = l.side === 'BUY' ? 'b' : 's';
    if (l.kind === 'FUT') {
      return '<div class="leg ' + sideCls + '" data-i="' + i + '">'
        + '<span class="pill ' + sideCls.toLowerCase() + ' side" data-i="' + i + '">' + l.side + '</span>'
        + '<span class="pill k kind" data-i="' + i + '">FUT</span>'
        + '<input class="in strike-entry" data-i="' + i + '" type="number" step="0.05" placeholder="entry price" value="' + (l.strike != null ? l.strike : '') + '">'
        + '<span class="in tiny" style="text-align:center">0 (FUT)</span>'
        + '<input class="in lots" data-i="' + i + '" type="number" min="1" max="50" value="' + l.qty + '">'
        + '<span class="x" data-i="' + i + '">×</span></div>';
    }
    var strikeCtl = hasStrikeSheet()
      ? ('<button type="button" class="in strike-btn" data-i="' + i + '">' + (l.strike != null ? fmtNum(l.strike, 0) : 'pick') + ' ▾</button>')
      : ('<select class="in strike" data-i="' + i + '">' + strikeOptionsHtml(l.strike) + '</select>');
    return '<div class="leg ' + sideCls + '" data-i="' + i + '">'
      + '<span class="pill ' + sideCls.toLowerCase() + ' side" data-i="' + i + '">' + l.side + '</span>'
      + '<span class="pill k kind" data-i="' + i + '">' + l.kind + '</span>'
      + strikeCtl
      + '<input class="in premium' + (l.pf ? ' pf' : '') + '" data-i="' + i + '" type="number" step="0.05" placeholder="no chain row" value="' + (l.premium != null ? l.premium : '') + '">'
      + '<input class="in lots" data-i="' + i + '" type="number" min="1" max="50" value="' + l.qty + '">'
      + '<span class="x" data-i="' + i + '">×</span></div>';
  }

  function renderLegs() {
    var box = $('#optLegs');
    if (!box) return;
    box.innerHTML = state.legs.map(legRowHtml).join('');
    var add = $('#optAddLeg');
    if (add) {
      add.textContent = '+ Add leg (' + state.legs.length + ' of 10)';
      add.disabled = state.legs.length >= 10;
    }
  }

  var KIND_CYCLE = { CE: 'PE', PE: 'FUT', FUT: 'CE' };

  // ── strike bottom sheet (app shell only -- see hasStrikeSheet()) ──────────────────────────
  var _sheetLegIndex = null;
  function openStrikeSheet(i) {
    var sheet = $('#optStrikeSheet'); if (!sheet) return;
    _sheetLegIndex = i;
    var list = $('#optStrikeSheetList');
    if (list) {
      list.innerHTML = state.chainRows.map(function (r) {
        return '<button type="button" class="sheet-row" data-strike="' + r.strike + '">' + fmtNum(r.strike, 0) + '</button>';
      }).join('');
    }
    sheet.classList.add('open');
  }
  function closeStrikeSheet() { var sheet = $('#optStrikeSheet'); if (sheet) sheet.classList.remove('open'); _sheetLegIndex = null; }
  function wireStrikeSheet() {
    var sheet = $('#optStrikeSheet');
    if (!sheet || sheet._wired) return;
    sheet._wired = true;
    sheet.addEventListener('click', function (e) {
      if (e.target === sheet) { closeStrikeSheet(); return; }
      var row = e.target.closest('.sheet-row');
      if (row && _sheetLegIndex != null) {
        var strike = Number(row.getAttribute('data-strike'));
        var i = _sheetLegIndex;
        state.legs[i].strike = strike;
        if (state.legs[i].kind !== 'FUT') {
          var p = chainPremium(strike, state.legs[i].kind);
          state.legs[i].premium = p; state.legs[i].pf = p != null;
        }
        closeStrikeSheet();
        renderLegs();
      }
    });
    var closeBtn = $('#optStrikeSheetClose');
    if (closeBtn) closeBtn.addEventListener('click', closeStrikeSheet);
  }

  function wireLegEvents() {
    var box = $('#optLegs');
    if (!box || box._wired) return;
    box._wired = true;
    wireStrikeSheet();
    box.addEventListener('click', function (e) {
      var side = e.target.closest('.side'); var kind = e.target.closest('.kind'); var x = e.target.closest('.x');
      var strikeBtn = e.target.closest('.strike-btn');
      if (side) { var i = +side.getAttribute('data-i'); state.legs[i].side = state.legs[i].side === 'BUY' ? 'SELL' : 'BUY'; renderLegs(); }
      else if (kind) { var j = +kind.getAttribute('data-i'); state.legs[j].kind = KIND_CYCLE[state.legs[j].kind]; state.legs[j].strike = null; state.legs[j].premium = null; state.legs[j].pf = false; renderLegs(); }
      else if (x) { var k = +x.getAttribute('data-i'); state.legs.splice(k, 1); renderLegs(); }
      else if (strikeBtn) { openStrikeSheet(+strikeBtn.getAttribute('data-i')); }
    });
    box.addEventListener('change', function (e) {
      var t = e.target, i = +t.getAttribute('data-i');
      if (t.classList.contains('strike') || t.classList.contains('strike-entry')) {
        state.legs[i].strike = t.value === '' ? null : Number(t.value);
        if (t.classList.contains('strike') && state.legs[i].kind !== 'FUT') {
          var p = chainPremium(state.legs[i].strike, state.legs[i].kind);
          state.legs[i].premium = p; state.legs[i].pf = p != null;
          renderLegs();
        }
      } else if (t.classList.contains('premium')) {
        state.legs[i].premium = t.value === '' ? null : Number(t.value);
        state.legs[i].pf = false; // cc#2038 behaviour: user edit removes the chain-filled mark
        renderLegs();
      } else if (t.classList.contains('lots')) {
        state.legs[i].qty = Math.max(1, Math.min(50, Number(t.value) || 1));
      }
    });
  }

  function addLeg() {
    if (state.legs.length >= 10) return;
    var strike = state.atm;
    var premium = strike != null ? chainPremium(strike, 'CE') : null;
    state.legs.push({ kind: 'CE', side: 'BUY', strike: strike, premium: premium, qty: 1, pf: premium != null });
    renderLegs();
  }

  // ── Readymade ──────────────────────────────────────────────────────────────────────────────
  async function loadTemplates() {
    state.templates = await getJSON('/api/options/templates?view=' + encodeURIComponent(state.view));
    renderTemplates();
  }

  function renderTemplates() {
    var grid = $('#optGrid');
    if (!grid) return;
    var rows = state.templates.filter(function (t) { return !state.sub || t.sub_view === state.sub; });
    grid.innerHTML = rows.map(function (t) {
      var rp = (t.risk_profile || '').split(' / ');
      return '<div class="tpl" data-id="' + t.id + '"><div><div class="n">' + t.name + '</div>'
        + (t.description ? ('<div class="d">' + t.description + '</div>') : '')
        + '</div><div class="rp">' + rp.map(function (r) { return '<i>' + r + '</i>'; }).join('') + '</div></div>';
    }).join('');
  }

  async function selectTemplate(id) {
    if (!state.expiry) return;
    var r = await postJSON('/api/options/resolve', { template_id: id, underlying: state.underlying, expiry: state.expiry });
    state.legs = r.legs.map(function (l) {
      return { kind: l.kind, side: l.side, strike: l.strike, premium: l.premium, qty: l.qty, pf: l.premium != null && l.kind !== 'FUT' };
    });
    renderLegs();
    setTab('build');
    await calculate();
  }

  // ── tabs ───────────────────────────────────────────────────────────────────────────────────
  function setTab(tab) {
    state.tab = tab;
    $all('#optTabs [data-tab]').forEach(function (el) { el.classList.toggle('on', el.getAttribute('data-tab') === tab); });
    var build = $('#optBuildPane'), ready = $('#optReadyPane');
    if (build) build.hidden = tab !== 'build';
    if (ready) ready.hidden = tab !== 'ready';
  }

  // ── payoff + result render ────────────────────────────────────────────────────────────────
  async function calculate() {
    if (!state.legs.length) return;
    var payload = {
      underlying: state.underlying, lot_size: state.lotSize, spot: state.spot,
      legs: state.legs.map(function (l) { return { kind: l.kind, side: l.side, strike: l.strike, premium: l.premium, qty: l.qty }; }),
    };
    var res = await postJSON('/api/options/payoff', payload);
    state.lastResult = res;
    renderResult(res);
  }

  function substantialCell(el, value, sublabel) {
    el.innerHTML = '<b class="subst" title="Tap to see the rupee figure">Substantial</b><small>' + sublabel + '</small>';
    var b = el.querySelector('.subst');
    var sheet = $('#optSubstSheet');
    b.addEventListener('click', function () {
      if (sheet) {
        var body = $('#optSubstSheetBody');
        if (body) body.textContent = fmtRupee(value, 0) + ' — bounded only by the index reaching zero.';
        sheet.classList.add('open');
      } else {
        b.textContent = fmtRupee(value, 0);
        b.title = 'Bounded only by the index reaching zero';
      }
    }, sheet ? {} : { once: true });
  }

  function wireSubstSheet() {
    var sheet = $('#optSubstSheet');
    if (!sheet || sheet._wired) return;
    sheet._wired = true;
    sheet.addEventListener('click', function (e) { if (e.target === sheet) sheet.classList.remove('open'); });
    var closeBtn = $('#optSubstSheetClose');
    if (closeBtn) closeBtn.addEventListener('click', function () { sheet.classList.remove('open'); });
  }

  function renderResult(res) {
    var box = $('#optRes'); if (box) box.hidden = false;
    var beEl = $('#optBE');
    if (beEl) beEl.innerHTML = (res.breakevens && res.breakevens.length) ? fmtNum(res.breakevens[0], 1) + (res.breakevens[1] != null ? ('<small>and ' + fmtNum(res.breakevens[1], 1) + '</small>') : '') : '—';
    var mpEl = $('#optMaxP');
    if (mpEl) {
      if (res.bounded_by_zero && res.bounded_by_zero.max_profit) substantialCell(mpEl, res.max_profit, 'index to zero');
      else mpEl.innerHTML = res.max_profit === 'Unlimited' ? 'Unlimited' : fmtRupee(res.max_profit, 0);
    }
    var mlEl = $('#optMaxL');
    if (mlEl) {
      if (res.bounded_by_zero && res.bounded_by_zero.max_loss) substantialCell(mlEl, res.max_loss, 'index to zero');
      else mlEl.innerHTML = res.max_loss === 'Unlimited' ? 'Unlimited' : fmtRupee(res.max_loss, 0);
    }
    renderChart(res);
    renderTable(res);
  }

  function renderChart(res) {
    var svg = $('#optChart');
    if (!svg || !res.curve || !res.curve.length) return;
    var xs = res.curve.map(function (p) { return p[0]; });
    var ys = res.curve.map(function (p) { return p[1]; });
    var xMin = Math.min.apply(null, xs), xMax = Math.max.apply(null, xs);
    var yMin = Math.min.apply(null, ys), yMax = Math.max.apply(null, ys);
    var pad = (yMax - yMin) * 0.12 || 1;
    yMin -= pad; yMax += pad;
    function X(x) { return (x - xMin) / ((xMax - xMin) || 1) * 400; }
    function Y(y) { return 140 - (y - yMin) / ((yMax - yMin) || 1) * 130; }
    var zeroY = Y(0);
    var path = res.curve.map(function (p, i) { return (i === 0 ? 'M' : 'L') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1); }).join(' ');
    var beDots = (res.breakevens || []).map(function (b) {
      return '<circle cx="' + X(b).toFixed(1) + '" cy="' + zeroY.toFixed(1) + '" r="3.5" fill="var(--bg)" stroke="var(--blu)" stroke-width="2"/>';
    }).join('');
    var spotLine = state.spot != null ? ('<line x1="' + X(state.spot).toFixed(1) + '" y1="10" x2="' + X(state.spot).toFixed(1) + '" y2="140" stroke="var(--mut)" stroke-width="1" stroke-dasharray="3 3"/>') : '';
    var winRect = '<rect x="0" y="10" width="400" height="' + Math.max(0, zeroY - 10).toFixed(1) + '" fill="color-mix(in srgb, var(--grn) 8%, transparent)"/>';
    var lossRect = '<rect x="0" y="' + zeroY.toFixed(1) + '" width="400" height="' + Math.max(0, 140 - zeroY).toFixed(1) + '" fill="color-mix(in srgb, var(--red) 8%, transparent)"/>';
    var zeroLine = '<line x1="0" y1="' + zeroY.toFixed(1) + '" x2="400" y2="' + zeroY.toFixed(1) + '" stroke="var(--line2)" stroke-width="1"/>';
    var axisL = '<text x="4" y="148" font-size="9" fill="var(--mut)">' + fmtNum(xMin, 0) + '</text>';
    var axisR = '<text x="368" y="148" font-size="9" fill="var(--mut)" text-anchor="end">' + fmtNum(xMax, 0) + '</text>';
    var axisSpot = state.spot != null ? ('<text x="' + X(state.spot).toFixed(1) + '" y="148" font-size="9" fill="var(--mut)" text-anchor="middle">spot</text>') : '';
    svg.innerHTML = winRect + lossRect + zeroLine + spotLine + '<path d="' + path + '" fill="none" stroke="var(--blu)" stroke-width="2.5"/>' + beDots + axisL + axisR + axisSpot;
  }

  function tableRows(res) {
    // cc#2038 item 4: "show 7 rows: range ends, each strike, each BE" -- the literal union,
    // deduped/sorted; the exact count follows from how many legs/BEs a strategy actually has
    // (curve/per_leg already carry an exact point for every one of these, cc#2036's own
    // extra-points addition -- no snapping, no approximation).
    var curve = res.curve, perLeg = res.per_leg;
    if (!curve || !curve.length) return [];
    var want = [curve[0][0], curve[curve.length - 1][0]]
      .concat(state.legs.filter(function (l) { return l.kind !== 'FUT'; }).map(function (l) { return l.strike; }))
      .concat(res.breakevens || []);
    var seen = [];
    want.forEach(function (w) { if (w != null && !seen.some(function (s) { return near(s, w); })) seen.push(w); });
    seen.sort(function (a, b) { return a - b; });
    return seen.map(function (S) {
      var c = curve.filter(function (p) { return near(p[0], S); })[0];
      var pl = perLeg.filter(function (p) { return near(p[0], S); })[0];
      var isBE = (res.breakevens || []).some(function (b) { return near(b, S); });
      return { S: S, net: c ? c[1] : null, legs: pl ? pl.slice(1) : [], be: isBE };
    });
  }

  function renderTable(res) {
    var box = $('#optTbl');
    if (!box) return;
    var rows = tableRows(res);
    var head = '<div class="tr"><span>NIFTY at expiry</span>' + state.legs.filter(function (l) { return l.kind !== 'FUT'; }).map(function (l) { return '<span>' + fmtNum(l.strike, 0) + ' ' + l.kind + '</span>'; }).join('') + '<span>Net</span></div>';
    var body = rows.map(function (r) {
      var cells = r.legs.map(function (v) { return '<span class="' + (v >= 0 ? 'w' : 'l') + '">' + (v >= 0 ? '+' : '') + fmtNum(v, 0) + '</span>'; }).join('');
      var netCls = r.net > 0 ? 'w' : (r.net < 0 ? 'l' : '');
      return '<div class="tr' + (r.be ? ' be' : '') + '"><span>' + fmtNum(r.S, 1) + '</span>' + cells + '<span class="' + netCls + '">' + (r.net > 0 ? '+' : '') + fmtNum(r.net, 0) + '</span></div>';
    }).join('');
    box.innerHTML = head + body;
  }

  // ── wiring ─────────────────────────────────────────────────────────────────────────────────
  function wireStatic() {
    wireSubstSheet();
    var tabs = $('#optTabs');
    if (tabs) tabs.addEventListener('click', function (e) { var t = e.target.closest('[data-tab]'); if (t) setTab(t.getAttribute('data-tab')); });
    var under = $('#optUnder');
    if (under) under.addEventListener('change', async function () { state.underlying = under.value; state.legs = []; renderLegs(); await loadMeta(); await loadTemplates(); });
    var exp = $('#optExpiry');
    if (exp) exp.addEventListener('change', async function () { state.expiry = exp.value; await loadChain(); });
    var addBtn = $('#optAddLeg');
    if (addBtn) addBtn.addEventListener('click', function (e) { e.preventDefault(); addLeg(); });
    var go = $('#optGo');
    if (go) go.addEventListener('click', function (e) { e.preventDefault(); calculate(); });
    var views = $('#optViews');
    if (views) views.addEventListener('click', function (e) {
      var v = e.target.closest('[data-view]'); if (!v) return;
      state.view = v.getAttribute('data-view'); state.sub = null;
      $all('#optViews [data-view]').forEach(function (el) { el.classList.toggle('on', el === v); });
      var subs = $('#optSubs');
      if (subs) $all('#optSubs [data-sub]').forEach(function (el) { el.classList.remove('on'); });
      loadTemplates();
    });
    var subs = $('#optSubs');
    if (subs) subs.addEventListener('click', function (e) {
      var s = e.target.closest('[data-sub]'); if (!s) return;
      var on = s.classList.contains('on');
      $all('#optSubs [data-sub]').forEach(function (el) { el.classList.remove('on'); });
      state.sub = on ? null : s.getAttribute('data-sub');
      if (!on) s.classList.add('on');
      renderTemplates();
    });
    var grid = $('#optGrid');
    if (grid) grid.addEventListener('click', function (e) { var t = e.target.closest('.tpl'); if (t) selectTemplate(Number(t.getAttribute('data-id'))); });
    wireLegEvents();
  }

  async function init() {
    wireStatic();
    await loadMeta();
    await loadTemplates();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.ScorrOptionStrategy = { state: state, calculate: calculate, loadMeta: loadMeta };
})();

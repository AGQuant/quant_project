/* scorr_model_portfolio.js — cc#1677 MODEL PORTFOLIO TAB = EMPTY LAUNCHER
   ═══════════════════════════════════════════════════════════════════════════════════════════
   Founder direction 04-Sep 09:35 IST, superseding cc#1584's chip-strip + /quant-basket iframe
   embed (which itself superseded a plain redirect): "keep page empty, already we captured
   everything in quant basket and quant screener; give a button and display screeners and
   baskets in model portfolio page on click in table view."

   CONTRACT
     window.ScorrModelPortfolio.mount(containerEl) — builds the empty state (header line, one
     sentence, two buttons) into containerEl and wires the two buttons. Idempotent: a second
     mount() call on an already-mounted container is a no-op, so a pane that re-shows itself
     (V8's showV8Pane) never doubles the markup.

   NOTHING RENDERS UNTIL A BUTTON IS PRESSED. No basket chips, no summary tiles, no rail — those
   all stay exactly as they were on the standalone /quant-basket page, which this file never
   touches. A second press on the ACTIVE button collapses the table back to the empty state.

   DATA — every number here is SERVED, never recomputed (rule 13 spirit; cc#1674 on this same
   dashboard is the live example of what recomputing a served aggregate costs):
     Baskets   /api/qb/registry (basket_name, capital, next_rebalance, is_active, and — cc#1677 —
               `type` Quant/Discretionary, now served by that endpoint off app_config key
               qb_discretionary_baskets) joined with /api/performance/qb (positions, market_value,
               pnl, return_pct) and /api/performance/alpha (alpha; its own return_pct wins where
               present — the SAME merge precedence cc#1584's retired chip strip used, so this
               table's numbers cannot disagree with what /quant-basket showed a moment ago).
     Screeners /api/screeners (id, name, members, last_run, and — cc#1677 — `newest`, the top-3
               most-recent entrants, computed server-side in screeners_endpoints.py so this pane
               never fires one fetch per screen).
     Row click routes to the existing pages: /quant-basket#<basket_name>, /screeners#<screen_id>.
     Nothing here recomputes a basket or reruns a screen — read-only, same as both source pages.
*/
(function () {
  if (window.ScorrModelPortfolio) return;

  function esc(s) { return String(s == null ? '' : s)
    .replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function getJ(u) { return fetch(u, { cache: 'no-store' }).then(function (r) {
    if (!r.ok) throw new Error(u + ' -> HTTP ' + r.status);
    return r.json();
  }); }
  function inr(v) {
    if (v == null || isNaN(Number(v))) return '—';
    var n = Number(v);
    return (n < 0 ? '−' : '') + '₹' + Math.abs(Math.round(n)).toLocaleString('en-IN');
  }
  function pct(v, dp) {
    if (v == null || isNaN(Number(v))) return '—';
    var n = Number(v);
    return (n >= 0 ? '+' : '') + n.toFixed(dp == null ? 2 : dp) + '%';
  }
  function pnlColor(v) { return v == null ? 'var(--mut)' : (Number(v) >= 0 ? 'var(--grn,#2FD48B)' : 'var(--red,#FF5C6C)'); }

  // Display labels only — the registry basket_name is what every fetch/link key is built on.
  var BASKET_LABEL = {
    large_cap: 'Large Cap', mid_cap: 'Mid Cap', small_cap: 'Small Cap',
    alpha_multicap: 'Alpha Multicap', breakout_52w: '52W Breakout', contra_value: 'Contra Value',
    finz_stable: 'FINZ Stable', finz_wcb: 'FINZ Wealth Compounder', finz_helios: 'FINZ Helios',
    finz_dividend: 'FINZ Dividend', finz_defence: 'FINZ Defence', finz_etf: 'FINZ ETF Basket'
  };

  function tableWrap(inner) {
    // cc#330 .mtable pattern (sticky first column, data-pri priorities, horizontal scroll on
    // narrow) — first real consumer on a dark-token page, so --mtable-bg is set locally to the
    // site's own panel token rather than the pattern's white default.
    return '<div class="tw mtable-wrap" style="--mtable-bg:var(--panel,#121A33);margin-top:10px">'
      + '<table class="mtable" style="width:100%;border-collapse:collapse;font-size:12.5px">' + inner + '</table></div>';
  }
  function th(label, extra) {
    return '<th ' + (extra || '') + ' style="padding:8px 10px;text-align:right;font-size:10.5px;'
      + 'color:var(--mut);text-transform:uppercase;letter-spacing:.04em;white-space:nowrap">' + esc(label) + '</th>';
  }
  function td(html, extra) { return '<td ' + (extra || '') + ' style="padding:8px 10px;text-align:right">' + html + '</td>'; }

  function renderBaskets(reg, qb, alpha) {
    if (reg && reg.error) throw new Error(reg.error);
    var bmap = {};
    ((qb && qb.baskets) || []).forEach(function (b) { bmap[b.basket] = Object.assign({}, b); });
    ((alpha && alpha.baskets) || []).forEach(function (b) {
      var d = bmap[b.basket] || (bmap[b.basket] = {});
      d.alpha = b.alpha; d.return_pct = b.return_pct;   // alpha's own return_pct wins, cc#1584's own rule
      if (d.pnl == null) d.pnl = b.pnl;
      if (d.positions == null) d.positions = b.positions;
    });
    var rows = (Array.isArray(reg) ? reg : []).filter(function (r) { return r && r.basket_name && r.is_active; });

    var totCapital = 0, totMkt = 0, totPnl = 0;
    var body = rows.map(function (r) {
      var d = bmap[r.basket_name] || {};
      var cap = Number(r.capital) || 0;
      var mkt = d.market_value != null ? Number(d.market_value) : null;
      var pnl = d.pnl != null ? Number(d.pnl) : null;
      var ret = d.return_pct != null ? Number(d.return_pct) : null;
      var alph = d.alpha != null ? Number(d.alpha) : null;
      var n = d.positions || 0;
      totCapital += cap;
      if (mkt != null) totMkt += mkt;
      if (pnl != null) totPnl += pnl;
      var href = '/quant-basket#' + encodeURIComponent(r.basket_name);
      return '<tr class="mrow-link" data-href="' + esc(href) + '" style="cursor:pointer;border-bottom:1px solid var(--line,#1E2A44)">'
        + '<td style="padding:8px 10px;text-align:left;font-weight:700">' + esc(BASKET_LABEL[r.basket_name] || r.basket_name) + '</td>'
        + '<td style="padding:8px 10px;text-align:left;color:var(--mut)">' + esc(r.type || 'Quant') + '</td>'
        + td(n, 'data-pri="3"')
        + td(inr(cap), 'data-pri="2"')
        + td(inr(mkt))
        + td('<span style="color:' + pnlColor(pnl) + '">' + inr(pnl) + '</span>')
        + td('<span style="color:' + pnlColor(ret) + '">' + pct(ret) + '</span>')
        + td('<span style="color:' + pnlColor(alph) + '">' + (alph == null ? '—' : pct(alph)) + '</span>', 'data-pri="3"')
        + '<td data-pri="2" style="padding:8px 10px;text-align:right;color:var(--mut);white-space:nowrap">' + esc(r.next_rebalance ? String(r.next_rebalance).slice(0, 10) : '—') + '</td>'
        + '</tr>';
    }).join('');

    // Footer: totals across the LISTED rows only. Alpha is deliberately blank — a mean of alphas
    // is not an alpha (the card's own instruction).
    var totRet = totCapital ? (totPnl / totCapital * 100) : null;
    var foot = '<tr style="border-top:2px solid var(--line2,#243049);font-weight:700">'
      + '<td style="padding:8px 10px;text-align:left">Total</td><td></td><td></td>'
      + td(inr(totCapital), 'data-pri="2"')
      + td(inr(totMkt))
      + td('<span style="color:' + pnlColor(totPnl) + '">' + inr(totPnl) + '</span>')
      + td('<span style="color:' + pnlColor(totRet) + '">' + pct(totRet) + '</span>')
      + '<td data-pri="3"></td><td data-pri="2"></td></tr>';

    if (!rows.length) return '<div style="font-size:11px;color:var(--mut);padding:10px 0">No active baskets in the registry.</div>';

    return tableWrap(
      '<thead><tr>'
      + '<th style="padding:8px 10px;text-align:left;font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em">Basket</th>'
      + '<th style="padding:8px 10px;text-align:left;font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em">Type</th>'
      + th('Holdings', 'data-pri="3"') + th('Capital', 'data-pri="2"') + th('Mkt Value') + th('P&amp;L') + th('Return %')
      + th('Alpha vs N500', 'data-pri="3"') + th('Next Rebal', 'data-pri="2"')
      + '</tr></thead><tbody>' + body + foot + '</tbody>'
    );
  }

  function renderScreeners(screens) {
    if (!screens || !screens.length) return '<div style="font-size:11px;color:var(--mut);padding:10px 0">No screens stored yet.</div>';
    var body = screens.map(function (s) {
      var st = window.scorrAsofStamp ? window.scorrAsofStamp(s.last_run) : { txt: s.last_run || '', amber: false };
      var newest = (s.newest || []).slice(0, 3).join(', ');
      var href = '/screeners#' + encodeURIComponent(s.id);
      return '<tr class="mrow-link" data-href="' + esc(href) + '" style="cursor:pointer;border-bottom:1px solid var(--line,#1E2A44)">'
        + '<td style="padding:8px 10px;text-align:left;font-weight:700">' + esc(s.name) + '</td>'
        + td(s.members != null ? s.members : '—')
        + '<td style="padding:8px 10px;text-align:left;color:' + (st.amber ? 'var(--amber,#F5B94A)' : 'var(--mut)') + '">' + esc(st.txt || '—') + '</td>'
        + '<td data-pri="2" style="padding:8px 10px;text-align:left;color:var(--mut)">' + esc(newest || '—') + '</td>'
        + '</tr>';
    }).join('');
    return tableWrap(
      '<thead><tr>'
      + '<th style="padding:8px 10px;text-align:left;font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em">Screen</th>'
      + th('Members')
      + '<th style="padding:8px 10px;text-align:left;font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em">As-of</th>'
      + '<th data-pri="2" style="padding:8px 10px;text-align:left;font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em">Newest entrant(s)</th>'
      + '</tr></thead><tbody>' + body + '</tbody>'
    );
  }

  function wireRowLinks(bodyEl) {
    // One delegated listener per body element (bodyEl is re-created via innerHTML on every load,
    // so this never accumulates duplicate handlers).
    bodyEl.addEventListener('click', function (e) {
      var tr = e.target && e.target.closest ? e.target.closest('.mrow-link') : null;
      if (tr && tr.getAttribute('data-href')) location.href = tr.getAttribute('data-href');
    });
  }

  function loadBaskets(bodyEl) {
    bodyEl.innerHTML = '<div style="font-size:11px;color:var(--mut);padding:10px 0">Loading baskets…</div>';
    Promise.all([
      getJ('/api/qb/registry'),
      getJ('/api/performance/qb'),
      getJ('/api/performance/alpha').catch(function () { return {}; })
    ]).then(function (a) {
      bodyEl.innerHTML = renderBaskets(a[0], a[1], a[2]);
      wireRowLinks(bodyEl);
    }).catch(function (e) {
      bodyEl.innerHTML = '<div style="font-size:11px;color:var(--mut);padding:10px 0">Baskets unavailable — ' + esc(e && e.message || 'fetch failed') + '</div>';
    });
  }

  function loadScreeners(bodyEl) {
    bodyEl.innerHTML = '<div style="font-size:11px;color:var(--mut);padding:10px 0">Loading screeners…</div>';
    getJ('/api/screeners').then(function (d) {
      if (d && d.error) throw new Error(d.error);
      bodyEl.innerHTML = renderScreeners((d && d.screens) || []);
      wireRowLinks(bodyEl);
    }).catch(function (e) {
      bodyEl.innerHTML = '<div style="font-size:11px;color:var(--mut);padding:10px 0">Screeners unavailable — ' + esc(e && e.message || 'fetch failed') + '</div>';
    });
  }

  var LOADERS = { baskets: loadBaskets, screeners: loadScreeners };

  // cc#1677: the card's own evidence named ".digl" as this pane's existing button style, but
  // that class is defined only in mobile/home.html (an APP page) — it does not exist anywhere on
  // v8_dashboard.html, so using it here would render two unstyled default buttons. Confirmed by
  // grepping this file's own CSS before writing the markup, not assumed from the card's text.
  // A small local style is injected once instead, in the SAME var(--token) vocabulary the rest of
  // this page's buttons use (.v8toptab's own --mut/--blu idle/active pair, just as a filled pill
  // rather than an underlined tab — this is a toggle, not a tab).
  function ensureButtonStyle() {
    if (document.getElementById('mp-btn-style')) return;
    var st = document.createElement('style');
    st.id = 'mp-btn-style';
    st.textContent =
      '.mp-btn{font:inherit;font-size:11.5px;font-weight:700;letter-spacing:.03em;'
      + 'color:var(--mut);background:var(--panel);border:1px solid var(--line2);border-radius:9px;'
      + 'padding:8px 16px;cursor:pointer;transition:.12s}'
      + '.mp-btn:hover{color:var(--txt);border-color:var(--blu)}'
      + '.mp-btn.active{color:var(--blu);border-color:var(--blu)}';
    document.head.appendChild(st);
  }

  function mount(container) {
    if (!container || container.getAttribute('data-mp-mounted') === '1') return;
    container.setAttribute('data-mp-mounted', '1');
    ensureButtonStyle();
    var active = null;
    container.innerHTML =
      '<div style="padding:4px 2px 2px">'
      + '<div style="font-size:15px;font-weight:800;color:var(--txt)">Model Portfolio</div>'
      + '<div style="font-size:12px;color:var(--mut);margin-top:4px">Baskets and screeners are captured on their own pages. Open a table view below.</div>'
      + '<div style="display:flex;gap:10px;margin-top:12px" id="mpBtnRow">'
      + '<button type="button" class="mp-btn" data-mp-btn="baskets">QUANT BASKETS</button>'
      + '<button type="button" class="mp-btn" data-mp-btn="screeners">QUANT SCREENERS</button>'
      + '</div>'
      + '<div id="mpBody"></div>'
      + '</div>';
    var btnRow = container.querySelector('#mpBtnRow');
    var bodyEl = container.querySelector('#mpBody');
    btnRow.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest ? e.target.closest('[data-mp-btn]') : null;
      if (!btn) return;
      var key = btn.getAttribute('data-mp-btn');
      if (active === key) {
        // second press on the active button collapses back to the empty state
        active = null;
        bodyEl.innerHTML = '';
      } else {
        active = key;
        LOADERS[key](bodyEl);
      }
      Array.prototype.forEach.call(btnRow.querySelectorAll('[data-mp-btn]'), function (b) {
        b.classList.toggle('active', b.getAttribute('data-mp-btn') === active);
      });
    });
  }

  window.ScorrModelPortfolio = { mount: mount };
})();

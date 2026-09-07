/* scorr_model_portfolio.js — cc#1715 MODEL PORTFOLIO TAB = the model_portfolio basket by default
   ═══════════════════════════════════════════════════════════════════════════════════════════
   Founder 05-Sep (screenshot of the empty launcher): "Deploy Model Portfolio 1 lakh on Model
   Portfolio tab and engine should work like QB here, rebalancing need basis, and display summary
   of rebalancing like QB tab." The pane now renders ON OPEN, no click: a KPI strip, the holdings
   table, and the Rebalance History (cc#1709 blocks) / HSL History tabs for basket_name
   model_portfolio — then the two cc#1677 buttons (QUANT BASKETS / QUANT SCREENERS) BELOW it,
   unchanged in behaviour. Every number is SERVED: /api/qb/registry (capital, max_stocks, next
   review, notes), /api/performance/qb + /alpha (cc#1702 NAV basis, same merge precedence as the
   table below), /api/qb/positions (holdings), /api/qb/rebalance_history (blocks + hsl). Nothing is
   recomputed here except Weight % = row value / basket market value (the same client-side ratio
   /quant-basket's own Holdings tab uses, cc#1299). The block/HSL markup is the cc#1709 renderer
   from quant_basket.html carried over line for line (same .rb-* classes; the CSS is injected once
   below because that page's styles are not shared) — one look, two surfaces.
   cc#1728 (founder 06-Sep, screenshot of the pane): the registry notes left the header for an (i)
   sheet (cc#1708 ScorrInfoSheet) sectioned SELECTION / SIZING / EXITS / CADENCE; the KPI strip is
   CAPITAL | HOLDING VALUE (open positions at CMP, no cash) | CASH | P&L | RETURN % | ALPHA |
   POSITIONS; NEXT REVIEW and STATE are hidden behind SHOW_REVIEW_STATE; the near-HS1 count is a
   chip beside the title only when > 0; the tag shows the served TYPE once.
   ─────────────────────────────────────────────────────────────────────────────────────────────
   cc#1677 (kept below, unchanged): EMPTY LAUNCHER + two table views
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

  function tableWrap(inner, cls) {
    // cc#330 .mtable pattern (sticky first column, data-pri priorities, horizontal scroll on
    // narrow) — first real consumer on a dark-token page, so --mtable-bg is set locally to the
    // site's own panel token rather than the pattern's white default.
    // cc#1731: `cls` = 'mp-sticky' for a table whose THEAD pins on scroll (see the CSS note).
    return '<div class="tw mtable-wrap' + (cls ? ' ' + cls : '') + '" style="--mtable-bg:var(--panel,#121A33);margin-top:10px">'
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

  // ═══════════════════════════════ cc#1715: the basket pane ═══════════════════════════════
  var MP_BASKET = 'model_portfolio';
  var MP_LABEL = 'Model Portfolio';
  var MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function fmtDMY(v) { if (!v) return '—'; var m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? (m[3] + '-' + MON[+m[2] - 1] + '-' + m[1]) : esc(String(v)); }
  function inr2(v) { return v == null || isNaN(+v) ? '—' : '₹' + (+v).toLocaleString('en-IN', { maximumFractionDigits: 2 }); }
  function lakh(v) { return v == null || isNaN(+v) ? '—' : '₹' + (+v / 100000).toFixed(2) + 'L'; }
  function num2(v) { return v == null || isNaN(+v) ? '—' : (+v).toFixed(2); }
  function cls(v) { return v == null ? 'neu' : (+v > 0 ? 'pos' : (+v < 0 ? 'neg' : 'neu')); }

  function ensurePaneStyle() {
    if (document.getElementById('mp-pane-style')) return;
    var st = document.createElement('style');
    st.id = 'mp-pane-style';
    st.textContent =
      '#mpMount .pos{color:var(--grn,#2FD48B);font-weight:700}#mpMount .neg{color:var(--red,#FF5C6C);font-weight:700}#mpMount .neu{color:var(--txt)}'
      + '#mpMount .mp-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin:12px 0 4px}'
      + '#mpMount .mp-kpi{background:var(--panel,#121A33);border:1px solid var(--line,#1E2A44);border-radius:12px;padding:10px 12px}'
      + '#mpMount .mp-kpi .k{font-size:9.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}'
      + '#mpMount .mp-kpi .v{font-family:var(--mono,inherit);font-size:16px;font-weight:800;margin-top:4px;white-space:nowrap}'
      + '#mpMount .mp-kpi .s{font-size:10px;color:var(--mut);margin-top:2px}'
      + '#mpMount .mp-badge{display:inline-block;font-size:9.5px;font-weight:800;padding:3px 9px;border-radius:8px;border:1px solid}'
      + '#mpMount .mp-badge.ok{background:rgba(47,212,139,.1);color:var(--grn,#2FD48B);border-color:rgba(47,212,139,.4)}'
      + '#mpMount .mp-badge.risk{background:rgba(255,92,108,.08);color:var(--red,#FF5C6C);border-color:rgba(255,92,108,.4)}'
      + '#mpMount .mp-sec{font-size:10.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:16px 0 6px}'
      + '#mpMount .mp-tabs{display:flex;gap:6px;margin:14px 0 8px}'
      + '#mpMount .mp-tab{font:inherit;font-size:11px;font-weight:700;color:var(--mut);background:transparent;border:1px solid var(--line2,#243049);border-radius:8px;padding:5px 12px;cursor:pointer}'
      + '#mpMount .mp-tab.active{color:var(--blu,#4D7CFE);border-color:var(--blu,#4D7CFE)}'
      + '#mpMount .mtable th[data-mp-sort]{cursor:pointer;user-select:none}'
      // cc#1731 (founder 06-Sep "similarly in model portfolio tab table header rows should stick"):
      // the HOLDINGS thead pins on vertical scroll. Offset = --scorr-nav-bottom, the ONE live
      // measurement cc#1730 put in scorr_card_common.js (window.ScorrNavOffset): the canonical
      // #scorr-nav bottom edge (44 shown, 20 = the cc#1718 Show strip when hidden, 0 when cc#926
      // has slid it away). Measured on this page: the V8 sub-nav row (.v8toptabs) is NOT sticky,
      // so it contributes nothing to the offset — nothing is hardcoded here, the static values
      // are the no-JS fallback. Sticky is on the TH elements, so the table layout never changes
      // and the columns stay aligned with the body (item 4). Page background + a 1px bottom line
      // (inset shadow — a border on a sticky th does not travel with it under border-collapse)
      // so rows never show through (item 3). At desktop widths the .mtable-wrap must not be a
      // scroll container (overflow-x:auto makes it the sticky containing block and the header
      // would pin to the wrap, not the viewport); below 768px the pattern's horizontal scroll is
      // kept and the header simply does not pin — the mobile pane is out of scope.
      + '@media(min-width:768px){#mpMount .mtable-wrap.mp-sticky{overflow:visible}}'
      + '#mpMount .mp-sticky thead th{position:sticky;top:var(--scorr-nav-bottom,44px);z-index:5;'
      + '  background:var(--bg,#070C1A);box-shadow:inset 0 -1px 0 var(--line,#1E2A44)}'
      // cc#1731 item 5: the rebalance-block Buy table is 20 rows on the seed block, so it gets the
      // same treatment. .rb-block dropped overflow:hidden (a clipped ancestor kills sticky); the
      // header keeps its rounded top on its own. HSL History has 0 rows today — skipped.
      + '@media(min-width:768px){#mpMount .rb-wrap .rb-block{overflow:visible}#mpMount .rb-hd{border-radius:12px 12px 0 0}'   /* .rb-wrap .rb-block outranks the base overflow:hidden rule declared later */
      + '  #mpMount .rb-tbl thead th{position:sticky;top:var(--scorr-nav-bottom,44px);z-index:5;'
      + '  background:var(--bg,#070C1A);box-shadow:inset 0 -1px 0 var(--line,#1E2A44)}}'
      + '#mpMount .mtable td.sym{text-align:left;font-weight:700;white-space:nowrap}'
      // cc#1709 block + HSL classes, carried over from quant_basket.html so the two surfaces match
      + '#mpMount .rb-wrap{padding:4px 0 0}'
      + '#mpMount .rb-block{border:1px solid var(--line,#1E2A44);border-radius:12px;margin-bottom:14px;overflow:hidden}'
      + '#mpMount .rb-hd{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--panel,#121A33);border-bottom:1px solid var(--line,#1E2A44)}'
      + '#mpMount .rb-title{font-size:12.5px;font-weight:700;color:var(--txt)}'
      + '#mpMount .rb-date{margin-left:auto;font-size:11.5px;font-weight:700;color:var(--txt);font-variant-numeric:tabular-nums}'
      + '#mpMount .rb-chip{display:inline-block;font-size:9.5px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;padding:2px 8px;border-radius:6px}'
      + '#mpMount .rb-chip.done{background:rgba(47,212,139,.12);color:var(--grn,#2FD48B)}'
      + '#mpMount .rb-chip.await{background:rgba(245,185,74,.14);color:var(--amber,#F5B94A)}'
      + '#mpMount .rb-chip.none{background:rgba(148,166,210,.14);color:var(--mut)}'
      + '#mpMount .rb-side{font-size:9.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;padding:10px 14px 0;color:var(--mut)}'
      + '#mpMount .rb-side.sell{color:var(--red,#FF5C6C)} #mpMount .rb-side.buy{color:var(--grn,#2FD48B)}'
      + '#mpMount .rb-tbl{margin:4px 0 6px;width:100%;border-collapse:collapse;font-size:12px}'
      + '#mpMount .rb-tbl th,#mpMount .rb-tbl td{padding:6px 14px;text-align:left}'
      + '#mpMount .rb-tbl th{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}'
      + '#mpMount .rb-tbl tr.rb-sell td{background:rgba(255,92,108,.07)}'
      + '#mpMount .rb-tbl tr.rb-buy td{background:rgba(47,212,139,.07)}'
      + '#mpMount .rb-tbl tr.rb-sell td.rb-act{color:var(--red,#FF5C6C);font-weight:700}'
      + '#mpMount .rb-tbl tr.rb-buy td.rb-act{color:var(--grn,#2FD48B);font-weight:700}'
      + '#mpMount .rb-none{font-size:11px;color:var(--dim,var(--mut));padding:6px 14px 8px;font-style:italic}'
      + '#mpMount .rb-foot{font-size:11px;color:var(--mut);padding:8px 14px 10px;border-top:1px solid var(--line,#1E2A44);white-space:normal}'
      + '#mpMount .rb-tag{display:inline-block;margin-left:6px;font-size:9.5px;font-weight:700;padding:1px 6px;border-radius:5px;background:rgba(148,166,210,.14);color:var(--mut);white-space:nowrap}'
      + '#mpMount .rb-ver{display:inline-block;font-size:9.5px;font-weight:800;padding:1px 6px;border-radius:5px;border:1px solid var(--line,#1E2A44);color:var(--mut)}'
      + '#mpMount .mp-msg{font-size:11px;color:var(--mut);padding:10px 0}'
      // cc#1728: the (i) button uses the shared cc#1708 .ibtn look; ScorrInfoSheet injects that CSS
      // only on first OPEN, so the same rule is carried here for the button's idle state.
      + '#mpMount .ibtn{width:20px;height:20px;border-radius:50%;border:1px solid var(--line,#243049);background:var(--panel,#0E1526);color:var(--mut,#94a3b8);font-size:11px;font-weight:800;line-height:1;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;flex:none}'
      + '#mpMount .ibtn:hover{color:var(--txt,#e6ecf5);border-color:var(--dim,#5E6B8F)}';
    document.head.appendChild(st);
  }

  // ---- cc#1709 renderers, carried over from quant_basket.html (rbSideTable / rbBlockHtml / HSL) ----
  function rbSideTable(rows, side) {
    var empty = side === 'sell' ? 'No sells' : 'No buys';
    if (!rows || !rows.length) return '<div class="rb-none">' + empty + '</div>';
    return '<table class="rb-tbl"><thead><tr><th>S.No</th><th>Stock</th><th>Price</th><th>Qty</th><th>Amount</th><th>Action</th></tr></thead><tbody>'
      + rows.map(function (r, i) {
        return '<tr class="rb-' + side + '"><td>' + (i + 1) + '</td>'
          + '<td><strong>' + esc(r.symbol) + '</strong>' + (r.reason ? ' <span class="rb-tag" title="' + esc(r.reason) + '">' + esc(r.rule || r.reason) + '</span>' : '') + '</td>'
          + '<td>' + num2(r.price) + '</td><td>' + (r.qty != null ? r.qty : '—') + '</td><td>' + inr2(r.amount) + '</td>'
          + '<td class="rb-act">' + (side === 'sell' ? 'Full Sell' : 'Buy') + '</td></tr>';
      }).join('') + '</tbody></table>';
  }
  function rbBlockHtml(b) {
    var f = b.footer || {};
    var chip = b.state === 'DONE' ? 'done' : (b.state === 'AWAITING CONFIRMATION' ? 'await' : 'none');
    var cash = '';
    if (f.cash_move) {
      var cm = f.cash_move, amt = cm.residual != null ? Math.round(+cm.residual).toLocaleString('en-IN') : inr2(cm.amount);
      cash = ' · ' + (cm.direction === 'out' ? '−' : '+') + esc(cm.symbol) + ' ' + amt + (cm.direction === 'out' ? ' (liquidated to cash)' : ' (cash residual)');
    } else if (f.cash_after != null) {
      cash = ' · cash ' + inr2(f.cash_after);   // cc#1715: cash left after this rebalance
    }
    return '<div class="rb-block"><div class="rb-hd">'
      + '<span class="rb-title">' + MP_LABEL + ' ' + (b.kind === 'quality_exit' ? 'Quality exit' : 'Rebalance') + '</span>'
      + '<span class="rb-chip ' + chip + '">' + esc(b.chip || b.state || '') + '</span>'
      + '<span class="rb-date">' + fmtDMY(b.date) + '</span></div>'
      + '<div class="rb-side sell">Sell</div>' + rbSideTable(b.sells, 'sell')
      + '<div class="rb-side buy">Buy</div>' + rbSideTable(b.buys, 'buy')
      + '<div class="rb-foot">held after = ' + (f.held_after != null ? f.held_after : '—') + (f.bees_held ? ' (+NIFTYBEES)' : '')
      + ' · book value after = ' + lakh(f.book_value_after) + cash + (b.next_due ? ' · next due ' + fmtDMY(b.next_due) : '') + '</div></div>';
  }
  function rebalanceHtml(d) {
    var blocks = (d && d.blocks) || [];
    if (!blocks.length) return '<div class="mp-msg">No rebalance yet — first review ' + (d && d.next_rebalance ? fmtDMY(d.next_rebalance) : '—') + '.</div>';
    return '<div class="rb-wrap">' + blocks.map(rbBlockHtml).join('') + '</div>'
      + (d.nightly_count ? '<div class="rb-foot" style="border-top:0;padding:2px 0 0">' + d.nightly_count + ' nightly stop-checks ran with no action; hard-stop exits are on the HSL History tab.</div>' : '');
  }
  function hslHtml(d) {
    var h = (d && d.hsl) || { rows: [], count: 0, total_pnl: 0 };
    if (!h.rows || !h.rows.length) return '<div class="mp-msg">No stop exits yet.</div>';
    return tableWrap('<thead><tr>' + ['Date','Stock','Entry Price','Exit Price','Qty','P&amp;L ₹','P&amp;L %','Rule','Rules'].map(function (l, i) { return th(l, i < 2 || i > 6 ? 'style="text-align:left"' : ''); }).join('') + '</tr></thead><tbody>'
      + h.rows.map(function (r) {
        return '<tr style="border-bottom:1px solid var(--line,#1E2A44)">'
          + '<td style="padding:8px 10px">' + fmtDMY(r.date) + '</td><td class="sym" style="padding:8px 10px">' + esc(r.symbol) + '</td>'
          + td(num2(r.entry_price)) + td(num2(r.exit_price)) + td(r.qty != null ? r.qty : '—')
          + td('<span class="' + cls(r.pnl) + '">' + inr2(r.pnl) + '</span>') + td('<span class="' + cls(r.pnl_pct) + '">' + pct(r.pnl_pct) + '</span>')
          + '<td style="padding:8px 10px">' + esc(r.rule_text || '—') + (r.measured ? ' <span class="rb-tag">' + esc(r.measured) + '</span>' : '') + '</td>'
          + '<td style="padding:8px 10px"><span class="rb-ver">' + esc(r.version || '') + '</span></td></tr>';
      }).join('') + '</tbody>')
      + '<div class="rb-foot" style="border-top:0;padding:8px 0 0">' + h.count + ' stop' + (h.count === 1 ? '' : 's') + ' · total P&amp;L <span class="' + cls(h.total_pnl) + '">' + inr2(h.total_pnl) + '</span></div>';
  }

  // ---- holdings table: sortable headers (cc#1680 pattern), default Weight % desc ----
  var HCOLS = [['symbol','Symbol','txt'],['qty','Qty','num'],['entry_price','Entry','num'],['current_price','CMP','num'],
               ['current_value','Value','num'],['pnl','P&amp;L ₹','num'],['pnl_pct','P&amp;L %','num'],['weight','Weight %','num'],['stop_loss_price','Stop','num']];
  var HSORT = { key: 'weight', dir: 1 };   // dir 1 = descending (cc#1680 convention)
  function holdingsHtml(rows, mktVal) {
    if (!rows || !rows.length) return '<div class="mp-msg">No open positions.</div>';
    rows = rows.map(function (r) { var o = Object.assign({}, r);
      o.weight = (mktVal && r.current_value != null) ? (+r.current_value / mktVal * 100) : null; return o; });
    var k = HSORT.key, kind = (HCOLS.filter(function (c) { return c[0] === k; })[0] || [])[2];
    rows.sort(function (a, b) {
      var av = a[k], bv = b[k];
      if (av == null && bv == null) return 0; if (av == null) return 1; if (bv == null) return -1;
      var base = kind === 'num' ? (Number(av) - Number(bv)) : String(av).localeCompare(String(bv));
      return HSORT.dir === 1 ? -base : base;
    });
    var arrow = function (c) { return HSORT.key === c ? (HSORT.dir === 1 ? ' <span style="color:var(--blu,#4D7CFE)">▼</span>' : ' <span style="color:var(--blu,#4D7CFE)">▲</span>') : ' <span style="opacity:.25;font-size:9px">⇅</span>'; };
    var head = HCOLS.map(function (c) { return th(c[1] + arrow(c[0]), 'data-mp-sort="' + c[0] + '"' + (c[2] === 'txt' ? ' style="text-align:left"' : '')).replace('>' + esc(c[1] + arrow(c[0])) + '<', '>' + c[1] + arrow(c[0]) + '<'); }).join('');
    var body = rows.map(function (r) {
      return '<tr style="border-bottom:1px solid var(--line,#1E2A44)">'
        + '<td class="sym" style="padding:8px 10px">' + esc(r.symbol) + (window.ScorrCardRow ? window.ScorrCardRow(r.symbol) : '') + '</td>'
        + td(r.qty != null ? r.qty : '—') + td(num2(r.entry_price)) + td(num2(r.current_price)) + td(inr2(r.current_value))
        + td('<span class="' + cls(r.pnl) + '">' + inr2(r.pnl) + '</span>') + td('<span class="' + cls(r.pnl_pct) + '">' + pct(r.pnl_pct) + '</span>')
        + td(r.weight == null ? '—' : (+r.weight).toFixed(1) + '%') + td(num2(r.stop_loss_price)) + '</tr>';
    }).join('');
    return tableWrap('<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody>', 'mp-sticky');   // cc#1731: pinned header
  }

  // cc#1728: NEXT REVIEW and STATE left the strip (founder 06-Sep: "hide box Next Review and
  // State"). Hidden behind a const, not deleted — flip to true and the two tiles come back in
  // their old place at the end of the strip. Next review still shows inside the (i) sheet.
  var SHOW_REVIEW_STATE = false;
  function money2(v) { return v == null || isNaN(+v) ? '—' : '₹' + (+v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

  // cc#1728: the cash figure. Nothing SERVES qb_nav_daily.cash_value on its own —
  // /api/performance/qb serves market_value = holdings_value + cash_value already added, and
  // /api/qb/rebalance_history serves the cash the last rebalance/seed run wrote
  // (blocks[].footer.cash_after, 159.60 for the 04-Sep seed). Endpoints are read-only on this
  // card, so: CASH = the served ledger cash, cross-checked against the served NAV basis —
  // HOLDING VALUE (sum of served current_value) + CASH must equal market_value within Rs 1
  // (market_value is served rounded to the rupee). If the check fails (a nightly HS1 exit moves
  // cash without a new rebalance block) the box shows the NAV-implied cash instead and the
  // discrepancy is logged — the number on glass is never one the NAV basis disagrees with.
  function cashFigure(mktVal, holdVal, ledgerCash) {
    var navCash = (mktVal != null && holdVal != null) ? (mktVal - holdVal) : null;
    if (ledgerCash == null) return { cash: navCash, sub: navCash == null ? 'uninvested' : 'uninvested · NAV-implied', ok: navCash != null, diff: null };
    if (navCash == null) return { cash: ledgerCash, sub: 'uninvested', ok: true, diff: null };
    var diff = (holdVal + ledgerCash) - mktVal;
    if (Math.abs(diff) <= 1) return { cash: ledgerCash, sub: 'uninvested', ok: true, diff: diff };
    try { console.warn('cc#1728 cash assert: holdings ' + holdVal.toFixed(2) + ' + ledger cash ' + ledgerCash.toFixed(2)
      + ' = ' + (holdVal + ledgerCash).toFixed(2) + ' vs NAV-basis market value ' + mktVal.toFixed(2) + ' (off by ' + diff.toFixed(2) + ')'); } catch (e) {}
    return { cash: navCash, sub: 'uninvested · NAV-implied (ledger ' + money2(ledgerCash) + ')', ok: false, diff: diff };
  }

  function kpiHtml(reg, d, hs1cnt, holdVal, cashInfo, nHold) {
    var cap = Number(reg.capital) || 0, maxN = reg.max_stocks != null ? reg.max_stocks : '—';
    var tile = function (k, v, sub, extra) { return '<div class="mp-kpi"><div class="k">' + k + '</div><div class="v ' + (extra || 'neu') + '">' + v + '</div>' + (sub ? '<div class="s">' + sub + '</div>' : '') + '</div>'; };
    var badge = hs1cnt > 0 ? '<span class="mp-badge risk">' + hs1cnt + ' near HS1</span>' : '<span class="mp-badge ok">No risk</span>';
    // cc#1728 order: CAPITAL | HOLDING VALUE | CASH | P&L | RETURN % | ALPHA | POSITIONS
    var h = '<div class="mp-kpis">'
      + tile('Capital', '₹' + cap.toLocaleString('en-IN'), 'Rs 5,000 slots')
      + tile('Holding Value', money2(holdVal), nHold + ' holding' + (nHold === 1 ? '' : 's') + ' at CMP')
      + tile('Cash', money2(cashInfo.cash), esc(cashInfo.sub))
      // cc#1819: P&L is live (marked at every price tick); Return %/Alpha update once a day at
      // EOD, so the two can legitimately show different pictures on the same screen — each sub-
      // label now says which basis it is, instead of leaving them to look like they disagree.
      + tile('P&amp;L', inr(d.pnl), d.pnl_as_of ? ('live · ' + esc(d.pnl_as_of)) : 'live', cls(d.pnl))
      + tile('Return %', pct(d.return_pct), 'on ₹' + cap.toLocaleString('en-IN') + ' · EOD ' + fmtDMY(d.nav_date), cls(d.return_pct))
      + tile('Alpha', d.alpha == null ? '—' : pct(d.alpha), 'vs basket benchmark · EOD ' + fmtDMY(d.nav_date), cls(d.alpha))
      + tile('Positions', (d.positions != null ? d.positions : '—') + '/' + maxN, 'cap ' + maxN);
    if (SHOW_REVIEW_STATE) {
      h += tile('Next Review', fmtDMY(reg.next_rebalance), esc(reg.rebalance_freq || 'monthly'))
        + '<div class="mp-kpi"><div class="k">State</div><div class="v" style="margin-top:6px">' + badge + '</div><div class="s">HS1 −20% nightly</div></div>';
    }
    return h + '</div>';
  }

  // cc#1728: the (i) sheet body — registry.notes sectioned SELECTION / SIZING / EXITS / CADENCE.
  // The text is the served notes string split into sentences and sorted by keyword (the cc#1708
  // basketInfoHtml pattern: pulled by keyword, never typed here). Registry fields that also
  // matter to the reader — max positions, weight band, next review, frequency — are appended as
  // served bullets in the section they belong to. A section with nothing in it is not rendered.
  function rulesHtml(reg) {
    var S = window.ScorrInfoSheet, sec = S ? S.section : function (k, h) { return '<div class="isheet-sec"><div class="isheet-k">' + esc(k) + '</div><div class="isheet-v">' + h + '</div></div>'; };
    var sents = String(reg.notes || '').split(/(?<=\.)\s+(?=[A-Z(])/).map(function (x) { return x.trim(); }).filter(Boolean);
    var buckets = { SELECTION: [], SIZING: [], EXITS: [], CADENCE: [] };
    sents.forEach(function (x) {
      if (/\bHS[12]\b|stop|exit/i.test(x)) buckets.EXITS.push(x);
      else if (/equal weight|weight band|\bslot|whole shares|\bRs ?\d|₹|capital|\/\d+\b/i.test(x)) buckets.SIZING.push(x);
      else if (/monthly|quarterly|nightly|weekly|rebalanc|review|rescreen/i.test(x)) buckets.CADENCE.push(x);
      else buckets.SELECTION.push(x);
    });
    if (reg.max_stocks != null || reg.weight_band) buckets.SIZING.push('Max ' + (reg.max_stocks != null ? reg.max_stocks : '—') + ' positions' + (reg.weight_band ? ' · ' + reg.weight_band + ' weight band' : '') + (reg.capital != null ? ' · capital ₹' + Number(reg.capital).toLocaleString('en-IN') : ''));
    if (reg.next_rebalance || reg.rebalance_freq) buckets.CADENCE.push('Next review ' + fmtDMY(reg.next_rebalance) + (reg.rebalance_freq ? ' · ' + reg.rebalance_freq : ''));
    var h = '';
    ['SELECTION', 'SIZING', 'EXITS', 'CADENCE'].forEach(function (k) {
      if (buckets[k].length) h += sec(k, '<ul>' + buckets[k].map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>');
    });
    return h || sec('RULES', esc(reg.notes || '—'));
  }
  function openRules(reg) {
    if (!window.ScorrInfoSheet) return;
    window.ScorrInfoSheet.open(MP_LABEL + ' · Rules', rulesHtml(reg));
  }

  function loadBasketPane(el) {
    el.innerHTML = '<div class="mp-msg">Loading ' + MP_LABEL + '…</div>';
    Promise.all([
      getJ('/api/qb/registry'),
      getJ('/api/performance/qb'),
      getJ('/api/performance/alpha').catch(function () { return {}; }),
      getJ('/api/qb/positions?basket_name=' + MP_BASKET + '&status=open'),
      getJ('/api/qb/rebalance_history?basket_name=' + MP_BASKET + '&limit=300').catch(function (e) { return { error: e.message }; })
    ]).then(function (a) {
      var regs = Array.isArray(a[0]) ? a[0] : [];
      var reg = regs.filter(function (r) { return r && r.basket_name === MP_BASKET; })[0];
      if (!reg) { el.innerHTML = '<div class="mp-msg">' + MP_LABEL + ' is not in the basket registry yet.</div>'; return; }
      var qb = a[1] || {}, alpha = a[2] || {};
      var d = {};
      (qb.baskets || []).forEach(function (b) { if (b.basket === MP_BASKET) d = Object.assign({}, b); });
      (alpha.baskets || []).forEach(function (b) { if (b.basket === MP_BASKET) { d.alpha = b.alpha; d.return_pct = b.return_pct; if (d.pnl == null) d.pnl = b.pnl; if (d.positions == null) d.positions = b.positions; if (d.nav_date == null) d.nav_date = b.nav_date; } });
      // cc#1819: P&L is live intraday marks (quant_paper_positions.updated_at, qb's own as_of_ist);
      // Return %/Alpha are the once-daily qb_nav_daily series, appended the FOLLOWING pre-market
      // morning for the prior close — always one EOD cycle behind P&L, and for a basket on its
      // very first tracked day (nav_date == its own inception day) that lag reads as an exact
      // 0.00%, not a computation error. Both numbers are correct on their own basis; the KPI
      // sub-labels below say which basis each is, so they stop looking like a disagreement.
      d.pnl_as_of = qb.as_of_ist || null;
      var pos = Array.isArray(a[3]) ? a[3] : [];
      // state badge = the QB card rule for a basket without HS2: names at/under HS1 −20% from entry
      var hs1cnt = (qb.positions || []).filter(function (p) { return p.basket === MP_BASKET && p.pnl_pct != null && +p.pnl_pct < -20; }).length;
      var mktVal = d.market_value != null ? +d.market_value : pos.reduce(function (s, p) { return s + (+p.current_value || 0); }, 0);
      var rh = a[4] || {};
      // cc#1728: HOLDING VALUE = sum of the served open positions at CMP (current_value), no cash.
      var holdVal = pos.reduce(function (s, p) { return s + (p.current_value != null ? +p.current_value : 0); }, 0);
      // the ledger cash the latest served block wrote (blocks are newest-first from the endpoint)
      var blk = ((rh && rh.blocks) || []).filter(function (b) { return b && b.footer && b.footer.cash_after != null; })[0];
      var cashInfo = cashFigure(d.market_value != null ? +d.market_value : null, holdVal, blk ? +blk.footer.cash_after : null);
      // cc#1728: TYPE once — the served `type` (Quant/Discretionary), not phase + type.
      var tag = String(reg.type || 'Discretionary');
      el.innerHTML =
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
        + '<span style="font-size:15px;font-weight:800;color:var(--txt)">' + MP_LABEL + '</span>'
        + '<button type="button" class="ibtn" id="mpInfoBtn" aria-label="Rules" title="Rules">i</button>'
        + (hs1cnt > 0 ? '<span class="mp-badge risk" id="mpHs1Chip">' + hs1cnt + ' near HS1</span>' : '')
        + '<span id="mpTag" style="font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)">' + esc(tag) + '</span>'
        + '</div>'
        + kpiHtml(reg, d, hs1cnt, holdVal, cashInfo, pos.length)
        + '<div class="mp-sec">Holdings · ' + pos.length + '</div><div id="mpHoldings">' + holdingsHtml(pos.slice(), mktVal) + '</div>'
        + '<div class="mp-tabs" id="mpTabs"><button type="button" class="mp-tab active" data-mp-tab="rebalance">Rebalance History</button><button type="button" class="mp-tab" data-mp-tab="hsl">HSL History</button></div>'
        + '<div id="mpHist">' + (rh.error ? '<div class="mp-msg">Rebalance history unavailable — ' + esc(rh.error) + '</div>' : rebalanceHtml(rh)) + '</div>';
      var ib = el.querySelector('#mpInfoBtn');
      if (ib) ib.addEventListener('click', function () { openRules(reg); });
      var hold = el.querySelector('#mpHoldings');
      hold.addEventListener('click', function (e) {
        var t = e.target && e.target.closest ? e.target.closest('[data-mp-sort]') : null;
        if (!t) return;
        var k = t.getAttribute('data-mp-sort');
        if (HSORT.key === k) HSORT.dir = -HSORT.dir; else { HSORT.key = k; HSORT.dir = (k === 'symbol') ? -1 : 1; }
        hold.innerHTML = holdingsHtml(pos.slice(), mktVal);
      });
      el.querySelector('#mpTabs').addEventListener('click', function (e) {
        var t = e.target && e.target.closest ? e.target.closest('[data-mp-tab]') : null;
        if (!t) return;
        var key = t.getAttribute('data-mp-tab');
        Array.prototype.forEach.call(el.querySelectorAll('[data-mp-tab]'), function (b) { b.classList.toggle('active', b === t); });
        el.querySelector('#mpHist').innerHTML = rh.error ? '<div class="mp-msg">Rebalance history unavailable — ' + esc(rh.error) + '</div>' : (key === 'hsl' ? hslHtml(rh) : rebalanceHtml(rh));
      });
    }).catch(function (e) {
      el.innerHTML = '<div class="mp-msg">' + MP_LABEL + ' unavailable — ' + esc(e && e.message || 'fetch failed') + '</div>';
    });
  }

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
    ensurePaneStyle();
    var active = null;
    // cc#1715: the basket pane renders first (on open, no click); the cc#1677 launcher sits BELOW it.
    container.innerHTML =
      '<div style="padding:4px 2px 2px">'
      + '<div id="mpDefault"></div>'
      + '<div class="mp-sec" style="margin-top:22px">All baskets &amp; screeners</div>'
      + '<div style="font-size:12px;color:var(--mut);margin-top:4px">Baskets and screeners are captured on their own pages. Open a table view below.</div>'
      + '<div style="display:flex;gap:10px;margin-top:12px" id="mpBtnRow">'
      + '<button type="button" class="mp-btn" data-mp-btn="baskets">QUANT BASKETS</button>'
      + '<button type="button" class="mp-btn" data-mp-btn="screeners">QUANT SCREENERS</button>'
      + '</div>'
      + '<div id="mpBody"></div>'
      + '</div>';
    var btnRow = container.querySelector('#mpBtnRow');
    var bodyEl = container.querySelector('#mpBody');
    loadBasketPane(container.querySelector('#mpDefault'));
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

  window.ScorrModelPortfolio = { mount: mount, rulesHtml: rulesHtml, cashFigure: cashFigure };   // cc#1728: builders exposed for the render check
})();

/* Scorr C·A·R·D quick-nav strip — SINGLE SOURCE (cc#789)
 * ======================================================
 * The C·A·R·D strip (cc#675) used to live INSIDE v8_dashboard.html, and the GVM page carried its
 * own divergent copy as an inline React button array. Two implementations meant two behaviours —
 * the A button drifted, which is what the founder reported. This file is now the ONLY place the
 * strip is defined.
 *
 * FORWARD RULE (cc#789, founder 02-Aug — binding on every future surface):
 *   ANY new surface (client holdings, adaptive dashboard, cockpit, R-card, anything) renders the
 *   strip ONLY via window.ScorrCardStripHtml(symbol, activeLetter). NEVER re-implement it, never
 *   copy this markup into a page. If a page needs different behaviour behind a letter, it
 *   REGISTERS an opener (below) — it does not fork the strip.
 *
 * WIRING
 *   window.ScorrCardStripHtml(sym, active)  -> strip HTML string
 *   window.ScorrCardNav(letter, sym)        -> dispatch a letter
 *   window.ScorrCardStrip.register({C,A,R,D})   page-local openers (richer than the fallbacks)
 *   window.ScorrCardStrip.registerClose(fn)     called before any nav, to close page overlays
 *   window.ScorrCardStrip.setFutures(symbols)   page hands over a futures universe it already has
 *
 * DISPATCH PRECEDENCE — one rule, everywhere: a page-registered opener wins; otherwise the
 * site-wide fallback runs (ScorrChartCard / ScorrRCard / cockpit deep-link). So every page goes
 * through the SAME dispatcher and the SAME availability logic; a page can only make a letter
 * richer, never make the strip look or behave differently.
 */
(function () {
  if (window.ScorrCardStrip) return;

  var _handlers = {};      // page-registered openers, by letter
  var _closers = [];       // page-registered overlay closers
  var _futures = null;     // Set of futures symbols, or null = not yet known
  var _futReq = null;      // in-flight fetch, so N strips on a page cause ONE request

  /* Strip CSS lives here too — a page that styled these buttons itself was part of the drift. */
  var CSS = ''
    + '.scorr-card-strip{display:inline-flex;gap:5px;align-items:center;flex-shrink:0}'
    + '.scorr-cs-b{width:32px;height:32px;border-radius:7px;font:800 13px/1 Sora,system-ui,sans-serif;'
    + 'display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line2,#e8ecf2);'
    + 'background:var(--surface2,#f6f8fb);color:var(--txt,#1c2536);cursor:pointer;padding:0}'
    + '.scorr-cs-b:hover{border-color:var(--blu,#4d7cfe)}'
    + '.scorr-cs-on{background:var(--blu,#4d7cfe);color:#fff;border-color:var(--blu,#4d7cfe);cursor:default}'
    + '.scorr-cs-off{background:transparent;color:var(--dim,#9aa4b5);opacity:.42;cursor:not-allowed}';
  try {
    var st = document.createElement('style');
    st.setAttribute('data-scorr', 'card-strip');
    st.appendChild(document.createTextNode(CSS));
    (document.head || document.documentElement).appendChild(st);
  } catch (e) {}

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ── availability ────────────────────────────────────────────────────────────────────────
   * cc#789 item 4: ONE rule for which letters render, everywhere. D is futures-only; R
   * self-handles its own empty state; C and A are always available.
   *
   * The old v8_dashboard version read `cache.metricsAll`, a page-local variable — on any other
   * page that is undefined, so a naive extraction would have ghosted D everywhere. The universe
   * is now resolved in three tiers: a page that already has it calls setFutures(); otherwise it
   * is fetched once and cached; until it resolves we render D ENABLED rather than ghosted,
   * because wrongly hiding a working button is worse than a tap that lands on a cockpit which
   * already handles an unknown symbol. */
  function setFutures(list) {
    try {
      var s = new Set();
      (list || []).forEach(function (x) {
        var v = (x && x.symbol) ? x.symbol : x;
        if (v) s.add(String(v).toUpperCase());
      });
      if (s.size) { _futures = s; _repaint(); }
    } catch (e) {}
  }

  function _loadFutures() {
    if (_futures || _futReq) return;
    try {
      _futReq = fetch('/api/v8/futures/list?active_only=true', { headers: { Accept: 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          var arr = d && (d.stocks || d.futures || d.symbols || d.data || (Array.isArray(d) ? d : null));
          if (arr) setFutures(arr);
        })
        .catch(function () {})
        .then(function () { _futReq = null; });
    } catch (e) { _futReq = null; }
  }

  function cardAvail(sym) {
    var known = (_futures !== null);
    var fut = known ? _futures.has(String(sym || '').toUpperCase()) : true;   // unknown -> enabled
    return { fut: fut, result: true, resolved: known };
  }

  /* Repaint every strip already on the page once the universe resolves, so D stops being
   * optimistic without the user having to reopen anything. */
  function _repaint() {
    try {
      var nodes = document.querySelectorAll('.scorr-card-strip[data-sym]');
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        var html = stripHtml(n.getAttribute('data-sym'), n.getAttribute('data-active') || '');
        if (html) {
          var tmp = document.createElement('div');
          tmp.innerHTML = html;
          if (tmp.firstChild) n.innerHTML = tmp.firstChild.innerHTML;
        }
      }
    } catch (e) {}
  }

  /* ── strip HTML ──────────────────────────────────────────────────────────────────────── */
  var ITEMS = [
    ['C', 'Chart'],
    ['A', 'Analysis'],
    ['R', 'Result'],
    ['D', 'Cockpit (Futures only)']
  ];

  function stripHtml(sym, active) {
    if (!sym) return '';
    if (_futures === null) _loadFutures();
    var av = cardAvail(sym);
    var s = esc(sym);
    var btns = ITEMS.map(function (it) {
      var k = it[0], title = it[1];
      var en = (k === 'D') ? av.fut : (k === 'R' ? av.result : true);
      if (k === active) return '<span class="scorr-cs-b scorr-cs-on" title="' + title + ' (current)">' + k + '</span>';
      if (!en) return '<span class="scorr-cs-b scorr-cs-off" title="' + title + '">' + k + '</span>';
      return '<button type="button" class="scorr-cs-b" title="' + title + '" '
        + 'onclick="ScorrCardNav(\'' + k + '\',\'' + s + '\')">' + k + '</button>';
    }).join('');
    return '<span class="scorr-card-strip" data-sym="' + s + '" data-active="' + esc(active || '') + '">'
      + btns + '</span>';
  }

  /* ── dispatch ───────────────────────────────────────────────────────────────────────── */
  function register(h) {
    if (!h) return;
    ['C', 'A', 'R', 'D'].forEach(function (k) { if (typeof h[k] === 'function') _handlers[k] = h[k]; });
  }
  function registerClose(fn) { if (typeof fn === 'function') _closers.push(fn); }

  function _closeAll() {
    _closers.forEach(function (fn) { try { fn(); } catch (e) {} });
    try { if (window.ScorrRCard && window.ScorrRCard.close) window.ScorrRCard.close(); } catch (e) {}
    try { if (window.ScorrChartCard && window.ScorrChartCard.close) window.ScorrChartCard.close(); } catch (e) {}
  }

  /* Site-wide fallbacks, used when a page registers no opener for a letter. These are the
   * globals every page already loads (results_card.js, scorr_chart_card.js). */
  function _fallback(k, sym) {
    if (k === 'C') { if (window.ScorrChartCard) window.ScorrChartCard.open(sym); return; }
    if (k === 'A') {
      /* cc#789 item 3 — A PARITY. The canonical A is the V8 Analysis modal (GVM + sector rank,
       * volume, delivery, trajectory, TC footer). Its renderer has a long transitive tail
       * (trajHtml/_volTilesHtml/heatHtml/_trajValTile/_heatTile/qaVolExplain/TC embed + their CSS)
       * that lives in v8_dashboard.html, so cloning it here would recreate the very duplication
       * this task exists to kill. Instead we deep-link to the ONE implementation via ?qa=.
       * Consequence, stated plainly: on non-V8 surfaces A opens the dashboard tab rather than an
       * in-page overlay. Content and behaviour are identical because it IS the same modal. */
      window.open('/dashboard?qa=' + encodeURIComponent(sym), '_blank');
      return;
    }
    if (k === 'R') { if (window.ScorrRCard) window.ScorrRCard.open(sym); return; }
    if (k === 'D') { window.open('/dashboard?dc=' + encodeURIComponent(sym), '_blank'); return; }
  }

  function cardNav(k, sym) {
    if (!sym || !k) return;
    _closeAll();
    var h = _handlers[k];
    if (h) { try { h(sym); return; } catch (e) { /* fall through to the site-wide opener */ } }
    _fallback(k, sym);
  }

  window.ScorrCardStripHtml = stripHtml;
  window.ScorrCardNav = cardNav;
  window.ScorrCardStrip = {
    html: stripHtml,
    nav: cardNav,
    avail: cardAvail,
    register: register,
    registerClose: registerClose,
    setFutures: setFutures
  };
})();

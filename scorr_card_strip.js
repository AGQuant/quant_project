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
 *   window.ScorrCardStrip.register({C,A,R,D})   RETAINED FOR COMPATIBILITY ONLY — see below
 *   window.ScorrCardStrip.registerClose(fn)     called before any nav, to close page overlays
 *   window.ScorrCardStrip.setFutures(symbols)   page hands over a futures universe it already has
 *
 * DISPATCH — one rule, everywhere: every letter opens its shared component.
 *   C -> scorr_chart_card.js      (cc#706, locked cc#803)
 *   A -> scorr_analysis_card.js   (cc#805, locked cc#805)
 *   R -> results_card.js          (cc#573, locked cc#803)
 *   D -> scorr_cockpit_card.js    (cc#805, locked cc#805)
 * As of cc#805 ALL FOUR letters are locked, so register() is a no-op that warns: there is no way for
 * a page to change what a letter does. Every surface therefore gets not just the same strip markup
 * (cc#789) but the same card behind every button.
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
    + '.scorr-cs-off{background:transparent;color:var(--dim,#9aa4b5);opacity:.42;cursor:not-allowed}'
    /* cc#798 COMPACT variant — for table rows, where the 32px modal-header strip is too heavy.
       Same component, same dispatcher, same availability rules; only the metrics change. This is a
       SIZE PROP, deliberately not a second implementation: a table-row copy is precisely how the
       R/V pair drifted from the modal in the first place. */
    + '.scorr-cs-sm{gap:3px;vertical-align:middle;margin-left:6px}'
    + '.scorr-cs-sm .scorr-cs-b{width:22px;height:22px;border-radius:5px;font-size:10px}';
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
        // cc#798: carry the compact flag through the repaint, or a table-row strip would silently
        // rebuild at modal size the moment the futures universe resolves.
        var html = stripHtml(n.getAttribute('data-sym'), n.getAttribute('data-active') || '',
                             { compact: n.getAttribute('data-compact') === '1' });
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

  /* opts.compact renders the table-row size. Everything else — availability, dispatch, the active
     state — is identical, because it IS the same function. */
  function stripHtml(sym, active, opts) {
    if (!sym) return '';
    if (_futures === null) _loadFutures();
    var av = cardAvail(sym);
    var s = esc(sym);
    var compact = !!(opts && opts.compact);
    var btns = ITEMS.map(function (it) {
      var k = it[0], title = it[1];
      var en = (k === 'D') ? av.fut : (k === 'R' ? av.result : true);
      if (k === active) return '<span class="scorr-cs-b scorr-cs-on" title="' + title + ' (current)">' + k + '</span>';
      if (!en) return '<span class="scorr-cs-b scorr-cs-off" title="' + title + '">' + k + '</span>';
      return '<button type="button" class="scorr-cs-b" title="' + title + '" '
        + 'onclick="ScorrCardNav(\'' + k + '\',\'' + s + '\')">' + k + '</button>';
    }).join('');
    return '<span class="scorr-card-strip' + (compact ? ' scorr-cs-sm' : '') + '" data-sym="' + s
      + '" data-active="' + esc(active || '') + '"' + (compact ? ' data-compact="1"' : '') + '>'
      + btns + '</span>';
  }

  /* cc#798 convenience for table cells — one short call per row, so a row author never hand-writes
     markup. Replaces the legacy ScorrRCard.pill + pillV pair. */
  function rowStrip(sym) { return stripHtml(sym, '', { compact: true }); }

  /* ── dispatch ───────────────────────────────────────────────────────────────────────── */

  /* cc#803/#805 LOCKED LETTERS — ALL FOUR. cc#789 made the strip's MARKUP single-source but left its
   * BEHAVIOUR overridable by any page, which is the same drift one level down: /dashboard registered
   * its own C and opened a page-local chart overlay while /gvm opened the shared ScorrChartCard — one
   * button, two different charts.
   *
   * cc#803 locked C and R and wrote down the exception that let A and D stay overridable: "a letter
   * is overridable ONLY while no shared component exists for it". THAT EXCEPTION IS NOW CLOSED.
   * cc#805 extracted the Analysis modal into scorr_analysis_card.js and the Derivative Cockpit into
   * scorr_cockpit_card.js (both built on scorr_card_common.js), so every letter has a shared
   * component and every letter is locked to it site-wide. There is no longer any condition under
   * which a page may register an opener — register() ignores all four with a console warning.
   *
   * FORWARD RULE: a new surface that wants different behaviour behind a letter EXTENDS that shared
   * file. It does not register, and it does not fork. Adding a fifth letter means adding its shared
   * component here at the same time, not "temporarily" leaving it page-overridable. */
  var _LOCKED = {
    C: 'scorr_chart_card.js',
    A: 'scorr_analysis_card.js',
    R: 'results_card.js',
    D: 'scorr_cockpit_card.js'
  };

  function register(h) {
    if (!h) return;
    ['C', 'A', 'R', 'D'].forEach(function (k) {
      if (typeof h[k] !== 'function') return;
      if (_LOCKED[k]) {
        try {
          console.warn('[ScorrCardStrip] "' + k + '" is locked to the shared ' + _LOCKED[k] +
                       ' and cannot be overridden per page (cc#803/#805 — all four letters are now ' +
                       'locked). Registration ignored — extend that shared file instead.');
        } catch (e) {}
        return;
      }
      _handlers[k] = h[k];
    });
  }
  function registerClose(fn) { if (typeof fn === 'function') _closers.push(fn); }

  function _closeAll() {
    _closers.forEach(function (fn) { try { fn(); } catch (e) {} });
    try { if (window.ScorrRCard && window.ScorrRCard.close) window.ScorrRCard.close(); } catch (e) {}
    try { if (window.ScorrChartCard && window.ScorrChartCard.close) window.ScorrChartCard.close(); } catch (e) {}
    /* cc#805: A and D are shared components now, so the dispatcher closes them itself instead of
       relying on each host page to have registered a closer for them. */
    try { if (window.ScorrAnalysisCard && window.ScorrAnalysisCard.close) window.ScorrAnalysisCard.close(); } catch (e) {}
    try { if (window.ScorrCockpitCard && window.ScorrCockpitCard.close) window.ScorrCockpitCard.close(); } catch (e) {}
  }

  /* Site-wide fallbacks, used when a page registers no opener for a letter. These are the
   * globals every page already loads (results_card.js, scorr_chart_card.js). */
  function _fallback(k, sym) {
    if (k === 'C') { if (window.ScorrChartCard) window.ScorrChartCard.open(sym); return; }
    if (k === 'A') {
      /* cc#805 — A PARITY, FINALLY IN-PAGE. cc#789 had to deep-link A to /dashboard?qa= because the
       * canonical Analysis renderer had a long transitive tail (trajHtml/_volTilesHtml/heatHtml/
       * _trajValTile/_heatTile/qaVolExplain/TC embed + their CSS) locked inside v8_dashboard.html;
       * cloning it here would have recreated the duplication cc#789 existed to kill. cc#805 moved
       * that tail into scorr_card_common.js + scorr_analysis_card.js, so A now opens the SAME modal
       * IN PAGE on every surface. The deep link stays only as a degradation path for a context where
       * the shared file did not load (embeds), so the strip never becomes a dead button. */
      if (window.ScorrAnalysisCard) { window.ScorrAnalysisCard.open(sym); return; }
      window.open('/dashboard?qa=' + encodeURIComponent(sym), '_blank');
      return;
    }
    if (k === 'R') { if (window.ScorrRCard) window.ScorrRCard.open(sym); return; }
    if (k === 'D') {
      /* cc#805 — same story as A: the Derivative Cockpit is now scorr_cockpit_card.js, opened in
       * page. ?dc= remains the degradation path only. */
      if (window.ScorrCockpitCard) { window.ScorrCockpitCard.open(sym); return; }
      window.open('/dashboard?dc=' + encodeURIComponent(sym), '_blank');
      return;
    }
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
  window.ScorrCardRow = rowStrip;      // cc#798: table-row shorthand, replaces ScorrRCard.pill+pillV
  window.ScorrCardStrip = {
    html: stripHtml,
    row: rowStrip,
    nav: cardNav,
    avail: cardAvail,
    register: register,
    registerClose: registerClose,
    setFutures: setFutures
  };
})();

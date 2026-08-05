/*
 * scorr_mobile_cards.js — cc#859 Part A: THE SHARED MOBILE SECTION CARD.
 *
 * Built ONCE and imported by every mobile surface. Duplicating it in a second surface is a spec
 * violation under UI_PATTERN_MASTER_INDEX_V1 (9016) pattern 2 and MOBILE_APP_FRAMEWORK_V1 (15913)
 * core rule 8 — so cc#862 (Home) and cc#863 (V8 stack) both consume THIS, and neither defines a
 * card of its own.
 *
 * SCOPE OF PART A — STRUCTURE ONLY, DELIBERATELY.
 * cc#859 item 9 reserves the visual language to the design ref (design_refs/scorr_mobile_R2.html,
 * committed by cc#861) and states "CC does NOT re-skin any page under this card". So this file
 * implements the GRAMMAR that is already written down — the breakpoint, the tier system, the header
 * contract, tap targets, collapse behaviour, skeletons, the state rail — and NO page is re-skinned
 * by it. When R2 lands, the ref governs the finish; the structure below is what it attaches to.
 *
 * The grammar is not invented here. Every rule traces to a written source:
 *   15913 core rule 1   breakpoint 768px, no horizontal tab row below it
 *   15913 core rule 3   PRIMARY / SECONDARY / TERTIARY, every surface needs >=1 PRIMARY
 *   15913 core rule 4   header contract: title + live value or state chip + chevron
 *   15913 core rule 5   44x44px targets, 8px apart, header tappable full width
 *   15913 core rule 6   missing values render as -- and NEVER 0.0
 *   15913 core rule 7   skeletons sized to final height, no layout jump
 *   15913 core rule 9   the STATE RAIL — 3px coloured left edge, live cards breathe
 *   15913 core rule 10  LIVE/STALE/OFF differ by SHAPE, not opacity alone
 *   15913 theme         DARK, founder-confirmed 05-Aug
 *
 * WHY THE HEADER CONTRACT IS THE LOAD-BEARING PART: the header alone must answer "do I need to open
 * this". A collapsed card that shows only a title has moved the work, not removed it — so a
 * SECONDARY card is required to carry its headline value in the header, and render() enforces that
 * by rendering the value slot whether or not the caller remembered to pass one.
 */
(function () {
  'use strict';
  if (window.ScorrMobileCards) return;          // import-once guard (core rule 8)

  var BREAKPOINT = 768;                          // core rule 1
  var TIERS = { PRIMARY: 'primary', SECONDARY: 'secondary', TERTIARY: 'tertiary' };

  function isMobile() { return window.matchMedia('(max-width:' + (BREAKPOINT - 1) + 'px)').matches; }

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* core rule 6 — a missing number is '--', never 0.0. This is the single place that decision is
     made, so no surface can quietly render a zero it does not have. */
  function num(v, dp) {
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) return '--';
    var n = Number(v);
    if (isNaN(n)) return '--';
    return n.toLocaleString('en-IN', {
      minimumFractionDigits: dp == null ? 2 : dp,
      maximumFractionDigits: dp == null ? 2 : dp
    });
  }

  function pct(v, dp) {
    if (v === null || v === undefined || isNaN(Number(v))) return '--';
    var n = Number(v);
    return (n >= 0 ? '+' : '') + n.toFixed(dp == null ? 2 : dp) + '%';
  }

  /* Collapse state — PAGE SESSION ONLY, per cc#859 item 1 ("persists in the page session only").
     Deliberately sessionStorage and not localStorage: a card the user opened once should not still
     be open next week, and the founder asked for session scope explicitly. */
  var SKEY = 'scorr_mcards_open';
  function readOpen() {
    try { return JSON.parse(sessionStorage.getItem(SKEY) || '{}'); } catch (e) { return {}; }
  }
  function writeOpen(m) { try { sessionStorage.setItem(SKEY, JSON.stringify(m)); } catch (e) {} }

  /* STATE RAIL + chip. Shape carries the meaning; colour only reinforces it (core rule 10), so a
     glancing or colour-blind reader cannot mistake a dead engine for a running one. */
  function stateChip(state) {
    if (!state) return '';
    var s = String(state).toUpperCase();
    var glyph = s === 'LIVE' ? '<i class="smc-dot"></i>'
              : s === 'STALE' ? '<i class="smc-ring"></i>'
              : s === 'OFF' ? '<i class="smc-bar"></i>' : '';
    return '<span class="smc-chip smc-' + s.toLowerCase() + '">' + glyph + esc(s) + '</span>';
  }

  /* Skeleton sized to the FINAL height (core rule 7) — a card that grows after paint feels slower
     than one that waited, which is the whole reason cc#858 sized its placeholders too. */
  function skeleton(h) {
    return '<div class="smc-skel" style="height:' + (h || 72) + 'px"></div>';
  }

  /*
   * render(cfg) -> HTML string for ONE section card.
   *   id        stable key for collapse persistence (required)
   *   title     uppercase muted label (core rule 4)
   *   tier      PRIMARY | SECONDARY | TERTIARY
   *   value     headline value shown IN THE HEADER — required in spirit for SECONDARY
   *   state     LIVE | STALE | OFF (optional; renders the shape chip + rail colour)
   *   body      HTML for the expanded region
   *   loading   render a skeleton body at `skelHeight`
   *
   * PRIMARY is always expanded and carries NO chevron — cc#859 item 3 says "full card, always
   * expanded", so giving it a collapse affordance would contradict its own tier.
   */
  function render(cfg) {
    cfg = cfg || {};
    var tier = TIERS[String(cfg.tier || 'SECONDARY').toUpperCase()] || 'secondary';
    var id = cfg.id || ('smc-' + Math.abs(String(cfg.title || '').split('').reduce(function (a, c) {
      return ((a << 5) - a) + c.charCodeAt(0) | 0; }, 0)));
    var isPrimary = tier === 'primary';
    var open = isPrimary ? true : (readOpen()[id] === true || cfg.open === true);
    var body = cfg.loading ? skeleton(cfg.skelHeight) : (cfg.body || '');

    return '<section class="smc smc-' + tier + (open ? ' open' : '') +
             (cfg.state ? ' smc-state-' + String(cfg.state).toLowerCase() : '') +
             '" data-smc-id="' + esc(id) + '">' +
      '<' + (isPrimary ? 'div' : 'button type="button"') + ' class="smc-hd"' +
        (isPrimary ? '' : ' aria-expanded="' + (open ? 'true' : 'false') + '"') + '>' +
        '<span class="smc-title">' + esc(cfg.title || '') + '</span>' +
        '<span class="smc-val">' + (cfg.value == null ? '' : cfg.value) + '</span>' +
        stateChip(cfg.state) +
        (isPrimary ? '' : '<span class="smc-chev" aria-hidden="true">›</span>') +
      '</' + (isPrimary ? 'div' : 'button') + '>' +
      '<div class="smc-body">' + body + '</div>' +
    '</section>';
  }

  /* stack(cards) — renders a list and ENFORCES the PRIMARY rule.
     cc#859 item 3: "A surface with no PRIMARY card is a defect." Rather than fail silently, the
     first card is promoted and a console warning names the surface, so the defect is visible in
     development instead of shipping as a flat, unranked stack — which is the exact flatness the
     card calls naive. */
  function stack(cards, surfaceName) {
    cards = cards || [];
    var hasPrimary = cards.some(function (c) {
      return String(c.tier || '').toUpperCase() === 'PRIMARY'; });
    if (!hasPrimary && cards.length) {
      cards[0] = Object.assign({}, cards[0], { tier: 'PRIMARY' });
      try {
        console.warn('[cc#859] surface "' + (surfaceName || '?') +
          '" declared no PRIMARY card — promoting the first. Every surface needs one (15913 rule 3).');
      } catch (e) {}
    }
    return '<div class="smc-stack">' + cards.map(render).join('') + '</div>';
  }

  /* Delegated toggle — ONE listener for every card on the page, so a surface never wires its own
     and they cannot drift. Header is tappable across its full width (core rule 5). */
  document.addEventListener('click', function (e) {
    var hd = e.target && e.target.closest && e.target.closest('.smc-hd');
    if (!hd) return;
    var card = hd.closest('.smc');
    if (!card || card.classList.contains('smc-primary')) return;   // PRIMARY never collapses
    var nowOpen = !card.classList.contains('open');
    card.classList.toggle('open', nowOpen);
    hd.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
    var m = readOpen(); m[card.getAttribute('data-smc-id')] = nowOpen; writeOpen(m);
  });

  window.ScorrMobileCards = {
    BREAKPOINT: BREAKPOINT,
    TIERS: TIERS,
    isMobile: isMobile,
    render: render,
    stack: stack,
    skeleton: skeleton,
    stateChip: stateChip,
    num: num,
    pct: pct,
    esc: esc
  };
})();

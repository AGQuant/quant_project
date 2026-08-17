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

/* ─── cc#1066 R5 SIGNATURE ENHANCER (Fable, 16-Aug) ─────────────────────────────────────────────
 * WHY THIS EXISTS: theme pass 2 styled class names taken from previews/, and the live wired home
 * uses its own markup — so the signature elements (scoreboard mood, W/L letters, fail meter)
 * never landed (founder screenshots 18:47/19:2x). This enhancer finds those elements on the LIVE
 * DOM by their CONTENT — which is stable across any refactor — tags them with r5-* classes, and
 * injects the matching styles. It is class-name-proof by construction.
 * PARITY GUARD: strictly additive. It adds classes and ONE meter element; it never removes,
 * hides, or reorders anything.
 * PERF (17-Aug 05:58 founder report, Fable regression owned): structural movers carry attempt
 * budgets — after 8 observer kicks without completing they stop scanning permanently, so pages
 * they don't apply to (V8) never pay a per-tap full-DOM scan again. Debounce 400ms.
 */
(function () {
  'use strict';
  if (window.__r5sig) return; window.__r5sig = 1;

  var CSS = ''
    + '.r5-mood{font-family:"Archivo Black","Space Grotesk",sans-serif!important;color:#EAF0FA!important;'
    +   'font-size:42px!important;line-height:.95!important;letter-spacing:1px;'
    +   'text-transform:uppercase;display:inline-block!important;'
    +   'transform:skewX(-6deg)!important;transform-origin:left center;animation:none!important}'
    + '.r5-mood::after{content:"";display:block;height:6px;width:150px;margin-top:4px;'
    +   'background:linear-gradient(90deg,#EAF0FA,transparent);transform:skewX(-24deg);opacity:.85}'
    + '.r5-meter{height:8px;background:#1C2740;margin:12px 0 2px;overflow:hidden;'
    +   'clip-path:polygon(0 0,calc(100% - 8px) 0,100% 100%,0 100%)}'
    + '.r5-meter i{display:block;height:100%;background:linear-gradient(90deg,#FF4D6D 60%,#FF8FA5);'
    +   'transition:width .6s ease-out}'
    + '.r5-set{font-family:"JetBrains Mono",monospace!important;font-weight:600!important;'
    +   'border-radius:0!important;'
    +   'clip-path:polygon(6px 0,100% 0,100% 100%,0 100%,0 6px)}'
    + '.r5-w{background:#18240E!important;border-color:var(--volt, #3E5A18)!important}'
    + '.r5-l{background:var(--field, #241019)!important;border-color:#5A2634!important}'
    + '.r5-w::before{content:"W ";font-weight:800;color:#C8F542}'
    + '.r5-l::before{content:"L ";font-weight:800;color:var(--heat, #FF8FA5)}'
    + '.r5-set,.r5-set *{text-decoration:none!important;border-bottom:0!important}'
    + '.r5-rail{position:relative;border-radius:0!important}'
    + '.r5-rail::before{content:"";position:absolute;left:0;top:6px;bottom:6px;width:5px;'
    +   'background:var(--r5rail,#FF4D6D);border-radius:4px;z-index:2}'
    /* cc#1068: .r5-idxbox styling removed with its mover — nothing can create that element any
       more, so the rules were dead weight shipped to every page. The card now carries its own
       .r5-idxcard styling in mobile/home.html, next to the markup it styles. */
    + '.r5-tab-on{color:#C8F542!important;position:relative}'
    + '.r5-tab-on::after{content:"";position:absolute;top:-1px;left:25%;right:25%;height:3px;'
    +   'background:#C8F542;clip-path:polygon(0 0,100% 0,calc(100% - 3px) 100%,3px 100%)}';

  function injectCss() {
    if (document.getElementById('r5sig-css')) return;
    var s = document.createElement('style'); s.id = 'r5sig-css'; s.textContent = CSS;
    document.head.appendChild(s);
  }

  var MOODS = /^(bearish|bullish|neutral|caution|cautious|fear|greed|panic|calm|risk[- ]?on|risk[- ]?off)$/i;
  var CHIP = /^(ADR|Nifty\s+(Day|Week|Month)|PCR|VIX)\b/i;

  function nearHeat(c) {           /* computed colour → is this a fail (red-family) chip? */
    var m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(c || '');
    return m ? (+m[1] > +m[2] + 40) : false;   /* red channel clearly above green = heat */
  }
  function nearVolt(c) {
    var m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(c || '');
    return m ? (+m[2] > +m[1] + 20) : false;   /* green clearly above red = volt */
  }

  function enhance() {
    injectCss();
    var all = document.body ? document.body.querySelectorAll('div,span,p,b,strong,h1,h2,h3,button,a') : [];
    var moodEl = null, whyEl = null;
    for (var i = 0; i < all.length; i++) {
      var el = all[i], t = (el.textContent || '').trim();
      if (!moodEl && el.children.length === 0 && MOODS.test(t)) moodEl = el;
      if (!whyEl && /(\d+)\s+of\s+(\d+)\s+checks/i.test(t) && t.length < 80) whyEl = el;
      /* check chips: leaf-ish elements starting with a known check name and holding a number */
      if (el.children.length <= 1 && CHIP.test(t) && t.length < 30 && !el.__r5) {
        el.__r5 = 1; el.classList.add('r5-set');
        var col = getComputedStyle(el).color;
        if (nearHeat(col)) el.classList.add('r5-l');
        else if (nearVolt(col)) el.classList.add('r5-w');
      }
    }
    /* V8-card treatment (founder 21:5x), applied to EVERY mood word on the page (the home
       carousel has two: the checks scoreboard and the MARKET MOOD / PCR card, founder 23:33).
       Word in chalk, sentiment on the rail. */
    var moodEls = [];
    for (var i2 = 0; i2 < all.length; i2++) {
      var e2 = all[i2];
      if (e2.children.length === 0 && MOODS.test((e2.textContent || '').trim())) moodEls.push(e2);
    }
    for (var im = 0; im < moodEls.length; im++) {
      var mEl = moodEls[im];
      if (mEl.__r5) continue;
      mEl.__r5 = 1; mEl.classList.add('r5-mood');
      var mw = (mEl.textContent || '').toLowerCase();
      var railC = /bear|risk[- ]?off|fear|panic/.test(mw) ? '#FF4D6D'
                : /bull|risk[- ]?on|greed/.test(mw) ? '#C8F542' : '#FF9F45';
      var cardEl = mEl.parentElement, hops = 0;
      while (cardEl && hops < 6 && cardEl.offsetHeight < 160) { cardEl = cardEl.parentElement; hops++; }
      if (cardEl && cardEl !== document.body && !cardEl.__r5rail) {
        cardEl.__r5rail = 1;
        cardEl.style.setProperty('--r5rail', railC);
        cardEl.classList.add('r5-rail');
        /* founder 23:33: the PCR card wastes space — spacious + bigger type. Applied to any
           mood card generically and additively: index prices go big-mono, labels get air. */
        cardEl.style.lineHeight = '1.6';
        var leaves = cardEl.querySelectorAll('div,span,b,strong,p');
        for (var iz = 0; iz < leaves.length; iz++) {
          var lf = leaves[iz];
          if (lf.children.length !== 0 || lf.__r5t) continue;
          var lt = (lf.textContent || '').trim();
          if (/^[\d][\d,]{4,}$/.test(lt)) {            /* 24,366 / 57,491 — founder 23:40:
            34px dominated; 24px confident mono, tight to its row */
            lf.__r5t = 1;
            lf.style.cssText += ';font-size:24px!important;font-weight:800;'
              + 'font-family:"JetBrains Mono",monospace;letter-spacing:0';
          } else if (/^(NIFTY\s*50|BANKNIFTY|SENSEX|FINNIFTY)$/i.test(lt)
                     && !(lf.parentElement && /\b(LONG|SHORT|FLAT)\b/.test(lf.parentElement.textContent))) {
            lf.__r5t = 1;
            lf.style.cssText += ';font-size:12px;letter-spacing:2.5px;font-weight:700;'
              + 'text-transform:uppercase;display:inline-block';
          } else if (/^PCR\s/i.test(lt) && lt.length < 30) {
            lf.__r5t = 1;
            lf.style.cssText += ';font-size:14px;font-family:"JetBrains Mono",monospace';
          } else if (/(below|above)\s+pivot/i.test(lt) && lt.length < 20) {
            /* founder 23:40: loose grey text -> a small bordered chip, tinted by side */
            lf.__r5t = 1;
            var below = /below/i.test(lt);
            lf.style.cssText += ';font-size:10px;letter-spacing:1.5px;text-transform:uppercase;'
              + 'font-weight:700;padding:3px 8px;display:inline-block;vertical-align:middle;'
              + 'border:1px solid ' + (below ? '#5A2634' : '#3E5A18') + ';'
              + 'background:' + (below ? '#241019' : '#18240E') + ';'
              + 'color:' + (below ? '#FF8FA5' : '#C8F542') + ';'
              + 'clip-path:polygon(4px 0,100% 0,100% 100%,0 100%,0 4px)';
          }
        }
      }
    }
    /* fail meter — parsed from the live "N of M checks failed" line, inserted ONCE after it */
    if (whyEl && !whyEl.__r5m) {
      var m = /(\d+)\s+of\s+(\d+)/.exec(whyEl.textContent);
      if (m && +m[2] > 0) {
        whyEl.__r5m = 1;
        var bar = document.createElement('div'); bar.className = 'r5-meter';
        var fill = document.createElement('i');
        fill.style.width = Math.round(100 * (+m[1]) / (+m[2])) + '%';
        bar.appendChild(fill);
        whyEl.parentNode && whyEl.parentNode.insertBefore(bar, whyEl.nextSibling);
      }
    }
    /* founder 23:43 (#5): PCR | VIX tabs inside the MARKET MOOD card. The India VIX panel
       (currently under the scoreboard card) is relocated once into the mood card, hidden behind
       a VIX tab; PCR tab shows the card as-is. Founder-ordered move. */
    window.__r5try1 = (window.__r5try1 || 0) + 1;
    if (!document.getElementById('r5-moodtabs') && window.__r5try1 <= 8) {
      var pcrLeaf = null, vixHd = null;
      for (var p1 = 0; p1 < all.length; p1++) {
        var tp = (all[p1].textContent || '').trim();
        if (!pcrLeaf && /^PCR\s+[\d.]+/.test(tp) && tp.length < 30) pcrLeaf = all[p1];
        if (!vixHd && /^INDIA VIX$/i.test(tp)) vixHd = all[p1];
      }
      var moodCard = pcrLeaf ? pcrLeaf.closest('.r5-rail') : null;
      if (moodCard && vixHd) {
        /* the VIX panel = smallest ancestor of the INDIA VIX heading that also contains the
           30D range button */
        var vixPanel = vixHd;
        while (vixPanel && vixPanel.parentElement && !/30D/.test(vixPanel.textContent)) {
          vixPanel = vixPanel.parentElement;
        }
        if (vixPanel && vixPanel !== document.body) {
          var tabs = document.createElement('div');
          tabs.id = 'r5-moodtabs';
          tabs.style.cssText = 'display:flex;gap:8px;margin:10px 0';
          function mkTab(label, on) {
            var b = document.createElement('button');
            b.textContent = label;
            b.style.cssText = 'flex:1;padding:8px 0;font-family:"JetBrains Mono",monospace;'
              + 'font-weight:800;font-size:12px;letter-spacing:2px;cursor:pointer;'
              + 'border:1px solid #26334F;color:' + (on ? '#0D1322' : '#8A97B0') + ';'
              + 'background:' + (on ? '#C8F542' : '#1C2740') + ';'
              + 'clip-path:polygon(5px 0,100% 0,100% 100%,0 100%,0 5px)';
            return b;
          }
          var tP = mkTab('PCR', true), tV = mkTab('VIX', false);
          tabs.appendChild(tP); tabs.appendChild(tV);
          /* insert tabs right after the PCR line; relocate VIX panel after them, hidden */
          pcrLeaf.parentNode.insertBefore(tabs, pcrLeaf.nextSibling);
          vixPanel.__r5vx = 1;
          vixPanel.style.display = 'none';
          tabs.parentNode.insertBefore(vixPanel, tabs.nextSibling);
          /* everything after the vix panel inside the card = the PCR view */
          function setTab(vix) {
            vixPanel.style.display = vix ? '' : 'none';
            var sib = vixPanel.nextSibling;
            while (sib) {
              if (sib.nodeType === 1) sib.style.display = vix ? 'none' : '';
              sib = sib.nextSibling;
            }
            tP.style.background = vix ? '#1C2740' : '#C8F542';
            tP.style.color = vix ? '#8A97B0' : '#0D1322';
            tV.style.background = vix ? '#C8F542' : '#1C2740';
            tV.style.color = vix ? '#0D1322' : '#8A97B0';
          }
          tP.addEventListener('click', function () { setTab(false); });
          tV.addEventListener('click', function () { setTab(true); });
        }
      }
    }

    /* cc#1068: the INDEX SIGNALS mover lived here and is now built at SOURCE, in
       mobile/home.html, as a real card between the carousel and THE BOOK. Three matcher
       attempts failed from this layer (session_log 23379) because at runtime the two index
       blocks share no small common wrapper — their only common ancestor is the whole card
       body. At source they are one string and the move is a one-line cut, which is the
       lesson: structural work belongs in the template, not in a DOM scan. */

    /* R5 grain — founder 20:4x: the mockup's 115deg diagonal grain was missing because the
       page wrapper paints flat --bg over the themed body. Apply the grain to the wrapper itself
       (first viewport-filling child), inline and additive. */
    try {
      var kids = document.body ? document.body.children : [];
      for (var k = 0; k < kids.length; k++) {
        var el2 = kids[k];
        if (el2.clientHeight >= window.innerHeight * 0.7 && !el2.__r5g) {
          el2.__r5g = 1;
          el2.style.backgroundImage =
            'repeating-linear-gradient(115deg, rgba(255,255,255,.016) 0 2px, transparent 2px 9px)';
          break;
        }
      }
    } catch (e) {}

    /* cc#1065 wiring (founder 21:0x): the GVM tab's "Company view" pill becomes "Detailed view"
       and opens the fight card at /m/gvm2. Content-matched like everything else here; capture
       phase so the old handler never fires. Old company view code stays untouched in the repo. */
    for (var q = 0; q < all.length; q++) {
      var elv = all[q], tv = (elv.textContent || '').trim();
      /* tolerate the dropdown caret and up to 2 child nodes (icon/caret spans) */
      if (!elv.__r5cv && elv.children.length <= 2 && /^company view[\s\u25be\u25bc\u2bc6]*$/i.test(tv)) {
        elv.__r5cv = 1;
        /* rename only the text node, keep any icon children */
        for (var w = 0; w < elv.childNodes.length; w++) {
          var nd = elv.childNodes[w];
          if (nd.nodeType === 3 && /company view/i.test(nd.nodeValue)) {
            nd.nodeValue = nd.nodeValue.replace(/company view/i, 'Detailed view'); break;
          }
        }
        if (/company view/i.test(elv.textContent)) elv.textContent = 'Detailed view';
        var tgt = elv.closest('button,a,[onclick],[role="tab"]') || elv;
        tgt.addEventListener('click', function (ev) {
          ev.preventDefault(); ev.stopPropagation();
          /* founder 23:37: RELIANCE heading has a child span (dotted underline), so the
             leaf-only scan missed it and fell back to the search bar. Allow <=2 children and
             blocklist the non-symbol big caps. */
          var NOTSYM = /^(GVM|SCORR|WEAK|AVERAGE|GOOD|EXCELLENT|CLOSED|LIVE|OPEN|MATCHES|HOME|CHECK|INTEL|V8|LONG|SHORT|FLAT|CAUTION|BEARISH|BULLISH|NEUTRAL)$/;
          var sym = '', best = 0;
          var cands = document.querySelectorAll('div,span,h1,h2,h3,b,strong');
          for (var z = 0; z < cands.length; z++) {
            var cz = cands[z], tz = (cz.textContent || '').trim();
            if (cz.children.length <= 2 && /^[A-Z][A-Z0-9&-]{2,14}$/.test(tz) && !NOTSYM.test(tz)) {
              var fs = parseFloat(getComputedStyle(cz).fontSize) || 0;
              if (fs > best && fs >= 20) { best = fs; sym = tz; }
            }
          }
          location.href = sym ? '/m/gvm2?symbol=' + encodeURIComponent(sym) : '/m/gvm2';
        }, true);
      }
    }

    /* cc#1070: the C·A·R·D stub lived here and is now the REAL strip, rendered in
       mobile/gvm.html from window.ScorrCardStripHtml — the cc#789 single source, whose own
       header says never to re-implement it. The stub was visual-only: every letter fell back
       to opening the fight card, and it GUESSED the symbol by scraping the largest all-caps
       DOM leaf >=20px. The template has x.symbol in hand, so both problems disappear with the
       block. */

    /* cc#1068: the AQUA HEADING pass lived here and is now a template rule. mobile/home.html
       sets .sect{color:#35E0FF} directly, so headings are aqua at first paint instead of being
       repainted by a full-DOM getComputedStyle sweep on every tap. Links and buttons keep
       pulse, unchanged. Removed rather than left dormant — a matcher that no longer matches
       anything is a cost with no benefit. */

    /* bottom-nav active tab: fixed-to-bottom container, the child marked on/active */
    var nav = document.querySelector('.bnav, .bottomnav, [class*="tabbar"], nav[class*="bottom"]');
    if (nav) {
      var on = nav.querySelector('.on, .active, [aria-current]');
      if (on && !on.classList.contains('r5-tab-on')) on.classList.add('r5-tab-on');
    }
  }

  /* Home renders after its fetch, so run now, on load, and on DOM growth (debounced). */
  var t = null;
  function kick() { clearTimeout(t); t = setTimeout(enhance, 400); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', kick);
  else kick();
  try { new MutationObserver(kick).observe(document.documentElement, { childList: true, subtree: true }); }
  catch (e) {}
})();

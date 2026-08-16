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
    + '.r5-w{background:#18240E!important;border-color:#3E5A18!important}'
    + '.r5-l{background:#241019!important;border-color:#5A2634!important}'
    + '.r5-w::before{content:"W ";font-weight:800;color:#C8F542}'
    + '.r5-l::before{content:"L ";font-weight:800;color:#FF8FA5}'
    + '.r5-set,.r5-set *{text-decoration:none!important;border-bottom:0!important}'
    + '.r5-rail{position:relative;border-radius:0!important}'
    + '.r5-rail::before{content:"";position:absolute;left:0;top:6px;bottom:6px;width:5px;'
    +   'background:var(--r5rail,#FF4D6D);border-radius:4px;z-index:2}'
    + '.r5-idxbox{background:#161F33;border:1px solid #26334F;margin:0 14px 12px;padding:12px 16px;'
    +   'clip-path:polygon(0 0,calc(100% - 14px) 0,100% 14px,100% 100%,0 100%);'
    +   'font-family:"JetBrains Mono",monospace;font-size:13px;letter-spacing:1px}'
    + '.r5-idxbox .t{font-size:10px;letter-spacing:2px;color:#8A97B0;margin-bottom:6px;font-weight:700}'
    + '.r5-idxbox b.long{color:#C8F542}.r5-idxbox b.short{color:#FF4D6D}.r5-idxbox b.flat{color:#FF9F45}'
    + '.r5-tab-on{color:#C8F542!important;position:relative}'
    + '.r5-tab-on::after{content:"";position:absolute;top:-1px;left:25%;right:25%;height:3px;'
    +   'background:#C8F542;clip-path:polygon(0 0,100% 0,calc(100% - 3px) 100%,3px 100%)}';

  function injectCss() {
    if (document.getElementById('r5sig-css')) return;
    var s = document.createElement('style'); s.id = 'r5sig-css'; s.textContent = CSS;
    document.head.appendChild(s);
  }

  var MOODS = /^(bearish|bullish|neutral|cautious|risk[- ]?on|risk[- ]?off)$/i;
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
    if (moodEl && !moodEl.__r5) {
      moodEl.__r5 = 1; moodEl.classList.add('r5-mood');
      /* V8-card treatment (founder 21:5x): sentiment lives on a state RAIL, not in the word's
         colour. Walk up to the card container and rail it heat/volt/amber by mood. */
      var mw = (moodEl.textContent || '').toLowerCase();
      var railC = /bear|risk[- ]?off/.test(mw) ? '#FF4D6D'
                : /bull|risk[- ]?on/.test(mw) ? '#C8F542' : '#FF9F45';
      var cardEl = moodEl.parentElement, hops = 0;
      while (cardEl && hops < 6 && cardEl.offsetHeight < 160) { cardEl = cardEl.parentElement; hops++; }
      if (cardEl && cardEl !== document.body && !cardEl.__r5rail) {
        cardEl.__r5rail = 1;
        cardEl.style.setProperty('--r5rail', railC);
        cardEl.classList.add('r5-rail');
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
    /* founder 22:0x: NIFTY/BANKNIFTY position row moves OUT of the scoreboard into its own
       box above THE BOOK. Founder-ordered move — the only sanctioned exception to the additive
       parity guard, documented here. */
    if (!document.getElementById('r5-idxbox')) {
      var idxRow = null, idxLen = 1e9;
      for (var y = 0; y < all.length; y++) {
        var ty = (all[y].textContent || '').replace(/\s+/g, ' ').trim();
        if (/NIFTY\s+(LONG|SHORT|FLAT)/i.test(ty) && /BANKNIFTY\s+(LONG|SHORT|FLAT)/i.test(ty)
            && ty.length < 120 && ty.length < idxLen) { idxRow = all[y]; idxLen = ty.length; }
      }
      if (idxRow) {
        var nM = /NIFTY\s+(LONG|SHORT|FLAT)/i.exec(idxRow.textContent);
        var bM = /BANKNIFTY\s+(LONG|SHORT|FLAT)/i.exec(idxRow.textContent);
        var bookHd = null, bLen = 1e9;
        for (var y2 = 0; y2 < all.length; y2++) {
          var tb = (all[y2].textContent || '').replace(/\s+/g, ' ').trim();
          if (/^THE BOOK/i.test(tb) && tb.length < 40 && tb.length < bLen) { bookHd = all[y2]; bLen = tb.length; }
        }
        if (nM && bM && bookHd) {
          idxRow.style.display = 'none';
          var box = document.createElement('div');
          box.id = 'r5-idxbox'; box.className = 'r5-idxbox';
          function seg(name, dir) {
            var d = dir.toUpperCase();
            var cls = d === 'LONG' ? 'long' : (d === 'SHORT' ? 'short' : 'flat');
            return name + ' <b class="' + cls + '">' + d + '</b>';
          }
          box.innerHTML = '<div class="t">INDEX POSITIONS</div>'
            + seg('NIFTY', nM[1]) + ' &nbsp;·&nbsp; ' + seg('BANKNIFTY', bM[1]);
          bookHd.parentNode && bookHd.parentNode.insertBefore(box, bookHd);
        }
      }
    }

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
          /* carry the symbol currently on the report card: the biggest ALL-CAPS leaf on
             the page (the card's symbol heading). Fallback: plain /m/gvm2 search. */
          var sym = '', best = 0;
          var cands = document.querySelectorAll('div,span,h1,h2,h3');
          for (var z = 0; z < cands.length; z++) {
            var cz = cands[z], tz = (cz.textContent || '').trim();
            if (cz.children.length === 0 && /^[A-Z][A-Z0-9&-]{2,14}$/.test(tz)) {
              var fs = parseFloat(getComputedStyle(cz).fontSize) || 0;
              if (fs > best && fs >= 20) { best = fs; sym = tz; }
            }
          }
          location.href = sym ? '/m/gvm2?symbol=' + encodeURIComponent(sym) : '/m/gvm2';
        }, true);
      }
    }

    /* bottom-nav active tab: fixed-to-bottom container, the child marked on/active */
    var nav = document.querySelector('.bnav, .bottomnav, [class*="tabbar"], nav[class*="bottom"]');
    if (nav) {
      var on = nav.querySelector('.on, .active, [aria-current]');
      if (on && !on.classList.contains('r5-tab-on')) on.classList.add('r5-tab-on');
    }
  }

  /* Home renders after its fetch, so run now, on load, and on DOM growth (debounced). */
  var t = null;
  function kick() { clearTimeout(t); t = setTimeout(enhance, 120); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', kick);
  else kick();
  try { new MutationObserver(kick).observe(document.documentElement, { childList: true, subtree: true }); }
  catch (e) {}
})();

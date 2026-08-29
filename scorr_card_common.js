/* scorr_card_common.js — cc#805 SHARED CARD PRIMITIVES (site-wide, self-contained).
 * ===============================================================================
 * WHY THIS FILE EXISTS — read before touching anything here.
 *
 * cc#803 locked C and R of the C·A·R·D strip to shared components. A (Analysis modal) and D
 * (Derivative Cockpit) could not follow, because both closed over ~33 functions that live in
 * v8_dashboard.html — and a large slice of that closure is PAGE-WIDE utility, not card code:
 *
 *     num  -> 139 call sites in v8_dashboard.html
 *     sign ->  82
 *     getJSON -> 58
 *     newsEsc -> 23
 *     _qaSetBody 7 · _heatTile 6 · _volTilesHtml/_volTile/_qaModal 2-3 each
 *
 * Moving A and D without moving those first would either (a) break 300+ unrelated call sites on the
 * primary trading surface, or (b) duplicate the utilities into the new card files — recreating the
 * exact duplication the extraction exists to kill. So the utility layer is extracted FIRST, into
 * this file, and everything else builds on it.
 *
 * WHAT IS IN HERE
 *   formatting  num · sign · newsEsc
 *   transport   getJSON
 *   volume      _volCol · _volTile · _volTilesHtml · qaVolExplain   (shared by A and D)
 *   heat cells  _heatBg · _heatTile · _deltaGrade · _perfGrade · _perfTile  (shared by A and D)
 *   sparkline   _qaSpark
 *   token       _MONO
 *
 * Every body below was moved VERBATIM out of v8_dashboard.html — same characters, same semantics.
 * Do not "improve" them here: the whole point is that the dashboard's 300+ existing call sites keep
 * behaving identically.
 *
 * HOW CALL SITES KEEP WORKING
 *   Each primitive is published twice:
 *     window.ScorrCardCommon.<name>   canonical namespace — what scorr_analysis_card.js and
 *                                     scorr_cockpit_card.js bind to (immune to a host page
 *                                     redefining a bare global).
 *     window.<name>                   bare global, GUARDED — only set when the name is not already
 *                                     a function on window. That guard is what makes this file safe
 *                                     to inject site-wide: a page that already owns a `num` or a
 *                                     `getJSON` with different semantics keeps its own.
 *
 * LOAD ORDER — NOT OPTIONAL
 *   main.py injects this file FIRST in the shared-JS block, before scorr_card_strip.js and before
 *   the two card files. v8_dashboard.html ALSO carries its own early <script src> tag near the top
 *   of the document. That second tag is deliberate and must not be removed: the site-wide injection
 *   is `defer` AND (for pages with no </head>, which includes v8_dashboard.html) lands at the END of
 *   <body>, so it executes AFTER the dashboard's inline scripts — but the dashboard calls loadAll(),
 *   loadNews(), pollFeedHealth() and loadDataIntegrity() at PARSE TIME, and those reach getJSON /
 *   newsEsc immediately. The early blocking tag is what makes those parse-time calls resolve. The
 *   IIFE below is idempotent, so loading the file twice costs nothing.
 */
(function () {
  if (window.ScorrCardCommon) return;

  /* v8_dashboard.html declares `const API_BASE=""` at page scope and getJSON's body references it.
     The body is kept verbatim, so the constant comes along. */
  var API_BASE = "";

  /* ── design token (was a page-scope const in v8_dashboard.html) ─────────────────────── */
  const _MONO="'IBM Plex Mono',ui-monospace,monospace";

  /* ── transport + formatting ──────────────────────────────────────────────────────────────
   * NOT a naive copy of one variant. v8_dashboard.html carried FOUR `num` and THREE `getJSON`
   * definitions in separate inline-script scopes, and they were not all behaviourally equal. Each
   * name below is the SUPERSET of the variants it replaces — never an invented behaviour, and never
   * the narrowest one. Each was diffed against every variant over the full input matrix
   * (null/undefined/NaN/''/0/false/true/[]/{}/'  '/numeric strings/negative-zero) before collapsing.
   *
   * getJSON — 3 variants, superset adopted on both axes:
   *   v8 global   fetch(API_BASE+path,{Accept:'application/json'})  err `${path} -> HTTP ${status}`
   *   Index Intel fetch(u)  NO Accept header                        err `${u} ${status}`
   *   V14 pane    fetch(u,{Accept:'application/json'})              err `HTTP ${status}`
   *   Adding the Accept header is safe (these are FastAPI JSON routes that ignore it); REMOVING it
   *   would not have been, which is why the header-carrying variant wins. The error text keeps the
   *   path AND the status, so it is a strict information superset of all three — the only place it
   *   surfaces is the V14 failure banner, which now names the failing endpoint instead of a bare
   *   "HTTP 500", and the Index-Intel console.warn.
   *
   * num — 4 variants. All four agree on EVERY input except two, and on both the widest variant
   *   (the BT6 pane's) is the correct one, so its body is what stands here:
   *     num('')      -> the other three returned "0.00", because Number('') === 0. That prints a
   *                     FABRICATED ZERO on a trading surface for a value the API did not send.
   *                     "--" is what an absent value must render as. THIS IS THE ONE DELIBERATE
   *                     OUTPUT CHANGE in cc#805 — an empty string now reads "--" everywhere.
   *     num(v,null)  -> `d=2` default parameters only fire on `undefined`, so an explicit null `d`
   *                     reached (1.5).toFixed(null) === "2". `d==null?2:d` fixes it.
   *
   * sign — 2 variants, PROVEN identical over the same input matrix (`x===null||x===undefined` is
   *   exactly `v==null`), so this one IS verbatim. Note it deliberately does NOT get num's ''-guard:
   *   no existing sign variant had one, and inventing behaviour here is out of scope.
   *
   * newsEsc — single definition, verbatim.
   */
  /* ── cc#878: fetchWithTimeout — THE ONE TIMEOUT, SITE-WIDE ────────────────────────────────
     cc#869 finding 7 / P0-B: 129 of 134 fetch() call sites in the served front end had no
     deadline. A request that never returns leaves a spinner or shimmer animating forever, which
     reads as "still loading" and never as "failed" — so the founder's phone showed a loading
     screen for a hung request instead of an error he could act on. cc#858 established the
     AbortController principle on the R card and it was applied nowhere else.

     This is deliberately a thin wrapper, not a new transport: same signature as fetch, same
     return, same Response. It only guarantees the promise SETTLES. On timeout it rejects with a
     named error so an existing .catch() renders its failed state without any change, and so a
     caller can tell a timeout apart from an HTTP error.

     15s default: comfortably above the slowest measured surface (cc#869 timed the worst query at
     93.7ms and the heaviest page assembly well under a second), and low enough that a human still
     reads it as "that failed" rather than "still going". */
  function fetchWithTimeout(url, opts, ms){
    opts = opts || {};
    var ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var t  = null;
    var weTimedOut = false;   // OUR deadline fired, as opposed to the caller cancelling
    if (ac) {
      /* Respect a signal the caller already passed — abort ours when theirs fires, so a page that
         cancels its own in-flight request keeps working. */
      if (opts.signal && typeof opts.signal.addEventListener === 'function') {
        opts.signal.addEventListener('abort', function(){ try { ac.abort(); } catch(e){} });
      }
      opts = Object.assign({}, opts, { signal: ac.signal });
      t = setTimeout(function(){ weTimedOut = true; try { ac.abort(); } catch(e){} }, ms == null ? 15000 : ms);
    }
    return fetch(url, opts).then(function(r){
      if (t) clearTimeout(t);
      return r;
    }, function(err){
      if (t) clearTimeout(t);
      /* Only OUR deadline becomes a TimeoutError. A caller that aborted its own request
         gets its AbortError back unchanged — mislabelling a deliberate cancel as a timeout
         would send the page into a failed state it never asked for. */
      if (weTimedOut && err && err.name === 'AbortError') {
        var e = new Error('Request timed out after ' + (ms == null ? 15000 : ms) + 'ms: ' + url);
        e.name = 'TimeoutError';
        e.timedOut = true;
        throw e;
      }
      throw err;
    });
  }

  async function getJSON(path){
    /* cc#878: every getJSON caller inherits the deadline for free — this one line covers the 58
       call sites in v8_dashboard.html alone. */
    const r=await fetchWithTimeout(API_BASE+path,{headers:{'Accept':'application/json'}});
    if(!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
    return r.json();
  }
  function num(v,d){if(v==null||v==='')return '--';var n=Number(v);return isNaN(n)?'--':n.toFixed(d==null?2:d);}
  function sign(x,d=2){if(x===null||x===undefined||isNaN(x))return"--";const v=Number(x);return(v>=0?"+":"")+v.toFixed(d);}
  function newsEsc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

  /* ── heat cells — shared by the Analysis trajectory/performance grids AND the cockpit
     5-day-OI grid (verbatim, v8_dashboard.html) ───────────────────────────────────────────── */
  function _heatBg(g){return {g1:'rgba(47,212,139,.10)',g2:'rgba(47,212,139,.16)',g3:'rgba(47,212,139,.22)',r1:'rgba(255,92,108,.10)',r2:'rgba(255,92,108,.16)',r3:'rgba(255,92,108,.22)',n:'var(--surface2)'}[g]||'transparent';}
  function _heatTile(text,grade,col){return `<div style="flex:1;min-width:0;background:${_heatBg(grade)};border-radius:8px;height:46px;display:flex;align-items:center;justify-content:center;font-family:${_MONO};font-weight:700;font-size:13px;color:${col}">${text}</div>`;}
  function _deltaGrade(v){if(v==null)return null;const a=Math.abs(v);if(a<0.05)return'n';return (v>0?'g':'r')+(a<0.25?'1':a<0.6?'2':'3');}
  function _perfGrade(v){if(v==null)return null;const a=Math.abs(v);if(a<0.3)return'n';return (v>0?'g':'r')+(a<3?'1':a<10?'2':'3');}
  function _perfTile(v){const col=v==null?'var(--dim)':v>0?'var(--grn)':v<0?'var(--red)':'var(--mut)';
    return _heatTile(v==null?'--':(v>0?'+':'')+v.toFixed(1)+'%',_perfGrade(v),col);}   // cc#644: shared _heatTile

  /* ── volume tiles — one component behind the Analysis "Volume" section and cockpit
     section 01 (verbatim, v8_dashboard.html) ────────────────────────────────────────────── */
  function _volCol(v){ if(v==null)return 'var(--mut)'; return v>=1.5?'var(--grn)':v>=0.8?'var(--txt)':'#d68a1e'; }
  function _volTile(cap,val,sub,tipKey,badge){
    var col=_volCol(val), vs=(val==null)?'--':(Number(val).toFixed(1)+'&times;');
    return '<button onclick="qaVolExplain(\''+tipKey+'\')" style="flex:1;min-width:0;text-align:left;'
      +'background:var(--surface2);border:1px solid var(--line2);border-radius:8px;padding:10px;cursor:pointer;font:inherit">'
      +'<div style="font-size:10px;color:var(--dim);font-weight:700;display:flex;align-items:center;gap:5px">'+cap+(badge||'')+'</div>'
      +'<div style="font-size:22px;font-weight:800;font-family:\'Sora\',sans-serif;color:'+col+'">'+vs+'</div>'
      +'<div style="font-size:10px;color:var(--dim);margin-top:2px;line-height:1.35">'+sub+'</div></button>';
  }
  function _volTilesHtml(E,M){
    E=E||{}; M=M||{}; var rv=E.rvol||{};
    var rvv=(rv&&rv.rvol!=null)?rv.rvol:null;
    var rvSub = rv&&rv.insufficient?'building profile ('+(rv.sessions_used||0)+' sess)'
              : rv&&rv.closed?('last session \u00b7 '+newsEsc(rv.asof||''))
              : rv&&rv.slot?('vs typical by '+newsEsc(rv.slot)+' IST'):'vs typical pace';
    var rvBadge = rv&&rv.early?'<span style="font-size:8px;font-weight:800;background:var(--amber, #d68a1e);color:#fff;padding:1px 4px;border-radius:4px">EARLY</span>':'';
    return '<div style="display:flex;gap:6px">'
      + _volTile('RVOL',rvv,rvSub,'rvol',rvBadge)
      + _volTile('VolX',E.volx,'last day vs 21d avg'+(E.volx_asof?(' \u00b7 '+newsEsc(E.volx_asof)):''),'volx','')
      + _volTile('Vol 3D/21D',E.recent3d_vol_ratio,newsEsc(M.recent3d_vol_ratio||'3d vs 21d avg'),'vol3d','')
      + '</div><div id="qaVolTip" style="display:none;margin-top:8px;font-size:11px;color:var(--mut);line-height:1.5;background:var(--surface2);border:1px solid var(--line2);border-radius:8px;padding:8px 10px"></div>';
  }
  function qaVolExplain(k){
    var t={rvol:'RVOL = today\u2019s cumulative volume up to this 5-min slot \u00f7 the 21-session average volume by the SAME time of day. 1.0\u00d7 = normal pace; >1 = trading faster than usual. NULL until the profile has \u226510 sessions; first two slots badged EARLY.',
           volx:'VolX = the latest session\u2019s total volume \u00f7 its 21-day average. Whole-day relative volume.',
           vol3d:'Vol 3D/21D = 3-day average volume \u00f7 21-day average volume. Participation trend \u2014 rising vs drying up.'}[k]||'';
    var el=document.getElementById('qaVolTip'); if(el){el.innerHTML=t; el.style.display='block';}
  }

  /* ── 30d delivery sparkline (verbatim, v8_dashboard.html) ──────────────────────────────── */
  function _qaSpark(series){
    const pts=(series||[]).map(p=>p.deliv_pct).filter(v=>v!=null);
    if(pts.length<2)return '';
    const w=280,H=34,lo=Math.min.apply(null,pts),hi=Math.max.apply(null,pts),rng=(hi-lo)||1;
    const xs=i=>i/(pts.length-1)*w, ys=v=>H-3-((v-lo)/rng)*(H-8);
    const line=pts.map((v,i)=>xs(i).toFixed(1)+','+ys(v).toFixed(1)).join(' ');
    return `<svg viewBox="0 0 ${w} ${H}" preserveAspectRatio="none" style="width:100%;height:${H}px;margin-top:8px;display:block"><polyline points="${line}" fill="none" stroke="var(--cyan,#3aa0ff)" stroke-width="1.5"/></svg>`;
  }

  /* ── publication ────────────────────────────────────────────────────────────────────────
   * Namespace first (always authoritative), then the guarded bare globals. */
  /* ── cc#878: THE BACKSTOP — a timeout must never leave a shimmer running ───────────────────
     Twelve call sites across the app are plain `.then()` chains with no `.catch()`. Before this
     card they hung forever; with a deadline they now REJECT, and an unhandled rejection means the
     `.then()` never runs — so whatever "Loading..." markup is sitting in the container stays on
     screen. That is the same symptom by a different route, and patching twelve sites by hand would
     miss the thirteenth someone writes next month.

     So the failed state is handled once, globally, and only for OUR timeouts (err.timedOut). A
     genuine HTTP error or a page's own AbortError is left completely alone — those already have
     owners, and hijacking them would stamp "failed to load" over states that are handled properly.

     It replaces every still-visible loading placeholder with a retry-able line. Retry is a reload
     rather than a re-issue of the one request, because from here we cannot know which of several
     in-flight calls fed which container — and a reload is honest and always correct. */
  var LOADING_SELECTORS = '.loading, .sg-loading, .bt-loading, .dc-skel, .smc-skel';

  function _renderTimeoutFailure(err) {
    var nodes;
    try { nodes = document.querySelectorAll(LOADING_SELECTORS); } catch (e) { return; }
    if (!nodes || !nodes.length) return;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.getAttribute('data-scorr-failed') === '1') continue;   // don't stack messages
      n.setAttribute('data-scorr-failed', '1');
      n.innerHTML =
        '<div style="padding:10px 12px;border:1px solid var(--line2,#2a3548);border-radius:8px;' +
        'font-size:12px;color:var(--dim,#7c8aa5);display:flex;align-items:center;gap:10px;' +
        'flex-wrap:wrap">' +
          '<span>Failed to load &mdash; the request timed out.</span>' +
          '<button type="button" onclick="location.reload()" style="padding:5px 12px;border-radius:6px;' +
          'border:1px solid var(--line2,#2a3548);background:transparent;color:inherit;cursor:pointer;' +
          'font:inherit;min-height:32px">Retry</button>' +
        '</div>';
    }
    try { console.error('[cc#878] request timed out:', err && err.message); } catch (e) {}
  }

  if (typeof window.addEventListener === 'function' && !window.__scorrTimeoutBackstop) {
    window.__scorrTimeoutBackstop = true;
    window.addEventListener('unhandledrejection', function (ev) {
      var err = ev && ev.reason;
      if (!err || !err.timedOut) return;    // ours only — never touch someone else's rejection
      _renderTimeoutFailure(err);
    });
  }

  /* ── cc#880: BASKET IDENTITY — ONE MAP, ONE DERIVATION ────────────────────────────────────
     Card item 4 asks for the slug -> human name mapping to exist in exactly one place, because
     two copies of a label map is two labels for the same basket the day someone edits one. Before
     this card there were two: v8_dashboard.html said "Buy S1 Bounce" and mobile_endpoints.py said
     "S1 Bounce" for the SAME slug. This map is now the canonical JS answer; the dashboard's own
     basketLabel() delegates here, so both surfaces read one dictionary.

     The Python side (mobile_endpoints.BASKET_LABELS) still exists because a server cannot call a
     browser file — it is aligned to this map by hand and both carry a note saying so. Two
     runtimes is the honest floor; two DIFFERENT answers is not.

     Card item 2 is the other half: the chip LIST is derived from the DATA, never from a hardcoded
     array. scorrBasketFacets does that derivation once, so no surface can quietly reintroduce a
     fixed list of five. A retired basket with historical rows still gets a chip because it is IN
     the rows; a basket with zero rows gets none because it is not. */
  var SCORR_BASKET_LABELS = {
    buy_reversal: 'Buy Reversal',
    buy_momentum: 'Buy Momentum',
    sell_reversal: 'Sell Reversal',
    sell_momentum: 'Sell Momentum',
    buy_s1_bounce: 'S1 Bounce',
    sell_overbought: 'Sell Overbought'
  };
  /* Canonical display order. A slug not listed here sorts last rather than being dropped — an
     unnamed basket is a naming gap, not a reason to hide real trades. */
  var SCORR_BASKET_ORDER = ['buy_reversal', 'buy_momentum', 'buy_s1_bounce',
                            'sell_reversal', 'sell_momentum', 'sell_overbought'];

  function scorrBasketLabel(slug) {
    if (!slug) return '--';
    return SCORR_BASKET_LABELS[slug] || String(slug).replace(/_/g, ' ');
  }

  /* Derive the chip list from a row set. Returns [{slug, label, n}] in canonical order.
     `pick` reads the basket off a row (defaults to row.basket). Rows with no basket are ignored
     rather than bucketed under '' — a blank chip filters to nothing and helps no one. */
  function scorrBasketFacets(rows, pick) {
    var get = pick || function (r) { return r && r.basket; };
    var counts = {};
    (rows || []).forEach(function (r) {
      var b = get(r);
      if (!b) return;
      counts[b] = (counts[b] || 0) + 1;
    });
    return Object.keys(counts).map(function (b) {
      return { slug: b, label: scorrBasketLabel(b), n: counts[b] };
    }).sort(function (a, b) {
      var ia = SCORR_BASKET_ORDER.indexOf(a.slug), ib = SCORR_BASKET_ORDER.indexOf(b.slug);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.slug.localeCompare(b.slug);
    });
  }

  var API = {
    num: num, sign: sign, getJSON: getJSON, newsEsc: newsEsc,
    fetchWithTimeout: fetchWithTimeout,   // cc#878
    scorrBasketLabel: scorrBasketLabel, scorrBasketFacets: scorrBasketFacets,   // cc#880
    _volCol: _volCol, _volTile: _volTile, _volTilesHtml: _volTilesHtml, qaVolExplain: qaVolExplain,
    _heatBg: _heatBg, _heatTile: _heatTile, _deltaGrade: _deltaGrade,
    _perfGrade: _perfGrade, _perfTile: _perfTile, _qaSpark: _qaSpark,
    _MONO: _MONO
  };
  window.ScorrCardCommon = API;

  /* Backward compatibility for every existing call site (and for the inline onclick= handlers the
     moved markup emits, e.g. qaVolExplain). NEVER clobber a name a host page already defines. */
  Object.keys(API).forEach(function (k) {
    if (typeof API[k] !== 'function') return;
    if (typeof window[k] === 'function') return;
    window[k] = API[k];
  });
  if (typeof window._MONO === 'undefined') window._MONO = _MONO;
  /* cc#880: the label MAP itself is published too, not just the accessor — a surface that needs to
     know the full set (rather than translate one slug) should read this rather than write its own. */
  if (typeof window.SCORR_BASKET_LABELS === 'undefined') window.SCORR_BASKET_LABELS = SCORR_BASKET_LABELS;
  API.SCORR_BASKET_LABELS = SCORR_BASKET_LABELS;
  API.SCORR_BASKET_ORDER = SCORR_BASKET_ORDER;
})();

/* ── FIVE-SLOT MOBILE NAV (cc#897; was six) — ONE SOURCE (founder 08-Aug: "show V8 in nav, it is the most
   important model"). Appended by Fable (ROLE_CHARTER_V3, session_log 17868).
   Every /m/* template carries a five-slot .bnav in its own markup; editing thirteen files for
   every nav change is the NAV CHURN trap. So the nav is now REBUILT here, in the one script
   every screen already loads — this block is the single owner of the mobile nav from today.
   Template navs remain as no-JS fallback only. Web pages are untouched: the rebuild only runs
   when a .bnav element exists AND the path is /m/*. */
(function () {
  if (window.__scorrNavOwner) return;
  window.__scorrNavOwner = true;
  /* cc#897 (founder 08-Aug): FIVE slots — Models drops out. Its children all have their own
     doorway on the Home Tools grid now, so the sixth slot was spending permanent nav real estate
     on a hub nobody needs to pass through. */
  var NAV = [
    { href: '/m/home',   icon: '\u25e7', label: 'Home' },
    { href: '/m/v8',     icon: '\u25b3', label: 'V8' },
    { href: '/m/gvm',    icon: '\u25c8', label: 'GVM' },
    { href: '/m/check',  icon: '\u25ce', label: 'Check' },
    { href: '/m/intel',  icon: '\u25a4', label: 'Intel' }
  ];
  /* deeper screens highlight their parent slot. cc#897: the three that pointed at /m/models were
     REPOINTED to /m/home — a parent that is no longer a slot highlights nothing, which reads as a
     broken nav rather than a deep screen. /m/models itself joins them for the same reason. */
  var PARENT = { '/m/positions': '/m/v8', '/m/qb': '/m/v8', '/m/results': '/m/v8',
                 '/m/holdings': '/m/home', '/m/screeners': '/m/home',
                 '/m/sector': '/m/home', '/m/fpc': '/m/home', '/m/digest': '/m/home',
                 '/m/models': '/m/home' };
  function build() {
    try {
      var nav = document.querySelector('.bnav');
      if (!nav) return;
      var p = location.pathname;
      if (p.indexOf('/m/') !== 0) return;
      var on = PARENT[p] || p;
      nav.innerHTML = NAV.map(function (s) {
        return '<a class="bn' + (s.href === on ? ' on' : '') + '" href="' + s.href + '">' +
               '<span class="i">' + s.icon + '</span>' + s.label + '</a>';
      }).join('');
    } catch (e) { /* nav fallback in the template stays */ }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();

/* ── cc#909: LOGO MENU — the two floating pills, moved where they belong (founder 08-Aug) ─────
   main.py's auth_gate injects #scorr-lo (Logout) and #scorr-th (theme) on every authenticated
   page as fixed top-right pills at 45% opacity. On a phone they hover over the content. The
   mobile stylesheet hides them on /m/* (they are untouched on web), and this block hangs the
   same two actions off a tap on the Scorr logo.

   ROUTE TAKEN: CSS hide + this handler, i.e. option (a) in the card — main.py is NOT touched,
   so the auth gate and the web pills keep working exactly as they did.

   THE THEME CONTRACT IS COPIED, NOT REINVENTED. Read from main.py's _THEME_BTN first, as the
   card required: the key is localStorage 'scorr_theme', the values are 'light' | 'dark', the
   default when unset is 'light', and the flip ENDS IN location.reload() — that reload is the
   mechanism, not a nicety, because _MOBILE_HEAD applies the saved theme synchronously before
   first paint and the older hardcoded pages only re-render on a load. Same key, same values,
   same reload. Anything else here would desync mobile from web.

   Web pages are untouched: everything below returns immediately unless the path is /m/*. */
(function () {
  if (window.__scorrLogoMenu) return;
  window.__scorrLogoMenu = true;

  function curTheme() {
    try { return localStorage.getItem('scorr_theme') || 'light'; } catch (e) { return 'light'; }
  }
  function flipTheme() {
    var t = curTheme() === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('scorr_theme', t); } catch (e) { /* private mode: still reload */ }
    location.reload();
  }

  function build() {
    try {
      if (location.pathname.indexOf('/m/') !== 0) return;
      var h1 = document.querySelector('.head h1');
      if (!h1 || !h1.parentNode) return;

      var dark = curTheme() === 'dark';
      var menu = document.createElement('div');
      menu.className = 'lgm';
      /* the label is the ACTION, not the current state — in a menu "Light" reads as a command
         and would flip the opposite way to what the founder taps for. The pill shows state
         because it is a toggle; a menu row should say what it will do. */
      menu.innerHTML =
        '<button type="button" id="lgm-th"><span class="ic">' + (dark ? '☀' : '☾')
        + '</span>Switch to ' + (dark ? 'light' : 'dark') + '</button>'
        + '<button type="button" id="lgm-lo"><span class="ic">⏏</span>Log out</button>';
      h1.classList.add('lgt');
      h1.parentNode.appendChild(menu);

      h1.addEventListener('click', function (e) {
        e.stopPropagation();
        menu.classList.toggle('open');
      });
      menu.addEventListener('click', function (e) { e.stopPropagation(); });
      // tap anywhere else (or press Escape) closes it
      document.addEventListener('click', function () { menu.classList.remove('open'); });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') menu.classList.remove('open');
      });

      var th = document.getElementById('lgm-th');
      if (th) th.onclick = flipTheme;
      var lo = document.getElementById('lgm-lo');
      if (lo) lo.onclick = function () { location.href = '/logout'; };
    } catch (e) { /* a missing header must never break the screen under it */ }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();

/* ── cc#898: UNIVERSAL SYMBOL TAP -> C·A·R·D SHEET (founder 08-Aug) ───────────────────────────
   Every symbol shown anywhere in the mobile app becomes tappable and opens the shared card
   strip. Done ONCE here, as a delegated listener, rather than by editing nine templates: a
   screen added tomorrow inherits this with no code, and there is no second copy to drift.

   HOW SYMBOLS ARE FOUND. The mobile screens do not share one symbol class — a survey of
   mobile/*.html found .sym (home), .sy (positions/qb/holdings/results), .sy2 (v8),
   .rs and .rsym (gvm), .vsym (check). All of them are matched, plus an explicit data-sym for
   anything new. The symbol is data-sym when present, else the element's trimmed text, and it
   must look like a symbol (uppercase, 1-20 chars, NSE punctuation only) — so a stray class
   match can never open a card for a heading or a number.

   THE STRIP IS CONSUMED, NEVER RE-IMPLEMENTED — window.ScorrCardStripHtml(sym) from
   scorr_card_strip.js (cc#789/#803/#805: all four letters are locked to shared components).
   No C/A/R/D markup is written here.

   TWO EXCLUSIONS THAT MATTER:
     1. .scorr-card-strip carries its OWN data-sym attribute. Without excluding it, tapping the
        strip would match this listener and reopen the sheet on top of itself.
     2. [data-scorr-skip] opts a surface out. Home's My Portfolio rows use it: cc#911 gives those
        rows an INLINE strip under the row, which is the founder-specified behaviour there, and
        two mechanisms firing on one tap is worse than either.
   A handled tap calls preventDefault + stopPropagation, so a symbol sitting inside a row link
   opens its card instead of navigating away — which is the point of the card.

   Web pages are untouched: everything returns immediately unless the path is /m/*. */
(function () {
  if (window.__scorrSymCard) return;
  window.__scorrSymCard = true;

  var SEL = '[data-sym],.sym,.sy,.sy2,.rs,.rsym,.vsym';
  var OK = /^[A-Z0-9][A-Z0-9&._\-]{0,19}$/;

  function up(el, test) {          // closest(), without depending on it
    while (el && el.nodeType === 1) { if (test(el)) return el; el = el.parentNode; }
    return null;
  }
  function matches(el, sel) {
    var f = el.matches || el.msMatchesSelector || el.webkitMatchesSelector;
    try { return !!f && f.call(el, sel); } catch (e) { return false; }
  }
  function symOf(el) {
    var s = el.getAttribute('data-sym');
    if (!s) s = (el.textContent || '').trim();
    s = s.toUpperCase();
    return OK.test(s) ? s : null;
  }

  function sheet() {
    var el = document.getElementById('scorr-symsheet');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'scorr-symsheet';
    el.className = 'symsheet';
    el.innerHTML = '<div class="symsheet-bd" role="dialog" aria-modal="true">'
      + '<div class="symsheet-hd"><b id="scorr-symsheet-t"></b>'
      + '<button type="button" class="symsheet-x" aria-label="Close">&#10005;</button></div>'
      + '<div class="symsheet-strip" id="scorr-symsheet-s"></div></div>';
    document.body.appendChild(el);
    el.addEventListener('click', function (e) {
      // backdrop tap closes; a tap inside the panel does not, unless it is the close button
      if (e.target === el || matches(e.target, '.symsheet-x')) { close(); return; }
      /* THE SHEET IS A LAUNCHER — picking a letter dismisses it.
         This is also load-bearing after cc#917. That card restyled this sheet and raised it to
         z-index 100000; every letter card sits far below (chart 12000, analysis 11400, cockpit
         11000, results 99999) and all of them are position:fixed children of <body>, so they
         share this stacking context and compare directly. ScorrCardNav's _closeAll() closes
         R/C/A/D but not this sheet — ScorrSymbolCard did not exist when that was written — so
         without this the card would open UNDERNEATH the sheet and the tap would look like it did
         nothing. Closing here fixes it whatever z-index the design lands on later.
         Bubble phase, and deferred a tick, so the button's own inline ScorrCardNav() has already
         run before the strip leaves the DOM. Only an enabled letter is a <button>; a disabled or
         active one is a <span> and dispatches nothing, so it must not dismiss the sheet. */
      if (e.target && e.target.tagName === 'BUTTON'
          && up(e.target, function (n) { return matches(n, '.scorr-card-strip'); })) {
        setTimeout(close, 0);
      }
    });
    return el;
  }
  function close() {
    var el = document.getElementById('scorr-symsheet');
    if (el) el.classList.remove('open');
    document.body.style.overflow = '';
  }
  function open(sym) {
    var el = sheet();
    document.getElementById('scorr-symsheet-t').textContent = sym;
    var host = document.getElementById('scorr-symsheet-s');
    var html = '';
    try {
      if (typeof window.ScorrCardStripHtml === 'function') html = window.ScorrCardStripHtml(sym, '');
    } catch (e) { html = ''; }
    // never a dead sheet: if the shared strip is missing, say so rather than opening empty
    host.innerHTML = html || '<span class="sum">Card view unavailable</span>';
    el.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  window.ScorrSymbolCard = { open: open, close: close };

  function onTap(e) {
    try {
      if (location.pathname.indexOf('/m/') !== 0) return;
      var el = up(e.target, function (n) { return matches(n, SEL); });
      if (!el) return;
      if (up(el, function (n) { return matches(n, '.scorr-card-strip,[data-scorr-skip]'); })) return;
      var sym = symOf(el);
      if (!sym) return;
      e.preventDefault();
      e.stopPropagation();
      open(sym);
    } catch (err) { /* a tap must never throw into the page */ }
  }

  function build() {
    if (location.pathname.indexOf('/m/') !== 0) return;
    document.addEventListener('click', onTap, true);   // capture, so a row's own handler cannot eat it
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();

/* ── cc#917: MOBILE CARD SHEET DESIGN + UNIFORM DISMISS (founder 08-Aug, Fable) ─────────────
   Two founder complaints on the /m/* card popup: (1) a tap OUTSIDE must close it; (2) the
   letter cards are web modals squeezed onto a phone. Fixed here, presentation-only:

   DISMISS — one rule for every layer. Each card component already owns a close(); what was
   missing is a uniform backdrop contract. On /m/*, a click that lands on a full-screen
   overlay element ITSELF (not on the dialog inside it) closes every known card component.
   Escape does the same. Component logic untouched — we only CALL the close() each shared
   file already exposes (the same set scorr_card_strip.js _closeAll uses).

   DESIGN — bottom sheets. At <=767px on /m/*, every card dialog ([role="dialog"] inside a
   fixed overlay) renders as a slide-up sheet: full width, rounded top, drag notch, 90vh cap,
   safe-area padding, 44px close targets. Pure CSS scoped by a body class this block adds on
   /m/* only — web pages never get the class, so /dashboard is pixel-identical. The letter
   cards' DOM and behaviour are consumed as-is (cc#803/805 lock respected). */
(function () {
  if (window.__scorrCardSheet) return;
  window.__scorrCardSheet = true;
  if (location.pathname.indexOf('/m/') !== 0) return;

  /* scope hook: CSS below only applies under body.mcards, which only /m/* gets */
  function addClass() { try { document.body.classList.add('mcards'); } catch (e) {} }

  var CSS = ''
    /* every card dialog becomes a bottom sheet on phone */
    + '@media(max-width:767px){'
    + 'body.mcards .rcard-ov, body.mcards .scorr-ml-ov{align-items:flex-end!important;padding:0!important}'
    + 'body.mcards .rcard-ov [role="dialog"], body.mcards .rcard,'
    + 'body.mcards .scorr-ml{'
    + '  position:relative;left:auto;top:auto;transform:none!important;'
    + '  width:100%!important;max-width:100%!important;margin:0!important;'
    + '  max-height:90vh!important;overflow-y:auto;-webkit-overflow-scrolling:touch;'
    + '  border-radius:18px 18px 0 0!important;border-bottom:none!important;'
    + '  padding-bottom:calc(16px + env(safe-area-inset-bottom,0px))!important;'
    + '  animation:sheetUp .22s ease}'
    /* drag notch */
    + 'body.mcards .rcard:before, body.mcards .scorr-ml:before{content:"";display:block;'
    + '  width:38px;height:4px;border-radius:2px;background:var(--line2,rgba(148,166,210,.35));'
    + '  margin:2px auto 10px}'
    /* close buttons reach thumb size */
    + 'body.mcards .rcard-x, body.mcards .scorr-ml-x, body.mcards .symsheet-x{'
    + '  min-width:44px;min-height:44px}'
    /* section labels breathe a little more on phone */
    + 'body.mcards .rcard-lbl{margin-top:20px;padding-top:14px}'
    + '@keyframes sheetUp{from{transform:translateY(40px);opacity:.6}to{transform:none;opacity:1}}'
    /* the symbol sheet itself (cc#898) gets the same finish */
    + 'body.mcards .symsheet{position:fixed;inset:0;z-index:100000;display:none;'
    + '  background:rgba(5,9,18,.55);align-items:flex-end}'
    + 'body.mcards .symsheet.open{display:flex}'
    + 'body.mcards .symsheet-bd{width:100%;background:var(--panel,#0E1526);'
    + '  border:1px solid var(--line,#1E2A44);border-bottom:none;border-radius:18px 18px 0 0;'
    + '  padding:10px 16px calc(18px + env(safe-area-inset-bottom,0px));animation:sheetUp .22s ease}'
    + 'body.mcards .symsheet-bd:before{content:"";display:block;width:38px;height:4px;'
    + '  border-radius:2px;background:var(--line2,rgba(148,166,210,.35));margin:0 auto 10px}'
    + 'body.mcards .symsheet-hd{display:flex;align-items:center;justify-content:space-between;'
    + '  margin-bottom:10px}'
    + 'body.mcards .symsheet-hd b{font:800 16px Sora,sans-serif;letter-spacing:.01em}'
    + 'body.mcards .symsheet-x{background:none;border:none;color:var(--dim,#5E6B8F);'
    + '  font-size:18px;cursor:pointer}'
    + 'body.mcards .symsheet-strip{display:flex;justify-content:center;padding:4px 0 2px}'
    + 'body.mcards .symsheet-strip .scorr-cs-b{width:52px;height:52px;border-radius:12px;'
    + '  font-size:17px;margin:0 5px}'
    /* ── cc#918: the three overlays cc#917 could not verify at push time ──────────────────────
       C = #scorrChartOv / #scorrChartBoxWrap, A = #scorrAnaOv / .sa-box, D = #dcOv / .dc-sheet.
       Selectors read out of the three files, not assumed.
       WHY !important, verified per target rather than sprinkled: the CHART overlay and its wrap
       are styled by INLINE cssText (position/align-items/padding on the overlay; width/max-height/
       border-radius on the wrap), and inline beats any selector, so those genuinely need it. The
       analysis and cockpit rules are stylesheet rules at (1,1,0) which body.mcards … out-specifies
       at (1,2,0) — !important is kept there only so all six read the same way. */
    + 'body.mcards #scorrChartOv, body.mcards #scorrAnaOv, body.mcards #dcOv{'
    + '  align-items:flex-end!important;justify-content:center!important;padding:0!important}'
    + 'body.mcards #scorrChartBoxWrap, body.mcards #scorrAnaOv .sa-box, body.mcards #dcOv .dc-sheet{'
    + '  width:100%!important;max-width:100%!important;margin:0!important;'
    + '  max-height:90vh!important;border-radius:18px 18px 0 0!important;'
    + '  animation:sheetUp .22s ease}'
    /* safe-area padding and the drag notch go ONLY on the two boxes that scroll their own content.
       #scorrChartBoxWrap is a flex column with overflow:hidden holding a fixed-height chart — a
       pseudo-element would eat flex height and bottom padding would clip the canvas, so it gets
       the sheet shape without either. */
    + 'body.mcards #scorrAnaOv .sa-box, body.mcards #dcOv .dc-sheet{'
    + '  padding-bottom:calc(16px + env(safe-area-inset-bottom,0px))!important}'
    + 'body.mcards #scorrAnaOv .sa-box:before, body.mcards #dcOv .dc-sheet:before{content:"";'
    + '  display:block;width:38px;height:4px;border-radius:2px;'
    + '  background:var(--line2,rgba(148,166,210,.35));margin:8px auto 2px}'
    /* thumb-size close targets. #scorrAnaX carries inline width/height:28px, so it needs the
       override; #scorrChartClose has no inline box at all and only needs the size. */
    + 'body.mcards #scorrChartClose, body.mcards #scorrAnaX{width:44px!important;height:44px!important;'
    + '  min-width:44px!important;min-height:44px!important;font-size:18px!important}'
    + 'body.mcards #dcOv .back{min-height:44px;display:flex;align-items:center}'

    /* ══════════════════════════════════════════════════════════════════════════════════════
       cc#946 — THE CONTENT INSIDE THE FOUR LETTERS, re-laid for a phone.
       cc#917/918 fixed the SHELLS: every letter became a bottom sheet with a drag notch and a
       44px close. What they never touched — cc#914 item 3 deferred it here on purpose — is the
       BODY of each card, which is still laid out at desktop scale inside a 360px sheet.
       CONTENT PARITY IS ABSOLUTE: nothing below hides a field, drops a column or changes a
       number. Every rule re-flows or re-scales what the web already shows, in the web's order.
       SCOPE: every selector begins `body.mcards` and every one of them sits inside the
       @media(max-width:767px) block this string opens with. body.mcards is added only on /m/*
       (see addClass above), so the website is untouched by construction, not by inspection —
       the same guarantee cc#918 relied on, and it is re-verified programmatically before push.
       !important appears only where the target carries an INLINE style, because inline beats
       any selector. Where the target is a stylesheet rule, it is left off.
       ══════════════════════════════════════════════════════════════════════════════════════ */

    /* ── C · chart card ──────────────────────────────────────────────────────────────────
       The peer table is emitted with an inline `min-width:520px` inside an overflow-x:auto
       wrapper. On a 360px sheet that is a guaranteed sideways scroll on the FIRST thing the
       founder sees, before any deliberate choice to scroll. The min-width goes; the wrapper
       keeps its scroller, so a genuinely wide row can still be dragged — that is the
       "intentional table scroller" the card allows, rather than one forced on every symbol.
       The Symbol column is left to take the slack and the numeric columns tighten, because a
       truncated ticker is unreadable while a tight percentage is not. */
    /* ── cc#956 + cc#957 · PEERS TAB — RESTACKED, NOT SHRUNK ────────────────────────────────
       cc#946 tried to make seven columns fit 360px by tightening them. The founder's 09-Aug
       screenshot shows why that was the wrong answer: MONTH and GVM collided into "MONTHGVM",
       value pairs fused with no gap ("+10.51%+16.98%"), the Watch chip clipped under the CARD
       button, and "South West Pinnacle Exploration Ltd" ran under the next line's percentages.
       Seven columns do not fit; the fix is to RESTACK the row, not to pad it.

       Each peer becomes TWO LINES:
         line 1   symbol (+ THIS) on the left · GVM bold and the band chip on the right
         line 2   D · W · M, each with a dim label above a mono value
       cc#957 removes two things from that: the COMPANY NAME (not required, and it was the string
       that ran under the next line) and the CARD BUTTON — tapping the row opens the peer's card.

       NO JAVASCRIPT CHANGED, and that is worth stating because cc#957 asked for a data-sym
       attribute: the peer <tr> ALREADY carries data-sym, so cc#898's delegated symbol-tap listener
       already matches the whole row. The CARD button was in fact already dead on /m/* — cc#898
       listens in the CAPTURE phase and calls stopPropagation, so the button's own handler never
       ran on a phone. Hiding it removes an affordance that already did nothing rather than taking
       a working one away, and the tap path is the same one every symbol in the app uses.
       scorr_chart_card.js is untouched (cc#803/805 lock), desktop keeps all seven columns. */
    + 'body.mcards #scorrPeerPane table{min-width:0!important;width:100%!important;display:block}'
    + 'body.mcards #scorrPeerPane thead{display:none}'            /* cc#956 item 2 */
    + 'body.mcards #scorrPeerPane tbody{display:block}'
    + 'body.mcards #scorrPeerPane tbody tr[data-sym]{display:grid;'
    + '  grid-template-columns:1fr auto auto;column-gap:10px;row-gap:3px;align-items:center;'
    + '  min-height:48px;padding:8px 2px;border-bottom:1px solid var(--line,#1E2740);cursor:pointer}'
    + 'body.mcards #scorrPeerPane tbody tr[data-sym] > td{border:0!important;padding:0!important;'
    + '  white-space:nowrap;font-size:11px!important;display:block}'
    /* line 1 — symbol left, GVM and band right */
    + 'body.mcards #scorrPeerPane td:nth-child(1){grid-column:1;grid-row:1;text-align:left!important;'
    + '  min-width:0;overflow:hidden}'
    + 'body.mcards #scorrPeerPane td:nth-child(5){grid-column:2;grid-row:1;text-align:right!important;'
    + '  font-size:12.5px!important}'
    + 'body.mcards #scorrPeerPane td:nth-child(6){grid-column:3;grid-row:1;text-align:right!important}'
    /* cc#957: the company name and the CARD button both go.
       !important here is EARNED, not sprinkled: the `tr[data-sym] > td` rule above sets
       display:block at (0,2,3) and out-specifies a plain `#id td:nth-child(7)` at (0,1,2), so
       without it the CARD column stayed on screen — the first measurement caught exactly that. */
    + 'body.mcards #scorrPeerPane td:nth-child(1) > div:nth-child(2){display:none!important}'
    + 'body.mcards #scorrPeerPane tbody tr[data-sym] > td:nth-child(7){display:none!important}'
    /* cc#957 item 3: the tap has to be discoverable now that the button is gone — the cc#898
       dotted-underline convention, on the symbol text itself. */
    + 'body.mcards #scorrPeerPane td:nth-child(1) > div:first-child{'
    + '  font-size:13px!important;text-decoration:underline dotted;text-underline-offset:3px;'
    + '  text-decoration-color:var(--line2,rgba(148,166,210,.45))}'
    /* line 2 — D · W · M with labels. The 10px column-gap above is what stops two values fusing;
       a label carries the column meaning per row, which is what lets the header row go. */
    + 'body.mcards #scorrPeerPane td:nth-child(2){grid-column:1;grid-row:2;text-align:left!important}'
    + 'body.mcards #scorrPeerPane td:nth-child(3){grid-column:2;grid-row:2}'
    + 'body.mcards #scorrPeerPane td:nth-child(4){grid-column:3;grid-row:2}'
    + 'body.mcards #scorrPeerPane td:nth-child(2):before{content:"D "}'
    + 'body.mcards #scorrPeerPane td:nth-child(3):before{content:"W "}'
    + 'body.mcards #scorrPeerPane td:nth-child(4):before{content:"M "}'
    + 'body.mcards #scorrPeerPane td:nth-child(2):before,'
    + 'body.mcards #scorrPeerPane td:nth-child(3):before,'
    + 'body.mcards #scorrPeerPane td:nth-child(4):before{'
    + '  font-size:8.5px;font-weight:800;letter-spacing:.06em;color:var(--dim,#5E6B8F)}'
    /* the inline drawer row keeps working, full width under its peer */
    + 'body.mcards #scorrPeerPane tbody tr[data-drawer]{display:block}'
    + 'body.mcards #scorrPeerPane tr[data-drawer] > td{display:block;width:100%;border:0!important}'
    /* the range/timeframe pills wrap instead of pushing the head sideways, and stay thumb-sized */
    + 'body.mcards #scorrChartTfs, body.mcards #scorrChartTabs{flex-wrap:wrap;gap:5px;row-gap:6px}'
    + 'body.mcards #scorrChartTfs button, body.mcards #scorrChartTabs button{'
    + '  min-height:34px!important;padding:6px 10px!important}'
    + 'body.mcards #scorrChartHead{flex-wrap:wrap;row-gap:8px}'
    /* the canvas must never be wider than the sheet, whatever the library sizes it to */
    + 'body.mcards #scorrChartBox, body.mcards #scorrChartBox canvas,'
    + 'body.mcards #scorrChartBox > div{max-width:100%!important}'
    + 'body.mcards #scorrChartBoxWrap{overflow-x:hidden!important}'
    + 'body.mcards #scorrChartTitle{font-size:15px!important}'

    /* ── cc#947 items 2-4 · the C sheet, from the founder's 09-Aug screenshot ────────────────
       ITEM 2 — THE PILLS WERE STACKING VERTICALLY. #scorrChartTfs and #scorrChartTabs are laid
       out by INLINE cssText in _buildModal, and cc#918 turned their container into a narrow
       flex column sheet; a flex child with no explicit direction inherited the column and every
       pill dropped onto its own line down the middle of the sheet. Forced back to a row here.
       It SCROLLS rather than wraps: 5m/1M/3M/6M/1Y/3Y/ALL plus Pivots/Fib/GVM is ten controls,
       and wrapping them eats three lines of the height item 4 is trying to give the chart.
       A one-line row you can flick is the smaller cost, and the scrollbar is hidden so it reads
       as a strip, not a scroller.
       ITEM 3 — HEADER ON ONE LINE. The head row wraps to at most two lines at 360px: title and
       price stay together and grow to fill, the verdict chip / fullscreen / close sit at the end.
       ITEM 4 — THE CHART OWNS THE HEIGHT. With the head and pills compact, the box is given the
       remaining sheet height via flex rather than a fixed pixel count, with a floor so it can
       never be squeezed to nothing by a long header. */
    + 'body.mcards #scorrChartTfs, body.mcards #scorrChartTabs{'
    + '  display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;'
    + '  align-items:center!important;overflow-x:auto;-webkit-overflow-scrolling:touch;'
    + '  scrollbar-width:none;padding:6px 12px!important;gap:5px!important}'
    + 'body.mcards #scorrChartTfs::-webkit-scrollbar, body.mcards #scorrChartTabs::-webkit-scrollbar{'
    + '  display:none}'
    + 'body.mcards #scorrChartTfs > *, body.mcards #scorrChartTabs > *{flex:0 0 auto!important}'
    + 'body.mcards #scorrChartTfs button, body.mcards #scorrChartTabs button{'
    + '  min-height:32px!important;padding:6px 9px!important;font-size:11px!important;'
    + '  white-space:nowrap!important}'
    /* header: one row, wrapping to two at most, nothing floating mid-sheet */
    + 'body.mcards #scorrChartHead{display:flex!important;flex-direction:row!important;'
    + '  flex-wrap:wrap!important;align-items:center!important;gap:6px!important;'
    + '  padding:10px 12px 6px!important}'
    + 'body.mcards #scorrChartTitle{flex:1 1 auto!important;min-width:0;white-space:nowrap;'
    + '  overflow:hidden;text-overflow:ellipsis}'
    + 'body.mcards #scorrChartHL{font-size:10.5px!important;white-space:nowrap}'
    + 'body.mcards #scorrChartVerdictChip{flex:0 0 auto!important}'
    /* the chart takes what is left of the sheet */
    + 'body.mcards #scorrChartBoxWrap{display:flex!important;flex-direction:column!important;'
    + '  height:90vh!important}'
    + 'body.mcards #scorrChartBox{flex:1 1 auto!important;min-height:280px!important;'
    + '  display:flex;flex-direction:column}'
    + 'body.mcards #scorrChartBox > div:first-child{flex:1 1 auto!important;min-height:0}'
    + 'body.mcards #scorrChartMsg{padding:0 12px}'
    + 'body.mcards #scorrPeerPane{flex:1 1 auto;overflow-y:auto;min-height:0}'

    /* ── A · analysis card ───────────────────────────────────────────────────────────────
       The trajectory and performance grids are flex rows whose label column is an inline
       `width:86px;min-width:86px`. At 360px, minus the sheet's 20px side padding, that leaves
       ~210px for four value tiles — they fit, but only just, and the numerals inside them are
       web-sized. Narrowing the label to 68px and stepping the tiles down buys back real room
       without moving a single value to a second line. Values right-align so a column of
       numbers reads down the edge rather than ragged. */
    + 'body.mcards #scorrAnaOv .sa-body{padding:14px 14px 26px!important}'
    + 'body.mcards #scorrAnaOv .sa-head{padding:14px 14px 12px!important}'
    + 'body.mcards #scorrAnaOv .sa-body div[style*="width:86px"]{'
    + '  width:68px!important;min-width:68px!important}'
    + 'body.mcards #scorrAnaOv .sa-body{font-size:12px}'
    + 'body.mcards #scorrAnaOv .sa-body table{width:100%;table-layout:fixed}'
    + 'body.mcards #scorrAnaOv .sa-body td, body.mcards #scorrAnaOv .sa-body th{'
    + '  font-size:11px!important;padding:6px 4px!important}'
    + 'body.mcards #scorrAnaOv .sa-body td+td, body.mcards #scorrAnaOv .sa-body th+th{'
    + '  text-align:right!important}'
    /* a numeral must never break across lines — a price split over two rows is unreadable */
    + 'body.mcards #scorrAnaOv .sa-body .mono, body.mcards #scorrAnaOv .sa-body td{'
    + '  white-space:nowrap}'
    + 'body.mcards #scorrAnaOv .qa-tcrow{flex-direction:column;gap:8px}'
    + 'body.mcards #scorrAnaOv .qa-tcbtn{width:100%;min-height:44px}'
    + 'body.mcards #scorrAnaOv #scorrAnaTitle{font-size:15px!important}'

    /* ── R · results card ────────────────────────────────────────────────────────────────
       The peer-results table is six columns — Peer, Result date, Sales YoY, PAT YoY, Margin vs
       LY, Move % — and six columns do not fit 360px at any honest font size. Restacking it into
       label:value rows would cost the reader the comparison it exists for, so this is the case
       where the card's alternative is right: a compact scroller with a STICKY first column, so
       the peer name stays anchored while the numbers slide under it. The peer name column
       therefore also gets the panel background — without it the sticky cell would show the
       scrolled numbers through itself. */
    + 'body.mcards .rcard-peerres-tbl{overflow-x:auto;-webkit-overflow-scrolling:touch}'
    + 'body.mcards .rcard-pr-table{min-width:520px;border-collapse:separate;border-spacing:0}'
    + 'body.mcards .rcard-pr-table th, body.mcards .rcard-pr-table td{'
    + '  font-size:11px;padding:6px 8px;white-space:nowrap}'
    + 'body.mcards .rcard-pr-table th:first-child, body.mcards .rcard-pr-nm{'
    + '  position:sticky;left:0;z-index:1;background:var(--panel,#0E1526);'
    + '  max-width:104px;overflow:hidden;text-overflow:ellipsis}'
    /* the rest of the R body is label:value rows already — they only need the phone type scale */
    + 'body.mcards .rcard-body{padding:14px 14px 22px}'
    /* cc#1429: RESULT ANALYSIS block only (the 4-line Sales/PAT/Margins/PE metric header, .rcard-ra
       marks that one rcard-body instance — see pwa_endpoints.py) — +2px on the base .rcard-body's
       13px, mobile only. do_not_touch: l1Html/auto_verdict (.rcard-l1*), peerHtml, v2Html are
       untouched; this selector matches none of them. */
    + 'body.mcards .rcard-body.rcard-ra{font-size:15px}'
    + 'body.mcards .rcard-l1row, body.mcards .rcard-exp-row, body.mcards .rcard-peer-row{'
    + '  gap:8px}'
    + 'body.mcards .rcard-l1v, body.mcards .rcard-exp-v, body.mcards .rcard-peer-v,'
    + 'body.mcards .rcard-fy27-v{text-align:right;white-space:nowrap}'
    + 'body.mcards .rcard-fy27{grid-template-columns:1fr 1fr}'
    + 'body.mcards .rcard-btn, body.mcards .rcard-detbtn, body.mcards .rcard-peerres-btn,'
    + 'body.mcards .rcard-viewmore, body.mcards .rcard-v2-more{min-height:44px}'
    + 'body.mcards .rcard-sym{font-size:15px}'

    /* ── D · cockpit card ────────────────────────────────────────────────────────────────
       This is the widest gap between web and phone of the four. The volume tile numeral is 40px
       and the symbol heading 26px — sized for a centred desktop modal, not a 360px sheet — and
       two of the three grids put a pair of boxes side by side, which at this width leaves each
       box too narrow for the numeral inside it. The two-up grids go single-column and the type
       scale steps down. Nothing is removed: the same tiles, in the same order, stacked. */
    + 'body.mcards #dcOv .sec, body.mcards #dcOv .hdr{padding-left:14px;padding-right:14px}'
    + 'body.mcards #dcOv .sym .l h1{font-size:21px}'
    + 'body.mcards #dcOv .sym .r .px{font-size:18px}'
    + 'body.mcards #dcOv .sym .r .chg{font-size:12px}'
    + 'body.mcards #dcOv .volgrid{grid-template-columns:1fr;gap:8px}'
    + 'body.mcards #dcOv .oiwrap{flex-direction:column;gap:8px}'
    + 'body.mcards #dcOv .vtile{padding:12px}'
    + 'body.mcards #dcOv .vtile .big{font-size:30px}'
    + 'body.mcards #dcOv .vtile.sm .big{font-size:24px}'
    + 'body.mcards #dcOv .vtile small{font-size:14px}'
    + 'body.mcards #dcOv .basis .v{font-size:19px}'
    + 'body.mcards #dcOv .quad .v{font-size:17px}'
    /* the OI strip is a real table of numbers — keep it a table, give it a scroller only if it
       genuinely overflows, and stop any numeral wrapping mid-value */
    + 'body.mcards #dcOv .strip td, body.mcards #dcOv .strip th{'
    + '  font-size:12px;padding:7px 0;white-space:nowrap}'
    + 'body.mcards #dcOv .strip th:first-child, body.mcards #dcOv .strip td:first-child{'
    + '  max-width:96px;overflow:hidden;text-overflow:ellipsis}'
    + 'body.mcards #dcOv .dc-chip, body.mcards #dcOv button{min-height:44px}'

    /* ── cc#963/964: APP CARD CHROME LEAKING INTO THE SHEETS ────────────────────────────────
       mobile_app.css owns two GENERIC one-letter card classes:
         .v {background:var(--panel2);border:1px solid var(--line2);border-radius:15px;
             padding:15px 15px 12px 19px;margin-bottom:8px;overflow:hidden;position:relative;
             cursor:pointer}
         .m {background:var(--panel);border:1px solid var(--line);border-radius:14px;
             padding:13px 13px 13px 17px;margin-bottom:9px;overflow:hidden;position:relative}
       The cockpit uses .v and .m as PLAIN VALUE LINES inside its own boxes (.quad .v is the
       verdict word, .quad .m is the OI/Px stat line). The card's own rules are id-scoped and win
       on the properties they set — but they never set background, border, padding or margin, so
       every one of those app declarations applied unopposed. MEASURED on the founder's SHORT
       BUILDUP case at 360px: .quad .v rendered 47px tall for one 17px line and .quad .m 60px tall
       for one 10px line, inflating the quadrant box to 179px against a 94px sibling. That is the
       "excessive empty padding" in the 13:57 screenshot — it was never the cockpit's own padding.
       Reset only what the app ADDED. margin-TOP is deliberately untouched: .quad .v and .quad .m
       set it themselves for their internal rhythm, and this selector out-specifies them. */
    + 'body.mcards #dcOv .v, body.mcards #dcOv .m,'
    + 'body.mcards #scorrAnaOv .v, body.mcards #scorrAnaOv .m{'
    + '  background:none;border:0;border-radius:0;padding:0;margin-bottom:0;min-height:0;'
    + '  overflow:visible;position:static;cursor:inherit}'
    /* .m is the MODELS card in the app sheet, and it paints a 3px status rail through ::before.
       Inside a cockpit quadrant that rail is a stray coloured bar with no meaning attached. */
    + 'body.mcards #dcOv .v:before, body.mcards #dcOv .m:before,'
    + 'body.mcards #scorrAnaOv .v:before, body.mcards #scorrAnaOv .m:before{content:none}'

    /* ── cc#964 item 1: C·A·R·D strip letters ───────────────────────────────────────────────
       The founder's "C, A, R are invisible glyphs" was the token bug fixed by the :root bridge —
       .scorr-cs-b sets color:var(--txt,#1c2536), --txt was out of scope on a body-level overlay,
       so the property was invalid and the letter INHERITED the dark ambient colour. With the
       bridge those letters now measure 13.7:1. This rule is the second half of the card: on
       mobile the not-current letters read as the MUT token, so the active blue letter is the one
       that stands out, and the unavailable state gets a legibility floor instead of dim x .42
       (which composited to ~1.6:1 on the panel — a glyph you cannot see is not a disabled
       control, it is a missing one). Mobile-scoped: the web strip is untouched. */
    + 'body.mcards .scorr-cs-b{color:var(--mut)}'
    + 'body.mcards .scorr-cs-on{color:#fff}'
    + 'body.mcards .scorr-cs-off{color:var(--mut);opacity:.75;'
    + '  background:var(--surface2);border-style:dashed}'

    /* ── cc#965 item 1: the C card's own header failed the audit at 360px ───────────────────
       #scorrChartHead is one nowrap flex row built for a 640px modal: title · H/L · verdict ·
       SEVEN timeframe pills · maximize · close. On a 360px phone it wrapped to THREE rows and
       measured: the pills took a full 334px row, and the two buttons dropped onto a third row at
       the LEFT — the close X sat at x=38 with 278px of empty space to its right, i.e. bottom-left
       of the header, nowhere near where a close control is looked for. The maximize button, which
       cc#959 just made the fullscreen-landscape trigger, measured 11x15px against a 44px minimum.
       Both buttons are pinned to the top-right corner here and given real targets; the pills keep
       their own row. Presentation only, mobile only — the 640px web modal is untouched. */
    /* !important on the padding only: _buildModal writes padding:12px 16px as INLINE cssText, and
       inline beats any stylesheet rule — without this the H/L line runs straight under the two
       pinned buttons (measured: HL ended at x=335 while the maximize button started at x=261). */
    + 'body.mcards #scorrChartHead{position:relative;flex-wrap:wrap;padding-right:100px !important}'
    + 'body.mcards #scorrChartTfs{flex-basis:100%;justify-content:flex-start}'
    + 'body.mcards #scorrChartFull, body.mcards #scorrChartClose{'
    + '  position:absolute;top:8px;width:44px;height:44px;padding:0;'
    + '  display:inline-flex;align-items:center;justify-content:center}'
    + 'body.mcards #scorrChartClose{right:8px}'
    + 'body.mcards #scorrChartFull{right:54px}'
    + '}';

  function inject() {
    addClass();
    try {
      var st = document.createElement('style');
      st.setAttribute('data-scorr', 'card-sheet');
      st.appendChild(document.createTextNode(CSS));
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  /* ── uniform dismiss ── */
  function closeAll() {
    try { if (window.ScorrSymbolCard) window.ScorrSymbolCard.close(); } catch (e) {}
    try { if (window.ScorrRCard && window.ScorrRCard.close) window.ScorrRCard.close(); } catch (e) {}
    try { if (window.ScorrChartCard && window.ScorrChartCard.close) window.ScorrChartCard.close(); } catch (e) {}
    try { if (window.ScorrAnalysisCard && window.ScorrAnalysisCard.close) window.ScorrAnalysisCard.close(); } catch (e) {}
    try { if (window.ScorrCockpitCard && window.ScorrCockpitCard.close) window.ScorrCockpitCard.close(); } catch (e) {}
    try { if (window.ScorrModels && window.ScorrModels.close) window.ScorrModels.close(); } catch (e) {}
  }
  function isBackdrop(el) {
    /* the click landed on the overlay ELEMENT itself (not the dialog inside it):
       fixed, spans the viewport, and hosts a dialog child */
    try {
      if (!el || el.nodeType !== 1 || el === document.body) return false;
      var cs = getComputedStyle(el);
      if (cs.position !== 'fixed') return false;
      var r = el.getBoundingClientRect();
      if (r.width < window.innerWidth * 0.9 || r.height < window.innerHeight * 0.9) return false;
      /* cc#918: none of the chart/analysis/cockpit inner boxes carry role="dialog" (checked all
         three files), so the uniform backdrop contract skipped them. Each of those components
         does self-close on its own backdrop click, so tap-outside was never broken — but Escape
         and closeAll() only reach them once they are recognised here, so the list is extended. */
      return !!el.querySelector('[role="dialog"],.rcard,.scorr-ml,.symsheet-bd,'
        + '#scorrChartBoxWrap,.sa-box,.dc-sheet');
    } catch (e) { return false; }
  }
  function onTap(e) {
    if (isBackdrop(e.target)) { closeAll(); }
  }

  /* ══ cc#965 item 2/3 — HARDWARE BACK CLOSES THE SHEET, IT DOES NOT LEAVE THE PAGE ══════════
     cc#917/918 wired backdrop-tap and Escape into closeAll() but never touched popstate, so on
     Android the back gesture popped REAL history and navigated away with the sheet still on
     screen. This is the one implementation for every sheet — no per-card handler, which is the
     whole point: the cards do not agree on class names, ids or open() signatures, but they all
     end up as a fixed full-viewport overlay hosting a dialog, and isBackdrop() above already
     knows how to recognise exactly that. So OPENNESS IS OBSERVED, not reported: a MutationObserver
     watches for the class/style/DOM changes the cards make when they open, and the same predicate
     that powers tap-outside decides whether a sheet is up. A card added later is covered for free.

     THE HISTORY CONTRACT — "open + close + back leaves the page exactly once":
       open  (0 -> n sheets)  pushState a marker.            history: [page, marker]
       close by X/backdrop/Esc (n -> 0)  the marker is still ours, so consume it with a
                              SUPPRESSED history.back().     history: [page]
       hardware back with a sheet open   the browser already consumed the marker, so we only
                              closeAll() and stay put.       history: [page]
       hardware back with nothing open   we never interfere.  normal navigation
     _marked stops a second push while a sheet is already up (opening C then A must not stack two
     entries); _suppress stops our own consuming back() from being read as a user press, which
     would otherwise close-and-navigate in one gesture. */
  var _marked = false, _suppress = false;
  function anyOpen() {
    try {
      var n = document.body ? document.body.children : [];
      for (var i = 0; i < n.length; i++) {
        var el = n[i];
        if (el.nodeType !== 1) continue;
        var cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position !== 'fixed') continue;
        if (isBackdrop(el)) return true;
      }
    } catch (e) {}
    return false;
  }
  function syncHistory() {
    var open = anyOpen();
    if (open && !_marked) {
      try { history.pushState({ scorrSheet: 1 }, '', location.href); _marked = true; } catch (e) {}
    } else if (!open && _marked) {
      /* the sheet closed by X / backdrop / Escape and OUR marker is still the top entry — take it
         back off, or every open/close pair would leave a ghost the user has to press back through. */
      _marked = false; _suppress = true;
      try { history.back(); } catch (e) { _suppress = false; }
    }
  }
  function onPop() {
    if (_suppress) { _suppress = false; return; }   // our own consuming back(), not a user press
    if (!_marked) return;                            // nothing of ours on the stack: let it navigate
    _marked = false;                                 // the browser just consumed our marker
    if (anyOpen()) closeAll();
  }

  function boot() {
    inject();
    document.addEventListener('click', onTap, true);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeAll(); });
    /* cc#965: openness is OBSERVED. Cards signal "open" in different ways (a class on the overlay,
       a style flip, or appending the overlay for the first time), so watch all three. */
    try {
      var mo = new MutationObserver(function () { syncHistory(); });
      mo.observe(document.body, { childList: true, subtree: false, attributes: true,
                                  attributeFilter: ['class', 'style'] });
      /* the overlays themselves are body children, but a card may flip a class on a node INSIDE
         its overlay; a cheap second observer on the whole tree would be wasteful, so the card
         close/open paths are also caught by the click/keydown handlers below calling sync. */
      document.addEventListener('click', function () { setTimeout(syncHistory, 0); }, true);
      window.addEventListener('popstate', onPop);
    } catch (e) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

/* ── cc#926: SCROLL-AWARE TOP NAV (founder 08-Aug) ────────────────────────────────────────────
   Scrolling DOWN gives the content the nav's space; scrolling UP, or reaching the top, brings it
   straight back. One shared listener for the whole site, because this file is the only script both
   the web pages and the /m/* screens load.

   WHAT IT ACTUALLY APPLIES TO, checked rather than assumed:
     .scorr-cnav  the compacted top nav pwa.js builds into #scorr-nav  (position:sticky, top:0)
     .model-nav   the canonical model nav on /dashboard                (position:sticky, top:0)
   The /dashboard TAB ROW is not sticky (only table headers are), so it scrolls away on its own and
   needs nothing. The /m/* page headers are not sticky either — .head is position:relative and
   .headx has no position at all — so there is nothing on mobile to hide, and I have not made
   anything sticky in order to then hide it.

   WHAT IS DELIBERATELY EXCLUDED:
     .bnav      the mobile bottom nav — position:fixed at the BOTTOM and must ALWAYS be visible.
                Excluded twice over: it is not in the selector list, and the geometry guard below
                only ever adopts an element sitting in the top quarter of the viewport, so a
                bottom bar could not be picked up even if a selector one day matched it.
     .navwrap   the /m/v8 grouping + filter bar (cc#922). It is sticky, but it is a control the
                user is actively working, not a nav — hiding it mid-scroll would fight them.

   MECHANICS per the card: passive listener, rAF-throttled so it does at most one write per frame,
   an 8px delta threshold so a slow or trembling scroll cannot flicker, transform only (never
   display:none, so nothing reflows and no layout jumps), and under prefers-reduced-motion the
   BEHAVIOUR stays while the animation is dropped. */
(function () {
  if (window.__scorrScrollNav) return;
  window.__scorrScrollNav = true;

  var SEL = '.scorr-cnav,.model-nav';
  var THRESH = 8;             // px of travel before we act at all
  var els = [], lastY = 0, ticking = false, hidden = false;

  function css() {
    try {
      var st = document.createElement('style');
      st.setAttribute('data-scorr', 'scroll-nav');
      st.appendChild(document.createTextNode(
        '.scorr-nav-auto{transition:transform .2s ease;will-change:transform}' +
        '.scorr-nav-auto.scorr-nav-off{transform:translateY(-100%)}' +
        '@media(prefers-reduced-motion:reduce){.scorr-nav-auto{transition:none}}'
      ));
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  /* pwa.js builds its nav after DOMContentLoaded, so the set is refreshed rather than read once. */
  function collect() {
    var found = [];
    try {
      Array.prototype.forEach.call(document.querySelectorAll(SEL), function (el) {
        var cs = getComputedStyle(el);
        if (cs.position !== 'sticky' && cs.position !== 'fixed') return;
        // geometry guard: only ever a TOP bar, so a bottom-fixed nav can never be adopted
        var r = el.getBoundingClientRect();
        if (r.top > window.innerHeight * 0.25) return;
        el.classList.add('scorr-nav-auto');
        found.push(el);
      });
    } catch (e) {}
    els = found;
    return els.length;
  }

  function apply(hide) {
    if (hide === hidden) return;                 // one write per state change, not per frame
    hidden = hide;
    for (var i = 0; i < els.length; i++) {
      els[i].classList[hide ? 'add' : 'remove']('scorr-nav-off');
    }
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      ticking = false;
      if (!els.length && !collect()) { lastY = window.pageYOffset || 0; return; }
      var y = window.pageYOffset || document.documentElement.scrollTop || 0;
      var dy = y - lastY;
      if (y <= 0) { lastY = y; apply(false); return; }          // at the top: always visible
      if (Math.abs(dy) < THRESH) return;                        // ignore jitter, and do NOT move lastY
      lastY = y;
      // never hide while still inside the nav's own height — it would vanish the instant you nudge
      apply(dy > 0 && y > (els[0] ? els[0].offsetHeight : 44));
    });
  }

  function boot() {
    css();
    collect();
    lastY = window.pageYOffset || 0;
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', function () { collect(); }, { passive: true });
    setTimeout(collect, 600);      // pwa.js may still be building #scorr-nav at this point
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* ── cc#933: MARKER LEGEND — ONE snippet, both surfaces ───────────────────────────────────────
   /dashboard and /m/v8 both render markers beside a symbol, and a reader needs the key. Built
   here, in the file both surfaces already load, so the wording cannot fork (DISPLAY_PARITY
   16202). The COPY itself is served by the engine (/api/v8/pivot_star -> legend[]), so the rule
   text and the code that applies the rule ship together; this function only lays it out. The
   fallback below is used when the endpoint is unreachable, and says the same thing. */
(function () {
  if (window.ScorrMarkerLegend) return;
  /* cc#1018 (21764), updated cc#1024 MARKER_GLYPH_V5 (22296): kept word-for-word in step with the
     legend[] served by v8_pivot_star.py. The server copy is the source; this only stands in when
     the endpoint is unreachable, and it must not say something the markers on screen no longer do.
     The shape-by-side line is gone because the rule is gone — no marker changes shape by side. */
  var FALLBACK = [
    'Amber star = Trade Check STRONG. VALID shows no marker.',
    'Blue star = held reversal at S1',
    'Red star = mirror at R1',
    '⚡ = Volume/OI spurt · volume >1.5x or OI >25% day-over-day'
  ];
  function esc(t){ return String(t == null ? '' : t)
    .replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  window.ScorrMarkerLegend = function (lines) {
    var L = (lines && lines.length) ? lines : FALLBACK;
    return '<div class="mk-legend">' + L.map(function (t) {
      return '<div>' + esc(t) + '</div>';
    }).join('') + '</div>';
  };
})();

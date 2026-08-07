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

  /* ── transport + formatting ────────────────────────────────────────────────────────────────
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
     5-day-OI grid (verbatim, v8_dashboard.html) ─────────────────────────────────────────── */
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
              : rv&&rv.closed?('last session · '+newsEsc(rv.asof||''))
              : rv&&rv.slot?('vs typical by '+newsEsc(rv.slot)+' IST'):'vs typical pace';
    var rvBadge = rv&&rv.early?'<span style="font-size:8px;font-weight:800;background:#d68a1e;color:#fff;padding:1px 4px;border-radius:4px">EARLY</span>':'';
    return '<div style="display:flex;gap:6px">'
      + _volTile('RVOL',rvv,rvSub,'rvol',rvBadge)
      + _volTile('VolX',E.volx,'last day vs 21d avg'+(E.volx_asof?(' · '+newsEsc(E.volx_asof)):''),'volx','')
      + _volTile('Vol 3D/21D',E.recent3d_vol_ratio,newsEsc(M.recent3d_vol_ratio||'3d vs 21d avg'),'vol3d','')
      + '</div><div id="qaVolTip" style="display:none;margin-top:8px;font-size:11px;color:var(--mut);line-height:1.5;background:var(--surface2);border:1px solid var(--line2);border-radius:8px;padding:8px 10px"></div>';
  }
  function qaVolExplain(k){
    var t={rvol:'RVOL = today’s cumulative volume up to this 5-min slot ÷ the 21-session average volume by the SAME time of day. 1.0× = normal pace; >1 = trading faster than usual. NULL until the profile has ≥10 sessions; first two slots badged EARLY.',
           volx:'VolX = the latest session’s total volume ÷ its 21-day average. Whole-day relative volume.',
           vol3d:'Vol 3D/21D = 3-day average volume ÷ 21-day average volume. Participation trend — rising vs drying up.'}[k]||'';
    var el=document.getElementById('qaVolTip'); if(el){el.innerHTML=t; el.style.display='block';}
  }

  /* ── 30d delivery sparkline (verbatim, v8_dashboard.html) ──────────────────────────── */
  function _qaSpark(series){
    const pts=(series||[]).map(p=>p.deliv_pct).filter(v=>v!=null);
    if(pts.length<2)return '';
    const w=280,H=34,lo=Math.min.apply(null,pts),hi=Math.max.apply(null,pts),rng=(hi-lo)||1;
    const xs=i=>i/(pts.length-1)*w, ys=v=>H-3-((v-lo)/rng)*(H-8);
    const line=pts.map((v,i)=>xs(i).toFixed(1)+','+ys(v).toFixed(1)).join(' ');
    return `<svg viewBox="0 0 ${w} ${H}" preserveAspectRatio="none" style="width:100%;height:${H}px;margin-top:8px;display:block"><polyline points="${line}" fill="none" stroke="var(--cyan,#3aa0ff)" stroke-width="1.5"/></svg>`;
  }

  /* ── publication ──────────────────────────────────────────────────────────────────────────
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

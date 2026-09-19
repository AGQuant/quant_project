/* scorr_analysis_card.js — cc#805 SHARED "A" (Analysis) card, site-wide, self-contained.
 * ==================================================================================
 * The canonical Analysis modal: GVM + sector rating/rank, Trajectory grid, Volume (RVOL/VOL P/VOL TREND),
 * Delivery (+ 30d sparkline), Performance heat grid, and the TC Trade/Investment Check footer.
 *
 * Until cc#805 this lived inside v8_dashboard.html, so scorr_card_strip.js could only offer A as a
 * deep-link (`window.open('/dashboard?qa=' + sym)`) — every non-V8 surface opened a NEW TAB to reach
 * one modal. This file is now the ONE implementation; the strip's A letter is LOCKED to it (cc#805,
 * same treatment cc#803 gave C and R).
 *
 * API:  window.ScorrAnalysisCard.open(symbol)
 *       window.ScorrAnalysisCard.close()
 *
 * cc#2055 SCORR_SHARED_CARD_THEME_LOCK_V1 audit note: qaAnalysis(sym) takes no theme/opts
 * parameter at all and never has -- this card's markup is styled entirely off CSS custom
 * properties inherited from the host page (var(--panel,#fff), var(--txt,#1c2536), var(--line,...)),
 * not a JS-computed palette. There is no override mechanism to lock here; confirmed by audit, not
 * assumed. See scorr_chart_card.js for the sibling that DOES carry the lock.
 *
 * DEPENDENCIES — scorr_card_common.js MUST load first (main.py injects it ahead of this file). Every
 * primitive is bound from window.ScorrCardCommon below rather than read off the bare globals, so a
 * host page that owns its own `num`/`getJSON`/`newsEsc` cannot change what this card renders.
 *
 * HOST MODAL. The bodies below are verbatim from v8_dashboard.html, including _qaModal's use of the
 * dashboard's #infoModal / #mTitle / #mSub / #mBody / #mDcBtn shell. The ONE deliberate change is
 * that _qaModal now RESOLVES its host: it reuses #infoModal when the page has one (so on
 * /dashboard the modal, its close button, its sticky header and its C·A·R·D strip slot behave
 * exactly as before), and builds an equivalent self-owned shell (#scorrAnaOv) when it does not.
 * Without that, the card would render into nothing on every page except the dashboard.
 *
 * BACKWARD COMPATIBILITY. qaAnalysis / _qaModal / _qaSetBody / trajHtml / heatHtml / qaTcFooterHtml /
 * qaTcEmbed are also republished as GUARDED bare globals, because (a) v8_dashboard.html's qaNews and
 * qaGvm still drive the same shell through _qaModal/_qaSetBody, and (b) the TC footer emits inline
 * onclick="qaTcEmbed(...)" markup, which can only resolve against a global.
 */
(function () {
  if (window.ScorrAnalysisCard) return;

  var CC = window.ScorrCardCommon;
  if (!CC) { try { console.warn('[ScorrAnalysisCard] scorr_card_common.js did not load first — A card disabled.'); } catch (e) {} return; }

  /* Bind the shared primitives to the SAME local names the moved bodies use, so every body below
     stays character-for-character what it was in v8_dashboard.html. */
  var getJSON = CC.getJSON, newsEsc = CC.newsEsc, _MONO = CC._MONO,
      _heatTile = CC._heatTile, _deltaGrade = CC._deltaGrade, _perfTile = CC._perfTile,
      _volTilesHtml = CC._volTilesHtml, _qaSpark = CC._qaSpark;

  /* ── CSS ───────────────────────────────────────────────────────────────────────────────────
   * The TC footer rules moved out of v8_dashboard.html's _qaInjectStyle() (they were only injected
   * when the quick-action strip had been opened at least once — a latent gap on this page too).
   * The #scorrAnaOv rules mirror the dashboard's .modal-overlay/.modal-box/.modal-head/.modal-body
   * so the self-owned shell looks identical on pages that have no #infoModal. */
  var CSS = ''
    + '.qa-tcrow{display:flex;gap:8px}'
    + '.qa-tcbtn{flex:1;min-width:0;padding:11px 10px;border-radius:9px;border:1px solid var(--line2,#e8ecf2);'
    + 'background:var(--surface2,#f6f8fb);color:var(--txt,#1c2536);font-weight:800;font-size:12.5px;cursor:pointer;'
    + 'white-space:nowrap;transition:background .12s,color .12s,border-color .12s}'
    + '.qa-tcbtn:hover{border-color:var(--blu,#4d7cfe)}'
    + '.qa-tcbtn.trade.on{background:var(--grn,#0a9e63);color:#fff;border-color:var(--grn,#0a9e63)}'
    + '.qa-tcbtn.invest.on{background:var(--pulse, #7c3aed);color:#fff;border-color:var(--pulse, #7c3aed)}'
    /* cc#1976: the third footer button. Modifier `.flags`, the same family as .trade/.invest; the
       active fill is the markers' own neutral blue (the colour ScorrMarkerFlagColor uses when no
       side-coloured marker leads), so the button reads as the flag family, not as a fourth palette. */
    + '.qa-tcbtn.flags.on{background:var(--blu,#4d7cfe);color:#fff;border-color:var(--blu,#4d7cfe)}'
    + '.qa-flags-hd{font-size:11px;color:var(--mut,#5a6b82);margin:0 0 8px}'
    + '@media(max-width:560px){.qa-tcrow{flex-direction:column}}'
    + '#scorrAnaOv{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:11400;align-items:center;justify-content:center}'
    + '#scorrAnaOv.open{display:flex}'
    /* cc#1835: base text colour, so every bare/unstyled element inside this card (e.g. qaAnalysis's
       Segment/Sector-rating <b> values, which set no colour of their own) inherits var(--txt) —
       the SAME token cc#1828 already bridged correctly for this shell — instead of falling through
       to mobile_endpoints.py's global `html,body{color:var(--chalk,#E9EEFB)}` rule. --chalk is
       only ever bridged inside #gvp/#ckp/#v8p (theme_mobile.css), never at body scope, so outside
       those three subtrees it is undefined and that rule''s literal #E9EEFB fallback wins — a
       near-white, dark-theme-tuned default that reads correctly on nothing built after it (this
       card''s own explicit var(--mut)/var(--dim) usages were already fine; the regression was
       specifically the values with NO colour declared, which is why cc#1828''s fix to the token
       BRIDGE did not catch it — those elements never read a token here at all). */
    + '#scorrAnaOv .sa-box{background:var(--panel,#fff);color:var(--txt,#1c2536);border-radius:16px;max-width:620px;width:94%;max-height:88vh;overflow-y:auto;box-shadow:0 24px 64px rgba(0,0,0,.22)}'
    + '#scorrAnaOv .sa-head{display:flex;align-items:flex-start;gap:12px;padding:18px 20px 14px;border-bottom:1px solid var(--line,#e2e7ee);position:sticky;top:0;background:var(--panel,#fff);z-index:1;flex-wrap:wrap}'
    + '#scorrAnaOv .sa-head>div:first-child{min-width:150px;flex:1}'
    + '#scorrAnaOv #scorrAnaCard:empty{display:none}'
    + '@media(max-width:560px){#scorrAnaOv #scorrAnaCard{order:3;flex-basis:100%;margin-top:2px}}'
    + '#scorrAnaOv .sa-body{padding:16px 20px 28px}'
    + '#scorrAnaOv .empty{padding:30px;text-align:center;color:var(--dim,#8a94ad);font-size:13px}'
    /* cc#2240 (founder 19-Sep screenshot: card renders dark navy inside a light app theme). ROOT
       CAUSE, confirmed by reading scorr_theme_r5.css directly: its `:root:root:not([data-theme=
       "light"])` rule (specificity 0,3,0) redefines --panel/--txt/--dim/--mut/--line/--line2/
       --panel2/--surface2/--grn/--red/--blu/--amber to its own fixed navy-dark R5 palette whenever
       <html>'s data-theme is not literally the string "light" -- true for EVERY one of the app's 15
       named themes (goldnight, aquawhite, goldday, ...), light or dark alike, since none of them is
       spelled "light". That beats scorr_themes.css's own body[data-theme="X"]{--panel:...} block
       (0,1,1) on every app page, so this card's legacy dashboard-era token names were reading R5's
       fixed dark palette instead of the app's actually-active theme -- not "dark when it should be
       light", but "always R5-navy, regardless of theme".
       --grn/--red/--blu/--pulse/--amber/--panel2/--surface2 are fixed by reading the app's OWN
       equivalent tokens (--win/--loss/--accent/--hi), which happen NOT to be in R5's override list.
       --panel/--txt/--dim/--mut/--line/--line2 have no such R5-untouched equivalent (R5 redefines
       --panel/--ink/--muted/--edge too), so those are restated per theme below, copied verbatim from
       scorr_themes.css's own body[data-theme="X"] blocks -- not re-derived, not invented -- scoped to
       ONLY this card's two possible shells at ID specificity (1,0,0), which beats R5's (0,3,0)
       regardless of which theme is active. A ROOT-FIX belongs in scorr_theme_r5.css's own gate or the
       <html>-level theme stamp in main.py's _MOBILE_HEAD (so every R5-styled element benefits, not
       just this one card) -- out of scope here: that is a sitewide change with a blast radius far
       beyond a P1 card scoped to one shared component, flagged in the task result as a follow-up.
       KNOWN MAINTENANCE COST, stated rather than hidden: if a theme's panel/ink/muted/edge value ever
       changes in scorr_themes.css, this block goes stale until updated to match -- the honest tradeoff
       of a targeted workaround instead of the sitewide fix. */
    + '#scorrAnaOv,#infoModal{--panel2:var(--hi,#f6f8fb);--surface2:var(--hi,#f6f8fb);--grn:var(--win,#0a9e63);--red:var(--loss,#e5484d);--blu:var(--accent,#4d7cfe);--pulse:var(--accent,#7c3aed);--amber:var(--label,#f5b94a)}'
    + 'body[data-theme="dark"] #scorrAnaOv,body[data-theme="dark"] #infoModal{--panel:#0F1A33;--txt:#EEF2FF;--dim:#93A0C4;--mut:#93A0C4;--line:#1E2C4E;--line2:#1E2C4E}'
    + 'body[data-theme="goldday"] #scorrAnaOv,body[data-theme="goldday"] #infoModal{--panel:#FFFFFF;--txt:#1A1A1E;--dim:#8A8578;--mut:#8A8578;--line:#E5DFD2;--line2:#E5DFD2}'
    + 'body[data-theme="goldnight"] #scorrAnaOv,body[data-theme="goldnight"] #infoModal{--panel:#131316;--txt:#F5F2EA;--dim:#8E8A7E;--mut:#8E8A7E;--line:#2A2A32;--line2:#2A2A32}'
    + 'body[data-theme="aquawhite"] #scorrAnaOv,body[data-theme="aquawhite"] #infoModal{--panel:#FFFFFF;--txt:#0E1A20;--dim:#46585F;--mut:#46585F;--line:#BFD4DD;--line2:#BFD4DD}'
    + 'body[data-theme="blush"] #scorrAnaOv,body[data-theme="blush"] #infoModal{--panel:#FFFFFF;--txt:#2B0E1A;--dim:#7A4658;--mut:#7A4658;--line:#F0DCE1;--line2:#F0DCE1}'
    + 'body[data-theme="rosenight"] #scorrAnaOv,body[data-theme="rosenight"] #infoModal{--panel:#1A1216;--txt:#FBF1F4;--dim:#B08A98;--mut:#B08A98;--line:#2E1F27;--line2:#2E1F27}'
    + 'body[data-theme="rosewall"] #scorrAnaOv,body[data-theme="rosewall"] #infoModal{--panel:#FFEAF1;--txt:#2A0F1C;--dim:#5E3347;--mut:#5E3347;--line:#F2B9CC;--line2:#F2B9CC}'
    + 'body[data-theme="ainight"] #scorrAnaOv,body[data-theme="ainight"] #infoModal{--panel:#0B1020;--txt:#EAF2FF;--dim:#7F90B3;--mut:#7F90B3;--line:#1E3050;--line2:#1E3050}'
    + 'body[data-theme="silvergold"] #scorrAnaOv,body[data-theme="silvergold"] #infoModal{--panel:#FFFFFF;--txt:#181A1F;--dim:#5B6170;--mut:#5B6170;--line:#CDD1D9;--line2:#CDD1D9}'
    + 'body[data-theme="winepurple"] #scorrAnaOv,body[data-theme="winepurple"] #infoModal{--panel:#1C1223;--txt:#EFE7F7;--dim:#AB98C4;--mut:#AB98C4;--line:#31223C;--line2:#31223C}'
    + 'body[data-theme="duskviolet"] #scorrAnaOv,body[data-theme="duskviolet"] #infoModal{--panel:#1F1D31;--txt:#DCD8EA;--dim:#A6A1BE;--mut:#A6A1BE;--line:#332F4A;--line2:#332F4A}'
    + 'body[data-theme="indigoash"] #scorrAnaOv,body[data-theme="indigoash"] #infoModal{--panel:#1C2331;--txt:#D6DDE8;--dim:#9EAABC;--mut:#9EAABC;--line:#2E3849;--line2:#2E3849}'
    + 'body[data-theme="duskday"] #scorrAnaOv,body[data-theme="duskday"] #infoModal{--panel:#FFFFFF;--txt:#241F38;--dim:#5B5175;--mut:#5B5175;--line:#DBD5EC;--line2:#DBD5EC}'
    + 'body[data-theme="orangepeel"] #scorrAnaOv,body[data-theme="orangepeel"] #infoModal{--panel:#FFFFFF;--txt:#1A1206;--dim:#5C5348;--mut:#5C5348;--line:#E8DFD4;--line2:#E8DFD4}'
    + 'body[data-theme="electricviolet"] #scorrAnaOv,body[data-theme="electricviolet"] #infoModal{--panel:#1D1533;--txt:#EFE8FF;--dim:#BCADE0;--mut:#BCADE0;--line:#3F2F63;--line2:#3F2F63}';
  function _injectStyle() {
    if (document.getElementById('scorr-ana-style')) return;
    try {
      var st = document.createElement('style');
      st.id = 'scorr-ana-style';
      st.appendChild(document.createTextNode(CSS));
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  /* ── host shell resolution ─────────────────────────────────────────────────────────────── */
  var PAGE_IDS = { ov: 'infoModal', t: 'mTitle', s: 'mSub', b: 'mBody', d: 'mDcBtn' };
  var OWN_IDS  = { ov: 'scorrAnaOv', t: 'scorrAnaTitle', s: 'scorrAnaSub', b: 'scorrAnaBody', d: 'scorrAnaCard' };
  var _cur = null;   // shell currently driven by _qaModal/_qaSetBody

  function _buildOwn() {
    _injectStyle();
    if (document.getElementById('scorrAnaOv')) return;
    var ov = document.createElement('div');
    ov.id = 'scorrAnaOv';
    ov.innerHTML = '<div class="sa-box"><div class="sa-head">'
      + '<div><div id="scorrAnaTitle" style="font-size:16px;font-weight:800;color:var(--txt,#1c2536)"></div>'
      + '<div id="scorrAnaSub" style="font-size:11px;color:var(--mut,#5a6b82);margin-top:3px"></div></div>'
      + '<span id="scorrAnaCard"></span>'
      + '<button type="button" id="scorrAnaX" style="width:28px;height:28px;border-radius:50%;border:1px solid var(--line2,#e8ecf2);'
      + 'background:var(--panel2,#f6f8fb);color:var(--mut,#5a6b82);cursor:pointer;font-size:13px;display:flex;align-items:center;'
      + 'justify-content:center;flex-shrink:0">&#10005;</button>'
      + '</div><div class="sa-body" id="scorrAnaBody"></div></div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function (e) { if (e.target === ov) close(); });
    var x = document.getElementById('scorrAnaX');
    if (x) x.addEventListener('click', close);
  }

  function _ids() {
    if (document.getElementById('infoModal')) return PAGE_IDS;
    _buildOwn();
    return OWN_IDS;
  }

  /* ── modal shell (verbatim bodies; host id resolved above) ──────────────────────────── */
  function _qaModal(title,sub,body){
    _cur=_ids();
    const T=document.getElementById(_cur.t),S=document.getElementById(_cur.s),B=document.getElementById(_cur.b),D=document.getElementById(_cur.d);
    if(T)T.textContent=title;if(S)S.textContent=sub||'';if(B)B.innerHTML=body||'';if(D)D.innerHTML='';
    document.getElementById(_cur.ov).classList.add('open');
  }
  function _qaSetBody(html){const ids=_cur||_ids();const B=document.getElementById(ids.b);if(B)B.innerHTML=html;}

  /* ── trajectory grid (verbatim, v8_dashboard.html) ──────────────────────────────────── */
  function _trajValTile(v){return _heatTile(v==null?'--':Number(v).toFixed(2),'n','var(--txt)');}   // Now column: neutral tint
  function _trajDeltaTile(v,bf){const col=v==null?'var(--dim)':v>0?'var(--grn)':v<0?'var(--red)':'var(--mut)';
    const t=(v==null?'--':(v>0?'+':'')+v.toFixed(2))+((bf&&v!=null)?'<span style="color:var(--amber, #F5B94A);font-size:9px;vertical-align:top">*</span>':'');
    return _heatTile(t,_deltaGrade(v),col);}
  function trajHtml(t){
    if(!t)return'';
    const rowLbl=a=>`<div style="width:86px;min-width:86px;font-size:11px;font-weight:700;color:var(--txt)">${a}</div>`;
    const hdr=x=>`<div style="flex:1;text-align:center;font-size:9.5px;color:var(--dim);font-family:${_MONO}">${x}</div>`;
    let h='<div style="font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:14px 0 6px">Trajectory</div>';
    h+=`<div style="display:flex;gap:6px;margin-bottom:5px"><div style="width:86px;min-width:86px"></div>${hdr('Now')}${hdr('Δ1M')}${hdr('Δ6M')}</div>`;
    const row=(lbl,now,d1,d6)=>`<div style="display:flex;gap:6px;margin-bottom:6px;align-items:center">${rowLbl(lbl)}${_trajValTile(now)}${_trajDeltaTile(d1,false)}${_trajDeltaTile(d6,t.backfill_flag_6m)}</div>`;
    h+=row('GVM',t.gvm_now,t.gvm_d21,t.gvm_d126);
    h+=row('Momentum',t.m_now,t.m_d21,t.m_d126);
    h+=row('Sector GVM',t.sector_gvm_now,t.sector_gvm_d21,t.sector_gvm_d126);
    if(t.backfill_flag_6m)h+='<div style="font-size:9.5px;color:var(--dim);margin-top:2px">* 6M baseline partially backfilled</div>';
    // cc#644: sector rank path REMOVED from the Trajectory grid — it now renders as one compact line
    // inside the sector-rating box above (see the GVM Analysis block in qaAnalysis).
    return h;
  }

  /* ── performance heat grid (verbatim, v8_dashboard.html) ────────────────────────────── */
  function heatHtml(d,stockName,segName){
    const sp=d.stock_perf,se=d.sector_perf;if(!sp&&!se)return'';
    const cols=['w1','m1','m3','y1'],hdr=['1W','1M','3M','1Y'];
    const rowLbl=(a,b)=>`<div style="width:86px;min-width:86px"><div style="font-size:11px;font-weight:700;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${a}</div><div style="font-size:9px;color:var(--dim)">${b}</div></div>`;
    let h='<div style="font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:16px 0 6px">Performance</div>';
    h+=`<div style="display:flex;gap:6px;margin-bottom:5px"><div style="width:86px;min-width:86px"></div>${hdr.map(x=>`<div style="flex:1;text-align:center;font-size:9.5px;color:var(--dim);font-family:'IBM Plex Mono',ui-monospace,monospace">${x}</div>`).join('')}</div>`;
    h+=`<div style="display:flex;gap:6px;margin-bottom:6px;align-items:center">${rowLbl(stockName||'Stock','Stock')}${cols.map(k=>_perfTile(sp?sp[k]:null)).join('')}</div>`;
    h+=`<div style="display:flex;gap:6px;align-items:center">${rowLbl(segName||'Sector','Sector')}${cols.map(k=>_perfTile(se?se[k]:null)).join('')}</div>`;
    return h;
  }

  /* ── the A card itself (verbatim, v8_dashboard.html) ────────────────────────────────── */
  // cc#2240: EVERY .open(sym) call, including a repeat tap on an already-open A tab or a return to
  // A after visiting C/R/D, unconditionally blanked the body to "Loading…" and re-fetched both APIs
  // from scratch -- zero caching existed. That is the founder's "C/A/R/D tab switching is not
  // smooth": a visible blank-then-repaint on every tap, not just the first. Fixed the ONE thing this
  // file can safely fix on its own: a same-symbol re-open now reuses the last rendered HTML with no
  // flash and no refetch. It does NOT fix cross-letter jank (A -> C -> A) -- that is structural:
  // C/A/R/D are four independently-built shared components (scorr_chart_card.js/this file/
  // pwa_endpoints.py's ScorrRCard/scorr_cockpit_card.js), each owning its own overlay shell, and
  // cardNav()'s _closeAll() (scorr_card_strip.js) closes all four before the next one opens -- by
  // the cc#803/#805 LOCKED-letters design, no single card's file can merge those shells without
  // rewriting the shared dispatcher, which is out of this card's scope. Stated rather than silently
  // left unfixed.
  var _lastSym = null, _lastHtml = null;
  async function qaAnalysis(sym){
    if (sym === _lastSym && _lastHtml) {
      _qaModal('Analysis · '+sym,'GVM & sector · volume · delivery · trajectory',_lastHtml);
      try{var _mb0=document.getElementById((_cur||_ids()).d);if(_mb0)_mb0.innerHTML=ScorrCardStripHtml(sym,'A');}catch(e){}
      return;
    }
    _qaModal('Analysis · '+sym,'GVM & sector · volume · delivery · trajectory','<div class="empty">Loading…</div>');
    // cc#1959: the strip goes into the RESOLVED host slot -- _cur.d, set by _qaModal() on the line
    // above (PAGE_IDS.d on /dashboard, OWN_IDS.d = the self-built shell's #scorrAnaCard). The old
    // lookup named the dashboard slot id directly, which does not exist on /m/*, so #scorrAnaCard
    // stayed empty and its :empty rule hid the whole row. Same try/catch, same active letter, same
    // order (after the shell clear).
    try{var _mb=document.getElementById((_cur||_ids()).d);if(_mb)_mb.innerHTML=ScorrCardStripHtml(sym,'A');}catch(e){}   // cc#675: C·A·R·D strip; cc#1959: resolved slot
    try{
      const [g,dm]=await Promise.all([
        getJSON('/api/gvm/snapshot/'+encodeURIComponent(sym)).catch(()=>null),
        getJSON('/api/deriv-metrics/'+encodeURIComponent(sym)).catch(()=>null)
      ]);
      const boxd=(inner)=>`<div style="background:var(--surface2);border:1px solid var(--line2);border-radius:8px;padding:10px 12px">${inner}</div>`;
      const secLbl=(t)=>`<div style="font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:14px 0 6px">${t}</div>`;
      let h='';
      // cc#2236: the watchlist + relocates onto this card -- it is the "stock card" every symbol
      // tap now opens (mobile/sector.html's members table dropped its own row button). Reuses the
      // existing shared component/write path unchanged (window.ScorrWatchlistAdd.button, the same
      // call every other surface makes) -- guarded, since this card also renders on /dashboard
      // (web), where the watchlist module may not be loaded.
      if(window.ScorrWatchlistAdd) h+='<div style="display:flex;justify-content:flex-end;margin-bottom:8px">'+window.ScorrWatchlistAdd.button(sym,'analysis',true)+'</div>';
      // (1) GVM ANALYSIS + sector rating/rank box
      if(g){
        const pill=(lbl,v)=>`<div style="flex:1;text-align:center;padding:8px;background:var(--surface2);border:1px solid var(--line2);border-radius:8px"><div style="font-size:10px;color:var(--dim);font-weight:700">${lbl}</div><div style="font-size:16px;font-weight:800;font-family:'Sora',sans-serif">${v==null?'--':Number(v).toFixed(2)}</div></div>`;
        const dg=g.dgvm_180,dgs=(dg==null?'--':(dg>=0?'+':'')+Number(dg).toFixed(2));
        // cc#644 rank_move: sector rank path (now / 1M / 6M) folds INTO this box (removed from Trajectory).
        // Prefer the trajectory rank series (rank_now/rank_1m/rank_6m/segment_n); fall back to snapshot rank.
        const _tj=g.trajectory||{};
        const _rn=(_tj.rank_now!=null?_tj.rank_now:g.segment_rank), _rN=(_tj.segment_n!=null?_tj.segment_n:g.segment_total);
        const _imp=(_tj.rank_6m!=null&&_rn!=null)?(_rn<_tj.rank_6m):null, _rc=_imp===true?'var(--grn)':_imp===false?'var(--red)':'var(--txt)';
        const _rankMain=(_rn!=null)?('#'+_rn+(_rN!=null?' of '+_rN:'')):'--';
        const _rp=[]; if(_tj.rank_1m!=null)_rp.push('1M #'+_tj.rank_1m); if(_tj.rank_6m!=null)_rp.push('6M #'+_tj.rank_6m);
        const _rankSub=_rp.length?` <span style="color:var(--dim);font-size:10px">(${_rp.join(', ')})</span>`:'';
        h+=secLbl('GVM Analysis');
        // cc#644 change_2: GVM number + verdict and the G/V/M pillar boxes share ONE row (headline left,
        // pillars fill the right) — saves a full row. On narrow widths the pill group wraps below.
        h+=`<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap">`+
          `<div style="display:flex;align-items:baseline;gap:8px;flex:0 0 auto"><span style="font-size:26px;font-weight:800;font-family:'Sora',sans-serif">${g.gvm_score==null?'--':Number(g.gvm_score).toFixed(2)}</span><span style="font-size:12px;font-weight:700;color:var(--mut)">${newsEsc(g.verdict||'')}</span></div>`+
          `<div style="display:flex;gap:6px;flex:1;min-width:220px">${pill('Growth',g.g_score)}${pill('Value',g.v_score)}${pill('Momentum',g.m_score)}</div></div>`;
        h+=boxd(`<div style="display:flex;gap:10px;font-size:12px">`+
          `<div style="flex:1;min-width:0"><span style="color:var(--dim)">Segment</span><br><b style="word-break:break-word">${newsEsc(g.segment||'--')}</b></div>`+
          `<div style="flex:1;min-width:0"><span style="color:var(--dim)">Sector rating</span><br><b style="font-size:14px">${g.sector_rating==null?'--':Number(g.sector_rating).toFixed(2)}</b> <span style="color:var(--dim);font-size:10px">${newsEsc(g.sector_verdict||'')}</span></div>`+
          `<div style="flex:1;min-width:0"><span style="color:var(--dim)">Rank in segment</span><br><b style="font-size:14px;color:${_rc}">${_rankMain}</b>${_rankSub}</div></div>`+
          `<div style="color:var(--dim);font-size:11px;margin-top:6px">180d Δ GVM ${dgs}</div>`);
      }
      // cc#630: TRAJECTORY block — score deltas + sector rank path (below the GVM pillars/sector box)
      if(g&&g.trajectory) h+=trajHtml(g.trajectory);
      // (2) VOLUME — cc#674: three adjacent tiles via the shared _volTilesHtml, each tap-to-explain.
      // cc#1445: the shared tiles render RVOL | VOL P | ACCUM (Deriv Cockpit _ad_21d) — the
      // no-data guard checks the fields the tiles actually read.
      const E=(dm&&dm.energy)||{},M=(dm&&dm.meanings)||{};
      const vp=(E.vol_p||{}).value,adp=(E.ad_21d||{}).up_vol_pct,rv=E.rvol;
      h+=secLbl('Volume');
      if(vp==null&&adp==null&&!(rv&&rv.rvol!=null)){ h+=boxd('<span style="color:var(--mut);font-size:12px">No volume data.</span>'); }
      else{ h+=_volTilesHtml(E,M); }
      // (2b) cc#1653 item 3: 21-day ACCUMULATION / DISTRIBUTION bars, above the Delivery box —
      // the SAME drawing gvm.html's Tape card uses (window.ScorrADBars, scorr_card_common.js),
      // fed by the SAME E.ad_21d payload the ACCUM volume tile above already reads (deriv_metrics
      // ._ad_21d now also returns `bars` — read-path only, no new table). This card is read-only:
      // opts is omitted, so no bar is picked and BIGGEST DAY is never outlined (item 4 — kept
      // only because the shared helper degrades to that for free when opts is absent, not built
      // specially for this card). total_return_pct is computed here from the bars' own `close`
      // values (first vs last of the window) since deriv_metrics does not carry a separate
      // 21-day price-change field the way GVM's own volume extras do.
      const _adBars=(E.ad_21d&&E.ad_21d.bars)||null;
      h+=secLbl('Accumulation / Distribution');
      if(_adBars&&_adBars.length){
        const _adFirst=_adBars[0],_adLast=_adBars[_adBars.length-1];
        const _adRet=(_adBars.length>1&&_adFirst.close)?Math.round((_adLast.close/_adFirst.close-1)*1000)/10:null;
        h+=window.ScorrADBars({bars:_adBars,verdict:E.ad_21d.label||'',up_vol_pct:E.ad_21d.up_vol_pct,total_return_pct:_adRet});
      } else {
        h+=boxd('<span style="color:var(--mut);font-size:12px">no per-session volume for this symbol</span>');
      }
      // (3) DELIVERY — deliv% vs own 21d avg + conviction/churn + 30d sparkline
      const dv=E.delivery||{};
      h+=secLbl('Delivery');
      if(dv.deliv_pct==null){ h+=boxd('<span style="color:var(--mut);font-size:12px">No delivery data yet.</span>'); }
      else{
        const lblc=dv.label==='conviction'?'var(--grn)':dv.label==='churn'?'var(--red)':'var(--mut)';
        const lblt=dv.label?dv.label.toUpperCase():'NEUTRAL';
        h+=boxd(`<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap"><span style="font-size:22px;font-weight:800;font-family:'Sora',sans-serif">${Number(dv.deliv_pct).toFixed(1)}%</span><span style="font-size:11px;color:var(--dim)">vs 21d avg ${dv.avg21==null?'--':Number(dv.avg21).toFixed(1)}%${dv.ratio!=null?(' · '+Number(dv.ratio).toFixed(2)+'x'):''}</span><span style="margin-left:auto;font-size:10px;font-weight:800;color:${lblc}">${lblt}</span></div>`+_qaSpark(dv.series||[]));
      }
      // cc#630: performance heat grid — the closing visual (bottom of the panel), Volume/Delivery unchanged
      if(g&&(g.stock_perf||g.sector_perf)) h+=heatHtml(g, newsEsc((g&&g.company_name)||sym), newsEsc((g&&g.segment)||'Sector'));
      // cc#671: footer — TC Trade Check / Investment Check. Buttons host the SAME navbar Check surface
      // (id=3005) INLINE via an iframe embed of /check (single source, never diverges). Context isolation
      // id=244 preserved: the Analysis card stays GVM/volume/delivery; the TC framework lives in its own
      // document, never merged into V8 render.
      h+=qaTcFooterHtml(sym);
      // cc#2240: cache AFTER a successful render only -- a failed/partial load (the catch below)
      // must never be cached, or a transient API error would freeze the card on that error until
      // page reload, exactly the risk skipping a genuine refresh would create.
      _lastSym = sym; _lastHtml = h || '<div class="empty">No analysis data for '+newsEsc(sym)+'.</div>';
      _qaSetBody(_lastHtml);
    }catch(e){_qaSetBody('<div class="empty" style="color:var(--red)">Analysis failed to load.</div>');}
  }

  /* ── TC Trade / Investment Check footer (verbatim, v8_dashboard.html) ───────────────── */
  // cc#671: Analysis-modal TC footer + inline Check embed.
  function qaTcFooterHtml(sym){
    var s=newsEsc(sym);
    return '<div style="height:1px;background:var(--line);margin:18px 0 12px"></div>'
      + '<div class="qa-tcrow">'
      +   '<button id="qaTcTrade" class="qa-tcbtn trade" onclick="qaTcEmbed(\''+s+'\',\'trade\')">&#9650; TC Trade Check</button>'
      +   '<button id="qaTcInvest" class="qa-tcbtn invest" onclick="qaTcEmbed(\''+s+'\',\'investment\')">&#9670; Investment Check</button>'
      +   '<button id="qaTcFlags" class="qa-tcbtn flags" onclick="qaFlagsEmbed(\''+s+'\')">&#9873; Check Flags</button>'   /* cc#1976 */
      + '</div>'
      + '<div id="qaTcHost" style="margin-top:12px"></div>';
  }
  function qaTcEmbed(sym,style){
    var host=document.getElementById('qaTcHost'); if(!host)return;
    var bt=document.getElementById('qaTcTrade'), bi=document.getElementById('qaTcInvest'), bf=document.getElementById('qaTcFlags');
    if(bt)bt.classList.toggle('on',style==='trade'); if(bi)bi.classList.toggle('on',style==='investment'); if(bf)bf.classList.remove('on');   /* cc#1976: one of the three lit at a time */
    var key=sym+'|'+style;
    if(host.dataset.cur===key&&document.getElementById('qaTcFrame'))return;   // cc#671: cache per symbol+style for the modal session
    host.dataset.cur=key;
    // one-time listener: the embedded Check page posts its content height so the iframe fits without a nested scrollbar.
    if(!window.__qaTcMsg){window.__qaTcMsg=true;window.addEventListener('message',function(e){
      var d=e&&e.data; if(!d||d.type!=='scorrTcHeight')return;
      var ifr=document.getElementById('qaTcFrame'); if(ifr&&d.height)ifr.style.height=Math.min(Math.max(d.height,240),1800)+'px';
    });}
    host.innerHTML='<div class="empty" style="padding:16px">Loading '+(style==='investment'?'Investment':'Trade')+' Check for '+newsEsc(sym)+'…</div>';
    var ifr=document.createElement('iframe');
    ifr.id='qaTcFrame';
    ifr.src='/check?symbol='+encodeURIComponent(sym)+'&style='+style+'&embed=1';
    ifr.setAttribute('title','Check — '+sym+' ('+style+')');
    ifr.style.cssText='width:100%;height:440px;border:1px solid var(--line);border-radius:10px;background:var(--panel)';
    host.innerHTML=''; host.appendChild(ifr);
  }

  /* cc#1976 (founder 10-Sep 17:45: "below Investment Check add one more button — Check Flags — and
     show the flags as per the V8 website"). RENDER IN PLACE (item 4): the A card is itself a sheet, so
     the flags expand inside #qaTcHost exactly where the Check iframe goes, never as a second overlay
     on top. CONSUMED, NEVER RE-IMPLEMENTED (item 2): the rows and their interpretation lines come from
     window.ScorrMarkerFlagDetailHtml with the endpoint's own legend[] — no flag markup or legend text
     lives here. SIDE (item 3): the A card is opened for a SYMBOL with no position side in hand, so the
     fired object is passed UNFILTERED — every family that fired shows (a guessed side would hide a
     real marker; today no symbol carries both a BUY and a SELL pivot star, so nothing double-renders).
     The maps are built from the same five lists /m/v8 reads (stars / activity / dma_state / tc_strong
     / channel_reject), dma given star_color = color exactly as /m/v8 does for the shared reader.
     EMPTY (item 5): the shared function's own "No marker fired for this row today." line. */
  function qaFlagsEmbed(sym){
    var host=document.getElementById('qaTcHost'); if(!host)return;
    var bt=document.getElementById('qaTcTrade'), bi=document.getElementById('qaTcInvest'), bf=document.getElementById('qaTcFlags');
    if(bt)bt.classList.remove('on'); if(bi)bi.classList.remove('on'); if(bf)bf.classList.add('on');
    var key=sym+'|flags';
    if(host.dataset.cur===key&&document.getElementById('qaFlagsBox'))return;
    host.dataset.cur=key;
    host.innerHTML='<div class="empty" style="padding:16px">Loading markers for '+newsEsc(sym)+'…</div>';
    getJSON('/api/v8/pivot_star').then(function(d){
      if(host.dataset.cur!==key)return;   // the user moved on to another button meanwhile
      d=d||{};
      var pick=function(list){ var hit=null; (list||[]).some(function(x){ if(x&&x.symbol===sym){hit=x;return true;} return false; }); return hit; };
      var dma=pick(d.dma_state||d.dma_cross);
      var fired={ stars:pick(d.stars), act:pick(d.activity), dma:(dma?Object.assign({},dma,{star_color:dma.color}):null), tcs:pick(d.tc_strong), chan:pick(d.channel_reject) };
      var body=window.ScorrMarkerFlagDetailHtml ? window.ScorrMarkerFlagDetailHtml(fired,null,d.legend||null)
             : '<div class="empty">Marker renderer unavailable</div>';
      var when=d.star_date?('session '+newsEsc(d.star_date)+(d.as_of_is_last_session?' (last session with markers)':'')):'';
      host.innerHTML='<div id="qaFlagsBox"><div class="qa-flags-hd">V8 markers'+(when?' · '+when:'')+' · shown for the symbol, not for a position (every family that fired, no side filter) · same flags as the V8 page</div>'+body+'</div>';
    }).catch(function(e){
      if(host.dataset.cur!==key)return;
      host.innerHTML='<div class="empty" style="padding:16px">Could not load markers ('+newsEsc(e&&e.message||e)+').</div>';
    });
  }

  function close() {
    try {
      var own = document.getElementById('scorrAnaOv');
      if (own) own.classList.remove('open');
    } catch (e) {}
    /* On a host page the shell is shared with the page's own modals; let the page own its closer. */
    try { if (typeof window.closeInfo === 'function') window.closeInfo(); } catch (e) {}
    _cur = null;
  }

  window.ScorrAnalysisCard = { open: qaAnalysis, close: close };

  /* Guarded bare globals — see the header note. Never clobber a name a host page already defines. */
  var G = {
    qaAnalysis: qaAnalysis, _qaModal: _qaModal, _qaSetBody: _qaSetBody,
    trajHtml: trajHtml, heatHtml: heatHtml, _trajValTile: _trajValTile, _trajDeltaTile: _trajDeltaTile,
    qaTcFooterHtml: qaTcFooterHtml, qaTcEmbed: qaTcEmbed, qaFlagsEmbed: qaFlagsEmbed
  };
  Object.keys(G).forEach(function (k) { if (typeof window[k] !== 'function') window[k] = G[k]; });
})();

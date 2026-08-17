/* scorr_analysis_card.js — cc#805 SHARED "A" (Analysis) card, site-wide, self-contained.
 * ==================================================================================
 * The canonical Analysis modal: GVM + sector rating/rank, Trajectory grid, Volume (RVOL/VolX/3D-21D),
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
    + '@media(max-width:560px){.qa-tcrow{flex-direction:column}}'
    + '#scorrAnaOv{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:11400;align-items:center;justify-content:center}'
    + '#scorrAnaOv.open{display:flex}'
    + '#scorrAnaOv .sa-box{background:var(--panel,#fff);border-radius:16px;max-width:620px;width:94%;max-height:88vh;overflow-y:auto;box-shadow:0 24px 64px rgba(0,0,0,.22)}'
    + '#scorrAnaOv .sa-head{display:flex;align-items:flex-start;gap:12px;padding:18px 20px 14px;border-bottom:1px solid var(--line,#e2e7ee);position:sticky;top:0;background:var(--panel,#fff);z-index:1;flex-wrap:wrap}'
    + '#scorrAnaOv .sa-head>div:first-child{min-width:150px;flex:1}'
    + '#scorrAnaOv #scorrAnaCard:empty{display:none}'
    + '@media(max-width:560px){#scorrAnaOv #scorrAnaCard{order:3;flex-basis:100%;margin-top:2px}}'
    + '#scorrAnaOv .sa-body{padding:16px 20px 28px}'
    + '#scorrAnaOv .empty{padding:30px;text-align:center;color:var(--dim,#8a94ad);font-size:13px}';
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
  async function qaAnalysis(sym){
    _qaModal('Analysis · '+sym,'GVM & sector · volume · delivery · trajectory','<div class="empty">Loading…</div>');
    try{var _mb=document.getElementById('mDcBtn');if(_mb)_mb.innerHTML=ScorrCardStripHtml(sym,'A');}catch(e){}   // cc#675: C·A·R·D strip
    try{
      const [g,dm]=await Promise.all([
        getJSON('/api/gvm/snapshot/'+encodeURIComponent(sym)).catch(()=>null),
        getJSON('/api/deriv-metrics/'+encodeURIComponent(sym)).catch(()=>null)
      ]);
      const boxd=(inner)=>`<div style="background:var(--surface2);border:1px solid var(--line2);border-radius:8px;padding:10px 12px">${inner}</div>`;
      const secLbl=(t)=>`<div style="font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);margin:14px 0 6px">${t}</div>`;
      let h='';
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
      // (2) VOLUME — cc#674: three adjacent tiles RVOL | VolX | Vol 3D/21D (shared _volTilesHtml), each
      // tap-to-explain. RVOL = time-of-day-adjusted relative volume (energy.rvol from rvol_profiles).
      const E=(dm&&dm.energy)||{},M=(dm&&dm.meanings)||{};
      const vx=E.volx,r3=E.recent3d_vol_ratio,rv=E.rvol;
      h+=secLbl('Volume');
      if(vx==null&&r3==null&&!(rv&&rv.rvol!=null)){ h+=boxd('<span style="color:var(--mut);font-size:12px">No volume data.</span>'); }
      else{ h+=_volTilesHtml(E,M); }
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
      _qaSetBody(h||'<div class="empty">No analysis data for '+newsEsc(sym)+'.</div>');
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
      + '</div>'
      + '<div id="qaTcHost" style="margin-top:12px"></div>';
  }
  function qaTcEmbed(sym,style){
    var host=document.getElementById('qaTcHost'); if(!host)return;
    var bt=document.getElementById('qaTcTrade'), bi=document.getElementById('qaTcInvest');
    if(bt)bt.classList.toggle('on',style==='trade'); if(bi)bi.classList.toggle('on',style==='investment');
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
    qaTcFooterHtml: qaTcFooterHtml, qaTcEmbed: qaTcEmbed
  };
  Object.keys(G).forEach(function (k) { if (typeof window[k] !== 'function') window[k] = G[k]; });
})();

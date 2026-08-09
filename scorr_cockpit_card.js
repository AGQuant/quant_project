/* scorr_cockpit_card.js — cc#805 SHARED "D" (Derivative Cockpit) card, site-wide, self-contained.
 * =============================================================================================
 * The full Derivative Cockpit sheet: header (live clock, price/change, C·A·R·D strip) + 01 VOLUME,
 * 02 OPEN INTEREST, 03 5-DAY ROLLING OI, 04 FUTURES BASIS, 05 LEVELS, 06 OPTIONS STRIKE CHAIN.
 *
 * Until cc#805 this lived inside v8_dashboard.html, so scorr_card_strip.js could only offer D as a
 * deep-link (`window.open('/dashboard?dc=' + sym)`) — every non-V8 surface opened a NEW TAB to reach
 * one sheet. This file is now the ONE implementation; the strip's D letter is LOCKED to it (cc#805,
 * same treatment cc#803 gave C and R).
 *
 * API:  window.ScorrCockpitCard.open(symbol, side, qty, entry, cmp)   (only `symbol` is required)
 *       window.ScorrCockpitCard.close()
 *
 * DEPENDENCIES — scorr_card_common.js MUST load first (main.py injects it ahead of this file). Every
 * primitive is bound from window.ScorrCardCommon below rather than read off the bare globals, so a
 * host page that owns its own `num`/`sign`/`getJSON` cannot change what this card renders.
 *
 * SELF-CONTAINED. The sheet builds its own #dcOv overlay and injects its own #dc-style sheet, so it
 * needs no markup from the host page. All rules stay scoped under #dcOv (as they already were), and
 * the cockpit's dark-terminal palette maps onto the host page's own theme vars.
 *
 * DEEP LINK. /dashboard?dc=SYMBOL still works — v8_dashboard.html's deferred retry loop waits for
 * `openDerivCockpit` to become defined, and this file republishes exactly that global (below).
 *
 * BACKWARD COMPATIBILITY. openDerivCockpit / _dcClose / _dcFmtDate / _dcFmtTs / _dcQd / dcFetchStrikes
 * and friends are republished as GUARDED bare globals, because the rendered markup carries inline
 * onclick="_dcClose()" and onclick="dcFetchStrikes('SYM')" handlers, which can only resolve against
 * a global — and because v8_dashboard.html's _dcSetupContext still formats dates with _dcFmtDate.
 */
(function () {
  if (window.ScorrCockpitCard) return;

  var CC = window.ScorrCardCommon;
  if (!CC) { try { console.warn('[ScorrCockpitCard] scorr_card_common.js did not load first — D card disabled.'); } catch (e) {} return; }

  /* Bind the shared primitives to the SAME local names the moved bodies use, so every body below
     stays character-for-character what it was in v8_dashboard.html. */
  var num = CC.num, sign = CC.sign, getJSON = CC.getJSON, _MONO = CC._MONO,
      _heatTile = CC._heatTile, _perfGrade = CC._perfGrade, _volTilesHtml = CC._volTilesHtml;

  /* v8_dashboard.html's page-scope EP map is not portable; _dcFetchBasketStatus's body is kept
     verbatim, so the one endpoint it reads is mirrored here. Same URL the page's EP.passCount built. */
  var EP = { passCount: function (b) { return '/api/v8/stock_passcount/' + b; } };

  /* Two classes the cockpit markup borrows from the host page's stylesheet (.empty on the error
     path, .dc-chip on the basket-status chip). Scoped under #dcOv so pages that already define them
     are untouched, and pages that don't still render correctly. */
  function _injectPortableStyle() {
    if (document.getElementById('dc-portable-style')) return;
    try {
      var st = document.createElement('style');
      st.id = 'dc-portable-style';
      st.appendChild(document.createTextNode(
        '#dcOv .empty{padding:30px;text-align:center;color:var(--dim,#8a94ad);font-size:13px}'
        + '#dcOv .dc-chip{display:inline-block;font-family:\'IBM Plex Mono\',ui-monospace,monospace;'
        + 'font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:5px;letter-spacing:.3px;margin-right:5px}'
      ));
      (document.head || document.documentElement).appendChild(st);
    } catch (e) {}
  }

  /* ── styles + shell (verbatim, v8_dashboard.html) ───────────────────────────────────── */
  function _dcInjectStyle(){
    if(document.getElementById('dc-style'))return;
    // cc#571 PART B: pixel-match design_refs/cockpit_v2_R1.html — dark institutional terminal, 5-block
    // information hierarchy (Volume -> OI -> 5D-OI -> Basis -> Levels). All rules scoped under #dcOv so
    // the cockpit's own dark palette never leaks into (or inherits from) the host page theme.
    const css=`#dcOv{position:fixed;inset:0;z-index:11000;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;padding:16px}
    #dcOv.open{display:flex}
    /* cc#665: cockpit drill re-skinned to the Analysis-modal LIGHT chrome — the dark --c-* tokens now
       map to the page's light palette (same vars the Analysis modal uses) so the whole sheet, header,
       LONG/SHORT pill and LIVE clock read as the A-card. Rounded stat tiles (.oibox/.quad/.basis/.lvc)
       become light tiles automatically; section 03 is rebuilt below with the shared _heatTile delta grid. */
    #dcOv .dc-sheet{--c-bg:var(--panel,#fff);--c-panel:var(--surface2,#f6f8fb);--c-bd:var(--line2,#e8ecf2);--c-tx:var(--txt,#1c2536);--c-mut:var(--mut,#5a6b82);--c-dim:var(--dim,#8a94ad);
      --c-grn:var(--grn,#0a9e63);--c-grnbg:rgba(47,212,139,.14);--c-red:var(--red,#dd3a4a);--c-redbg:rgba(255,92,108,.12);--c-amb:var(--amber,#c98a12);--c-ambbg:rgba(245,166,35,.15);
      --c-blu:var(--blu,#1847DF);--c-blubg:rgba(24,71,223,.10);--c-grid:var(--line2,#e8ecf2);
      font-family:'Sora',system-ui,sans-serif;position:relative;width:min(94vw,620px);max-width:620px;max-height:88vh;
      background:var(--c-bg);color:var(--c-tx);border:1px solid var(--c-bd);border-radius:16px;overflow-y:auto;
      box-shadow:0 24px 64px rgba(0,0,0,.22)}   /* cc#679: centered modal (was a right-side slide panel), A-modal footprint */
    @media(max-width:560px){#dcOv .dc-sheet{width:100%;max-width:100%;max-height:92vh}}
    #dcOv .mono{font-family:'IBM Plex Mono',ui-monospace,monospace}
    #dcOv .up{color:var(--c-grn)} #dcOv .dn{color:var(--c-red)}
    #dcOv .hdr{padding:14px 16px 12px;border-bottom:1px solid var(--c-bd);position:relative}
    #dcOv .navrow{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
    #dcOv .back{display:flex;align-items:center;gap:7px;color:var(--c-mut);font-size:12px;cursor:pointer;font-family:'IBM Plex Mono',ui-monospace,monospace}
    #dcOv .back .ar{font-size:15px}
    /* cc#965 item 1: the D card was the only letter card with no X — C, A and R all carry one, and
       a set where one member closes differently is a set you have to learn twice. The labelled
       back-to-funnel control stays exactly as it was: it says WHERE it goes, while the X is the
       unlabelled universal "close this sheet". Both call _dcClose; there is no in-card step
       navigation in this component for them to disagree about. 44px on mobile comes from the
       existing body.mcards #dcOv button rule. */
    #dcOv .navr{display:flex;align-items:center;gap:8px}
    #dcOv .dc-x{background:none;border:0;color:var(--c-mut);font-size:24px;line-height:1;
      cursor:pointer;padding:0;min-width:44px;min-height:44px;font-family:inherit;
      display:inline-flex;align-items:center;justify-content:center}
    #dcOv .dc-x:hover{color:var(--c-tx)}
    #dcOv .ts{font-size:10.5px;color:var(--c-grn);letter-spacing:.5px;border:1px solid #17603f;background:var(--c-grnbg);border-radius:8px;padding:5px 11px}
    #dcOv .sym{display:flex;align-items:flex-end;justify-content:space-between}
    #dcOv .sym .l h1{font-size:26px;font-weight:800;letter-spacing:-.5px;line-height:1;color:var(--c-tx)}   /* cc#648 part_4: explicit bright text — parity with the price numeral (was inheriting, read dim) */
    #dcOv .sym .l .tag{margin-top:6px;display:inline-block;font-size:10px;color:var(--c-mut);border:1px solid var(--c-bd);border-radius:5px;padding:2px 7px;letter-spacing:.5px}
    #dcOv .sym .r{text-align:right}
    #dcOv .sym .r .px{font-size:22px;font-weight:700}
    #dcOv .sym .r .chg{font-size:13px;font-weight:600;margin-top:3px}
    #dcOv .sec{padding:14px 16px;border-bottom:1px solid var(--c-bd)}
    #dcOv .lbl{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;letter-spacing:1.5px;color:var(--c-mut);text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}
    #dcOv .lbl .n{color:var(--c-dim);font-weight:600}
    #dcOv .volgrid{display:grid;grid-template-columns:1.4fr 1fr;gap:10px}
    #dcOv .vtile{background:var(--c-panel);border:1px solid var(--c-bd);border-radius:12px;padding:14px 14px 12px;overflow:hidden}
    #dcOv .vtile .cap{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--c-mut);letter-spacing:.5px}
    #dcOv .vtile .big{font-size:40px;font-weight:800;letter-spacing:-1px;line-height:1;margin-top:6px}
    #dcOv .vtile .big.on{color:var(--c-grn)} #dcOv .vtile small{font-size:18px;font-weight:600;color:var(--c-mut)}
    #dcOv .vtile .sub{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--c-mut);margin-top:6px}
    #dcOv .vbar{height:5px;border-radius:3px;background:#132038;margin-top:11px;overflow:hidden}
    #dcOv .vbar i{display:block;height:100%;background:linear-gradient(90deg,#1c9c63,var(--c-grn))}
    #dcOv .vtile.sm .big{font-size:30px}
    #dcOv .oiwrap{display:flex;align-items:stretch;gap:12px}
    #dcOv .oibox{flex:1;background:var(--c-panel);border:1px solid var(--c-bd);border-radius:12px;padding:14px}
    #dcOv .oibox .d{font-size:11px;margin-top:4px}
    #dcOv .quad{flex:1.05;border-radius:12px;padding:14px;display:flex;flex-direction:column;justify-content:center;background:var(--c-grnbg);border:1px solid #17603f}
    #dcOv .quad.bear{background:var(--c-redbg);border-color:#5e2230} #dcOv .quad.neu{background:var(--c-ambbg);border-color:#5a4620}
    #dcOv .quad .k{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:#8fe6bb;letter-spacing:1px}
    #dcOv .quad.bear .k{color:#f2a6b1} #dcOv .quad.neu .k{color:#e8c890}
    #dcOv .quad .v{font-size:20px;font-weight:800;color:var(--c-grn);line-height:1.05;margin-top:6px;letter-spacing:-.4px}
    #dcOv .quad.bear .v{color:var(--c-red)} #dcOv .quad.neu .v{color:var(--c-amb)}
    /* cc#964: the label text goes neutral-dim; the NUMBERS carry the colour, per sign, from _qStat.
       #6fbf95 was a hardcoded green with no .bear counterpart, which is how a bearish box ended up
       with green stats on a red fill. */
    #dcOv .quad .m{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;color:var(--c-mut);margin-top:8px}
    #dcOv .strip{width:100%;border-collapse:collapse;font-family:'IBM Plex Mono',ui-monospace,monospace}
    #dcOv .strip th{font-size:10.5px;color:var(--c-mut);text-align:right;font-weight:500;letter-spacing:.5px;padding:0 0 8px}
    #dcOv .strip th:first-child{text-align:left}
    #dcOv .strip td{font-size:14px;padding:8px 0;border-top:1px solid var(--c-grid);text-align:right}   /* cc#648 part_4: bump OI numerals to FUTURES-BASIS scale (IBM Plex Mono, inherited from .strip) */
    #dcOv .strip td:first-child{text-align:left;color:var(--c-mut)}
    #dcOv .qd{display:inline-block;font-size:9px;padding:2px 6px;border-radius:4px;letter-spacing:.3px}
    /* cc#624 item_1: quadrant chip is bullish(green)/bearish(red) only — LB+SC green, LU+SB red */
    #dcOv .qd.qg{background:var(--c-grnbg);color:var(--c-grn)} #dcOv .qd.qr{background:var(--c-redbg);color:var(--c-red)}
    /* cc#624 item_4: LEVELS section-header verdict chip */
    #dcOv .lvv{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:5px;letter-spacing:.5px;margin-left:auto}
    #dcOv .lvv.grn{background:var(--c-grnbg);color:var(--c-grn);border:1px solid #17603f}
    #dcOv .lvv.amb{background:var(--c-ambbg);color:var(--c-amb);border:1px solid #5a4620}
    #dcOv .lvv.red{background:var(--c-redbg);color:var(--c-red);border:1px solid #5e2230}
    #dcOv .basis{display:flex;align-items:center;gap:14px;background:var(--c-panel);border:1px solid var(--c-bd);border-radius:12px;padding:13px 15px}
    #dcOv .basis .v{font-size:24px;font-weight:800;letter-spacing:-.5px}
    #dcOv .basis .col{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;color:var(--c-mut)}
    #dcOv .basis .col b{color:var(--c-tx);font-weight:600}
    #dcOv .chip{margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;padding:5px 11px;border-radius:20px;background:var(--c-grnbg);color:var(--c-grn);border:1px solid #17603f;letter-spacing:.5px}
    #dcOv .chip.dn{background:var(--c-redbg);color:var(--c-red);border-color:#5e2230} #dcOv .chip.neu{background:var(--c-ambbg);color:var(--c-amb);border-color:#5a4620}
    #dcOv .lv{display:grid;grid-template-columns:1fr 1fr;gap:9px}
    #dcOv .lvc{background:var(--c-panel);border:1px solid var(--c-bd);border-radius:11px;padding:11px 12px}
    #dcOv .lvc .k{font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;color:var(--c-mut);letter-spacing:.5px;display:flex;justify-content:space-between;align-items:center}
    #dcOv .lvc .v{font-size:19px;font-weight:700;margin-top:5px;letter-spacing:-.3px}
    #dcOv .pin{font-size:8.5px;padding:1px 6px;border-radius:4px}
    #dcOv .pin.mag{background:var(--c-blubg);color:var(--c-blu)} #dcOv .pin.nak{background:var(--c-ambbg);color:var(--c-amb)}
    #dcOv .foot{padding:12px 16px 18px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9px;color:var(--c-dim);text-align:center;line-height:1.6}
    /* cc#621 Issue 2: hard single-screen WIDTH — no horizontal scroll at ~380px. Flex/grid children
       default to min-width:auto and refuse to shrink below their content, so a long quadrant label /
       basis chip / big number pushes the whole sheet wide. Force shrinkability + wrapping on the
       value cells, and clip any residual overflow at the sheet edge (belt-and-suspenders). */
    #dcOv .dc-sheet{overflow-x:hidden}
    #dcOv .sec,#dcOv .hdr{max-width:100%}
    #dcOv .oiwrap,#dcOv .basis,#dcOv .volgrid,#dcOv .lv,#dcOv .sym,#dcOv .navrow{max-width:100%}
    #dcOv .oibox,#dcOv .quad,#dcOv .vtile,#dcOv .lvc,#dcOv .basis>*,#dcOv .sym>*{min-width:0}
    #dcOv .vtile .big,#dcOv .quad .v,#dcOv .oibox .d,#dcOv .basis .v,#dcOv .basis .col,#dcOv .lvc .v{overflow-wrap:anywhere;word-break:break-word}
    #dcOv .basis .chip,#dcOv .chip{flex-shrink:0}
    #dcOv .sym .l h1{overflow-wrap:anywhere}
    #dcOv .dc-skel{height:64px;border-radius:12px;margin:12px 16px;background:linear-gradient(90deg,var(--c-panel) 25%,#16223c 37%,var(--c-panel) 63%);background-size:400% 100%;animation:dcsh 1.2s ease infinite}
    @keyframes dcsh{0%{background-position:100% 0}100%{background-position:-100% 0}}`;
    const st=document.createElement('style');st.id='dc-style';st.textContent=css;document.head.appendChild(st);
  }
  function _dcClose(){const o=document.getElementById('dcOv');if(o)o.classList.remove('open');}
  // cc#515: plain "YYYY-MM-DD" -> "DD-Mon" (Results chip dates; distinct from _dcFmtTs which parses
  // full timestamps).
  function _dcFmtDate(s){
    const m=String(s||'').match(/(\d{4})-(\d{2})-(\d{2})/);
    if(!m)return String(s||'');
    const mon=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(m[2],10)-1];
    return `${m[3]}-${mon}`;
  }

  /* ── header (verbatim, v8_dashboard.html) ───────────────────────────────────────────── */
  function _dcHeader(sym,side,qty,entry,cmp,dcmp){
    // cc#571 PART B: cockpit_v2 header — back-to-funnel (closes the sheet, returns to the funnel it
    // opened from), live clock, big symbol + price/change. Matches design_refs/cockpit_v2_R1.html.
    const isShort=(side||'').toUpperCase()==='SHORT';
    const c=(cmp!=null?cmp:dcmp);
    const chg=(entry&&c)?((isShort?(entry-c):(c-entry))/entry*100):null;
    const cc=(chg==null)?'':(chg>=0?'up':'dn'), ar=(chg==null)?'':(chg>=0?'▲':'▼');
    let clk='';try{clk=new Date().toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'});}catch(e){}
    return `<div class="hdr">
      <div class="navrow">
        <div class="back mono" onclick="_dcClose()"><span class="ar">&larr;</span> V8 FUNNEL</div>
        <div class="navr">
          <div class="ts mono">&#9727; ${clk} &middot; LIVE</div>
          <button type="button" class="dc-x" aria-label="Close" onclick="_dcClose()">&times;</button>
        </div>
      </div>
      <div class="sym">
        <div class="l"><h1>${sym}</h1>
          <span class="tag mono">${side||'FUT'}${qty?(' &middot; '+qty):''}${entry?(' @ '+num(entry)):''}</span>
          <div style="margin-top:8px">${window.ScorrCardStripHtml?ScorrCardStripHtml(sym,'D'):''}</div></div>
        <div class="r"><div class="px mono">${c!=null?num(c,1):'--'}</div>
          ${chg!=null?`<div class="chg mono ${cc}">${ar} ${sign(chg,2)}%</div>`:''}</div>
      </div>
    </div>`;
  }

  /* ── basket-status context chip (verbatim, v8_dashboard.html) ──────────────────────── */
  // cc#516 Part A: best-effort, non-blocking V8 basket-status context chip. Reuses the existing
  // stock_passcount payloads (no new endpoint); "display-only" per spec -- appended into #dcSetupCtx
  // once resolved, never awaited by the main render (a slow/failed fetch just means no chip).
  async function _dcFetchBasketStatus(sym){
    const baskets=[['buy_reversal','BuyRev'],['buy_momentum','BuyMom'],['sell_reversal','SellRev'],['sell_momentum','SellMom']];
    try{
      const results=await Promise.all(baskets.map(([b])=>getJSON(EP.passCount(b)).catch(()=>null)));
      let best=null;
      results.forEach((pc,i)=>{
        if(!pc||!pc.stocks)return;
        const row=pc.stocks.find(x=>x.symbol===sym);
        if(row&&(!best||row.passed>best.row.passed))best={label:baskets[i][1],row};
      });
      if(!best)return;
      const missing=(best.row.failed_filters||[])[0];
      const txt=`${best.label} ${best.row.passed}/${best.row.total}${missing?(' &middot; missing '+missing.replace(/_/g,' ')):''}`;
      const el=document.getElementById('dcSetupCtx');
      if(el)el.insertAdjacentHTML('beforeend',`<span class="dc-chip" style="background:rgba(140,153,189,.14);color:var(--mut)">${txt}</span>`);
    }catch(e){/* display-only context — silent on failure */}
  }

  /* ── render (verbatim, v8_dashboard.html) ───────────────────────────────────────────── */
  // cc#571 PART B: cockpit_v2 5-block render (Volume -> OI -> 5D-OI -> Basis -> Levels), pixel-matched
  // to design_refs/cockpit_v2_R1.html. Live data from the SAME deriv-metrics payload; ATM dropped
  // (per the locked ref — stock option OI is no longer on the WS). Graceful: any block whose data is
  // absent is omitted rather than rendering an empty shell.
  // cc#624 item_1: two-letter quadrant chip. LB (Long Buildup) + SC (Short Covering) = GREEN (bullish);
  // LU (Long Unwinding) + SB (Short Buildup) = RED (bearish). Null/unknown quadrant -> no chip.
  function _dcQd(q){ q=(q||'').toLowerCase();
    if(q.indexOf('long')>=0&&q.indexOf('build')>=0)return['qg','LB'];
    if(q.indexOf('cover')>=0)return['qg','SC'];
    if(q.indexOf('unwind')>=0)return['qr','LU'];
    if(q.indexOf('short')>=0&&q.indexOf('build')>=0)return['qr','SB'];
    return null; }
  /* cc#964 item 3: each half of the quadrant stat line is coloured by ITS OWN sign.
     The .m line was a single element painted #6fbf95 (green) with no .bear override, so on a SHORT
     BUILDUP box it printed green text on the red-tinted fill — measured, and the founder's
     "green stat text on a pastel pink fill". One colour cannot be right here anyway: the whole
     point of a buildup quadrant is that OI and price moved in OPPOSITE directions, so a single
     tint would have to misreport one of the two numbers. Null stays neutral, never a fake sign. */
  function _qStat(lbl,v){
    var c = (v==null) ? 'var(--c-mut)' : (v>0 ? 'var(--c-grn)' : (v<0 ? 'var(--c-red)' : 'var(--c-mut)'));
    return lbl+' <span style="color:'+c+';font-weight:700">'+(v==null?'--':sign(v,1)+'%')+'</span>';
  }
  function _dcRender(sym,side,qty,entry,cmp,d){
    const E=d.energy||{},M=d.meanings||{},F=d.flow||{},V=d.verdict||{},L=d.levels||{};
    const pctw=(x,mx)=>x==null?0:Math.max(3,Math.min(100,Math.round(x/mx*100)));

    // cc#624 item_3: section 01 VOLUME (VolX + Vol 3D/21D tiles) REMOVED from the cockpit display.
    // E.volx / E.recent3d_vol_ratio remain in the API payload — they still feed the composite READ votes.

    // 02 OPEN INTEREST — day OI change + OI×price quadrant
    const fo=F.fut_oi||{},q=V.oi_quadrant||{};
    const qcls=q.proxy?'neu':(q.color==='bull'?'':(q.color==='bear'?'bear':'neu'));
    const qv=q.label?String(q.label).toUpperCase():(q.stale?'OI STALE':'DATA THIN');
    const s2=`<div class="sec"><div class="lbl"><span class="n">02</span> OPEN INTEREST</div>
      <div class="oiwrap">
        <div class="oibox">
          <div class="d ${(fo.chg_pct||0)>=0?'up':'dn'}" style="font-size:26px;font-weight:800;line-height:1">${fo.chg_pct!=null?sign(fo.chg_pct,1)+'%':'--'}</div>
          <div class="d" style="color:var(--c-mut)">day OI change vs prev</div>
          ${fo.price_chg!=null?`<div class="d" style="color:var(--c-mut)">price ${sign(fo.price_chg,1)}%</div>`:''}
        </div>
        <div class="quad ${qcls}"><div class="k">OI &times; PRICE QUADRANT</div>
          <div class="v">${qv}${q.proxy?' (PROXY)':''}</div>
          <div class="m">${q.oi_chg_pct!=null?(_qStat('OI',q.oi_chg_pct)+' &middot; '+_qStat('Px',q.price_chg_pct)):(M.oi_quadrant||(q.stale_since?('since '+_dcFmtDate(q.stale_since)):''))}</div>
        </div>
      </div></div>`;

    // 03 5-DAY ROLLING OI — cc#665: rebuilt as the Analysis-modal TRAJECTORY-style heat grid using the
    // SHARED _heatTile delta cell (green/red tinted, "--" muted for NO DATA, "0.0%" for a TRUE zero),
    // replacing the old flat table. Renders exactly what cc#648 part_4 surfaces — present rows show real
    // values, genuinely-missing rows stay "--" (never coerced to a green 0).
    const o5=F.oi_5d||{};
    let s3='';
    if(o5.series&&o5.series.length){
      const _oiHeat=v=>{
        if(v==null) return _heatTile('--','n','var(--dim)');
        if(v===0)   return _heatTile('0.0%','n','var(--txt)');
        const col=v>0?'var(--grn)':'var(--red)';
        return _heatTile((v>0?'+':'')+v.toFixed(1)+'%',_perfGrade(v),col);
      };
      const hdr=x=>`<div style="flex:1;text-align:center;font-size:9.5px;color:var(--dim);font-family:${_MONO}">${x}</div>`;
      const sigCell=x=>{const qd=_dcQd(x.quadrant);
        const inner=qd?`<span class="qd ${qd[0]}">${qd[1]}</span>`:`<span style="color:var(--c-mut);font-family:${_MONO}">--</span>`;
        return `<div style="flex:1;display:flex;align-items:center;justify-content:center">${inner}</div>`;};
      let grid=`<div style="display:flex;gap:6px;margin-bottom:5px"><div style="width:62px;min-width:62px"></div>${hdr('OI &Delta;%')}${hdr('Price &Delta;%')}${hdr('Signal')}</div>`;
      o5.series.forEach(x=>{ grid+=`<div style="display:flex;gap:6px;margin-bottom:6px;align-items:center"><div style="width:62px;min-width:62px;font-size:11px;font-weight:700;color:var(--txt);font-family:${_MONO}">${_dcFmtDate(x.date)}</div>${_oiHeat(x.chg_pct)}${_oiHeat(x.px_chg)}${sigCell(x)}</div>`; });
      s3=`<div class="sec"><div class="lbl"><span class="n">03</span> 5-DAY ROLLING OI</div>${grid}${o5.net_pct!=null?`<div class="sub mono" style="text-align:right;margin-top:4px;color:var(--c-mut)">net ${sign(o5.net_pct,1)}%</div>`:''}</div>`;
    }

    // 04 FUTURES BASIS
    const b=F.basis||{},fut=d.fut,spot=d.spot;
    let s4='';
    if(b.value!=null||fut!=null){
      const prem=(b.value||0)>=0;
      // cc#624 item_2: chip color now driven by the server tag_color (grn/red/amb), not the side —
      // DISCOUNT can read FADING(green) or DEEP(red); PREMIUM STRONG(green)/WEAK(red)/AVERAGE(amber).
      const _chipCls=b.tag_color==='grn'?'':b.tag_color==='red'?'dn':b.tag_color==='amb'?'neu':(prem?'':'dn');
      s4=`<div class="sec"><div class="lbl"><span class="n">04</span> FUTURES BASIS</div>
        <div class="basis"><div class="v ${prem?'up':'dn'}">${b.value!=null?sign(b.value,1):'--'}</div>
          <div class="col">${fut!=null?`FUT <b>${num(fut,1)}</b>`:''}${spot!=null?`${fut!=null?' &middot; ':''}EQ <b>${num(spot,1)}</b>`:''}${b.percentile!=null?`<br>5d percentile <b>${num(b.percentile,0)}%</b>`:''}</div>
          <div class="chip ${_chipCls}">${prem?'PREMIUM':'DISCOUNT'}${b.tag?(' &middot; '+b.tag):''}</div>
        </div></div>`;
    }

    // 05 LEVELS — cc#666 part_2: each level card carries a WEAK/STRONG/NAKED strength chip (VPOC today/
    // prior, VWAP from touch-count + CMP distance; Delivery from deliv% vs its 21d avg). ATR is a
    // volatility measure, NOT a level -> deliberately no chip.
    const vp=L.vpoc_today||{},vpp=L.vpoc_prior||{},vw=L.vwap||{},dv=E.delivery||{};
    const strCh=s=>(s&&s.label)?`<span class="lvv ${s.color||'amb'}" style="margin-left:auto;font-size:8px;padding:1px 6px;letter-spacing:.3px">${s.label}</span>`:'';
    const delCh=(function(){
      if(dv.deliv_pct==null)return'';
      let lab=null,col='amb';
      if(dv.ratio!=null){ if(dv.ratio>=1.2){lab='STRONG';col='grn';} else if(dv.ratio<=0.7){lab='WEAK';col='red';} else {lab='AVG';col='amb';} }
      else if(dv.avg21!=null){ lab=dv.deliv_pct>=dv.avg21?'STRONG':'WEAK'; col=dv.deliv_pct>=dv.avg21?'grn':'red'; }
      return lab?strCh({label:lab,color:col}):'';
    })();
    const lvc=(k,pin,v,chip)=>v==null?'':`<div class="lvc"><div class="k">${k}${pin||''}${chip||''}</div><div class="v">${v}</div></div>`;
    let cards='';
    cards+=lvc('VPOC &middot; TODAY',' <span class="pin mag">POC</span>',vp.value!=null?num(vp.value,0):null,strCh(vp.strength));
    cards+=lvc('VPOC &middot; PRIOR','',vpp.value!=null?num(vpp.value,0):null,strCh(vpp.strength));
    cards+=lvc('VWAP','',vw.value!=null?num(vw.value,1):null,strCh(vw.strength));
    cards+=lvc('ATR &middot; 14D','',E.atr_daily!=null?num(E.atr_daily,1):null);
    cards+=lvc('DELIVERY %','',dv.deliv_pct!=null?(num(dv.deliv_pct,1)+'<span style="font-size:12px;color:var(--c-mut)">%</span>'):null,delCh);
    // cc#624 item_4: structural LEVELS verdict chip (STRONG/MODERATE/WEAK · winning/non-neutral votes),
    // assembled server-side so it can never drift from the tiles. ATR coiling -> caution line, not a vote.
    const lv=L.verdict||{};
    const lvChip=lv.label?`<span class="lvv ${lv.color||'amb'}">${lv.label} ${lv.score||''}</span>`:'';
    const lvCaution=lv.caution?`<div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:9.5px;color:var(--c-amb);margin-top:8px">&#9888; ${lv.caution}</div>`:'';
    const s5=cards?`<div class="sec"><div class="lbl"><span class="n">05</span> LEVELS${lvChip}</div><div class="lv">${cards}</div>${lvCaution}</div>`:'';

    // cc#666 part_3: OPTIONS strike chain — on-demand ATM±10 CE/PE with live ltp vs Black-Scholes fair.
    const s6=`<div class="sec"><div class="lbl"><span class="n">06</span> OPTIONS &middot; STRIKE CHAIN
        <button onclick="dcFetchStrikes('${sym}')" style="margin-left:auto;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:10px;font-weight:700;letter-spacing:.5px;border:1px solid var(--c-bd);background:var(--c-panel);color:var(--c-tx);border-radius:7px;padding:5px 10px;cursor:pointer">&#8862; FETCH STRIKES</button></div>
      <div id="dcStrikeChain" style="font-size:11px;color:var(--c-mut);line-height:1.5">ATM &plusmn;10 CE/PE &middot; live ltp vs Black-Scholes fair (&sigma;=RV20) &middot; tap FETCH STRIKES.</div></div>`;
    const foot=`<div class="foot">DERIVATIVE COCKPIT v3 &middot; live from deriv_metrics${d.data_ts?(' &middot; options '+_dcFmtTs(d.data_ts)+' IST'):''}</div>`;
    // cc#674: section 01 VOLUME restored as the shared 3-tile row (RVOL | VolX | Vol 3D/21D) — same
    // component as the Analysis modal (cc#624 removed the old 2-tile; this is the new RVOL-led row).
    const s1=`<div class="sec"><div class="lbl"><span class="n">01</span> VOLUME <span class="n">RVOL · VolX · 3D/21D</span></div>`+_volTilesHtml(E,M)+`</div>`;
    return _dcHeader(sym,side,qty,entry,cmp,d.cmp)+s1+s2+s3+s4+s5+s6+foot;
  }

  /* ── strike chain + timestamp (verbatim, v8_dashboard.html) ────────────────────────── */
  // cc#666 part_3: fetch + render the ATM±10 strike chain (live ltp vs BS fair). Reuses the deployed
  // /api/deriv/strike-chain endpoint; live pricing verified at Monday open (sandbox fyers egress blocked).
  async function dcFetchStrikes(sym){
    const box=document.getElementById('dcStrikeChain'); if(!box)return;
    box.innerHTML='<div style="padding:10px 0;color:var(--c-mut)">Fetching live strikes for '+sym+'&hellip;</div>';
    let d;
    try{d=await getJSON('/api/deriv/strike-chain/'+encodeURIComponent(sym));}
    catch(e){box.innerHTML='<div style="color:var(--c-red);padding:8px 0">Strike chain unavailable ('+((e&&e.message)||e)+').</div>';return;}
    if(!d||d.error||!(d.strikes&&d.strikes.length)){box.innerHTML='<div style="color:var(--c-mut);padding:8px 0">'+((d&&d.error)||'No strikes returned.')+'</div>';return;}
    const tagCol=t=>t==='EXPENSIVE'?'var(--c-red)':t==='CHEAP'?'var(--c-grn)':'var(--c-mut)';
    const B='text-align:right;padding:5px 6px;border-top:1px solid var(--c-grid);white-space:nowrap';
    const cell=o=>{
      if(!o||o.ltp==null)return '<td style="'+B+'">&mdash;</td><td style="'+B+'">&mdash;</td><td style="'+B+'">&mdash;</td><td style="'+B+'">&mdash;</td>';
      return '<td style="'+B+';font-weight:700">'+o.ltp+'</td>'
        +'<td style="'+B+'">'+(o.iv!=null?o.iv+'%':'&mdash;')+'</td>'
        +'<td style="'+B+';color:var(--c-mut)">'+(o.fair!=null?o.fair:'&mdash;')+'</td>'
        +'<td style="'+B+'"><span style="font-size:9px;font-weight:800;color:'+tagCol(o.tag)+'">'+(o.tag||'&mdash;')+(o.ratio!=null?' '+o.ratio+'&times;':'')+'</span></td>';
    };
    const rows=d.strikes.map(r=>{
      const hl=r.atm?' style="background:var(--c-blubg)"':'';
      return '<tr'+hl+'>'+cell(r.ce)
        +'<td style="text-align:center;padding:5px 8px;border-top:1px solid var(--c-grid);font-weight:800;font-family:\'IBM Plex Mono\',ui-monospace,monospace;color:var(--c-tx)">'+r.strike+(r.atm?'&nbsp;ATM':'')+'</td>'
        +cell(r.pe)+'</tr>';
    }).join('');
    const hcol='style="text-align:right;padding:4px 6px;font-size:9px;color:var(--c-mut);font-weight:700"';
    box.innerHTML='<div style="font-size:10px;color:var(--c-dim);margin-bottom:6px">Spot '+d.spot+' &middot; exp '+d.expiry+' ('+d.days_to_expiry+'d) &middot; RV20 '+(d.rv20!=null?d.rv20+'%':'&mdash;')+' &middot; '+d.quoted+' contracts quoted</div>'
      +'<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-family:\'IBM Plex Mono\',ui-monospace,monospace;font-size:11px">'
      +'<thead><tr><td '+hcol+'>CE ltp</td><td '+hcol+'>IV</td><td '+hcol+'>fair</td><td '+hcol+'>tag</td>'
      +'<td style="text-align:center;padding:4px 8px;font-size:9px;color:var(--c-mut);font-weight:700">STRIKE</td>'
      +'<td '+hcol+'>PE ltp</td><td '+hcol+'>IV</td><td '+hcol+'>fair</td><td '+hcol+'>tag</td></tr></thead>'
      +'<tbody>'+rows+'</tbody></table></div>'
      +'<div style="font-size:9px;color:var(--c-dim);margin-top:6px">Fair = Black-Scholes (&sigma;=RV20, r=7%). Tag: EXPENSIVE &gt;+25% &middot; REASONABLE 0..+25% &middot; CHEAP &lt;0% (ltp vs fair).</div>';
  }
  // cc#368: format the naive-IST option_chain ts ("2026-07-10T10:45:00") -> "10-Jul 10:45" without
  // Date()/timezone drift (the server stamp is already IST).
  function _dcFmtTs(s){
    const m=String(s||'').match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
    if(!m)return String(s||'');
    const mon=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][parseInt(m[2],10)-1];
    return `${m[3]}-${mon} ${m[4]}:${m[5]}`;
  }

  /* ── opener (verbatim, v8_dashboard.html) ───────────────────────────────────────────── */
  async function openDerivCockpit(sym,side,qty,entry,cmp){
    _dcInjectStyle();
    let ov=document.getElementById('dcOv');
    if(!ov){ov=document.createElement('div');ov.id='dcOv';ov.innerHTML='<div class="dc-sheet" id="dcSheet"></div>';document.body.appendChild(ov);
      ov.addEventListener('click',e=>{if(e.target===ov)_dcClose();});}
    const sheet=document.getElementById('dcSheet');
    sheet.innerHTML=_dcHeader(sym,side,qty,entry,cmp,null)+'<div class="dc-skel"></div><div class="dc-skel"></div><div class="dc-skel"></div>';
    ov.classList.add('open');
    // cc#368: never let the skeleton shimmer forever — race the fetch against a 10s timeout so the
    // sheet ALWAYS resolves (to data, or an explicit failure/timeout state).
    const _to=new Promise((_,rej)=>setTimeout(()=>rej(new Error('dc_timeout')),10000));
    let d;try{d=await Promise.race([getJSON('/api/deriv-metrics/'+encodeURIComponent(sym)+(side?('?side='+encodeURIComponent(side)):'')),_to]);}
    catch(e){const isTo=(e&&e.message==='dc_timeout');
      const msg=isTo?'Cockpit timed out (10s) — tap the symbol to retry.':'Cockpit failed to load.';
      // cc#449: surface the actual error detail (not a bare one-liner) so a real API failure is diagnosable.
      const det=(!isTo&&e&&e.message)?`<div style="color:var(--mut);font-size:11px;margin-top:6px">${String(e.message).slice(0,240)}</div>`:'';
      sheet.innerHTML=_dcHeader(sym,side,qty,entry,cmp,null)+`<div class="empty" style="padding:18px;color:var(--red);font-size:12px">${msg}${det}</div>`;return;}
    sheet.innerHTML=_dcRender(sym,side,qty,entry,cmp,d||{});
    _dcFetchBasketStatus(sym);   // cc#516: non-blocking, patches #dcSetupCtx in place when it resolves
  }

  /* The exported opener also lays down the two portable CSS rules, so every entry point (strip D,
     ?dc= deep link, a page's own call) renders identically on any surface. */
  function open(sym, side, qty, entry, cmp) {
    _injectPortableStyle();
    return openDerivCockpit(sym, side, qty, entry, cmp);
  }

  window.ScorrCockpitCard = { open: open, close: _dcClose };

  /* Guarded bare globals — see the header note. Never clobber a name a host page already defines. */
  var G = {
    openDerivCockpit: open, _dcClose: _dcClose, _dcInjectStyle: _dcInjectStyle, _dcHeader: _dcHeader,
    _dcRender: _dcRender, _dcQd: _dcQd, _dcFmtDate: _dcFmtDate, _dcFmtTs: _dcFmtTs,
    _dcFetchBasketStatus: _dcFetchBasketStatus, dcFetchStrikes: dcFetchStrikes
  };
  Object.keys(G).forEach(function (k) { if (typeof window[k] !== 'function') window[k] = G[k]; });
})();

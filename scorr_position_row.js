/* scorr_position_row.js — cc#2101 SHARED position-row primitives.
 * ==================================================================
 * Extracted from mobile/v8.html (cc#1660/1726/1868/2097/2098/2099/2100) so mobile/v8.html and
 * mobile/tcscan.html consume ONE definition of the price-rail geometry, the TC-score capsule
 * bands and the C.A.R.D tap-reveal toggle, instead of TC Scanner hand-rolling a second copy.
 *
 * WHAT IS HERE, and why only this much: the PURE, page-agnostic pieces -- geometry math, band
 * thresholds, the tap-reveal toggle. What is NOT here: the actual row markup (symbol, P&L
 * headline, meta line) and trkLabels' SL/TGT/ENTRY/CMP text assembly, because those genuinely
 * differ per engine (V8 has a basket badge and leads with rupees; TC Scanner has no basket and
 * leads with percent; V8's price formatter is px(), TC Scanner's is f2()) -- forcing those into
 * one function would just move the per-engine branching into a parameter object, not remove it.
 * Each page keeps its own trkLabels()/posRow()-equivalent, built ON these primitives.
 *
 * Site-wide script (main.py _MOBILE_HEAD, same pattern as scorr_card_strip.js), so it is present
 * on every /m/* page before that page's own inline script runs. Injects its own CSS scoped under
 * .scorr-prow so it can never compete with mobile/v8.html's existing #v8p-scoped rules (an ID
 * always outranks a class, so V8's own CSS keeps winning there unchanged) while still being the
 * one definition TC Scanner (which carries no competing #v8p-scoped rule) actually uses.
 */
(function () {
  if (window.ScorrPositionRow) return;

  /* Bare var(--token) throughout, no colour-literal fallbacks -- every token here (--hi/--edge/
     --win/--loss/--muted/--brand) is in the official scorr_themes.css contract (theme_validator.py
     check_sets), which both consuming pages already link, so a fallback would only be a second,
     silent answer to "what colour is this" (exactly what the fallback ratchet, cc#1998, exists to
     catch -- it scans .js files too). */
  var CSS = ''
    + '.scorr-prow .trk{position:relative;height:10px;margin-top:18px;margin-bottom:16px;background:var(--hi);border:1px solid var(--edge);border-radius:2px}'
    + '.scorr-prow .trk .tfill{position:absolute;top:0;bottom:0;left:0}'
    + '.scorr-prow .trk.pnl-win .tfill{background:var(--win)} .scorr-prow .trk.pnl-loss .tfill{background:var(--loss)} .scorr-prow .trk.pnl-flat .tfill{background:var(--muted)}'
    + '.scorr-prow .trk .cmp{position:absolute;top:-2px;width:3px;height:14px;background:var(--brand);z-index:2}'
    + '.scorr-prow .trk .z{position:absolute;top:-2px;width:2px;height:14px;background:var(--muted);opacity:.6;z-index:2}'
    + '.scorr-prow .trk.none{border-style:dashed;background:transparent}'
    + '.scorr-prow .trk .lbl{position:absolute;font-family:monospace;font-size:11px;letter-spacing:.3px;white-space:nowrap;z-index:3}'
    + '.scorr-prow .trk .lbl.end{bottom:-16px;color:var(--muted)}'
    + '.scorr-prow .trk .lbl.end.l{left:0} .scorr-prow .trk .lbl.end.r{right:0}'
    + '.scorr-prow .trk .lbl.tick{top:-15px}'
    + '.scorr-prow .trk .lbl.tick.entry{color:var(--muted)}'
    + '.scorr-prow .trk .lbl.tick.entry.below{top:auto;bottom:-14px}'
    + '.scorr-prow .trk .lbl.tick.cmp{color:var(--brand);width:auto;height:auto;background:none;box-shadow:none}'
    + '.scorr-prow .fn-tc{display:inline-block;font-family:monospace;font-size:10.5px;font-weight:700;padding:1px 6px;border-radius:4px;border:1px solid currentColor;background:transparent}'
    + '.scorr-prow .fn-tc-strong{color:var(--win);background:color-mix(in srgb, var(--win) 14%, transparent);font-weight:800}'
    + '.scorr-prow .fn-tc-valid{color:var(--win)}'
    + '.scorr-prow .fn-tc-watch{color:var(--muted);font-weight:700}'
    + '.scorr-prow .fn-tc-fail{color:var(--loss)}'
    + '.scorr-prow .rowstrip{display:flex;justify-content:center;margin-top:10px;padding-top:10px;border-top:1px solid var(--edge)}';
  try {
    var st = document.createElement('style');
    st.setAttribute('data-scorr', 'position-row');
    st.appendChild(document.createTextNode(CSS));
    (document.head || document.documentElement).appendChild(st);
  } catch (e) {}

  /* ── price-rail geometry (verbatim from cc#1660/cc#1726, mobile/v8.html) ──────────────── */
  function trkAxis(p) {
    var t = p.target, sl = p.stop_loss;
    if (typeof t !== 'number' || !isFinite(t) || typeof sl !== 'number' || !isFinite(sl) || t === sl) return null;
    var lo = Math.min(t, sl), hi = Math.max(t, sl);
    return { lo: lo, hi: hi, span: hi - lo };
  }
  function trkFrac(v, ax) {
    if (typeof v !== 'number' || !isFinite(v) || !ax) return null;
    return Math.max(0, Math.min(1, (v - ax.lo) / ax.span));
  }
  function trkTickStyle(f) {
    if (f <= 0.15) return 'left:0;text-align:left';
    if (f >= 0.85) return 'left:100%;transform:translateX(-100%);text-align:right';
    return 'left:' + Math.round(f * 100) + '%;transform:translateX(-50%);text-align:center';
  }
  /* cc#1726 label-collision pass, verbatim. Generic .trk/.lbl.tick.entry/.lbl.tick.cmp/.lbl.end
     selectors -- no page scoping baked in, safe to share as-is. */
  function trkFixLabels(root) {
    var GAP = 6, trks = (root || document).querySelectorAll('.trk');
    for (var i = 0; i < trks.length; i++) {
      var trk = trks[i], en = trk.querySelector('.lbl.tick.entry'), cm = trk.querySelector('.lbl.tick.cmp');
      if (!en || !cm) continue;
      var tr = trk.getBoundingClientRect(); if (!tr.width) continue;
      var box = function (el) { try { var r = document.createRange(); r.selectNodeContents(el); var b = r.getBoundingClientRect(); if (b.width) return b; } catch (e) {} return el.getBoundingClientRect(); };
      var re = box(en), rc = box(cm);
      if (!(re.right + GAP > rc.left && rc.right + GAP > re.left)) continue;
      en.classList.add('below');
      re = box(en);
      var ends = trk.querySelectorAll('.lbl.end');
      for (var j = 0; j < ends.length; j++) {
        var e = ends[j], rr = box(e);
        if (!(re.right + GAP > rr.left && rr.right + GAP > re.left)) continue;
        var left = e.classList.contains('l') ? (rr.right - tr.left + GAP) : (rr.left - tr.left - GAP - re.width);
        left = Math.max(0, Math.min(left, tr.width - re.width));
        en.style.left = Math.round(left) + 'px'; en.style.transform = 'none';
        en.style.textAlign = e.classList.contains('l') ? 'left' : 'right';
        re = box(en);
      }
    }
  }

  /* ── TC-score capsule, verbatim from cc#2099 (STRONG>=84/VALID>=65/WATCH>=50/else FAIL) ── */
  function fnTcBand(score) {
    if (score == null) return null;
    if (score >= 84) return 'strong';
    if (score >= 65) return 'valid';
    if (score >= 50) return 'watch';
    return 'fail';
  }
  function fnTcCapsule(score) {
    var band = fnTcBand(score);
    if (!band) return '<span style="font-family:monospace;font-size:12.5px;font-weight:800;color:var(--muted)">—</span>';
    return '<span class="fn-tc fn-tc-' + band + '">' + Number(score).toFixed(1) + '</span>';
  }

  /* ── C.A.R.D tap-reveal toggle, verbatim from cc#2098. Row markup contract: the row carries
     data-card-strip="<symbol>" and a trailing <div class="rowstrip" style="display:none"></div>;
     each page's own delegated click handler calls this after checking .scorr-card-strip first
     (a tap on the strip's own pill must not re-collapse it -- see mobile/v8.html's own guard). */
  function cardStripToggle(row) {
    var box = row.querySelector('.rowstrip'); if (!box) return;
    if (box.style.display !== 'none') { box.style.display = 'none'; return; }
    if (!box.firstChild) {
      if (!window.ScorrCardStripHtml) return;
      var html = window.ScorrCardStripHtml(row.getAttribute('data-card-strip'), '');
      if (!html) return;
      box.innerHTML = html;
    }
    box.style.display = '';
  }

  window.ScorrPositionRow = {
    trkAxis: trkAxis, trkFrac: trkFrac, trkTickStyle: trkTickStyle, trkFixLabels: trkFixLabels,
    fnTcBand: fnTcBand, fnTcCapsule: fnTcCapsule, cardStripToggle: cardStripToggle
  };
})();

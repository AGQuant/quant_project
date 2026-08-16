/* scrub_layer.js — cc#1061 the ONE crosshair-scrub layer
   ======================================================
   Extracted from index_tape_card.js (cc#1058) so the PCR trend panel gets the identical
   interaction instead of a second implementation. cc#1061 asked for reuse and this is it:
   the tape and the PCR lines now share every pixel of the pointer behaviour, and a fix to
   the mapping fixes both.

   WHAT IT OWNS
     • measuring the container and re-rendering at TRUE pixel width on resize (ResizeObserver,
       debounced window-resize fallback) — no CSS stretching, so dots stay round
     • the crosshair line, the point dot and the floating label, as positioned DIVs rather than
       SVG re-serialisation (a 100-point path per mousemove buys nothing)
     • mapping pointer-x to the nearest plotted point
     • flipping the label left when it would run past the right edge
     • clearing every trace on leave/cancel
     • DRAG vs CLICK: movement under CLICK_SLOP px between down and up is a click and calls
       onActivate; more is a scrub and never activates

   WHAT IT DOES NOT KNOW
   Anything about prices, dates, sessions or PCR. The caller supplies build(w,h) returning the
   SVG plus the plotted points in the SAME coordinate space, and the layer does the rest. That
   is why a point can carry y === null: the PCR panel needs a scrubbable day that has NO value
   (a guarded NULL, cc#1061), and the layer just skips the dot and shows the caller's label.

   API: window.ScorrScrub.attach(wrap, opts) -> { redraw }
     opts.h          px height
     opts.color      accent for the dot
     opts.build      (w,h) => { svg: string, pts: [{x, y|null, label}] }
     opts.onActivate optional (wrap) => void, fired on a click
*/
(function (root) {
  'use strict';
  if (root.ScorrScrub) return;   // cc#1060: double-init guard, like every other shared card file

  var CLICK_SLOP = 5;     // px of movement below which a pointer gesture counts as a click
  var MIN_W = 120;        // never render narrower than this, however cramped the column
  var CROSS = '#64748b';

  function attach(wrap, opts) {
    opts = opts || {};
    var h = opts.h || 46;
    var color = opts.color || CROSS;
    var pts = [], w = 0;

    var cross = document.createElement('div');
    cross.style.cssText = 'position:absolute;top:0;width:1px;pointer-events:none;' +
      'background:' + CROSS + ';opacity:.55;display:none';
    var dot = document.createElement('div');
    dot.style.cssText = 'position:absolute;width:7px;height:7px;border-radius:50%;' +
      'pointer-events:none;display:none;background:' + color +
      ';box-shadow:0 0 0 1.5px var(--card,#fff)';
    var lab = document.createElement('div');
    lab.style.cssText = 'position:absolute;top:-2px;pointer-events:none;display:none;' +
      'white-space:nowrap;font:700 10px/1.35 Sora,system-ui,sans-serif;' +
      'padding:3px 6px;border-radius:5px;z-index:5;' +
      'background:var(--card,#fff);color:var(--txt,#0f172a);' +
      'border:1px solid var(--line,#e2e8f0);box-shadow:0 2px 8px rgba(0,0,0,.14)';

    function hide() { cross.style.display = dot.style.display = lab.style.display = 'none'; }

    function redraw() {
      w = Math.max(MIN_W, Math.floor(wrap.clientWidth || 0));
      var out = opts.build(w, h) || {};
      wrap.innerHTML = out.svg || '';
      wrap.appendChild(cross); wrap.appendChild(dot); wrap.appendChild(lab);
      cross.style.height = h + 'px';
      pts = out.pts || [];
      hide();
    }

    function at(clientX) {
      if (!pts.length) return;
      var r = wrap.getBoundingClientRect();
      if (!r.width) return;
      /* Map through the SVG's own coordinate space. `scale` is 1 while the SVG is rendered at
         true pixel width (it is), and keeps this correct if a container ever CSS-scales it.
         Nearest-by-x rather than index arithmetic, so the caller is free to leave gaps in the
         series without the layer having to know about them. */
      var px = (clientX - r.left) * (w / r.width);
      var best = 0, bd = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].x - px);
        if (d < bd) { bd = d; best = i; }
      }
      var p = pts[best];

      cross.style.left = p.x.toFixed(1) + 'px';
      cross.style.display = 'block';
      if (p.y == null) {
        dot.style.display = 'none';        // a gap day has no point to sit on
      } else {
        dot.style.left = (p.x - 3.5).toFixed(1) + 'px';
        dot.style.top = (p.y - 3.5).toFixed(1) + 'px';
        dot.style.display = 'block';
      }
      lab.textContent = p.label == null ? '' : String(p.label);
      lab.style.display = 'block';
      // Measured after the text is set, because the width depends on the string.
      var lw = lab.offsetWidth || 0, left = p.x + 8;
      if (left + lw > w - 2) left = Math.max(0, p.x - 8 - lw);
      lab.style.left = left.toFixed(1) + 'px';
    }

    var downX = null, downY = null, moved = false;
    wrap.addEventListener('pointerdown', function (e) {
      downX = e.clientX; downY = e.clientY; moved = false; at(e.clientX);
    });
    wrap.addEventListener('pointermove', function (e) {
      if (downX !== null &&
          (Math.abs(e.clientX - downX) > CLICK_SLOP || Math.abs(e.clientY - downY) > CLICK_SLOP)) {
        moved = true;
      }
      // Desktop hovers with no button held; touch has no hover, and the same handler covers the
      // drag because pointermove fires while the finger is down.
      at(e.clientX);
    });
    wrap.addEventListener('pointerup', function () {
      var wasClick = downX !== null && !moved;
      downX = downY = null;
      if (wasClick) {
        hide();   // a tap must never leave a stuck crosshair behind whatever it opened
        if (typeof opts.onActivate === 'function') opts.onActivate(wrap);
      }
    });
    wrap.addEventListener('pointercancel', function () { downX = downY = null; hide(); });
    wrap.addEventListener('pointerleave', function () { downX = downY = null; hide(); });

    redraw();

    if (typeof ResizeObserver === 'function') {
      new ResizeObserver(function () { redraw(); }).observe(wrap);
    } else {
      var t = null;
      window.addEventListener('resize', function () {
        clearTimeout(t); t = setTimeout(redraw, 120);
      });
    }
    return { redraw: redraw };
  }

  root.ScorrScrub = { attach: attach, CLICK_SLOP: CLICK_SLOP, MIN_W: MIN_W, CROSS: CROSS };
})(window);

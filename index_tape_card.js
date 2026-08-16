/* index_tape_card.js — cc#1054 index tape · cc#1058 scrub + responsive
   ====================================================================
   Both index day-change surfaces draw from this file: the V8 dashboard's Index Intel pane
   and the standalone V10 dashboard. Not two hand-copied SVG builders that drift apart the
   first time one of them is touched (cc#1034: mirroring means sharing the renderer, not
   copying it).

   WHAT IT DRAWS
   A rolling ~100-bar 5-min cash tape, about 1.3 sessions. The CURRENT session is drawn in
   the day-change colour; everything before it is dimmed, so the eye reads today as the
   subject and yesterday's tail as context. A dashed vertical breaker sits at every session
   boundary inside the window, and a dot marks the live end.

   WHY IT IS RENDERED AT TRUE PIXEL WIDTH (cc#1058 item 3)
   The first cut used viewBox + preserveAspectRatio="none" and let CSS stretch it. That fills
   the container, but it stretches the coordinate system with it: the end-dot became an
   ellipse and the breaker's dash pattern smeared at wide widths. So the width is now MEASURED
   from the container and the SVG is drawn 1:1 into it, re-rendered on resize through a
   ResizeObserver. Circles stay circular at every width, and pointer-x maps to a bar index
   without having to undo a scale factor.

   WHAT IT DELIBERATELY DOES NOT DO
   It never computes or prints the day-change percentage. That number keeps coming from
   whatever the host page already used, and is passed in as `chgColor`. The tape got longer
   and gained a crosshair; the number must not change meaning, and not touching it is the
   only way to be sure.

   BREAKERS COME FROM THE DATA. The server returns `breaks` as array positions taken from
   ts::date transitions in the rows it actually sent. Never count 75 bars back for "yesterday"
   — 06-Aug-2026 carries 17 bars and 07-Aug carries 37. On such a window the tape spans three
   sessions and draws two breakers, which is correct.

   THE SCRUB LAYER IS DIVS, NOT SVG. Crosshair, dot and label are absolutely-positioned
   elements over the chart, moved on pointermove. Re-serialising the SVG on every mouse event
   would rebuild a 100-point path per frame for no visual gain.

   DRAG vs CLICK (the cc#1059 contract, settled here so both cards agree)
   A pointer that moves less than CLICK_SLOP px between down and up is a CLICK and calls
   opts.onActivate(tapeKey); anything more is a SCRUB and never activates. cc#1059 wires
   onActivate to the large chart popup. The hook ships here, unwired, because the interaction
   contract has to exist before the thing that depends on it.

   API: window.ScorrIndexTape
     .load(bars)                     -> Promise<payload|null>   (GET /api/index/tape)
     .placeholder(tapeKey, color)    -> string   (drop into an innerHTML template)
     .mountAll(root, payload, opts)  -> void     (call AFTER innerHTML is assigned)
     .svg(entry, color, opts)        -> string   (pure builder; used by mountAll)
     .caption(payload)               -> string
*/
(function (root) {
  'use strict';

  var DIM = '#94a3b8';          // prior-session stroke — muted, never invisible
  var BREAK = '#64748b';        // session divider
  var CLICK_SLOP = 5;           // px of movement below which a pointer gesture is a click
  var MIN_W = 120;              // never render narrower than this, however cramped the column
  var DEFAULT_H = 46;

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function _num(v) { var n = Number(v); return isFinite(n) ? n : null; }

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* "2026-08-14 13:45:00" -> "13:45", or "13 Aug 13:45" when the bar is not from the session
     the tape is headlining. The date is shown precisely so a prior-session bar can never be
     misread as today's — that is the whole point of dimming the leg. */
  function _stamp(ts, sessionDate) {
    var d = String(ts || ''), day = d.slice(0, 10), hm = d.slice(11, 16);
    if (!day) return hm;
    if (day === sessionDate) return hm;
    var m = parseInt(day.slice(5, 7), 10);
    return parseInt(day.slice(8, 10), 10) + ' ' + (MONTHS[m - 1] || '') + ' ' + hm;
  }

  function _fmt(v) {
    return Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2,
                                               maximumFractionDigits: 2 });
  }

  /* GET the tape. Returns null rather than throwing, so a dead endpoint degrades the card
     to its existing "no intraday" state instead of taking the whole dashboard render down. */
  function load(bars) {
    var url = '/api/index/tape?bars=' + (bars || 100);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  /* Geometry for one entry at a given pixel width. Shared by the SVG builder and the scrub
     layer so a crosshair can never land somewhere the line is not. */
  function _geom(entry, w, h) {
    var rows = (entry && entry.rows) || [];
    var vals = [], keep = [];
    for (var i = 0; i < rows.length; i++) {
      var v = _num(rows[i].close);
      if (v !== null) { vals.push(v); keep.push(rows[i]); }
    }
    if (vals.length < 2) return null;
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    if (mn === mx) { mn -= 1; mx += 1; }
    var pad = 4, iw = w - pad * 2, ih = h - pad * 2;
    return {
      vals: vals, rows: keep, pad: pad, w: w, h: h,
      X: function (i) { return pad + (i / (vals.length - 1)) * iw; },
      Y: function (v) { return pad + ih - ((v - mn) / (mx - mn)) * ih; },
      cut: Math.max(0, Math.min(Number(entry.session_start) || 0, vals.length - 1))
    };
  }

  /* Build one index's tape as inline SVG at an explicit pixel width. */
  function svg(entry, chgColor, opts) {
    opts = opts || {};
    var h = opts.h || DEFAULT_H;
    var w = Math.max(MIN_W, Math.round(opts.w || 320));
    var g = _geom(entry, w, h);
    if (!g) return '<div style="font-size:8px;color:var(--dim,#94a3b8)">no intraday</div>';

    var path = function (a, b) {   // inclusive slice a..b as an SVG path
      var d = '';
      for (var i = a; i <= b; i++) {
        d += (i === a ? 'M' : 'L') + g.X(i).toFixed(1) + ' ' + g.Y(g.vals[i]).toFixed(1);
        if (i < b) d += ' ';
      }
      return d;
    };

    var s = '<svg class="spark" width="' + w + '" height="' + h +
            '" viewBox="0 0 ' + w + ' ' + h + '" style="display:block">';

    if (opts.showBreaks !== false) {
      var brk = (entry.breaks || []).filter(function (i) { return i > 0 && i < g.vals.length; });
      for (var k = 0; k < brk.length; k++) {
        /* Sit the divider midway between the last bar of one session and the first of the
           next, so it reads as the gap between them and not as a bar of its own. */
        var bx = ((g.X(brk[k] - 1) + g.X(brk[k])) / 2).toFixed(1);
        s += '<line x1="' + bx + '" y1="' + g.pad + '" x2="' + bx + '" y2="' + (h - g.pad) +
             '" stroke="' + BREAK + '" stroke-width="1" stroke-dasharray="2 3" opacity="0.7"/>';
      }
    }

    if (g.cut > 0) {
      /* Prior sessions, dimmed. Drawn through index `cut` so the two paths share that point
         and the line is continuous across the session break rather than visibly snapped. */
      s += '<path d="' + path(0, g.cut) + '" fill="none" stroke="' + (opts.dim || DIM) +
           '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity="0.75"/>';
    }
    s += '<path d="' + path(g.cut, g.vals.length - 1) + '" fill="none" stroke="' + chgColor +
         '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';

    var lx = g.X(g.vals.length - 1).toFixed(1), ly = g.Y(g.vals[g.vals.length - 1]).toFixed(1);
    s += '<circle cx="' + lx + '" cy="' + ly + '" r="3.5" fill="' + chgColor +
         '" stroke="var(--card,#fff)" stroke-width="1.5"/>';
    s += '</svg>';
    return s;
  }

  /* The slot a host page drops into its innerHTML template. mountAll() finds these by
     [data-tape] afterwards and fills them — the tape cannot be built inline because its
     width is not known until the element is in the document. */
  function placeholder(tapeKey, chgColor, h) {
    return '<div class="sit-wrap" data-tape="' + _esc(tapeKey) + '" data-col="' +
           _esc(chgColor) + '" data-h="' + (h || DEFAULT_H) + '" ' +
           'style="position:relative;width:100%;min-width:0;touch-action:pan-y;' +
           'cursor:pointer;-webkit-tap-highlight-color:transparent"></div>';
  }

  /* cc#1061: the crosshair, the resize handling and the drag-vs-click contract all moved to
     window.ScorrScrub, so the PCR trend panel gets the IDENTICAL interaction instead of a second
     copy. What stays here is the only part that is about a price tape: the geometry, the label
     text, and which bars belong to the current session. */
  function _mountOne(wrap, entry, opts) {
    var color = wrap.getAttribute('data-col') || '#64748b';
    var key = wrap.getAttribute('data-tape');
    var h = parseInt(wrap.getAttribute('data-h'), 10) || DEFAULT_H;

    /* The reference for the % column: the LAST bar of the session before the headlined one.
       Bars in the current session are measured against it. Bars in a prior session are NOT
       given a % — there is no earlier close inside this window to measure them from, and a
       number with no defined reference is worse than no number. They show date, time and
       price instead. */
    var rowsAll = (entry && entry.rows) || [];
    var cut = Math.max(0, Math.min(Number(entry.session_start) || 0, rowsAll.length - 1));
    var ref = cut > 0 ? _num(rowsAll[cut - 1].close) : null;
    var sessionDate = entry.session_date;

    return root.ScorrScrub.attach(wrap, {
      h: h, color: color,
      onActivate: function () {
        if (typeof opts.onActivate === 'function') opts.onActivate(key, entry);
      },
      build: function (w, hh) {
        var g = _geom(entry, w, hh);
        if (!g) return { svg: '<div style="font-size:8px;color:var(--dim,#94a3b8)">no intraday</div>', pts: [] };
        var pts = g.rows.map(function (r, i) {
          var txt = _stamp(r.ts, sessionDate) + ' \u00b7 ' + _fmt(r.close);
          if (ref && i >= cut) {
            var pc = (Number(r.close) / ref - 1) * 100;
            txt += ' \u00b7 ' + (pc >= 0 ? '+' : '') + pc.toFixed(2) + '%';
          }
          return { x: g.X(i), y: g.Y(g.vals[i]), label: txt };
        });
        return { svg: svg(entry, color, { w: w, h: hh, dim: opts.dim }), pts: pts };
      }
    });
  }

  /* Fill every [data-tape] slot under `root`. Call AFTER the host page has assigned its
     innerHTML — the elements must be in the document for clientWidth to be real. */
  function mountAll(rootEl, payload, opts) {
    opts = opts || {};
    var host = rootEl || document;
    var slots = host.querySelectorAll('[data-tape]');
    for (var i = 0; i < slots.length; i++) {
      var wrap = slots[i];
      var key = wrap.getAttribute('data-tape');
      var entry = payload && payload.indices ? payload.indices[key] : null;
      if (!entry || !entry.rows || entry.rows.length < 2 || !root.ScorrScrub) {
        wrap.innerHTML = '<div style="font-size:8px;color:var(--dim,#94a3b8)">no intraday</div>';
        continue;
      }
      try {
        _mountOne(wrap, entry, opts);
      } catch (e) {
        // One bad tape must not take the dashboard render with it.
        wrap.innerHTML = '<div style="font-size:8px;color:var(--dim,#94a3b8)">tape unavailable</div>';
        if (window.console) console.warn('[idxtape]', key, e && e.message);
      }
    }
  }

  /* Sub-label for the card: how much tape is on screen and how many sessions it covers.
     States the real bar count rather than the requested one — a short feed day must not be
     described as 100 bars. */
  function caption(payload) {
    if (!payload || payload.status !== 'ok') return '';
    var any = payload.indices && payload.indices.NIFTY50;
    var ns = (any && any.sessions ? any.sessions.length : 0);
    return payload.bars + ' bars · 5-min · ' + ns + (ns === 1 ? ' session' : ' sessions');
  }

  root.ScorrIndexTape = {
    load: load, svg: svg, caption: caption,
    placeholder: placeholder, mountAll: mountAll,
    DIM: DIM, BREAK: BREAK, CLICK_SLOP: CLICK_SLOP
  };
})(window);

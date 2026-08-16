/* index_tape_card.js — cc#1054 INDEX 100-BAR TAPE, the ONE renderer
   =================================================================
   Both index day-change surfaces draw from this file: the V8 dashboard's Index Intel pane
   and the standalone V10 dashboard. Not two hand-copied SVG builders that drift apart the
   first time one of them is touched (cc#1034: mirroring means sharing the renderer, not
   copying it).

   WHAT IT DRAWS
   A rolling ~100-bar 5-min cash tape, about 1.3 sessions. The CURRENT session is drawn in
   the day-change colour; everything before it is dimmed, so the eye reads today as the
   subject and yesterday's tail as context. A dashed vertical breaker sits at every session
   boundary inside the window, and a dot marks the live end.

   WHAT IT DELIBERATELY DOES NOT DO
   It never computes or prints the day-change percentage. That number keeps coming from
   whatever the host page already used, and is passed in as `chgColor`. The tape got longer;
   the number must not change meaning, and not touching it is the only way to be sure.

   BREAKERS COME FROM THE DATA. The server returns `breaks` as array positions taken from
   ts::date transitions in the rows it actually sent. Never count 75 bars back for "yesterday"
   — 06-Aug-2026 carries 17 bars and 07-Aug carries 37. On such a window the tape spans three
   sessions and draws two breakers, which is correct.

   API: window.ScorrIndexTape
     .load(bars)                      -> Promise<payload|null>   (GET /api/index/tape)
     .svg(entry, chgColor, opts)      -> string                  (one index's tape)
   `entry` is one value out of payload.indices, i.e. {rows, breaks, session_start, ...}. */
(function (root) {
  'use strict';

  var DIM = '#94a3b8';          // prior-session stroke — muted, never invisible
  var BREAK = '#64748b';        // session divider

  function _num(v) { var n = Number(v); return isFinite(n) ? n : null; }

  /* GET the tape. Returns null rather than throwing, so a dead endpoint degrades the card
     to its existing "no intraday" state instead of taking the whole dashboard render down. */
  function load(bars) {
    var url = '/api/index/tape?bars=' + (bars || 100);
    return fetch(url, { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  /* Build one index's tape as inline SVG.
       entry     — {rows:[{ts,close}], breaks:[i], session_start:i}
       chgColor  — the host page's day-change colour; the current session is drawn in it
       opts      — {w,h,dim,showBreaks} */
  function svg(entry, chgColor, opts) {
    opts = opts || {};
    var w = opts.w || 320, h = opts.h || 46, pad = 4;
    var rows = (entry && entry.rows) || [];
    var vals = rows.map(function (r) { return _num(r.close); })
                   .filter(function (v) { return v !== null; });
    if (vals.length < 2) {
      return '<div style="font-size:8px;color:var(--dim,#94a3b8)">no intraday</div>';
    }

    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    if (mn === mx) { mn -= 1; mx += 1; }
    var iw = w - pad * 2, ih = h - pad * 2;
    var X = function (i) { return pad + (i / (vals.length - 1)) * iw; };
    var Y = function (v) { return pad + ih - ((v - mn) / (mx - mn)) * ih; };

    /* The split point is clamped into range: a window that happens to hold only the current
       session has session_start 0, and then the whole tape is live-coloured with no dim leg
       and no breaker — the pre-cc#1054 look, reached honestly rather than special-cased. */
    var cut = Math.max(0, Math.min(Number(entry.session_start) || 0, vals.length - 1));

    var path = function (a, b) {   // inclusive slice a..b as an SVG path
      var d = '';
      for (var i = a; i <= b; i++) {
        d += (i === a ? 'M' : 'L') + X(i).toFixed(1) + ' ' + Y(vals[i]).toFixed(1);
        if (i < b) d += ' ';
      }
      return d;
    };

    var s = '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">';

    if (opts.showBreaks !== false) {
      var brk = (entry.breaks || []).filter(function (i) { return i > 0 && i < vals.length; });
      for (var k = 0; k < brk.length; k++) {
        /* Sit the divider midway between the last bar of one session and the first of the
           next, so it reads as the gap between them and not as a bar of its own. */
        var bx = ((X(brk[k] - 1) + X(brk[k])) / 2).toFixed(1);
        s += '<line x1="' + bx + '" y1="' + pad + '" x2="' + bx + '" y2="' + (h - pad) +
             '" stroke="' + BREAK + '" stroke-width="1" stroke-dasharray="2 3" opacity="0.7"/>';
      }
    }

    if (cut > 0) {
      /* Prior sessions, dimmed. Drawn through index `cut` so the two paths share that point
         and the line is continuous across the session break rather than visibly snapped. */
      s += '<path d="' + path(0, cut) + '" fill="none" stroke="' + (opts.dim || DIM) +
           '" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" opacity="0.75"/>';
    }
    s += '<path d="' + path(cut, vals.length - 1) + '" fill="none" stroke="' + chgColor +
         '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';

    var lx = X(vals.length - 1).toFixed(1), ly = Y(vals[vals.length - 1]).toFixed(1);
    s += '<circle cx="' + lx + '" cy="' + ly + '" r="3.5" fill="' + chgColor +
         '" stroke="var(--card,#fff)" stroke-width="1.5"/>';
    s += '</svg>';
    return s;
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

  root.ScorrIndexTape = { load: load, svg: svg, caption: caption, DIM: DIM, BREAK: BREAK };
})(window);

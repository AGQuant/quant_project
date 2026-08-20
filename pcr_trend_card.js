/* pcr_trend_card.js — cc#1061 PCR trend line: honest gaps + scrub labels
   ======================================================================
   The 7-day NIFTY / BANKNIFTY PCR lines on the Index Intel pane.

   THE BUG THIS CARD STARTED FROM, and why the renderer is part of the fix rather than a
   cosmetic follow-up: the deep V in the NIFTY line on 12-Aug was pcr 0.000 — a put leg that
   had collapsed to 5,265 against 53,179,620 calls, charted as if the market had moved. cc#745's
   detector had already marked that row `suspect`; nothing read the flag, so it drew anyway.
   The data is fixed (pcr_guard.py + the restate), but a NULL alone is not enough here:

       Number(null) === 0

   so a null-unaware renderer turns the corrected NULL straight back into the same fake 0.000.
   That is why parsing lives in this file and returns null, never a number, for a missing day.

   GAPS ARE DRAWN AS GAPS. The line BREAKS across a null day — no point, no interpolation, no
   zero. A missing reading must look missing, because the alternative is a chart that quietly
   invents a market event.

   SCRUB comes from the shared window.ScorrScrub layer (extracted from the cc#1058 index tape),
   so both charts have identical pointer behaviour and one place to fix it. Scrubbing a null day
   shows the date and "no data" — never 0.00.

   API: window.ScorrPcrTrend
     .parse(resp)                 -> {days:[{date, pcr|null}], cur|null, chg|null, n}
     .placeholder(key, color)     -> string  (drop into an innerHTML template)
     .mountAll(root, series)      -> void    (call AFTER innerHTML is assigned)
*/
(function (root) {
  'use strict';
  if (root.ScorrPcrTrend) return;   // cc#1060: double-init guard

  var DEFAULT_H = 55;
  var GAP = '#94a3b8';
  var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c];
    });
  }

  /* null for anything that is not a real reading. Number('') and Number(null) are both 0, which
     is exactly the trap this card exists to close, so the check is explicit rather than a cast. */
  function _val(v) {
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function _day(d) {
    var s = String(d || '');
    if (s.length < 10) return s;
    return parseInt(s.slice(8, 10), 10) + ' ' + (MONTHS[parseInt(s.slice(5, 7), 10) - 1] || '');
  }

  /* /api/daily/pcr returns newest-first. Oldest-first is the chart order.
     `cur` is the latest day that actually HAS a value — a null last day must not blank the
     headline number, and must not silently borrow the previous day's identity either, so the
     caller is told which date `cur` came from. */
  function parse(resp) {
    var rows = (resp && resp.data) || (Array.isArray(resp) ? resp : []);
    var days = rows.slice().reverse().slice(-8).map(function (r) {
      return { date: r.price_date || r.date || '', pcr: _val(r.pcr) };
    });
    var cur = null, curDate = null, prev = null;
    for (var i = days.length - 1; i >= 0; i--) {
      if (days[i].pcr === null) continue;
      if (cur === null) { cur = days[i].pcr; curDate = days[i].date; }
      else { prev = days[i].pcr; break; }
    }
    return {
      days: days, n: days.length,
      cur: cur, cur_date: curDate,
      chg: (cur !== null && prev !== null) ? (cur - prev) : null,
      gaps: days.filter(function (d) { return d.pcr === null; }).length
    };
  }

  /* SVG + the point list the scrub layer maps against. Nulls break the path and carry a point
     with y === null so the day is still scrubbable. */
  function _build(series, color, w, h) {
    var days = (series && series.days) || [];
    var vals = days.map(function (d) { return d.pcr; });
    var real = vals.filter(function (v) { return v !== null; });
    if (real.length < 1) {
      return { svg: '<div style="font-size:8px;color:var(--dim,#94a3b8)">no data</div>', pts: [] };
    }
    var mn = Math.min.apply(null, real), mx = Math.max.apply(null, real);
    mn = Math.min(mn, 1.0); mx = Math.max(mx, 1.0);      // keep the 1.0 reference line in frame
    if (mn === mx) { mn -= 0.1; mx += 0.1; }
    var pad = 4, iw = w - pad * 2, ih = h - pad * 2;
    var n = vals.length;
    var X = function (i) { return pad + (n === 1 ? iw / 2 : (i / (n - 1)) * iw); };
    var Y = function (v) { return pad + ih - ((v - mn) / (mx - mn)) * ih; };

    var s = '<svg class="spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h +
            '" style="display:block">';
    var ry = Y(1.0).toFixed(1);
    s += '<line x1="' + pad + '" y1="' + ry + '" x2="' + (w - pad) + '" y2="' + ry +
         '" stroke="' + GAP + '" stroke-width="1" stroke-dasharray="3 2" opacity=".5"/>';
    /* cc#1168 push 2: label the reference line, per the index_intel_R2 PCR card. The dashed line
       has always been at PCR 1.00 and a reader had to know that; the ref prints the number on it.
       Styled with attributes rather than a class because this module is consumed by two different
       pages and only one of them declares .axis-t — a class here would render correctly on one
       page and invisibly on the other. Placed just ABOVE the line and clear of the left padding,
       and pointer-events:none so it can never take a scrub touch away from the chart. */
    /* The label flips to the UNDERSIDE when the line is near the top. mn/mx are clamped to keep
       1.00 in frame, so on a series that sits entirely below 1.00 the reference line pins to the
       very top of the chart — and a label above it then lands on the meta text in the card above.
       Measured, not predicted: it collided with "7-day 0.68-0.79" at 1280 and with the no-data
       note as well at 390. */
    var _ly = Number(ry) < 12 ? (Number(ry) + 9) : (Number(ry) - 2.5);
    s += '<text x="' + (pad + 1) + '" y="' + _ly.toFixed(1) +
         '" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="8.5" ' +
         'fill="' + GAP + '" opacity=".85" style="pointer-events:none">1.00</text>';

    // One path per unbroken run. A null ends the current run — the line does not bridge it.
    var run = [], paths = [];
    for (var i = 0; i < n; i++) {
      if (vals[i] === null) { if (run.length) { paths.push(run); run = []; } continue; }
      run.push(i);
    }
    if (run.length) paths.push(run);
    paths.forEach(function (idx) {
      if (idx.length === 1) {
        // A lone reading between two gaps still has to be visible; a zero-length path is not.
        s += '<circle cx="' + X(idx[0]).toFixed(1) + '" cy="' + Y(vals[idx[0]]).toFixed(1) +
             '" r="2" fill="' + color + '"/>';
        return;
      }
      var d = idx.map(function (i2, k) {
        return (k === 0 ? 'M' : 'L') + X(i2).toFixed(1) + ' ' + Y(vals[i2]).toFixed(1);
      }).join(' ');
      s += '<path d="' + d + '" fill="none" stroke="' + color +
           '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
    });

    // Hollow marker on each gap day, on the reference line — the eye should be able to SEE that
    // a day is missing without scrubbing for it.
    for (var g = 0; g < n; g++) {
      if (vals[g] !== null) continue;
      s += '<circle cx="' + X(g).toFixed(1) + '" cy="' + ry + '" r="2.5" fill="none" stroke="' +
           GAP + '" stroke-width="1.2" stroke-dasharray="1.5 1.5"/>';
    }

    // Live dot on the most recent day that has a value.
    for (var L = n - 1; L >= 0; L--) {
      if (vals[L] === null) continue;
      s += '<circle cx="' + X(L).toFixed(1) + '" cy="' + Y(vals[L]).toFixed(1) +
           '" r="3.5" fill="' + color + '" stroke="var(--card,#fff)" stroke-width="1.5"/>';
      break;
    }
    s += '</svg>';

    var pts = days.map(function (d, i) {
      var lbl;
      if (d.pcr === null) {
        lbl = _day(d.date) + ' · no data';          // cc#1061 item 5 — never 0.00
      } else {
        lbl = _day(d.date) + ' · ' + d.pcr.toFixed(3);
        // change vs the previous day that HAS a value, so a gap does not fabricate a jump
        for (var k = i - 1; k >= 0; k--) {
          if (days[k].pcr === null) continue;
          var ch = d.pcr - days[k].pcr;
          lbl += ' · ' + (ch >= 0 ? '+' : '') + ch.toFixed(3);
          break;
        }
      }
      return { x: X(i), y: d.pcr === null ? null : Y(d.pcr), label: lbl };
    });
    return { svg: s, pts: pts };
  }

  function placeholder(key, color, h) {
    return '<div class="pcr-wrap" data-pcr="' + _esc(key) + '" data-col="' + _esc(color) +
           '" data-h="' + (h || DEFAULT_H) + '" ' +
           'style="position:relative;width:100%;min-width:0;touch-action:pan-y;' +
           '-webkit-tap-highlight-color:transparent"></div>';
  }

  /* Fill every [data-pcr] slot under `root`. `series` is {key: parse(resp)}. */
  function mountAll(rootEl, series) {
    var host = rootEl || document;
    var slots = host.querySelectorAll('[data-pcr]');
    for (var i = 0; i < slots.length; i++) {
      var wrap = slots[i];
      var key = wrap.getAttribute('data-pcr');
      var color = wrap.getAttribute('data-col') || GAP;
      var h = parseInt(wrap.getAttribute('data-h'), 10) || DEFAULT_H;
      var s = series && series[key];
      if (!s || !s.days || !s.days.length || !root.ScorrScrub) {
        wrap.innerHTML = '<div style="font-size:8px;color:var(--dim,#94a3b8)">no data</div>';
        continue;
      }
      try {
        (function (sv, cv) {
          root.ScorrScrub.attach(wrap, {
            h: h, color: cv,
            build: function (w, hh) { return _build(sv, cv, w, hh); }
          });
        })(s, color);
      } catch (e) {
        wrap.innerHTML = '<div style="font-size:8px;color:var(--dim,#94a3b8)">chart unavailable</div>';
        if (root.console) console.warn('[pcrtrend]', key, e && e.message);
      }
    }
  }

  root.ScorrPcrTrend = { parse: parse, placeholder: placeholder, mountAll: mountAll, DEFAULT_H: DEFAULT_H };
})(window);

/* v8_ladder_v2.js — cc#1021 LADDER_VIEW_V2 (founder-approved mock, 15-Aug-2026)
 *
 * WHAT WAS WRONG WITH V1
 * The v1 rail was one flat bar from SL to target with a CMP dot on it. Entry was nowhere on the
 * rail, so a position half a percent under its entry and a position in deep drawdown drew the
 * IDENTICAL picture — the eye could not find the one number that says whether the trade is
 * working. V2 puts entry on the rail and splits it into three zones, so the shape of the row
 * answers "am I above or below my entry, and how much room is left either way" without reading
 * a single digit.
 *
 * THE THREE ZONES (roles, not geometry — which is what makes SHORT work for free)
 *   A  SL .. the nearer of (entry, CMP)     the loss side. Light red, always.
 *   B  entry .. CMP                          the move so far. Green when favourable, coral when not.
 *   C  the farther of (entry, CMP) .. target  the room left. Pale green when favourable, grey when not.
 * "Favourable" is price-vs-entry in the position's own direction: CMP >= entry for a long, CMP <=
 * entry for a short. Defining the zones by ROLE means the short case is the same code, not a
 * mirrored copy that can drift — for a short the rail simply runs target(left) .. SL(right),
 * because the mapping is by price and a short's SL sits above its target.
 *
 * NUMBERS ON THE ROW (item 3)
 * Under the rail: SL and its distance from CMP on the left, target and its distance on the right,
 * CMP in the middle. In the info column: R:R LEFT = room to target divided by room to SL, from
 * HERE — not from entry. That is the number that decides whether to hold, and it is the one v1
 * made you compute in your head. When CMP has already crossed the SL (long) or risen above it
 * (short) the ratio has no meaning, so it renders as an em dash rather than a negative or an
 * Infinity.
 *
 * PRICES ARE SERVED, NEVER DERIVED (item 7)
 * Every price here is printed exactly as the payload gave it. Since cc#1019 the book's entry_price
 * IS the futures fill price, so there is no conversion to do and none is done — no EQ fallback
 * maths, no reconstructing an entry out of SL and target. If a price is missing the row says so by
 * omission; it never invents one.
 *
 * HOW IT IS CALLED
 *   ScorrLadderV2.header(rows, fns)  -> the aggregate strip (net P&L, potential left, W/L chip)
 *   ScorrLadderV2.row(x, fns)        -> one position row
 * `fns` carries the dashboard's own formatters (num, inr, sign, cls, pnlCls, fmtTs, basketPill,
 * posTags). They are PASSED IN rather than reached for: the dashboard declares several of them
 * with `const` at script top level, and a top-level const in a classic script is NOT on window —
 * the cc#871 bug that made every KPI read zero. This module never touches globals it did not get.
 */
(function () {
  if (window.ScorrLadderV2) return;   // same double-init guard the shared card files use

  var ZONE = {
    loss:  'rgba(255,92,108,.20)',    // A — SL side, always the loss side
    gain:  'rgba(47,212,139,.30)',    // B — moved your way
    down:  'rgba(255,92,108,.34)',    // B — moved against you
    room:  'rgba(47,212,139,.12)',    // C — room left, while ahead
    flat:  'rgba(148,166,210,.14)',   // C — room left, while behind
    track: 'var(--line2)'
  };

  function n(v) { var f = Number(v); return isFinite(f) ? f : null; }

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* Distance from CMP to a level, as a percent of CMP, unsigned. The direction is already told by
     which side of the rail the number sits on, so a minus sign here would only add noise. */
  function distPct(cmp, level) {
    if (cmp == null || level == null || !cmp) return null;
    return Math.abs(level - cmp) / cmp * 100;
  }

  /* Remaining reward-to-risk, measured from CMP. Long: (target-CMP)/(CMP-SL). Short: inverted.
     Returns null when the risk leg is zero or already blown — an Infinity or a negative ratio is
     not a smaller number, it is a different situation, and it must not be printed as if it were
     a ratio. */
  function rrLeft(isShort, cmp, tgt, sl) {
    if (cmp == null || tgt == null || sl == null) return null;
    var reward = isShort ? (cmp - tgt) : (tgt - cmp);
    var risk = isShort ? (sl - cmp) : (cmp - sl);
    if (!(risk > 0)) return null;      // CMP at or through the stop: no risk leg left to divide by
    if (!(reward > 0)) return 0;       // target already reached or passed: no reward left, but real
    return reward / risk;
  }

  /* Price -> percent along the rail. The rail always runs low price (left) to high price (right),
     so a long reads SL..target and a short reads target..SL. */
  function scaler(lo, hi) {
    return function (p) {
      if (p == null || !isFinite(hi - lo) || hi === lo) return null;
      var v = (p - lo) / (hi - lo) * 100;
      return Math.max(0, Math.min(100, v));
    };
  }

  function band(pos, a, b, colour) {
    if (a == null || b == null) return '';
    var l = Math.min(a, b), w = Math.abs(b - a);
    if (!(w > 0.15)) return '';        // sub-pixel slivers just muddy the rail
    return '<div style="position:absolute;top:50%;left:' + l.toFixed(2) + '%;width:' + w.toFixed(2)
      + '%;height:8px;transform:translateY(-50%);background:' + colour + '"></div>';
  }

  function row(x, f) {
    f = f || {};
    var num = f.num || function (v, d) { return v == null ? '--' : Number(v).toFixed(d == null ? 1 : d); };
    var inr = f.inr || function (v) { return v == null ? '--' : String(v); };
    var sign = f.sign || function (v, d) { return (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(d); };
    var pnlCls = f.pnlCls || function () { return ''; };
    var cls = f.cls || function () { return ''; };
    var fmtTs = f.fmtTs || function (t) { return String(t || ''); };
    var basketPill = f.basketPill || function () { return ''; };
    var posTags = f.posTags || function () { return ''; };

    var isShort = !!x.isShort || String(x.side || '').toUpperCase() === 'SHORT';
    var cmp = n(x.cmp), entry = n(x.entry_price), tgt = n(x.target), sl = n(x.stop_loss);
    var netPnl = n(x.net_pnl);

    var lo = Math.min(sl == null ? Infinity : sl, tgt == null ? Infinity : tgt);
    var hi = Math.max(sl == null ? -Infinity : sl, tgt == null ? -Infinity : tgt);
    var pos = scaler(lo, hi);

    // favourable = price has moved the way the position wanted. Same test the zone colours and the
    // header W/L chip use, so one row can never disagree with the strip above it.
    var fav = (cmp != null && entry != null) ? (isShort ? cmp <= entry : cmp >= entry) : null;
    var nearSL = (entry == null || cmp == null) ? null : (isShort ? Math.max(entry, cmp) : Math.min(entry, cmp));
    var farSL = (entry == null || cmp == null) ? null : (isShort ? Math.min(entry, cmp) : Math.max(entry, cmp));

    var pSL = pos(sl), pT = pos(tgt), pC = pos(cmp), pE = pos(entry);
    var zones =
      band(pos, pSL, pos(nearSL), ZONE.loss) +
      band(pos, pE, pC, fav ? ZONE.gain : ZONE.down) +
      band(pos, pos(farSL), pT, fav ? ZONE.room : ZONE.flat);

    var dotClr = (netPnl != null && netPnl >= 0) ? 'var(--grn)' : 'var(--red)';
    var entryMark = pE == null ? '' :
      // triangle at entry — a shape, not a colour, so it stays readable on every zone tint
      '<div title="Entry ' + num(entry) + '" style="position:absolute;left:' + pE.toFixed(2)
      + '%;top:calc(50% - 11px);transform:translateX(-50%);width:0;height:0;border-left:5px solid transparent;'
      + 'border-right:5px solid transparent;border-top:7px solid var(--txt);opacity:.85"></div>';
    var tgtMark = pT == null ? '' :
      '<div title="Target ' + num(tgt) + '" style="position:absolute;left:' + pT.toFixed(2)
      + '%;top:50%;width:2px;height:16px;transform:translate(-50%,-50%);background:var(--grn);opacity:.85"></div>';
    var cmpMark = pC == null ? '' :
      '<div style="position:absolute;left:' + pC.toFixed(2) + '%;top:50%;width:11px;height:11px;border-radius:50%;'
      + 'transform:translate(-50%,-50%);background:' + dotClr + ';box-shadow:0 0 0 2px var(--panel)"></div>';

    var rr = rrLeft(isShort, cmp, tgt, sl);
    var rrTxt = rr == null ? '&mdash;' : (rr.toFixed(1) + ' left');
    var rrTitle = rr == null
      ? 'No reward-to-risk left to state: CMP is at or through the stop, so the risk leg is zero or negative.'
      : 'Remaining reward-to-risk from CMP, not from entry: room to target / room to stop.';

    var dSL = distPct(cmp, sl), dT = distPct(cmp, tgt);
    var accent = isShort ? 'var(--red)' : 'var(--grn)';
    var badgeBg = isShort ? 'var(--red-d)' : 'var(--grn-d)';
    var badgeFg = isShort ? 'var(--red)' : 'var(--grn)';
    var mono = "font-family:'Sora',sans-serif;font-variant-numeric:tabular-nums";

    // Left rail label is whichever level sits at the low end of the price axis: SL for a long,
    // target for a short. The readouts below name themselves, so the two can never be confused.
    var leftIsSL = !isShort;

    return '<div style="display:flex;align-items:center;gap:20px;background:var(--panel);border:1px solid var(--line);'
      + 'border-left:4px solid ' + accent + ';border-radius:5px;padding:12px 18px">'
      + '<div style="width:210px;flex-shrink:0">'
      +   '<div style="display:flex;align-items:center;gap:8px;margin-bottom:5px">'
      +     '<span class="dc-link" style="font-size:15px;font-weight:700;letter-spacing:.04em" onclick="openQuickActions(event,\''
      +       esc(x.symbol) + '\',\'' + esc(x.side || '') + '\',' + (Number(x.qty) || 0) + ','
      +       (n(x.entry_price) == null ? 'null' : Number(x.entry_price)) + ','
      +       (cmp == null ? 'null' : cmp) + ')">' + esc(x.symbol) + '</span>' + posTags(x)
      +     '<span style="font-size:9px;font-weight:700;letter-spacing:.09em;padding:2px 6px;border-radius:3px;background:'
      +       badgeBg + ';color:' + badgeFg + '">' + esc(x.side || '') + '</span></div>'
      +   '<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:5px">'
      +     basketPill(x.basket) + '<span style="font-size:10px;color:var(--mut)">' + fmtTs(x.entry_ts) + '</span>'
      +     '<span style="font-size:10px;color:var(--mut);' + mono + '">D' + (x.days == null ? '--' : x.days) + '</span></div>'
      +   '<div style="display:flex;align-items:baseline;gap:8px">'
      +     '<span style="font-size:13px;font-weight:700;' + mono + '" class="' + pnlCls(netPnl) + '">' + inr(netPnl) + '</span>'
      +     '<span title="' + rrTitle + '" style="font-size:10px;color:var(--mut);' + mono + '">R:R ' + rrTxt + '</span></div>'
      + '</div>'
      + '<div style="flex:1;min-width:0">'
      +   '<div style="position:relative;height:30px">'
      +     '<div style="position:absolute;top:50%;left:0;right:0;height:8px;transform:translateY(-50%);background:'
      +       ZONE.track + ';border-radius:4px"></div>'
      +     zones + entryMark + tgtMark + cmpMark
      +   '</div>'
      +   '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-size:10px;color:var(--mut);'
      +     mono + ';margin-top:2px">'
      +     '<span>' + (leftIsSL ? 'SL ' : 'TGT ') + num(leftIsSL ? sl : tgt)
      +       (((leftIsSL ? dSL : dT) == null) ? '' : ' <span style="color:var(--dim)">' + (leftIsSL ? dSL : dT).toFixed(1) + '% away</span>') + '</span>'
      +     '<span style="color:var(--txt);font-weight:700">' + num(cmp) + '</span>'
      +     '<span>' + (leftIsSL ? 'TGT ' : 'SL ') + num(leftIsSL ? tgt : sl)
      +       (((leftIsSL ? dT : dSL) == null) ? '' : ' <span style="color:var(--dim)">' + (leftIsSL ? dT : dSL).toFixed(1) + '% away</span>') + '</span>'
      +   '</div>'
      + '</div></div>';
  }

  /* The aggregate strip. NET P&L and POTENTIAL LEFT are unchanged from v1; the W/L chip is new
     (item 5) and counts positions whose price has moved their way — the SAME `fav` test the zone
     colours use, so the chip and the rails always tell one story. It is not net of brokerage: a
     cost you have not paid twice is not what makes a position a winner, and the NET P&L figure
     beside it already carries that deduction. */
  function header(rows, f) {
    f = f || {};
    var inr = f.inr || function (v) { return String(v); };
    var pnlCls = f.pnlCls || function () { return ''; };
    rows = rows || [];
    var net = 0, pot = 0, w = 0, l = 0;
    for (var i = 0; i < rows.length; i++) {
      var x = rows[i];
      net += Number(x.net_pnl || 0);
      pot += Number(x.potential_left || 0);
      var isShort = !!x.isShort || String(x.side || '').toUpperCase() === 'SHORT';
      var cmp = n(x.cmp), entry = n(x.entry_price);
      if (cmp == null || entry == null) continue;   // unpriced rows are counted neither way
      if (isShort ? cmp <= entry : cmp >= entry) w++; else l++;
    }
    var mono = "font-family:'Sora',sans-serif;font-variant-numeric:tabular-nums";
    var chip = '<span title="Positions whose price has moved their way (CMP vs entry) — the same test the rail zones use."'
      + ' style="display:inline-flex;align-items:baseline;gap:4px;font-size:12px;font-weight:700;' + mono
      + ';border:1px solid var(--line2);border-radius:6px;padding:3px 8px">'
      + '<span style="color:var(--grn)">' + w + 'W</span><span style="color:var(--dim)">&ndash;</span>'
      + '<span style="color:var(--red)">' + l + 'L</span></span>';
    return '<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px">'
      + '<div style="display:flex;gap:28px;align-items:flex-end">'
      +   '<div><div style="font-size:10px;color:var(--dim)">NET P&L</div>'
      +     '<div style="font-size:22px;font-weight:700;' + mono + '" class="' + pnlCls(net) + '">' + inr(net) + '</div></div>'
      +   '<div><div style="font-size:10px;color:var(--dim)">POTENTIAL LEFT</div>'
      +     '<div style="font-size:22px;font-weight:700;' + mono + ';color:var(--mut)">' + inr(pot) + '</div></div>'
      +   '<div style="padding-bottom:3px">' + chip + '</div>'
      + '</div>'
      + '<div style="text-align:right;font-size:10px;color:var(--dim);' + mono + '">' + rows.length
      +   ' position' + (rows.length === 1 ? '' : 's') + ' &middot; sorted by Net P&L</div></div>';
  }

  window.ScorrLadderV2 = { row: row, header: header, rrLeft: rrLeft, version: 'v2' };
})();

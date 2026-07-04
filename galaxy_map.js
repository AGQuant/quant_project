/* galaxy_map.js — Scorr reusable constellation map (cc#196)
 * ============================================================================
 * A GENERIC, canvas-2D "galaxy" renderer. It knows nothing about articles or
 * stocks — you hand it a config and it draws category constellations of glowing
 * star nodes with thin intra-cluster links, pan/zoom, tap, and filter-dimming.
 *
 *   GalaxyMap.create(container, {
 *     nodes:      [{ id, label, category, tier:'beginner'|'pro', size?, payload? }],
 *     categories: [{ name, color }],           // per-category hull tint + label
 *     cacheKey:   'knowledge',                  // localStorage position cache namespace
 *     layout:     'force' | 'honeycomb',        // default 'force'; honeycomb = low-end fallback
 *   }, {
 *     onTap:         node => {},                // node.payload passed through
 *     onCategoryTap: name => {},
 *   })  ->  instance { setDim(fn), resetView(), zoomToCategory(name), resize(), destroy() }
 *
 * Instance #1 = Knowledge Hub (articles). Instance #2 (planned) = Quant Basket
 * builder universe (stocks). No instance-specific logic lives here.
 */
(function () {
  "use strict";

  var TIER = {
    beginner: { r: 3.4, core: "#34d399", glow: "rgba(52,211,153,0.55)" },
    pro:      { r: 5.2, core: "#fbbf24", glow: "rgba(251,191,36,0.55)" },
  };
  var CAT_FALLBACK = ["#60a5fa","#f472b6","#34d399","#fbbf24","#a78bfa",
                      "#22d3ee","#fb7185","#c084fc","#4ade80","#facc15"];

  function create(container, config, opts) {
    opts = opts || {};
    var nodes = (config.nodes || []).map(function (n, i) {
      return {
        id: n.id, label: n.label || "", category: n.category || "",
        tier: (n.tier === "pro" ? "pro" : "beginner"),
        size: n.size, sublabel: n.sublabel || "", payload: n.payload, _i: i,
        x: 0, y: 0, vx: 0, vy: 0, dim: false,
      };
    });
    var cats = (config.categories || []).map(function (c, i) {
      return { name: c.name, color: c.color || CAT_FALLBACK[i % CAT_FALLBACK.length] };
    });
    var catColor = {}; cats.forEach(function (c) { catColor[c.name] = c.color; });
    var byCat = {}; cats.forEach(function (c) { byCat[c.name] = []; });
    nodes.forEach(function (n) { (byCat[n.category] || (byCat[n.category] = [])).push(n); });

    // ── canvas ───────────────────────────────────────────────────────────────
    var canvas = document.createElement("canvas");
    canvas.style.cssText = "display:block;width:100%;height:100%;touch-action:none;cursor:grab";
    container.appendChild(canvas);
    var ctx = canvas.getContext("2d");
    var DPR = Math.min(window.devicePixelRatio || 1, 2);
    var W = 0, H = 0;

    // world transform
    var view = { s: 1, tx: 0, ty: 0 };
    var links = [];
    var hover = null;
    var raf = null, dirty = true;

    // ── layout ────────────────────────────────────────────────────────────────
    var LKEY = "galaxy_pos_" + (config.cacheKey || "default") + "_v2_" + nodes.length;

    function ringR() { return 200 + nodes.length * 3.2; }
    function catCenters() {
      // arrange category centers evenly on a ring (well separated so hulls +
      // labels don't overlap); radius grows with the node count.
      var centers = {}, n = cats.length || 1, R = ringR();
      cats.forEach(function (c, i) {
        var a = (i / n) * Math.PI * 2 - Math.PI / 2;
        centers[c.name] = { x: Math.cos(a) * R, y: Math.sin(a) * R };
      });
      return centers;
    }

    function honeycomb() {
      // deterministic hex-grid per category cluster — the low-end fallback
      var centers = catCenters();
      Object.keys(byCat).forEach(function (cat) {
        var arr = byCat[cat], c = centers[cat] || { x: 0, y: 0 }, step = 26;
        arr.forEach(function (nd, k) {
          var ring = Math.floor((Math.sqrt(12 * k + 1) - 1) / 6) || 0;
          var ang = (k * 2.399963); // golden angle → phyllotaxis spiral (stable, dense)
          var rad = step * Math.sqrt(k);
          nd.x = c.x + Math.cos(ang) * rad;
          nd.y = c.y + Math.sin(ang) * rad;
        });
      });
    }

    function simulate() {
      // one-time force sim: strong gravity to the category centre keeps each
      // cluster a tight blob; repulsion is OVERLAP-ONLY (push apart just when two
      // nodes are within `min`) so there is no long-range instability that flings
      // outliers out and blows up the view. Velocity + radius clamps keep it sane.
      var centers = catCenters(), RMAX = ringR() * 1.9;
      nodes.forEach(function (nd) {
        var c = centers[nd.category] || { x: 0, y: 0 };
        nd.x = c.x + (hash(nd.id) % 60) - 30;
        nd.y = c.y + (hash(nd.id + "y") % 60) - 30;
        nd.vx = 0; nd.vy = 0;
      });
      var iters = 260;
      for (var it = 0; it < iters; it++) {
        var k = 1 - it / iters;
        for (var a = 0; a < nodes.length; a++) {
          var na = nodes[a], c = centers[na.category] || { x: 0, y: 0 };
          na.vx += (c.x - na.x) * 0.03;
          na.vy += (c.y - na.y) * 0.03;
          for (var b = a + 1; b < nodes.length; b++) {
            var nb = nodes[b], dx = na.x - nb.x, dy = na.y - nb.y;
            var d = Math.sqrt(dx * dx + dy * dy) || 0.01;
            var min = (na.category === nb.category) ? 20 : 46;
            if (d < min) {
              var f = (min - d) * 0.5, ux = dx / d, uy = dy / d;
              na.vx += ux * f; na.vy += uy * f;
              nb.vx -= ux * f; nb.vy -= uy * f;
            }
          }
        }
        for (var i = 0; i < nodes.length; i++) {
          var nd = nodes[i];
          var vm = Math.sqrt(nd.vx * nd.vx + nd.vy * nd.vy);
          if (vm > 10) { nd.vx *= 10 / vm; nd.vy *= 10 / vm; }
          nd.x += nd.vx * 0.5 * k; nd.y += nd.vy * 0.5 * k;
          nd.vx *= 0.8; nd.vy *= 0.8;
          var dr = Math.sqrt(nd.x * nd.x + nd.y * nd.y);
          if (dr > RMAX) { nd.x *= RMAX / dr; nd.y *= RMAX / dr; }
        }
      }
    }

    function buildLinks() {
      // thin lines: each node to its 2-3 nearest SAME-category neighbours,
      // + flag ONE anchor per category (nearest the cluster centroid) so we can
      // show a single label per constellation at default zoom (cc#197 fix_4c).
      links = [];
      Object.keys(byCat).forEach(function (cat) {
        var arr = byCat[cat];
        if (arr.length) {
          var mx = 0, my = 0; arr.forEach(function (n) { mx += n.x; my += n.y; }); mx /= arr.length; my /= arr.length;
          var anchor = null, ad = 1e18;
          arr.forEach(function (n) { n.anchor = false; var d = (n.x - mx) * (n.x - mx) + (n.y - my) * (n.y - my); if (d < ad) { ad = d; anchor = n; } });
          if (anchor) anchor.anchor = true;
        }
        arr.forEach(function (na) {
          var near = arr.filter(function (nb) { return nb !== na; })
            .map(function (nb) { var dx = na.x - nb.x, dy = na.y - nb.y; return { nb: nb, d: dx * dx + dy * dy }; })
            .sort(function (p, q) { return p.d - q.d; })
            .slice(0, 3);   // cc#197: 2-3 nearest same-category neighbours
          near.forEach(function (p) {
            if (na._i < p.nb._i) links.push([na, p.nb, catColor[cat] || "#8ca6e6"]);   // dedupe + tint
          });
        });
      });
    }

    function loadPositions() {
      try {
        var raw = localStorage.getItem(LKEY);
        if (!raw) return false;
        var pos = JSON.parse(raw), ok = 0;
        nodes.forEach(function (nd) { if (pos[nd.id]) { nd.x = pos[nd.id][0]; nd.y = pos[nd.id][1]; ok++; } });
        return ok === nodes.length;
      } catch (e) { return false; }
    }
    function savePositions() {
      try {
        var pos = {}; nodes.forEach(function (nd) { pos[nd.id] = [Math.round(nd.x), Math.round(nd.y)]; });
        localStorage.setItem(LKEY, JSON.stringify(pos));
      } catch (e) {}
    }

    function layout() {
      if ((config.layout || "force") === "honeycomb") { honeycomb(); }
      else if (!loadPositions()) { simulate(); savePositions(); }   // cache once, stable across visits
      buildLinks();
      fitView();
    }

    // ── view helpers ────────────────────────────────────────────────────────
    function bounds() {
      var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      nodes.forEach(function (nd) { x0 = Math.min(x0, nd.x); y0 = Math.min(y0, nd.y); x1 = Math.max(x1, nd.x); y1 = Math.max(y1, nd.y); });
      return { x0: x0, y0: y0, x1: x1, y1: y1, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, w: x1 - x0, h: y1 - y0 };
    }
    function fitView() {
      var b = bounds(), pad = 90;
      var s = Math.min((W - pad) / (b.w || 1), (H - pad) / (b.h || 1));
      s = Math.max(0.15, Math.min(s, 2.2));
      view.s = s;
      view.tx = W / 2 - b.cx * s;
      view.ty = H / 2 - b.cy * s;
      dirty = true;
    }
    function resetView() { fitView(); }
    function zoomToCategory(name) {
      var arr = byCat[name]; if (!arr || !arr.length) return;
      var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      arr.forEach(function (nd) { x0 = Math.min(x0, nd.x); y0 = Math.min(y0, nd.y); x1 = Math.max(x1, nd.x); y1 = Math.max(y1, nd.y); });
      var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, w = (x1 - x0) || 120, h = (y1 - y0) || 120, pad = 140;
      var s = Math.min((W - pad) / w, (H - pad) / h); s = Math.max(0.4, Math.min(s, 2.6));
      view.s = s; view.tx = W / 2 - cx * s; view.ty = H / 2 - cy * s; dirty = true;
    }
    function toWorld(px, py) { return { x: (px - view.tx) / view.s, y: (py - view.ty) / view.s }; }

    // ── rendering ─────────────────────────────────────────────────────────────
    function resize() {
      var rect = container.getBoundingClientRect();
      W = Math.max(1, rect.width); H = Math.max(1, rect.height);
      canvas.width = W * DPR; canvas.height = H * DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      dirty = true;
    }

    var bgStars = null;
    function drawBg() {
      ctx.fillStyle = "#070b1a";
      ctx.fillRect(0, 0, W, H);
      var g = ctx.createRadialGradient(W / 2, H * 0.42, 0, W / 2, H * 0.42, Math.max(W, H) * 0.75);
      g.addColorStop(0, "rgba(30,45,90,0.55)");
      g.addColorStop(1, "rgba(7,11,26,0)");
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      if (!bgStars) {
        bgStars = [];
        for (var i = 0; i < 70; i++) bgStars.push([hash("bx" + i) % 1000 / 1000, hash("by" + i) % 1000 / 1000, (hash("br" + i) % 100) / 100]);
      }
      ctx.fillStyle = "rgba(255,255,255,0.5)";
      bgStars.forEach(function (s) {
        ctx.globalAlpha = 0.12 + s[2] * 0.22;
        ctx.beginPath(); ctx.arc(s[0] * W, s[1] * H, 0.6 + s[2] * 0.8, 0, 6.283); ctx.fill();
      });
      ctx.globalAlpha = 1;
    }

    function toScreen(wx, wy) { return { x: wx * view.s + view.tx, y: wy * view.s + view.ty }; }

    function draw() {
      raf = null;
      drawBg();

      // ── WORLD PASS: hulls, links, node glow + core ──
      ctx.save();
      ctx.translate(view.tx, view.ty); ctx.scale(view.s, view.s);

      var clusters = [];
      Object.keys(byCat).forEach(function (cat) {
        var arr = byCat[cat]; if (!arr.length) return;
        var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
        arr.forEach(function (nd) { x0 = Math.min(x0, nd.x); y0 = Math.min(y0, nd.y); x1 = Math.max(x1, nd.x); y1 = Math.max(y1, nd.y); });
        var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rw = Math.max((x1 - x0) / 2 + 34, 30), rh = Math.max((y1 - y0) / 2 + 34, 30);
        clusters.push({ cat: cat, arr: arr, col: catColor[cat] || "#60a5fa", cx: cx, cy: cy, rw: rw, rh: rh, R: Math.max(rw, rh) });
        ctx.globalAlpha = 0.10; ctx.fillStyle = catColor[cat] || "#60a5fa";
        ctx.beginPath(); ctx.ellipse(cx, cy, rw, rh, 0, 0, 6.283); ctx.fill();
      });
      ctx.globalAlpha = 1;

      ctx.lineWidth = 1.1 / view.s; ctx.lineCap = "round";
      links.forEach(function (l) {
        var a = l[0], b = l[1];
        var al = (a.dim && b.dim) ? 0.05 : (a.dim || b.dim ? 0.10 : 0.30);
        ctx.strokeStyle = hexA(l[2] || "#8ca6e6", al);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      });

      nodes.forEach(function (nd) {
        var t = TIER[nd.tier], r = (nd.size || t.r), isH = hover === nd;
        var a = nd.dim ? 0.15 : 1, grad = r * (isH ? 5.5 : 3.4);
        ctx.globalAlpha = (isH ? 1 : 0.7) * (nd.dim ? 0.22 : 1);
        var gr = ctx.createRadialGradient(nd.x, nd.y, 0, nd.x, nd.y, grad);
        gr.addColorStop(0, t.glow); gr.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = gr; ctx.beginPath(); ctx.arc(nd.x, nd.y, grad, 0, 6.283); ctx.fill();
        ctx.globalAlpha = a; ctx.fillStyle = t.core;
        ctx.beginPath(); ctx.arc(nd.x, nd.y, r + (isH ? 1.6 : 0), 0, 6.283); ctx.fill();
        ctx.globalAlpha = a * 0.9; ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.beginPath(); ctx.arc(nd.x - r * 0.3, nd.y - r * 0.3, r * 0.35, 0, 6.283); ctx.fill();
      });
      ctx.globalAlpha = 1;
      ctx.restore();

      // ── SCREEN PASS: labels OUTSIDE clusters, collision-free, edge-safe (cc#198) ──
      var M = 10, placed = [];
      function hit(b) { return placed.some(function (p) { return b.x < p.x + p.w && b.x + b.w > p.x && b.y < p.y + p.h && b.y + b.h > p.y; }); }

      // (a) category header — above its cluster, flips BELOW if it would clip the
      //     top edge; x-clamped so border clusters never clip sideways; never on dots.
      ctx.font = "700 11px ui-monospace,Menlo,monospace"; ctx.textBaseline = "middle";
      clusters.forEach(function (c) {
        var s = toScreen(c.cx, c.cy), R = c.R * view.s;
        if (s.x < -R - 60 || s.x > W + R + 60 || s.y < -R - 60 || s.y > H + R + 60) return;  // cull off-screen cluster
        var txt = (c.cat || "").toUpperCase() + " · " + c.arr.length, tw = ctx.measureText(txt).width;
        var above = (s.y - R - 16) > (M + 8);
        var ly = above ? (s.y - R - 12) : (s.y + R + 14);
        ly = Math.max(M + 8, Math.min(H - M, ly));
        var cx = Math.max(tw / 2 + M, Math.min(W - tw / 2 - M, s.x));
        ctx.textAlign = "center";
        ctx.globalAlpha = 0.75; ctx.fillStyle = "rgba(7,11,26,0.85)"; ctx.fillText(txt, cx + 0.6, ly + 0.6);
        ctx.globalAlpha = 0.96; ctx.fillStyle = c.col; ctx.fillText(txt, cx, ly);
        placed.push({ x: cx - tw / 2 - 4, y: ly - 9, w: tw + 8, h: 18 });
      });
      ctx.globalAlpha = 1;

      // (b) node labels — only when zoomed in. Each label sits OUTSIDE the cluster
      //     circle, radially outward from the node's direction, with a thin leader
      //     line; collision nudge + edge clamp guarantee no overlap / no clipping.
      if (view.s > 1.35) {
        ctx.font = "600 10px -apple-system,Segoe UI,sans-serif";
        clusters.forEach(function (c) {
          var sC = toScreen(c.cx, c.cy), R = c.R * view.s;
          c.arr.forEach(function (nd) {
            if (nd.dim || hover === nd) return;
            var sN = toScreen(nd.x, nd.y);
            if (sN.x < -30 || sN.x > W + 30 || sN.y < -30 || sN.y > H + 30) return;   // cull off-screen node
            var dx = sN.x - sC.x, dy = sN.y - sC.y, dd = Math.sqrt(dx * dx + dy * dy) || 0.01, ux = dx / dd, uy = dy / dd;
            var lx = sC.x + ux * (R + 14), ly = sC.y + uy * (R + 14);
            var txt = clip(nd.label, 26), tw = ctx.measureText(txt).width;
            var align = ux > 0.25 ? "left" : (ux < -0.25 ? "right" : "center");
            var bx = align === "left" ? lx : (align === "right" ? lx - tw : lx - tw / 2);
            bx = Math.max(M, Math.min(W - M - tw, bx));
            ly = Math.max(M + 8, Math.min(H - M, ly));
            var box = { x: bx - 3, y: ly - 8, w: tw + 6, h: 16 }, tries = 0;
            // collision: nudge vertically; if pinned to a top/bottom edge, slide horizontally instead
            while (tries < 12 && hit(box)) {
              if (ly <= M + 9 || ly >= H - M - 1) { bx += (ux >= 0 ? 1 : -1) * (tw * 0.4 + 12); bx = Math.max(M, Math.min(W - M - tw, bx)); box.x = bx - 3; }
              else { ly += (uy >= 0 ? 1 : -1) * 15; ly = Math.max(M + 8, Math.min(H - M, ly)); box.y = ly - 8; }
              tries++;
            }
            placed.push(box);
            var drawX = align === "left" ? bx : (align === "right" ? bx + tw : bx + tw / 2);
            var er = (nd.size || TIER[nd.tier].r) * view.s;
            ctx.globalAlpha = 0.45; ctx.strokeStyle = hexA(c.col, 0.5); ctx.lineWidth = 0.8;
            ctx.beginPath(); ctx.moveTo(sN.x + ux * er, sN.y + uy * er);
            ctx.lineTo(align === "left" ? bx - 3 : (align === "right" ? bx + tw + 3 : drawX), ly); ctx.stroke();
            ctx.textAlign = align; ctx.globalAlpha = 0.9;
            ctx.fillStyle = "rgba(6,10,22,0.85)"; ctx.fillText(txt, drawX + 0.6, ly + 0.6);
            ctx.fillStyle = "rgba(222,231,252,0.96)"; ctx.fillText(txt, drawX, ly);
          });
        });
        ctx.globalAlpha = 1;
      }

      if (hover) drawTooltip(hover);   // screen-space tooltip
    }

    function drawTooltip(nd) {
      // cc#198: drawn in SCREEN space (called after the world transform is
      // restored) so it is crisp and edge-safe regardless of zoom.
      var s = toScreen(nd.x, nd.y), r = (nd.size || TIER[nd.tier].r) * view.s;
      var pad = 6, fs = 12, sf = 10;
      ctx.font = "700 " + fs + "px -apple-system,Segoe UI,sans-serif";
      var t1 = clip(nd.label, 46), w1 = ctx.measureText(t1).width;
      var t2 = nd.sublabel || "";
      ctx.font = "600 " + sf + "px -apple-system,Segoe UI,sans-serif";
      var w2 = t2 ? ctx.measureText(t2).width : 0;
      var w = Math.max(w1, w2) + pad * 2, h = (t2 ? fs + sf + 5 : fs) + pad * 2;
      var bx = s.x + r + 8, by = s.y - h - 4;
      if (bx + w > W - 6) bx = s.x - r - 8 - w;     // flip left near right edge
      if (bx < 6) bx = 6;
      if (by < 6) by = s.y + r + 8;                 // flip below near top edge
      if (by + h > H - 6) by = H - 6 - h;
      ctx.globalAlpha = 0.97;
      ctx.fillStyle = "rgba(13,20,40,0.97)";
      roundRect(bx, by, w, h, 6); ctx.fill();
      ctx.strokeStyle = "rgba(120,150,220,0.55)"; ctx.lineWidth = 1;
      roundRect(bx, by, w, h, 6); ctx.stroke();
      ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.globalAlpha = 1;
      ctx.fillStyle = "#eaf0ff";
      ctx.font = "700 " + fs + "px -apple-system,Segoe UI,sans-serif";
      ctx.fillText(t1, bx + pad, by + pad);
      if (t2) {
        ctx.fillStyle = "#95a8d6";
        ctx.font = "600 " + sf + "px -apple-system,Segoe UI,sans-serif";
        ctx.fillText(t2, bx + pad, by + pad + fs + 3);
      }
      ctx.globalAlpha = 1;
    }
    function roundRect(x, y, w, h, r) {
      ctx.beginPath();
      ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
    }
    function clip(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
    function hexA(hex, a) {
      var h = hex.replace("#", "");
      if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      var r = parseInt(h.slice(0, 2), 16), g = parseInt(h.slice(2, 4), 16), b = parseInt(h.slice(4, 6), 16);
      return "rgba(" + r + "," + g + "," + b + "," + a + ")";
    }
    function schedule() { if (!raf) raf = requestAnimationFrame(draw); }

    // ── interactions ─────────────────────────────────────────────────────────
    var ptrs = {}, dragging = false, moved = false, downXY = null, pinchD0 = 0, pinchS0 = 1;

    function nodeAt(px, py) {
      var wpt = toWorld(px, py), best = null, bd = 1e9;
      for (var i = nodes.length - 1; i >= 0; i--) {
        var nd = nodes[i], t = TIER[nd.tier], r = (nd.size || t.r) + 6;
        var dx = nd.x - wpt.x, dy = nd.y - wpt.y, d = dx * dx + dy * dy;
        var rr = (r / 1) * (r / 1);
        if (d < rr * (1 / (view.s * view.s)) && d < bd) { bd = d; best = nd; }
      }
      return best;
    }
    function catLabelAt(px, py) {
      var wpt = toWorld(px, py), hit = null;
      Object.keys(byCat).forEach(function (cat) {
        var arr = byCat[cat]; if (!arr.length) return;
        var x0 = 1e9, y0 = 1e9, x1 = -1e9;
        arr.forEach(function (nd) { x0 = Math.min(x0, nd.x); y0 = Math.min(y0, nd.y); x1 = Math.max(x1, nd.x); });
        var cx = (x0 + x1) / 2, ly = y0 - 16;
        if (Math.abs(wpt.x - cx) < 90 && Math.abs(wpt.y - ly) < 16 / view.s) hit = cat;
      });
      return hit;
    }

    var lpTimer = null, lpFired = false;
    function clearLP() { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } }
    function onDown(e) {
      canvas.setPointerCapture(e.pointerId);
      ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      var n = Object.keys(ptrs).length;
      if (n === 1) {
        dragging = true; moved = false; lpFired = false; downXY = rel(e);
        // cc#197: long-press (mobile) shows the same title tooltip as desktop hover
        clearLP();
        lpTimer = setTimeout(function () {
          if (!moved) { var nd = nodeAt(downXY.x, downXY.y); if (nd) { hover = nd; lpFired = true; schedule(); } }
        }, 380);
      } else if (n === 2) { clearLP(); var d = pinchDist(); pinchD0 = d.d; pinchS0 = view.s; }
    }
    function onMove(e) {
      if (!(e.pointerId in ptrs)) {
        var r = rel(e), nd = nodeAt(r.x, r.y);
        if (nd !== hover) { hover = nd; canvas.style.cursor = nd ? "pointer" : "grab"; schedule(); }
        return;
      }
      var prev = ptrs[e.pointerId]; ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      var n = Object.keys(ptrs).length;
      if (n >= 2) {                       // pinch zoom
        var d = pinchDist();
        if (pinchD0 > 0) {
          var ns = Math.max(0.15, Math.min(4, pinchS0 * d.d / pinchD0));
          zoomAt(d.cx, d.cy, ns);
        }
        moved = true; return;
      }
      if (dragging) {
        var dx = e.clientX - prev.x, dy = e.clientY - prev.y;
        if (Math.abs(e.clientX - (downXY.gx)) + Math.abs(e.clientY - (downXY.gy)) > 6) { moved = true; clearLP(); }
        view.tx += dx; view.ty += dy; schedule();
      }
    }
    function onUp(e) {
      clearLP();
      var wasTap = dragging && !moved && !lpFired;   // long-press = tooltip only, not a tap
      var r = downXY;
      delete ptrs[e.pointerId];
      try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
      if (Object.keys(ptrs).length === 0) dragging = false;
      if (wasTap && r) {
        var nd = nodeAt(r.x, r.y);
        if (nd) { if (opts.onTap) opts.onTap(nd); return; }
        var cat = catLabelAt(r.x, r.y);
        if (cat) { zoomToCategory(cat); if (opts.onCategoryTap) opts.onCategoryTap(cat); }
      }
      lpFired = false;
    }
    function onWheel(e) {
      e.preventDefault();
      var r = rel(e), factor = Math.pow(1.0015, -e.deltaY);
      zoomAt(r.x, r.y, Math.max(0.15, Math.min(4, view.s * factor)));
    }
    function zoomAt(px, py, ns) {
      var w = toWorld(px, py);
      view.s = ns; view.tx = px - w.x * ns; view.ty = py - w.y * ns; schedule();
    }
    function rel(e) { var b = canvas.getBoundingClientRect(); return { x: e.clientX - b.left, y: e.clientY - b.top, gx: e.clientX, gy: e.clientY }; }
    function pinchDist() {
      var ks = Object.keys(ptrs), a = ptrs[ks[0]], b = ptrs[ks[1]], bb = canvas.getBoundingClientRect();
      var dx = a.x - b.x, dy = a.y - b.y;
      return { d: Math.sqrt(dx * dx + dy * dy), cx: (a.x + b.x) / 2 - bb.left, cy: (a.y + b.y) / 2 - bb.top };
    }

    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointercancel", onUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    var ro = null;
    if (window.ResizeObserver) { ro = new ResizeObserver(function () { resize(); fitView(); schedule(); }); ro.observe(container); }

    // ── public API ─────────────────────────────────────────────────────────────
    function setDim(pred) {
      // pred(node) truthy => keep bright; falsy => dim to 15%. null => all bright.
      nodes.forEach(function (nd) { nd.dim = pred ? !pred(nd) : false; });
      schedule();
    }
    function destroy() {
      if (ro) ro.disconnect();
      if (raf) cancelAnimationFrame(raf);
      canvas.remove();
    }

    // hash for deterministic jitter / bg stars
    function hash(s) { s = String(s); var h = 2166136261; for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = (h * 16777619) >>> 0; } return h; }

    resize(); layout(); schedule();

    return {
      setDim: setDim, resetView: resetView, zoomToCategory: zoomToCategory,
      resize: function () { resize(); fitView(); schedule(); }, destroy: destroy,
      _nodes: nodes,
    };
  }

  window.GalaxyMap = { create: create };
})();

/* galaxy_map.js — Scorr Knowledge Galaxy (cc#202: one true spiral galaxy)
 * ============================================================================
 * A GENERIC, canvas-2D renderer that draws ONE living spiral galaxy from a config
 * of category "star systems". It knows nothing about articles or stocks.
 *
 *   GalaxyMap.create(container, {
 *     nodes:      [{ id, label, category, tier:'beginner'|'pro', size?, sublabel?, payload? }],
 *     categories: [{ name, color }],
 *     cacheKey:   'knowledge',
 *   }, {
 *     onTap:         node => {},
 *     onCategoryTap: name => {},
 *   })  ->  { setDim(fn), resetView(), zoomToCategory(name), resize(), destroy() }
 *
 * cc#202 concept (locked): NOT 10 blobs on a ring. ONE spiral galaxy — a bright
 * warm core (Basics = "start here") with two logarithmic arms carrying categories
 * outward; beginner-leaning near the core, Pro-heavy at the rim. The whole galaxy
 * rotates clockwise as one body (~4 min/rev); all text stays upright (screen space).
 */
(function () {
  "use strict";

  // Beginner/Pro dot colours are SEMANTIC — never remapped by the palette.
  var TIER = {
    beginner: { r: 4.4, core: "#34d399", glow: "rgba(52,211,153,0.6)" },
    pro:      { r: 6.8, core: "#fbbf24", glow: "rgba(251,191,36,0.6)" },
  };
  var CAT_FALLBACK = ["#60a5fa","#f472b6","#34d399","#fbbf24","#a78bfa",
                      "#22d3ee","#fb7185","#c084fc","#4ade80","#facc15"];

  // ── colour story: core warm gold → teal → violet at the rim ──
  var STOPS = [[255,206,120], [102,214,214], [167,139,250]];
  function palette(u) {
    u = u < 0 ? 0 : (u > 1 ? 1 : u);
    var seg = u * (STOPS.length - 1), i = Math.floor(seg), f = seg - i;
    var a = STOPS[i], b = STOPS[Math.min(i + 1, STOPS.length - 1)];
    return [Math.round(a[0] + (b[0] - a[0]) * f),
            Math.round(a[1] + (b[1] - a[1]) * f),
            Math.round(a[2] + (b[2] - a[2]) * f)];
  }
  function rgb(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + (a == null ? 1 : a) + ")"; }
  function rgbHex(c) { return "#" + c.map(function (v) { return ("0" + v.toString(16)).slice(-2); }).join(""); }

  function create(container, config, opts) {
    opts = opts || {};
    var nodes = (config.nodes || []).map(function (n, i) {
      return {
        id: n.id, label: n.label || "", category: n.category || "",
        tier: (n.tier === "pro" ? "pro" : "beginner"),
        size: n.size, sublabel: n.sublabel || "", payload: n.payload, _i: i,
        _tp: (i * 137) % 628 / 100,
        x: 0, y: 0, sun: false, core: false, dim: false,
      };
    });
    var cats = (config.categories || []).map(function (c, i) {
      return { name: c.name, color: c.color || CAT_FALLBACK[i % CAT_FALLBACK.length] };
    });
    var byCat = {}; cats.forEach(function (c) { byCat[c.name] = []; });
    nodes.forEach(function (n) { (byCat[n.category] || (byCat[n.category] = [])).push(n); });

    // ── canvas ────────────────────────────────────────────────────────────────
    var canvas = document.createElement("canvas");
    canvas.style.cssText = "display:block;width:100%;height:100%;touch-action:none;cursor:grab";
    container.appendChild(canvas);
    var ctx = canvas.getContext("2d");
    var DPR = Math.min(window.devicePixelRatio || 1, 2);
    var W = 0, H = 0;

    var view = { s: 1, tx: 0, ty: 0 };
    var links = [];
    var hover = null, hoverCat = null;
    var raf = null;

    // ── motion state ───────────────────────────────────────────────────────────
    var mq = window.matchMedia || function () { return { matches: false }; };
    var reduceMotion = mq("(prefers-reduced-motion: reduce)").matches;
    var isTouch = mq("(pointer: coarse)").matches;
    var MOTION = !reduceMotion;
    var DO_PARALLAX = MOTION && !isTouch;
    var DO_SHOOT = MOTION && !isTouch;
    var _t = 0, _t0 = null, _prev = null, _loop = null;
    var gPhase = 0;                                    // global galaxy rotation angle
    var gOmega = (2 * Math.PI) / (isTouch ? 360 : 240); // 4 min/rev (6 on mobile)
    var lastInput = -1e9, pinching = false, dragging = false;
    var mouseNX = 0, mouseNY = 0;

    // ── spiral geometry ─────────────────────────────────────────────────────────
    var SP = { A: 130, b: 0.21, th0: 1.5, dth: 0.98 };
    var cmeta = {};    // cat -> { cx, cy, u, col, sun }
    var ringMeta = {}; // cat -> { omega, phase }
    var Rmax = 400;

    // ── pre-rendered sprites (built once; no per-frame gradients) ──
    function makeGlow(color) {
      var S = 128, c = document.createElement("canvas"); c.width = S; c.height = S;
      var g = c.getContext("2d"), gr = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
      gr.addColorStop(0, color); gr.addColorStop(0.5, color.replace(/0?\.\d+\)$/, "0.18)")); gr.addColorStop(1, "rgba(0,0,0,0)");
      g.fillStyle = gr; g.fillRect(0, 0, S, S); return c;
    }
    function radialSprite(S, stops) {
      var c = document.createElement("canvas"); c.width = S; c.height = S;
      var g = c.getContext("2d"), gr = g.createRadialGradient(S / 2, S / 2, 0, S / 2, S / 2, S / 2);
      stops.forEach(function (st) { gr.addColorStop(st[0], st[1]); });
      g.fillStyle = gr; g.fillRect(0, 0, S, S); return c;
    }
    var GLOW = { beginner: makeGlow(TIER.beginner.glow), pro: makeGlow(TIER.pro.glow) };
    var CORE = radialSprite(360, [
      [0, "rgba(255,252,238,1)"], [0.18, "rgba(255,224,150,0.92)"],
      [0.42, "rgba(255,180,96,0.40)"], [0.72, "rgba(220,150,90,0.12)"], [1, "rgba(255,150,60,0)"]]);
    var NEB = [
      radialSprite(320, [[0, "rgba(150,110,235,0.55)"], [1, "rgba(150,110,235,0)"]]),   // violet
      radialSprite(320, [[0, "rgba(70,200,210,0.5)"], [1, "rgba(70,200,210,0)"]]),      // teal
      radialSprite(320, [[0, "rgba(240,120,160,0.5)"], [1, "rgba(240,120,160,0)"]]),    // rose
    ];
    var SMUDGE = radialSprite(96, [[0, "rgba(210,220,255,0.6)"], [0.5, "rgba(180,190,240,0.18)"], [1, "rgba(180,190,240,0)"]]);
    var armSprite = null, armHalf = 0;

    // ── layout: deterministic spiral placement (no force sim for centres) ──
    function proShare(cat) {
      var arr = byCat[cat] || []; if (!arr.length) return 0;
      var p = 0; arr.forEach(function (n) { if (n.tier === "pro") p++; });
      return p / arr.length;
    }
    function phyllo(cat, ceX, ceY, bright) {
      var arr = byCat[cat], step = 15;
      arr.forEach(function (nd, k) {
        nd.core = bright;
        if (k === 0) { nd.x = ceX; nd.y = ceY; nd.sun = true; return; }
        var ang = k * 2.399963, rad = step * Math.sqrt(k);
        nd.x = ceX + Math.cos(ang) * rad; nd.y = ceY + Math.sin(ang) * rad;
      });
    }
    function layout() {
      cmeta = {}; ringMeta = {};
      var core = cats.filter(function (c) { return /^basics$/i.test(c.name); }).map(function (c) { return c.name; });
      var arm = cats.map(function (c) { return c.name; })
        .filter(function (n) { return core.indexOf(n) < 0; })
        .sort(function (a, b) { var d = proShare(a) - proShare(b); return d !== 0 ? d : (a < b ? -1 : 1); });

      // Basics lives IN the core halo (brighter dots)
      core.forEach(function (cat) { cmeta[cat] = { cx: 0, cy: 0, u: 0, col: palette(0), sun: true, coreCat: true }; phyllo(cat, 0, 0, true); });

      // remaining categories along two log-spiral arms, alternating sides, θ increasing
      var perArm = [0, 0];
      arm.forEach(function (cat, idx) {
        var a = idx % 2, k = perArm[a]++;
        var th = SP.th0 + k * SP.dth, r = SP.A * Math.exp(SP.b * th);
        var ang = th + a * Math.PI;
        var cx = Math.cos(ang) * r, cy = Math.sin(ang) * r;
        var u = Math.min(1, (r - SP.A) / (SP.A * Math.exp(SP.b * (SP.th0 + 4 * SP.dth)) - SP.A));
        cmeta[cat] = { cx: cx, cy: cy, u: u, col: palette(u), sun: true, th: th };
        phyllo(cat, cx, cy, false);
      });

      // furthest extent (for fit + arm sprite)
      Rmax = 0;
      nodes.forEach(function (nd) { var d = Math.sqrt(nd.x * nd.x + nd.y * nd.y); if (d > Rmax) Rmax = d; });
      Rmax += 80;

      Object.keys(byCat).forEach(function (cat) {
        var h = hash("ring" + cat), period = 20 + (h % 21);
        ringMeta[cat] = { omega: 2 * Math.PI / period, phase: (h % 628) / 100 };
      });

      buildArmSprite();
      buildLinks();
      fitView();
    }

    function buildArmSprite() {
      var pad = 30, S = Math.ceil((Rmax + pad) * 2);
      armHalf = S / 2;
      var cv = document.createElement("canvas"); cv.width = S; cv.height = S;
      var g = cv.getContext("2d"); g.translate(armHalf, armHalf);
      var thMax = SP.th0 + 4.4 * SP.dth;
      for (var a = 0; a < 2; a++) {
        // (a) soft nebula dust ribbon following the spiral centreline
        for (var th = 0.3; th < thMax; th += 0.05) {
          var r = SP.A * Math.exp(SP.b * th); if (r > Rmax) break;
          var ang = th + a * Math.PI, x = Math.cos(ang) * r, y = Math.sin(ang) * r;
          var u = Math.min(1, (r - SP.A) / (Rmax - SP.A)), col = palette(u), w = 24 + u * 70;
          var gr = g.createRadialGradient(x, y, 0, x, y, w);
          gr.addColorStop(0, rgb(col, 0.10)); gr.addColorStop(1, rgb(col, 0));
          g.fillStyle = gr; g.beginPath(); g.arc(x, y, w, 0, 6.283); g.fill();
        }
        // (b) sparse micro-star dust sprinkled along the arm
        for (var k = 0; k < 140; k++) {
          var t2 = 0.3 + (hash("d" + a + "_" + k) % 1000 / 1000) * (thMax - 0.3);
          var rr = SP.A * Math.exp(SP.b * t2); if (rr > Rmax) continue;
          var jr = ((hash("jr" + a + k) % 100) / 100 - 0.5) * (30 + rr * 0.10);
          var ja = ((hash("ja" + a + k) % 100) / 100 - 0.5) * 0.5;
          var an = t2 + a * Math.PI + ja, px = Math.cos(an) * (rr + jr), py = Math.sin(an) * (rr + jr);
          g.globalAlpha = 0.25 + (hash("db" + a + k) % 60) / 100;
          g.fillStyle = "rgba(226,234,255,0.9)";
          g.beginPath(); g.arc(px, py, 0.5 + (hash("ds" + a + k) % 10) / 10, 0, 6.283); g.fill();
        }
        g.globalAlpha = 1;
      }
      armSprite = cv;
    }

    function buildLinks() {
      links = [];
      Object.keys(byCat).forEach(function (cat) {
        var arr = byCat[cat], col = (cmeta[cat] && cmeta[cat].col) || [140, 166, 230];
        arr.forEach(function (na) {
          var near = arr.filter(function (nb) { return nb !== na; })
            .map(function (nb) { var dx = na.x - nb.x, dy = na.y - nb.y; return { nb: nb, d: dx * dx + dy * dy }; })
            .sort(function (p, q) { return p.d - q.d; }).slice(0, 3);
          near.forEach(function (p) { if (na._i < p.nb._i) links.push([na, p.nb, col]); });
        });
      });
    }

    // ── rotation-aware transforms (galaxy spins about world origin) ──
    function rot(x, y, ph) { var c = Math.cos(ph), s = Math.sin(ph); return { x: x * c - y * s, y: x * s + y * c }; }
    function toScreen(wx, wy) { var p = rot(wx, wy, gPhase); return { x: p.x * view.s + view.tx, y: p.y * view.s + view.ty }; }
    function toWorld(px, py) { var ux = (px - view.tx) / view.s, uy = (py - view.ty) / view.s; return rot(ux, uy, -gPhase); }

    // ── view helpers (fit to bounding CIRCLE so rotation never clips corners) ──
    var FIT_BOOST = 1.35;   // cc#203: founder wants default ~2 notches bigger
    function fitView() {
      var pad = 70, s = Math.min((W - pad) / (2 * Rmax || 1), (H - pad) / (2 * Rmax || 1));
      s = Math.max(0.12, Math.min(s * FIT_BOOST, 2.6));
      view.s = s; view.tx = W / 2; view.ty = H / 2;
    }
    function resetView() { fitView(); markInput(); schedule(); }
    function zoomToCategory(name) {
      var m = cmeta[name], arr = byCat[name]; if (!m || !arr || !arr.length) return;
      var maxr = 0; arr.forEach(function (nd) { var dx = nd.x - m.cx, dy = nd.y - m.cy, d = Math.sqrt(dx * dx + dy * dy); if (d > maxr) maxr = d; });
      var span = (maxr + 70) * 2, s = Math.min(W / span, H / span); s = Math.max(0.5, Math.min(s, 3));
      var p = rot(m.cx, m.cy, gPhase);
      view.s = s; view.tx = W / 2 - p.x * s; view.ty = H / 2 - p.y * s; markInput(); schedule();
    }

    // ── background: depth-parallax star layers + nebula fields + smudges ──
    var layers = null;
    function buildLayers() {
      function mk(n, prefix, smin, smax, depth, tw) {
        var out = [];
        for (var i = 0; i < n; i++) out.push([hash(prefix + "x" + i) % 1000 / 1000, hash(prefix + "y" + i) % 1000 / 1000,
          smin + (hash(prefix + "s" + i) % 100) / 100 * (smax - smin), (hash(prefix + "p" + i) % 628) / 100, depth, tw]);
        return out;
      }
      layers = isTouch
        ? [mk(70, "far", 0.4, 1.0, 0.12, 0.6)]
        : [mk(90, "far", 0.4, 0.9, 0.10, 0.5), mk(52, "mid", 0.7, 1.4, 0.32, 0.8), mk(26, "near", 1.1, 2.1, 0.7, 1.1)];
    }
    function drawBg() {
      ctx.fillStyle = "#060912"; ctx.fillRect(0, 0, W, H);
      // 2-3 slow-drifting nebula colour fields
      var drift = MOTION ? _t * 3 : 0;
      var neb = [[0.26, 0.32, 0], [0.74, 0.40, 1], [0.50, 0.78, 2]];
      neb.forEach(function (nb, i) {
        var px = (DO_PARALLAX ? mouseNX * 10 : 0) + Math.sin(drift * 0.02 + i) * 8;
        var py = (DO_PARALLAX ? mouseNY * 8 : 0) + Math.cos(drift * 0.017 + i) * 6;
        var sz = Math.max(W, H) * (0.7 + i * 0.12);
        ctx.globalAlpha = 0.07;
        ctx.drawImage(NEB[nb[2]], nb[0] * W - sz / 2 + px, nb[1] * H - sz / 2 + py, sz, sz);
      });
      ctx.globalAlpha = 1;
      // 4-5 distant galaxy smudges (faint, tilted)
      for (var s = 0; s < 5; s++) {
        var gx = (hash("gx" + s) % 1000 / 1000) * W, gy = (hash("gy" + s) % 1000 / 1000) * H;
        var gs = 26 + (hash("gg" + s) % 40), gr = (hash("gr" + s) % 628) / 100;
        ctx.save(); ctx.translate(gx, gy); ctx.rotate(gr); ctx.globalAlpha = 0.10;
        ctx.drawImage(SMUDGE, -gs, -gs * 0.42, gs * 2, gs * 0.84); ctx.restore();
      }
      ctx.globalAlpha = 1;
      // parallax star layers (far → near)
      if (!layers) buildLayers();
      ctx.fillStyle = "rgba(255,255,255,0.9)";
      layers.forEach(function (arr) {
        arr.forEach(function (st) {
          var px = DO_PARALLAX ? mouseNX * 40 * st[4] : 0, py = DO_PARALLAX ? mouseNY * 28 * st[4] : 0;
          var tw = MOTION ? (0.55 + 0.45 * Math.sin(_t * st[5] + st[3])) : 1;
          ctx.globalAlpha = (0.10 + st[2] * 0.28) * tw;
          ctx.beginPath(); ctx.arc(st[0] * W - px, st[1] * H - py, st[2], 0, 6.283); ctx.fill();
        });
      });
      ctx.globalAlpha = 1;
    }

    // ── shooting star (screen space, ~every 25-45s) ──
    var shoot = null, shootNext = 0;
    function scheduleShoot(now) { shootNext = now + 25000 + Math.random() * 20000; }
    function updateShoot(now) {
      if (!DO_SHOOT) return;
      if (!shootNext) { scheduleShoot(now); return; }
      if (!shoot && now >= shootNext) {
        var edge = Math.random(), sx = Math.random() * W, sy = Math.random() * H * 0.5;
        var ang = (Math.PI * 0.15) + Math.random() * (Math.PI * 0.5);
        shoot = { x: sx, y: sy, dx: Math.cos(ang), dy: Math.sin(ang), t0: now, len: 140 + Math.random() * 120 };
        scheduleShoot(now);
      }
      if (shoot && now - shoot.t0 > 800) shoot = null;
    }
    function drawShoot(now) {
      if (!shoot) return;
      var p = (now - shoot.t0) / 800, ease = p < 0.5 ? 1 : 1 - (p - 0.5) / 0.5;
      var travel = 380, hx = shoot.x + shoot.dx * travel * p, hy = shoot.y + shoot.dy * travel * p;
      var tx = hx - shoot.dx * shoot.len, ty = hy - shoot.dy * shoot.len;
      var g = ctx.createLinearGradient(tx, ty, hx, hy);
      g.addColorStop(0, "rgba(255,255,255,0)"); g.addColorStop(1, "rgba(255,255,255," + (0.9 * ease) + ")");
      ctx.strokeStyle = g; ctx.lineWidth = 1.6; ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(hx, hy); ctx.stroke();
      ctx.globalAlpha = ease; ctx.fillStyle = "#fff";
      ctx.beginPath(); ctx.arc(hx, hy, 1.8, 0, 6.283); ctx.fill(); ctx.globalAlpha = 1;
    }

    // ── main render ──────────────────────────────────────────────────────────
    var catHitBoxes = [];
    function resize() {
      var rect = container.getBoundingClientRect();
      W = Math.max(1, rect.width); H = Math.max(1, rect.height);
      canvas.width = W * DPR; canvas.height = H * DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    }

    function draw() {
      drawBg();

      // ── WORLD PASS: single rotate(gPhase) spins the entire galaxy as one body ──
      ctx.save();
      ctx.translate(view.tx, view.ty); ctx.scale(view.s, view.s); ctx.rotate(gPhase);

      // arm nebula + dust (pre-rendered, rotates with galaxy)
      if (armSprite) ctx.drawImage(armSprite, -armHalf, -armHalf);

      // galactic core: breathing nucleus at world origin (Basics start-here)
      var bre0 = MOTION ? (1 + 0.05 * Math.sin(_t * (2 * Math.PI / 5))) : 1;
      var cS = (SP.A * 2.6) * bre0;
      ctx.globalAlpha = 1; ctx.drawImage(CORE, -cS / 2, -cS / 2, cS, cS);

      // cluster glows + faint dashed rings
      var clusters = [];
      Object.keys(byCat).forEach(function (cat) {
        var arr = byCat[cat], m = cmeta[cat]; if (!arr.length || !m) return;
        var maxr = 0; arr.forEach(function (nd) { var dx = nd.x - m.cx, dy = nd.y - m.cy, d = Math.sqrt(dx * dx + dy * dy); if (d > maxr) maxr = d; });
        var R = Math.max(maxr + 34, 40);
        clusters.push({ cat: cat, arr: arr, col: m.col, cx: m.cx, cy: m.cy, R: R, coreCat: !!m.coreCat });
      });
      clusters.forEach(function (c) {
        if (c.coreCat) return;                     // core cluster already lit by the nucleus
        var pulse = (hoverCat === c.cat) ? (0.5 + 0.5 * Math.sin(_t * 3.2)) : 0;
        var GRr = c.R * (1.14 + pulse * 0.10);
        var gr = ctx.createRadialGradient(c.cx, c.cy, c.R * 0.15, c.cx, c.cy, GRr);
        gr.addColorStop(0, rgb(c.col, 0.15 + pulse * 0.06)); gr.addColorStop(1, rgb(c.col, 0));
        ctx.fillStyle = gr; ctx.beginPath(); ctx.arc(c.cx, c.cy, GRr, 0, 6.283); ctx.fill();
      });
      clusters.forEach(function (c) {
        var m = ringMeta[c.cat] || { omega: 0.25, phase: 0 }, rr = c.R + 8, spin = MOTION ? _t : 0;
        ctx.save();
        ctx.strokeStyle = rgb(c.col, 0.15 + (hoverCat === c.cat ? 0.15 : 0));   // fainter — galaxy is the motion now
        ctx.lineWidth = 1.0 / view.s; ctx.setLineDash([5 / view.s, 7 / view.s]);
        ctx.lineDashOffset = -spin * m.omega * rr;
        ctx.beginPath(); ctx.arc(c.cx, c.cy, rr, 0, 6.283); ctx.stroke();
        ctx.restore();
        var a = m.phase + spin * m.omega;
        ctx.globalAlpha = 0.5; ctx.fillStyle = rgbHex(c.col);
        ctx.beginPath(); ctx.arc(c.cx + Math.cos(a) * rr, c.cy + Math.sin(a) * rr, 2.2 / view.s, 0, 6.283); ctx.fill();
        ctx.globalAlpha = 1;
      });

      // intra-cluster links
      ctx.lineWidth = 1.0 / view.s; ctx.lineCap = "round";
      links.forEach(function (l) {
        var a = l[0], b = l[1], al = (a.dim && b.dim) ? 0.04 : (a.dim || b.dim ? 0.08 : 0.24);
        ctx.strokeStyle = rgb(l[2], al);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      });

      // stars — sun (anchor) 2x with strong glow; core cluster dots brighter
      nodes.forEach(function (nd) {
        var t = TIER[nd.tier], r = (nd.size || t.r) * (nd.sun ? 2 : 1), isH = hover === nd;
        var tw = isH ? 1 : (0.72 + 0.28 * Math.sin(_t * (1.4 + (nd._i % 5) * 0.35) + nd._tp));
        var bright = nd.core ? 1.25 : 1;
        var a = nd.dim ? 0.15 : 1, grad = r * (isH ? 5.5 : (nd.sun ? 4.2 : 3.4));
        ctx.globalAlpha = Math.min(1, (isH ? 1 : 0.62 * tw) * bright) * (nd.dim ? 0.22 : 1);
        ctx.drawImage(GLOW[nd.tier], nd.x - grad, nd.y - grad, grad * 2, grad * 2);
        ctx.globalAlpha = a; ctx.fillStyle = t.core;
        ctx.beginPath(); ctx.arc(nd.x, nd.y, r + (isH ? 1.6 : 0), 0, 6.283); ctx.fill();
        ctx.globalAlpha = a * 0.9; ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.beginPath(); ctx.arc(nd.x - r * 0.3, nd.y - r * 0.3, r * 0.35, 0, 6.283); ctx.fill();
      });
      ctx.globalAlpha = 1;
      ctx.restore();

      // ── SCREEN PASS: all text upright, collision-free (cc#198 rules kept) ──
      var M = 10, placed = []; catHitBoxes = [];
      function hit(b) { return placed.some(function (p) { return b.x < p.x + p.w && b.x + b.w > p.x && b.y < p.y + p.h && b.y + b.h > p.y; }); }

      ctx.font = "700 11px ui-monospace,Menlo,monospace"; ctx.textBaseline = "middle";
      clusters.forEach(function (c) {
        var s = toScreen(c.cx, c.cy), R = c.R * view.s;
        if (s.x < -R - 60 || s.x > W + R + 60 || s.y < -R - 60 || s.y > H + R + 60) return;
        var txt = (c.cat || "").toUpperCase() + " · " + c.arr.length, tw = ctx.measureText(txt).width;
        var above = (s.y - R - 16) > (M + 8);
        var ly = above ? (s.y - R - 12) : (s.y + R + 14);
        ly = Math.max(M + 8, Math.min(H - M, ly));
        var cx = Math.max(tw / 2 + M, Math.min(W - tw / 2 - M, s.x));
        ctx.textAlign = "center";
        ctx.globalAlpha = 0.8; ctx.fillStyle = "rgba(6,9,18,0.9)"; ctx.fillText(txt, cx + 0.6, ly + 0.6);
        ctx.globalAlpha = 0.97; ctx.fillStyle = rgbHex(c.col); ctx.fillText(txt, cx, ly);
        var box = { x: cx - tw / 2 - 4, y: ly - 9, w: tw + 8, h: 18 };
        placed.push(box); catHitBoxes.push({ x: box.x, y: box.y, w: box.w, h: box.h, cat: c.cat });
      });
      ctx.globalAlpha = 1;

      if (view.s > 1.35) {
        ctx.font = "600 10px -apple-system,Segoe UI,sans-serif";
        clusters.forEach(function (c) {
          var sC = toScreen(c.cx, c.cy), R = c.R * view.s;
          c.arr.forEach(function (nd) {
            if (nd.dim || hover === nd) return;
            var sN = toScreen(nd.x, nd.y);
            if (sN.x < -30 || sN.x > W + 30 || sN.y < -30 || sN.y > H + 30) return;
            var dx = sN.x - sC.x, dy = sN.y - sC.y, dd = Math.sqrt(dx * dx + dy * dy) || 0.01, ux = dx / dd, uy = dy / dd;
            var lx = sC.x + ux * (R + 14), ly = sC.y + uy * (R + 14);
            var txt = clip(nd.label, 26), tw = ctx.measureText(txt).width;
            var align = ux > 0.25 ? "left" : (ux < -0.25 ? "right" : "center");
            var bx = align === "left" ? lx : (align === "right" ? lx - tw : lx - tw / 2);
            bx = Math.max(M, Math.min(W - M - tw, bx));
            ly = Math.max(M + 8, Math.min(H - M, ly));
            var box = { x: bx - 3, y: ly - 8, w: tw + 6, h: 16 }, tries = 0;
            while (tries < 12 && hit(box)) {
              if (ly <= M + 9 || ly >= H - M - 1) { bx += (ux >= 0 ? 1 : -1) * (tw * 0.4 + 12); bx = Math.max(M, Math.min(W - M - tw, bx)); box.x = bx - 3; }
              else { ly += (uy >= 0 ? 1 : -1) * 15; ly = Math.max(M + 8, Math.min(H - M, ly)); box.y = ly - 8; }
              tries++;
            }
            placed.push(box);
            var drawX = align === "left" ? bx : (align === "right" ? bx + tw : bx + tw / 2);
            var er = (nd.size || TIER[nd.tier].r) * (nd.sun ? 2 : 1) * view.s;
            ctx.globalAlpha = 0.45; ctx.strokeStyle = rgb(c.col, 0.5); ctx.lineWidth = 0.8;
            ctx.beginPath(); ctx.moveTo(sN.x + ux * er, sN.y + uy * er);
            ctx.lineTo(align === "left" ? bx - 3 : (align === "right" ? bx + tw + 3 : drawX), ly); ctx.stroke();
            ctx.textAlign = align; ctx.globalAlpha = 0.9;
            ctx.fillStyle = "rgba(6,10,22,0.85)"; ctx.fillText(txt, drawX + 0.6, ly + 0.6);
            ctx.fillStyle = "rgba(222,231,252,0.96)"; ctx.fillText(txt, drawX, ly);
          });
        });
        ctx.globalAlpha = 1;
      }

      if (hover) drawTooltip(hover);
      drawShoot(_now);
    }

    function drawTooltip(nd) {
      var s = toScreen(nd.x, nd.y), r = (nd.size || TIER[nd.tier].r) * (nd.sun ? 2 : 1) * view.s;
      var pad = 6, fs = 12, sf = 10;
      ctx.font = "700 " + fs + "px -apple-system,Segoe UI,sans-serif";
      var t1 = clip(nd.label, 46), w1 = ctx.measureText(t1).width;
      var t2 = nd.sublabel || "";
      ctx.font = "600 " + sf + "px -apple-system,Segoe UI,sans-serif";
      var w2 = t2 ? ctx.measureText(t2).width : 0;
      var w = Math.max(w1, w2) + pad * 2, h = (t2 ? fs + sf + 5 : fs) + pad * 2;
      var bx = s.x + r + 8, by = s.y - h - 4;
      if (bx + w > W - 6) bx = s.x - r - 8 - w;
      if (bx < 6) bx = 6;
      if (by < 6) by = s.y + r + 8;
      if (by + h > H - 6) by = H - 6 - h;
      ctx.globalAlpha = 0.97; ctx.fillStyle = "rgba(13,20,40,0.97)";
      roundRect(bx, by, w, h, 6); ctx.fill();
      ctx.strokeStyle = "rgba(120,150,220,0.55)"; ctx.lineWidth = 1;
      roundRect(bx, by, w, h, 6); ctx.stroke();
      ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.globalAlpha = 1; ctx.fillStyle = "#eaf0ff";
      ctx.font = "700 " + fs + "px -apple-system,Segoe UI,sans-serif";
      ctx.fillText(t1, bx + pad, by + pad);
      if (t2) { ctx.fillStyle = "#95a8d6"; ctx.font = "600 " + sf + "px -apple-system,Segoe UI,sans-serif"; ctx.fillText(t2, bx + pad, by + pad + fs + 3); }
      ctx.globalAlpha = 1; ctx.textBaseline = "middle";
    }
    function roundRect(x, y, w, h, r) {
      ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
    }
    function clip(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

    // ── animation loop (global rotation pauses while interacting) ──
    var _now = 0;
    function frame(now) {
      _loop = null; _now = now;
      if (_t0 == null) { _t0 = now; _prev = now; }
      _t = (now - _t0) / 1000;
      var dt = Math.min((now - _prev) / 1000, 0.05); _prev = now;
      var idle = (now - lastInput) > 3000;
      if (MOTION && idle && !dragging && !pinching) gPhase += gOmega * dt;   // resume 3s after last input
      updateShoot(now);
      draw();
      if (MOTION && !document.hidden) _loop = requestAnimationFrame(frame);
    }
    function startLoop() { if (MOTION && _loop == null && !document.hidden) _loop = requestAnimationFrame(frame); }
    function stopLoop() { if (_loop != null) { cancelAnimationFrame(_loop); _loop = null; } }
    function schedule() {
      if (MOTION) { startLoop(); return; }
      if (!raf) raf = requestAnimationFrame(function () { raf = null; _now = performance.now(); draw(); });
    }
    function markInput() { lastInput = (typeof performance !== "undefined" && performance.now) ? performance.now() : _now; }

    // ── interactions ─────────────────────────────────────────────────────────
    var ptrs = {}, moved = false, downXY = null, pinchD0 = 0, pinchS0 = 1;
    function nodeAt(px, py) {
      var wpt = toWorld(px, py), best = null, bd = 1e9;
      for (var i = nodes.length - 1; i >= 0; i--) {
        var nd = nodes[i], t = TIER[nd.tier], r = (nd.size || t.r) * (nd.sun ? 2 : 1) + 6;
        var dx = nd.x - wpt.x, dy = nd.y - wpt.y, d = dx * dx + dy * dy;
        if (d < r * r * (1 / (view.s * view.s)) && d < bd) { bd = d; best = nd; }
      }
      return best;
    }
    function catLabelAt(px, py) {
      for (var i = 0; i < catHitBoxes.length; i++) {
        var b = catHitBoxes[i];
        if (px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h) return b.cat;
      }
      return null;
    }

    var lpTimer = null, lpFired = false;
    function clearLP() { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } }
    function onDown(e) {
      canvas.setPointerCapture(e.pointerId);
      ptrs[e.pointerId] = { x: e.clientX, y: e.clientY }; markInput();
      var n = Object.keys(ptrs).length;
      if (n === 1) {
        dragging = true; moved = false; lpFired = false; downXY = rel(e);
        clearLP();
        lpTimer = setTimeout(function () {
          if (!moved) { var nd = nodeAt(downXY.x, downXY.y); if (nd) { hover = nd; hoverCat = nd.category; lpFired = true; schedule(); } }
        }, 380);
      } else if (n === 2) { clearLP(); pinching = true; var d = pinchDist(); pinchD0 = d.d; pinchS0 = view.s; }
    }
    function onMove(e) {
      if (!(e.pointerId in ptrs)) {
        var r = rel(e), nd = nodeAt(r.x, r.y);
        if (DO_PARALLAX) { mouseNX = W ? (r.x / W - 0.5) : 0; mouseNY = H ? (r.y / H - 0.5) : 0; }
        if (nd !== hover) { hover = nd; hoverCat = nd ? nd.category : null; canvas.style.cursor = nd ? "pointer" : "grab"; schedule(); }
        return;
      }
      var prev = ptrs[e.pointerId]; ptrs[e.pointerId] = { x: e.clientX, y: e.clientY }; markInput();
      var n = Object.keys(ptrs).length;
      if (n >= 2) {
        var d = pinchDist();
        if (pinchD0 > 0) zoomAt(d.cx, d.cy, Math.max(0.12, Math.min(4, pinchS0 * d.d / pinchD0)));
        moved = true; return;
      }
      if (dragging) {
        var dx = e.clientX - prev.x, dy = e.clientY - prev.y;
        if (Math.abs(e.clientX - downXY.gx) + Math.abs(e.clientY - downXY.gy) > 6) { moved = true; clearLP(); }
        view.tx += dx; view.ty += dy; schedule();
      }
    }
    function onUp(e) {
      clearLP();
      var wasTap = dragging && !moved && !lpFired, r = downXY;
      delete ptrs[e.pointerId];
      try { canvas.releasePointerCapture(e.pointerId); } catch (_) {}
      var left = Object.keys(ptrs).length;
      if (left === 0) { dragging = false; pinching = false; } else if (left < 2) pinching = false;
      markInput();
      if (wasTap && r) {
        var nd = nodeAt(r.x, r.y);
        if (nd) { if (opts.onTap) opts.onTap(nd); return; }
        var cat = catLabelAt(r.x, r.y);
        if (cat) { zoomToCategory(cat); if (opts.onCategoryTap) opts.onCategoryTap(cat); }
      }
      lpFired = false;
    }
    function onWheel(e) {
      e.preventDefault(); markInput();
      var r = rel(e), factor = Math.pow(1.0015, -e.deltaY);
      zoomAt(r.x, r.y, Math.max(0.12, Math.min(4, view.s * factor)));
    }
    function zoomAt(px, py, ns) { var w = toWorld(px, py); view.s = ns; var p = rot(w.x, w.y, gPhase); view.tx = px - p.x * ns; view.ty = py - p.y * ns; schedule(); }
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
    function onVis() { if (document.hidden) stopLoop(); else { _prev = null; schedule(); } }
    document.addEventListener("visibilitychange", onVis);

    // ── public API ─────────────────────────────────────────────────────────────
    function setDim(pred) { nodes.forEach(function (nd) { nd.dim = pred ? !pred(nd) : false; }); schedule(); }
    function destroy() {
      if (ro) ro.disconnect(); stopLoop();
      if (raf) cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange", onVis);
      canvas.remove();
    }

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

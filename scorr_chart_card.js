/* scorr_chart_card.js — cc#706 SHARED "V8-type" price chart card (site-wide, self-contained).
 *
 * One shared component so the GVM page (cc#710 C button) and SmartGain rows (cc#711 CARD button)
 * open the SAME V8-style candle card — not a fork. It is ADDITIVE: it injects its own overlay modal
 * and lazy-loads LightweightCharts (same unpkg build the V8 dashboard uses) only if absent. It does
 * NOT touch the live V8 dashboard or the GVM clean Price chart (both kept per founder 27-Jul).
 *
 * API:  window.ScorrChartCard.open(symbol, {theme:'light'|'dark'})
 *       theme auto-detected from the host page when omitted (light on GVM/SmartGain, dark on V8-like).
 *
 * Data:  daily 1M/3M/6M/1Y/ALL  -> GET /api/candles/{sym}?days=N   (raw_prices; all stocks)
 *        5-min intraday      -> GET /api/intraday/{sym}?sessions=N  (fyers feed; FUTURES universe only)
 * 5m gating: probed once per symbol via a 1-session intraday call — rows => futures (5m enabled),
 *            empty => non-futures (5m greyed with "5-min available for F&O stocks" tooltip); daily default.
 * Times are IST (Asia/Kolkata). Crosshair/tooltip is LightweightCharts-native.
 *
 * cc#730: Pivots + Fib overlays (default ON) so EVERY C button that opens this card renders the same
 * fib+pivot chart the V8 dashboard shows. Pivots = latest v8_paper_pivots row via GET
 * /api/trade-check/fibcheck (cc#478; full universe per cc#342, so non-futures GVM symbols have pivots
 * too). Fib = retracement levels off the loaded-range swing (same derivation as the V8 chart, cc#668),
 * both drawn as LightweightCharts price lines. Toggle buttons + persistence mirror the V8 card.
 */
(function () {
  "use strict";
  if (window.ScorrChartCard) return;

  var LWC_SRC = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";
  // cc#752: TF row unified with the V8 dashboard chart (qaChart _QA_TF) so every surface — V8,
  // SmartGain, TC cards, GVM "C" — shows the SAME timeframes. 5m=intraday; ALL=full stored history.
  var TF = { "5m": null, "1M": 30, "3M": 90, "6M": 180, "1Y": 365, "ALL": 365 * 20 };
  var TF_ORDER = ["5m", "1M", "3M", "6M", "1Y", "ALL"];

  var _chart = null, _series = null, _sym = null, _tf = "3M", _theme = "light";
  var _gvmSeries = null;            // cc#779: GVM quality-trend line (secondary fixed 0-10 axis)
  var _verdict = null;              // cc#779: cached trend verdict for the current symbol+timeframe
  var _full = false;                // cc#779: fullscreen state
  var GVM_COL = "#7b6bd6";          // muted violet — distinct from price/pivot/fib/VWAP palettes
  var _futCache = {};   // {sym: bool} — 5m availability, cached per session (no repeat probe)
  // cc#730: fib + pivot overlay state (default BOTH on so the card matches the V8 chart out of the box).
  var _fibCache = {};   // {sym: fibcheck json} — pivots fetched once per symbol
  var _priceLines = [], _lastData = [];
  var _vwapSeries = null, _vwapLast = null;   // cc#755: session-VWAP line series (intraday only) + latest value for the chip
  var _ov = _readOv();
  var FIB_RATIOS = [0, 23.6, 38.2, 50, 61.8, 78.6, 100, 123.6];   // cc#668 ladder (0=swing low, 100=high, 123.6=extension)
  var FIB_ZLINE = { breakout: "#0a9e63", resist: "#0a9e63", strength: "#12864f", decision: "#8a94ad", weak: "#dd3a4a", breakdown: "#dd3a4a" };
  // cc#750: fib ZONE bands (tint fills between consecutive fib levels). Edges are the exact fib ratios so
  // a band snaps flush to its bounding lines by construction. Consistent green->neutral->red ramp, top→bottom.
  var FIB_ZONES = [
    { key: "EXTENSION",          lo: 100,  hi: 123.6, col: "#0a9e63" },
    { key: "STRENGTH·RESIST",    lo: 61.8, hi: 100,   col: "#12864f" },
    { key: "DECISION",           lo: 38.2, hi: 61.8,  col: "#8a94ad" },
    { key: "WEAK",               lo: 23.6, hi: 38.2,  col: "#e0913a" },
    { key: "BREAKDOWN",          lo: 0,    hi: 23.6,  col: "#dd3a4a" }
  ];
  var _fibBand = null;   // cc#750: {lo, rng} of the loaded-range swing, for priceToCoordinate band placement
  function _readOv() {
    var vwap = false;   // cc#755: VWAP is an opt-in overlay (default off), persisted separately so the
    try { vwap = localStorage.getItem("scorr_chart_vwap") === "1"; } catch (e) {}   // pivot/fib scheme is untouched
    var gvm = false;    // cc#779: GVM quality-trend line, opt-in, persisted separately (same reason)
    try { gvm = localStorage.getItem("scorr_chart_gvm") === "1"; } catch (e) {}
    try { var s = localStorage.getItem("scorr_chart_overlay");
      if (s === "none") return { pivot: false, fib: false, vwap: vwap, gvm: gvm };
      if (s === "pivot") return { pivot: true, fib: false, vwap: vwap, gvm: gvm };
      if (s === "fib") return { pivot: false, fib: true, vwap: vwap, gvm: gvm };
    } catch (e) {}
    return { pivot: true, fib: true, vwap: vwap, gvm: gvm };   // default: pivots + fib on, VWAP/GVM off
  }
  function _ovStr() { return _ov.pivot && _ov.fib ? "both" : _ov.pivot ? "pivot" : _ov.fib ? "fib" : "none"; }
  function _fibZoneKey(p) {
    if (p == null) return null;
    if (p >= 100) return "breakout"; if (p >= 78.6) return "resist"; if (p >= 61.8) return "strength";
    if (p >= 38.2) return "decision"; if (p >= 23.6) return "weak"; return "breakdown";
  }

  var IST_MONY = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "2-digit" });
  var IST_MY = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", month: "short", year: "2-digit" });
  var IST_HM = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
  var IST_DHMY = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });

  function _istDate(t) {
    if (t == null) return null;
    if (typeof t === "number") return { d: new Date(t * 1000), dateOnly: false };
    if (typeof t === "object" && t.year) return { d: new Date(Date.UTC(t.year, t.month - 1, t.day)), dateOnly: true };
    if (typeof t === "string") { var p = t.split("-"); if (p.length >= 3) return { d: new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])), dateOnly: true }; }
    return null;
  }
  function _istTick(t, tickType) {
    var o = _istDate(t); if (!o) return "";
    if (o.dateOnly) return (tickType != null && tickType <= 1) ? IST_MY.format(o.d) : IST_MONY.format(o.d);
    return (tickType != null && tickType <= 2) ? IST_MONY.format(o.d) : IST_HM.format(o.d);
  }
  function _istCross(t) { var o = _istDate(t); return o ? (o.dateOnly ? IST_MONY.format(o.d) : IST_DHMY.format(o.d)) : ""; }

  function _detectTheme() {
    try {
      var b = document.body, r = document.documentElement;
      var dt = (r.getAttribute("data-theme") || b.getAttribute("data-theme") || "").toLowerCase();
      if (dt === "dark") return "dark";
      if (dt === "light") return "light";
      var bg = getComputedStyle(b).backgroundColor || "";
      var m = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      if (m) { var lum = (+m[1] * 0.299 + +m[2] * 0.587 + +m[3] * 0.114); return lum < 110 ? "dark" : "light"; }
    } catch (e) {}
    return "light";
  }
  function _pal() {
    return _theme === "dark"
      ? { panel: "#1c2536", line: "#2a3548", txt: "#e8ecf2", mut: "#9aa4b5", sub: "#5a6781", grid: "rgba(150,160,180,.14)", btn: "#0f1623", btnOn: "#4d7cfe" }
      : { panel: "#ffffff", line: "#e6eaf0", txt: "#1c2536", mut: "#5a6b82", sub: "#8a94a6", grid: "rgba(120,130,150,.12)", btn: "#f4f6fa", btnOn: "#4d7cfe" };
  }

  function _ensureLib(cb) {
    if (window.LightweightCharts) return cb(true);
    var ex = document.getElementById("scorr-lwc-lib");
    if (ex) { ex.addEventListener("load", function () { cb(!!window.LightweightCharts); }); return; }
    var s = document.createElement("script");
    s.id = "scorr-lwc-lib"; s.src = LWC_SRC; s.async = true;
    s.onload = function () { cb(!!window.LightweightCharts); };
    s.onerror = function () { cb(false); };
    document.head.appendChild(s);
  }

  // cc#779: Esc exits fullscreen FIRST (one step back, as the spec's "Esc or collapse icon returns
  // to the modal" requires); a second Esc closes the card.
  function _esc(k) { if (k.key === "Escape") { if (_full) _toggleFull(false); else close(); } }

  function _buildModal() {
    var ov = document.getElementById("scorrChartOv");
    if (ov) return ov;
    var p = _pal();
    ov = document.createElement("div");
    ov.id = "scorrChartOv";
    ov.style.cssText = "position:fixed;inset:0;z-index:12000;background:rgba(10,16,25,.5);display:none;align-items:center;justify-content:center;padding:16px";
    ov.innerHTML =
      '<div id="scorrChartBoxWrap" style="border-radius:16px;box-shadow:0 24px 64px rgba(0,0,0,.32);width:min(94vw,640px);max-height:88vh;overflow:hidden;display:flex;flex-direction:column">' +
        '<div id="scorrChartHead" style="display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid">' +
          '<b id="scorrChartTitle" style="font-size:14px"></b>' +
          '<span id="scorrChartHL" style="margin-left:6px;font-size:11.5px"></span>' +
          '<span id="scorrChartVerdict" style="margin-left:6px"></span>' +   /* cc#779 trend badge */
          '<span style="margin-left:auto;display:flex;gap:5px;align-items:center;flex-wrap:wrap;justify-content:flex-end" id="scorrChartTfs"></span>' +
          '<button id="scorrChartFull" title="Maximize (Esc to exit)" style="border:none;background:none;font-size:15px;line-height:1;cursor:pointer;margin-left:4px">&#9974;</button>' +
          '<button id="scorrChartClose" style="border:none;background:none;font-size:20px;line-height:1;cursor:pointer;margin-left:4px">&times;</button>' +
        '</div>' +
        '<div id="scorrChartBox" style="width:100%;height:412px;padding:8px 8px 0"></div>' +
        '<div id="scorrChartMsg" style="padding:6px 16px 12px;font-size:11px"></div>' +
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    ov.querySelector("#scorrChartClose").addEventListener("click", close);
    ov.querySelector("#scorrChartFull").addEventListener("click", function (e) { e.stopPropagation(); _toggleFull(); });
    return ov;
  }

  // cc#779: MAXIMIZE — expand the same modal to the full viewport (desktop: browser window; mobile:
  // full screen, landscape-friendly). Every control persists because we only restyle the existing
  // wrapper — the timeframe pills, Pivots/Fib/VWAP/GVM toggles, verdict badge and crosshair are the
  // same DOM. Esc or the collapse icon returns to the modal (close() also resets the flag).
  function _toggleFull(force) {
    _full = (force === undefined) ? !_full : !!force;
    var wrap = document.getElementById("scorrChartBoxWrap");
    var box = document.getElementById("scorrChartBox");
    var ovEl = document.getElementById("scorrChartOv");
    var btn = document.getElementById("scorrChartFull");
    if (!wrap || !box) return;
    if (_full) {
      if (ovEl) ovEl.style.padding = "0";
      wrap.style.width = "100vw"; wrap.style.maxHeight = "100vh"; wrap.style.height = "100vh";
      wrap.style.borderRadius = "0";
      box.style.height = "calc(100vh - 108px)";
      if (btn) { btn.innerHTML = "&#10066;"; btn.title = "Exit fullscreen (Esc)"; }
    } else {
      if (ovEl) ovEl.style.padding = "16px";
      wrap.style.width = "min(94vw,640px)"; wrap.style.maxHeight = "88vh"; wrap.style.height = "";
      wrap.style.borderRadius = "16px";
      box.style.height = "412px";
      if (btn) { btn.innerHTML = "&#9974;"; btn.title = "Maximize (Esc to exit)"; }
    }
    try { if (_chart) _chart.applyOptions({ width: box.clientWidth, height: box.clientHeight }); } catch (e) {}
    _renderFx();
  }

  // cc#779: trend-strength badge next to the return %. Server-computed (one endpoint, cached per
  // symbol+timeframe per day) so the rule table lives in ONE place and cannot drift per surface.
  function _loadVerdict() {
    var host = document.getElementById("scorrChartVerdict");
    if (!host) return;
    host.innerHTML = "";
    _verdict = null;
    if (_tf === "5m" || !_sym) return;   // daily-series verdict; meaningless intraday
    var sym = _sym, tf = _tf;
    _getJSON("/api/gvm/trend_verdict/" + encodeURIComponent(sym) + "?tf=" + encodeURIComponent(tf))
      .then(function (d) {
        if (_sym !== sym || _tf !== tf) return;          // user switched mid-flight
        var v = d && d.verdict;
        if (!v) return;                                   // no verdict -> no badge, never a guess
        _verdict = v;
        var h = document.getElementById("scorrChartVerdict");
        if (!h) return;
        h.innerHTML = '<span id="scorrChartVerdictChip" role="button" tabindex="0" title="Tap for the inputs behind this verdict" ' +
          'style="cursor:pointer;font:800 9.5px/1 -apple-system,Segoe UI,sans-serif;letter-spacing:.03em;color:#fff;' +
          'background:' + v.color + ';border-radius:5px;padding:3px 6px;white-space:nowrap">' + v.label + '</span>';
        var chip = document.getElementById("scorrChartVerdictChip");
        var show = function (e) { e.stopPropagation(); _verdictPopup(v); };
        chip.addEventListener("click", show);
        chip.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(e); } });
      })
      .catch(function () { /* badge is additive — never break the chart */ });
  }

  // Plain-language popup: the three inputs with their current values + one line of meaning.
  function _verdictPopup(v) {
    var p = _pal();
    var old = document.getElementById("scorrVerdictPop");
    if (old) { old.remove(); return; }                    // tap again to dismiss
    var MEAN = {
      "1_VERY_STRONG": "Price is climbing and business quality is improving from an already-high base — the strongest combination.",
      "2_STRONG": "Price is climbing with quality either improving or holding firm at a good level.",
      "3_STEADY": "Nothing is breaking down, but there is no strong quality signal either way — a hold-and-watch picture.",
      "4_CAUTION": "Price is rising while quality is FALLING from a high base. Quality slipping against a price tailwind is a real warning, not noise.",
      "5_WEAK": "Quality is deteriorating, and price is either falling with it or rising on a low-quality base.",
      "6_QUALITY_DIP": "Price is down but quality is high and not falling — the classic accumulation-candidate pattern."
    };
    var arrow = function (s) { return s === "up" ? "rising" : s === "down" ? "falling" : "flat"; };
    var lvlTxt = v.level === "high" ? "high (≥7)" : v.level === "mid" ? "mid (6–7)" : "low (<6)";
    var d = document.createElement("div");
    d.id = "scorrVerdictPop";
    d.style.cssText = "position:fixed;z-index:12100;max-width:300px;background:" + p.panel + ";color:" + p.txt +
      ";border:1px solid " + p.line + ";border-radius:11px;box-shadow:0 14px 40px rgba(0,0,0,.28);padding:12px 14px;font:12px/1.55 -apple-system,Segoe UI,sans-serif";
    d.innerHTML =
      '<div style="font:800 11px/1 -apple-system,Segoe UI,sans-serif;color:' + v.color + ';margin-bottom:7px">' + v.label + '</div>' +
      '<div><b>Price</b> ' + arrow(v.price_state) + ' &middot; ' + (v.price_chg_pct >= 0 ? "+" : "") + v.price_chg_pct + '% over ' + _tf + '</div>' +
      '<div><b>GVM trend</b> ' + arrow(v.gvm_state) + ' &middot; ' + v.gvm_then + ' → ' + v.gvm_now + ' (' + (v.gvm_delta >= 0 ? "+" : "") + v.gvm_delta + ')</div>' +
      '<div><b>Quality level</b> ' + lvlTxt + '</div>' +
      '<div style="margin-top:8px;color:' + p.sub + '">' + (MEAN[v.key] || "") + '</div>' +
      '<div style="margin-top:7px;font-size:10.5px;color:' + p.sub + '">Bands: price flat ±2% &middot; GVM flat ±0.15 pts &middot; window = selected timeframe.</div>';
    document.body.appendChild(d);
    var chip = document.getElementById("scorrChartVerdictChip");
    var r = chip ? chip.getBoundingClientRect() : { left: 40, bottom: 60 };
    d.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 316)) + "px";
    d.style.top = (r.bottom + 8) + "px";
    setTimeout(function () {
      document.addEventListener("click", function h() { var x = document.getElementById("scorrVerdictPop"); if (x) x.remove(); document.removeEventListener("click", h); });
    }, 0);
  }

  function _paintChrome() {
    var p = _pal();
    var wrap = document.getElementById("scorrChartBoxWrap");
    wrap.style.background = p.panel; wrap.style.border = "1px solid " + p.line;
    document.getElementById("scorrChartHead").style.borderBottom = "1px solid " + p.line;
    document.getElementById("scorrChartTitle").style.color = p.txt;
    document.getElementById("scorrChartClose").style.color = p.mut;
    var _fb = document.getElementById("scorrChartFull"); if (_fb) _fb.style.color = p.mut;   // cc#779
    document.getElementById("scorrChartMsg").style.color = p.sub;
    // timeframe buttons
    var host = document.getElementById("scorrChartTfs");
    host.innerHTML = "";
    TF_ORDER.forEach(function (k) {
      var b = document.createElement("button");
      b.textContent = k; b.setAttribute("data-tf", k);
      var is5 = (k === "5m");
      var futOk = _futCache[_sym] !== false;   // undefined (not probed) or true => allow; false => grey
      var disabled = is5 && !futOk;
      var on = (k === _tf);
      b.style.cssText = "padding:4px 9px;border-radius:7px;font:700 11.5px/1 -apple-system,Segoe UI,sans-serif;cursor:pointer;border:1px solid " + p.line +
        ";background:" + (on ? p.btnOn : p.btn) + ";color:" + (on ? "#fff" : p.mut) +
        (disabled ? ";opacity:.4;cursor:not-allowed" : "");
      if (disabled) b.title = "5-min available for F&O (futures) stocks";
      if (!disabled) b.onclick = function () { _load(k); };
      host.appendChild(b);
    });
    // cc#730: Pivots / Fib overlay toggles (mirror the V8 card). Pivots suppressed at ALL — rolling
    // levels are meaningless at multi-year scale (same rule as the V8 chart's ALL guard).
    var sep = document.createElement("span");
    sep.style.cssText = "width:1px;height:16px;background:" + p.line + ";margin:0 2px;align-self:center";
    host.appendChild(sep);
    [["pivot", "Pivots", "Pivots — PP / R1 / R2 / S1 / S2 (v8_paper_pivots)"],
     ["fib", "Fib", "Fibonacci retracement levels (loaded-range swing, same as the V8 chart)"]].forEach(function (o) {
      var b = document.createElement("button");
      b.textContent = o[1];
      var pivBlocked = (o[0] === "pivot" && _tf === "ALL");
      var on = _ov[o[0]] && !pivBlocked;
      b.style.cssText = "padding:4px 9px;border-radius:7px;font:700 11.5px/1 -apple-system,Segoe UI,sans-serif;cursor:pointer;border:1px solid " + p.line +
        ";background:" + (on ? p.btnOn : p.btn) + ";color:" + (on ? "#fff" : p.mut) + (pivBlocked ? ";opacity:.4;cursor:not-allowed" : "");
      b.title = pivBlocked ? "Pivots hidden at ALL (rolling levels meaningless at full-history scale)" : o[2];
      if (!pivBlocked) b.onclick = function () { _toggleOv(o[0]); };
      host.appendChild(b);
    });
    // cc#755: VWAP toggle — third chip, independent of Pivots/Fib. Session VWAP is intraday-only, so it
    // is greyed on every EOD timeframe (1M/3M/6M/1Y/ALL). Value shows in the chip strip, never the axis.
    (function () {
      var b = document.createElement("button");
      b.textContent = "VWAP";
      var vwapBlocked = (_tf !== "5m");
      var on = _ov.vwap && !vwapBlocked;
      b.style.cssText = "padding:4px 9px;border-radius:7px;font:700 11.5px/1 -apple-system,Segoe UI,sans-serif;cursor:pointer;border:1px solid " + p.line +
        ";background:" + (on ? "#f5a623" : p.btn) + ";color:" + (on ? "#1c2536" : p.mut) + (vwapBlocked ? ";opacity:.4;cursor:not-allowed" : "");
      b.title = vwapBlocked ? "Session VWAP is intraday-only — switch to the 5m timeframe" : "Session VWAP (cumulative price·volume / volume, from 09:15; resets each session)";
      if (!vwapBlocked) b.onclick = function () { _toggleOv("vwap"); };
      host.appendChild(b);
    })();
    // cc#779: GVM quality-trend toggle — fourth chip. The GVM series is a DAILY score, so it is
    // greyed on the 5m intraday timeframe (nothing to plot at intraday resolution).
    (function () {
      var b = document.createElement("button");
      b.textContent = "GVM";
      var gBlocked = (_tf === "5m");
      var on = _ov.gvm && !gBlocked;
      b.style.cssText = "padding:4px 9px;border-radius:7px;font:700 11.5px/1 -apple-system,Segoe UI,sans-serif;cursor:pointer;border:1px solid " + p.line +
        ";background:" + (on ? GVM_COL : p.btn) + ";color:" + (on ? "#fff" : p.mut) + (gBlocked ? ";opacity:.4;cursor:not-allowed" : "");
      b.title = gBlocked ? "GVM is a daily quality score — switch to 1M or longer" :
        "GVM quality trend (gvm_history, full score) on a fixed 0-10 right axis";
      if (!gBlocked) b.onclick = function () { _toggleOv("gvm"); };
      host.appendChild(b);
    })();
  }
  function _toggleOv(kind) {
    _ov[kind] = !_ov[kind];
    try {
      if (kind === "vwap") localStorage.setItem("scorr_chart_vwap", _ov.vwap ? "1" : "0");
      else if (kind === "gvm") localStorage.setItem("scorr_chart_gvm", _ov.gvm ? "1" : "0");
      else localStorage.setItem("scorr_chart_overlay", _ovStr());
    } catch (e) {}
    _paintChrome();
    if (kind === "vwap") { _applyVwap(); _renderFx(); }
    else if (kind === "gvm") { _applyGvm(); }
    else { _applyOverlays(); }
  }

  // cc#779: GVM quality-trend line on a SECONDARY right axis with a FIXED 0-10 scale, so the line's
  // vertical position always means the same thing (a 7 sits at the same height on every chart) and
  // never rescales with price. Muted colour + thin stroke — context, never a price signal.
  function _applyGvm() {
    if (!_chart) return;
    if (_gvmSeries) { try { _chart.removeSeries(_gvmSeries); } catch (e) {} _gvmSeries = null; }
    if (!_ov.gvm || _tf === "5m") return;
    var days = TF[_tf] || 365;
    var sym = _sym;
    _getJSON("/api/gvm/history/" + encodeURIComponent(sym) + "?days=" + Math.min(days + 5, 2500))
      .then(function (d) {
        if (!_chart || _sym !== sym || !_ov.gvm) return;           // modal closed / symbol changed mid-flight
        var pts = ((d && d.points) || []).filter(function (r) { return r.gvm_score != null; })
          .map(function (r) { return { time: String(r.score_date).slice(0, 10), value: +r.gvm_score }; })
          .sort(function (a, b) { return a.time < b.time ? -1 : 1; });
        if (!pts.length) return;
        _gvmSeries = _chart.addLineSeries({
          color: GVM_COL, lineWidth: 2, priceScaleId: "gvm",
          priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 }
        });
        _gvmSeries.setData(pts);
        try {
          _chart.priceScale("gvm").applyOptions({
            scaleMargins: { top: 0.05, bottom: 0.05 }, visible: true, borderVisible: false,
            autoScale: false
          });
          // FIXED 0-10: pin the range so the axis cannot autoscale to the data's own min/max.
          _gvmSeries.applyOptions({ autoscaleInfoProvider: function () {
            return { priceRange: { minValue: 0, maxValue: 10 } };
          } });
        } catch (e) {}
      })
      .catch(function () { /* quality line is additive — never break the price chart */ });
  }
  // cc#755: session-VWAP line (intraday only). Cumulative SUM(close·volume)/SUM(volume) from each
  // session's first bar (~09:15), reset when the bar's date changes. A native LightweightCharts line
  // series so it tracks the price scale; the latest value is surfaced as a chip (never on the axis).
  function _applyVwap() {
    if (!_chart) return;
    if (_vwapSeries) { try { _chart.removeSeries(_vwapSeries); } catch (e) {} _vwapSeries = null; }
    _vwapLast = null;
    if (!(_ov.vwap && _tf === "5m" && _lastData && _lastData.length)) return;
    var pts = [], cumPV = 0, cumV = 0, day = null;
    for (var i = 0; i < _lastData.length; i++) {
      var bar = _lastData[i];
      if (bar._day !== day) { day = bar._day; cumPV = 0; cumV = 0; }   // reset at each session boundary
      var v = isFinite(bar.volume) ? bar.volume : 0;
      cumPV += bar.close * v; cumV += v;
      pts.push({ time: bar.time, value: cumV > 0 ? cumPV / cumV : bar.close });
    }
    if (!pts.length) return;
    _vwapSeries = _chart.addLineSeries({ color: "#f5a623", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    _vwapSeries.setData(pts);
    _vwapLast = pts[pts.length - 1].value;
  }
  // cc#730/#750: pivot + fib overlays. Pivot LEVELS + fib LINES are native price lines (they track the
  // scale); cc#750 removes the pivot AXIS badges (they collided with the price ticks + CMP tag) in favour
  // of a compact top-left chip strip, and adds fib ZONE tint bands with right-inside labels (an HTML layer
  // positioned via priceToCoordinate, repositioned on pan/zoom/resize). Only the CMP tag stays on the axis.
  function _applyOverlays() {
    if (!_series) return;
    _priceLines.forEach(function (pl) { try { _series.removePriceLine(pl); } catch (e) {} });
    _priceLines = [];
    if (_ov.pivot && _tf !== "ALL") {
      var drawPiv = function (f) {
        if (!_series || !f || !f.pivots) return; var P = f.pivots;
        // cc#750: axisLabelVisible:false — values now live in the top-left chip strip, not on the axis.
        [["R2", P.r2, "#0a9e63"], ["R1", P.r1, "#0a9e63"], ["PP", P.pp, "#8a94ad"], ["S1", P.s1, "#dd3a4a"], ["S2", P.s2, "#dd3a4a"]].forEach(function (row) {
          if (row[1] != null) _priceLines.push(_series.createPriceLine({ price: +row[1], color: row[2], lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" }));
        });
        _renderFx();
      };
      var cached = _fibCache[_sym];
      if (cached) { drawPiv(cached); }
      else {
        _getJSON("/api/trade-check/fibcheck?symbol=" + encodeURIComponent(_sym) + "&lookback=6m")
          .then(function (f) { _fibCache[_sym] = f; if (_ov.pivot && _tf !== "ALL") drawPiv(f); }).catch(function () {});
      }
    }
    _fibBand = null;
    if (_ov.fib && _lastData && _lastData.length) {
      var hi = -Infinity, lo = Infinity;
      _lastData.forEach(function (d) { if (isFinite(d.high) && d.high > hi) hi = d.high; if (isFinite(d.low) && d.low < lo) lo = d.low; });
      if (isFinite(hi) && isFinite(lo) && hi > lo) {
        var rng = hi - lo;
        _fibBand = { lo: lo, rng: rng };   // cc#750: drive the tint bands off the SAME swing as the lines
        FIB_RATIOS.forEach(function (r) {
          var col = FIB_ZLINE[_fibZoneKey(r)] || "#8a94ad";
          _priceLines.push(_series.createPriceLine({ price: lo + rng * r / 100, color: col, lineWidth: 1, lineStyle: 3, axisLabelVisible: false, title: "" }));
        });
      }
    }
    _renderFx();
  }

  // cc#750: the HTML FX layer over the plot area — pivot chip strip (top-left) + fib zone tint bands
  // with right-inside labels. pointer-events:none so the native crosshair/tooltip still work.
  function _ensureFx() {
    var box = document.getElementById("scorrChartBox"); if (!box) return null;
    box.style.position = "relative";
    var fx = document.getElementById("scorrChartFx");
    if (!fx) {
      fx = document.createElement("div"); fx.id = "scorrChartFx";
      fx.style.cssText = "position:absolute;left:8px;top:8px;pointer-events:none;overflow:hidden;z-index:3";
      box.appendChild(fx);
    }
    return fx;
  }
  function _renderFx() {
    var fx = _ensureFx(); if (!fx) return;
    var pal = _pal();
    var html = "";
    var chipBg = (_theme === "dark" ? "rgba(18,24,36,.72)" : "rgba(255,255,255,.78)");
    var pivShown = false;
    // pivot chip strip — one row, top-left; R green / PP gray / S red; semi-transparent bg.
    if (_ov.pivot && _tf !== "ALL") {
      var f = _fibCache[_sym];
      if (f && f.pivots) {
        var P = f.pivots;
        var chips = [["R2", P.r2, "#0a9e63"], ["R1", P.r1, "#0a9e63"], ["PP", P.pp, "#8a94ad"], ["S1", P.s1, "#dd3a4a"], ["S2", P.s2, "#dd3a4a"]]
          .filter(function (r) { return r[1] != null; })
          .map(function (r) { return '<span style="color:' + r[2] + ';font-weight:700">' + r[0] + '</span>&nbsp;<span style="color:' + pal.txt + '">' + Number(r[1]).toLocaleString("en-IN", { maximumFractionDigits: 1 }) + '</span>'; })
          .join('<span style="color:' + pal.sub + ';margin:0 5px">|</span>');
        if (chips) { html += '<div class="sc-pivchip" style="position:absolute;left:0;top:0;font:600 10.5px/1.2 -apple-system,Segoe UI,sans-serif;background:' + chipBg + ';border:1px solid ' + pal.line + ';border-radius:7px;padding:3px 7px;white-space:nowrap;backdrop-filter:blur(2px)">' + chips + '</div>'; pivShown = true; }
      }
    }
    // cc#755: VWAP value chip (amber) — top-left, below the pivot strip if present. Intraday only.
    if (_ov.vwap && _tf === "5m" && _vwapLast != null && isFinite(_vwapLast)) {
      html += '<div class="sc-vwapchip" style="position:absolute;left:0;top:' + (pivShown ? "26px" : "0") + ';font:600 10.5px/1.2 -apple-system,Segoe UI,sans-serif;background:' + chipBg + ';border:1px solid ' + pal.line + ';border-radius:7px;padding:3px 7px;white-space:nowrap;backdrop-filter:blur(2px)"><span style="color:#f5a623;font-weight:800">VWAP</span>&nbsp;<span style="color:' + pal.txt + '">' + Number(_vwapLast).toLocaleString("en-IN", { maximumFractionDigits: 1 }) + '</span></div>';
    }
    // fib zone bands (positioned in _positionFx); one label per band, right-inside vertical stack.
    if (_ov.fib && _fibBand) {
      FIB_ZONES.forEach(function (z, i) {
        html += '<div class="sc-fibz" data-zi="' + i + '" style="position:absolute;left:0;right:0;display:none;background:' + _rgba(z.col, 0.09) + '">' +
          '<span style="position:absolute;right:4px;top:50%;transform:translateY(-50%);font:700 9px/1 -apple-system,Segoe UI,sans-serif;letter-spacing:.4px;text-transform:uppercase;font-variant:small-caps;color:' + z.col + ';white-space:nowrap;opacity:.92">' + z.key + '</span></div>';
      });
    }
    fx.innerHTML = html;
    _positionFx();
  }
  function _rgba(hex, a) {
    var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
    return m ? "rgba(" + parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," + parseInt(m[3], 16) + "," + a + ")" : hex;
  }
  function _positionFx() {
    if (!_chart || !_series) return;
    var box = document.getElementById("scorrChartBox"); var fx = document.getElementById("scorrChartFx");
    if (!box || !fx) return;
    var rightW = 0, botH = 0;
    try { rightW = _chart.priceScale("right").width() || 0; } catch (e) {}
    try { botH = _chart.timeScale().height() || 0; } catch (e) {}
    var plotW = Math.max(0, box.clientWidth - 16 - rightW);   // 16 = fx left+right inset (8+8)
    var plotH = Math.max(0, box.clientHeight - 8 - botH);     // 8 = fx top inset
    fx.style.width = plotW + "px"; fx.style.height = plotH + "px";
    if (!(_ov.fib && _fibBand)) return;
    var lo = _fibBand.lo, rng = _fibBand.rng;
    fx.querySelectorAll(".sc-fibz").forEach(function (el) {
      var z = FIB_ZONES[+el.getAttribute("data-zi")];
      var yTop = _series.priceToCoordinate(lo + rng * z.hi / 100);
      var yBot = _series.priceToCoordinate(lo + rng * z.lo / 100);
      if (yTop == null || yBot == null) { el.style.display = "none"; return; }
      var top = Math.max(0, Math.min(yTop, yBot)), bot = Math.min(plotH, Math.max(yTop, yBot));
      if (bot - top < 2) { el.style.display = "none"; return; }
      el.style.display = "block"; el.style.top = top + "px"; el.style.height = (bot - top) + "px";
    });
  }

  function _autoscale() { return null; }

  function _setHL(data, hoverTxt) {
    var el = document.getElementById("scorrChartHL"); if (!el) return;
    var p = _pal();
    if (!data.length) { el.textContent = ""; return; }
    var hi = -Infinity, lo = Infinity, first = data[0].close, last = data[data.length - 1].close;
    data.forEach(function (d) { if (d.high > hi) hi = d.high; if (d.low < lo) lo = d.low; });
    var chg = first ? ((last - first) / first * 100) : 0;
    var col = chg >= 0 ? "#0a9e63" : "#dd3a4a";
    el.innerHTML = '<span style="color:' + col + ';font-weight:700">' + (chg >= 0 ? "+" : "") + chg.toFixed(1) + '%</span>' +
      '<span style="color:' + p.sub + '"> · H ' + _fmt(hi) + ' · L ' + _fmt(lo) + '</span>';
  }
  function _fmt(n) { return (n == null || !isFinite(n)) ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 }); }

  function _getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  function _probeFutures(sym) {
    if (_futCache[sym] != null) return Promise.resolve(_futCache[sym]);
    return _getJSON("/api/intraday/" + encodeURIComponent(sym) + "?sessions=1")
      .then(function (rows) { _futCache[sym] = !!(rows && rows.length); return _futCache[sym]; })
      .catch(function () { _futCache[sym] = false; return false; });
  }

  function _load(tf) {
    _tf = tf;
    var box = document.getElementById("scorrChartBox"), msg = document.getElementById("scorrChartMsg");
    _paintChrome();
    if (!window.LightweightCharts) { msg.textContent = "Chart library unavailable."; return; }
    if (_chart) { try { _chart.remove(); } catch (e) {} _chart = null; _series = null; }
    box.innerHTML = ""; msg.textContent = "Loading…";
    var isIntraday = (TF[tf] === null);
    var url = isIntraday
      ? "/api/intraday/" + encodeURIComponent(_sym) + "?sessions=5"
      : "/api/candles/" + encodeURIComponent(_sym) + "?days=" + TF[tf];
    _getJSON(url).then(function (rows) {
      var data;
      if (isIntraday) {
        data = (rows || []).map(function (r) {
          return { time: Math.floor(new Date(String(r.ts).replace(" ", "T") + "+05:30").getTime() / 1000), open: +r.open, high: +r.high, low: +r.low, close: +r.close, volume: +r.volume, _day: String(r.ts).slice(0, 10) };   // cc#755: volume + session date for VWAP
        }).filter(function (d) { return isFinite(d.close) && isFinite(d.time); });
      } else {
        data = (rows || []).map(function (r) { return { time: r.date, open: +r.open, high: +r.high, low: +r.low, close: +r.close }; })
          .filter(function (d) { return isFinite(d.close); });
      }
      if (!data.length) { msg.textContent = "No " + (isIntraday ? "5-min" : tf) + " data for " + _sym + "."; _setHL([]); return; }
      var p = _pal();
      var c = LightweightCharts.createChart(box, {
        width: box.clientWidth, height: 412,
        layout: { background: { color: "transparent" }, textColor: p.mut },
        grid: { vertLines: { visible: false }, horzLines: { color: p.grid } },
        localization: { locale: "en-IN", timeFormatter: _istCross },
        timeScale: { timeVisible: isIntraday, borderVisible: false, tickMarkFormatter: _istTick },
        rightPriceScale: { borderVisible: false },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal }
      });
      var s = c.addCandlestickSeries({ upColor: "#0a9e63", downColor: "#dd3a4a", borderVisible: false, wickUpColor: "#0a9e63", wickDownColor: "#dd3a4a" });
      s.setData(data); c.timeScale().fitContent();
      _chart = c; _series = s; _lastData = data;
      _setHL(data);
      _applyOverlays();   // cc#730: draw pivot + fib price lines for the freshly loaded timeframe
      _applyVwap();       // cc#755: draw the session-VWAP line (intraday only; no-op on EOD TFs)
      _applyGvm();        // cc#779: GVM quality-trend line (secondary fixed 0-10 axis; no-op on 5m)
      _loadVerdict();     // cc#779: trend-strength badge, recomputed for THIS timeframe
      try { c.timeScale().subscribeVisibleLogicalRangeChange(_positionFx); } catch (e) {}   // cc#750: keep fib bands aligned on pan/zoom
      msg.textContent = isIntraday
        ? "5-min · last 5 sessions · IST (F&O feed)"
        : tf + " · daily · raw_prices (IST)";
      window.addEventListener("resize", _onResize);
    }).catch(function () { msg.textContent = "Chart failed to load."; });
  }

  function _onResize() {
    if (!_chart) return;
    var box = document.getElementById("scorrChartBox"); if (box) try { _chart.applyOptions({ width: box.clientWidth }); } catch (e) {}
    _positionFx();   // cc#750: re-fit the fib bands + chip strip to the new width (mobile PWA)
  }

  function open(symbol, opts) {
    if (!symbol) return;
    opts = opts || {};
    _sym = String(symbol).toUpperCase();
    _theme = opts.theme || _detectTheme();
    var ov = _buildModal();
    ov.style.display = "flex";
    document.getElementById("scorrChartTitle").textContent = _sym + " · Price";
    document.getElementById("scorrChartHL").textContent = "";
    document.addEventListener("keydown", _esc);
    _tf = "3M";
    _ensureLib(function (ok) {
      if (!ok) { document.getElementById("scorrChartMsg").textContent = "Chart library failed to load."; _paintChrome(); return; }
      _probeFutures(_sym).then(function () { _load(_tf); });
      _paintChrome();
    });
  }

  function close() {
    var ov = document.getElementById("scorrChartOv");
    if (ov) ov.style.display = "none";
    if (_chart) { try { _chart.remove(); } catch (e) {} _chart = null; _series = null; }
    _priceLines = []; _lastData = []; _fibBand = null;   // cc#730/#750
    _vwapSeries = null; _vwapLast = null;   // cc#755 (series is owned by _chart, already removed above)
    _gvmSeries = null; _verdict = null;     // cc#779 (same — owned by _chart)
    if (_full) _toggleFull(false);          // cc#779: never leave the wrapper stuck full-viewport
    var vp = document.getElementById("scorrVerdictPop"); if (vp) vp.remove();   // cc#779
    var fx = document.getElementById("scorrChartFx"); if (fx && fx.parentNode) fx.parentNode.removeChild(fx);   // cc#750: rebuilt fresh on next open
    document.removeEventListener("keydown", _esc);
    window.removeEventListener("resize", _onResize);
  }

  window.ScorrChartCard = { open: open, close: close };
})();

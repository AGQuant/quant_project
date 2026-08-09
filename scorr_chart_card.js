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
 * Data:  daily 1M/3M/6M/1Y/3Y/ALL -> GET /api/candles/{sym}?days=N  (raw_prices; all stocks)
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
 *
 * cc#806: TFs are 5m/1M/3M/6M/1Y/3Y/ALL; pivots render on 5m/1M/3M/6M only (greyed beyond — rolling
 * levels lose meaning at longer scales).
 * cc#807: session VWAP + VPOC are AUTOMATIC on 5m and absent on every other timeframe — no toggle,
 * no greyed pill. Both are flat dashed LEVEL lines (grey VWAP / muted-blue VPOC) with value chips.
 */
(function () {
  "use strict";
  if (window.ScorrChartCard) return;

  var LWC_SRC = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";
  // cc#752: TF row unified with the V8 dashboard chart (qaChart _QA_TF) so every surface — V8,
  // SmartGain, TC cards, GVM "C" — shows the SAME timeframes. 5m=intraday.
  // cc#806: 3Y added; ALL is now 5 years, not a 20-year sentinel. raw_prices retention IS ~5 years, so
  // the old 365*20 asked the API for history that does not exist and simply returned whatever was
  // stored — "ALL" now states the real bound instead of implying two decades.
  var TF = { "5m": null, "1M": 30, "3M": 90, "6M": 180, "1Y": 365, "3Y": 1095, "ALL": 1825 };
  var TF_ORDER = ["5m", "1M", "3M", "6M", "1Y", "3Y", "ALL"];
  // cc#806 FOUNDER RULE: pivots render ONLY on 5m and 1M/3M/6M. Rolling levels lose meaning at 1Y+,
  // so those pills grey out exactly like VWAP does on EOD frames. This replaces the old ALL-only
  // suppression. One predicate, used by the toggle chip, the price lines and the chip strip — so the
  // button state and what is actually drawn can never disagree.
  var PIV_TFS = { "5m": 1, "1M": 1, "3M": 1, "6M": 1 };
  function _pivOk() { return !!PIV_TFS[_tf]; }

  var _chart = null, _series = null, _sym = null, _tf = "3M", _theme = "light";
  var _gvmSeries = null;            // cc#779: GVM quality-trend line (secondary fixed 0-10 axis)
  var _verdict = null;              // cc#779: cached trend verdict for the current symbol+timeframe
  var _full = false;                // cc#779: fullscreen state
  var GVM_COL = "#7b6bd6";          // muted violet — distinct from price/pivot/fib/VWAP palettes
  // cc#800: the GVM axis bounds live here so the pin has ONE source. The scale is conceptually
  // 0-10 and must never be derived from the data — that is the bug this fixes.
  var GVM_MIN = 0, GVM_MAX = 10;
  // cc#800 polish: optional 5-day EMA of gvm_score. OFF by default per founder preference — daily
  // M-pillar noise is real, but the trend is the signal and the raw line is what he asked to see.
  // Flip to true (or expose a toggle) if the noise ever outweighs the fidelity.
  var GVM_SMOOTH = false;

  // Simple EMA over {time,value} points. Seeded with the first value rather than a zero so the
  // series does not open with a false climb from 0 up to the real level.
  function _ema(pts, n) {
    if (!pts || pts.length < 2) return pts;
    var k = 2 / (n + 1), prev = pts[0].value, out = [{ time: pts[0].time, value: prev }];
    for (var i = 1; i < pts.length; i++) {
      prev = pts[i].value * k + prev * (1 - k);
      out.push({ time: pts[i].time, value: Math.round(prev * 100) / 100 });
    }
    return out;
  }
  var _futCache = {};   // {sym: bool} — 5m availability, cached per session (no repeat probe)
  // cc#730: fib + pivot overlay state (default BOTH on so the card matches the V8 chart out of the box).
  var _fibCache = {};   // {sym: fibcheck json} — pivots fetched once per symbol
  var _priceLines = [], _lastData = [];
  // cc#807: intraday session LEVELS — VWAP + VPOC. Both are flat price lines (not curves) held in
  // their OWN array, separate from _priceLines, because _applyOverlays() wipes that one on every
  // pivot/fib change and these must survive it.
  var _ilLines = [];                          // price-line handles for the VWAP + VPOC levels
  var _vwapLast = null, _vpocLast = null;     // latest values, surfaced as chips
  var VWAP_COL = "#8a94ad";                   // grey — same family as the PP pivot line
  var VPOC_COL = "#5b7fb3";                   // muted blue — distinct from grey VWAP and the fib palette
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
    // cc#807: `vwap` is GONE from the overlay state. VWAP is no longer a user decision — it renders
    // automatically on 5m and does not exist anywhere else, so there is nothing to persist. The old
    // "scorr_chart_vwap" localStorage key is intentionally not read; it is now dead and simply ages
    // out of browsers. Do not re-add it — a preference that has only one sensible value is not a
    // preference, and a permanently-disabled pill on 6 of 7 timeframes was pure UI noise.
    var gvm = false;    // cc#779: GVM quality-trend line, opt-in, persisted separately (same reason)
    try { gvm = localStorage.getItem("scorr_chart_gvm") === "1"; } catch (e) {}
    try { var s = localStorage.getItem("scorr_chart_overlay");
      if (s === "none") return { pivot: false, fib: false, gvm: gvm };
      if (s === "pivot") return { pivot: true, fib: false, gvm: gvm };
      if (s === "fib") return { pivot: false, fib: true, gvm: gvm };
    } catch (e) {}
    return { pivot: true, fib: true, gvm: gvm };   // default: pivots + fib on, GVM off
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
        /* cc#845: CHART | PEERS tab pair at CARD level. Toggle in place — same card, no popup,
           no navigation, because a peer scan is a comparison workflow and navigating away breaks it. */
        '<div id="scorrChartTabs" style="display:flex;gap:4px;padding:8px 16px 0"></div>' +
        '<div id="scorrChartBox" style="width:100%;height:412px;padding:8px 8px 0"></div>' +
        '<div id="scorrPeerPane" style="display:none;padding:6px 12px 12px;overflow:auto;max-height:436px"></div>' +
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
  // wrapper — the timeframe pills, Pivots/Fib/GVM toggles, verdict badge and crosshair are the
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
    // cc#730: Pivots / Fib overlay toggles (mirror the V8 card). cc#806: pivots are suppressed on every
    // timeframe longer than 6M (see PIV_TFS), not just ALL — rolling levels lose meaning well before
    // full history. Fib is unaffected: its swing is derived from the loaded range, so it stays valid.
    var sep = document.createElement("span");
    sep.style.cssText = "width:1px;height:16px;background:" + p.line + ";margin:0 2px;align-self:center";
    host.appendChild(sep);
    [["pivot", "Pivots", "Pivots — PP / R1 / R2 / S1 / S2 (v8_paper_pivots)"],
     ["fib", "Fib", "Fibonacci retracement levels (loaded-range swing, same as the V8 chart)"]].forEach(function (o) {
      var b = document.createElement("button");
      b.textContent = o[1];
      var pivBlocked = (o[0] === "pivot" && !_pivOk());
      var on = _ov[o[0]] && !pivBlocked;
      b.style.cssText = "padding:4px 9px;border-radius:7px;font:700 11.5px/1 -apple-system,Segoe UI,sans-serif;cursor:pointer;border:1px solid " + p.line +
        ";background:" + (on ? p.btnOn : p.btn) + ";color:" + (on ? "#fff" : p.mut) + (pivBlocked ? ";opacity:.4;cursor:not-allowed" : "");
      b.title = pivBlocked ? "Pivots shown on 1M–6M — rolling levels lose meaning at longer scales" : o[2];
      if (!pivBlocked) b.onclick = function () { _toggleOv(o[0]); };
      host.appendChild(b);
    });
    // cc#807: the VWAP toggle that used to sit here is DELETED. Session VWAP (and now VPOC) exist only
    // intraday, so the pill was permanently disabled on 6 of the 7 timeframes — noise offering a
    // decision with one right answer. Both levels now render automatically on 5m and are absent
    // everywhere else: no pill, no greyed state, the concept simply not present. See
    // _applyIntradayLevels(). Do not reintroduce a toggle for them.
    // cc#779: GVM quality-trend toggle — third chip. The GVM series is a DAILY score, so it is
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
      if (kind === "gvm") localStorage.setItem("scorr_chart_gvm", _ov.gvm ? "1" : "0");
      else localStorage.setItem("scorr_chart_overlay", _ovStr());
    } catch (e) {}
    _paintChrome();
    if (kind === "gvm") { _applyGvm(); }
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
        if (GVM_SMOOTH) pts = _ema(pts, 5);
        // cc#800 FIX — the 0-10 axis was not actually pinned, for TWO reasons, and BOTH had to go:
        //
        // 1. autoscaleInfoProvider was applied via applyOptions AFTER addLineSeries. In
        //    LightweightCharts 4.1.3 the provider is read when the series is created for a custom
        //    priceScaleId; setting it afterwards leaves the scale on its default autoscale.
        // 2. the scale was ALSO set autoScale:false. That alone would have defeated the fix even
        //    with the provider in the right place — when autoScale is off the scale keeps a manual
        //    range and never consults the provider at all. The pin comes FROM the provider, so
        //    autoscaling must stay ON and simply always be handed the same 0-10 range.
        //
        // Together these made the axis fit the data's own min/max, so a symbol living in 6.0-7.5
        // stretched across the full height and read as violently price-correlated (the PETRONET
        // screenshot). With this, that symbol occupies a narrow band in the upper-middle instead.
        _gvmSeries = _chart.addLineSeries({
          color: GVM_COL, lineWidth: 2, priceScaleId: "gvm",
          priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
          priceFormat: { type: "price", precision: 2, minMove: 0.01 },
          autoscaleInfoProvider: function () {
            return { priceRange: { minValue: GVM_MIN, maxValue: GVM_MAX } };
          }
        });
        _gvmSeries.setData(pts);
        try {
          _chart.priceScale("gvm").applyOptions({
            // Margins kept tight so the pinned range uses nearly the whole height; autoScale STAYS
            // TRUE so the provider above is what decides the range.
            scaleMargins: { top: 0.05, bottom: 0.05 }, visible: true, borderVisible: false,
            autoScale: true
          });
        } catch (e) {}
      })
      .catch(function () { /* quality line is additive — never break the price chart */ });
  }
  // cc#807: intraday session LEVELS — VWAP and VPOC. Both are automatic on 5m and absent on every
  // other timeframe; there is no toggle (cc#755's pill is gone).
  //
  // VWAP is drawn as a FLAT horizontal level, not the cumulative curve cc#755 used to plot. Founder
  // call, and the right one: the curve traces where VWAP HAS BEEN, which is history you can already
  // read off the candles. A level answers the only live question — is price above or below the
  // session's average traded price right now. It re-anchors as new 5m bars arrive but is always flat.
  //
  // VPOC = the price bin holding the most traded volume in the CURRENT session. Bars are binned by
  // CLOSE, so this is a bar-close approximation of a volume profile, NOT a tick-level one: within a
  // 5-min bar all volume is attributed to the closing price. That is honest enough to mark the
  // session's heaviest-traded shelf and wrong enough that it should never be read as a precise
  // auction level. A true profile needs tick data we do not store.
  //
  // Both use the LAST session in the loaded window (the 5m feed returns ~5 sessions), so "session"
  // means today, not the whole visible range.
  // cc#807 VOLUME NORMALISER — required, and it exposes a real feed defect.
  // intraday_prices.volume is NOT consistently per-bar. Measured over 24-Jul..31-Jul on
  // RELIANCE/TCS/HDFCBANK/INFY/SBIN: 27/28/30-Jul are 100% non-decreasing (the column holds a
  // CUMULATIVE day counter), 24/29-Jul are 45-58% non-decreasing (genuine per-bar), and 31-Jul is
  // 82-86% (the feed switched mode mid-session, per-bar to 11:20 then cumulative from 11:25).
  // Summing a cumulative counter is nonsense — on 31-Jul RELIANCE it totals 206.7M against a true
  // day volume of 8.6M — and it drags VWAP toward the late session while pinning VPOC to whatever
  // bin the last bars closed in. So both levels are computed from a normalised series:
  //   * >=97% non-decreasing -> cumulative -> first-difference it. Verified on 30-Jul RELIANCE: the
  //     differenced series sums EXACTLY to the day total and has the textbook intraday U (501k open,
  //     ~55-70k midday, 1.09M on the closing bar).
  //   * <75% non-decreasing  -> already per-bar -> use as-is.
  //   * in between -> the feed changed representation mid-session and the two halves are not even
  //     mutually consistent (31-Jul's morning per-bar volumes sum to 3.39M, MORE than the 2.44M the
  //     cumulative counter reads at 11:25). That cannot be repaired client-side from what is stored,
  //     so return null and DRAW NOTHING. A missing level is honest; a confidently wrong one is not.
  // The real fix belongs in the feed worker, not here. Reported with cc#807.
  function _perBarVolumes(bars) {
    var v = bars.map(function (b) { return isFinite(b.volume) ? b.volume : 0; });
    if (v.length < 2) return v;
    // Ties are NEUTRAL, excluded from both sides of the ratio. An illiquid symbol can print the same
    // volume on many consecutive bars; counting those as "non-decreasing" would misread a per-bar
    // series as a cumulative counter and silently difference real volumes away. A session with no
    // informative steps at all falls through to the per-bar default, which is the safe direction.
    var up = 0, steps = 0, i;
    for (i = 1; i < v.length; i++) {
      if (v[i] === v[i - 1]) continue;
      steps++; if (v[i] > v[i - 1]) up++;
    }
    if (!steps) return v;
    var frac = up / steps;
    if (frac < 0.75) return v;            // per-bar already
    if (frac < 0.97) return null;         // mixed / mid-session mode switch — unrecoverable here
    var out = [v[0]];                     // cumulative: first bar's counter IS its own volume
    for (i = 1; i < v.length; i++) out.push(Math.max(0, v[i] - v[i - 1]));
    return out;
  }

  function _applyIntradayLevels() {
    if (!_series) return;
    _ilLines.forEach(function (pl) { try { _series.removePriceLine(pl); } catch (e) {} });
    _ilLines = [];
    _vwapLast = null; _vpocLast = null;
    if (!(_tf === "5m" && _lastData && _lastData.length)) return;

    var lastDay = _lastData[_lastData.length - 1]._day;
    var bars = _lastData.filter(function (b) { return b._day === lastDay; });
    if (!bars.length) return;

    var vols = _perBarVolumes(bars);
    if (!vols) return;   // volume series unusable for this session — no VWAP line, no VPOC, no chips

    // ── VWAP: SUM(close·volume)/SUM(volume) across the session, rendered as a flat level.
    var cumPV = 0, cumV = 0;
    bars.forEach(function (b, ix) { cumPV += b.close * vols[ix]; cumV += vols[ix]; });
    if (cumV > 0 && isFinite(cumPV / cumV)) _vwapLast = cumPV / cumV;
    else _vwapLast = bars[bars.length - 1].close;   // zero-volume session (pre-open / halted): fall back to last price

    // ── VPOC: 24 equal bins across the session's high-low range; heaviest bin's MIDPOINT.
    var hi = -Infinity, lo = Infinity;
    bars.forEach(function (b) { if (isFinite(b.high) && b.high > hi) hi = b.high; if (isFinite(b.low) && b.low < lo) lo = b.low; });
    if (isFinite(hi) && isFinite(lo)) {
      if (hi <= lo) {
        _vpocLast = lo;   // flat session (one price all day) — the whole range IS the POC
      } else {
        var NB = 24, w = (hi - lo) / NB, vol = new Array(NB), i;
        for (i = 0; i < NB; i++) vol[i] = 0;
        bars.forEach(function (b, ix) {
          var idx = Math.floor((b.close - lo) / w);
          if (idx < 0) idx = 0; if (idx >= NB) idx = NB - 1;   // the high-closing bar lands in the top bin, not past it
          vol[idx] += vols[ix];
        });
        var best = -1, bi = -1;
        for (i = 0; i < NB; i++) { if (vol[i] > best) { best = vol[i]; bi = i; } }
        if (bi >= 0 && best > 0) _vpocLast = lo + w * (bi + 0.5);
      }
    }

    // Dashed level lines, pivot styling. axisLabelVisible:false — values live in the chips, and the
    // axis is already crowded by the CMP tag (the cc#750 reason pivots lost their badges).
    if (_vwapLast != null && isFinite(_vwapLast)) {
      _ilLines.push(_series.createPriceLine({ price: +_vwapLast, color: VWAP_COL, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" }));
    }
    if (_vpocLast != null && isFinite(_vpocLast)) {
      _ilLines.push(_series.createPriceLine({ price: +_vpocLast, color: VPOC_COL, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" }));
    }
  }
  // cc#730/#750: pivot + fib overlays. Pivot LEVELS + fib LINES are native price lines (they track the
  // scale); cc#750 removes the pivot AXIS badges (they collided with the price ticks + CMP tag) in favour
  // of a compact top-left chip strip, and adds fib ZONE tint bands with right-inside labels (an HTML layer
  // positioned via priceToCoordinate, repositioned on pan/zoom/resize). Only the CMP tag stays on the axis.
  function _applyOverlays() {
    if (!_series) return;
    _priceLines.forEach(function (pl) { try { _series.removePriceLine(pl); } catch (e) {} });
    _priceLines = [];
    if (_ov.pivot && _pivOk()) {
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
        /* cc#947: the cache key was read at RESOLVE time, so a fib response arriving after a
           symbol switch was filed under the NEW symbol — the same wrong-symbol class as _load,
           one cache deep. Keyed to the symbol it was requested for. */
        (function (fsym) {
          _getJSON("/api/trade-check/fibcheck?symbol=" + encodeURIComponent(fsym) + "&lookback=6m")
            .then(function (f) {
              _fibCache[fsym] = f;
              if (_sym === fsym && _ov.pivot && _pivOk()) drawPiv(f);
            }).catch(function () {});
        })(_sym);
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
    if (_ov.pivot && _pivOk()) {
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
    // cc#807: VWAP + VPOC value chips — one row, top-left, below the pivot strip if present. 5m only,
    // always shown there (no toggle). Each label is coloured to MATCH ITS LINE so the chip row reads
    // as the chart's legend; a legend whose colour does not match the line it names is not a legend.
    // (cc#755's chip was amber while cc#807 makes the line grey — matching them is the deviation.)
    if (_tf === "5m" && ((_vwapLast != null && isFinite(_vwapLast)) || (_vpocLast != null && isFinite(_vpocLast)))) {
      var ilChips = [["VWAP", _vwapLast, VWAP_COL], ["VPOC", _vpocLast, VPOC_COL]]
        .filter(function (r) { return r[1] != null && isFinite(r[1]); })
        .map(function (r) { return '<span style="color:' + r[2] + ';font-weight:800">' + r[0] + '</span>&nbsp;<span style="color:' + pal.txt + '">' + Number(r[1]).toLocaleString("en-IN", { maximumFractionDigits: 1 }) + '</span>'; })
        .join('<span style="color:' + pal.sub + ';margin:0 5px">|</span>');
      html += '<div class="sc-vwapchip" style="position:absolute;left:0;top:' + (pivShown ? "26px" : "0") + ';font:600 10.5px/1.2 -apple-system,Segoe UI,sans-serif;background:' + chipBg + ';border:1px solid ' + pal.line + ';border-radius:7px;padding:3px 7px;white-space:nowrap;backdrop-filter:blur(2px)">' + ilChips + '</div>';
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
    return fetchWithTimeout(url, { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
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
    /* cc#947 item 1 — THE WRONG-SYMBOL BUG. _load builds its URL from _sym at CALL time and then
       resolves LATER, and this .then() was the ONE async path in this file with no stale-response
       guard: the verdict fetch (line ~239), the GVM overlay (~378) and the peer pane (~782) all
       check `_sym !== sym` before touching the DOM, and this one did not.
       close() does not abort an in-flight request, so the founder's exact sequence — open NMDC,
       close, open GLENMARK — leaves NMDC's response in the air. It lands after GLENMARK's, passes
       no guard, and CREATES THE CHART with NMDC candles under a title that already says GLENMARK.
       That is the screenshot: header GLENMARK Rs 2,296, readout NMDC · Price.
       Reproduced with this exact interleaving before the fix and confirmed gone after.
       Captured here, checked at every point the callback would write anything. */
    var wantSym = _sym, wantTf = tf;
    var stale = function () { return _sym !== wantSym || _tf !== wantTf; };
    var url = isIntraday
      ? "/api/intraday/" + encodeURIComponent(_sym) + "?sessions=5"
      : "/api/candles/" + encodeURIComponent(_sym) + "?days=" + TF[tf];
    _getJSON(url).then(function (rows) {
      if (stale()) return;                       // a response for a symbol/timeframe we left
      var data;
      if (isIntraday) {
        data = (rows || []).map(function (r) {
          return { time: Math.floor(new Date(String(r.ts).replace(" ", "T") + "+05:30").getTime() / 1000), open: +r.open, high: +r.high, low: +r.low, close: +r.close, volume: +r.volume, _day: String(r.ts).slice(0, 10) };   // cc#755: volume + session date for VWAP
        }).filter(function (d) { return isFinite(d.close) && isFinite(d.time); });
      } else {
        data = (rows || []).map(function (r) { return { time: r.date, open: +r.open, high: +r.high, low: +r.low, close: +r.close }; })
          .filter(function (d) { return isFinite(d.close); });
      }
      /* the empty-data message names the symbol it was FETCHED for, not whatever _sym happens to
         be now — the same class of mistake one line down from the guard above. */
      if (!data.length) { msg.textContent = "No " + (isIntraday ? "5-min" : tf) + " data for " + wantSym + "."; _setHL([]); return; }
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
      _applyIntradayLevels();   // cc#807: flat VWAP + VPOC session levels (5m only; no-op on EOD TFs)
      _applyGvm();        // cc#779: GVM quality-trend line (secondary fixed 0-10 axis; no-op on 5m)
      _loadVerdict();     // cc#779: trend-strength badge, recomputed for THIS timeframe
      try { c.timeScale().subscribeVisibleLogicalRangeChange(_positionFx); } catch (e) {}   // cc#750: keep fib bands aligned on pan/zoom
      msg.textContent = isIntraday
        ? "5-min · last 5 sessions · IST (F&O feed)"
        : tf + " · daily · raw_prices (IST)";
      window.addEventListener("resize", _onResize);
    }).catch(function () { if (stale()) return; msg.textContent = "Chart failed to load."; });
  }

  function _onResize() {
    if (!_chart) return;
    var box = document.getElementById("scorrChartBox"); if (box) try { _chart.applyOptions({ width: box.clientWidth }); } catch (e) {}
    _positionFx();   // cc#750: re-fit the fib bands + chip strip to the new width (mobile PWA)
  }

  // ══════════════════════════════════════════════════════════════════════════════════════════
  // cc#845 PEERS TAB — segment peer table inside the same card.
  // Read-only. Every number comes from /api/chart/peers, which LEFT-joins v8_metrics and falls
  // back to raw_prices per symbol: v8_metrics covers only the ~209 futures names, so on the
  // founder's own PETRONET example just 1 of 5 City Gas peers has a row. An inner join would have
  // rendered a peer table containing only the stock you were already looking at.
  // ══════════════════════════════════════════════════════════════════════════════════════════
  var _tab = "chart", _peers = null, _peerOpen = null;

  function _paintTabs() {
    var host = document.getElementById("scorrChartTabs");
    if (!host) return;
    var p = _pal();
    host.innerHTML = ["chart", "peers"].map(function (k) {
      var on = (_tab === k);
      return '<button data-tab="' + k + '" style="border:1px solid ' + (on ? p.btnOn : p.line) +
        ';background:' + (on ? p.btnOn : p.btn) + ';color:' + (on ? "#fff" : p.mut) +
        ';border-radius:8px;padding:5px 14px;min-height:32px;font-size:11.5px;font-weight:700;' +
        'cursor:pointer;font-family:inherit">' + (k === "chart" ? "Chart" : "Peers") + '</button>';
    }).join("");
    Array.prototype.forEach.call(host.querySelectorAll("[data-tab]"), function (b) {
      b.onclick = function () { _setTab(b.getAttribute("data-tab")); };
    });
  }

  function _setTab(k) {
    _tab = k;
    var box = document.getElementById("scorrChartBox");
    var pane = document.getElementById("scorrPeerPane");
    var tfs = document.getElementById("scorrChartTfs");
    var msg = document.getElementById("scorrChartMsg");
    if (box) box.style.display = (k === "chart") ? "" : "none";
    if (pane) pane.style.display = (k === "peers") ? "" : "none";
    // Timeframe pills belong to the chart; hiding them on Peers stops them implying they filter
    // the table (they do not — the peer columns are fixed Day/Week/Month).
    if (tfs) tfs.style.display = (k === "chart") ? "" : "none";
    if (msg) msg.style.display = (k === "chart") ? "" : "none";
    _paintTabs();
    if (k === "peers") _loadPeers();
    else if (_chart) { try { _chart.applyOptions({ width: box.clientWidth }); } catch (e) {} }
  }

  function _pc(v) {
    if (v == null) return '<span style="opacity:.45">—</span>';
    var col = v > 0 ? "#2FD48B" : (v < 0 ? "#FF5C6C" : "#8C99BD");
    return '<span style="color:' + col + ';font-weight:700">' + (v >= 0 ? "+" : "") + v.toFixed(2) + '%</span>';
  }

  function _bandChip(b) {
    if (!b) return "";
    var c = b === "Strong Buy" ? "#2FD48B" : b === "Buy" ? "#7FD4A8"
          : b === "Watch" ? "#F5B94A" : "#FF5C6C";
    return '<span style="font-size:9.5px;font-weight:800;letter-spacing:.03em;padding:2px 6px;' +
      'border-radius:6px;color:' + c + ';border:1px solid ' + c + '55;white-space:nowrap">' + b + '</span>';
  }

  function _loadPeers() {
    var pane = document.getElementById("scorrPeerPane");
    if (!pane) return;
    var p = _pal(), sym = _sym;
    pane.innerHTML = '<div style="padding:14px 4px;font-size:11.5px;color:' + p.mut + '">Loading peers…</div>';
    _getJSON("/api/chart/peers/" + encodeURIComponent(sym))
      .then(function (d) {
        if (_sym !== sym) return;                       // user switched symbol mid-flight
        _peers = d; _peerOpen = null;
        _renderPeers();
      })
      .catch(function (e) {
        pane.innerHTML = '<div style="padding:14px 4px;font-size:11.5px;color:' + p.mut +
          '">Peers unavailable — ' + String(e && e.message || e) + '</div>';
      });
  }

  function _renderPeers() {
    var pane = document.getElementById("scorrPeerPane");
    var d = _peers; if (!pane || !d) return;
    var p = _pal();
    if (d.error || !(d.peers || []).length) {
      pane.innerHTML = '<div style="padding:14px 4px;font-size:11.5px;color:' + p.mut + '">' +
        (d.error ? ("Peers unavailable — " + d.error) : (d.reason || "No peers in this segment.")) +
        '</div>';
      return;
    }
    var head = '<div style="font-size:11px;color:' + p.mut + ';padding:2px 4px 8px">' +
      '<b style="color:' + p.txt + '">' + d.segment + '</b> · ' + d.count + ' peers · ' + d.band_rule + '</div>';
    var th = 'padding:6px 8px;font-size:9.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:' +
      p.sub + ';border-bottom:1px solid ' + p.line + ';text-align:right;white-space:nowrap';
    var rows = d.peers.map(function (r, i) {
      var td = 'padding:7px 8px;font-size:12px;border-bottom:1px solid ' + p.line + ';text-align:right;white-space:nowrap';
      // Own row pinned visually rather than reordered — the table stays in GVM order so the
      // reader can see WHERE the stock sits among its peers, which is the point of the view.
      var selfBg = r.is_self ? ('background:' + (p.btnOn + "1f") + ';') : '';
      return '<tr data-sym="' + r.symbol + '" style="' + selfBg + '">' +
        '<td style="' + td + ';text-align:left">' +
          '<div style="font-weight:' + (r.is_self ? "800" : "700") + ';color:' + p.txt + '">' + r.symbol +
            (r.is_self ? ' <span style="font-size:9px;opacity:.7">THIS</span>' : '') + '</div>' +
          '<div style="font-size:10px;color:' + p.sub + '">' + (r.company_name || "") + '</div>' +
        '</td>' +
        '<td style="' + td + '">' + _pc(r.day_pct) + '</td>' +
        '<td style="' + td + '">' + _pc(r.week_pct) + '</td>' +
        '<td style="' + td + '">' + _pc(r.month_pct) + '</td>' +
        '<td style="' + td + ';color:' + p.txt + ';font-weight:700">' + (r.gvm == null ? "—" : r.gvm.toFixed(2)) + '</td>' +
        '<td style="' + td + '">' + _bandChip(r.band) + '</td>' +
        '<td style="' + td + '"><button data-card="' + r.symbol + '" style="border:1px solid ' + p.line +
          ';background:' + p.btn + ';color:' + p.mut + ';border-radius:7px;padding:4px 10px;min-height:30px;' +
          'font-size:10.5px;font-weight:700;cursor:pointer;font-family:inherit">CARD</button></td>' +
        '</tr>' +
        '<tr data-drawer="' + r.symbol + '" style="display:none"><td colspan="7" style="padding:0"></td></tr>';
    }).join("");
    pane.innerHTML = head +
      '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;min-width:520px">' +
      '<thead><tr>' +
        '<th style="' + th + ';text-align:left">Symbol</th><th style="' + th + '">Day</th>' +
        '<th style="' + th + '">Week</th><th style="' + th + '">Month</th>' +
        '<th style="' + th + '">GVM</th><th style="' + th + '">Band</th><th style="' + th + '"></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div style="font-size:9.5px;color:' + p.sub + ';padding:8px 4px 0;line-height:1.5">' + d.basis + '</div>';
    Array.prototype.forEach.call(pane.querySelectorAll("[data-card]"), function (b) {
      b.onclick = function (e) { e.stopPropagation(); _toggleDrawer(b.getAttribute("data-card")); };
    });
  }

  // cc#845 founder-delegated decision: INLINE DRAWER, not navigation. A peer scan is an A/B
  // comparison workflow; navigating away breaks it. The full-screen path is preserved as a link
  // inside the drawer so the deep route is never lost.
  function _toggleDrawer(sym) {
    var pane = document.getElementById("scorrPeerPane"); if (!pane) return;
    var row = pane.querySelector('[data-drawer="' + sym + '"]'); if (!row) return;
    var cell = row.firstChild;
    if (_peerOpen === sym) { row.style.display = "none"; _peerOpen = null; return; }
    if (_peerOpen) {
      var prev = pane.querySelector('[data-drawer="' + _peerOpen + '"]');
      if (prev) prev.style.display = "none";
    }
    _peerOpen = sym;
    row.style.display = "";
    var p = _pal();
    cell.innerHTML = '<div style="padding:10px 12px;font-size:11.5px;color:' + p.mut + '">Loading ' + sym + '…</div>';
    _getJSON("/api/chart/tradecard/" + encodeURIComponent(sym))
      .then(function (t) { if (_peerOpen === sym) cell.innerHTML = _drawerHtml(sym, t); })
      .catch(function () {
        if (_peerOpen !== sym) return;
        cell.innerHTML = '<div style="padding:10px 12px;font-size:11.5px;color:' + p.mut + '">' +
          'Trade card unavailable for ' + sym + '. ' + _fullLink(sym) + '</div>';
      });
  }

  function _fullLink(sym) {
    return '<a href="/check?symbol=' + encodeURIComponent(sym) + '" target="_blank" rel="noopener" ' +
      'style="color:#4d7cfe;font-weight:700;text-decoration:none">Open full Trade Check →</a>';
  }

  function _drawerHtml(sym, t) {
    var p = _pal();
    t = t || {};
    // Every value is server-derived (/api/chart/tradecard) so the arithmetic lives in ONE place.
    // A cash peer legitimately has no lot size, so LOT/REWARD/RISK render as em dashes rather than
    // a fabricated single-share position; R:R and the levels stay valid because they are
    // lot-independent.
    var g = function (k) { var v = t[k]; return (v == null || v === "") ? "—" : v; };
    var pair = function (k, label) {
      return '<div style="display:flex;justify-content:space-between;gap:10px;padding:3px 0">' +
        '<span style="color:' + p.sub + '">' + label + '</span>' +
        '<span style="color:' + p.txt + ';font-weight:700;font-family:ui-monospace,monospace">' + g(k) + '</span></div>';
    };
    if (t.reason || (t.error && t.entry == null)) {
      return '<div style="margin:2px 8px 10px;padding:10px 12px;border:1px solid ' + p.line +
        ';border-radius:10px;background:' + p.btn + ';font-size:11.5px;color:' + p.mut + '">' +
        '<b style="color:' + p.txt + '">' + sym + '</b> — ' + (t.reason || t.error) +
        '<div style="margin-top:8px;font-size:10px">' + _fullLink(sym) + '</div></div>';
    }
    var vch = t.verdict ? '<span style="margin-left:8px;font-size:9.5px;font-weight:800;padding:2px 6px;' +
      'border-radius:6px;border:1px solid ' + p.line + ';color:' + p.mut + '">' + t.verdict + '</span>' : '';
    return '<div style="margin:2px 8px 10px;padding:10px 12px;border:1px solid ' + p.line +
      ';border-radius:10px;background:' + p.btn + '">' +
      '<div style="font-weight:800;color:' + p.txt + ';margin-bottom:6px;font-size:12px">' + sym +
        ' · Trade card' + vch + '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:0 18px;font-size:11.5px">' +
        pair("entry", "ENTRY") + pair("lot_size", "LOT SIZE") +
        pair("target", "TARGET") + pair("reward", "REWARD") +
        pair("sl", "SL") + pair("risk", "RISK") +
        pair("rr", "R:R") + pair("cmp", "PRICE") +
      '</div>' +
      '<div style="margin-top:7px;font-size:9.5px;color:' + p.sub + ';line-height:1.5">' +
        (t.basis || "") + (t.as_of ? (' · pivots ' + t.as_of) : '') + '</div>' +
      '<div style="margin-top:6px;font-size:10px">' + _fullLink(sym) + '</div>' +
    '</div>';
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
    _tab = "chart"; _peers = null; _peerOpen = null;   // cc#845: every open starts on Chart
    _setTab("chart");
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
    _ilLines = []; _vwapLast = null; _vpocLast = null;   // cc#807 (price lines are owned by _series, removed with it)
    _gvmSeries = null; _verdict = null;     // cc#779 (same — owned by _chart)
    _tab = "chart"; _peers = null; _peerOpen = null;   // cc#845
    if (_full) _toggleFull(false);          // cc#779: never leave the wrapper stuck full-viewport
    var vp = document.getElementById("scorrVerdictPop"); if (vp) vp.remove();   // cc#779
    var fx = document.getElementById("scorrChartFx"); if (fx && fx.parentNode) fx.parentNode.removeChild(fx);   // cc#750: rebuilt fresh on next open
    document.removeEventListener("keydown", _esc);
    window.removeEventListener("resize", _onResize);
  }

  window.ScorrChartCard = { open: open, close: close };
})();

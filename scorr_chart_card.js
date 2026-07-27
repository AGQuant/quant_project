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
 * Data:  daily 3M/1Y/3Y/5Y  -> GET /api/candles/{sym}?days=N   (raw_prices; all stocks)
 *        5-min intraday      -> GET /api/intraday/{sym}?sessions=N  (fyers feed; FUTURES universe only)
 * 5m gating: probed once per symbol via a 1-session intraday call — rows => futures (5m enabled),
 *            empty => non-futures (5m greyed with "5-min available for F&O stocks" tooltip); daily default.
 * Times are IST (Asia/Kolkata). Crosshair/tooltip is LightweightCharts-native.
 * (Pivots/Fib overlays from the V8 card are intentionally deferred here — core candle parity first.)
 */
(function () {
  "use strict";
  if (window.ScorrChartCard) return;

  var LWC_SRC = "https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js";
  var TF = { "5m": null, "3M": 90, "1Y": 365, "3Y": 365 * 3, "5Y": 365 * 5 };
  var TF_ORDER = ["5m", "3M", "1Y", "3Y", "5Y"];

  var _chart = null, _series = null, _sym = null, _tf = "3M", _theme = "light";
  var _futCache = {};   // {sym: bool} — 5m availability, cached per session (no repeat probe)

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

  function _esc(k) { if (k.key === "Escape") close(); }

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
          '<span style="margin-left:auto;display:flex;gap:5px;align-items:center;flex-wrap:wrap;justify-content:flex-end" id="scorrChartTfs"></span>' +
          '<button id="scorrChartClose" style="border:none;background:none;font-size:20px;line-height:1;cursor:pointer;margin-left:4px">&times;</button>' +
        '</div>' +
        '<div id="scorrChartBox" style="width:100%;height:412px;padding:8px 8px 0"></div>' +
        '<div id="scorrChartMsg" style="padding:6px 16px 12px;font-size:11px"></div>' +
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener("click", function (e) { if (e.target === ov) close(); });
    ov.querySelector("#scorrChartClose").addEventListener("click", close);
    return ov;
  }

  function _paintChrome() {
    var p = _pal();
    var wrap = document.getElementById("scorrChartBoxWrap");
    wrap.style.background = p.panel; wrap.style.border = "1px solid " + p.line;
    document.getElementById("scorrChartHead").style.borderBottom = "1px solid " + p.line;
    document.getElementById("scorrChartTitle").style.color = p.txt;
    document.getElementById("scorrChartClose").style.color = p.mut;
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
          return { time: Math.floor(new Date(String(r.ts).replace(" ", "T") + "+05:30").getTime() / 1000), open: +r.open, high: +r.high, low: +r.low, close: +r.close };
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
      _chart = c; _series = s;
      _setHL(data);
      msg.textContent = isIntraday
        ? "5-min · last 5 sessions · IST (F&O feed)"
        : tf + " · daily · raw_prices (IST)";
      window.addEventListener("resize", _onResize);
    }).catch(function () { msg.textContent = "Chart failed to load."; });
  }

  function _onResize() {
    if (!_chart) return;
    var box = document.getElementById("scorrChartBox"); if (box) try { _chart.applyOptions({ width: box.clientWidth }); } catch (e) {}
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
    document.removeEventListener("keydown", _esc);
    window.removeEventListener("resize", _onResize);
  }

  window.ScorrChartCard = { open: open, close: close };
})();

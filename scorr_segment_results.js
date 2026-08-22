/* scorr_segment_results.js — cc#1191 scopes 3+4: the SEGMENT RESULTS popout.
 *
 * WHAT IT IS. The R card's header carries a segment chip; clicking it opens every verified
 * reporter in that segment as one table — result date, Sales/PAT YoY, vs-est, FY27 est, GVM and
 * the shared C·A·R·D strip per row — with unreported members greyed underneath.
 *
 * IT BUILDS ITS OWN SHELL, and that is deliberate rather than lazy. The R card's overlay is a
 * private closure inside pwa_endpoints' ScorrRCard IIFE: `ov` and `box` are not exported, and the
 * only public surface is ScorrRCard.open/openV/renderInline/renderCollapsible/close. Reaching into
 * it would mean either exporting internals or reusing the SAME box the R card is currently
 * rendering into — which would blow away the card the reader clicked from. So this owns a second
 * overlay that reuses the R card's CLASS NAMES (.rcard-ov / .rcard / .rcard-hd / .rcard-x), so it
 * inherits every visual token from the one stylesheet and cannot drift from the card shell.
 *
 * THE C·A·R·D STRIP IS NEVER RE-IMPLEMENTED. scorr_card_strip.js's own header says it: the strip
 * comes ONLY from window.ScorrCardStripHtml(symbol, activeLetter). If that global is missing the
 * cell renders empty rather than a hand-drawn imitation that would drift the first time the strip
 * changes.
 *
 * SORTING KEEPS NULLS LAST IN BOTH DIRECTIONS. A row with no PAT YoY is not the worst performer in
 * the segment — it is a row we could not read — and letting it sort as zero would rank a company
 * we know nothing about against companies we do. Same rule the tape sort uses (cc#939).
 *
 * WEB ONLY. No app token, font or layout is imported here.
 */
(function () {
  "use strict";
  if (window.ScorrSegmentResults) { return; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pct(v) {
    if (v == null) { return "—"; }
    var n = Number(v);
    if (!isFinite(n)) { return "—"; }
    return (n > 0 ? "+" : "") + n.toFixed(1) + "%";
  }
  function cls(v) {
    if (v == null) { return "sg-na"; }
    return Number(v) > 0 ? "sg-pos" : (Number(v) < 0 ? "sg-neg" : "sg-flat");
  }

  var CSS = [
    ".sg-wrap{margin-top:10px}",
    ".sg-cnt{font-size:11px;color:var(--dim,#8892a6);margin:2px 0 10px}",
    ".sg-tbl{width:100%;border-collapse:collapse;font-size:11.5px}",
    ".sg-tbl th{text-align:right;font:700 9.5px/1.3 Sora,sans-serif;text-transform:uppercase;",
    "letter-spacing:.07em;color:var(--mut,#667085);padding:6px 7px;border-bottom:1px solid var(--line,rgba(148,166,210,.2));",
    "white-space:nowrap;cursor:pointer;user-select:none}",
    ".sg-tbl th:first-child,.sg-tbl td:first-child{text-align:left}",
    ".sg-tbl th.sg-nosort{cursor:default}",
    ".sg-tbl th .sg-ar{opacity:.55;font-size:8.5px;margin-left:3px}",
    ".sg-tbl td{text-align:right;padding:7px;border-bottom:1px solid var(--line,rgba(148,166,210,.12));white-space:nowrap}",
    ".sg-tbl tr:last-child td{border-bottom:none}",
    ".sg-sym{font-weight:700}",
    ".sg-pos{color:var(--grn,#0f9d58)}.sg-neg{color:var(--red,#d93025)}",
    ".sg-flat{color:var(--mut,#667085)}.sg-na{color:var(--dim,#8892a6)}",
    ".sg-tag{font:700 9px/1 Sora,sans-serif;letter-spacing:.05em;padding:2px 5px;border-radius:4px;border:1px solid}",
    ".sg-BEAT{color:var(--grn,#0f9d58);border-color:rgba(47,212,139,.45);background:rgba(47,212,139,.12)}",
    ".sg-MISS{color:var(--red,#d93025);border-color:rgba(217,48,37,.4);background:rgba(217,48,37,.10)}",
    ".sg-IN-LINE{color:var(--mut,#667085);border-color:rgba(148,166,210,.35);background:rgba(148,166,210,.12)}",
    /* An unreported member is dimmed, not hidden: the reader is being told the segment is only
       partly in, which is a fact about the quarter and not noise. */
    ".sg-un td{opacity:.55}",
    ".sg-sub{font:700 9.5px/1.3 Sora,sans-serif;text-transform:uppercase;letter-spacing:.09em;",
    "color:var(--dim,#8892a6);padding:12px 7px 5px}",
    ".sg-err{font-size:12px;color:var(--dim,#8892a6);padding:14px 2px}"
  ].join("");

  var ov = null, box = null, DATA = null, SORT = { col: null, dir: -1 };

  function ensureShell() {
    if (ov) { return; }
    var st = document.createElement("style");
    st.textContent = CSS;
    document.head.appendChild(st);
    ov = document.createElement("div");
    ov.className = "rcard-ov";                    /* the R card's own shell classes */
    ov.innerHTML = '<div class="rcard" role="dialog" aria-modal="true"></div>';
    document.body.appendChild(ov);
    box = ov.querySelector(".rcard");
    ov.addEventListener("click", function (e) { if (e.target === ov) { close(); } });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && ov.classList.contains("on")) { close(); }
    });
    /* Delegated, so it survives every re-render the sort does. */
    box.addEventListener("click", function (e) {
      var x = e.target.closest ? e.target.closest(".rcard-x") : null;
      if (x) { close(); return; }
      var th = e.target.closest ? e.target.closest("th[data-col]") : null;
      if (th && DATA) {
        var c = th.getAttribute("data-col");
        SORT = (SORT.col === c) ? { col: c, dir: -SORT.dir } : { col: c, dir: -1 };
        render();
      }
    });
  }
  function close() { if (ov) { ov.classList.remove("on"); } }

  function sortedRows(rows) {
    var rep = rows.filter(function (r) { return r.reported; });
    var un = rows.filter(function (r) { return !r.reported; });
    if (SORT.col) {
      var c = SORT.col, d = SORT.dir;
      rep = rep.slice().sort(function (a, b) {
        var av = a[c], bv = b[c];
        if (c === "vs_est") { av = a.vs_est && a.vs_est.dev_pct; bv = b.vs_est && b.vs_est.dev_pct; }
        /* Nulls last in BOTH directions — a row we could not read is not a worst performer. */
        if (av == null && bv == null) { return 0; }
        if (av == null) { return 1; }
        if (bv == null) { return -1; }
        if (c === "result_date") { return (av < bv ? 1 : av > bv ? -1 : 0) * -d * -1; }
        return (Number(av) - Number(bv)) * d;
      });
    }
    return rep.concat(un);
  }

  function rowHtml(r) {
    var strip = window.ScorrCardStripHtml ? window.ScorrCardStripHtml(r.symbol, "R") : "";
    if (!r.reported) {
      /* expected_date is genuinely null for members with no future calendar row — measured on
         FMCG - Large, where both unreported names have none. "Date TBC" says that; a blank cell
         would read as a rendering fault and an invented date would be worse than either. */
      return '<tr class="sg-un"><td class="sg-sym">' + esc(r.symbol) + "</td>"
        + '<td colspan="5">Not reported · '
        + (r.expected_date ? "expected " + esc(r.expected_date) : "date TBC") + "</td>"
        + "<td>" + (r.gvm == null ? "—" : Number(r.gvm).toFixed(2)) + "</td>"
        + "<td>" + strip + "</td></tr>";
    }
    var v = r.vs_est;
    return "<tr><td class=\"sg-sym\">" + esc(r.symbol) + "</td>"
      + "<td>" + esc(r.result_date || "—") + "</td>"
      + '<td class="' + cls(r.sales_yoy) + '">' + pct(r.sales_yoy) + "</td>"
      + '<td class="' + cls(r.pat_yoy) + '">' + pct(r.pat_yoy) + "</td>"
      + "<td>" + (v ? '<span class="sg-tag sg-' + esc(v.tag) + '">' + esc(v.tag) + " "
                      + pct(v.dev_pct) + "</span>" : "—") + "</td>"
      + "<td>" + (r.fy27_est == null ? "—" : Number(r.fy27_est).toFixed(0) + "%") + "</td>"
      + "<td>" + (r.gvm == null ? "—" : Number(r.gvm).toFixed(2)) + "</td>"
      + "<td>" + strip + "</td></tr>";
  }

  function render() {
    var d = DATA;
    var arrow = function (c) {
      return SORT.col === c ? '<span class="sg-ar">' + (SORT.dir < 0 ? "▼" : "▲") + "</span>" : "";
    };
    var head = '<div class="rcard-hd"><div><div class="rcard-sym">'
      + esc(d.segment) + " · " + esc(d.quarter || "") + " RESULTS</div></div>"
      + '<button class="rcard-x" aria-label="Close">&times;</button></div>'
      + '<div class="sg-cnt">' + d.n_reported + " of " + d.n_total + " members reported this quarter</div>";
    var rows = sortedRows(d.rows || []);
    var firstUn = rows.findIndex(function (r) { return !r.reported; });
    var body = rows.map(function (r, i) {
      var pre = (i === firstUn && firstUn > -1)
        ? '<tr><td class="sg-sub" colspan="8">Not yet reported</td></tr>' : "";
      return pre + rowHtml(r);
    }).join("");
    box.innerHTML = head + '<div class="sg-wrap"><table class="sg-tbl"><thead><tr>'
      + '<th class="sg-nosort">Symbol</th>'
      + '<th data-col="result_date">Result date' + arrow("result_date") + "</th>"
      + '<th data-col="sales_yoy">Sales YoY' + arrow("sales_yoy") + "</th>"
      + '<th data-col="pat_yoy">PAT YoY' + arrow("pat_yoy") + "</th>"
      /* The tooltip names the SOURCE, per estimate_sources_locked — the reader must never take a
         Screener run-rate for a broker consensus. */
      + '<th data-col="vs_est" title="vs Screener projected run-rate">vs Est' + arrow("vs_est") + "</th>"
      + '<th data-col="fy27_est" title="FY27 consensus growth (Trendlyne)">FY27 Est' + arrow("fy27_est") + "</th>"
      + '<th data-col="gvm">GVM' + arrow("gvm") + "</th>"
      + '<th class="sg-nosort">C·A·R·D</th>'
      + "</tr></thead><tbody>" + body + "</tbody></table></div>";
  }

  function open(segment) {
    if (!segment) { return; }
    ensureShell();
    SORT = { col: null, dir: -1 };
    box.innerHTML = '<div class="rcard-hd"><div class="rcard-sym">' + esc(segment)
      + '</div><button class="rcard-x" aria-label="Close">&times;</button></div>'
      + '<div class="sg-err">Loading segment results…</div>';
    ov.classList.add("on");
    fetch("/api/results/segment/" + encodeURIComponent(segment))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.error || !d.rows) {
          box.querySelector(".sg-err").textContent =
            (d && d.error) ? d.error : "Segment results unavailable.";
          return;
        }
        DATA = d;
        render();
      })
      .catch(function () {
        var e = box.querySelector(".sg-err");
        if (e) { e.textContent = "Segment results unavailable."; }
      });
  }

  /* Delegated on the document so it works for EVERY R card on every page — the modal, the inline
     card and the collapsible one — without each render having to re-bind a handler. */
  document.addEventListener("click", function (e) {
    var chip = e.target.closest ? e.target.closest("[data-segchip]") : null;
    if (!chip) { return; }
    e.preventDefault();
    e.stopPropagation();
    open(chip.getAttribute("data-segchip"));
  });

  window.ScorrSegmentResults = { open: open, close: close };
})();

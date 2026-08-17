"""
gvm_twopager.py — cc#1085 · APP_QA_R6 · the GVM 2-Pager print route
====================================================================
GET /gvm/2pager/{symbol} — a standalone, server-rendered, two-A4-page quant note built for
print-to-PDF. Not React, not inside the cio2 shell: the whole point of the sheet is that it
lands on exactly two pages, and that is a property of a plain document with a tuned @page rule,
not of an app shell that happens to be printable.

ITS OWN ROUTER, wired with one include_router() line in main.py, which stays wiring only
(rule 4). The cc#1065 mistake — parking a page route on whichever router was "small and proven
mounted" — is the thing this repo has now corrected twice; not repeating it a third time.

READ-ONLY. This module writes NOTHING. It reads through the EXISTING builder
(gvm_company_report.build_company_report) rather than re-implementing any scoring, so the sheet
and the live /cio2 card can never disagree about a number: there is one computation, consumed
twice. §D of the report is explicit that gvm_scores, screener_raw and input_raw are untouched.

ROUTE, NOT NAV. NAV-COMPLETE (session_log 2987) does not apply — this is a print destination
reached from the 2 Pager button, not a screen anyone navigates to. Stated here so a later audit
does not flag it as an unfinished page.

MARKET CAP TRAP (report §E): page 1 binds mcap from screener_raw.market_cap, which is what the
live GVM page already uses and what reconciles to pe x profit_after_tax. NEVER
gvm_scores.market_cap — that column is stale across the board (BHARATSE reads 1,156.78 Cr there
against 1,557.75 Cr in screener_raw at identical prices). Nothing user-facing is wrong today
because the live page already reads the right one; this note exists so P3 does not bind the
wrong column and quietly introduce the bug.
"""

import logging
import os
from datetime import datetime, timedelta
from string import Template as _Tmpl

import psycopg
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

log = logging.getLogger("scorr.gvm.twopager")

router = APIRouter(tags=["gvm"])


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _ist_today():
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def doc_title(symbol: str, on=None) -> str:
    """SYMBOL_Quant_Note_DDMonYYYY — the browser offers the <title> as the PDF filename, so the
    title IS the filename spec. Kept as its own function because P8 renders four symbols and the
    filename is part of what gets checked."""
    d = on or _ist_today()
    return "%s_Quant_Note_%s" % (symbol.upper(), d.strftime("%d%b%Y"))


def symbol_exists(cur, symbol: str) -> bool:
    """Is this symbol in the LATEST scored set? The report is explicit that unknown means absent
    from the latest gvm_scores.score_date — a symbol scored last month but dropped from the
    universe should 404 rather than render a sheet from stale rows."""
    cur.execute(
        """SELECT 1 FROM gvm_scores
           WHERE symbol = %s AND score_date = (SELECT MAX(score_date) FROM gvm_scores)
           LIMIT 1""",
        (symbol,),
    )
    return cur.fetchone() is not None


# ══════════════════════════════════════════════════════════════════════════════════════════════
# R6-P2 · THE TEMPLATE, PORTED VERBATIM FROM design_refs/scorr_gvm_2pager_R1.html @ 81784e3
# ══════════════════════════════════════════════════════════════════════════════════════════════
# THE CSS BELOW IS BYTE-IDENTICAL TO THE REF'S <style> BLOCK. It was EXTRACTED programmatically,
# not retyped, and test_twopager_css_parity.py re-extracts it from the ref and asserts equality —
# so the parity P2 asks for is checked by a test rather than promised in a comment.
#
# WHY THAT MATTERS MORE THAN IT LOOKS. The report says the ref is tuned, not decorative: every
# font size, padding value and column width was adjusted until the content landed on exactly two
# A4 pages with no orphan third. A "harmless" tidy-up of this CSS — rounding a padding, swapping
# a font stack, letting a formatter reflow it — is a silent third page. So it is transplanted
# whole and left alone, and PAGE_BODY keeps the ref's markup with {placeholders} where P3 and P4
# bind real values. The static prose in it is template copy and stays.
REF_CSS = r"""
@page { size: A4; margin: 12mm 12mm 10mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif; color:#12161C; margin:0; font-size:8.3pt; line-height:1.28; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }

.masthead { display:flex; justify-content:space-between; align-items:baseline; border-bottom:2.5px solid #12161C; padding-bottom:5px; }
.mast-l { font-size:7.4pt; letter-spacing:.16em; font-weight:700; color:#12161C; }
.mast-r { font-size:7.4pt; letter-spacing:.08em; color:#6B7683; }

h1 { font-size:17pt; margin:9px 0 2px; letter-spacing:-.4px; }
.sub { font-size:8pt; color:#6B7683; letter-spacing:.06em; text-transform:uppercase; }

.hero { display:flex; gap:0; margin:9px 0 3px; border:1px solid #DDE2E8; }
.hero .cell { flex:1; padding:5px 9px; border-right:1px solid #DDE2E8; }
.hero .cell:last-child { border-right:none; }
.hero .k { font-size:6.8pt; letter-spacing:.13em; color:#6B7683; text-transform:uppercase; }
.hero .v { font-size:13.5pt; font-weight:700; margin-top:2px; letter-spacing:-.5px; }
.hero .v small { font-size:8pt; font-weight:600; color:#6B7683; letter-spacing:0; }
.rate { background:#12161C; color:#fff; }
.rate .k { color:#9AA5B1; }
.tag { display:inline-block; font-size:7pt; letter-spacing:.1em; padding:1.5px 6px; border-radius:2px; background:#1F7A4D; color:#fff; font-weight:700; vertical-align:middle; }

.pillars { display:flex; gap:7px; margin:6px 0 2px; }
.pil { flex:1; border:1px solid #DDE2E8; padding:5px 8px; }
.pil .n { font-size:6.8pt; letter-spacing:.13em; color:#6B7683; text-transform:uppercase; }
.pil .s { font-size:11.5pt; font-weight:700; margin-top:2px; }
.pil .l { font-size:6.8pt; color:#6B7683; }
.bar { height:3px; background:#E7EBF0; margin-top:5px; }
.bar i { display:block; height:3px; background:#12161C; }

.punch { font-size:7.5pt; font-style:italic; color:#39424E; border-left:2.5px solid #12161C; padding:4px 0 4px 9px; margin:7px 0 2px; }

h2 { font-size:7.4pt; letter-spacing:.16em; text-transform:uppercase; margin:8px 0 3px; padding-bottom:3px; border-bottom:1px solid #12161C; }
h3 { font-size:8pt; margin:8px 0 3px; letter-spacing:.02em; }

table { width:100%; border-collapse:collapse; font-size:7.1pt; }
th { text-align:right; font-size:6.4pt; letter-spacing:.1em; text-transform:uppercase; color:#6B7683; padding:3px 5px; border-bottom:1px solid #C9D0D8; font-weight:600;}
th.l, td.l { text-align:left; }
td { padding:1.35px 5px; border-bottom:1px solid #EEF1F5; text-align:right; }
tr.grp td { background:#F4F6F9; font-size:6.4pt; letter-spacing:.11em; text-transform:uppercase; color:#39424E; font-weight:700; padding:3px 5px; }
tr.me td { background:#FFF6DA; font-weight:700; }
.g { color:#1F7A4D; font-weight:700; } .r { color:#C22B3E; font-weight:700; } .a { color:#B07A00; font-weight:700; }
.muted { color:#6B7683; }
.note { font-size:7pt; color:#6B7683; margin-top:5px; line-height:1.4; }
p { margin:4px 0 7px; }
ul { margin:3px 0 7px; padding-left:14px; } li { margin:2px 0; }
.two { display:flex; gap:14px; } .two > div { flex:1; }
.foot { font-size:6.5pt; margin-top:5px; color:#8B95A1; border-top:1px solid #DDE2E8; padding-top:3px; }
"""

PAGE1_TMPL = _Tmpl(r"""
<!-- ================= PAGE 1 ================= -->
<div class="page">
<div class="masthead">
  <div class="mast-l">QUANT RESEARCH NOTE &nbsp;&middot;&nbsp; PAGE 1 OF 2 &nbsp;&middot;&nbsp; QUANT ANALYTICS</div>
  <div class="mast-r">${printed_on} &nbsp;&middot;&nbsp; DATA AS OF ${score_date}</div>
</div>

<h1>${company_name}</h1>
<div class="sub">${symbol} &nbsp;&middot;&nbsp; NSE / BSE &nbsp;&middot;&nbsp; ${segment} &nbsp;&middot;&nbsp; ${cap_category} &nbsp;&middot;&nbsp; Mcap rank ${mcap_rank}</div>

<div class="hero">
  <div class="cell rate">
    <div class="k">Overall Rating</div>
    <div class="v">${gvm} <span class="tag">${verdict}</span></div>
  </div>
  <div class="cell">
    <div class="k">${price_label}</div>
    <div class="v">${price}</div>
  </div>
  <div class="cell">
    <div class="k">Market Cap</div>
    <div class="v">${market_cap}</div>
  </div>
  <div class="cell">
    <div class="k">P/E &middot; TTM</div>
    <div class="v">${pe}</div>
  </div>
  <div class="cell">
    <div class="k">1-Year Return</div>
    <div class="v">${ret_1y}</div>
  </div>
</div>

<div class="pillars">
${pillars}
</div>

<div class="punch">${punchline}</div>

<h2>Parameter detail &mdash; company vs segment peers</h2>
<table>
<tr><th class="l" style="width:38%">Parameter</th><th>Company</th><th>Segment median</th><th>Gap</th><th style="width:12%">Rating</th></tr>
${param_rows}
</table>
<div class="note">${param_note}</div>

<div class="two">
<div>
<h2>Price position</h2>
<table>
${price_rows}
</table>
<div class="note">${price_note}</div>
</div>
<div>
<h2>Where the segment sits</h2>
<table>
<tr><th class="l">${family} segment</th><th>Cos</th><th>Avg rating</th></tr>
${family_rows}
</table>
<div class="note">${family_note}</div>
</div>
</div>

<h2>Segment ladder &mdash; ${segment}</h2>
<table>
<tr><th style="width:6%">#</th><th class="l" style="width:34%">Company</th><th>Mcap (&#8377; Cr)</th><th>CMP (&#8377;)</th><th>Growth</th><th>Value</th><th>Momentum</th><th>Rating</th><th class="l" style="width:11%">Verdict</th></tr>
${ladder_rows}
</table>
<div class="note">${ladder_note}</div>

</div>
""")

PAGE2_TMPL = _Tmpl(r"""<!-- ================= PAGE 2 ================= -->
<div class="page">
<div class="masthead">
  <div class="mast-l">QUANT RESEARCH NOTE &nbsp;&middot;&nbsp; PAGE 2 OF 2 &nbsp;&middot;&nbsp; COMPANY BACKGROUND</div>
  <div class="mast-r">${company_uc} &nbsp;&middot;&nbsp; ${symbol}</div>
</div>

<h2>What the company does</h2>
${overview}
${moat_risk}
${financials}
${quarter}
<div class="foot">${foot}</div>
</div>
""")




def render_page(title: str, body_html: str) -> str:
    """The full document: ref CSS + ref markup. One place assembles the sheet, so nothing can
    serve the template with a different head than the one the parity test checks."""
    return ("<!DOCTYPE html>\n<html><head><meta charset='utf-8'><title>" + title + "</title>\n"
            "<style>" + REF_CSS + "</style>\n</head>\n<body>" + body_html + "</body></html>")


_NOT_FOUND = (
    "<!doctype html><meta charset='utf-8'><title>Not found</title>"
    "<body style=\"font-family:Helvetica,Arial,sans-serif;padding:40px;color:#12161C\">"
    "<h2 style='margin:0 0 8px'>No quant note for %s</h2>"
    "<p style='color:#6B7683;margin:0'>That symbol is not in the latest scored universe. "
    "Check the ticker, or open it from the GVM screen and press <b>2 Pager</b>.</p></body>"
)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# R6-P3 · PAGE 1 BINDING
# ══════════════════════════════════════════════════════════════════════════════════════════════
# EVERY NUMBER ON PAGE 1 COMES FROM THE EXISTING BUILDER, called in-process. gvm_company_report()
# is imported and invoked directly rather than fetched over HTTP: the sheet and the /cio2 card
# must be one computation consumed twice, and an HTTP hop to ourselves would only add a way for
# the two to disagree. Nothing here re-implements any scoring.
#
# TWO PLACES THIS BINDING DELIBERATELY DEPARTS FROM THE REF, both because the ref's static copy
# says something the data does not support. Both are logged to the room, neither is silent:
#
#   1. THE LADDER'S MARKET CAP COLUMN. report §E warns that gvm_scores.market_cap is stale and
#      says nothing user-facing is wrong today because the live page reads screener_raw. That is
#      true of the HEADER only. `ladder[].market_cap` in the /api/gvm/company payload is built
#      from gvm_scores (gvm_page_extras.py step 9), so the live card already prints BHARATSE at
#      1,157 Cr in its own ladder while its header reads 1,558 Cr — the same company, the same
#      page, two numbers. The ref's ladder prints 1,558 and its peer mcaps reconcile to
#      screener_raw exactly (BELRISE 24,696 · MINDACORP 17,215 · SSWL 4,921), so the ref is
#      screener-based and the payload's ladder column is the stale one. This module therefore
#      re-reads mcap for the ladder symbols from screener_raw and never touches the payload's
#      column. Fixing the GVM page itself is out of scope (§D: payload shape is read-only).
#
#   2. "MEDIAN RATING" IN THE SEGMENT-FAMILY TABLE. The card's prose says median gvm_score; the
#      ref's numbers are the simple AVERAGE (7.02 / 6.56 / 6.47 / 6.40 / 6.37 reproduce exactly
#      as AVG, while the medians are 7.19 / 6.82 / 6.95 / 6.41 / 6.43 and reorder the table).
#      The ref is the binding artefact, so the average is what is computed — and the column
#      header is relabelled "Avg rating" rather than left saying median over a mean.
#
# PROSE. The ref's <div class="note"> lines are company-specific commentary written by hand for
# BHARATSE. Reproducing that sentence structure for an arbitrary symbol would invent readings the
# data does not carry, which is precisely what P5 forbids. So the notes are rebuilt from the
# numbers alone: each states what the figures are, and stops. A shorter, true note beats a
# fluent, invented one.

def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _esc(s) -> str:
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _seg_disp(seg) -> str:
    """The ref types segment names with an em dash; the DB stores ' - '. Typography only."""
    return _esc(seg).replace(" - ", " &mdash; ") if seg else "&mdash;"


def _num(v, nd=2, suffix="", plus=False):
    v = _f(v)
    if v is None:
        return "&mdash;"
    s = ("%+.*f" if plus else "%.*f") % (nd, v)
    return s + suffix


def _inr_cr(v):
    v = _f(v)
    return "&mdash;" if v is None else "&#8377;%s<small> Cr</small>" % format(int(round(v)), ",d")


def _rating_cls(r):
    r = _f(r)
    if r is None:
        return "muted"
    return "g" if r >= 7.5 else ("a" if r >= 3.75 else "r")


def _verdict_cls(v):
    v = (v or "").strip().lower()
    return "g" if v == "good" else ("r" if v == "weak" else "a")


# The ref's page-1 parameter table, in the ref's order and grouping, keyed to the benchmark rows
# the existing builder already returns. Labels are the ref's — they read better than the API's and
# they are accurate; the VALUES behind them are all bound.
PARAM_GROUPS = [
    ("Growth &amp; quality", [
        ("sales_5y",     "Sales growth &mdash; 5 year"),
        ("sales_3y",     "Sales growth &mdash; 3 year"),
        ("profit_5y",    "Profit growth &mdash; 5 year"),
        ("profit_3y",    "Profit growth &mdash; 3 year"),
        ("qoq_sales",    "Sales growth &mdash; latest quarter YoY"),
        ("qoq_profit",   "Profit growth &mdash; latest quarter YoY"),
        ("roce",         "Return on capital employed"),
        ("int_cov",      "Interest coverage"),
        ("opm",          "Operating margin"),
    ]),
    ("Valuation &amp; ownership", [
        ("pe",           "P/E (TTM)"),
        ("div_yield",    "Dividend yield"),
        ("inst_abs",     "Institutional holding"),
        ("inst_chg",     "Institutional holding &mdash; QoQ change"),
    ]),
    ("Momentum", [
        ("ret_1y",       "Return &mdash; 1 year"),
        ("ret_52w_idx",  "52-week return vs index"),
    ]),
]


def _bench_map(rep):
    out = {}
    for b in (rep.get("benchmark") or []):
        if b.get("key"):
            out[b["key"]] = b
    return out


def _param_rows(bm):
    """One <tr> per ref row. A parameter with no company value prints an em-dash row rather than
    vanishing — a missing line in a fixed table reads as "not applicable", which is a claim."""
    out = []
    for group, keys in PARAM_GROUPS:
        out.append('<tr class="grp"><td class="l" colspan="5">%s</td></tr>' % group)
        for key, label in keys:
            b = bm.get(key) or {}
            unit = b.get("unit") or ""
            comp, peer, rating = _f(b.get("company")), _f(b.get("peer_median")), _f(b.get("rating"))
            if peer is None:
                peer = _f(b.get("peer_avg"))
            gap = None if (comp is None or peer is None) else comp - peer
            # Direction is not the sign: a lower P/E is a better gap. `beats_peer` already encodes
            # each metric's direction inside the builder, so the colour is read from it, never
            # re-derived here where it would get P/E and margins backwards.
            gcls = "muted" if gap is None else ("g" if b.get("beats_peer") else "r")
            out.append(
                '<tr><td class="l">%s</td><td>%s</td><td>%s</td><td class="%s">%s</td>'
                '<td class="%s">%s</td></tr>' % (
                    label, _num(comp, 2, unit), _num(peer, 2, unit),
                    gcls, _num(gap, 2, "", plus=True),
                    _rating_cls(rating), _num(rating, 1)))
    return "\n".join(out)


def _pillars(rep, ladder):
    """Growth / Value / Momentum with the score, the bar, and — in place of the ref's hand-written
    verdict line — the pillar's RANK inside the segment, computed from the ladder the builder
    already returns. A rank is a fact; "top of segment on every growth window" is a reading."""
    scores = rep.get("scores") or {}
    sym = rep.get("symbol")
    out = []
    for name, skey, lkey in (("Growth", "g", "g"), ("Value", "v", "v"), ("Momentum", "m", "m")):
        val = _f(scores.get(skey))
        vals = [(r.get("symbol"), _f(r.get(lkey))) for r in ladder if _f(r.get(lkey)) is not None]
        vals.sort(key=lambda x: -x[1])
        rank = next((i + 1 for i, (s, _) in enumerate(vals) if s == sym), None)
        line = ("Rank %d of %d in segment" % (rank, len(vals))) if rank else "Not ranked in segment"
        pct = 0 if val is None else max(0, min(100, int(round(val * 10))))
        out.append('  <div class="pil"><div class="n">%s</div><div class="s">%s</div>'
                   '<div class="l">%s</div><div class="bar"><i style="width:%d%%"></i></div></div>'
                   % (name, _num(val, 2), line, pct))
    return "\n".join(out)


def _price_rows(rep, price):
    ex = rep.get("extras") or {}
    r52 = ex.get("range52") or {}
    hi, lo = _f(r52.get("hi")), _f(r52.get("lo"))
    price = _f(price)
    dist = None if (hi is None or price is None or hi == 0) else (price - hi) / hi * 100.0
    bars = (ex.get("volume") or {}).get("bars") or []
    vols = [_f(b.get("v")) for b in bars if _f(b.get("v")) is not None]
    avg_vol = (sum(vols) / len(vols)) if vols else None
    rows = [
        ('52-week high', "&#8377;%s" % _num(hi, 2)),
        ('52-week low', "&#8377;%s" % _num(lo, 2)),
        ('Distance from 52w high', _num(dist, 1, "%", plus=True)),
        ('Avg daily volume &middot; %dd' % len(vols) if vols else 'Avg daily volume',
         "&mdash;" if avg_vol is None else "%.2f lakh" % (avg_vol / 100000.0)),
    ]
    cls = "" if dist is None else (' class="a"' if dist > -10 else ' class="muted"')
    out = []
    for n, (label, value) in enumerate(rows):
        c = cls if n == 2 else ""
        out.append('<tr><td class="l">%s</td><td%s>%s</td></tr>' % (label, c, value))
    return "\n".join(out), hi, lo, dist, avg_vol, len(vols)


def _family_rows(cur, segment, score_date):
    """Sibling segments sharing the family prefix before the dash, at the latest score_date.

    AVG, not median — see the header note: the binding ref's numbers are averages. The subject's
    own segment carries class="me" so it is highlighted the way the ref highlights it."""
    fam = (segment or "").split(" - ")[0].strip()
    if not fam:
        return "", fam, [], None
    cur.execute(
        """SELECT segment, COUNT(*) AS n, AVG(gvm_score) AS avg_gvm
             FROM gvm_scores
            WHERE score_date = %s AND (segment = %s OR segment LIKE %s)
            GROUP BY segment
            ORDER BY AVG(gvm_score) DESC NULLS LAST""",
        (score_date, fam, fam + " - %"))
    rows = cur.fetchall()
    out = []
    for seg, n, avg in rows:
        me = ' class="me"' if seg == segment else ""
        out.append('<tr%s><td class="l">%s</td><td>%d</td><td>%s</td></tr>'
                   % (me, _seg_disp(seg), int(n), _num(avg, 2)))
    rank = next((i + 1 for i, r in enumerate(rows) if r[0] == segment), None)
    return "\n".join(out), fam, rows, rank


def _screener_quotes(cur, symbols):
    """Price AND market cap for the ladder, both from screener_raw — the pair that reconciles.

    NEVER the payload's `ladder[].market_cap` (gvm_scores-derived and stale), and the payload
    carries no per-peer price at all, so screener_raw is the only source that can fill the CMP
    column for twelve of the thirteen rows. It reproduces the ref's ladder exactly on both
    columns for all 13 Auto - Body & Stampings names.

    ONE PRICE SOURCE FOR THE WHOLE SHEET. The subject's hero CMP is taken from here too, not from
    the report payload's resolved price, because a sheet whose header says one number and whose
    own ladder row says another is the exact defect this module already avoids on market cap.
    For BHARATSE today that is 248.05 (also the 14-Aug daily close, and the ref's figure) against
    the payload's 245.70 "Last Tick"; the divergence is logged to the room, not papered over.

    screener_raw keys on nse_code — it has no `symbol` column."""
    if not symbols:
        return {}
    cur.execute("SELECT nse_code, price, market_cap FROM screener_raw WHERE nse_code = ANY(%s)",
                (list(symbols),))
    return {s: {"price": _f(p), "market_cap": _f(m)} for s, p, m in cur.fetchall()}


def _ladder_rows(rep, ladder, quotes):
    out = []
    for i, r in enumerate(ladder, 1):
        me = ' class="me"' if r.get("is_self") else ""
        q = quotes.get(r.get("symbol")) or {}
        mc, px = q.get("market_cap"), q.get("price")
        out.append(
            '<tr%s><td>%d</td><td class="l">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>'
            '<td>%s</td><td>%s</td><td class="l %s">%s</td></tr>' % (
                me, i, _esc(r.get("company_name") or r.get("symbol")),
                "&mdash;" if mc is None else format(int(round(mc)), ",d"),
                _num(px, 2), _num(r.get("g"), 2), _num(r.get("v"), 2), _num(r.get("m"), 2),
                _num(r.get("gvm"), 2),
                _verdict_cls(r.get("verdict")), _esc(r.get("verdict") or "&mdash;")))
    return "\n".join(out)


def build_page1(cur, rep) -> str:
    """Assemble page 1 from the report payload plus the two reads the payload cannot supply
    honestly (screener_raw mcap for the ladder, and the segment-family averages)."""
    bm = _bench_map(rep)
    ladder = rep.get("ladder") or []
    segment = rep.get("segment")
    score_date = rep.get("score_date")

    quotes = _screener_quotes(cur, [r.get("symbol") for r in ladder if r.get("symbol")])
    # ONE price for the whole sheet — see _screener_quotes. Falls back to the payload's resolved
    # price only if the symbol is absent from screener_raw, so the hero is never blank.
    px = (quotes.get(rep.get("symbol")) or {}).get("price")
    if px is None:
        px = _f(rep.get("price"))
    fam_rows, fam, fam_data, fam_rank = _family_rows(cur, segment, score_date)
    price_rows, hi, lo, dist, avg_vol, vol_n = _price_rows(rep, px)

    peer_n = rep.get("peer_count") or rep.get("segment_total")
    pe_b = bm.get("pe") or {}
    hist = (pe_b.get("extra_marker") or {})
    # The ref's note says "10-year average" while the source field is the OWN 5-YEAR average
    # (extra_marker label "own 5y avg", 18.88). The label is taken from the data, not the ref.
    hist_txt = ""
    if _f(hist.get("value")) is not None:
        hist_txt = (" P/E of %s against its %s of %s."
                    % (_num(pe_b.get("company"), 1, "x"), _esc(hist.get("label") or "own average"),
                       _num(hist.get("value"), 1, "x")))
    param_note = ("Peer figure is the median of the %s rated companies in the segment; ratings are "
                  "peer-benchmarked 0&ndash;10.%s" % (peer_n if peer_n else "&mdash;", hist_txt))

    price_note = ""
    if hi is not None and lo is not None and px is not None:
        span = hi - lo
        pos = None if span <= 0 else (px - lo) / span * 100.0
        price_note = ("Trading %s of its 52-week range%s."
                      % (_num(pos, 0, "%") if pos is not None else "&mdash;",
                         "" if dist is None else ", %s from the high" % _num(abs(dist), 1, "%")))
    if avg_vol is not None:
        price_note += " Volume is the mean of the last %d sessions." % vol_n

    fam_note = ""
    if fam_data and fam_rank:
        fam_note = ("%s ranks %d of the %d %s segments by average rating."
                    % (_seg_disp(segment), fam_rank, len(fam_data), _esc(fam)))

    self_row = next((r for r in ladder if r.get("is_self")), None)
    ladder_note = ""
    if ladder:
        ladder_note = ("%s of %s in the segment by overall rating."
                       % (self_row.get("rank") if self_row and self_row.get("rank") else "&mdash;",
                          len(ladder)))
        gs = [_f(r.get("g")) for r in ladder if _f(r.get("g")) is not None]
        ms = [_f(r.get("m")) for r in ladder if _f(r.get("m")) is not None]
        if gs and ms:
            ladder_note += (" Growth spans %s&ndash;%s, momentum spans %s&ndash;%s across the ladder."
                            % (_num(min(gs), 2), _num(max(gs), 2), _num(min(ms), 2), _num(max(ms), 2)))
        ladder_note += (" CMP and market cap from screener_raw &mdash; the pair that reconciles."
                        " Data as of %s &middot; research only, not investment advice."
                        % _esc(score_date))

    return PAGE1_TMPL.safe_substitute(
        printed_on=_ist_today().strftime("%d %b %Y").upper(),
        score_date=_esc(score_date),
        company_name=_esc(rep.get("company_name") or rep.get("symbol")),
        symbol=_esc(rep.get("symbol")),
        segment=_seg_disp(segment),
        cap_category=_esc((rep.get("cap_category") or "").title() + " cap").strip() or "&mdash;",
        mcap_rank=_esc(rep.get("mcap_rank")) if rep.get("mcap_rank") is not None else "&mdash;",
        gvm=_num((rep.get("scores") or {}).get("gvm"), 2),
        verdict=_esc((rep.get("verdict") or "").upper()) or "&mdash;",
        price_label="CMP",
        price="&#8377;%s" % _num(px, 2),
        market_cap=_inr_cr(rep.get("market_cap")),
        pe="%s<small>x</small>" % _num(pe_b.get("company"), 1),
        ret_1y=_num((bm.get("ret_1y") or {}).get("company"), 1, "%", plus=True),
        pillars=_pillars(rep, ladder),
        punchline=_esc(rep.get("punchline") or ""),
        param_rows=_param_rows(bm),
        param_note=param_note,
        price_rows=price_rows,
        price_note=price_note,
        family=_esc(fam),
        family_rows=fam_rows,
        family_note=fam_note,
        ladder_rows=_ladder_rows(rep, ladder, quotes),
        ladder_note=ladder_note,
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════
# R6-P4 · PAGE 2 BINDING — COMPANY BACKGROUND ONLY
# ══════════════════════════════════════════════════════════════════════════════════════════════
# FOUNDER-LOCKED: page 2 carries NO rating, NO score, NO pillar value, and the string GVM must not
# appear. Nothing in this section reads rep["scores"], rep["verdict"] or any benchmark rating — the
# page is built from input_raw.overview and screener_raw and from nothing else.
#
# THE VERIFY GREP HAS TO USE WORD BOUNDARIES, and that is not a loophole. P4 asks for a
# case-insensitive grep for `gvm`, `rating`, `score`. As a bare substring, "rating" is inside
# "ope-RATING margin" — a field P4 itself requires on this page — so the literal grep fails on the
# REF's own page 2, which prints "Operating margin" twice. `\brating\b` is the test that means what
# the rule means. test_twopager_page2_clean.py runs it.
#
# NINE COMPANIES WHOSE OWN BUSINESS IS RATINGS. ICRA, CRISIL, CARERATING, and six lenders/media
# names carry "rating" or "score" inside input_raw.overview as a description of what they DO
# (ICRA is a credit rating agency). Redacting that would make the page wrong about the company.
# The ban is on OUR assessment appearing on page 2, not on the English language: the module emits
# no rating of its own, and source prose is passed through with its own words. All nine are named
# in the room log rather than silently rewritten. Zero of 1,791 overviews contain "GVM".
#
# NO INVENTED PROSE. The ref's "The business model in one line" and "The one thing to watch" are
# hand-written readings of BHARATSE with no field behind them, so they are NOT reproduced — there
# is no source that could fill them for another symbol, and filling them from a template would be
# the fabrication P5 exists to prevent. Moat and risk are split out of the overview's own
# "Moat:" / "Key risk:" markers when the text carries them, and the block is dropped when it
# does not.

# Every screener_raw column page 2 reads, in one query. Quoted names are screener.in's own headers.
_SCREENER_COLS = [
    "Sales", "Profit after tax", "opm", "roce", "Return on equity", "Debt to equity",
    "interest_coverage", "Price to book value", "dividend_yield", "Promoter holding",
    "fii_holding", "dii_holding", "pe", "historical_pe", "sales_growth_3y", "sales_growth_5y",
    "profit_growth_3y", "profit_growth_5y", "sales_latest_quarter",
    "profit_after_tax_latest_quarter", "sales_preceding_year_quarter",
    "profit_after_tax_preceding_year_quarter", "opm_latest_q", "opm_prev_year_q",
    "last_result_quarter",
]


def _screener_row(cur, symbol):
    """One row of screener_raw, keyed by nse_code. Returns None when the company has no row —
    P5 omits the financial sections entirely in that case rather than printing a grid of dashes."""
    cols = ", ".join('"%s"' % c for c in _SCREENER_COLS)
    cur.execute("SELECT %s FROM screener_raw WHERE nse_code = %%s" % cols, (symbol,))
    row = cur.fetchone()
    return dict(zip(_SCREENER_COLS, row)) if row else None


def _overview_text(cur, symbol):
    cur.execute("SELECT overview FROM input_raw WHERE nse_code = %s", (symbol,))
    row = cur.fetchone()
    return (row[0] if row else None) or ""


def _paras(text):
    """Source prose to <p> blocks. Escaped, never reflowed, never trimmed to fit."""
    out = []
    for block in [b.strip() for b in (text or "").split("\n\n")]:
        if block:
            out.append("<p>%s</p>" % _esc(block).replace("\n", "<br>"))
    return "\n".join(out)


def _split_moat_risk(text):
    """Pull the overview's own "Moat:" and "Key risk:" clauses out into the two-column block.

    These markers are a convention of the input_raw prose, not something this module invents. When
    a symbol's overview has neither, the caller drops the block — an empty pair of headings is
    worse than no block at all."""
    import re as _re
    body, moat, risk = text or "", None, None
    m = _re.search(r"\bMoat\s*:\s*(.+?)(?=\n\n|\Z)", body, _re.S | _re.I)
    if m:
        moat = m.group(1).strip()
        body = body[:m.start()] + body[m.end():]
    r = _re.search(r"\bKey risk[s]?\s*:\s*(.+?)(?=\n\n|\Z)", body, _re.S | _re.I)
    if r:
        risk = r.group(1).strip()
        body = body[:r.start()] + body[r.end():]
    return body.strip(), moat, risk


def _cr(v, nd=0):
    v = _f(v)
    if v is None:
        return "&mdash;"
    return "&#8377;%s Cr" % (format(int(round(v)), ",d") if nd == 0 else ("%.*f" % (nd, v)))


def _row2(label, value):
    return '<tr><td class="l">%s</td><td>%s</td></tr>' % (label, value)


def _financials_block(sc):
    """The two side-by-side tables. Returns "" when there is no screener_raw row (P5)."""
    if not sc:
        return ""
    sales, pat = _f(sc["Sales"]), _f(sc["Profit after tax"])
    net_margin = None if (sales in (None, 0) or pat is None) else pat / sales * 100.0
    left = "\n".join([
        '<tr class="grp"><td class="l" colspan="2">Scale &middot; trailing twelve months</td></tr>',
        _row2("Revenue", _cr(sales)),
        _row2("Profit after tax", _cr(pat, 1)),
        _row2("Operating margin", _num(sc["opm"], 2, "%")),
        _row2("Net margin", _num(net_margin, 2, "%")),
        '<tr class="grp"><td class="l" colspan="2">Returns &amp; balance sheet</td></tr>',
        _row2("Return on capital employed", _num(sc["roce"], 2, "%")),
        _row2("Return on equity", _num(sc["Return on equity"], 2, "%")),
        _row2("Debt to equity", _num(sc["Debt to equity"], 2, "x")),
        _row2("Interest coverage", _num(sc["interest_coverage"], 2, "x")),
        _row2("Price to book", _num(sc["Price to book value"], 2, "x")),
    ])
    prom, fii, dii = (_f(sc["Promoter holding"]), _f(sc["fii_holding"]), _f(sc["dii_holding"]))
    public = None if None in (prom, fii, dii) else max(0.0, 100.0 - prom - fii - dii)
    right = "\n".join([
        '<tr class="grp"><td class="l" colspan="3">Growth record &middot; CAGR</td></tr>',
        '<tr><th class="l">&nbsp;</th><th>3 year</th><th>5 year</th></tr>',
        '<tr><td class="l">Sales</td><td>%s</td><td>%s</td></tr>'
        % (_num(sc["sales_growth_3y"], 2, "%"), _num(sc["sales_growth_5y"], 2, "%")),
        '<tr><td class="l">Profit</td><td>%s</td><td>%s</td></tr>'
        % (_num(sc["profit_growth_3y"], 2, "%"), _num(sc["profit_growth_5y"], 2, "%")),
        '<tr class="grp"><td class="l" colspan="3">Ownership</td></tr>',
        '<tr><td class="l">Promoter group</td><td colspan="2">%s</td></tr>' % _num(prom, 2, "%"),
        '<tr><td class="l">Foreign institutions</td><td colspan="2">%s</td></tr>' % _num(fii, 2, "%"),
        '<tr><td class="l">Domestic institutions</td><td colspan="2">%s</td></tr>' % _num(dii, 2, "%"),
        '<tr><td class="l">Public &amp; others</td><td colspan="2">%s</td></tr>' % _num(public, 2, "%"),
        '<tr class="grp"><td class="l" colspan="3">Valuation reference</td></tr>',
        '<tr><td class="l">P/E &mdash; current</td><td colspan="2">%s</td></tr>' % _num(sc["pe"], 2, "x"),
        '<tr><td class="l">P/E &mdash; own 5-year average</td><td colspan="2">%s</td></tr>'
        % _num(sc["historical_pe"], 2, "x"),
        '<tr><td class="l">Dividend yield</td><td colspan="2">%s</td></tr>'
        % _num(sc["dividend_yield"], 2, "%"),
    ])
    # The note states the arithmetic relationship between the two growth lines and stops. The ref's
    # version reads that as operating leverage flattening; that is a judgement, not a field.
    note = ""
    s3, p3 = _f(sc["sales_growth_3y"]), _f(sc["profit_growth_3y"])
    s5, p5v = _f(sc["sales_growth_5y"]), _f(sc["profit_growth_5y"])
    if None not in (s3, p3, s5, p5v):
        faster = (p3 > s3) and (p5v > s5)
        slower = (p3 < s3) and (p5v < s5)
        if faster:
            note = "Profit has compounded faster than sales over both windows."
        elif slower:
            note = "Sales has compounded faster than profit over both windows."
        else:
            note = "Profit and sales lead each other on different windows."
    q_now, q_prev = _f(sc["opm_latest_q"]), _f(sc["opm_prev_year_q"])
    if None not in (q_now, q_prev):
        note += (" Latest-quarter operating margin is %s against %s a year ago."
                 % (_num(q_now, 2, "%"), _num(q_prev, 2, "%")))
    return ('<h2>Financial profile</h2>\n<div class="two">\n<div>\n<table>\n%s\n</table>\n</div>\n'
            '<div>\n<table>\n%s\n</table>\n</div>\n</div>\n<div class="note">%s</div>'
            % (left, right, note))


def _quarter_block(sc):
    """Latest quarter vs the same quarter last year. Returns "" with no screener_raw row (P5)."""
    if not sc:
        return ""
    s_now, s_prev = _f(sc["sales_latest_quarter"]), _f(sc["sales_preceding_year_quarter"])
    p_now, p_prev = (_f(sc["profit_after_tax_latest_quarter"]),
                     _f(sc["profit_after_tax_preceding_year_quarter"]))
    m_now, m_prev = _f(sc["opm_latest_q"]), _f(sc["opm_prev_year_q"])
    if s_now is None and p_now is None and m_now is None:
        return ""
    q = _esc(sc["last_result_quarter"]) if sc["last_result_quarter"] else "latest quarter"

    def pct(now, prev):
        if now is None or prev in (None, 0):
            return "muted", "&mdash;"
        ch = (now - prev) / abs(prev) * 100.0
        return ("g" if ch >= 0 else "r"), _num(ch, 1, "%", plus=True)

    scls, sval = pct(s_now, s_prev)
    pcls, pval = pct(p_now, p_prev)
    if None in (m_now, m_prev):
        mcls, mval = "muted", "&mdash;"
    else:
        bps = (m_now - m_prev) * 100.0
        mcls, mval = ("g" if bps >= 0 else "a"), "%+d bps" % int(round(bps))
    rows = "\n".join([
        '<tr><th class="l" style="width:40%%">&nbsp;</th><th>%s</th><th>Year ago</th><th>Change</th></tr>' % q,
        '<tr><td class="l">Revenue</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>'
        % (_cr(s_now, 1), _cr(s_prev, 1), scls, sval),
        '<tr><td class="l">Profit after tax</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>'
        % (_cr(p_now, 1), _cr(p_prev, 1), pcls, pval),
        '<tr><td class="l">Operating margin</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>'
        % (_num(m_now, 2, "%"), _num(m_prev, 2, "%"), mcls, mval),
    ])
    note = ""
    if None not in (s_now, s_prev, p_now, p_prev) and s_prev and p_prev:
        sg = (s_now - s_prev) / abs(s_prev) * 100.0
        pg = (p_now - p_prev) / abs(p_prev) * 100.0
        note = ("Profit grew faster than sales this quarter." if pg > sg
                else ("Sales grew faster than profit this quarter." if sg > pg
                      else "Sales and profit grew at the same rate this quarter."))
    return ('<h2>Latest quarter &mdash; %s</h2>\n<table>\n%s\n</table>\n<div class="note">%s</div>'
            % (q, rows, note))


def build_page2(cur, rep) -> str:
    """Company background. Reads input_raw.overview + screener_raw and nothing score-bearing."""
    sym = rep.get("symbol")
    raw = _overview_text(cur, sym)
    sc = _screener_row(cur, sym)

    # P5's threshold, applied here so P4 never renders a stub paragraph as if it were a profile.
    if len((raw or "").strip()) < 100:
        overview_html = "<p>Business profile not available for this company.</p>"
        moat = risk = None
    else:
        body, moat, risk = _split_moat_risk(raw)
        overview_html = _paras(body) or "<p>Business profile not available for this company.</p>"

    moat_risk = ""
    if moat or risk:
        cols = []
        if moat:
            cols.append("<div>\n<h3>Why the position is hard to attack</h3>\n%s\n</div>" % _paras(moat))
        if risk:
            cols.append("<div>\n<h3>What would break it</h3>\n%s\n</div>" % _paras(risk))
        moat_risk = '\n<div class="two">\n%s\n</div>\n' % "\n".join(cols)

    foot = ("Figures from company filings via the fundamentals pipeline, as of %s. "
            "Research only &mdash; not investment advice." % _esc(rep.get("score_date")))

    return PAGE2_TMPL.safe_substitute(
        company_uc=_esc((rep.get("company_name") or sym or "").upper()),
        symbol=_esc(sym),
        overview=overview_html,
        moat_risk=moat_risk,
        financials=_financials_block(sc),
        quarter=_quarter_block(sc),
        foot=foot,
    )

@router.get("/gvm/2pager/{symbol}", response_class=HTMLResponse)
def gvm_two_pager(symbol: str):
    """The 2-Pager. P3 binds page 1 from the existing builder, P4 binds page 2 from
    input_raw.overview + screener_raw. Page 2 carries nothing score-bearing (founder-locked)."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=404, detail="symbol required")

    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                known = symbol_exists(cur, sym)
    except Exception as e:
        # A DB failure is not a missing symbol, and saying "not found" here would be a lie that
        # sends someone hunting for a ticker problem they do not have.
        log.error("gvm_twopager: lookup failed for %s: %s", sym, e, exc_info=True)
        raise HTTPException(status_code=503, detail="scoring data unavailable right now")

    if not known:
        return HTMLResponse(_NOT_FOUND % sym, status_code=404)

    # The existing builder, in-process. Imported here rather than at module scope so an import
    # cycle in the report stack can never take down app start-up over a print route.
    try:
        from gvm_report_endpoints import gvm_company_report
        rep = gvm_company_report(sym)
    except HTTPException:
        raise
    except Exception as e:
        log.error("gvm_twopager: report build failed for %s: %s", sym, e, exc_info=True)
        raise HTTPException(status_code=503, detail="report unavailable right now")

    try:
        with _conn() as conn, conn.cursor() as cur:
            page1 = build_page1(cur, rep)
            page2 = build_page2(cur, rep)
    except Exception as e:
        log.error("gvm_twopager: page bind failed for %s: %s", sym, e, exc_info=True)
        raise HTTPException(status_code=503, detail="report unavailable right now")

    return HTMLResponse(render_page(doc_title(sym), page1 + "\n" + page2))

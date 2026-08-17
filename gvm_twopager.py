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

PAGE_BODY = r"""

<!-- ================= PAGE 1 ================= -->
<div class="page">
<div class="masthead">
  <div class="mast-l">QUANT RESEARCH NOTE &nbsp;·&nbsp; PAGE 1 OF 2 &nbsp;·&nbsp; QUANT ANALYTICS</div>
  <div class="mast-r">17 AUG 2026 &nbsp;·&nbsp; DATA AS OF 16 AUG 2026</div>
</div>

<h1>Bharat Seats Ltd</h1>
<div class="sub">BHARATSE &nbsp;·&nbsp; NSE / BSE &nbsp;·&nbsp; Auto — Body &amp; Stampings &nbsp;·&nbsp; Micro cap &nbsp;·&nbsp; Mcap rank 1396</div>

<div class="hero">
  <div class="cell rate">
    <div class="k">Overall Rating</div>
    <div class="v">7.28 <span class="tag">GOOD</span></div>
  </div>
  <div class="cell">
    <div class="k">CMP</div>
    <div class="v">₹248.05</div>
  </div>
  <div class="cell">
    <div class="k">Market Cap</div>
    <div class="v">₹1,558<small> Cr</small></div>
  </div>
  <div class="cell">
    <div class="k">P/E · TTM</div>
    <div class="v">33.0<small>x</small></div>
  </div>
  <div class="cell">
    <div class="k">1-Year Return</div>
    <div class="v g" style="color:#1F7A4D">+111%</div>
  </div>
</div>

<div class="pillars">
  <div class="pil"><div class="n">Growth</div><div class="s">7.14</div><div class="l">Healthy — top of segment on every growth window</div><div class="bar"><i style="width:71%"></i></div></div>
  <div class="pil"><div class="n">Value</div><div class="s">6.25</div><div class="l">Fair — P/E above segment and above own history</div><div class="bar"><i style="width:63%"></i></div></div>
  <div class="pil"><div class="n">Momentum</div><div class="s">8.44</div><div class="l">Strong — 2nd best in segment on relative strength</div><div class="bar"><i style="width:84%"></i></div></div>
</div>

<div class="punch">Buy for a medium to long term view — healthy growth, fair valuation and strong momentum, on a good overall rating.</div>

<h2>Parameter detail — company vs segment peers</h2>
<table>
<tr><th class="l" style="width:38%">Parameter</th><th>Company</th><th>Segment median</th><th>Gap</th><th style="width:12%">Rating</th></tr>

<tr class="grp"><td class="l" colspan="5">Growth &amp; quality</td></tr>
<tr><td class="l">Sales growth — 5 year</td><td>28.9%</td><td>15.9%</td><td class="g">+13.0</td><td class="g">10.0</td></tr>
<tr><td class="l">Sales growth — 3 year</td><td>22.9%</td><td>8.2%</td><td class="g">+14.7</td><td class="g">10.0</td></tr>
<tr><td class="l">Profit growth — 5 year</td><td>56.7%</td><td>32.9%</td><td class="g">+23.7</td><td class="g">10.0</td></tr>
<tr><td class="l">Profit growth — 3 year</td><td>26.2%</td><td>21.6%</td><td class="g">+4.6</td><td class="g">8.8</td></tr>
<tr><td class="l">Sales growth — latest quarter YoY</td><td>35.3%</td><td>25.2%</td><td class="g">+10.1</td><td class="g">10.0</td></tr>
<tr><td class="l">Profit growth — latest quarter YoY</td><td>43.9%</td><td>35.2%</td><td class="g">+8.7</td><td class="g">8.8</td></tr>
<tr><td class="l">Return on capital employed</td><td>20.2%</td><td>14.8%</td><td class="g">+5.4</td><td class="g">10.0</td></tr>
<tr><td class="l">Interest coverage</td><td>7.5x</td><td>4.1x</td><td class="g">+3.4</td><td class="g">8.8</td></tr>
<tr><td class="l">Operating margin</td><td>5.03%</td><td>10.29%</td><td class="r">−5.26</td><td class="a">5.0</td></tr>

<tr class="grp"><td class="l" colspan="5">Valuation &amp; ownership</td></tr>
<tr><td class="l">P/E (TTM)</td><td>32.96x</td><td>24.27x</td><td class="r">+8.7</td><td class="r">2.5</td></tr>
<tr><td class="l">Dividend yield</td><td>0.60%</td><td>0.36%</td><td class="g">+0.24</td><td class="g">7.5</td></tr>
<tr><td class="l">Institutional holding</td><td>0.37%</td><td>2.19%</td><td class="r">−1.82</td><td class="r">2.5</td></tr>
<tr><td class="l">Institutional holding — QoQ change</td><td>+0.01</td><td>+0.02</td><td class="muted">−0.01</td><td class="a">3.8</td></tr>

<tr class="grp"><td class="l" colspan="5">Momentum</td></tr>
<tr><td class="l">Return — 1 year</td><td>+111.5%</td><td>+45.4%</td><td class="g">+66.1</td><td class="g">10.0</td></tr>
<tr><td class="l">52-week return vs index</td><td>+104.6%</td><td>+51.0%</td><td class="g">+53.6</td><td class="g">10.0</td></tr>
</table>
<div class="note">Peer figure is the median of the 13 rated companies in the segment; ratings are peer-benchmarked 0–10. P/E of 33.0x against its own 10-year average of 18.9x is the single weakest input.</div>

<div class="two">
<div>
<h2>Price position</h2>
<table>
<tr><td class="l">52-week high</td><td>₹263.18</td></tr>
<tr><td class="l">52-week low</td><td>₹121.51</td></tr>
<tr><td class="l">Distance from 52w high</td><td class="a">−5.7%</td></tr>
<tr><td class="l">Avg daily volume · 30d</td><td>4.80 lakh</td></tr>
</table>
<div class="note">Trading in the top decile of its own 52-week range. Momentum is earned, not extrapolated — but entry at 5.7% off the high carries no cushion.</div>
</div>
<div>
<h2>Where the segment sits</h2>
<table>
<tr><th class="l">Auto segment</th><th>Cos</th><th>Median rating</th></tr>
<tr><td class="l">Auto — Wiring &amp; Electricals</td><td>14</td><td>7.02</td></tr>
<tr class="me"><td class="l">Auto — Body &amp; Stampings</td><td>13</td><td>6.56</td></tr>
<tr><td class="l">Auto — Drivetrain &amp; Precision</td><td>18</td><td>6.47</td></tr>
<tr><td class="l">Auto — Engines &amp; Thermal</td><td>23</td><td>6.40</td></tr>
<tr><td class="l">Auto OEM</td><td>20</td><td>6.37</td></tr>
</table>
<div class="note">Body &amp; Stampings ranks 2nd of the five auto segments. The band is narrow — read it as an ordering of preference, not a quality gap.</div>
</div>
</div>

<h2>Segment ladder — Auto · Body &amp; Stampings</h2>
<table>
<tr><th style="width:6%">#</th><th class="l" style="width:34%">Company</th><th>Mcap (₹ Cr)</th><th>CMP (₹)</th><th>Growth</th><th>Value</th><th>Momentum</th><th>Rating</th><th class="l" style="width:11%">Verdict</th></tr>
<tr><td>1</td><td class="l">Minda Corporation</td><td>17,215</td><td>720.05</td><td>7.23</td><td>6.25</td><td>9.06</td><td>7.51</td><td class="l g">Good</td></tr>
<tr class="me"><td>2</td><td class="l">Bharat Seats</td><td>1,558</td><td>248.05</td><td>7.14</td><td>6.25</td><td>8.44</td><td>7.28</td><td class="l g">Good</td></tr>
<tr><td>3</td><td class="l">Munjal Auto Industries</td><td>1,205</td><td>120.53</td><td>5.54</td><td>6.25</td><td>10.00</td><td>7.26</td><td class="l g">Good</td></tr>
<tr><td>4</td><td class="l">Talbros Automotive</td><td>2,590</td><td>419.50</td><td>7.41</td><td>5.62</td><td>8.28</td><td>7.10</td><td class="l g">Good</td></tr>
<tr><td>5</td><td class="l">Steel Strips Wheels</td><td>4,921</td><td>312.90</td><td>6.61</td><td>5.62</td><td>8.91</td><td>7.05</td><td class="l g">Good</td></tr>
<tr><td>6</td><td class="l">Belrise Industries</td><td>24,696</td><td>255.35</td><td>6.79</td><td>5.62</td><td>8.44</td><td>6.95</td><td class="l a">Average</td></tr>
<tr><td>7</td><td class="l">Wheels India</td><td>3,508</td><td>1,435.60</td><td>6.70</td><td>6.88</td><td>6.88</td><td>6.82</td><td class="l a">Average</td></tr>
<tr><td>8</td><td class="l">Munjal Showa</td><td>553</td><td>138.21</td><td>5.18</td><td>7.50</td><td>7.50</td><td>6.73</td><td class="l a">Average</td></tr>
<tr><td>9</td><td class="l">Jay Bharat Maruti</td><td>1,384</td><td>127.86</td><td>5.54</td><td>7.50</td><td>5.94</td><td>6.33</td><td class="l a">Average</td></tr>
<tr><td>10</td><td class="l">Automotive Stampings</td><td>792</td><td>499.10</td><td>6.34</td><td>6.25</td><td>4.69</td><td>5.76</td><td class="l r">Weak</td></tr>
<tr><td>11</td><td class="l">Alicon Castalloy</td><td>1,181</td><td>719.60</td><td>5.62</td><td>6.25</td><td>5.00</td><td>5.62</td><td class="l r">Weak</td></tr>
<tr><td>12</td><td class="l">Harsha Engineers Intl.</td><td>3,753</td><td>412.25</td><td>6.07</td><td>6.25</td><td>4.22</td><td>5.51</td><td class="l r">Weak</td></tr>
<tr><td>13</td><td class="l">JBM Auto</td><td>14,732</td><td>622.95</td><td>6.61</td><td>6.25</td><td>3.28</td><td>5.38</td><td class="l r">Weak</td></tr>
</table>
<div class="note">Second of thirteen, and the only top-three name with growth above 7 and momentum above 8 — Munjal Auto's rank rests almost entirely on momentum. What separates this ladder is momentum, not growth: growth spans 5.2–7.4, momentum spans 3.3–10.0. Data as of 16 Aug 2026 · research only, not investment advice.</div>

</div>

<!-- ================= PAGE 2 ================= -->
<div class="page">
<div class="masthead">
  <div class="mast-l">QUANT RESEARCH NOTE &nbsp;·&nbsp; PAGE 2 OF 2 &nbsp;·&nbsp; COMPANY BACKGROUND</div>
  <div class="mast-r">BHARAT SEATS LTD &nbsp;·&nbsp; BHARATSE</div>
</div>

<h2>What the company does</h2>
<p>Bharat Seats makes complete seating systems for passenger cars. The product is not a component but a full assembly — seat frames, moulded foam cushions, and fabric or leather trim, built and delivered as one finished unit. It also supplies seat assemblies for two-wheelers.</p>
<p>The company is part of the Maruti Suzuki ecosystem. Maruti Suzuki and Suzuki Motor Corporation of Japan both sit inside the shareholding alongside the Relan family; total promoter group holding is 74.66%. Plants sit alongside Maruti's own assembly lines and supply on a just-in-time basis.</p>

<h2>The business model in one line</h2>
<p>Effectively a single-customer supplier with a co-located, just-in-time delivery model. Volumes track Maruti Suzuki's production almost one for one. That is the entire moat and the entire risk in the same sentence.</p>

<div class="two">
<div>
<h3>Why the position is hard to attack</h3>
<ul>
  <li>Seats are bulky and cannot be shipped economically over distance — supply must be local to the assembly line.</li>
  <li>Just-in-time sequencing is built into the customer's line; switching suppliers means re-engineering the line, not signing a new contract.</li>
  <li>Shareholding by both the customer and its Japanese parent aligns the relationship structurally, not just commercially.</li>
  <li>Content per seat is rising — lumbar support, ventilated and heated seats on premium variants — so revenue can grow even when car volumes do not.</li>
</ul>
</div>
<div>
<h3>What would break it</h3>
<ul>
  <li>Customer concentration is close to total. A fall in Maruti Suzuki's production flows straight to the top line with no offset.</li>
  <li>Operating margin runs near 5%, roughly half the segment median. This is a volume-and-throughput business, not a pricing business.</li>
  <li>Just-in-time leaves no inventory buffer — a plant stoppage at either end is immediately material.</li>
  <li>Raw material and trim costs are passed through with a lag, so a sharp input move compresses an already thin margin.</li>
  <li>Institutional ownership is 0.37%. Liquidity is thin and the stock can move on small flows.</li>
</ul>
</div>
</div>

<h2>Financial profile</h2>
<div class="two">
<div>
<table>
<tr class="grp"><td class="l" colspan="2">Scale · trailing twelve months</td></tr>
<tr><td class="l">Revenue</td><td>₹2,102 Cr</td></tr>
<tr><td class="l">Profit after tax</td><td>₹47.3 Cr</td></tr>
<tr><td class="l">Operating margin</td><td>5.03%</td></tr>
<tr><td class="l">Net margin</td><td>2.25%</td></tr>
<tr class="grp"><td class="l" colspan="2">Returns &amp; balance sheet</td></tr>
<tr><td class="l">Return on capital employed</td><td>20.19%</td></tr>
<tr><td class="l">Return on equity</td><td>20.43%</td></tr>
<tr><td class="l">Debt to equity</td><td>0.48x</td></tr>
<tr><td class="l">Interest coverage</td><td>7.53x</td></tr>
<tr><td class="l">Price to book</td><td>6.78x</td></tr>
</table>
</div>
<div>
<table>
<tr class="grp"><td class="l" colspan="3">Growth record · CAGR</td></tr>
<tr><th class="l">&nbsp;</th><th>3 year</th><th>5 year</th></tr>
<tr><td class="l">Sales</td><td>22.9%</td><td>28.9%</td></tr>
<tr><td class="l">Profit</td><td>26.2%</td><td>56.7%</td></tr>
<tr class="grp"><td class="l" colspan="3">Ownership</td></tr>
<tr><td class="l">Promoter group</td><td colspan="2">74.66%</td></tr>
<tr><td class="l">Foreign institutions</td><td colspan="2">0.15%</td></tr>
<tr><td class="l">Domestic institutions</td><td colspan="2">0.22%</td></tr>
<tr><td class="l">Public &amp; others</td><td colspan="2">24.97%</td></tr>
<tr class="grp"><td class="l" colspan="3">Valuation reference</td></tr>
<tr><td class="l">P/E — current</td><td colspan="2">32.96x</td></tr>
<tr><td class="l">P/E — own 10-year average</td><td colspan="2">18.88x</td></tr>
<tr><td class="l">Dividend yield</td><td colspan="2">0.60%</td></tr>
</table>
</div>
</div>
<div class="note">Profit has compounded faster than sales over both windows, so the improvement has come from operating leverage on a fixed cost base rather than from pricing. With margin at 5.0% against 5.1% a year ago, that leverage has now flattened — from here, profit growth needs volume growth.</div>

<h2>Latest quarter — Q1 FY27</h2>
<table>
<tr><th class="l" style="width:40%">&nbsp;</th><th>Q1 FY27</th><th>Q1 FY26</th><th>Change</th></tr>
<tr><td class="l">Revenue</td><td>₹577.8 Cr</td><td>₹427.1 Cr</td><td class="g">+35.3%</td></tr>
<tr><td class="l">Profit after tax</td><td>₹13.2 Cr</td><td>₹9.2 Cr</td><td class="g">+43.9%</td></tr>
<tr><td class="l">Operating margin</td><td>4.88%</td><td>5.08%</td><td class="a">−20 bps</td></tr>
</table>
<div class="note">A strong quarter on both lines, and profit grew faster than sales despite margin slipping 20 basis points — the gain therefore came from below the operating line — interest or tax — not from operations. Limited review; a fuller read follows once the concall and investor presentation are out.</div>

<h2>The one thing to watch</h2>
<p>Everything in this business resolves to one number that Bharat Seats does not control: Maruti Suzuki's monthly production. Track that, and the premium-variant mix within it, and the rest of the model follows. A thin-margin, high-throughput supplier is a leveraged bet on its customer's volumes in both directions.</p>

<div class="foot">Figures from company filings via the fundamentals pipeline, as of 16 Aug 2026. Research only — not investment advice.</div>
</div>

"""


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


@router.get("/gvm/2pager/{symbol}", response_class=HTMLResponse)
def gvm_two_pager(symbol: str):
    """The 2-Pager. P1 ships the skeleton — route, 404 and the PDF-filename title; P2 ports the
    locked template from design_refs/scorr_gvm_2pager_R1.html and P3/P4 bind the data."""
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

    title = doc_title(sym)
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'><title>%s</title></head>"
        "<body style=\"font-family:Helvetica,Arial,sans-serif;padding:40px;color:#12161C\">"
        "<p style='color:#6B7683'>Quant note for <b>%s</b> — template lands in R6-P2.</p>"
        "</body></html>" % (title, sym)
    )

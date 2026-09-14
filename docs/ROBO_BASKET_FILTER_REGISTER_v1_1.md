# Robo Basket — Filter Register & Proposal v1.1 (condensed reference)

Founder-provided reference doc (14-Sep-2026), distilled for engineering use. Full source:
"Robo Basket — User Manual & Proposal, v1.1 (11-Aug-2026)", a DIFFERENT platform's basket-builder
tool used as a comprehensiveness benchmark for Scorr's own V12 Quant Basket Builder
(v12_endpoints.py, v12_backtest.py, quant_basket.html). This file is the register CC/Fable should
consult when extending V12 — do not re-paste the PDF into every card; link here instead.

## What Scorr V12 ALREADY HAS (checked against this register 14-Sep-2026, v12_endpoints.py _UNI_COLS)

price, market_cap, mcap_rank, gvm/g_score/v_score/m_score (Scorr-native composite, no Robo
equivalent), roe, roce, opm, de (Debt/Equity), pb (Price/Book), pe, dividend_yield,
interest_coverage, sales_growth_3y/5y, profit_growth_3y/5y, qoq_sales, qoq_profit, promoter
holding, fii_change (numeric), dii_change (numeric), w52_index, segments.
Entry: ROC lookback (1M/3M/6M/12M) OR a multi-lookback BLEND with weights (already ahead of the
Robo tool's "proposed" A4-item-5 multi-timeframe composite), rsi_gate, ema_gate, top_x,
min/max_stocks, manual_list.
Exit: trailing_peak_pct (%), rank_fall_y, weight_max_pct, weight_cushion, gate_mirror.
Rebalance: weekly/monthly/quarterly. Costs: txn_pct, slippage_pct.

Notably AHEAD of the Robo tool: fii_change/dii_change are numeric here (Robo's institutional-
holding filter is tick-only, Rise/Decline, no cutoff — listed as a known gap in their own doc).
ROE, D/E, P/B are live filters here (Robo's manual states these four are NOT built on their
screen at all, v1.0 listed them in error).

## KNOWN SCORR-SIDE GAP — matches the Robo doc's own #1 finding (their section A5)

V12's own preset (Alpha Multicap, v12_endpoints.py V12_PRESETS) carries this caveat verbatim:
"Survivorship-inflated absolutes; current-universe backtest; no costs/stops modeled." This is
EXACTLY the look-ahead/point-in-time bias the Robo doc's A5 calls "the single change that
corrects the measurement; everything else improves the model" — today's ratings/filters get
applied retroactively across the whole backtest period, so a stock's CURRENT quality decides
whether it appears in a PAST universe. Confirm scope and cost of a genuinely point-in-time/
dynamic universe backtest (using a historical GVM/rating/segment record by date, per the Robo
doc's own before/after table) — this is the highest-value single fix in the whole register.

## Phase-2-approved filters (Robo doc A6) NOT yet in V12 — 24 fields, already vetted by Robo's own
Head of Research, standard fundamentals, no new data vendor needed on their side

Valuation: P/E TTM (Scorr has pe already), P/E multiplier (current/5Y avg), PEG ratio,
Mcap/Sales, Price/Free-Cash-Flow.
Returns & Growth: Operating Profit Growth 1/3/5Y CAGR, QoQ Sales/Profit Growth (sequential —
Scorr's qoq_sales/qoq_profit may already cover this, confirm), Asset Growth 1/3/5Y CAGR.
Cash Flow: Operating Cash Flow/Sales (CFO margin), Net Cash Flow/Sales, Net Operating Cash Flow
(absolute, Rs Cr), Closing Cash Balance (absolute, Rs Cr).
Working Capital: Receivable Days, Payable Days, Inventory Days, Current Ratio, Quick Ratio.
Ownership: FII holding change QoQ (numeric — Scorr already has this), DII holding change QoQ
(numeric — Scorr already has this), Promoter Pledge %.
Risk: Beta vs Nifty (1Y).

## Full 150-filter candidate register (Robo doc A7) — DO NOT build all at once; standing backlog,
pull opportunistically, priority P1 first. Full detail lives in the source PDF (founder has it);
group headings + counts below for triage.

A. Valuation (18) — P/E, Forward P/E, sector-relative P/E, EV/EBITDA, EV/Sales, EV/EBIT,
   earnings yield, FCF yield, P/B vs own 5Y avg, CAPE, sector-relative P/B & P/S, EBIT/EBITDA
   yields, + the 5 already in Phase-2 above.
B. Profitability & Returns (13) — ROA, Net/Gross/EBITDA margin, margin trend, 3Y avg ROE, 5Y avg
   ROCE, incremental ROCE, cash conversion (CFO/EBITDA), ROIC, margin stability, FCF margin.
C. Growth (9) — EPS growth 1/3/5Y, EBITDA growth, growth-acceleration flag, revenue/share growth,
   book value growth, consecutive YoY profit-growth quarters, sales growth streak.
D. Balance Sheet/Solvency/Efficiency (14) — Net Debt/EBITDA, debt reduction YoY, net-cash flag,
   cash % of mcap, working capital days, CFO/PAT 3Y, contingent liabilities %, asset turnover,
   + the 5 already in Phase-2 above (D/E already live in V12).
E. Cash Flow (7) — CFO growth 3Y, FCF absolute, FCF-positive-years count, capex/sales, capex
   growth, dividend+buyback %FCF, R&D/sales.
F. Ownership & Flows (10) — promoter absolute level, promoter holding change, FII/DII level,
   mutual fund holding + change, public/free-float %, MF-schemes-holding count, retail
   shareholder count change.
G. Dividends (5) — payout ratio, consecutive paying years, 5Y CAGR, dividend-cut flag, 1Y growth.
H. Price Structure & Technicals (20) — RSI D/W/M screen + crossover event, price vs 20/50/100/200
   MA, golden/death cross, MACD state, distance from 52w low, new-52w-high flag, drawdown from
   ATH, ATR % of price, weekly ATR, Supertrend, ADX, beta 1Y, annualised vol 1Y, consecutive
   up-weeks, 1M/6M return screens.
I. Relative Strength/Momentum/Risk-Adjusted (11) — relative return vs Nifty/sector (difference-
   based), RS percentile (universe + sector), beat-Nifty consistency, multi-timeframe composite
   (Scorr already has the blend mechanism), momentum 12-1, RRG ratios, alpha vs Nifty, Sharpe,
   correlation vs Nifty.
J. Volume & Liquidity (9) — avg daily traded value floor, volume ratio vs 20D avg, delivery %
   windows, delivery-spike flag, free-float mcap, capacity gate (traded value vs order size),
   F&O availability flag, circuit-band filter, bid-ask spread. NONE of these exist in V12 today
   — real gap, matches the Robo doc's own A4-item-1 proposal.
K. Sector & Segment (6) — sector rating roll-up, sector return D/W/M/Q/Y (Scorr has sector_gvm as
   a quality proxy but not sector RETURN as a filter — gap), stock-vs-sector relative return,
   sector breadth, max-stocks-per-sector cap (construction rule), sector avg mcap floor.
L. Results/Events/Earnings Momentum (10) — days to/since next result, earnings surprise, result-
   quality flag, Finkhoz rating delta, rating-upgrade event, corporate-action pending flag, new-
   pledge event, earnings-upgrade/momentum score (needs consensus data).
M. Index Membership & Universe (4) — index membership flags (enables one-click Nifty
   50/100/200/500/Midcap150/Smallcap250 load, matches Robo's A3), inclusion/exclusion event,
   listing age, ASM/GSM exclusion.
N. Composite Quality/Forensic (6) — Piotroski F-Score, Altman Z-Score, earnings consistency,
   cash-flow accrual ratio, Beneish M-Score, Magic Formula rank.
O. Factor Model Scores (3) — single-factor (Value/Quality/Momentum/Growth), multi-factor
   composites, custom factor-model builder (user-weighted, saves as reusable screen).
P. Analyst Estimates (5) — needs a NEW consensus-estimates data feed (the one group in this
   whole register that needs new data, not just UI/compute work): buy-rating %, upside to
   target, forward P/B/P/S/PEG, EPS/sales growth next 12M consensus.

## Platform-level proposals (Robo doc A2/A3/A4/A8) beyond single filters

- A2 three-layer performance comparison: basket vs itself (standalone) / vs its own sector
  average / vs a chosen index benchmark — exposes a basket that "beat the market" only by
  riding a hot sector. V12 backtest currently reports vs Nifty only (need to confirm exact
  current behaviour before building this).
- A8 benchmark selection: Nifty 50/100/200/500/Midcap150/Smallcap250/Bank Nifty/sector indices,
  selectable per run, defaulting to match the universe (a smallcap basket defaults to Smallcap
  250) — prefer Total Return (TRI) series so dividends count.
- A3 one-click index-universe load: pick Nifty 500/100/etc as the WHOLE universe in one step
  (vs V12's current filter-assembly-only approach via mcap_rank) — makes runs standard/
  comparable across people.
- A4 item 4 ATR-based stop/target: ATR(14) weekly, stop/target as ATR multiples (fixed or
  trailing) — replaces a flat % stop, which is too tight for a volatile smallcap and too loose
  for a steady largecap. V12's exit.trailing_peak_pct today is exactly this flat-% stop.

## Source

Founder-uploaded PDF, "Robo Basket — User Manual & Proposal v1.1", 11-Aug-2026, supersedes v1.0.
Original has full per-filter detail (definitions, priority tags P1/P2/P3, notes) for all 150
register items — consult the founder's copy for anything this condensed version omits.

# T_Memory — V10 ST+EMA Trading System

Scorr (scorr.in) · Repo: github.com/AGQuant/quant_project · Live: quantproject-production.up.railway.app
Maintainer: Arpit (solo, self-funded). This card is the canonical memory for the V10 directional intraday strategy.

---

## CATEGORY-WISE

### 1. Strategy Spec [LOCKED]
- **Instrument:** NIFTY / BANKNIFTY directional intraday (on the UNDERLYING index).
- **Trigger:** Supertrend, ATR period **150**, multiplier **3.0**, on **10-min** candles.
- **Gate:** EMA **3 / 10** on **30-min** candles. EMA3 > EMA10 = buy zone; EMA3 < EMA10 = sell zone.
- **Entry:** ST flip whose direction matches the 30-min gate zone. (BUY = ST flips up in a buy zone; SELL = ST flips down in a sell zone.)
- **Exit:** SL **100** pts / Target **200** pts, **close-based** on the underlying — OR an opposite ST flip.
- **Constants:** ST_PERIOD=150, ST_MULT=3.0, EMA_FAST=3, EMA_SLOW=10, SL_PTS=100, TGT_PTS=200, TF_MAIN=10min, TF_GATE=30min.

### 2. Execution Layer — Option Writing
- Real-world execution is via **ATM option writing** (~₹60 round-trip brokerage), NOT futures (~₹1,000).
- All strategy math (ST, EMA, SL, target) stays on the **underlying index in points**. Option writing is only the execution wrapper.
- **BUY signal → write ATM PUT (PE).  SELL signal → write ATM CALL (CE).**  (Premium decay is the writer's gain.)
- Cheap ₹60 economics favour HIGHER frequency than the ₹1,000-futures backtest implied — to be re-optimised Thursday.

### 3. Sizing [LOCKED]
- NIFTY lot = **65**.  BANKNIFTY lot = **30**.  ~₹5 L capital per lot.

### 4. Backtest Reference (NIFTY Futures, 1yr, worst-case ₹1,000/trade)
- **+5,936 pts (~₹4.45 L/lot/yr), 49.3% win, PF 1.88, 150 trades, max DD −1,138 pts.** Short-side-heavy.
- Deliberate principle: always backtest under the HARSHEST cost so live can only surprise upward.
- BANKNIFTY params NOT yet optimised (~2.2× NIFTY vol) — currently runs NIFTY params on paper.

### 5. Two-Leg Paper Engine (live)
- `v10_st_ema.py`: resamples live 1m WS feed → appends closed 5m bars (NIFTY50→nifty_5m_test_data, BANKNIFTY→banknifty_5m_test_data); computes signal on last closed 10m bar + 30m gate; runs both legs.
- **FUT leg:** P&L = points × lot.
- **OPT leg:** writes ATM **monthly** option from `option_chain`; P&L = (entry_ltp − exit_ltp) × lot. ATM = strike nearest underlying at signal time; expiry = nearest monthly (currently 2026-06-25).
- Both legs open and close together on SL / target / flip.
- Runs every 5 min via scheduler, market hours only. Telegram alert on each new FUT entry (with the OPT write line appended).

### 6. Data
- `nifty_5m_test_data`: 18,463 rows, 2025-06-06 → 2026-06-05.
- `banknifty_5m_test_data`: 18,388 rows, 2025-06-09 → 2026-06-05.
- `option_chain`: NIFTY + BANKNIFTY **index options only** (NOT the 208 stocks). NIFTY 29 strikes, BANKNIFTY 38 strikes, CE+PE, ~3–4 day rolling, monthly expiry 2026-06-25. Cols: symbol, underlying, strike, option_type, expiry, ltp, oi, volume, bid, ask, ts. Fed by `fyers_options_feed.py`.
- CAVEAT: NIFTY spot (~23,980) sits at the TOP EDGE of the chain (max strike 23,900). "ATM" clips to nearest available; verify the feed re-centres daily.

### 7. DB Tables
- `v10_positions` (open): symbol, side, entry_price, entry_ts, stop, target, lot_size, status, leg (FUT/OPT), opt_strike, opt_type, opt_expiry. UNIQUE(symbol, leg, status).
- `v10_trades` (closed): symbol, side, entry/exit price+ts, exit_reason, points, lot_size, pnl, leg, opt_strike, opt_type.

### 8. Files & Endpoints
- `v10_st_ema.py` — strategy + two-leg engine. Reads: get_open_positions, get_closed_trades, get_summary (includes backtest reference block).
- `v10_endpoints.py` — `/api/v10`: GET /signal /positions /trades /summary; POST /append /tick (admin-token).
- `scheduler.py` — `_bg_v10_tick()` every 5 ticks alongside V8 signal writer.
- `main.py` v2.9.22 — v10_router wired + v10_signal / v10_tick MCP tools.
- `V10_ST_EMA.pine` — TradingView (option-writing mode, Finkhoz account, no Scorr branding).
- `V10_Dashboard.gs` — Apps Script dashboard: settings + live paper P&L by leg + backtest reference; open positions (FUT+OPT); closed trade log. Full timestamp on last refresh.

### 9. Alerts
- Telegram: env `V10_TELEGRAM_BOT_TOKEN` / `V10_TELEGRAM_CHAT_ID` (falls back to BOT_TOKEN / CHAT_ID). No alert sends until set.
- Month-3 plan: add Twilio WhatsApp alongside Telegram (additive).

### 10. Known Issues / Pending
- Dead helper `_exit_reason` inside `_paper_step` (references undefined `entry_fut`, never called, inert) — remove Thursday.
- Set Telegram env vars in Railway.
- Replace V10 `.gs` in Apps Script with the latest version.

---

## DAY-WISE

### 2026-06-07 (Sat)
- Locked V10 ST+EMA spec (150/3 on 10m + EMA 3/10 30m gate, SL100/T200, close-based).
- Built + deployed two-leg paper engine (FUT + ATM-monthly option writing) → main.py v2.9.22, healthy.
- Confirmed `option_chain` is live (NIFTY+BANKNIFTY index options, monthly 2026-06-25); wired OPT leg to real premiums.
- Extended v10_positions / v10_trades with leg / strike / type / expiry columns.
- Backfilled banknifty_5m_test_data (18,388 rows, full year).
- Built TradingView Pine (option-writing mode) and Google Sheet dashboard (two-leg, timestamp, backtest block).
- Decision: re-optimise at ₹60 option-writing cost (not ₹1,000) — deferred to Thursday for fuel.

### THURSDAY agenda
1. Re-backtest NIFTY at ₹60 cost (option-writing economics; likely higher-frequency optimum).
2. Backtest + optimise BANKNIFTY's own SL/target (~2.2× vol); lock its params.
3. Deploy the ₹60-optimised option-writing variant.
4. Remove dead `_exit_reason` from v10_st_ema.py.
5. Finalise option-writing alert mode; polish Telegram / Google Sheet.
6. WhatsApp alerts via Twilio (Month-3).

### Deferred (separate sessions)
- main.py refactor (break the ~79 KB monolith — high risk, own session).
- V9 pair-trade inverse "out of the box" strategy.

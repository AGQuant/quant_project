# cc#2186 — /m/qbbuilder rebuilt on the web Build-a-Basket flow (V12)

**Card:** cc#2186 P1 BUILD — the Basket Builder app page still ran the old momentum-rank spec; rebuild the four
steps on the V12 quant engine with the SAME endpoints and the SAME basket-definition object as the web
Build-a-Basket flow (cc#2132), keeping the founder's step shell.
**Files:** `mobile/qbbuilder.html` (rebuilt: shell kept, body script and step CSS new), this report.
**Route / nav:** unchanged — `/m/qbbuilder` (served by `mobile_scanners.py`, Home grid "QB Builder" tile,
NAV_REGISTRY + app_route_map rows already present). No main.py change.

## What the page does now

| Step | Controls | Endpoints (the web's own) |
|---|---|---|
| 1 Universe | pool chips (tap to combine) · ONE live count on top (`count of pool_count`, binding filter named, as-of date) · 7 category accordions, collapsed by default, each row = the Universe page's own filter (39 rows, catalogue copied verbatim from `scorr_qb_universe.html`) · per-applied-row ALONE / STEP counts · AND/OR join chip on the 2nd+ applied row · Load a saved universe · Clear all | `GET /api/qb/universe2/pools` · `/coverage` · `/segments` · `/preview?…&limit=12` (debounced 250 ms, same query string builder as the web page) · `/defs` · `/load` |
| 2 Entry | ROC lookback chips (1M/3M/6M/12M) · hold top X · min stocks · RSI gate toggle (tf, period, threshold, above/below) · EMA gate toggle (tf, ema1, ema2, ema3) | — (definition only) |
| 3 Exit & risk | hard stop % · trailing fall % · rank-fall · gate mirror · ATR stop (mult, target, trailing) · basket cap (`entry.max_stocks`) · sector cap % (`risk.sector_cap_pct`) · residual split stated as a note, exactly as the web states it | — |
| 4 Test & save | rebalance chips · benchmark chips · costs · basket name · the rule in words · the definition JSON (collapsible) · Run 5-year backtest · Save basket | `POST /api/v12/backtest` + poll `GET /api/v12/backtest/{id}` · `POST /api/v12/basket` (draft) then `POST /api/qb/universe2/save` under the basket name (the web flow's two persists) |
| Home | saved baskets (`GET /api/v12/basket`) with their rule line · presets (`GET /api/v12/presets`) · Build a new basket | |

`buildDefinition()` mirrors `scorr_v12.html`'s function line for line — same keys, same insertion order, same
parse defaults — and `loadDef()` is its inverse, so a basket saved by the web reopens on mobile as the identical
object. CAT 5 / CAT 6 rows read the coverage endpoint: before the 02:30 IST precompute (`qb_universe_derived`,
cc#2148) has run they are shown with "computes nightly" and are not filterable; after it they filter like every
other row. Contract tokens only (58 tokens used, none missing, no literal fallbacks; the one hex is the meta
theme-color the boot script overwrites).

## Parity evidence (harness `scratchpad/cc2186_test.py`, Chromium, mocks shaped from the endpoints' own code)

**Same inputs on both builders — Nifty 500 pool, 6M momentum, top 10 / min 5, RSI(14) above 55 D, EMA 20>50>200 D,
hard stop 10, trailing 15, rank-fall 12, gate mirror, ATR 2× target 4× trailing, cap 15, sector cap 25, quarterly,
NIFTY500, costs 0.2 / 0.15.**

mobile `window.qbb.def()`:
```
{"meta":{"builder":"v12"},"universe_ref":{"filters":{}},"entry":{"roc_lookback":"6M","top_x":10,"min_stocks":5,"max_stocks":15,"rsi_gate":{"tf":"D","period":14,"threshold":55,"dir":"above"},"ema_gate":{"tf":"D","ema1":20,"ema2":50,"ema3":200}},"exit":{"trailing_peak_pct":15,"rank_fall_y":12,"gate_mirror":true,"hard_stop_pct":10,"atr_stop":{"mult":2,"target_mult":4,"trailing":true}},"rebalance":{"freq":"quarterly"},"costs":{"txn_pct":0.2,"slippage_pct":0.15},"risk":{"sector_cap_pct":25}}
```
web `buildDefinition()` (scorr_v12.html, same inputs):
```
{"meta":{"builder":"v12"},"universe_ref":{"filters":{}},"entry":{"roc_lookback":"6M","top_x":10,"min_stocks":5,"max_stocks":15,"rsi_gate":{"tf":"D","period":14,"threshold":55,"dir":"above"},"ema_gate":{"tf":"D","ema1":20,"ema2":50,"ema3":200}},"exit":{"trailing_peak_pct":15,"rank_fall_y":12,"gate_mirror":true,"hard_stop_pct":10,"atr_stop":{"mult":2,"target_mult":4,"trailing":true}},"rebalance":{"freq":"quarterly"},"costs":{"txn_pct":0.2,"slippage_pct":0.15},"risk":{"sector_cap_pct":25}}
```
- byte-identical (string equality in the harness), and the `/api/v12/backtest` POST bodies recorded from both pages
  are byte-identical too (`{"definition":…,"benchmark":"NIFTY500"}`).
- `POST /api/v12/basket` body = `{name, definition, status:"draft"}` with exactly the definition shown on step 4;
  `POST /api/qb/universe2/save` = `{basket_name, def_name:"default", pools:["nifty500"], filters:[gvm ≥ 6 AND, roce ≥ 15 AND], as_of_score_date, preview_count:17}`.
- **Round trip:** a basket saved by the web (`universe_ref.filters {gvm ≥ 6, mcap_rank ≤ 500}`, RSI gate, trailing 15,
  hard stop 10, sector cap 25) reopened on mobile → `JSON.stringify(def())` equals the stored JSON byte for byte;
  step 1 states those V12 filters and keeps them in the definition. A preset loads into the four steps the same way.
- **Backtest result, same mocked result on both:** 14 of 14 figures equal on web and mobile — CAGR +18.4%, Absolute
  +132.6%, vs NIFTY500 +71.2%, Alpha +61.4%, vs Sector +58%, Sector alpha +74.6%, Beta 0.86, Max DD -23.8%,
  Sharpe 1.12, Calmar 0.77, XIRR +19.1%, Accuracy 58.3%, Trades 72, Win/loss 42 / 30; 5 yearly rows, 3 trade-log
  rows (same order, newest first), the equity + benchmark chart, the honesty line. Screenshots
  `cc2186_goldnight_step4_result.png` (mobile 375) and `cc2186_web_result_1280.png` (web).
- Step 1 evidence: 5 pool chips · count line "17 of 490 in the pool pass · binding: ROCE % cuts to 17 · as of 16 Sep" ·
  gvm row "alone 53 · step 53", roce row "alone 301 · step 17" with the AND chip · header "2 applied" · saved
  universe loaded ("Loaded Web momentum 10 / default.", count 53) · CAT 5/6 "computes nightly" before the first
  precompute, filterable after it (F&O pool, sales growth ≥ 10 → count from the preview).
- No page errors on either page; no horizontal overflow at 375 or 360 on any step, with a result, or with an open row.

## Screenshots looked at (375 × 812 dark goldnight + light aquawhite, 360 dark)

`cc2186_goldnight_step1.png` · `_step2` · `_step3` · `_step4_result` · `cc2186_aquawhite_step1..step4_result` ·
`cc2186_aquawhite_cat5_ready.png` · `cc2186_360_step1.png` · `cc2186_web_backtest_1280.png` / `cc2186_web_result_1280.png`.
Rendered: home 30 elements (Build row, 1 preset, 1 basket); step 1 five pool chips + seven accordions (39 rows);
step 4 hero + 15 tiles + chart + 5 yearly rows + 3 trade rows. Read as: step chips 1-4 with the current step in
brand; pool chips with their scored counts; the one big count; category cards with "N filters · n applied";
open rows with AT LEAST / AT MOST inputs and the alone/step counts; toggles with their summary line; the rule
in words; the result hero (+18.4% a year) with chips, tiles, sector line, chart, yearly split (green/red bars),
scrollable trade log, "Read this first". Light theme: same layout on white panels. Known nit: the "Win / loss"
tile value wraps to two lines at 375 (the shell's tile font, untouched).

## Where the card's wording and the web definition disagree (decision logged, cc#2186 log 7065)

- Scope items 2 and 3 name the cc#2126 / cc#2127 **toggles**. The web Build-a-Basket flow's definition object
  (scorr_v12.html `buildDefinition()`, validated by `_validate_basket_def`) does not carry them — cc#2129 wired only
  `exit.hard_stop_pct` and `risk.sector_cap_pct`; it still requires `entry.roc_lookback` + `top_x` and has
  `rebalance.freq`. Scope item 7 ("one JSON object shared with web — if they ever disagree on a field the card is
  not done") outranks items 2/3, so the mobile page carries exactly the web's fields. The verify item "no leftover
  momentum-window / hold-top / rebalance chips" is therefore met in the old-page sense (no old ids or code remain —
  checked) but the ROC lookback, top X and rebalance controls exist because the shared definition requires them.
  Bringing the cc#2126/2127 toggles into the definition is engine + web + mobile work — proposed as a follow-up card.
- Residual split (CASH / GOLDBEES / SILVERBEES, 100% validator): no definition field exists; shown as a note, exactly
  as the web states it (scorr_v12.html step 4), not as a control that would do nothing.
- Universe join gap (cc#2132 report): the V12 backtest still reads `universe_ref.filters`, not the saved
  `qb_universe_defs` row — mobile persists both, as the web does. Unchanged here.

## In-scope fixes made while building (stated, not silent)

- The "Loaded <name>" message after loading a saved universe was wiped by the redraw — now carried in state.
- Home count reads "1 saved basket" (singular).
- Paired fields on steps 2/3 sit side by side with their hint under each (the hint was landing in the right column).

## Verify (Fable)

Diff at the sha; `GET /api/v12/basket` rows created from mobile carry `meta.builder = 'v12'` and validate on
`POST /api/v12/backtest`; `qb_universe_defs` gets one row per saved basket name; `theme_validate` on the
deployed page.

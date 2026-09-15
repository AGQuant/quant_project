# cc#2086 (high) — V12 UNIVERSE: Robo Basket Phase-2 filters

Built under session-standing founder authorization (Fable unavailable, "clear the queue push all
to main, founder decision"). Reference: `docs/ROBO_BASKET_FILTER_REGISTER_v1_1.md`, section A6.

## Count discrepancy — the card's own spec already flagged this, confirmed and not forced

The card title says 24 filters; the register doc's own A6 table is numbered 1-23; the doc prose
(condensed further) lists 21 distinct lines, three of which bundle multiple sub-metrics (Op.
Profit Growth 1/3/5Y, QoQ Sales/Profit Growth, Asset Growth 1/3/5Y). Per the card's own
instruction ("treat the TABLE as authoritative... report the actual count worked through, rather
than forcing a 24th item to exist"), this report works through all 21 doc lines and reports each
on its own merits — no filter was invented to hit a round number.

## Disposition — all 21 lines

**Already had (5)** — confirmed match, no action:
- P/E TTM → existing `pe`
- QoQ Sales Growth → existing `qoq_sales` (confirmed: `screener_raw.qoq_sales_growth` is genuinely
  sequential quarter-over-quarter, matching the doc's definition, not a same-quarter YoY figure)
- QoQ Profit Growth → existing `qoq_profit` (same confirmation)
- FII holding change QoQ → existing `fii_change`
- DII holding change QoQ → existing `dii_change`

**Added (4)** — new `_UNI_COLS` keys, direct EOD columns/joins, verified against real production
data (see Verify below):
| Key | Expression | Population (of 1881 screener_raw rows, or 1793 gvm universe for beta) |
|---|---|---|
| `pe_multiplier` | `s.pe / NULLIF(s.historical_pe, 0)` — current PE ÷ 5Y-avg PE | 1729 (92%) |
| `mcap_sales` | `s.market_cap / NULLIF(s."Sales", 0)` | 1877 (99.8%) |
| `promoter_pledge` | `s."Promoter holding" - s."Unpledged promoter holding"` | 1852 (98.5%); 409 stocks show actual pledge > 0, the rest 0 — a real, non-trivial distribution, not a constant |
| `beta_1y` | `bt.beta`, new `LEFT JOIN LATERAL` against `beta_daily` (cc#2032, this session), latest date per symbol, 252-session window vs NIFTY50 | 1795 of 1793 gvm symbols have a beta row (effectively full coverage) |

**Dropped before shipping — column exists, but is 100% empty in production (2)**. Checked across
all 1881 `screener_raw` rows, not assumed from a handful of large-cap samples (which would have
looked identical either way): `"PEG Ratio"` and `"Operating profit growth"` both return `COUNT() =
0` non-null rows, platform-wide. A filter on either would silently return an empty universe every
time it's touched — worse than not shipping it, since it looks like a working field. Not added to
`_UNI_COLS`, not added to the UI. This is the same "blocked on missing data" bucket the card's
scope asks for, just discovered at the data layer rather than the schema layer.

**Deferred — data exists in `fundamentals_history`, needs a dedicated derived-field build (7
items, own follow-up, not this card)**. `fundamentals_history` (jsonb `metrics`, per
symbol/section/period, confirmed populated: balance-sheet, cash-flow, profit-loss, ratios,
quarters, shareholding sections all have real rows back to ~2015) has the underlying data for
these, but every one needs multi-period selection (latest annual, `consolidated=true`,
`period_end IS NOT NULL` to skip the null-dated TTM row), text-to-numeric cleanup (values are
comma-formatted strings, e.g. `"113,506"`), and in three cases a CAGR across two period rows — a
materially bigger and riskier lift than `_UNI_COLS`'s flat `key -> column` pattern, and this
card's own `_UNI_BASE` is on the hot path of every V12 preview/backtest call. Rushing a first pass
risked exactly the kind of silent-wrong-number this platform cannot ship. Source keys confirmed
present, so a follow-up card can execute directly without re-doing this reconnaissance:
- Price/Free-Cash-Flow — `cash-flow."Free Cash Flow"` ÷ price or market_cap
- Operating Profit Growth 3Y/5Y CAGR — `profit-loss."Operating Profit"`, multi-year
- Asset Growth 1/3/5Y CAGR — `balance-sheet."Total Assets"`, multi-year
- Operating Cash Flow/Sales — `cash-flow."Cash from Operating Activity"` ÷ `screener_raw."Sales"`
- Net Cash Flow/Sales — `cash-flow."Net Cash Flow"` ÷ `screener_raw."Sales"`
- Net Operating Cash Flow (absolute) — `cash-flow."Cash from Operating Activity"` directly (single
  latest-period extraction, no ratio — the simplest of this group)
- Receivable/Payable/Inventory Days — `ratios."Debtor Days"` / `"Days Payable"` / `"Inventory
  Days"` directly (single latest-period extraction each; will be NULL for banks/NBFCs, correctly
  so — confirmed the `ratios` section schema differs by company type)

**Genuinely blocked — no column anywhere, checked not assumed (3)**: Current Ratio, Quick Ratio,
Closing Cash Balance. Checked the `balance-sheet` section for a bank (HDFCBANK) and two industrials
(RELIANCE, WELCORP) — Screener's condensed scrape carries only `Total Assets` / `Total Liabilities`
plus a few named lines (`Reserves`, `Borrowings`, `Fixed Assets`, `CWIP`, `Investments`, `Other
Assets`, `Other Liabilities`); there is no current-assets/current-liabilities/cash breakdown for
any company type sampled. Not derivable from what this platform scrapes today.

## What changed

- **`v12_endpoints.py`**: `_UNI_COLS` gains 4 keys (`pe_multiplier`, `mcap_sales`,
  `promoter_pledge`, `beta_1y`). `_UNI_BASE` gains one `LEFT JOIN LATERAL` against `beta_daily`
  (latest row per symbol by `d DESC`), aliased `bt`. `_uni_where()` and every existing key are
  untouched — the new keys ride the exact same generic min/max machinery as every prior field, no
  new code path.
- **`scorr_v12.html`**: Step 1 Universe panel gains 4 new `.fld` min/max range inputs (P/E
  multiplier, Mcap/Sales, Promoter pledge %, Beta vs Nifty 1y), same visual style as the existing
  GVM/Mcap-rank fields. `buildFilters()` reads all 4 into the filter object the same way as every
  existing field (`rng()` helper, unchanged).

## What did NOT change

Every existing `_UNI_COLS` key, `_UNI_LEVERAGE_KEYS` (BFSI exclusion still triggers only on
`de`/`int_cov`, untouched), `_uni_where()`'s logic, `v12_universe_save`, `v12_universe_preview`'s
response shape, `v12_backtest.py` (still resolves every non-GVM `_UNI_COLS` key, new ones included,
against today's snapshot only — consistent with the already-documented, already-accepted
limitation cc#2085 found and left in place for every filter except the 4 GVM component keys).
`scorr_v12.html`'s `applyPreset()` was already not wired for half the existing Step 1 fields (roe,
roce, de, profit_growth_3y, promoter all missing from the preset-load path before this card) — a
pre-existing gap, not touched here, not expanded by adding 4 more fields with the same status quo.

## EOD-basis compliance (14-Sep founder ruling, cited in the card spec)

All 4 added fields source from EOD tables only: `screener_raw`/`input_raw` (daily-refreshed
fundamentals snapshot) and `beta_daily` (01:20 IST EOD job, cc#2032). None touch
`intraday_prices`/`cmp_prices`/`v10_st_ema` or any live/intraday source.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `v12_endpoints.py`; `node --check` clean on
`scorr_v12.html`'s inline script.

**Real module, real SQL, real data — not simulated.** Imported the actual edited
`v12_endpoints` module directly and called its real `_UNI_COLS`/`_UNI_BASE`/`_uni_where()`, not a
re-typed copy. Confirmed `pe_multiplier`/`mcap_sales`/`promoter_pledge`/`beta_1y` present with the
expected expressions, confirmed `peg_ratio`/`opm_growth_1y` are absent. Built a real filter
(`beta_1y` 0.9-1.1, `pe_multiplier` max 1.0, `mcap_sales` max 5, `promoter_pledge` min 0.01) through
`_uni_where()`, then ran the EXACT generated SQL against production: **22 stocks match**, sample
rows show every computed value internally consistent (`pe/historical_pe` = `pe_mult` exactly,
`market_cap/Sales` = `mcap_sales` exactly, `promoter - unpledged` = `pledge` exactly, every
returned `beta` inside [0.9, 1.1] as filtered) — e.g. VINCOFE gvm=8.57, pe_mult=0.644,
mcap_sales=4.313, pledge=8.99, beta=1.0585.

**Regression check — the LATERAL join adds zero rows, drops zero rows.** `_UNI_BASE` with no
filters: `1793` (matches `gvm_scores` latest-date count exactly, both before and after adding the
`beta_daily` LATERAL join) — proves the new join neither duplicates a symbol with multiple beta
rows nor silently drops one lacking a beta row.

**Null handling, proven not asserted**: a symbol with no `beta_daily` row gets `bt.beta = NULL`
via the LEFT JOIN LATERAL; `NULL >= x AND NULL <= y` is `NULL` (falsy) in SQL, so it is correctly
excluded from a `beta_1y` range filter — same behaviour as every other nullable `_UNI_COLS` field,
no special-case code needed or added.

**Population rates, checked platform-wide, not sampled**: ran `COUNT()` across all 1881
`screener_raw` rows for every candidate field before deciding disposition — this is what caught
`PEG Ratio`/`Operating profit growth` at 0 populated rows despite 5 large-cap symbols (RELIANCE,
TCS, HDFCBANK, WELCORP, HAL) all individually showing NULL, which alone wouldn't have distinguished
"these 5 happen to lack it" from "this column is dead platform-wide."

**Live check**: `/api/v12/universe/preview` needs no restart/trigger — filters are read
synchronously on every call, live as soon as this deploys. Arpit can confirm on `/v12` directly;
CC's container has no egress path to scorr.in to self-check the rendered page (established
constraint this session).

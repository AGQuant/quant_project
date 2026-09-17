# cc#2133 — Screeners table: WEEK% and MONTH% beside DAY%

## How DAY% is sourced (task 1) and what WEEK%/MONTH% reuse
`screener_detail` overlays DAY% through `cmp_resolver.resolve_cmp_many` (cc#811): `day_pct` =
CMP vs the last `raw_prices` close strictly before today. WEEK%/MONTH% now come from the **same
price source** — `raw_prices` closes for the screen's own symbols (`symbol` = the screen symbol,
uppercase) — in one query per request over that small row set. No precompute, no new table, no
scheduled job (that was cc#2134's shape, not this one's); `v8_metrics.week_return/month_return`
is deliberately NOT used — it covers the ~209 F&O names only, and a screen draws from the full
universe.

## The calculation (task 2), anchored like `invest_check_v2._ret_over`
Per symbol: `d0` = its own last traded date in raw_prices (not today); `c0` = that close;
`cw` = the last close **on or before d0 − 7 calendar days**, `cm` = … **d0 − 30 calendar days**;
`week_pct = (c0/cw − 1)×100`, `month_pct = (c0/cm − 1)×100`, 2 dp from SQL, rendered at one
decimal by the page's growth formatter. A symbol without a close that far back gets `null` →
`--` on the page (task 4), never a number for missing history. Anchoring on the symbol's own
last date is visible in real data: ACCENTMIC's last raw_prices close is 15-Sep, so its week runs
08-Sep → 15-Sep while SPECTRUM/DIACABS (last 16-Sep) run 09-Sep → 16-Sep — no silently longer
window. The overlay is wrapped like the CMP overlay: a failure logs, rolls back, and the page
still renders.

Real values on the live screen 13 (52-Week Breakout, 3 members this morning): ACCENTMIC
+6.78% / +17.76%, SPECTRUM +12.48% / +45.11%, DIACABS +17.91% / +9.68% — the endpoint's own SQL
run on production.

## Page (task 3)
`scorr_screeners.html`: getters `WEEK` / `MONTH` (numbers, null when absent), default sort
descending like every numeric column, header order now **Sr · Symbol · CMP · Day% · Week% ·
Month% · GVM · Inv Score · <screen fields> · Added On**, cells through the shared `fmtGrowth` +
`cls` (green/red/muted), so they sort, colour and format exactly like DAY%. Payload also carries
`ret_as_of` per row (the anchor date) for anyone who needs it; not rendered.

## Verify
`ast`/`py_compile` clean; `node --check` clean; 0 new literal fallbacks. Playwright on the real
page with the **real screen-13 payload** (the endpoint's own join, captured from production, one
row's month blanked in the harness copy — DIACABS, real +9.68 — to exercise `--`): header order
as above, cells `+6.8% / +17.8%`, `+12.5% / +45.1%`, `+17.9% / --`, positive cells `.pos`, the
missing one `.zero`, Week% sort desc = DIACABS > SPECTRUM > ACCENTMIC, Month% sort desc puts the
`--` row last, meta line names the sort, 375px no page overflow. Screenshot looked at
(VISUAL_VERIFY_GATE_V1).

## Mobile (out of scope, answered)
`/m/screeners` (`screeners_app_mobile.py`) runs its own query against `v13_screen_results` and
does not call `screener_detail`, so it does **not** get these columns for free; it would need its
own pass (a copy of the same SQL block) — noted, not done.

## Not touched
DAY%, GVM, MO IDX, VOL X21, 52W IDX and their computation; `v8_metrics`; mobile Screeners.

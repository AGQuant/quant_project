# cc#2151 — READ-ONLY diagnostic: the 16-Sep screener reload dropped 23 GVM-universe symbols

Date: 17-Sep-2026. No writes: every number below is from SELECTs on production. Nothing was
reloaded, edited or fixed.

## 1. The 23 symbols

`gvm_history` 2026-09-15 had 1,793 rows; 2026-09-16 has 1,773. The delta reconciles exactly:
**23 dropped, 3 added** (PRIMO, SHIVALIK, MEGATHERM): 1,793 − 23 + 3 = 1,773.

All 23 are in `input_raw`, none has a row in `screener_raw` (the 16-Sep load), none has a
`universe_technicals` row for 17-Sep (technicals follow the scored universe), and all 23 still trade
(10–11 `raw_prices` sessions in September each). Last GVM score_date for all: 2026-09-15.

| Symbol | Company | Cap band (June rank) | Last bar | June mcap (Cr) | Est. mcap now (Cr) |
|---|---|---|---|---|---|
| APS | Australian Premium Solar (India) | micro 1654 | 15-Sep | 658 | 437 |
| AQYLON | Aqylon Nexus | micro 1284 | 16-Sep | 1,512 | 501 |
| CHANDAN | Chandan Healthcare | micro 1714 | 16-Sep | 567 | — (no June bar) |
| CSLFINANCE | CSL Finance | micro 1750 | 16-Sep | 519 | 480 |
| GPECO | GP Eco Solutions India | micro 1743 | 15-Sep | 530 | 504 |
| INDOTHAI | Indo Thai Securities | micro 1080 | 16-Sep | 2,497 | 367 |
| INTLCONV | International Conveyors | micro 1753 | 16-Sep | 515 | 503 |
| IRIS | IRIS Business Services | micro 1734 | 16-Sep | 540 | 502 |
| IWARE | Iware Supplychain | — | 16-Sep | 611 (Jul) | 464 |
| KAYA | Kaya | — | 16-Sep | — | — |
| KHAICHEM | Khaitan Chemicals & Fertilizers | micro 1742 | 16-Sep | 530 | 483 |
| KNAGRI | KN Agri Resources | — | 16-Sep | — | — |
| MINDTECK | Mindteck (India) | micro 1630 | 16-Sep | 687 | 530 |
| MUNJALSHOW | Munjal Showa | micro 1762 | 16-Sep | 509 | 461 |
| OSWALGREEN | Oswal Green Tech | micro 1674 | 16-Sep | 619 | 471 |
| PENINLAND | Peninsula Land | micro 1735 | 16-Sep | 539 | 468 |
| PLASTIBLEN | Plastiblends India | — | 16-Sep | — | — |
| RAMANEWS | Shree Rama Newsprint | — | 16-Sep | — | — |
| SJLOGISTIC | S J Logistics (India) | micro 1758 | 15-Sep | 511 | 508 |
| SUKHJITS | Sukhjit Starch & Chemicals | micro 1727 | 16-Sep | 548 | 465 |
| VISACHROME | VISA Chrome | — | 16-Sep | 578 (Jul) | 464 |
| VTMLTD | VTM | — | 16-Sep | — | — (no June bar) |
| ZEEMEDIA | Zee Media Corporation | micro 1757 | 16-Sep | 512 | 480 |

"Est. mcap now" = the June `input_raw` market cap scaled by the price move since the June load
(`raw_prices` close then vs the latest close). It uses June share counts, so ±5% is noise.

## 2. Cause, per class

**The export is a market-cap screen with a Rs.500 Cr floor.** In the 16-Sep load the smallest
market cap is 501.69 Cr, zero rows sit below 500, and 30 rows sit between 500 and 550. The three
names that ENTERED on 16-Sep came in just above it (MEGATHERM 504.92, PRIMO 518.88, SHIVALIK 612.41).

| Class | Count | Symbols |
|---|---|---|
| (a) delisted / renamed | **0** | none — every symbol still prints daily bars; no `screener_raw` row shares their BSE code or company name under another NSE code |
| (b) in the CSV with a blank / odd NSE code | **0 shown, cannot be fully excluded** | the CSV itself is not stored (the loader drops those rows before insert; the Drive copy was not found from this seat), so the 7 without a June market cap (CHANDAN, IWARE*, KAYA, KNAGRI, PLASTIBLEN, RAMANEWS, VTMLTD) are unmeasured on the floor test — all are micro caps in the same band |
| (c) missing from the export — **fell below the Rs.500 Cr floor** | **16 measured** (+ 7 consistent) | every measured name estimates at 367–530 Cr today; 14 of 16 are under 505 |

*IWARE has a July market cap (611) and estimates at 464 now.

Micro caps sitting on the floor will keep falling out and back in on every weekly export; the
16-Sep drop is the first time it removed a whole band at once because the market fell into the
floor (the median estimate lost ~10% since June).

**Mechanism inside the platform:** `gvm_nightly._load_merged_df` INNER JOINs `input_raw` to
`screener_raw` on `nse_code`, so a name absent from the new export is silently unscored that
night; `universe_technicals`, pools, screeners and baskets inherit the shrink.

## 3. Did anything fire?

| Guard | What it watches | 16-Sep | Verdict |
|---|---|---|---|
| cc#828 `SCREENER_LOAD_COLUMN_DROP` | columns that were >90% populated and are missing from the file | fired 09:53:58 — for `id` and `loaded_at` | **false alarm**: those are table columns, never CSV headers; it has said the same thing on every load since 12-Aug (8 of 8), so the alert is noise. It knows nothing about rows. |
| `GVM_METRIC_COVERAGE` (cc#1095) | per-metric coverage % of the scored universe | 20:00:37 "clean: true", universe 1773 | measures the share of a universe that had already shrunk; blind to the size |
| `universe_shrink` (task #35) | `raw_prices` symbol count day over day | fired twice on 16-Sep — same ~45 SME / InvIT names it has flagged every day since 10-Sep | watches the price feed, not scoring; daily noise on names that never had a bar |
| loader response `rows_loaded` | 1,881 → 1,865 | logged, compared with nothing | nobody reads it |

**Finding:** the scored universe lost 20 names and no alert said so. Two of the three guards that
exist fire every day on something else, which is worse than silence.

## 4. PROPOSAL (not executed)

**Fix per class**
- (c) floor-edge names — founder's call, two options: (i) widen the export screen to a **Rs.400 Cr**
  floor so the platform's own 500-Cr micro-cap edge stops being the export's edge (a hysteresis
  band); or (ii) keep the export as is and make the loader **keep the previous row for a symbol
  absent from the new file**, stamped `stale_since`, for at most two loads, then drop it with an
  alert. (ii) keeps scoring on stale fundamentals for up to two weeks and must show as stale on every
  surface; (i) is cleaner. Recommendation: (i).
- (a) none to retire. (b) if the CSV shows blank NSE codes for any of the 7 unmeasured names, an alias
  map (`nse_code` from `input_raw` by BSE code) in the loader; decide after one look at the file.

**Guard (new card):** a symbol-count delta guard on the loader that writes an ops_log alert like
the cc#828 guard, `SCREENER_LOAD_UNIVERSE_DELTA`: after each load, `rows_loaded` vs the prior load,
AND the GVM-universe coverage (`input_raw ∩ screener_raw` before vs after), listing every dropped
symbol by name when the drop exceeds 5 names or 0.5%. Plus, after gvm nightly, `GVM_UNIVERSE_DELTA`
comparing the scored count with the previous score_date in `gvm_history` and naming the symbols —
this second one catches the shrink whatever its cause. Same card: exclude `id` and `loaded_at` from
the cc#828 comparison so that alert means something again.

## Seen while reading — separate card, P1

`tc_scanner_score_daily` (cc#2126) has failed on every chained nightly run since it landed:
`cannot import name '_ema' from 'v8_pivot_star'` (scheduler_master last_status = error,
16-Sep 20:00:38). `qb_entry_rules.py` line 56 imports `_ema` from `v8_pivot_star`, which has never
defined it (git history of that file has no `_ema`; the helper lives in `v12_backtest._ema` and
`v10_st_ema._ema`). So the module — and `qb_exit_rules`, which imports from it — cannot be imported
in production, and the table is frozen at its manual first run (828 rows, 2026-09-16). cc#2150's
screen path now reads that table, so until this is fixed `tc_asof` stays at 2026-09-16.

# cc#2184 — /m/mf: compact fund cards, top 10 then View more, category average beside every fund

Built 17-Sep-2026 (founder review 14:40 IST); the landing time is in the task row. Depends on cc#2180 — landed 12:39 IST (c15e15b), the route is green.

## What shipped

| File | Change |
|---|---|
| `mf_app_mobile.py` | Additive. `_cat_avgs()` computes, server-side, the mean 1y / 3y return and expense ratio per category over the scored, direct-growth funds (mf_master joined to mf_scores — the population the screener lists), keyed by the category the row displays (`_derive_cat`, the screener's own mapping). Every `/api/mobile/mf_app/list` row gains `cat_avg_1y`, `cat_avg_3y`, `cat_avg_er`, `cat_n_1y`; the payload gains `cat_avgs`, `cat_avg_basis` and `rating_basis`. A DB failure leaves the averages empty and the list intact. |
| `mobile/mf.html` | The list is rebuilt as `mf-` prefixed cards (the old `.row/.v/.n/.c` names are also mobile_app.css bare selectors — they boxed the value cell and stretched every row, the founder's "tall, half-empty" picture). Card = name (two-line clamp) + category chip, a 56×44 score tile on the right (gold outline at 65+), one metrics row 1y · Cat avg 1y · ER · 3y, and a foot line with AUM and "cat avg on N funds". Plain `--panel` fill, 1 px `--edge` hairlines between cards, no side bar. LIST RULE: the top 10 by the current sort show first; "View more · N more" (44 px) reveals the next 10 per tap (expand, not paging); the hint reads "Top 10 of 24 by score". Category chips, their order, the search box and the Score / 1y / Size sort are untouched. |
| `tests/test_mf_cat_avgs.py` | 2 tests: means and counts per category (a fund without a figure counts in n, not in the mean; a NULL category derives from the name), and a DB failure returns `{}`. |
| `reports/CC2184_mf_cards.md` | This report. |

CC's two extra fields (spec item 2): **3y return** (already in the payload; "young" when the fund has no 3y history) and **AUM** — the two things a reader asks next after the score and the 1y.

**Rating (spec item 2): none exists to show.** `crisil_rank` and `finkhoz_rating` are NULL for every one of the 509 scored direct-growth funds (checked 17-Sep), and the scoring engine defines the score only. The card shows the score out of 100 and nothing invented; `rating_basis` in the payload says so.

## Category averages on production (spec verify item 3), run_sql 17-Sep

Mean of `mf_master.ret_1y` over scored direct-growth funds, grouped by category — the same join `_cat_avgs()` runs:

| Category | funds | with 1y | avg 1y | avg 3y | avg ER |
|---|---|---|---|---|---|
| Sectoral/Thematic | 185 | 166 | +5.13% | +16.63% | 1.12% |
| Flexi Cap | 45 | 41 | +2.31% | +14.14% | 1.09% |
| Large Cap | 37 | 35 | +0.35% | +11.69% | 1.12% |
| ELSS | 37 | 37 | +0.55% | +13.55% | 1.09% |
| Small Cap | 36 | 32 | +7.49% | +18.36% | 0.87% |
| Mid Cap | 36 | 35 | +6.24% | +19.43% | 0.94% |

Large Cap is the spot check: the page shows "Cat avg 1y +0.3%" (0.35 at one decimal) with "cat avg on 35 funds" — the run_sql mean above.

## Harness (Playwright, real Chromium, `scratchpad/cc2184_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture = the production top 12 by score (All) plus the top 12 Large Cap rows, in the endpoint's shape, with the category means above.

- All: exactly 10 cards, "View more · 14 more", hint "Top 10 of 24 by score"; first card HDFC Defence Fund · SECTORAL · score tile "73 / score" 56×44 px; metrics 1y +19.5% · Cat avg 1y +5.1% · ER 0.87% · 3y +41.4%; foot "₹10.5k Cr AUM · cat avg on 166 funds"; cards 116–123 px tall; a fund with no 3y reads "young".
- Chips unchanged: All + the 11 categories in whitelist order; no sideways overflow; no `::before` side bar; 1 px hairlines; the list fill is `--panel` (white on Aqua White).
- View more → 20 (4 left) → 24, button gone, hint "Top 24 of 24 by score".
- Large Cap chip → 10 of 12 with "View more · 2 more"; Cat avg 1y "+0.3%" on 35 funds; View more → all 12; sort by 1y resets to the top 10 ("Top 10 of 12 by 1y return").
- No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2184_*.png`): `375_dark_all` — search box, the chip rail (All lit), "ALL CATEGORIES" with Score/1y/Size, "Top 10 of 24 by score", then the cards: HDFC Defence Fund with the gold 73 tile and the four metrics in one row, Quant Value Fund 73, HDFC Pharma 71 with "young" under 3y, Kotak MNC 71. `375_light_large` — the Large Cap list on Aqua White after View more: white panel, hairlines, red 1y on the losers, ER 1.36% in red where it is above the category average, the note card below.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/api/mobile/mf_app/list?category=Large%20Cap%20Fund` → every row carries `cat_avg_1y` ≈ 0.35 and `cat_n_1y` 35; `cat_avgs` has 11 categories.
2. `https://scorr.in/m/mf` on the phone: 10 compact cards, View more, the chips as before; the fund detail page is unchanged.

## Out of scope

The fund detail view (`renderFund`) still uses the old `.row/.lr/.strip` names; the card asked for the list. Scored-only means: an unscored fund is not in any average.

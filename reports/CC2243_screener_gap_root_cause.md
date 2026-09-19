# cc#2243 — READ-ONLY diagnostic: 3 Nifty 500 names missing from screener_raw, root cause + wider-gap check

Date: 19-Sep-2026. No writes: every number below is from SELECTs on production plus one direct read of
the source export file on Drive. Nothing was reloaded, edited, or inserted. No substitute symbols were
added to `nifty500_universe`.

## 1. Root cause for JBCHEPHARM / GUJGASLTD / GSPL: absent from the source CSV, not a loader bug

**Source CSV checked directly.** The founder's "Screener csv" Drive folder (`1niqYSbFInWp0Y-Vj0-tXpKCTGjW-kj-U`)
holds one current export: `query-results.csv` (Drive id `1TWXTHKE_AHioZ2zz8UUfATl9FTPFlHik`, created
29-Aug-2026, last modified 08-Sep-2026, 43 columns matching `gvm_nightly.SCREENER_COLUMNS`, 2,034 data
rows). Read in full and searched for all three names by NSE code, BSE-style company name, and substring
("JBCHEPHARM", "GUJGASLTD", "GSPL", "JB Chem", "Gujarat Gas", "Gujarat State Petronet"): **zero matches,
anywhere in the file.**

This is not a class-wide sector exclusion — other "Gujarat "-prefixed names (GUJALKALI, GUJENERGY — a
gas/LPG peer, FLUOROCHEM, GKSL) and large caps (RELIANCE, TCS) are present and correctly formatted in the
same file. The three names are simply not rows in the export screener.in produced.

**Ruled out: a loader bug.** `gvm_nightly._sql_clean_replace_screener_v2` (the real loader behind
`POST /api/admin/load_screener_from_drive`) only drops rows two ways: a null/blank `NSE Code`
(`dropped_no_nse`) and a duplicate `NSE Code` keeping the first occurrence (`dropped_dupe`). Neither
applies here — the file has 1,881 distinct, non-blank NSE codes and none of the three names appears under
any code at all, so there was never a row for the loader to drop.

**Verdict, per the card's own framework: (c) missing from the export — a founder action (re-export /
check the screener.in screen definition), not a code fix.** `input_raw` still carries all three with
sensible large/mid-cap market caps (JBCHEPHARM Rs.34,186.99 Cr, GUJGASLTD Rs.27,325.60 Cr, GSPL
Rs.15,140.61 Cr — unchanged since the January 2026 load) and deep `raw_prices` history (live, traded
names, not delisted). **This is not the same mechanism as cc#2151's 23-name drop** — that was a Rs.500 Cr
market-cap floor on the export catching micro-caps at the edge; these three sit 30–68x above that floor,
so a market-cap floor does not explain their absence. Whatever screener.in-side screen criterion is
excluding them is outside this codebase and could not be determined further from this seat (the one
supporting doc found, `New Screener Filter Rules For APP.pdf`, is a scanned/image PDF this tool could not
extract text from). Per the card's own instruction, this stops here rather than being worked around —
no row was fabricated or substituted into `screener_raw`.

## 2. Wider-gap check (item 5): the gap is much wider than three names

Nifty 500 specifically: exactly these 3 of 500 are missing from `screener_raw` (`SELECT symbol FROM
nifty500_universe WHERE symbol NOT IN (SELECT nse_code FROM screener_raw)` → GSPL, GUJGASLTD, JBCHEPHARM,
count 3). No other Nifty 500 constituent has this problem.

Beyond Nifty 500, against the full tracked universe: **235 of `input_raw`'s 2,008 symbols (11.7%) have no
row in `screener_raw` at all.**

| Market cap band (`input_raw`) | Count | Read |
|---|---|---|
| No market cap on file (NULL) | 179 | Likely stale/inactive names `input_raw` never refreshed — consistent with names that dropped out of active tracking, not a screener.in export problem specifically. |
| Rs.500–700 Cr | 45 | Consistent with cc#2151's already-diagnosed Rs.500 Cr floor-edge mechanism (proposal not yet executed — see that report's Section 4). |
| **Above Rs.700 Cr** | **11** | **Same unexplained class as JBCHEPHARM/GUJGASLTD/GSPL** — real, actively-traded, comfortably-above-the-floor names absent from the export for a reason this session could not determine. List: JBCHEPHARM (34,186.99), GUJGASLTD (27,325.60), GSPL (15,140.61), CIGNITITEC (3,471.74), ASHIKA (2,692.12), INDOTHAI (2,497.39), NATIONSTD (2,480), MIRCELECTR (1,530.59), AQYLON (1,512.48), AMIRCHAND (1,262.71), MCLEODRUSS (786.76). |
| **Total** | **235** | |

**Read:** the founder's Nifty 500 question surfaced 3 names, but they are the visible tip of an
11-name-and-growing class (comfortably-above-floor names absent from the export, cause undetermined) sitting
inside a much larger 235-name gap that is mostly explained by two already-understood mechanisms (stale
`input_raw` rows with no current market cap, and the Rs.500 Cr floor edge from cc#2151). The 11-name class
is the one worth a founder look on screener.in's own screen/query configuration — the other 224 are not new
information, they are the untouched tail of a mechanism already on record.

## 3. Not done in this card (by the card's own scope + do_not_touch)

- **No restore attempted.** Items 1–3 required either a loader-code fix (ruled out — no such bug exists)
  or a founder re-export (outside this codebase). Per the card: "the card should stop there rather than
  working around it."
- **No substitute symbols added anywhere** — `nifty500_universe` is untouched, exactly as instructed.
- **No backfill, no universe rebuild** — out of scope, not attempted.
- Item 4 (honest counts, "never a hardcoded 500"): nothing needed changing — this card made no UI/count
  changes. One process note: this card's own spec cross-references "cc#2242 item 2" for a "Nifty 500
  toggle... 497 scored / 496 filed" honest-count precedent. **cc#2242, as actually built this session, is
  the `/m/results` page cleanup (hero trim, Written up removal, Profit jumps/falls cards) — it does not
  contain a Nifty 500 toggle or a 497/496 count anywhere.** Flagged in `cc_task_logs` rather than silently
  treated as correct; does not change this card's own findings above.

## Recommendation

1. **Founder-side:** check the screener.in saved screen/query behind `query-results.csv` for a criterion
   (beyond market cap) that would exclude JBCHEPHARM, GUJGASLTD, GSPL and the other 8 above-Rs.700cr names
   in the table above, then re-export. That is the only path that gets the Nifty 500 count to 500 — no
   code path in this repo can restore rows the export never contained.
2. **If a dedicated card is opened for the wider 235-name gap:** split it exactly like the table above —
   the 179 null-mcap names likely need an `input_raw` refresh (separate question from the screener export),
   the 45 floor-edge names are cc#2151's existing proposal waiting on a founder decision, and the 11
   above-floor names are the same open question as this card's three, just a larger list.

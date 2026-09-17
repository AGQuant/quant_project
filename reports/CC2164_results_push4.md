# cc#2164 — Results app PUSH 4/5: companies view honours ?segment=, movers rails tidy, dead code out

Sprint RESULTS_APP_V3. Built 17-Sep-2026, after 13:43 IST server time (the landing time is in the task row). Page only: `mobile/results.html`. `results_app_mobile.py` untouched beyond what push 3 added.

## What changed

1. **`/m/results?view=companies&segment=<name>`** — the page reads `segment` from the URL, fetches `/api/mobile/results_app/companies?segment=…` (push 3, small payload) and pre-filters the rows to that segment on the client as well. Page title = the sector name; the as-of line reads "{n} companies · {segment} · Clear filter", and Clear filter returns to `/m/results?view=companies` (unfiltered, title "All filings"). Without the parameter nothing changes.
2. **Movers rails** ("Biggest profit jumps" / "falls") keep their geometry (card 46vw, max 172 px) — the founder did not flag them. A card with no write-up now shows nothing in that slot; "Read ›" appears only when the name is written up. The segment line was already one line with an ellipsis; measured, not assumed.
3. **Dead code out.** The three-cell hero grid class is gone: the two-cell grid is `.medians` (renamed so no `.kg` remains). The `.lad` / `.lr` ladder CSS and the "Every company that filed" line had already gone with push 2; the header comment no longer mentions "Sector by sector". 0 literal token fallbacks. One extra, stated here: the first screenshot of the short segment-filtered companies view showed a band of the html fallback colour beneath the table on the dark theme (the cc#2155 shared-CSS pattern), so `body` now carries `min-height:100dvh; background:var(--field)` like the other app pages fixed this week.
4. **Section order**, top to bottom: hero (two cells + Leading sectors strip) → Sectors this season → Biggest profit jumps → Biggest profit falls → Written up → Next 10 days → the "How to read this" note.

## Harness (Playwright, real Chromium, `scratchpad/cc2164_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Fixture: the 87 sector rows and the two segments' company rows from cc#2162 / cc#2163 (production replicates); movers = two real Shipping & Maritime rows, one flagged written (KMEW) and one not, plus one unwritten fall.

- Cold open `?view=companies&segment=Shipping%20%26%20Maritime` → the fetch carries `segment=Shipping & Maritime`; 6 rows, every row's segment is Shipping & Maritime; title "Shipping & Maritime"; as-of "6 companies · Shipping & Maritime · Clear filter" with the link to `/m/results?view=companies`. Tap Clear filter → 31 rows, title "All filings", fetch without the parameter, no `segment` in the URL.
- Season page: section headings in order Sectors this season → Biggest profit jumps → Biggest profit falls → Written up → Next 10 days, the hero first, the sector table before the movers; no mover card says "no write-up"; Read › on exactly the written card; segment lines one line; card widths 172 px; no sideways scroll; no page errors.
- Source grep: no `.kg`, no "Sector by sector", no ladder CSS, no "no write-up" span in `mobile/results.html`.
- The push-1, push-2 and push-3 harnesses were rerun after this change: ALL PASS (push-1's harness now selects `.medians .cell`).

**Screenshots looked at** (`scratchpad/cc2164_*.png`): `375_goldnight_full` and `375_aquawhite_full` — the full season page in the stated order: hero with the two cells and the sales strip, then "SECTORS THIS SEASON" with the toggle, chips and five rows directly under it, then the two movers rails (KMEW with "Read ›", GESHIP with the GVM only), then Written up and Next 10 days, then the note. `375_dark_companies_segment` — the companies view titled "Shipping & Maritime", the line "6 companies · Shipping & Maritime · Clear filter" in gold, and the six-row sortable table.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `https://scorr.in/m/results?view=companies&segment=Tyres` → only Tyres rows, title "Tyres", Clear filter → all filings.
2. `https://scorr.in/m/results` → a mover without a write-up shows no "no write-up" text; a written one shows "Read ›"; sections in the order above.

## Out of scope (by spec)

Anything on the web /result-corner page.

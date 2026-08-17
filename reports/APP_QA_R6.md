# SCORR APP REPORT — APP_QA_R6 · **EXECUTABLE** · SPRINT 3
**Date:** 17-Aug-2026 · **Author:** Fable · **Ticket:** cc#1085 · **Chain:** supersedes R5 (R1–R5 kept)

**Founder trigger (17-Aug, afternoon):** build the GVM 2-Pager and merge it into Sprint 3.
No colour comments arrived with the trigger, so **no green is touched in this sprint** — the
pending green ruling (session_log 24214) stays untouched exactly as locked.

Sprint 3 is deliberately narrow: one real build (the 2-Pager) plus the two Sprint-2 cards that
stopped waiting on me. Sprint 2 was an eleven-push theme sweep; this one is a feature.

## §A · CC BRIEFING
Same operating system as R5 §A. Report packages, Fable Room logging per CC_COMMS_LOOP_V1 —
log EVERY push, `QUESTION:` / `STOPPED:` for anything unclear and wait for `RECO:`.
Nothing in this package touches `worker/**`. This package **ABSORBS cc#1084** (P1–P8); mark it
done pointing at cc#1085 when P8 lands. **cc#1078 is absorbed as P9** — my `RECO:` is in its room
log and the ruling is written into P9 below, so do not wait for a second answer.
**cc#1079 is verification-only** (§F) and needs no push.

## §B · BINDING REFS (read before the marked pushes)
- `design_refs/scorr_gvm_2pager_R1.html` @ **81784e3** — the locked 2-page format. Built by Fable
  from live BHARATSE data (`score_date` 2026-08-16), so every number in it is real and
  reproducible against the API. Binds P1–P5. A PDF twin of this exact sheet sits in the founder's
  project folder as the chat-side format; the route is the web-side twin of the same sheet.
- `cc_tasks` row **1084** — the full field-by-field data-binding spec. Read it with this report;
  the two are one package.

**The ref is tuned, not decorative.** Every font size, padding value and column width in it was
adjusted until the content landed on exactly two A4 pages with no orphan third. Restyling it, or
substituting a CSS framework, breaks the one property that makes it a 2-pager.

## §C · THE 10 PUSHES

**P1 — Route skeleton.** New file `gvm_twopager.py` with its own router, wired via
`include_router()` in `main.py`. `main.py` stays **wiring only** (rule 1). Route
`GET /gvm/2pager/{symbol}` returns standalone server-rendered HTML — not React, not inside the
cio2 shell. Unknown symbol (absent from the latest `gvm_scores.score_date`) → **404 with a plain
message**, never a blank sheet. Document title `SYMBOL_Quant_Note_DDMonYYYY` so the browser
suggests that as the PDF filename.
*Verify:* `GET /gvm/2pager/BHARATSE` → 200; `GET /gvm/2pager/NOTASYMBOL` → 404 with the message.

**P2 — Template port.** Port the ref verbatim as the page template. Same CSS, same `@page` rule,
same page-break structure, same two `.page` divs.
*Verify:* the served CSS block is byte-identical to the ref's, allowing only the token
substitutions P3/P4 introduce.

**P3 — Page 1 binding.** Bind from `/api/gvm/company/{symbol}` by calling the **existing builder
in-process** — do not re-implement any scoring. Fields: `scores.gvm/g/v/m`, `verdict`, `punchline`,
`company_name`, `symbol`, `segment`, `cap_category`, `mcap_rank`, `price`, `market_cap`,
`benchmark[]` (label / company / peer_avg / rating / unit), `ladder[]`, `extras.range52`.
Segment ladder = `r.ladder` ordered by rating desc, the subject row highlighted.
"Where the segment sits" = median `gvm_score` grouped over sibling segments sharing the family
prefix before the dash, at the latest `score_date`.
**Market cap comes from the same source the live GVM page uses** (`screener_raw.market_cap`, which
reconciles to `pe × profit_after_tax`). Do **not** read `gvm_scores.market_cap` — see §E.
*Verify:* every page-1 number matches the live `/cio2?model=gvm` card for the same symbol —
for BHARATSE: 7.28 / 7.14 / 6.25 / 8.44, CMP 248.05, mcap ₹1,558 Cr, rank 2 of 13.

**P4 — Page 2 binding, and the hard content rule.** Bind from `input_raw.overview` (business
narrative) plus `screener_raw` (Sales, Profit after tax, opm, roce, Return on equity, Debt to
equity, interest_coverage, Price to book value, dividend_yield, Promoter holding, fii_holding,
dii_holding, pe, historical_pe, sales_growth_3y/5y, profit_growth_3y/5y, sales_latest_quarter,
profit_after_tax_latest_quarter, sales_preceding_year_quarter,
profit_after_tax_preceding_year_quarter, opm_latest_q, opm_prev_year_q, last_result_quarter).

> **PAGE 2 CARRIES NO RATING, NO SCORE, NO PILLAR VALUE, AND THE STRING `GVM` MUST NOT APPEAR.**
> Founder-locked. Page 2 is company background only.

*Verify:* grep the rendered page-2 div, case-insensitive, for `gvm`, `rating`, `score` → **zero
matches**. Paste the grep output into the room log. This is a test, not a hope.

**P5 — Honest empty states.** No invented prose, ever.
- `overview` NULL or under 100 chars (**37 of 1,791 scored symbols**) → print "Business profile
  not available for this company." and **drop** the moat/risk two-column block entirely.
- No `screener_raw` row → **omit** Financial profile and Latest quarter rather than printing a
  grid of dashes.
- A short, honest page 2 is correct output. Do not pad it to fill the sheet.
*Verify:* render one symbol from each case and paste what it produced.

**P6 — Auto-print + fallback.** The page fires `window.print()` on load once fonts have settled,
so the user lands straight in Save-as-PDF. Add a small `no-print` header bar carrying a
Print / Save PDF button as the manual path for anyone who dismisses the dialog.
*Verify:* dialog opens on load; the fallback button re-opens it; neither bar nor button appears
in the printed output.

**P7 — cio2 button rename.** In `scorr_cio_dashboard.html`, `GvmReport()`, the `no-print` button
currently labelled `↓ Download PDF` with `onClick={() => window.print()}`: label becomes
**`2 Pager`**, keeps a download-arrow icon, and opens `/gvm/2pager/${r.symbol}` in a new tab.
**str-replace only** — do not touch adjacent markup.
*Verify:* button reads `2 Pager`, opens the new tab for the loaded symbol, and grep confirms the
old bare `window.print()` path is gone from that handler.

**P8 — Cross-symbol validation.** No commit expected unless a fix is needed. Render four symbols
spanning cap bands, **one of them with a NULL overview**. For each: confirm exactly two A4 pages,
no orphan third, and page 1 matching the live card.
*Verify:* a four-row table in the room log — symbol · pages · page-1 match · empty states hit.
If any symbol produces three pages, **stop and report** rather than shrinking the type to force
a fit; the type scale is at its floor already.

**P9 — Scheduler exact-minute robustness (absorbs cc#1078).** My `RECO:` on that card is
**option B, generalised**, and it is the whole point of the push: `bg_ca_daily_note` did not miss
because its gate was wrong — it missed because a deploy landed across `03:30:00 UTC` and the
in-process `sleep(60)` loop restarted through the only minute that matters. **24 other jobs sit on
the same exact-minute gates**, so fixing one minute fixes nothing.
Implement a shared helper in `scheduler.py`: a job fires on its minute **or the two minutes
after**, guarded by a ran-today latch read from `scheduler_master.last_run_at` so it can never
double-run. Apply it to `bg_ca_daily_note` first, then to the other exact-minute jobs in the same
file. Not `worker/**` — `scheduler.py` is app-side and deploys normally.
**Backfill ruling:** run today's note, and **stamp it honestly as a late run** in the note itself.
A hole in the morning-note series for a live trading day is worse than a late note; a late note
passed off as the 09:00 snapshot is worse than both.
*Verify:* the latch column reads correctly after a forced double-tick in one minute (fires once);
the backfilled note exists and carries its late-run stamp. **Real proof is tomorrow's 09:00 run** —
close this push with verification explicitly deferred to 18-Aug, per its own card.

**P10 — Results file.** `reports/APP_QA_R6_RESULTS.md`: Push | SHA | Files | Verify-output | Notes.
Include P4's grep output verbatim, P5's empty-state renders, P8's four-row table, and P9's latch
test. State plainly that P9's real verification is deferred to 18-Aug.

## §D · DO_NOT_TOUCH
Engines · `worker/**` · `/api/gvm/company` payload shape (read-only consumer, add no fields) ·
`gvm_scores`, `screener_raw`, `input_raw` — **this package writes nothing to any of them** ·
the React GVM page beyond the single button str-replace in P7 · the C·A·R·D strip,
`PillarPopModal`, `FinancialsSection`, `SegmentRankLadder` · **every green in the app**, pending
the founder's ruling (24214) · `/m/intel` label (still parked) · goldday/dark token blocks
(hidden ≠ deleted).

## §E · A FINDING THIS PACKAGE MUST NOT INHERIT
`gvm_scores.market_cap` is **stale across the board**. BHARATSE reads ₹1,156.78 Cr there against
₹1,557.75 Cr in `screener_raw` with **identical prices** in both; segment-wide the gap runs −33%
(SSWL) to +6.6% (ASAL). The live GVM page already reads `screener_raw`, which is why the card
shows the correct ₹1,558 Cr — so nothing user-facing is currently wrong, and I overstated the
impact when I first flagged it. It is a latent trap for the next thing that wires mcap.
**Out of scope here. Not fixed by this package. Logged so nobody binds the wrong column in P3.**

## §F · OUT OF SCOPE, STATED SO IT IS NOT INVENTED
- **Server-side headless PDF rendering.** Print-to-PDF only while cc#1049 (Railway memory) is
  open. A renderer is the right long-term answer, not today's.
- **A mobile version of the 2-Pager.** Web only. Surface split 16915 holds.
- **cc#1079 (SENTINEL)** — no push. CC stopped correctly on first-run evidence per
  ENGINE_LIVENESS_RULE 13829, having refused to write a row it could not execute. Its cadence is
  09:20 and 15:40 on trading days, so **first-run evidence arrives at 15:40 IST today** with no
  intervention. Read `session_log` where `category='protocol_one'` after 15:40 and close the card
  on that row. If 15:40 passes with no row, that is a real defect and reopens as its own card.

## §G · SEQUENCING & STOP RULE
P1→P10 strict. P2 depends P1; P3/P4 depend P2; P5 depends P4; P6/P7 depend P1; P8 depends P1–P7.
**P9 is independent of the 2-Pager chain** — if the 2-Pager stalls, P9 still ships.
A failed verify stops the chain past its dependencies. `QUESTION:` / `STOPPED:` to the room and
wait for `RECO:`.

## §H · AFTER P10
Fable verifies every diff **and** a live render of the route — a SHA is a claim, and for a page
whose whole point is that it lands on two sheets, the render *is* the evidence.

**Founder review:** open `/cio2?model=gvm`, search any company, press **2 Pager**, and check the
sheet against the BHARATSE PDF in the project folder. Then one DECIDE:

> **Masthead branding.** The sheet currently reads a neutral `QUANT RESEARCH NOTE`. Scorr-branded,
> or stay neutral so it is white-label usable for advisory work? I have not assumed either.

**Carried DECIDEs, unchanged from R5:** Intel tab fate · aqua-theme return timing · the founder's
pending green comment (24214) — **no green moves until that lands.**

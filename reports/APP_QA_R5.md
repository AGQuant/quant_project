# SCORR APP REPORT — APP_QA_R5 · **EXECUTABLE** · SPRINT 2
**Date:** 17-Aug-2026 · **Author:** Fable · **Ticket:** cc#1082 · **Chain:** supersedes R4 (R1–R4 kept)
**Founder rulings folded in (12:00):** GOLD NIGHT (black·white·gold) is the ONLY visible theme this sprint; goldday + dark hidden, aqua returns next build. Home lower-section fonts raised + more styling. VIX never green. Digest gets DAY|WEEK + scale.

## §A · CC BRIEFING
Same operating system as R4 §A (report packages, Fable Room logging per CC_COMMS_LOOP_V1 — log EVERY push, QUESTION:/STOPPED: for anything unclear). Production mode ON. Nothing touches `worker/**`. This package ABSORBS cc#1080 (P4+P5) and cc#1081 (P3) — mark them done pointing at cc#1082 when their pushes land. cc#1078/1079 stay separate claims.

## §B · BINDING REFS (read before the marked pushes)
- `design_refs/scorr_sprint2_goldnight_R1.html` @ **cd75e2c** — the sprint's visual language: raised scale (stat values 18px, rows 13px, sub-labels 10.5/700, floor 9.5px, news 13.5px, tools 12px, nav 11px), gold-deep keys, gold nav active. Binds P2, P6–P10.
- `design_refs/scorr_digest_mobile_R2.html` @ **c77cae9** — DAY|WEEK switch + digest scale. Binds P4/P5.
- `design_refs/scorr_appshell_R1.html` @ **a20ae0d** — shell variants (already live from R4; P7–P9 pages GET the shell as part of migration, variant B where the page owns a nav tab).
- `scorr_themes.css` @ current — token names; P1 edits defaults only, never token values.

## §C · THE 11 PUSHES

**P1 — Theme gate: goldnight forced, switch hidden.** `mobile/home.html`: default `data-theme="goldnight"`; ignore any stored `scorr_theme` ≠ goldnight this sprint (read it, but force goldnight — do NOT delete the storage key); HIDE the ◆◇◈ switch control (display:none or removed markup, state which). Tokens for goldday/dark stay untouched in `scorr_themes.css`.
*Verify:* home renders goldnight on fresh load AND on a device that previously chose dark; no switch visible.

**P2 — Home raised scale + styling.** Per goldnight ref: THE BOOK stat values → 18px with gold-deep keys + gold rail on the card; MY PORTFOLIO rows → 13px sym / 12.5px values / 48px min row; LIVE NEWS headlines → 13.5px with gold-deep age stamps; TOOLS labels 12px, icons 19px gold-glow; bottom nav 11px labels, gold active state. Floor 9.5px — grep for smaller after.
*Verify:* grep font-size < 9.5px in the themed sections → 0; founder screenshot.

**P3 — VIX chip (absorbs cc#1081).** Execute cc#1081's spec verbatim: never green, red on confirming fear (rising + bearish gate, or ≥17), chalk otherwise, flat chrome, paste-before-change, the context-vs-check read-only item included.

**P4 — Digest payload week% (absorbs cc#1080 half A).** `digest_v3.py`: `week_chg_pct` per global row = close vs close 5 trading sessions back from `global_indices` history; <6 sessions → null. Log first-run coverage count.

**P5 — Digest page: DAY|WEEK + scale (absorbs cc#1080 half B).** Per digest R2: chip toggle (default DAY, in-memory), null week = muted em-dash; the R2 type scale on all lower sections. Grep floor 9.5px.

**P6 — R5 pages forced goldnight.** `/m/v10`, `/m/digest`, `/m/gvm2`: default goldnight, honor nothing else this sprint (same force pattern as P1). Their content already reads tokens, so this is the default line only.
*Verify:* all three render black·white·gold.

**P7 — /m/gvm migration (WS4 page 1).** `mobile/gvm.html` (23KB): goldnight tokens via `scorr_themes.css` + bridge if it has the `.screen` scope issue (check first — P2 of R4 documented the pattern); raised scale per goldnight ref; shell per appshell variant B (GVM tab lit) if the page lacks it. Data wiring untouched.
*Verify:* zero legacy hardcoded palette hex in the migrated sections; page functions identically (founder tap-through).

**P8 — /m/check migration (WS4 page 2).** Same treatment, `mobile/check.html` (6.5KB), variant B (Check tab).

**P9 — /m/models + /m/qb migration (WS4 pages 3–4).** Same treatment, both small shells; models = variant B (Models tab), qb = variant A.

**P10 — Nav + tools polish sweep.** Any page carrying the 5-slot nav or tools grid gets the goldnight nav/tools styling from the ref (gold active, sizes), so no page shows the old volt-active nav beside migrated pages.
*Verify:* nav renders identically (goldnight) on home + all migrated pages.

**P11 — Results file.** `reports/APP_QA_R5_RESULTS.md`: Push | SHA | Files | Verify-output | Notes, incl. P3's paste-before-change and context-vs-check finding, P4's coverage count.

## §D · DO_NOT_TOUCH
Engines, `worker/**`, `/api` payload keys already consumed (P4 ADDS a key only), P&L green/red hues, `scorr_themes.css` token VALUES, `/m/intel` label (still parked), goldday/dark token blocks (hidden ≠ deleted), cc#1078/1079 (separate).

## §E · SEQUENCING & STOP RULE
P1→P11 strict. Failed verify stops the chain past dependencies (P2 depends P1; P5 depends P4; P7–P10 depend P1; P3/P4 independent). QUESTION:/STOPPED: to the room, wait for Fable RECO:.

## §F · AFTER P11
Fable verifies all diffs + DB; founder device review: home (scale+gold, no switch), digest (WEEK toggle live values), VIX chip chalk/red only, gvm/check/models/qb in goldnight. Carried DECIDEs: Intel tab · aqua-theme return timing · founder's pending green comment (24214).

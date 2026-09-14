# cc#2069 — `.l` chip text contrast follow-up (partial, honestly reported)

This is CC's own follow-up, filed while fixing cc#2060 (Pixel Lens theme_leak), for the one part
of that card's own scope lock (chip text colour) that testing showed had a real, measured problem.

## What shipped

`.l`'s `color` in both `scorr_digest_mobile.html` and `scorr_v10_signal.html`: literal `#FF8FA5`
(never bridged to any theme) → `var(--heat)` (bridges to `--loss` via `scorr_appshell.css`'s
`body[data-theme]{--heat:var(--loss);...}` rule, the same mechanism `.w`/`.n` already use
correctly). `.w` and `.n` are **untouched** — confirmed by real-Chromium re-measure that their own
cc#2060 numbers are unchanged (aquawhite `.w` still ~4.01, goldnight `.n` still ~3.46).

## This is a real improvement — and honestly, not a complete fix

The task's own spec called `var(--heat)` "the clear, high-confidence fix." Measuring it precisely
(real Chromium, the exact background this rule already ships) shows that framing understated the
picture — worth correcting here rather than letting the spec's own optimism stand as the record:

**Real, unambiguous wins — 3 of 7 light themes now fully clear AA(4.5)**, where they were badly
failing before: Blush 1.53→4.61, Rose Wall 1.36→4.94, Silver Gold 1.62→4.87.

**Meaningfully less severe, but still under the floor — 4 more themes**, all light: Gold Day
1.55→3.22 (worst case), Aqua White 1.54→4.17, Dusk Day 1.44→4.32, Orange Peel 1.55→4.04. Still a
real readability improvement (roughly double the contrast ratio in the worst case) even though none
of these four clear AA yet.

**A trade-off disclosed, not hidden — 3 DARK themes regress from passing to just under the floor**:
Dark (the app's own default theme) 6.25→4.20, Dusk Violet (withdrawn from the switcher, lower
stakes) 5.46→4.24, Indigo Ash 5.12→4.19. All three stay clearly legible in practice (4.2:1 is not
the 1.4:1 the light themes had — it is a borderline technical AA miss, not a visibility failure)
but it is a real regression on real themes, including the one most readers see by default, and it
would be dishonest to ship this as "fixed" without saying so.

**Root cause of why a bridged token alone can't fully solve this**: `.l`'s new text (`--loss`) now
sits on a **wash of that same colour** (`color-mix(in srgb, var(--loss) 14%, var(--hi))`) rather
than a neutral ground — the exact same tight-margin pairing `.w`/`.n` already have (cc#2060's own
measured gaps: `.w` fails all 7 light themes, `.n` fails 8/15 including two dark ones). Pointing
`.l`'s text at the "correct" bridged token was necessary but not sufficient; the real fix most
likely lives on the **background** side (a lower mix percentage, or mixing toward a different base
than `--hi`) for all three badges together, not a per-badge text swap.

## Consolidated design question — one, not two

cc#2060 originally scoped `.w`/`.n`'s background-treatment question as open, separate from `.l`.
Having now measured `.l` precisely, it turns out to be the **same** open question, not a second
one: all three badges need a background-side decision from Fable/founder, not a mechanical fix CC
can make unilaterally. Logged as one `QUESTION:` line on this task rather than left implicit.

## A related, previously-unknown finding — filed separately, not fixed here

Grepped the app for the same literal hex pair while implementing this card and found
`mobile/home.html:1649` — `.dirbadge.bear{border-color:#5A2634;background:#241019;color:#FF8FA5}` —
byte-identical to `.l`'s pre-cc#2060 state, on a **third** live file neither cc#2060 nor cc#2069
named in scope. `.dirbadge.bull`'s text already bridges (`--grn:var(--t-win)`, confirmed by direct
read), `.dirbadge.bear`'s does not — the exact same asymmetry as `.w`/`.l`. Not fixed in this
commit (out of this card's own stated file scope: `scorr_digest_mobile.html` +
`scorr_v10_signal.html` only) — filed as **cc#2070** (P2) instead of silently expanded or silently
left for someone else to rediscover.

## Verify

`node --check` clean (inline-script extraction, both files). Real headless Chromium, the exact
shipped `.w`/`.l`/`.n` rules extracted verbatim from both committed files, run against all 15 real
themes from `scorr_themes.css` plus the real `scorr_appshell.css` bridge — **9/9 checks pass**:

- Both files confirmed to carry `color:var(--heat)`, with the literal `#FF8FA5` gone.
- Every one of the 15 themes' real-browser pass/fail verdict matches exactly what the shipped code
  comment states — the comment is not aspirational, it is what a real browser actually renders.
- The specific cited numbers (Gold Day's 3.22 worst case, Dark's regression to 4.20, the three
  newly-passing light themes) are each independently re-confirmed to within rounding.
- `.w` and `.n` are confirmed unaffected by this change — their own cc#2060 numbers hold exactly.
- Zero page errors across all 15 theme runs.

**Not fixed and not silently deferred**: the remaining gap on all three badges is logged as one
open `QUESTION:` for Fable/founder, per the Fable Room protocol, rather than left implicit in a
report nobody is pointed at.

# cc#2191 — /m/fpc step 2: plain numbered questions, no commentary

Built 17-Sep-2026 (founder 15:02 IST); the landing time is in the task row. Page only: `mobile/fpc.html`, step 2 (`s2()` and `q()`); wording, option sets and scoring untouched.

## What changed

- Every question carries a leading number in the brand colour, continuous across the two sections: 1–2 under YOUR SITUATION, 3–7 under YOUR COMFORT.
- The section titles stay as small-caps headers; their right-side subtitles ("what you can afford to risk", "what you are happy to risk") are gone.
- All helper sentences are gone: "These three come from your sliders — no need to answer twice", "seven taps, or skip", the "Why two parts" note. The skip link reads "Skip the comfort questions ›".
- The three slider-derived answers are one compact read-only line under the SITUATION header — "From your sliders: 6+ months runway · almost no EMIs · 1 depending on you" — no explanation sentence, a hairline under it, no dashed separator.
- The step label is "Step 2 of 3".
- Spacing: every question block has the same padding (12 px above, 6 px below), no hairlines between questions, one hairline between the derived line and the questions; the dashed `.auto` block is gone.

## Harness (Playwright, real Chromium, `scratchpad/cc2191_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Step 2 reached through the real Next tap; the text nodes of the step were walked and classified.

- Seven questions numbered 1..7 ("1. When will you need this money?", "2. How steady is your income?", "3. Where is your money kept today?" …).
- Headers "Your situation" / "Your comfort" present, no subtitles.
- Text nodes outside questions, options, headers, the derived line and the buttons: none (the spec's "zero helper sentences").
- Derived line as above, exactly two separators, no "no need to answer twice".
- Option grids intact: 7 × 4 options, one preselected each; tapping an option selects it.
- Step label "Step 2 of 3"; skip link "Skip the comfort questions ›"; one padding value across all questions; zero dashed borders on the step.
- No page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2191_*.png`): `375_light_step2` and `375_dark_step2` — YOUR SITUATION with the one derived line, then "1." and "2." with their four chips each, YOUR COMFORT with "3." to "7.", the Back / See my plan buttons and the short skip link. Nothing else on the step.

## Live check (Fable — the sandbox cannot reach scorr.in)

`https://scorr.in/m/fpc` → Next: seven numbered questions and one derived line, no sentences between them.

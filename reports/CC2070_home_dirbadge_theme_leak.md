# cc#2070 — `mobile/home.html` `.dirbadge` theme_leak + frozen text (found during cc#2069)

Not founder-filed — found by CC while implementing cc#2069, grepping the app for the same literal
hex pair cc#2060 fixed and finding a third, previously-unknown live instance.

## What this is

`#heroPositionsBody .dirbadge.bull`/`.bear` (Home's Hero Positions card, LONG/SHORT direction
badge) carried the **identical** literal hex pattern `.w`/`.l` had before cc#2060 — background/
border frozen near-black on every theme, plus (on `.bear` only) a text colour that never bridged
to any theme. Confirmed byte-identical to the pre-cc#2060 `.w`/`.l` values before touching anything.

## Step 1 gate — this file's token system is genuinely different, checked before assuming

`scorr_digest_mobile.html`/`scorr_v10_signal.html` bridge `--volt`/`--heat`/`--amber` straight from
`scorr_themes.css`'s `--win`/`--loss`/`--label` via one `body[data-theme]` rule
(`scorr_appshell.css`). `mobile/home.html` has **two** hops instead: an unscoped
`body[data-theme]{--t-win:var(--win); --t-loss:var(--loss); ...}` bridge (line 43), then a
**scoped** legacy-name bridge on `.screen,.bnav,.tpo,...{--grn:var(--t-win); --red:var(--t-loss);
--panel2:var(--t-hi); --line:var(--t-edge); ...}` (lines 58-71) — confirmed by direct read, not
assumed to match the other two files.

`.dirbadge.bull`'s pre-existing `color:var(--grn)` already resolves correctly today (it is live,
shipped code) — proof the scoped bridge reaches `.dirbadge` wherever it actually renders, without
needing to trace its exact DOM ancestry by hand. The fix reuses this file's **own already-proven**
idiom (`--grn`/`--red`/`--panel2`/`--line`), not `--win`/`--loss`/`--hi`/`--edge` directly — one
token family per rule, no mixing.

## The fix

```
.dirbadge.bull{border-color:color-mix(in srgb,var(--grn) 40%,var(--line));background:color-mix(in srgb,var(--grn) 14%,var(--panel2));color:var(--grn)}
.dirbadge.bear{border-color:color-mix(in srgb,var(--red) 40%,var(--line));background:color-mix(in srgb,var(--red) 14%,var(--panel2));color:var(--red)}
```

Same 14%/40% ratios cc#2060 shipped, same pattern. `.bull`'s text is untouched (already correct).
`.bear`'s text moves from the literal `#FF8FA5` (cc#2069's exact class of problem — never bridged,
catastrophic on light themes at any background) to `var(--red)`, matching `.bull`'s own working
idiom rather than importing `--heat` (a name this file's token system does not use).

## Honestly, not a full AA fix — same limitation as cc#2060/cc#2069, disclosed the same way

Real-Chromium numbers land within rounding of `.w`/`.l`'s own post-fix numbers (expected — same
underlying `--win`/`--loss`/`--hi`/`--edge` values, reached through a different bridge chain):
`bull` fails AA on the same 7 light themes `.w` does (2.59–4.46:1); `bear` fails on the same 7
themes `.l` does post-cc#2069 (3 light + 3 dark including the app's own default `dark` theme,
4.04–4.32:1). This is **the same open design question** logged on cc#2069, not a new one — folded
into that one `QUESTION:` rather than opening a second, duplicate thread.

## Verify

`node --check` clean (inline-script extraction). Real headless Chromium, the actual bridge chain
(both hops) and `.dirbadge` rules **extracted verbatim from the committed file**, run inside a real
`.screen`-wrapped DOM (proving the scoped bridge actually resolves for this element, not assuming
it does because `.bull`'s pre-existing usage happened to already work) — **14/14 checks pass**:

- No literal hex remains; both rules confirmed using `color-mix`; `.bull`'s text confirmed
  unchanged, `.bear`'s confirmed moved to `var(--red)`.
- The scoped legacy bridge resolves to real, non-null colours on every one of the 15 themes inside
  `.screen` — the DOM-scope question is answered empirically, not assumed.
- theme_leak resolved on all 7 light themes for both badges (background tracks the page, no longer
  inverted).
- Real WCAG contrast measured for both badges, all 15 themes — reported honestly, matching (not
  claiming to beat) `.w`/`.l`'s own already-disclosed AA gaps.
- `.bear`'s new text is confirmed a real, measured improvement over what the old literal would have
  produced against this same new background, on the worst light theme.
- Zero page errors across all 15 theme runs.

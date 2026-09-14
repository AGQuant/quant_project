# cc#2060 — Pixel Lens theme_leak: .w/.l/.n chip fills/borders

Founder ask: review the nightly Pixel Lens (cc#2012) run for anything worth fixing. This was the
one hard defect flagged — `check_name=theme_leak`, the only FAIL of that check in 176 captures,
on `/m/digest`'s state chip (`span#dd-state`, `.n`) on aquawhite: `bg rgb(36,29,14) lum=0.01`
against a page luminance of 0.94 — a dark blob on a light theme.

## Root cause, re-confirmed before editing (Step 1 gate)

`.w`/`.l`/`.n` in both `scorr_digest_mobile.html` (lines 85-87 pre-fix) and `scorr_v10_signal.html`
(lines 30-32 pre-fix) carried a **literal hex** border/background, constant on every theme:

```
.w{border-color:#3E5A18;background:#18240E;color:var(--volt)}
.l{border-color:#5A2634;background:#241019;color:#FF8FA5}
.n{border-color:#5A4218;background:#241D0E;color:var(--amber)}
```

Confirmed both files carry the identical three rules verbatim — the card's own evidence noted the
deck helpers were ported between these files and the chips came with them.

## The fix

Fills/borders now wash the theme's own tokens over neutral grounds, the same `color-mix` pattern
`.tile.up`/`.tile.dn` (cc#1780, `scorr_digest_mobile.html`) already uses in this exact file:

```
.w{border-color:color-mix(in srgb,var(--win) 40%,var(--edge));background:color-mix(in srgb,var(--win) 14%,var(--hi));color:var(--volt)}
.l{border-color:color-mix(in srgb,var(--loss) 40%,var(--edge));background:color-mix(in srgb,var(--loss) 14%,var(--hi));color:#FF8FA5}
.n{border-color:color-mix(in srgb,var(--amber) 40%,var(--edge));background:color-mix(in srgb,var(--amber) 14%,var(--hi));color:var(--amber)}
```

Background: 14%, matching the card's own given example exactly (`color-mix(in srgb, var(--win)
14%, var(--hi))`). Border: 40% into `--edge`, matching cc#1780's own border ratio/base for this
same wash-over-neutral pattern (no percentage was specified for border by the card; this borrows
the nearest existing precedent in the same file rather than inventing a new number). Text colour
is **byte-identical to before** in both files, per the card's own explicit scope lock (item 2:
"stays --volt/--heat/--amber as today").

`--win`/`--loss` are not local to these files — confirmed via `<link rel="stylesheet"
href="/static/scorr_themes.css">` in both, which defines all 15 live themes' `--win`/`--loss`/
`--label`/`--hi`/`--edge`. `--amber` resolves through a second, separate mechanism: both files also
load `/static/scorr_appshell.css`, whose `body[data-theme]{--volt:var(--win);--heat:var(--loss);
--amber:var(--label);...}` bridge rule overrides the page's own static local `:root` definitions
of `--volt`/`--heat`/`--amber` for everything inside `<body>` — read directly, not assumed, since
it changes what "leave text as today" actually resolves to at runtime (see next section).

**Do not touch, respected:** the deck/pager/OI-histogram CSS around these rules — nothing else in
either file was edited.

## A finding surfaced by testing, not shipped here, filed as cc#2069

Before editing, the plan was "background only, text is a solved problem via the appshell bridge."
Real-Chromium measurement (below) showed that's only true for `.w`/`.n` (which bridge to `--win`/
`--label` and therefore already track the theme) — and even those have real AA gaps once their
text sits on a wash of the *same* colour rather than a neutral ground. `.l`'s text is a raw literal
`#FF8FA5`, not `var(--heat)` — it never bridges at all, and fails badly on every light theme at any
background percentage, including a hypothetical 0% (pure `--hi`).

This is a real, freshly-quantified defect, but fixing it means changing a `color:` declaration the
card explicitly scoped out ("text stays as today"). Rather than silently expand scope or silently
ship a measured regression, it's filed as **cc#2069** (P2) with the exact numbers below, a clear
recommended fix for `.l` (point it at `var(--heat)`, matching `.w`/`.n`'s already-correct pattern),
and an honest "needs a design call" for `.w`/`.n` (the text tokens are already theme-correct; the
wash treatment for these specific same-hue chips is the more likely fix, not the text). Both source
files carry a comment citing cc#2069 directly above the `.w`/`.l`/`.n` rules.

## Verify

`node --check` clean (inline-script extraction). Real headless Chromium, **verbatim CSS extracted
from the real committed files and the real `scorr_themes.css` (all 15 theme blocks) +
`scorr_appshell.css` (the bridge rule)** — not retyped, not approximated — **26/26 checks pass**:

- No literal hex remains in `.w`/`.l`/`.n` in either file; all six declarations now use `color-mix`;
  text colours are confirmed byte-identical to the pre-fix source.
- **theme_leak signal reproduced pre-fix and resolved post-fix**, using the same signal Pixel Lens
  itself measures (element background luminance vs page luminance): on aquawhite, `.n`'s background
  luminance was 0.013 against a page luminance of 0.939 pre-fix (matching the crawler's own
  `lum=0.01` finding almost exactly) and 0.716 post-fix — no longer inverted. Confirmed across all
  7 light themes for all 3 chips, not just the one instance the crawler happened to catch.
- Real-browser WCAG contrast (`getComputedStyle`, actual `color-mix` resolution — Chromium
  serializes a mixed colour as `color(srgb …)` floats rather than `rgb()` ints, handled explicitly
  after an initial test-harness bug where the parser only understood the latter format):
  - `.w`: AA(4.5) on all 8 dark themes (5.7–8.7:1), fails on all 7 light themes (2.59–4.46:1).
  - `.n`: AA on 7/15 (not purely light/dark split — fails include two dark themes, goldnight 3.46
    and winepurple 2.82).
  - `.l`: fails all 7 light themes (1.37–1.62:1 as shipped; 1.72–2.03:1 even at a 0% background
    mix, confirming no percentage choice rescues it), passes all 8 dark themes (5.1–6.9:1).
- Zero page errors across all 15 theme runs.

Two test-harness bugs were found and fixed before the suite was trustworthy: a cascade-order
mistake where a generic `.chip` base rule was declared *after* `.w`/`.l`/`.n` in the harness
(opposite of the real files' order), silently resetting `border-color` back to `currentColor`; and
the `color(srgb …)` parsing gap above. Neither was a product bug.

Not done here, not needed for this card: the founder's own live tap-through on scorr.in (this
container has no route to the deployed site) and the Pixel Lens re-capture itself — both marked
FOUNDER-ONLY / external-system in the card's own verify section.

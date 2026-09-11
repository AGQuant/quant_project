# cc#1998 Step B — progress 1: mobile/home.html done, mobile/alerts.html correction

Step B removes `var(--name, #literal)` fallbacks where cc#1970's bridge guarantees the token is
defined, starting with the two `--txt` groups per R7's sequencing. This push does the first group
**properly verified, not assumed** — and corrects a real error in the earlier scoping (log 6317),
which called both `mobile/home.html` and `mobile/alerts.html` "straightforwardly safe" without
checking each file's actual bridge coverage.

## mobile/home.html — DONE, 53 `--txt` fallbacks removed (log 6317 estimated ~50)

Verified the bridge actually reaches every element before touching anything, not just trusted the
earlier estimate:

- `<body data-theme="goldnight">` (line 1841), `<div class="screen">` opens right after (line
  1873) — essentially all page content lives inside `.screen`.
- The file's own inline `<style>` (lines 42-71) declares `--txt:var(--t-ink)` on
  `.screen,.bnav,.tpo,.pfveil,.pfsheet,.isheet-ov,.symsheet,.scorr-ml-ov,.pwa-sheet-ov,.pwa-sheet,.rcard-ov,#scorr-bell-ov`
  — `.screen`/`.bnav` cover the page body, and the ten overlay-sheet classes cover everything that
  mounts outside `.screen` as a body-level sibling (cc#1770, 06-Sep, fixed exactly this gap — the
  file's own comment at lines 47-56 documents the PRE-cc#1770 bug this bridge fixes, describing a
  past problem, not a present one).
- Spot-checked the one non-obvious case: `pcrGaugeSvg()`'s JS-templated SVG (3 of the 50
  occurrences, lines ~5388/5389/5396) renders into `.pmood`, a mood-deck card — a normal content
  card, not an overlay, so it lives inside `.screen` like everything else.

Replaced all 53 with a single `replace_all` edit (`var(--txt,#E9EEFB)` -> `var(--txt)` — every
occurrence used byte-identical text, confirmed before editing). The validator's precise count
(117 fallbacks in this file before, not just the ~50 `--txt` ones — the other ~64 are different
token names, untouched this push) is stricter than the earlier estimate; 64 remain after, all
non-`--txt`. Gate re-run post-edit: `theme_validator.gate('mobile/home.html', ...)` returns
`(True, None)`. `node --check` clean on all 3 non-empty inline script blocks (1,206 / 905 /
290,083 chars). No visual regression possible from this construction: `var(--txt)` with no
fallback resolves to the SAME value `var(--txt,#E9EEFB)` always resolved to on this page, because
`--txt` was always defined here — the fallback was genuinely dead weight, not a safety net.

## mobile/alerts.html — CORRECTION, not done. The earlier "straightforwardly safe" call was wrong.

Checked before editing, same as home.html — and it does not hold up. `mobile/alerts.html` has
**no local bridge** (unlike `home.html`, its `<style>` block never declares `--txt:var(--t-ink)`
or anything like it), and its three linked stylesheets are `mobile_app.css`, `scorr_themes.css`,
`scorr_appshell.css` — **not** `mobile/theme_mobile.css`, which is one of only three files in the
whole repo that actually contain the `--txt:var(--t-ink)` bridge declaration (the other two are
`home.html` itself and `mobile/models.html`). `scorr_appshell.css` was checked directly: no
`.screen{` rule, no `--txt:var` anywhere in it.

**This means alerts.html's 4 `var(--txt,#E9EEFB)` fallbacks are genuinely load-bearing right now**
— there is nothing else in this page's cascade providing `--txt`, so the literal `#E9EEFB` is
what is actually painting the text. Stripping them, as the earlier scoping assumed was safe, would
have been a real regression: text would fall through to `color: initial` (effectively unstyled/
black) the moment anything upstream of the literal ever changed — the exact "token that silently
resolves wrong on a scope nobody checked" failure this whole card exists to prevent, this time
almost shipped by the card's own earlier estimate rather than caught by it. **Not touched.**

Fixing this properly is a small, separate, genuine addition (link `theme_mobile.css` on this page,
or add the same three-line bridge alerts.html is missing) — not a fallback removal at all. Filing
it as a one-line addendum to this card rather than doing it unasked inside a "remove fallbacks"
push: it is new code, not a deletion, and deserves its own explicit go rather than riding in here.

## What's still open on Step B (unchanged scope, restated for the log)

- `v8_dashboard.html` (4 fallbacks) — not yet checked for bridge coverage.
- The seven shared JS modules (`scorr_card_common.js` 6, `scorr_alert_create.js` 3,
  `scorr_analysis_card.js` 3, `scorr_card_strip.js` 2, `scorr_model_portfolio.js`,
  `scorr_cockpit_card.js`, `scorr_news_row.js` — 1 each) plus `pwa_endpoints.py`,
  `mobile_endpoints.py`, `scrub_layer.js`, `scorr_home.html` (1 each) — each is injected into
  multiple surfaces and needs its own per-surface scope check, exactly as flagged before. Not
  started — genuinely needs the same file-by-file treatment as above, not a batch assumption.
- Correction: the original count cited "six files with 1 each" but named seven — the true count is
  seven (matches the 79 total exactly: 50+6+4+4+3+3+2 = 72, +7x1 = 79). Noting the off-by-one now
  so it does not propagate further.

## Verify

- `grep -c 'var(--txt,#E9EEFB)' mobile/home.html` = 0.
- `theme_validator.gate('mobile/home.html', ...)` = `(True, None)` post-edit.
- Fallback total: baseline 847, this file alone drops from 117 to 64 (-53) — total fallback count
  decreases, the ratchet only ever refuses an increase, so nothing to re-baseline.
- `node --check` clean on `mobile/home.html`'s inline scripts.
- `mobile/alerts.html`: unchanged, 0 lines touched — confirmed by not being in this push's diff.

Nothing under `worker/**`. Only file changed: `mobile/home.html` (+ this report). Card **NOT**
set done — Fable verifies, and Step B itself stays open (13 files / ~26 fallbacks + the
alerts.html bridge gap still ahead).

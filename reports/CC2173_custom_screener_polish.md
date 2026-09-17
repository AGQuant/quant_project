# cc#2173 -- Custom screener: no per-chip counts, one live count at the top, shorter rows (founder review 17-Sep 12:19)

Founder on the live `/m/screeners?view=custom`: "very nice. Don't give count inside buttons, not required --
just give it on the top; as the user selects, the count changes. Make the rows a little lower in height."
Page only (`mobile/screeners.html`); `custom_screener_app.py` untouched (`/meta` still returns `alone`, the
page just stops rendering it); the names sheet untouched.

## 1. What changed

- **Chips are label-only**: `Large` / `Mid` / `Small & micro` ..., centred, 44px tall (measured 44 on every
  chip), same on-state. Zero digits inside any chip.
- **The one live count sits under the subtitle**: `247 names match` in the 18px/800 style, fed by the same
  debounced, abortable `/run` call the footer used (it dims while loading, never blanks; `0 · names match ·
  pick at least one filter` when nothing is selected; `No name passes all of these today` on a real zero).
- **The sticky footer keeps the buttons only** -- `Clear`, `Share` (cc#2160), `Show names` -- right-aligned;
  the bar is 61px.
- **Rows are ~30% shorter**: the label and the rule now share ONE line (`SIZE  Large = top 100 by market
  cap, Mid = next 150, ...` with an ellipsis where it does not fit); tapping that line opens the full rule
  text, tapping again closes it (the `title` attribute carries it too); row padding 12 -> 7px; chip gap
  8 -> 6px.

## 2. Measured (Playwright 375x812, the shipped page vs this one, same stubs)

| | before | after |
|---|---|---|
| filter row height, average (min-max) | 112 (105-120) | 78 (77-78) = -30% |
| panel of ten rows | 1,114 | 781 = -30% |
| whole page height | 1,556 (1.92 screens) | 1,252 (1.54 screens) |
| chips carrying a number | 30 | 0 |
| chip height | 44+ | 44 |
| count position | sticky footer | under the subtitle, 18px/800 |
| footer | count + 3 buttons | `Clear · Share · Show names`, right gap 16px |

The head (title, subtitle, count line), the "How it works" note and the footer pad do not shrink, which is
why the whole page comes down 20% while the rows come down 30%.

## 3. Checks -- `scratchpad/cc2173_test.py`, `=== ALL cc#2173 CHECKS PASS ===` (375px, dark + light)

no number inside any chip; count line under the subtitle in 18px/800; footer buttons only, right-aligned;
rows -30% / panel -30% / page -20%; 1.54 screens; chips 44px, one line per row, no horizontal overflow;
rule text one line with ellipsis, tap opens it (13 -> 26px) and closes it; nothing selected -> `0` + hint,
Show names disabled; tap Large + Mid -> `247 names match` (100 + 147, the live `alone` figures -- the stub
returns the sum because the size buttons are disjoint), Show names enabled; Clear -> `0`, disabled; the demo
combo still `32` and the sheet still opens with 32 rows and `Your screen`; no page errors. The cc#2160 and
cc#2159 harnesses re-run green on the final page.

Note for the founder (from the card): Mid shows 147, not 150, because three mid-cap names carry no current
GVM score -- the scored count is the right one.

Screenshots looked at: `cc2173_custom_goldnight.png`, `cc2173_custom_aquawhite.png` (Large + Mid on, `247
names match` under the subtitle, label-only chips, one-line rule headers, the button-only footer),
`cc2173_custom_dark_full.png` (all ten rows).

## 4. Live check

https://scorr.in/m/screeners?view=custom -- tap Large and Mid: the line under the subtitle reads the count;
no number inside the buttons; the rows are shorter; tap a row's grey header line to read its full rule.

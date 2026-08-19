# cc#1108 — Index Intel, restore the pane

**19-Aug-2026 · CC**

The founder's report: V8 Index Intel formatting on web changed and is much worse, verdict
labels render as enormous washed-out text over the sparklines, and it is **every badge on the
pane**, not the PCR verdicts alone.

---

## What was wrong

One selector, in a file that is not part of the page.

`scorr_theme_r5.css` line 221 (cc#1064 Telemetry Drop, Signature Pass 2) carried:

```css
:root:root:not([data-theme="light"]) .hero .mood,
:root:root:not([data-theme="light"]) .mood{
  font-family:var(--r5-display);
  font-size:56px; line-height:.95; letter-spacing:1px;
  text-transform:uppercase;
  display:inline-block; transform:skewX(-6deg); transform-origin:left center;
}
```

That rule was drawn for the **mood word** on the mobile home hero. It never matched it.
The live home renders the word as `.hero-mood` (`mobile_endpoints.py:1683`, font-size 32px),
and there is no `.hero .mood` anywhere in the repo. So the intended target was never hit.

What the bare `.mood` **did** hit is three **containers** that happen to share the name:

| surface | element | what it is |
|---|---|---|
| `/dashboard` Index Intel | `#pane-index .mood` (`v8_dashboard.html:4850`) | the gate strip — a flex row |
| `/dashboard` master | `.mood` (`v8_dashboard.html:715`) | the market gate block |
| `/m/v8` | `.mood` (`mobile/v8.html:401`) | the mood **card**; its word is `.mv` |

None of the three declares `font-size`, so nothing out-specified `56px` and it won on all
three. `#pane-index .mood` did keep `display:flex` (an ID beats R5's specificity), so the
strip stayed a row — but at 56px, uppercase, in Archivo Black, with a 150px `::after` swoosh
bar drawn under it. That is the enormous washed-out text.

`/dashboard` is in `_PWA_INJECT_PATHS`, and `main.py:311` links this sheet into every page in
that set. It is not in the local copy of `v8_dashboard.html`, which is exactly why the pane
reads correctly to anyone looking at the file, and why an earlier Chromium measurement of
`bdg()` on the local file came back clean.

---

## What was NOT wrong

Recorded so neither costs anyone a second look.

- **`#pane-index` is closed.** It opens at 808 and closes at 823, thirteen lines below where
  the first diagnosis looked, after the overlay closes its own tag at 822. Whole-file tag
  sweep clean. Adding a `</div>` would have *unbalanced* a balanced tree.
- **`bdg()` and the 115 `#pane-index` rules are correct.** Measured: font-size 10px,
  display inline-flex, padding 2px 8px, border 1px solid. Every scoped property applies.
- **The seven duplicated function names are not a collision.** `cls`, `tick`, `fmtDate`,
  `rOpen`, `rClosed`, `render`, `esc` sit in separate IIFEs (4474–5484, 5488–5705, 5737–5885).
- **Neither external component ships any CSS.** `index_tape_card.js` (252 lines),
  `pcr_trend_card.js` (196) and `scrub_layer.js` (142, which both mount through) contain no
  `<style>`, no `document.head` write, no `insertRule`. Every style they emit is inline and on
  their own elements (`.sit-wrap`, `.pcr-wrap`, `.spark`). `ScorrPcrTrend` renders **no badge
  and no verdict at all** — only the sparkline SVG into `[data-pcr]` slots.

---

## The fix

`scorr_theme_r5.css` — point the rule at the class the mood word actually uses, and drop the
bare selector that was hitting containers.

```css
:root:root:not([data-theme="light"]) .hero-mood{ … }
:root:root:not([data-theme="light"]) .hero-mood::after{ … }
```

Nothing in `v8_dashboard.html` was touched by this half. `bdg()`, the `#pane-index .bdg` rule,
the div structure, every `r*()` render function, the cc#1060 build-then-assign pattern and the
cc#1061 null-honest PCR logic are all unchanged.

---

## Measured — every element on the pane, before and after

Chromium 1440×900, `data-theme="dark"`, the real markup the pane's render functions emit,
same file both runs, with and without the sheet.

| element | font-size | family | text-transform | transform |
|---|---|---|---|---|
| `.mood` **before fix** | 16px → **56px** | Sora → Archivo Black | none → **uppercase** | none → skew |
| `.mood` **after fix** | 16px → 16px | Sora → Space Grotesk | none → none | none → none |
| `.mood-gate` | 14px → 14px | — | none | none |
| `.mc .l` | 9px → 9px | — | none | none |
| `.mc .v` | 11px → 11px | — | none | none |
| `.bdg` | 10px → 10px | — | none | none |
| `.bdg.sm` | 9px → 9px | — | none | none |
| `.card` | 16px → 16px | — | none | none |
| `.card-hd .t` | 10px → 10px | — | none | none |
| `.big-val` | 26px → 26px | — | none | none |
| `.td-mono` | 16px → 16px | — | none | none |
| `.row .pv` / `.row .pc` | 16px → 16px | — | none | none |

`display` and `padding` are byte-identical on every row, before and after, with and without
the sheet. No layout value, colour, card order or copy changed.

And the rule now reaches its intended target for the first time — `.hero-mood` measures 56px
Archivo Black uppercase skewed, while `/m/v8`'s `.mood` card is back to 16px with its `.mv`
word at 19px.

---

## Still open, and deliberately not changed

R5 legitimately restyles the whole app, and eight other pane classes are inside that by
design, not by accident:

| class | what R5 does |
|---|---|
| `.bdg`, `.card` | `border-radius` → 0 with a cut-corner `clip-path` (`.bdg` named explicitly at line 253 with `!important`) |
| `.card` | border and background re-pointed to the R5 edge/panel tokens |
| `.card-hd .t` | Sora → Archivo Black |
| `.td-mono`, `.big-val`, `.row .pv`, `.row .pc`, `.mc .v` | → JetBrains Mono |

These are the R5 look landing on 30+ pages as intended. They are a change, but not a defect,
and squaring a badge corner is not "enormous washed-out text". Exempting the pane from them
would be a design decision, so it is not made here — it is asked in the room.

---

## Also shipped under this thread

**`5aa7929`** — `#v10ChartOv` moved out of `#pane-index` to sit after it as a sibling.
It is `position:fixed; inset:0; z-index:9998` but sat inside a `display:none` parent, and a
`display:none` ancestor removes the whole subtree from the box tree — so the fullscreen V10
chart could not render at all, whatever `openV10Chart()` set on it. Introduced by `a111962`.

Moved rather than re-closed, for the reason above. Measured with the pane hidden and the
overlay set to `display:flex`:

| | parent | box |
|---|---|---|
| before | `#pane-index` | 0×0, not rendered |
| after | the tab wrap | 1280×720, rendered |

Tag balance identical to HEAD. The single stray `</body>` at 5925 is pre-existing and is not a
defect — this file has no `<head>`/`<body>` open tags by design.

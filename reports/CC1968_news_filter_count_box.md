# cc#1968 — Live News category filter: drop the count background box

Founder ask, 10-Sep 17:19: in the Live News category filter menu, remove the background box
colour from the count, just show the text.

## Surface (not assumed — traced)

`/m/digest` → `v10_page_endpoints.py:79` → `scorr_digest_mobile.html` — the same file cc#1966
already established for this page (Daily Digest, old five-slot nav, per that card's own note).
The filter menu itself is built by `nwFilterMenuHtml()` in that file, opened from the gear icon
next to the POLISH/RAW tabs. Each row renders `<button class="nwfitem…">CATEGORY
<span class="n">COUNT</span></button>`.

## Root cause (read from the CSS, not guessed)

The count span carries `class="n"`. A **completely unrelated component**, four lines from the
top of this file's `<style>` block, also uses the bare class `.n` — one of four state-modifier
classes (`.w`/`.l`/`.n`/`.mu`) meant to colour a `.pchip` position badge win/loss/neutral/muted:

```
.n{border-color:#5A4218;background:#241D0E;color:var(--amber)}
```

Because CSS class names are global, that neutral-badge rule's `background` and `border-color`
apply to **any** element carrying `class="n"` — including the filter menu's count span, which was
never meant to be a badge at all. `.nwfitem .n{color:var(--muted)}` (two classes, higher
specificity) already overrides the *colour* — which is why the digits read muted grey, not amber
— but it never declared `background` or `border-color`, so the raw `#241D0E` fill and `#5A4218`
border-color from the unrelated neutral badge survive underneath, unstyled by anything this menu
actually intended. **That fill is the box the founder saw.** (No `border-radius` is set on either
rule, so it renders as a sharp-cornered patch of colour, not a pill — the founder's screenshot
read it as "rounded," which is a reasonable eyeball read of a small filled rectangle next to
rounded neighbours, not a sign there's a second box elsewhere.)

## Fix (verify item B — removed declarations / added rule)

```diff
-.nwfitem .n{color:var(--muted)}
+.nwfitem .n{color:var(--muted);background:none;border:0}
 .nwfitem.on .n{color:var(--aqua)}
```

`background:none` and `border:0` stop the leak at the one place it manifests. The shared
`.pchip .n` neutral-badge component (do_not_touch — it's a different, correctly-used badge
elsewhere) is untouched; nothing was renamed. The count's colour rule was already present
(`var(--muted)`, a contract token) and needed no change — item 2 of the card was already
satisfied, the box was the only defect.

## Resolved count colour (verify item C)

`--muted` is unchanged by this card — reading its existing per-set value:

| Set | `--muted` (the count's colour, before and after — only the box is new/removed) |
|---|---|
| goldnight (dark, default) | `#8E8A7E` |
| aquawhite (light) | `#46585F` |
| silvergold (light) | `#5B6170` |

All three are legible secondary-text greys against `var(--panel)` (the menu's own background,
`.nwfmenu{background:var(--panel)…}`) — the row-selected state (`.nwfitem.on`) still lifts both
label and count to `var(--aqua)`/`var(--hi)`, unchanged (scope item 3 — the row highlight stays,
its shade is cc#1967's territory, not this card's).

## Alignment / row height (verify item D)

Unaffected. The count span carried no padding, radius or explicit sizing of its own before this
change (`.pchip`'s box-model properties require the `.pchip` class, which this span never had) —
only a background colour and an inert border-color (no `border-style`, so no border ever actually
rendered). Removing `background`/`border` changes paint only, not layout. `.nwfitem` itself
(`display:flex;justify-content:space-between`) is untouched, so the count stays right-aligned in
its column and row height is unchanged.

## theme_validate (verify item E) — pre-existing debt, stated plainly, not this card's

`theme_validate` on `scorr_digest_mobile.html`: **11 raw declarations vs baseline 9 — flagged as
a file-level regression by the ratchet, both before and after this edit.** The detail listing
confirms none of the 11 flagged rows touch `.nwfitem .n`, `.nwfitem`, or `.nwfmenu`'s colour
property — this card's specific change added zero raw primitives. The +2 over baseline is
pre-existing: `scorr_digest_mobile.html` landed at 11 (not 9) after cc#1966's own push, per that
card's own logged note. This card did not create the debt and did not fix it — fixing the other
8 unrelated raw literals (mostly `rgba(...)` shadows and hardcoded `font-size` values in
`.plab`/`.pval`/`.pdaylbl`/`.nwchip.sent`) is outside this card's scope (removing a box, not a
theme-token sweep of the whole file) and is flagged here rather than silently absorbed into this
push.

## Not done

`.nwfmenu`'s box-shadow, `.rsheet`'s box-shadow, and the small hardcoded `font-size` values
elsewhere in this file are untouched — none of them are the box the founder pointed at, and
fixing them is a separate, larger token-cleanup card. CC cannot render the app — this is a
static/token claim, not a visual one; the founder confirms on glass.

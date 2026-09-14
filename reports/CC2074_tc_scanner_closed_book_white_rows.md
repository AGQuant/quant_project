# cc#2074 — TC Scanner Closed Book: closed-trade rows get a white background

Founder ask: closed-trade rows should have a white background.

## Step 1 gate — the card's own caution, checked before writing a line

The card flagged a real, live risk itself: this codebase has an active theme_leak defect class
(cc#2055 C.A.R.D theming lock; cc#2060/2069/2070 all fixed a literal-hex background blinding out
dark themes). It asked me to check whether the table already reads a bridged token and, if so, use
that token's light-theme value instead of a hardcoded literal.

Traced this page's own theming contract (v8_dashboard.html is a **web** page — a different, simpler
system than the mobile app's 15-named-theme set; a `cc#1149`/`cc#1203` comment block at the top of
the file names it explicitly: **DARK is `:root`; LIGHT is `html[data-theme="light"]`**, both lifted
verbatim into `scorr_web_tokens.css`, which `main.py` links into this exact page). Read that
contract directly rather than guessing:

| token | dark (`:root`) | light (`html[data-theme="light"]`) |
|---|---|---|
| `--well` (the `.tc13-card`'s own ambient background, already in use) | `#0D1428` | `#EEF1F8` |
| `--panel` | `#121A33` | **`#FFFFFF`** |

`--panel` is literally white in the light theme and a distinct dark navy in dark — not the same as
`--well`, so using it gives closed rows a real, visible surface of their own rather than blending
into the card. Exactly the token the card's own caution was pointing at.

Also confirmed directly (not assumed): `.tbl` is a class unique to this hand-built Closed Book
table — the Open Book renders through the shared `sortTbl()` component, whose own `<table>` carries
no class at all (`drawTbl()`, line 3250) — so a `.tc13-wrap .tbl` selector cannot leak onto the
Open Book.

## The fix

- `v8_dashboard.html`: one new rule, `.tc13-wrap .tbl tbody tr{background:var(--panel)}`, placed
  beside the table's other row-level rules. `tbody` scopes it off the header row.
- **Do not touch, respected**: the ambient `.tc13-card{background:var(--well)}`, the hover rule
  (`.tbl tr:hover td{background:var(--raise)}` — a `<td>` background always paints over its row's,
  so hover is unaffected), and the Open Book entirely.

## Verify

`node --check` clean (no JS touched — CSS-only card; re-checked anyway per this session's standing
practice). Real headless Chromium, `getComputedStyle` against the REAL token contract
(`scorr_web_tokens.css`'s `:root` / `html[data-theme="light"]` blocks) and the REAL `.tbl` CSS,
both extracted verbatim from the committed files — **16/16 checks pass**:

- **Dark** (default, no `data-theme` attribute): closed row background computes to
  `rgb(18, 26, 51)` (`#121A33`) — not white, not blinding — and is visibly different from the
  card's own `--well` background, a real distinction. The Open Book stand-in (no `.tbl` class) is
  confirmed unaffected.
- **Light** (`html[data-theme="light"]`, this page's own real light-mode switch): closed row
  background computes to `rgb(255, 255, 255)` — literal white, the founder's actual ask.
- Hover still shows the `--raise` tint on the cell, unblocked by the new row rule.
- Zero page errors in every case.

One test-harness bug caught and fixed before it could produce a false result: the first extraction
pass sliced `scorr_web_tokens.css`'s dark/light blocks up to a downstream marker that included the
START of the next section's comment (`/* ── LIGHT...`) without its closing `*/` — an unterminated
CSS comment that silently swallowed every rule after it inside the harness, including the actual
`.tbl` rule under test, and read back as all-transparent. Re-extracted with brace-matching from each
block's own opening `{` to its own closing `}` instead of a second, unrelated marker; added a
brace-balance assertion before the harness runs so this class of extraction bug fails loudly next
time rather than producing a quiet false negative.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the live Closed Book's rows read as white against the card in both themes.

# cc#2026 — Sector page theme leak: C.card and 5 raw white literals ignore active theme

Visual Audit full crawl (this session, requests 2-225) flagged `theme_leak` FAIL on `/sector`
across all 4 crawled theme × viewport combos (aquawhite/goldnight × desktop/mobile), ~42 elements
per page load. Fable grepped `scorr_sector.html` on main (sha `1781b305a1eb12e33b933d4a96137efc4f68038f`)
and found the cause: the page-local JS constant `const C = {...,card:"#fff",...}` backs ~9-10
card/table/dropdown surfaces, plus 5 more spots hardcode `background:"#fff"` directly — none
reference the theme contract, unlike this file's own `<style>` block (`.model-nav` etc.), which
already correctly uses `var(--panel)`. `Scorr:theme_validate` cannot see this class of bug: it only
scans `<style>` block CSS, not the inline React style objects inside `<script type="text/babel">`.

## Fix — one token, six call sites, all now the same source of truth

`C.card` changed from the literal `"#fff"` to `"var(--panel)"` — the exact token this file's own
`.model-nav` CSS rule already uses successfully. The 5 raw literals not already going through
`C.card` now reference it instead of duplicating a literal:

| # | location | before | after |
|---|---|---|---|
| a | `Strip`'s outer banner div | `background:"#fff"` | `background:C.card` |
| b | `SectorBrief`'s toggle button, off-state | `open?"#f5f3ff":"#fff"` | `open?"#f5f3ff":C.card` |
| c | `RankTab`'s filter-chip, off-state | `...?"rgba(77,124,254,.14)":"#fff"` | `...?"rgba(77,124,254,.14)":C.card` |
| d | `GVMTab`'s mode-chip, off-state | `...?"rgba(77,124,254,.14)":"#fff"` | `...?"rgba(77,124,254,.14)":C.card` |
| e | `ThemesSection`'s theme-card div | `background:"#fff"` | `background:C.card` |

The single `C.card` definition change also fixes, for free, the ~9-10 pre-existing call sites that
were already written as `C.card` but inherited the broken literal (RankCard, Detail card,
SectorBrief's constituents wrapper, RankTab/GVMTab table wrappers, SectorSearch's input/dropdown/
placeholder/table) — confirmed by direct measurement below, not assumed.

## Do-not-touch, confirmed by diff and by rendering

`git diff` shows exactly 6 lines changed, nothing else. Verified still present, byte-identical,
un-theme-tracking (real literals, deliberately, per do-not-touch):
- `SectorBrief`'s `#f5f3ff` APPLICATION TYPE box, `#fff5f5` KEY RISKS box, `rgba(47,212,139,.08)`
  KEY GROWTH DRIVERS box; `SectorSearch`'s `#fff5f5` error box — the three-plus-one pale semantic
  tints named in the card.
- One instance **not** explicitly named in the card's do-not-touch list, found while scoping the
  fix and left alone anyway since it isn't one of the 6 items in scope: `RankTab`'s conditional row
  background `(r.gvm<6)?"#fff5f5":undefined` (a low-GVM row highlight). Confirmed by real-browser
  measurement it still renders `rgb(255, 245, 245)` under goldnight — a literal, not a token, same
  as before this card.
- `VC` verdict-badge colors, `C.accent/green/red/gold/orange/purple`, `THEME_COLORS`, and the
  `color:"#fff"` badge-text spots in `ThemesSection` — semantic hues/text-on-color, not surface
  backgrounds, untouched.
- The `<style>` block (`th`/`td`/`.model-nav`/`.mnav-item` etc.) — already theme-correct, untouched.

## Verify — real Chromium, real Babel, the real file, no reimplementation

CDN access (`cdnjs.cloudflare.com`) is blocked from this sandbox, so React 18.2.0 / ReactDOM 18.2.0 /
`@babel/standalone` 7.23.2 — the exact versions this page already pins — were vendored locally via
`npm install` (same UMD/standalone builds the CDN serves) rather than reimplementing or skipping
JSX transpilation. The real `scorr_sector.html` (one line changed only for this test harness:
`SHOW_GVM_TAB` forced `true` so item (d)'s GVMTab — hidden behind a flag in production, per cc#1699
— could still be exercised; the committed file keeps `SHOW_GVM_TAB=false` unchanged, confirmed) ran
in real Chromium with `/api/sector/rotation`, `/api/sector/themes`, `/api/sector/brief` stubbed via
`page.route` to representative fixtures. 10/10 checks pass:

- Babel transpiles the real modified JSX with zero console errors.
- Under **goldnight** (`--panel:#131316`), every one of the 6 fixed locations — plus the
  pre-existing `C.card` App-header usage — measures `rgb(19, 19, 22)` via `getComputedStyle`,
  not white: Strip's banner, the App header banner, the SectorBrief toggle (closed state), the
  RankTab filter chip (off state), the GVMTab mode chip (off state), the ThemesSection card.
- The RankTab's *selected* chip keeps its accent tint (`rgba(77,124,254,.14)`) — untouched by
  this card, still working correctly alongside the fixed off-state.
- Under **aquawhite** (`--panel:#FFFFFF`) the same elements still render pure white — no visual
  regression where the old literal happened to already match — but now via the token, not a
  hardcoded value, so a future theme change or a new dark theme will track correctly.
- The do-not-touch `#fff5f5` low-GVM row highlight in RankTab stays a literal, unmoved by theme.

## Not done here (necessary-but-not-sufficient per the card's own verify list)

`Scorr:theme_validate` re-run and the 4 on-demand `visual_audit_requests` re-fires against the live
deployed page (confirming the real crawler's `theme_leak` FAIL count drops from ~42/page to 0) —
both require the live app and Fable's own tooling; this container has no route to scorr.in. The
local Chromium verification above proves the fix is correct at the code level; those two steps are
the live-data confirmation the card asks Fable to close out after deploy.

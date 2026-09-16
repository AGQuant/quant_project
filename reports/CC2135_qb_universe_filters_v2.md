# cc#2135 — QB Universe page: compact pools, collapsible tick-to-apply filters, per-row AND/OR

## What shipped
- **`scorr_qb_universe.html`** (display + interaction, per the design ref
  `design_refs/scorr_qb_universe_step_R1.html` — Pool + Filters + result bar only; the stepper is
  cc#2132's):
  - Pool rows are one line each: radio · name · (stale badge only when the rank is stale) · a
    tight 9.5px single-line caption carrying the full note text (cc#2123's gap note, cc#2125's
    "ranked as of 2026-09-16 (screener CSV load)") · count. Nothing removed from the caption; it
    ellipsizes on narrow screens and the full text is the tooltip. Measured: every row 28px tall.
  - The six GVM/component filters are fixed rows, collapsed by default; `+` expands one row in
    place (min/max inputs, or the four real verdict checkboxes — the old option list said "Poor",
    the real `gvm_scores.verdict` values are Excellent/Good/Average/Weak, checked live, fixed).
  - The "note — why this gate" free-text field is gone.
  - Each row has an **Apply** tick. Once ticked, the row shows a live pass-count for THAT filter
    alone against the selected pool ("56 of 490 pass") and reads `applied` on the right.
  - Each row has its **own AND/OR toggle in the middle of the row** (between the name and the
    status). Default AND.
  - A result bar "Universe after filters: N stocks of M in <pool>" recomputes live on every tick,
    untick, value change, toggle change or pool change; the line under it prints the expression
    the server evaluated (e.g. `((gvm OR verdict) AND seg_rank)`) and the binding filter. Clicking
    the number reveals the matching-stocks table (dynamic columns, as before).
- **`qb_universe_builder.py`** — `/api/qb/universe2/preview`, additive:
  - Conditions are built **per filter row** (a min and a max on one score are one filter, ANDed
    inside their own parentheses).
  - `per_filter_counts`: each applied filter alone against the pool — a real query per row, same
    CTE as the combined count (the endpoint's binding-filter loop already ran these; it only
    returned the minimum). `binding_filter` is now the smallest of them (keyed by filter, e.g.
    `verdict`, where it used to be keyed per bound, e.g. `gvm_min`).
  - New `ops` parameter (`verdict:OR,seg_rank:AND`): each applied filter's own AND/OR. Rows are
    combined **left-to-right in the page's fixed row order** (`_FILTER_ORDER`, one constant, the
    same order the page renders) with explicit parentheses at every step: `((f1 OP f2) OP f3)`.
    The first applied row's flag has nothing to its left and is ignored. Omitting `ops` is AND
    everywhere — byte-for-byte what the endpoint did before, so nothing else that calls it changes.
  - Also returns `filter_order`, `ops` (as resolved) and `expression` (readable) so the page never
    has to guess how the server combined things. Every count on the page is the server's; the
    page does no arithmetic (data-honesty doctrine).

## The combine assumption, flagged as the spec asked
Mixed AND/OR rows are evaluated **left-to-right, top-to-bottom, with explicit parentheses at each
step** — `((gvm OR verdict) AND seg_rank)`, not SQL's native precedence (which would read it as
`gvm OR (verdict AND seg_rank)`). This is the spec's own "simplest defensible default". If the
founder wants standard precedence, or AND-groups-then-OR, it is one function (`_combine`) to
change; the page's expression line will show whichever rule is live. Corrections welcome.

## Verify
- `ast.parse` + `py_compile` clean; `node --check` clean on the page's inline script; zero
  `var(--x, #literal)` fallbacks in the page (the theme ratchet's three current regressions —
  `mobile/v8.html`, `pwa_endpoints.py`, `scorr_bell.js` — are other files, not touched here,
  stated so nobody reads them as this card's). The page itself has no baseline yet ("unmeasured").
- `_parse_ops` / `_combine` unit-tested on the verbatim-extracted functions: the SQL produced for
  `gvm:AND,verdict:OR,seg_rank:AND` is exactly
  `(((gvm_score >= …) OR (UPPER(verdict) = ANY(…))) AND (seg_rank <= …))`, readable text
  `((gvm OR verdict) AND seg_rank)`; empty ops → all AND; single row → bare condition.
- **Real counts, measured live with the same CTE + WHERE the endpoint builds** (pool Nifty 500 at
  the latest rank_date, gvm_scores as of 2026-09-15): pool 490 scored; gvm ≥ 7.5 alone → **56**;
  verdict = Excellent alone → **14**; seg_rank ≤ 3 alone → **229**; combined
  `((gvm OR verdict) AND seg_rank)` → **55**; all-AND → 14. Pools: large 100/100, mid 150/147,
  small 750/734, micro 865/789, F&O 208/205, Nifty 500 500/490.
- **Real-data Playwright harness** (page verbatim, only `fetchWithTimeout` stubbed to return the
  numbers above, exactly the cc#2123 method): 6 pool rows, all single-line (28px), five carry
  "ranked as of 2026-09-16", no stale badge (rank is current), no note lines; 6 filter rows
  collapsed, no free-text input, an AND/OR toggle in every row; `+` expands GVM in place; Apply →
  `applied` + "56 of 490 pass" + result 56; Verdict set to OR + Excellent + Apply; Rank ≤ 3 +
  Apply → result **55**, expression line `((gvm OR verdict) AND seg_rank)`, per-row 14/229,
  binding "Verdict alone leaves 14", "3 active of 6"; the query the page actually sent carries
  `ops=gvm:AND,verdict:OR,seg_rank:AND` in page order; clicking the count reveals 6 real rows
  (CHENNPETRO first) with the 3 applied columns; unticking drops to 2 active. 375px: no errors,
  no horizontal overflow, pass-count renders. Zero page errors throughout.
- **VISUAL_VERIFY_GATE_V1** — three screenshots looked at: desktop initial (compact pools, six
  collapsed rows, empty result bar "pick a pool"), desktop with three filters applied (as above,
  expanded rows show inputs + tick + green pass-count, toggles read AND/OR/AND), mobile 375px
  (captions ellipsize, toggles and status fit on one line; only "Rank within Segment" wraps its
  label to two lines — legible, no overflow).

## Not done / not touched
Pool definitions, counts and staleness logic (just cut over by cc#2125) — untouched. gvm_scores
computation — untouched. Entry/Exit/Risk/Backtest/Deploy, the stepper and nav — cc#2132. The six
COMING categories stay COMING. The old "Run preview" button is gone: counts are live now and the
table is one click on the result number, so a separate run step had nothing left to do.

# APP QA R7 RESULTS — cc#1090 Sprint 4, the card depth system

Twelve pushes, 18-Aug-2026. One shell class in the shared sheet, then eleven surfaces adopting it.
Every number below was measured in Chromium on a magenta control page or read from the database.
Nothing here is an impression.

---

## The pushes

| Push | SHA | Files | Verify output | Notes |
|---|---|---|---|---|
| P0 · prototype route | `1806ec4` | main.py · mobile_cards_endpoints.py · scorr_cards_preview.html | route serves both shells + one slant deck card on real GVM ladder data | ships FIRST so the press can be felt on glass. Not a nav page — 2987 does not apply |
| P1 · shell class | `4837d95` | scorr_theme_r5.css | `.slab` / `:active` / `.dense` / `.dense:active` / `.well` defined once | every `var(--token)` carries a literal fallback. One undefined var in a box-shadow list invalidates the WHOLE declaration and the ledge silently becomes `none` |
| P11 · floating pills | `dea2d06` | scorr_theme_r5.css | pills move to top 6px/40px under 768px, z-index 70 | first version was a no-op: main.py injects the pills with their own in-body `<style>`, which won on order at equal specificity. Fixed by doubling the id — `#scorr-lo#scorr-lo` at (2,0,0) |
| P1 revision · ruling B | `ad4a659` | scorr_theme_r5.css | tokens declared ON `.slab, .pressed`, not at `:root` | scoping is the ruling's condition 1 — a page changes colour when it takes a shell and not before, so cc#1073 is not swallowed |
| founder rulings 1+2 | `7527202` | scorr_theme_r5.css · scorr_themes.css | `clip-path:none` on adopting slabs; 3 tokens added to GOLD NIGHT | ruling named 7 tokens, 3 added. `--gold`, `--well`, `--line`, `--chalk` already resolve on a goldnight page, so adding them would OVERRIDE not ADD — stopped and logged, upheld by Fable in RECO 2747 |
| P2 · home | `6d3efd8` | mobile/home.html · scorr_theme_r5.css | adopted `.c slab` bg 23,23,27 · border 42,42,49 · radius 13 · clip none · ledge 122,99,32 at +2 and +4, page magenta at +9 | the override had to be extended to carry background and border too. Without it the result was the OLD navy card with a gold ledge bolted underneath — a diff claiming adoption with a half-adopted result |
| P3 · gvm + fight card | `b7b752b` | mobile/gvm.html · scorr_gvm_fightcard.html · scorr_theme_r5.css | rep radius 13 · rows radius 11 · fight card 7 blocks radius 13, ledge 122,99,32, magenta at +9 | radius needed `!important` and only the radius: section 3 already carried `.card{border-radius:0 !important}`, so the fight card came out ledge-correct but SQUARE |
| P4 · digest + news | `df9c72c` | mobile/intel.html · scorr_digest_mobile.html | 10 of 12 cards adopted · GLOBAL TAPE + INTERNALS stay navy 22,31,51 with the polygon intact | intel news took `.dense` — 40 items a page |
| P5 · check/models/qb | `c86ad8d` | mobile/check.html · mobile/models.html · mobile/qb.html | verdict radius 11 3px ledge · model tile radius 13 5px ledge · basket radius 13 5px ledge | dense decided from real counts: check 20 default, models 8 registered / 6 public, qb 6 open baskets |
| P6 · tools + stat blocks | `70cf2e7` | mobile/home.html · scorr_digest_mobile.html · scorr_gvm_fightcard.html · scorr_theme_r5.css | tile well 12,12,15 radius 8 clip none shadow `0 2px 0 #000` · controls keep their polygons | `.well` needed the same cut release the slab got — a clip-path erases the sunk shadow exactly as it erased the ledge |
| RECO 2747 item 4 | `4c667f4` | scorr_theme_r5.css | `.c .pressed` / `.m .pressed` / `.card .pressed` all read 22,31,51 with the polygon BEFORE; all read 12,12,15 / #000 / 12px / clip none AFTER | a trap armed for the first page that combines them, not a live bug. Only `.pressed` in the repo is scorr_cards_preview.html:122, standing alone |
| P7 · dense sweep | `835ee2e` | mobile/positions.html · results.html · screeners.html · sector.html · trade_wall.html | all five bg 23,23,27 · radius 11 · clip none · pad 10/13 · ledge 122,99,32 at 3px | counts read from the database, not estimated |
| P8 · this file | — | reports/APP_QA_R7_RESULTS.md · scorr_digest_v3.html | web `/digest` DAY label restored to 28,39,64 · radius 0 · no ledge | the audit found a live collision — see below |

---

## P7 audit — every list surface in the routed mobile app

**Twelve surfaces render 8 or more rows.** Nine took `.slab.dense`:

| Surface | Page | Rows | Source of the count |
|---|---|---|---|
| `.g-lrow` leaderboard | /m/gvm | 25 | endpoint default, 50 max |
| `.c` news card | /m/intel | 40 | endpoint default, 100 max |
| `.v` verdict | /m/check | 20 | endpoint default, 50 max |
| `.tool` tile | /m/home | 16 | counted in the file |
| `.c` position | /m/positions | 21 | `v8_paper_positions` status OPEN |
| `.c` stock | /m/screeners | 3–152 | `v13_screen_results`, 5 of 7 screens over the line |
| `.se-srow` rating | /m/sector | 130 | `sector_ratings` |
| `.tw-card` event | /m/trades | 40 per page | page LIMIT, infinite scroll |
| `.c ref` day card | /m/results | 10 | the page asks `days=10` |

**Three did NOT, and the reason is the nesting rule rather than an oversight.** Each renders inside a
card that already adopted, and slab-in-slab is the one thing the card forbids outright:

| Surface | Rows | Sits inside |
|---|---|---|
| `.g-lrow` sector rows | 130 | the `.c slab` sector ratings card |
| `.rrow` company rows | up to 120 | the `.c ref slab` day card |
| `.lrow` ladder rows | up to 12 | the `.card slab` ladder block |

**Under the line and correctly not dense:** models 8 registered / 6 public · qb 6 baskets ·
holdings 3 rows · digest news 4 per block.

**Two surfaces left alone on purpose**, stated rather than skipped quietly:

- **/m/holdings** — 3 rows, under the line, and no page push in the sprint assigned it a shell, so
  it stays flat entirely.
- **/m/v8** — the biggest page in the app and in NO Sprint 4 page push. Its `.lcard` containers
  never adopted, so raising the 7 day rows / 10 funnel baskets / 10 segments inside them would
  produce raised rows in a flat card — the same half-adoption defect caught and fixed in P2.

---

## Nesting audit

Asserted on the **rendered DOM**, not by grepping strings, because every one of these surfaces is
built in JavaScript and a source grep cannot see what nests inside what:

```
slab inside slab        0
well outside a slab     0
pressed inside pressed  0
```

Source-level census of every `slab` / `well` / `pressed` markup site in the repo: **47 sites across
11 files**, all app surfaces plus the prototype page and Fable's preview file. Each was read and
its container identified; the three flat-by-design cases above are the only rows that sit inside an
adopted card.

Blur audit: every `box-shadow` in the `.slab` family has a **0 blur radius**. The moment the ledge
softens it stops being an edge and becomes a drop shadow.

Money colours: **unchanged**. No green or red hex moved anywhere in the sprint. `--rc`, `--vc`,
`bull`/`bear`, the sentiment rails and the model state glyphs all carry meaning the shell has no
business in, and none of them was touched.

---

## What the audit found that the card did not ask for

**A live collision on a WEB page.** `scorr_digest_v3.html`, served at `/digest`, already had its own
`.slab` class — a 52px DAY/WEEK label in the heat strip. P1 put a `.slab` class in the shared sheet,
and main.py injects that sheet into `/digest` as well as the app, so the two names collided.

Measured before the fix: the label computed background 23,23,27, radius 13px, padding 13/15 and a
5px gold ledge inside a 52px box — 30 of its 52 pixels eaten by padding. The web digest is
explicitly **out of Sprint 4's scope**, so the shell reaching it is the bug.

Fixed by renaming the page's local class to `.striplab`. The shared class was NOT narrowed, because
a page could always adopt it deliberately later. Re-measured after: background 28,39,64, radius 0,
no padding, no ledge.

---

## Open with Fable

1. The sector ratings card took `.slab`, not `.pressed`. By the split it holds other content, but
   home P2 already shipped `.c` cards as slabs and GVM looking different from the founder's first
   screen seemed worse. Side effect: rows inside it now compute the same face colour as the card,
   separated by their 1px `--line` border only.
2. `.c.ed` sets the editorial news face to `var(--panel2)` at (0,2,0); the ruling-1 override is
   (0,3,0) and now wins, so an editorial card on /m/intel is the same 23,23,27 as a normal one. Not
   patched, because neither locked ref carries a lighter panel value and inventing a hex is not
   mine to do.
3. The tools grid re-flows one label earlier under the shell — 430px: 2 wrapped labels becomes 3;
   360px: 3 becomes 4. No overflow at any width. Holding the shipped wrap points exactly needs a
   tools-only padding rule, which is a spacing decision.
4. Does /m/v8 and /m/holdings want a P9, or are they deliberately outside the card system?

---

*Founder device review on home, gvm and one dense ladder is the one verify line this file cannot
close — it needs a thumb on glass.*

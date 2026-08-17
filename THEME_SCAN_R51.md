# THEME_SCAN_R51 — app-wide colour scan vs THEME_TOKENS_R51_V1

cc#1072 · read-only diagnostic · scanned at `97629880e871a218a581185b6ba93ebaa8a95081`

Source of truth: **THEME_TOKENS_R51_V1, session_log 23878**. Every token hex below is quoted
from that entry; none is invented here. This file changes no behaviour — it is the input that
gates cc#1073, and cc#1073 stays blocked until the DECIDE rows have a founder ruling.

## Summary

| | |
|---|---|
| Files scanned, FIX universe | **242** (`.html .css .js .py`, git-tracked, excluding `design_refs/` + `previews/`) |
| …of those, carrying at least one colour | **59** |
| Files scanned, reference universe | **41** (`design_refs/` + `previews/`, listed separately, never rewritten) |
| Unique colour values, FIX universe | **719** |
| Total occurrences | **3103** |
| Rows in the per-file tables (file × colour) | **1713** |
| OK | **58** |
| REPLACE | **1501** |
| DECIDE | **154** |

Consistency: 58 + 1501 + 154 = **1713** rows, and the per-file occurrence counts sum to **3103**. Both are recomputed from the same table that generated this file, so the header cannot drift from the body.

### How each verdict is decided — meaning first, distance only as a tiebreak

A pure nearest-colour pass was written first and thrown away, and it is worth saying why,
because the failure is not obvious. Measured by distance, every mid-green in the app —
`#2FD48B`, `#0A9E63`, `#34D399`, the up/gain colour on every dashboard — maps to **`--aqua`**,
because R51's positive token is a yellow-green and the palette holds no mid-green at all. That
is numerically correct and semantically backwards, and cc#1073 would have acted on it. So the
order below is: what the colour MEANS, then which token inside that meaning is nearest.

1. **OK** — the value already IS a canonical token.
2. **The live theme's own map wins.** If a colour is declared as a legacy token name that
   `scorr_theme_r5.css` already re-points (`--grn` → volt, `--blu` → pulse, `--panel` → panel,
   and 34 others), that mapping is the answer. It is not invented here — it is shipped and in
   production today, which makes it the strongest authority available on legacy intent.
3. **Otherwise, hue family** — red → `--heat`, orange → `--amber`, green → `--volt`, cyan →
   `--aqua`, blue/violet → `--pulse`, low-chroma → the surface/text ramp by lightness.
4. **DECIDE** where a family genuinely straddles a boundary (teal between green and cyan,
   yellow between amber and volt), where no token exists (magenta), where a grey lands more
   than 14 ΔE from every rung of the ramp, or where 23878 names the value itself.

Distances are CIE ΔE76 in Lab, not RGB — RGB distance calls `#FFFFFF` and `#EAF0FA` nearly the
same colour when the eye does not.

## The canonical tokens (session_log 23878, verbatim)

| token | hex | meaning |
|---|---|---|
| `--field` | `#0D1322` | page background, petrol |
| `--panel` | `#161F33` | card |
| `--panel-hi` | `#1C2740` | nested / raised |
| `--edge` | `#26334F` | borders |
| `--chalk` | `#EAF0FA` | primary text + mood words |
| `--muted` | `#8A97B0` | secondary text |
| `--volt` | `#C8F542` | WIN / LONG / ACTIVE tab / pass |
| `--heat` | `#FF4D6D` | LOSS / SHORT / fail / bearish rail |
| `--amber` | `#FF9F45` | NEUTRAL / CAUTION / momentum pillar |
| `--aqua` | `#35E0FF` | structure: headings, tools grid, inactive nav, eyebrows |
| `--pulse` | `#7C5CFF` | INTERACTIVE ONLY: buttons, links, tappable affordances |

Semantic rules from the same entry: colour carries meaning, never decoration — volt =
positive/active, heat = negative, amber = neutral/caution, aqua = structure, pulse = press-me,
chalk = facts. Mood words are always chalk; sentiment lives on the state rail. Active nav tab is
always volt with the notch; inactive nav is aqua.

## DECIDE — the rulings cc#1073 is waiting on

Grouped by colour rather than by file, because the ruling is per colour and each one below is a
single decision that then applies everywhere it appears.

### DECIDE-0 · There is no tint scale in R51, and the app leans on tints heavily

**726 translucent values across 52 files, 1001 occurrences.** R51 names 11 tokens and every one of
them is opaque, so strictly all of these are unmapped. Filing them as 726 separate DECIDE rows
would bury the real decision, which is a single one: *what is the tint scale?* The alphas the
code already leans on are `0.14` (103 rows), `0.12` (63 rows), `0.10` (44 rows), `0.40` (32 rows), `0.06` (30 rows), `0.13` (30 rows).

Recommendation, so this is a yes/no rather than a design exercise: adopt three steps — `.08`
wash, `.16` tint, `.28` edge — and express every translucent value as one of them over its
nearest token. That turns all 726 rows into mechanical replacements. Until it is ruled on they
are marked REPLACE with the target named as a tint, and the scale is the open item.

### DECIDE-1 · `#4DD8FF` — fold into `--aqua` or keep a distinct V-pillar blue

Named explicitly in 23878 as the consolidation candidate. Every occurrence:

| file | count | lines | context |
|---|---|---|---|
| `scorr_gvm_fightcard.html` | 5 | 11, 209, 210 | `--volt:#C8F542;--heat:#FF4D6D;--pulse:#7C5CFF;--cut:16px;--g:#C8F542;-` |

ΔE to `--aqua` #35E0FF is 7.3 — visually the same colour at a glance. It is confined to ONE
file, the GVM fight card, where it is the V (value) pillar. So this is cheap either way: fold
it and the fight card loses a pillar colour it may need to stay readable against G and M;
keep it and aqua has a near-twin that will be re-introduced by anyone copying that card.

### DECIDE-2 · the cyan family still routed to `--pulse` — the theme map predates `--aqua`

`scorr_theme_r5.css` folds `--cyan` into pulse, with the comment "R5 has one interactive
colour, not two". That was true when it was written: the palette had no structure colour.
23878 then added `--aqua`, and cc#1071 put it on the nav and the tools grid. These values are
cyan by hue and are only reaching pulse through that older line — `#37D3E8` is 7.0 ΔE from
aqua and 105.1 from pulse, so the current map turns a teal into a purple. Ruling: send the
cyan family to `--aqua`, or say these particular surfaces are interactive and keep them.

**7 values, 61 occurrences, 24 files — ONE ruling covers all of them.**

Candidates: **`--aqua`, or keep them interactive**. No suggestion column — this table exists to ASK, and the raw
nearest-distance answer is exactly the one that is unreliable here.

| colour | occurrences | files | lives in |
|---|---:|---:|---|
| `#37D3E8` | 34 | 21 | `fpc_v11.html +20 more` |
| `#3AA0FF` | 11 | 2 | `mobile_endpoints.py +1 more` |
| `#0891B2` | 7 | 5 | `scorr_cio_dashboard.html +4 more` |
| `rgba(55,211,232) @0.14` | 6 | 5 | `pwa_endpoints.py +4 more` |
| `#0FA8C4` | 1 | 1 | `pwa_endpoints.py` |
| `rgba(15,168,196) @0.12` | 1 | 1 | `pwa_endpoints.py` |
| `#0E8FA8` | 1 | 1 | `scorr_v15.html` |

### DECIDE-3 · values doing two jobs — the same colour declared under names that map differently

These are declared in one place under a legacy name that becomes one token, and in another
place under a name that becomes a different one. Almost all of it is the WEB light theme
meeting the dark app: `#FFFFFF` is a card surface under `--card` in a light block and plain
white text everywhere else. Nothing can map a value to two tokens at once, so the ruling is
whether the light theme keeps its own palette (likely — R51 is a dark system and
`scorr_theme_r5.css` deliberately stays out of light mode) or these values converge.

**3 values, 196 occurrences, 45 files — ONE ruling covers all of them.**

Candidates: **one token each, or split the value**. No suggestion column — this table exists to ASK, and the raw
nearest-distance answer is exactly the one that is unreliable here.

| colour | occurrences | files | lives in |
|---|---:|---:|---|
| `#FFFFFF` | 174 | 45 | `fpc_v11.html +44 more` |
| `#F6F8FB` | 12 | 8 | `pwa_endpoints.py +7 more` |
| `#F4F7FE` | 10 | 4 | `pwa_endpoints.py +3 more` |

### DECIDE-4 · the emerald / teal greens — fold to `--volt`, or add a mid-green token

R51's positive colour is `--volt` #C8F542, a yellow-green. These sit at hue 158–172, between
that and `--aqua`. They matter because most of them are doing the SAME JOB volt does — they
are the up/gain/pass colour on the web dashboards (Tailwind's emerald ramp, brought in
piecemeal). Two ways out: fold them all to `--volt`, which is one mechanical pass and gives
the app one positive colour; or admit a mid-green token, which keeps the dashboards looking
the way they do now and costs a twelfth token. One ruling covers every value below.

**18 values, 47 occurrences, 12 files — ONE ruling covers all of them.**

Candidates: **`--volt` or a new mid-green**. No suggestion column — this table exists to ASK, and the raw
nearest-distance answer is exactly the one that is unreliable here.

| colour | occurrences | files | lives in |
|---|---:|---:|---|
| `#34D399` | 12 | 4 | `galaxy_map.js +3 more` |
| `#065F46` | 5 | 3 | `scorr_performance.html +2 more` |
| `#059669` | 5 | 1 | `scorr_structure.html` |
| `#1A9070` | 4 | 2 | `hr_report_pdf.py +1 more` |
| `rgba(52,211,153) @0.45` | 4 | 2 | `pwa_endpoints.py +1 more` |
| `rgba(29,158,117) @0.08` | 2 | 1 | `fpc_v11.html` |
| `rgba(29,158,117) @0.15` | 2 | 1 | `fpc_v11.html` |
| `rgba(52,211,153) @0.12` | 2 | 1 | `pwa_endpoints.py` |
| `#10B981` | 2 | 2 | `v10_dashboard.html +1 more` |
| `rgba(29,158,117) @0.07` | 1 | 1 | `fpc_v11.html` |
| `rgba(29,158,117) @0.12` | 1 | 1 | `fpc_v11.html` |
| `rgba(52,211,153) @0.60` | 1 | 1 | `galaxy_map.js` |
| `rgba(52,211,153) @0.08` | 1 | 1 | `scorr_digest_v3.html` |
| `rgba(52,211,153) @0.14` | 1 | 1 | `scorr_digest_v3.html` |
| `rgba(52,211,153) @0.18` | 1 | 1 | `scorr_digest_v3.html` |
| `rgba(52,211,153) @0.30` | 1 | 1 | `scorr_digest_v3.html` |
| `rgba(52,211,153) @0.55` | 1 | 1 | `scorr_digest_v3.html` |
| `#066B46` | 1 | 1 | `scorr_news.html` |

### DECIDE-5 · magenta / pink — no token exists for them

R51 has no magenta. Each of these is currently carrying a meaning of its own on some chart or
chip. The ruling is whether that meaning is real (add a token) or whether it collapses into
`--heat` / `--pulse`.

**6 values, 12 occurrences, 9 files — ONE ruling covers all of them.**

Candidates: **a new token, or `--heat`**. No suggestion column — this table exists to ASK, and the raw
nearest-distance answer is exactly the one that is unreliable here.

| colour | occurrences | files | lives in |
|---|---:|---:|---|
| `#5A2634` | 4 | 3 | `scorr_gvm_fightcard.html +2 more` |
| `#DB2777` | 3 | 3 | `scorr_cio_dashboard.html +2 more` |
| `#F472B6` | 2 | 2 | `galaxy_map.js +1 more` |
| `#D4537E` | 1 | 1 | `fpc_v11.html` |
| `rgba(240,120,160) @0.00` | 1 | 1 | `galaxy_map.js` |
| `rgba(240,120,160) @0.50` | 1 | 1 | `galaxy_map.js` |

### DECIDE-6 · the yellows — `--amber` or `--volt`

Hue 45–72 falls between amber #FF9F45 and volt #C8F542. Both are legitimate readings: amber
if the value means caution, volt if it means pass/positive. That is a semantic call, not a
distance one, which is why it is not auto-mapped.

**6 values, 8 occurrences, 6 files — ONE ruling covers all of them.**

Candidates: **`--amber` or `--volt`**. No suggestion column — this table exists to ASK, and the raw
nearest-distance answer is exactly the one that is unreliable here.

| colour | occurrences | files | lives in |
|---|---:|---:|---|
| `rgba(234,179,8) @0.15` | 2 | 1 | `scorr_cio_dashboard.html` |
| `#FDE68A` | 2 | 2 | `scorr_structure.html +1 more` |
| `#FACC15` | 1 | 1 | `galaxy_map.js` |
| `#FCD34D` | 1 | 1 | `quant_basket.html` |
| `#F0DFA6` | 1 | 1 | `scorr_check.html` |
| `#FEF3C7` | 1 | 1 | `scorr_structure.html` |

### DECIDE-7 · greys that sit between the surface steps

The surface ramp (field / panel / panel-hi / edge / muted / chalk) is deliberately tight, so
most greys snap to a step cleanly and were auto-mapped. These are the ones more than 14 ΔE
from every step — they are a step the ramp does not have. Snap to nearest, or the ramp needs
another rung.

**13 values, 20 occurrences, 11 files — ONE ruling covers all of them.**

Candidates: **snap to nearest rung, or add a rung**. No suggestion column — this table exists to ASK, and the raw
nearest-distance answer is exactly the one that is unreliable here.

| colour | occurrences | files | lives in |
|---|---:|---:|---|
| `#18240E` | 4 | 3 | `scorr_gvm_fightcard.html +2 more` |
| `#30363D` | 3 | 1 | `test_cio_endpoints.py` |
| `#475569` | 2 | 1 | `pwa_endpoints.py` |
| `#B8BECC` | 2 | 2 | `scorr_adaptive.html +1 more` |
| `#888780` | 1 | 1 | `fpc_v11.html` |
| `#B3BCCB` | 1 | 1 | `scorr_news.html` |
| `#B3BCCD` | 1 | 1 | `scorr_news.html` |
| `#B9C4D6` | 1 | 1 | `scorr_news.html` |
| `#D6F3E6` | 1 | 1 | `scorr_news.html` |
| `#FDF0E1` | 1 | 1 | `scorr_news.html` |
| `rgba(90,103,129) @0.10` | 1 | 1 | `scorr_news.html` |
| `#B6BDCC` | 1 | 1 | `scorr_structure.html` |
| `#EAF3DE` | 1 | 1 | `v8_dashboard.html` |

**So the whole scan reduces to 8 rulings**: the tint scale, `#4DD8FF`, and the groups above. Everything else
in the tables is mechanical.

## Top 10 most-used off-token colours

| colour | occurrences | files | verdict | maps to |
|---|---:|---:|---|---|
| `#FFFFFF` | 174 | 45 | DECIDE | `--panel` |
| `#4D7CFE` | 132 | 35 | REPLACE | `--pulse` |
| `#2FD48B` | 99 | 32 | REPLACE | `--volt` |
| `#F5B94A` | 83 | 31 | REPLACE | `--amber` |
| `#FF5C6C` | 81 | 32 | REPLACE | `--heat` |
| `rgba(148,166,210) @0.14` | 75 | 30 | REPLACE | `--edge` |
| `#8C99BD` | 61 | 30 | REPLACE | `--muted` |
| `#121A33` | 58 | 27 | REPLACE | `--panel` |
| `#E9EEFB` | 55 | 31 | REPLACE | `--chalk` |
| `#0A0F1E` | 51 | 32 | REPLACE | `--field` |

These ten alone are 869 of the 3103 occurrences (28%) — the legacy palette is small and the
standardisation is mostly a find-and-replace of about a dozen values, not a repaint of 59 files.

## Token-map coverage — legacy `var(--*)` names not yet re-pointed

`scorr_theme_r5.css` re-points **38** legacy names in its map block and defines **15** `--r5-*`
primitives. The names below are used with `var()` somewhere in the FIX universe and are NOT
re-pointed, so on a themed page each falls back to whatever the page's own `:root` says — which
is exactly how a surface stays on the old palette while every other one moves.

| legacy var | var() uses | note |
|---|---:|---|
| `--faint` | 43 | not in the map block |
| `--blue` | 37 | not in the map block |
| `--bull` | 35 | not in the map block |
| `--f-m` | 35 | not in the map block |
| `--tx3` | 34 | not in the map block |
| `--text` | 31 | not in the map block |
| `--font` | 30 | not in the map block |
| `--c-mut` | 26 | not in the map block |
| `--text2` | 24 | not in the map block |
| `--bear` | 23 | not in the map block |
| `--cy` | 20 | not in the map block |
| `--cyan-d` | 19 | not in the map block |
| `--amber-d` | 19 | not in the map block |
| `--red-b` | 18 | not in the map block |
| `--grn-b` | 16 | not in the map block |
| `--paper` | 15 | not in the map block |
| `--amb` | 15 | not in the map block |
| `--accent` | 13 | not in the map block |
| `--amber-b` | 13 | not in the map block |
| `--tx2` | 12 | not in the map block |
| `--gr` | 11 | not in the map block |
| `--grey` | 11 | not in the map block |
| `--radius` | 10 | not in the map block |
| `--c-grn` | 10 | not in the map block |
| `--c-bd` | 9 | not in the map block |
| `--am` | 9 | not in the map block |
| `--purp` | 8 | not in the map block |
| `--c-red` | 8 | not in the map block |
| `--c-panel` | 7 | not in the map block |
| `--tx` | 7 | not in the map block |
| `--border2` | 6 | not in the map block |
| `--green` | 6 | not in the map block |
| `--rc` | 6 | not in the map block |
| `--amber-soft` | 6 | not in the map block |
| `--c-tx` | 6 | not in the map block |
| `--disp` | 5 | not in the map block |
| `--r` | 5 | not in the map block |
| `--c-grnbg` | 5 | not in the map block |
| `--c-amb` | 5 | not in the map block |
| `--panel-hi` | 5 | not in the map block |
| `--grn-t` | 5 | not in the map block |
| `--violet` | 5 | not in the map block |
| `--bull-soft` | 5 | not in the map block |
| `--mux-tap` | 4 | not in the map block |
| `--c-dim` | 4 | not in the map block |
| `--c-redbg` | 4 | not in the map block |
| `--c-ambbg` | 4 | not in the map block |
| `--amDim` | 4 | not in the map block |
| `--rd` | 4 | not in the map block |
| `--cut` | 4 | not in the map block |
| `--g` | 4 | not in the map block |
| `--m` | 4 | not in the map block |
| `--red-t` | 4 | not in the map block |
| `--mast` | 3 | not in the map block |
| `--c-grid` | 3 | not in the map block |
| `--grDim` | 3 | not in the map block |
| `--v` | 3 | not in the map block |
| `--sans` | 3 | not in the map block |
| `--serif` | 3 | not in the map block |
| `--bear-soft` | 3 | not in the map block |
| `--tkr-dur` | 2 | not in the map block |
| `--vc` | 2 | not in the map block |
| `--mtable-bg` | 2 | not in the map block |
| `--hover` | 2 | not in the map block |
| `--shadow-md` | 2 | not in the map block |
| `--c-blubg` | 2 | not in the map block |
| `--amb-t` | 2 | not in the map block |
| `--on-accent` | 2 | not in the map block |
| `--acc` | 2 | not in the map block |
| `--s` | 2 | not in the map block |

*plus 15 more with fewer uses.*

Re-pointed today, for the record: `--amber`, `--bg`, `--bg2`, `--bg3`, `--blu`, `--border`, `--card`, `--card2`, `--chalk`, `--cyan`, `--dim`, `--dn`, `--down`, `--edge`, `--field`, `--grn`, `--grn-d`, `--heat`, `--ink`, `--line`, `--line2`, `--mono`, `--mut`, `--muted`, `--panel`, `--panel2`, `--panel3`, `--pulse`, `--red`, `--red-d`, `--rule`, `--shadow`, `--surface`, `--surface2`, `--txt`, `--up`, `--volt`, `--well`.

## Per-file tables — FIX universe

One table per file that carries a colour, ordered by occurrence count. `lines` is a sample of
up to three; `count` is the true total for that file.


### `fpc_v11.html`

33 colours, 101 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#2FD48B` | 17 | 16, 442, 442 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--green` |
| `#F5B94A` | 12 | 16, 17, 442 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--gold`, `--warn` |
| `#0E7A50` | 10 | 51, 82, 85 | `--volt` | REPLACE | green family -> --volt (ΔE 76.1) |
| `#8B949E` | 9 | 514, 514, 558 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.6) |
| `#4D7CFE` | 7 | 17, 54, 482 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blue` |
| `#C0392B` | 7 | 52, 87, 548 | `--heat` | REPLACE | red family -> --heat (ΔE 27.7) |
| `#FF5C6C` | 6 | 17, 442, 490 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(20,35,70) @0.08` | 3 | 514, 558, 630 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `#A16207` | 2 | 53, 86 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.6) |
| `rgba(186,117,23) @0.15` | 2 | 53, 86 | `--amber` | REPLACE | orange family -> --amber (ΔE 20.5) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(226,75,74) @0.15` | 2 | 52, 87 | `--heat` | REPLACE | red family -> --heat (ΔE 16.7) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(29,158,117) @0.08` | 2 | 513, 555 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(29,158,117) @0.15` | 2 | 51, 85 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#0A0F1E` | 1 | 13 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#121A33` | 1 | 13 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--bg2` |
| `#182241` | 1 | 13 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--bg3` |
| `#37D3E8` | 1 | 16 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--accent` |
| `#5E6B8F` | 1 | 14 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--text3` |
| `#7F77DD` | 1 | 482 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 35.8) |
| `#888780` | 1 | 482 | `--muted` | DECIDE | grey ramp value 19.5 ΔE from the nearest surface token — between steps |
| `#8C99BD` | 1 | 14 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--text2` |
| `#D4537E` | 1 | 482 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#E9EEFB` | 1 | 14 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--text` |
| `#FFFFFF` | 1 | 80 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(139,148,158) @0.04` | 1 | 626 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.6) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 15 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--border`, `--border2` |
| `rgba(148,166,210) @0.24` | 1 | 15 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--border`, `--border2` |
| `rgba(186,117,23) @0.04` | 1 | 627 | `--amber` | REPLACE | orange family -> --amber (ΔE 20.5) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(226,75,74) @0.04` | 1 | 628 | `--heat` | REPLACE | red family -> --heat (ΔE 16.7) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(29,158,117) @0.07` | 1 | 624 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(29,158,117) @0.12` | 1 | 82 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(55,138,221) @0.05` | 1 | 625 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 49.0) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(55,138,221) @0.15` | 1 | 54 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 49.0) · translucent 0.15, so a tint of it (see DECIDE-0) |

### `galaxy_map.js`

40 colours, 42 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#34D399` | 2 | 25, 28 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#FBBF24` | 2 | 26, 28 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) |
| `#060912` | 1 | 250 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.7) |
| `#22D3EE` | 1 | 29 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) |
| `#4ADE80` | 1 | 29 | `--volt` | REPLACE | green family -> --volt (ΔE 48.0) |
| `#60A5FA` | 1 | 28 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 59.0) |
| `#95A8D6` | 1 | 470 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.2) |
| `#A78BFA` | 1 | 28 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 34.0) |
| `#C084FC` | 1 | 29 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 30.4) |
| `#EAF0FF` | 1 | 467 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.6) |
| `#F472B6` | 1 | 28 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#FACC15` | 1 | 29 | `--amber` | DECIDE | yellow — yellow sits between --amber and --volt |
| `#FB7185` | 1 | 29 | `--heat` | REPLACE | red family -> --heat (ΔE 16.5) |
| `#FFFFFF` | 1 | 307 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(0,0,0) @0.00` | 1 | 97 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(120,150,220) @0.55` | 1 | 465 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 60.0) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(13,20,40) @0.97` | 1 | 463 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 4.0) · translucent 0.97, so a tint of it (see DECIDE-0) |
| `rgba(150,110,235) @0.00` | 1 | 111 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.1) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(150,110,235) @0.55` | 1 | 111 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.1) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(180,190,240) @0.00` | 1 | 115 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 73.6) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(180,190,240) @0.18` | 1 | 115 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 73.6) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(210,220,255) @0.60` | 1 | 115 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 85.2) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(220,150,90) @0.12` | 1 | 109 | `--amber` | REPLACE | orange family -> --amber (ΔE 20.7) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(222,231,252) @0.96` | 1 | 439 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 6.6) · translucent 0.96, so a tint of it (see DECIDE-0) |
| `rgba(226,234,255) @0.90` | 1 | 194 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 6.1) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(240,120,160) @0.00` | 1 | 113 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `rgba(240,120,160) @0.50` | 1 | 113 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `rgba(251,191,36) @0.60` | 1 | 26 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(255,150,60) @0.00` | 1 | 109 | `--amber` | REPLACE | orange family -> --amber (ΔE 5.1) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(255,180,96) @0.40` | 1 | 109 | `--amber` | REPLACE | orange family -> --amber (ΔE 12.8) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(255,224,150) @0.92` | 1 | 108 | `--amber` | REPLACE | orange family -> --amber (ΔE 37.7) · translucent 0.92, so a tint of it (see DECIDE-0) |
| `rgba(255,252,238) @1.00` | 1 | 108 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 13.3) |
| `rgba(255,255,255) @0.00` | 1 | 304 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.85` | 1 | 383 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.85, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.90` | 1 | 272 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(52,211,153) @0.60` | 1 | 25 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(6,10,22) @0.85` | 1 | 438 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.6) · translucent 0.85, so a tint of it (see DECIDE-0) |
| `rgba(6,9,18) @0.90` | 1 | 403 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.7) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(70,200,210) @0.00` | 1 | 112 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 14.3) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(70,200,210) @0.50` | 1 | 112 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 14.3) · translucent 0.50, so a tint of it (see DECIDE-0) |

### `gvm_nightly.py`

6 colours, 6 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0A7F4F` | 1 | 864 | `--volt` | REPLACE | green family -> --volt (ΔE 72.9) |
| `#12A05F` | 1 | 866 | `--volt` | REPLACE | green family -> --volt (ΔE 62.4) |
| `#1F8FA8` | 1 | 873 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 30.1) |
| `#8A94AD` | 1 | 877 | `--muted` | OK | already the canonical value |
| `#DD3A4A` | 1 | 871 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) |
| `#E0913A` | 1 | 869 | `--amber` | REPLACE | orange family -> --amber (ΔE 9.9) |

### `hr_report_pdf.py`

22 colours, 53 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0B6E42` | 7 | 171, 205, 222 | `--volt` | REPLACE | declared as `--grn` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --volt |
| `#5B667D` | 7 | 170, 278, 283 | `--muted` | REPLACE | declared as `--muted` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --muted |
| `#B52432` | 6 | 171, 205, 222 | `--heat` | REPLACE | declared as `--red` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --heat |
| `#E5E8EF` | 6 | 309, 309, 312 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.2) |
| `#07111F` | 4 | 294, 302, 327 | `--chalk` | REPLACE | declared as `--ink` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#9098A8` | 3 | 285, 310, 328 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.3) |
| `#1847DF` | 2 | 325, 353 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#8B93A5` | 2 | 313, 315 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.4) |
| `#8F5C07` | 2 | 171, 324 | `--amber` | REPLACE | orange family -> --amber (ΔE 34.8) |
| `rgba(255,255,255) @0.40` | 2 | 303, 339 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `#1A9070` | 1 | 353 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#5340C2` | 1 | 353 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 20.4) |
| `#EEF0F4` | 1 | 317 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.4) |
| `#FFFFFF` | 1 | 302 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(11,110,66) @0.12` | 1 | 322 | `--volt` | REPLACE | green family -> --volt (ΔE 77.2) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(143,92,7) @0.12` | 1 | 324 | `--amber` | REPLACE | orange family -> --amber (ΔE 34.8) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(181,36,50) @0.12` | 1 | 323 | `--heat` | REPLACE | red family -> --heat (ΔE 24.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(24,71,223) @0.12` | 1 | 325 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 16.4) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.30` | 1 | 381 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.35` | 1 | 380 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.50` | 1 | 305 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.88` | 1 | 333 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.88, so a tint of it (see DECIDE-0) |

### `index_tape_card.js`

3 colours, 8 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#94A3B8` | 5 | 53, 122, 199 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) |
| `#64748B` | 2 | 54, 178 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.9) |
| `#FFFFFF` | 1 | 158 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |

### `main.py`

9 colours, 18 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#2A3548` | 2 | 186, 204 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 6.2) |
| `#3D6BEC` | 2 | 211, 211 | `--pulse` | REPLACE | declared as `--blu` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#4D7CFE` | 2 | 207, 207 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#5A6781` | 2 | 187, 204 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#5B6B94` | 2 | 193, 210 | `--muted` | REPLACE | declared as `--mut` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#B45309` | 2 | 190, 190 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.5) |
| `rgba(15,22,35) @0.88` | 2 | 186, 204 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.9) · translucent 0.88, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.14` | 2 | 193, 210 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 72.5) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.92` | 2 | 192, 209 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.92, so a tint of it (see DECIDE-0) |

### `mobile/check.html`

2 colours, 2 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(245,185,74) @0.10` | 1 | 107 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.40` | 1 | 107 | `--amber` | REPLACE | declared as `--amber` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.40 — see DECIDE-0) |

### `mobile/home.html`

28 colours, 72 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 14 | 36, 53, 86 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#2FD48B` | 8 | 129, 145, 177 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#FF5C6C` | 8 | 129, 144, 178 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `#F5B94A` | 6 | 172, 179, 340 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#5E6B8F` | 5 | 502, 503, 1082 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `rgba(77,124,254) @0.10` | 3 | 130, 175, 1087 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.12` | 3 | 36, 86, 108 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.50` | 3 | 36, 86, 108 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 2 | 38, 60 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field |
| `#E9EEFB` | 2 | 30, 247 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk |
| `#161F33` | 1 | 276 | `--panel` | OK | already the canonical value |
| `#26334F` | 1 | 276 | `--edge` | OK | already the canonical value |
| `#35E0FF` | 1 | 274 | `--aqua` | OK | already the canonical value |
| `#94A6D2` | 1 | 1111 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) |
| `#E8EDF7` | 1 | 64 | `--chalk` | OK | already the canonical value |
| `rgba(148,166,210) @0.09` | 1 | 1080 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.35` | 1 | 95 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.06` | 1 | 159 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.07` | 1 | 147 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.07` | 1 | 76 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 186 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(255,92,108) @0.16` | 1 | 144 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.07` | 1 | 75 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.13` | 1 | 185 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.16` | 1 | 145 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(53,224,255) @0.40` | 1 | 274 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 0.0) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 497 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(77,124,254) @0.16` | 1 | 146 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.16, so a tint of it (see DECIDE-0) |

### `mobile/login.html`

1 colours, 1 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 1 | 43 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |

### `mobile/trade_wall.html`

4 colours, 5 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(47,212,139) @0.13` | 2 | 20, 21 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 22 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(255,92,108) @0.40` | 1 | 33 | `--heat` | REPLACE | declared as `--red` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 1 | 32 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |

### `mobile/v8.html`

18 colours, 38 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 8 | 82, 90, 134 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#0A0F1E` | 3 | 119, 151, 177 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field |
| `#E9EEFB` | 3 | 34, 131, 287 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk |
| `rgba(77,124,254) @0.12` | 3 | 82, 90, 186 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `#2FD48B` | 2 | 140, 661 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#CFD9FF` | 2 | 242, 262 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 83.4) |
| `#FF5C6C` | 2 | 141, 661 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `rgba(255,92,108) @0.40` | 2 | 33, 188 | `--heat` | REPLACE | declared as `--red` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(77,124,254) @0.16` | 2 | 131, 262 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.50` | 2 | 82, 90 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.55` | 2 | 186, 242 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `#F87171` | 1 | 629 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) |
| `rgba(10,15,30) @0.00` | 1 | 177 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.7) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.45` | 1 | 268 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.07` | 1 | 142 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.16` | 1 | 141 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.16` | 1 | 140 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 1 | 32 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |

### `mobile_endpoints.py`

37 colours, 147 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#3AA0FF` | 10 | 1450, 1453, 1627 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 48.1 to aqua, 56.3 to pulse) · bound to `--cyan` |
| `#E9EEFB` | 8 | 1284, 1289, 1456 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `rgba(148,166,210) @0.14` | 8 | 1288, 1455, 1547 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#0A0F1E` | 7 | 1284, 1287, 1454 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `rgba(47,212,139) @0.14` | 7 | 1290, 1457, 1468 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `#121A33` | 6 | 1287, 1454, 1631 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 6 | 1287, 1454, 1631 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 6 | 1290, 1457, 1634 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 6 | 1291, 1451, 1628 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#4D7CFE` | 6 | 1291, 1458, 1635 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 6 | 1289, 1456, 1633 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 6 | 1289, 1456, 1633 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#F5B94A` | 6 | 1291, 1458, 1635 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 6 | 1291, 1458, 1635 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(148,166,210) @0.24` | 6 | 1288, 1455, 1632 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#141B2A` | 5 | 1453, 1630, 1795 | `--panel` | REPLACE | declared as `--surface` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--surface` |
| `#1A2233` | 5 | 1453, 1630, 1795 | `--panel-hi` | REPLACE | declared as `--surface2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#FFFFFF` | 5 | 1440, 1617, 1782 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(255,92,108) @0.45` | 4 | 1331, 1477, 1689 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.45` | 4 | 1328, 1476, 1688 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.45` | 3 | 1327, 1330, 1754 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.10` | 3 | 1689, 1755, 1944 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.40` | 2 | 1487, 1944 | `--heat` | REPLACE | declared as `--red` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 2 | 1488, 1943 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(77,124,254) @0.55` | 2 | 1311, 1507 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.45` | 1 | 2039 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.35` | 1 | 2065 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.12` | 1 | 1754 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.50` | 1 | 1375 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.12` | 1 | 1469 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.12 — see DECIDE-0) |
| `rgba(4,8,18) @0.62` | 1 | 2051 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.5) · translucent 0.62, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.50` | 1 | 1374 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.40` | 1 | 1325 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.12` | 1 | 1326 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 1311 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(77,124,254) @0.40` | 1 | 1566 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.50` | 1 | 1326 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.50, so a tint of it (see DECIDE-0) |

### `pcr_trend_card.js`

2 colours, 5 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#94A3B8` | 4 | 34, 87, 178 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) |
| `#FFFFFF` | 1 | 136 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |

### `pwa_endpoints.py`

131 colours, 324 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#667085` | 24 | 1264, 1268, 1274 | `--muted` | REPLACE | declared as `--mut` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted |
| `#FFFFFF` | 14 | 253, 254, 257 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0F9D58` | 10 | 1262, 1278, 1296 | `--volt` | REPLACE | declared as `--grn` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --volt |
| `rgba(148,166,210) @0.14` | 9 | 241, 247, 514 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `#8892A6` | 8 | 1259, 1270, 1289 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 3.4) |
| `rgba(47,212,139) @0.14` | 7 | 846, 1262, 1278 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--bull`, `--bull-soft` |
| `#4D7CFE` | 6 | 521, 521, 849 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blue` |
| `#64748B` | 6 | 878, 883, 922 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.9) |
| `#F4F7FE` | 6 | 89, 90, 178 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x1, `--chalk` x1) — one value doing two jobs |
| `#2563EB` | 5 | 27, 253, 254 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#34D399` | 5 | 868, 895, 932 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#3D6BEC` | 5 | 527, 527, 1067 | `--pulse` | REPLACE | declared as `--blu` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu`, `--blue`, `--accent`, `--blu-d` |
| `#5B6B94` | 5 | 525, 1061, 1062 | `--muted` | REPLACE | declared as `--mut` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--text2`, `--mut`, `--grey`, `--tag-fg` |
| `#C98A12` | 5 | 1069, 1069, 1069 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber`, `--gold`, `--warn` |
| `#D0433B` | 5 | 1279, 1549, 1626 | `--heat` | REPLACE | declared as `--red` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --heat |
| `#E9EEFB` | 5 | 238, 242, 248 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk |
| `#EDF1FA` | 5 | 1052, 1053, 1054 | `--chalk` | OK | already the canonical value · bound to `--bg2`, `--panel2`, `--card2` |
| `#FBBF24` | 5 | 869, 896, 933 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) |
| `rgba(148,166,210) @0.30` | 5 | 913, 1264, 1284 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.10` | 5 | 524, 528, 1059 | `--edge` | REPLACE | declared as `--line` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.10 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(47,212,139) @0.40` | 5 | 1262, 1278, 1334 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |
| `#0E1630` | 4 | 526, 1058, 1061 | `--chalk` | REPLACE | declared as `--ink` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--ink`, `--txt`, `--text` |
| `#37D3E8` | 4 | 228, 243, 243 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) |
| `#94A3B8` | 4 | 821, 897, 943 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) |
| `#E6EDF7` | 4 | 880, 914, 921 | `--chalk` | OK | already the canonical value |
| `rgba(148,166,210) @0.20` | 4 | 1254, 1268, 1326 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `#0FA968` | 3 | 1063, 1063, 1063 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--green`, `--bull` |
| `#101828` | 3 | 1254, 1298, 1320 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#121A33` | 3 | 233, 513, 843 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--surface` |
| `#131C31` | 3 | 903, 928, 949 | `--panel-hi` | REPLACE | declared as `--panel2` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --panel-hi |
| `#182241` | 3 | 242, 248, 843 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#1E2A44` | 3 | 863, 920, 928 | `--edge` | REPLACE | declared as `--line` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --edge |
| `#2F6DF4` | 3 | 1338, 1604, 1813 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#334155` | 3 | 867, 897, 943 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 8.4) |
| `#7A5AF8` | 3 | 1295, 1308, 1308 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 2.7) |
| `#8C99BD` | 3 | 225, 518, 845 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E7ECF8` | 3 | 1052, 1053, 1058 | `--field` | REPLACE | declared as `--bg3` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg3`, `--panel3`, `--well` |
| `rgba(0,0,0) @0.25` | 3 | 650, 774, 813 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.00` | 3 | 772, 807, 808 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `#000000` | 2 | 1004, 1004 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) |
| `#0A0F1E` | 2 | 410, 843 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--ink` |
| `#0E1526` | 2 | 863, 919 | `--panel` | REPLACE | declared as `--panel` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --panel |
| `#1C2536` | 2 | 1343, 1808 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#2A3A5C` | 2 | 919, 929 | `--edge` | REPLACE | declared as `--line2` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --edge |
| `#475569` | 2 | 870, 934 | `--edge` | DECIDE | grey ramp value 15.8 ΔE from the nearest surface token — between steps |
| `#7C3AED` | 2 | 1071, 1071 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) · bound to `--violet`, `--purp`, `--purp-d`, `--purp-b` |
| `#8A97BC` | 2 | 1061, 1062 | `--muted` | REPLACE | declared as `--dim` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--text3`, `--dim` |
| `#E0405A` | 2 | 1065, 1065 | `--heat` | REPLACE | declared as `--red` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--bear` |
| `rgba(120,130,150) @0.40` | 2 | 768, 803 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.5) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(13,20,40) @0.92` | 2 | 221, 1038 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 4.0) · translucent 0.92, so a tint of it (see DECIDE-0) |
| `rgba(148,163,184) @0.06` | 2 | 1316, 1316 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.12` | 2 | 915, 1343 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 2 | 222, 844 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(20,35,80) @0.18` | 2 | 1059, 1060 | `--edge` | REPLACE | declared as `--line` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.18 — see DECIDE-0) · bound to `--line`, `--line2`, `--border`, `--border2` |
| `rgba(245,185,74) @0.16` | 2 | 1263, 1354 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.45` | 2 | 1263, 1354 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(251,191,36) @0.12` | 2 | 896, 942 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(251,191,36) @0.45` | 2 | 896, 942 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.96` | 2 | 807, 808 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.96, so a tint of it (see DECIDE-0) |
| `rgba(52,211,153) @0.12` | 2 | 895, 941 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(52,211,153) @0.45` | 2 | 895, 941 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(77,124,254) @0.35` | 2 | 1007, 1248 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `#0D1428` | 1 | 843 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#0F1730` | 1 | 967 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 5.7) |
| `#0FA8C4` | 1 | 1068 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 20.2 to aqua, 95.3 to pulse) · bound to `--cyan`, `--cyan-d` |
| `#1E9E68` | 1 | 998 | `--volt` | REPLACE | green family -> --volt (ΔE 66.5) |
| `#2789C9` | 1 | 1001 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 40.8) |
| `#2FD48B` | 1 | 846 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--bull`, `--bull-soft` |
| `#5A6781` | 1 | 819 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#5E6B8F` | 1 | 845 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8A5BD6` | 1 | 1000 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.9) |
| `#8A94A6` | 1 | 1318 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) |
| `#9DAEC8` | 1 | 937 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.5) |
| `#B08CFF` | 1 | 1000 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 32.5) |
| `#C2404E` | 1 | 999 | `--heat` | REPLACE | red family -> --heat (ΔE 20.6) |
| `#F5B94A` | 1 | 848 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber`, `--amber-soft` |
| `#F6F8FB` | 1 | 817 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs |
| `#FF5C6C` | 1 | 847 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--bear`, `--bear-soft` |
| `rgba(0,0,0) @0.22` | 1 | 1256 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.45` | 1 | 1251 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(122,90,248) @0.12` | 1 | 1295 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 2.7) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(122,90,248) @0.32` | 1 | 1295 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 2.7) · translucent 0.32, so a tint of it (see DECIDE-0) |
| `rgba(124,58,237) @0.10` | 1 | 1071 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--violet`, `--purp`, `--purp-d`, `--purp-b` |
| `rgba(124,58,237) @0.35` | 1 | 1071 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) · translucent 0.35, so a tint of it (see DECIDE-0) · bound to `--violet`, `--purp`, `--purp-d`, `--purp-b` |
| `rgba(148,163,184) @0.16` | 1 | 1316 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.10` | 1 | 1321 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.28` | 1 | 1319 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.35` | 1 | 1309 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(15,168,196) @0.12` | 1 | 1068 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 20.2 to aqua, 95.3 to pulse) · bound to `--cyan`, `--cyan-d` |
| `rgba(15,169,104) @0.12` | 1 | 1064 | `--volt` | REPLACE | declared as `--grn-d` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.12 — see DECIDE-0) · bound to `--grn-d`, `--grn-b` |
| `rgba(15,169,104) @0.22` | 1 | 1077 | `--volt` | REPLACE | green family -> --volt (ΔE 61.9) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(15,169,104) @0.35` | 1 | 1064 | `--volt` | REPLACE | declared as `--grn-d` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.35 — see DECIDE-0) · bound to `--grn-d`, `--grn-b` |
| `rgba(15,169,104) @0.50` | 1 | 1078 | `--volt` | REPLACE | green family -> --volt (ΔE 61.9) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(15,22,35) @0.45` | 1 | 231 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.9) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 524 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(20,35,70) @0.18` | 1 | 236 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.08` | 1 | 1072 | `--field` | REPLACE | declared as `--shadow` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.08 — see DECIDE-0) · bound to `--shadow`, `--shadow-md` |
| `rgba(20,35,80) @0.14` | 1 | 1080 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 72.5) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(201,138,18) @0.12` | 1 | 1070 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.3) · translucent 0.12, so a tint of it (see DECIDE-0) · bound to `--amber-d`, `--amber-b` |
| `rgba(201,138,18) @0.35` | 1 | 1070 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.3) · translucent 0.35, so a tint of it (see DECIDE-0) · bound to `--amber-d`, `--amber-b` |
| `rgba(208,67,59) @0.12` | 1 | 1279 | `--heat` | REPLACE | red family -> --heat (ΔE 22.3) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(208,67,59) @0.35` | 1 | 1279 | `--heat` | REPLACE | red family -> --heat (ΔE 22.3) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(224,64,90) @0.10` | 1 | 1066 | `--heat` | REPLACE | declared as `--red-d` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.10 — see DECIDE-0) · bound to `--red-d`, `--red-b` |
| `rgba(224,64,90) @0.35` | 1 | 1066 | `--heat` | REPLACE | declared as `--red-d` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.35 — see DECIDE-0) · bound to `--red-d`, `--red-b` |
| `rgba(245,185,74) @0.14` | 1 | 848 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) · bound to `--amber`, `--amber-soft` |
| `rgba(245,185,74) @0.30` | 1 | 1036 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.14` | 1 | 1008 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.15` | 1 | 640 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.90` | 1 | 1080 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.92` | 1 | 772 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.92, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 847 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--bear`, `--bear-soft` |
| `rgba(255,92,108) @0.18` | 1 | 1028 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.35` | 1 | 961 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.55` | 1 | 1028 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(3,7,20) @0.40` | 1 | 515 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.2) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(3,7,20) @0.50` | 1 | 967 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) |
| `rgba(47,109,244) @0.08` | 1 | 1338 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 24.5) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(47,109,244) @0.16` | 1 | 1339 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 24.5) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,109,244) @0.28` | 1 | 1338 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 24.5) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.18` | 1 | 1028 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.24` | 1 | 1348 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.24, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.25` | 1 | 1034 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.35` | 1 | 959 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.38` | 1 | 1296 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.38, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.55` | 1 | 1028 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(5,9,18) @0.72` | 1 | 916 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.7) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.14` | 1 | 243 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) |
| `rgba(61,107,236) @0.12` | 1 | 1067 | `--pulse` | REPLACE | declared as `--blu` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.12 — see DECIDE-0) · bound to `--blu`, `--blue`, `--accent`, `--blu-d` |
| `rgba(77,124,254) @0.10` | 1 | 1323 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.12` | 1 | 1248 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.22` | 1 | 1250 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.22, so a tint of it (see DECIDE-0) |

### `quant_basket.html`

41 colours, 58 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#243049` | 3 | 326, 433, 476 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 2.4) |
| `#2FD48B` | 3 | 17, 222, 428 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#4D7CFE` | 3 | 17, 97, 222 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#FF8A5C` | 3 | 52, 70, 222 | `--amber` | REPLACE | orange family -> --amber (ΔE 20.3) |
| `rgba(255,92,108) @0.08` | 3 | 89, 98, 115 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `#37D3E8` | 2 | 17, 222 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#8C99BD` | 2 | 10, 428 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#F5B94A` | 2 | 17, 222 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FCA5A5` | 2 | 89, 107 | `--heat` | REPLACE | red family -> --heat (ΔE 41.0) |
| `#FF5C6C` | 2 | 17, 98 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#FFFFFF` | 2 | 14, 14 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(47,212,139) @0.10` | 2 | 72, 87 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 1 | 10 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 10 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 10 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#14213E` | 1 | 142 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 4.3) |
| `#182241` | 1 | 10 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#5E6B8F` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#9B7CFF` | 1 | 222 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) |
| `#A7F3D0` | 1 | 87 | `--volt` | REPLACE | green family -> --volt (ΔE 66.9) |
| `#E9EEFB` | 1 | 10 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#FCD34D` | 1 | 88 | `--amber` | DECIDE | yellow — yellow sits between --amber and --volt |
| `rgba(10,18,36) @0.48` | 1 | 118 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 2.2) · translucent 0.48, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) |
| `rgba(20,30,50) @0.09` | 1 | 64 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 0.6) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 19 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) · bound to `--shadow` |
| `rgba(20,35,70) @0.15` | 1 | 121 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.10` | 1 | 90 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.40` | 1 | 90 | `--amber` | REPLACE | declared as `--amber` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(255,138,92) @0.13` | 1 | 70 | `--amber` | REPLACE | orange family -> --amber (ΔE 20.3) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(255,138,92) @0.50` | 1 | 52 | `--amber` | REPLACE | orange family -> --amber (ΔE 20.3) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.12` | 1 | 76 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.12 — see DECIDE-0) |
| `rgba(37,99,235) @0.15` | 1 | 65 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.15` | 1 | 75 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 1 | 54 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(55,211,232) @0.12` | 1 | 71 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.50` | 1 | 53 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.10` | 1 | 166 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.12` | 1 | 142 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 97 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_adaptive.html`

27 colours, 40 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#DFE3EA` | 5 | 69, 70, 133 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.8) |
| `#FFFFFF` | 5 | 31, 69, 101 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0B6E42` | 4 | 17, 101, 102 | `--volt` | REPLACE | declared as `--grn` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#07111F` | 2 | 14, 15 | `--chalk` | REPLACE | declared as `--ink` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --chalk · bound to `--mast`, `--ink` |
| `#667085` | 2 | 133, 138 | `--muted` | REPLACE | declared as `--mut` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted |
| `#060B16` | 1 | 20 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.8) · bound to `--mast` |
| `#0A0F1E` | 1 | 20 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#101830` | 1 | 20 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 4.8) · bound to `--paper` |
| `#1847DF` | 1 | 17 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#2FD48B` | 1 | 23 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#4D7CFE` | 1 | 23 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5B667D` | 1 | 15 | `--muted` | REPLACE | declared as `--muted` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--muted` |
| `#5E6B8F` | 1 | 21 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--faint` |
| `#8C99BD` | 1 | 21 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--muted` |
| `#B52432` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#B8BECC` | 1 | 15 | `--muted` | DECIDE | grey ramp value 16.2 ΔE from the nearest surface token — between steps · bound to `--faint` |
| `#E9EEFB` | 1 | 21 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--ink` |
| `#ECEEF3` | 1 | 14 | `--field` | REPLACE | declared as `--bg` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#EEF1F5` | 1 | 136 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.3) |
| `#F8F9FC` | 1 | 14 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 5.2) · bound to `--paper` |
| `#FF5C6C` | 1 | 23 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(148,166,210) @0.05` | 1 | 22 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.05 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(148,166,210) @0.14` | 1 | 22 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(255,255,255) @0.35` | 1 | 32 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.45` | 1 | 34 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(7,17,31) @0.03` | 1 | 16 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.03 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(7,17,31) @0.10` | 1 | 16 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.10 — see DECIDE-0) · bound to `--rule`, `--hover` |

### `scorr_analysis_card.js`

13 colours, 22 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 4 | 53, 54, 58 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0A9E63` | 2 | 53, 53 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) |
| `#1C2536` | 2 | 50, 86 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#5A6B82` | 2 | 87, 90 | `--muted` | REPLACE | declared as `--mut` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --muted |
| `#7C3AED` | 2 | 54, 54 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) |
| `#E8ECF2` | 2 | 49, 89 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge |
| `#F6F8FB` | 2 | 50, 90 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs |
| `#4D7CFE` | 1 | 52 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#8A94AD` | 1 | 64 | `--muted` | OK | already the canonical value |
| `#E2E7EE` | 1 | 59 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.5) |
| `#F5B94A` | 1 | 117 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `rgba(0,0,0) @0.22` | 1 | 58 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.45` | 1 | 56 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.45, so a tint of it (see DECIDE-0) |

### `scorr_ask.html`

18 colours, 27 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 3 | 15, 32, 32 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#FFFFFF` | 3 | 60, 64, 96 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.14` | 3 | 12, 25, 34 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#121A33` | 2 | 11, 24 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#8C99BD` | 2 | 13, 30 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E9EEFB` | 2 | 13, 31 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#0A0F1E` | 1 | 11 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 11 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#182241` | 1 | 11 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 14 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 15 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#5E6B8F` | 1 | 13 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#F5B94A` | 1 | 15 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 14 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `rgba(148,166,210) @0.24` | 1 | 12 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(20,35,70) @0.06` | 1 | 26 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 14 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(47,212,139) @0.14` | 1 | 14 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |

### `scorr_auth.py`

10 colours, 19 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0F1623` | 3 | 205, 214, 286 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.9) |
| `#B45309` | 3 | 211, 216, 219 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.5) |
| `#FFFFFF` | 3 | 210, 214, 219 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#2A3548` | 2 | 207, 214 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 6.2) |
| `#3D4F6B` | 2 | 217, 222 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 12.1) |
| `#DD3A4A` | 2 | 187, 190 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) |
| `#1C2536` | 1 | 207 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#5A6781` | 1 | 212 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#9A4507` | 1 | 221 | `--amber` | REPLACE | orange family -> --amber (ΔE 36.3) |
| `rgba(0,0,0) @0.50` | 1 | 209 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.50, so a tint of it (see DECIDE-0) |

### `scorr_card_common.js`

19 colours, 26 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(148,166,210) @0.35` | 3 | 612, 628, 659 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `#0E1526` | 2 | 624, 838 | `--panel` | REPLACE | declared as `--panel` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --panel |
| `#2A3548` | 2 | 229, 234 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 6.2) |
| `#5E6B8F` | 2 | 632, 745 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#D68A1E` | 2 | 163, 178 | `--amber` | REPLACE | orange family -> --amber (ΔE 13.0) |
| `#FFFFFF` | 2 | 178, 912 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#1C2536` | 1 | 904 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#1E2740` | 1 | 714 | `--panel-hi` | OK | already the canonical value |
| `#1E2A44` | 1 | 625 | `--edge` | REPLACE | declared as `--line` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --edge |
| `#3AA0FF` | 1 | 199 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 48.1 to aqua, 56.3 to pulse) |
| `#7C8AA5` | 1 | 230 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.2) |
| `rgba(148,166,210) @0.45` | 1 | 733 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.10` | 1 | 154 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.16` | 1 | 154 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.22` | 1 | 154 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.10` | 1 | 154 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.16` | 1 | 154 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.22` | 1 | 154 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(5,9,18) @0.55` | 1 | 622 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.7) · translucent 0.55, so a tint of it (see DECIDE-0) |

### `scorr_card_strip.js`

6 colours, 8 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 3 | 44, 45, 45 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#1C2536` | 1 | 43 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#9AA4B5` | 1 | 46 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted |
| `#E8ECF2` | 1 | 42 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge |
| `#F6F8FB` | 1 | 43 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs |
| `#FFFFFF` | 1 | 45 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |

### `scorr_chart_card.js`

31 colours, 65 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0A9E63` | 10 | 106, 106, 120 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) |
| `#DD3A4A` | 10 | 106, 106, 124 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) |
| `#8A94AD` | 6 | 102, 106, 122 | `--muted` | OK | already the canonical value |
| `#FFFFFF` | 6 | 190, 424, 490 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#4D7CFE` | 3 | 189, 190, 1289 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#12864F` | 2 | 106, 121 | `--volt` | REPLACE | green family -> --volt (ΔE 69.0) |
| `#1C2536` | 2 | 189, 190 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#2FD48B` | 2 | 1013, 1022 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#FF5C6C` | 2 | 1013, 1023 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `#0F1623` | 1 | 189 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.9) |
| `#2A3548` | 1 | 189 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 6.2) |
| `#5A6781` | 1 | 189 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#5A6B82` | 1 | 190 | `--muted` | REPLACE | declared as `--mut` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --muted |
| `#5B7FB3` | 1 | 103 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 68.3) |
| `#7B6BD6` | 1 | 73 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 31.4) |
| `#7FD4A8` | 1 | 1022 | `--volt` | REPLACE | green family -> --volt (ΔE 63.4) |
| `#8A94A6` | 1 | 190 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) |
| `#8C99BD` | 1 | 1013 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#9AA4B5` | 1 | 189 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted |
| `#E0913A` | 1 | 123 | `--amber` | REPLACE | orange family -> --amber (ΔE 9.9) |
| `#E6EAF0` | 1 | 190 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.0) |
| `#E8ECF2` | 1 | 189 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge |
| `#F4F6FA` | 1 | 190 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.0) |
| `#F5B94A` | 1 | 1023 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `rgba(0,0,0) @0.28` | 1 | 452 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.32` | 1 | 216 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.32, so a tint of it (see DECIDE-0) |
| `rgba(10,16,25) @0.50` | 1 | 214 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.4) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(120,130,150) @0.12` | 1 | 190 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.5) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(150,160,180) @0.14` | 1 | 189 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.6) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(18,24,36) @0.72` | 1 | 764 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.1) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.78` | 1 | 764 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.78, so a tint of it (see DECIDE-0) |

### `scorr_check.html`

70 colours, 84 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#A5D8E8` | 5 | 135, 211, 218 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 23.5) |
| `#FFFFFF` | 4 | 86, 339, 685 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0D1428` | 2 | 13, 22 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2`, `--well` |
| `#182241` | 2 | 13, 22 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2`, `--surface2` |
| `#5A6781` | 2 | 882, 882 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#D8DEE9` | 2 | 882, 882 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 6.3) |
| `#EEF1F6` | 2 | 882, 882 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.8) |
| `rgba(15,169,104) @0.03` | 2 | 176, 265 | `--volt` | REPLACE | green family -> --volt (ΔE 61.9) · translucent 0.03, so a tint of it (see DECIDE-0) |
| `rgba(226,55,68) @0.03` | 2 | 177, 266 | `--heat` | REPLACE | red family -> --heat (ΔE 15.2) · translucent 0.03, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 1 | 13 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0A7D47` | 1 | 880 | `--volt` | REPLACE | green family -> --volt (ΔE 70.8) |
| `#121A33` | 1 | 13 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#1D52D5` | 1 | 88 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 23.3) |
| `#2FD48B` | 1 | 16 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d`, `--grn-b` |
| `#37D3E8` | 1 | 19 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan`, `--cyan-d` |
| `#4D7CFE` | 1 | 18 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu`, `--blu-d` |
| `#5E6B8F` | 1 | 15 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#6D28D9` | 1 | 91 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 18.1) |
| `#8A6D1A` | 1 | 881 | `--amber` | REPLACE | orange family -> --amber (ΔE 38.8) |
| `#8C99BD` | 1 | 15 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#9B7CFF` | 1 | 21 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · bound to `--purp`, `--purp-d`, `--purp-b` |
| `#A5641A` | 1 | 881 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.6) |
| `#B6E6CD` | 1 | 880 | `--volt` | REPLACE | green family -> --volt (ΔE 71.1) |
| `#C0392B` | 1 | 880 | `--heat` | REPLACE | red family -> --heat (ΔE 27.7) |
| `#E7F7EE` | 1 | 880 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 10.5) |
| `#E9EEFB` | 1 | 15 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F0DFA6` | 1 | 881 | `--chalk` | DECIDE | yellow — yellow sits between --amber and --volt |
| `#F2B8BF` | 1 | 880 | `--heat` | REPLACE | red family -> --heat (ΔE 53.8) |
| `#F2D8AD` | 1 | 881 | `--amber` | REPLACE | orange family -> --amber (ΔE 45.8) |
| `#F5B94A` | 1 | 20 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber`, `--amber-d`, `--amber-b` |
| `#FDE8EA` | 1 | 880 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 10.5) |
| `#FDF0DC` | 1 | 881 | `--amber` | REPLACE | orange family -> --amber (ΔE 59.8) |
| `#FDF3D9` | 1 | 881 | `--amber` | REPLACE | orange family -> --amber (ΔE 59.0) |
| `#FF5C6C` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d`, `--red-b` |
| `rgba(0,0,0) @0.07` | 1 | 149 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.08` | 1 | 233 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(10,15,30) @0.55` | 1 | 390 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.7) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(10,158,99) @0.00` | 1 | 603 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(10,158,99) @0.55` | 1 | 602 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(120,140,180) @0.07` | 1 | 252 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 9.8) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(124,58,237) @0.25` | 1 | 90 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(124,58,237) @0.32` | 1 | 91 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) · translucent 0.32, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 14 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 14 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(15,22,35) @0.45` | 1 | 306 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.9) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(155,124,255) @0.16` | 1 | 21 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · translucent 0.16, so a tint of it (see DECIDE-0) · bound to `--purp`, `--purp-d`, `--purp-b` |
| `rgba(155,124,255) @0.40` | 1 | 21 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · translucent 0.40, so a tint of it (see DECIDE-0) · bound to `--purp`, `--purp-d`, `--purp-b` |
| `rgba(217,119,6) @0.03` | 1 | 195 | `--amber` | REPLACE | orange family -> --amber (ΔE 15.7) · translucent 0.03, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.09` | 1 | 372 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 20 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) · bound to `--amber`, `--amber-d`, `--amber-b` |
| `rgba(245,185,74) @0.40` | 1 | 20 | `--amber` | REPLACE | declared as `--amber` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.40 — see DECIDE-0) · bound to `--amber`, `--amber-d`, `--amber-b` |
| `rgba(255,255,255) @0.60` | 1 | 228 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.07` | 1 | 373 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d`, `--red-b` |
| `rgba(255,92,108) @0.16` | 1 | 1366 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.30` | 1 | 1366 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.40` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.40 — see DECIDE-0) · bound to `--red`, `--red-d`, `--red-b` |
| `rgba(3,7,20) @0.50` | 1 | 26 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(3,7,20) @0.55` | 1 | 27 | `--field` | REPLACE | declared as `--shadow` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.55 — see DECIDE-0) · bound to `--shadow-md` |
| `rgba(37,99,235) @0.25` | 1 | 87 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.32` | 1 | 88 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.32, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.09` | 1 | 371 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 16 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d`, `--grn-b` |
| `rgba(47,212,139) @0.16` | 1 | 1366 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.30` | 1 | 1366 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 1 | 16 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) · bound to `--grn`, `--grn-d`, `--grn-b` |
| `rgba(55,211,232) @0.14` | 1 | 19 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan`, `--cyan-d` |
| `rgba(77,124,254) @0.14` | 1 | 18 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) · bound to `--blu`, `--blu-d` |
| `rgba(8,145,178) @0.08` | 1 | 60 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 29.3) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(8,145,178) @0.25` | 1 | 512 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 29.3) · translucent 0.25, so a tint of it (see DECIDE-0) |

### `scorr_cio_dashboard.html`

56 colours, 132 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 11 | 16, 65, 69 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#F5B94A` | 10 | 59, 60, 62 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#2FD48B` | 9 | 59, 62, 643 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#8C99BD` | 8 | 61, 642, 757 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#FF5C6C` | 8 | 60, 645, 645 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `#9AA4B5` | 7 | 1420, 1428, 1434 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted |
| `#121A33` | 5 | 58, 62, 143 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel |
| `rgba(245,185,74) @0.14` | 4 | 88, 644, 1346 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `#0891B2` | 3 | 102, 105, 108 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 29.3 to aqua, 89.0 to pulse) |
| `#C98A12` | 3 | 66, 67, 69 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `rgba(148,166,210) @0.06` | 3 | 1093, 2509, 2845 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 3 | 87, 643, 1344 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |
| `#0FA968` | 2 | 66, 69 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#1C2536` | 2 | 902, 1403 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#3D6BEC` | 2 | 66, 69 | `--pulse` | REPLACE | declared as `--blu` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#4D7CFE` | 2 | 59, 62 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#6D4FE0` | 2 | 101, 110 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 10.4) |
| `#F4F6F9` | 2 | 12, 3230 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.4) |
| `rgba(148,166,210) @0.10` | 2 | 642, 1341 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.18` | 2 | 616, 1160 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 2 | 642, 1341 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) |
| `rgba(234,179,8) @0.15` | 2 | 1906, 2110 | `--amber` | DECIDE | yellow — yellow sits between --amber and --volt |
| `rgba(239,68,68) @0.14` | 2 | 1906, 2110 | `--heat` | REPLACE | red family -> --heat (ΔE 18.3) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 2 | 645, 1345 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(34,197,94) @0.16` | 2 | 1906, 2110 | `--volt` | REPLACE | green family -> --volt (ΔE 47.8) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(6,10,20) @0.62` | 2 | 344, 1881 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.8) · translucent 0.62, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 1 | 58 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field |
| `#0E1630` | 1 | 68 | `--chalk` | REPLACE | declared as `--ink` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk |
| `#5B6B94` | 1 | 68 | `--muted` | REPLACE | declared as `--mut` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#7C3AED` | 1 | 67 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) |
| `#8C99BD @0.44` | 1 | 1160 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 6.3) · translucent 0.44, so a tint of it (see DECIDE-0) |
| `#8C99BD @0.56` | 1 | 864 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 6.3) · translucent 0.56, so a tint of it (see DECIDE-0) |
| `#9B7CFF` | 1 | 60 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) |
| `#B0B8C8` | 1 | 1409 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.6) |
| `#CFD8E3` | 1 | 3231 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 8.7) |
| `#D98B1F` | 1 | 1603 | `--amber` | REPLACE | orange family -> --amber (ΔE 12.2) |
| `#DB2777` | 1 | 106 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#E0405A` | 1 | 67 | `--heat` | REPLACE | declared as `--red` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --heat |
| `#E9EEFB` | 1 | 61 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk |
| `#F4F7FE` | 1 | 65 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x1, `--chalk` x1) — one value doing two jobs |
| `rgba(0,0,0) @0.35` | 1 | 801 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.40` | 1 | 998 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.12` | 1 | 61 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 58 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(20,28,45) @0.25` | 1 | 1666 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 2.5) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(20,28,45) @0.55` | 1 | 1663 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 2.5) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.08` | 1 | 68 | `--field` | REPLACE | declared as `--shadow` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.08 — see DECIDE-0) |
| `rgba(20,35,80) @0.10` | 1 | 65 | `--edge` | REPLACE | declared as `--line` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.10 — see DECIDE-0) |
| `rgba(245,185,74) @0.35` | 1 | 1373 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.06` | 1 | 2332 | `--field` | REPLACE | declared as `--shadow` (in `scorr_intraday.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(25,40,70) @0.10` | 1 | 3105 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.5) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.12` | 1 | 3081 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.5) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.16` | 1 | 3116 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.5) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(4,7,15) @0.72` | 1 | 995 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 8.7) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(6,10,22) @0.55` | 1 | 797 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.6) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 89 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_cockpit.html`

36 colours, 55 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 5 | 90, 126, 139 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#4D7CFE` | 4 | 18, 39, 39 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu`, `--blu-d` |
| `#2FD48B` | 3 | 16, 91, 701 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `rgba(148,166,210) @0.14` | 3 | 14, 32, 41 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#121A33` | 2 | 13, 31 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 2 | 13, 13 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2`, `--panel3` |
| `#8C99BD` | 2 | 15, 37 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#B7E0EB` | 2 | 140, 150 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 27.3) |
| `#E9EEFB` | 2 | 15, 38 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 2 | 20, 93 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber`, `--amber-d` |
| `rgba(20,35,70) @0.06` | 2 | 23, 33 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) · bound to `--shadow` |
| `rgba(8,145,178) @0.08` | 2 | 124, 195 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 29.3) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `#08131F` | 1 | 701 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 2.9) |
| `#0A0F1E` | 1 | 13 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0A7D99` | 1 | 128 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 37.0) |
| `#0D1428` | 1 | 13 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#1A1F2E` | 1 | 161 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 3.7) |
| `#1D52D5` | 1 | 200 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 23.3) |
| `#37D3E8` | 1 | 19 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan`, `--cyan-d` |
| `#5E6B8F` | 1 | 15 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#C3EDD8` | 1 | 16 | `--volt` | REPLACE | green family -> --volt (ΔE 73.1) · bound to `--grn-b` |
| `#E8E8E8` | 1 | 161 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 6.1) |
| `#E8F9F1` | 1 | 16 | `--volt` | REPLACE | declared as `--grn-d` (in `scorr_cockpit.html`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn-d` |
| `#F5C6CA` | 1 | 17 | `--heat` | REPLACE | red family -> --heat (ΔE 59.5) · bound to `--red-b` |
| `#FCD89A` | 1 | 20 | `--amber` | REPLACE | orange family -> --amber (ΔE 37.4) · bound to `--amber-b` |
| `#FDF0F1` | 1 | 17 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_cockpit.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red-d` |
| `#FF5C6C` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(148,166,210) @0.24` | 1 | 14 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(15,169,104) @0.40` | 1 | 75 | `--volt` | REPLACE | green family -> --volt (ΔE 61.9) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.09` | 1 | 24 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.09, so a tint of it (see DECIDE-0) · bound to `--shadow-md` |
| `rgba(226,55,68) @0.25` | 1 | 202 | `--heat` | REPLACE | red family -> --heat (ΔE 15.2) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 20 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) · bound to `--amber`, `--amber-d` |
| `rgba(37,99,235) @0.25` | 1 | 199 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.32` | 1 | 200 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.32, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.14` | 1 | 19 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan`, `--cyan-d` |
| `rgba(77,124,254) @0.14` | 1 | 18 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) · bound to `--blu`, `--blu-d` |

### `scorr_cockpit_card.js`

27 colours, 37 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#17603F` | 4 | 98, 120, 140 | `--volt` | REPLACE | green family -> --volt (ΔE 84.2) |
| `#5A4620` | 3 | 121, 141, 148 | `--amber` | REPLACE | orange family -> --amber (ΔE 60.2) |
| `#5E2230` | 3 | 121, 142, 148 | `--heat` | REPLACE | red family -> --heat (ΔE 56.8) |
| `#6FBF95` | 2 | 127, 244 | `--volt` | REPLACE | green family -> --volt (ΔE 65.4) |
| `#8A94AD` | 2 | 54, 74 | `--muted` | OK | already the canonical value |
| `#E8ECF2` | 2 | 74, 76 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--c-grid` |
| `#0A9E63` | 1 | 75 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · bound to `--c-grn` |
| `#132038` | 1 | 114 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 2.9) |
| `#16223C` | 1 | 167 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 2.6) |
| `#1847DF` | 1 | 76 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--c-blu`, `--c-blubg` |
| `#1C2536` | 1 | 74 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#1C9C63` | 1 | 115 | `--volt` | REPLACE | green family -> --volt (ΔE 65.5) |
| `#5A6B82` | 1 | 74 | `--muted` | REPLACE | declared as `--mut` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --muted |
| `#8FE6BB` | 1 | 122 | `--volt` | REPLACE | green family -> --volt (ΔE 63.5) |
| `#C98A12` | 1 | 75 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#DD3A4A` | 1 | 75 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) · bound to `--c-red` |
| `#E8C890` | 1 | 123 | `--amber` | REPLACE | orange family -> --amber (ΔE 38.1) |
| `#F2A6B1` | 1 | 123 | `--heat` | REPLACE | red family -> --heat (ΔE 45.2) |
| `#F6F8FB` | 1 | 74 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs · bound to `--c-panel` |
| `#FFFFFF` | 1 | 74 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(0,0,0) @0.22` | 1 | 79 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.45` | 1 | 68 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(140,153,189) @0.14` | 1 | 226 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 6.3) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(24,71,223) @0.10` | 1 | 76 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 16.4) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--c-blu`, `--c-blubg` |
| `rgba(245,166,35) @0.15` | 1 | 75 | `--amber` | REPLACE | orange family -> --amber (ΔE 15.0) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.12` | 1 | 75 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.12 — see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 75 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--c-grnbg` |

### `scorr_digest_v3.html`

49 colours, 65 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#34D399` | 4 | 14, 856, 857 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) · bound to `--gr` |
| `#F87171` | 4 | 14, 856, 857 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) · bound to `--rd` |
| `#1E2A44` | 3 | 12, 849, 850 | `--edge` | REPLACE | declared as `--line` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line` |
| `rgba(251,191,36) @0.40` | 3 | 59, 200, 227 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `#22D3EE` | 2 | 14, 861 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · bound to `--cy` |
| `#9DAEC8` | 2 | 13, 847 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.5) · bound to `--tx2` |
| `#FFFFFF` | 2 | 92, 147 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(30,42,68) @0.55` | 2 | 848, 848 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 1.5) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.55` | 2 | 48, 159 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(52,211,153) @0.45` | 2 | 69, 199 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#05131A` | 1 | 249 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.8) |
| `#0A0F1E` | 1 | 11 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0E1526` | 1 | 11 | `--panel` | REPLACE | declared as `--panel` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#131C31` | 1 | 11 | `--panel-hi` | REPLACE | declared as `--panel2` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#18233C` | 1 | 11 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 1.9) · bound to `--raise` |
| `#2A3A5C` | 1 | 12 | `--edge` | REPLACE | declared as `--line2` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line2` |
| `#64748B` | 1 | 13 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.9) · bound to `--tx3` |
| `#A78BFA` | 1 | 14 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 34.0) · bound to `--vi` |
| `#DCE6F5` | 1 | 91 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.7) |
| `#E6EDF7` | 1 | 13 | `--chalk` | OK | already the canonical value · bound to `--tx` |
| `#FBBF24` | 1 | 14 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · bound to `--am` |
| `rgba(10,15,30) @0.55` | 1 | 108 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.7) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(148,163,184) @0.07` | 1 | 118 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(248,113,113) @0.08` | 1 | 117 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(248,113,113) @0.14` | 1 | 15 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) · translucent 0.14, so a tint of it (see DECIDE-0) · bound to `--rdDim` |
| `rgba(248,113,113) @0.18` | 1 | 117 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(248,113,113) @0.30` | 1 | 117 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(248,113,113) @0.55` | 1 | 141 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(251,191,36) @0.05` | 1 | 120 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(251,191,36) @0.13` | 1 | 15 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) · translucent 0.13, so a tint of it (see DECIDE-0) · bound to `--amDim` |
| `rgba(30,42,68) @0.60` | 1 | 187 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 1.5) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(30,42,68) @0.75` | 1 | 134 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 1.5) · translucent 0.75, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.00` | 1 | 862 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.04` | 1 | 189 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.07` | 1 | 144 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.09` | 1 | 70 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.10` | 1 | 25 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.18` | 1 | 48 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.20` | 1 | 862 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.25` | 1 | 45 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.28` | 1 | 145 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.35` | 1 | 70 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(34,211,238) @0.60` | 1 | 143 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(5,9,18) @0.72` | 1 | 232 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.7) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(52,211,153) @0.08` | 1 | 116 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(52,211,153) @0.14` | 1 | 15 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) · bound to `--grDim` |
| `rgba(52,211,153) @0.18` | 1 | 116 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(52,211,153) @0.30` | 1 | 116 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `rgba(52,211,153) @0.55` | 1 | 142 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |

### `scorr_filters.html`

21 colours, 34 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 11 | 19, 19, 28 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#FFFFFF` | 3 | 81, 85, 101 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#2FD48B` | 2 | 26, 57 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#0A0F1E` | 1 | 10 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 10 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 10 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 10 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#5E6B8F` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#8C99BD` | 1 | 10 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#92400E` | 1 | 90 | `--amber` | REPLACE | orange family -> --amber (ΔE 40.0) |
| `#C2C9D6` | 1 | 42 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 13.9) |
| `#E9EEFB` | 1 | 10 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 27 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#FF5C6C` | 1 | 58 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `rgba(148,166,210) @0.14` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 15 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(20,35,70) @0.10` | 1 | 87 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 90 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,255,255) @0.28` | 1 | 88 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 80 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_gvm_fightcard.html`

21 colours, 38 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#C8F542` | 6 | 11, 11, 178 | `--volt` | OK | already the canonical value · bound to `--volt`, `--g` |
| `#0E1526` | 5 | 42, 58, 73 | `--panel` | REPLACE | declared as `--panel` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --panel |
| `#4DD8FF` | 5 | 11, 209, 210 | `--aqua` | DECIDE | named in 23878 as the consolidation candidate — founder call (ΔE 7.3 to --aqua) · bound to `--v` |
| `#FF9F45` | 3 | 11, 300, 301 | `--amber` | OK | already the canonical value |
| `#0D1322` | 2 | 10, 27 | `--field` | OK | already the canonical value · bound to `--field` |
| `#26334F` | 2 | 10, 208 | `--edge` | OK | already the canonical value · bound to `--edge` |
| `#070B14` | 1 | 13 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.0) |
| `#161F33` | 1 | 10 | `--panel` | OK | already the canonical value · bound to `--panel` |
| `#18240E` | 1 | 65 | `--field` | DECIDE | grey ramp value 27.4 ΔE from the nearest surface token — between steps |
| `#1C2740` | 1 | 10 | `--panel-hi` | OK | already the canonical value · bound to `--panel-hi` |
| `#241019` | 1 | 66 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 13.5) |
| `#3E5A18` | 1 | 65 | `--volt` | REPLACE | green family -> --volt (ΔE 72.1) |
| `#5A2634` | 1 | 66 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#7C5CFF` | 1 | 11 | `--pulse` | OK | already the canonical value · bound to `--pulse` |
| `#8A97B0` | 1 | 10 | `--muted` | OK | already the canonical value |
| `#EAF0FA` | 1 | 10 | `--chalk` | OK | already the canonical value · bound to `--chalk` |
| `#FF4D6D` | 1 | 11 | `--heat` | OK | already the canonical value · bound to `--heat` |
| `#FF8FA5` | 1 | 66 | `--heat` | REPLACE | red family -> --heat (ΔE 30.7) |
| `#FFFFFF` | 1 | 101 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(200,245,66) @0.08` | 1 | 76 | `--volt` | REPLACE | green family -> --volt (ΔE 0.0) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.01` | 1 | 16 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.01, so a tint of it (see DECIDE-0) |

### `scorr_health.html`

50 colours, 77 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 12 | 82, 86, 127 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#07111F` | 4 | 21, 22, 558 | `--chalk` | REPLACE | declared as `--ink` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --chalk · bound to `--mast`, `--ink` |
| `#1A9070` | 3 | 174, 435, 583 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#8892AA` | 3 | 435, 532, 579 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 2.0) |
| `rgba(255,255,255) @0.35` | 3 | 121, 132, 685 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `#0B6E42` | 2 | 24, 435 | `--volt` | REPLACE | declared as `--grn` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#1847DF` | 2 | 24, 435 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#4ED8A0` | 2 | 134, 450 | `--volt` | REPLACE | green family -> --volt (ΔE 61.9) |
| `#5340C2` | 2 | 176, 579 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 20.4) |
| `#5B667D` | 2 | 22, 435 | `--muted` | REPLACE | declared as `--muted` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--muted` |
| `rgba(255,255,255) @0.25` | 2 | 135, 377 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.45` | 2 | 125, 687 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `#060B16` | 1 | 30 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.8) · bound to `--mast` |
| `#0A0F1E` | 1 | 30 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#101830` | 1 | 30 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 4.8) · bound to `--paper` |
| `#2A3650` | 1 | 435 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 2.0) |
| `#2FD48B` | 1 | 33 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#4532A8` | 1 | 435 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.7) |
| `#4D7CFE` | 1 | 33 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 1 | 31 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--faint` |
| `#8C99BD` | 1 | 31 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--muted` |
| `#8F5C07` | 1 | 24 | `--amber` | REPLACE | orange family -> --amber (ΔE 34.8) · bound to `--amb` |
| `#9A3C1E` | 1 | 435 | `--heat` | REPLACE | red family -> --heat (ΔE 40.7) |
| `#B52432` | 1 | 24 | `--heat` | REPLACE | declared as `--red` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#B8BECC` | 1 | 22 | `--muted` | DECIDE | grey ramp value 16.2 ΔE from the nearest surface token — between steps · bound to `--faint` |
| `#E9EEFB` | 1 | 31 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--ink` |
| `#ECEEF3` | 1 | 21 | `--field` | REPLACE | declared as `--bg` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#F5B94A` | 1 | 33 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amb` |
| `#F8F9FC` | 1 | 21 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 5.2) · bound to `--paper` |
| `#FF5C6C` | 1 | 33 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(0,0,0) @0.35` | 1 | 895 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(11,110,66) @0.10` | 1 | 25 | `--volt` | REPLACE | green family -> --volt (ΔE 77.2) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--grn-t` |
| `rgba(143,92,7) @0.10` | 1 | 26 | `--amber` | REPLACE | orange family -> --amber (ΔE 34.8) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--amb-t` |
| `rgba(148,166,210) @0.05` | 1 | 32 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.05 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(148,166,210) @0.14` | 1 | 32 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(181,36,50) @0.03` | 1 | 27 | `--heat` | REPLACE | red family -> --heat (ΔE 24.0) · translucent 0.03, so a tint of it (see DECIDE-0) · bound to `--flagbg` |
| `rgba(181,36,50) @0.10` | 1 | 25 | `--heat` | REPLACE | red family -> --heat (ΔE 24.0) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--red-t` |
| `rgba(24,71,223) @0.10` | 1 | 26 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 16.4) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--blu-t` |
| `rgba(245,185,74) @0.12` | 1 | 35 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.12, so a tint of it (see DECIDE-0) · bound to `--amb-t` |
| `rgba(255,255,255) @0.18` | 1 | 128 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.20` | 1 | 378 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.30` | 1 | 375 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.70` | 1 | 128 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.70, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.75` | 1 | 374 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.75, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.05` | 1 | 36 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.05, so a tint of it (see DECIDE-0) · bound to `--flagbg` |
| `rgba(255,92,108) @0.12` | 1 | 34 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.12 — see DECIDE-0) · bound to `--red-t` |
| `rgba(47,212,139) @0.12` | 1 | 34 | `--volt` | REPLACE | declared as `--grn-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.12 — see DECIDE-0) · bound to `--grn-t` |
| `rgba(7,17,31) @0.03` | 1 | 23 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.03 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(7,17,31) @0.10` | 1 | 23 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.10 — see DECIDE-0) · bound to `--rule`, `--hover` |
| `rgba(77,124,254) @0.13` | 1 | 35 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.13, so a tint of it (see DECIDE-0) · bound to `--blu-t` |

### `scorr_holdings.html`

36 colours, 44 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#B45309` | 3 | 108, 403, 475 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.5) |
| `#C5CFE0` | 3 | 58, 609, 610 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 12.4) |
| `#FFFFFF` | 3 | 117, 549, 603 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(255,92,108) @0.13` | 2 | 64, 90 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 2 | 63, 89 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |
| `#0A0F1E` | 1 | 11 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0A9E63` | 1 | 567 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) |
| `#0D1428` | 1 | 14 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 11 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--card` |
| `#182241` | 1 | 14 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#2563EB` | 1 | 567 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#2FD48B` | 1 | 13 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#4D7CFE` | 1 | 13 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5A6781` | 1 | 603 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#5E6B8F` | 1 | 12 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#7A4F00` | 1 | 159 | `--amber` | REPLACE | orange family -> --amber (ΔE 42.2) |
| `#8A5A00` | 1 | 157 | `--amber` | REPLACE | orange family -> --amber (ΔE 36.2) |
| `#8C99BD` | 1 | 12 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#A06A00` | 1 | 160 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.6) |
| `#DD3A4A` | 1 | 567 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) |
| `#E9EEFB` | 1 | 12 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#EEF1F6` | 1 | 605 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.8) |
| `#F1F4F9` | 1 | 133 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.1) |
| `#F5B8BD` | 1 | 100 | `--heat` | REPLACE | red family -> --heat (ΔE 52.8) |
| `#F8FAFC` | 1 | 39 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 5.6) |
| `#FF5C6C` | 1 | 13 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(148,166,210) @0.14` | 1 | 11 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--border` |
| `rgba(148,166,210) @0.24` | 1 | 14 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line2` |
| `rgba(20,35,70) @0.12` | 1 | 143 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.10` | 1 | 156 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.35` | 1 | 156 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.40` | 1 | 64 | `--heat` | REPLACE | declared as `--red` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(37,99,235) @0.02` | 1 | 627 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.02, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.18` | 1 | 627 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 1 | 63 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 121 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_home.html`

35 colours, 46 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 5 | 85, 186, 187 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.10` | 4 | 68, 79, 103 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `#121A33` | 2 | 11, 11 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--card`, `--panel` |
| `#37D3E8` | 2 | 14, 193 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan`, `--accent` |
| `#7C3AED` | 2 | 190, 191 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) · bound to `--accent` |
| `rgba(47,212,139) @0.14` | 2 | 47, 64 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |
| `#0891B2` | 1 | 190 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 29.3 to aqua, 89.0 to pulse) · bound to `--accent` |
| `#0A0F1E` | 1 | 11 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 17 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#182241` | 1 | 17 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#1C2536` | 1 | 106 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#2563EB` | 1 | 191 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--accent` |
| `#2FD48B` | 1 | 14 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#4D7CFE` | 1 | 14 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 1 | 13 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#7F77DD` | 1 | 192 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 35.8) |
| `#8C99BD` | 1 | 13 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#B45309` | 1 | 94 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.5) |
| `#C98A12` | 1 | 556 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#DB2777` | 1 | 191 | `—` | DECIDE | magenta/pink — no token in the magenta family · bound to `--accent` |
| `#E9EEFB` | 1 | 13 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 14 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 14 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(148,166,210) @0.14` | 1 | 12 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 12 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(20,28,45) @0.25` | 1 | 75 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 2.5) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(20,28,45) @0.50` | 1 | 72 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 2.5) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(20,30,50) @0.04` | 1 | 180 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 0.6) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(20,30,50) @0.09` | 1 | 181 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 0.6) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.16` | 1 | 556 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.45` | 1 | 556 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.12` | 1 | 114 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.5) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 65 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(3,7,20) @0.50` | 1 | 16 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(77,124,254) @0.10` | 1 | 208 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.10, so a tint of it (see DECIDE-0) |

### `scorr_intraday.html`

29 colours, 34 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(148,166,210) @0.14` | 3 | 13, 26, 455 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(47,212,139) @0.14` | 3 | 15, 455, 460 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `#121A33` | 2 | 12, 25 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#0A0F1E` | 1 | 12 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 12 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#182241` | 1 | 12 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 15 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 17 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#4D7CFE` | 1 | 17 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 1 | 14 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 14 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#9B7CFF` | 1 | 18 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · bound to `--purp` |
| `#A7E3C6` | 1 | 15 | `--volt` | REPLACE | green family -> --volt (ΔE 69.3) · bound to `--grn-b` |
| `#C98A12` | 1 | 461 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#E9EEFB` | 1 | 14 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F0EBFF` | 1 | 18 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 6.5) · bound to `--purp-d` |
| `#F5B8BE` | 1 | 16 | `--heat` | REPLACE | red family -> --heat (ΔE 52.8) · bound to `--red-b` |
| `#F5B94A` | 1 | 17 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 16 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `rgba(10,15,30) @0.60` | 1 | 48 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.7) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 13 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(20,35,70) @0.06` | 1 | 27 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(240,80,80) @0.14` | 1 | 455 | `--heat` | REPLACE | red family -> --heat (ΔE 15.0) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.16` | 1 | 461 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.50` | 1 | 461 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.06` | 1 | 19 | `--field` | REPLACE | declared as `--shadow` (in `scorr_intraday.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) · bound to `--shadow` |
| `rgba(255,92,108) @0.13` | 1 | 16 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(255,92,108) @0.14` | 1 | 87 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.10` | 1 | 86 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.10, so a tint of it (see DECIDE-0) |

### `scorr_mobile.html`

16 colours, 19 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 3 | 12, 24, 36 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs · bound to `--card` |
| `#2563EB` | 2 | 6, 14 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#0891B2` | 1 | 14 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 29.3 to aqua, 89.0 to pulse) · bound to `--cyan` |
| `#0FA968` | 1 | 14 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#1C2536` | 1 | 13 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#5A6781` | 1 | 13 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#7C3AED` | 1 | 14 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) |
| `#8A96AD` | 1 | 13 | `--muted` | OK | already the canonical value · bound to `--dim` |
| `#D4DBE7` | 1 | 12 | `--edge` | REPLACE | declared as `--line2` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line2` |
| `#D97706` | 1 | 14 | `--amber` | REPLACE | declared as `--amber` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#DB2777` | 1 | 48 | `—` | DECIDE | magenta/pink — no token in the magenta family · bound to `--accent` |
| `#E23744` | 1 | 14 | `--heat` | REPLACE | declared as `--red` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#E4E9F1` | 1 | 12 | `--edge` | REPLACE | declared as `--line` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line` |
| `#F6F8FB` | 1 | 12 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs · bound to `--bg` |
| `rgba(20,30,50) @0.04` | 1 | 42 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 0.6) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 16 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) · bound to `--shadow` |

### `scorr_mobile_cards.js`

15 colours, 38 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#C8F542` | 8 | 205, 214, 216 | `--volt` | OK | already the canonical value |
| `#1C2740` | 4 | 196, 349, 368 | `--panel-hi` | OK | already the canonical value |
| `#0D1322` | 3 | 348, 369, 371 | `--field` | OK | already the canonical value |
| `#8A97B0` | 3 | 348, 369, 371 | `--muted` | OK | already the canonical value |
| `#FF4D6D` | 3 | 198, 210, 265 | `--heat` | OK | already the canonical value |
| `#FF8FA5` | 3 | 198, 206, 302 | `--heat` | REPLACE | red family -> --heat (ΔE 30.7) |
| `#18240E` | 2 | 203, 301 | `--field` | DECIDE | grey ramp value 27.4 ΔE from the nearest surface token — between steps |
| `#241019` | 2 | 204, 301 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 13.5) |
| `#3E5A18` | 2 | 203, 300 | `--volt` | REPLACE | green family -> --volt (ΔE 72.1) |
| `#5A2634` | 2 | 204, 300 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#EAF0FA` | 2 | 190, 195 | `--chalk` | OK | already the canonical value |
| `#26334F` | 1 | 348 | `--edge` | OK | already the canonical value |
| `#35E0FF` | 1 | 447 | `--aqua` | OK | already the canonical value |
| `#FF9F45` | 1 | 266 | `--amber` | OK | already the canonical value |
| `rgba(255,255,255) @0.02` | 1 | 396 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.02, so a tint of it (see DECIDE-0) |

### `scorr_news.html`

92 colours, 188 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 14 | 17, 180, 189 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#FFFFFF` | 8 | 51, 189, 222 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0D1428` | 7 | 18, 237, 238 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#29406E` | 6 | 233, 237, 238 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 12.6) |
| `#E8EEFC` | 6 | 220, 230, 237 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.2) |
| `#E9EEFB` | 6 | 16, 179, 182 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#0FA968` | 5 | 357, 357, 851 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#8A94A6` | 5 | 181, 185, 203 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) |
| `#8C99BD` | 5 | 16, 19, 187 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut`, `--tag-fg` |
| `#9FB0D6` | 5 | 221, 238, 246 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.7) |
| `rgba(148,166,210) @0.14` | 5 | 15, 179, 187 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#000000` | 4 | 66, 66, 67 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) |
| `#0A9E63` | 4 | 190, 190, 240 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) |
| `#121A33` | 4 | 15, 179, 187 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--card` |
| `#16223F` | 4 | 256, 274, 275 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.9) |
| `#B45309` | 4 | 191, 191, 241 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.5) |
| `#EEF3FF` | 4 | 263, 267, 271 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 1.6) |
| `#070B1A` | 3 | 217, 223, 894 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.4) |
| `#1C2B52` | 3 | 217, 244, 254 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 8.7) |
| `#7F92BD` | 3 | 250, 252, 259 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 10.4) |
| `rgba(148,166,210) @0.08` | 3 | 120, 195, 204 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(7,11,26) @0.00` | 3 | 219, 229, 895 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.4) · translucent 0.00, so a tint of it (see DECIDE-0) |
| `#182241` | 2 | 18, 199 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#2FD48B` | 2 | 17, 202 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#60A5FA` | 2 | 887, 900 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 59.0) |
| `#CBD3E0` | 2 | 172, 174 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 10.5) |
| `#E23744` | 2 | 358, 358 | `--heat` | REPLACE | declared as `--red` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --heat |
| `#F5B94A` | 2 | 17, 215 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `rgba(77,124,254) @0.14` | 2 | 196, 201 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |
| `#066B46` | 1 | 214 | `--muted` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#0A0F1E` | 1 | 15 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0A1226` | 1 | 275 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.7) |
| `#1D4ED8` | 1 | 351 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 20.2) |
| `#22D3EE` | 1 | 887 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 5.0) |
| `#23345C` | 1 | 278 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 8.0) |
| `#2A4D8F` | 1 | 231 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 58.5) |
| `#2B3D66` | 1 | 273 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 9.3) |
| `#34D399` | 1 | 887 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#37D3E8` | 1 | 18 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#5E6B8F` | 1 | 16 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#6D80AB` | 1 | 243 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.9) |
| `#7C3AED` | 1 | 352 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) |
| `#7FB0FF` | 1 | 272 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 61.5) |
| `#8095BD` | 1 | 280 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.7) |
| `#8A96AD` | 1 | 359 | `--muted` | OK | already the canonical value |
| `#8CA6E6` | 1 | 909 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 64.7) |
| `#9B7CFF` | 1 | 202 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) |
| `#9FC0FF` | 1 | 231 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 70.0) |
| `#A78BFA` | 1 | 887 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 34.0) |
| `#AEB9CC` | 1 | 175 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.2) |
| `#AEBBDD` | 1 | 264 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 81.0) |
| `#B3BCCB` | 1 | 194 | `--muted` | DECIDE | grey ramp value 15.0 ΔE from the nearest surface token — between steps |
| `#B3BCCD` | 1 | 157 | `--muted` | DECIDE | grey ramp value 14.7 ΔE from the nearest surface token — between steps |
| `#B9C4D6` | 1 | 188 | `--chalk` | DECIDE | grey ramp value 16.5 ΔE from the nearest surface token — between steps |
| `#B9C6E4` | 1 | 249 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 84.4) |
| `#C084FC` | 1 | 887 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 30.4) |
| `#C2620A` | 1 | 353 | `--amber` | REPLACE | orange family -> --amber (ΔE 22.4) |
| `#C3D6FB` | 1 | 351 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 84.0) |
| `#CDD8F2` | 1 | 233 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 12.0) |
| `#D3DDF3` | 1 | 265 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 9.3) |
| `#D6F3E6` | 1 | 214 | `--chalk` | DECIDE | grey ramp value 14.6 ΔE from the nearest surface token — between steps |
| `#DDC9FB` | 1 | 352 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 73.9) |
| `#E8F0FE` | 1 | 351 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.1) |
| `#F3ECFD` | 1 | 352 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 5.8) |
| `#F472B6` | 1 | 887 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#F6D4A8` | 1 | 353 | `--amber` | REPLACE | orange family -> --amber (ΔE 42.7) |
| `#FB7185` | 1 | 887 | `--heat` | REPLACE | red family -> --heat (ΔE 16.5) |
| `#FBBF24` | 1 | 887 | `--amber` | REPLACE | orange family -> --amber (ΔE 27.0) |
| `#FDF0E1` | 1 | 353 | `--chalk` | DECIDE | grey ramp value 14.5 ΔE from the nearest surface token — between steps |
| `#FF5C6C` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(0,0,0) @0.50` | 1 | 244 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.55` | 1 | 254 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(13,20,40) @0.90` | 1 | 233 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 4.0) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.04` | 1 | 121 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.05` | 1 | 206 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.05 — see DECIDE-0) |
| `rgba(148,166,210) @0.10` | 1 | 19 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--tag-bg` |
| `rgba(148,166,210) @0.12` | 1 | 199 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 15 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(155,124,255) @0.16` | 1 | 202 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 29 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(217,119,6) @0.14` | 1 | 96 | `--amber` | REPLACE | orange family -> --amber (ΔE 15.7) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(226,55,68) @0.12` | 1 | 95 | `--heat` | REPLACE | red family -> --heat (ΔE 15.2) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 215 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(30,45,90) @0.50` | 1 | 895 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 12.1) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.07` | 1 | 150 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.08` | 1 | 144 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.12` | 1 | 152 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.22` | 1 | 231 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 202 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(7,11,26) @0.72` | 1 | 219 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.4) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(7,11,26) @0.94` | 1 | 229 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.4) · translucent 0.94, so a tint of it (see DECIDE-0) |
| `rgba(90,103,129) @0.10` | 1 | 97 | `--muted` | DECIDE | grey ramp value 18.9 ΔE from the nearest surface token — between steps |

### `scorr_performance.html`

22 colours, 26 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 4 | 19, 19, 28 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#2FD48B` | 2 | 37, 47 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#065F46` | 1 | 51 | `--edge` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#0A0F1E` | 1 | 10 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 10 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 10 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 10 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#1E40AF` | 1 | 51 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 32.9) |
| `#5E6B8F` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#8C99BD` | 1 | 10 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#92400E` | 1 | 51 | `--amber` | REPLACE | orange family -> --amber (ΔE 40.0) |
| `#991B1B` | 1 | 51 | `--heat` | REPLACE | red family -> --heat (ΔE 34.4) |
| `#E9EEFB` | 1 | 10 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 38 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#FF5C6C` | 1 | 48 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `rgba(148,166,210) @0.14` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 15 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 51 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 51 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 51 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 51 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_result_corner.html`

43 colours, 54 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(255,92,108) @0.22` | 3 | 84, 421, 449 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.22` | 3 | 83, 421, 449 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 2 | 11, 53 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#3A4666` | 2 | 54, 260 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 8.8) |
| `#FFFFFF` | 2 | 20, 96 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs · bound to `--panel` |
| `rgba(255,92,108) @0.10` | 2 | 421, 449 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.16` | 2 | 421, 449 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.10` | 2 | 421, 449 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.16` | 2 | 421, 449 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `#0D1428` | 1 | 11 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#101828` | 1 | 22 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#121A33` | 1 | 11 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 11 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 14 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#37D3E8` | 1 | 14 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#4D7CFE` | 1 | 14 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5A6B82` | 1 | 22 | `--muted` | REPLACE | declared as `--mut` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#5E6B8F` | 1 | 13 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 13 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#94A3BE` | 1 | 22 | `--muted` | REPLACE | declared as `--dim` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#E9EEFB` | 1 | 13 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#EAEEF6` | 1 | 20 | `--chalk` | OK | already the canonical value · bound to `--bg2` |
| `#F1F4FA` | 1 | 20 | `--panel-hi` | REPLACE | declared as `--panel2` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#F4F6FB` | 1 | 20 | `--field` | REPLACE | declared as `--bg` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#F5B94A` | 1 | 14 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 14 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `rgba(0,0,0) @0.50` | 1 | 150 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 12 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 12 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(15,157,88) @0.10` | 1 | 23 | `--volt` | REPLACE | declared as `--grn-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.10 — see DECIDE-0) · bound to `--grn-d` |
| `rgba(20,40,90) @0.10` | 1 | 21 | `--edge` | REPLACE | declared as `--line` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.10 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(20,40,90) @0.18` | 1 | 21 | `--edge` | REPLACE | declared as `--line` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.18 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(208,67,59) @0.10` | 1 | 23 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.10 — see DECIDE-0) · bound to `--red-d` |
| `rgba(245,185,74) @0.12` | 1 | 87 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.06` | 1 | 84 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.12` | 1 | 15 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.12 — see DECIDE-0) · bound to `--red-d` |
| `rgba(255,92,108) @0.13` | 1 | 84 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(47,212,139) @0.06` | 1 | 83 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.12` | 1 | 15 | `--volt` | REPLACE | declared as `--grn-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.12 — see DECIDE-0) · bound to `--grn-d` |
| `rgba(47,212,139) @0.13` | 1 | 83 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.07` | 1 | 79 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.35` | 1 | 102 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(6,10,20) @0.62` | 1 | 148 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.8) · translucent 0.62, so a tint of it (see DECIDE-0) |

### `scorr_scanners.html`

25 colours, 31 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 5 | 19, 19, 28 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#2FD48B` | 2 | 34, 48 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `rgba(55,211,232) @0.14` | 2 | 57, 58 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) |
| `#065F46` | 1 | 52 | `--edge` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#0A0F1E` | 1 | 10 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 10 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 10 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 10 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#1E40AF` | 1 | 53 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 32.9) |
| `#37D3E8` | 1 | 56 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) |
| `#5E6B8F` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#8C99BD` | 1 | 10 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#92400E` | 1 | 54 | `--amber` | REPLACE | orange family -> --amber (ΔE 40.0) |
| `#991B1B` | 1 | 55 | `--heat` | REPLACE | red family -> --heat (ΔE 34.4) |
| `#E9EEFB` | 1 | 10 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 35 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#FF5C6C` | 1 | 49 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `#FFFFFF` | 1 | 56 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.14` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 15 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 54 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 55 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 52 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 53 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_scheduler_master.html`

13 colours, 13 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0A0F1E` | 1 | 9 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 9 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 9 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--card` |
| `#2FD48B` | 1 | 10 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--green` |
| `#4D7CFE` | 1 | 10 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#5E6B8F` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 10 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E9EEFB` | 1 | 10 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--text` |
| `#F5B94A` | 1 | 10 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 10 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#FFFFFF` | 1 | 24 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.14` | 1 | 9 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line` |
| `rgba(148,166,210) @0.30` | 1 | 23 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.30, so a tint of it (see DECIDE-0) |

### `scorr_screeners.html`

16 colours, 20 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#243044` | 2 | 9, 9 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line` |
| `#E8ECF2` | 2 | 7, 7 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line`, `--line2` |
| `#F6F8FB` | 2 | 7, 7 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs · bound to `--bg`, `--card2` |
| `#FFFFFF` | 2 | 7, 24 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0E1420` | 1 | 9 | `--field` | REPLACE | declared as `--bg` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0F9D58` | 1 | 8 | `--volt` | REPLACE | declared as `--grn` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#141C2B` | 1 | 9 | `--panel` | REPLACE | declared as `--panel` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182131` | 1 | 9 | `--panel-hi` | REPLACE | declared as `--card2` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--card2` |
| `#1C2536` | 1 | 8 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#2F6DF4` | 1 | 8 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#667085` | 1 | 8 | `--muted` | REPLACE | declared as `--mut` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#6C7C93` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#93A0B5` | 1 | 10 | `--muted` | REPLACE | declared as `--mut` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#9AA4B5` | 1 | 8 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#D0433B` | 1 | 8 | `--heat` | REPLACE | declared as `--red` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#E8EEF7` | 1 | 10 | `--chalk` | OK | already the canonical value · bound to `--txt` |

### `scorr_sector.html`

34 colours, 79 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#9B7CFF` | 7 | 36, 36, 66 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) |
| `#FFFFFF` | 7 | 66, 126, 162 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#2FD48B` | 6 | 66, 67, 87 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#F5B94A` | 6 | 66, 66, 67 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber |
| `#4D7CFE` | 5 | 19, 66, 67 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse |
| `#FF5C6C` | 5 | 66, 67, 87 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat |
| `rgba(148,166,210) @0.07` | 5 | 21, 376, 384 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `#FECACA` | 3 | 136, 192, 548 | `--heat` | REPLACE | red family -> --heat (ΔE 58.5) |
| `#FFF5F5` | 3 | 192, 332, 548 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 8.0) |
| `#166534` | 2 | 188, 458 | `--volt` | REPLACE | green family -> --volt (ΔE 76.6) |
| `#37D3E8` | 2 | 399, 399 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) |
| `#BBF7D0` | 2 | 183, 458 | `--volt` | REPLACE | green family -> --volt (ΔE 64.3) |
| `#DDD6FE` | 2 | 176, 179 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 81.1) |
| `#F5F3FF` | 2 | 162, 176 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.4) |
| `rgba(148,166,210) @0.12` | 2 | 20, 465 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.08` | 2 | 183, 458 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 1 | 13 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 13 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#121A33` | 1 | 13 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 13 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#5E6B8F` | 1 | 13 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#8C99BD` | 1 | 13 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#991B1B` | 1 | 197 | `--heat` | REPLACE | red family -> --heat (ΔE 34.4) |
| `#E9EEFB` | 1 | 13 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `rgba(0,0,0) @0.04` | 1 | 436 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.06` | 1 | 436 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(10,158,99) @0.12` | 1 | 95 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 13 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 13 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 30 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(221,58,74) @0.12` | 1 | 95 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.12` | 1 | 524 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.5) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.08` | 1 | 136 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 369 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_structure.html`

31 colours, 95 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 9 | 13, 26, 28 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#2563EB` | 8 | 17, 17, 27 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#5A6781` | 8 | 15, 36, 59 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#8A94A6` | 7 | 22, 34, 53 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) |
| `#E4E9F1` | 7 | 13, 19, 28 | `--edge` | REPLACE | declared as `--line` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --edge |
| `#DC2626` | 6 | 38, 74, 92 | `--heat` | REPLACE | red family -> --heat (ΔE 27.6) |
| `#059669` | 5 | 37, 74, 210 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#F1F4F9` | 5 | 30, 69, 80 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.1) |
| `#065F46` | 3 | 46, 61, 63 | `--edge` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#1C2536` | 3 | 11, 16, 95 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#991B1B` | 3 | 48, 62, 65 | `--heat` | REPLACE | red family -> --heat (ΔE 34.4) |
| `#CBD5E1` | 3 | 217, 255, 256 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 9.9) |
| `#D1FAE5` | 3 | 46, 61, 63 | `--volt` | REPLACE | green family -> --volt (ΔE 73.3) |
| `#FEE2E2` | 3 | 48, 62, 65 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 13.5) |
| `#3A455C` | 2 | 47, 50 | `--edge` | REPLACE | surface/text ramp family -> --edge (ΔE 8.9) |
| `#92400E` | 2 | 64, 84 | `--amber` | REPLACE | orange family -> --amber (ΔE 40.0) |
| `#DDE2ED` | 2 | 26, 35 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.9) |
| `#E5E9F2` | 2 | 47, 66 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.5) |
| `rgba(20,35,70) @0.05` | 2 | 43, 57 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `#1D4ED8` | 1 | 40 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 20.2) |
| `#1E40AF` | 1 | 49 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 32.9) |
| `#A0AEC0` | 1 | 41 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 9.4) |
| `#B6BDCC` | 1 | 88 | `--muted` | DECIDE | grey ramp value 15.6 ΔE from the nearest surface token — between steps |
| `#DBEAFE` | 1 | 49 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 6.4) |
| `#EEF3FF` | 1 | 160 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 1.6) |
| `#F7F8FC` | 1 | 11 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.6) |
| `#F8FAFF` | 1 | 31 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.7) |
| `#FDE68A` | 1 | 84 | `--amber` | DECIDE | yellow — yellow sits between --amber and --volt |
| `#FEF3C7` | 1 | 64 | `--chalk` | DECIDE | yellow — yellow sits between --amber and --volt |
| `rgba(20,35,70) @0.06` | 1 | 13 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(20,35,70) @0.12` | 1 | 28 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.12, so a tint of it (see DECIDE-0) |

### `scorr_tc_v4_scan.html`

22 colours, 22 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0A0F1E` | 1 | 7 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 7 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#121A33` | 1 | 7 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 7 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 10 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 12 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#4D7CFE` | 1 | 12 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 1 | 9 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 9 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E9EEFB` | 1 | 9 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 12 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 11 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `rgba(148,166,210) @0.14` | 1 | 8 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 8 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(245,185,74) @0.09` | 1 | 66 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 45 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,92,108) @0.07` | 1 | 67 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 11 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(4,8,20) @0.72` | 1 | 50 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.4) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.06` | 1 | 36 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.09` | 1 | 65 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 10 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |

### `scorr_theme_r5.css`

28 colours, 34 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0A101E` | 2 | 158, 231 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 1.8) |
| `#111829` | 2 | 61, 65 | `--field` | REPLACE | declared as `--bg2` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2`, `--well` |
| `#35E0FF` | 2 | 46, 261 | `--aqua` | OK | defines the token · bound to `--r5-aqua` |
| `#7C5CFF` | 2 | 45, 262 | `--pulse` | OK | defines the token · bound to `--r5-pulse` |
| `#C8F542` | 2 | 43, 260 | `--volt` | OK | defines the token · bound to `--r5-volt` |
| `#EAF0FA` | 2 | 23, 41 | `--chalk` | OK | defines the token · bound to `--bg`, `--r5-chalk` |
| `#0A0F1C` | 1 | 61 | `--field` | REPLACE | declared as `--bg3` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --field · bound to `--bg3` |
| `#0D1322` | 1 | 37 | `--field` | OK | defines the token · bound to `--r5-field` |
| `#161F33` | 1 | 38 | `--panel` | OK | defines the token · bound to `--r5-panel` |
| `#18240E` | 1 | 206 | `--field` | DECIDE | grey ramp value 27.4 ΔE from the nearest surface token — between steps |
| `#1C2740` | 1 | 39 | `--panel-hi` | OK | defines the token · bound to `--r5-panel-hi` |
| `#241019` | 1 | 207 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 13.5) |
| `#26334F` | 1 | 40 | `--edge` | OK | defines the token · bound to `--r5-edge` |
| `#3E5A18` | 1 | 206 | `--volt` | REPLACE | green family -> --volt (ΔE 72.1) |
| `#5A2634` | 1 | 207 | `—` | DECIDE | magenta/pink — no token in the magenta family |
| `#6B7893` | 1 | 72 | `--muted` | REPLACE | declared as `--dim` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8A97B0` | 1 | 42 | `--muted` | OK | defines the token · bound to `--r5-muted` |
| `#9B82FF` | 1 | 150 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 25.6) |
| `#F4F7FE` | 1 | 23 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x1, `--chalk` x1) — one value doing two jobs · bound to `--bg` |
| `#F5B94A` | 1 | 83 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF4D6D` | 1 | 44 | `--heat` | OK | defines the token · bound to `--r5-heat` |
| `#FF8FA5` | 1 | 204 | `--heat` | REPLACE | red family -> --heat (ΔE 30.7) |
| `#FFFFFF` | 1 | 23 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs · bound to `--bg` |
| `rgba(200,245,66) @0.14` | 1 | 77 | `--volt` | REPLACE | declared as `--grn-d` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn-d` |
| `rgba(255,255,255) @0.01` | 1 | 92 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.01, so a tint of it (see DECIDE-0) |
| `rgba(255,77,109) @0.13` | 1 | 79 | `--heat` | REPLACE | declared as `--red-d` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red-d` |
| `rgba(3,7,20) @0.55` | 1 | 84 | `--field` | REPLACE | declared as `--shadow` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.55 — see DECIDE-0) · bound to `--shadow` |
| `rgba(38,51,79) @0.62` | 1 | 68 | `--edge` | REPLACE | declared as `--line` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.62 — see DECIDE-0) · bound to `--line` |

### `scorr_v10_signal.html`

12 colours, 12 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#0D1322` | 1 | 22 | `--field` | OK | already the canonical value · bound to `--field` |
| `#161F33` | 1 | 22 | `--panel` | OK | already the canonical value · bound to `--panel` |
| `#1C2740` | 1 | 22 | `--panel-hi` | OK | already the canonical value · bound to `--panel-hi` |
| `#26334F` | 1 | 22 | `--edge` | OK | already the canonical value · bound to `--edge` |
| `#7C5CFF` | 1 | 27 | `--pulse` | OK | already the canonical value · bound to `--pulse` |
| `#8A97B0` | 1 | 23 | `--muted` | OK | already the canonical value · bound to `--muted` |
| `#C8F542` | 1 | 24 | `--volt` | OK | already the canonical value · bound to `--volt` |
| `#EAF0FA` | 1 | 23 | `--chalk` | OK | already the canonical value · bound to `--chalk` |
| `#FF4D6D` | 1 | 25 | `--heat` | OK | already the canonical value · bound to `--heat` |
| `#FF9F45` | 1 | 26 | `--amber` | OK | already the canonical value · bound to `--amber` |
| `rgba(22,31,51) @0.50` | 1 | 58 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 0.0) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.01` | 1 | 32 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.01, so a tint of it (see DECIDE-0) |

### `scorr_v12.html`

19 colours, 24 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#4D7CFE` | 3 | 11, 259, 276 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 3 | 9, 258, 276 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#FFFFFF` | 2 | 26, 38 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0A0F1E` | 1 | 7 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 7 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#121A33` | 1 | 7 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 7 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 10 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 11 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#8C99BD` | 1 | 9 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E9EEFB` | 1 | 9 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 11 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 10 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `rgba(148,166,210) @0.14` | 1 | 8 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 8 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(245,185,74) @0.14` | 1 | 44 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 10 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(47,212,139) @0.14` | 1 | 10 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `rgba(77,124,254) @0.14` | 1 | 45 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_v13.html`

26 colours, 28 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 2 | 84, 87 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.14` | 2 | 10, 90 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#0A0F1E` | 1 | 9 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 9 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#121A33` | 1 | 9 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 9 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 12 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 14 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#4D7CFE` | 1 | 14 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 1 | 11 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 11 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E9EEFB` | 1 | 11 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 14 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 13 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `rgba(148,166,210) @0.04` | 1 | 56 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 1 | 10 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(245,185,74) @0.13` | 1 | 72 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 80 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 13 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(3,7,20) @0.50` | 1 | 15 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(4,8,20) @0.72` | 1 | 130 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.4) · translucent 0.72, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 12 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `rgba(77,124,254) @0.05` | 1 | 74 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.07` | 1 | 57 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.08` | 1 | 103 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 1 | 83 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |

### `scorr_v14.html`

29 colours, 34 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#F5B94A` | 3 | 16, 85, 85 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#182241` | 2 | 12, 85 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#4D7CFE` | 2 | 16, 86 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `rgba(77,124,254) @0.12` | 2 | 30, 52 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 1 | 12 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 12 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#121A33` | 1 | 12 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#2FD48B` | 1 | 15 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 16 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#5E6B8F` | 1 | 14 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 14 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#9B7CFF` | 1 | 16 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · bound to `--violet` |
| `#E9EEFB` | 1 | 14 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#FF5C6C` | 1 | 15 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `#FFFFFF` | 1 | 64 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.06` | 1 | 46 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 13 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 13 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(155,124,255) @0.12` | 1 | 32 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 15 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(255,92,108) @0.30` | 1 | 251 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(3,7,20) @0.50` | 1 | 17 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(47,212,139) @0.14` | 1 | 15 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `rgba(47,212,139) @0.30` | 1 | 251 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.12` | 1 | 31 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(6,10,22) @0.78` | 1 | 71 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.6) · translucent 0.78, so a tint of it (see DECIDE-0) |
| `rgba(6,10,22) @0.94` | 1 | 54 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.6) · translucent 0.94, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.22` | 1 | 53 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.28` | 1 | 251 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.28, so a tint of it (see DECIDE-0) |

### `scorr_v15.html`

81 colours, 121 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 13 | 19, 22, 22 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs · bound to `--surface`, `--surface2` |
| `rgba(13,20,40) @0.75` | 4 | 125, 133, 156 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 4.0) · translucent 0.75, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.10` | 4 | 23, 167, 170 | `--edge` | REPLACE | declared as `--line` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.10 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#0A0F1E` | 3 | 10, 15, 27 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--ink`, `--on-accent` |
| `rgba(20,35,80) @0.04` | 3 | 168, 172, 205 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 72.5) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(3,7,20) @0.60` | 3 | 64, 152, 228 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 6.2) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.10` | 3 | 33, 106, 154 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.35` | 3 | 106, 150, 154 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `#0E1630` | 2 | 19, 24 | `--chalk` | REPLACE | declared as `--ink` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--text` |
| `#F4F7FE` | 2 | 19, 22 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x1, `--chalk` x1) — one value doing two jobs · bound to `--ink` |
| `#FAFBFF` | 2 | 169, 171 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 5.3) |
| `rgba(148,166,210) @0.04` | 2 | 36, 37 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.30` | 2 | 94, 198 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.92` | 2 | 166, 183 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.92, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.30` | 2 | 95, 199 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.07` | 2 | 34, 52 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.30` | 2 | 93, 197 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.35` | 2 | 52, 82 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.10` | 2 | 114, 202 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.35` | 2 | 60, 149 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `#000000` | 1 | 38 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) |
| `#0D1428` | 1 | 10 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--well` |
| `#0E8FA8` | 1 | 26 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 29.9 to aqua, 93.2 to pulse) · bound to `--cyan` |
| `#0FA968` | 1 | 25 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--bull`, `--bull-soft` |
| `#121A33` | 1 | 10 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--surface` |
| `#182241` | 1 | 10 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#2F6DF4` | 1 | 534 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#2FD48B` | 1 | 13 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--bull`, `--bull-soft` |
| `#37D3E8` | 1 | 14 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#3D6BEC` | 1 | 26 | `--pulse` | REPLACE | declared as `--blu` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blue` |
| `#4D7CFE` | 1 | 14 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blue` |
| `#5B6B94` | 1 | 24 | `--muted` | REPLACE | declared as `--mut` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#5E6B8F` | 1 | 12 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8B96B8` | 1 | 24 | `--muted` | REPLACE | declared as `--dim` (in `scorr_v15.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 12 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#B8791A` | 1 | 26 | `--amber` | REPLACE | declared as `--amber` (in `scorr_v15.html`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber`, `--amber-soft` |
| `#D93B4A` | 1 | 25 | `--heat` | REPLACE | red family -> --heat (ΔE 13.8) · bound to `--bear`, `--bear-soft` |
| `#E9EEFB` | 1 | 12 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--text` |
| `#EAF0FB` | 1 | 22 | `--chalk` | OK | already the canonical value |
| `#F5B94A` | 1 | 14 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber`, `--amber-soft` |
| `#F7F9FE` | 1 | 167 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.4) |
| `#FF5C6C` | 1 | 13 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--bear`, `--bear-soft` |
| `rgba(13,20,40) @0.70` | 1 | 79 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 4.0) · translucent 0.70, so a tint of it (see DECIDE-0) |
| `rgba(13,20,40) @0.88` | 1 | 152 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 4.0) · translucent 0.88, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.05` | 1 | 111 | `--edge` | REPLACE | declared as `--rule` (in `scorr_adaptive.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.05 — see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 11 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 11 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.25` | 1 | 69 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(15,169,104) @0.12` | 1 | 25 | `--volt` | REPLACE | declared as `--grn-d` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.12 — see DECIDE-0) · bound to `--bull`, `--bull-soft` |
| `rgba(15,23,48) @0.94` | 1 | 64 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 5.7) · translucent 0.94, so a tint of it (see DECIDE-0) |
| `rgba(18,26,51) @0.70` | 1 | 147 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 4.7) · translucent 0.70, so a tint of it (see DECIDE-0) |
| `rgba(18,26,51) @0.80` | 1 | 57 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 4.7) · translucent 0.80, so a tint of it (see DECIDE-0) |
| `rgba(18,26,51) @0.85` | 1 | 104 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 4.7) · translucent 0.85, so a tint of it (see DECIDE-0) |
| `rgba(18,26,51) @0.90` | 1 | 85 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 4.7) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(184,121,26) @0.12` | 1 | 26 | `--amber` | REPLACE | declared as `--amber` (in `scorr_v15.html`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.12 — see DECIDE-0) · bound to `--amber`, `--amber-soft` |
| `rgba(20,35,80) @0.05` | 1 | 169 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 72.5) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.16` | 1 | 184 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 72.5) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(20,35,80) @0.18` | 1 | 23 | `--edge` | REPLACE | declared as `--line` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.18 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(217,59,74) @0.10` | 1 | 25 | `--heat` | REPLACE | red family -> --heat (ΔE 13.8) · translucent 0.10, so a tint of it (see DECIDE-0) · bound to `--bear`, `--bear-soft` |
| `rgba(233,238,251) @0.05` | 1 | 64 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 1.7) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(24,34,65) @0.55` | 1 | 104 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 5.1) · translucent 0.55, so a tint of it (see DECIDE-0) |
| `rgba(24,34,65) @0.65` | 1 | 85 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 5.1) · translucent 0.65, so a tint of it (see DECIDE-0) |
| `rgba(24,34,65) @0.90` | 1 | 64 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 5.1) · translucent 0.90, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 14 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) · bound to `--amber`, `--amber-soft` |
| `rgba(245,185,74) @0.25` | 1 | 119 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.35` | 1 | 80 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.60` | 1 | 167 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.60, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 13 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--bear`, `--bear-soft` |
| `rgba(255,92,108) @0.25` | 1 | 120 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(3,7,20) @0.55` | 1 | 86 | `--field` | REPLACE | declared as `--shadow` (in `scorr_theme_r5.css`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.55 — see DECIDE-0) |
| `rgba(47,212,139) @0.13` | 1 | 65 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 13 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--bull`, `--bull-soft` |
| `rgba(47,212,139) @0.25` | 1 | 118 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.25, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.12` | 1 | 151 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.15` | 1 | 106 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.20` | 1 | 151 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.30` | 1 | 155 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.16` | 1 | 32 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.18` | 1 | 58 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.18, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.28` | 1 | 114 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.40` | 1 | 61 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.40, so a tint of it (see DECIDE-0) |

### `scorr_v9.html`

24 colours, 25 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(245,185,74) @0.12` | 2 | 112, 149 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 1 | 13 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0D1428` | 1 | 13 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2` |
| `#121A33` | 1 | 13 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182241` | 1 | 13 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2` |
| `#2FD48B` | 1 | 16 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d` |
| `#37D3E8` | 1 | 18 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#4D7CFE` | 1 | 18 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 1 | 15 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#8C99BD` | 1 | 15 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#E9EEFB` | 1 | 15 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#F5B94A` | 1 | 18 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `#FF5C6C` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d` |
| `rgba(148,166,210) @0.07` | 1 | 41 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 1 | 14 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(148,166,210) @0.24` | 1 | 14 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(245,185,74) @0.08` | 1 | 50 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.30` | 1 | 51 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.40` | 1 | 52 | `--amber` | REPLACE | declared as `--amber` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(255,92,108) @0.13` | 1 | 17 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(3,7,20) @0.50` | 1 | 19 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(47,212,139) @0.14` | 1 | 16 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `rgba(55,211,232) @0.12` | 1 | 148 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.12` | 1 | 147 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |

### `screener.html`

21 colours, 56 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(148,166,210) @0.14` | 11 | 11, 15, 20 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `#121A33` | 7 | 11, 15, 24 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel |
| `rgba(148,166,210) @0.08` | 6 | 34, 47, 68 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `#8C99BD` | 5 | 11, 17, 37 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted |
| `#FFFFFF` | 3 | 36, 40, 58 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(77,124,254) @0.14` | 3 | 35, 84, 90 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |
| `#0D1428` | 2 | 11, 83 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field |
| `#2FD48B` | 2 | 11, 90 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--green` |
| `#4D7CFE` | 2 | 11, 90 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blue` |
| `#E9EEFB` | 2 | 11, 18 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk |
| `#F5B94A` | 2 | 11, 90 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--gold` |
| `#FF5C6C` | 2 | 11, 90 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#0A0F1E` | 1 | 11 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#182241` | 1 | 11 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi |
| `rgba(148,166,210) @0.30` | 1 | 57 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 15 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(20,35,70) @0.10` | 1 | 42 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 7.5) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 1 | 90 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,255,255) @0.28` | 1 | 43 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.14` | 1 | 90 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 1 | 90 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) |

### `scrub_layer.js`

5 colours, 6 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 2 | 51, 56 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#0F172A` | 1 | 56 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 3.7) |
| `#64748B` | 1 | 37 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 13.9) |
| `#E2E8F0` | 1 | 57 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.0) |
| `rgba(0,0,0) @0.14` | 1 | 57 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.14, so a tint of it (see DECIDE-0) |

### `test_cio_endpoints.py`

15 colours, 29 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#8B949E` | 5 | 298, 305, 311 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.6) |
| `#E6EDF3` | 5 | 296, 300, 306 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 2.4) |
| `#161B22` | 3 | 300, 305, 309 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.0) |
| `#30363D` | 3 | 300, 305, 309 | `--edge` | DECIDE | grey ramp value 14.3 ΔE from the nearest surface token — between steps |
| `#0D1117` | 2 | 296, 312 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 7.3) |
| `#2F81F7` | 2 | 301, 306 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 37.5) |
| `#1F6FEB @0.20` | 1 | 315 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 31.1) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `#21262D` | 1 | 312 | `--panel` | REPLACE | surface/text ramp family -> --panel (ΔE 10.1) |
| `#238636` | 1 | 302 | `--volt` | REPLACE | green family -> --volt (ΔE 59.3) |
| `#238636 @0.20` | 1 | 316 | `--volt` | REPLACE | green family -> --volt (ΔE 59.3) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `#3FB950` | 1 | 316 | `--volt` | REPLACE | green family -> --volt (ΔE 44.5) |
| `#58A6FF` | 1 | 315 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 45.2) |
| `#F85149` | 1 | 317 | `--heat` | REPLACE | red family -> --heat (ΔE 19.6) |
| `#F85149 @0.20` | 1 | 317 | `--heat` | REPLACE | red family -> --heat (ΔE 19.6) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `#FFFFFF` | 1 | 302 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |

### `trade_wall_web.html`

18 colours, 23 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#243044` | 2 | 9, 9 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line` |
| `#E8ECF2` | 2 | 7, 7 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge · bound to `--line`, `--line2` |
| `#F6F8FB` | 2 | 7, 7 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs · bound to `--bg`, `--card2` |
| `#FFFFFF` | 2 | 7, 23 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(15,157,88) @0.12` | 2 | 41, 42 | `--volt` | REPLACE | green family -> --volt (ΔE 61.2) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `#0E1420` | 1 | 9 | `--field` | REPLACE | declared as `--bg` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#0F9D58` | 1 | 8 | `--volt` | REPLACE | declared as `--grn` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#141C2B` | 1 | 9 | `--panel` | REPLACE | declared as `--panel` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel` |
| `#182131` | 1 | 9 | `--panel-hi` | REPLACE | declared as `--card2` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--card2` |
| `#1C2536` | 1 | 8 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `#2F6DF4` | 1 | 8 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#667085` | 1 | 8 | `--muted` | REPLACE | declared as `--mut` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#6C7C93` | 1 | 10 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#93A0B5` | 1 | 10 | `--muted` | REPLACE | declared as `--mut` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#9AA4B5` | 1 | 8 | `--muted` | REPLACE | declared as `--dim` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#D0433B` | 1 | 8 | `--heat` | REPLACE | declared as `--red` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#E8EEF7` | 1 | 10 | `--chalk` | OK | already the canonical value · bound to `--txt` |
| `rgba(208,67,59) @0.12` | 1 | 43 | `--heat` | REPLACE | red family -> --heat (ΔE 22.3) · translucent 0.12, so a tint of it (see DECIDE-0) |

### `v10_dashboard.html`

27 colours, 55 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FF5C6C` | 7 | 22, 245, 1010 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red` |
| `#2FD48B` | 6 | 22, 1010, 1014 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn` |
| `#F5B94A` | 5 | 22, 208, 208 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `rgba(148,166,210) @0.14` | 4 | 20, 252, 1048 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#0A0F1E` | 3 | 19, 219, 1046 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg` |
| `#121A33` | 3 | 19, 19, 253 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--card`, `--panel` |
| `#0D1428` | 2 | 19, 23 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--card2`, `--well` |
| `#182241` | 2 | 23, 208 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--surface2` |
| `#4D7CFE` | 2 | 22, 209 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#8C99BD` | 2 | 21, 1046 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `#FFFFFF` | 2 | 223, 984 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `rgba(148,166,210) @0.06` | 2 | 1047, 1047 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `#0891B2` | 1 | 247 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 29.3 to aqua, 89.0 to pulse) |
| `#0FA968` | 1 | 244 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#10B981` | 1 | 714 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#1C2536` | 1 | 250 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#2563EB` | 1 | 246 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#37D3E8` | 1 | 22 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#5A6781` | 1 | 251 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#5E6B8F` | 1 | 21 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#7C3AED` | 1 | 249 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) |
| `#9B7CFF` | 1 | 22 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) |
| `#D97706` | 1 | 248 | `--amber` | REPLACE | declared as `--amber` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --amber |
| `#E9EEFB` | 1 | 21 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `rgba(148,166,210) @0.24` | 1 | 20 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(3,7,20) @0.50` | 1 | 26 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(37,99,235) @0.03` | 1 | 173 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.03, so a tint of it (see DECIDE-0) |

### `v8_dashboard.html`

142 colours, 332 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#FFFFFF` | 14 | 88, 252, 511 | `--panel` | DECIDE | same value is declared under legacy names that map to different tokens (`--panel` x3, `--field` x1, `--panel-hi` x1) — one value doing two jobs |
| `#FF5C6C` | 12 | 14, 18, 322 | `--heat` | REPLACE | declared as `--red` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --heat · bound to `--red`, `--red-d`, `--bear` |
| `#2FD48B` | 11 | 13, 18, 322 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt · bound to `--grn`, `--grn-d`, `--bull` |
| `#0A9E63` | 10 | 1981, 1982, 2018 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) |
| `#4D7CFE` | 10 | 15, 322, 1738 | `--pulse` | REPLACE | declared as `--blu` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --pulse · bound to `--blu` |
| `#5E6B8F` | 9 | 12, 321, 594 | `--muted` | REPLACE | declared as `--dim` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--dim` |
| `#DD3A4A` | 9 | 1981, 1982, 2019 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) |
| `#F5B94A` | 8 | 15, 322, 2289 | `--amber` | REPLACE | declared as `--amber` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --amber · bound to `--amber` |
| `rgba(148,166,210) @0.07` | 7 | 161, 161, 165 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.14` | 7 | 11, 320, 590 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) · bound to `--line`, `--line2` |
| `#121A33` | 6 | 10, 17, 319 | `--panel` | REPLACE | declared as `--panel` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel · bound to `--panel`, `--surface`, `--card` |
| `#1C2536` | 6 | 275, 1727, 1736 | `--chalk` | REPLACE | declared as `--txt` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --chalk |
| `#8C99BD` | 6 | 12, 321, 655 | `--muted` | REPLACE | declared as `--mut` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --muted · bound to `--mut` |
| `rgba(245,185,74) @0.12` | 6 | 242, 308, 314 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `#0A0F1E` | 5 | 10, 17, 319 | `--field` | REPLACE | declared as `--bg` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --field · bound to `--bg`, `--ink` |
| `#0D1428` | 5 | 10, 17, 319 | `--field` | REPLACE | declared as `--well` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --field · bound to `--bg2`, `--well`, `--card2` |
| `#8A94A6` | 5 | 4106, 4109, 4164 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) |
| `#D92D3C` | 5 | 673, 4350, 4372 | `--heat` | REPLACE | red family -> --heat (ΔE 17.6) |
| `rgba(77,124,254) @0.12` | 5 | 477, 499, 567 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `#000000` | 4 | 207, 207, 208 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) |
| `#8A94AD` | 4 | 2018, 2052, 2120 | `--muted` | OK | already the canonical value |
| `#D97706` | 4 | 52, 52, 53 | `--amber` | REPLACE | declared as `--amber` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --amber |
| `rgba(140,153,189) @0.14` | 4 | 1665, 1667, 1669 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 6.3) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.06` | 4 | 493, 3783, 5199 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.14` | 4 | 13, 169, 232 | `--volt` | REPLACE | declared as `--grn` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.14 — see DECIDE-0) · bound to `--grn`, `--grn-d` |
| `#182241` | 3 | 10, 17, 323 | `--panel-hi` | REPLACE | declared as `--panel2` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --panel-hi · bound to `--panel2`, `--surface2` |
| `#2563EB` | 3 | 276, 276, 4403 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --pulse |
| `#37D3E8` | 3 | 15, 322, 2325 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) · bound to `--cyan` |
| `#5A6B82` | 3 | 1726, 1729, 1974 | `--muted` | REPLACE | declared as `--mut` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --muted |
| `#639922` | 3 | 2567, 2585, 2840 | `--volt` | REPLACE | green family -> --volt (ΔE 40.5) |
| `#E24B4A` | 3 | 2567, 2585, 2840 | `--heat` | REPLACE | red family -> --heat (ΔE 16.7) |
| `#E9EEFB` | 3 | 12, 321, 593 | `--chalk` | REPLACE | declared as `--txt` (in `mobile_endpoints.py`), which scorr_theme_r5.css already re-points to --chalk · bound to `--txt` |
| `rgba(0,0,0) @0.22` | 3 | 282, 1723, 1902 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.24` | 3 | 11, 320, 646 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.24 — see DECIDE-0) · bound to `--line`, `--line2` |
| `rgba(255,92,108) @0.13` | 3 | 14, 2721, 3852 | `--heat` | REPLACE | declared as `--red` (in `scorr_ask.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.13 — see DECIDE-0) · bound to `--red`, `--red-d` |
| `rgba(255,92,108) @0.16` | 3 | 1428, 1675, 3141 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.08` | 3 | 168, 3692, 3782 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `#12864F` | 2 | 2018, 2053 | `--volt` | REPLACE | green family -> --volt (ΔE 69.0) |
| `#5A6781` | 2 | 274, 4408 | `--muted` | REPLACE | declared as `--mut` (in `scorr_mobile.html`), which scorr_theme_r5.css already re-points to --muted |
| `#D9820E` | 2 | 4350, 4374 | `--amber` | REPLACE | orange family -> --amber (ΔE 13.4) |
| `#E2E7EE` | 2 | 1723, 2868 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 3.5) |
| `#E8ECF2` | 2 | 1735, 1739 | `--edge` | REPLACE | declared as `--line` (in `scorr_screeners.html`), which scorr_theme_r5.css already re-points to --edge |
| `#F6F8FB` | 2 | 1736, 1739 | `--field` | DECIDE | same value is declared under legacy names that map to different tokens (`--field` x3, `--panel-hi` x2) — one value doing two jobs |
| `#F87171` | 2 | 3166, 3213 | `--heat` | REPLACE | red family -> --heat (ΔE 17.9) |
| `rgba(0,0,0) @0.45` | 2 | 280, 1901 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(10,158,99) @0.14` | 2 | 2016, 2270 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(138,148,166) @0.15` | 2 | 4108, 4223 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.20` | 2 | 592, 649 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.09` | 2 | 170, 175 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.14` | 2 | 233, 234 | `--amber` | REPLACE | declared as `--amber` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --amber (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(245,185,74) @0.15` | 2 | 171, 2747 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.35` | 2 | 233, 234 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.08` | 2 | 3692, 3782 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(3,7,20) @0.50` | 2 | 16, 326 | `--field` | REPLACE | declared as `--shadow` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.50 — see DECIDE-0) · bound to `--shadow` |
| `rgba(47,212,139) @0.30` | 2 | 3850, 5543 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.35` | 2 | 232, 5284 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.12` | 2 | 478, 1235 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.14` | 2 | 101, 313 | `--pulse` | REPLACE | declared as `--blu` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --pulse (tint, alpha 0.14 — see DECIDE-0) |
| `#0891B2` | 1 | 4404 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 29.3 to aqua, 89.0 to pulse) |
| `#0A7D47` | 1 | 4876 | `--volt` | REPLACE | green family -> --volt (ΔE 70.8) |
| `#0A7D4F` | 1 | 1528 | `--volt` | REPLACE | green family -> --volt (ΔE 74.1) |
| `#0B6B7A` | 1 | 2328 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 44.6) |
| `#0B7A4B` | 1 | 3850 | `--volt` | REPLACE | green family -> --volt (ΔE 74.1) |
| `#0FA968` | 1 | 4401 | `--volt` | REPLACE | declared as `--grn` (in `pwa_endpoints.py`), which scorr_theme_r5.css already re-points to --volt |
| `#10B981` | 1 | 4900 | `--aqua` | DECIDE | teal — teal sits between --volt (green) and --aqua (cyan) |
| `#1E2A44` | 1 | 642 | `--edge` | REPLACE | declared as `--line` (in `scorr_digest_v3.html`), which scorr_theme_r5.css already re-points to --edge |
| `#22A565` | 1 | 4164 | `--volt` | REPLACE | green family -> --volt (ΔE 61.8) |
| `#2A78D6` | 1 | 4093 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 48.1) |
| `#3987E5` | 1 | 4093 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 50.4) |
| `#3B6D11` | 1 | 2328 | `--volt` | REPLACE | green family -> --volt (ΔE 60.4) |
| `#7C3AED` | 1 | 4406 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 14.2) |
| `#8A5B00` | 1 | 2328 | `--amber` | REPLACE | orange family -> --amber (ΔE 36.2) |
| `#94A3B8` | 1 | 240 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 5.0) |
| `#9B7CFF` | 1 | 322 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) |
| `#A32D2D` | 1 | 2328 | `--heat` | REPLACE | red family -> --heat (ΔE 30.8) |
| `#B3202F` | 1 | 3853 | `--heat` | REPLACE | red family -> --heat (ΔE 24.7) |
| `#B45309` | 1 | 706 | `--amber` | REPLACE | orange family -> --amber (ΔE 28.5) |
| `#B6E6CD` | 1 | 4876 | `--volt` | REPLACE | green family -> --volt (ΔE 71.1) |
| `#C0392B` | 1 | 4875 | `--heat` | REPLACE | red family -> --heat (ΔE 27.7) |
| `#D9F2F6` | 1 | 2328 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.3) |
| `#DDE3EA` | 1 | 2894 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 4.9) |
| `#E0F2F6` | 1 | 2467 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 5.2) |
| `#E5484D` | 1 | 4164 | `--heat` | REPLACE | red family -> --heat (ΔE 14.3) |
| `#E7F7EE` | 1 | 4876 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 10.5) |
| `#EAF3DE` | 1 | 2328 | `--chalk` | DECIDE | grey ramp value 16.0 ΔE from the nearest surface token — between steps |
| `#F2B8BF` | 1 | 4875 | `--heat` | REPLACE | red family -> --heat (ΔE 53.8) |
| `#FBEECB` | 1 | 2328 | `--amber` | REPLACE | orange family -> --amber (ΔE 54.8) |
| `#FCEBEB` | 1 | 2328 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 9.6) |
| `#FDE68A` | 1 | 308 | `--amber` | DECIDE | yellow — yellow sits between --amber and --volt |
| `#FDE8EA` | 1 | 4875 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 10.5) |
| `rgba(0,0,0) @0.14` | 1 | 2849 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.28` | 1 | 713 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.35` | 1 | 2898 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(0,0,0) @0.40` | 1 | 3221 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 12.8) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(10,158,99) @0.09` | 1 | 2016 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · translucent 0.09, so a tint of it (see DECIDE-0) |
| `rgba(10,158,99) @0.16` | 1 | 2016 | `--volt` | REPLACE | green family -> --volt (ΔE 65.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(120,130,150) @0.12` | 1 | 1977 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 8.5) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(138,148,166) @0.05` | 1 | 4084 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(138,148,166) @0.28` | 1 | 4085 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 4.1) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(138,148,173) @0.05` | 1 | 2083 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 1.4) · translucent 0.05, so a tint of it (see DECIDE-0) |
| `rgba(138,148,173) @0.10` | 1 | 2017 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 1.4) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(148,166,210) @0.12` | 1 | 607 | `--muted` | REPLACE | surface/text ramp family -> --muted (ΔE 11.9) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(15,169,104) @0.16` | 1 | 51 | `--volt` | REPLACE | green family -> --volt (ΔE 61.9) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(155,124,255) @0.12` | 1 | 479 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 21.6) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(20,35,70) @0.06` | 1 | 270 | `--field` | REPLACE | declared as `--shadow` (in `quant_basket.html`), which scorr_theme_r5.css already re-points to --field (tint, alpha 0.06 — see DECIDE-0) |
| `rgba(217,119,6) @0.12` | 1 | 706 | `--amber` | REPLACE | orange family -> --amber (ΔE 15.7) · translucent 0.12, so a tint of it (see DECIDE-0) |
| `rgba(217,119,6) @0.45` | 1 | 706 | `--amber` | REPLACE | orange family -> --amber (ΔE 15.7) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(221,58,74) @0.07` | 1 | 2017 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(221,58,74) @0.15` | 1 | 2017 | `--heat` | REPLACE | red family -> --heat (ΔE 13.1) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(226,55,68) @0.07` | 1 | 54 | `--heat` | REPLACE | red family -> --heat (ΔE 15.2) · translucent 0.07, so a tint of it (see DECIDE-0) |
| `rgba(226,55,68) @0.16` | 1 | 55 | `--heat` | REPLACE | red family -> --heat (ΔE 15.2) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(229,72,77) @0.08` | 1 | 4172 | `--heat` | REPLACE | red family -> --heat (ΔE 14.3) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.06` | 1 | 4171 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.13` | 1 | 573 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.45` | 1 | 2747 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.45, so a tint of it (see DECIDE-0) |
| `rgba(245,185,74) @0.50` | 1 | 5282 | `--amber` | REPLACE | orange family -> --amber (ΔE 18.4) · translucent 0.50, so a tint of it (see DECIDE-0) |
| `rgba(25,40,70) @0.14` | 1 | 27 | `--panel-hi` | REPLACE | surface/text ramp family -> --panel-hi (ΔE 3.5) · translucent 0.14, so a tint of it (see DECIDE-0) |
| `rgba(255,255,255) @0.40` | 1 | 556 | `--chalk` | REPLACE | surface/text ramp family -> --chalk (ΔE 7.7) · translucent 0.40, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.04` | 1 | 1190 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.06` | 1 | 1424 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.30` | 1 | 5543 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.32` | 1 | 3853 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.32, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.35` | 1 | 5283 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.35, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.40` | 1 | 2721 | `--heat` | REPLACE | declared as `--red` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --heat (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(34,165,101) @0.08` | 1 | 4170 | `--volt` | REPLACE | green family -> --volt (ΔE 61.8) · translucent 0.08, so a tint of it (see DECIDE-0) |
| `rgba(37,99,235) @0.03` | 1 | 438 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 22.7) · translucent 0.03, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.04` | 1 | 1190 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.04, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.06` | 1 | 1424 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.13` | 1 | 3851 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.13, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.16` | 1 | 1428 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.16, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.40` | 1 | 2720 | `--volt` | REPLACE | declared as `--grn` (in `scorr_check.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.40 — see DECIDE-0) |
| `rgba(55,211,232) @0.10` | 1 | 560 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(55,211,232) @0.14` | 1 | 316 | `--aqua` | DECIDE | cyan by hue, but reaches --pulse through the legacy `--cyan` map, which was written before --aqua existed (ΔE 7.0 to aqua, 105.1 to pulse) |
| `rgba(55,211,232) @0.30` | 1 | 560 | `--aqua` | REPLACE | cyan family -> --aqua (ΔE 7.0) · translucent 0.30, so a tint of it (see DECIDE-0) |
| `rgba(56,132,255) @0.10` | 1 | 3740 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 35.3) · translucent 0.10, so a tint of it (see DECIDE-0) |
| `rgba(6,10,22) @0.78` | 1 | 517 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.6) · translucent 0.78, so a tint of it (see DECIDE-0) |
| `rgba(6,10,22) @0.94` | 1 | 501 | `--field` | REPLACE | surface/text ramp family -> --field (ΔE 5.6) · translucent 0.94, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.06` | 1 | 566 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.06, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.15` | 1 | 2731 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.15, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.22` | 1 | 500 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.22, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.28` | 1 | 5543 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.28, so a tint of it (see DECIDE-0) |
| `rgba(77,124,254) @0.45` | 1 | 2731 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 28.0) · translucent 0.45, so a tint of it (see DECIDE-0) |

### `v8_ladder_v2.js`

5 colours, 5 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `rgba(148,166,210) @0.14` | 1 | 50 | `--edge` | REPLACE | declared as `--border` (in `fpc_v11.html`), which scorr_theme_r5.css already re-points to --edge (tint, alpha 0.14 — see DECIDE-0) |
| `rgba(255,92,108) @0.20` | 1 | 46 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.20, so a tint of it (see DECIDE-0) |
| `rgba(255,92,108) @0.34` | 1 | 48 | `--heat` | REPLACE | red family -> --heat (ΔE 7.0) · translucent 0.34, so a tint of it (see DECIDE-0) |
| `rgba(47,212,139) @0.12` | 1 | 49 | `--volt` | REPLACE | declared as `--grn-d` (in `scorr_result_corner.html`), which scorr_theme_r5.css already re-points to --volt (tint, alpha 0.12 — see DECIDE-0) |
| `rgba(47,212,139) @0.30` | 1 | 47 | `--volt` | REPLACE | green family -> --volt (ΔE 57.1) · translucent 0.30, so a tint of it (see DECIDE-0) |

### `worker/fyers_feed.py`

1 colours, 1 occurrences.

| colour | count | lines | maps to | verdict | note |
|---|---:|---|---|---|---|
| `#441166` | 1 | 66 | `--pulse` | REPLACE | blue/violet family -> --pulse (ΔE 52.0) |

## Reference universe — `design_refs/` and `previews/`

Listed, never rewritten. `design_refs/**` is the numbered design chain and `previews/**` are
the review screens; both are records of what was approved at a point in time, so a colour
sweep through them would destroy the thing they exist to be. Counts only.

| file | unique colours | occurrences |
|---|---:|---:|
| `design_refs/cockpit_v2_R1.html` | 24 | 28 |
| `design_refs/qb_six_baskets_R1.html` | 30 | 33 |
| `design_refs/scorr_batch2_R1.html` | 24 | 34 |
| `design_refs/scorr_batch4_R1.html` | 27 | 31 |
| `design_refs/scorr_batch5_R1.html` | 23 | 29 |
| `design_refs/scorr_cockpit_R1.html` | 19 | 21 |
| `design_refs/scorr_digest_v3_R2.html` | 44 | 49 |
| `design_refs/scorr_digest_v3_R3.html` | 44 | 48 |
| `design_refs/scorr_gvm_mobile_R1.html` | 20 | 50 |
| `design_refs/scorr_gvm_popout_R1.html` | 23 | 23 |
| `design_refs/scorr_home_R1.html` | 25 | 28 |
| `design_refs/scorr_mobile_R3.html` | 32 | 34 |
| `design_refs/scorr_mobile_R4.html` | 29 | 29 |
| `design_refs/scorr_mobile_R5.html` | 17 | 21 |
| `design_refs/scorr_result_corner_R1.html` | 27 | 29 |
| `design_refs/scorr_v15_R1.html` | 17 | 18 |
| `design_refs/scorr_v15_R2.html` | 62 | 78 |
| `design_refs/scorr_v8_R1.html` | 30 | 34 |
| `design_refs/scorr_v8_R2.html` | 36 | 40 |
| `design_refs/scorr_v8_v10_R1.html` | 26 | 26 |
| `design_refs/scorr_v8_v10_R2.html` | 35 | 48 |
| `previews/check.html` | 21 | 27 |
| `previews/digest.html` | 16 | 21 |
| `previews/fpc.html` | 18 | 23 |
| `previews/gvm.html` | 18 | 24 |
| `previews/holdings.html` | 16 | 21 |
| `previews/home.html` | 21 | 25 |
| `previews/home_v2.html` | 12 | 15 |
| `previews/intel.html` | 25 | 31 |
| `previews/login.html` | 19 | 24 |
| `previews/models.html` | 20 | 28 |
| `previews/models_tools.html` | 28 | 152 |
| `previews/qb.html` | 18 | 23 |
| `previews/results.html` | 19 | 26 |
| `previews/scanners.html` | 18 | 23 |
| `previews/sector.html` | 15 | 20 |
| `previews/trade_card.html` | 18 | 24 |
| `previews/v8.html` | 25 | 34 |
| `previews/v8_lower_redesign.html` | 15 | 16 |
| `previews/v8_positions.html` | 25 | 30 |
| `previews/v8_surfaces.html` | 40 | 172 |

## Method, and what this scan does not catch

Scanned: every git-tracked `.html .css .js .py` file, line by line, for hex literals (3/4/6/8
digit), `rgb()`/`rgba()`, and `var(--*)` names. Python and JS files are included because a large
part of this app's CSS is embedded in them — `pwa_endpoints.py` alone carries 131 colours, more
than any HTML file except the V8 dashboard.

Deliberate limits, stated so nobody reads this as exhaustive:

- A bare 3- or 4-digit **all-numeric** hex is only counted on a line that is visibly styling
  something. Without that guard `cc#1072` and `(#838)` read as colours, and this repo is full of
  card references. The cost is that a `#123` with no styling word on its line is missed.
- Named CSS colours (`white`, `tomato`) and `hsl()` are not counted by the scanner. That was
  measured, not assumed: a grep of the same 242 files for `hsl(`, for `color|background|border|
  fill|stroke: <name>`, and for the SVG attribute form `fill="white"` returns **zero** hits in
  all three. The app is entirely hex and rgb(), so nothing is missing on this account.
- Modern space-separated `rgb(0 0 0 / 50%)` syntax is not matched; the app uses the comma form.
- Colours arriving from data at runtime (a score band picking a hex in JS) are counted where the
  literal appears, which is the right place to change them.

No file other than this one is touched by cc#1072.

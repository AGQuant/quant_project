# cc#1985 — GVM segment moves: the 10 ruled, applied (8 written, 2 held at the gate)

Source: cc#1980 (`reports/CC1980_segment_sanity.md`, sha ffd0cbe). Founder ruling 10-Sep 22:30 —
"Yes" to the 10 proposed moves, "460 also scan". Closes cc#1146.

Run: 11-Sep-2026, 07:45–08:15 IST (server clock, `Scorr:server_now` — not the container's).

---

## 1 — WHICH TABLE AND COLUMN CARRY THE SEGMENT (card item 1, "establish it from code")

**`input_raw.gvm_segment` is the source of truth. `gvm_scores.segment` is a derived copy and is
replaced on every run — writing it directly would be silently undone by tonight's nightly.**

Traced in code, not assumed:

| Step | Where | What happens |
|---|---|---|
| read | `gvm_nightly.py:646-648` | `SELECT nse_code, company_name, gvm_segment, fy27_growth FROM input_raw` |
| normalise | `gvm_nightly.py:667` | blank / `nan` → `"Unknown"` |
| peer sets | `gvm_nightly.py:_peer_averages` | `df.groupby("gvm_segment")` → the per-parameter median every relative score is measured against |
| persist | `gvm_nightly.py:927` | `INSERT INTO gvm_scores (..., segment, ...)` — the table is **DELETE + INSERT** each run ("latest_table": "gvm_scores (replaced)") |
| fan-out | `gvm_nightly.py:794-802`, `909`, `937` | `gvm_cache`, `peer_averages`, `gvm_history`, `sector_ratings` all derive from the same value |

So one write to `input_raw.gvm_segment` + one recompute moves the company everywhere at once.
`screener_raw` was **not** touched: it carries Screener's own independent `industry_group` /
`Industry` taxonomy, which is the cross-check, not the thing being edited.

---

## 2 — THE GATE: WHAT A 2-MEMBER SEGMENT DOES TO PEER SCORING

**The gate fired. HEG and GRAPHITE were NOT written. Everything else in the 10 was.**

The card said: *"If item 2 shows n=2 breaks peer scoring, STOP before writing the HEG/GRAPHITE
move."* It does break it. Not by crashing, and not by falling back — there is **no minimum-member
guard anywhere in the code**. It breaks by quietly ceasing to measure anything.

### What the code does

`_peer_averages` takes `vals.median()` per segment with no floor on `len(grp)`. At n=2 the median
is just the midpoint of the two members, so **each company is 50% of its own benchmark**.
`param_score` is then half absolute (`score_absolute`, no peers) and half relative
(`score_relative`, all peers), so the relative half of every G parameter, and the segment-PE half
of V, run off that midpoint.

### What that forces, arithmetically

With two positive values `a > b`, the higher member's ratio to the midpoint is strictly between
100 and 200 and the lower member's strictly between 0 and 100. So on **every** relative
parameter the pair can only land on `(10.0, 2.5)` or `(7.5, 5.0)` — both of which sum to 12.5.
The pair's average relative score is therefore **pinned at 6.25 whatever the two businesses are
worth**. A world-class pair and a failing pair both average 6.25.

### Measured, with the real `gvm_engine` functions

Peer medians taken from Postgres (`PERCENTILE_CONT(0.5)`, the same median pandas computes).
Only the peer set changes between scenarios; every company value is identical.

| | same relative score for BOTH members |
|---|---|
| n=25, today's `Electronics - Consumer & Smart` | **12 of 15** parameters |
| n=2, the proposed `Graphite Electrodes` | **0 of 15** parameters |
| n=18, the fallback `Pumps, Refractories & Industrial` | **9 of 15** parameters |

At n=2 the two can never agree on anything. The relative half stops asking "is this a good
business" and asks only "which of these two is bigger on this line".

Pair sums at n=2: **12.5 on 14 of the 15 parameters**, 15.0 on the one where both values are
negative (`profit_growth_3y`, the sign-flip branch of `score_relative`).

### The scores that would result

M is segment-independent (it comes from `momentum_scores`, not `_stock_dict`), so it is held.

| Symbol | Scenario | G | V | GVM |
|---|---|---|---|---|
| HEG | A — n=25, today | **5.71** | **7.50** | **5.44** |
| | B — n=2, new `Graphite Electrodes` | 6.52 | 6.88 | 5.51 |
| | C — n=18, `Pumps, Refractories & Industrial` | 6.07 | 8.12 | 5.77 |
| GRAPHITE | A — n=25, today | 5.62 | **6.88** | 7.40 |
| | B — n=2, new `Graphite Electrodes` | 6.25 | 5.62 | 7.19 |
| | C — n=18, `Pumps, Refractories & Industrial` | 5.80 | 6.25 | 7.25 |

**Simulation validity, stated honestly.** Scenario A is the simulator reproducing what the live
table already holds. HEG comes out **exact** on both G (5.71) and V (7.50); GRAPHITE is **exact on
V (6.88) and 0.09 low on G (5.62 vs the live 5.71)**. The likely cause is `qoq_sales_growth` /
`qoq_profit_growth`, which `gvm_nightly` recomputes from the quarterly sales/profit columns rather
than using `screener_raw`'s passthrough, and which this simulation feeds from the passthrough. It
is not hidden: the same inputs are used in all three scenarios, so the **deltas** between them are
unaffected, and HEG — the exactly-reproduced one — shows the same pattern.

### The fallback, with its evidence

`Pumps, Refractories & Industrial` (16 members today, 18 with the pair) is not a compromise —
it is arguably the better answer on business grounds. Screener's own `Industry` tag for both HEG
and GRAPHITE is **"Electrodes & Refractories"**, and four of that segment's current members are
refractories makers: **RHIM** (RHI Magnesita), **VESUVIUS**, **IFGLEXPOR**, **ORIENTCER**. Full
membership: AMBER, BLUESTARCO, ELGIEQUIP, GKENERGY, IFGLEXPOR, INGERRAND, KIRLOSBROS, KIRLPNU,
KSB, MONOLITH, ORIENTCER, RHIM, ROTO, SHAKTIPUMP, VESUVIUS, VOLTAS.

**Held for a ruling. Not written.** The founder's "Yes" was to a proposal whose own report said
the new segment would be "thin, but correct"; the measurement above says thin is not correct here,
so the instruction and the evidence disagree and that is a stop, not a judgement call to make
alone.

---

## 3 — THE 8 MOVES, WRITTEN AND RECOMPUTED

`gvm_recompute(refresh_momentum=False)` — momentum deliberately held so the ONLY variable is the
segment. 1,793 scored, 0 errors, 128 segments, `score_date` 2026-09-11.

| Symbol | Company | From | To | GVM before | after | Δ | Verdict |
|---|---|---|---|---|---|---|---|
| ARTEMISMED | Artemis Medicare Services | Diagnostics & Healthcare Services | Hospitals - Mid & Small | 7.90 | 8.26 | **+0.36** | Good → **Excellent** |
| KALPATARU | Kalpataru Ltd | Engineering - EPC Mid | Realty - Mid | 4.45 | 4.18 | −0.27 | Weak → Weak |
| RATEGAIN | Rategain Travel Technologies | Hotels - Mid | IT - Small | 8.06 | 7.88 | −0.18 | Excellent → **Good** |
| SOUTHWEST | South West Pinnacle Exploration | Mining | Business Services | 7.44 | 7.60 | +0.16 | Good → Good |
| GNFC | Gujarat Narmada Valley Fert. & Chem. | Agro Chemicals - Small | Commodity & Chlor-Alkali Chemicals | 8.40 | 8.28 | −0.12 | Excellent → Excellent |
| ARKADE | Arkade Developers | Engineering - Industrial Mfg A | Realty - Small | 5.79 | 5.76 | −0.03 | Weak → Weak |
| REDINGTON | Redington | IT - Mid | Diversified Trading | 8.07 | 8.04 | −0.03 | Excellent → Excellent |
| SALASAR | Salasar Techno Engineering | Steel Tubes & Wires | Capital Goods - Industrial Small | 3.67 | 3.65 | −0.02 | Weak → Weak |
| HEG | HEG Ltd | — HELD AT THE GATE — | | 5.44 | 5.44 | 0.00 | unchanged |
| GRAPHITE | Graphite India | — HELD AT THE GATE — | | 7.43 | 7.43 | 0.00 | unchanged |

**No score moved by more than 1.0 point**, so the card's "name it" threshold was not reached.
Two verdict labels did change and are named above.

The KALPATARU / KPIL confusion cc#1980 warned about is **not present in the data**: `KALPATARU` is
"Kalpataru Ltd" (the realty developer, the one moved) and `KPIL` is "Kalpataru Projects
International Ltd" (the EPC contractor, in `Power Services & Trading`, untouched). Two clean rows.

### Blast radius — a clean control

Across all 1,793 companies, comparing 10-Sep to 11-Sep:

- **190 companies changed** — and **every one of them is inside the 16 segments the 8 names left
  or joined. Zero changed outside.** That is the proof that nothing else moved: `screener_raw` was
  stable and momentum was genuinely held, so all 190 changes are attributable to these 8 moves.
- **0 companies moved more than 1.0 point.** Largest absolute change in the entire universe: 0.36.
- **HEG and GRAPHITE came back 0.00 on G, V and GVM** — the held pair doubling as a control.
- **17 verdict labels flipped** (list in the room log). This is the founder-visible effect.

### A fragility this exposed (not caused by this card)

Eight of the 17 verdict flips came from a GVM change of **0.06 or less**, three from **0.03**.
`_label_gvm` cuts hard at 6.0 / 7.0 / 8.0, so a company sitting on a boundary changes its public
label on a rounding-level move. MASTEK went 6.00 → 5.97 and its label went Average → Weak.
Reported here, not fixed — the scoring rules are `do_not_touch` on this card.

---

## 4 — SEGMENTS WITH FEWER THAN 5 MEMBERS AFTER THE MOVES (card item 6)

No segment lost members to these moves (every source segment held 12+ before), and no segment was
created or deleted — 128 before, 128 after.

| Segment | Members | Median GVM | Symbols |
|---|---|---|---|
| Plastics and Packaging | 3 | 5.85 | HITECHCORP, KANPRPLA, KNACK |
| Textiles Smallcap | 4 | 6.44 | LAKSHMIMIL, NAHARINDUS, ORBTEXP, PASUPTAC |

Two more sit at exactly 5, worth seeing next to them given section 2: **City Gas Distribution**
(AEGISLOG, GUJENERGY, IGL, MGL, PETRONET) and **Shipping & Maritime** (ABSMARINE, JITFINFRA, KMEW,
SCI, SEAMECLTD). The n=2 arithmetic above weakens as n grows but does not vanish at 3 or 4 — a
3-member segment still lets one company be a third of its own benchmark.

---

## 5 — THE 460 REVIEW (card items 4 and 5)

In progress, in batches of 50, with a running tally in the room log. Output goes to
`reports/CC1985_segment_review_460.md` as a **list for the founder** — per the card's gate, none
of it is applied. Only the 10 were pre-approved, and of those only 8 were written.

---

## Notes on the recompute

`gvm_recompute` exposes no `target_date`, so it stamped `date.today()` = **2026-09-11**, one day
ahead of the last nightly (10-Sep). Tonight's `bg_gvm` at 01:30 IST stamps 2026-09-11 as well and
will overwrite this snapshot with one carrying fresh momentum, so the early stamp is transient and
self-correcting. It is recorded here rather than left for someone to find.

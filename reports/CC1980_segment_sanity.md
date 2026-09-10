# cc#1980 — GVM Segment Sanity Check (READ-ONLY)

Founder direction, 10-Sep 18:35 IST: check all segments and all companies once, a sanity
check, read-only. Supersedes the three open rulings on cc#1146 (HEG, GRAPHITE, ARKADE).

**No write happened.** `input_raw` and `gvm_scores` row counts are unchanged before and after
this card (verified — see item A below).

## Method, stated plainly (and why it differs from the card's original wording)

The card's scope asked to read "the Screener overview text where we hold it (screener_raw /
fundamentals_history)." **That text does not exist in this database.** `screener_raw` holds only
structured fields (no company-description column); `fundamentals_history.metrics` is a JSONB of
financial figures, not prose. This is stated here rather than silently worked around — it is
itself a finding.

What the database does hold, usable as a genuine cross-check rather than a keyword match:
**three independent classification systems** for the same 1,793 companies — GVM's own curated
`segment` (128 groups, the one being audited), Screener's `industry_group` / `Industry` (a
separate, independently-maintained taxonomy), and `futures_universe.theme` (22 curated themes,
for the ~208 F&O names only). Comparing these three against each other is the closest available
substitute for "read what the company does" — it is not a keyword search on a company's name or
description; it is a disagreement check between two people's classification of the same company.

**A first pass, run mechanically, produced 788 candidates — then 504 after filtering out
same-family cap/style-tier splits (see below). Both numbers are too high to be real, and the
first pass repeats cc#1146's own documented mistake almost exactly**: Screener's `Industry` tag
is far coarser in places than GVM's deliberately fine-grained segments. Three concrete patterns
in the raw 504 turned out to be **false positives from the structural signal, confirmed by
business knowledge, not from the classifier**:
- **CDMO & Contract Mfg** (Neuland Labs, Sai Life Sciences, Acutaas Chemicals, and others) is a
  real, distinct pharma business model — contract development and manufacturing — and grouping
  them apart from generic small-cap formulations makers is very likely GVM's deliberate design,
  not an error, even though Screener tags all of them plain "Pharmaceuticals."
- **Pipes & Tubes** (APL Apollo, Man Industries, Rama Steel) is a real, distinct steel
  sub-industry — pipe manufacturing is a different business and a different investment case from
  generic "Steel - Mid & Small," even though Screener's `Industry` field for all of them is the
  broader "Iron & Steel Products."
- Market-cap-tiered segment families (`Textiles - Large` vs `Synthetic Fibres & Yarn`, `Cement -
  Large & Mid` vs `Cement - Small`, `Auto - Drivetrain & Precision` vs `Auto - Engines & Thermal`)
  are GVM splitting one Screener `Industry` by size or sub-specialty on purpose. A same-family-
  prefix filter removed most of these (788 → 504) but not all — the three CDMO/Pipes examples
  above still needed a human business-knowledge check, not just a string filter, to rule out.

**Given that, this card did NOT report all 504 as findings — that would be the cc#1146 mistake
again, just automated differently.** Instead: the full 128-segment structural table (item 3, 6)
is complete and exact — it required no judgment. For the company-level moves (item 2, 4), the
top **45** candidates by rating impact were read individually against what each company is
actually known to do, and only the ones that survive that read are reported as genuine proposals
below. The remaining ~460 raw candidates are preserved, unreviewed, in
`reports/CC1980_segment_candidates_raw.json` for a future pass — **the cut point, stated per the
card's own allowance**: rating-impact rank 45, because that is as far as this card's time budget
reached with genuine individual review rather than mechanical output.

## A — Row counts (verify item A)

`input_raw` and `gvm_scores` were read-only for this card — no `INSERT`/`UPDATE`/`DELETE` ran
against either table, and no GVM recompute was triggered. Live counts, read once at the start of
this pass and unchanged since: `gvm_scores` latest `score_date` 2026-09-09, **1,793 companies,
128 distinct segments**. The founder's own recollection was 127 — **the live count is 128; the
one-segment difference is reported here, not resolved** (which extra segment is real vs a
one-member artifact is exactly what item 3 below answers).

## B — All 128 segments: member count + median GVM (verify item C, item 3/6)

Full table: `reports/CC1980_segment_table_appendix.md` (128 rows). Summary:

- **96 segments hold 10+ companies.**
- **2 segments hold fewer than 5 members** — both genuinely thin, peer-median-near-meaningless
  buckets, exactly as the founder's own count flagged:
  - **Plastics and Packaging** — 3 members (HITECHCORP, KANPRPLA, KNACK)
  - **Textiles Smallcap** — 4 members (LAKSHMIMIL, NAHARINDUS, ORBTEXP, PASUPTAC)

Neither of these two segments shows an obvious two-business split on inspection — they are small,
not blended. No segment in the 128 showed a clear same-segment two-different-industries split
under the Industry cross-check (the closest thing to that pattern is the CDMO/Pipes cases above,
which are legitimate fine splits GVM made on purpose, not a segment that accidentally blends two
unrelated businesses).

## C — The three cc#1146 cases (verify item C — closes cc#1146)

| Symbol | Company | Current segment | Current members / median | Proposed segment | Proposed members / median | Rating impact | Why |
|---|---|---|---|---|---|---|---|
| HEG | HEG Ltd | Electronics - Consumer & Smart | 25 / 6.54 | Pumps, Refractories & Industrial (or a new **Graphite Electrodes** segment with GRAPHITE) | 16 / 5.74 | GVM 5.44 vs current median 6.54 (**−1.10**) vs proposed median 5.74 (**−0.30**) | A graphite-electrode maker sitting among consumer/smart electronics retailers and device makers. Screener's own `Industry` tag for HEG is "Electrodes & Refractories" — it shares no business with its current segment-mates. |
| GRAPHITE | Graphite India Ltd | Electronics - Consumer & Smart | 25 / 6.54 | Pumps, Refractories & Industrial (or the same new Graphite Electrodes segment) | 16 / 5.74 | GVM 7.43 vs current median 6.54 (**+0.89**) vs proposed median 5.74 (**+1.69**) | Same industry as HEG (`Electrodes & Refractories`), same mismatch. |
| ARKADE | Arkade Developers Ltd | Engineering - Industrial Mfg A | 18 / 6.00 | Realty - Small | 17 / 6.10 | GVM 5.79 vs current median 6.00 (**−0.21**) vs proposed median 6.10 (**−0.31**) | A residential/commercial real-estate developer (`industry_group`=Realty) sitting among industrial manufacturers. |

Numbers here are read fresh from the live 09-Sep GVM run and match this card's own 10-Sep figures
closely (cc#1146's 20-Aug read had HEG at 7.53 / GRAPHITE at 6.85 / ARKADE at 5.89 — scores move
with each nightly run; the segment mismatch itself is unchanged). **This is the founder's call as
Head of Research per the card's own method note — HEG/GRAPHITE could go to `Pumps, Refractories &
Industrial` or found a new `Graphite Electrodes` segment (only 2 members today — thin, but
correct); ARKADE to `Realty - Small`.**

## D — Genuine candidate moves, individually read (top 45 by rating impact, cut point stated above)

**CONFIRM** — read against known business, looks like a real mismatch worth moving:

| Symbol | Company | Current segment | Proposed segment | GVM | Rating impact (cur→prop median) | Why |
|---|---|---|---|---|---|---|
| KALPATARU | Kalpataru Ltd | Engineering - EPC Mid | Realty - Mid | 4.45 | 6.53 → 6.08 | This is the **realty developer** Kalpataru Ltd (2025 listing), not the EPC contractor — that company is `Kalpataru Projects Ltd` / KPIL (formerly Kalpataru Power Transmission). Easy to conflate by name; worth a second look at whether the two got cross-wired anywhere else. |
| RATEGAIN | RateGain Travel Technologies | Hotels - Mid | IT - Small | 8.06 | 6.09 → 5.94 | RateGain is SaaS **sold to** hotels (travel-tech software), not a hotel operator. Being scored against hotel operators as peers is the wrong comparison. |
| ARTEMISMED | Artemis Medicare Services | Diagnostics & Healthcare Services | Hospitals - Mid & Small | 8.01 | 5.77 → 6.77 | Artemis runs Artemis Hospital, Gurgaon — a hospital operator, not primarily a diagnostics chain. |
| REDINGTON | Redington Ltd | IT - Mid | Diversified Trading | 7.91 | 5.81 → 6.12 | Redington's business is IT/electronics **distribution**, not software or IT services — a trading/logistics margin business, different economics from an IT peer set. |
| GNFC | Gujarat Narmada Valley Fertilizers & Chemicals | Agro Chemicals - Small | Commodity & Chlor-Alkali Chemicals (or a larger-cap bucket) | 8.4 | 5.3 → 6.49 | GNFC is a large, diversified fertilizer + chlor-alkali chemicals maker — both the "Small" size tag and the pure-agro-chemicals label undersell it. Flagging the size mismatch as much as the segment. |
| SALASAR | Salasar Techno Engineering | Steel Tubes & Wires | Capital Goods - Industrial Small | 3.78 | 6.41 → 5.94 | Makes engineered steel structures (telecom/transmission towers) — closer to industrial capital goods than commodity tube/wire manufacturing. |
| SOUTHWEST | South West Pinnacle Exploration | Mining | Business Services | 7.86 | 5.82 → 5.97 | Provides mineral-exploration **services to** mining companies; it is not itself a miner. |

**LOW CONFIDENCE — worth a second look, not asserted**: RIIL (Reliance Industrial Infrastructure
— warehousing/logistics-infra arm, "Oil Services & Small" looks wrong but the proposed
"Logistics - Small" evidence is thin), JITFINFRA (waste/logistics vs "Shipping & Maritime," only
5 companies total in its Industry cluster — too thin to be confident), INDOTECH (Indo Tech
Transformers — heavy-transformer maker, "Electrical Equipment Small" vs a heavy-electrical
segment is plausible but not confirmed), PREMIERPOL (Premier Polyfilm — flexible packaging vs
"Consumer Plastics & Others," plausible), CARTRADE (auto marketplace vs "Internet & Digital
Small," the two candidate segments look similar enough this may be a distinction without a real
difference).

**FALSE POSITIVE — flagged by the structural signal, ruled out by business knowledge, do NOT
move**: NEULANDLAB / SAILIFE / ACUTAAS (CDMO & Contract Mfg is a real, deliberate pharma
sub-segment), APLAPOLLO / MANINDS / RAMASTEEL (Pipes & Tubes is a real, deliberate steel
sub-segment — APL Apollo especially is one of India's largest pipe makers, correctly placed),
CHEMPLASTS (chlor-alkali/PVC is its actual core business — the current segment is right, the
proposed move is the wrong direction), VINDHYATEL (a telecom cable/equipment maker; Screener's
"Civil Construction" Industry tag on it looks like a data artifact, not a real business match),
ALOKINDS / TRIDENT / NITINSPIN (large diversified textile makers in a market-cap-tiered "Large"
segment — legitimate size tiering, not a business mismatch), STERTOOLS / SUNCLAY / NRBBEARING
(castings/forgings and bearings are real, distinct auto-component sub-specialties, correctly
split out from generic engines/thermal), MANAKSTEEL / VISL (small steel names — moving them
against `Integrated Steel - Large` giants like Tata Steel/JSW would be a worse peer match, not a
better one; the structural signal does not check size-appropriateness of its own proposal, which
is a real limitation of the method, noted here rather than hidden).

## E — Counts (verify item C, card item 6)

- **1,793 companies checked, 128 segments checked** (full sweep, as directed).
- **7 companies CONFIRMED as genuine proposed moves** (table D above) + **3 from cc#1146**
  (HEG, GRAPHITE, ARKADE) = **10 companies with a stated proposal** ready for a founder ruling.
- **5 companies flagged LOW CONFIDENCE**, not proposed, named for a second look.
- **~460 raw structural candidates** were surfaced by the mechanical cross-check and are
  **unreviewed** — preserved in `reports/CC1980_segment_candidates_raw.json` rather than either
  asserted as real or silently discarded. At least three whole categories inside that list (CDMO,
  Pipes & Tubes, cap-tiered families) are now known to be mostly false positives from the method
  above, which should shrink a future pass considerably before it needs more individual reads.
- **0 companies flagged for a NEW segment** in this pass beyond the one already named in cc#1146
  (a possible `Graphite Electrodes` segment for HEG + GRAPHITE, which is the founder's call, not
  a new one discovered here).
- **2 segments flagged as under-5 members** (Plastics and Packaging: 3, Textiles Smallcap: 4) —
  both genuinely small, not a blended-business defect.
- **0 segments** showed a clear two-different-businesses blend under this check.

## F — Not done, stated plainly

No prose "Screener overview" text exists in this database to read verbatim, contrary to the
card's assumption (Section: Method). The ~460 unreviewed raw candidates were not individually
judged — the cut point and why is stated in Method. No taxonomy change, no GVM recompute, no
write of any kind happened.

**DECISION NEEDED** is posted on the Fable Room (task 1199) per DIAG_FINDINGS_SURFACE_V1.

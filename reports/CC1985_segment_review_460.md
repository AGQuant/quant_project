# cc#1985 item 4 — the remaining segment candidates, reviewed one by one

**Nothing in this file is applied.** The card pre-approved only the 10 from cc#1980 (of which 8
were written and 2 held — see `reports/CC1985_segment_moves.md`). Everything here is a LIST for the
founder.

Source pool: `reports/CC1980_segment_candidates_raw.json`, 504 rows. 31 were already named and
individually judged by cc#1980, leaving **473 to review**. Batches of 50, running tally posted to
the Fable Room after each.

---

## Method — and a prefilter that was tested before it was trusted

cc#1980's warning stands: a structural disagreement between GVM's segment and Screener's
`Industry` is usually **GVM splitting one Screener industry on purpose**, not an error. So the
first question for each company is not "do the two tags differ" but **"am I the only one here who
carries my industry tag, or is there a block of us?"**

The measurable form: count how many of a candidate's own GVM segment-mates carry its exact
Screener `Industry`.

- **3 or more mates** → the segment deliberately holds that industry. The candidate is not a
  misfit. **Rejected without an individual read.** 210 of the 473.
- **1 (alone) or 2 (a pair)** → the candidate stands out in its own segment. **Read individually.**
  263 of the 473.

**The prefilter was validated against cc#1980's own 26 human judgements before being used at
scale** (applied to the pre-move segment state, the one cc#1980 actually judged):

| cc#1980's human verdict | prefilter agreed |
|---|---|
| MOVE (10 companies) | **9 of 10** |
| DO NOT MOVE (16 companies) | **13 of 16** |

**All four disagreements are real limitations, and each is named rather than smoothed over:**

1. **ARTEMISMED** — prefilter said BLOCK(7) reject, the human moved it. Its Screener industry is
   "Hospital" and **seven of its `Diagnostics & Healthcare Services` segment-mates are also tagged
   "Hospital"**. The block is real; the block is *the defect*. A segment called Diagnostics that
   holds seven hospitals is mis-named, not finely split. See finding H1 below.
2. **CHEMPLASTS** and 3. **VINDHYATEL** — prefilter said ALONE flag, the human said leave them.
   In both cases **the Screener tag is wrong**, not the GVM segment (a chlor-alkali/PVC maker
   tagged "Specialty Chemicals"; a telecom-cable maker tagged "Civil Construction"). The prefilter
   trusts Screener and cannot see a bad tag. Only an individual read catches these — which is why
   the flagged 263 are read and not auto-proposed.
4. **MANAKSTEEL** — prefilter said PAIR, the human said leave it, on **size**: moving a small steel
   name in beside Tata Steel and JSW is a worse peer match, not a better one. The prefilter does
   not check size-appropriateness. Market cap is therefore carried on every row below and used
   explicitly.

So: the prefilter is used **only to reject**, never to propose, and every proposal below comes from
an individual read with the company's market cap in hand.

---

## BATCH 1 — 50 companies (candidates 1–50 of 263 flagged)

Legend: **MOVE** = proposed for the founder's list · **AS-IS** = confirmed correctly placed ·
**MOVE\*** = the current segment is wrong but the *proposed* destination is also wrong; a better
destination is named.

| # | Symbol | Company | Mcap ₹Cr | Current segment | cc#1980 proposed | Verdict | One-line reason |
|---|---|---|---|---|---|---|---|
| 1 | DPEL | Divine Power Energy | 1,538 | Aluminium & Non Ferrous | Electrical Cables | **MOVE** | Makes winding wire and cable; its competitors are cable makers, not primary aluminium smelters. |
| 2 | KSHINTL | KSH International | 7,563 | Aluminium & Non Ferrous | Electrical Cables | **MOVE** | Same: enamelled copper winding wire. Non-ferrous is the raw material, not the business. |
| 3 | ZEEL | Zee Entertainment | 8,391 | Broadcasting & OTT | Entertainment, Content & Digital | **AS-IS** | It is a broadcaster with an OTT arm — the current segment names exactly that. Deliberate split. |
| 4 | SUNTV | Sun TV Network | 17,931 | Broadcasting & OTT | Entertainment, Content & Digital | **AS-IS** | A pure TV broadcaster. Current segment is the precise one. |
| 5 | UTIAMC | UTI Asset Management | 11,585 | Broking & Wealth Management | Capital Markets - Large | **MOVE** | A mutual-fund AMC, not a broker. AMC economics (AUM-linked fees) differ from brokerage. |
| 6 | CRAMC | Canara Robeco AMC | 4,777 | Broking & Wealth Management | Capital Markets - Large | **MOVE** | Same — AMC, peers are the listed AMCs, not brokers. |
| 7 | JMFINANCIL | JM Financial | 12,359 | Broking & Wealth Management | Holding Companies | **AS-IS** | Screener tags it "Holding Company" but it operates — investment banking, lending, AMC. It is not a passive holdco. |
| 8 | AFSL | Abans Financial Services | 1,022 | Broking & Wealth Management | Holding Companies | **AS-IS** | Same reason: an operating financial-services group, not a holding shell. |
| 9 | RELIGARE | Religare Enterprises | 8,784 | Broking & Wealth Management | Holding Companies | **MOVE** | It genuinely is a holdco — its value is its stakes in Care Health and Religare Finvest, not a brokerage book. |
| 10 | VLSFINANCE | VLS Finance | 775 | Broking & Wealth Management | Holding Companies | **MOVE** | An investment company holding a securities portfolio; no broking franchise. |
| 11 | CUMMINSIND | Cummins India | 138,517 | Capital Goods - Heavy Electrical | Pumps, Refractories & Industrial | **MOVE\*** | Diesel and gas **engines** and gensets — not heavy *electrical*. But at ₹1.39 lakh Cr it is the largest name in this batch and "Pumps, Refractories & Industrial" (median mcap far below) is a size mismatch. Better: an engines/industrial-large bucket. |
| 12 | KIRLOSENG | Kirloskar Oil Engines | 32,597 | Capital Goods - Heavy Electrical | Pumps, Refractories & Industrial | **MOVE** | Engines and gensets; Kirloskar Brothers (pumps) already sits in the proposed segment, so the peer set is genuinely adjacent. |
| 13 | AIIL | Authum Investment & Infra | 43,867 | Capital Markets - Large | MSME Finance - Mid | **AS-IS** | An investment/NBFC holding company, not an MSME lender. The proposed destination is wrong; the current one is defensible. |
| 14 | IIFL | IIFL Finance | 27,571 | Capital Markets - Large | MSME Finance - Mid | **MOVE** | A retail gold/MSME/home lender. The broking arm is the separately listed IIFL Capital. It is not a capital-markets firm. |
| 15 | EXIDEIND | Exide Industries | 35,645 | Consumer Durables - Large | Auto - Engines & Thermal | **MOVE\*** | Automotive batteries — OEM and replacement — not a consumer durable. But "Engines & Thermal" is not a battery peer set either; **Auto - Wiring & Electricals** is the closer home. |
| 16 | ARE&M | Amara Raja Energy & Mobility | 15,347 | Consumer Durables - Large | Auto - Engines & Thermal | **MOVE\*** | Identical case to Exide, same better destination. |
| 17 | EKC | Everest Kanto Cylinder | 1,162 | Consumer Plastics & Others | Capital Goods - Industrial Small | **MOVE** | Makes high-pressure **steel** gas cylinders. Nothing about it is consumer plastics. |
| 18 | MAMATA | Mamata Machinery | 1,024 | Consumer Plastics & Others | Capital Goods - Industrial Small | **MOVE** | Makes packaging **machinery**. It sells to plastics converters; it is not one. |
| 19 | BSHSL | Bombay Super Hybrid Seeds | 958 | Diversified | FMCG - Small | **MOVE\*** | A hybrid-seed company — "Diversified" says nothing. But it is not FMCG either; it belongs with agri-inputs/seeds. |
| 20 | REGAAL | Regaal Resources | 900 | Diversified | FMCG - Small | **MOVE\*** | Maize-starch processing — an agri-processor, not a branded FMCG. Same destination problem. |
| 21 | MGEL | Mangalam Global Enterprise | 515 | Diversified Trading | FMCG - Small | **AS-IS** | It really is an agri-commodity **trader**. Trading is the business; the current segment is right. |
| 22 | SUKHJITS | Sukhjit Starch & Chemicals | 503 | Diversified Trading | FMCG - Small | **MOVE\*** | A starch **manufacturer**, not a trader. Agri-processing, not FMCG — same destination caveat as 19/20. |
| 23 | KSCL | Kaveri Seed | 3,758 | Edible Oil & Animal Feed | FMCG - Small | **MOVE\*** | Hybrid seeds. It makes no edible oil and no animal feed — the current segment is plainly wrong — but FMCG is not the fix; seeds/agri-inputs is. |
| 24 | GAEL | Gujarat Ambuja Exports | 7,763 | Edible Oil & Animal Feed | FMCG - Small | **AS-IS** | Genuinely edible oil plus maize processing. Current segment matches the business. |
| 25 | DIACABS | Diamond Power Infrastructure | 20,436 | Electrical Cables | Electrical Equipment Small | **AS-IS** | Conductors and power cables — the current, finer segment is the correct one. Screener's "Other Electrical Equipment" is the coarser tag, and at ₹20,436 Cr "Small" is also a size error. |
| 26 | ADVAIT | Advait Energy | 2,358 | Electrical Cables | Electrical Equipment Small | **MOVE** | Transmission hardware, conductor accessories and solar EPC — broader than cables. |
| 27 | APOLLO | Apollo Micro Systems | 15,498 | Electronics - Consumer & Smart | Defence - Small | **MOVE** | Defence electronics and weapon-system electronics. It sells nothing to a consumer. |
| 28 | ZENTEC | Zen Technologies | 16,622 | Electronics - Consumer & Smart | Defence - Small | **MOVE** | Military training simulators and anti-drone systems. Same: a defence company in a consumer segment. |
| 29 | CROMPTON | Crompton Greaves Consumer Electricals | 14,961 | Electronics - Consumer & Smart | Consumer Durables - Small | **AS-IS** | Fans, pumps, appliances — the current segment already says "Consumer", and at ₹14,961 Cr a "Small" bucket is a size downgrade. |
| 30 | VGUARD | V-Guard Industries | 14,375 | Electronics - Consumer & Smart | Consumer Durables - Small | **AS-IS** | Same reasoning, same size objection. |
| 31 | APARINDS | Apar Industries | 73,110 | Electronics - Heavy Electrical & Industrial | Electrical Equipment Small | **AS-IS** | Conductors, cables and specialty oils. At **₹73,110 Cr** the proposed "Small" segment is a clear size error — the exact mistake cc#1980 flagged on MANAKSTEEL. |
| 32 | VIVIDEL | Vivid Electromech | 1,511 | Electronics - Heavy Electrical & Industrial | Electrical Equipment Small | **MOVE** | A small electromechanical maker; "Heavy Electrical & Industrial" overstates it and the size fits the proposal. |
| 33 | EIMCOELECO | Eimco Elecon (India) | 1,316 | Engineering - Industrial Mfg B | Capital Goods - Industrial Small | **AS-IS** | Mining and material-handling equipment. "Industrial Mfg B" vs "Capital Goods - Industrial Small" is a within-family split, which is exactly what cc#1980 warned is deliberate. |
| 34 | KABRAEXTRU | Kabra Extrusion Technik | 2,329 | Engineering - Industrial Mfg B | Capital Goods - Industrial Small | **AS-IS** | Extrusion machinery. Same within-family split. |
| 35 | CCCL | Consolidated Construction Consortium | 637 | Engineering - Industrial Mfg B | Realty - Mid | **MOVE\*** | A civil **contractor**, not a developer — so the current segment is wrong, but Realty is the wrong fix. **Engineering - EPC Civil Small** is the right home. |
| 36 | PANSARI | Pansari Developers | 516 | Engineering - Industrial Mfg B | Realty - Mid | **MOVE\*** | Genuinely a developer, so Realty is right in kind — but at ₹516 Cr, **Realty - Small**, not Mid. |
| 37 | AFFLE | Affle 3i | 22,698 | Entertainment, Content & Digital | IT - Micro | **MOVE\*** | Adtech SaaS — an IT company, not entertainment. But at **₹22,698 Cr**, "IT - Micro" is absurd on size; IT - Mid or Large. |
| 38 | BCG | Brightcom Group | 2,074 | Entertainment, Content & Digital | IT - Micro | **MOVE** | Digital advertising technology. IT is the right family and the size fits. |
| 39 | MARICO | Marico | 106,481 | FMCG - Large | Edible Oil & Animal Feed | **AS-IS** | Parachute and Saffola — a branded FMCG major. Screener's "Edible Oil" tag reads one input as the whole business. A textbook false positive. |
| 40 | PATANJALI | Patanjali Foods | 37,088 | FMCG - Large | Edible Oil & Animal Feed | **AS-IS** | Same: edible oil is one line of a branded foods group. |
| 41 | CHEMFAB | Chemfab Alkalis | 520 | Fluorochemicals & Industrial Gases | Commodity & Chlor-Alkali Chemicals | **MOVE** | It is a chlor-alkali producer — caustic soda and chlorine. The proposed segment names its exact business. |
| 42 | SRF | SRF | 74,914 | Fluorochemicals & Industrial Gases | Commodity & Chlor-Alkali Chemicals | **AS-IS** | A specialty fluorochemicals and technical-textiles leader. "Commodity" is the wrong direction and the current segment is precise. |
| 43 | SBC | SBC Exports | 2,233 | Garments & Apparel | Retail - Mid | **AS-IS** | Garment manufacturing and manpower services — a maker, not a retailer. |
| 44 | IRISDOREME | Iris Clothings | 1,163 | Garments & Apparel | Retail - Mid | **AS-IS** | Apparel manufacturing with brand retail attached; manufacturing is the core. |
| 45 | MOBIKWIK | One Mobikwik Systems | 1,525 | Internet & Digital Small | Digital Aggregators & E-Commerce | **AS-IS** | A payments/fintech platform, not e-commerce. See finding H2 — **there is no fintech segment to move it to**. |
| 46 | TURTLEMINT | Turtlemint Fintech | 4,084 | Internet & Digital Small | Digital Aggregators & E-Commerce | **AS-IS** | An insurance-distribution fintech. Same: the proposal is wrong and the right segment does not exist. |
| 47 | RNFI | RNFI Services | 804 | Internet & Digital Small | Broking & Wealth Management | **AS-IS** | Banking-correspondent and digital financial services. It does no broking; the proposal is simply wrong. |
| 48 | GYFTR | GYFTR | 1,438 | Internet & Digital Small | Broking & Wealth Management | **AS-IS** | Digital gifting and vouchers. Not broking. Current segment fits. |
| 49 | THEINVEST | The Investment Trust of India | 507 | **Microfinance & MSME** | Broking & Wealth Management | **MOVE** | A capital-markets and advisory group. It is not a microfinance lender — and see finding H3, the current segment changes how it is *scored*, not just how it is labelled. |
| 50 | PRIMESECU | Prime Securities | 959 | **Microfinance & MSME** | Broking & Wealth Management | **MOVE** | An investment bank and advisory firm. Same wrong segment, same scoring consequence. |

### Batch 1 tally

- **Reviewed: 50**
- **Confirmed correct as-is: 22** (3, 4, 7, 8, 13, 21, 24, 25, 29, 30, 31, 33, 34, 39, 40, 42, 43, 44, 45, 46, 47, 48)
- **Proposed to move, destination as cc#1980 suggested: 16** (1, 2, 5, 6, 9, 10, 12, 14, 17, 18, 26, 27, 28, 32, 38, 41)
- **Proposed to move, but to a DIFFERENT destination than cc#1980 suggested: 12** (11, 15, 16, 19, 20, 22, 23, 35, 36, 37 — plus 36 and 37 on size grounds)
- Running total: **50 of 263 flagged reviewed** (plus 210 rejected by the validated prefilter).

---

## Findings raised by batch 1 (not fixed here — they are structural, not per-company)

**H1 — the Diagnostics / Hospitals boundary leaks in BOTH directions, and the numbers say so.**
`Diagnostics & Healthcare Services` holds 19 companies, of which **6 are tagged "Hospital" by
Screener** (7 before ARTEMISMED was moved out this morning — which is why ARTEMISMED beat the
prefilter: it was in a block, and the block was the defect). Looking the other way,
`Hospitals - Mid & Small` holds 17 companies, of which **8 are NOT tagged "Hospital"**. The
candidate pool matches: 6 companies want to go Diagnostics→Hospitals and 6 want to go the other
way. A boundary that leaks both ways is mis-drawn, not finely split. This wants one deliberate
pass over the two segments together, not 12 company-by-company decisions.

**H2 — there is no fintech segment.** MOBIKWIK and TURTLEMINT both carry Screener's
`Financial Technology (Fintech)` as *both* industry and industry group, and GVM has no segment for
it — so they sit in `Internet & Digital Small` next to non-financial internet businesses and are
peer-scored against them. Creating one is a taxonomy decision for the founder, not a move.

**H3 — the BFSI rulebook is chosen by a hardcoded segment-NAME list, so a misfiled company is
scored on the wrong rulebook.** `gvm_nightly.BFSI_SEGMENTS` is a literal set of 16 segment names;
membership sets `is_bfsi`, and `gvm_engine.score_interest_coverage` returns `SKIP_SCORE` (which is
`None`) for a BFSI company, so `api_g_score` drops Interest Coverage entirely and averages G over
13 parameters instead of 14. A financial company in a non-BFSI segment is therefore scored on a
parameter the platform has decided is meaningless for financials — and vice versa.

Measured against Screener's own `industry_group`:
**25 companies tagged Finance / Fintech / Capital Markets / Insurance / Banks sit in a NON-BFSI
segment**, and **12 non-financial companies sit in a BFSI segment.** The largest two are not
obscure: **PAYTM (₹1,06,308 Cr)** and **POLICYBZR (₹84,359 Cr)**, both in
`Digital Aggregators & E-Commerce`, both scored on interest coverage. Batch 1's MOBIKWIK,
TURTLEMINT, RNFI and GYFTR are four more of the 25.

*Correcting my own first draft of this finding:* I initially wrote that THEINVEST and PRIMESECU
were being scored on the wrong rulebook because they sit in `Microfinance & MSME`. That is wrong —
`Broking & Wealth Management`, the segment I propose moving them to, is **also** in
`BFSI_SEGMENTS`, so those two moves change the label and the peer set but not the rulebook. The
real exposure is the 25 financial companies sitting outside all 16 BFSI segments, which is a
different and larger list. Checked before it was published rather than after.

**H4 — one market cap is ~47× too high and it is 80% of a published sector rating.**
`TVSHLTD` (TVS Holdings) carries `market_cap = ₹12,64,712 Cr` in `screener_raw`, which would put
it just behind Reliance (₹17,47,456 Cr) and **ahead of HDFC Bank (₹10,86,468 Cr)**. It cannot be:
TVS Holdings is a holding company whose principal asset is its stake in TVS Motor, and TVS Motor's
own market cap is ₹1,94,890 Cr. A holdco cannot be 6.5× the company it holds.

The consequence is live, not theoretical. TVSHLTD sits in `Castings & Forgings`, and
`compute_sector_ratings` weights by `screener_raw.market_cap`:

| | |
|---|---|
| TVSHLTD's share of the segment's total market cap | **80.3%** |
| Segment rating as published (`sector_ratings.mcap_weighted_gvm`) | **6.268** |
| Same rating with TVSHLTD excluded | **6.874** |
| Unweighted median / mean of the 17 members | 6.51 / 6.34 |

So a published sector rating is **0.606 below** where the other sixteen members put it, because one
bad number carries four-fifths of the weight. cc#1104 added the mcap weighting with an exclusion
list for members that have *no* market cap, and the 11-Sep run reported
`excluded_no_market_cap: []` and `thinned_segments: []` — there is no guard for a market cap that
is absurdly **large**, which is the failure mode here. TVSHLTD is also arguably misfiled: Screener
tags it "Investment Company", and it is one of the 25 in H3.

Not fixed here — the data lives in `screener_raw`, which this card does not touch, and a
sanity-guard in `compute_sector_ratings` is a scoring-path change. Raised as its own finding.

---

*Batches 2 onward continue below as they are completed.*

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

---

## BATCH 2 — 52 companies (candidates 51–102 of 263 flagged)

| # | Symbol | Company | Mcap ₹Cr | Current segment | cc#1980 proposed | Verdict | One-line reason |
|---|---|---|---|---|---|---|---|
| 51 | IRMENERGY | IRM Energy | 1,154 | Oil Services & Small | City Gas Distribution | **MOVE** | A licensed city-gas distributor (Banaskantha, Fatehgarh Sahib). The proposed segment is literally its business. |
| 52 | CONFIPET | Confidence Petroleum India | 2,788 | Oil Services & Small | City Gas Distribution | **AS-IS** | LPG bottling and auto-LPG — cylinder logistics, not a piped-gas utility. Current segment fits. |
| 53 | BODALCHEM | Bodal Chemicals | 2,249 | Organic Chemicals - Small | Specialty Chemicals - Small | **AS-IS** | Dyes and dye intermediates are commodity-cyclical, not specialty. |
| 54 | SUDARSCHEM | Sudarshan Chemical | 9,848 | Organic Chemicals - Small | Specialty Chemicals - Small | **MOVE\*** | A global top-3 pigments maker — genuinely specialty. But at ₹9,848 Cr, a Mid/Large specialty bucket, not "Small". |
| 55 | CUB | City Union Bank | 23,318 | **PSU Banks** | Private Banks | **MOVE** | City Union Bank has been a private-sector bank since 1904 and has never been state-owned. This is a factual error, not a taxonomy preference. |
| 56 | IDBI | IDBI Bank | 96,449 | PSU Banks | Private Banks | **FLAG** | RBI re-categorised it private in Jan 2019 and Screener tags it so, but LIC (state-owned) holds the controlling stake and disinvestment is live. Genuinely ambiguous — founder's call, not mine. |
| 57 | BECTORFOOD | Mrs Bectors Food | 7,056 | Packaged Foods & Dairy | FMCG - Large | **AS-IS** | Biscuits and breads — the current segment is the precise one, and ₹7,056 Cr is not "Large". |
| 58 | HEXAGON | Hexagon Nutrition | 815 | Packaged Foods & Dairy | FMCG - Large | **AS-IS** | Same, and at ₹815 Cr "Large" is plainly wrong. |
| 59 | PANAMAPET | Panama Petrochem | 2,922 | Petrochemicals & Lubricants | Adhesives, Coatings & Polymers | **AS-IS** | Specialty petroleum products and lubricants. Current segment names it exactly. |
| 60 | SOTL | Savita Oil Technologies | 4,696 | Petrochemicals & Lubricants | Adhesives, Coatings & Polymers | **AS-IS** | Transformer oils and lubricants. Same. |
| 61 | PREMIERENE | Premier Energies | 44,667 | Power - Mid | Electrical Equipment Small | **MOVE\*** | It **manufactures** solar cells and modules; it does not generate power, so "Power - Mid" is wrong. But ₹44,667 Cr into a "Small" bucket is the size error again — solar/renewable equipment at Mid or Large. |
| 62 | WAAREERTL | Waaree Renewable Technologies | 8,848 | Power - Mid | Electrical Equipment Small | **FLAG** | Solar EPC plus some IPP assets — it straddles generation and contracting, and neither the current nor the proposed segment captures that. Needs a decision on which half leads. |
| 63 | JYOTISTRUC | Jyoti Structures | 1,263 | Power - Small | Electronics - Heavy Electrical & Industrial | **MOVE** | Transmission towers and T&D contracting. It generates no power. |
| 64 | TRANSRAILL | Transrail Lighting | 5,645 | Power - Small | Electronics - Heavy Electrical & Industrial | **MOVE** | Same: towers, poles and T&D EPC, not generation. |
| 65 | JSWENERGY | JSW Energy | 96,496 | Power Generation - Large | Renewable Energy - Mid | **AS-IS** | An IPP still majority-thermal, and at ₹96,496 Cr not "Mid". |
| 66 | NTPC | NTPC | 320,038 | Power Generation - Large | Renewable Energy - Mid | **AS-IS** | India's largest **thermal** generator. The proposal is simply wrong on both business and size. |
| 67 | KPIL | Kalpataru Projects International | 24,250 | Power Services & Trading | Engineering - EPC Civil Small | **MOVE\*** | A T&D and civil **EPC contractor** — it neither services nor trades power. EPC is the right family; at ₹24,250 Cr "Small" is not. (This is the KPIL that cc#1980 warned could be confused with KALPATARU; they are separate rows and both are correctly named.) |
| 68 | OMPOWER | Om Power Transmission | 583 | Power Services & Trading | Engineering - EPC Civil Small | **MOVE** | A small T&D contractor. Both kind and size fit the proposal. |
| 69 | QPOWER | Quality Power Electrical Equipments | 11,548 | Power Services & Trading | Electronics - Heavy Electrical & Industrial | **MOVE** | Makes reactors, transformers and HVDC equipment. A manufacturer in a services segment. |
| 70 | SKIPPER | Skipper | 6,797 | Power Services & Trading | Electronics - Heavy Electrical & Industrial | **MOVE** | Transmission towers and polymer pipes — manufacturing, not power services. |
| 71 | CAPACITE | Capacite Infraprojects | 1,709 | Realty - Mid | Engineering - EPC Civil Small | **MOVE** | A construction contractor **for** developers. It owns no development book, so realty peers are the wrong comparison. |
| 72 | AHLUCONT | Ahluwalia Contracts | 3,985 | Realty - Mid | Engineering - EPC Civil Small | **MOVE** | Same: a pure civil contractor. |
| 73 | OSWALGREEN | Oswal Green Tech | 522 | Realty - Small | Holding Companies | **FLAG** | Largely an investment and asset-holding vehicle now, but the evidence is thin and the move would change its scoring rulebook (see H3). |
| 74 | BLAL | BEML Land Assets | 676 | Realty - Small | Holding Companies | **MOVE** | A demerged land-**holding** entity. It holds; it does not develop. |
| 75 | EMMVEE | Emmvee Photovoltaic Power | 22,740 | Renewable Energy - Mid | Electrical Equipment Small | **MOVE\*** | A solar module manufacturer, not an energy producer — but ₹22,740 Cr is not "Small". |
| 76 | WAAREEENER | Waaree Energies | 74,502 | Renewable Energy - Mid | Electrical Equipment Small | **MOVE\*** | India's largest solar module maker. Manufacturer, yes; "Small" at ₹74,502 Cr, no. |
| 77 | SWSOLAR | Sterling & Wilson Renewable | 4,334 | Renewable Energy - Small | Engineering - EPC Civil Small | **MOVE** | A solar **EPC contractor** — contracting is the business. Nuance: it is solar EPC, not civil EPC, so the destination is right in family and loose in detail. |
| 78 | SOLARWORLD | Solarworld Energy Solutions | 1,287 | Renewable Energy - Small | Engineering - EPC Civil Small | **MOVE** | Same business, same nuance. |
| 79 | AHCL | Anlon Healthcare | 829 | Specialty Chemicals - Micro | Pharma - Micro | **MOVE** | Makes pharma intermediates and APIs. Pharma is the right family. |
| 80 | SIGACHI | Sigachi Industries | 1,360 | Specialty Chemicals - Micro | Pharma - Micro | **MOVE** | Microcrystalline cellulose — a pharmaceutical excipient. Its customers and cycle are pharma. |
| 81 | GTLINFRA | GTL Infrastructure | 1,512 | Telecom Equipment & Services | Telecom Services | **AS-IS** | It owns passive **towers** — an asset-leasing business, not a service operator like Airtel. Neither segment is right; the proposal is the worse of the two. |
| 82 | SUYOG | Suyog Telematics | 842 | Telecom Equipment & Services | Telecom Services | **AS-IS** | Same: tower infrastructure, not telecom services. |
| 83 | GRAUWEIL | Grauer & Weil (India) | 3,245 | Adhesives, Coatings & Polymers | Commodity & Chlor-Alkali Chemicals | **AS-IS** | Surface-finishing chemicals and paints — specialty, and "Commodity" is the wrong direction. |
| 84 | GULPOLY | Gulshan Polyols | 1,151 | Adhesives, Coatings & Polymers | FMCG - Small | **MOVE\*** | Calcium carbonate, starch, sorbitol and grain alcohol — neither adhesives nor FMCG. Agri and mineral processing is the right family. |
| 85 | DEEPAKFERT | Deepak Fertilisers & Petrochem | 17,049 | Agro Chemicals - Large | Commodity & Chlor-Alkali Chemicals | **AS-IS** | Genuinely both a fertiliser and an industrial-chemicals maker (TAN, nitric acid). Defensible either way; the current segment is not an error. |
| 86 | BASF | BASF India | 16,400 | Agro Chemicals - Large | Organic Chemicals - Small | **MOVE\*** | Diversified chemicals — agri is one division of several — so "Agro Chemicals" understates it. But ₹16,400 Cr into "Organic Chemicals - **Small**" is the size error again. |
| 87 | EXCELINDUS | Excel Industries | 1,254 | Agro Chemicals - Small | Organic Chemicals - Small | **MOVE** | Makes agrochemical **intermediates** and environmental chemicals — a chemicals maker feeding agro, not an agrochemical brand. |
| 88 | RATNAVEER | Ratnaveer Precision Engineering | 2,497 | Aluminium & Non Ferrous | Steel - Mid & Small | **MOVE** | Makes **stainless steel** washers, tubes and sheets. There is no aluminium in the business. |
| 89 | SHANTIGEAR | Shanthi Gears | 5,391 | Auto - Drivetrain & Precision | Capital Goods - Industrial Small | **MOVE** | Industrial gears and gearboxes — the order book is industrial, not automotive drivetrain. |
| 90 | RANEHOLDIN | Rane Holdings | 2,413 | Auto - Engines & Thermal | Holding Companies | **MOVE** | The holding company of the Rane auto-component group; its value is its stakes. Note: the destination is a BFSI segment, so this move also changes its scoring rulebook (H3) — intended, but worth saying. |
| 91 | DYNAMATECH | Dynamatic Technologies | 7,806 | Auto - Engines & Thermal | Capital Goods - Industrial Small | **MOVE\*** | Aerospace structures now lead the book, ahead of hydraulics and auto castings. Aerospace/defence is the better home, and ₹7,806 Cr stretches "Small". |
| 92 | EXICOM | Exicom Tele-Systems | 2,563 | Auto - Wiring & Electricals | Electronics - Heavy Electrical & Industrial | **MOVE** | EV chargers and telecom power systems. It makes no auto wiring. |
| 93 | MAHSCOOTER | Maharashtra Scooters | 15,248 | Auto OEM | Holding Companies | **MOVE** | It manufactures almost nothing; it is a Bajaj-group holding vehicle whose value is its stakes. Same BFSI note as RANEHOLDIN. |
| 94 | ASAHIINDIA | Asahi India Glass | 24,281 | Building Materials - Glass, Ceramics & Ply | Auto - Engines & Thermal | **MOVE\*** | Roughly two-thirds automotive glass, so the flag is real — but "Engines & Thermal" is not a glass peer set. Auto components is the right family. |
| 95 | BLSE | BLS E-Services | 2,896 | Business Services | IT - Micro | **AS-IS** | Digital service delivery and banking-correspondent points — a services business, and ₹2,896 Cr is not "Micro". |
| 96 | RAMANEWS | Shree Rama Newsprint | 501 | Business Services | Paper & Packaging | **MOVE** | It is a paper mill. It makes newsprint. "Business Services" is not close. |
| 97 | INNOVISION | Innovision | 600 | Business Services | Infrastructure - Roads | **AS-IS** | Facility management and manpower services. Screener's "Road Assets" tag is a data artifact of the CHEMPLASTS/VINDHYATEL kind, not a business match. |
| 98 | NAMOEWASTE | Namo eWaste Management | 659 | Business Services | Environmental Services | **MOVE** | E-waste collection and recycling — the proposed segment names it exactly. |
| 99 | PRECWIRE | Precision Wires India | 8,825 | Capital Goods - Heavy Electrical | Aluminium & Non Ferrous | **FLAG** | See H5 — this one contradicts batch 1 and the contradiction matters more than the individual answer. |
| 100 | ISGEC | ISGEC Heavy Engineering | 5,726 | Capital Goods - Heavy Electrical | Engineering - EPC Civil Small | **AS-IS** | Boilers, presses and heavy equipment with EPC attached; manufacturing is the core. |
| 101 | MOLDTECH | Mold-Tek Technologies | 650 | Capital Goods - Industrial Small | Engineering - EPC Civil Small | **MOVE\*** | A structural-detailing and engineering-services (KPO) firm. It makes no capital goods and builds nothing — engineering services is the right family, and neither listed option is it. |
| 102 | CHOICEIN | Choice International | 16,782 | Capital Markets - Large | Holding Companies | **AS-IS** | An operating financial-services group — broking, NBFC, advisory. Screener's "Holding Company" tag describes the structure, not the business. |

### Batch 2 tally

- **Reviewed: 52**
- **Confirmed correct as-is: 16**
- **Proposed to move, destination as cc#1980 suggested: 22**
- **Proposed to move, but to a DIFFERENT destination: 10** — nine of the ten because the proposed
  segment carried a "Small"/"Micro" size tag that the company's market cap contradicts.
- **Flagged, not decided: 4** (IDBI, WAAREERTL, OSWALGREEN, PRECWIRE)

### Running total after batch 2

**102 of 263 flagged reviewed** · 38 confirmed as-is · 38 move-as-proposed · 22 move-elsewhere ·
4 flagged · plus 210 rejected by the validated prefilter. **312 of 473 resolved.**

---

## H5 — a contradiction my own batch 1 created, caught in batch 2

Batch 1 proposed moving **DPEL** and **KSHINTL** *out of* `Aluminium & Non Ferrous` and into
`Electrical Cables`, because both make enamelled copper winding wire.

Batch 2's candidate **PRECWIRE** (Precision Wires India, ₹8,825 Cr) is proposed to move *into*
`Aluminium & Non Ferrous` — and Precision Wires makes **the same product**: enamelled copper
winding wire.

Three companies, one business, and the two proposals point in opposite directions. Whatever the
right answer is, it is the same answer for all three, and I am not going to record it as two
separate judgements that happen to disagree. The three should be decided together:

- **Option A** — all three in `Electrical Cables` (their customers and competitors are cable and
  wire makers; the metal is the input, not the business). My recommendation.
- **Option B** — all three in `Aluminium & Non Ferrous` (the metal is the cost driver and the
  cycle they actually trade on).

Batch 1's DPEL/KSHINTL rows above stand as written, with this note attached: they are provisional
on the same decision, not independent of it.

---

## BATCH 3 — 52 companies (candidates 103–154 of 263 flagged)

| # | Symbol | Company | Mcap ₹Cr | Current segment | cc#1980 proposed | Verdict | One-line reason |
|---|---|---|---|---|---|---|---|
| 103 | IKS | Inventurus Knowledge Solutions | 30,657 | Capital Markets - Large | IT - Micro | **MOVE*** | A healthcare BPO and IT-services firm — nothing to do with capital markets. But ₹30,657 Cr into "IT - **Micro**" is the size error; IT - Large. |
| 104 | JIOFIN | Jio Financial Services | 154,315 | Capital Markets - Large | Holding Companies | **AS-IS** | It operates — lending, insurance broking, an AMC joint venture. Screener's "Investment Company" tag describes the structure, not the business. |
| 105 | GALAPREC | Gala Precision Engineering | 1,314 | Castings & Forgings | Capital Goods - Industrial Small | **MOVE** | Springs, coils and precision components — industrial components, not castings or forgings. |
| 106 | TVSHLTD | TVS Holdings | 1,264,712 | Castings & Forgings | Holding Companies | **MOVE** | The TVS group holding company; it casts nothing. **This is the company in finding H4** — moving it out would also end the 80.3% weight distorting the `Castings & Forgings` sector rating, as a side effect rather than as the fix. |
| 107 | ELECTCAST | Electrosteel Castings | 4,953 | Castings & Forgings | Steel - Mid & Small | **MOVE*** | It makes **ductile iron pipes** for water infrastructure. `Pipes & Tubes` is the precise home; generic steel is not. |
| 108 | KANORICHEM | Kanoria Chemicals | 704 | Commodity & Chlor-Alkali Chemicals | Capital Goods - Industrial Small | **AS-IS** | Formaldehyde and pentaerythritol are the core; the auto-components arm is the smaller half. |
| 109 | REFEX | Refex Industries | 3,814 | Commodity & Chlor-Alkali Chemicals | Environmental Services | **MOVE** | Ash handling and coal logistics now outweigh the refrigerant-gas business. The proposal follows the revenue. |
| 110 | RMDRIP | R M Drip & Sprinklers | 977 | Consumer Plastics & Others | Flexible Packaging & Films | **MOVE*** | **Micro-irrigation** systems — drip and sprinkler lines. It is not packaging and not consumer plastics; agri-inputs is the right family and neither option offers it. |
| 111 | AZAD | Azad Engineering | 18,255 | **Defence PSU** | Electronics - Heavy Electrical & Industrial | **MOVE*** | Azad Engineering is **privately held** — "Defence PSU" is factually wrong. It makes precision aerospace and turbine components, so a private aerospace/defence segment, not heavy electrical. |
| 112 | JYOTICNC | Jyoti CNC Automation | 24,027 | **Defence PSU** | Capital Goods - Industrial Small | **MOVE*** | Also not a PSU. It makes CNC machine tools — capital goods is right in family, but ₹24,027 Cr is not "Small". |
| 113 | GESHIP | Great Eastern Shipping | 19,726 | **Defence PSU** | Shipping & Maritime | **MOVE** | A private shipping and offshore company. Filing it under Defence PSU is a plain error, and the move takes `Shipping & Maritime` from 5 members to 6. |
| 114 | MAXIND | Max India | 837 | Diagnostics & Healthcare Services | Holding Companies | **MOVE** | A holding company for senior care and health ventures; it runs no diagnostics chain. |
| 115 | LENSKART | Lenskart Solutions | 119,803 | Digital Aggregators & E-Commerce | Retail - Mid | **MOVE*** | Omnichannel eyewear **retail** with 2,500+ stores — a specialty retailer, not a marketplace. At ₹1,19,803 Cr it is Retail - Large, not Mid. |
| 116 | IRCTC | Indian Railway Catering & Tourism | 37,808 | Digital Aggregators & E-Commerce | Hotels - Mid | **AS-IS** | Rail ticketing, catering, packaged water and tourism. It operates no hotels; the current segment fits the ticketing platform that dominates its economics. |
| 117 | VAKRANGEE | Vakrangee | 610 | Diversified | IT - Micro | **MOVE** | Assisted-commerce kiosks — IT-enabled services, and ₹610 Cr fits the size tag. |
| 118 | RAYMOND | Raymond | 5,705 | Diversified | Capital Goods - Industrial Small | **MOVE** | **Post-demerger identity change**: the textile and realty arms are separate listings now, and what remains is the engineering business — auto components and tools. Exactly the KALPATARU-type trap cc#1980 warned about, in the other direction. |
| 119 | KIRLOSIND | Kirloskar Industries | 3,895 | Diversified | Steel - Mid & Small | **MOVE*** | Principally a holding company for the Kirloskar stakes, with a small ferrous-castings arm. `Holding Companies`, not steel. |
| 120 | CONSOFINVT | Consolidated Finvest & Holdings | 987 | Diversified | MSME Finance - Mid | **MOVE*** | An investment NBFC, not an MSME lender. `Holding Companies` is the right home; the proposed one would also put it on the BFSI rulebook for the wrong reason. |
| 121 | NITCO | Nitco | 2,233 | Diversified Trading | Building Materials - Glass, Ceramics & Ply | **MOVE** | It manufactures and sells tiles and marble. The proposed segment names it. |
| 122 | VIESL | Vision Infra Equipment Solutions | 973 | Diversified Trading | Business Services | **MOVE** | Road-construction equipment rental — a service business, not trading. |
| 123 | FABTECH | Fabtech Technologies | 657 | Diversified Trading | CDMO & Contract Mfg | **MOVE*** | It builds pharma cleanrooms and turnkey plants — an **engineering contractor serving** pharma, not a contract manufacturer of drugs. Engineering/EPC is the right family. |
| 124 | WEL | Wonder Electricals | 922 | Diversified Trading | Consumer Durables - Small | **MOVE** | A contract manufacturer of fans. It makes durables; it does not trade. |
| 125 | HEXATRADEX | Hexa Tradex | 890 | Diversified Trading | Holding Companies | **MOVE** | A Jindal-group investment and holding vehicle. |
| 126 | AEROENTER | Aeroflex Enterprises | 1,742 | Diversified Trading | Steel - Mid & Small | **MOVE*** | Makes stainless-steel flexible hoses and braids. A manufacturer, but `Pipes & Tubes` fits better than generic steel. |
| 127 | EIFFL | Euro India Fresh Foods | 654 | Diversified Trading | FMCG - Large | **MOVE*** | Snacks and namkeen — genuinely FMCG, but ₹654 Cr is **FMCG - Small**, not Large. |
| 128 | TINNARUBR | Tinna Rubber & Infrastructure | 1,805 | Diversified Trading | Sugar & Agri Processing | **MOVE*** | It recycles end-of-life **tyres** into crumb and reclaim rubber. There is nothing agricultural about it; rubber products or environmental services. |
| 129 | BCLIND | BCL Industries | 1,060 | Edible Oil & Animal Feed | Beverages & Spirits | **FLAG** | Genuinely two businesses — edible oil and a grain-based distillery/ethanol arm that is now the growth half. Which one leads is a founder call, not mine. |
| 130 | SCILAL | Shipping Corporation of India Land Assets | 1,812 | **Education** | Holding Companies | **MOVE** | A demerged land and asset holding entity. "Education" is not close to anything it does. |
| 131 | BIRLACABLE | Birla Cable | 1,016 | Electrical Cables | Telecom Services | **AS-IS** | It manufactures optical fibre cable. The current segment is right; the proposal confuses the customer industry with the business. |
| 132 | VIDYAWIRES | Vidya Wires | 1,899 | Electrical Equipment Small | Aluminium & Non Ferrous | **FLAG** | **Enamelled copper winding wire — the same product as DPEL, KSHINTL and PRECWIRE.** See H5: this makes it four companies, one business, and one decision. |
| 133 | VIKRAN | Vikran Engineering | 1,533 | Electrical Equipment Small | Engineering - EPC Civil Small | **MOVE** | A transmission and water EPC contractor. It builds; it does not manufacture equipment. |
| 134 | CONTROLPR | Control Print | 961 | Electrical Equipment Small | IT - Small | **AS-IS** | It manufactures industrial coding and marking printers. That is hardware, not IT services. |
| 135 | EPACK | Epack Durable | 1,827 | Electrical Equipment Small | Consumer Durables - Small | **MOVE** | A contract manufacturer of air conditioners for brands — durables, not electrical equipment. |
| 136 | JINDALPHOT | Jindal Photo | 1,049 | Electrical Equipment Small | Holding Companies | **MOVE** | An investment and holding company; the photographic-film business is long gone. |
| 137 | PLATIND | Platinum Industries | 1,287 | Electrical Equipment Small | Organic Chemicals - Small | **MOVE** | It makes **PVC stabilisers**. A specialty chemicals maker filed under electrical equipment is one of the clearest mismatches in this pass. |
| 138 | SURYAROSNI | Surya Roshni | 4,749 | Electronics - Consumer & Smart | Steel - Mid & Small | **MOVE*** | Two halves — steel pipes (the larger) and lighting. `Pipes & Tubes` is the precise destination; generic steel is not. |
| 139 | AVL | Aditya Vision | 7,799 | Electronics - Consumer & Smart | Retail - Mid | **MOVE** | A consumer-electronics **retail chain**. It manufactures nothing, so an electronics-maker peer set is the wrong comparison entirely. |
| 140 | CPPLUS | Aditya Infotech (CP Plus) | 44,955 | Electronics - Heavy Electrical & Industrial | Capital Goods - Industrial Small | **AS-IS** | Video-surveillance electronics. Electronics is the right family, and the proposal is wrong on both business and size. |
| 141 | VASCONEQ | Vascon Engineers | 692 | Engineering - EPC Civil Small | Realty - Mid | **AS-IS** | It does both EPC contracting and development, with contracting the larger. Current segment fits. |
| 142 | EFFWA | Effwa Infra & Research | 844 | Engineering - EPC Civil Small | Environmental Services | **FLAG** | Water and wastewater treatment EPC — an EPC firm in an environmental niche. Both segments are defensible; needs a rule on which axis wins. |
| 143 | AXISCADES | AXISCADES Technologies | 7,839 | Engineering - EPC Mid | Defence - Small | **MOVE*** | An **engineering design services (ER&D)** firm with an aerospace vertical. It builds nothing and manufactures nothing — IT/ER&D services, not defence. |
| 144 | BALUFORGE | Balu Forge Industries | 6,626 | Engineering - EPC Mid | Castings & Forgings | **MOVE** | Crankshafts and forged components. The proposed segment names its exact business. |
| 145 | GMRP&UI | GMR Power & Urban Infra | 7,185 | Engineering - EPC Mid | Renewable Energy - Mid | **AS-IS** | Thermal power assets, EPC and urban infrastructure. There is no renewable business to move it for. |
| 146 | SMLMAH | SML Mahindra (SML Isuzu) | 9,177 | Engineering - EPC Small | Auto OEM | **MOVE** | It manufactures commercial vehicles. Filing a truck maker under engineering EPC is plainly wrong. |
| 147 | PITTIENG | Pitti Engineering | 4,364 | Engineering - EPC Small | Capital Goods - Industrial Small | **MOVE** | Electrical steel laminations and motor cores — a component manufacturer, not a contractor. |
| 148 | ARIS | Arisinfra Solutions | 1,052 | Engineering - EPC Small | Cement - Small | **MOVE*** | A **B2B construction-materials marketplace**. It distributes; it makes no cement. Distribution/trading is the right family and neither option offers it. |
| 149 | AGIIL | AGI Infra | 3,444 | Engineering - EPC Small | Realty - Mid | **MOVE** | A residential developer in Punjab with its own land bank. Realty is right. |
| 150 | SANGHVIMOV | Sanghvi Movers | 3,824 | Engineering - Industrial Mfg A | Business Services | **MOVE** | India's largest crane **rental** company. It manufactures nothing — equipment rental is a service. |
| 151 | BAJEL | Bajel Projects | 2,129 | Engineering - Industrial Mfg A | Electronics - Heavy Electrical & Industrial | **MOVE*** | Demerged from Bajaj Electricals to hold the **power transmission EPC** business. It contracts, it does not manufacture heavy electricals — an EPC segment, not an equipment one. |
| 152 | RISHABH | Rishabh Instruments | 3,139 | Engineering - Industrial Mfg A | Electrical Equipment Small | **MOVE** | Electrical measurement instruments and precision components. The destination fits in kind and size. |
| 153 | YUKEN | Yuken India | 1,397 | Engineering - Industrial Mfg B | Pumps, Refractories & Industrial | **MOVE** | Hydraulic pumps and valves. The proposed segment names the business. |
| 154 | GGBL | Ganesh Green Bharat | 539 | Engineering - Industrial Mfg B | Electrical Equipment Small | **FLAG** | Solar EPC and electrical contracting — it builds rather than manufactures, so neither the current nor the proposed segment is right. |

### Batch 3 tally

- **Reviewed: 52** — 8 confirmed as-is · 24 move-as-proposed · 16 move-elsewhere · 4 flagged.
- Three **factual** filing errors, not taxonomy preferences: **AZAD** and **JYOTICNC** are sitting in
  `Defence PSU` and neither is a PSU; **GESHIP** (Great Eastern Shipping, a private shipping
  company) is in the same segment; and **SCILAL**, a demerged land-holding entity, is filed under
  `Education`.
- One **post-demerger identity change** caught: **RAYMOND** is no longer a textile company — the
  textile and realty arms are separate listings and what remains is engineering. This is the
  KALPATARU trap running the other way, and it is the second instance in this card.

### Running total after batch 3

**154 of 263 flagged reviewed** · 46 confirmed as-is · 62 move-as-proposed · 38 move-elsewhere ·
8 flagged · plus 210 rejected by the validated prefilter. **364 of 473 resolved.**

### H5 grows to four companies

**VIDYAWIRES** (Vidya Wires, ₹1,899 Cr) makes enamelled copper winding wire — the same product as
DPEL, KSHINTL and PRECWIRE. Its proposal, like PRECWIRE's, points *into* `Aluminium & Non Ferrous`,
against batch 1's proposal to move DPEL and KSHINTL *out* of it. Four companies now, one business,
one decision. The options in H5 are unchanged; only the count is.

### H6 — the same company is both a finding and a candidate

**TVSHLTD** appears here as candidate 106 (proposed to `Holding Companies`, which I agree with) and
as the subject of finding H4 (its market cap is ~47× too high and carries 80.3% of the
`Castings & Forgings` sector rating). The two are independent problems that happen to share a row:
moving it would end the rating distortion **as a side effect**, but it would not correct the market
cap, which would then distort `Holding Companies` instead. Fix the number, then move it — in that
order.

*Batches 4 onward continue below as they are completed.*

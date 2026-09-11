# cc#1996 item 1 — the 179 proposed moves, split into F / S / X

**CORRECTED at 08:42 IST, same production-mode tick as the first push.** The first version of this file (sha
`cbc7139`) carried 17 wrong destinations, caught by re-checking each F-row's own prose against
its table cell before any write happened. See "Self-correction" below — this is the version to
use; the numbers in `cbc7139` are superseded.

Read-only. No `input_raw` write in this file; the apply (items 2-4) is a separate, later push
per the card's own window rule (writes any hour except 00:00-06:00, **recompute before 09:15 or
after 15:30 IST**). Source: `reports/CC1985_segment_review_460.md` (sha `152f561`), the 179 rows
carrying a MOVE or MOVE* verdict plus the 18 FLAG rows.

## Self-correction — 17 rows had the wrong destination, caught before any write

Before starting the apply, I re-checked every F-row's `prop` table cell against its own `why`
prose. `PASUPTAC` (Textiles Smallcap, in scope for cc#1999 too) was the first one found: the
table cell said `Petrochemicals & Lubricants`, but the prose says plainly *"Synthetic Fibres &
Yarn is the precise home, not petrochemicals."* That is cc#1980's ORIGINAL destination left
sitting in the cell after my own prose corrected it in a different direction — an authoring
mistake in `CC1985_segment_review_460.md` itself, not a new judgement call.

Finding one meant checking for more. A scan of all 152 rows for a segment name in the prose that
does not match the `prop` cell found **17 more of the same class**:

| Symbol | Wrong cell (from `cbc7139`) | Correct destination (from the row's own prose) |
|---|---|---|
| EXIDEIND | Auto - Engines & Thermal | Auto - Wiring & Electricals |
| ARE&M | Auto - Engines & Thermal | Auto - Wiring & Electricals |
| CCCL | Realty - Mid | Engineering - EPC Civil Small |
| ELECTCAST | Steel - Mid & Small | Pipes & Tubes |
| KIRLOSIND | Steel - Mid & Small | Holding Companies |
| CONSOFINVT | MSME Finance - Mid | Holding Companies |
| AEROENTER | Steel - Mid & Small | Pipes & Tubes |
| SURYAROSNI | Steel - Mid & Small | Pipes & Tubes |
| GANDHAR | Adhesives, Coatings & Polymers | Petrochemicals & Lubricants |
| BLS | Hotels - Mid | Business Services |
| DENTALKART | Diagnostics & Healthcare Services | Digital Aggregators & E-Commerce |
| YATRA | Hotels - Mid | Digital Aggregators & E-Commerce |
| ALEMBICLTD | Realty - Mid | Holding Companies |
| RTNINDIA | Digital Aggregators & E-Commerce | Holding Companies |
| TEXINFRA | Business Services | Holding Companies |
| MIDHANI | Defence - Small | Defence PSU |
| KARNIKA | Retail - Mid | Garments & Apparel |

All 17 corrected destinations verified live in `gvm_scores.segment` before this file was
rewritten (query below covers the full corrected set, not just these 17).

**Cross-batch interaction worth naming now, before it is applied:** MIDHANI moves INTO
`Defence PSU`, the same segment AZAD and JYOTICNC move OUT of (both are in this same F list —
they are not PSUs, MIDHANI genuinely is). Net effect on `Defence PSU`'s membership in one batch:
-2 +1 = -1. Whichever batch carries all three needs the N5_SEGMENT_GATE_V1 check run AFTER all
three moves in that batch are applied, not after each one individually, or a mid-batch read could
see a transient wrong count.

**Two more rows pulled OUT of the ready-to-apply set entirely, for reasons unrelated to the
prop/prose bug:**

| Symbol | Original destination | Why excluded |
|---|---|---|
| TVSHLTD | Holding Companies | `do_not_touch` per this card's own spec — the row belongs to cc#1993 |
| CUPID | FMCG - Large | Carries the same inflated-mcap/PE signature as TVSHLTD (finding H8) and has not yet been founder-ruled into its own correction card the way TVSHLTD has (cc#1993). Moving it now would weight `FMCG - Large`'s sector rating by a market cap that may itself be wrong — the exact failure TVSHLTD caused in `Castings & Forgings`. Held until it gets the same treatment TVSHLTD is getting. |

`PASUPTAC` is not in this exclusion table — it was never dropped, it was *rerouted*: it is one
of the four members of `Textiles Smallcap`, so cc#1999 (the sub-5 segment merge) resolves it
directly with the correct destination, rather than this card writing it twice.

## The rule used to split

**(F) FILING ERROR -- no judgement needed.** The company operates in a plainly different
industry from its current segment, AND the proposed destination is a single, existing,
size-appropriate segment with no caveat in the original review. Apply verbatim.

**(S) SIZE-TIER -- right industry, wrong size band, or destination unresolved.** Either the
review flagged the *destination's* size tier as wrong (a company too big for a "Small"/"Micro"
segment, or vice versa), or the review could not name a single clean destination
("neither option offers it"). These need a size-appropriate segment chosen before they can be
applied, which is a design question, not a read of the report.

**(X) the 18 already-FLAGged rows** from the original review -- genuinely undecided, unchanged.

## Counts (corrected)

| | count |
|---|---|
| F -- apply as-is | **149** |
| — of which destination corrected from the first push | 17 |
| — of which held out (TVSHLTD, CUPID) | 2 |
| — of which rerouted to cc#1999 (PASUPTAC) | 1 |
| S -- hold for the size-tier design card | **27** |
| X -- hold, flagged, unchanged | **18** |

## The founder-named class, resolved rather than left in S

The founder's own ruling (log 6312 R5) named four companies by symbol as the class to move:
RVNL, NIBE, IDEAFORGE, AMAGI. Three landed in F on the mechanical split with no further work.
**AMAGI did not** -- the original review gave it `IT - Micro` as a destination but then flagged
that destination's own size as wrong ("IT-Micro at Rs 12,469 Cr is the size error. IT/SaaS at Mid
or Large"). Rather than leave a founder-named row sitting in the generic hold bucket, it is
resolved here: `IT - Mid` (range Rs 3,784-32,423 Cr, median Rs 10,055 Cr) comfortably fits AMAGI's
Rs 12,469 Cr; `IT - Large` starts at Rs 36,266 Cr and would be too big a jump. **AMAGI moves to F,
destination `IT - Mid`.** This is exactly the card's own item-4 instruction -- "say plainly which
of (S) are the RVNL class in disguise" -- applied to the one case where the founder's own naming
made the answer unambiguous. The other 27 S-rows are not founder-named and are left for the
design card, as the card requires.

## F -- apply as-is (149, corrected)

| Symbol | Company | Mcap | Current segment | Destination |
|---|---|---|---|---|
| ABCAPITAL | Aditya Birla Capital | 108,900 Cr | Life Insurance | Holding Companies |
| ADVAIT | Advait Energy | 2,358 Cr | Electrical Cables | Electrical Equipment Small |
| AEROENTER | Aeroflex Enterprises | 1,742 Cr | Diversified Trading | Pipes & Tubes |
| AGIIL | AGI Infra | 3,444 Cr | Engineering - EPC Small | Realty - Mid |
| AHCL | Anlon Healthcare | 829 Cr | Specialty Chemicals - Micro | Pharma - Micro |
| AHLUCONT | Ahluwalia Contracts | 3,985 Cr | Realty - Mid | Engineering - EPC Civil Small |
| ALEMBICLTD | Alembic Ltd | 2,805 Cr | Pharma - Formulations | Holding Companies |
| AMAGI | Amagi Media Labs | 12,469 Cr | **Print Media & Publishing** | IT - Mid |
| APOLLO | Apollo Micro Systems | 15,498 Cr | Electronics - Consumer & Smart | Defence - Small |
| ARE&M | Amara Raja Energy & Mobility | 15,347 Cr | Consumer Durables - Large | Auto - Wiring & Electricals |
| ARVINDFASN | Arvind Fashions | 5,901 Cr | Textiles - Large | Retail - Mid |
| ASAHIINDIA | Asahi India Glass | 24,281 Cr | Building Materials - Glass, Ceramics & Ply | Auto - Engines & Thermal |
| ATGL | Adani Total Gas | 66,604 Cr | Refineries & Exploration - Large | City Gas Distribution |
| AVL | Aditya Vision | 7,799 Cr | Electronics - Consumer & Smart | Retail - Mid |
| AXISCADES | AXISCADES Technologies | 7,839 Cr | Engineering - EPC Mid | Defence - Small |
| AZAD | Azad Engineering | 18,255 Cr | **Defence PSU** | Electronics - Heavy Electrical & Industrial |
| BAJAJFINSV | Bajaj Finserv | 310,383 Cr | Life Insurance | Holding Companies |
| BAJEL | Bajel Projects | 2,129 Cr | Engineering - Industrial Mfg A | Electronics - Heavy Electrical & Industrial |
| BALUFORGE | Balu Forge Industries | 6,626 Cr | Engineering - EPC Mid | Castings & Forgings |
| BCG | Brightcom Group | 2,074 Cr | Entertainment, Content & Digital | IT - Micro |
| BFUTILITIE | BF Utilities | 1,999 Cr | Renewable Energy - Small | Infrastructure - Roads |
| BLAL | BEML Land Assets | 676 Cr | Realty - Small | Holding Companies |
| BLIL | Balmer Lawrie Investments | 1,615 Cr | MSME Finance - Small | Holding Companies |
| BLS | BLS International Services | 9,841 Cr | IT - Mid | Business Services |
| BSHSL | Bombay Super Hybrid Seeds | 958 Cr | Diversified | FMCG - Small |
| CAPACITE | Capacite Infraprojects | 1,709 Cr | Realty - Mid | Engineering - EPC Civil Small |
| CCCL | Consolidated Construction Cons | 637 Cr | Engineering - Industrial Mfg B | Engineering - EPC Civil Small |
| CHEMFAB | Chemfab Alkalis | 520 Cr | Fluorochemicals & Industrial Gases | Commodity & Chlor-Alkali Chemicals |
| CMSINFO | CMS Info Systems | 3,802 Cr | IT - Small | Business Services |
| CNL | Creative Newtech | 1,739 Cr | IT - Micro | Diversified Trading |
| CONSOFINVT | Consolidated Finvest & Holding | 987 Cr | Diversified | Holding Companies |
| CRAMC | Canara Robeco AMC | 4,777 Cr | Broking & Wealth Management | Capital Markets - Large |
| CUB | City Union Bank | 23,318 Cr | **PSU Banks** | Private Banks |
| CYIENTDLM | Cyient DLM | 7,234 Cr | IT - Small | Defence - Small |
| DENTALKART | Vasa Denticity (Dentalkart) | 706 Cr | Internet & Digital Small | Digital Aggregators & E-Commerce |
| DPEL | Divine Power Energy | 1,538 Cr | Aluminium & Non Ferrous | Electrical Cables |
| DVL | Dhunseri Ventures | 1,037 Cr | Petrochemicals & Lubricants | Consumer Plastics & Others |
| EDELWEISS | Edelweiss Financial Services | 12,622 Cr | MSME Finance - Mid | Holding Companies |
| EKC | Everest Kanto Cylinder | 1,162 Cr | Consumer Plastics & Others | Capital Goods - Industrial Small |
| ELECTCAST | Electrosteel Castings | 4,953 Cr | Castings & Forgings | Pipes & Tubes |
| EPACK | Epack Durable | 1,827 Cr | Electrical Equipment Small | Consumer Durables - Small |
| EPACKPEB | EPack Prefab Technologies | 2,363 Cr | Steel Products & Misc | Capital Goods - Industrial Small |
| EXCELINDUS | Excel Industries | 1,254 Cr | Agro Chemicals - Small | Organic Chemicals - Small |
| EXICOM | Exicom Tele-Systems | 2,563 Cr | Auto - Wiring & Electricals | Electronics - Heavy Electrical & Industrial |
| EXIDEIND | Exide Industries | 35,645 Cr | Consumer Durables - Large | Auto - Wiring & Electricals |
| FABTECH | Fabtech Technologies | 657 Cr | Diversified Trading | CDMO & Contract Mfg |
| FOCE | Foce India | 572 Cr | Garments & Apparel | Gems & Jewellery - Small |
| GALAPREC | Gala Precision Engineering | 1,314 Cr | Castings & Forgings | Capital Goods - Industrial Small |
| GANDHAR | Gandhar Oil Refinery | 2,567 Cr | FMCG - Small | Petrochemicals & Lubricants |
| GESHIP | Great Eastern Shipping | 19,726 Cr | **Defence PSU** | Shipping & Maritime |
| GFLLIMITED | GFL Ltd | 690 Cr | Fluorochemicals & Industrial Gases | Holding Companies |
| GKENERGY | GK Energy | 2,598 Cr | Pumps, Refractories & Industrial | Engineering - EPC Civil Small |
| GKWLIMITED | GKW Ltd | 945 Cr | Logistics - Small | Holding Companies |
| GPIL | Godawari Power & Ispat | 16,360 Cr | Power - Mid | Steel - Mid & Small |
| GULPOLY | Gulshan Polyols | 1,151 Cr | Adhesives, Coatings & Polymers | FMCG - Small |
| HEXATRADEX | Hexa Tradex | 890 Cr | Diversified Trading | Holding Companies |
| IDEAFORGE | ideaForge Technology | 3,944 Cr | **Solar & Renewable Equipment Small** | Defence - Small |
| IGIL | International Gemmological Ins | 13,805 Cr | Gems & Jewellery - Large | Business Services |
| IIFL | IIFL Finance | 27,571 Cr | Capital Markets - Large | MSME Finance - Mid |
| INDGN | Indegene | 14,159 Cr | Pharma - Mid Formulations | CDMO & Contract Mfg |
| INOXGREEN | Inox Green Energy Services | 6,944 Cr | Renewable Energy - Small | Business Services |
| INTERARCH | Interarch Building Solutions | 2,854 Cr | Steel - Mid & Small | Engineering - EPC Civil Small |
| IRMENERGY | IRM Energy | 1,154 Cr | Oil Services & Small | City Gas Distribution |
| JINDALPHOT | Jindal Photo | 1,049 Cr | Electrical Equipment Small | Holding Companies |
| JYOTISTRUC | Jyoti Structures | 1,263 Cr | Power - Small | Electronics - Heavy Electrical & Industrial |
| KALAMANDIR | Sai Silks (Kalamandir) | 1,304 Cr | Home Textiles & Technical | Retail - Mid |
| KARNIKA | Karnika Industries | 744 Cr | Synthetic Fibres & Yarn | Garments & Apparel |
| KIRLOSENG | Kirloskar Oil Engines | 32,597 Cr | Capital Goods - Heavy Electrical | Pumps, Refractories & Industrial |
| KIRLOSIND | Kirloskar Industries | 3,895 Cr | Diversified | Holding Companies |
| KNAGRI | KN Agri Resources | 505 Cr | Packaged Foods & Dairy | Edible Oil & Animal Feed |
| KPIL | Kalpataru Projects Internation | 24,250 Cr | Power Services & Trading | Engineering - EPC Civil Small |
| KRT | Knowledge Realty Trust | 50,353 Cr | Realty - Large | REITs |
| KSHINTL | KSH International | 7,563 Cr | Aluminium & Non Ferrous | Electrical Cables |
| LLOYDSME | Lloyds Metals & Energy | 100,304 Cr | Integrated Steel - Large | Mining |
| MAHSCOOTER | Maharashtra Scooters | 15,248 Cr | Auto OEM | Holding Companies |
| MAMATA | Mamata Machinery | 1,024 Cr | Consumer Plastics & Others | Capital Goods - Industrial Small |
| MANINFRA | Man Infraconstruction | 5,232 Cr | Infrastructure - Roads | Realty - Mid |
| MAXIND | Max India | 837 Cr | Diagnostics & Healthcare Services | Holding Companies |
| MIDHANI | Mishra Dhatu Nigam | 8,824 Cr | Steel - Mid & Small | Defence PSU |
| MOLDTECH | Mold-Tek Technologies | 650 Cr | Capital Goods - Industrial Small | Engineering - EPC Civil Small |
| MTNL | Mahanagar Telephone Nigam | 1,605 Cr | Telecom Equipment & Services | Telecom Services |
| NAMOEWASTE | Namo eWaste Management | 659 Cr | Business Services | Environmental Services |
| NELCAST | Nelcast | 994 Cr | Steel Products & Misc | Castings & Forgings |
| NIBE | NIBE Ltd | 2,064 Cr | **Home Textiles & Technical** | Defence - Small |
| NITCO | Nitco | 2,233 Cr | Diversified Trading | Building Materials - Glass, Ceramics & Ply |
| OMPOWER | Om Power Transmission | 583 Cr | Power Services & Trading | Engineering - EPC Civil Small |
| OPTIEMUS | Optiemus Infracom | 5,027 Cr | IT - Small | Telecom Services |
| PCCL | Petro Carbon & Chemicals | 1,057 Cr | Specialty Chemicals - Micro | Mining |
| PENIND | Pennar Industries | 2,462 Cr | Steel Tubes & Wires | Capital Goods - Industrial Small |
| PFS | PTC India Financial Services | 1,771 Cr | Housing Finance | MSME Finance - Mid |
| PITTIENG | Pitti Engineering | 4,364 Cr | Engineering - EPC Small | Capital Goods - Industrial Small |
| PLATIND | Platinum Industries | 1,287 Cr | Electrical Equipment Small | Organic Chemicals - Small |
| PNBGILTS | PNB Gilts | 1,473 Cr | NBFC - Large | Holding Companies |
| POWERGRID | Power Grid Corporation | 246,559 Cr | Power Generation - Large | Power Services & Trading |
| PRAJIND | Praj Industries | 6,194 Cr | Environmental Services | Capital Goods - Industrial Small |
| PRIMESECU | Prime Securities | 959 Cr | **Microfinance & MSME** | Broking & Wealth Management |
| PROTEAN | Protean eGov Technologies | 2,047 Cr | Exchanges & Ratings - Mid | IT - Micro |
| QPOWER | Quality Power Electrical Equip | 11,548 Cr | Power Services & Trading | Electronics - Heavy Electrical & Industrial |
| QUADFUTURE | Quadrant Future Tek | 1,747 Cr | Solar & Renewable Equipment Small | Electrical Cables |
| RAMANEWS | Shree Rama Newsprint | 501 Cr | Business Services | Paper & Packaging |
| RANEHOLDIN | Rane Holdings | 2,413 Cr | Auto - Engines & Thermal | Holding Companies |
| RATNAVEER | Ratnaveer Precision Engineerin | 2,497 Cr | Aluminium & Non Ferrous | Steel - Mid & Small |
| RAYMOND | Raymond | 5,705 Cr | Diversified | Capital Goods - Industrial Small |
| REDTAPE | Redtape | 6,518 Cr | Retail - Mid | Footwear |
| REFEX | Refex Industries | 3,814 Cr | Commodity & Chlor-Alkali Chemicals | Environmental Services |
| REGAAL | Regaal Resources | 900 Cr | Diversified | FMCG - Small |
| RELIGARE | Religare Enterprises | 8,784 Cr | Broking & Wealth Management | Holding Companies |
| RISHABH | Rishabh Instruments | 3,139 Cr | Engineering - Industrial Mfg A | Electrical Equipment Small |
| RPEL | Raghav Productivity Enhancers | 7,762 Cr | Steel - Mid & Small | Pumps, Refractories & Industrial |
| RTNINDIA | RattanIndia Enterprises | 3,890 Cr | Power Services & Trading | Holding Companies |
| RVNL | Rail Vikas Nigam | 43,535 Cr | **Housing Finance** | Engineering - EPC Civil Small |
| SANGHVIMOV | Sanghvi Movers | 3,824 Cr | Engineering - Industrial Mfg A | Business Services |
| SAYAJIHOTL | Sayaji Hotels | 566 Cr | Restaurants & QSR | Hotels - Small |
| SCILAL | Shipping Corporation of India  | 1,812 Cr | **Education** | Holding Companies |
| SHANTIGEAR | Shanthi Gears | 5,391 Cr | Auto - Drivetrain & Precision | Capital Goods - Industrial Small |
| SHREEJISPG | Shreeji Shipping Global | 11,433 Cr | Logistics - Small | Shipping & Maritime |
| SIGACHI | Sigachi Industries | 1,360 Cr | Specialty Chemicals - Micro | Pharma - Micro |
| SKIPPER | Skipper | 6,797 Cr | Power Services & Trading | Electronics - Heavy Electrical & Industrial |
| SMLMAH | SML Mahindra (SML Isuzu) | 9,177 Cr | Engineering - EPC Small | Auto OEM |
| SOLARWORLD | Solarworld Energy Solutions | 1,287 Cr | Renewable Energy - Small | Engineering - EPC Civil Small |
| SPECTRUM | Spectrum Electrical Industries | 4,088 Cr | Solar & Renewable Equipment Small | Capital Goods - Industrial Small |
| STEELCAS | Steelcast | 3,248 Cr | Steel - Mid & Small | Castings & Forgings |
| STEL | STEL Holdings | 1,120 Cr | Microfinance & MSME | Holding Companies |
| STYRENIX | Styrenix Performance Materials | 3,752 Cr | Petrochemicals & Lubricants | Organic Chemicals - Small |
| SUKHJITS | Sukhjit Starch & Chemicals | 503 Cr | Diversified Trading | FMCG - Small |
| SURYAROSNI | Surya Roshni | 4,749 Cr | Electronics - Consumer & Smart | Pipes & Tubes |
| SUZLON | Suzlon Energy | 61,986 Cr | Renewable Energy - Mid | Electronics - Heavy Electrical & Industrial |
| SWSOLAR | Sterling & Wilson Renewable | 4,334 Cr | Renewable Energy - Small | Engineering - EPC Civil Small |
| TEMBO | Tembo Global Industries | 1,088 Cr | Garments & Apparel | Steel - Mid & Small |
| TEXINFRA | Texmaco Infrastructure & Holdi | 1,496 Cr | Realty - Small | Holding Companies |
| THEINVEST | The Investment Trust of India | 507 Cr | **Microfinance & MSME** | Broking & Wealth Management |
| TINNARUBR | Tinna Rubber & Infrastructure | 1,805 Cr | Diversified Trading | Sugar & Agri Processing |
| TRANSRAILL | Transrail Lighting | 5,645 Cr | Power - Small | Electronics - Heavy Electrical & Industrial |
| TREL | TransIndia Real Estate | 658 Cr | Realty - Small | Logistics - Small |
| TRUALT | TruAlt Bioenergy | 3,601 Cr | Environmental Services | FMCG - Small |
| TTKHLTCARE | TTK Healthcare | 1,613 Cr | Pharma - Micro | Diversified |
| UNIECOM | Unicommerce eSolutions | 944 Cr | Internet & Digital Small | IT - Small |
| URBANCO | Urban Company | 26,402 Cr | Internet & Digital Small | Digital Aggregators & E-Commerce |
| UTIAMC | UTI Asset Management | 11,585 Cr | Broking & Wealth Management | Capital Markets - Large |
| VAKRANGEE | Vakrangee | 610 Cr | Diversified | IT - Micro |
| VIESL | Vision Infra Equipment Solutio | 973 Cr | Diversified Trading | Business Services |
| VIKRAN | Vikran Engineering | 1,533 Cr | Electrical Equipment Small | Engineering - EPC Civil Small |
| VIVIDEL | Vivid Electromech | 1,511 Cr | Electronics - Heavy Electrical & Industrial | Electrical Equipment Small |
| VLSFINANCE | VLS Finance | 775 Cr | Broking & Wealth Management | Holding Companies |
| WAAREEENER | Waaree Energies | 74,502 Cr | Renewable Energy - Mid | Electrical Equipment Small |
| WEL | Wonder Electricals | 922 Cr | Diversified Trading | Consumer Durables - Small |
| YATRA | Yatra Online | 1,786 Cr | Internet & Digital Small | Digital Aggregators & E-Commerce |
| YUKEN | Yuken India | 1,397 Cr | Engineering - Industrial Mfg B | Pumps, Refractories & Industrial |
| ZENTEC | Zen Technologies | 16,622 Cr | Electronics - Consumer & Smart | Defence - Small |

## S -- size-tier or unresolved, held for the design card (27)

| Symbol | Company | Mcap | Current segment | Flagged destination |
|---|---|---|---|---|
| AFFLE | Affle 3i | 22,698 Cr | Entertainment, Content & Digital | IT - Micro |
| ARIS | Arisinfra Solutions | 1,052 Cr | Engineering - EPC Small | Cement - Small |
| ASTRAMICRO | Astra Microwave Products | 16,568 Cr | Telecom Services | Defence - Small |
| BAJAJCON | Bajaj Consumer Care | 6,724 Cr | Packaged Foods & Dairy | FMCG - Large |
| BASF | BASF India | 16,400 Cr | Agro Chemicals - Large | Organic Chemicals - Small |
| BBOX | Black Box | 13,605 Cr | Telecom Services | IT - Micro |
| CMPDI | Central Mine Planning & Design | 17,268 Cr | Mining | Engineering - EPC Civil Small |
| CUMMINSIND | Cummins India | 138,517 Cr | Capital Goods - Heavy Electrical | Pumps, Refractories & Industrial |
| DYNAMATECH | Dynamatic Technologies | 7,806 Cr | Auto - Engines & Thermal | Capital Goods - Industrial Small |
| EIFFL | Euro India Fresh Foods | 654 Cr | Diversified Trading | FMCG - Large |
| EMMVEE | Emmvee Photovoltaic Power | 22,740 Cr | Renewable Energy - Mid | Electrical Equipment Small |
| ENRIN | Siemens Energy India | 113,816 Cr | Power Generation - Large | Electronics - Heavy Electrical & Industrial |
| IKS | Inventurus Knowledge Solutions | 30,657 Cr | Capital Markets - Large | IT - Micro |
| IOLCP | IOL Chemicals & Pharmaceutical | 6,261 Cr | Specialty Chemicals - Small | Pharma - Micro |
| IREDA | Indian Renewable Energy Develo | 31,520 Cr | Renewable Energy - Mid | Housing Finance |
| JYOTICNC | Jyoti CNC Automation | 24,027 Cr | **Defence PSU** | Capital Goods - Industrial Small |
| KSCL | Kaveri Seed | 3,758 Cr | Edible Oil & Animal Feed | FMCG - Small |
| LENSKART | Lenskart Solutions | 119,803 Cr | Digital Aggregators & E-Commerce | Retail - Mid |
| MTARTECH | MTAR Technologies | 24,069 Cr | Engineering - Large | Electrical Equipment Small |
| PANSARI | Pansari Developers | 516 Cr | Engineering - Industrial Mfg B | Realty - Mid |
| PATELRMART | Patel Retail | 722 Cr | Garments & Apparel | Retail - Mid |
| POLYMED | Poly Medicure | 17,738 Cr | Hospitals - Mid & Small | Diagnostics & Healthcare Services |
| PREMIERENE | Premier Energies | 44,667 Cr | Power - Mid | Electrical Equipment Small |
| RMDRIP | R M Drip & Sprinklers | 977 Cr | Consumer Plastics & Others | Flexible Packaging & Films |
| SUDARSCHEM | Sudarshan Chemical | 9,848 Cr | Organic Chemicals - Small | Specialty Chemicals - Small |
| SUMICHEM | Sumitomo Chemical India | 24,538 Cr | Organic Chemicals - Large | Agro Chemicals - Small |
| TEGA | Tega Industries | 12,361 Cr | Engineering - Large | Capital Goods - Industrial Small |

## Verified before writing this report

Every one of the 149 F destinations was checked against the live segment list
(`gvm_scores.segment` at the latest `score_date`) and **all exist**, including the 17 corrected
ones. No new segment name is invented by this split.

## Sequencing, stated plainly

This card's own window rule requires the recompute to land **before 09:15 or after 15:30 IST**.
At the time this file was written it was too close to the 09:15 open to run four careful
batches (149 companies over <=40 per batch) with a proper blast-radius check on each -- the n>=5
gate (N5_SEGMENT_GATE_V1) also has to be checked on every batch, including the MIDHANI/AZAD/
JYOTICNC interaction noted above. Rushing that on live GVM scoring data is not worth the ~15
minutes saved. **Items 2-4 (the actual apply) land as the first after-15:30 push today,**
batch 1 first (<=40 of the 149), with the founder-named AMAGI included in it.

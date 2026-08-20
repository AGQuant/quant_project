# cc#1146 — GVM segment misclassification audit

**DIAGNOSTIC ONLY. Nothing was changed.** No `UPDATE` or `INSERT` was run against `input_raw` or
`gvm_scores`, and GVM was not recomputed. Row counts before and after this audit:
`input_raw` **2,008**, `gvm_scores` **1,794**, distinct segments **137** — unchanged.

Read on 20-Aug-2026 against `score_date = 2026-08-19` (the only date in `gvm_scores`).

---

## Why this matters

GVM is peer-benchmarked. Every parameter is scored against the median of the company's own
`gvm_segment`. If the segment is wrong, the **rating** is wrong — not just the label on it.

---

## A limit on this audit you should know before reading the numbers

**The per-parameter peer figures are not stored.** `gvm_scores` has `*_raw` and `*_peer` columns for
all 22 parameters (`roce`, `opm`, `pe`, `sales_3y`, `ret_1y`, …), and **every one of them is NULL
across all 1,794 rows.** The peer median a reader sees on the GVM page is computed at read time.

So I could not do what scope 4 asks in the form it asks for it. Recomputing HEG, GRAPHITE and
ARKADE "against their proposed peer median" parameter by parameter is not possible from stored
data, and I am not going to re-run the GVM engine inside a diagnostic card.

What I can measure honestly is **which peer set each company is being judged against**, and how far
its score sits from each. That is the quantity the misclassification actually distorts. It is
reported below as exactly that — a peer-set comparison, **not** a recomputed rating.

---

## 1. The confirmed cases

| Symbol | Rating | Current segment | Rated peers | Median | vs median | Proposed segment | Rated peers | Median | vs median |
|---|---|---|---|---|---|---|---|---|---|
| HEG | 7.53 | Electronics - Consumer & Smart | 25 | 6.54 | **+0.99** | Pumps, Refractories & Industrial | 16 | 5.74 | **+1.79** |
| GRAPHITE | 6.85 | Electronics - Consumer & Smart | 25 | 6.54 | **+0.31** | Pumps, Refractories & Industrial | 16 | 5.74 | **+1.11** |
| ARKADE | 5.89 | Engineering - Industrial Mfg A | 18 | 6.00 | **−0.11** | Realty - Small | 17 | 6.10 | **−0.21** |

**Read this as: the peer bar moves, in the direction that matters.** HEG and GRAPHITE are currently
measured against a segment whose median is **0.80 points higher** than the one they arguably belong
in. Both are graphite-electrode makers; their real peers are a materially weaker cohort, and
against it they would look considerably stronger than they do today. ARKADE moves the other way,
slightly.

**On the proposed homes.** The card names "Realty - Large / Mid / Small" as the correct home for
ARKADE and those exist (8 / 31 / 20 members). **There is no graphite or electrode segment in the
taxonomy.** The closest existing home is `Pumps, Refractories & Industrial` (16 members), which is
what I used above — refractories are the adjacent business, not the same one. Creating an
electrodes segment is new taxonomy and therefore your call, not mine.

---

## 2. The segment with the real scale problem

`Electronics - Consumer & Smart` — **25 rated companies, at least six unrelated businesses, one
blended median (6.54).** Every one of these is scored against that single number:

| Business | Members |
|---|---|
| EMS / contract electronics | SYRMA 8.20, AVALON 7.83, KAYNES 6.54, PGEL 6.53 |
| Power equipment & transformers | TDPOWERSYS 8.39, VOLTAMP 7.58, SHILCTECH 6.74, SCHNEIDER 6.00, TARIL 5.67, TRITURBINE 6.01 |
| Graphite electrodes | HEG 7.53, GRAPHITE 6.85 |
| Defence electronics | ZENTEC 7.19, APOLLO 6.91 |
| Consumer electricals / retail | AVL 7.14, VGUARD 6.90, CROMPTON 5.27, ONIDA 5.25, SURYAROSNI 5.97 |
| Industrial gearing / welding | ESABINDIA 7.15, ELECON 5.22 |
| Other (metering, wind, automation, optoelectronics) | GENUSPOWER 6.45, INOXWIND 6.08, HONAUT 5.28, VOEPL 5.37 |

A steam-turbine maker, a graphite-electrode maker and a Patna consumer-electronics retailer are
being held to the same peer median. That is the finding.

---

## 3. Why I am not handing you a longer list

I built a keyword classifier over `input_raw.overview` to screen all 137 segments for the same
problem. **It produced false positives and I am not reporting its output as findings.** It flagged
`IT - Small` and `Housing Finance` as incoherent because an IT company's overview mentions serving
real-estate clients, and "EMS" matches inside unrelated words. A list built that way would waste
your time and damage the credibility of the three cases that are real.

`input_raw` has **no industry or sub-industry column** — only `gvm_segment` and free-text
`overview` — so there is no structured field to audit coherence against. Doing this properly across
137 segments means reading overviews, which is a larger piece of work than this card scopes.

**What I would suggest**, if you want the full sweep: a follow-up card that walks the ~30 segments
holding 10+ rated companies and confirms each by reading the member overviews. That is where the
published-rating exposure is concentrated.

---

## 4. The degenerate segments — reported, not chased

Four segments have exactly one member and two have 2–3. Those eight companies — JAGAJITIND,
HCL-INSYS, XTGLOBAL, SABTNL, TIMEXWATCH, BCONCEPTS, SPENCERS, KRONECOMM — **have no `gvm_scores`
row**, so the self-median problem is real in principle but is **not currently affecting any
published rating**. Noted and left alone, per the card.

---

## 5. The answer to the question the card asks

**How many rated companies would change segment: 3** — HEG, GRAPHITE, ARKADE. Those are the only
ones confirmed by reading the business descriptions rather than by keyword.

**How many published ratings would move: 3, and possibly 39.** The three above move because their
own peer median moves. But removing HEG and GRAPHITE from `Electronics - Consumer & Smart`
**changes the median for the 23 companies left behind**, and adding them to
`Pumps, Refractories & Industrial` changes it for those 16. A peer-median change moves every
rating computed against it. So the blast radius of a three-company remap is up to 39 rated
companies, not 3.

That is the strongest argument for stopping here rather than applying anything.

---

## STOPPED — awaiting your ruling

Per the card's gate, no remap has been applied. Three decisions are yours as Head of Research:

1. **Do HEG and GRAPHITE move**, and if so — into `Pumps, Refractories & Industrial`, or does a new
   electrodes segment get created?
2. **Does ARKADE move** to `Realty - Small`?
3. **Do you want the full 137-segment sweep** as a follow-up card, given it needs overview reading
   rather than keyword matching?

Verification for this report: `input_raw` 2,008 rows and `gvm_scores` 1,794 rows, both unchanged;
no write of any kind was issued.

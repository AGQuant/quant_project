# cc#1859 build-order step 2 — bucket skew, the 60-day floor, and the coverage report

Read-only. Nothing wired, nothing shipped. Companion to
`reports/CC1859_atm_iv_definition.md` (sha 63208bd), which measured the call/put IV asymmetry that
runs through everything below.

Method, stated so it can be re-run: `option_iv_daily`, `iv > 0.005`, `spot > 0`, `DTE ≥ 1` (expiry
day excluded). Per symbol/date/expiry, strikes ranked by `|strike − spot|` and the nearest **11**
kept — that is the ATM±5 band the card actually tags, and measuring anything wider would report
coverage the product will never use. Moneyness rounded to whole percent. CE counted above spot, PE
below. Expiry class: `wk` = DTE ≤ 10, `mo` = DTE > 10. A bucket is **usable at 60+ distinct
trade dates**, the floor the card proposes.

---

## 1 — THE ANCHOR CHANGES THE SIGN OF THE SKEW. This governs the whole build.

The first companion report measured a systematic gap between the ATM call's IV and the ATM put's IV
— median +3.5 vol points, the put higher, on 97.7% of 50,670 rows. Here is what that does to a
bucket skew, on NIFTY weeklies, ATM±5, over 197 sessions:

| leg | moneyness | median skew measured **within the leg** (vs the ATM **call** for calls, ATM **put** for puts) | median skew measured **against a blended ATM** |
|---|---|---|---|
| CE | 0 (ATM) | **0.0000** | −0.0266 |
| CE | +1% | **+0.0194** | −0.0241 |
| PE | −1% | **−0.0031** | +0.0242 |
| PE | 0 (ATM) | **0.0000** | +0.0301 |

**The 1%-OTM NIFTY call is +1.94 vol points RICHER than the ATM call. Against a blended anchor the
same strike, same data, reads 2.41 points CHEAPER.** The sign flips. Nothing about the market
changed — only the choice of anchor.

The give-away is the **ATM bucket itself**. Measured within its own leg it is `0.0000` on both
sides, which is what an ATM bucket must be by construction. Measured against a blended anchor it
reads −2.66 on the call side and +3.01 on the put side — **symmetric about zero, equal and
opposite**, which is precisely the signature the identical-vega argument in the first report
predicts. It is the artefact, not a skew.

### This is the RV20 failure wearing a new coat

cc#1847 was blocked because RV20 made 91% of clean strikes read EXPENSIVE *by construction* — a tag
that says the same thing every day carries no information. A blended anchor does the same thing
here, only split by side: **every call bucket inherits about −2.5 points and every put bucket about
+2.5 points, every day, forever.** Calls would read permanently cheap and puts permanently rich.
The card exists to stop exactly that, and it would have been invisible.

### It affects the numbers already written into this card

The card's `evidence_it_carries_signal` block reports NIFTY weekly bucket skews of **1% OTM call
−1.67, 2% OTM call −0.98, 1% OTM put +0.70, 2% OTM put +1.73** — call side negative, put side
positive. That is the exact shape of the blended-anchor column above, and the opposite sign to the
within-leg measurement on the call wing. I am not asserting which anchor produced them; I am
stating that the two measurements disagree in sign and that this needs settling before the bucket
skew is wired, because the whole per-strike fair value is built on it.

**Recommendation: measure every bucket skew WITHIN ITS OWN LEG.** Anchor a call bucket on the ATM
call and a put bucket on the ATM put. It forces the ATM bucket to zero (a free sanity check on
every run) and it is immune to whatever is causing the call/put asymmetry, whether that turns out
to be a solver error or real market structure.

---

## 2 — Coverage against the 60-day floor, ATM±5 only

| class | expiry | buckets | usable at 60+ dates | % | symbols | observations |
|---|---|---|---|---|---|---|
| **INDEX** | weekly | 20 | **20** | **100%** | 5 | 7,576 |
| **INDEX** | monthly | 16 | **16** | **100%** | 4 | 11,443 |
| STOCK | monthly | 5,216 | 2,727 | **52.3%** | 230 | 423,721 |
| STOCK | weekly | 4,951 | 708 | **14.3%** | 227 | 166,981 |

**Index confirms Fable exactly in conclusion — 100% usable on both expiry classes.** Every index
ATM±5 strike can be tagged.

**Stocks come out materially thinner than the card's stated figures.** The card records Fable's
measurement as stock monthly **83%** usable and stock weekly **25%**; restricted to ATM±5 with the
method above I get **52.3%** and **14.3%**. The difference is almost certainly the bucket universe:
without the ATM±5 restriction my own unrestricted run gives 10,155 stock-monthly buckets against
the card's 4,133, so the two runs are not counting the same set. I have not reverse-engineered
Fable's filter. **What matters for the build is coverage over the strikes that will actually be
tagged, and that is the table above.**

### The per-symbol view, which is what the card asked for

| expiry | stock symbols | **zero** usable wing buckets | partially covered | fully covered | median usable buckets per symbol (of ~22) |
|---|---|---|---|---|---|
| monthly | 230 | 21 | 202 | **7** | **12** |
| weekly | 227 | **113** | 114 | **0** | **1** |

Read plainly:

- **Index, both expiries — tag the whole ATM±5 band.** No caveat needed.
- **Stock monthly — expect about half the wing strikes to carry a tag.** The median symbol clears
  the floor on 12 of roughly 22 buckets, only 7 symbols of 230 clear all of them, and 21 symbols
  clear none. A visibly patchy chain is the correct output here, not a bug, and the surface should
  say why rather than leave a gap the user has to interpret.
- **Stock weekly — the wings are not taggable.** Half the symbols (113 of 227) clear the floor on
  **no** wing bucket at all, none clears them all, and the median symbol clears **one**. Ship the
  ATM tag only and say the wing is not covered yet, exactly as the card's G2 requires. Do not lower
  the floor to make tags appear.

### The index caveat the card raised is confirmed

Index strikes are dense — 50-point steps on NIFTY — so ATM±5 spans only about ±1% of spot. Every
index bucket in this measurement sits at moneyness 0 or ±1. There is no index history at all
between 3% and 8% OTM, so **index coverage is 100% of the band we tag and 0% of anything wider.**
If the band is ever widened beyond ATM±5 on an index, it will have no history behind it and the
surface must say so.

One gap worth naming: **NIFTY has no usable monthly bucket in this measurement** — its monthly
rows in the ATM±5 band do not reach the 60-date floor, while BANKNIFTY's do (170 dates). The index
monthly row above is carried by the other three symbols.

---

## 3 — The bucket skews themselves, measured within-leg

NIFTY weekly, ATM±5, 197 sessions, anchored on the ATM call for calls and the ATM put for puts:

| leg | moneyness | dates | median skew | p25 | p75 |
|---|---|---|---|---|---|
| CE | 0 (ATM) | 197 | 0.0000 | — | — |
| CE | +1% | 140 | **+0.0194** | | |
| PE | 0 (ATM) | 197 | 0.0000 | — | — |
| PE | −1% | 101 | **−0.0031** | | |

On NIFTY weeklies the **call wing carries the smile** (+1.94 points at 1% OTM) and the **put wing
is flat within its own leg** (−0.31 points). Against a blended anchor that reads as the exact
opposite on both sides, which is the point of section 1.

BANKNIFTY monthly (170 dates) and weekly (67 dates) both clear the floor on all four of their
ATM±5 buckets.

---

## 4 — What this step delivers and what it does not

Delivered: the coverage table, the per-symbol counts, the 60-day floor confirmed as the right
floor, and the finding that the skew anchor decides the sign of the answer.

Not delivered, and deliberately: no per-strike fair value, no tag, no surface, nothing wired. Two
things must be ruled first, and they are the same question asked twice —

1. **which leg defines the ATM fair IV** (first report — I recommend the CE/PE average, on the
   identical-vega argument), and
2. **which anchor defines a bucket skew** (this report — I recommend within-leg, on the evidence
   above).

They are not the same answer, and that is not a contradiction. The **level** wants the average,
because averaging cancels a forward error. The **shape** wants within-leg, because a blended anchor
injects that same error back in with opposite signs on the two wings. Level from the blend, shape
from the leg.

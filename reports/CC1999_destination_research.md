# cc#1999 item 1 — candidate destinations for the two sub-5 GVM segments

Read-only. No `input_raw` write in this file. Per session_log 44021 (N5_SEGMENT_GATE_V1), item 1:
"list every segment whose name shares the industry ... with member counts. Pick the nearest by
business, state why in one line."

## Plastics and Packaging (n=3): HITECHCORP, KANPRPLA, KNACK

Segments sharing the industry, with member counts (`gvm_scores`, latest `score_date`):

| Segment | n | Composition |
|---|---|---|
| Paper & Packaging | 10 | Paper mills packaging (ANDHRAPAP, JKPAPER, TNPL) — paper-based, not plastic |
| **Flexible Packaging & Films** | **15** | Plastic/film packaging makers (AGI, EPL, JINDALPOLY, POLYPLEX, UFLEX) |
| Consumer Plastics & Others | 17 | Mixed plastics catch-all (machinery, irrigation, cylinders — not packaging-specific) |

**Recommendation: `Flexible Packaging & Films` for all three.** All three carry Screener's own
`Industry` tag of exactly **"Packaging"** (industry_group: Industrial Products) — none carries a
paper-industry tag, ruling out Paper & Packaging. Flexible Packaging & Films is the segment built
specifically around plastic packaging materials makers (the same business as HITECHCORP, KANPRPLA
and KNACK), rather than the broader Consumer Plastics & Others catch-all, which mixes packaging
in with unrelated plastics processors (irrigation systems, cylinders, machinery).

## Textiles Smallcap (n=4): LAKSHMIMIL, NAHARINDUS, ORBTEXP, PASUPTAC

Segments sharing the industry:

| Segment | n | Composition |
|---|---|---|
| Textiles - Large | 9 | Large diversified/branded names (ALOKINDS, ARVINDFASN, TRIDENT, KPRMILL, WELSPUNLIV) |
| Home Textiles & Technical | 15 | Mixed home-textile / technical-fabric makers (BOMDYEING, FILATEX, SANATHAN, NITINSPIN) |
| **Synthetic Fibres & Yarn** | **25** | Overwhelmingly SPINNING mills (RSWM, PRECOT, SUTLEJTEX, AMBIKCO, **NAHARSPING**) |
| Garments & Apparel | 6 | Finished apparel/garment retailers and makers |

**PASUPTAC — already resolved, high confidence.** Makes acrylic fibre (cc#1985 review, sha
`152f561`). `Synthetic Fibres & Yarn` names the business exactly and already holds the fibre/yarn
peer set. **-> Synthetic Fibres & Yarn.**

**NAHARINDUS (Nahar Industrial Enterprises) — high confidence.** `Synthetic Fibres & Yarn`
already holds `NAHARSPING` (Nahar Spinning Mills) — the same Nahar Group, same cotton-spinning
business. A sister company already correctly placed in this segment is about as strong a signal
as this database can give without a description field. **-> Synthetic Fibres & Yarn.**

**LAKSHMIMIL (Lakshmi Mills Company) — medium-high confidence.** A cotton spinning mill by name
and by Screener's own tag (`Other Textile Products`, `Textiles & Apparels` group). The segment's
membership (RSWM, PRECOT, SUTLEJTEX, AMBIKCO) is dominated by exactly this business — cotton/yarn
spinners, not finished-garment or home-textile makers. **-> Synthetic Fibres & Yarn.**

**ORBTEXP (Orbit Exports) — lower confidence, named as such rather than asserted.** Known
primarily as an embroidered-fabric **exporter** (finished/processed fabric for export), which is
a different business from raw spinning — it is closer to a fabric-processing/technical-textile
operation than a yarn spinner. This database carries no free-text business description (confirmed
in cc#1859: `screener_raw` holds only structured fields), so this reads on the company name and
Screener's industry tag alone, not a verified business summary. **Proposed: `Home Textiles &
Technical`** (the segment built for fabric processors and technical/home-textile makers, holding
similarly-sized names like FILATEX, SANATHAN) — **but flagged as the one candidate of the seven
worth a second look before applying**, unlike the other six.

## Summary for item 2 (the actual moves, not applied by this file)

| Symbol | -> Destination | Confidence |
|---|---|---|
| HITECHCORP | Flexible Packaging & Films | high |
| KANPRPLA | Flexible Packaging & Films | high |
| KNACK | Flexible Packaging & Films | high |
| PASUPTAC | Synthetic Fibres & Yarn | high (already resolved in cc#1985) |
| NAHARINDUS | Synthetic Fibres & Yarn | high (sister-company signal) |
| LAKSHMIMIL | Synthetic Fibres & Yarn | medium-high |
| ORBTEXP | Home Textiles & Technical | medium — worth a second look |

All seven destination segments verified to exist live in `gvm_scores.segment`. Both source
segments (Plastics and Packaging, Textiles Smallcap) go to **zero members** once all seven move —
per the card, the two segment NAMES retire (item 4), not merely thin out.

Item 2 (the writes + recompute) and item 3 (the `gvm_nightly` n>=5 guard, pure code) are next,
sequenced into the after-15:30 window with cc#1996 batch 1, for the same reason stated there:
the recompute must land before 09:15 or after 15:30 IST, and it is now too close to the open to
do it carefully.
